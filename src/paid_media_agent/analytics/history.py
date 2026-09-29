"""Fixed, parameterized queries over the history views. Nobody, including the model, writes SQL.

Results name accounts by alias; provider account ids stay in the store.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from paid_media_agent.store.db import Store

ModelHistoryView = Literal[
    "coverage", "daily", "settings", "changes", "lag", "outcomes", "goals", "signals", "constraints"
]
"""The views the model may query."""
HistoryView = ModelHistoryView | Literal["usage"]
"""Every view; `usage` (model calls and their cost) is for operators only."""
MODEL_HISTORY_VIEWS: tuple[ModelHistoryView, ...] = (
    "coverage",
    "daily",
    "settings",
    "changes",
    "lag",
    "outcomes",
    "goals",
    "signals",
    "constraints",
)
HISTORY_VIEWS: tuple[HistoryView, ...] = (*MODEL_HISTORY_VIEWS, "usage")
MAX_ROWS = 500

_QUERIES: dict[HistoryView, tuple[str, str, str]] = {
    # (select and source, date column for the window, order)
    "coverage": (
        "SELECT account_alias, platform, entity_type, count(*) AS snapshot_rows, "
        "count(DISTINCT entity_ref) AS entities, min(day) AS first_day, max(day) AS last_day, "
        "count(DISTINCT pull_id) AS pulls, max(pulled_at) AS last_pulled_at "
        "FROM entity_daily_snapshots",
        "day",
        "GROUP BY account_alias, platform, entity_type ORDER BY account_alias, entity_type",
    ),
    "daily": (
        "SELECT account_alias, platform, entity_type, entity_ref, entity_name, day, currency, "
        "spend, impressions, clicks, conversions, conversion_value, is_complete, age_days, "
        "is_matured, conversions_matured, status, daily_budget, pacing_ratio "
        "FROM entity_daily_panel",
        "day",
        "ORDER BY day DESC, account_alias, entity_ref",
    ),
    "settings": (
        "SELECT account_alias, platform, entity_ref, entity_name, valid_from, valid_to, status, "
        "daily_budget, budget_type, bid_strategy, target_cpa, target_roas, currency "
        "FROM entity_settings_history",
        "CAST(valid_from AS DATE)",
        "ORDER BY valid_from DESC, account_alias, entity_ref",
    ),
    "changes": (
        "SELECT occurred_at, source, status, account_alias, platform, entity_ref, field, "
        "before_value, after_value, proposal_id, revision, tool_name, risk_flags "
        "FROM change_events",
        "CAST(occurred_at AS DATE)",
        "ORDER BY occurred_at DESC",
    ),
    "lag": (
        "SELECT account_alias, platform, entity_type, age_days, entity_days, "
        "conversions_at_age, conversions_matured, completeness FROM conversion_lag",
        "",
        "ORDER BY account_alias, entity_type, age_days",
    ),
    "outcomes": (
        "SELECT decision_day, account_alias, platform, entity_ref, entity_name, policy, outcome, "
        "current_budget, final_budget AS recommended_budget, budget_in_force, proposal_status, "
        "proposal_id, days_matured, hold_days, spend, conversions_matured, "
        "expected_conversions_window, run_id FROM bandit_outcomes",
        "decision_day",
        "ORDER BY decision_day DESC, account_alias, entity_ref",
    ),
    "signals": (
        "SELECT account_alias, platform, entity_ref, day, impression_share, budget_lost_share, "
        "rank_lost_share FROM entity_daily_signals_latest",
        "day",
        "ORDER BY day DESC, account_alias, entity_ref",
    ),
    "constraints": (
        "SELECT * FROM (SELECT r.decision_day, d.account_alias, d.entity_ref, d.entity_name, "
        "c.kind, c.confidence, c.ceiling, d.current_budget, d.final_budget, d.constrained_by, "
        "c.evidence, c.run_id FROM bandit_decision_constraints c "
        "JOIN bandit_decisions d USING (run_id, arm_key) JOIN bandit_runs r USING (run_id)) q",
        "decision_day",
        "ORDER BY decision_day DESC, account_alias, entity_ref",
    ),
    "usage": (
        "SELECT CAST(created_at AS DATE) AS day, purpose, provider, model, count(*) AS calls, "
        "count(*) FILTER (WHERE status <> 'ok') AS failed, sum(input_tokens) AS input_tokens, "
        "sum(cached_tokens) AS cached_tokens, sum(output_tokens) AS output_tokens, "
        "sum(cost_usd) AS cost_usd, count(cost_usd) AS costed_calls, "
        "CAST(median(latency_ms) AS INTEGER) AS p50_latency_ms, "
        "CAST(quantile_cont(latency_ms, 0.95) AS INTEGER) AS p95_latency_ms FROM llm_calls",
        "CAST(created_at AS DATE)",
        "GROUP BY ALL ORDER BY day DESC, cost_usd DESC NULLS LAST",
    ),
    "goals": (
        "SELECT account_alias, effective_from, target_cpa, target_roas, monthly_budget, notes, "
        "source, proposal_id, set_at FROM account_goals",
        "effective_from",
        "ORDER BY effective_from DESC, account_alias",
    ),
}


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, list):
        return [_json_ready(v) for v in value]
    return value


def query_history(
    store: Store,
    view: HistoryView,
    *,
    account_alias: str | None = None,
    entity_ref: str | None = None,
    start: date | None = None,
    end: date | None = None,
    limit: int = 100,
) -> tuple[list[dict[str, Any]], bool]:
    """Rows of one view, newest first, and whether more rows matched than `limit`."""
    select, day_column, order = _QUERIES[view]
    clauses: list[str] = []
    params: list[Any] = []
    if account_alias is not None and view == "usage":
        raise ValueError("model usage is not per account; omit the alias")
    if account_alias is not None:
        clauses.append("account_alias = ?")
        params.append(account_alias)
    if entity_ref is not None and view not in ("coverage", "lag", "goals", "usage"):
        clauses.append("entity_ref = ?")
        params.append(entity_ref)
    if day_column and start is not None:
        clauses.append(f"{day_column} >= ?")
        params.append(start)
    if day_column and end is not None:
        clauses.append(f"{day_column} <= ?")
        params.append(end)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    bounded = max(1, min(limit, MAX_ROWS))
    sql = f"{select}{where} {order} LIMIT {bounded + 1}"
    found = store.fetch_dicts(sql, params)
    rows = [{name: _json_ready(value) for name, value in row.items()} for row in found[:bounded]]
    return rows, len(found) > bounded
