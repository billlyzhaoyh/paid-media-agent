"""One budget decision: read history, fit every campaign's curve, draw, allocate, and log it.

The steps follow CBS's Algorithm 1 (Han & Arndt, KDD 2021):

1. Load each campaign's lag-corrected history as it stood on the decision day, and check it.
2. Ask the global model for pseudo-samples near each campaign's recent spend.
3. Fit each campaign's local model to its history plus pseudo-samples.
4. Draw one curve per campaign (Thompson sampling) and one at the posterior mean (greedy).
5. Split the total budget across the drawn curves within each campaign's bounds.

Bounds keep every change reviewable: at most `max_step` up or down per decision, no more than
`max_spend_multiple` times the highest spend seen, and nothing for a campaign whose budget changed
within `hold_days` (its last change has not been measured yet). When the data checks fail, the
last good posteriors are reused and the run says so (`fallback_used`). The result is a
recommendation; applying it goes through the normal proposal and approval flow.
"""

from __future__ import annotations

import json
import math
import secrets
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Literal

import numpy as np

from paid_media_agent.bandit.allocate import Allocation, allocate
from paid_media_agent.bandit.arms import Arm, DataChecks
from paid_media_agent.bandit.fit import fit_account
from paid_media_agent.bandit.policy import GUARD_Z, draw_thompson, greedy
from paid_media_agent.bandit.posterior import (
    LADDER_Z,
    PRIOR_KAPPA2,
    Posterior,
    PowerCurve,
)
from paid_media_agent.predict.protocol import Predictor
from paid_media_agent.store.db import Store, utc_now

Policy = Literal["thompson", "greedy"]
Mode = Literal["recommend", "simulate", "backtest"]
POLICY_VERSION = "cbs-v1"
OBJECTIVE = "max_conversions"
PROPENSITY_BAND = 0.05
"""A redraw counts toward the propensity when its budget is within 5% of the chosen one."""
TARGET_TOLERANCE = 0.005
"""A recommended split within 0.5% of the target CPA counts as meeting it."""


@dataclass(frozen=True)
class BanditConfig:
    policy: Policy = "thompson"
    train_days: int = 90
    pseudo_samples: int = 512
    """Per campaign; CBS tuned 128 to 512. More pulls each curve toward the global model."""
    prior_half_life_days: float = 28.0
    max_step: float = 0.25
    max_spend_multiple: float = 1.5
    hold_days: int = 7
    min_budget: float = 1.0
    max_budget: float | None = None
    max_cpia: float | None = None
    """Stop a campaign where its next conversion would cost more than this (currency)."""
    target_cpa: float | None = None
    """The account's target average CPA (currency). When the model expects the split to cost more
    per conversion than this, the total is cut until it does not."""
    spend_model: Literal["ceiling", "linear"] = "ceiling"
    """`ceiling`: spend = min(budget share, what the campaign can win) where demand or a bid target
    limits it. `linear`: spend always grows with budget (the model before, kept for comparison)."""
    prior_kappa2: float = PRIOR_KAPPA2
    ladder_z: float = LADDER_Z
    guard_z: float = GUARD_Z
    propensity_draws: int = 200

    def __post_init__(self) -> None:
        if not 0 < self.max_step < 1:
            raise ValueError("max_step must be between 0 and 1")
        if self.max_spend_multiple < 1:
            raise ValueError("max_spend_multiple must be at least 1")
        if self.pseudo_samples < 0 or self.train_days < 14 or self.hold_days < 0:
            raise ValueError("pseudo_samples >= 0, train_days >= 14, and hold_days >= 0")


@dataclass
class ArmDecision:
    arm: Arm
    lower: float | None = None
    upper: float | None = None
    posterior: Posterior | None = None
    sampled: tuple[float, float] | None = None
    rejected_draws: int | None = None
    budget_thompson: float | None = None
    budget_greedy: float | None = None
    final_budget: float | None = None
    constrained_by: list[str] = field(default_factory=list)
    expected_conversions: float | None = None
    """At the final budget, from the posterior-mean curve (conversions a day)."""
    expected_conversions_now: float | None = None
    """At the current budget, from the same curve, so the gain is computed rather than guessed."""
    propensity: float | None = None

    def as_json(self) -> dict[str, Any]:
        arm, post = self.arm, self.posterior
        return {
            "account_alias": arm.account_alias,
            "entity_ref": arm.entity_ref,
            "entity_name": arm.entity_name,
            "eligible": arm.eligible,
            "reason": arm.reason,
            "current_budget": arm.current_budget,
            "final_budget": _round(self.final_budget),
            "change": None
            if not arm.current_budget or self.final_budget is None
            else round(self.final_budget / arm.current_budget - 1, 4),
            "bounds": [_round(self.lower), _round(self.upper)],
            "constrained_by": self.constrained_by,
            "pacing_ratio": round(arm.pacing, 3),
            "cost_per_conversion": _round(arm.unit),
            "history_days": arm.n_history,
            "cold_start": arm.cold_start,
            "kappa2": None if post is None else round(float(post.kappa_mean[1]), 3),
            "kappa2_sd": None if post is None else round(post.kappa2_sd, 3),
            "expected_conversions": _round(self.expected_conversions),
            "expected_conversions_now": _round(self.expected_conversions_now),
            "propensity": _round(self.propensity),
            "constraint": constraint_record(arm),
        }


def constraint_record(arm: Arm) -> dict[str, Any]:
    """What limits the campaign's spend, how sure, the evidence, and its spend ceiling."""
    return {
        **arm.constraint.as_record(),
        "ceiling": arm.ceiling.as_record() if arm.capped and arm.ceiling else None,
        "utilisation": None if arm.utilisation is None else round(arm.utilisation, 3),
        "bid_strategy": arm.bid_strategy,
        "channel_type": arm.signals.channel_type,
        "platform_notes": list(arm.rules.notes) if arm.rules else [],
    }


def _round(value: float | None) -> float | None:
    return None if value is None or not math.isfinite(value) else round(value, 2)


@dataclass
class BanditRun:
    run_id: uuid.UUID
    decision_day: date
    policy: Policy
    total_budget: float
    currency: str | None
    prior_source: str
    data_checks: DataChecks
    fallback_used: bool
    seed: int
    decisions: list[ArmDecision]
    notes: list[str] = field(default_factory=list)
    total_source: str = "current"
    """Where the total came from: `current` budgets, an `explicit` total, or the `monthly_budget`."""
    expected_cpa: float | None = None
    """The model's expected average CPA of the recommended split, over campaigns with a curve."""
    capped_by_target_cpa: bool = False
    target_cpa_reached: bool | None = None
    """With a target CPA: whether the expected CPA is at or below it (None without a target)."""
    pseudo: dict[str, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)
    """Arm key to the global model's pseudo-samples (spend, log(conversions + 1)); for charts,
    never stored."""

    @property
    def budgets(self) -> dict[str, float]:
        """Entity ref to the recommended daily budget, for every campaign with a budget."""
        return {
            d.arm.entity_ref: float(d.final_budget)
            for d in self.decisions
            if d.final_budget is not None
        }

    def as_json(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "decision_day": self.decision_day.isoformat(),
            "policy": self.policy,
            "objective": OBJECTIVE,
            "total_budget": round(self.total_budget, 2),
            "total_source": self.total_source,
            "expected_cpa": _round(self.expected_cpa),
            "capped_by_target_cpa": self.capped_by_target_cpa,
            "target_cpa_reached": self.target_cpa_reached,
            "currency": self.currency,
            "prior_source": self.prior_source,
            "data_checks": self.data_checks.results,
            "fallback_used": self.fallback_used,
            "decisions": [d.as_json() for d in self.decisions],
            "notes": self.notes,
        }


def budget_bounds(arm: Arm, config: BanditConfig) -> tuple[float, float, list[str]]:
    """The budgets this decision may choose from for one eligible campaign.

    The platform's own rules tighten the step, the spacing between changes, and the minimum
    budget. A campaign in a learning phase is held. Where demand or a bid target limits spend,
    the budget stops at what the campaign can spend, so budget it cannot use moves elsewhere.
    """
    rules = arm.rules
    current = float(arm.current_budget or 0.0)
    if arm.constraint.kind == "learning":
        return current, current, ["learning"]
    hold = max(config.hold_days, (rules.min_days_between_changes or 0) if rules else 0)
    if arm.days_since_change is not None and arm.days_since_change < hold:
        return current, current, ["hold"]
    step = min(config.max_step, rules.max_step) if rules and rules.max_step else config.max_step
    floor = max(config.min_budget, (rules.min_daily_budget or 0.0) if rules else 0.0)
    if floor > current * (1 + step):
        # The platform's minimum is beyond one step: a decision may not jump there on its own.
        return current, current, ["platform_minimum"]
    lower = max(floor, current * (1 - step))
    upper = current * (1 + step)
    why: list[str] = ["platform_step"] if step < config.max_step else []
    ceiling = config.max_spend_multiple * arm.max_spend / arm.spend_slope
    if ceiling < upper:
        upper, why = ceiling, ["spend_history"]
    if arm.capped and arm.ceiling is not None:
        can_spend = arm.ceiling.high / arm.spend_slope
        if can_spend < upper:
            upper, why = can_spend, [f"{arm.constraint.kind}_ceiling"]
    if config.max_budget is not None and config.max_budget < upper:
        # A cap below the lower bound moves the budget one step toward it, never past the step
        # limit or below the platform's minimum in one decision.
        upper, why = max(config.max_budget, lower), ["max_budget"]
    return lower, max(upper, lower), why


def _usable(decision: ArmDecision) -> tuple[Posterior, tuple[float, float]]:
    """A movable campaign's posterior and its valid mean curve (checked when it was made movable)."""
    post = decision.posterior
    mean = greedy(post) if post is not None else None
    if post is None or mean is None:
        raise RuntimeError(f"{decision.arm.entity_ref} has no valid curve to allocate with")
    return post, mean


def _allocate_curves(
    decisions: list[ArmDecision],
    curves: list[PowerCurve],
    total: float,
    config: BanditConfig,
) -> list[float]:
    return list(_allocation(decisions, curves, total, config).budgets)


def _allocation(
    decisions: list[ArmDecision],
    curves: list[PowerCurve],
    total: float,
    config: BanditConfig,
) -> Allocation:
    return allocate(
        curves,
        [d.arm.spend_slope for d in decisions],
        [float(d.lower or 0.0) for d in decisions],
        [float(d.upper or 0.0) for d in decisions],
        total,
        max_cpia=[config.max_cpia] * len(decisions),
    )


def _cap_to_target_cpa(
    movable: list[ArmDecision],
    curves: list[PowerCurve],
    fixed: list[tuple[ArmDecision, float]],
    free: float,
    config: BanditConfig,
    run: BanditRun,
) -> tuple[float, bool]:
    """The largest free budget whose expected account CPA is at or below the target.

    With concave curves each extra unit of budget buys fewer conversions, so the expected CPA of
    the best split rises with the total: bisection finds the cap. Returns (free, cut).
    """
    target = float(config.target_cpa or 0.0)
    run.target_cpa_reached = True

    def cpa(amount: float) -> float:
        budgets = _allocate_curves(movable, curves, amount, config)
        value = _expected_cpa([*zip(movable, budgets, strict=True), *fixed])
        return math.inf if value is None else value

    before = cpa(free)
    if before <= target:
        return free, False
    floor = sum(float(d.lower or 0.0) for d in movable)
    if floor >= free or cpa(floor) > target:
        run.notes.append(
            f"expected CPA {before:,.2f} is above the {target:,.2f} target even with every "
            "movable campaign at its step limit; budgets are at their lower bounds"
        )
        cut = min(free, floor)
        run.target_cpa_reached = False
    else:
        low, high = floor, free
        for _ in range(60):
            mid = (low + high) / 2
            if cpa(mid) <= target:
                low = mid
            else:
                high = mid
        cut = low
        run.notes.append(
            f"expected CPA {before:,.2f} was above the {target:,.2f} target; the total was cut by "
            f"{free - cut:,.2f} so the expected CPA lands at the target"
        )
    run.total_budget -= free - cut
    run.capped_by_target_cpa = True
    return cut, True


def _expected_cpa(pairs: list[tuple[ArmDecision, float]]) -> float | None:
    """Expected spend over expected conversions, with each campaign's mean curve."""
    spend = conversions = 0.0
    for decision, budget in pairs:
        post = decision.posterior
        mean = greedy(post) if post is not None else None
        if post is None or mean is None:
            continue
        s = decision.arm.expected_spend(budget)
        spend += s
        conversions += post.curve(mean).value(s)
    return spend / conversions if conversions > 0 else None


async def recommend(
    store: Store,
    predictor: Predictor | None,
    *,
    as_of: date,
    config: BanditConfig | None = None,
    total_budget: float | None = None,
    account_alias: str | None = None,
    mode: Mode = "recommend",
    scenario_id: str | None = None,
    seed: int | None = None,
    record: bool = True,
    clock: Callable[[], datetime] = utc_now,
    budget_scale: float | None = None,
) -> BanditRun:
    """Recommend tomorrow's budgets for one account's campaigns; nothing is changed."""
    config = config or BanditConfig()
    seed = secrets.randbits(63) if seed is None else seed
    rng = np.random.default_rng(seed)
    fitted = await fit_account(
        store, predictor, as_of=as_of, config=config, account_alias=account_alias
    )
    eligible = fitted.eligible
    currencies = {a.currency for a in eligible if a.currency}
    run = BanditRun(
        run_id=uuid.uuid4(),
        decision_day=as_of,
        policy=config.policy,
        total_budget=0.0,
        currency=next(iter(currencies), None),
        prior_source=fitted.prior_source,
        data_checks=fitted.checks,
        fallback_used=not fitted.checks.passed,
        seed=seed,
        decisions=[ArmDecision(arm=a, posterior=fitted.posteriors.get(a.key)) for a in fitted.arms],
        notes=list(fitted.notes),
        pseudo=dict(fitted.pseudo),
    )
    decisions = [d for d in run.decisions if d.arm.eligible]
    for decision in run.decisions:
        if not decision.arm.eligible:
            decision.final_budget = decision.arm.current_budget
            decision.constrained_by = ["ineligible"]
    if not decisions:
        run.notes.append("no campaign can be allocated: none is enabled with spend and history")
        return _finish(store, run, config, mode, scenario_id, account_alias, record, clock)

    # Bounds, and campaigns that stay where they are.
    fixed_total = 0.0
    movable: list[ArmDecision] = []
    for decision in decisions:
        lower, upper, why = budget_bounds(decision.arm, config)
        decision.lower, decision.upper, decision.constrained_by = lower, upper, why
        post = decision.posterior
        if post is None or greedy(post) is None:
            decision.lower = decision.upper = decision.arm.current_budget
            decision.constrained_by = ["no_valid_curve"]
        if decision.lower == decision.upper:
            decision.final_budget = decision.lower
            fixed_total += float(decision.lower or 0.0)
        else:
            movable.append(decision)
    current_total = sum(float(d.arm.current_budget or 0.0) for d in decisions)
    if total_budget is not None:
        run.total_budget, run.total_source = float(total_budget), "explicit"
    elif budget_scale is not None and current_total > 0:
        run.total_budget, run.total_source = current_total * budget_scale, "monthly_budget"
        run.notes.append(
            f"the total follows the monthly budget: current budgets x {budget_scale:.2f} "
            "(still within each campaign's step limits)"
        )
    else:
        run.total_budget = current_total
    free = run.total_budget - fixed_total
    moving = {id(d) for d in movable}
    fixed_with_curves = [
        (d, float(d.final_budget)) for d in decisions if id(d) not in moving and d.final_budget
    ]

    # 4-5. Draw and allocate.
    if movable:
        greedy_curves = [
            d.arm.decision_curve(post.curve(mean))
            for d, (post, mean) in zip(movable, map(_usable, movable), strict=True)
        ]
        by_greedy = _allocate_curves(movable, greedy_curves, free, config)
        if config.target_cpa is not None:
            free, cut = _cap_to_target_cpa(
                movable, greedy_curves, fixed_with_curves, free, config, run
            )
            if cut:
                by_greedy = _allocate_curves(movable, greedy_curves, free, config)
        sampled_curves = []
        for decision in movable:
            post, _ = _usable(decision)
            decision.sampled, decision.rejected_draws = draw_thompson(
                post, rng, guard_z=config.guard_z
            )
            sampled_curves.append(decision.arm.decision_curve(post.curve(decision.sampled)))
        by_thompson_run = _allocation(movable, sampled_curves, free, config)
        by_thompson = list(by_thompson_run.budgets)
        chosen = by_thompson if config.policy == "thompson" else by_greedy
        binding = (
            by_thompson_run.binding
            if config.policy == "thompson"
            else _allocation(movable, greedy_curves, free, config).binding
        )
        for decision, g, t, c, stops in zip(
            movable, by_greedy, by_thompson, chosen, binding, strict=True
        ):
            decision.budget_greedy, decision.budget_thompson, decision.final_budget = g, t, c
            if c <= float(decision.lower or 0.0) + 1e-6:
                decision.constrained_by.append("lower")
            elif c >= float(decision.upper or 0.0) - 1e-6 and not decision.constrained_by:
                # The step limit, unless another bound (history, a ceiling) set the upper end.
                decision.constrained_by.append("upper")
            elif "cpia" in stops:
                decision.constrained_by.append("cpia")
            if run.capped_by_target_cpa and c < float(decision.arm.current_budget or 0):
                decision.constrained_by.append("target_cpa")
        if config.policy == "thompson" and config.propensity_draws > 0:
            hits = np.zeros(len(movable))
            for _ in range(config.propensity_draws):
                redraw = [
                    d.arm.decision_curve(
                        post.curve(draw_thompson(post, rng, guard_z=config.guard_z)[0])
                    )
                    for d, (post, _) in zip(movable, map(_usable, movable), strict=True)
                ]
                budgets = _allocate_curves(movable, redraw, free, config)
                chosen_budgets = np.asarray([float(d.final_budget or 0.0) for d in movable])
                hits += np.abs(np.asarray(budgets) / chosen_budgets - 1) <= PROPENSITY_BAND
            for decision, hit in zip(movable, hits, strict=True):
                decision.propensity = float(hit / config.propensity_draws)
        elif config.policy == "greedy":
            for decision in movable:
                decision.propensity = 1.0
    for decision in decisions:
        post = decision.posterior
        centre = greedy(post) if post is not None else None
        if post is not None and centre is not None and decision.final_budget is not None:
            curve = post.curve(centre)
            decision.expected_conversions = curve.value(
                decision.arm.expected_spend(decision.final_budget)
            )
            if decision.arm.current_budget:
                decision.expected_conversions_now = curve.value(
                    decision.arm.expected_spend(decision.arm.current_budget)
                )
    run.expected_cpa = _expected_cpa(
        [(d, float(d.final_budget)) for d in decisions if d.final_budget is not None]
    )
    if config.target_cpa is not None and run.expected_cpa is not None:
        # Judge the target on the budgets recommended, not on the mean-curve split used to cut:
        # an exploring (Thompson) split can land a little above it.
        target = float(config.target_cpa)
        unreachable = run.target_cpa_reached is False  # already noted by the cap
        run.target_cpa_reached = run.expected_cpa <= target * (1 + TARGET_TOLERANCE)
        if not run.target_cpa_reached and config.policy != "greedy" and not unreachable:
            run.notes.append(
                f"the recommended budgets' expected CPA is {run.expected_cpa:,.2f}, "
                f"{run.expected_cpa / target - 1:+.1%} against the {target:,.2f} target "
                "(this split explores, so it can sit a little above the mean-curve cut)"
            )
    freed = sum(
        max(0.0, float(d.arm.current_budget or 0.0) - float(d.final_budget or 0.0))
        for d in decisions
        if any(c.endswith("_ceiling") for c in d.constrained_by)
    )
    if freed >= 0.01:
        run.notes.append(
            f"{freed:,.2f} a day moved away from campaigns that cannot spend more of their budget "
            "(limited by demand or a bid target, not budget)"
        )
    if free < sum(float(d.lower or 0.0) for d in movable) - 1e-6:
        run.notes.append("the total is below the campaigns' minimum budgets; all are at minimum")
    return _finish(store, run, config, mode, scenario_id, account_alias, record, clock)


def _insert(table: str, columns: int) -> str:
    return f"INSERT INTO {table} VALUES ({', '.join(['?'] * columns)})"  # noqa: S608 - constants


def _finish(
    store: Store,
    run: BanditRun,
    config: BanditConfig,
    mode: Mode,
    scenario_id: str | None,
    account_alias: str | None,
    record: bool,
    clock: Callable[[], datetime],
) -> BanditRun:
    if not record:
        return run
    at = clock()
    with store.transaction() as cursor:
        cursor.execute(
            _insert("bandit_runs", 17),
            [
                run.run_id,
                mode,
                scenario_id,
                account_alias,
                run.decision_day,
                run.policy,
                POLICY_VERSION,
                OBJECTIVE,
                run.total_budget,
                run.currency,
                run.prior_source,
                json.dumps(asdict(config)),
                run.seed,
                json.dumps(run.data_checks.results),
                run.fallback_used,
                json.dumps(run.notes),
                at,
            ],
        )
        for d in run.decisions:
            arm, post = d.arm, d.posterior
            stored = post.as_record() if post is not None else None
            cursor.execute(
                _insert("bandit_decisions", 29),
                [
                    run.run_id,
                    arm.key,
                    arm.platform,
                    arm.provider_account_id,
                    arm.account_alias,
                    arm.entity_ref,
                    arm.entity_name,
                    arm.eligible,
                    arm.reason,
                    arm.current_budget,
                    arm.pacing,
                    arm.unit if math.isfinite(arm.unit) else None,
                    arm.n_history,
                    post.n_pseudo if post is not None else 0,
                    post.precision_kappa2 if post is not None else None,
                    json.dumps(stored["post_mean"]) if stored else None,
                    json.dumps(stored["post_cov"]) if stored else None,
                    d.sampled[0] if d.sampled else None,
                    d.sampled[1] if d.sampled else None,
                    d.rejected_draws,
                    d.budget_thompson,
                    d.budget_greedy,
                    d.final_budget,
                    d.lower,
                    d.upper,
                    d.constrained_by,
                    d.expected_conversions,
                    d.propensity,
                    None,
                ],
            )
            cursor.execute(
                _insert("bandit_decision_constraints", 6),
                [
                    run.run_id,
                    arm.key,
                    arm.constraint.kind,
                    arm.constraint.confidence,
                    arm.ceiling.high if arm.capped and arm.ceiling else None,
                    json.dumps(constraint_record(arm)),
                ],
            )
    return run
