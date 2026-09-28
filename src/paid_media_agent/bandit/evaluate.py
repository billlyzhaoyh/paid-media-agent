"""How good are the bandit's decisions and curves? Measured on simulated accounts with known truth.

**Closed-loop regret.** Every policy gets the same simulated account: an operator's budget
schedule for `warmup_days`, then the policy sets budgets every `hold_days` for `days` days, with
the store, views, and bounds a live run would use. Campaign-days share their noise across policies
(common random numbers). A policy's score is the expected conversions its realised spend buys
under the true curves; regret is the oracle's score minus its own. The oracle knows the true
curves and pacing and faces the same bounds, so regret measures what was lost to not knowing them.

- `static`: the budgets at the end of the warm-up, never changed.
- `cpa_rule`: a common manual heuristic. Each week, raise by 20% the campaigns whose last two
  weeks' cost per conversion is more than 10% below the account's, cut by 20% those more than 10%
  above, then rescale to the same total.
- `greedy`: the bandit with posterior-mean curves (no exploration).
- `thompson`: the bandit as designed.

**Payout error** (CBS Tables 1 and 2). On a simulated history, at weekly cutoffs, each model
predicts the next two weeks' daily conversions at the spend that actually happened; errors are
reported overall and for cold-start campaigns (under seven days of settled history). Coverage is
the share of actual days inside each model's central 80% predictive interval.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, Literal

import numpy as np

from paid_media_agent.bandit.allocate import allocate
from paid_media_agent.bandit.arms import Arm, load_arms
from paid_media_agent.bandit.posterior import fit_posterior
from paid_media_agent.bandit.prior import Query, global_predict, pseudo_samples
from paid_media_agent.bandit.recommend import BanditConfig, budget_bounds, recommend
from paid_media_agent.predict.protocol import Predictor
from paid_media_agent.sim.scenario import ScenarioDriver, run_scenario
from paid_media_agent.sim.simulator import ScenarioParams, budget_schedule
from paid_media_agent.store.db import Store

LoopPolicy = Literal["oracle", "static", "cpa_rule", "greedy", "thompson"]
POLICIES: tuple[LoopPolicy, ...] = ("oracle", "static", "cpa_rule", "greedy", "thompson")
RULE_STEP = 0.2
RULE_MARGIN = 0.1
Z80 = 1.2816


@dataclass
class LoopResult:
    policy: LoopPolicy
    expected_conversions: float
    spend: float
    decisions: int
    kappa: list[dict[str, Any]] = field(default_factory=list)
    """Per decision and campaign: posterior kappa2 mean and sd next to the true kappa2."""
    violations: list[str] = field(default_factory=list)


def _cpa_rule(
    arms: Sequence[Arm], movable: Sequence[Arm], free: float, config: BanditConfig
) -> dict[str, float]:
    recent = [(a, float(a.spend[-14:].sum()), float(a.conversions[-14:].sum())) for a in arms]
    spent, converted = sum(s for _, s, _ in recent), sum(c for _, _, c in recent)
    account = spent / converted if converted else math.inf
    budgets = {}
    for arm, s, c in recent:
        if arm not in movable:
            continue
        cpa = s / c if c else math.inf
        factor = 1.0
        if cpa < account * (1 - RULE_MARGIN):
            factor = 1 + RULE_STEP
        elif cpa > account * (1 + RULE_MARGIN):
            factor = 1 - RULE_STEP
        budgets[arm.entity_ref] = float(arm.current_budget or 0.0) * factor
    bounds = {a.entity_ref: budget_bounds(a, config)[:2] for a in movable}
    # Rescale to the free total, then clip to the bounds, a few times so both roughly hold.
    for _ in range(20):
        scale = free / max(sum(budgets.values()), 1e-9)
        budgets = {
            ref: min(max(b * scale, bounds[ref][0]), bounds[ref][1]) for ref, b in budgets.items()
        }
        if abs(sum(budgets.values()) - free) < 1e-6:
            break
    if sum(budgets.values()) > free:
        excess = sum(budgets.values()) / free
        budgets = {ref: max(bounds[ref][0], b / excess) for ref, b in budgets.items()}
    return budgets


async def run_closed_loop(
    params: ScenarioParams,
    policy: LoopPolicy,
    *,
    warmup_days: int,
    days: int,
    config: BanditConfig | None = None,
    predictor: Predictor | None = None,
    store: Store | None = None,
) -> LoopResult:
    """Run one policy over a simulated account and score it against the truth."""
    config = config or BanditConfig()
    params = replace(params, days=warmup_days + days)
    store = store if store is not None else Store()
    driver = ScenarioDriver(store, params)
    sim = driver.sim
    truth = {c.entity_ref: c for c in sim.campaigns}
    schedule = budget_schedule(sim)
    budgets = {ref: values[0] for ref, values in schedule.items()}
    controlled: set[str] = set()
    result = LoopResult(policy=policy, expected_conversions=0.0, spend=0.0, decisions=0)
    for index in range(params.days):
        for campaign in sim.active(index):
            if campaign.entity_ref not in controlled:
                budgets[campaign.entity_ref] = schedule[campaign.entity_ref][index]
        if index >= warmup_days and (index - warmup_days) % max(config.hold_days, 1) == 0:
            as_of = sim.day(index)
            changes = await _decide(
                store, predictor, policy, as_of, config, truth, params, result, index
            )
            budgets.update(changes)
            controlled |= {c.entity_ref for c in sim.active(index)}
            result.decisions += 1
        outcomes = driver.run_day(index, budgets)
        if index >= warmup_days:
            result.expected_conversions += sum(o.expected_conversions for o in outcomes)
            result.spend += sum(o.spend for o in outcomes)
    return result


async def _decide(
    store: Store,
    predictor: Predictor | None,
    policy: LoopPolicy,
    as_of: date,
    config: BanditConfig,
    truth: dict[str, Any],
    params: ScenarioParams,
    result: LoopResult,
    index: int,
) -> dict[str, float]:
    if policy == "static":
        return {}
    if policy in ("greedy", "thompson"):
        run = await recommend(
            store,
            predictor,
            as_of=as_of,
            config=replace(config, policy=policy),
            mode="simulate",
            scenario_id=params.scenario_id,
            seed=params.seed * 10_000 + index,
        )
        _check(run.decisions, run.total_budget, result, as_of, config.max_step)
        for d in run.decisions:
            if d.posterior is not None and d.arm.eligible:
                result.kappa.append(
                    {
                        "decision": result.decisions,
                        "entity_ref": d.arm.entity_ref,
                        "kappa2": float(d.posterior.kappa_mean[1]),
                        "sd": d.posterior.kappa2_sd,
                        "true": truth[d.arm.entity_ref].kappa2,
                    }
                )
        return {d.arm.entity_ref: float(d.final_budget) for d in run.decisions if d.final_budget}
    arms = [a for a in load_arms(store, as_of=as_of, train_days=config.train_days) if a.eligible]
    bounds = {a.entity_ref: budget_bounds(a, config) for a in arms}
    movable = [a for a in arms if bounds[a.entity_ref][0] < bounds[a.entity_ref][1]]
    fixed = sum(float(a.current_budget or 0.0) for a in arms if a not in movable)
    total = sum(float(a.current_budget or 0.0) for a in arms)
    free = total - fixed
    if not movable:
        return {}
    if policy == "cpa_rule":
        return _cpa_rule(arms, movable, free, config)
    allocation = allocate(
        [truth[a.entity_ref] for a in movable],
        [params.pacing_mean] * len(movable),
        [bounds[a.entity_ref][0] for a in movable],
        [bounds[a.entity_ref][1] for a in movable],
        free,
    )
    return {a.entity_ref: b for a, b in zip(movable, allocation.budgets, strict=True)}


def _check(
    decisions: Sequence[Any], total: float, result: LoopResult, as_of: date, max_step: float
) -> None:
    """Record any recommendation outside its bounds, its step limit, or the total."""
    allocated = 0.0
    for d in decisions:
        if not d.arm.eligible or d.final_budget is None:
            continue
        allocated += d.final_budget
        current = float(d.arm.current_budget or 0.0)
        if d.lower is not None and d.final_budget < d.lower - 1e-6:
            result.violations.append(f"{as_of} {d.arm.entity_ref} below its lower bound")
        if d.upper is not None and d.final_budget > d.upper + 1e-6:
            result.violations.append(f"{as_of} {d.arm.entity_ref} above its upper bound")
        if current and abs(d.final_budget / current - 1) > max_step + 1e-6:
            result.violations.append(f"{as_of} {d.arm.entity_ref} moved more than the step limit")
        if "hold" in d.constrained_by and abs(d.final_budget - current) > 1e-6:
            result.violations.append(f"{as_of} {d.arm.entity_ref} moved during its hold period")
    if allocated > total + 1e-6:
        result.violations.append(f"{as_of} allocated {allocated:.2f} of a {total:.2f} total")


def kappa_contraction(result: LoopResult) -> dict[str, float]:
    """Mean |kappa2 error| and posterior sd at the first and last decision, over campaigns."""
    by_decision: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in result.kappa:
        by_decision[row["decision"]].append(row)
    if not by_decision:
        return {}
    first, last = by_decision[min(by_decision)], by_decision[max(by_decision)]
    common = {r["entity_ref"] for r in first} & {r["entity_ref"] for r in last}

    def summary(rows: list[dict[str, Any]], key: str) -> float:
        picked = [r for r in rows if r["entity_ref"] in common]
        if key == "error":
            return float(np.mean([abs(r["kappa2"] - r["true"]) for r in picked]))
        return float(np.mean([r["sd"] for r in picked]))

    return {
        "error_first": summary(first, "error"),
        "error_last": summary(last, "error"),
        "sd_first": summary(first, "sd"),
        "sd_last": summary(last, "sd"),
    }


async def compare_policies(
    params: ScenarioParams,
    *,
    warmup_days: int,
    days: int,
    config: BanditConfig | None = None,
    predictor: Predictor | None = None,
    policies: Sequence[LoopPolicy] = POLICIES,
) -> dict[str, LoopResult]:
    return {
        policy: await run_closed_loop(
            params,
            policy,
            warmup_days=warmup_days,
            days=days,
            config=config,
            predictor=predictor if policy in ("greedy", "thompson") else None,
        )
        for policy in policies
    }


@dataclass
class _Errors:
    predicted: list[float] = field(default_factory=list)
    actual: list[float] = field(default_factory=list)
    inside: list[bool] = field(default_factory=list)

    def summary(self) -> dict[str, float | int | None]:
        if not self.actual:
            return {"n": 0, "bias": None, "mae": None, "rmse": None, "coverage80": None}
        error = np.asarray(self.predicted) - np.asarray(self.actual)
        return {
            "n": len(error),
            "bias": round(float(error.mean()), 3),
            "mae": round(float(np.abs(error).mean()), 3),
            "rmse": round(float(np.sqrt((error**2).mean())), 3),
            "coverage80": round(float(np.mean(self.inside)), 3) if self.inside else None,
        }


async def payout_error(
    params: ScenarioParams,
    *,
    config: BanditConfig | None = None,
    predictor: Predictor | None = None,
    horizon: int = 14,
    first_cutoff: int | None = None,
    every: int = 7,
) -> dict[str, dict[str, dict[str, float | int | None]]]:
    """Forecast error of the local-only, global-only, and combined (CBS) models."""
    config = config or BanditConfig()
    store = Store()
    run = run_scenario(store, params)
    truth: dict[tuple[str, date], tuple[float, int]] = {
        (ref, day): (spend, conversions)
        for ref, day, spend, conversions in store.fetch(
            "SELECT entity_ref, day, spend, conversions FROM sim_truth"
        )
    }
    if first_cutoff is None:
        # Eight weeks of history when there is room, and never less than four.
        first_cutoff = max(28, min(56, params.days - horizon - 1))
    cutoffs = set(range(first_cutoff, params.days - horizon, every))
    # Cold starts: a cutoff while each new campaign has only a few settled days.
    for campaign in run.campaigns:
        if campaign.start_index:
            cutoffs |= {campaign.start_index + 7, campaign.start_index + 10}
    groups = {
        name: {"all": _Errors(), "cold_start": _Errors()} for name in ("local", "global", "cbs")
    }
    for cutoff in sorted(c for c in cutoffs if c < params.days - horizon):
        as_of = params.start + timedelta(days=cutoff)
        arms = [
            a for a in load_arms(store, as_of=as_of, train_days=config.train_days) if a.eligible
        ]
        if not arms:
            continue
        future = [
            (i, as_of + timedelta(days=h))
            for i, arm in enumerate(arms)
            for h in range(horizon)
            if (arm.entity_ref, as_of + timedelta(days=h)) in truth
        ]
        queries = [
            Query(i, truth[(arms[i].entity_ref, day)][0], day.weekday()) for i, day in future
        ]
        predicted_global = await global_predict(
            arms,
            queries,
            as_of=as_of,
            predictor=predictor,
            half_life_days=config.prior_half_life_days,
        )
        pseudo = await pseudo_samples(
            arms,
            as_of=as_of,
            k=config.pseudo_samples,
            predictor=predictor,
            half_life_days=config.prior_half_life_days,
        )
        fits = {}
        for i, arm in enumerate(arms):
            fits[("local", i)] = fit_posterior(
                arm.spend,
                arm.conversions,
                arm.weekdays,
                arm.unit,
                prior_kappa2=config.prior_kappa2,
                ladder_z=config.ladder_z,
            )
            fits[("cbs", i)] = fit_posterior(
                arm.spend,
                arm.conversions,
                arm.weekdays,
                arm.unit,
                pseudo_spend=pseudo.spend.get(i),
                pseudo_target=pseudo.target.get(i),
                pseudo_weight=pseudo.weight,
                prior_kappa2=config.prior_kappa2,
                ladder_z=config.ladder_z,
            )
        for j, (i, day) in enumerate(future):
            spend, actual = truth[(arms[i].entity_ref, day)]
            group = "cold_start" if arms[i].cold_start else "all"
            for name in ("local", "cbs"):
                centre, scale = fits[(name, i)].predictive(
                    np.asarray([spend]), np.asarray([day.weekday()])
                )
                low = math.exp(centre[0] - Z80 * scale[0]) - 1
                high = math.exp(centre[0] + Z80 * scale[0]) - 1
                for target in {group, "all"}:
                    errors = groups[name][target]
                    errors.predicted.append(
                        math.exp(centre[0] + fits[(name, i)].noise_variance / 2) - 1
                    )
                    errors.actual.append(actual)
                    errors.inside.append(low <= actual <= high)
            value = predicted_global.values[j]
            if math.isfinite(value):
                for target in {group, "all"}:
                    groups["global"][target].predicted.append(math.exp(value) - 1)
                    groups["global"][target].actual.append(actual)
    return {
        name: {group: errors.summary() for group, errors in by_group.items()}
        for name, by_group in groups.items()
    }
