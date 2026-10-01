"""What-if budget forecasts: scenarios resolve as asked, and forecasts match simulated truth."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from paid_media_agent.analytics.goals import GoalStore
from paid_media_agent.bandit.evaluate import whatif_calibration
from paid_media_agent.bandit.fit import FittedAccount, fit_account
from paid_media_agent.bandit.recommend import BanditConfig
from paid_media_agent.bandit.whatif import (
    BudgetChange,
    Scenario,
    forecast,
    resolve_budgets,
    what_if,
)
from paid_media_agent.sim.scenario import run_scenario
from paid_media_agent.sim.simulator import ScenarioParams
from paid_media_agent.store import Store

DEFAULT = ScenarioParams(scenario_id="w", n_campaigns=6, days=120, start=date(2026, 1, 5))
CONSTRAINED = replace(
    DEFAULT, scenario_id="wc", ceiling_share=0.5, target_share=0.5, emit_signals=True
)
CUTOFF = 90


@pytest.mark.parametrize(
    ("params", "seed"),
    [(DEFAULT, 1), (DEFAULT, 2), (DEFAULT, 3), (CONSTRAINED, 1), (CONSTRAINED, 2)],
)
async def test_forecasts_match_the_simulated_truth(params: ScenarioParams, seed: int) -> None:
    # Measured on 2026-09-29: account MAE 1.2-5.3%, direction 100%, spend MAE <= 1.6%.
    cal = await whatif_calibration(replace(params, seed=seed), cutoff=CUTOFF, seed=seed)
    summary = cal.summary()
    assert summary["cases"] >= 14
    assert summary["mae"] < 0.10
    assert summary["change_mae"] < 0.05, "changes are right to within 5% of today's conversions"
    assert summary["direction"] == 1.0
    assert summary["spend_mae"] < 0.05


async def test_budget_a_capped_campaign_cannot_spend_buys_nothing() -> None:
    cal = await whatif_calibration(replace(CONSTRAINED, seed=2), cutoff=CUTOFF, seed=2)
    capped = next(c for c in cal.cases if c.label == "capped +50%")
    assert abs(capped.predicted_change) < 0.02 * capped.predicted_base
    assert abs(capped.true_change) < 0.02 * capped.true_base


@pytest.fixture(scope="module")
async def account() -> tuple[Store, FittedAccount]:
    store = Store()
    run_scenario(store, replace(CONSTRAINED, seed=2, days=CUTOFF))
    fitted = await fit_account(
        store, None, as_of=DEFAULT.start + timedelta(days=CUTOFF), config=BanditConfig()
    )
    return store, fitted


async def test_shared_draws_give_an_honest_interval_on_the_difference(
    account: tuple[Store, FittedAccount],
) -> None:
    _, fitted = account
    budgets = {a.key: float(a.current_budget or 0) * 1.2 for a in fitted.eligible}
    shared = forecast(fitted, budgets, seed=3).conversions_change
    independent = forecast(fitted, budgets, seed=3, shared=False).conversions_change
    assert shared.high - shared.low < 0.75 * (independent.high - independent.low)
    assert shared.low < shared.mean < shared.high


async def test_scenarios_resolve_as_asked(account: tuple[Store, FittedAccount]) -> None:
    _, fitted = account
    config = BanditConfig()
    arms = fitted.eligible
    first, second = arms[0], arms[1]
    budgets, label = resolve_budgets(
        fitted,
        Scenario(
            changes=(
                BudgetChange(first.entity_ref, change=0.2),
                BudgetChange(second.entity_ref, budget=100.0),
            )
        ),
        config,
    )
    assert budgets[first.key] == pytest.approx(float(first.current_budget) * 1.2)
    assert budgets[second.key] == 100.0 and "+20%" in label
    total_now = sum(float(a.current_budget or 0) for a in arms)
    proportional, _ = resolve_budgets(fitted, Scenario(total_change=0.1), config)
    assert sum(proportional.values()) == pytest.approx(total_now * 1.1)
    best, label = resolve_budgets(fitted, Scenario(total_change=0.1, split="best"), config)
    assert sum(best.values()) == pytest.approx(total_now * 1.1, rel=1e-3)
    assert "split by the curves" in label
    for bad in (
        Scenario(),
        Scenario(total_change=0.1, total_daily_budget=100),
        Scenario(changes=(BudgetChange(first.entity_ref),)),
        Scenario(changes=(BudgetChange("nope", change=0.1),)),
        Scenario(total_change=-1),
    ):
        with pytest.raises(ValueError):
            resolve_budgets(fitted, bad, config)


async def test_the_report_flags_what_to_distrust_and_reads_against_goals(
    account: tuple[Store, FittedAccount],
) -> None:
    store, fitted = account
    alias = fitted.eligible[0].account_alias
    GoalStore(store).set(
        alias,
        {"target_cpa": 20, "monthly_budget": 60_000},
        effective_from=DEFAULT.start,
        source="test",
    )
    goal = GoalStore(store).current(alias, fitted.as_of)
    capped = next(a for a in fitted.eligible if a.capped)
    free = next(a for a in fitted.eligible if not a.capped)
    report = what_if(
        store,
        fitted,
        Scenario(
            changes=(
                BudgetChange(free.entity_ref, change=4.0),
                BudgetChange(capped.entity_ref, change=0.5),
            )
        ),
        account_alias=alias,
        config=BanditConfig(),
        goal=goal,
        currency="USD",
    )
    rows = {c.entity_ref: c for c in report.forecast.campaigns}
    assert {"step_advice", "outside_history"} <= set(rows[free.entity_ref].flags)
    assert "capped" in rows[capped.entity_ref].flags
    assert "go unspent" in report.reading
    assert "target" in (report.against_goals or "") and report.month is not None
    # A raise's marginal conversions cost more than a 20 target here: worse, said by code.
    marginal = report.incremental_vs_target or ""
    assert (
        marginal.startswith("each extra conversion costs about") and "20.00 USD target" in marginal
    )
    inc = report.forecast.incremental_cpa
    assert inc is not None and (("above" in marginal) == (inc > 20))
    assert ("(worse)" in marginal) == (inc > 20) and marginal in (report.against_goals or "")
    assert marginal[0].upper() + marginal[1:] in report.reading
    raised = rows[free.entity_ref]
    if raised.incremental_cpa is not None:
        assert raised.as_json()["incremental_vs_target"].startswith("each extra conversion")
    assert "monthly budget" in report.reading
    body = report.as_json()
    best = body["best_split"]
    assert best["unplaced"] == 0 and "sim-001" in best["budgets"]
    even = what_if(
        store,
        fitted,
        Scenario(total_change=0.05),
        account_alias=alias,
        config=BanditConfig(),
        goal=None,
        currency="USD",
    ).as_json()["best_split"]
    assert even["unplaced"] == 0 and even["gain"] >= -1e-6, "the curves' own split is not worse"
    assert "today's is" in even["reading"], "the reading names both totals"
    huge = what_if(
        store,
        fitted,
        Scenario(total_change=9.0, split="best"),
        account_alias=alias,
        config=BanditConfig(),
        goal=None,
        currency="USD",
    )
    assert huge.best is None, "a best split is the scenario itself"
    unplaced = what_if(
        store,
        fitted,
        Scenario(total_change=9.0),
        account_alias=alias,
        config=BanditConfig(),
        goal=None,
        currency="USD",
    )
    assert unplaced.best and unplaced.best["unplaced"] > 0
    assert "beyond what these campaigns have shown they can spend" in unplaced.reading
    assert body["difference"]["conversions"]["low"] <= body["difference"]["conversions"]["mean"]
    assert "sim-wc" not in str(body["campaigns"]), "provider account ids stay in the host"


async def test_an_unchanged_scenario_lands_the_month_where_pacing_does(
    account: tuple[Store, FittedAccount],
) -> None:
    from paid_media_agent.analytics.pacing import compute_pacing

    store, fitted = account
    alias = fitted.eligible[0].account_alias
    GoalStore(store).set(
        alias, {"monthly_budget": 60_000}, effective_from=DEFAULT.start, source="test"
    )
    goal = GoalStore(store).current(alias, fitted.as_of)
    report = what_if(
        store,
        fitted,
        Scenario(total_change=0.0),
        account_alias=alias,
        config=BanditConfig(),
        goal=goal,
        currency="USD",
    )
    pacing = compute_pacing(store, account_alias=alias, today=fitted.as_of, goal=goal)
    assert report.month["projected_spend"] == pytest.approx(pacing.projected_spend, abs=0.01)


def test_a_cut_is_judged_by_what_its_lost_conversions_cost() -> None:
    from paid_media_agent.bandit.whatif import incremental_vs_target

    dear = incremental_vs_target(46.83, -50.0, 30.0, "USD")
    cheap = incremental_vs_target(20.0, -50.0, 30.0, "USD")
    assert dear is not None and "56% above the 30.00 USD target" in dear
    assert dear.endswith("drops conversions that cost more than the target (better)")
    assert cheap is not None and cheap.endswith("cost less than the target (worse)")
    raise_ = incremental_vs_target(46.83, 50.0, 30.0, "USD")
    assert (
        raise_
        == "each extra conversion costs about 46.83 USD, 56% above the 30.00 USD target (worse)"
    )
    assert incremental_vs_target(46.83, 50.0, None, "USD") is None
