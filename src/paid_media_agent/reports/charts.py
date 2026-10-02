"""Chart geometry for the report's day-by-day and spend-response charts.

The template draws; this module only places. Every mark, tick, and label position is computed
here on one scale per axis, in the chart's own viewBox units, so the SVG has no arithmetic and the
same numbers can be tested. Axis ticks are round values the data actually spans.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from paid_media_agent.domain.reports import (
    BandSeries,
    BudgetCurve,
    BudgetRow,
    BudgetTrial,
    MethodScore,
    RangeExample,
)

WIDTH, HEIGHT = 560.0, 230.0
WIDE_WIDTH, WIDE_HEIGHT = 900.0, 250.0
LEFT, RIGHT, TOP, BOTTOM = 52.0, 14.0, 12.0, 30.0


@dataclass(frozen=True)
class Tick:
    at: float
    """Position on the axis, in viewBox units."""
    label: str


@dataclass(frozen=True)
class Mark:
    x: float
    y: float
    title: str
    """What a reader sees on hover, and what a screen reader says."""


@dataclass(frozen=True)
class Frame:
    """The plot rectangle and its axes."""

    width: float = WIDTH
    height: float = HEIGHT
    left: float = LEFT
    right: float = WIDTH - RIGHT
    top: float = TOP
    bottom: float = HEIGHT - BOTTOM
    x_ticks: tuple[Tick, ...] = ()
    y_ticks: tuple[Tick, ...] = ()


@dataclass(frozen=True)
class Callout:
    """A short label beside a mark, placed to stay inside the frame."""

    x: float
    y: float
    anchor: str
    text: str


@dataclass(frozen=True)
class BandChart:
    frame: Frame
    area: str
    """Path of the expected range, closed."""
    expected: str
    observed: str
    points: tuple[Mark, ...]
    flags: tuple[Mark, ...]
    planted: tuple[Mark, ...]
    summary: str
    callouts: tuple[Callout, ...] = ()


@dataclass(frozen=True)
class TrialLine:
    key: str
    label: str
    points: str
    end: Mark
    value: str
    """The run's conversions a day, as its end label states it."""


@dataclass(frozen=True)
class TrialChart:
    frame: Frame
    lines: tuple[TrialLine, ...]
    labels: tuple[Callout, ...]
    summary: str


@dataclass(frozen=True)
class RangeBar:
    """One judged day on a single scale: the range the model returned and the value observed."""

    width: float
    height: float
    lo: float
    expected: float
    hi: float
    observed: float
    """Horizontal positions."""
    band_top: float
    band_height: float
    lo_label: str
    expected_label: str
    hi_label: str
    observed_label: str
    observed_anchor: str
    outside: bool
    summary: str


@dataclass(frozen=True)
class AlertBar:
    """One method's alerts on a shared scale: real problems first, then false alarms."""

    label: str
    caught: int
    false_alarms: int
    caught_width: float
    false_width: float
    """Widths in percent of the longest bar."""


@dataclass(frozen=True)
class BudgetBar:
    row: BudgetRow
    start: float
    now: float
    recommended: float | None
    """Positions in percent of the largest budget shown."""
    change: str
    """The move from start to now, as '+18%' or 'no change'."""


@dataclass(frozen=True)
class CurveChart:
    frame: Frame
    history: tuple[Mark, ...]
    pseudo: tuple[Mark, ...]
    fitted: str
    alternative: str
    truth: str
    current: Mark | None = None
    recommended: Mark | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


def nice_ticks(low: float, high: float, count: int = 4) -> list[float]:
    """Round values (1, 2, 2.5, or 5 times a power of ten) from `low` to `high`, both inside."""
    if not math.isfinite(low) or not math.isfinite(high):
        return [0.0]
    if high <= low:
        return [low]
    raw = (high - low) / max(count, 1)
    power = 10 ** math.floor(math.log10(raw))
    step = next(m * power for m in (1, 2, 2.5, 5, 10) if m * power >= raw - 1e-12)
    first = math.ceil(low / step - 1e-9) * step
    ticks = []
    value = first
    while value <= high + step * 1e-9:
        ticks.append(round(value, 10))
        value += step
    return ticks or [low]


def _label(value: float) -> str:
    if abs(value) >= 100 or float(value).is_integer():
        return f"{value:,.0f}"
    return f"{value:,.1f}" if abs(value) >= 1 else f"{value:.2f}"


def _extent(values: Sequence[float], *, floor_zero: bool, pad: float = 0.08) -> tuple[float, float]:
    """The scale's ends: the data's range with a little air, never below zero for an amount."""
    finite = [v for v in values if math.isfinite(v)]
    if not finite:
        return 0.0, 1.0
    low, high = min(finite), max(finite)
    span = high - low or abs(high) or 1.0
    low, high = low - span * pad, high + span * pad
    return (max(low, 0.0) if floor_zero else low), high


class _Scale:
    def __init__(self, low: float, high: float, start: float, end: float) -> None:
        self.low, self.high, self.start, self.end = low, high, start, end

    def __call__(self, value: float) -> float:
        span = self.high - self.low
        share = 0.5 if span == 0 else (value - self.low) / span
        return round(self.start + share * (self.end - self.start), 2)


def _line(points: Sequence[tuple[float, float]]) -> str:
    return " ".join(f"{x},{y}" for x, y in points)


def _money(value: float, unit: str) -> str:
    return f"{value:,.2f} {unit}".strip() if unit != "conversions" else f"{value:,.1f}"


def _day(value: date) -> str:
    return f"{value:%b} {value.day}"


def band_chart(series: BandSeries) -> BandChart:
    """Observed values against the expected range, one point per day."""
    days = series.days
    frame = Frame()
    low, high = _extent(
        [v for d in days for v in (d.lo, d.hi, d.observed, d.expected)], floor_zero=True
    )
    y = _Scale(low, high, frame.bottom, frame.top)
    x = _Scale(0, max(len(days) - 1, 1), frame.left, frame.right)
    xs = [x(i) if len(days) > 1 else (frame.left + frame.right) / 2 for i in range(len(days))]
    # A range that reaches below zero is drawn to the axis: the metric cannot go there.
    upper = [(xs[i], max(y(d.hi), frame.top)) for i, d in enumerate(days)]
    lower = [(xs[i], min(y(d.lo), frame.bottom)) for i, d in enumerate(days)]
    area = "M" + " L".join(f"{px},{py}" for px, py in upper + lower[::-1]) + " Z" if days else ""
    unit = series.unit

    def title(i: int) -> str:
        d = days[i]
        text = (
            f"{_day(d.day)}: {_money(d.observed, unit)}; expected {_money(d.expected, unit)} "
            f"({_money(d.lo, unit)} to {_money(d.hi, unit)})"
        )
        if d.flagged:
            text += "; flagged"
        if d.planted:
            text += f"; planted {d.planted.replace('_', ' ')}"
        return text

    marks = tuple(Mark(xs[i], y(d.observed), title(i)) for i, d in enumerate(days))
    every = max(1, math.ceil(len(days) / 6))
    flagged = sum(1 for d in days if d.flagged)
    return BandChart(
        frame=Frame(
            x_ticks=tuple(Tick(xs[i], _day(d.day)) for i, d in enumerate(days) if i % every == 0),
            y_ticks=tuple(Tick(y(v), _label(v)) for v in nice_ticks(low, high) if low <= v <= high),
        ),
        area=area,
        expected=_line([(xs[i], y(d.expected)) for i, d in enumerate(days)]),
        observed=_line([(m.x, m.y) for m in marks]),
        points=marks,
        flags=tuple(m for m, d in zip(marks, days, strict=True) if d.flagged),
        planted=tuple(
            Mark(m.x, frame.bottom, m.title) for m, d in zip(marks, days, strict=True) if d.planted
        ),
        summary=(
            f"{series.entity_name}, {series.metric}: {len(days)} days, {flagged} outside the "
            "expected range"
        ),
        callouts=_callouts(frame, marks, [d.note for d in days]),
    )


def curve_chart(curve: BudgetCurve, currency: str | None = None) -> CurveChart:
    """Conversions against daily spend: the days seen, the models' curves, and the budgets."""
    unit = currency or ""
    lines = [curve.fitted, curve.alternative, curve.truth]
    budgets = [s for s in (curve.current_spend, curve.recommended_spend) if s is not None]
    seen = [*curve.history, *curve.pseudo]
    spends = [p[0] for p in seen] + [p[0] for line in lines for p in line] + budgets
    values = [p[1] for p in seen] + [p[1] for line in lines for p in line]
    x_low, x_high = _extent(spends, floor_zero=True, pad=0.04)
    y_low, y_high = _extent(values, floor_zero=True)
    frame = Frame()
    x = _Scale(x_low, x_high, frame.left, frame.right)
    y = _Scale(y_low, y_high, frame.bottom, frame.top)

    def inside(points: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
        return [(x(s), y(v)) for s, v in points if x_low <= s <= x_high and y_low <= v <= y_high]

    def point(spend: float, value: float, what: str) -> Mark:
        text = f"{what}: {spend:,.0f} {unit} a day, {value:.1f} conversions".replace("  ", " ")
        return Mark(x(spend), y(value), text)

    def budget(
        spend: float | None, value: float | None, what: str, amount: float | None
    ) -> Mark | None:
        if spend is None or value is None or amount is None:
            return None
        text = (
            f"{what} budget {amount:,.0f} {unit}: about {spend:,.0f} {unit} spent, "
            f"{value:.1f} conversions a day"
        ).replace("  ", " ")
        return Mark(x(spend), y(value), text)

    return CurveChart(
        frame=Frame(
            x_ticks=tuple(
                Tick(x(v), _label(v)) for v in nice_ticks(x_low, x_high, 5) if x_low <= v <= x_high
            ),
            y_ticks=tuple(
                Tick(y(v), _label(v)) for v in nice_ticks(y_low, y_high) if y_low <= v <= y_high
            ),
        ),
        history=tuple(point(s, v, "A day") for s, v in curve.history),
        pseudo=tuple(point(s, v, "Global model") for s, v in curve.pseudo),
        fitted=_line(inside(curve.fitted)),
        alternative=_line(inside(curve.alternative)),
        truth=_line(inside(curve.truth)),
        current=budget(curve.current_spend, curve.expected_now, "Current", curve.current_budget),
        recommended=budget(
            curve.recommended_spend,
            curve.expected_recommended,
            "Recommended",
            curve.recommended_budget,
        ),
    )


def _callouts(
    frame: Frame, marks: Sequence[Mark], notes: Sequence[str | None]
) -> tuple[Callout, ...]:
    """A label for each noted day, set to the side that keeps it inside the plot."""
    found = []
    middle = (frame.left + frame.right) / 2
    for mark, note in zip(marks, notes, strict=True):
        if not note:
            continue
        left = mark.x > middle
        found.append(
            Callout(
                x=mark.x - 12 if left else mark.x + 12,
                y=min(max(mark.y + 3.5, frame.top + 9), frame.bottom - 4),
                anchor="end" if left else "start",
                text=note,
            )
        )
    return tuple(found)


def trial_chart(trial: BudgetTrial) -> TrialChart:
    """Weekly conversions for each way of setting budgets, on one scale."""
    weeks = trial.weeks
    values = [v for run in trial.runs for v in run.weekly]
    low, high = _extent(values, floor_zero=False, pad=0.15)
    # A full-width chart: drawn wide so its text is the size of every other chart's, with room
    # on the right for the end labels.
    frame = Frame(
        width=WIDE_WIDTH,
        height=WIDE_HEIGHT,
        right=WIDE_WIDTH - 150,
        bottom=WIDE_HEIGHT - BOTTOM,
    )
    x = _Scale(0, max(len(weeks) - 1, 1), frame.left, frame.right)
    y = _Scale(low, high, frame.bottom, frame.top)
    lines = []
    for run in trial.runs:
        points = [(x(i), y(v)) for i, v in enumerate(run.weekly)]
        if not points:
            continue
        last_x, last_y = points[-1]
        value = f"{run.per_day:.1f} a day"
        lines.append(
            TrialLine(
                key=run.key,
                label=run.label,
                points=_line(points),
                end=Mark(last_x, last_y, f"{run.label}: {value}"),
                value=value,
            )
        )
    # End labels are spread so two runs that finish together do not overprint.
    order = sorted(lines, key=lambda line: line.end.y)
    placed: list[float] = []
    for line in order:
        at = line.end.y + 3.5
        if placed and at - placed[-1] < 14:
            at = placed[-1] + 14
        placed.append(at)
    labels = tuple(
        Callout(x=frame.right + 10, y=at, anchor="start", text=line.label)
        for line, at in zip(order, placed, strict=True)
    )
    every = max(1, math.ceil(len(weeks) / 8))
    return TrialChart(
        frame=Frame(
            width=frame.width,
            height=frame.height,
            right=frame.right,
            bottom=frame.bottom,
            x_ticks=tuple(Tick(x(i), _day(w)) for i, w in enumerate(weeks) if i % every == 0),
            y_ticks=tuple(Tick(y(v), _label(v)) for v in nice_ticks(low, high) if low <= v <= high),
        ),
        lines=tuple(lines),
        labels=labels,
        summary="Conversions per week: "
        + "; ".join(f"{line.label} {line.value}" for line in lines),
    )


RANGE_WIDTH, RANGE_HEIGHT, RANGE_PAD = 360.0, 86.0, 48.0


def range_bar(example: RangeExample) -> RangeBar:
    """The model's range for one day and the observed value, on one scale."""
    low = min(example.lo, example.observed)
    high = max(example.hi, example.observed)
    span = (high - low) or 1.0

    def x(value: float) -> float:
        return round(RANGE_PAD + (RANGE_WIDTH - 2 * RANGE_PAD) * (value - low) / span, 2)

    def text(value: float) -> str:
        return f"{value:,.0f}" if example.metric == "spend" else f"{value:,.1f}"

    at = x(example.observed)
    anchor = "end" if at > RANGE_WIDTH - 90 else "start" if at < 90 else "middle"
    verb = "spent" if example.metric == "spend" else "recorded"
    outside = not example.lo <= example.observed <= example.hi
    return RangeBar(
        width=RANGE_WIDTH,
        height=RANGE_HEIGHT,
        lo=x(example.lo),
        expected=x(example.expected),
        hi=x(example.hi),
        observed=at,
        band_top=40.0,
        band_height=14.0,
        lo_label=text(example.lo),
        expected_label=f"expected {text(example.expected)}",
        hi_label=text(example.hi),
        observed_label=f"{verb} {text(example.observed)}",
        observed_anchor=anchor,
        outside=outside,
        summary=(
            f"{example.entity_name} on {_day(example.day)}: {verb} {text(example.observed)}, "
            f"against an expected {text(example.expected)} and a range of {text(example.lo)} to "
            f"{text(example.hi)}; {'outside' if outside else 'inside'} the range."
        ),
    )


def alert_bars(scores: Sequence[MethodScore]) -> tuple[AlertBar, ...]:
    """Each method's alerts split into real problems and false alarms, on one scale."""
    known = [s for s in scores if s.caught is not None and s.false_alarms is not None]
    longest = max((int(s.caught or 0) + int(s.false_alarms or 0) for s in known), default=0)
    if longest == 0:
        return ()
    return tuple(
        AlertBar(
            label=s.label,
            caught=int(s.caught or 0),
            false_alarms=int(s.false_alarms or 0),
            caught_width=round(100 * int(s.caught or 0) / longest, 2),
            false_width=round(100 * int(s.false_alarms or 0) / longest, 2),
        )
        for s in known
    )


def budget_bars(rows: Sequence[BudgetRow]) -> tuple[BudgetBar, ...]:
    """Each campaign's budget at the start, now, and as recommended, on one scale."""
    largest = max((v for r in rows for v in (r.start, r.now, r.recommended or 0.0)), default=0.0)
    if largest <= 0:
        return ()

    def at(value: float) -> float:
        return round(100 * value / largest, 2)

    def change(row: BudgetRow) -> str:
        if not row.start or abs(row.now / row.start - 1) < 0.005:
            return "no change"
        return f"{row.now / row.start - 1:+.0%}"

    return tuple(
        BudgetBar(
            row=r,
            start=at(r.start),
            now=at(r.now),
            recommended=None if r.recommended is None else at(r.recommended),
            change=change(r),
        )
        for r in rows
    )
