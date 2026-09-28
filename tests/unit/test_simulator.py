"""The simulator: reproducible truth, delayed conversions, and a scenario that loads into history."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from paid_media_agent.sim.scenario import run_scenario, scenario_path
from paid_media_agent.sim.simulator import ScenarioParams, Simulator, budget_schedule
from paid_media_agent.store import Store

PARAMS = ScenarioParams(scenario_id="unit", seed=3, n_campaigns=3, days=45, start=date(2026, 1, 5))


def _run(sim: Simulator) -> None:
    budgets = budget_schedule(sim)
    for t in range(sim.params.days):
        sim.step(t, {ref: days[t] for ref, days in budgets.items()})


def test_a_seed_fixes_the_truth_and_the_outcomes() -> None:
    first, second = Simulator(PARAMS), Simulator(PARAMS)
    _run(first)
    _run(second)
    assert first.campaigns == second.campaigns
    assert first.outcomes == second.outcomes
    assert (
        Simulator(ScenarioParams(seed=4)).campaigns != Simulator(ScenarioParams(seed=3)).campaigns
    )


def test_the_response_curve_is_the_power_law_the_bandit_assumes() -> None:
    campaign = Simulator(PARAMS).campaigns[0]
    low, high = campaign.response(100.0), campaign.response(400.0)
    assert 0 < low < high < 4 * low, "more spend buys more, with diminishing returns"
    assert 0.55 <= campaign.kappa2 <= 0.9


def test_conversions_arrive_late_but_never_exceed_or_shrink() -> None:
    sim = Simulator(PARAMS)
    _run(sim)
    for outcome in sim.outcomes.values():
        seen = [outcome.reported_conversions(age) for age in range(0, 30)]
        assert seen == sorted(seen) and seen[0] == 0 and seen[-1] == outcome.conversions
        assert 0.5 * outcome.budget <= outcome.spend <= 1.8 * outcome.budget


def test_cold_starts_have_no_rows_before_they_begin() -> None:
    sim = Simulator(ScenarioParams(n_campaigns=4, cold_starts=2, days=60))
    late = [c for c in sim.campaigns if c.start_index > 0]
    assert len(late) == 2
    _run(sim)
    for campaign in late:
        days = sorted(i for ref, i in sim.outcomes if ref == campaign.entity_ref)
        assert days[0] == campaign.start_index


def test_a_scenario_loads_into_the_history_views() -> None:
    store = Store()
    run = run_scenario(store, PARAMS)

    assert run.truth_rows == store.fetch("SELECT count(*) FROM sim_truth")[0][0]
    assert store.fetch("SELECT count(*) FROM entity_daily_latest")[0][0] == run.truth_rows
    unmatured = store.fetch(
        "SELECT count(*), min(day) FROM entity_daily_panel WHERE NOT is_matured"
    )[0]
    assert unmatured == (3 * 6, PARAMS.start + timedelta(days=PARAMS.days - 6)), "last six days"
    first_day_share = store.fetch("SELECT completeness FROM conversion_lag WHERE age_days = 1")
    assert 0.2 < float(first_day_share[0][0]) < 0.45, "a third of conversions arrive on day one"
    changes = store.fetch("SELECT count(*) FROM change_events WHERE source='external_detected'")
    versions = store.fetch("SELECT count(*) FROM entity_settings_history")[0][0]
    assert changes[0][0] == run.external_changes == versions - 3
    pacing = store.fetch(
        "SELECT min(pacing_ratio), max(pacing_ratio), count(*) FILTER (daily_budget IS NULL) "
        "FROM entity_daily_panel"
    )[0]
    assert 0.5 <= pacing[0] and pacing[1] <= 1.8 and pacing[2] == 0
    truth = store.fetch(
        "SELECT t.budget, p.daily_budget FROM sim_truth t JOIN entity_daily_panel p "
        "USING (entity_ref, day) LIMIT 50"
    )
    assert all(abs(budget - float(observed)) < 0.01 for budget, observed in truth)


def test_scenario_names_are_checked_before_they_become_paths(tmp_path: object) -> None:
    with pytest.raises(ValueError, match="scenario id"):
        scenario_path(tmp_path, "../escape")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="scenario id"):
        run_scenario(Store(), ScenarioParams(scenario_id="Bad Name"))
