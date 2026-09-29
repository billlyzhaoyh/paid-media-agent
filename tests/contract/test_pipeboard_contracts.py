"""Live read contracts against Pipeboard-shaped payloads, through the real MCP read provider.

The payloads in `tests/fixtures/pipeboard/` are built from Pipeboard's published Meta server source
(and, for Google, from its CLI's documented tool and a guessed shape). Each file says so. When a
token arrives, `paid-media-agent doctor --live` checks the same path against the real servers.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from paid_media_agent.analytics.ingest import AnalyticsRecorder
from paid_media_agent.analytics.live_check import run_live_checks
from paid_media_agent.analytics.sync import run_backfill, run_sync
from paid_media_agent.bandit.recommend import BanditConfig, recommend
from paid_media_agent.config import AccountBinding, AccountRegistry, Settings
from paid_media_agent.domain.common import Platform
from paid_media_agent.store import Store
from paid_media_agent.tools.artifacts import ArtifactStore
from paid_media_agent.tools.catalog import DEFAULT_LOCAL_POLICY, ToolClass
from paid_media_agent.tools.contracts import contract_for, reviewed_reads
from paid_media_agent.tools.pipeboard import (
    McpResult,
    McpTool,
    PipeboardCatalogLoader,
    PipeboardReadProvider,
)
from paid_media_agent.tools.providers import ProviderError, ProviderRateLimited
from paid_media_agent.tools.reads import ReadDispatcher

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pipeboard"
TODAY = datetime.now(UTC).date()
END = TODAY - timedelta(days=1)
"""Pulls are stamped with the real time and the bandit only uses what was pulled by its decision
day, so the synced days end yesterday, as they would live."""
PURCHASE = "offsite_conversion.fb_pixel_purchase"


def _load(relative: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / relative).read_text())
    data.pop("_provenance", None)
    return data


class FakePipeboard:
    """Serves the fixture tool lists and payloads the way the published servers wrap them."""

    def __init__(
        self,
        *,
        google_shape: str = "nested",
        drop_args: dict[str, str] | None = None,
        budget_scale: int = 1,
        fail_platform: Platform | None = None,
        fail_signals: bool = False,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.queued: dict[str, list[dict[str, Any]]] = {}
        self.google_shape = google_shape
        self.drop_args = drop_args or {}
        self.budget_scale = budget_scale
        self.fail_platform = fail_platform
        self.fail_signals = fail_signals

    async def list_tools(self, platform: Platform, url: str) -> list[McpTool]:
        if platform is self.fail_platform:
            raise RuntimeError("401 invalid_token")
        source = {Platform.META_ADS: "meta", Platform.GOOGLE_ADS: "google"}.get(platform)
        if source is None:
            return []
        tools = []
        for tool in _load(f"{source}/tools.json")["tools"]:
            schema = json.loads(json.dumps(tool["inputSchema"]))
            schema["properties"].pop(self.drop_args.get(tool["name"], ""), None)
            tools.append(McpTool(tool["name"], tool["description"], schema, None))
        return tools

    async def call_tool(self, url: str, name: str, arguments: dict[str, Any]) -> McpResult:
        self.calls.append((name, arguments))
        if self.queued.get(name):
            payload = self.queued[name].pop(0)
        elif name == "get_insights":
            payload = _load("meta/get_insights_day.json")
            span = arguments["time_range"]
            for row in payload["data"]:
                row["date_start"], row["date_stop"] = span["since"], span["until"]
        elif name == "get_campaigns":
            page = "page2" if arguments.get("after") else "page1"
            payload = _load(f"meta/get_campaigns_{page}.json")
            for item in payload["data"]:
                for key in ("daily_budget", "lifetime_budget"):
                    if key in item:
                        item[key] = str(int(item[key]) * self.budget_scale)
        elif name == "execute_google_ads_gaql_query" and "lost_impression_share" in str(
            arguments["query"]
        ):
            if self.fail_signals:
                return McpResult(is_error=True, structured=None, text="unrecognized field")
            payload = _load("google/gaql_signals.json")
        elif name == "execute_google_ads_gaql_query":
            payload = (
                _load(f"google/gaql_performance_{self.google_shape}.json")
                if "segments.date" in arguments["query"]
                else _load("google/gaql_settings.json")
            )
        else:
            raise AssertionError(f"unexpected tool {name}")
        text = json.dumps(payload)
        if url.startswith("https://meta"):
            # FastMCP wraps a str return as structured {"result": "<json>"} plus a text part.
            return McpResult(is_error=False, structured={"result": text}, text=text)
        return McpResult(is_error=False, structured=None, text=text)


ACCOUNTS = AccountRegistry(
    bindings=(
        AccountBinding(
            alias="meta-us",
            platform=Platform.META_ADS,
            provider_account_id="act_1234567890",
            currency="USD",
            timezone="America/New_York",
            conversion_action=PURCHASE,
        ),
        AccountBinding(
            alias="meta-jp",
            platform=Platform.META_ADS,
            provider_account_id="act_5550001111",
            currency="JPY",
            timezone="Asia/Tokyo",
        ),
        AccountBinding(
            alias="google",
            platform=Platform.GOOGLE_ADS,
            provider_account_id="1234567890",
            currency="USD",
            timezone="America/New_York",
        ),
    )
)


async def _wire(
    settings: Settings, tmp_path: Path, client: FakePipeboard, **provider: Any
) -> SimpleNamespace:
    configured = settings.model_copy(
        update={
            "pipeboard_api_token": SecretStr("test-token"),
            "pipeboard_meta_ads_mcp_url": "https://meta-ads.mcp.pipeboard.co/",
        }
    )
    loader = PipeboardCatalogLoader(
        settings=configured,
        policy=DEFAULT_LOCAL_POLICY.model_copy(update={"reviewed_reads": reviewed_reads()}),
        client=client,
    )
    catalog = await loader.refresh()
    store = Store()
    dispatcher = ReadDispatcher(
        catalog_provider=loader,
        accounts=ACCOUNTS,
        provider=PipeboardReadProvider(loader, **provider),
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        recorder=AnalyticsRecorder(store),
    )
    return SimpleNamespace(
        settings=configured,
        catalog=catalog,
        store=store,
        loader=loader,
        dispatcher=dispatcher,
        client=client,
        profile=SimpleNamespace(accounts=ACCOUNTS, catalog_provider=loader),
        components=SimpleNamespace(read_dispatcher=dispatcher),
    )


def _sync_kwargs(wired: SimpleNamespace) -> dict[str, Any]:
    return {"accounts": ACCOUNTS, "catalog": wired.catalog, "dispatcher": wired.dispatcher}


async def test_meta_reads_are_admitted_and_sync_one_day_per_call_into_currency(
    settings: Settings, tmp_path: Path
) -> None:
    wired = await _wire(settings, tmp_path, FakePipeboard())
    insights = wired.catalog.get("meta_ads__get_insights")
    assert insights.tool_class is ToolClass.READ and insights.policy.reason == "reviewed_read"
    assert insights.account_arg == "account_id"
    details = wired.catalog.get("meta_ads__get_campaign_details")
    assert details.tool_class is ToolClass.DENIED, "no account scope, so never a read"
    contract = contract_for(wired.catalog, Platform.META_ADS)
    assert contract is not None and contract.name == "pipeboard_meta"

    run = await run_sync(**_sync_kwargs(wired), end=END, days=28, aliases=("meta-us",))
    assert run.unavailable == [] and run.contracts == {
        "meta-us": "pipeboard_meta (published_source)"
    }
    insight_calls = [a for n, a in wired.client.calls if n == "get_insights"]
    assert len(insight_calls) == 8, "per-day contract re-pulls only its 8-day maturity window"
    assert insight_calls[0] == {
        "level": "campaign",
        "limit": 500,
        "time_range": {"since": END.isoformat(), "until": END.isoformat()},
        "account_id": "act_1234567890",
    }, "the newest day first, so a call limit never drops yesterday"
    assert insight_calls[-1]["time_range"]["since"] == (END - timedelta(days=7)).isoformat()
    assert [a.get("after") for n, a in wired.client.calls if n == "get_campaigns"] == [
        None,
        "QVFIUm",
    ]
    assert run.calls == 10 and run.rows == 24 and run.settings == 4

    rows = wired.store.fetch_dicts(
        "SELECT entity_ref, spend::DOUBLE AS spend, conversions::DOUBLE AS conversions, "
        "conversion_value::DOUBLE AS value FROM entity_daily_latest "
        "WHERE account_alias = 'meta-us' AND day = ? ORDER BY entity_ref",
        [END],
    )
    assert rows == [
        {"entity_ref": "120210000000001", "spend": 40.12, "conversions": 3.0, "value": 150.0},
        {"entity_ref": "120210000000002", "spend": 19.5, "conversions": 2.0, "value": 88.4},
        {"entity_ref": "120210000000003", "spend": 3.1, "conversions": 0.0, "value": 0.0},
    ]
    budgets = wired.store.fetch(
        "SELECT entity_ref, daily_budget::DOUBLE, budget_type FROM entity_settings_snapshots "
        "WHERE account_alias = 'meta-us' ORDER BY entity_ref"
    )
    assert budgets == [
        ("120210000000001", 50.0, "daily"),
        ("120210000000002", 25.0, "daily"),
        ("120210000000003", None, "lifetime"),
        ("120210000000004", None, "ad_set_budgets"),
    ]

    run = await recommend(
        wired.store,
        None,
        as_of=TODAY,
        config=BanditConfig(policy="greedy"),
        account_alias="meta-us",
        mode="recommend",
    )
    decisions = {d["entity_ref"]: d for d in run.as_json()["decisions"]}
    assert decisions["120210000000001"]["current_budget"] == pytest.approx(50.0)
    assert decisions["120210000000002"]["current_budget"] == pytest.approx(25.0)
    assert "no daily budget" in decisions["120210000000003"]["reason"]


async def test_meta_without_a_conversion_action_records_conversions_as_missing(
    settings: Settings, tmp_path: Path
) -> None:
    wired = await _wire(settings, tmp_path, FakePipeboard())
    result = await wired.dispatcher.execute(
        "meta_ads__get_insights",
        {
            "account_alias": "meta-jp",
            "level": "campaign",
            "time_range": {"since": "2026-09-01", "until": "2026-09-01"},
        },
    )
    assert result.artifact_kind == "performance_rows" and "conversions" in result.missing_fields
    assert PURCHASE in result.preview["action_types"]
    assert result.requested_window == "2026-09-01..2026-09-01"
    stored = wired.store.fetch("SELECT count(*), count(conversions) FROM entity_daily_snapshots")
    assert stored == [(3, 0)], "missing, not zero"

    await run_sync(**_sync_kwargs(wired), end=END, days=1, aliases=("meta-jp",))
    yen = wired.store.fetch(
        "SELECT daily_budget::DOUBLE FROM entity_settings_snapshots "
        "WHERE account_alias = 'meta-jp' AND entity_ref = '120210000000001'"
    )
    assert yen == [(5000.0,)], "JPY has no minor unit"


async def test_a_multi_day_meta_total_stays_out_of_daily_history(
    settings: Settings, tmp_path: Path
) -> None:
    wired = await _wire(settings, tmp_path, FakePipeboard())
    result = await wired.dispatcher.execute(
        "meta_ads__get_insights",
        {
            "account_alias": "meta-us",
            "level": "campaign",
            "time_range": {"since": "2026-09-01", "until": "2026-09-07"},
        },
    )
    assert result.artifact_kind == "provider_result"
    assert wired.store.fetch("SELECT count(*) FROM entity_daily_snapshots") == [(0,)]


async def test_errors_in_the_payload_raise_and_rate_limits_retry_with_backoff(
    settings: Settings, tmp_path: Path
) -> None:
    pauses: list[float] = []

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)

    client = FakePipeboard()
    wired = await _wire(settings, tmp_path, client, sleep=sleep)
    limited = _load("meta/error_rate_limited.json")
    client.queued["get_insights"] = [limited, limited]
    args = {
        "account_alias": "meta-us",
        "level": "campaign",
        "time_range": {"since": "2026-09-01", "until": "2026-09-01"},
    }
    result = await wired.dispatcher.execute("meta_ads__get_insights", args)
    assert result.row_count == 3 and pauses == [2.0, 4.0]

    client.queued["get_insights"] = [limited] * 4
    with pytest.raises(ProviderRateLimited, match="code 17"):
        await wired.dispatcher.execute("meta_ads__get_insights", args)
    assert pauses == [2.0, 4.0, 2.0, 4.0, 8.0]

    invalid = {"error": {"message": "HTTP Error: 400", "details": {"error": {"code": 100}}}}
    client.queued["get_insights"] = [invalid]
    with pytest.raises(ProviderError, match="code 100") as caught:
        await wired.dispatcher.execute("meta_ads__get_insights", args)
    assert not isinstance(caught.value, ProviderRateLimited)


async def test_sync_stops_at_the_call_limit_and_says_what_it_skipped(
    settings: Settings, tmp_path: Path
) -> None:
    wired = await _wire(settings, tmp_path, FakePipeboard())
    run = await run_backfill(
        **_sync_kwargs(wired),
        start=date(2026, 9, 1),
        end=END,
        aliases=("meta-us", "google"),
        max_calls=5,
    )
    assert run.calls <= 5 and len(wired.client.calls) == run.calls
    (stopped,) = run.unavailable
    assert stopped.startswith("meta-us 2026-09-2") and "share of the call limit" in stopped
    assert "google" in run.contracts, "one account's per-day calls never starve the next"
    meta_days = [a["time_range"]["since"] for n, a in wired.client.calls if n == "get_insights"]
    assert meta_days == sorted(meta_days, reverse=True), "newest days first"


@pytest.mark.parametrize("shape", ["nested", "dotted"])
async def test_google_gaql_rows_normalize_from_either_shape(
    settings: Settings, tmp_path: Path, shape: str
) -> None:
    wired = await _wire(settings, tmp_path, FakePipeboard(google_shape=shape))
    run = await run_sync(**_sync_kwargs(wired), end=date(2026, 9, 1), days=1, aliases=("google",))
    assert run.contracts == {"google": "pipeboard_google_gaql (unverified)"}
    assert run.rows == 2 and run.settings == 3 and run.unavailable == []
    query = wired.client.calls[0][1]
    assert query["customer_id"] == "1234567890"
    assert "BETWEEN '2026-09-01' AND '2026-09-01'" in query["query"]
    rows = wired.store.fetch(
        "SELECT entity_ref, entity_name, spend::DOUBLE, conversions::DOUBLE, "
        "conversion_value::DOUBLE FROM entity_daily_latest ORDER BY entity_ref"
    )
    assert rows == [
        ("111", "Search - Brand", 25.5, 12.0, 960.5),
        ("222", "PMax - All", 80.0, 9.5, 1210.0),
    ]
    settings_rows = wired.store.fetch(
        "SELECT entity_ref, status, daily_budget::DOUBLE, budget_type, target_cpa::DOUBLE, "
        "target_roas::DOUBLE FROM entity_settings_snapshots ORDER BY entity_ref"
    )
    assert settings_rows == [
        ("111", "ENABLED", 30.0, "daily", 40.0, None),
        ("222", "ENABLED", 90.0, "daily", None, 3.5),
        ("333", "PAUSED", None, "shared", None, None),
    ]


async def test_doctor_live_confirms_the_path_and_names_what_to_fix(
    settings: Settings, tmp_path: Path
) -> None:
    now = date(2026, 9, 21)

    def clock() -> datetime:
        return datetime(now.year, now.month, now.day, 12)

    wired = await _wire(settings, tmp_path, FakePipeboard())
    checks = {c.name: c for c in await run_live_checks(wired, aliases=("meta-us",), clock=clock)}
    assert checks["live:meta-us:contract"].detail == "pipeboard_meta, published source"
    assert checks["live:meta-us:read"].status == "ok"
    assert checks["live:meta-us:budgets"].status == "ok"
    assert "about 9 provider calls" in checks["live:meta-us:calls"].detail
    assert all(c.ok for c in checks.values()), checks

    no_action = {c.name: c for c in await run_live_checks(wired, aliases=("meta-jp",), clock=clock)}
    assert PURCHASE in no_action["live:meta-jp:conversions"].detail

    cents = await _wire(settings, tmp_path, FakePipeboard(budget_scale=100))
    wrong = {c.name: c for c in await run_live_checks(cents, aliases=("meta-us",), clock=clock)}
    assert wrong["live:meta-us:budgets"].status == "fail"
    assert "unit is probably wrong" in wrong["live:meta-us:budgets"].detail

    missing = await _wire(settings, tmp_path, FakePipeboard(drop_args={"get_insights": "level"}))
    gaps = {c.name: c for c in await run_live_checks(missing, aliases=("meta-us",), clock=clock)}
    assert gaps["live:meta-us:schema"].status == "fail"
    assert "get_insights: schema lacks level" in gaps["live:meta-us:schema"].detail

    down = await _wire(settings, tmp_path, FakePipeboard(fail_platform=Platform.META_ADS))
    assert down.loader.failures == {Platform.META_ADS: "RuntimeError: 401 invalid_token"}
    failed = await run_live_checks(down, aliases=("meta-us",), clock=clock)
    assert failed[0].status == "fail" and "catalog did not load" in failed[0].detail


async def test_google_signals_say_what_limits_spend_and_never_block_the_sync(
    settings: Settings, tmp_path: Path
) -> None:
    wired = await _wire(settings, tmp_path, FakePipeboard())
    run = await run_sync(**_sync_kwargs(wired), end=date(2026, 9, 1), days=1, aliases=("google",))
    assert run.unavailable == [] and run.signals == 2 and run.rows == 2
    query = [a["query"] for n, a in wired.client.calls if "lost_impression_share" in a["query"]]
    assert len(query) == 1 and "BETWEEN '2026-09-01' AND '2026-09-01'" in query[0]
    shares = wired.store.fetch(
        "SELECT entity_ref, impression_share, budget_lost_share, rank_lost_share "
        "FROM entity_daily_signals ORDER BY entity_ref"
    )
    assert shares == [("111", 0.61, 0.27, 0.12), ("222", None, None, None)]
    status = wired.store.fetch(
        "SELECT entity_ref, channel_type, status_reasons, bidding_status, "
        "recommended_budget::DOUBLE FROM entity_delivery_status ORDER BY entity_ref"
    )
    assert status == [
        ("111", "SEARCH", ["BUDGET_CONSTRAINED"], "LIMITED_BY_BUDGET", 45.0),
        ("222", "PERFORMANCE_MAX", ["BIDDING_STRATEGY_CONSTRAINED"], "ENABLED", None),
    ]

    failing = await _wire(settings, tmp_path, FakePipeboard(fail_signals=True))
    run = await run_sync(**_sync_kwargs(failing), end=date(2026, 9, 1), days=1, aliases=("google",))
    assert run.rows == 2 and run.settings == 3, "performance and settings still land"
    assert run.unavailable == ["google signals: ProviderError"]
