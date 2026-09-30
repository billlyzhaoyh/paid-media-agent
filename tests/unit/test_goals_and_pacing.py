"""Goals versioned by date, and monthly pacing checked against hand-computed numbers."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

import pytest

from paid_media_agent.analytics.goals import (
    GoalError,
    GoalStore,
    account_today,
    goal_line,
    update_goals,
)
from paid_media_agent.analytics.history import query_history
from paid_media_agent.analytics.pacing import account_pacing, compute_pacing
from paid_media_agent.config import AccountBinding, AccountRegistry
from paid_media_agent.domain.common import Platform
from paid_media_agent.store import Store

TODAY = date(2026, 9, 21)
ACCOUNTS = AccountRegistry(
    bindings=(
        AccountBinding(
            alias="acme",
            platform=Platform.GOOGLE_ADS,
            provider_account_id="1234567890",
            currency="USD",
            timezone="UTC",
        ),
        AccountBinding(
            alias="kiwi",
            platform=Platform.META_ADS,
            provider_account_id="act_1",
            currency="NZD",
            timezone="Pacific/Auckland",
        ),
    )
)


def _spend(
    store: Store,
    days: dict[date, float],
    *,
    conversions: float = 2.0,
    requested: tuple[date, date] | None = None,
) -> None:
    pull, pulled_at = uuid.uuid4(), datetime(2026, 9, 21, 6)
    if requested is not None:
        store.write(
            "INSERT INTO pulls VALUES (?, 'sync', 'get_campaign_performance', NULL, 'google_ads', "
            "'1234567890', 'acme', 'campaign', ?, ?, NULL, NULL, NULL, NULL, ?, [], ?, ?)",
            [pull, *requested, len(days), pulled_at, pulled_at.date()],
        )
    for day, spend in days.items():
        store.write(
            "INSERT INTO entity_daily_snapshots VALUES "
            "('google_ads', '1234567890', 'campaign', 'c1', ?, ?, ?, ?, 'acme', 'Search', 'USD', "
            "?, NULL, NULL, ?, NULL, true, [])",
            [day, pull, pulled_at, pulled_at.date(), spend, conversions],
        )


def test_goals_merge_clear_and_apply_from_their_day() -> None:
    store = Store()
    goals = GoalStore(store)
    goals.set(
        "acme",
        {"target_cpa": 30, "monthly_budget": 9000},
        effective_from=date(2026, 9, 1),
        source="cli",
    )
    goals.set("acme", {"monthly_budget": 12000}, effective_from=date(2026, 10, 1), source="cli")
    september, october = (
        goals.current("acme", date(2026, 9, 30)),
        goals.current("acme", date(2026, 10, 2)),
    )
    assert (september.target_cpa, september.monthly_budget) == (30.0, 9000.0)
    assert (october.target_cpa, october.monthly_budget) == (30.0, 12000.0), "unchanged goals carry"
    assert goals.current("acme", date(2026, 8, 31)) is None

    goals.set("acme", {"target_cpa": None}, effective_from=date(2026, 10, 1), source="console")
    replaced = goals.current("acme", date(2026, 10, 1))
    assert replaced.target_cpa is None and replaced.monthly_budget == 12000.0
    assert len(goals.history("acme")) == 2, "a second set on the same day replaces that day's row"

    for bad in ({"target_cpa": 0}, {"target_cpa": "abc"}, {"cpc": 1}, {}):
        with pytest.raises(GoalError):
            goals.set("acme", bad, effective_from=date(2026, 10, 1), source="cli")
    with pytest.raises(GoalError, match="both set and cleared"):
        update_goals(
            store,
            ACCOUNTS,
            "acme",
            values={"target_cpa": 5},
            clear=["target_cpa"],
            source="cli",
        )
    with pytest.raises(GoalError, match="unknown account alias"):
        update_goals(store, ACCOUNTS, "nobody", values={"target_cpa": 5}, source="cli")
    rows, _ = query_history(store, "goals", account_alias="acme")
    assert [r["effective_from"] for r in rows] == ["2026-10-01", "2026-09-01"]


def test_goal_lines_say_which_side_is_worse() -> None:
    assert goal_line("CPA", 42.1, 35, lower_is_better=True) == (
        "CPA 42.10 against a 35.00 target: 20% above target (worse)"
    )
    assert goal_line("ROAS", 3.3, 3.0, lower_is_better=False).endswith("10% above target (better)")
    assert goal_line("CPA", None, 35, lower_is_better=True) == ""


def test_pacing_projects_the_month_and_the_spend_that_lands_on_budget() -> None:
    store = Store()
    _spend(store, {date(2026, 8, 20) + timedelta(days=i): 100.0 for i in range(32)})
    goal = GoalStore(store).set(
        "acme",
        {"monthly_budget": 3300, "target_cpa": 40},
        effective_from=date(2026, 9, 1),
        source="cli",
    )
    report = compute_pacing(store, account_alias="acme", today=TODAY, goal=goal, currency="USD")
    # September 1-20 at 100 a day; 21-30 still to come at the same rate.
    assert report.data_through == date(2026, 9, 20) and report.days_remaining == 10
    assert report.spend == pytest.approx(2000) and report.run_rate == pytest.approx(100)
    assert report.projected_spend == pytest.approx(3000)
    assert report.needed_daily == pytest.approx(130) and report.budget_scale == pytest.approx(1.3)
    assert report.status == "under"
    assert report.conversions == pytest.approx(40) and report.cpa == pytest.approx(50)
    assert report.reading == (
        "Spent 2,000 USD of 3,300 USD (61%) in 2026-09 through 2026-09-20, with 10 of 30 days "
        "left. At the current rate the month ends near 3,000 USD (-9.1%, under budget). Spending "
        "about 130 USD a day lands on budget. CPA 50.00 against a 40.00 target: 25% above target "
        "(worse)."
    )
    assert any("no measured lag" in n for n in report.notes), "young days are reported as is"


def test_weekday_patterns_shape_the_projection_and_gaps_are_not_zero_spend() -> None:
    store = Store()
    start = date(2026, 8, 17)
    _spend(
        store,
        {
            start + timedelta(days=i): (
                50.0 if (start + timedelta(days=i)).weekday() >= 5 else 150.0
            )
            for i in range(32)  # through 2026-09-17: three days not yet synced
        },
    )
    report = compute_pacing(store, account_alias="acme", today=TODAY, goal=None, currency="USD")
    remaining = [date(2026, 9, 18) + timedelta(days=i) for i in range(13)]
    expected = sum(50.0 if d.weekday() >= 5 else 150.0 for d in remaining)
    assert report.weekday_adjusted and report.days_remaining == 13
    assert report.projected_spend == pytest.approx(report.spend + expected)
    assert report.status == "no_budget" and report.needed_daily is None
    assert any("data runs through 2026-09-17" in n for n in report.notes)
    assert "no monthly budget is set" in report.reading


def test_each_account_paces_its_own_local_month() -> None:
    now = datetime(2026, 9, 30, 13, 0)  # already 1 October in Auckland
    assert account_today(ACCOUNTS, "kiwi", now) == date(2026, 10, 1)
    assert account_today(ACCOUNTS, "acme", now) == date(2026, 9, 30)
    report = account_pacing(Store(), ACCOUNTS, "kiwi", now=now)
    assert report.month == "2026-10" and report.currency == "NZD"
    assert report.reading == "No spend recorded for 2026-10 yet."
    with pytest.raises(ValueError, match="unknown account alias"):
        account_pacing(Store(), ACCOUNTS, "nobody", now=now)


def test_days_with_no_rows_inside_the_synced_range_count_as_zero_spend() -> None:
    store = Store()
    # Weekdays only: platforms send no rows for days nothing spent.
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(20)]
    _spend(store, {d: 100.0 for d in days if d.weekday() < 5}, requested=(days[0], days[-1]))
    report = compute_pacing(store, account_alias="acme", today=TODAY, goal=None, currency="USD")
    # The last seven days (14-20) hold five weekdays at 100 and a weekend at 0.
    assert report.run_rate == pytest.approx(500 / 7)


def test_days_no_pull_asked_for_are_unknown_not_zero() -> None:
    store = Store()
    # Pulled on their own days, with 15-18 never read (a failed call): not zero spend.
    for day in (date(2026, 9, d) for d in (13, 14, 19, 20)):
        _spend(store, {day: 100.0}, requested=(day, day))
    report = compute_pacing(store, account_alias="acme", today=TODAY, goal=None, currency="USD")
    assert report.run_rate == pytest.approx(100.0)


def test_value_is_reported_as_is_and_with_value_still_arriving() -> None:
    store = Store()
    pull, pulled_at = uuid.uuid4(), datetime(2026, 9, 21, 6)
    rows = [
        # (day, entity, conversions, value): a recent day still maturing, an old one complete,
        # and a row with value but no reported conversions.
        (date(2026, 9, 20), "c1", 2.0, 100.0),
        (date(2026, 9, 5), "c1", 4.0, 200.0),
        (date(2026, 9, 5), "c2", None, 50.0),
    ]
    for day, entity, conversions, value in rows:
        store.write(
            "INSERT INTO entity_daily_snapshots VALUES "
            "('google_ads', '1234567890', 'campaign', ?, ?, ?, ?, ?, 'acme', 'Search', 'USD', "
            "100, NULL, NULL, ?, ?, true, [])",
            [entity, day, pull, pulled_at, pulled_at.date(), conversions, value],
        )
    report = compute_pacing(store, account_alias="acme", today=TODAY, goal=None, currency="USD")
    assert report.conversion_value == pytest.approx(350.0), "as reported, every row"
    assert report.conversion_value_expected is not None
    assert report.conversion_value_expected >= 350.0, "the row without conversions still counts"
    assert report.roas == pytest.approx(report.conversion_value_expected / 300.0)
