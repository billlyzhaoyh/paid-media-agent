"""What a different set of daily budgets would buy: spend, conversions and CPA, with uncertainty.

The forecast uses exactly the curves and spend limits a budget recommendation would (`fit_account`):
for each campaign, spend is what the budget lets through, up to the ceiling where demand or a bid
target limits spend (`Arm.expected_spend`), and conversions come from its fitted response curve at
that spend. Intervals come from plain draws of each curve's parameters (multivariate normal on the
posterior, invalid draws dropped). The same draws price the current budgets and the scenario, so
the difference is compared like for like; account totals add the draws across campaigns. The
intervals cover how uncertain the curves are and how far each curve has recently been from the
campaign's own conversions (the same error for both, per campaign), not day-to-day noise.

A scenario either changes named campaigns (a new budget or a relative change) or sets a new
account total, split in proportion to today's budgets or the way the curves say is best (without
the per-change step limit: it answers where the account should end up, not the next step).
Nothing is recorded or changed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, Literal

import numpy as np

from paid_media_agent.analytics.goals import Goal, GoalStore, account_today, goal_line
from paid_media_agent.analytics.pacing import compute_pacing
from paid_media_agent.bandit.allocate import allocate
from paid_media_agent.bandit.arms import Arm
from paid_media_agent.bandit.fit import FittedAccount, fit_account
from paid_media_agent.bandit.posterior import Posterior, is_valid
from paid_media_agent.bandit.recommend import BanditConfig
from paid_media_agent.config import AccountRegistry
from paid_media_agent.domain.analysis import verdict
from paid_media_agent.predict.protocol import Predictor
from paid_media_agent.store.db import Store

Split = Literal["proportional", "best"]
DRAWS = 400
LOW_Q, HIGH_Q = 10, 90
RECENT_DAYS = 28
MIN_ACCEPTED = 0.5
"""Below this share of valid curve draws, a campaign's interval is flagged as wide."""


@dataclass(frozen=True)
class BudgetChange:
    campaign: str
    budget: float | None = None
    change: float | None = None
    """Relative: 0.2 is +20%, -1 pauses."""


@dataclass(frozen=True)
class Scenario:
    changes: tuple[BudgetChange, ...] = ()
    total_daily_budget: float | None = None
    total_change: float | None = None
    split: Split = "proportional"
    horizon_days: int = 7

    def validate(self) -> None:
        given = sum(
            [bool(self.changes), self.total_daily_budget is not None, self.total_change is not None]
        )
        if given != 1:
            raise ValueError(
                "give exactly one of: changes to named campaigns, total_daily_budget, total_change"
            )
        for c in self.changes:
            if (c.budget is None) == (c.change is None):
                raise ValueError(f"{c.campaign}: give either budget or change, not both or neither")
            if c.budget is not None and c.budget < 0:
                raise ValueError(f"{c.campaign}: budget cannot be negative")
            if c.change is not None and c.change < -1:
                raise ValueError(f"{c.campaign}: change cannot be below -1 (-100%)")
        if self.total_daily_budget is not None and self.total_daily_budget <= 0:
            raise ValueError("total_daily_budget must be positive")
        if self.total_change is not None and self.total_change <= -1:
            raise ValueError("total_change must be above -1 (-100%)")


@dataclass
class Interval:
    mean: float
    low: float
    high: float

    def as_json(self, places: int = 2) -> dict[str, float]:
        return {
            "mean": round(self.mean, places),
            "low": round(self.low, places),
            "high": round(self.high, places),
        }


def _interval(mean: float, draws: np.ndarray) -> Interval:
    finite = draws[np.isfinite(draws)]
    if not len(finite):
        return Interval(mean, math.nan, math.nan)
    low, high = np.percentile(finite, [LOW_Q, HIGH_Q])
    return Interval(mean, float(low), float(high))


@dataclass
class CampaignForecast:
    entity_ref: str
    entity_name: str
    budget_now: float
    budget: float
    spend_now: float
    spend: float
    conversions_now: float | None
    conversions: float | None
    conversions_change: Interval | None = None
    constraint: str = "unknown"
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    vs_target: str | None = None
    """The incremental CPA against the account's target CPA, judged in code."""

    @property
    def incremental_cpa(self) -> float | None:
        if self.conversions is None or self.conversions_now is None:
            return None
        gained = self.conversions - self.conversions_now
        spent = self.spend - self.spend_now
        return spent / gained if abs(gained) > 1e-9 and spent * gained > 0 else None

    def as_json(self) -> dict[str, Any]:
        return {
            "campaign": self.entity_ref,
            "name": self.entity_name,
            "budget_now": round(self.budget_now, 2),
            "budget": round(self.budget, 2),
            "change": round(self.budget / self.budget_now - 1, 4) if self.budget_now else None,
            "spend_now": round(self.spend_now, 2),
            "spend": round(self.spend, 2),
            "conversions_now": _r(self.conversions_now),
            "conversions": _r(self.conversions),
            "conversions_change": (
                self.conversions_change.as_json() if self.conversions_change else None
            ),
            "incremental_cpa": _r(self.incremental_cpa),
            "incremental_vs_target": self.vs_target,
            "constraint": self.constraint,
            "flags": self.flags,
            "notes": self.notes,
        }


@dataclass
class Totals:
    budget: float
    spend: float
    conversions: Interval
    cpa: Interval | None

    def as_json(self) -> dict[str, Any]:
        return {
            "daily_budget": round(self.budget, 2),
            "spend": round(self.spend, 2),
            "conversions": self.conversions.as_json(),
            "cpa": self.cpa.as_json() if self.cpa else None,
        }


@dataclass
class BudgetForecast:
    """Daily expectations at two budget vectors from the same curve draws."""

    baseline: Totals
    scenario: Totals
    conversions_change: Interval
    spend_change: float
    campaigns: list[CampaignForecast]
    notes: list[str] = field(default_factory=list)

    @property
    def incremental_cpa(self) -> float | None:
        gained, spent = self.conversions_change.mean, self.spend_change
        return spent / gained if abs(gained) > 1e-9 and spent * gained > 0 else None


def _r(value: float | None, places: int = 2) -> float | None:
    return None if value is None or not math.isfinite(value) else round(value, places)


def curve_draws(post: Posterior, n: int, rng: np.random.Generator) -> tuple[np.ndarray, float]:
    """Up to `n` valid (kappa1, kappa2) draws from the posterior, and the share that were valid."""
    raw = rng.multivariate_normal(
        post.kappa_mean, post.kappa_cov, size=2 * n, check_valid="ignore", method="eigh"
    )
    ok = np.asarray([is_valid(float(k1), float(k2)) for k1, k2 in raw])
    accepted = float(ok.mean()) if len(ok) else 0.0
    valid = raw[ok][:n]
    return valid, accepted


def level_error(arm: Arm, curve: Any) -> float:
    """How far the mean curve has recently been from the campaign's own conversions (log sd).

    Over the last four weeks of settled history: the log ratio of actual to predicted conversions
    at the spend that happened, combined with the Poisson noise of that many conversions. The
    curve's parameter draws do not see this model error; without it the ranges are too narrow.
    """
    spend = arm.spend[-RECENT_DAYS:]
    actual = float(arm.conversions[-RECENT_DAYS:].sum())
    predicted = float(sum(curve.value(float(s)) for s in spend))
    if predicted <= 0 or actual <= 0:
        return 1.0 / math.sqrt(max(actual, 1.0))
    return math.sqrt(math.log(actual / predicted) ** 2 + 1.0 / actual)


def _values(post: Posterior, kappas: np.ndarray, spend: float) -> np.ndarray:
    return np.asarray([post.curve((float(k1), float(k2))).value(spend) for k1, k2 in kappas])


def _recent(arm: Arm) -> float:
    """Recent average daily conversions, for a campaign without a usable curve."""
    tail = arm.conversions[-RECENT_DAYS:]
    return float(np.mean(tail)) if len(tail) else 0.0


def forecast(
    fitted: FittedAccount,
    budgets: dict[str, float],
    *,
    draws: int = DRAWS,
    seed: int = 0,
    shared: bool = True,
) -> BudgetForecast:
    """Daily spend and conversions at today's budgets and at `budgets` (arm key to budget).

    `shared=False` draws the scenario's curves independently of the baseline's; it exists only to
    show, in tests, why the shared draws give the honest (narrower) interval on the difference.
    """
    rng = np.random.default_rng(seed)
    base_total = np.zeros(draws)
    scen_total = np.zeros(draws)
    campaigns: list[CampaignForecast] = []
    notes: list[str] = []
    budget_now_sum = budget_sum = spend_now_sum = spend_sum = 0.0
    base_mean = scen_mean = 0.0
    for arm in fitted.eligible:
        now = float(arm.current_budget or 0.0)
        new = float(budgets.get(arm.key, now))
        spend_now = arm.expected_spend(now)
        spend = arm.expected_spend(new) if new > 0 else 0.0
        budget_now_sum += now
        budget_sum += new
        spend_now_sum += spend_now
        spend_sum += spend
        row = CampaignForecast(
            entity_ref=arm.entity_ref,
            entity_name=arm.entity_name,
            budget_now=now,
            budget=new,
            spend_now=spend_now,
            spend=spend,
            conversions_now=None,
            conversions=None,
            constraint=arm.constraint.kind,
        )
        post = fitted.posteriors.get(arm.key)
        curve = fitted.curve(arm)
        if post is None or curve is None:
            recent = _recent(arm)
            row.conversions_now = recent
            row.conversions = recent if new > 0 else 0.0
            row.flags.append("no_curve")
            row.notes.append(
                "no reliable spend response yet; counted at its recent average conversions"
            )
            base_total += recent
            scen_total += row.conversions
            base_mean += recent
            scen_mean += row.conversions
            campaigns.append(row)
            continue
        row.conversions_now = curve.value(spend_now)
        row.conversions = curve.value(spend) if new > 0 else 0.0
        kappas, accepted = curve_draws(post, draws, rng)
        if len(kappas) < draws:
            # Too few valid draws: pad with the mean so the interval reflects what was drawn.
            pad = np.tile(np.asarray(post.kappa_mean[:2]), (draws - len(kappas), 1))
            kappas = np.vstack([kappas, pad]) if len(kappas) else pad
        if accepted < MIN_ACCEPTED:
            row.flags.append("wide_uncertainty")
        base = _values(post, kappas, spend_now)
        other = kappas if shared else curve_draws(post, draws, rng)[0]
        if len(other) < draws:
            other = np.vstack(
                [other, np.tile(np.asarray(post.kappa_mean[:2]), (draws - len(other), 1))]
            )
        scen = _values(post, other, spend) if new > 0 else np.zeros(draws)
        # The same model error scales today's and the scenario's conversions for a campaign.
        level = np.exp(rng.normal(0.0, level_error(arm, curve), draws))
        base, scen = base * level, scen * level
        row.conversions_change = _interval(row.conversions - row.conversions_now, scen - base)
        base_total += base
        scen_total += scen
        base_mean += row.conversions_now
        scen_mean += row.conversions
        campaigns.append(row)

    def totals(budget: float, spend: float, mean: float, draws_: np.ndarray) -> Totals:
        conv = _interval(mean, draws_)
        with np.errstate(divide="ignore", invalid="ignore"):
            cpa_draws = np.where(draws_ > 0, spend / draws_, np.nan)
        cpa = _interval(spend / mean, cpa_draws) if mean > 0 else None
        return Totals(budget=budget, spend=spend, conversions=conv, cpa=cpa)

    return BudgetForecast(
        baseline=totals(budget_now_sum, spend_now_sum, base_mean, base_total),
        scenario=totals(budget_sum, spend_sum, scen_mean, scen_total),
        conversions_change=_interval(scen_mean - base_mean, scen_total - base_total),
        spend_change=spend_sum - spend_now_sum,
        campaigns=campaigns,
        notes=notes,
    )


# Scenarios ---------------------------------------------------------------------------------------


def _movable(fitted: FittedAccount) -> list[Arm]:
    return [
        a
        for a in fitted.eligible
        if fitted.curve(a) is not None and a.constraint.kind != "learning"
    ]


def steady_bounds(arm: Arm, config: BanditConfig) -> tuple[float, float]:
    """Where a budget can sensibly end up, without the per-change step limit."""
    rules = arm.rules
    lower = max(config.min_budget, (rules.min_daily_budget or 0.0) if rules else 0.0)
    upper = config.max_spend_multiple * arm.max_spend / arm.spend_slope
    if arm.capped and arm.ceiling is not None:
        upper = min(upper, arm.ceiling.high / arm.spend_slope)
    if config.max_budget is not None:
        upper = min(upper, config.max_budget)
    return lower, max(upper, lower)


def best_split(fitted: FittedAccount, total: float, config: BanditConfig) -> dict[str, float]:
    """The curves' best split of `total` a day; campaigns without a curve or learning stay."""
    movable = _movable(fitted)
    moving = {a.key for a in movable}
    budgets = {a.key: float(a.current_budget or 0.0) for a in fitted.eligible}
    fixed = sum(v for k, v in budgets.items() if k not in moving)
    if not movable:
        return budgets
    curves = []
    for arm in movable:
        curve = fitted.curve(arm)
        assert curve is not None  # noqa: S101 - _movable keeps only arms with a curve
        curves.append(arm.decision_curve(curve))
    bounds = [steady_bounds(a, config) for a in movable]
    result = allocate(
        curves,
        [a.spend_slope for a in movable],
        [b[0] for b in bounds],
        [b[1] for b in bounds],
        max(total - fixed, 0.0),
    )
    for arm, budget in zip(movable, result.budgets, strict=True):
        budgets[arm.key] = float(budget)
    return budgets


def resolve_budgets(
    fitted: FittedAccount, scenario: Scenario, config: BanditConfig
) -> tuple[dict[str, float], str]:
    """Arm key to scenario budget, and a short description of the scenario."""
    scenario.validate()
    eligible = {a.entity_ref: a for a in fitted.eligible}
    known = {a.entity_ref: a for a in fitted.arms}
    budgets = {a.key: float(a.current_budget or 0.0) for a in fitted.eligible}
    if scenario.changes:
        parts = []
        for change in scenario.changes:
            arm = eligible.get(change.campaign)
            if arm is None:
                if change.campaign in known:
                    raise ValueError(
                        f"{change.campaign} is paused, has no recent spend, or lacks history; "
                        "what-if covers running campaigns"
                    )
                raise ValueError(
                    f"unknown campaign {change.campaign}; campaigns: " + ", ".join(sorted(eligible))
                )
            now = float(arm.current_budget or 0.0)
            new = change.budget if change.budget is not None else now * (1 + (change.change or 0))
            budgets[arm.key] = float(new)
            parts.append(
                f"{arm.entity_ref} {new / now - 1:+.0%}"
                if now
                else f"{arm.entity_ref} to {new:.2f}"
            )
        return budgets, ", ".join(parts)
    current_total = sum(budgets.values())
    total = (
        scenario.total_daily_budget
        if scenario.total_daily_budget is not None
        else current_total * (1 + (scenario.total_change or 0.0))
    )
    label = f"total {total:,.2f} a day ({total / current_total - 1:+.0%})" if current_total else ""
    if scenario.split == "best":
        return best_split(fitted, total, config), label + ", split by the curves"
    movable = {a.key for a in _movable(fitted)}
    fixed = sum(v for k, v in budgets.items() if k not in movable)
    moving_now = sum(v for k, v in budgets.items() if k in movable)
    factor = (total - fixed) / moving_now if moving_now > 0 else 1.0
    for key in movable:
        budgets[key] *= max(factor, 0.0)
    return budgets, label + ", in proportion to today's budgets"


def _flag(row: CampaignForecast, arm: Arm, config: BanditConfig) -> None:
    """Flags and notes that say how far to trust a campaign's forecast, and how to get there."""
    if (
        row.budget > 0
        and arm.max_spend > 0
        and row.spend > config.max_spend_multiple * arm.max_spend
    ):
        row.flags.append("outside_history")
        row.notes.append(
            f"it would spend {row.spend:,.0f} a day, over {config.max_spend_multiple:g}x the most "
            f"it has spent ({arm.max_spend:,.0f}); treat this part as a guess"
        )
    if arm.capped and arm.ceiling is not None and row.budget > row.budget_now:
        wanted = arm.spend_slope * row.budget
        unspent = max(0.0, wanted - arm.ceiling.mean) - max(
            0.0, arm.spend_slope * row.budget_now - arm.ceiling.mean
        )
        if unspent > 0.01:
            row.flags.append("capped")
            limit = (
                "demand (search volume or audience)"
                if arm.constraint.kind == "demand"
                else ("its bid target")
            )
            extra = row.budget - row.budget_now
            share = (
                f"all of the extra {extra:,.0f} a day"
                if unspent >= 0.95 * extra
                else f"about {unspent:,.0f} of the extra {extra:,.0f} a day"
            )
            row.notes.append(f"{share} would go unspent: limited by {limit}, not budget")
    if arm.constraint.kind == "learning" and abs(row.budget - row.budget_now) > 0.01:
        row.flags.append("learning")
        row.notes.append("its bid strategy is learning; a budget change now can restart that")
    rules = arm.rules
    step = min(config.max_step, rules.max_step) if rules and rules.max_step else config.max_step
    if row.budget_now > 0 and row.budget > 0 and abs(row.budget / row.budget_now - 1) > step:
        ratio = row.budget / row.budget_now
        per = math.log(1 + step) if ratio > 1 else -math.log(1 - step)
        steps = math.ceil(abs(math.log(ratio)) / per - 1e-9)
        hold = max(config.hold_days, (rules.min_days_between_changes or 0) if rules else 0)
        row.flags.append("step_advice")
        row.notes.append(
            f"a {ratio - 1:+.0%} move is {steps} changes of at most {step:.0%}, about "
            f"{(steps - 1) * hold} days apart in total ({hold} days between changes)"
        )
    multiple = rules.min_budget_cpa_multiple if rules else None
    cpa = row.spend / row.conversions if row.conversions else None
    if multiple and cpa and 0 < row.budget < multiple * cpa:
        row.flags.append("below_cpa_multiple")
        row.notes.append(
            f"below the platform's advice of a budget of at least {multiple:g} conversions "
            f"({multiple * cpa:,.0f} a day at its CPA)"
        )


@dataclass
class WhatIfReport:
    account_alias: str
    currency: str | None
    as_of: date
    description: str
    horizon_days: int
    forecast: BudgetForecast
    best: dict[str, Any] | None = None
    goals: dict[str, Any] | None = None
    against_goals: str | None = None
    incremental_vs_target: str | None = None
    month: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)
    reading: str = ""

    def as_json(self) -> dict[str, Any]:
        f = self.forecast
        h = self.horizon_days
        return {
            "account_alias": self.account_alias,
            "currency": self.currency,
            "as_of": self.as_of.isoformat(),
            "scenario": self.description,
            "baseline": f.baseline.as_json(),
            "forecast": f.scenario.as_json(),
            "difference": {
                "spend": round(f.spend_change, 2),
                "conversions": f.conversions_change.as_json(),
                "incremental_cpa": _r(f.incremental_cpa),
                "incremental_vs_target": self.incremental_vs_target,
            },
            "over_horizon": {
                "days": h,
                "spend": round(f.spend_change * h, 2),
                "conversions": round(f.conversions_change.mean * h, 2),
            },
            "campaigns": [c.as_json() for c in f.campaigns],
            "best_split": self.best,
            "goals": self.goals,
            "against_goals": self.against_goals,
            "month": self.month,
            "notes": [*f.notes, *self.notes],
            "reading": self.reading,
        }


def _money(value: float, currency: str | None) -> str:
    number = f"{value:,.0f}" if abs(value) >= 1000 else f"{value:,.2f}"
    return f"{number} {currency}" if currency else number


def incremental_vs_target(
    incremental_cpa: float | None, spend_change: float, target: float | None, currency: str | None
) -> str | None:
    """What the marginal conversions cost against the target CPA, with the verdict.

    A raise is worse when each extra conversion costs more than the target. A cut is better when
    each conversion it gives up cost more than the target: it drops the expensive ones.
    """
    if incremental_cpa is None or not target or abs(spend_change) < 1e-9:
        return None
    gap = incremental_cpa / target - 1
    where = (
        "on the target"
        if abs(gap) < 0.005
        else f"{abs(gap):.0%} {'above' if gap > 0 else 'below'} the {_money(target, currency)} "
        "target"
    )
    if spend_change > 0:
        judged = "" if abs(gap) < 0.005 else f" ({'worse' if gap > 0 else 'better'})"
        return f"each extra conversion costs about {_money(incremental_cpa, currency)}, {where}{judged}"
    if abs(gap) < 0.005:
        return f"each conversion given up saves about {_money(incremental_cpa, currency)}, {where}"
    dearer = gap > 0
    return (
        f"each conversion given up saves about {_money(incremental_cpa, currency)}, {where}: "
        f"the cut drops conversions that cost {'more' if dearer else 'less'} than the target "
        f"({'better' if dearer else 'worse'})"
    )


def whatif_reading(report: WhatIfReport) -> str:
    f, c = report.forecast, report.currency
    d = f.conversions_change
    parts = [
        f"{report.description}: spend {f.spend_change:+,.2f} a day "
        f"({_money(f.baseline.spend, c)} -> {_money(f.scenario.spend, c)}) and conversions "
        f"{d.mean:+.1f} a day (80% range {d.low:+.1f} to {d.high:+.1f}; "
        f"{f.baseline.conversions.mean:.1f} -> {f.scenario.conversions.mean:.1f})."
    ]
    if f.baseline.cpa and f.scenario.cpa:
        judged = verdict("cpa", f.scenario.cpa.mean / f.baseline.cpa.mean - 1)
        parts.append(
            f"CPA {_money(f.baseline.cpa.mean, c)} -> {_money(f.scenario.cpa.mean, c)}"
            + (f" ({judged})." if judged in ("better", "worse") else ".")
        )
    inc = f.incremental_cpa
    if report.incremental_vs_target:
        parts.append(
            report.incremental_vs_target[0].upper() + report.incremental_vs_target[1:] + "."
        )
    elif inc is not None:
        if f.spend_change > 0:
            parts.append(f"Each extra conversion costs about {_money(inc, c)}.")
        else:
            parts.append(f"Each conversion given up saves about {_money(inc, c)}.")
    if d.low < 0 < d.high:
        parts.append("The range includes no change: the curves cannot tell this apart from today.")
    for row in f.campaigns:
        for note in row.notes:
            if any(flag in row.flags for flag in ("capped", "outside_history", "learning")):
                parts.append(f"{row.entity_ref}: {note}.")
                break
    if report.best and report.best.get("unplaced", 0) > 0.01:
        parts.append(
            f"The curves would place only part of this total: about "
            f"{_money(report.best['unplaced'], c)} a day is beyond what these campaigns have "
            "shown they can spend."
        )
    elif report.best and report.best.get("gain", 0) > 0.05:
        parts.append(
            f"Split by the curves instead, the scenario's total of "
            f"{_money(report.best['total_daily_budget'], c)} a day would give about "
            f"{report.best['gain']:.1f} more conversions a day."
        )
    if report.against_goals:
        parts.append(report.against_goals + ".")
    if report.month and report.month.get("reading"):
        parts.append(report.month["reading"])
    parts.append(
        f"Over {report.horizon_days} days: {f.spend_change * report.horizon_days:+,.0f} spend, "
        f"{d.mean * report.horizon_days:+.1f} conversions."
    )
    return " ".join(parts)


def what_if(
    store: Store,
    fitted: FittedAccount,
    scenario: Scenario,
    *,
    account_alias: str,
    config: BanditConfig,
    goal: Goal | None,
    currency: str | None,
    seed: int = 0,
) -> WhatIfReport:
    """Forecast one scenario for an account whose curves are fitted."""
    if not fitted.eligible:
        raise ValueError(
            f"{account_alias} has no running campaigns with history to forecast; run sync first"
        )
    budgets, description = resolve_budgets(fitted, scenario, config)
    result = forecast(fitted, budgets, seed=seed)
    arms = {a.entity_ref: a for a in fitted.eligible}
    for row in result.campaigns:
        _flag(row, arms[row.entity_ref], config)
    report = WhatIfReport(
        account_alias=account_alias,
        currency=currency or fitted.currency,
        as_of=fitted.as_of,
        description=description,
        horizon_days=scenario.horizon_days,
        forecast=result,
        notes=list(fitted.notes),
    )
    if not fitted.checks.passed:
        report.notes.append("data checks failed; the last good curves were used")
    report.notes.append(
        "ranges cover how uncertain each campaign's spend response is, and how far it has "
        "recently been from actual conversions; not day-to-day noise"
    )
    if scenario.split != "best" or scenario.changes:
        best = best_split(fitted, result.scenario.budget, config)
        best_forecast = forecast(fitted, best, seed=seed)
        gain = best_forecast.scenario.conversions.mean - result.scenario.conversions.mean
        report.best = {
            "total_daily_budget": round(result.scenario.budget, 2),
            "conversions": round(best_forecast.scenario.conversions.mean, 2),
            "gain": round(gain, 2),
            "budgets": {
                a.entity_ref: round(best[a.key], 2)
                for a in fitted.eligible
                if abs(best[a.key] - budgets[a.key]) >= 0.01
            },
            "unplaced": round(max(0.0, result.scenario.budget - sum(best.values())), 2),
            "reading": (
                f"The scenario's total of {_money(result.scenario.budget, report.currency)} a "
                f"day (today's is {_money(result.baseline.budget, report.currency)}), split by "
                f"the curves, would give about {best_forecast.scenario.conversions.mean:.1f} "
                f"conversions a day ({gain:+.1f} against the scenario)."
            ),
            "note": "the scenario's total (total_daily_budget) split by the curves, without "
            "the per-change step limit; "
            "'unplaced' is budget beyond what the campaigns have shown they can spend",
        }
    if goal is not None:
        report.goals = {**goal.values(), "effective_from": goal.effective_from.isoformat()}
        cpa = result.scenario.cpa.mean if result.scenario.cpa else None
        line = goal_line("Expected CPA", cpa, goal.target_cpa, lower_is_better=True)
        report.incremental_vs_target = incremental_vs_target(
            result.incremental_cpa, result.spend_change, goal.target_cpa, report.currency
        )
        for row in result.campaigns:
            row.vs_target = incremental_vs_target(
                row.incremental_cpa, row.spend - row.spend_now, goal.target_cpa, report.currency
            )
        marginal = report.incremental_vs_target
        report.against_goals = "; ".join(x for x in (line, marginal) if x) or None
        pacing = compute_pacing(
            store, account_alias=account_alias, today=fitted.as_of, goal=goal, currency=currency
        )
        if goal.monthly_budget:
            # Pacing already projects every campaign at its run rate (including ones the curves
            # cannot model); the scenario only changes that by its own difference in spend.
            month_end = (
                pacing.projected_spend + result.spend_change * pacing.days_remaining
                if pacing.projected_spend is not None
                else pacing.spend + result.scenario.spend * pacing.days_remaining
            )
            gap = month_end / goal.monthly_budget - 1
            report.month = {
                "spend_to_date": round(pacing.spend, 2),
                "days_remaining": pacing.days_remaining,
                "projected_spend": round(month_end, 2),
                "monthly_budget": goal.monthly_budget,
                "reading": (
                    f"Kept up for the {pacing.days_remaining} days left, the month would end "
                    f"near {_money(month_end, report.currency)} against a "
                    f"{_money(goal.monthly_budget, report.currency)} monthly budget ({gap:+.1%})."
                ),
            }
    report.reading = whatif_reading(report)
    return report


def scenario_config(config: BanditConfig) -> BanditConfig:
    """The config a forecast fits with: the live one, never capped by goals (they are reported)."""
    return replace(config, target_cpa=None, max_cpia=None)


async def what_if_account(
    store: Store,
    accounts: AccountRegistry,
    predictor: Predictor | None,
    alias: str,
    scenario: Scenario,
    *,
    config: BanditConfig,
    now: datetime | None = None,
) -> WhatIfReport:
    """Fit one account's curves on its own local date and forecast the scenario."""
    binding = accounts.resolve(alias)
    if binding is None:
        raise ValueError(f"unknown account alias {alias}; call list_accounts")
    scenario.validate()
    day = account_today(accounts, alias, now)
    config = scenario_config(config)
    fitted = await fit_account(store, predictor, as_of=day, config=config, account_alias=alias)
    return what_if(
        store,
        fitted,
        scenario,
        account_alias=alias,
        config=config,
        goal=GoalStore(store).current(alias, day),
        currency=binding.currency,
    )
