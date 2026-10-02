"""Predictors: the local band, the cache and token guard, and TabPFN's REST contract (mocked)."""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import numpy as np
import pytest

from paid_media_agent.predict.budget import GuardedPredictor
from paid_media_agent.predict.local import LocalPredictor
from paid_media_agent.predict.protocol import (
    Prediction,
    PredictionRequest,
    PredictorUnavailable,
)
from paid_media_agent.predict.tabpfn import TabPFNPredictor
from paid_media_agent.store import Store

Q = (0.025, 0.5, 0.975)


def _request(
    n_train: int = 200, n_test: int = 50, kind: str = "amount", seed: int = 0
) -> tuple[PredictionRequest, np.ndarray]:
    rng = np.random.default_rng(seed)
    naive = rng.uniform(50, 500, size=n_train + n_test)
    noise = rng.lognormal(0, 0.1, size=n_train + n_test)
    if kind == "count":
        naive = rng.uniform(2, 30, size=n_train + n_test)
        y = rng.poisson(naive).astype(float)
    else:
        y = naive * noise
    x = np.column_stack([np.arange(n_train + n_test, dtype=float), naive])
    return (
        PredictionRequest(
            purpose="test",
            columns=("t", "naive"),
            x_train=x[:n_train],
            y_train=y[:n_train],
            x_test=x[n_train:],
            quantiles=Q,
            kind=kind,  # type: ignore[arg-type]
        ),
        y[n_train:],
    )


async def test_the_local_band_covers_about_95_percent_for_amounts_and_counts() -> None:
    for kind in ("amount", "count"):
        request, actual = _request(n_train=400, n_test=400, kind=kind)
        prediction = await LocalPredictor().predict(request)
        lo, hi = prediction.at(0.025), prediction.at(0.975)
        coverage = float(np.mean((actual >= lo) & (actual <= hi)))
        assert 0.9 <= coverage <= 0.995, (kind, coverage)
        assert (prediction.at(0.5) >= lo).all() and (prediction.at(0.5) <= hi).all()


async def test_the_local_band_refuses_without_enough_rows_or_a_naive_column() -> None:
    request, _ = _request(n_train=10)
    with pytest.raises(PredictorUnavailable, match="needs 20"):
        await LocalPredictor().predict(request)
    request, _ = _request()
    renamed = PredictionRequest(
        purpose="test",
        columns=("t", "other"),
        x_train=request.x_train,
        y_train=request.y_train,
        x_test=request.x_test,
        quantiles=Q,
    )
    with pytest.raises(PredictorUnavailable, match="naive"):
        await LocalPredictor().predict(renamed)


def test_requests_are_validated_and_identified_by_content() -> None:
    request, _ = _request()
    same, _ = _request()
    other, _ = _request(seed=1)
    assert request.digest() == same.digest() != other.digest()
    with pytest.raises(ValueError, match="one column per name"):
        PredictionRequest("p", ("a",), request.x_train, request.y_train, request.x_test, Q)
    with pytest.raises(ValueError, match="between 0 and 1"):
        PredictionRequest(
            "p", ("t", "naive"), request.x_train, request.y_train, request.x_test, (1.0,)
        )


class _Counting:
    name = "counting"

    def __init__(self, tokens: int = 10_000, fail: bool = False) -> None:
        self.calls = 0
        self.tokens = tokens
        self.fail = fail

    async def estimate_tokens(self, request: PredictionRequest) -> int:
        return self.tokens

    async def predict(self, request: PredictionRequest) -> Prediction:
        self.calls += 1
        if self.fail:
            raise PredictorUnavailable("provider down")
        return Prediction(Q, np.ones((3, request.n_test)) * self.calls, self.name, "v1")


async def test_the_guard_caches_identical_requests_and_logs_every_call() -> None:
    store, inner = Store(), _Counting()
    guarded = GuardedPredictor(inner, store, daily_tokens=100_000, monthly_tokens=100_000)
    request, _ = _request()
    first = await guarded.predict(request)
    again = await guarded.predict(request)
    assert inner.calls == 1 and np.array_equal(first.values, again.values)
    other, _ = _request(seed=2)
    await guarded.predict(other)
    assert inner.calls == 2
    assert store.fetch(
        "SELECT status, tokens_estimated FROM predictor_calls ORDER BY created_at"
    ) == [
        ("ok", 10_000),
        ("cached", 0),
        ("ok", 10_000),
    ]


async def test_the_guard_refuses_over_the_caps_and_counts_failed_calls() -> None:
    store = Store()
    clock = lambda: datetime(2026, 9, 28, 12)  # noqa: E731
    failing = GuardedPredictor(
        _Counting(fail=True), store, daily_tokens=25_000, monthly_tokens=1_000_000, clock=clock
    )
    request, _ = _request()
    with pytest.raises(PredictorUnavailable, match="provider down"):
        await failing.predict(request)
    capped = GuardedPredictor(
        _Counting(tokens=10_000), store, daily_tokens=25_000, monthly_tokens=1_000_000, clock=clock
    )
    capped.inner.name = "counting"  # the same provider, so the failed call's tokens count
    await capped.predict(_request(seed=3)[0])
    with pytest.raises(PredictorUnavailable, match="token cap: 20000/25000 today"):
        await capped.predict(_request(seed=4)[0])
    statuses = [s for (s,) in store.fetch("SELECT status FROM predictor_calls ORDER BY created_at")]
    assert statuses == ["failed", "ok", "refused"]


class _FakePriorLabs:
    """The hosted API's upload, fit, and predict sequence, recorded for inspection."""

    def __init__(self, *, remaining: int = 5_000_000, fit_pending: int = 1) -> None:
        self.remaining = remaining
        self.fit_pending = fit_pending
        self.requests: list[httpx.Request] = []
        self.uploads: dict[str, bytes] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "storage.example":
            self.uploads[path] = self.uploads.get(path, b"") + request.content
            return httpx.Response(200)
        body = json.loads(request.content) if request.content else {}
        signed = lambda name: {  # noqa: E731
            "signed_urls": [
                f"https://storage.example/{name}/0",
                f"https://storage.example/{name}/1",
            ],
            "required_headers": {"x-goog-meta": "1"},
        }
        if path == "/tabpfn/estimate_cost":
            return httpx.Response(200, json={"estimated_cost": 10_000})
        if path == "/get_api_usage/":
            return httpx.Response(
                200,
                json={
                    "daily_tokens_used": 5_000_000 - self.remaining,
                    "daily_token_limit": 5_000_000,
                    "monthly_tokens_used": 0,
                    "monthly_token_limit": 20_000_000,
                },
            )
        if path == "/tabpfn/prepare_train_set_upload":
            return httpx.Response(
                200,
                json={
                    "train_set_upload_id": "u1",
                    "x_train_info": signed("x"),
                    "y_train_info": signed("y"),
                },
            )
        if path in ("/tabpfn/fit", "/tabpfn/get_fit_status"):
            status = "pending" if self.fit_pending else "completed"
            self.fit_pending = max(0, self.fit_pending - 1)
            return httpx.Response(200, json={"status": status, "fitted_train_set_id": "f1"})
        if path == "/tabpfn/prepare_test_set_upload":
            return httpx.Response(
                200, json={"test_set_upload_id": "t1", "x_test_info": signed("xt")}
            )
        if path == "/tabpfn/predict":
            quantiles = body["task_config"]["predict_params"]["quantiles"]
            rows = (
                self.uploads["/xt/0"].decode().count("\n")
                + self.uploads["/xt/1"].decode().count("\n")
                - 1
            )
            return httpx.Response(
                200,
                json={
                    "prediction": [[q * 100] * rows for q in quantiles],
                    "metadata": {"model_file": "tabpfn-v3.5-test"},
                },
            )
        return httpx.Response(404)


async def test_tabpfn_uploads_fits_polls_and_predicts_quantiles() -> None:
    api = _FakePriorLabs()
    predictor = TabPFNPredictor("secret-token", transport=httpx.MockTransport(api), poll_seconds=0)
    request, _ = _request(n_train=30, n_test=4)
    request = PredictionRequest(
        purpose="test",
        columns=request.columns,
        x_train=np.where(np.arange(30)[:, None] == 0, np.nan, request.x_train),
        y_train=request.y_train,
        x_test=request.x_test,
        quantiles=Q,
    )
    assert await predictor.estimate_tokens(request) == 10_000
    prediction = await predictor.predict(request)

    assert prediction.values.shape == (3, 4) and prediction.at(0.5).tolist() == [50.0] * 4
    assert prediction.model_version == "tabpfn-v3.5-test"
    x_train = (api.uploads["/x/0"] + api.uploads["/x/1"]).decode().splitlines()
    assert x_train[0] == "t,naive" and x_train[1].startswith(",") and len(x_train) == 31
    paths = [r.url.path for r in api.requests if r.url.host != "storage.example"]
    assert paths.count("/tabpfn/get_fit_status") == 1, "a pending fit is polled"
    for sent in api.requests:
        on_storage = sent.url.host == "storage.example"
        assert ("secret-token" in sent.headers.get("authorization", "")) is not on_storage
    predict = json.loads(next(r for r in api.requests if r.url.path == "/tabpfn/predict").content)
    assert predict["task_config"]["predict_params"] == {
        "output_type": "quantiles",
        "quantiles": list(Q),
    }


async def test_tabpfn_refuses_before_sending_data_when_it_cannot_run() -> None:
    request, _ = _request(n_train=30, n_test=4)
    with pytest.raises(PredictorUnavailable, match="TABPFN_TOKEN is not set"):
        await TabPFNPredictor("").estimate_tokens(request)
    low = _FakePriorLabs(remaining=5_000)
    with pytest.raises(PredictorUnavailable, match="5000 tokens left"):
        await TabPFNPredictor("t", transport=httpx.MockTransport(low)).estimate_tokens(request)
    assert all("prepare" not in r.url.path for r in low.requests)

    def broken(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal detail that must not leak")

    with pytest.raises(PredictorUnavailable) as caught:
        await TabPFNPredictor("t", transport=httpx.MockTransport(broken)).predict(request)
    assert "HTTP 500" in str(caught.value) and "internal detail" not in str(caught.value)


async def test_a_guarded_tabpfn_call_is_logged_with_its_tokens() -> None:
    store = Store()
    guarded = GuardedPredictor(
        TabPFNPredictor("t", transport=httpx.MockTransport(_FakePriorLabs()), poll_seconds=0),
        store,
        daily_tokens=1_000_000,
        monthly_tokens=5_000_000,
    )
    request, _ = _request(n_train=30, n_test=4)
    await guarded.predict(request)
    row = store.fetch_dicts(
        "SELECT provider, model_version, status, tokens_estimated, n_train, n_test FROM predictor_calls"
    )
    assert row == [
        {
            "provider": "tabpfn",
            "model_version": "tabpfn-v3.5-test",
            "status": "ok",
            "tokens_estimated": 10_000,
            "n_train": 30,
            "n_test": 4,
        }
    ]
