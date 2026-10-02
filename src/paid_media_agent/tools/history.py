"""Model-facing `query_history` tool: stored history, through fixed queries only.

Every read the agent, the daily sync, or a backfill made is kept in the state store. This tool
answers from that history: what is stored, the daily panel with budgets and matured conversions,
settings versions, the change log, and how late conversions arrive. The model picks a view and
filters; it never writes SQL, and results name accounts by alias.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from paid_media_agent.analytics.goals import account_today
from paid_media_agent.analytics.history import MAX_ROWS, GroupKey, ModelHistoryView, query_history
from paid_media_agent.analytics.pacing import ACTIVE_STATUSES
from paid_media_agent.config import AccountRegistry
from paid_media_agent.domain.windows import days_ago
from paid_media_agent.harness.tools import ToolContext, ToolSpec, parameters_for
from paid_media_agent.redaction import sanitize_exception
from paid_media_agent.store.db import Store
from paid_media_agent.tools.summary import cross_platform_caveats, platform_comparisons

QUERY_HISTORY_TOOL = "query_history"


class QueryHistoryArgs(BaseModel):
    view: ModelHistoryView = Field(
        description=(
            "coverage: what is stored per account. daily: entity-day rows with budget, pacing, "
            "and matured conversions. settings: budget and status versions. changes: proposal "
            "decisions, executions, and changes detected outside this agent. lag: share of "
            "final conversions reported N days after the day. outcomes: each budget "
            "recommendation, whether it was followed, and matured conversions over the week "
            "after it against what was expected. goals: versions of each account's target CPA, "
            "target ROAS, and monthly budget, and who set them. signals: the platform's daily "
            "impression share and the share lost to budget or to rank. constraints: what each "
            "budget recommendation took to limit each campaign's spend (budget, demand, target, "
            "learning), its spend ceiling, and the evidence."
        )
    )
    account_alias: str | None = Field(default=None, description="Alias from list_accounts.")
    entity_ref: str | None = Field(default=None, description="Campaign or entity id.")
    start_date: date | None = None
    end_date: date | None = None
    limit: int = Field(default=100, ge=1, le=MAX_ROWS)
    group_by: list[GroupKey] | None = Field(
        default=None,
        description=(
            "daily or signals only: one row per account, entity, day, and/or week instead of per "
            "entity-day. daily sums spend, conversions, clicks, impressions, and value, with CPA, "
            "ROAS, CTR, and CVR from the sums; signals averages the shares. Use it instead of "
            "adding up rows."
        ),
    )
    fields: list[str] | None = Field(
        default=None, description="Only these columns in each row, e.g. ['day', 'spend']."
    )


FIELD_NOTES: dict[str, dict[str, str]] = {
    "daily": {
        "pacing_ratio": "spend / daily budget that day; above 1 means it spent more than its "
        "budget that day",
        "conversions_matured": "conversions once the day is old enough to count as final; "
        "empty while it is recent",
    },
    "signals": {
        "budget_lost_share": "share of eligible impressions lost because the budget ran out: "
        "the budget limits the campaign. It is not spend above the budget.",
        "rank_lost_share": "share lost to ad rank (bid or quality): the bid target or ads "
        "limit the campaign, not the budget",
        "impression_share": "share of eligible impressions the campaign won",
    },
    "constraints": {
        "kind": "what limits spend: budget, demand, target (bid target), learning, unknown",
        "ceiling": "the most the campaign can spend a day whatever its budget",
    },
}
"""What the easily misread fields mean, returned with the view."""


def run_query_history(
    store: Store, accounts: AccountRegistry, args: QueryHistoryArgs
) -> dict[str, Any]:
    if args.account_alias is not None and accounts.resolve(args.account_alias) is None:
        raise ValueError(f"unknown account alias {args.account_alias}; call list_accounts")
    rows, truncated = query_history(
        store,
        args.view,
        account_alias=args.account_alias,
        entity_ref=args.entity_ref,
        start=args.start_date,
        end=args.end_date,
        limit=args.limit,
        group_by=args.group_by,
    )
    note = (
        "History holds only what was pulled; missing days were never read. Matured conversions "
        "are final enough to compare; recent days are still arriving."
    )
    # Headline and notes first: a large result is offloaded and shows only its beginning.
    result: dict[str, Any] = {"view": args.view, "row_count": len(rows), "truncated": truncated}
    if args.view in FIELD_NOTES:
        result["notes"] = FIELD_NOTES[args.view]
    if args.view == "settings" and not truncated:
        result["budget_totals"] = _budget_totals(rows)
    if args.view in DATED:
        _add_days_ago(rows, accounts, DATED[args.view])
    if args.group_by:
        result["grouped_by"] = args.group_by
        if args.view == "daily":
            _read_totals(result, rows, args.group_by)
    result["note"] = note
    result["rows"] = _project(rows, args.fields) if args.fields else rows
    return result


def _read_totals(
    result: dict[str, Any], rows: list[dict[str, Any]], group_by: Sequence[str]
) -> None:
    """What grouped totals need said with them: how much is still arriving and, across
    accounts, the comparison and the caveats."""
    for row in rows:
        total, matured = row.get("conversions"), row.get("conversions_matured")
        if total and matured is not None and matured < total - 1e-9:
            row["maturity"] = (
                f"{matured:g} of {total:g} conversions are from days old enough to be final; "
                "the newest days' are still arriving, so CPA may still fall and ROAS rise"
            )
    if list(group_by) != ["account"] or len({r["account_alias"] for r in rows}) < 2:
        return
    headline = [
        {
            "platform": r["platform"],
            "account": r["account_alias"],
            "currency": r["currency"],
            "covered_window": f"{r['first_day']}..{r['last_day']}",
            "cpa": r.get("cpa"),
            "roas": r.get("roas"),
        }
        for r in rows
    ]
    compared = platform_comparisons(headline)
    if compared:
        result["comparisons"] = compared
    caveats = cross_platform_caveats(
        {h["platform"] for h in headline}, {h["covered_window"] for h in headline}
    )
    if caveats:
        result["caveats"] = caveats


def _project(rows: list[dict[str, Any]], fields: list[str]) -> list[dict[str, Any]]:
    known = {name for row in rows for name in row}
    unknown = [f for f in fields if rows and f not in known]
    if unknown:
        raise ValueError(
            f"unknown fields {', '.join(unknown)}; rows have {', '.join(sorted(known))}"
        )
    return [{f: row[f] for f in fields if f in row} for row in rows]


DATED = {"changes": "occurred_at", "settings": "valid_from"}
"""Views whose rows are events: each gets `days_ago` from its account's own today."""


def _add_days_ago(rows: list[dict[str, Any]], accounts: AccountRegistry, column: str) -> None:
    todays: dict[str, date] = {}
    for row in rows:
        alias, when = row.get("account_alias"), row.get(column)
        if not alias or when is None or accounts.resolve(str(alias)) is None:
            continue
        if alias not in todays:
            todays[alias] = account_today(accounts, str(alias))
        day = when.date() if isinstance(when, datetime) else date.fromisoformat(str(when)[:10])
        row["days_ago"] = days_ago(day, todays[alias])


def _budget_totals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each account's current daily budgets added up, so no total is summed by hand."""
    totals: dict[str, dict[str, Any]] = {}
    for row in rows:
        status = str(row.get("status") or "").upper()
        if row.get("valid_to") is not None or row.get("daily_budget") is None:
            continue
        if status and status not in ACTIVE_STATUSES:
            continue
        entry = totals.setdefault(
            row["account_alias"],
            {
                "account_alias": row["account_alias"],
                "currency": row.get("currency"),
                "active_daily_budget_total": 0.0,
                "campaigns": 0,
            },
        )
        entry["active_daily_budget_total"] += float(row["daily_budget"])
        entry["campaigns"] += 1
    for entry in totals.values():
        entry["active_daily_budget_total"] = round(entry["active_daily_budget_total"], 2)
    return list(totals.values())


def build_query_history_tool(store: Store, accounts: AccountRegistry) -> ToolSpec:
    def _run(kwargs: dict[str, Any], _context: ToolContext) -> str:
        try:
            args = QueryHistoryArgs.model_validate(kwargs)
            return json.dumps(run_query_history(store, accounts, args))
        except (ValidationError, ValueError) as exc:
            return json.dumps({"error": True, "detail": sanitize_exception(exc)})

    return ToolSpec(
        name=QUERY_HISTORY_TOOL,
        description=(
            "Query stored history instead of re-reading a platform: coverage, daily rows with "
            "budgets, pacing and matured conversions, settings versions, the change log, and "
            "the conversion lag curve. Use it for trends longer than one read, what changed and "
            "when, and whether recent conversions are still incomplete. For totals per account, "
            "campaign, day, or week, set group_by rather than adding up rows; to compare "
            "accounts, group_by ['account'] in one call without account_alias, which adds the "
            "comparisons and caveats."
        ),
        parameters=parameters_for(QueryHistoryArgs),
        handler=_run,
    )
