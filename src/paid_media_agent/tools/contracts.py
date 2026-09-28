"""Read contracts: which provider tools give daily performance and campaign settings, and how.

Sync, reports, the read dispatcher, and `doctor --live` all go through a contract instead of
assuming tool names or payload shapes. For each platform the first contract whose performance
tool is an authorized read in the current catalog is used, so the fixture catalog and the direct
adapters keep the `rows` shape this repository produces, and a live Pipeboard catalog gets the
contract written for Pipeboard's tools.

Every contract says how far it is verified:
- `host`: the payload is produced by this repository (fixtures, direct adapters).
- `published_source`: built from the provider's published server source, not yet seen live.
- `unverified`: tool names are documented, argument and response shapes are not.

Contracts turn provider payloads into the flat rows `normalize_rows` reads and into a settings
payload whose budgets are already in account currency per day.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal

from paid_media_agent.config import AccountBinding
from paid_media_agent.domain.common import EntityType, JsonValue, Platform
from paid_media_agent.domain.money import MoneyUnit, to_currency
from paid_media_agent.tools.catalog import AuthorizedToolCatalog, ToolClass, qualified_name

Verification = Literal["host", "published_source", "unverified"]


@dataclass(frozen=True)
class ReadCall:
    """One provider call a contract asks for. Arguments never carry the account id."""

    tool: str
    arguments: dict[str, JsonValue]
    window: tuple[date, date] | None = None


@dataclass(frozen=True)
class ContractRows:
    rows: list[dict[str, JsonValue]]
    entity_type: EntityType | None = None
    """None lets the tool name decide (e.g. `get_ad_group_performance`)."""
    entity_names: dict[str, str] = field(default_factory=dict)
    observed: dict[str, JsonValue] = field(default_factory=dict)
    """Facts worth showing, e.g. the Meta action types seen when no conversion action is set."""


def _decimal(value: JsonValue) -> Decimal | None:
    if value is None or isinstance(value, (bool, dict, list)):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _window(start: JsonValue, end: JsonValue) -> tuple[date, date] | None:
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        return date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        return None


def _dicts(value: JsonValue) -> list[dict[str, JsonValue]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


class ReadContract:
    """The host-side `rows` shape: fixtures and direct adapters. Other contracts override."""

    name = "rows"
    verification: Verification = "host"
    performance_tool = "get_campaign_performance"
    settings_tool: str | None = "list_campaigns"
    extra_tools: tuple[str, ...] = ()
    """Other tools whose payloads this contract understands (e.g. a single-campaign read)."""
    reviewed_reads: tuple[str, ...] = ()
    """Tools admitted as reads although their server sends no readOnlyHint, reviewed against the
    server's source. Each only issues provider GET or search requests."""
    required_args: Mapping[str, tuple[str, ...]] = {}
    """Arguments each tool's live schema must have for this contract to work."""
    budget_unit: MoneyUnit = "currency"
    resync_days: int | None = None
    """Trailing days a sync re-pulls when every day costs a call; None keeps the configured days."""

    def tools(self) -> tuple[str, ...]:
        names = [self.performance_tool, *self.extra_tools]
        if self.settings_tool:
            names.append(self.settings_tool)
        return tuple(dict.fromkeys(names))

    def per_day(self, schema: Mapping[str, JsonValue]) -> bool:  # noqa: ARG002
        return False

    def performance_calls(
        self, start: date, end: date, schema: Mapping[str, JsonValue]
    ) -> list[ReadCall]:
        del schema
        return [
            ReadCall(
                self.performance_tool,
                {"start_date": start.isoformat(), "end_date": end.isoformat()},
                (start, end),
            )
        ]

    def settings_call(self) -> ReadCall | None:
        return ReadCall(self.settings_tool, {}) if self.settings_tool else None

    def requested_window(self, arguments: Mapping[str, JsonValue]) -> tuple[date, date] | None:
        return _window(arguments.get("start_date"), arguments.get("end_date"))

    def rows(
        self, tool: str, payload: Mapping[str, JsonValue], binding: AccountBinding
    ) -> ContractRows | None:
        """Flat daily rows, or None when the payload is not daily performance."""
        del tool, binding
        rows = _dicts(payload.get("rows"))
        if not rows:
            return None
        names = payload.get("entity_names")
        declared = payload.get("entity_type")
        try:
            entity_type = EntityType(declared) if isinstance(declared, str) else None
        except ValueError:
            entity_type = None
        return ContractRows(
            rows=rows,
            entity_type=entity_type,
            entity_names={str(k): str(v) for k, v in names.items()}
            if isinstance(names, dict)
            else {},
        )

    def settings(
        self, tool: str, payload: Mapping[str, JsonValue], binding: AccountBinding
    ) -> Mapping[str, JsonValue]:
        """A `{"campaigns": [...]}` payload with budgets in account currency per day."""
        del tool, binding
        return payload

    def next_page(
        self, tool: str, payload: Mapping[str, JsonValue], arguments: Mapping[str, JsonValue]
    ) -> dict[str, JsonValue] | None:
        del tool, payload, arguments
        return None


class MetaInsightsContract(ReadContract):
    """Pipeboard's Meta server, from `pipeboard-co/meta-ads-mcp` @ 093062c6 (2026-09-23).

    `get_insights` has no `time_increment`, so a row covers the whole requested range: daily
    history takes one call per day. Graph returns numbers as strings, conversions inside
    `actions` / `action_values`, and budgets in the currency's minor unit.
    """

    name = "pipeboard_meta"
    verification: Verification = "published_source"
    performance_tool = "get_insights"
    settings_tool = "get_campaigns"
    extra_tools = ("get_campaign_details",)
    reviewed_reads = ("get_insights", "get_campaigns")
    required_args = {
        "get_insights": ("account_id", "time_range", "level", "limit", "after"),
        "get_campaigns": ("account_id", "limit", "after"),
    }
    budget_unit: MoneyUnit = "minor"
    resync_days = 8
    page_size = 500

    def per_day(self, schema: Mapping[str, JsonValue]) -> bool:
        properties = schema.get("properties")
        return not (isinstance(properties, dict) and "time_increment" in properties)

    def performance_calls(
        self, start: date, end: date, schema: Mapping[str, JsonValue]
    ) -> list[ReadCall]:
        base: dict[str, JsonValue] = {"level": "campaign", "limit": self.page_size}
        if not self.per_day(schema):
            return [
                ReadCall(
                    self.performance_tool,
                    {
                        **base,
                        "time_range": {"since": start.isoformat(), "until": end.isoformat()},
                        "time_increment": 1,
                    },
                    (start, end),
                )
            ]
        calls: list[ReadCall] = []
        day = start
        while day <= end:
            calls.append(
                ReadCall(
                    self.performance_tool,
                    {**base, "time_range": {"since": day.isoformat(), "until": day.isoformat()}},
                    (day, day),
                )
            )
            day += timedelta(days=1)
        return calls

    def settings_call(self) -> ReadCall | None:
        return ReadCall(self.settings_tool, {"limit": 100})

    def requested_window(self, arguments: Mapping[str, JsonValue]) -> tuple[date, date] | None:
        span = arguments.get("time_range")
        if isinstance(span, str) and span.startswith("{"):
            try:
                span = json.loads(span)
            except json.JSONDecodeError:
                return None
        return _window(span.get("since"), span.get("until")) if isinstance(span, dict) else None

    def rows(
        self, tool: str, payload: Mapping[str, JsonValue], binding: AccountBinding
    ) -> ContractRows | None:
        if tool != self.performance_tool:
            return None
        items = _dicts(payload.get("data"))
        if not items or any(i.get("date_start") != i.get("date_stop") for i in items):
            return None  # empty, or a multi-day total: not daily history
        entity_type = (
            EntityType.AD
            if any("ad_id" in i for i in items)
            else EntityType.AD_GROUP
            if any("adset_id" in i for i in items)
            else EntityType.CAMPAIGN
        )
        action = binding.conversion_action
        seen: set[str] = set()
        rows: list[dict[str, JsonValue]] = []
        for item in items:
            actions = _dicts(item.get("actions"))
            values = _dicts(item.get("action_values"))
            seen.update(str(a.get("action_type")) for a in actions if a.get("action_type"))
            row: dict[str, JsonValue] = {
                k: v for k, v in item.items() if not isinstance(v, (dict, list))
            }
            row["date"] = item.get("date_start")
            row["actions"] = actions
            row["action_values"] = values
            if action is not None:
                # Meta omits action types with nothing to report, so absent means zero.
                row["conversions"] = str(_action_total(actions, action))
                row["conversion_value"] = str(_action_total(values, action))
            rows.append(row)
        observed: dict[str, JsonValue] = {}
        if action is None:
            observed["conversion_action"] = "not set; conversions are recorded as missing"
            observed["action_types"] = sorted(seen)[:40]
        return ContractRows(rows=rows, entity_type=entity_type, observed=observed)

    def settings(
        self, tool: str, payload: Mapping[str, JsonValue], binding: AccountBinding
    ) -> Mapping[str, JsonValue]:
        if tool not in (self.settings_tool, *self.extra_tools):
            return payload
        items = _dicts(payload.get("data")) or ([dict(payload)] if "id" in payload else [])
        campaigns: list[JsonValue] = []
        for item in items:
            daily = _decimal(item.get("daily_budget"))
            lifetime = _decimal(item.get("lifetime_budget"))
            campaign: dict[str, JsonValue] = {
                k: v for k, v in item.items() if k not in ("daily_budget", "lifetime_budget")
            }
            campaign["daily_budget"] = (
                str(to_currency(daily, "minor", binding.currency)) if daily else None
            )
            campaign["budget_type"] = (
                "daily" if daily else "lifetime" if lifetime else "ad_set_budgets"
            )
            campaign["provider_daily_budget"] = item.get("daily_budget")
            campaign["provider_lifetime_budget"] = item.get("lifetime_budget")
            campaigns.append(campaign)
        return {"campaigns": campaigns}

    def next_page(
        self, tool: str, payload: Mapping[str, JsonValue], arguments: Mapping[str, JsonValue]
    ) -> dict[str, JsonValue] | None:
        del tool
        paging = payload.get("paging")
        if not isinstance(paging, dict) or not paging.get("next"):
            return None
        cursors = paging.get("cursors")
        after = cursors.get("after") if isinstance(cursors, dict) else None
        return {**arguments, "after": after} if isinstance(after, str) and after else None


def _action_total(actions: Sequence[Mapping[str, JsonValue]], action_type: str) -> Decimal:
    total = Decimal(0)
    for action in actions:
        if action.get("action_type") == action_type:
            total += _decimal(action.get("value")) or Decimal(0)
    return total


_GAQL_PERFORMANCE = (
    "SELECT campaign.id, campaign.name, segments.date, metrics.cost_micros, "
    "metrics.impressions, metrics.clicks, metrics.conversions, metrics.conversions_value "
    "FROM campaign WHERE segments.date BETWEEN '{start}' AND '{end}'"
)
_GAQL_SETTINGS = (
    "SELECT campaign.id, campaign.name, campaign.status, campaign.bidding_strategy_type, "
    "campaign_budget.amount_micros, campaign_budget.period, campaign_budget.explicitly_shared, "
    "campaign.target_cpa.target_cpa_micros, campaign.maximize_conversions.target_cpa_micros, "
    "campaign.target_roas.target_roas, campaign.maximize_conversion_value.target_roas "
    "FROM campaign WHERE campaign.status != 'REMOVED'"
)
_GOOGLE_STATUS = {2: "ENABLED", 3: "PAUSED", 4: "REMOVED"}
_GOOGLE_PERIOD = {2: "daily", 5: "custom_period"}
"""Enum numbers, for a server that passes protobuf values through instead of names."""
_GAQL_BETWEEN = re.compile(
    r"segments\.date\s+BETWEEN\s+'(\d{4}-\d{2}-\d{2})'\s+AND\s+'(\d{4}-\d{2}-\d{2})'", re.I
)
_RESULT_KEYS = ("results", "rows", "data", "items", "result")
_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def _snake(key: str) -> str:
    return ".".join(_CAMEL.sub("_", part).lower() for part in key.split("."))


def flatten(item: Mapping[str, JsonValue], prefix: str = "") -> dict[str, JsonValue]:
    """Nested or dotted, camelCase or snake_case: one level of `a.b_c` keys."""
    flat: dict[str, JsonValue] = {}
    for key, value in item.items():
        name = _snake(f"{prefix}{key}")
        if isinstance(value, dict):
            flat.update(flatten(value, f"{name}."))
        else:
            flat[name] = value
    return flat


def _gaql_rows(payload: Mapping[str, JsonValue]) -> list[dict[str, JsonValue]]:
    for key in _RESULT_KEYS:
        rows = _dicts(payload.get(key))
        if rows:
            return [flatten(r) for r in rows]
    return []


class GoogleGaqlContract(ReadContract):
    """Pipeboard's Google Ads server through its GAQL tool. Names are confirmed by Pipeboard's CLI
    (`customer_id` as ten digits, `query`); the response shape is not, so rows are flattened from
    whichever of the nested or dotted, camelCase or snake_case shapes arrives."""

    name = "pipeboard_google_gaql"
    verification: Verification = "unverified"
    performance_tool = "execute_google_ads_gaql_query"
    settings_tool = "execute_google_ads_gaql_query"
    reviewed_reads = ("execute_google_ads_gaql_query",)
    required_args = {"execute_google_ads_gaql_query": ("customer_id", "query")}
    budget_unit: MoneyUnit = "micros"

    def performance_calls(
        self, start: date, end: date, schema: Mapping[str, JsonValue]
    ) -> list[ReadCall]:
        del schema
        query = _GAQL_PERFORMANCE.format(start=start.isoformat(), end=end.isoformat())
        return [ReadCall(self.performance_tool, {"query": query}, (start, end))]

    def settings_call(self) -> ReadCall | None:
        return ReadCall(self.settings_tool, {"query": _GAQL_SETTINGS})

    def requested_window(self, arguments: Mapping[str, JsonValue]) -> tuple[date, date] | None:
        query = arguments.get("query")
        found = _GAQL_BETWEEN.search(query) if isinstance(query, str) else None
        return _window(found.group(1), found.group(2)) if found else None

    def rows(
        self, tool: str, payload: Mapping[str, JsonValue], binding: AccountBinding
    ) -> ContractRows | None:
        del binding
        if tool != self.performance_tool:
            return None
        flat = _gaql_rows(payload)
        needed = ("campaign.id", "segments.date", "metrics.cost_micros")
        if not flat or not all(all(k in r for k in needed) for r in flat):
            return None
        rows: list[dict[str, JsonValue]] = [
            {
                "date": r["segments.date"],
                "campaign_id": str(r["campaign.id"]),
                "campaign_name": r.get("campaign.name"),
                "cost_micros": r["metrics.cost_micros"],
                "impressions": r.get("metrics.impressions"),
                "clicks": r.get("metrics.clicks"),
                "conversions": r.get("metrics.conversions"),
                "conversion_value": r.get("metrics.conversions_value"),
            }
            for r in flat
        ]
        return ContractRows(rows=rows, entity_type=EntityType.CAMPAIGN)

    def settings(
        self, tool: str, payload: Mapping[str, JsonValue], binding: AccountBinding
    ) -> Mapping[str, JsonValue]:
        if tool != self.settings_tool:
            return payload
        flat = [
            r
            for r in _gaql_rows(payload)
            if "campaign.id" in r and "campaign.status" in r and "segments.date" not in r
        ]
        if not flat:
            return payload
        campaigns: list[JsonValue] = []
        for r in flat:
            status = r.get("campaign.status")
            if isinstance(status, int):
                status = _GOOGLE_STATUS.get(status, str(status))
            shared = r.get("campaign_budget.explicitly_shared") is True
            raw_period = r.get("campaign_budget.period") or "DAILY"
            period = (
                _GOOGLE_PERIOD.get(raw_period, str(raw_period))
                if isinstance(raw_period, int)
                else str(raw_period).lower()
            )
            micros = _decimal(r.get("campaign_budget.amount_micros"))
            daily = (
                to_currency(micros, "micros", binding.currency)
                if micros is not None and not shared and period == "daily"
                else None
            )
            cpa = _decimal(
                r.get("campaign.target_cpa.target_cpa_micros")
                or r.get("campaign.maximize_conversions.target_cpa_micros")
            )
            roas = _decimal(
                r.get("campaign.target_roas.target_roas")
                or r.get("campaign.maximize_conversion_value.target_roas")
            )
            campaigns.append(
                {
                    "id": str(r["campaign.id"]),
                    "name": r.get("campaign.name"),
                    "status": status,
                    "daily_budget": None if daily is None else str(daily),
                    "budget_type": "shared" if shared else period,
                    "bid_strategy": r.get("campaign.bidding_strategy_type"),
                    "target_cpa": None
                    if cpa is None
                    else str(to_currency(cpa, "micros", binding.currency)),
                    "target_roas": None if roas is None else str(roas),
                    "provider_budget_micros": r.get("campaign_budget.amount_micros"),
                }
            )
        return {"campaigns": campaigns}


HOST_CONTRACT = ReadContract()
_CONTRACTS: dict[Platform, tuple[ReadContract, ...]] = {
    Platform.META_ADS: (MetaInsightsContract(),),
    Platform.GOOGLE_ADS: (GoogleGaqlContract(),),
}


def candidates(platform: Platform) -> tuple[ReadContract, ...]:
    return (*_CONTRACTS.get(platform, ()), HOST_CONTRACT)


def contract_for(catalog: AuthorizedToolCatalog, platform: Platform) -> ReadContract | None:
    """The first contract whose performance tool is an authorized read in `catalog`."""
    for contract in candidates(platform):
        entry = catalog.get(qualified_name(platform, contract.performance_tool))
        if entry is not None and entry.tool_class is ToolClass.READ:
            return contract
    return None


def contract_for_tool(
    catalog: AuthorizedToolCatalog, platform: Platform, tool: str
) -> ReadContract | None:
    contract = contract_for(catalog, platform)
    return contract if contract is not None and tool in contract.tools() else None


def reviewed_reads() -> tuple[str, ...]:
    """Qualified names admitted as reads without a readOnlyHint (see `ReadContract`)."""
    return tuple(
        qualified_name(platform, tool)
        for platform, contracts in _CONTRACTS.items()
        for contract in contracts
        for tool in contract.reviewed_reads
    )


def schema_gaps(
    contract: ReadContract, catalog: AuthorizedToolCatalog, platform: Platform
) -> list[str]:
    """What the live catalog lacks for `contract`: missing tools, non-read tools, missing args."""
    gaps: list[str] = []
    for tool in dict.fromkeys((contract.performance_tool, contract.settings_tool or "")):
        if not tool:
            continue
        entry = catalog.get(qualified_name(platform, tool))
        if entry is None:
            gaps.append(f"{tool}: not in the catalog")
            continue
        if entry.tool_class is not ToolClass.READ:
            gaps.append(f"{tool}: not an authorized read ({entry.policy.reason})")
            continue
        properties = entry.input_schema.get("properties")
        names = set(properties) if isinstance(properties, dict) else set()
        missing = [a for a in contract.required_args.get(tool, ()) if a not in names]
        if missing:
            gaps.append(f"{tool}: schema lacks {', '.join(missing)}")
    return gaps
