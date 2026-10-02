"""The demo's summary and next steps, written by the agent from the page's own figures.

The models find and predict; the agent explains and plans. Here the real agent loop runs with the
project's instructions, the configured model, `calculate`, and two tools that return exactly the
figures the page draws. Its reply is validated and every figure in it is checked against those
tool results, as every answer is. If anything fails, a plain code-written text is used and the
page says so.

The agent's text is recorded with the account it was written about and replayed, so the demo
needs no model key.
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from paid_media_agent.assembly import system_prompt_for
from paid_media_agent.domain.reports import NARRATIVE_MAX, Recommendation, ReportInsights
from paid_media_agent.grounding import answer_check, assess, calls_of
from paid_media_agent.harness.loop import Agent
from paid_media_agent.harness.messages import AssistantMessage
from paid_media_agent.harness.models import ChatModel
from paid_media_agent.harness.tools import ToolContext, ToolDispatcher, ToolSpec
from paid_media_agent.store.conversations import ConversationStore
from paid_media_agent.store.db import Store
from paid_media_agent.tools.artifacts import ArtifactStore
from paid_media_agent.tools.calculate import build_calculate_tool
from paid_media_agent.tools.catalog import StaticCatalogProvider, build_authorized_catalog

RECORDED = "demo_narrative.json"
CODE = "Written by code from the figures above"
MAX_RECOMMENDATIONS = 3
TASK = """
## This session

You are writing the summary and next steps of a report page for one simulated ad account. Only
the tools listed are available: `get_unusual_days`, `get_budget_result`, and `calculate`.
"""
PROMPT = """Write the summary and next steps for the {account} report.

First call get_unusual_days and get_budget_result. Then reply with one JSON object and nothing
else:

{{"summary": "...", "recommendations": [{{"target": "...", "action": "...", "evidence": "...",
"expected_effect": "...", "confidence": "low|medium|high", "measurement": "...",
"reversal": "..."}}]}}

- summary: at most 90 words. Lead with what matters most. Say this is a simulated account.
- recommendations: at most three, most important first. Cover what to investigate among the
  unusual days and the next budget moves the result supports. Each needs a concrete action, the
  evidence for it, the effect to expect, how to measure it, and how to reverse it.
- Quote every figure exactly as a tool returned it. Compute anything else with calculate. Do
  not state a figure no tool returned.
- Write for a marketer: plain sentences, no markdown."""


@dataclass
class Narrative:
    summary: str
    recommendations: list[Recommendation] = field(default_factory=list)
    source: str = CODE
    """Who wrote it, as the page states it."""
    model: str | None = None
    tools_called: list[str] = field(default_factory=list)


def recorded_path() -> Path:
    return Path(str(resources.files("paid_media_agent.fixtures.data").joinpath(RECORDED)))


def unusual_days(insights: ReportInsights) -> dict[str, Any]:
    """What the anomaly panel found, as the agent's tool returns it."""
    panel = insights.anomaly
    if panel is None:
        return {"available": False, "note": "no expected ranges could be drawn"}
    days = [
        {
            "campaign": series.entity_name,
            "metric": series.metric,
            "day": day.day.isoformat(),
            "observed": day.observed,
            "expected": day.expected,
            "expected_range": [day.lo, day.hi],
            "what_it_was": day.note,
        }
        for series in panel.series
        for day in series.days
        if day.note
    ]
    return {
        "judged_by": panel.label,
        "finding": panel.headline,
        "days_checked": panel.windows,
        "planted_anomalies_in_those_days": panel.planted,
        "methods_on_the_same_days": [
            {
                "method": score.label,
                "alerts": score.flagged,
                "real_problems_caught": score.caught,
                "false_alarms": score.false_alarms,
            }
            for score in panel.scores
        ],
        "days": days,
        "note": "what_it_was comes from the simulation's truth: on a real account a flag is a "
        "prompt to investigate, not a finding.",
    }


def budget_result(insights: ReportInsights) -> dict[str, Any]:
    """What the budget panel measured and recommends, as the agent's tool returns it."""
    panel = insights.budgets
    if panel is None:
        return {"available": False, "note": "no budget curves could be fitted"}
    body: dict[str, Any] = {"predicted_by": panel.label, "currency": panel.currency}
    if panel.trial is not None:
        trial = panel.trial
        body["trial"] = {
            "finding": panel.headline,
            "weeks": len(trial.weeks),
            "weekly_moves": trial.decisions,
            "conversions_a_day": {run.label: run.per_day for run in trial.runs},
            "gain_against_budgets_left_alone_pct": round(100 * trial.gain, 1),
            "best_possible_gain_pct": round(100 * trial.best_gain, 1),
            "share_of_available_gain_captured_pct": None
            if trial.captured is None
            else round(100 * trial.captured),
            "note": "scored by the simulation's true curves, at the same total budget",
        }
    body["campaigns"] = [
        {
            "campaign": curve.entity_name,
            "spend_limited_by": curve.limited_by,
            "daily_budget_now": curve.current_budget,
            "daily_budget_recommended_next": curve.recommended_budget,
            "expected_conversions_a_day_now": curve.expected_now,
            "expected_conversions_a_day_at_recommended": curve.expected_recommended,
        }
        for curve in panel.curves
    ]
    body["daily_budget_total_now"] = panel.total_now
    body["daily_budget_total_recommended"] = panel.total_recommended
    body["note"] = (
        "Recommendations move at most 25% per campaign per change and change nothing by "
        "themselves; a change needs a proposal and an approval."
    )
    return body


def template_narrative(insights: ReportInsights, currency: str) -> Narrative:
    """A plain text from the panels, for when no agent text is available."""
    parts = ["A simulated account, so the planted anomalies and the true curves are known."]
    if insights.anomaly and insights.anomaly.headline:
        parts.append(insights.anomaly.headline)
    budgets = insights.budgets
    if budgets and budgets.headline:
        parts.append(budgets.headline)
    found = []
    if budgets:
        moves = sorted(
            (c for c in budgets.curves if c.current_budget and c.recommended_budget),
            key=lambda c: -abs((c.recommended_budget or 0) - (c.current_budget or 0)),
        )
        for curve in moves[:2]:
            now, advised = float(curve.current_budget or 0), float(curve.recommended_budget or 0)
            if abs(advised - now) < 1:
                continue
            found.append(
                Recommendation(
                    target=curve.entity_name,
                    action=f"Move the daily budget from {now:,.2f} to {advised:,.2f} {currency}.",
                    evidence="The allocator moves budget toward the campaigns where the next "
                    "unit of spend buys the most.",
                    expected_effect="See the budget section for the expected conversions.",
                    confidence="medium",
                    measurement="Compare matured conversions over the 7 days after the change "
                    "with the expectation.",
                    reversal=f"Restore the daily budget to {now:,.2f} {currency}.",
                )
            )
    return Narrative(summary=" ".join(parts)[:NARRATIVE_MAX], recommendations=found, source=CODE)


def _tool(name: str, description: str, body: dict[str, Any]) -> ToolSpec:
    def run(_args: dict[str, Any], _context: ToolContext) -> str:
        return json.dumps(body, default=str)

    return ToolSpec(
        name=name,
        description=description,
        parameters={"type": "object", "properties": {}},
        handler=run,
    )


def parse_narrative(text: str) -> tuple[str, list[Recommendation]] | None:
    """The summary and recommendations from the agent's reply, or None when it is not valid."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        return None
    try:
        body = json.loads(match.group(0))
        summary = str(body["summary"]).strip()
        recommendations = [
            Recommendation.model_validate(item)
            for item in list(body.get("recommendations") or [])[:MAX_RECOMMENDATIONS]
        ]
    except (ValueError, KeyError, TypeError, ValidationError):
        return None
    if not summary or len(summary) > NARRATIVE_MAX:
        return None
    return summary, recommendations


async def agent_narrative(
    model: ChatModel, insights: ReportInsights, *, account: str, root: Path
) -> Narrative | None:
    """Run the agent loop over the page's figures. None when its reply is not valid or states a
    figure the tools did not return."""
    tools = [
        _tool(
            "get_unusual_days",
            "The days judged against their expected range: what was flagged, what it turned "
            "out to be, and how other methods judged the same days.",
            unusual_days(insights),
        ),
        _tool(
            "get_budget_result",
            "What the weekly budget reallocations bought, measured against the simulation's "
            "truth, and the budgets recommended next.",
            budget_result(insights),
        ),
        build_calculate_tool(),
    ]
    with tempfile.TemporaryDirectory() as scratch:
        artifacts = ArtifactStore(Path(scratch))
        agent = Agent(
            model=model,
            system_prompt=system_prompt_for(root) + TASK,
            dispatcher=ToolDispatcher(
                tools={tool.name: tool for tool in tools},
                catalog_provider=StaticCatalogProvider(build_authorized_catalog([], source="demo")),
                artifacts=artifacts,
                offload_chars=1_000_000,
            ),
            conversations=ConversationStore(Store()),
            gate=lambda _call, _context: False,
            max_model_calls=10,
            check_answer=answer_check(artifacts),
        )
        conversation = await agent.send("demo-narrative", "demo", PROMPT.format(account=account))
    messages = conversation.messages
    answer = next(
        (m.content for m in reversed(messages) if isinstance(m, AssistantMessage) and m.content),
        "",
    )
    parsed = parse_narrative(answer)
    calls = calls_of(messages)
    if parsed is None or not assess(answer, calls, asked=[account]).passed:
        return None
    summary, recommendations = parsed
    return Narrative(
        summary=summary,
        recommendations=recommendations,
        source=f"Written by the agent ({model.name})",
        model=model.name,
        tools_called=[call.name for call in calls],
    )


def save_recorded(
    narrative: Narrative, account_check: dict[str, float], path: Path | None = None
) -> None:
    body = {
        "note": "The demo page's summary and next steps, written once by the agent loop from "
        "the page's own figures and replayed for the same simulated account.",
        "model": narrative.model,
        "account_check": account_check,
        "tools_called": narrative.tools_called,
        "summary": narrative.summary,
        "recommendations": [r.model_dump(mode="json") for r in narrative.recommendations],
    }
    (path or recorded_path()).write_text(json.dumps(body, indent=1) + "\n", encoding="utf-8")


def load_recorded(path: Path | None = None) -> tuple[Narrative, dict[str, float]] | None:
    """The recorded text and the account it was written about, or None when there is none."""
    source = path or recorded_path()
    if not source.exists():
        return None
    try:
        body = json.loads(source.read_text("utf-8"))
        narrative = Narrative(
            summary=str(body["summary"]),
            recommendations=[Recommendation.model_validate(r) for r in body["recommendations"]],
            source=f"Written by the agent ({body['model']}), recorded",
            model=body["model"],
            tools_called=list(body.get("tools_called") or []),
        )
    except (ValueError, KeyError, TypeError, ValidationError):
        return None
    return narrative, dict(body.get("account_check") or {})
