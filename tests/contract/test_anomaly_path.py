"""Anomaly checks through the real paths: the agent's tool, predictor settings, the CLI, and jobs."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from paid_media_agent.analytics.sync import run_sync
from paid_media_agent.cli import main
from paid_media_agent.config import Settings
from paid_media_agent.harness.messages import AssistantMessage
from paid_media_agent.testing.scripted_model import last_tool_results, tool_call_message
from tests.contract.helpers import build_runtime

END = date(2026, 8, 28)


async def _ask(settings: Settings, project_root: Path, calls: list[dict[str, Any]]) -> list[Any]:
    steps = [
        *(lambda _m, args=args: tool_call_message("check_anomalies", args) for args in calls),
        lambda messages: AssistantMessage(json.dumps(last_tool_results(messages))),
    ]
    runtime, _ = build_runtime(settings, project_root, steps)
    await run_sync(
        accounts=runtime.profile.accounts,
        catalog=runtime.catalog,
        dispatcher=runtime.components.read_dispatcher,
        end=END,
    )
    conversation = await runtime.agent.send("a-1", "local-user", "Anything unusual?")
    return [
        json.loads(m.content)
        for m in conversation.messages
        if getattr(m, "name", "") == "check_anomalies"
    ]


async def test_the_agent_checks_synced_history_with_the_local_band(
    settings: Settings, project_root: Path
) -> None:
    checked, unknown = await _ask(
        settings, project_root, [{"window_days": 7}, {"account_alias": "nobody"}]
    )
    assert checked["predictor"] == "local"
    assert checked["methods"] == {"spend": "local_band95", "conversions": "local_band95"}
    assert checked["rows_checked"]["spend"] > 0 and checked["window"] == "2026-08-22..2026-08-28"
    assert "fixture-" not in json.dumps(checked), "provider ids stay host-side"
    for flag in checked.get("flags", []):
        assert "score" not in flag and "band_distance" in flag, "never read as a percentage"
    assert "not a percentage" in checked["note"]
    assert unknown["error"] is True and "unknown account alias" in unknown["detail"]


async def test_the_rule_and_a_tokenless_tabpfn_are_labelled_and_never_call_out(
    settings: Settings, project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    def no_network(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("no request may leave the process")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    (rule,) = await _ask(
        settings.model_copy(update={"paid_media_predictor": "none"}), project_root, [{}]
    )
    assert set(rule["methods"].values()) == {"dod_rule_fallback"}
    (tabpfn,) = await _ask(
        settings.model_copy(update={"paid_media_predictor": "tabpfn"}), project_root, [{}]
    )
    assert tabpfn["predictor"] == "tabpfn"
    assert set(tabpfn["methods"].values()) == {"dod_rule_fallback"}
    assert any("TABPFN_TOKEN is not set" in note for note in tabpfn["notes"])


def test_the_cli_checks_a_simulated_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAID_MEDIA_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("PAID_MEDIA_STATE_PATH", str(tmp_path / "state" / "pma.duckdb"))
    runner = CliRunner()
    made = runner.invoke(
        main,
        ["simulate", "--scenario", "cli", "--days", "80", "--start", "2026-01-01", "--json"],
    )
    assert made.exit_code == 0, made.output
    checked = runner.invoke(
        main, ["anomalies", "--scenario", "cli", "--as-of", "2026-03-22", "--json"]
    )
    assert checked.exit_code == 0, checked.output
    report = json.loads(checked.output)
    assert report["window"] == "2026-03-14..2026-03-20" and report["predictor"] == "local"
    text = runner.invoke(
        main, ["anomalies", "--scenario", "cli", "--as-of", "2026-03-22", "--predictor", "none"]
    )
    assert "by dod_rule_fallback" in text.output


async def test_the_anomalies_job_runs_inside_serve(
    settings: Settings, project_root: Path, tmp_path: Path
) -> None:
    from datetime import datetime

    from paid_media_agent.runtime.self_hosted import build_self_hosted_runtime
    from paid_media_agent.scheduler import Scheduler, build_jobs
    from paid_media_agent.store import Store
    from paid_media_agent.testing.scripted_model import ScriptedChatModel

    runtime = build_self_hosted_runtime(
        settings.model_copy(update={"paid_media_data_mode": "sample"}),
        project_root=project_root,
        model=ScriptedChatModel(steps=[]),
        store=Store(tmp_path / "state.duckdb"),
    )
    # Sync the pinned sample window; pulls are stamped with the real time, so check as of now.
    pinned = lambda: datetime(2026, 8, 29, 7)  # noqa: E731
    syncing = Scheduler(runtime.store, build_jobs(runtime, clock=pinned), clock=pinned)
    assert (await syncing.run_now("sync")).status == "ok"
    run = await Scheduler(runtime.store, build_jobs(runtime)).run_now("anomalies")
    assert run.status == "ok" and run.detail["methods"]["spend"] == "local_band95"
    assert runtime.store.fetch("SELECT count(*) FROM anomaly_checks") == [(1,)]
    runtime.store.close()
