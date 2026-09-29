"""Why it changed and what if, through the real paths: the agent's tools, the CLI, and the API."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from paid_media_agent.analytics.sync import run_sync
from paid_media_agent.config import Settings
from paid_media_agent.domain.common import Platform
from paid_media_agent.harness.messages import AssistantMessage
from paid_media_agent.runtime.local import LocalRuntime
from paid_media_agent.store import Store
from paid_media_agent.testing.scripted_model import last_tool_results, tool_call_message
from paid_media_agent.tools.fixtures import FixtureState, load_fixture_dataset, shift_dataset
from tests.contract.helpers import build_runtime

# Pulls are stamped with the real time, so the sample data ends two days ago, as it would live.
TODAY = datetime.now(UTC).date()
ANCHOR = TODAY - timedelta(days=2)
OFFSET = ANCHOR - date(2026, 8, 28)
CURRENT = (date(2026, 8, 17) + OFFSET, date(2026, 8, 23) + OFFSET)
PREVIOUS = (date(2026, 8, 10) + OFFSET, date(2026, 8, 16) + OFFSET)


def _fixture_cpa(window: tuple[date, date]) -> float:
    """Google CPA over a window straight from the shipped fixture rows (the eval's own method)."""
    dataset = shift_dataset(load_fixture_dataset(Platform.GOOGLE_ADS), OFFSET)
    rows = [r for r in dataset["daily"] if window[0] <= date.fromisoformat(r["date"]) <= window[1]]
    spend = sum(Decimal(str(r["spend"])) for r in rows)
    conversions = sum(Decimal(str(r["conversions"])) for r in rows)
    return float(spend / conversions)


async def _synced(settings: Settings, project_root: Path, steps: list[Any]) -> LocalRuntime:
    anchored = settings.model_copy(update={"paid_media_fixture_anchor": ANCHOR})
    runtime, _ = build_runtime(anchored, project_root, steps, fixture_state=FixtureState(ANCHOR))
    await run_sync(
        accounts=runtime.profile.accounts,
        catalog=runtime.catalog,
        dispatcher=runtime.components.read_dispatcher,
        end=TODAY - timedelta(days=1),
    )
    return runtime


async def test_the_agent_explains_a_change_and_forecasts_a_scenario(
    settings: Settings, project_root: Path
) -> None:
    calls = [
        (
            "explain_change",
            {
                "account_aliases": ["demo-google"],
                "current_start": CURRENT[0].isoformat(),
                "current_end": CURRENT[1].isoformat(),
                "previous_start": PREVIOUS[0].isoformat(),
                "previous_end": PREVIOUS[1].isoformat(),
            },
        ),
        ("explain_change", {"account_aliases": ["nobody"]}),
        (
            "what_if_budgets",
            {
                "account_alias": "demo-google",
                "changes": [
                    {"campaign": "g-101", "change": 0.2},
                    {"campaign": "g-103", "change": -0.2},
                ],
            },
        ),
        ("what_if_budgets", {"account_alias": "demo-google", "total_change": 0.1, "split": "best"}),
        (
            "what_if_budgets",
            {"account_alias": "demo-google", "changes": [{"campaign": "zzz", "change": 0.1}]},
        ),
    ]
    steps = [
        *(lambda _m, name=name, args=args: tool_call_message(name, args) for name, args in calls),
        lambda messages: AssistantMessage(json.dumps(last_tool_results(messages))),
    ]
    runtime = await _synced(settings, project_root, steps)
    conversation = await runtime.agent.send("e-1", "local-user", "Why did CPA move? What if?")
    results = [
        json.loads(m.content)
        for m in conversation.messages
        if getattr(m, "name", "") in ("explain_change", "what_if_budgets")
    ]
    explained, unknown, mixed, total, bad = results

    (report,) = explained["reports"]
    assert report["previous"] == pytest.approx(_fixture_cpa(PREVIOUS), abs=0.01)
    assert report["current"] == pytest.approx(_fixture_cpa(CURRENT), abs=0.01)
    assert sum(e["points"] for e in report["effects"]) == pytest.approx(
        report["change"] * 100, abs=0.05
    )
    assert report["drivers"] and report["reading"].startswith("CPA ")
    assert unknown["error"] is True and "list_accounts" in unknown["detail"]

    assert mixed["scenario"] == "g-101 +20%, g-103 -20%"
    moved = {c["campaign"]: c for c in mixed["campaigns"]}
    assert moved["g-101"]["budget"] == pytest.approx(moved["g-101"]["budget_now"] * 1.2, abs=0.01)
    assert mixed["difference"]["conversions"]["low"] <= mixed["difference"]["conversions"]["high"]
    assert mixed["reading"] and "nothing has changed" in mixed["note"]
    assert total["forecast"]["daily_budget"] == pytest.approx(
        total["baseline"]["daily_budget"] * 1.1, rel=1e-3
    )
    assert bad["error"] is True and "unknown campaign zzz" in bad["detail"]
    for result in (explained, mixed, total):
        assert "fixture-" not in json.dumps(result), "provider ids stay host-side"


def test_the_cli_explains_and_forecasts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from paid_media_agent.cli import main

    monkeypatch.setenv("PAID_MEDIA_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("PAID_MEDIA_STATE_PATH", str(tmp_path / "state" / "pma.duckdb"))
    monkeypatch.setenv("PAID_MEDIA_DATA_MODE", "sample")
    monkeypatch.setenv("PAID_MEDIA_FIXTURE_ANCHOR", ANCHOR.isoformat())
    runner = CliRunner()
    assert runner.invoke(main, ["sync"]).exit_code == 0
    span = f"{CURRENT[0].isoformat()}:{CURRENT[1].isoformat()}"
    explained = runner.invoke(main, ["explain", "--alias", "demo-google", "--current", span])
    assert explained.exit_code == 0, explained.output
    assert explained.output.startswith("demo-google: CPA ")
    as_json = json.loads(
        runner.invoke(
            main, ["explain", "--alias", "demo-google", "--current", span, "--json"]
        ).output
    )
    assert as_json["reports"][0]["previous_window"]["end"] == PREVIOUS[1].isoformat()
    assert runner.invoke(main, ["explain", "--current", "2026-08-01"]).exit_code != 0

    assert (
        runner.invoke(
            main, ["goals", "set", "--alias", "demo-google", "--target-cpa", "30",
                   "--monthly-budget", "25000"]
        ).exit_code
        == 0
    )  # fmt: skip
    forecast = runner.invoke(
        main, ["whatif", "--alias", "demo-google", "--set", "g-101=+20%", "--set", "g-103=-20%"]
    )
    assert forecast.exit_code == 0, forecast.output
    assert "g-101 +20%, g-103 -20%" in forecast.output and "monthly budget" in forecast.output
    assert "target" in forecast.output
    best = json.loads(
        runner.invoke(
            main,
            ["whatif", "--alias", "demo-google", "--total", "+10%", "--split", "best", "--json"],
        ).output
    )
    assert best["scenario"].endswith("split by the curves") and best["best_split"] is None
    assert runner.invoke(main, ["whatif", "--alias", "demo-google"]).exit_code != 0
    assert (
        runner.invoke(main, ["whatif", "--alias", "demo-google", "--set", "g-101=lots"]).exit_code
        != 0
    )


async def test_the_api_explains_and_forecasts(settings: Settings, project_root: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from paid_media_agent.surfaces.api.app import create_app

    runtime = await _synced(settings, project_root, [])

    class Holder:
        pass

    holder: Any = Holder()
    holder.settings = settings.model_copy(
        update={"paid_media_api_tokens": SecretStr("tok:someone")}
    )
    holder.agent = runtime.agent
    holder.components = runtime.components
    holder.profile = runtime.profile
    holder.catalog = runtime.catalog
    holder.threads = Store().repositories.threads
    holder.persistence = "memory"
    client = TestClient(create_app(holder))
    auth = {"Authorization": "Bearer tok"}

    explained = client.get("/explain?alias=demo-google&metric=conversions", headers=auth)
    assert explained.status_code == 200, explained.text
    assert explained.json()["reports"][0]["metric"] == "conversions"
    assert client.get("/explain?alias=nobody", headers=auth).status_code == 404
    assert client.get("/explain", headers={}).status_code == 401

    body = {"account_alias": "demo-google", "total_daily_budget": 500}
    forecast = client.post("/what-if", json=body, headers=auth)
    assert forecast.status_code == 200, forecast.text
    assert forecast.json()["forecast"]["daily_budget"] == pytest.approx(500, rel=1e-3)
    assert (
        client.post("/what-if", json={"account_alias": "demo-google"}, headers=auth).status_code
        == 422
    )
    assert (
        client.post("/what-if", json={**body, "account_alias": "x"}, headers=auth).status_code
        == 404
    )
