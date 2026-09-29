"""Only complete reads reach history: the model's filtered or partial reads never replace a sync."""

from __future__ import annotations

from datetime import date

from paid_media_agent.tools.contracts import GoogleGaqlContract, MetaInsightsContract, ReadContract

GAQL = GoogleGaqlContract()
META = MetaInsightsContract()


def test_the_contracts_own_calls_are_canonical() -> None:
    (performance,) = GAQL.performance_calls(date(2026, 9, 1), date(2026, 9, 7), {})
    assert GAQL.canonical(GAQL.performance_tool, performance.arguments)
    spaced = {"query": "  " + str(performance.arguments["query"]).replace(" ", "   ") + " "}
    assert GAQL.canonical(GAQL.performance_tool, spaced), "whitespace does not matter"
    assert GAQL.canonical(GAQL.settings_tool, GAQL.settings_call().arguments)
    host = ReadContract()
    assert host.canonical(
        "get_campaign_performance", {"start_date": "2026-09-01", "end_date": "2026-09-07"}
    )
    day = {"level": "campaign", "limit": 25, "after": "c1",
           "time_range": '{"since": "2026-09-03", "until": "2026-09-03"}'}  # fmt: skip
    assert META.canonical("get_insights", day), "paging and JSON-in-a-string still match"


def test_filtered_or_partial_reads_are_not() -> None:
    mobile = (
        "SELECT campaign.id, segments.date, metrics.cost_micros FROM campaign "
        "WHERE segments.date BETWEEN '2026-09-01' AND '2026-09-07' AND segments.device = 'MOBILE'"
    )
    assert not GAQL.canonical(GAQL.performance_tool, {"query": mobile})
    listing = {"query": "SELECT campaign.id, campaign.name, campaign.status FROM campaign"}
    assert not GAQL.canonical(GAQL.settings_tool, listing)
    ranged = {"level": "campaign", "time_range": {"since": "2026-09-01", "until": "2026-09-07"}}
    assert not META.canonical("get_insights", ranged), "a multi-day row is not daily history"
    assert not META.canonical(
        "get_insights",
        {"level": "ad", "time_range": {"since": "2026-09-03", "until": "2026-09-03"}},
    )
