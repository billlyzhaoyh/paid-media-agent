"""Monthly pacing: month-to-date spend against the monthly budget, and where the month will land.

For one account and its own calendar month (in the account's timezone), from the newest stored
numbers of each campaign-day:
- month-to-date spend, conversions (as reported, and expected once late conversions arrive, from
  the account's lag curve), conversion value, CPA, and ROAS;
- a run rate: the trailing seven days' average daily spend, adjusted for weekday patterns when four
  weeks of history show them;
- the projected month-end spend, the daily spend that lands on the monthly budget, and today's
  total of active campaign budgets;
- CPA and ROAS against the account's goals.

Every number is computed here; the reading is code-written. Days not yet synced count as remaining,
so a stale sync shows up as fewer days of data, never as zero spend.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from paid_media_agent.analytics.goals import Goal, GoalStore, account_today, goal_line
from paid_media_agent.analytics.panel import PanelRow, completeness, lag_curves, maturity_days
from paid_media_agent.bandit.platform_rules import rules_for
from paid_media_agent.config import AccountRegistry
from paid_media_agent.store.db import Store

ON_TRACK = 0.05
"""A projection within this share of the monthly budget is on track."""
RUN_RATE_DAYS = 7
WEEKDAY_DAYS = 28
MIN_RUN_RATE_DAYS = 3
ACTIVE_STATUSES = ("ENABLED", "ACTIVE")


@dataclass
class PacingReport:
    account_alias: str
    currency: str | None
    month: str
    today: date
    data_through: date | None
    days_in_month: int
    days_with_data: int
    days_remaining: int
    """Days from the day after `data_through` to the month's end: what is left to spend."""
    spend: float
    conversions: float | None
    conversions_expected: float | None
    conversion_value: float | None
    """As reported, like `conversions`."""
    conversion_value_expected: float | None
    """With value still arriving, like `conversions_expected`; roas uses it."""
    cpa: float | None
    roas: float | None
    run_rate: float | None
    weekday_adjusted: bool
    projected_spend: float | None
    monthly_budget: float | None
    needed_daily: float | None
    active_budgets: float | None
    budget_scale: float | None
    """Needed daily spend over the projected daily spend: >1 means spend more to land on budget."""
    status: str
    target_cpa: float | None = None
    target_roas: float | None = None
    notes: list[str] = field(default_factory=list)
    reading: str = ""

    def as_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["today"] = self.today.isoformat()
        data["data_through"] = self.data_through.isoformat() if self.data_through else None
        return {k: round(v, 4) if isinstance(v, float) else v for k, v in data.items()}


def _money(value: float | None, currency: str | None) -> str:
    if value is None:
        return "n/a"
    number = f"{value:,.0f}" if abs(value) >= 100 else f"{value:,.2f}"
    return f"{number} {currency}" if currency else number


def _daily(store: Store, alias: str, since: date, until: date) -> list[dict[str, Any]]:
    return store.fetch_dicts(
        """
        SELECT platform, provider_account_id, entity_ref, any_value(entity_name) AS entity_name,
            day, any_value(currency) AS currency, sum(spend)::DOUBLE AS spend,
            sum(conversions)::DOUBLE AS conversions, count(conversions) AS reported,
            sum(conversion_value)::DOUBLE AS conversion_value, min(pulled_on - day) AS age_days
        FROM entity_daily_latest
        WHERE account_alias = ? AND entity_type = 'campaign' AND day BETWEEN ? AND ?
        GROUP BY platform, provider_account_id, entity_ref, day
        ORDER BY day
        """,
        [alias, since, until],
    )


def _pulled_days(store: Store, alias: str, since: date, until: date) -> set[date]:
    """Days some recorded campaign pull asked for: a day in one with no rows spent nothing."""
    spans = store.fetch(
        """
        SELECT greatest(requested_start, ?::DATE), least(requested_end, ?::DATE) FROM pulls
        WHERE account_alias = ? AND entity_type = 'campaign' AND requested_start IS NOT NULL
            AND requested_end IS NOT NULL AND requested_end >= ? AND requested_start <= ?
        """,
        [since, until, alias, since, until],
    )
    days: set[date] = set()
    for start, end in spans:
        days.update(start + timedelta(days=i) for i in range((end - start).days + 1))
    return days


def _active_budgets(store: Store, alias: str) -> float | None:
    rows = store.fetch(
        """
        SELECT status, daily_budget::DOUBLE FROM entity_settings_snapshots
        WHERE account_alias = ? AND entity_type = 'campaign'
        QUALIFY row_number() OVER (PARTITION BY entity_ref ORDER BY observed_at DESC) = 1
        """,
        [alias],
    )
    budgets = [
        b
        for status, b in rows
        if b is not None and (status is None or str(status).upper() in ACTIVE_STATUSES)
    ]
    return sum(budgets) if budgets else None


def _weekday_factors(by_day: dict[date, float], end: date) -> dict[int, float] | None:
    """Spend by weekday over the trailing four weeks, relative to the average day."""
    window = {d: s for d, s in by_day.items() if end - timedelta(days=WEEKDAY_DAYS) < d <= end}
    if len(window) < WEEKDAY_DAYS - 7:
        return None
    grouped: dict[int, list[float]] = defaultdict(list)
    for day, spend in window.items():
        grouped[day.weekday()].append(spend)
    mean = sum(window.values()) / len(window)
    if mean <= 0 or len(grouped) < 7 or min(len(v) for v in grouped.values()) < 3:
        return None
    return {wd: (sum(v) / len(v)) / mean for wd, v in grouped.items()}


def compute_pacing(
    store: Store,
    *,
    account_alias: str,
    today: date,
    goal: Goal | None,
    currency: str | None = None,
) -> PacingReport:
    """Pacing for the calendar month containing `today` (the account's local date)."""
    month_start = today.replace(day=1)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    month_end = month_start + timedelta(days=days_in_month - 1)
    history_start = min(month_start, today - timedelta(days=WEEKDAY_DAYS + 1))
    rows = _daily(store, account_alias, history_start, today - timedelta(days=1))
    currency = currency or next((r["currency"] for r in rows if r["currency"]), None)
    by_day: dict[date, float] = defaultdict(float)
    for row in rows:
        by_day[row["day"]] += row["spend"] or 0.0
    # Platforms return no rows for a day nothing spent: a day a pull asked for with no rows
    # spent zero, and skipping it would lift the run rate. A day no pull asked for (a failed
    # call, a gap in the resync) is unknown, not zero, and is left out.
    for day in _pulled_days(store, account_alias, history_start, today - timedelta(days=1)):
        by_day.setdefault(day, 0.0)
    month_rows = [r for r in rows if r["day"] >= month_start]
    data_through = max((r["day"] for r in month_rows), default=None)
    notes: list[str] = []

    spend = sum(r["spend"] or 0.0 for r in month_rows)
    reported = [r for r in month_rows if r["reported"]]
    conversions = sum(r["conversions"] or 0.0 for r in reported) if reported else None
    expected: float | None = None
    corrected_value: float | None = None
    if reported:
        maturity, curves = maturity_days(store), lag_curves(store)
        expected, unknown = 0.0, 0
        for r in reported:
            share = completeness(
                PanelRow(
                    r["platform"], r["provider_account_id"], account_alias, r["entity_ref"],
                    r["entity_name"] or "", r["day"], r["spend"] or 0.0, r["conversions"],
                    True, int(r["age_days"]), None,
                ),
                maturity,
                curves,
            )  # fmt: skip
            if share is None:
                unknown += 1
                share = 1.0
            expected += (r["conversions"] or 0.0) / max(share, 0.05)
            if r["conversion_value"] is not None:
                # Value arrives with its conversions: the same share corrects it.
                corrected_value = (corrected_value or 0.0) + r["conversion_value"] / max(
                    share, 0.05
                )
        if unknown:
            notes.append(
                f"{unknown} recent campaign-days have no measured lag yet; their conversions are "
                "counted as reported and may still grow"
            )
    if month_rows and conversions is None:
        notes.append("conversions are not reported for this account (see conversion_action)")
    values = [r["conversion_value"] for r in month_rows if r["conversion_value"] is not None]
    value = sum(values) if values else None
    # Value on a row without reported conversions has no lag to correct by; it counts as is.
    unmatched = [
        r["conversion_value"]
        for r in month_rows
        if not r["reported"] and r["conversion_value"] is not None
    ]
    expected_value = corrected_value + sum(unmatched) if corrected_value is not None else value
    cpa = spend / expected if expected else None
    roas = expected_value / spend if expected_value is not None and spend > 0 else None

    last = data_through or today - timedelta(days=1)
    if data_through is not None and data_through < today - timedelta(days=1):
        notes.append(
            f"data runs through {data_through.isoformat()}; the days since are projected, not "
            "missing spend (run `sync`)"
        )
    remaining_days = [last + timedelta(days=i) for i in range(1, (month_end - last).days + 1)]
    recent = [by_day[d] for d in by_day if last - timedelta(days=RUN_RATE_DAYS) < d <= last]
    factors = _weekday_factors(by_day, last)
    run_rate: float | None = None
    projected_remaining: float | None = None
    if len(recent) >= MIN_RUN_RATE_DAYS:
        if factors:
            recent_days = [d for d in by_day if last - timedelta(days=RUN_RATE_DAYS) < d <= last]
            base = sum(by_day[d] / factors[d.weekday()] for d in recent_days) / len(recent_days)
            projected_remaining = sum(base * factors[d.weekday()] for d in remaining_days)
        else:
            base = sum(recent) / len(recent)
            projected_remaining = base * len(remaining_days)
        run_rate = sum(recent) / len(recent)
    else:
        notes.append("fewer than three recent days of spend; no run rate or projection")
    projected = spend + projected_remaining if projected_remaining is not None else None

    budget = goal.monthly_budget if goal else None
    needed = (budget - spend) / len(remaining_days) if budget and remaining_days else None
    scale: float | None = None
    status = "no_budget"
    if budget:
        if projected is None:
            status = "unknown"
        else:
            ratio = projected / budget
            status = "on_track" if abs(ratio - 1) <= ON_TRACK else "over" if ratio > 1 else "under"
        if needed is not None and projected_remaining and remaining_days:
            scale = max(needed, 0.0) / (projected_remaining / len(remaining_days))
        if spend >= budget:
            notes.append("the monthly budget is already spent")

    report = PacingReport(
        account_alias=account_alias,
        currency=currency,
        month=month_start.strftime("%Y-%m"),
        today=today,
        data_through=data_through,
        days_in_month=days_in_month,
        days_with_data=len({r["day"] for r in month_rows}),
        days_remaining=len(remaining_days),
        spend=spend,
        conversions=conversions,
        conversions_expected=expected,
        conversion_value=value,
        conversion_value_expected=expected_value,
        cpa=cpa,
        roas=roas,
        run_rate=run_rate,
        weekday_adjusted=factors is not None,
        projected_spend=projected,
        monthly_budget=budget,
        needed_daily=needed,
        active_budgets=_active_budgets(store, account_alias),
        budget_scale=scale,
        status=status,
        target_cpa=goal.target_cpa if goal else None,
        target_roas=goal.target_roas if goal else None,
        notes=notes,
    )
    report.reading = pacing_reading(report)
    return report


def pacing_reading(report: PacingReport) -> str:
    """One code-written paragraph; every number comes from the report."""
    c = report.currency
    parts: list[str] = []
    if report.data_through is None:
        parts.append(f"No spend recorded for {report.month} yet.")
    elif report.monthly_budget:
        share = report.spend / report.monthly_budget
        parts.append(
            f"Spent {_money(report.spend, c)} of {_money(report.monthly_budget, c)} ({share:.0%}) "
            f"in {report.month} through {report.data_through.isoformat()}, with "
            f"{report.days_remaining} of {report.days_in_month} days left."
        )
    else:
        parts.append(
            f"Spent {_money(report.spend, c)} in {report.month} through "
            f"{report.data_through.isoformat()}; no monthly budget is set, so pacing is not judged."
        )
    if report.projected_spend is not None and report.monthly_budget:
        gap = report.projected_spend / report.monthly_budget - 1
        verdict = {"on_track": "on track", "over": "over budget", "under": "under budget"}.get(
            report.status, report.status
        )
        parts.append(
            f"At the current rate the month ends near {_money(report.projected_spend, c)} "
            f"({gap:+.1%}, {verdict})."
        )
    elif report.projected_spend is not None:
        parts.append(
            f"At the current rate the month ends near {_money(report.projected_spend, c)}."
        )
    if report.needed_daily is not None and report.needed_daily > 0:
        line = f"Spending about {_money(report.needed_daily, c)} a day lands on budget"
        if report.active_budgets is not None:
            line += f"; active campaign budgets total {_money(report.active_budgets, c)} a day"
        parts.append(line + ".")
    for line in (
        goal_line("CPA", report.cpa, report.target_cpa, lower_is_better=True),
        goal_line("ROAS", report.roas, report.target_roas, lower_is_better=False),
    ):
        if line:
            parts.append(line + ".")
    return " ".join(parts)


def account_pacing(
    store: Store, accounts: AccountRegistry, alias: str, *, now: datetime | None = None
) -> PacingReport:
    """Pacing for one configured alias, on its own local date and with its goal in force."""
    binding = accounts.resolve(alias)
    if binding is None:
        raise ValueError(f"unknown account alias {alias}; call list_accounts")
    today = account_today(accounts, alias, now)
    report = compute_pacing(
        store,
        account_alias=alias,
        today=today,
        goal=GoalStore(store).current(alias, today),
        currency=binding.currency,
    )
    note = rules_for(binding.platform).pacing_note()
    if note:
        report.notes.append(note)
    return report
