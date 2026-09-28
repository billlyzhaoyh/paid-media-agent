"""Campaigns as bandit arms: their history, budget, pacing, and whether they can be moved today.

History is read as it stood on the decision day. A day's conversions count once most have arrived:
matured days as reported, recent days divided by the share the account's lag curve says has
arrived (the surrogate reward; CBS section 3.2), and days still mostly unreported are left out.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np

from paid_media_agent.analytics.panel import (
    PanelRow,
    completeness,
    lag_curves,
    load_panel,
    maturity_days,
)
from paid_media_agent.store.db import Store

MIN_COMPLETENESS = 0.75
MIN_HISTORY_DAYS = 3
COLD_START_DAYS = 7
PACING_DAYS = 14
DEFAULT_PACING = 0.9
RECENT_SPEND_DAYS = 3
MIN_COVERAGE = 0.5
STALE_DAYS = 4
"""No complete day reported for this long means the sync stopped (settling takes longer)."""
ACTIVE_STATUSES = {"ENABLED", "ACTIVE"}


@dataclass
class Arm:
    platform: str
    provider_account_id: str
    account_alias: str
    entity_ref: str
    entity_name: str
    currency: str | None
    days: list[date] = field(default_factory=list)
    spend: np.ndarray = field(default_factory=lambda: np.zeros(0))
    conversions: np.ndarray = field(default_factory=lambda: np.zeros(0))
    """Lag-corrected final conversions per history day."""
    current_budget: float | None = None
    status: str | None = None
    pacing: float = DEFAULT_PACING
    unit: float = math.nan
    """Trailing cost per conversion: spend is modelled in units of it."""
    days_since_change: int | None = None
    last_reported: date | None = None
    """Newest complete day any pull reported, settled or not (freshness is judged on it)."""
    eligible: bool = True
    reason: str | None = None

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.provider_account_id}:{self.entity_ref}"

    @property
    def n_history(self) -> int:
        return len(self.days)

    @property
    def cold_start(self) -> bool:
        return self.n_history < COLD_START_DAYS

    @property
    def weekdays(self) -> np.ndarray:
        return np.asarray([d.weekday() for d in self.days], dtype=np.int64)

    @property
    def max_spend(self) -> float:
        return float(self.spend.max()) if len(self.spend) else 0.0

    @property
    def recent_spend(self) -> float:
        """Median spend over the last four weeks of history."""
        return float(np.median(self.spend[-28:])) if len(self.spend) else 0.0


@dataclass(frozen=True)
class DataChecks:
    passed: bool
    results: dict[str, dict[str, Any]]


def _settings(
    store: Store, as_of: date, account_alias: str | None
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """The settings in force on the decision morning, and when each budget last changed."""
    cutoff = datetime.combine(as_of + timedelta(days=1), time())
    alias_clause = "AND account_alias = ?" if account_alias else ""
    params: list[Any] = [cutoff, *([account_alias] if account_alias else [])]
    rows = store.fetch_dicts(
        f"""
        WITH s AS (
            SELECT platform, provider_account_id, entity_ref, account_alias, entity_name, status,
                daily_budget::DOUBLE AS daily_budget, currency, observed_at,
                lag(daily_budget::DOUBLE) OVER (
                    PARTITION BY platform, provider_account_id, entity_type, entity_ref
                    ORDER BY observed_at
                ) AS previous
            FROM entity_settings_snapshots
            WHERE entity_type = 'campaign' AND observed_at < ? {alias_clause}
        )
        SELECT platform, provider_account_id, entity_ref,
            arg_max(account_alias, observed_at) AS account_alias,
            arg_max(entity_name, observed_at) AS entity_name,
            arg_max(status, observed_at) AS status,
            arg_max(daily_budget, observed_at) AS daily_budget,
            arg_max(currency, observed_at) AS currency,
            max(observed_at) FILTER (
                WHERE previous IS NOT NULL AND previous IS DISTINCT FROM daily_budget
            ) AS changed_at,
            max(observed_at) AS observed_at
        FROM s
        GROUP BY platform, provider_account_id, entity_ref
        """,  # noqa: S608 - the only interpolation is a constant clause
        params,
    )
    found = {(r["platform"], r["provider_account_id"], r["entity_ref"]): r for r in rows}
    # A budget this agent changed (and read back) after the last settings observation is the
    # budget in force now, and the change starts the hold period, before the next sync sees it.
    applied = store.fetch(
        f"""
        SELECT platform, provider_account_id, entity_ref,
            arg_max(after_value, occurred_at)::VARCHAR, max(occurred_at)
        FROM change_events
        WHERE source = 'agent' AND status = 'verified' AND field = 'daily_budget'
            AND entity_type = 'campaign' AND occurred_at < ? {alias_clause}
        GROUP BY platform, provider_account_id, entity_ref
        """,  # noqa: S608 - the only interpolation is a constant clause
        params,
    )
    for platform, account, entity_ref, after, occurred_at in applied:
        setting = found.get((platform, account, entity_ref))
        if setting is None or occurred_at <= setting["observed_at"]:
            continue
        try:
            setting["daily_budget"] = float(json.loads(after))
        except (TypeError, ValueError):
            continue
        setting["changed_at"] = max(filter(None, (setting["changed_at"], occurred_at)))
    return found


def load_arms(
    store: Store, *, as_of: date, train_days: int = 90, account_alias: str | None = None
) -> list[Arm]:
    """Every campaign with settings or history in scope, as the store stood on `as_of`."""
    panel = load_panel(
        store, as_of=as_of, since=as_of - timedelta(days=train_days), account_alias=account_alias
    )
    maturity, curves = maturity_days(store), lag_curves(store)
    settings = _settings(store, as_of, account_alias)
    by_key: dict[tuple[str, str, str], list[PanelRow]] = defaultdict(list)
    newest: dict[str, date] = {}
    for row in panel:
        if row.is_complete and row.day < as_of:
            by_key[row.key].append(row)
            account = row.provider_account_id
            newest[account] = max(newest.get(account, row.day), row.day)
    arms: list[Arm] = []
    for key in sorted(set(by_key) | set(settings)):
        rows = by_key.get(key, [])
        setting = settings.get(key, {})
        first = rows[0] if rows else None
        arm = Arm(
            platform=key[0],
            provider_account_id=key[1],
            account_alias=setting.get("account_alias") or (first.account_alias if first else ""),
            entity_ref=key[2],
            entity_name=setting.get("entity_name") or (first.entity_name if first else key[2]),
            currency=setting.get("currency"),
            current_budget=setting.get("daily_budget"),
            status=setting.get("status"),
        )
        arm.last_reported = rows[-1].day if rows else None
        days, spend, conversions, pacing = [], [], [], []
        for row in rows:
            if row.conversions is None:
                continue
            share = completeness(row, maturity, curves)
            if share is None or share < MIN_COMPLETENESS:
                continue
            days.append(row.day)
            spend.append(row.spend)
            conversions.append(row.conversions / share)
            if row.budget:
                pacing.append(row.spend / row.budget)
        arm.days = days
        arm.spend = np.asarray(spend, dtype=np.float64)
        arm.conversions = np.asarray(conversions, dtype=np.float64)
        if pacing:
            arm.pacing = float(np.clip(np.median(pacing[-PACING_DAYS:]), 0.3, 1.0))
        if arm.conversions.sum() >= 1:
            arm.unit = float(arm.spend.sum() / arm.conversions.sum())
        changed_at = setting.get("changed_at")
        if changed_at is not None:
            arm.days_since_change = (as_of - changed_at.date()).days
        latest = newest.get(key[1])
        recent = [
            r for r in rows if latest is not None and (latest - r.day).days < RECENT_SPEND_DAYS
        ]
        arm.eligible, arm.reason = _eligibility(arm, recent)
        arms.append(arm)
    # Campaigns without conversions yet borrow the account's cost per conversion.
    for account in {a.provider_account_id for a in arms}:
        mine = [a for a in arms if a.provider_account_id == account]
        spent = sum(float(a.spend.sum()) for a in mine)
        converted = sum(float(a.conversions.sum()) for a in mine)
        fallback = spent / converted if converted >= 1 else math.nan
        for arm in mine:
            if not math.isfinite(arm.unit):
                arm.unit = fallback if math.isfinite(fallback) else max(arm.recent_spend, 1.0)
    return arms


def _eligibility(arm: Arm, recent: list[PanelRow]) -> tuple[bool, str | None]:
    """`recent` holds the campaign's rows from the account's last three complete days."""
    if arm.status is not None and arm.status.upper() not in ACTIVE_STATUSES:
        return False, f"status {arm.status}"
    if not arm.current_budget or arm.current_budget <= 0:
        return False, "no daily budget (shared or lifetime budgets are not allocated)"
    if not any(r.spend > 0 for r in recent):
        return False, "no spend in the account's last three complete days"
    if arm.n_history < MIN_HISTORY_DAYS:
        return False, f"{arm.n_history} days of settled history; needs {MIN_HISTORY_DAYS}"
    return True, None


def check_data(arms: list[Arm], as_of: date) -> DataChecks:
    """Checks that fail when a pipeline broke; the bandit then reuses its last good models."""
    results: dict[str, dict[str, Any]] = {}
    live = [a for a in arms if a.eligible]
    reported = max((a.last_reported for a in live if a.last_reported), default=None)
    results["fresh"] = {
        "ok": reported is not None and (as_of - reported).days <= STALE_DAYS,
        "newest_reported_day": reported.isoformat() if reported else None,
    }
    # Campaigns that are on, have a budget, and reported in the week before the last three days:
    # a pipeline that drops campaigns shows up as most of them going quiet together. One campaign
    # without delivery (platforms omit rows with no impressions) is not a pipeline failure.
    active = [
        a
        for a in arms
        if a.current_budget
        and (a.status is None or a.status.upper() in ACTIVE_STATUSES)
        and a.last_reported is not None
        and reported is not None
        and (reported - a.last_reported).days <= RECENT_SPEND_DAYS + 7
    ]
    covered = [
        a
        for a in active
        if reported is not None
        and a.last_reported is not None
        and (reported - a.last_reported).days < RECENT_SPEND_DAYS
    ]
    share = len(covered) / len(active) if active else 0.0
    results["coverage"] = {"ok": share >= MIN_COVERAGE, "share_reporting_recently": round(share, 2)}
    newest = max((a.days[-1] for a in live if a.days), default=None)
    totals: dict[date, float] = defaultdict(float)
    for arm in live:
        for day, spent in zip(arm.days, arm.spend, strict=True):
            totals[day] += float(spent)
    ratio = math.nan
    if newest is not None:
        before = [totals[d] for d in sorted(totals) if d < newest][-14:]
        if before and np.median(before) > 0:
            ratio = totals[newest] / float(np.median(before))
    results["spend_level"] = {
        "ok": not math.isfinite(ratio) or 0.2 <= ratio <= 5.0,
        "newest_vs_median": None if not math.isfinite(ratio) else round(ratio, 2),
    }
    return DataChecks(all(r["ok"] for r in results.values()), results)
