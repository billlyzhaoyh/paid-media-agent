"""Named windows are resolved in code, across week, month, and year ends."""

from __future__ import annotations

from datetime import date

import pytest

from paid_media_agent.domain.windows import days_ago, resolve_preset


@pytest.mark.parametrize(
    ("preset", "today", "through", "days", "current", "previous"),
    [
        # Wednesday 2026-09-30: last week is Mon 21 - Sun 27.
        ("last_week", date(2026, 9, 30), date(2026, 9, 28), 7,
         (date(2026, 9, 21), date(2026, 9, 27)), (date(2026, 9, 14), date(2026, 9, 20))),
        # On a Monday, last week ended yesterday.
        ("last_week", date(2026, 9, 28), date(2026, 9, 27), 7,
         (date(2026, 9, 21), date(2026, 9, 27)), (date(2026, 9, 14), date(2026, 9, 20))),
        # The last N days of data end where the data does, not yesterday.
        ("last_n_days_of_data", date(2026, 9, 30), date(2026, 9, 27), 7,
         (date(2026, 9, 21), date(2026, 9, 27)), (date(2026, 9, 14), date(2026, 9, 20))),
        ("last_n_days_of_data", date(2026, 1, 2), date(2025, 12, 31), 28,
         (date(2025, 12, 4), date(2025, 12, 31)), (date(2025, 11, 6), date(2025, 12, 3))),
        ("month_to_date", date(2026, 9, 30), date(2026, 9, 28), 7,
         (date(2026, 9, 1), date(2026, 9, 28)), (date(2026, 8, 4), date(2026, 8, 31))),
        ("last_month", date(2026, 3, 10), date(2026, 3, 8), 7,
         (date(2026, 2, 1), date(2026, 2, 28)), (date(2026, 1, 4), date(2026, 1, 31))),
        ("last_month", date(2026, 1, 5), date(2026, 1, 3), 7,
         (date(2025, 12, 1), date(2025, 12, 31)), (date(2025, 10, 31), date(2025, 11, 30))),
    ],
)  # fmt: skip
def test_presets_resolve_to_equal_windows(
    preset: str, today: date, through: date, days: int, current: tuple, previous: tuple
) -> None:
    got = resolve_preset(preset, today=today, data_through=through, days=days)  # type: ignore[arg-type]
    assert got == (current, previous)
    assert (got[0][1] - got[0][0]) == (got[1][1] - got[1][0]), "the same number of days"


def test_month_to_date_without_data_this_month_says_so() -> None:
    with pytest.raises(ValueError, match="no data this month"):
        resolve_preset("month_to_date", today=date(2026, 10, 1), data_through=date(2026, 9, 29))
    assert days_ago(date(2026, 9, 18), date(2026, 9, 30)) == 12


def test_platforms_are_compared_both_ways_with_verdicts() -> None:
    from paid_media_agent.tools.summary import platform_comparisons

    google = {
        "platform": "google_ads",
        "account": "demo-google",
        "currency": "USD",
        "covered_window": "2026-09-01..2026-09-28",
        "cpa": "26.38",
        "roas": "6.17",
    }
    meta = {
        **google,
        "platform": "meta_ads",
        "account": "demo-meta",
        "cpa": "49.09",
        "roas": "2.68",
    }
    lines = platform_comparisons([google, meta])
    cpa = next(line for line in lines if line.startswith("CPA"))
    assert "google_ads (demo-google) 26.38 USD is 46% lower (better)" in cpa
    assert "meta_ads (demo-meta)'s is 86% higher (worse)" in cpa, "the gap from both bases"
    assert "a gap of 22.71 USD" in cpa, "and in currency, so no subtraction is left to do"
    roas = next(line for line in lines if line.startswith("ROAS"))
    assert "is 130% higher (better)" in roas
    gbp = {**meta, "currency": "GBP"}
    assert platform_comparisons([google, gbp]) == [
        "google_ads (demo-google) and meta_ads (demo-meta) report in different currencies "
        "(USD, GBP): not compared"
    ]
