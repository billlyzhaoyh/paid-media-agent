"""The visual demo end to end: a report page for a simulated store, with no token or network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from paid_media_agent.config import Settings
from paid_media_agent.harness.messages import AssistantMessage
from paid_media_agent.testing import demo_narrative, demo_visual
from paid_media_agent.testing.demo_narrative import (
    CODE,
    agent_narrative,
    budget_result,
    parse_narrative,
    unusual_days,
)
from paid_media_agent.testing.demo_visual import ACCOUNT, DEMO, run_visual_demo
from paid_media_agent.testing.scripted_model import ScriptedChatModel, tool_call_message

HEADINGS = (
    "What was unusual",
    "What the budget moves bought",
    "Next steps",
    "A change, approved and verified",
)
DROPPED = ("Account performance", "All accounts")


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the demo must not use the network")

    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)


async def test_the_demo_replays_its_recordings_with_no_network_even_with_a_token(
    settings: Settings, project_root: Path, tmp_path: Path, no_network: None
) -> None:
    # A token is set, as on the machine that recorded: a plain demo still never calls out.
    with_token = settings.model_copy(update={"tabpfn_token": SecretStr("not-used")})
    result = await run_visual_demo(
        with_token, root=project_root, state_dir=tmp_path / "state", open_browser=False
    )
    html = Path(result["html"]).read_text("utf-8")
    assert Path(result["html"]).name == "demo_report.html"
    for heading in HEADINGS:
        assert heading in html
    for heading in DROPPED:
        assert heading not in html, "sections that say nothing about one account are left out"
    assert result["ranges"] == {"label": "TabPFN, 95% expected range", "source": "recorded"}
    assert result["budgets"]["source"] == "recorded" and result["tabpfn_tokens_billed"] == 0
    assert result["weekly_predictions"] == "recorded"
    assert (
        result["narrative"].startswith("Written by the agent (")
        and "recorded" in result["narrative"]
    )
    trial = result["trial"]
    left, agent, best = trial["conversions_a_day"].values()
    assert left < agent <= best and 0 < trial["gain"] <= trial["best_gain"]
    assert 0 < trial["captured"] <= 1
    assert f"{ACCOUNT} · paid media review" in html and "Brand Search" in html
    assert "recorded answers" in html and "no call was made" in html
    assert "caught" in html and "false alarm" in html, "flags say what they turned out to be"
    assert "status=verified" in html, "the approved change and its readback are on the page"
    # How the model is applied: the rows that went in, the range that came out, what it decides.
    assert html.count("How TabPFN is applied") == 2
    assert "What goes in" in html and "What comes out" in html and "The verdict" in html
    assert '<span class="ask">?</span>' in html and "predicted</td>" in html
    assert "Next 100 a day buys" in html and "Before the agent (true)" in html
    assert "The range moves with the budget." in html
    assert "The range is as wide as the campaign is noisy." in html

    # A second run reuses the store and says the same thing.
    again = await run_visual_demo(
        with_token, root=project_root, state_dir=tmp_path / "state", open_browser=False
    )
    assert again["trial"] == trial and again["ranges"] == result["ranges"]


async def test_without_recordings_local_models_and_code_write_the_page_and_say_so(
    settings: Settings,
    project_root: Path,
    tmp_path: Path,
    no_network: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(demo_visual, "recorded_path", lambda: tmp_path / "missing.json")
    monkeypatch.setattr(demo_narrative, "recorded_path", lambda: tmp_path / "missing-text.json")
    result = await run_visual_demo(
        settings, root=project_root, state_dir=tmp_path / "state", open_browser=False
    )
    assert result["ranges"] == {"label": "Local model, 95% expected range", "source": "local"}
    assert result["budgets"]["label"] == "Pooled regression as the global model"
    assert result["weekly_predictions"] == "local" and result["narrative"] == CODE
    html = Path(result["html"]).read_text("utf-8")
    assert "computed locally" in html and CODE in html
    assert "TabPFN, 95% expected range" not in html
    assert "How TabPFN is applied" not in html
    assert "How the local model is applied" in html
    assert "How the pooled regression is applied" in html
    assert result["trial"]["gain"] > 0, "the pooled model's moves still beat budgets left alone"


async def test_a_recording_for_another_store_is_not_applied(
    settings: Settings,
    project_root: Path,
    tmp_path: Path,
    no_network: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.loads(demo_visual.recorded_path().read_text("utf-8"))
    body["account_check"]["spend"] += 1
    other = tmp_path / "other.json"
    other.write_text(json.dumps(body))
    monkeypatch.setattr(demo_visual, "recorded_path", lambda: other)
    result = await run_visual_demo(
        settings, root=project_root, state_dir=tmp_path / "state", open_browser=False
    )
    assert result["weekly_predictions"] == "local", "the recorded decisions built another store"
    assert result["ranges"]["source"] == "local" and result["narrative"] == CODE
    assert body["scenario"]["seed"] == DEMO.seed


async def _insights(settings: Settings, project_root: Path, tmp_path: Path) -> Any:
    """The demo's panels, from the recordings."""
    from paid_media_agent.reports.insights import build_insights
    from paid_media_agent.sim.scenario import scenario_binding
    from paid_media_agent.testing.demo_visual import (
        AS_OF,
        CONFIG,
        WINDOW_DAYS,
        demo_predictors,
        demo_store,
        demo_truth,
    )

    store, built = await demo_store(settings, tmp_path / "state")
    try:
        (predictor, *_rest) = demo_predictors(settings, store, built)
        return await build_insights(
            store, predictor, account_alias=scenario_binding(DEMO).alias, as_of=AS_OF,
            window_days=WINDOW_DAYS, truth=demo_truth(), currency="USD", trial=built.trial,
            config=CONFIG,
        )  # fmt: skip
    finally:
        store.close()


async def test_the_agent_writes_the_text_and_an_invented_figure_is_refused(
    settings: Settings, project_root: Path, tmp_path: Path, no_network: None
) -> None:
    insights = await _insights(settings, project_root, tmp_path)
    facts = budget_result(insights)
    per_day = facts["trial"]["conversions_a_day"]
    left, agent = per_day["Budgets left alone"], per_day["The agent, with TabPFN"]
    assert unusual_days(insights)["days"], "the tool names the flagged days"

    def reply(conversions: float) -> str:
        return json.dumps(
            {
                "summary": f"A simulated account. The agent's moves produced {conversions} "
                f"conversions a day against {left} with budgets left alone.",
                "recommendations": [
                    {
                        "target": "Shopping", "action": "Keep the current split.",
                        "evidence": f"{conversions} conversions a day.",
                        "expected_effect": "No change.", "confidence": "medium",
                        "measurement": "Watch weekly conversions.", "reversal": "Not needed.",
                    }
                ],
            }
        )  # fmt: skip

    def script(final: str) -> ScriptedChatModel:
        return ScriptedChatModel(
            steps=[
                lambda _m: tool_call_message("get_unusual_days", {}),
                lambda _m: tool_call_message("get_budget_result", {}),
                lambda _m: AssistantMessage(final),
                lambda _m: AssistantMessage(final),  # the same again when asked to repair
            ]
        )

    written = await agent_narrative(
        script(reply(agent)), insights, account=ACCOUNT, root=project_root
    )
    assert written is not None and written.source.startswith("Written by the agent (")
    assert written.tools_called == ["get_unusual_days", "get_budget_result"]
    assert f"{agent} conversions a day" in written.summary and len(written.recommendations) == 1

    invented = await agent_narrative(
        script(reply(123.45)), insights, account=ACCOUNT, root=project_root
    )
    assert invented is None, "a figure no tool returned is not published"
    assert parse_narrative("no json here") is None
    assert parse_narrative('{"summary": "", "recommendations": []}') is None
