"""Opt-in: one real TabPFN call through the guard, on synthetic numbers only.

Set PAID_MEDIA_LIVE_TESTS=1 and TABPFN_TOKEN. It bills one call (10,000 tokens).
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from paid_media_agent.predict.budget import GuardedPredictor
from paid_media_agent.predict.protocol import PredictionRequest
from paid_media_agent.predict.tabpfn import TabPFNPredictor
from paid_media_agent.store import Store

# Read at import: the test environment fixture removes settings variables before each test.
TOKEN = os.environ.get("TABPFN_TOKEN", "")
pytestmark = pytest.mark.skipif(
    os.environ.get("PAID_MEDIA_LIVE_TESTS") != "1" or not TOKEN,
    reason="set PAID_MEDIA_LIVE_TESTS=1 and TABPFN_TOKEN to call TabPFN",
)


async def test_live_tabpfn_returns_ordered_quantiles_and_is_logged() -> None:
    rng = np.random.default_rng(0)
    x = rng.uniform(50, 500, size=(80, 1))
    y = x[:, 0] * rng.lognormal(0, 0.1, size=80)
    store = Store()
    predictor = GuardedPredictor(
        TabPFNPredictor(TOKEN), store, daily_tokens=100_000, monthly_tokens=100_000
    )
    request = PredictionRequest(
        purpose="live-test",
        columns=("naive",),
        x_train=x[:60],
        y_train=y[:60],
        x_test=x[60:],
        quantiles=(0.05, 0.5, 0.95),
    )
    prediction = await predictor.predict(request)

    lo, mid, hi = prediction.at(0.05), prediction.at(0.5), prediction.at(0.95)
    assert prediction.values.shape == (3, 20) and (lo <= mid).all() and (mid <= hi).all()
    assert float(np.mean((y[60:] >= lo) & (y[60:] <= hi))) >= 0.7
    assert store.fetch("SELECT provider, status, tokens_estimated FROM predictor_calls") == [
        ("tabpfn", "ok", 10_000)
    ]
    assert "3.5" in prediction.model_version or "v3" in prediction.model_version
