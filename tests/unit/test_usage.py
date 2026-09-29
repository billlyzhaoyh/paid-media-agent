"""Model usage: parsed from the response, cached through OpenRouter, recorded once per attempt."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from paid_media_agent.harness.messages import AssistantMessage, Usage, UserMessage
from paid_media_agent.harness.models import ModelError, OpenAICompatibleModel, parse_usage
from paid_media_agent.harness.usage import LlmCallRecorder
from paid_media_agent.store import Store
from paid_media_agent.testing.scripted_model import ScriptedChatModel
from tests.unit.test_harness_loop import _agent

OPENROUTER = "https://openrouter.ai/api/v1"
USAGE = {
    "prompt_tokens": 10339,
    "completion_tokens": 60,
    "total_tokens": 10399,
    "prompt_tokens_details": {"cached_tokens": 10318, "cache_write_tokens": 0},
    "completion_tokens_details": {"reasoning_tokens": 5},
    "cost": 0.0012,
}


def _model(model: str, base_url: str, **kwargs: Any) -> tuple[OpenAICompatibleModel, list[Any]]:
    sent: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "gen-1",
                "model": model,
                "choices": [{"message": {"content": "ok"}}],
                "usage": USAGE,
            },
        )

    return (
        OpenAICompatibleModel(
            model=model,
            base_url=base_url,
            api_key="k",
            transport=httpx.MockTransport(respond),
            **kwargs,
        ),
        sent,
    )


async def test_usage_and_cost_come_back_on_the_reply() -> None:
    model, _ = _model("anthropic/claude-haiku-4.5", OPENROUTER)
    reply = await model.complete(system="s", messages=[UserMessage("hi")], tools=[])
    usage = reply.usage
    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens) == (10339, 60)
    assert (usage.cached_tokens, usage.cache_write_tokens, usage.reasoning_tokens) == (10318, 0, 5)
    assert usage.cost_usd == pytest.approx(0.0012)
    assert (usage.generation_id, usage.response_model) == ("gen-1", "anthropic/claude-haiku-4.5")
    assert parse_usage({"choices": []}) is None, "no usage block, no usage"
    assert parse_usage({"usage": {"prompt_tokens": 3}}).cost_usd is None, "cost not reported"


@pytest.mark.parametrize(
    ("model_id", "base_url", "setting", "cached"),
    [
        ("anthropic/claude-haiku-4.5", OPENROUTER, "auto", True),
        ("anthropic/claude-haiku-4.5", OPENROUTER, "off", False),
        ("openai/gpt-5.4-mini", OPENROUTER, "auto", False),
        ("claude-sonnet-4-6", "https://api.anthropic.com/v1", "auto", False),
    ],
)
async def test_prompt_caching_is_requested_for_anthropic_models_on_openrouter(
    model_id: str, base_url: str, setting: str, cached: bool
) -> None:
    model, sent = _model(model_id, base_url, prompt_cache=setting, zero_data_retention=True)
    await model.complete(system="s", messages=[UserMessage("hi")], tools=[])
    assert ("cache_control" in sent[0]) is cached and model.cache_requested is cached
    if cached:
        assert sent[0]["cache_control"] == {"type": "ephemeral"}
    assert ("provider" in sent[0]) is (base_url == OPENROUTER), "zdr goes to OpenRouter only"


class _Recovering:
    """Fails with the given errors, then answers with usage attached."""

    name = "anthropic/claude-haiku-4.5"
    provider = "openrouter"
    cache_requested = True

    def __init__(self, errors: list[BaseException]) -> None:
        self.errors = errors

    async def complete(self, *, system: str, messages: Any, tools: Any) -> AssistantMessage:
        if self.errors:
            raise self.errors.pop(0)
        return AssistantMessage(
            "done",
            usage=Usage(input_tokens=1200, output_tokens=30, cached_tokens=1000, cost_usd=0.002),
        )


async def test_every_attempt_is_recorded_and_usage_never_enters_the_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("paid_media_agent.harness.loop.BACKOFF_SECONDS", (0.0,))
    store = Store()
    model = _Recovering([ModelError("429", transient=True), TimeoutError()])
    agent, _ = _agent(store, tmp_path, model, call_log=LlmCallRecorder(store))
    conversation = await agent.send("t-1", "alice", "go")
    assert conversation.messages[-1].content == "done"
    rows = store.fetch_dicts("SELECT * FROM llm_calls ORDER BY attempt")
    assert [(r["attempt"], r["status"]) for r in rows] == [(1, "error"), (2, "timeout"), (3, "ok")]
    ok = rows[-1]
    assert (ok["thread_id"], ok["caller_ref"], ok["purpose"]) == ("t-1", "alice", "agent")
    assert (ok["provider"], ok["model"], ok["cache_requested"]) == (
        "openrouter", "anthropic/claude-haiku-4.5", True
    )  # fmt: skip
    assert (ok["input_tokens"], ok["cached_tokens"], ok["cost_usd"]) == (1200, 1000, 0.002)
    assert ok["latency_ms"] >= 0 and ok["messages_sent"] == 1 and ok["est_prompt_tokens"] > 0
    assert rows[0]["error"] and rows[0]["input_tokens"] is None, "failed attempts have no usage"
    stored = store.fetch("SELECT content FROM messages WHERE role = 'assistant'")
    assert stored == [("done",)] and "1200" not in str(store.fetch("SELECT * FROM messages"))


async def test_scripted_models_record_without_tokens_and_a_broken_log_never_breaks_a_turn(
    tmp_path: Path,
) -> None:
    store = Store()
    scripted = ScriptedChatModel(steps=[lambda _m: AssistantMessage("hi")])
    agent, _ = _agent(store, tmp_path, scripted, call_log=LlmCallRecorder(store))
    await agent.send("t", "bob", "go")
    (row,) = store.fetch_dicts("SELECT status, input_tokens, cost_usd FROM llm_calls")
    assert row == {"status": "ok", "input_tokens": None, "cost_usd": None}

    class Broken:
        def record(self, call: Any) -> None:
            raise RuntimeError("disk full")

    closed = Store()
    recorder = LlmCallRecorder(closed)
    closed.close()
    for log in (recorder, Broken()):
        again = ScriptedChatModel(steps=[lambda _m: AssistantMessage("still fine")])
        agent, _ = _agent(Store(), tmp_path, again, call_log=log)
        assert (await agent.send("t", "bob", "go")).messages[-1].content == "still fine"


def test_usage_totals_and_the_history_view_are_for_operators_only(tmp_path: Path) -> None:
    from datetime import datetime

    from paid_media_agent.analytics.history import MODEL_HISTORY_VIEWS, query_history
    from paid_media_agent.harness.usage import CallRecord, usage_summary

    store = Store()
    recorder = LlmCallRecorder(store, clock=lambda: datetime(2026, 9, 29, 12))
    for attempt, (status, usage) in enumerate(
        [
            ("ok", Usage(input_tokens=1000, cached_tokens=800, output_tokens=50, cost_usd=0.01)),
            ("ok", Usage(input_tokens=3000, cached_tokens=0, output_tokens=20, cost_usd=0.03)),
            ("error", None),
        ]
    ):
        recorder.record(
            CallRecord(
                "t",
                "u",
                "agent",
                "openrouter",
                "m",
                attempt + 1,
                status,
                100 * (attempt + 1),
                usage,
            )
        )
    totals = usage_summary(store, since=datetime(2026, 9, 1))["totals"]
    assert (totals["calls"], totals["failed"]) == (3, 1)
    assert (totals["input_tokens"], totals["cached_tokens"]) == (4000, 800)
    assert totals["cache_hit_rate"] == 0.2 and totals["cost_usd"] == pytest.approx(0.04)
    assert totals["costed_calls"] == 2
    rows, _ = query_history(store, "usage")
    assert rows[0]["calls"] == 3 and rows[0]["model"] == "m"
    assert "usage" not in MODEL_HISTORY_VIEWS, "the model never sees usage"
    with pytest.raises(ValueError):
        query_history(store, "usage", account_alias="acme")
