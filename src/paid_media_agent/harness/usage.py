"""Recording model calls: one `llm_calls` row per attempt, with the provider's own usage and cost.

The agent loop reports every attempt, successful or not, with its latency; the provider's usage
block (tokens, cache reads and writes, reported cost) comes back on the reply. A recording failure
is logged and never breaks a turn.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from paid_media_agent.harness.messages import Usage
from paid_media_agent.store.db import Store, utc_now

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CallRecord:
    thread_id: str | None
    caller_ref: str | None
    purpose: str
    provider: str | None
    model: str
    attempt: int
    status: str
    """ok, error, or timeout."""
    latency_ms: int
    usage: Usage | None = None
    error: str | None = None
    messages_sent: int | None = None
    est_prompt_tokens: int | None = None
    stubbed_results: int = 0
    cache_requested: bool = False


class CallLog(Protocol):
    def record(self, call: CallRecord) -> None: ...


class LlmCallRecorder:
    """Writes `llm_calls`. Implements `CallLog`."""

    def __init__(self, store: Store, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._store = store
        self._clock = clock

    def record(self, call: CallRecord) -> None:
        u = call.usage or Usage()
        try:
            self._store.write(
                "INSERT INTO llm_calls VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    uuid.uuid4(),
                    call.thread_id,
                    call.caller_ref,
                    call.purpose,
                    call.provider,
                    call.model,
                    call.attempt,
                    call.status,
                    call.error,
                    call.latency_ms,
                    u.input_tokens,
                    u.output_tokens,
                    u.cached_tokens,
                    u.cache_write_tokens,
                    u.reasoning_tokens,
                    u.cost_usd,
                    u.response_model,
                    u.generation_id,
                    call.messages_sent,
                    call.est_prompt_tokens,
                    call.stubbed_results,
                    call.cache_requested,
                    self._clock(),
                ],
            )
        except Exception:
            log.warning("could not record a model call", exc_info=True)


UsageBy = Literal["model", "day", "thread", "purpose"]
_GROUP = {
    "model": "provider || ':' || model",
    "day": "CAST(CAST(created_at AS DATE) AS VARCHAR)",
    "thread": "coalesce(thread_id, '(none)')",
    "purpose": "purpose",
}


def usage_summary(store: Store, *, since: datetime, by: UsageBy = "model") -> dict[str, Any]:
    """Calls, failures, tokens, cache hit rate, reported cost, and latency since `since`."""
    key = _GROUP[by]
    select = (
        f"SELECT {key} AS key, count(*) AS calls, "  # noqa: S608 - key is from a fixed map
        "count(*) FILTER (WHERE status <> 'ok') AS failed, "
        "coalesce(sum(input_tokens), 0) AS input_tokens, "
        "coalesce(sum(cached_tokens), 0) AS cached_tokens, "
        "coalesce(sum(output_tokens), 0) AS output_tokens, "
        "sum(cost_usd) AS cost_usd, count(cost_usd) AS costed_calls, "
        "CAST(median(latency_ms) AS INTEGER) AS p50_latency_ms, "
        "CAST(quantile_cont(latency_ms, 0.95) AS INTEGER) AS p95_latency_ms "
        "FROM llm_calls WHERE created_at >= ?"
    )
    rows = store.fetch_dicts(f"{select} GROUP BY 1 ORDER BY 1", [since])
    totals = store.fetch_dicts(select.replace(f"{key} AS key, ", "'all' AS key, "), [since])[0]
    for row in [*rows, totals]:
        row["cache_hit_rate"] = (
            round(row["cached_tokens"] / row["input_tokens"], 3) if row["input_tokens"] else None
        )
        row["cost_usd"] = round(row["cost_usd"], 4) if row["cost_usd"] is not None else None
    return {"since": since.isoformat(), "by": by, "rows": rows, "totals": totals}
