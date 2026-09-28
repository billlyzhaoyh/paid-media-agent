"""Every predictor call goes through here: cached by request, capped by tokens, and logged.

An identical request (same features, targets, and quantiles) is answered from `predictor_calls`
without calling the provider. Otherwise the provider's estimate is checked against the local daily
and monthly caps, which sit below the account's own limits, and the call is recorded whether it
succeeds, fails, or is refused. Failed calls count against the caps, since they may be billed.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from datetime import datetime

import numpy as np

from paid_media_agent.predict.protocol import (
    Prediction,
    PredictionRequest,
    Predictor,
    PredictorUnavailable,
)
from paid_media_agent.store.db import Store, utc_now


class GuardedPredictor:
    def __init__(
        self,
        inner: Predictor,
        store: Store,
        *,
        daily_tokens: int,
        monthly_tokens: int,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.inner = inner
        self.name = inner.name
        self._store = store
        self._daily = daily_tokens
        self._monthly = monthly_tokens
        self._clock = clock

    async def estimate_tokens(self, request: PredictionRequest) -> int:
        return await self.inner.estimate_tokens(request)

    def tokens_used(self, since: datetime) -> int:
        rows = self._store.fetch(
            "SELECT coalesce(sum(tokens_estimated), 0) FROM predictor_calls "
            "WHERE provider = ? AND status IN ('ok', 'failed') AND created_at >= ?",
            [self.inner.name, since],
        )
        return int(rows[0][0])

    def _cached(self, sha: str) -> Prediction | None:
        rows = self._store.fetch(
            "SELECT model_version, result FROM predictor_calls "
            "WHERE provider = ? AND request_sha = ? AND status = 'ok' "
            "ORDER BY created_at DESC LIMIT 1",
            [self.inner.name, sha],
        )
        if not rows:
            return None
        result = json.loads(rows[0][1])
        return Prediction(
            quantiles=tuple(result["quantiles"]),
            values=np.asarray(result["values"], dtype=np.float64),
            provider=self.inner.name,
            model_version=rows[0][0],
        )

    def _record(
        self,
        request: PredictionRequest,
        sha: str,
        *,
        status: str,
        tokens: int,
        model_version: str | None = None,
        latency_ms: int | None = None,
        error: str | None = None,
        result: dict[str, object] | None = None,
    ) -> None:
        self._store.write(
            "INSERT INTO predictor_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                uuid.uuid4(),
                self.inner.name,
                model_version,
                request.purpose,
                sha,
                request.n_train,
                request.n_test,
                len(request.columns),
                tokens,
                latency_ms,
                status,
                error,
                json.dumps(result) if result is not None else None,
                self._clock(),
            ],
        )

    async def predict(self, request: PredictionRequest) -> Prediction:
        sha = request.digest()
        cached = self._cached(sha)
        if cached is not None:
            self._record(
                request, sha, status="cached", tokens=0, model_version=cached.model_version
            )
            return cached
        try:
            tokens = await self.inner.estimate_tokens(request)
        except PredictorUnavailable as exc:
            self._record(request, sha, status="refused", tokens=0, error=str(exc)[:500])
            raise
        now = self._clock()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        used_today = self.tokens_used(day_start)
        used_month = self.tokens_used(day_start.replace(day=1))
        if used_today + tokens > self._daily or used_month + tokens > self._monthly:
            reason = (
                f"{self.inner.name} token cap: {used_today}/{self._daily} today, "
                f"{used_month}/{self._monthly} this month; this call needs {tokens}"
            )
            self._record(request, sha, status="refused", tokens=tokens, error=reason)
            raise PredictorUnavailable(reason)
        started = time.monotonic()
        try:
            prediction = await self.inner.predict(request)
        except PredictorUnavailable as exc:
            # A failed call may still have been billed; it counts against the caps.
            self._record(request, sha, status="failed", tokens=tokens, error=str(exc)[:500])
            raise
        self._record(
            request,
            sha,
            status="ok",
            tokens=tokens,
            model_version=prediction.model_version,
            latency_ms=int((time.monotonic() - started) * 1000),
            result={"quantiles": list(prediction.quantiles), "values": prediction.values.tolist()},
        )
        return prediction
