"""Why a KPI changed between two windows: an exact split into funnel rates and spend mix.

For each campaign i, conversions per unit of spend factor through the funnel:

    e_i = conversions / spend = 1000 * CTR * CVR / CPM,

and the account's conversions per unit of spend is the spend-weighted sum E = sum_i w_i * e_i,
where w_i is the campaign's share of spend. CPA is 1/E, conversions are S * E (S the total spend),
and ROAS multiplies each e_i by its value per conversion (AOV).

The change is split exactly, in two stages. For the campaigns that spent in both windows, the
change in E splits in levels into spend mix and each campaign's own rate change (the Bennet
indicator, dE = sum_i e_bar_i dw_i + sum_i w_bar_i de_i, with no residual). Each campaign's mix term
is (its rate - the account's) x its change in share, so moving share toward a campaign that
converts better than the account lowers CPA: the signs read the way a marketer reads them. Each
rate change splits into its funnel factors by their log changes, because L(e1, e0) * ln(e1/e0) =
e1 - e0 with L the logarithmic mean (LMDI within the campaign; Ang 2004, 2015). A rate that goes to
or from zero is wholly that factor's. A campaign that spent in only one window (new or paused)
then moves the aggregate from the continuing campaigns' rate: ln(E/E_both) = (E - E_both) /
L(E, E_both), split by each one's gap (its conversions minus E_both times its spend), also exact.
The parts are scaled to percentage points of the actual change, so they add up to the headline
exactly.

Conversions are lag-corrected with the account's lag curve (as pacing does); significance uses the
reported counts. Every number is computed here and every reading is code-written.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Literal

from paid_media_agent.analytics.goals import account_today
from paid_media_agent.analytics.panel import PanelRow, completeness, lag_curves, maturity_days
from paid_media_agent.config import AccountRegistry
from paid_media_agent.store.db import Store

if TYPE_CHECKING:
    from paid_media_agent.bandit.recommend import BanditConfig
    from paid_media_agent.predict.protocol import Predictor

Metric = Literal["cpa", "conversions", "roas"]
Factor = Literal[
    "total_spend", "spend_mix", "cpm", "ctr", "cvr", "aov", "cost_per_conversion", "new_or_paused"
]
FACTORS: tuple[Factor, ...] = (
    "total_spend",
    "spend_mix",
    "cpm",
    "ctr",
    "cvr",
    "aov",
    "cost_per_conversion",
    "new_or_paused",
)
RATE_FACTORS = ("cpm", "ctr", "cvr", "cost_per_conversion")
LABELS: dict[str, str] = {
    "total_spend": "total spend",
    "spend_mix": "spend moving between campaigns",
    "cpm": "cost per thousand impressions",
    "ctr": "click-through rate",
    "cvr": "conversion rate",
    "aov": "value per conversion",
    "cost_per_conversion": "cost per conversion",
    "new_or_paused": "campaigns starting or stopping",
}
DEFAULT_DAYS = 7
MAX_DRIVERS = 6
MAX_CAMPAIGNS = 10
MAX_CHANGES = 10
Z = 1.96
SPEND_MOVED = 0.10
"""A campaign's daily spend must move at least this much before its curve is asked about it."""
_TINY = 1e-12


def log_mean(a: float, b: float) -> float:
    """The logarithmic mean L(a, b) = (a - b) / (ln a - ln b), with L(a, a) = a and L(a, 0) = 0."""
    if a <= 0 or b <= 0:
        return 0.0
    if abs(a - b) <= _TINY * max(a, b):
        return a
    return (a - b) / (math.log(a) - math.log(b))


@dataclass
class Cell:
    """One campaign over one window."""

    account_alias: str
    platform: str
    provider_account_id: str
    entity_ref: str
    entity_name: str
    currency: str | None
    days: int = 0
    spend: float = 0.0
    impressions: float | None = None
    clicks: float | None = None
    conversions: float | None = None
    """Lag-corrected: what the window's conversions will be once late ones arrive."""
    reported: float | None = None
    value: float | None = None
    unknown_lag_days: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return (self.account_alias, self.entity_ref)

    @property
    def funnel(self) -> bool:
        return bool(self.impressions) and bool(self.clicks)

    def rate(self, factor: str) -> float | None:
        """The factor's value for this campaign in its window (CPM, CTR, CVR, AOV, CPA)."""
        conv = self.conversions or 0.0
        if factor == "cpm":
            return self.spend / self.impressions * 1000 if self.impressions else None
        if factor == "ctr":
            return self.clicks / self.impressions if self.impressions and self.clicks else None
        if factor == "cvr":
            return conv / self.clicks if self.clicks else None
        if factor == "aov":
            return self.value / conv if self.value is not None and conv > 0 else None
        if factor == "cost_per_conversion":
            return self.spend / conv if conv > 0 else None
        return None


@dataclass(frozen=True)
class Window:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def as_json(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


def window_cells(
    store: Store, aliases: Sequence[str], window: Window
) -> dict[tuple[str, str], Cell]:
    """Per-campaign totals over the window from the newest numbers, conversions lag-corrected."""
    if not aliases:
        return {}
    marks = ", ".join("?" * len(aliases))
    rows = store.fetch_dicts(
        f"""
        SELECT account_alias, platform, provider_account_id, entity_ref,
            any_value(entity_name) AS entity_name, any_value(currency) AS currency, day,
            sum(spend)::DOUBLE AS spend, sum(impressions)::DOUBLE AS impressions,
            sum(clicks)::DOUBLE AS clicks, sum(conversions)::DOUBLE AS conversions,
            count(conversions) AS reported, sum(conversion_value)::DOUBLE AS conversion_value,
            min(pulled_on - day) AS age_days
        FROM entity_daily_latest
        WHERE account_alias IN ({marks}) AND entity_type = 'campaign' AND day BETWEEN ? AND ?
            AND is_complete
        GROUP BY account_alias, platform, provider_account_id, entity_ref, day
        ORDER BY day
        """,  # noqa: S608 - only placeholders are interpolated
        [*aliases, window.start, window.end],
    )
    maturity, curves = maturity_days(store), lag_curves(store)
    cells: dict[tuple[str, str], Cell] = {}
    for r in rows:
        key = (r["account_alias"], r["entity_ref"])
        cell = cells.get(key)
        if cell is None:
            cell = cells[key] = Cell(
                account_alias=r["account_alias"],
                platform=r["platform"],
                provider_account_id=r["provider_account_id"],
                entity_ref=r["entity_ref"],
                entity_name=r["entity_name"] or r["entity_ref"],
                currency=r["currency"],
            )
        cell.days += 1
        cell.spend += r["spend"] or 0.0
        if r["impressions"] is not None:
            cell.impressions = (cell.impressions or 0.0) + r["impressions"]
        if r["clicks"] is not None:
            cell.clicks = (cell.clicks or 0.0) + r["clicks"]
        share = completeness(
            PanelRow(
                r["platform"], r["provider_account_id"], r["account_alias"], r["entity_ref"],
                cell.entity_name, r["day"], r["spend"] or 0.0, r["conversions"], True,
                int(r["age_days"]), None,
            ),
            maturity,
            curves,
        )  # fmt: skip
        if r["reported"] and share is None:
            cell.unknown_lag_days += 1
        # Value arrives with its conversions, so it is corrected by the same share.
        factor = 1 / max(share, 0.05) if share is not None else 1.0
        if r["conversion_value"] is not None:
            cell.value = (cell.value or 0.0) + r["conversion_value"] * factor
        if r["reported"]:
            conv = r["conversions"] or 0.0
            cell.reported = (cell.reported or 0.0) + conv
            cell.conversions = (cell.conversions or 0.0) + conv * factor
    return cells


# The decomposition ----------------------------------------------------------------------------


@dataclass
class Contribution:
    account_alias: str
    entity_ref: str
    factor: Factor
    log_effect: float
    """Contribution to ln(Y1/Y0), already signed for the metric."""
    points: float = 0.0
    """Percentage points of the headline's relative change."""
    before: float | None = None
    after: float | None = None
    within_noise: bool | None = None


@dataclass
class Decomposition:
    metric: Metric
    before: float | None
    after: float | None
    log_change: float | None
    contributions: list[Contribution] = field(default_factory=list)
    reason: str | None = None
    """Why the change cannot be split, when it cannot."""
    weights: dict[tuple[str, str], float] = field(default_factory=dict)
    """Each continuing campaign's weight on its log rate change: w_bar * L(e1, e0) / L(E1, E0)."""

    def amount(self, points: float) -> float | None:
        """Points of the relative change, in the metric's own units (currency for CPA)."""
        return None if self.before is None else points / 100 * self.before

    def points_per_log(self) -> float:
        """Percentage points of the headline per unit of signed log effect."""
        total, relative = self.log_change or 0.0, self.change or 0.0
        return (relative / total if abs(total) > _TINY else 1.0) * 100

    @property
    def change(self) -> float | None:
        if self.before is None or self.after is None or self.before == 0:
            return None
        return self.after / self.before - 1

    def effects(self) -> dict[str, float]:
        totals: dict[str, float] = {f: 0.0 for f in FACTORS}
        for c in self.contributions:
            totals[c.factor] += c.points
        return totals


def _numerator(cell: Cell, metric: Metric) -> float:
    return (cell.value or 0.0) if metric == "roas" else (cell.conversions or 0.0)


def _aggregate(cells: Iterable[Cell]) -> tuple[float, float, float | None]:
    """(spend, conversions, value) over the cells."""
    spend = conv = 0.0
    value: float | None = None
    for c in cells:
        spend += c.spend
        conv += c.conversions or 0.0
        if c.value is not None:
            value = (value or 0.0) + c.value
    return spend, conv, value


def headline(cells: Iterable[Cell], metric: Metric) -> float | None:
    spend, conv, value = _aggregate(list(cells))
    if metric == "cpa":
        return spend / conv if conv > 0 else None
    if metric == "conversions":
        return conv
    return value / spend if value is not None and spend > 0 else None


def _noise(factor: str, a: Cell, b: Cell) -> bool | None:
    """Whether the campaign's change in this factor is within Poisson noise."""
    if factor in ("spend_mix", "new_or_paused", "total_spend"):
        return None
    if factor == "cpm":
        counts = (a.impressions, b.impressions)
    elif factor == "ctr":
        counts = (a.clicks, b.clicks)
    else:
        counts = (a.reported, b.reported)
    if not counts[0] or not counts[1]:
        return None
    ra, rb = a.rate(factor), b.rate(factor)
    if not ra or not rb:
        return None
    se = math.sqrt(1 / counts[0] + 1 / counts[1])
    return abs(math.log(rb / ra)) <= Z * se


def decompose(
    previous: dict[tuple[str, str], Cell],
    current: dict[tuple[str, str], Cell],
    metric: Metric,
) -> Decomposition:
    """Split the metric's change from `previous` to `current` into campaign x factor effects.

    Two exact stages. Campaigns that spent in both windows are split in levels into spend mix and
    each campaign's rate change (Bennet), and each rate change into its funnel factors by their log
    changes (LMDI within the campaign). A campaign that spent in only one window then moves the
    aggregate away from the continuing campaigns' rate by (its numerator - that rate x its spend),
    so starting a campaign cheaper than the rest lowers CPA and stopping a dear one does too.
    """
    before, after = headline(previous.values(), metric), headline(current.values(), metric)
    result = Decomposition(metric=metric, before=before, after=after, log_change=None)
    keys = sorted(set(previous) | set(current))
    both = [
        k
        for k in keys
        if k in previous and k in current and previous[k].spend > 0 and current[k].spend > 0
    ]
    only_before = [k for k in keys if k not in both and k in previous and previous[k].spend > 0]
    only_after = [k for k in keys if k not in both and k in current and current[k].spend > 0]
    s0, _, _ = _aggregate(previous.values())
    s1, _, _ = _aggregate(current.values())
    c0 = sum(previous[k].spend for k in both)
    c1 = sum(current[k].spend for k in both)
    n0 = sum(_numerator(previous[k], metric) for k in both)
    n1 = sum(_numerator(current[k], metric) for k in both)
    missing = "value is not reported in both windows" if metric == "roas" else None
    if before is None or after is None or before <= 0 or after <= 0:
        result.reason = missing or "there are no conversions in one of the windows"
        return result
    if not both or n0 <= 0 or n1 <= 0:
        result.reason = missing or "no campaign converted in both windows"
        return result
    sign = -1.0 if metric == "cpa" else 1.0
    e0, e1 = n0 / c0, n1 / c1
    big_l = log_mean(e1, e0)
    contributions: list[Contribution] = []

    def add(key: tuple[str, str], factor: Factor, log_effect: float, **extra: Any) -> None:
        contributions.append(Contribution(key[0], key[1], factor, sign * log_effect, **extra))

    # Continuing campaigns, in levels (Bennet): dE = sum(e_bar * dw) + sum(w_bar * de), exactly.
    # Mix per campaign is (its rate - the account's) x its change in share, so moving share toward
    # a campaign that converts better than the account lowers CPA; the sum is unchanged.
    account_rate = (e0 + e1) / 2
    for key in both:
        a, b = previous[key], current[key]
        ea, eb = _numerator(a, metric) / a.spend, _numerator(b, metric) / b.spend
        wa, wb = a.spend / c0, b.spend / c1
        mix = ((ea + eb) / 2 - account_rate) * (wb - wa)
        add(key, "spend_mix", mix / big_l, before=a.spend / s0, after=b.spend / s1)
        share = (wa + wb) / 2
        if ea <= 0 or eb <= 0:
            # A rate went to or from zero: the campaign's whole rate change is that factor's.
            if not ea and not eb:
                continue
            conv_zero = not (a.conversions or 0.0) or not (b.conversions or 0.0)
            factor: Factor = (
                ("cvr" if a.funnel and b.funnel else "cost_per_conversion") if conv_zero else "aov"
            )
            add(key, factor, share * (eb - ea) / big_l, before=a.rate(factor),
                after=b.rate(factor))  # fmt: skip
            continue
        # L(e1, e0) * ln(e1 / e0) = e1 - e0, so each factor's log change carries its exact part.
        weight = share * log_mean(eb, ea) / big_l
        result.weights[key] = weight
        rates: list[tuple[Factor, float]] = []
        if a.funnel and b.funnel:
            rates = [("cpm", -1.0), ("ctr", 1.0), ("cvr", 1.0)]
        else:
            rates = [("cost_per_conversion", -1.0)]
        if metric == "roas":
            rates.append(("aov", 1.0))
        for name, power in rates:
            ra, rb = a.rate(name), b.rate(name)
            if not ra or not rb:
                continue
            add(key, name, weight * power * math.log(rb / ra), before=ra, after=rb,
                within_noise=_noise(name, a, b))  # fmt: skip
    # Entering and leaving campaigns: ln(E1/E1_both) = (E1 - E1_both) / L(E1, E1_both), exactly.
    for window, cells, spend, rate, direction in (
        ("after", only_after, s1, e1, 1.0),
        ("before", only_before, s0, e0, -1.0),
    ):
        if not cells:
            continue
        source = current if window == "after" else previous
        numerator = sum(_numerator(source[k], metric) for k in source if source[k].spend > 0)
        whole = numerator / spend
        link = log_mean(whole, rate) if whole > 0 else 0.0
        for key in cells:
            cell = source[key]
            gap = _numerator(cell, metric) - rate * cell.spend
            if link <= 0:
                continue
            add(key, "new_or_paused", direction * gap / (spend * link),
                before=0.0 if window == "after" else cell.spend,
                after=cell.spend if window == "after" else 0.0)  # fmt: skip
    whole0 = sum(_numerator(c, metric) for c in previous.values() if c.spend > 0) / s0
    whole1 = sum(_numerator(c, metric) for c in current.values() if c.spend > 0) / s1
    total = sign * math.log(whole1 / whole0)
    if metric == "conversions":
        total_spend = math.log(s1 / s0)
        contributions.append(Contribution("", "", "total_spend", total_spend, before=s0, after=s1))
        total += total_spend
    result.log_change = total
    relative = after / before - 1
    scale = relative / total if abs(total) > _TINY else 1.0
    for c in contributions:
        c.points = c.log_effect * scale * 100
    result.contributions = contributions
    return result


def significance(
    previous: Iterable[Cell], current: Iterable[Cell], log_change: float | None
) -> bool | None:
    """Whether the headline moved by more than Poisson noise on the reported conversions."""
    c0 = sum(c.reported or 0.0 for c in previous)
    c1 = sum(c.reported or 0.0 for c in current)
    if log_change is None or c0 <= 0 or c1 <= 0:
        return None
    return abs(log_change) > Z * math.sqrt(1 / c0 + 1 / c1)


# The report --------------------------------------------------------------------------------------


@dataclass
class CampaignShift:
    """What one campaign added to the change, and whether its own curve expected it."""

    account_alias: str
    entity_ref: str
    entity_name: str
    points: float
    mix_points: float
    rate_points: float
    daily_spend_before: float | None
    daily_spend_after: float | None
    expected_rate_points: float | None = None
    """The part of `rate_points` its response curve expects from its own change in spend."""
    constraint: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "account_alias": self.account_alias,
            "campaign": self.entity_ref,
            "name": self.entity_name,
            "points": round(self.points, 2),
            "mix_points": round(self.mix_points, 2),
            "rate_points": round(self.rate_points, 2),
            "daily_spend_before": _r(self.daily_spend_before),
            "daily_spend_after": _r(self.daily_spend_after),
            "expected_from_spend_points": _r(self.expected_rate_points),
            "constraint": self.constraint,
        }


@dataclass
class ChangeReport:
    accounts: list[str]
    metric: Metric
    currency: str | None
    current: Window
    previous: Window
    decomposition: Decomposition
    significant: bool | None
    campaigns: list[CampaignShift] = field(default_factory=list)
    by_account: list[dict[str, Any]] = field(default_factory=list)
    known_changes: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    names: dict[tuple[str, str], str] = field(default_factory=dict)
    reading: str = ""

    def drivers(self) -> list[Contribution]:
        ranked = [c for c in self.decomposition.contributions if c.factor != "total_spend"]
        ranked.sort(key=lambda c: abs(c.points), reverse=True)
        return [c for c in ranked if abs(c.points) >= 0.05][:MAX_DRIVERS]

    def as_json(self) -> dict[str, Any]:
        d = self.decomposition
        effects = d.effects()
        headline_points = (d.change or 0.0) * 100
        return {
            "accounts": self.accounts,
            "metric": self.metric,
            "currency": self.currency,
            "current_window": self.current.as_json(),
            "previous_window": self.previous.as_json(),
            "previous": _r(d.before),
            "current": _r(d.after),
            "change": _r(d.change, 4),
            "significant": self.significant,
            "effects": [
                {
                    "factor": f,
                    "label": LABELS[f],
                    "points": round(p, 2),
                    "amount": _r(d.amount(p), 4),
                    "share": round(p / headline_points, 3) if abs(headline_points) > 1e-9 else None,
                }
                for f, p in effects.items()
                if abs(p) >= 0.005
            ],
            "drivers": [self._driver(c) for c in self.drivers()],
            "campaigns": [c.as_json() for c in self.campaigns],
            "by_account": self.by_account,
            "known_changes": self.known_changes,
            "unsplit_reason": d.reason,
            "notes": self.notes,
            "reading": self.reading,
        }

    def _driver(self, c: Contribution) -> dict[str, Any]:
        return {
            "account_alias": c.account_alias,
            "campaign": c.entity_ref,
            "name": self.names.get((c.account_alias, c.entity_ref), c.entity_ref),
            "factor": c.factor,
            "points": round(c.points, 2),
            "amount": _r(self.decomposition.amount(c.points), 4),
            "before": _r(c.before, 4),
            "after": _r(c.after, 4),
            "within_noise": c.within_noise,
            "reading": driver_reading(c, self.names, self.currency, self.effect_text),
        }

    def effect_text(self, points: float) -> str:
        """'-6.5 points (-1.82 USD of CPA)': the part of the change, in points and in units."""
        amount = self.decomposition.amount(points)
        text = f"{points:+.1f} points"
        if amount is None:
            return text
        if self.metric == "cpa":
            unit = f"{amount:+,.2f}" + (f" {self.currency}" if self.currency else "") + " of CPA"
        elif self.metric == "roas":
            unit = f"{amount:+.2f} of ROAS"
        else:
            unit = f"{amount:+,.1f} conversions"
        return f"{text} ({unit})"


def _r(value: float | None, places: int = 2) -> float | None:
    return None if value is None else round(value, places)


def _money(value: float, currency: str | None) -> str:
    number = f"{value:,.0f}" if abs(value) >= 1000 else f"{value:,.2f}"
    return f"{number} {currency}" if currency else number


def _factor_value(factor: str, value: float | None, currency: str | None) -> str:
    if value is None:
        return "n/a"
    if factor in ("ctr", "cvr"):
        return f"{value:.2%}"
    if factor == "spend_mix":
        return f"{value:.0%} of spend"
    if factor == "new_or_paused":
        return _money(value, currency)
    return _money(value, currency)


def driver_reading(
    c: Contribution,
    names: dict[tuple[str, str], str],
    currency: str | None,
    effect_text: Callable[[float], str] | None = None,
) -> str:
    name = names.get((c.account_alias, c.entity_ref), c.entity_ref)
    where = f"{name} ({c.entity_ref})"
    effect = effect_text(c.points) if effect_text else f"{c.points:+.1f} points"
    if c.factor == "new_or_paused":
        verb = "started" if not c.before else "stopped" if not c.after else "changed"
        return (
            f"{where} {verb} (spend {_factor_value(c.factor, c.before, currency)} -> "
            f"{_factor_value(c.factor, c.after, currency)}): {effect}"
        )
    text = (
        f"{where} {LABELS[c.factor]} {_factor_value(c.factor, c.before, currency)} -> "
        f"{_factor_value(c.factor, c.after, currency)}: {effect}"
    )
    if c.within_noise:
        text += " (within noise)"
    return text


_METRIC_NAMES = {"cpa": "CPA", "conversions": "Conversions", "roas": "ROAS"}


def _fmt_metric(metric: Metric, value: float | None, currency: str | None) -> str:
    if value is None:
        return "n/a"
    if metric == "cpa":
        return _money(value, currency)
    if metric == "roas":
        return f"{value:.2f}"
    return f"{value:,.1f}"


def report_reading(report: ChangeReport) -> str:
    d = report.decomposition
    name = _METRIC_NAMES[report.metric]
    span = (
        f"{report.current.start.isoformat()} to {report.current.end.isoformat()} against "
        f"{report.previous.start.isoformat()} to {report.previous.end.isoformat()}"
    )
    if d.change is None or d.reason:
        return (
            f"{name} cannot be compared for {span}: {d.reason or 'a window has no data'}. "
            f"Previous {_fmt_metric(report.metric, d.before, report.currency)}, current "
            f"{_fmt_metric(report.metric, d.after, report.currency)}."
        )
    direction = "rose" if d.change > 0 else "fell" if d.change < 0 else "held"
    parts = [
        f"{name} {direction} {abs(d.change):.1%} "
        f"({_fmt_metric(report.metric, d.before, report.currency)} -> "
        f"{_fmt_metric(report.metric, d.after, report.currency)}) for {span}"
    ]
    if report.significant is False:
        parts[0] += ", within normal noise for this many conversions"
    elif report.significant:
        parts[0] += ", more than noise"
    parts[0] += "."
    effects = sorted(d.effects().items(), key=lambda kv: abs(kv[1]), reverse=True)
    lines = [
        f"{LABELS[f]} {report.effect_text(p)}"
        for f, p in effects
        if abs(p) >= max(0.5, abs(d.change) * 10)
    ]
    if lines:
        parts.append("By cause: " + "; ".join(lines[:4]) + ".")
    drivers = report.drivers()[:3]
    real = [c for c in drivers if c.within_noise is False and c.factor in RATE_FACTORS]
    if report.significant is False and real:
        c = real[0]
        parts.append(
            f"But {c.entity_ref}'s {LABELS[c.factor]} moved by more than noise "
            f"({_factor_value(c.factor, c.before, report.currency)} -> "
            f"{_factor_value(c.factor, c.after, report.currency)}); other campaigns offset it."
        )
    if drivers:
        parts.append(
            "Largest: "
            + "; ".join(
                driver_reading(c, report.names, report.currency, report.effect_text)
                for c in drivers
            )
            + "."
        )
    for shift in report.campaigns:
        if shift.expected_rate_points is None or abs(shift.rate_points) < 1.0:
            continue
        before, after = shift.daily_spend_before or 0.0, shift.daily_spend_after or 0.0
        moved = after / before - 1 if before else 0.0
        same_way = shift.expected_rate_points * shift.rate_points > 0
        if same_way and abs(shift.expected_rate_points) >= abs(shift.rate_points):
            parts.append(
                f"{shift.entity_ref}'s rate change ({shift.rate_points:+.1f} points) is no more "
                f"than its response curve expects from spending {moved:+.0%} a day "
                f"({shift.expected_rate_points:+.1f}): diminishing returns, not a new problem."
            )
        elif same_way and abs(shift.expected_rate_points) >= 0.5 * abs(shift.rate_points):
            parts.append(
                f"{shift.entity_ref}'s rate change is mostly what its response curve expects from "
                f"spending {moved:+.0%} a day ({shift.expected_rate_points:+.1f} of "
                f"{shift.rate_points:+.1f} points): diminishing returns, not a new problem."
            )
        else:
            parts.append(
                f"{shift.entity_ref}'s rate change goes beyond what its spend change explains "
                f"({shift.expected_rate_points:+.1f} of {shift.rate_points:+.1f} points expected)."
            )
        break
    if report.known_changes:
        first = report.known_changes[0]
        parts.append(
            f"Settings changed in these windows: {first['campaign']} {first['field']} "
            f"{first['before']} -> {first['after']} on {first['day']}"
            + (
                f" and {len(report.known_changes) - 1} more"
                if len(report.known_changes) > 1
                else ""
            )
            + "."
        )
    return " ".join(parts)


def campaign_shifts(
    decomposition: Decomposition,
    previous: dict[tuple[str, str], Cell],
    current: dict[tuple[str, str], Cell],
) -> list[CampaignShift]:
    by_key: dict[tuple[str, str], list[Contribution]] = defaultdict(list)
    for c in decomposition.contributions:
        if c.factor != "total_spend":
            by_key[(c.account_alias, c.entity_ref)].append(c)
    shifts = []
    for key, items in by_key.items():
        a, b = previous.get(key), current.get(key)
        cell = b or a
        if cell is None:
            continue
        shifts.append(
            CampaignShift(
                account_alias=key[0],
                entity_ref=key[1],
                entity_name=cell.entity_name,
                points=sum(c.points for c in items),
                mix_points=sum(c.points for c in items if c.factor == "spend_mix"),
                rate_points=sum(c.points for c in items if c.factor in RATE_FACTORS),
                daily_spend_before=a.spend / a.days if a and a.days else None,
                daily_spend_after=b.spend / b.days if b and b.days else None,
            )
        )
    shifts.sort(key=lambda s: abs(s.points), reverse=True)
    return shifts[:MAX_CAMPAIGNS]


def known_changes(
    store: Store, aliases: Sequence[str], start: date, end: date
) -> list[dict[str, Any]]:
    """Budget, status, bid strategy and target changes seen in the settings from start to end."""
    if not aliases:
        return []
    marks = ", ".join("?" * len(aliases))
    rows = store.fetch_dicts(
        f"""
        SELECT account_alias, entity_ref, valid_from, status, daily_budget::DOUBLE AS daily_budget,
            bid_strategy, target_cpa::DOUBLE AS target_cpa, target_roas::DOUBLE AS target_roas,
            lag(status) OVER w AS prev_status,
            lag(daily_budget::DOUBLE) OVER w AS prev_daily_budget,
            lag(bid_strategy) OVER w AS prev_bid_strategy,
            lag(target_cpa::DOUBLE) OVER w AS prev_target_cpa,
            lag(target_roas::DOUBLE) OVER w AS prev_target_roas,
            row_number() OVER w AS version
        FROM entity_settings_history
        WHERE account_alias IN ({marks}) AND entity_type = 'campaign'
        WINDOW w AS (PARTITION BY platform, provider_account_id, entity_type, entity_ref
                     ORDER BY valid_from)
        """,  # noqa: S608 - only placeholders are interpolated
        list(aliases),
    )
    found = []
    for r in rows:
        day = r["valid_from"].date()
        if r["version"] == 1 or not start <= day <= end:
            continue
        for name in ("status", "daily_budget", "bid_strategy", "target_cpa", "target_roas"):
            old, new = r[f"prev_{name}"], r[name]
            if old != new:
                found.append(
                    {
                        "account_alias": r["account_alias"],
                        "campaign": r["entity_ref"],
                        "day": day.isoformat(),
                        "field": name,
                        "before": round(old, 2) if isinstance(old, float) else old,
                        "after": round(new, 2) if isinstance(new, float) else new,
                    }
                )
    found.sort(key=lambda c: c["day"])
    return found


def default_windows(
    store: Store, aliases: Sequence[str], days: int = DEFAULT_DAYS, *, before: date | None = None
) -> tuple[Window, Window] | None:
    """The newest `days` complete days across the accounts (before `before`, the accounts' today),
    and the `days` before them. A partial day would understate spend and conversions."""
    if not aliases:
        return None
    marks = ", ".join("?" * len(aliases))
    cutoff = "AND day < ?" if before is not None else ""
    newest = store.fetch(
        f"SELECT max(day) FROM entity_daily_latest WHERE account_alias IN ({marks}) "  # noqa: S608
        f"AND entity_type = 'campaign' AND is_complete {cutoff}",
        [*aliases, *([before] if before is not None else [])],
    )[0][0]
    if newest is None:
        return None
    current = Window(newest - timedelta(days=days - 1), newest)
    previous = Window(current.start - timedelta(days=days), current.start - timedelta(days=1))
    return current, previous


def explain(
    store: Store,
    aliases: Sequence[str],
    *,
    metric: Metric,
    current: Window,
    previous: Window,
    currency: str | None,
) -> ChangeReport:
    """The report without the curve check (which needs fitted curves; see `tools/drivers.py`)."""
    before = window_cells(store, aliases, previous)
    after = window_cells(store, aliases, current)
    d = decompose(before, after, metric)
    all_cells = {**before, **after}
    report = ChangeReport(
        accounts=list(aliases),
        metric=metric,
        currency=currency,
        current=current,
        previous=previous,
        decomposition=d,
        significant=significance(before.values(), after.values(), d.log_change),
        names={k: c.entity_name for k, c in all_cells.items()},
    )
    report.campaigns = campaign_shifts(d, before, after)
    if len(aliases) > 1 and d.contributions:
        per: dict[str, float] = defaultdict(float)
        for c in d.contributions:
            if c.account_alias:
                per[c.account_alias] += c.points
        report.by_account = [
            {"account_alias": a, "points": round(p, 2)}
            for a, p in sorted(per.items(), key=lambda kv: abs(kv[1]), reverse=True)
        ]
    report.known_changes = known_changes(store, aliases, previous.start, current.end)[:MAX_CHANGES]
    report.reading = report_reading(report)
    unknown = sum(c.unknown_lag_days for c in after.values())
    added = sum((c.conversions or 0) - (c.reported or 0) for c in after.values())
    if added > 0.05:
        report.notes.append(
            f"current-window conversions include about {added:,.1f} expected late conversions "
            "(from the account's lag curve)"
        )
    if unknown:
        report.notes.append(
            f"{unknown} recent campaign-days have no measured lag yet; their conversions are "
            "counted as reported and may still grow"
        )
    if current.days != previous.days:
        report.notes.append("the windows differ in length; shares of spend still compare")
    if current.days % 7 or previous.days % 7:
        report.notes.append("windows that are not whole weeks can mix weekday patterns")
    if not before or not after:
        report.notes.append("one window has no synced data for these accounts (run `sync`)")
    return report


def expected_rate_points(d: Decomposition, key: tuple[str, str], curve_ratio: float) -> float:
    """Points its response curve expects, from the ratio of conversions per spend it predicts at
    the new daily spend to the old one."""
    sign = -1.0 if d.metric == "cpa" else 1.0
    return sign * d.weights.get(key, 0.0) * math.log(curve_ratio) * d.points_per_log()


async def add_curve_check(
    report: ChangeReport,
    store: Store,
    predictor: Predictor | None,
    config: BanditConfig,
) -> ChangeReport:
    """Ask each moving campaign's response curve how much of its rate change its spend explains.

    A campaign that spent more converts less per unit of spend on a concave curve; that part of
    its rate change is diminishing returns, not a new problem. Curves are fitted per account as a
    budget recommendation would fit them, on history through the current window. Each campaign
    also gets what limits its spend. Accounts whose curves cannot be fitted are skipped, noted.
    """
    from paid_media_agent.bandit.fit import fit_account

    as_of = report.current.end + timedelta(days=1)
    arms: dict[tuple[str, str], Any] = {}
    for alias in report.accounts:
        try:
            fitted = await fit_account(
                store, predictor, as_of=as_of, config=config, account_alias=alias
            )
        except ValueError as exc:
            report.notes.append(f"{alias}: curves not checked ({exc})")
            continue
        for arm in fitted.arms:
            arms[(alias, arm.entity_ref)] = (arm, fitted)
    checked = False
    for shift in report.campaigns:
        found = arms.get((shift.account_alias, shift.entity_ref))
        if found is None:
            continue
        arm, fitted = found
        shift.constraint = arm.constraint.kind
        before, after = shift.daily_spend_before, shift.daily_spend_after
        curve = fitted.curve(arm) if arm.eligible else None
        if curve is None or not before or not after or abs(after / before - 1) < SPEND_MOVED:
            continue
        per_before, per_after = curve.value(before) / before, curve.value(after) / after
        if per_before <= 0 or per_after <= 0:
            continue
        shift.expected_rate_points = expected_rate_points(
            report.decomposition, (shift.account_alias, shift.entity_ref), per_after / per_before
        )
        checked = True
    if checked:
        report.notes.append(
            "expected_from_spend_points is the part of a campaign's rate change its own response "
            "curve expects from its change in daily spend (diminishing returns)"
        )
    report.reading = report_reading(report)
    return report


def resolve_windows(
    store: Store,
    aliases: Sequence[str],
    *,
    current_start: date | None = None,
    current_end: date | None = None,
    previous_start: date | None = None,
    previous_end: date | None = None,
    today: date | None = None,
) -> tuple[Window, Window]:
    """The windows asked for; by default the newest week of data against the week before."""
    if (current_start is None) != (current_end is None) or (previous_start is None) != (
        previous_end is None
    ):
        raise ValueError("give both the start and the end of a window")
    if current_start is None or current_end is None:
        if previous_start is not None:
            raise ValueError("give the current window when giving the previous one")
        found = default_windows(store, aliases, before=today)
        if found is None:
            raise ValueError("no synced history for these accounts; run sync first")
        return found
    current = Window(current_start, current_end)
    if current.days < 1:
        raise ValueError("the current window ends before it starts")
    if previous_start is None or previous_end is None:
        previous = Window(
            current.start - timedelta(days=current.days), current.start - timedelta(days=1)
        )
    else:
        previous = Window(previous_start, previous_end)
        if previous.days < 1:
            raise ValueError("the previous window ends before it starts")
    return current, previous


async def explain_accounts(
    store: Store,
    accounts: AccountRegistry,
    aliases: Sequence[str] | None,
    *,
    metric: Metric = "cpa",
    predictor: Predictor | None,
    config: BanditConfig,
    current_start: date | None = None,
    current_end: date | None = None,
    previous_start: date | None = None,
    previous_end: date | None = None,
) -> list[ChangeReport]:
    """One report for accounts sharing a currency; one per account when currencies differ."""
    chosen = list(aliases) if aliases else list(accounts.aliases())
    currencies: dict[str, str | None] = {}
    for alias in chosen:
        binding = accounts.resolve(alias)
        if binding is None:
            raise ValueError(f"unknown account alias {alias}; call list_accounts")
        currencies[alias] = binding.currency
    groups = [chosen] if len(set(currencies.values())) <= 1 else [[a] for a in chosen]
    reports = []
    for group in groups:
        current, previous = resolve_windows(
            store,
            group,
            current_start=current_start,
            current_end=current_end,
            previous_start=previous_start,
            previous_end=previous_end,
            today=min(account_today(accounts, alias) for alias in group),
        )
        report = explain(
            store,
            group,
            metric=metric,
            current=current,
            previous=previous,
            currency=currencies[group[0]],
        )
        if len(groups) > 1:
            report.notes.append(
                "accounts use different currencies, so each is explained on its own"
            )
        reports.append(await add_curve_check(report, store, predictor, config))
    return reports
