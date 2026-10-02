"""Named windows resolved in code: models miscount dates, so tools accept a preset instead.

A comparison's previous window always has the same number of days as the current one, because
totals over different day counts are not comparable: the days immediately before, or for month to
date the same calendar days of last month. Every tool that resolves a
preset returns the dates it chose, so an answer states them rather than working them out.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal, NamedTuple

WindowPreset = Literal["last_week", "last_n_days_of_data", "month_to_date", "last_month"]
PRESET_HELP = (
    "Instead of dates: last_week (the last complete Monday-Sunday week), last_n_days_of_data "
    "(the newest `days` days every read covers, never today), month_to_date (this month through "
    "the newest day of data, against the same days of last month; the latest month with data "
    "when this one has none yet), or last_month (the previous calendar month). Otherwise the "
    "previous window is the same number of days immediately before."
)
Span = tuple[date, date]


class Resolved(NamedTuple):
    current: Span
    previous: Span
    note: str = ""
    """What the answer must say about how the current window was chosen."""
    previous_note: str = ""
    """And about the previous window, when it is not the obvious one."""

    @property
    def notes(self) -> str:
        return "; ".join(n for n in (self.note, self.previous_note) if n)


def _same_days_last_month(current: Span) -> tuple[Span, str]:
    """The prior month's same calendar days, when it has them all; else the days just before."""
    start, end = current
    prior_end = start - timedelta(days=1)
    prior_start = prior_end.replace(day=1)
    if end.day <= prior_end.day:
        return (prior_start, prior_start.replace(day=end.day)), ""
    length = (end - start).days + 1
    fallback = (start - timedelta(days=length), start - timedelta(days=1))
    return fallback, (
        f"{prior_start.strftime('%B')} has only {prior_end.day} days, so the previous window is "
        f"the {length} days just before instead"
    )


def resolve_preset(
    preset: WindowPreset, *, today: date, data_through: date, days: int = 7
) -> Resolved:
    """(current, previous, note) for `preset`. `data_through` is the newest day the data covers."""
    note = ""
    if preset == "last_week":
        monday = today - timedelta(days=today.weekday() + 7)
        current = (monday, monday + timedelta(days=6))
    elif preset == "last_n_days_of_data":
        if days < 1:
            raise ValueError("days must be at least 1")
        current = (data_through - timedelta(days=days - 1), data_through)
    elif preset == "month_to_date":
        start = today.replace(day=1)
        end = min(data_through, today - timedelta(days=1))
        if end < start:
            # No complete day of this month yet: the latest month with data, to date, said so.
            start = data_through.replace(day=1)
            end = data_through
            note = (
                f"{today.strftime('%B')} has no complete day of data yet (data runs through "
                f"{data_through.isoformat()}), so this is {start.strftime('%B')} to date"
            )
        current = (start, end)
        previous, fallback = _same_days_last_month(current)
        return Resolved(current, previous, note, fallback)
    elif preset == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        current = (end.replace(day=1), end)
    else:  # pragma: no cover - Literal guards it
        raise ValueError(f"unknown window {preset}")
    length = (current[1] - current[0]).days + 1
    previous = (current[0] - timedelta(days=length), current[0] - timedelta(days=1))
    return Resolved(current, previous, note)


def span_text(span: Span) -> str:
    return f"{span[0].isoformat()}..{span[1].isoformat()}"


def days_ago(day: date, today: date) -> int:
    """Whole days from `day` to `today` in the same calendar: yesterday is 1."""
    return (today - day).days
