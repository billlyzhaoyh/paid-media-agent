"""The predictor contract: a tabular regression with quantile outputs, fit and predicted in one call.

Callers build numeric feature matrices (NaN for missing) and ask for quantiles of the target for
each test row. A predictor that cannot answer raises `PredictorUnavailable`; callers then fall back
to a labelled rule instead of failing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, Protocol

import numpy as np

TargetKind = Literal["amount", "count"]


class PredictorUnavailable(RuntimeError):
    """No prediction this time: not configured, over budget, too little data, or provider error."""


@dataclass(frozen=True)
class PredictionRequest:
    purpose: str
    columns: tuple[str, ...]
    x_train: np.ndarray
    y_train: np.ndarray
    x_test: np.ndarray
    quantiles: tuple[float, ...]
    kind: TargetKind = "amount"
    """Amounts (money) vary in proportion to their level; counts vary like Poisson counts."""

    def __post_init__(self) -> None:
        width = len(self.columns)
        if self.x_train.ndim != 2 or self.x_train.shape[1] != width:
            raise ValueError("x_train must have one column per name")
        if self.x_test.ndim != 2 or self.x_test.shape[1] != width:
            raise ValueError("x_test must have one column per name")
        if len(self.y_train) != len(self.x_train):
            raise ValueError("y_train must have one value per training row")
        if not self.quantiles or any(not 0 < q < 1 for q in self.quantiles):
            raise ValueError("quantiles must be between 0 and 1")

    @property
    def n_train(self) -> int:
        return int(self.x_train.shape[0])

    @property
    def n_test(self) -> int:
        return int(self.x_test.shape[0])

    def digest(self) -> str:
        """Identifies the exact request, so a repeated one is answered from the cache."""
        h = hashlib.sha256()
        h.update(json.dumps([self.purpose, self.columns, self.quantiles, self.kind]).encode())
        for array in (self.x_train, self.y_train, self.x_test):
            h.update(np.ascontiguousarray(array, dtype=np.float64).tobytes())
        return h.hexdigest()


@dataclass(frozen=True)
class Prediction:
    quantiles: tuple[float, ...]
    values: np.ndarray
    """Shape (len(quantiles), n_test): row i holds quantile `quantiles[i]` for every test row."""
    provider: str
    model_version: str

    def at(self, quantile: float) -> np.ndarray:
        return np.asarray(self.values[self.quantiles.index(quantile)], dtype=np.float64)


class Predictor(Protocol):
    name: str

    async def estimate_tokens(self, request: PredictionRequest) -> int:
        """Provider tokens the call will bill; 0 for local predictors."""
        ...

    async def predict(self, request: PredictionRequest) -> Prediction: ...
