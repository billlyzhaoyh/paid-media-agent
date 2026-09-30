"""Figures no tool returned come from `calculate`, through the real agent loop and dispatcher."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from paid_media_agent.analytics.sync import run_sync
from paid_media_agent.assembly import CORE_TOOLS
from paid_media_agent.config import Settings
from paid_media_agent.harness.messages import AssistantMessage
from paid_media_agent.testing.scripted_model import last_tool_results, tool_call_message
from paid_media_agent.tools.fixtures import FixtureState
from tests.contract.helpers import build_runtime

TODAY = datetime.now(UTC).date()


async def test_the_agent_totals_budgets_and_calculates_what_no_tool_returned(
    settings: Settings, project_root: Path
) -> None:
    def calculate(messages: object) -> AssistantMessage:
        (history,) = last_tool_results(messages)  # type: ignore[arg-type]
        (google,) = [t for t in history["budget_totals"] if t["account_alias"] == "demo-google"]
        total = google["active_daily_budget_total"]
        return tool_call_message(
            "calculate",
            {
                "calculations": [
                    {"label": "a month", "expression": f"{total} * 30.4", "format": "money"},
                    {"label": "rogue", "expression": "__import__('os')"},
                ]
            },
        )

    steps = [
        lambda _m: tool_call_message(
            "query_history", {"view": "settings", "account_alias": "demo-google"}
        ),
        calculate,
        lambda messages: AssistantMessage(json.dumps(last_tool_results(messages))),
    ]
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
    assert "calculate" in CORE_TOOLS
    assert "calculate" in {t.name for t in runtime.agent.bound_tools("c-1")}, "always bound"
    conversation = await runtime.agent.send("c-1", "local-user", "What do budgets add up to?")
    history = json.loads(
        next(m.content for m in conversation.messages if getattr(m, "name", "") == "query_history")
    )
    (google,) = [t for t in history["budget_totals"] if t["account_alias"] == "demo-google"]
    active = [
        r["daily_budget"]
        for r in history["rows"]
        if r["valid_to"] is None and str(r["status"]).upper() in ("ENABLED", "ACTIVE")
    ]
    assert google["active_daily_budget_total"] == round(sum(active), 2) == 900.0
    assert google["campaigns"] == len(active) and google["currency"] == "USD"
    assert all(isinstance(r["days_ago"], int) and r["days_ago"] >= 0 for r in history["rows"])

    (monthly, rogue) = json.loads(conversation.messages[-1].content)[0]["results"]
    assert monthly["display"] == "27,360.00", "900 x 30.4, computed in code"
    assert rogue["error"] is True, "nothing but arithmetic runs"
