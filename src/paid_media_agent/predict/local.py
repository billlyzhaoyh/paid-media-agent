"""A local predictor: a naive expectation times the spread its own history shows.

The caller supplies a `naive` feature column, the value it would expect with nothing unusual (for
example the trailing median, adjusted for a budget change). This learns how far real values land
from that expectation on the training rows and returns those quantiles for each test row. Nothing
leaves the machine and nothing is billed.
"""

from __future__ import annotations

import numpy as np

from paid_media_agent.predict.protocol import (
    Prediction,
    PredictionRequest,
    PredictorUnavailable,
)

NAIVE_COLUMN = "naive"
MIN_TRAIN_ROWS = 20


class LocalPredictor:
    name = "local"
    model_version = "empirical-band/1"

    async def estimate_tokens(self, request: PredictionRequest) -> int:  # noqa: ARG002
        return 0

    async def predict(self, request: PredictionRequest) -> Prediction:
        if NAIVE_COLUMN not in request.columns:
            raise PredictorUnavailable("the local predictor needs a 'naive' feature column")
        col = request.columns.index(NAIVE_COLUMN)
        naive_train = request.x_train[:, col].astype(np.float64)
        naive_test = request.x_test[:, col].astype(np.float64)
        y = request.y_train.astype(np.float64)
        usable = np.isfinite(naive_train) & np.isfinite(y)
        if request.kind == "amount":
            usable &= naive_train > 0
        if int(usable.sum()) < MIN_TRAIN_ROWS:
            raise PredictorUnavailable(
                f"{int(usable.sum())} usable training rows; the local band needs {MIN_TRAIN_ROWS}"
            )
        levels = np.asarray(request.quantiles, dtype=np.float64)
        base = np.where(np.isfinite(naive_test), naive_test, np.nan)
        if request.kind == "amount":
            ratios = y[usable] / naive_train[usable]
            spread = np.quantile(ratios, levels)
            values = np.outer(spread, base)
        else:
            scale_train = np.sqrt(naive_train[usable] + 1.0)
            residuals = (y[usable] - naive_train[usable]) / scale_train
            spread = np.quantile(residuals, levels)
            values = base[None, :] + np.outer(spread, np.sqrt(np.clip(base, 0, None) + 1.0))
        return Prediction(
            quantiles=request.quantiles,
            values=np.clip(values, 0.0, None),
            provider=self.name,
            model_version=self.model_version,
        )
