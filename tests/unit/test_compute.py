from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from paid_media_agent.domain.common import DataQualityFlag, EntityType, Platform
from paid_media_agent.domain.metrics import MetricWindow, PerformanceRow
from paid_media_agent.tools.compute import (
    aggregate,
    compare_periods,
    compare_platform,
    deltas,
    summarize,
)
from paid_media_agent.tools.fixtures import load_fixture_dataset
from paid_media_agent.tools.normalize import normalize_rows

CURRENT = MetricWindow(
    start=date(2026, 8, 15), end=date(2026, 8, 28), timezone="America/New_York", is_complete=True
)
PREVIOUS = MetricWindow(
    start=date(2026, 8, 1), end=date(2026, 8, 14), timezone="America/New_York", is_complete=True
)


def _row(
    day: date,
    spend: str,
    clicks: int | None,
    impressions: int | None,
    conv: str | None,
    value: str | None,
    ref: str = "c1",
) -> PerformanceRow:
    return PerformanceRow(
        platform=Platform.GOOGLE_ADS,
        account_ref="a",
        entity_type=EntityType.CAMPAIGN,
        entity_ref=ref,
        entity_name=ref,
        window=MetricWindow(start=day, end=day, timezone="UTC", is_complete=True),
        currency="USD",
        spend=Decimal(spend),
        clicks=clicks,
        impressions=impressions,
        conversions=Decimal(conv) if conv is not None else None,
        conversion_value=Decimal(value) if value is not None else None,
    )


def test_missing_metric_stays_missing_and_zero_denominators_are_unavailable() -> None:
    rows = [
        _row(date(2026, 8, 1), "10.00", 0, 100, None, None),
        _row(date(2026, 8, 2), "5.50", 0, 50, None, None),
    ]
    agg = aggregate(rows)
    assert agg.spend == Decimal("15.500000")
    assert agg.conversions is None and agg.conversion_value is None
    assert (
        agg.cpa is None and agg.roas is None and agg.cpc is None
    )  # zero clicks -> unavailable, not infinity
    assert agg.ctr == Decimal("0")
    assert agg.cpm == Decimal("103.333333")


def test_deltas_handle_missing_and_zero_previous() -> None:
    cur = aggregate([_row(date(2026, 8, 2), "10", 10, 100, "2", "40")])
    prev = aggregate([_row(date(2026, 8, 1), "0", 0, 100, None, None)])
    by = {d.metric: d for d in deltas(cur, prev)}
    assert by["spend"].absolute == Decimal("10") and by["spend"].relative is None  # previous zero
    assert (
        by["conversions"].absolute is None and by["conversions"].relative is None
    )  # previous missing


def test_platform_comparison_reconciles_to_fixture_oracle(project_root: Path) -> None:
    data = load_fixture_dataset(Platform.GOOGLE_ADS)
    native = [
        {
            "date": r["date"],
            "campaign_id": r["campaign_id"],
            "cost_micros": int(Decimal(str(r["spend"])) * 1_000_000),
            "impressions": r["impressions"],
            "clicks": r["clicks"],
            "conversions": r["conversions"],
            "conversions_value": r["value"],
        }
        for r in data["daily"]
    ]
    rows, missing = normalize_rows(
        platform=Platform.GOOGLE_ADS,
        account_ref="demo-google",
        currency="USD",
        timezone="America/New_York",
        rows=native,
        entity_type=EntityType.CAMPAIGN,
        entity_names={c["id"]: c["name"] for c in data["campaigns"]},
        data_complete_through=date.fromisoformat(data["data_complete_through"]),
    )
    assert missing == ()
    result = compare_platform(
        platform=Platform.GOOGLE_ADS,
        account_ref="demo-google",
        rows=rows,
        current_window=CURRENT,
        previous_window=PREVIOUS,
        entity_type=EntityType.CAMPAIGN,
        source_artifacts=["art_x"],
        provider_totals={
            "spend": str(sum(Decimal(str(r["spend"])) for r in data["daily"])),
            "row_count": len(data["daily"]),
        },
    )
    # Independent oracle straight from the JSON fixture.
    raw_current = [r for r in data["daily"] if "2026-08-15" <= r["date"] <= "2026-08-28"]
    oracle_spend = sum(Decimal(str(r["spend"])) for r in raw_current)
    oracle_conv = sum(Decimal(str(r["conversions"])) for r in raw_current)
    assert result.current.spend == oracle_spend.quantize(Decimal("0.000001"))
    assert result.current.conversions == oracle_conv.quantize(Decimal("0.000001"))
    assert result.current.cpa == (oracle_spend / oracle_conv).quantize(Decimal("0.000001"))
    assert all(check.passed for check in result.reconciliation), result.reconciliation
    g103 = next(e for e in result.entities if e.entity_ref == "g-103")
    assert DataQualityFlag.INCOMPLETE_WINDOW in g103.quality_flags
    # g-103 has rows on 5 of 7 days (platforms send no row for a day it did not serve): that is
    # flagged on g-103, while the platform's window is complete because every day is covered.
    assert result.current_window.is_complete and result.previous_window.is_complete
    assert sum(e.current.spend for e in result.entities) == result.current.spend


def test_provider_totals_reconcile_against_the_whole_read_not_just_the_windows() -> None:
    wide = [_row(d, "100.00", 1, 10, "1", "2") for d in CURRENT.days() + PREVIOUS.days()]
    extra = wide[0].model_copy(update={"window": wide[0].window.model_copy(
        update={"start": PREVIOUS.start - timedelta(days=1), "end": PREVIOUS.start - timedelta(days=1)}
    )})  # fmt: skip
    rows = [*wide, extra]
    result = compare_platform(
        platform=Platform.GOOGLE_ADS, account_ref="acme", rows=rows, current_window=CURRENT,
        previous_window=PREVIOUS, entity_type=EntityType.CAMPAIGN, source_artifacts=["a"],
        provider_totals={"spend": str(100 * len(rows)), "row_count": len(rows)},
    )  # fmt: skip
    assert all(check.passed for check in result.reconciliation), result.reconciliation


def test_cross_platform_total_requires_compatible_complete_sources() -> None:
    complete_rows = [_row(d, "1.00", 1, 10, "1", "2") for d in CURRENT.days() + PREVIOUS.days()]
    complete = compare_platform(
        platform=Platform.GOOGLE_ADS,
        account_ref="a",
        rows=complete_rows,
        current_window=CURRENT,
        previous_window=PREVIOUS,
        entity_type=EntityType.CAMPAIGN,
        source_artifacts=[],
    )
    total = compare_periods(
        platforms=[complete, complete], requested_current=CURRENT, requested_previous=PREVIOUS
    )
    assert total.cross_platform_total is not None and total.cross_platform_total.spend == Decimal(
        "28.000000"
    )
    partial_rows = [_row(d, "1.00", 1, 10, None, None) for d in CURRENT.days() + PREVIOUS.days()]
    partial = compare_platform(
        platform=Platform.META_ADS,
        account_ref="b",
        rows=partial_rows,
        current_window=CURRENT,
        previous_window=PREVIOUS,
        entity_type=EntityType.CAMPAIGN,
        source_artifacts=[],
        missing_fields=["conversions"],
    )
    suppressed = compare_periods(
        platforms=[complete, partial], requested_current=CURRENT, requested_previous=PREVIOUS
    )
    assert suppressed.cross_platform_total is None and "missing" in (
        suppressed.total_suppressed_reason or ""
    )
    unavailable = compare_periods(
        platforms=[complete],
        requested_current=CURRENT,
        requested_previous=PREVIOUS,
        unavailable_sources=["reddit_ads"],
    )
    assert unavailable.cross_platform_total is None
    summary = summarize(unavailable, "art_summary")
    assert summary.cross_platform_total is None and summary.unavailable_sources == ("reddit_ads",)
    assert json.loads(summary.model_dump_json())["platforms"][0]["spend_current"] == "14.00 USD"


def test_normalization_preserves_zero_clicks() -> None:
    rows, _ = normalize_rows(
        platform=Platform.GOOGLE_ADS,
        account_ref="example",
        currency="USD",
        timezone="UTC",
        rows=[
            {
                "date": "2026-08-01",
                "campaign_id": "c1",
                "spend": "10",
                "impressions": 100,
                "clicks": 0,
                "link_clicks": 5,
            }
        ],
        entity_type=EntityType.CAMPAIGN,
        data_complete_through=None,
    )
    assert rows[0].clicks == 0
    assert aggregate(rows).ctr == Decimal(0)


def test_every_change_says_whether_it_is_better_or_worse() -> None:
    from paid_media_agent.domain.analysis import change_text, verdict

    assert verdict("cpa", Decimal("0.08")) == "worse" and verdict("cpa", -0.08) == "better"
    assert verdict("roas", Decimal("0.0984")) == "better", "ROAS 2.44 -> 2.68 improved"
    assert verdict("roas", -0.1) == "worse" and verdict("conversions", 0.2) == "better"
    assert verdict("spend", 0.3) is None, "more spend is neither better nor worse on its own"
    assert verdict("cpa", 0.001) == "flat" and verdict("cpa", None) is None
    assert change_text("cpa", Decimal("0.082")) == "+8.2% (worse)"
    assert change_text("spend", Decimal("-0.03")) == "-3.0%"


def test_comparisons_carry_verdicts_and_rank_campaigns_with_enough_conversions() -> None:
    def days(start: date, spend: str, conv: str, value: str, ref: str) -> list[PerformanceRow]:
        return [
            _row(start + timedelta(days=i), spend, 100, 1000, conv, value, ref) for i in range(7)
        ]

    cur, prev = date(2026, 8, 15), date(2026, 8, 1)
    rows = [
        # c1: spend up, CPA up (worse), ROAS down (worse); c2: cheap and improving;
        # c3: too few conversions to rank.
        *days(prev, "100", "5", "400", "c1"), *days(cur, "120", "4", "360", "c1"),
        *days(prev, "50", "5", "300", "c2"), *days(cur, "50", "6", "360", "c2"),
        *days(prev, "10", "0.1", "5", "c3"), *days(cur, "10", "0.1", "5", "c3"),
    ]  # fmt: skip
    window = lambda start: MetricWindow(  # noqa: E731
        start=start, end=start + timedelta(days=6), timezone="UTC", is_complete=True
    )
    platform = compare_platform(
        platform=Platform.GOOGLE_ADS, account_ref="a", rows=rows, current_window=window(cur),
        previous_window=window(prev), entity_type=EntityType.CAMPAIGN, source_artifacts=["art_1"],
    )  # fmt: skip
    summary = summarize(
        compare_periods(
            platforms=[platform], requested_current=window(cur), requested_previous=window(prev)
        ),
        "art_1",
    )
    (headline,) = summary.platforms
    assert headline.cpa_change.endswith("(worse)") and headline.roas_change.endswith("(worse)")
    by_ref = {line.split("[")[1].split("]")[0]: line for line in headline.attention}
    assert "(worse)" in by_ref["c1"] and "ROAS" in by_ref["c1"]
    assert by_ref["c2"].count("(better)") == 2, "CPA fell and ROAS rose: both better"
    rankings = "\n".join(headline.rankings)
    assert "lowest (best) CPA of 2 with at least 5 conversions: c2 [c2]" in rankings
    assert "highest (worst) CPA of 2 with at least 5 conversions: c1 [c1]" in rankings
    assert "largest CPA rise (worse): c1 [c1]" in rankings
    assert "spend up while ROAS fell (worse): c1 [c1]" in rankings
    assert "c3" not in rankings, "0.7 conversions are too few to rank"
