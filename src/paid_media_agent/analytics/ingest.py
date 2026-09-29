"""Append-only history of what reads returned: pulls, entity-day snapshots, and settings.

Every read through `ReadDispatcher` lands here as well as in its artifact. A pull is one provider
call. Snapshots are never updated: pulling a day again adds a row, so the views can show both the
latest numbers and the matured ones, and how conversions arrived in between (`conversion_lag`).
Settings observations (status, budget, bidding) build a slowly changing history, and a change
nobody proposed through this agent is recorded as `external_detected`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import duckdb

from paid_media_agent.config import AccountBinding
from paid_media_agent.domain.common import DataQualityFlag, EntityType, JsonValue
from paid_media_agent.domain.metrics import PerformanceRow
from paid_media_agent.store.db import Store, json_rows, utc_now

AnalyticsSource = Literal["agent_read", "report", "sync", "backfill", "simulator", "doctor"]
SETTINGS_FIELDS = ("status", "daily_budget")
"""Fields whose changes are logged as change events when nobody proposed them here."""

_SNAPSHOT_SHAPE = {
    "entity_type": "VARCHAR",
    "entity_ref": "VARCHAR",
    "day": "DATE",
    "entity_name": "VARCHAR",
    "currency": "VARCHAR",
    "spend": "DECIMAL(18,4)",
    "impressions": "BIGINT",
    "clicks": "BIGINT",
    "conversions": "DECIMAL(18,4)",
    "conversion_value": "DECIMAL(18,4)",
    "is_complete": "BOOLEAN",
    "quality_flags": "VARCHAR[]",
}
_SETTINGS_SHAPE = {
    "entity_ref": "VARCHAR",
    "entity_name": "VARCHAR",
    "status": "VARCHAR",
    "daily_budget": "DECIMAL(18,4)",
    "budget_type": "VARCHAR",
    "bid_strategy": "VARCHAR",
    "target_cpa": "DECIMAL(18,4)",
    "target_roas": "DECIMAL(18,6)",
    "raw": "VARCHAR",
}
_SIGNAL_SHAPE = {
    "entity_ref": "VARCHAR",
    "day": "DATE",
    "impression_share": "DOUBLE",
    "budget_lost_share": "DOUBLE",
    "rank_lost_share": "DOUBLE",
}
_STATUS_SHAPE = {
    "entity_ref": "VARCHAR",
    "channel_type": "VARCHAR",
    "status_reasons": "VARCHAR[]",
    "bidding_status": "VARCHAR",
    "learning_status": "VARCHAR",
    "recommended_budget": "DECIMAL(18,4)",
    "raw": "VARCHAR",
}
_ID_KEYS = ("id", "campaign_id")
_NAME_KEYS = ("name", "campaign_name")
_STATUS_KEYS = ("status", "effective_status", "configured_status")
_BUDGET_KEYS: tuple[tuple[str, Decimal], ...] = (
    ("daily_budget", Decimal(1)),
    ("daily_budget_amount", Decimal(1)),
    ("budget_amount", Decimal(1)),
    ("daily_budget_micros", Decimal(1_000_000)),
    ("budget_amount_micros", Decimal(1_000_000)),
    ("amount_micros", Decimal(1_000_000)),
)
"""Budgets in account currency per day, the unit the write policy uses. Anything else stays in
`raw` until its unit is verified."""
_BUDGET_TYPE_KEYS = ("budget_type", "budget_period", "budget_delivery_method")
_BID_KEYS = ("bid_strategy", "bidding_strategy_type", "bidding_strategy", "bid_type")
_TARGET_ROAS_KEYS = ("target_roas",)


@dataclass(frozen=True)
class SettingsRecord:
    pull_id: UUID
    entities: int
    external_changes: int


def local_date(moment: datetime, timezone: str) -> date:
    """The calendar day `moment` (naive UTC) falls on in the account's timezone."""
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    return moment.replace(tzinfo=UTC).astimezone(zone).date()


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decimal(value: JsonValue) -> Decimal | None:
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list)):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _first(item: Mapping[str, JsonValue], keys: Sequence[str]) -> JsonValue:
    for key in keys:
        value = item.get(key)
        if value is not None:
            return value
    return None


def _string(value: JsonValue) -> str | None:
    return None if value is None or isinstance(value, (dict, list)) else str(value)


def _json_setting(field: str, value: JsonValue) -> JsonValue:
    if field == "daily_budget" and value is not None:
        return float(Decimal(str(value)))
    return value


def settings_entities(payload: Mapping[str, JsonValue]) -> list[dict[str, JsonValue]]:
    """Campaign objects in a `list_campaigns` or `get_campaign` style payload."""
    campaigns = payload.get("campaigns")
    if isinstance(campaigns, list):
        return [c for c in campaigns if isinstance(c, dict) and _first(c, _ID_KEYS) is not None]
    campaign = payload.get("campaign")
    if isinstance(campaign, dict) and _first(campaign, _ID_KEYS) is not None:
        return [campaign]
    return []


def settings_row(item: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    budget: Decimal | None = None
    for key, divisor in _BUDGET_KEYS:
        value = _decimal(item.get(key))
        if value is not None:
            budget = value / divisor
            break
    target_cpa = _decimal(item.get("target_cpa"))
    if target_cpa is None and (micros := _decimal(item.get("target_cpa_micros"))) is not None:
        target_cpa = micros / Decimal(1_000_000)
    return {
        "entity_ref": str(_first(item, _ID_KEYS)),
        "entity_name": _string(_first(item, _NAME_KEYS)),
        "status": _string(_first(item, _STATUS_KEYS)),
        "daily_budget": _text(budget),
        "budget_type": _string(_first(item, _BUDGET_TYPE_KEYS)),
        "bid_strategy": _string(_first(item, _BID_KEYS)),
        "target_cpa": _text(target_cpa),
        "target_roas": _text(_decimal(_first(item, _TARGET_ROAS_KEYS))),
        "raw": json.dumps(item, sort_keys=True, default=str),
    }


def _merge_duplicates(rows: Sequence[PerformanceRow]) -> list[dict[str, Any]]:
    """One snapshot row per entity-day. Split rows are summed and flagged, as compute does."""
    merged: dict[tuple[str, date], dict[str, Any]] = {}
    for row in rows:
        key = (row.entity_ref, row.window.start)
        current = merged.get(key)
        flags = {flag.value for flag in row.quality_flags}
        if current is None:
            merged[key] = {
                "entity_type": row.entity_type.value,
                "entity_ref": row.entity_ref,
                "day": row.window.start,
                "entity_name": row.entity_name,
                "currency": row.currency,
                "spend": row.spend,
                "impressions": row.impressions,
                "clicks": row.clicks,
                "conversions": row.conversions,
                "conversion_value": row.conversion_value,
                "is_complete": row.window.is_complete,
                "quality_flags": flags,
            }
            continue
        current["spend"] += row.spend
        for metric in ("impressions", "clicks", "conversions", "conversion_value"):
            left, right = current[metric], getattr(row, metric)
            current[metric] = None if left is None or right is None else left + right
        current["is_complete"] = current["is_complete"] and row.window.is_complete
        current["quality_flags"] = (
            current["quality_flags"] | flags | {DataQualityFlag.DUPLICATE_ROWS.value}
        )
    return [
        {
            **row,
            "day": row["day"].isoformat(),
            "spend": str(row["spend"]),
            "conversions": _text(row["conversions"]),
            "conversion_value": _text(row["conversion_value"]),
            "quality_flags": sorted(row["quality_flags"]),
        }
        for row in merged.values()
    ]


class AnalyticsRecorder:
    """Writes pulls and snapshots to the state store. It never changes an existing row."""

    def __init__(self, store: Store, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._store = store
        self._clock = clock

    @property
    def store(self) -> Store:
        return self._store

    def _insert_pull(
        self,
        cursor: duckdb.DuckDBPyConnection,
        *,
        pull_id: UUID,
        source: AnalyticsSource,
        binding: AccountBinding,
        tool_name: str,
        catalog_revision: str | None,
        entity_type: str,
        requested: tuple[date, date] | None,
        actual: tuple[date, date] | None,
        data_complete_through: date | None,
        artifact_id: str | None,
        row_count: int,
        quality_flags: Sequence[str],
        pulled_at: datetime,
    ) -> None:
        cursor.execute(
            "INSERT INTO pulls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                pull_id,
                source,
                tool_name,
                catalog_revision,
                binding.platform.value,
                binding.provider_account_id,
                binding.alias,
                entity_type,
                requested[0] if requested else None,
                requested[1] if requested else None,
                actual[0] if actual else None,
                actual[1] if actual else None,
                data_complete_through,
                artifact_id,
                row_count,
                sorted(quality_flags),
                pulled_at,
                local_date(pulled_at, binding.timezone),
            ],
        )

    def record_performance(
        self,
        *,
        source: AnalyticsSource,
        binding: AccountBinding,
        tool_name: str,
        catalog_revision: str | None,
        rows: Sequence[PerformanceRow],
        requested: tuple[date, date] | None = None,
        data_complete_through: date | None = None,
        artifact_id: str | None = None,
        quality_flags: Sequence[str] = (),
        pulled_at: datetime | None = None,
    ) -> UUID:
        """Record one performance read: a pull row plus one snapshot per entity-day."""
        pull_id = uuid.uuid4()
        at = pulled_at or self._clock()
        snapshots = _merge_duplicates(rows)
        days = sorted(date.fromisoformat(r["day"]) for r in snapshots)
        entity_type = rows[0].entity_type.value if rows else EntityType.CAMPAIGN.value
        columns = ", ".join(_SNAPSHOT_SHAPE)
        with self._store.transaction() as cursor:
            self._insert_pull(
                cursor,
                pull_id=pull_id,
                source=source,
                binding=binding,
                tool_name=tool_name,
                catalog_revision=catalog_revision,
                entity_type=entity_type,
                requested=requested,
                actual=(days[0], days[-1]) if days else None,
                data_complete_through=data_complete_through,
                artifact_id=artifact_id,
                row_count=len(snapshots),
                quality_flags=quality_flags,
                pulled_at=at,
            )
            if snapshots:
                cursor.execute(
                    f"INSERT INTO entity_daily_snapshots (platform, provider_account_id, "  # noqa: S608
                    f"pull_id, pulled_at, pulled_on, account_alias, {columns}) "
                    f"SELECT ?, ?, ?, ?, ?, ?, {columns} FROM ({json_rows(_SNAPSHOT_SHAPE)}) "
                    "ON CONFLICT DO NOTHING",
                    [
                        binding.platform.value,
                        binding.provider_account_id,
                        pull_id,
                        at,
                        local_date(at, binding.timezone),
                        binding.alias,
                        json.dumps(snapshots),
                    ],
                )
        return pull_id

    def record_settings(
        self,
        *,
        source: AnalyticsSource,
        binding: AccountBinding,
        tool_name: str,
        catalog_revision: str | None,
        payload: Mapping[str, JsonValue],
        artifact_id: str | None = None,
        observed_at: datetime | None = None,
        currency: str | None = None,
    ) -> SettingsRecord | None:
        """Record campaign settings from a campaign read, and detect changes made elsewhere."""
        entities = [settings_row(item) for item in settings_entities(payload)]
        if not entities:
            return None
        pull_id = uuid.uuid4()
        at = observed_at or self._clock()
        with self._store.transaction() as cursor:
            previous = self._previous_settings(cursor, binding, [e["entity_ref"] for e in entities])
            self._insert_pull(
                cursor,
                pull_id=pull_id,
                source=source,
                binding=binding,
                tool_name=tool_name,
                catalog_revision=catalog_revision,
                entity_type=EntityType.CAMPAIGN.value,
                requested=None,
                actual=None,
                data_complete_through=None,
                artifact_id=artifact_id,
                row_count=len(entities),
                quality_flags=(),
                pulled_at=at,
            )
            columns = ", ".join(_SETTINGS_SHAPE)
            cursor.execute(
                f"INSERT INTO entity_settings_snapshots (platform, provider_account_id, "  # noqa: S608
                f"entity_type, observed_at, pull_id, account_alias, currency, {columns}) "
                f"SELECT ?, ?, ?, ?, ?, ?, ?, {columns} FROM ({json_rows(_SETTINGS_SHAPE)}) "
                "ON CONFLICT DO NOTHING",
                [
                    binding.platform.value,
                    binding.provider_account_id,
                    EntityType.CAMPAIGN.value,
                    at,
                    pull_id,
                    binding.alias,
                    currency or binding.currency,
                    json.dumps(entities),
                ],
            )
            external = self._external_changes(cursor, binding, entities, previous, at)
        return SettingsRecord(pull_id=pull_id, entities=len(entities), external_changes=external)

    def record_signals(
        self,
        *,
        source: AnalyticsSource,
        binding: AccountBinding,
        tool_name: str,
        catalog_revision: str | None,
        daily: Sequence[Mapping[str, JsonValue]],
        status: Sequence[Mapping[str, JsonValue]],
        artifact_id: str | None = None,
        observed_at: datetime | None = None,
    ) -> UUID | None:
        """Record what the platform says limits spend: daily auction shares and current status."""
        if not daily and not status:
            return None
        pull_id = uuid.uuid4()
        at = observed_at or self._clock()
        days = sorted(date.fromisoformat(str(r["day"])) for r in daily)
        with self._store.transaction() as cursor:
            self._insert_pull(
                cursor,
                pull_id=pull_id,
                source=source,
                binding=binding,
                tool_name=tool_name,
                catalog_revision=catalog_revision,
                entity_type=EntityType.CAMPAIGN.value,
                requested=None,
                actual=(days[0], days[-1]) if days else None,
                data_complete_through=None,
                artifact_id=artifact_id,
                row_count=len(daily) + len(status),
                quality_flags=("signals",),
                pulled_at=at,
            )
            key = [binding.platform.value, binding.provider_account_id, EntityType.CAMPAIGN.value]
            if daily:
                columns = ", ".join(_SIGNAL_SHAPE)
                cursor.execute(
                    f"INSERT INTO entity_daily_signals (platform, provider_account_id, "  # noqa: S608
                    f"entity_type, pull_id, pulled_at, account_alias, {columns}) "
                    f"SELECT ?, ?, ?, ?, ?, ?, {columns} FROM ({json_rows(_SIGNAL_SHAPE)}) "
                    "ON CONFLICT DO NOTHING",
                    [*key, pull_id, at, binding.alias, json.dumps(list(daily), default=str)],
                )
            if status:
                rows = [
                    {**{k: item.get(k) for k in _STATUS_SHAPE if k != "raw"},
                     "status_reasons": list(item.get("status_reasons") or []),
                     "raw": json.dumps(item.get("raw") or {}, sort_keys=True, default=str)}
                    for item in status
                ]  # fmt: skip
                columns = ", ".join(_STATUS_SHAPE)
                cursor.execute(
                    f"INSERT INTO entity_delivery_status (platform, provider_account_id, "  # noqa: S608
                    f"entity_type, observed_at, pull_id, account_alias, {columns}) "
                    f"SELECT ?, ?, ?, ?, ?, ?, {columns} FROM ({json_rows(_STATUS_SHAPE)}) "
                    "ON CONFLICT DO NOTHING",
                    [*key, at, pull_id, binding.alias, json.dumps(rows, default=str)],
                )
        return pull_id

    def _previous_settings(
        self, cursor: duckdb.DuckDBPyConnection, binding: AccountBinding, refs: Sequence[str]
    ) -> dict[str, tuple[datetime, dict[str, str | None]]]:
        rows = cursor.execute(
            "SELECT entity_ref, observed_at, status, CAST(daily_budget AS VARCHAR) "
            "FROM entity_settings_snapshots "
            "WHERE platform = ? AND provider_account_id = ? AND entity_type = 'campaign' "
            "AND list_contains(?, entity_ref) "
            "QUALIFY row_number() OVER (PARTITION BY entity_ref ORDER BY observed_at DESC) = 1",
            [binding.platform.value, binding.provider_account_id, list(refs)],
        ).fetchall()
        return {
            ref: (observed, {"status": status, "daily_budget": budget})
            for ref, observed, status, budget in rows
        }

    def _external_changes(
        self,
        cursor: duckdb.DuckDBPyConnection,
        binding: AccountBinding,
        entities: Sequence[Mapping[str, JsonValue]],
        previous: Mapping[str, tuple[datetime, dict[str, str | None]]],
        at: datetime,
    ) -> int:
        count = 0
        for entity in entities:
            ref = str(entity["entity_ref"])
            if ref not in previous:
                continue
            since, before = previous[ref]
            for field in SETTINGS_FIELDS:
                old, new = before[field], entity[field]
                if field == "daily_budget":
                    same = (old is None and new is None) or (
                        old is not None
                        and new is not None
                        and Decimal(str(old)) == Decimal(str(new))
                    )
                else:
                    same = old == new
                if same:
                    continue
                explained = cursor.execute(
                    "SELECT count(*) FROM change_events WHERE source = 'agent' "
                    "AND status = 'verified' AND platform = ? AND provider_account_id = ? "
                    "AND entity_ref = ? AND field = ? AND occurred_at BETWEEN ? AND ?",
                    [binding.platform.value, binding.provider_account_id, ref, field, since, at],
                ).fetchone()
                if explained and explained[0]:
                    continue
                cursor.execute(
                    "INSERT INTO change_events VALUES "
                    "(?, 'external_detected', NULL, NULL, ?, ?, ?, 'campaign', ?, NULL, ?, ?, ?, "
                    "'detected', [], ?)",
                    [
                        uuid.uuid4(),
                        binding.platform.value,
                        binding.provider_account_id,
                        binding.alias,
                        ref,
                        field,
                        json.dumps(_json_setting(field, old)),
                        json.dumps(_json_setting(field, new)),
                        at,
                    ],
                )
                count += 1
        return count
