"""Model-facing `query_history` tool: stored history, through fixed queries only.

Every read the agent, the daily sync, or a backfill made is kept in the state store. This tool
answers from that history: what is stored, the daily panel with budgets and matured conversions,
settings versions, the change log, and how late conversions arrive. The model picks a view and
filters; it never writes SQL, and results name accounts by alias.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from paid_media_agent.analytics.history import MAX_ROWS, HistoryView, query_history
from paid_media_agent.config import AccountRegistry
from paid_media_agent.harness.tools import ToolContext, ToolSpec, parameters_for
from paid_media_agent.redaction import sanitize_exception
from paid_media_agent.store.db import Store

QUERY_HISTORY_TOOL = "query_history"


class QueryHistoryArgs(BaseModel):
    view: HistoryView = Field(
        description=(
            "coverage: what is stored per account. daily: entity-day rows with budget, pacing, "
            "and matured conversions. settings: budget and status versions. changes: proposal "
            "decisions, executions, and changes detected outside this agent. lag: share of "
            "final conversions reported N days after the day. outcomes: each budget "
            "recommendation, whether it was followed, and matured conversions over the week "
            "after it against what was expected. goals: versions of each account's target CPA, "
            "target ROAS, and monthly budget, and who set them."
        )
    )
    account_alias: str | None = Field(default=None, description="Alias from list_accounts.")
    entity_ref: str | None = Field(default=None, description="Campaign or entity id.")
    start_date: date | None = None
    end_date: date | None = None
    limit: int = Field(default=100, ge=1, le=MAX_ROWS)


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
    )
    note = (
        "History holds only what was pulled; missing days were never read. Matured conversions "
        "are final enough to compare; recent days are still arriving."
    )
    return {
        "view": args.view,
        "row_count": len(rows),
        "truncated": truncated,
        "rows": rows,
        "note": note,
    }


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
            "when, and whether recent conversions are still incomplete."
        ),
        parameters=parameters_for(QueryHistoryArgs),
        handler=_run,
    )
