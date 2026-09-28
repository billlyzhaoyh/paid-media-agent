"""What happened after each budget decision: followed, overridden, superseded, or still pending."""

from __future__ import annotations

from datetime import date

from paid_media_agent.bandit.recommend import BanditConfig, recommend
from paid_media_agent.sim.scenario import ScenarioDriver
from paid_media_agent.sim.simulator import ScenarioParams, budget_schedule
from paid_media_agent.store import Store

PARAMS = ScenarioParams(
    scenario_id="outcomes",
    seed=5,
    n_campaigns=3,
    days=80,
    start=date(2026, 3, 2),
    cold_starts=0,
    shock_rate=0.0,
)


def _outcomes(store: Store, day: date) -> dict[str, dict[str, object]]:
    rows = store.fetch_dicts(
        "SELECT * FROM bandit_outcomes WHERE decision_day = ? ORDER BY entity_ref", [day]
    )
    return {str(r["entity_ref"]): r for r in rows}


async def test_outcomes_follow_what_was_actually_applied_and_count_matured_days_only() -> None:
    store = Store()
    driver = ScenarioDriver(store, PARAMS)
    schedule = budget_schedule(driver.sim)
    budgets = {ref: days[0] for ref, days in schedule.items()}
    config = BanditConfig(policy="greedy")

    async def decide(index: int) -> dict[str, float]:
        run = await recommend(
            store, None, as_of=driver.sim.day(index), config=config, mode="simulate", seed=index
        )
        return run.budgets

    for index in range(42):
        budgets = {ref: days[index] for ref, days in schedule.items()}
        driver.run_day(index, budgets)
    first = await decide(42)
    applied = dict(first)
    applied["sim-002"] = round(first["sim-002"] * 1.15, 2)  # an operator overrides one campaign
    for index in range(42, 60):
        driver.run_day(index, applied)
    second = await decide(60)
    for index in range(60, 62):
        driver.run_day(index, second)
    third = await decide(62)  # within the second decision's hold window
    for index in range(62, 64):
        driver.run_day(index, third)

    week1 = _outcomes(store, driver.sim.day(42))
    assert {r: o["outcome"] for r, o in week1.items()} == {
        "sim-001": "followed",
        "sim-002": "overridden",
        "sim-003": "followed",
    }
    followed = week1["sim-001"]
    assert followed["days_matured"] == 7 and followed["budget_in_force"] == first["sim-001"]
    matured = store.fetch(
        "SELECT sum(conversions)::DOUBLE FROM entity_daily_matured "
        "WHERE entity_ref = 'sim-001' AND day >= ? AND day < ?",
        [driver.sim.day(42), driver.sim.day(49)],
    )[0][0]
    assert followed["conversions_matured"] == matured
    assert followed["expected_conversions_window"] and followed["spend"]

    assert {o["outcome"] for o in _outcomes(store, driver.sim.day(60)).values()} == {"superseded"}
    latest = _outcomes(store, driver.sim.day(62))
    assert {o["outcome"] for o in latest.values()} == {"pending"}
    assert all(int(o["days_matured"]) == 0 for o in latest.values()), "nothing matured yet"
