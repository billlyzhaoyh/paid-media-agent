"""Named windows resolved in code: models miscount dates, so tools accept a preset instead.

A comparison's previous window is always the same number of days immediately before the current
one, because totals over different day counts are not comparable. Every tool that resolves a
preset returns the dates it chose, so an answer states them rather than working them out.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

WindowPreset = Literal["last_week", "last_n_days_of_data", "month_to_date", "last_month"]
PRESET_HELP = (
    "Instead of dates: last_week (the last complete Monday-Sunday week), last_n_days_of_data "
    "(the newest `days` days every read covers, never today), month_to_date (this month through "
    "the newest day of data), or last_month (the previous calendar month). The previous window "
    "is the same number of days immediately before."
)
Span = tuple[date, date]


def resolve_preset(
    preset: WindowPreset, *, today: date, data_through: date, days: int = 7
) -> tuple[Span, Span]:
    """(current, previous) for `preset`. `data_through` is the newest day the data covers."""
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
            raise ValueError(
                f"no data this month yet: data runs through {data_through.isoformat()}"
            )
        current = (start, end)
    elif preset == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        current = (end.replace(day=1), end)
    else:  # pragma: no cover - Literal guards it
        raise ValueError(f"unknown window {preset}")
    length = (current[1] - current[0]).days + 1
    previous = (current[0] - timedelta(days=length), current[0] - timedelta(days=1))
    return current, previous


def span_text(span: Span) -> str:
    return f"{span[0].isoformat()}..{span[1].isoformat()}"


def days_ago(day: date, today: date) -> int:
    """Whole days from `day` to `today` in the same calendar: yesterday is 1."""
    return (today - day).days
