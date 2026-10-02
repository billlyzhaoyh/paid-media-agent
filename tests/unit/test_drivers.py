"""Why a KPI changed: the LMDI split is exact, names the right cause, and handles edge cases."""

from __future__ import annotations

import math
import uuid
from datetime import date, datetime, timedelta

import pytest

from paid_media_agent.analytics import drivers
from paid_media_agent.analytics.drivers import Window, explain, log_mean
from paid_media_agent.store import Store

PREVIOUS = Window(date(2026, 9, 1), date(2026, 9, 7))
CURRENT = Window(date(2026, 9, 8), date(2026, 9, 14))
PULLED = datetime(2026, 9, 30, 6)


def _days(window: Window) -> list[date]:
    return [window.start + timedelta(days=i) for i in range(window.days)]


def _rows(
    store: Store,
    ref: str,
    window: Window,
    *,
    spend: float,
    impressions: float | None,
    clicks: float | None,
    conversions: float | None,
    value: float | None = None,
    alias: str = "acme",
    pulled: datetime = PULLED,
) -> None:
    """One campaign's daily rows over a window, with the same numbers each day."""
    pull = uuid.uuid4()
    for day in _days(window):
        store.write(
            "INSERT INTO entity_daily_snapshots VALUES "
            "('google_ads', ?, 'campaign', ?, ?, ?, ?, ?, ?, ?, 'USD', ?, ?, ?, ?, ?, true, [])",
            [
                f"acct-{alias}", ref, day, pull, pulled, pulled.date(), alias, f"Campaign {ref}",
                spend, impressions, clicks, conversions, value,
            ],
        )  # fmt: skip


def _report(store: Store, metric: drivers.Metric = "cpa", aliases: tuple[str, ...] = ("acme",)):
    return explain(
        store, aliases, metric=metric, current=CURRENT, previous=PREVIOUS, currency="USD"
    )


def _effects(report) -> dict[str, float]:
    return {k: v for k, v in report.decomposition.effects().items() if abs(v) > 1e-9}


def test_log_mean() -> None:
    assert log_mean(2.0, 2.0) == 2.0
    assert log_mean(4.0, 1.0) == pytest.approx(3 / math.log(4))
    assert log_mean(3.0, 0.0) == 0.0


def test_a_conversion_rate_drop_on_one_campaign_is_all_that_campaign_s_conversion_rate() -> None:
    store = Store()
    # A: CPM 10, CTR 5%, CVR 10% -> CPA 20. B: CPA 25 both weeks.
    _rows(store, "a", PREVIOUS, spend=100, impressions=10_000, clicks=500, conversions=50)
    _rows(store, "a", CURRENT, spend=100, impressions=10_000, clicks=500, conversions=25)
    _rows(store, "b", PREVIOUS, spend=100, impressions=5_000, clicks=200, conversions=40)
    _rows(store, "b", CURRENT, spend=100, impressions=5_000, clicks=200, conversions=40)
    report = _report(store)
    d = report.decomposition
    # Hand-computed: CPA 1400/630 = 2.2222 -> 1400/455 = 3.0769, +38.46%.
    assert d.before == pytest.approx(1400 / 630) and d.after == pytest.approx(1400 / 455)
    assert d.change == pytest.approx(630 / 455 - 1)
    effects = _effects(report)
    assert effects.keys() == {"cvr"}
    assert effects["cvr"] == pytest.approx(d.change * 100, abs=1e-9)
    top = report.drivers()[0]
    assert (top.entity_ref, top.factor, top.within_noise) == ("a", "cvr", False)
    assert (top.before, top.after) == (pytest.approx(0.10), pytest.approx(0.05))
    assert report.significant is True
    assert "CPA rose 38.5% (worse)" in report.reading and "conversion rate" in report.reading


def test_moving_spend_between_campaigns_with_fixed_rates_is_all_mix() -> None:
    store = Store()
    # Rates identical in both weeks; spend moves from the cheap campaign to the dear one.
    _rows(store, "cheap", PREVIOUS, spend=300, impressions=30_000, clicks=900, conversions=30)
    _rows(store, "cheap", CURRENT, spend=100, impressions=10_000, clicks=300, conversions=10)
    _rows(store, "dear", PREVIOUS, spend=100, impressions=5_000, clicks=100, conversions=2)
    _rows(store, "dear", CURRENT, spend=300, impressions=15_000, clicks=300, conversions=6)
    report = _report(store)
    effects = _effects(report)
    assert effects.keys() == {"spend_mix"}
    assert effects["spend_mix"] == pytest.approx(report.decomposition.change * 100)
    assert report.decomposition.change > 0
    # Each campaign's part reads the way a marketer reads it: share lost by the cheap campaign
    # and share gained by the dear one both raise CPA.
    mix = {
        c.entity_ref: c.points
        for c in report.decomposition.contributions
        if c.factor == "spend_mix"
    }
    assert mix["cheap"] > 0 and mix["dear"] > 0


def test_effects_add_up_to_the_headline_exactly_for_every_metric() -> None:
    store = Store()
    _rows(store, "a", PREVIOUS, spend=120, impressions=9_000, clicks=410, conversions=21, value=900)
    _rows(store, "a", CURRENT, spend=150, impressions=10_500, clicks=380, conversions=17, value=820)
    _rows(store, "b", PREVIOUS, spend=80, impressions=6_100, clicks=150, conversions=6, value=400)
    _rows(store, "b", CURRENT, spend=60, impressions=3_900, clicks=160, conversions=9, value=610)
    _rows(store, "c", CURRENT, spend=40, impressions=2_000, clicks=60, conversions=2, value=90)
    for metric in ("cpa", "conversions", "roas"):
        report = _report(store, metric)
        d = report.decomposition
        total = sum(d.effects().values())
        assert total == pytest.approx(d.change * 100, abs=1e-9), metric
    roas = _effects(_report(store, "roas"))
    assert "aov" in roas and "new_or_paused" in roas
    assert "total_spend" in _effects(_report(store, "conversions"))
    assert "total_spend" not in _effects(_report(store, "cpa"))


def test_new_paused_and_zero_conversion_campaigns() -> None:
    store = Store()
    _rows(store, "stays", PREVIOUS, spend=100, impressions=10_000, clicks=300, conversions=10)
    _rows(store, "stays", CURRENT, spend=100, impressions=10_000, clicks=300, conversions=10)
    _rows(store, "stopped", PREVIOUS, spend=100, impressions=4_000, clicks=100, conversions=2)
    _rows(store, "broke", PREVIOUS, spend=50, impressions=5_000, clicks=100, conversions=5)
    _rows(store, "broke", CURRENT, spend=50, impressions=5_000, clicks=100, conversions=0)
    report = _report(store)
    by = {(c.entity_ref, c.factor): c for c in report.decomposition.contributions}
    assert by[("stopped", "new_or_paused")].points < 0, "stopping a dear campaign lowers CPA"
    assert by[("broke", "cvr")].points > 0, "conversions to zero is a conversion-rate effect"
    assert sum(report.decomposition.effects().values()) == pytest.approx(
        report.decomposition.change * 100
    )
    assert "stopped" in drivers.driver_reading(by[("stopped", "new_or_paused")], {}, "USD")


def test_without_clicks_the_rate_is_cost_per_conversion() -> None:
    store = Store()
    _rows(store, "a", PREVIOUS, spend=100, impressions=None, clicks=None, conversions=10)
    _rows(store, "a", CURRENT, spend=100, impressions=None, clicks=None, conversions=8)
    _rows(store, "b", PREVIOUS, spend=100, impressions=None, clicks=None, conversions=5)
    _rows(store, "b", CURRENT, spend=100, impressions=None, clicks=None, conversions=5)
    effects = _effects(_report(store))
    assert effects.keys() == {"cost_per_conversion"}


def test_no_conversions_cannot_be_split() -> None:
    store = Store()
    _rows(store, "a", PREVIOUS, spend=100, impressions=100, clicks=10, conversions=0)
    _rows(store, "a", CURRENT, spend=100, impressions=100, clicks=10, conversions=3)
    report = _report(store)
    assert report.decomposition.reason and "cannot be compared" in report.reading


def test_small_changes_on_few_conversions_are_called_noise() -> None:
    store = Store()
    _rows(store, "a", PREVIOUS, spend=100, impressions=10_000, clicks=100, conversions=2)
    _rows(store, "a", CURRENT, spend=100, impressions=10_000, clicks=100, conversions=1.8)
    report = _report(store)
    assert report.significant is False and "within normal noise" in report.reading
    assert report.drivers()[0].within_noise is True


def test_young_days_are_lag_corrected(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store()
    _rows(store, "a", PREVIOUS, spend=100, impressions=10_000, clicks=500, conversions=10)
    young = datetime(2026, 9, 14, 6)
    _rows(store, "a", CURRENT, spend=100, impressions=10_000, clicks=500, conversions=5,
          pulled=young)  # fmt: skip
    unknown = _report(store)
    assert any("no measured lag" in n for n in unknown.notes)
    # A lag curve saying half the conversions are in by every young age doubles them back.
    monkeypatch.setattr(
        drivers, "lag_curves", lambda _store: {"acct-acme": dict.fromkeys(range(7), 0.5)}
    )
    corrected = _report(store)
    assert corrected.decomposition.change == pytest.approx(0.0, abs=1e-9)
    assert any("expected late conversions" in n for n in corrected.notes)


def test_accounts_combine_and_settings_changes_are_listed() -> None:
    store = Store()
    _rows(store, "a", PREVIOUS, spend=100, impressions=10_000, clicks=500, conversions=10)
    _rows(store, "a", CURRENT, spend=150, impressions=15_000, clicks=750, conversions=12)
    _rows(store, "m", PREVIOUS, spend=100, impressions=20_000, clicks=300, conversions=8,
          alias="meta")  # fmt: skip
    _rows(store, "m", CURRENT, spend=100, impressions=20_000, clicks=300, conversions=8,
          alias="meta")  # fmt: skip
    for when, budget in ((datetime(2026, 8, 20), 100.0), (datetime(2026, 9, 8, 9), 150.0)):
        store.write(
            "INSERT INTO entity_settings_snapshots VALUES ('google_ads', 'acct-acme', 'campaign', "
            "'a', ?, ?, 'acme', 'Campaign a', 'ENABLED', ?, 'daily', 'MAXIMIZE_CONVERSIONS', "
            "NULL, NULL, 'USD', '{}')",
            [when, uuid.uuid4(), budget],
        )
    report = _report(store, aliases=("acme", "meta"))
    assert {a["account_alias"] for a in report.by_account} == {"acme", "meta"}
    assert sum(a["points"] for a in report.by_account) == pytest.approx(
        report.decomposition.change * 100, abs=0.02
    )
    assert report.known_changes == [
        {"account_alias": "acme", "campaign": "a", "day": "2026-09-08", "field": "daily_budget",
         "before": 100.0, "after": 150.0}
    ]  # fmt: skip
    assert "Settings changed" in report.reading
    dated = explain(
        store, ("acme", "meta"), metric="cpa", current=CURRENT, previous=PREVIOUS,
        currency="USD", today=date(2026, 9, 30),
    )  # fmt: skip
    assert dated.known_changes[0]["days_ago"] == 22 and "(22 days ago)" in dated.reading
    body = report.as_json()
    assert body["accounts"] == ["acme", "meta"] and body["effects"] and body["drivers"]
    assert "acct-" not in str(body), "provider ids stay in the host"
