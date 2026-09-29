"""Business goals per account: target CPA or ROAS and a monthly budget, versioned by date.

Goals are host-owned data in the state file. A person sets them with `goals set` or the setup
console; the agent reads them (`list_accounts`, `check_pacing`, `against_goals` in analyses) and
can only propose a change, which applies after approval (`tools/host_writes.py`).

The goal on a day is the latest version that took effect on or before it, so pacing, analyses,
and past budget decisions are judged against the goal that applied then. Setting a goal merges
the changed fields into the goal in force and writes a new version; `None` clears a field.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from paid_media_agent.analytics.ingest import local_date
from paid_media_agent.config import AccountRegistry
from paid_media_agent.domain.common import JsonValue
from paid_media_agent.store.db import Store, utc_now

GOAL_FIELDS = ("target_cpa", "target_roas", "monthly_budget")
"""Numeric goals. Money is in the account's currency; ROAS is value per unit of spend."""
_UNSET: Any = object()

GoalLookup = Callable[[str, date], "Goal | None"]
"""The goal in force for an account alias on a day."""


class GoalError(ValueError):
    pass


def account_today(accounts: AccountRegistry, alias: str, now: datetime | None = None) -> date:
    """Today in the account's timezone; goals take effect on account-local days."""
    binding = accounts.resolve(alias)
    return local_date(now or utc_now(), binding.timezone if binding else "UTC")


@dataclass(frozen=True)
class Goal:
    account_alias: str
    effective_from: date
    target_cpa: float | None = None
    target_roas: float | None = None
    monthly_budget: float | None = None
    notes: str | None = None
    source: str = ""
    proposal_id: UUID | None = None
    set_at: datetime | None = None

    def values(self) -> dict[str, float | None]:
        return {field: getattr(self, field) for field in GOAL_FIELDS}

    def as_json(self) -> dict[str, JsonValue]:
        return {
            **self.values(),
            "effective_from": self.effective_from.isoformat(),
            "notes": self.notes,
            "source": self.source,
        }


def validate_changes(changes: Mapping[str, JsonValue]) -> dict[str, float | None]:
    """Goal fields with positive values, or None to clear. Anything else is refused."""
    unknown = sorted(set(changes) - set(GOAL_FIELDS))
    if unknown:
        raise GoalError(f"unknown goal fields: {', '.join(unknown)}; use {', '.join(GOAL_FIELDS)}")
    if not changes:
        raise GoalError("no goal fields to change")
    clean: dict[str, float | None] = {}
    for field, value in changes.items():
        if value is None:
            clean[field] = None
            continue
        if isinstance(value, bool) or isinstance(value, (dict, list)):
            raise GoalError(f"{field} must be a number")
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise GoalError(f"{field} must be a number") from None
        if not number.is_finite() or number <= 0:
            raise GoalError(f"{field} must be greater than zero")
        clean[field] = float(round(number, 6 if field == "target_roas" else 2))
    return clean


def _goal(row: Mapping[str, Any]) -> Goal:
    return Goal(
        account_alias=str(row["account_alias"]),
        effective_from=row["effective_from"],
        target_cpa=None if row["target_cpa"] is None else float(row["target_cpa"]),
        target_roas=None if row["target_roas"] is None else float(row["target_roas"]),
        monthly_budget=None if row["monthly_budget"] is None else float(row["monthly_budget"]),
        notes=row["notes"],
        source=str(row["source"]),
        proposal_id=row["proposal_id"],
        set_at=row["set_at"],
    )


class GoalStore:
    def __init__(self, store: Store, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._store = store
        self._clock = clock

    def current(self, account_alias: str, day: date) -> Goal | None:
        rows = self._store.fetch_dicts(
            "SELECT * FROM account_goals WHERE account_alias = ? AND effective_from <= ? "
            "ORDER BY effective_from DESC LIMIT 1",
            [account_alias, day],
        )
        return _goal(rows[0]) if rows else None

    def history(self, account_alias: str | None = None) -> list[Goal]:
        rows = self._store.fetch_dicts(
            "SELECT * FROM account_goals WHERE ? IS NULL OR account_alias = ? "
            "ORDER BY account_alias, effective_from",
            [account_alias, account_alias],
        )
        return [_goal(r) for r in rows]

    def set(
        self,
        account_alias: str,
        changes: Mapping[str, JsonValue],
        *,
        effective_from: date,
        source: str,
        proposal_id: UUID | None = None,
        notes: str | None = _UNSET,
    ) -> Goal:
        """Merge `changes` into the goal in force on `effective_from` and store the new version."""
        clean = validate_changes(changes) if changes else {}
        if not clean and notes is _UNSET:
            raise GoalError("no goal fields to change")
        base = self.current(account_alias, effective_from)
        values = base.values() if base else dict.fromkeys(GOAL_FIELDS)
        values.update(clean)
        goal = Goal(
            account_alias=account_alias,
            effective_from=effective_from,
            notes=(base.notes if base else None) if notes is _UNSET else notes,
            source=source,
            proposal_id=proposal_id,
            set_at=self._clock().replace(tzinfo=None),
            **values,
        )
        with self._store.transaction() as cursor:
            cursor.execute(
                "DELETE FROM account_goals WHERE account_alias = ? AND effective_from = ?",
                [account_alias, effective_from],
            )
            cursor.execute(
                "INSERT INTO account_goals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    goal.account_alias,
                    goal.effective_from,
                    goal.target_cpa,
                    goal.target_roas,
                    goal.monthly_budget,
                    goal.notes,
                    goal.source,
                    goal.proposal_id,
                    goal.set_at,
                ],
            )
        return goal

    def lookup(self) -> GoalLookup:
        return self.current


def update_goals(
    store: Store,
    accounts: AccountRegistry,
    alias: str,
    *,
    values: Mapping[str, float | None],
    clear: Sequence[str] = (),
    effective_from: date | None = None,
    notes: str | None = _UNSET,
    source: str,
) -> Goal:
    """A person's direct change (CLI, console, API): set some fields, clear others, from a day."""
    if accounts.resolve(alias) is None:
        raise GoalError(f"unknown account alias {alias}")
    changes: dict[str, JsonValue] = {k: v for k, v in values.items() if v is not None}
    for name in clear:
        if name in changes:
            raise GoalError(f"{name} is both set and cleared")
        changes[name] = None
    return GoalStore(store).set(
        alias,
        changes,
        effective_from=effective_from or account_today(accounts, alias),
        source=source,
        notes=notes,
    )


def goal_line(
    label: str, value: float | None, target: float | None, *, lower_is_better: bool
) -> str:
    """'CPA 42.10 against a 35.00 target: 20% above target (worse)', or '' without both."""
    if value is None or target is None:
        return ""
    gap = value / target - 1
    if abs(gap) < 0.005:
        where = "on target"
    else:
        worse = gap > 0 if lower_is_better else gap < 0
        where = (
            f"{abs(gap):.0%} {'above' if gap > 0 else 'below'} target "
            f"({'worse' if worse else 'better'})"
        )
    return f"{label} {value:,.2f} against a {target:,.2f} target: {where}"
