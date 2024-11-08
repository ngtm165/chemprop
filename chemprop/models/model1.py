from __future__ import annotations

from typing import Iterable
import warnings

from lightning import pytorch as pl
import torch
from torch import Tensor, distributed, nn, optim
from torch_geometric.nn import GINConv
from torch_geometric.nn import MLP  # Multi-layer perceptron dùng trong GINConv

from chemprop.data import BatchMolGraph, TrainingBatch
from chemprop.nn import Aggregation, LossFunction, Predictor
from chemprop.nn.metrics import Metric
from chemprop.nn.transforms import ScaleTransform
from chemprop.schedulers import build_NoamLike_LRSched

class GIN(pl.LightningModule):
    r"""An :class:`GIN` is a sequence of GINConv layers, an aggregation routine, and a predictor routine.

    The first two modules calculate learned fingerprints from an input molecule
    reaction graph, and the final module takes these learned fingerprints as input to calculate a
    final prediction. I.e., the following operation:

    .. math::
        \mathtt{GIN}(\mathcal{G}) =
            \mathtt{predictor}(\mathtt{agg}(\mathtt{GINConv}(\mathcal{G})))

    The full model is trained end-to-end.
    """

    def __init__(
        self,
        hidden_dim: int,  # Kích thước của các lớp ẩn trong GINConv
        num_features: int,  # Số lượng đặc trưng của các đỉnh trong đồ thị
        num_classes: int,  # Số lớp phân loại hoặc số lượng đầu ra
        agg: Aggregation,
        predictor: Predictor,
        batch_norm: bool = True,
        metrics: Iterable[Metric] | None = None,
        warmup_epochs: int = 2,
        init_lr: float = 1e-4,
        max_lr: float = 1e-3,
        final_lr: float = 1e-4,
        X_d_transform: ScaleTransform | None = None,
    ):
        super().__init__()
        self.save_hyperparameters(ignore=["X_d_transform", "agg", "predictor"])

        self.hparams.update(
            {
                "agg": agg.hparams,
                "predictor": predictor.hparams,
            }
        )

        # Tạo lớp GINConv sử dụng MLP để cập nhật trọng số
        mlp = MLP([num_features, hidden_dim, hidden_dim])  # MLP để xử lý thông tin cho GINConv
        self.message_passing = GINConv(mlp)  # GINConv thực hiện message passing

        self.agg = agg
        self.bn = nn.BatchNorm1d(hidden_dim) if batch_norm else nn.Identity()
        self.predictor = predictor

        self.X_d_transform = X_d_transform if X_d_transform is not None else nn.Identity()

        self.metrics = (
            [*metrics, self.criterion]
            if metrics
            else [self.predictor._T_default_metric(), self.criterion]
        )

        self.warmup_epochs = warmup_epochs
        self.init_lr = init_lr
        self.max_lr = max_lr
        self.final_lr = final_lr

    @property
    def output_dim(self) -> int:
        return self.predictor.output_dim

    @property
    def n_tasks(self) -> int:
        return self.predictor.n_tasks

    @property
    def n_targets(self) -> int:
        return self.predictor.n_targets

    @property
    def criterion(self) -> LossFunction:
        return self.predictor.criterion

    def fingerprint(
        self, bmg: BatchMolGraph, V_d: Tensor | None = None, X_d: Tensor | None = None
    ) -> Tensor:
        """The learned fingerprints for the input molecules"""
        H_v = self.message_passing(bmg.x, bmg.edge_index)  # Lấy đặc trưng của các đỉnh và cạnh
        H = self.agg(H_v, bmg.batch)
        H = self.bn(H)

        return H if X_d is None else torch.cat((H, self.X_d_transform(X_d)), 1)

    def encoding(
        self, bmg: BatchMolGraph, V_d: Tensor | None = None, X_d: Tensor | None = None, i: int = -1
    ) -> Tensor:
        """Calculate the :attr:`i`-th hidden representation"""
        return self.predictor.encode(self.fingerprint(bmg, V_d, X_d), i)

    def forward(
        self, bmg: BatchMolGraph, V_d: Tensor | None = None, X_d: Tensor | None = None
    ) -> Tensor:
        """Generate predictions for the input molecules/reactions"""
        return self.predictor(self.fingerprint(bmg, V_d, X_d))

    def training_step(self, batch: TrainingBatch, batch_idx):
        bmg, V_d, X_d, targets, weights, lt_mask, gt_mask = batch

        mask = targets.isfinite()
        targets = targets.nan_to_num(nan=0.0)

        Z = self.fingerprint(bmg, V_d, X_d)
        preds = self.predictor.train_step(Z)
        l = self.criterion(preds, targets, mask, weights, lt_mask, gt_mask)

        self.log("train_loss", l, prog_bar=True)

        return l

    def on_validation_model_eval(self) -> None:
        self.eval()
        self.predictor.output_transform.train()

    def validation_step(self, batch: TrainingBatch, batch_idx: int = 0):
        losses = self._evaluate_batch(batch)
        metric2loss = {f"val/{m.alias}": l for m, l in zip(self.metrics, losses)}

        self.log_dict(metric2loss, batch_size=len(batch[0]))
        self.log(
            "val_loss",
            losses[0],
            batch_size=len(batch[0]),
            prog_bar=True,
            sync_dist=distributed.is_initialized(),
        )

    def test_step(self, batch: TrainingBatch, batch_idx: int = 0):
        losses = self._evaluate_batch(batch)
        metric2loss = {f"batch_averaged_test/{m.alias}": l for m, l in zip(self.metrics, losses)}

        self.log_dict(metric2loss, batch_size=len(batch[0]))

    def _evaluate_batch(self, batch) -> list[Tensor]:
        bmg, V_d, X_d, targets, _, lt_mask, gt_mask = batch

        mask = targets.isfinite()
        targets = targets.nan_to_num(nan=0.0)
        preds = self(bmg, V_d, X_d)
        weights = torch.ones_like(targets)

        if self.predictor.n_targets > 1:
            preds = preds[..., 0]

        return [
            metric(preds, targets, mask, weights, lt_mask, gt_mask) for metric in self.metrics[:-1]
        ]

    def predict_step(self, batch: TrainingBatch, batch_idx: int, dataloader_idx: int = 0) -> Tensor:
        """Return the predictions of the input batch"""
        bmg, X_vd, X_d, *_ = batch
        return self(bmg, X_vd, X_d)

    def configure_optimizers(self):
        opt = optim.Adam(self.parameters(), self.init_lr)
        if self.trainer.train_dataloader is None:
            self.trainer.estimated_stepping_batches
        steps_per_epoch = self.trainer.num_training_batches
        warmup_steps = self.warmup_epochs * steps_per_epoch
        if self.trainer.max_epochs == -1:
            warnings.warn(
                "For infinite training, the number of cooldown epochs in learning rate scheduler is set to 100 times the number of warmup epochs."
            )
            cooldown_steps = 100 * warmup_steps
        else:
            cooldown_epochs = self.trainer.max_epochs - self.warmup_epochs
            cooldown_steps = cooldown_epochs * steps_per_epoch

        lr_sched = build_NoamLike_LRSched(
            opt, warmup_steps, cooldown_steps, self.init_lr, self.max_lr, self.final_lr
        )

        lr_sched_config = {"scheduler": lr_sched, "interval": "step"}

        return {"optimizer": opt, "lr_scheduler": lr_sched_config}
