"""Campaign-days as the stored history stood on a date, for checks and budget models.

Every consumer that asks "what did we know on day D" reads through here: the newest snapshot of
each campaign-day pulled by D, the budget in force that day, and how complete its conversions were
(from the platform's maturity window and each account's measured lag curve).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

from paid_media_agent.store.db import Store


@dataclass(frozen=True)
class PanelRow:
    platform: str
    provider_account_id: str
    account_alias: str
    entity_ref: str
    entity_name: str
    day: date
    spend: float
    conversions: float | None
    is_complete: bool
    age_days: int
    budget: float | None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.platform, self.provider_account_id, self.entity_ref)


def load_panel(
    store: Store, *, as_of: date, since: date, account_alias: str | None = None
) -> list[PanelRow]:
    """Campaign-days as the history stood on `as_of`, with the budget in force each day."""
    alias_clause = "AND account_alias = ?" if account_alias else ""
    params: list[Any] = [as_of, since, *([account_alias] if account_alias else [])]
    rows = store.fetch(
        f"""
        WITH snap AS (
            SELECT platform, provider_account_id, account_alias, entity_type, entity_ref,
                entity_name, day, spend, conversions, is_complete, pulled_on
            FROM entity_daily_snapshots
            WHERE entity_type = 'campaign' AND pulled_on <= ? AND day >= ? {alias_clause}
            QUALIFY row_number() OVER (
                PARTITION BY platform, provider_account_id, entity_type, entity_ref, day
                ORDER BY pulled_at DESC, pull_id DESC
            ) = 1
        )
        SELECT s.platform, s.provider_account_id, s.account_alias, s.entity_ref, s.entity_name,
            s.day, s.spend::DOUBLE, s.conversions::DOUBLE, s.is_complete, s.pulled_on - s.day,
            st.daily_budget::DOUBLE
        FROM snap s
        ASOF LEFT JOIN entity_settings_snapshots st
            ON st.platform = s.platform
            AND st.provider_account_id = s.provider_account_id
            AND st.entity_type = s.entity_type
            AND st.entity_ref = s.entity_ref
            AND CAST(s.day + 1 AS TIMESTAMP) > st.observed_at
        ORDER BY s.platform, s.provider_account_id, s.entity_ref, s.day
        """,  # noqa: S608 - the only interpolation is a constant clause
        params,
    )
    return [PanelRow(*row) for row in rows]


def maturity_days(store: Store) -> dict[str, int]:
    return {p: int(d) for p, d in store.fetch("SELECT platform, days FROM maturity_days")}


def lag_curves(store: Store) -> dict[str, dict[int, float]]:
    """Share of final conversions reported by each age, per account, where enough days exist."""
    curves: dict[str, dict[int, float]] = defaultdict(dict)
    for account, age, share in store.fetch(
        "SELECT provider_account_id, age_days, completeness::DOUBLE FROM conversion_lag "
        "WHERE entity_days >= 20 AND completeness IS NOT NULL"
    ):
        curves[account][int(age)] = min(1.0, float(share))
    return curves


def completeness(
    row: PanelRow, maturity: dict[str, int], curves: dict[str, dict[int, float]]
) -> float | None:
    """Share of the row's final conversions already reported; None when the lag is unknown."""
    if row.age_days >= maturity.get(row.platform, 7):
        return 1.0
    return curves.get(row.provider_account_id, {}).get(row.age_days)
