"""TabPFN-3.5 through Prior Labs' hosted API: upload, fit, predict quantiles. Opt-in only.

Enabling it sends the feature rows (campaign codes, dates, and metric values; no names or account
ids) to Prior Labs. The REST contract, as the official client uses it:

1. `POST /tabpfn/prepare_train_set_upload` returns signed URLs; the CSVs are PUT to them, split
   evenly when there are several.
2. `POST /tabpfn/fit` fits the training set (`pending` is polled at `/tabpfn/get_fit_status`).
3. `POST /tabpfn/prepare_test_set_upload`, then PUT the test CSV.
4. `POST /tabpfn/predict` with `output_type = quantiles` returns one array per quantile.

`POST /tabpfn/estimate_cost` and `POST /get_api_usage/` are free and run before every call, so a
request that would exceed the account's remaining tokens is refused before any data is sent.
"""

from __future__ import annotations

import asyncio
import csv
import io
import math
from typing import Any

import httpx
import numpy as np

from paid_media_agent.predict.protocol import (
    Prediction,
    PredictionRequest,
    PredictorUnavailable,
)

DEFAULT_BASE_URL = "https://api.priorlabs.ai"
MODEL_PATH = "v3.5_default"
FIT_POLL_SECONDS = 1.0
FIT_POLL_LIMIT = 120


def _csv(header: list[str], rows: np.ndarray) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if not math.isfinite(float(v)) else repr(float(v)) for v in row])
    return buffer.getvalue().encode()


class TabPFNPredictor:
    name = "tabpfn"

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_seconds: float = FIT_POLL_SECONDS,
    ) -> None:
        self._token = token
        self._base_url = base_url
        self._timeout = timeout_seconds
        self._transport = transport
        self._poll = poll_seconds
        self.model_version = MODEL_PATH

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=self._timeout,
            transport=self._transport,
        )

    @staticmethod
    async def _json(response: httpx.Response, step: str) -> dict[str, Any]:
        if response.status_code >= 400:
            # Status only: bodies can echo request details.
            raise PredictorUnavailable(f"TabPFN {step} failed: HTTP {response.status_code}")
        data: dict[str, Any] = response.json()
        return data

    async def _put(self, info: dict[str, Any], data: bytes) -> None:
        urls = info["signed_urls"]
        size = len(data) // len(urls)
        headers = info.get("required_headers") or {}
        # Signed URLs carry their own authorization, so this client never holds the API token.
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as uploads:
            for i, url in enumerate(urls):
                end = None if i == len(urls) - 1 else (i + 1) * size
                response = await uploads.put(url, content=data[i * size : end], headers=headers)
                if response.status_code >= 400:
                    raise PredictorUnavailable(f"TabPFN upload failed: HTTP {response.status_code}")

    async def usage(self) -> dict[str, Any]:
        async with self._client() as client:
            return await self._json(await client.post("/get_api_usage/"), "usage")

    async def estimate_tokens(self, request: PredictionRequest) -> int:
        """The billed estimate, refused up front when the account has fewer tokens left."""
        if not self._token:
            raise PredictorUnavailable("TABPFN_TOKEN is not set")
        try:
            async with self._client() as client:
                estimate = await self._json(
                    await client.post(
                        "/tabpfn/estimate_cost",
                        json={
                            "train_rows": request.n_train,
                            "test_rows": request.n_test,
                            "raw_columns": len(request.columns),
                        },
                    ),
                    "estimate_cost",
                )
                usage = await self._json(await client.post("/get_api_usage/"), "usage")
        except httpx.HTTPError as exc:
            raise PredictorUnavailable(f"TabPFN unreachable: {type(exc).__name__}") from None
        tokens = int(estimate["estimated_cost"])
        daily_left = int(usage["daily_token_limit"]) - int(usage["daily_tokens_used"])
        monthly_left = int(usage["monthly_token_limit"]) - int(usage["monthly_tokens_used"])
        if tokens > min(daily_left, monthly_left):
            raise PredictorUnavailable(
                f"TabPFN account has {min(daily_left, monthly_left)} tokens left; "
                f"this call needs {tokens}"
            )
        return tokens

    async def predict(self, request: PredictionRequest) -> Prediction:
        try:
            return await self._predict(request)
        except httpx.HTTPError as exc:
            raise PredictorUnavailable(f"TabPFN unreachable: {type(exc).__name__}") from None
        except (KeyError, ValueError, TypeError) as exc:
            raise PredictorUnavailable(f"TabPFN response not understood: {exc}") from None

    async def _predict(self, request: PredictionRequest) -> Prediction:
        header = list(request.columns)
        config = {
            "task": "regression",
            "tabpfn_config": {"model_path": MODEL_PATH, "random_state": 0},
        }
        async with self._client() as client:
            prep = await self._json(
                await client.post(
                    "/tabpfn/prepare_train_set_upload",
                    json={"x_train_info": {"format": "csv"}, "y_train_info": {"format": "csv"}},
                ),
                "prepare_train_set_upload",
            )
            await self._put(prep["x_train_info"], _csv(header, request.x_train))
            await self._put(prep["y_train_info"], _csv(["target"], request.y_train.reshape(-1, 1)))
            fit = await self._json(
                await client.post(
                    "/tabpfn/fit",
                    json={
                        "task_config": config,
                        "tabpfn_systems": ["preprocessing"],
                        "train_set_upload_id": prep["train_set_upload_id"],
                    },
                ),
                "fit",
            )
            polls = 0
            while fit.get("status") != "completed":
                if fit.get("status") not in (None, "pending", "running") or polls >= FIT_POLL_LIMIT:
                    raise PredictorUnavailable(f"TabPFN fit ended as {fit.get('status')}")
                polls += 1
                await asyncio.sleep(self._poll)
                fit = await self._json(
                    await client.post(
                        "/tabpfn/get_fit_status",
                        json={"fitted_train_set_id": fit["fitted_train_set_id"]},
                    ),
                    "get_fit_status",
                )
            fitted = fit["fitted_train_set_id"]
            test = await self._json(
                await client.post(
                    "/tabpfn/prepare_test_set_upload",
                    json={"fitted_train_set_id": fitted, "x_test_info": {"format": "csv"}},
                ),
                "prepare_test_set_upload",
            )
            await self._put(test["x_test_info"], _csv(header, request.x_test))
            result = await self._json(
                await client.post(
                    "/tabpfn/predict",
                    json={
                        "test_set_upload_id": test["test_set_upload_id"],
                        "fitted_train_set_id": fitted,
                        "task_config": {
                            **config,
                            "predict_params": {
                                "output_type": "quantiles",
                                "quantiles": list(request.quantiles),
                            },
                        },
                    },
                ),
                "predict",
            )
        values = np.asarray(result["prediction"], dtype=np.float64)
        if values.shape != (len(request.quantiles), request.n_test):
            raise ValueError(f"expected {len(request.quantiles)}x{request.n_test} quantiles")
        metadata = result.get("metadata") or {}
        named = [str(v) for k, v in metadata.items() if "model" in str(k) and isinstance(v, str)]
        version = named[0] if named else MODEL_PATH
        return Prediction(
            quantiles=request.quantiles,
            values=values,
            provider=self.name,
            model_version=version,
        )
