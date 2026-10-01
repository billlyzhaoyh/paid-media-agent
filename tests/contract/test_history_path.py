"""History through the real paths: agent reads, the change log, sync, backfill, and the CLI."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from paid_media_agent.analytics.sync import run_backfill, run_sync
from paid_media_agent.cli import main
from paid_media_agent.config import Settings
from paid_media_agent.domain.common import Platform
from paid_media_agent.harness.messages import AssistantMessage
from paid_media_agent.testing.scripted_model import last_tool_results, tool_call_message
from paid_media_agent.tools.fixtures import FakeWriteProvider, FixtureState
from paid_media_agent.tools.reads import ACCOUNT_ALIAS_ARG
from tests.contract.helpers import (
    build_runtime,
    config,
    execute_step,
    final_step,
    propose_step,
    resume,
    run_until_interrupt,
)

END = date(2026, 8, 28)


async def _list_campaigns(runtime: Any, alias: str = "demo-google") -> None:
    await runtime.components.read_dispatcher.execute(
        "google_ads__list_campaigns", {ACCOUNT_ALIAS_ARG: alias}
    )


async def test_agent_reads_land_in_history_and_a_broken_history_never_fails_a_read(
    settings: Settings, project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = build_runtime(settings, project_root, [])
    dispatcher = runtime.components.read_dispatcher
    result = await dispatcher.execute(
        "meta_ads__get_campaign_performance",
        {ACCOUNT_ALIAS_ARG: "demo-meta", "start_date": "2026-08-15", "end_date": "2026-08-28"},
    )
    store = runtime.profile.store
    assert store.fetch("SELECT source, tool_name, artifact_id, row_count FROM pulls") == [
        ("agent_read", "meta_ads__get_campaign_performance", result.artifact_id, result.row_count)
    ]
    assert store.fetch("SELECT count(*) FROM entity_daily_latest")[0][0] == result.row_count

    def broken(**_kwargs: Any) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(dispatcher._recorder, "record_performance", broken)
    again = await dispatcher.execute(
        "meta_ads__get_campaign_performance",
        {ACCOUNT_ALIAS_ARG: "demo-meta", "start_date": "2026-08-15", "end_date": "2026-08-28"},
    )
    assert again.row_count == result.row_count, "the read still answers"


async def test_the_change_log_follows_a_proposal_and_tells_agent_changes_from_external_ones(
    settings: Settings, project_root: Path
) -> None:
    state = FixtureState()
    runtime, _ = build_runtime(
        settings,
        project_root,
        [propose_step(), execute_step, final_step],
        fixture_state=state,
        write_provider=FakeWriteProvider(state),
    )
    await _list_campaigns(runtime)
    cfg = config()
    await run_until_interrupt(runtime, cfg)
    await resume(runtime, cfg)
    await _list_campaigns(runtime)

    store = runtime.profile.store
    agent = store.fetch(
        "SELECT status, field, before_value, after_value, provider_account_id FROM change_events "
        "WHERE source = 'agent' ORDER BY occurred_at, status"
    )
    assert [row[0] for row in agent] == ["proposed", "approved", "verified"]
    assert agent[-1][1:] == ("daily_budget", "300.0", "240", "fixture-google-0001")
    assert store.fetch("SELECT count(*) FROM change_events WHERE source != 'agent'") == [(0,)]

    campaign = state.campaign(Platform.GOOGLE_ADS, "g-101")
    assert campaign is not None
    campaign["daily_budget"] = 999.0  # someone changed it in the ad platform's UI
    await _list_campaigns(runtime)
    external = store.fetch(
        "SELECT entity_ref, after_value FROM change_events WHERE source = 'external_detected'"
    )
    assert external == [("g-101", "999.0")]


async def test_a_rejected_proposal_is_logged_as_an_override(
    settings: Settings, project_root: Path
) -> None:
    runtime, _ = build_runtime(settings, project_root, [propose_step(), execute_step, final_step])
    cfg = config()
    await run_until_interrupt(runtime, cfg)
    await runtime.agent.resume(cfg.thread_id, cfg.caller_ref, "reject", "not now")
    service = runtime.components.proposal_service
    record = service.proposals.list_for_thread(cfg.thread_id)[0]
    service.reject(record.changeset.proposal_id, actor_ref="reviewer-1", message="not now")
    statuses = runtime.profile.store.fetch(
        "SELECT status FROM change_events ORDER BY occurred_at, status"
    )
    assert [s for (s,) in statuses] == ["proposed", "rejected"]


async def test_sync_repulls_the_trailing_window_and_backfill_walks_chunks(
    settings: Settings, project_root: Path
) -> None:
    runtime, _ = build_runtime(settings, project_root, [])
    kwargs = {
        "accounts": runtime.profile.accounts,
        "catalog": runtime.catalog,
        "dispatcher": runtime.components.read_dispatcher,
    }
    first = await run_sync(**kwargs, end=END, days=14)
    second = await run_sync(**kwargs, end=END, days=14)
    assert first.rows == second.rows > 0 and first.settings == 8 and not first.unavailable
    store = runtime.profile.store
    assert store.fetch(
        "SELECT count(*) FROM pulls WHERE source = 'sync' AND entity_type = 'campaign' "
        "AND requested_start IS NOT NULL"
    ) == [(6,)]
    assert store.fetch("SELECT count(*) FROM entity_daily_latest")[0][0] == first.rows

    back = await run_backfill(**kwargs, start=date(2026, 8, 1), end=END, chunk_days=10)
    # Three accounts by three chunks, each a performance read and a signals read.
    assert len(back.reads) == 3 * 3 * 2 and back.settings == 0 and back.signals > 0
    windows = store.fetch(
        "SELECT requested_start, requested_end FROM pulls WHERE source = 'backfill' "
        "AND platform = 'google_ads' AND NOT list_contains(quality_flags, 'signals') ORDER BY 1"
    )
    assert windows == [
        (date(2026, 8, 1), date(2026, 8, 10)),
        (date(2026, 8, 11), date(2026, 8, 20)),
        (date(2026, 8, 21), date(2026, 8, 28)),
    ]
    missing = await run_sync(**kwargs, end=END, aliases=("nobody",))
    assert missing.unavailable == ["nobody: unknown alias"]


async def test_the_model_reads_history_by_alias_through_query_history(
    settings: Settings, project_root: Path
) -> None:
    steps = [
        lambda _m: tool_call_message(
            "query_history", {"view": "daily", "account_alias": "demo-reddit", "limit": 3}
        ),
        lambda _m: tool_call_message("query_history", {"view": "coverage", "account_alias": "x"}),
        lambda messages: AssistantMessage(json.dumps(last_tool_results(messages))),
    ]
    runtime, _ = build_runtime(settings, project_root, steps)
    await run_sync(
        accounts=runtime.profile.accounts,
        catalog=runtime.catalog,
        dispatcher=runtime.components.read_dispatcher,
        end=END,
    )
    conversation = await runtime.agent.send("h-1", "local-user", "What does history show?")

    results = [
        json.loads(m.content)
        for m in conversation.messages
        if getattr(m, "name", "") == "query_history"
    ]
    daily, unknown = results
    assert daily["row_count"] == 3 and daily["truncated"] is True
    assert {r["account_alias"] for r in daily["rows"]} == {"demo-reddit"}
    assert "fixture-reddit" not in json.dumps(daily), "provider ids stay host-side"
    assert unknown["error"] is True and "unknown account alias" in unknown["detail"]


def test_the_cli_simulates_syncs_and_shows_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAID_MEDIA_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("PAID_MEDIA_STATE_PATH", str(tmp_path / "state" / "pma.duckdb"))
    monkeypatch.setenv("PAID_MEDIA_DATA_MODE", "sample")
    monkeypatch.setenv("PAID_MEDIA_MODEL", "openai:gpt-5.4-mini")
    runner = CliRunner()

    simulated = runner.invoke(
        main, ["simulate", "--scenario", "cli", "--days", "40", "--campaigns", "2", "--json"]
    )
    assert simulated.exit_code == 0, simulated.output
    assert json.loads(simulated.output)["truth_rows"] > 0
    assert runner.invoke(main, ["simulate", "--scenario", "cli"]).exit_code != 0, "no overwrite"
    lag = runner.invoke(main, ["history", "--scenario", "cli", "--view", "lag", "--json"])
    assert json.loads(lag.output)["rows"][0]["age_days"] == 1

    synced = runner.invoke(main, ["sync", "--end", "2026-08-28", "--json"])
    assert synced.exit_code == 0, synced.output
    assert json.loads(synced.output)["rows"] > 0
    shown = runner.invoke(main, ["history", "--view", "coverage"])
    assert shown.exit_code == 0 and "demo-google" in shown.output
    assert (tmp_path / "state" / "pma.duckdb").exists()
    assert (tmp_path / "state" / "sim-cli.duckdb").exists()


def test_sync_asks_serve_to_run_its_job_when_serve_holds_the_state_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from paid_media_agent import cli
    from paid_media_agent.store import StoreBusy

    def busy(_settings: Settings) -> None:
        raise StoreBusy("held by serve")

    posted: list[tuple[str, dict[str, str]]] = []

    def request(
        method: str, url: str, *, headers: dict[str, str], json: object, timeout: float
    ) -> httpx.Response:
        assert method == "POST" and json is None
        posted.append((url, headers))
        summary = {"source": "sync", "start": "a", "end": "b", "rows": 7, "settings": 2}
        detail = {**summary, "reads": ["art_1"], "unavailable": []}
        return httpx.Response(200, json={"status": "ok", "detail": detail})

    monkeypatch.setattr(cli, "_state_runtime", busy)
    monkeypatch.setattr(httpx, "request", request)
    runner = CliRunner()

    monkeypatch.setenv("PAID_MEDIA_API_TOKENS", "")
    assert "PAID_MEDIA_API_TOKENS is not set" in runner.invoke(main, ["sync"]).output
    monkeypatch.setenv("PAID_MEDIA_API_TOKENS", "tok-ops:ops")
    monkeypatch.setenv("PAID_MEDIA_API_HOST", "0.0.0.0")  # noqa: S104 - the bind address in .env
    assert "without options" in runner.invoke(main, ["sync", "--days", "3"]).output
    done = runner.invoke(main, ["sync"])
    assert done.exit_code == 0 and "7 entity-days" in done.output
    assert posted == [("http://127.0.0.1:8080/jobs/sync", {"Authorization": "Bearer tok-ops"})]


async def test_report_reads_are_labelled_as_report_pulls(
    settings: Settings, project_root: Path
) -> None:
    from paid_media_agent.reports.cadence import run_cadence_report

    runtime, _ = build_runtime(settings, project_root, [])
    await run_cadence_report(
        cadence="weekly",
        end=END,
        accounts=runtime.profile.accounts,
        catalog=runtime.catalog,
        dispatcher=runtime.components.read_dispatcher,
        artifacts=runtime.profile.artifacts,
        render=False,
    )
    assert runtime.profile.store.fetch("SELECT DISTINCT source FROM pulls") == [("report",)]


def test_a_report_window_the_data_does_not_cover_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAID_MEDIA_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("PAID_MEDIA_DATA_MODE", "sample")
    # The sample data holds 28 days; a monthly report also needs the 28 before them.
    result = CliRunner().invoke(
        main, ["report", "--cadence", "monthly", "--end", "2026-08-28", "--no-render"]
    )
    assert result.exit_code == 1 and "Traceback" not in result.output
    assert "FAIL report:" in result.output and "the source has no data before" in result.output


async def test_totals_come_from_the_tools_and_match_the_summary_exactly(
    settings: Settings, project_root: Path
) -> None:
    from paid_media_agent.harness.tools import ToolContext
    from paid_media_agent.tools.summary import SummarizeWindowArgs, run_summarize_window

    runtime, _ = build_runtime(settings, project_root, [])
    read = await runtime.components.read_dispatcher.execute(
        "google_ads__get_campaign_performance",
        {ACCOUNT_ALIAS_ARG: "demo-google", "start_date": "2026-08-15", "end_date": "2026-08-28"},
    )
    week = {"start_date": "2026-08-22", "end_date": "2026-08-28"}
    summary = run_summarize_window(
        runtime.profile.artifacts,
        SummarizeWindowArgs(artifact_ids=[read.artifact_id], **week),  # type: ignore[arg-type]
    )
    (headline,) = summary["headline"]
    tools = runtime.components.dispatcher.tools
    context = ToolContext("h-2", "local-user")

    def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return json.loads(tools[name].handler(args, context))  # type: ignore[arg-type]

    grouped = call(
        "query_history",
        {"view": "daily", "account_alias": "demo-google", "group_by": ["account"], **week},
    )
    (account,) = grouped["rows"]
    assert grouped["grouped_by"] == ["account"] and account["days"] == 7
    assert f"{account['spend']:.2f}" == headline["spend"] and account["currency"] == "USD"
    assert account["cpa"] == float(headline["cpa"]), "the ratio of the sums, as the summary has it"

    weekly = call(
        "query_history",
        {"view": "daily", "account_alias": "demo-google", "group_by": ["entity", "week"],
         "fields": ["entity_ref", "week", "spend"], "start_date": "2026-08-15"},
    )  # fmt: skip
    assert set(weekly["rows"][0]) == {"entity_ref", "week", "spend"}
    assert {r["week"] for r in weekly["rows"]} <= {"2026-08-10", "2026-08-17", "2026-08-24"}
    assert call("query_history", {"view": "settings", "group_by": ["day"]})["error"] is True
    unknown = call("query_history", {"view": "daily", "fields": ["nope"], "limit": 1})
    assert unknown["error"] is True and "unknown fields nope" in unknown["detail"]

    total = call("read_artifact", {"artifact_id": read.artifact_id, "group_by": "total", **week})
    (row,) = total["rows"]
    assert row["spend"] == headline["spend"] and row["cpa"] == headline["cpa"]
    assert row["day_coverage"] == 7 and row["currency"] == "USD"
    per_day = call(
        "read_artifact",
        {"artifact_id": read.artifact_id, "group_by": "day", "entity_ref": "g-101", **week},
    )
    assert [r["day"] for r in per_day["rows"]] == [f"2026-08-{d}" for d in range(22, 29)]
    raw = call(
        "read_artifact",
        {"artifact_id": read.artifact_id, "entity_ref": "g-102", "fields": ["day", "spend"]},
    )
    assert raw["row_count"] == 14 and set(raw["rows"][0]) == {"day", "spend"}
    paged = call("read_artifact", {"artifact_id": summary["artifact_id"], "group_by": "total"})
    assert paged["error"] is True and "performance_rows" in paged["detail"]


async def test_a_read_past_the_data_says_so_and_summaries_name_it(
    settings: Settings, project_root: Path
) -> None:
    from paid_media_agent.tools.compute import ComputeError
    from paid_media_agent.tools.summary import SummarizeWindowArgs, run_summarize_window

    runtime, _ = build_runtime(settings, project_root, [])
    empty = await runtime.components.read_dispatcher.execute(
        "google_ads__get_campaign_performance",
        {ACCOUNT_ALIAS_ARG: "demo-google", "start_date": "2026-08-30", "end_date": "2026-08-30"},
    )
    assert empty.row_count == 0
    assert empty.note.startswith("No rows for 2026-08-30..2026-08-30: the data runs through")
    with pytest.raises(ComputeError, match=r"is an empty read\. No rows for 2026-08-30"):
        run_summarize_window(
            runtime.profile.artifacts,
            SummarizeWindowArgs(artifact_ids=[empty.artifact_id], window="last_n_days_of_data"),
        )


async def test_account_totals_compare_accounts_and_say_what_is_still_arriving(
    settings: Settings, project_root: Path
) -> None:
    from datetime import UTC, datetime, timedelta

    from paid_media_agent.harness.tools import ToolContext

    # Data that ends two days ago, as live: its newest days are not yet final.
    today = datetime.now(UTC).date()
    anchor = today - timedelta(days=2)
    anchored = settings.model_copy(update={"paid_media_fixture_anchor": anchor})
    runtime, _ = build_runtime(anchored, project_root, [], fixture_state=FixtureState(anchor))
    await run_sync(
        accounts=runtime.profile.accounts,
        catalog=runtime.catalog,
        dispatcher=runtime.components.read_dispatcher,
        end=today - timedelta(days=1),
    )
    tool = runtime.components.dispatcher.tools["query_history"]
    body = json.loads(
        tool.handler(  # type: ignore[arg-type]
            {
                "view": "daily",
                "group_by": ["account"],
                "start_date": (anchor - timedelta(days=27)).isoformat(),
            },
            ToolContext("h-3", "local-user"),
        )
    )
    accounts = {r["account_alias"]: r for r in body["rows"]}
    assert {"demo-google", "demo-meta"} <= set(accounts)
    assert any(c.startswith("CPA: google_ads (demo-google)") for c in body["comparisons"])
    assert any("own attribution" in c for c in body["caveats"])
    google = accounts["demo-google"]
    assert google["first_day"] <= google["last_day"]
    assert google["conversions_matured"] < google["conversions"], "the newest days are recent"
    assert "still arriving" in google["maturity"]
    assert list(body).index("comparisons") < list(body).index("rows"), "verdicts first"
