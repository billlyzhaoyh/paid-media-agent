"""Figures an answer must quote, computed from the same sample rows the agent reads.

The sample data ends on the fixture anchor (two days before the run, as live platforms report),
shifted from the shipped dates. "Last week" is the most recent complete Monday-to-Sunday week
before today, or the seven days ending on each platform's latest complete day; either reading is
accepted, as the instructions allow both when data ends mid-week.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from paid_media_agent.domain.common import FIXTURE_PLATFORMS
from paid_media_agent.tools.fixtures import load_fixture_dataset, shift_dataset

SHIPPED_ANCHOR = date(2026, 8, 28)
Window = tuple[date, date]
Figure = tuple[str, ...]
"""Acceptable renderings of one number, commas removed (e.g. "3862.42", "3862")."""
Alternative = list[Figure]


def _dataset(platform: str, anchor: date) -> dict[str, object]:
    chosen = next(p for p in FIXTURE_PLATFORMS if p.value == platform)
    return shift_dataset(load_fixture_dataset(chosen), anchor - SHIPPED_ANCHOR)


def data_end(platform: str, anchor: date) -> date:
    return date.fromisoformat(str(_dataset(platform, anchor)["data_complete_through"]))


def window_spend(platform: str, window: Window, anchor: date) -> Decimal:
    rows = _dataset(platform, anchor)["daily"]
    assert isinstance(rows, list)  # noqa: S101 - the shipped fixture shape
    return sum(
        (
            Decimal(str(r["spend"]))
            for r in rows
            if window[0] <= date.fromisoformat(str(r["date"])) <= window[1]
        ),
        Decimal(0),
    )


def calendar_weeks(today: date) -> tuple[Window, Window]:
    last_monday = today - timedelta(days=today.weekday() + 7)
    last = (last_monday, last_monday + timedelta(days=6))
    return last, (last[0] - timedelta(days=7), last[1] - timedelta(days=7))


def trailing_weeks(end: date) -> tuple[Window, Window]:
    last = (end - timedelta(days=6), end)
    return last, (last[0] - timedelta(days=7), last[1] - timedelta(days=7))


def forms(value: Decimal) -> Figure:
    exact = f"{value:.2f}"
    return (exact, str(round(value))) if value >= 100 else (exact,)


def _weekly(platforms: tuple[str, ...], anchor: date, today: date) -> list[Alternative]:
    alternatives: list[Alternative] = []
    for pick in ("calendar", "trailing"):
        figures: Alternative = []
        for platform in platforms:
            weeks = (
                calendar_weeks(today)
                if pick == "calendar"
                else trailing_weeks(data_end(platform, anchor))
            )
            figures += [forms(window_spend(platform, w, anchor)) for w in weeks]
        alternatives.append(figures)
    return alternatives


def expected(name: str, anchor: date, today: date) -> list[Alternative]:
    """Alternatives for a named figure set; an answer passes when one alternative is all present."""
    platforms = tuple(p.value for p in FIXTURE_PLATFORMS)
    if name == "week_spend":
        return _weekly(platforms, anchor, today)
    if name == "google_week_spend":
        return _weekly(("google_ads",), anchor, today)
    if name == "window_28d_spend":
        return [
            [
                forms(
                    window_spend(
                        p, (data_end(p, anchor) - timedelta(days=27), data_end(p, anchor)), anchor
                    )
                )
                for p in platforms
            ]
        ]
    raise ValueError(f"unknown figure set {name}")


def present(answer: str, alternatives: list[Alternative]) -> tuple[bool, list[Figure]]:
    """Whether one alternative is fully in the answer; otherwise the closest one's missing figures."""
    text = answer.replace(",", "")
    best: list[Figure] | None = None
    for alternative in alternatives:
        missing = [f for f in alternative if not any(form in text for form in f)]
        if not missing:
            return True, []
        if best is None or len(missing) < len(best):
            best = missing
    return False, best or []
