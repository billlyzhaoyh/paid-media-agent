"""Planted causes in a simulated account: `explain_change` names the campaign and the funnel step."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from paid_media_agent.analytics.drivers import (
    ChangeReport,
    Window,
    add_curve_check,
    explain,
)
from paid_media_agent.bandit.recommend import BanditConfig
from paid_media_agent.sim.scenario import ScenarioDriver, scenario_binding
from paid_media_agent.sim.simulator import ScenarioParams, SimEvent, Simulator, budget_schedule
from paid_media_agent.store import Store

DAYS = 90
CHANGE_DAY = 76
"""The current window is days 76-82; later days are too young to count fully."""
QUIET = ScenarioParams(
    scenario_id="ev",
    seed=4,
    n_campaigns=5,
    days=DAYS,
    start=date(2026, 3, 2),
    cold_starts=0,
    shock_rate=0.0,
    budget_change_every=(400, 401),
)
CURRENT = Window(QUIET.start + timedelta(days=CHANGE_DAY), QUIET.start + timedelta(days=82))
PREVIOUS = Window(CURRENT.start - timedelta(days=7), CURRENT.start - timedelta(days=1))


def _run(params: ScenarioParams, *, moves: dict[str, float] | None = None) -> tuple[Store, str]:
    """Run the schedule; from CHANGE_DAY, multiply the named campaigns' budgets."""
    store = Store()
    driver = ScenarioDriver(store, params)
    schedule = budget_schedule(driver.sim)
    for index in range(params.days):
        budgets = {ref: days[index] for ref, days in schedule.items()}
        if index >= CHANGE_DAY:
            for ref, factor in (moves or {}).items():
                budgets[ref] *= factor
        driver.run_day(index, budgets)
    return store, scenario_binding(params).alias


def _explain(store: Store, alias: str) -> ChangeReport:
    return explain(store, [alias], metric="cpa", current=CURRENT, previous=PREVIOUS, currency="USD")


def _biggest_spender(params: ScenarioParams) -> str:
    return max(Simulator(params).campaigns, key=lambda c: c.base_budget).entity_ref


def test_a_conversion_rate_drop_is_named_on_the_right_campaign() -> None:
    target = _biggest_spender(QUIET)
    params = replace(QUIET, events=(SimEvent(target, "cvr", 0.6, CHANGE_DAY),))
    report = _explain(*_run(params))
    top, *others = report.drivers()
    assert (top.entity_ref, top.factor, top.within_noise) == (target, "cvr", False)
    assert top.after == pytest.approx(0.6 * top.before, rel=0.15), "the planted 40% drop"
    assert all(abs(o.points) < 0.7 * top.points for o in others), "it stands out from noise"
    assert f"{target}'s conversion rate moved by more than noise" in report.reading


def test_dearer_impressions_are_named_as_cost_per_thousand() -> None:
    target = _biggest_spender(QUIET)
    params = replace(QUIET, events=(SimEvent(target, "cpm", 1.5, CHANGE_DAY),))
    report = _explain(*_run(params))
    top = report.drivers()[0]
    assert (top.entity_ref, top.factor) == (target, "cpm")
    assert report.decomposition.effects()["cpm"] > 0.7 * report.decomposition.change * 100


@pytest.mark.parametrize("seed", [1, 2])
async def test_more_budget_on_one_campaign_is_mix_and_diminishing_returns(seed: int) -> None:
    params = replace(QUIET, seed=seed)
    target = _biggest_spender(params)
    store, alias = _run(params, moves={target: 1.8})
    report = _explain(store, alias)
    await add_curve_check(report, store, None, BanditConfig())
    first = report.campaigns[0]
    assert first.entity_ref == target and abs(first.mix_points) > abs(first.rate_points)
    assert first.rate_points > 0, "it converts its extra spend dearer"
    assert first.expected_rate_points is not None
    assert first.expected_rate_points > 0.5 * first.rate_points, "its curve expects most of it"
    assert "diminishing returns" in report.reading and first.constraint is not None
    assert report.known_changes[0]["campaign"] == target, "the budget change is listed"


def test_a_quiet_account_is_mostly_called_noise() -> None:
    quiet = [_explain(*_run(replace(QUIET, seed=seed))).significant for seed in (1, 2, 3, 5)]
    assert sum(1 for s in quiet if s is False) >= 3
