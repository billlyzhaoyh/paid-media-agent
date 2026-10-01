"""Each final answer's figures are checked against the thread's tool results, through the loop."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from paid_media_agent.analytics.sync import run_sync
from paid_media_agent.config import Settings
from paid_media_agent.harness.loop import RunEvent
from paid_media_agent.harness.messages import AssistantMessage, Message, UserMessage
from paid_media_agent.runtime.local import LocalRuntime
from paid_media_agent.testing.scripted_model import tool_call_message
from paid_media_agent.tools.fixtures import FixtureState
from tests.contract.helpers import build_runtime

TODAY = datetime.now(UTC).date()
SETTINGS_READ = {"view": "settings", "account_alias": "demo-google"}


async def _runtime(settings: Settings, project_root: Path, steps: list[Any]) -> LocalRuntime:
    anchored = settings.model_copy(update={"paid_media_fixture_anchor": TODAY - timedelta(days=2)})
    runtime, _ = build_runtime(
        anchored, project_root, steps, fixture_state=FixtureState(TODAY - timedelta(days=2))
    )
    await run_sync(
        accounts=runtime.profile.accounts,
        catalog=runtime.catalog,
        dispatcher=runtime.components.read_dispatcher,
        end=TODAY - timedelta(days=1),
    )
    return runtime


def _purposes(runtime: LocalRuntime, thread_id: str) -> list[str]:
    rows = runtime.profile.store.fetch(
        "SELECT purpose FROM llm_calls WHERE thread_id = ? ORDER BY created_at", [thread_id]
    )
    return [r[0] for r in rows]


async def test_an_invented_figure_is_sent_back_once_and_only_the_answer_is_shown(
    settings: Settings, project_root: Path
) -> None:
    seen: list[str] = []

    def repaired(messages: Sequence[Message]) -> AssistantMessage:
        note = messages[-1]
        assert isinstance(note, UserMessage) and note.origin == "host"
        seen.append(note.content)
        return AssistantMessage("Active daily budgets total 900.00 USD.")

    steps = [
        lambda _m: tool_call_message("query_history", SETTINGS_READ),
        lambda _m: AssistantMessage("Active daily budgets total $950.00, up from $900.00."),
        repaired,
    ]
    runtime = await _runtime(settings, project_root, steps)
    events: list[RunEvent] = []

    async def collect(event: RunEvent) -> None:
        events.append(event)

    conversation = await runtime.agent.send(
        "g-1", "local-user", "What do budgets add up to?", on_event=collect
    )
    assert conversation.messages[-1].content == "Active daily budgets total 900.00 USD."
    assert "$950.00" in seen[0] and "900.00" not in seen[0], "names only the unsourced figure"
    draft, note = conversation.messages[-3:-1]
    assert isinstance(draft, AssistantMessage) and "$950.00" in draft.content, "kept for the record"
    assert isinstance(note, UserMessage) and note.origin == "host"
    assert [e.text for e in events if e.kind == "text"] == [conversation.messages[-1].content]
    assert _purposes(runtime, "g-1") == ["agent", "agent", "repair"]
    reloaded = runtime.agent.conversation("g-1").messages
    assert [m for m in reloaded if isinstance(m, UserMessage)][-1].origin == "host", "stored"

    from paid_media_agent.evals.runner import transcript_of

    transcript = transcript_of("q", conversation.messages, runtime)
    assert transcript.repaired and "$950.00" in transcript.draft
    assert transcript.answer == "Active daily budgets total 900.00 USD."


async def test_a_second_miss_is_shown_with_the_figures_marked(
    settings: Settings, project_root: Path
) -> None:
    steps = [
        lambda _m: tool_call_message("query_history", SETTINGS_READ),
        lambda _m: AssistantMessage("Budgets total $950.00."),
        lambda _m: AssistantMessage("Budgets total $975.00."),
    ]
    runtime = await _runtime(settings, project_root, steps)
    conversation = await runtime.agent.send("g-2", "local-user", "What do budgets add up to?")
    final = conversation.messages[-1].content
    assert final.startswith("Budgets total $975.00.")
    assert final.endswith("not found in any tool result, so treat them as unverified: $975.00.")
    assert _purposes(runtime, "g-2") == ["agent", "agent", "repair"], "one repair, never two"


async def test_a_grounded_answer_costs_no_extra_call_and_offloaded_rows_count(
    settings: Settings, project_root: Path
) -> None:
    daily = {"view": "daily", "account_alias": "demo-google", "limit": 200}

    def quote_deep_row(messages: Sequence[Message]) -> AssistantMessage:
        stub = json.loads(messages[-1].content)
        assert stub.get("offloaded"), "the rows are only in the artifact"
        rows = runtime.profile.store.fetch_dicts(
            "SELECT spend FROM entity_daily_panel WHERE account_alias = 'demo-google' "
            "ORDER BY day LIMIT 1"
        )
        return AssistantMessage(f"The oldest day's spend was {rows[0]['spend']:.2f} USD.")

    steps = [
        lambda _m: tool_call_message("query_history", SETTINGS_READ),
        lambda _m: tool_call_message("query_history", daily),
        quote_deep_row,
    ]
    runtime = await _runtime(settings, project_root, steps)
    conversation = await runtime.agent.send("g-3", "local-user", "How much did we spend?")
    assert not any(isinstance(m, UserMessage) and m.origin == "host" for m in conversation.messages)
    assert _purposes(runtime, "g-3") == ["agent", "agent", "agent"]


async def test_the_check_can_be_turned_off(settings: Settings, project_root: Path) -> None:
    steps = [
        lambda _m: tool_call_message("query_history", SETTINGS_READ),
        lambda _m: AssistantMessage("Budgets total $950.00."),
    ]
    runtime = await _runtime(
        settings.model_copy(update={"paid_media_answer_repair": False}), project_root, steps
    )
    conversation = await runtime.agent.send("g-4", "local-user", "What do budgets add up to?")
    assert conversation.messages[-1].content == "Budgets total $950.00."
