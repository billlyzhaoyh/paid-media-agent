"""Campaigns whose spend stops at a ceiling: the simulator makes them, and the bandit sees them."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from paid_media_agent.bandit.evaluate import run_closed_loop
from paid_media_agent.bandit.recommend import BanditConfig, recommend
from paid_media_agent.sim.scenario import run_scenario
from paid_media_agent.sim.simulator import ScenarioParams, Simulator
from paid_media_agent.store import Store

CAPPED = ScenarioParams(
    scenario_id="capped",
    seed=3,
    n_campaigns=5,
    days=102,
    start=date(2026, 1, 5),
    cold_starts=0,
    shock_rate=0.0,
    ceiling_share=0.5,
    target_share=0.5,
)


def test_ceilings_are_opt_in_and_never_change_the_rest_of_a_scenario() -> None:
    plain = Simulator(replace(CAPPED, ceiling_share=0.0))
    capped = Simulator(CAPPED)
    assert all(c.ceiling is None for c in plain.campaigns)
    limited = [c for c in capped.campaigns if c.ceiling is not None]
    assert limited and {c.limit for c in limited} <= {"demand", "target"}
    for a, b in zip(plain.campaigns, capped.campaigns, strict=True):
        assert (a.kappa1, a.kappa2, a.base_budget) == (b.kappa1, b.kappa2, b.base_budget)
    assert plain.shocks == capped.shocks
    budgets = {c.entity_ref: c.base_budget * 2 for c in capped.campaigns}
    for outcome in capped.step(0, budgets):
        truth = next(c for c in capped.campaigns if c.entity_ref == outcome.entity_ref)
        if truth.ceiling is None:
            assert outcome.limited_by == "budget"
        else:
            assert outcome.spend <= truth.ceiling * 1.4 and outcome.limited_by == truth.limit
            assert truth.value(1e9) == truth.value(truth.ceiling), "no value above the ceiling"


async def test_the_bandit_moves_budget_away_from_campaigns_that_cannot_spend_it() -> None:
    store = Store()
    run_scenario(store, replace(CAPPED, emit_signals=True))
    strategies = dict(
        store.fetch(
            "SELECT entity_ref, any_value(bid_strategy) FROM entity_settings_snapshots GROUP BY 1"
        )
    )
    truths = {c.entity_ref: c for c in Simulator(CAPPED).campaigns}
    for ref, truth in truths.items():
        expected = "TARGET_CPA" if truth.limit == "target" else "MAXIMIZE_CONVERSIONS"
        assert strategies[ref] == expected
    assert store.fetch("SELECT count(*) FROM entity_daily_signals")[0][0] > 0

    run = await recommend(
        store,
        None,
        as_of=date(2026, 4, 17),
        config=BanditConfig(policy="greedy"),
        mode="simulate",
        seed=1,
    )
    decisions = {d.arm.entity_ref: d for d in run.decisions}
    for ref, truth in truths.items():
        d = decisions[ref]
        if truth.ceiling is not None and d.arm.constraint.kind in ("demand", "target"):
            assert d.arm.capped and d.arm.ceiling is not None
            if "hold" in d.constrained_by:
                continue
            can_spend = d.arm.ceiling.high / d.arm.spend_slope
            assert float(d.upper or 0) <= max(can_spend, float(d.lower or 0)) + 0.01
    stored = store.fetch(
        "SELECT kind, count(*) FROM bandit_decision_constraints WHERE run_id = ? GROUP BY 1",
        [run.run_id],
    )
    assert dict(stored).keys() & {"demand", "target"}, "the capped campaigns are named"


@pytest.mark.parametrize("policy", ["greedy"])
async def test_on_capped_accounts_the_ceiling_model_beats_the_linear_one(policy: str) -> None:
    params = replace(CAPPED, days=60)
    linear = await run_closed_loop(
        params, policy, warmup_days=42, days=60, config=BanditConfig(spend_model="linear")
    )
    ceiling = await run_closed_loop(
        params, policy, warmup_days=42, days=60, config=BanditConfig(spend_model="ceiling")
    )
    oracle = await run_closed_loop(params, "oracle", warmup_days=42, days=60)
    regret_linear = oracle.expected_conversions - linear.expected_conversions
    regret_ceiling = oracle.expected_conversions - ceiling.expected_conversions
    assert regret_ceiling < 0.2 * regret_linear
    assert ceiling.unspendable < 0.5 * linear.unspendable
    assert not ceiling.violations
