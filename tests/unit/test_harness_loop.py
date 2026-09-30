"""The agent loop's guarantees: one result per call, durable pauses, bounded retries and calls."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from paid_media_agent.harness.loop import ABANDONED, INTERRUPTED, Agent
from paid_media_agent.harness.messages import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from paid_media_agent.harness.models import ModelError, ToolSchema
from paid_media_agent.harness.tools import ToolContext, ToolDispatcher, ToolSpec
from paid_media_agent.store import Store
from paid_media_agent.store.conversations import ConversationStore
from paid_media_agent.testing.scripted_model import ScriptedChatModel
from paid_media_agent.tools.artifacts import ArtifactStore
from paid_media_agent.tools.catalog import StaticCatalogProvider
from paid_media_agent.tools.fixtures import build_fixture_catalog

OBJECT = {"type": "object", "properties": {"x": {"type": "integer"}}, "additionalProperties": False}


def _calls(*names: str) -> AssistantMessage:
    return AssistantMessage(
        tool_calls=tuple(ToolCall(id=f"c-{n}", name=n, args={"x": 1}) for n in names)
    )


def _agent(store: Store, tmp_path: Path, model: Any, **kwargs: Any) -> tuple[Agent, list[str]]:
    ran: list[str] = []

    def record(name: str) -> Any:
        def handler(args: dict[str, Any], context: ToolContext) -> str:
            ran.append(name)
            return json.dumps({"tool": name, "thread": context.thread_id})

        return handler

    tools = [
        ToolSpec("read", "Read", OBJECT, record("read")),
        ToolSpec("other", "Other", OBJECT, record("other")),
        ToolSpec("execute", "Execute", OBJECT, record("execute"), kind="write", gated=True),
        ToolSpec("platform_a", "A", OBJECT, record("platform_a"), kind="read"),
        ToolSpec("platform_b", "B", OBJECT, record("platform_b"), kind="read"),
    ]
    dispatcher = ToolDispatcher(
        tools={t.name: t for t in tools},
        catalog_provider=StaticCatalogProvider(build_fixture_catalog()),
        artifacts=ArtifactStore(tmp_path / "artifacts"),
    )
    agent = Agent(
        model=model,
        system_prompt="system",
        dispatcher=dispatcher,
        conversations=ConversationStore(store),
        gate=lambda call, _context: call.name == "execute",
        **kwargs,
    )
    return agent, ran


def _tool_results(messages: Sequence[Message]) -> dict[str, ToolMessage]:
    return {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}


async def test_a_gated_call_pauses_after_the_rest_of_its_batch_runs(tmp_path: Path) -> None:
    store = Store()
    model = ScriptedChatModel(
        steps=[lambda _m: _calls("read", "execute", "other"), lambda _m: AssistantMessage("done")]
    )
    agent, ran = _agent(store, tmp_path, model)

    paused = await agent.send("t", "alice", "go")

    assert ran == ["read", "other"] and paused.awaiting_approval
    assert [c.name for c in paused.pending] == ["execute"]
    assert "c-execute" not in _tool_results(paused.messages)
    assert model.call_count == 1, "no model call while a decision is pending"

    finished = await agent.resume("t", "alice", "approve")

    assert ran == ["read", "other", "execute"] and not finished.awaiting_approval
    assert set(_tool_results(finished.messages)) == {"c-read", "c-other", "c-execute"}
    assert finished.messages[-1] == AssistantMessage("done")


async def test_a_rejection_answers_the_call_without_running_it(tmp_path: Path) -> None:
    model = ScriptedChatModel(
        steps=[lambda _m: _calls("execute"), lambda _m: AssistantMessage("understood")]
    )
    agent, ran = _agent(Store(), tmp_path, model)
    await agent.send("t", "alice", "go")

    finished = await agent.resume("t", "alice", "reject", "wrong campaign")

    result = _tool_results(finished.messages)["c-execute"]
    assert ran == [] and result.status == "error"
    assert "rejected" in result.content and "wrong campaign" in result.content


async def test_a_rejection_never_runs_the_call_even_when_the_gate_no_longer_applies(
    tmp_path: Path,
) -> None:
    store = Store()
    model = ScriptedChatModel(
        steps=[lambda _m: _calls("execute"), lambda _m: AssistantMessage("understood")]
    )
    agent, ran = _agent(store, tmp_path, model)
    await agent.send("t", "alice", "go")
    agent._gate = lambda _call, _context: False  # e.g. the proposal was deleted meanwhile

    finished = await agent.resume("t", "alice", "reject")

    assert ran == [] and "rejected" in _tool_results(finished.messages)["c-execute"].content


async def test_a_paused_call_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.duckdb"
    first_store = Store(path)
    first, _ = _agent(
        first_store, tmp_path, ScriptedChatModel(steps=[lambda _m: _calls("execute")])
    )
    await first.send("t", "alice", "go")
    first_store.close()

    store = Store(path)
    restarted, ran = _agent(
        store, tmp_path, ScriptedChatModel(steps=[lambda _m: AssistantMessage("done")])
    )
    assert restarted.conversation("t").awaiting_approval
    finished = await restarted.resume("t", "alice", "approve")
    assert ran == ["execute"] and finished.messages[-1] == AssistantMessage("done")
    store.close()


async def test_a_second_resume_never_runs_the_paused_call_again(tmp_path: Path) -> None:
    model = ScriptedChatModel(
        steps=[lambda _m: _calls("execute"), lambda _m: AssistantMessage("done")]
    )
    agent, ran = _agent(Store(), tmp_path, model)
    await agent.send("t", "alice", "go")
    await agent.resume("t", "alice", "approve")
    await agent.resume("t", "alice", "approve")
    assert ran == ["execute"]


async def test_moving_on_abandons_the_paused_call(tmp_path: Path) -> None:
    model = ScriptedChatModel(
        steps=[lambda _m: _calls("execute"), lambda _m: AssistantMessage("new topic")]
    )
    agent, ran = _agent(Store(), tmp_path, model)
    await agent.send("t", "alice", "go")

    moved = await agent.send("t", "alice", "never mind, something else")

    abandoned = _tool_results(moved.messages)["c-execute"]
    assert abandoned.status == "error" and abandoned.content == ABANDONED
    assert not moved.awaiting_approval and ran == []
    assert await agent.resume("t", "alice", "approve") == agent.conversation("t")
    assert ran == [], "an abandoned call can never be approved later"


async def test_a_crash_leaves_no_call_without_a_result(tmp_path: Path) -> None:
    store = Store()
    conversations = ConversationStore(store)
    conversations.append("t", UserMessage("go"))
    conversations.append("t", _calls("read"))  # the process died before the result was saved
    agent, _ = _agent(store, tmp_path, ScriptedChatModel(steps=[lambda _m: AssistantMessage("ok")]))

    resumed = await agent.send("t", "alice", "are you there?")

    result = _tool_results(resumed.messages)["c-read"]
    assert result.status == "error" and result.content == INTERRUPTED
    assert resumed.messages.index(result) < resumed.messages.index(UserMessage("are you there?"))


async def test_denied_and_invalid_calls_become_error_results(tmp_path: Path) -> None:
    model = ScriptedChatModel(
        steps=[
            lambda _m: AssistantMessage(
                tool_calls=(
                    ToolCall("c1", "not_a_tool", {}),
                    ToolCall("c2", "read", {"x": "not an integer"}),
                    ToolCall("c3", "read", {}, invalid_arguments="{broken"),
                )
            ),
            lambda _m: AssistantMessage("done"),
        ]
    )
    agent, ran = _agent(Store(), tmp_path, model)
    finished = await agent.send("t", "alice", "go")

    results = _tool_results(finished.messages)
    assert ran == [] and all(r.status == "error" for r in results.values())
    assert json.loads(results["c1"].content)["denied"] is True
    assert json.loads(results["c2"].content)["error"] == "invalid_arguments"
    assert json.loads(results["c3"].content)["error"] == "invalid_arguments"
    assert agent.dispatcher.denials == [("not_a_tool", "outside_tool_surface")]


class _FlakyModel:
    name = "flaky"

    def __init__(self, errors: list[BaseException]) -> None:
        self.errors = errors
        self.calls = 0

    async def complete(
        self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolSchema]
    ) -> AssistantMessage:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return AssistantMessage("recovered")


async def test_transient_model_errors_retry_and_permanent_ones_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("paid_media_agent.harness.loop.BACKOFF_SECONDS", (0.0,))
    flaky = _FlakyModel([ModelError("429", transient=True), ModelError("503", transient=True)])
    agent, _ = _agent(Store(), tmp_path, flaky)
    assert (await agent.send("t", "alice", "go")).messages[-1].content == "recovered"
    assert flaky.calls == 3

    broken = _FlakyModel([ModelError("HTTP 401", transient=False)])
    agent, _ = _agent(Store(), tmp_path, broken)
    reply = (await agent.send("t", "alice", "go")).messages[-1].content
    assert reply.startswith("Model call failed after 1 attempts") and broken.calls == 1


async def test_a_run_stops_after_its_model_call_budget(tmp_path: Path) -> None:
    looping = ScriptedChatModel(steps=[lambda _m: _calls("read")] * 10)
    agent, ran = _agent(Store(), tmp_path, looping, max_model_calls=3)
    finished = await agent.send("t", "alice", "go")
    assert looping.call_count == 3 and len(ran) == 3
    assert finished.messages[-1].content == "Stopped after 3 model calls without finishing."


async def test_read_tools_are_bound_only_once_activated_and_the_oldest_is_dropped(
    tmp_path: Path,
) -> None:
    agent, _ = _agent(Store(), tmp_path, ScriptedChatModel(steps=[]), max_active_reads=1)
    bound = {t.name for t in agent.bound_tools("t")}
    assert {"read", "other", "execute"} <= bound and not {"platform_a", "platform_b"} & bound

    context = agent._context("t", "alice")
    context.activate(["platform_a", "unknown_tool", "read"])
    assert {t.name for t in agent.bound_tools("t")} & {"platform_a", "platform_b"} == {"platform_a"}
    context.activate(["platform_b"])
    assert {t.name for t in agent.bound_tools("t")} & {"platform_a", "platform_b"} == {"platform_b"}
    assert agent.conversation("t").activated_tools == ("platform_b",)


async def test_two_turns_on_one_thread_run_one_after_the_other(tmp_path: Path) -> None:
    import asyncio
    from dataclasses import replace

    steps = [
        lambda _m: _calls("read"),
        lambda _m: AssistantMessage("first done"),
        lambda _m: AssistantMessage("second done"),
    ]
    agent, _ = _agent(Store(), tmp_path, ScriptedChatModel(steps=steps))
    started = asyncio.Event()
    spec = agent.dispatcher.tools["read"]

    async def slow(args: dict[str, Any], context: ToolContext) -> str:
        started.set()
        await asyncio.sleep(0.05)
        return "{}"

    agent.dispatcher.tools["read"] = replace(spec, handler=slow)
    first = asyncio.create_task(agent.send("t", "alice", "first"))
    await started.wait()
    await agent.send("t", "alice", "second")
    await first
    messages = agent.conversation("t").messages
    ids = [m.tool_call_id for m in messages if isinstance(m, ToolMessage)]
    assert len(ids) == len(set(ids)) == 1, "one result per call, never INTERRUPTED plus a result"
    assert [m.content for m in messages if isinstance(m, AssistantMessage) and m.content] == [
        "first done",
        "second done",
    ]


async def test_a_failing_progress_callback_never_drops_a_pause(tmp_path: Path) -> None:
    model = ScriptedChatModel(steps=[lambda _m: _calls("execute")])
    agent, ran = _agent(Store(), tmp_path, model)

    async def broken(_event: Any) -> None:
        raise RuntimeError("slack is down")

    conversation = await agent.send("t", "alice", "go", on_event=broken)
    assert conversation.awaiting_approval and ran == []


def test_the_calendar_resolves_windows_across_month_and_year_boundaries() -> None:
    from datetime import date

    from paid_media_agent.harness.loop import calendar_lines

    text = calendar_lines(date(2026, 1, 1))
    assert "Today is Thursday 2026-01-01" in text
    assert "last week (Monday to Sunday): 2025-12-22 to 2025-12-28" in text
    assert "last month: 2025-12-01 to 2025-12-31" in text
    assert "this month to date: 2026-01-01 to 2026-01-01" in text
    tuesday = calendar_lines(date(2026, 9, 29))
    assert "last week (Monday to Sunday): 2026-09-21 to 2026-09-27" in tuesday
    assert "the week before: 2026-09-14 to 2026-09-20" in tuesday
    assert "7 and 28 calendar days to yesterday: 2026-09-22 to 2026-09-28" in tuesday
    assert "'The last N days' is different" in tuesday and "never on yesterday" in tuesday, (
        "agrees with instructions.md: the last N days end where the platform's data does"
    )


async def test_turn_locks_are_released_with_their_threads(tmp_path: Path) -> None:
    import gc

    agent, _ = _agent(Store(), tmp_path, ScriptedChatModel([lambda _m: AssistantMessage("hi")] * 3))
    for thread in ("a", "b", "c"):
        await agent.send(thread, "local-user", "hello")
    gc.collect()
    assert len(agent._turns) == 0, "an idle thread keeps no lock"
