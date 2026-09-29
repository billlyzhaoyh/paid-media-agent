"""A long thread stays inside its token budget; stored history is never rewritten."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from paid_media_agent.harness.context import estimate_tokens, fit_to_budget, stub
from paid_media_agent.harness.messages import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from paid_media_agent.store import Store
from tests.unit.test_harness_loop import _agent

BIG = "x" * 20_000


def _thread(turns: int, per_turn: int) -> list[Message]:
    messages: list[Message] = []
    for t in range(turns):
        messages.append(UserMessage(f"question {t}"))
        for c in range(per_turn):
            call = ToolCall(id=f"c{t}-{c}", name="read", args={"x": c})
            messages.append(AssistantMessage(tool_calls=(call,)))
            body = {"artifact_id": f"art-{t}-{c}", "reading": f"read {t}-{c}", "rows": BIG}
            messages.append(ToolMessage(call.id, "read", json.dumps(body)))
        messages.append(AssistantMessage(f"answer {t}"))
    return messages


def _ids(messages: Sequence[Message]) -> list[str]:
    return [m.tool_call_id for m in messages if isinstance(m, ToolMessage)]


def test_old_results_are_stubbed_until_the_thread_fits() -> None:
    thread = _thread(turns=6, per_turn=5)
    assert estimate_tokens("s", thread, []) > 150_000
    view = fit_to_budget("s", thread, [], budget=60_000)
    assert view.est_tokens <= 60_000 and view.stubbed > 0
    assert estimate_tokens("s", view.messages, []) <= 60_000
    assert _ids(view.messages) == _ids(thread), "one result per call, in place"
    last_turn = view.messages[-16:]
    assert not any("stubbed" in m.content for m in last_turn if isinstance(m, ToolMessage))
    first = json.loads(view.messages[2].content)
    assert first["stubbed"] and first["artifact_ids"] == ["art-0-0"]
    assert first["summary"] == "read 0-0" and first["tool"] == "read"
    assert [m for m in view.messages if isinstance(m, AssistantMessage)] == [
        m for m in thread if isinstance(m, AssistantMessage)
    ], "assistant messages are untouched"


def test_an_oversized_current_turn_keeps_its_newest_results() -> None:
    thread = _thread(turns=1, per_turn=12)
    view = fit_to_budget("s", thread, [], budget=10_000)
    results = [m for m in view.messages if isinstance(m, ToolMessage)]
    assert all("stubbed" not in m.content for m in results[-4:])
    assert all("stubbed" in m.content for m in results[:-4])


def test_no_budget_or_a_small_thread_changes_nothing() -> None:
    thread = _thread(turns=3, per_turn=3)
    assert fit_to_budget("s", thread, [], budget=0).messages == thread
    small = _thread(turns=1, per_turn=1)[:2]
    assert fit_to_budget("s", small, [], budget=60_000).stubbed == 0
    plain = ToolMessage("c", "read", "not json " * 100)
    assert json.loads(stub(plain).content)["summary"].startswith("not json")


class _Seeing:
    name = "seeing"

    def __init__(self) -> None:
        self.sent: list[list[Message]] = []

    async def complete(self, *, system: str, messages: Sequence[Message], tools: Any) -> Any:
        self.sent.append(list(messages))
        return AssistantMessage("ok")


async def test_the_loop_sends_the_view_but_stores_the_whole_thread(tmp_path: Path) -> None:
    path = tmp_path / "state.duckdb"
    store = Store(path)
    model = _Seeing()
    agent, _ = _agent(store, tmp_path, model, context_budget_tokens=20_000)
    for message in _thread(turns=4, per_turn=4):
        agent.conversations.append("t", message)
    before = store.fetch("SELECT seq, content FROM messages WHERE thread_id = 't' ORDER BY seq")
    await agent.send("t", "alice", "one more")
    sent = model.sent[0]
    assert estimate_tokens("system", sent, []) <= 20_000
    assert any("stubbed" in m.content for m in sent if isinstance(m, ToolMessage))
    after = store.fetch("SELECT seq, content FROM messages WHERE thread_id = 't' ORDER BY seq")
    assert after[: len(before)] == before, "stored history is byte-identical"
    store.close()

    reopened = Store(path)
    model_again = _Seeing()
    agent_again, _ = _agent(reopened, tmp_path, model_again, context_budget_tokens=20_000)
    await agent_again.send("t", "alice", "and another")
    again = model_again.sent[0][: len(sent)]
    assert [m.content for m in again[:-1]] == [m.content for m in sent[:-1]], "same view"
    reopened.close()
