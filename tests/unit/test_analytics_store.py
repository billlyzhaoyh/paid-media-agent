"""History views: latest and matured snapshots, the lag curve, settings versions, and changes."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal

from paid_media_agent.analytics.history import query_history
from paid_media_agent.analytics.ingest import AnalyticsRecorder
from paid_media_agent.config import AccountBinding
from paid_media_agent.domain.common import EntityType, Platform
from paid_media_agent.domain.metrics import MetricWindow, PerformanceRow
from paid_media_agent.store import Store

BINDING = AccountBinding(
    alias="acme-google",
    platform=Platform.GOOGLE_ADS,
    provider_account_id="123-456-7890",
    currency="USD",
    timezone="America/New_York",
)
DAY = date(2026, 3, 2)


def _row(
    ref: str, day: date, spend: str, conversions: str | None, complete: bool = True
) -> PerformanceRow:
    return PerformanceRow(
        platform=Platform.GOOGLE_ADS,
        account_ref=BINDING.alias,
        entity_type=EntityType.CAMPAIGN,
        entity_ref=ref,
        entity_name=f"Campaign {ref}",
        window=MetricWindow(start=day, end=day, timezone=BINDING.timezone, is_complete=complete),
        currency="USD",
        spend=Decimal(spend),
        conversions=None if conversions is None else Decimal(conversions),
        conversion_value=None,
    )


def _pull(recorder: AnalyticsRecorder, rows: list[PerformanceRow], age_days: int) -> None:
    """A pull made `age_days` after DAY, at noon UTC (morning in New York, the same date)."""
    recorder.record_performance(
        source="sync",
        binding=BINDING,
        tool_name="google_ads__get_campaign_performance",
        catalog_revision="rev",
        rows=rows,
        pulled_at=datetime.combine(DAY + timedelta(days=age_days), datetime.min.time()).replace(
            hour=12
        ),
    )


def test_every_pull_is_kept_and_the_latest_view_has_one_row_per_entity_day() -> None:
    store = Store()
    recorder = AnalyticsRecorder(store)
    _pull(recorder, [_row("c1", DAY, "100", "2", complete=False)], age_days=1)
    _pull(recorder, [_row("c1", DAY, "101.5", "5")], age_days=3)

    assert store.fetch("SELECT count(*) FROM entity_daily_snapshots") == [(2,)]
    assert store.fetch("SELECT spend, conversions, pulled_on - day FROM entity_daily_latest") == [
        (Decimal("101.5000"), Decimal("5.0000"), 3)
    ]
    assert store.fetch("SELECT provider_account_id, pulled_on FROM pulls ORDER BY pulled_at") == [
        ("123-456-7890", DAY + timedelta(days=1)),
        ("123-456-7890", DAY + timedelta(days=3)),
    ]


def test_split_rows_in_one_pull_are_summed_and_flagged() -> None:
    store = Store()
    _pull(AnalyticsRecorder(store), [_row("c1", DAY, "10", "1"), _row("c1", DAY, "5", None)], 1)

    rows = store.fetch("SELECT spend, conversions, quality_flags FROM entity_daily_snapshots")
    assert rows == [(Decimal("15.0000"), None, ["duplicate_rows"])], "a missing part stays missing"


def test_a_day_matures_only_when_pulled_late_enough_and_complete() -> None:
    store = Store()
    recorder = AnalyticsRecorder(store)
    _pull(recorder, [_row("c1", DAY, "100", "4")], age_days=6)
    assert store.fetch("SELECT count(*) FROM entity_daily_matured") == [(0,)]

    _pull(recorder, [_row("c1", DAY, "100", "9", complete=False)], age_days=8)
    assert store.fetch("SELECT count(*) FROM entity_daily_matured") == [(0,)], "incomplete"

    _pull(recorder, [_row("c1", DAY, "100", "10")], age_days=9)
    _pull(recorder, [_row("c1", DAY, "100", "10")], age_days=10)
    assert store.fetch("SELECT conversions, age_days FROM entity_daily_matured") == [
        (Decimal("10.0000"), 10)
    ]
    panel = store.fetch_dicts("SELECT is_matured, conversions_matured FROM entity_daily_panel")
    assert panel == [{"is_matured": True, "conversions_matured": Decimal("10.0000")}]


def test_the_lag_curve_is_the_share_of_matured_conversions_seen_at_each_age() -> None:
    store = Store()
    recorder = AnalyticsRecorder(store)
    for age, (c1, c2) in {1: ("2", "0"), 3: ("6", "2"), 8: ("8", "4")}.items():
        _pull(recorder, [_row("c1", DAY, "50", c1), _row("c2", DAY, "50", c2)], age_days=age)

    curve = {
        age: float(share)
        for age, share in store.fetch("SELECT age_days, completeness FROM conversion_lag")
    }
    assert curve == {1: 2 / 12, 3: 8 / 12, 8: 1.0}
    rows, _ = query_history(store, "lag")
    assert {r["account_alias"] for r in rows} == {"acme-google"}


def _settings(recorder: AnalyticsRecorder, budget: float, status: str, day: int) -> int:
    record = recorder.record_settings(
        source="sync",
        binding=BINDING,
        tool_name="google_ads__list_campaigns",
        catalog_revision="rev",
        payload={
            "campaigns": [{"id": "c1", "name": "One", "status": status, "daily_budget": budget}]
        },
        observed_at=datetime(2026, 3, day, 11),
    )
    assert record is not None
    return record.external_changes


def test_settings_versions_collapse_and_the_panel_uses_the_budget_in_force() -> None:
    store = Store()
    recorder = AnalyticsRecorder(store)
    _settings(recorder, 100, "ENABLED", 1)
    _settings(recorder, 100, "ENABLED", 2)
    _settings(recorder, 150, "ENABLED", 3)
    for day in (1, 2, 3):
        _pull(recorder, [_row("c1", date(2026, 3, day), "90", "3")], age_days=10)

    versions = store.fetch(
        "SELECT valid_from, valid_to, daily_budget FROM entity_settings_history ORDER BY 1"
    )
    assert versions == [
        (datetime(2026, 3, 1, 11), datetime(2026, 3, 3, 11), Decimal("100.0000")),
        (datetime(2026, 3, 3, 11), None, Decimal("150.0000")),
    ]
    panel = store.fetch("SELECT day, daily_budget, pacing_ratio FROM entity_daily_panel ORDER BY 1")
    assert [(d.day, float(b), round(float(p), 3)) for d, b, p in panel] == [
        (1, 100.0, 0.9),
        (2, 100.0, 0.9),
        (3, 150.0, 0.6),
    ]


def test_changes_made_elsewhere_are_detected_but_verified_agent_changes_are_not() -> None:
    store = Store()
    recorder = AnalyticsRecorder(store)
    assert _settings(recorder, 100, "ENABLED", 1) == 0
    assert _settings(recorder, 120, "PAUSED", 2) == 2

    store.write(
        "INSERT INTO change_events VALUES (uuid(), 'agent', uuid(), 1, 'google_ads', "
        "'123-456-7890', 'acme-google', 'campaign', 'c1', 'google_ads__update_campaign_budget', "
        "'daily_budget', '120', '90', 'verified', [], ?)",
        [datetime(2026, 3, 2, 15)],
    )
    assert _settings(recorder, 90, "PAUSED", 3) == 0

    detected = store.fetch(
        "SELECT field, before_value, after_value FROM change_events "
        "WHERE source = 'external_detected' ORDER BY field"
    )
    assert [(f, json.loads(b), json.loads(a)) for f, b, a in detected] == [
        ("daily_budget", 100.0, 120.0),
        ("status", "ENABLED", "PAUSED"),
    ]


def test_history_queries_filter_bound_and_never_return_provider_ids() -> None:
    store = Store()
    recorder = AnalyticsRecorder(store)
    rows = [_row(f"c{i}", DAY - timedelta(days=d), "10", "1") for i in range(3) for d in range(5)]
    _pull(recorder, rows, age_days=1)

    daily, truncated = query_history(store, "daily", limit=4)
    assert len(daily) == 4 and truncated
    assert daily[0]["day"] == DAY.isoformat() and daily[0]["spend"] == 10.0
    one, _ = query_history(
        store, "daily", entity_ref="c1", start=DAY - timedelta(days=1), end=DAY, limit=50
    )
    assert [r["day"] for r in one] == [DAY.isoformat(), (DAY - timedelta(days=1)).isoformat()]
    assert query_history(store, "daily", account_alias="someone-else")[0] == []
    coverage, _ = query_history(store, "coverage")
    assert coverage[0]["snapshot_rows"] == 15 and coverage[0]["entities"] == 3
    for view in ("coverage", "daily", "settings", "changes", "lag"):
        for row in query_history(store, view)[0]:  # type: ignore[arg-type]
            assert "123-456-7890" not in json.dumps(row)
