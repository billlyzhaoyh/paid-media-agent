"""Model-facing `recommend_budgets` tool: how to split one account's daily budget across campaigns.

It reads stored history, fits each campaign's spend response, and returns a recommendation for
every campaign with a code-written reading. It changes nothing: applying a recommendation is a
normal `propose_change` that waits for approval.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from paid_media_agent.analytics.goals import account_today
from paid_media_agent.bandit.live import goal_inputs
from paid_media_agent.bandit.recommend import BanditConfig, recommend
from paid_media_agent.config import AccountRegistry
from paid_media_agent.harness.tools import ToolContext, ToolSpec, parameters_for
from paid_media_agent.predict.protocol import Predictor
from paid_media_agent.redaction import sanitize_exception
from paid_media_agent.store.db import Store

RECOMMEND_BUDGETS_TOOL = "recommend_budgets"
MAX_CAMPAIGNS = 25
_LIMITS = {
    "hold": "kept: its budget changed within the last 7 days, and that change is still being measured",
    "no_valid_curve": "kept: its history does not yet give a reliable spend response",
    "upper": "at the step limit (at most 25% up per change)",
    "lower": "at the step limit (at most 25% down per change)",
    "spend_history": "capped at 1.5 times the most it has ever spent",
    "max_budget": "capped at the configured maximum budget",
    "cpia": "stopped where its next conversion would cost more than the CPIA cap",
    "target_cpa": "lowered toward the account's target CPA",
}
_TOTALS = {
    "current": "the campaigns' current total",
    "explicit": "the total you asked for",
    "monthly_budget": "the total that paces toward the monthly budget",
}


class RecommendBudgetsArgs(BaseModel):
    account_alias: str = Field(
        description="Alias from list_accounts; budgets are split per account."
    )
    total_budget: float | None = Field(
        default=None,
        gt=0,
        description="Total daily budget to split; omit to keep the campaigns' current total.",
    )


def budget_reading(row: dict[str, Any], currency: str | None) -> str:
    """One code-written sentence per campaign, from a decision's `as_json()`."""
    name = f"{row['entity_ref']} {row['entity_name']}".strip()
    if not row["eligible"]:
        return f"{name}: not allocated ({row['reason']})."
    current, new = row["current_budget"], row["final_budget"]
    unit = f" {currency}" if currency else ""
    limits = [_LIMITS[c] for c in row["constrained_by"] if c in _LIMITS]
    if new is None or current is None or abs(new - current) < 0.005:
        verb = f"keep at {current:.2f}{unit}"
    else:
        verb = f"{'raise' if new > current else 'lower'} from {current:.2f} to {new:.2f}{unit} ({row['change']:+.0%})"
    reading = f"{name}: {verb}"
    if row["expected_conversions"] is not None:
        reading += f"; about {row['expected_conversions']:.1f} conversions a day expected"
        if row.get("expected_conversions_now") is not None and not verb.startswith("keep"):
            reading += f" (about {row['expected_conversions_now']:.1f} at the current budget)"
    if limits:
        reading += f"; {'; '.join(limits)}"
    return reading + "."


def build_recommend_budgets_tool(
    store: Store, accounts: AccountRegistry, predictor: Predictor | None, *, config: BanditConfig
) -> ToolSpec:
    async def _run(kwargs: dict[str, Any], _context: ToolContext) -> str:
        try:
            args = RecommendBudgetsArgs.model_validate(kwargs)
            binding = accounts.resolve(args.account_alias)
            if binding is None:
                raise ValueError(f"unknown account alias {args.account_alias}; call list_accounts")
            day = account_today(accounts, args.account_alias)
            account_config, scale, goals = goal_inputs(
                store,
                args.account_alias,
                day,
                config,
                total_budget=args.total_budget,
                currency=binding.currency,
            )
            run = await recommend(
                store,
                predictor,
                as_of=day,
                config=account_config,
                total_budget=args.total_budget,
                account_alias=args.account_alias,
                mode="recommend",
                budget_scale=scale,
            )
        except (ValidationError, ValueError) as exc:
            return json.dumps({"error": True, "detail": sanitize_exception(exc)})
        result = run.as_json()
        rows = result.pop("decisions")
        moved = [r for r in rows if r["eligible"] and r["change"] and abs(r["change"]) >= 0.005]
        failed = [k for k, v in result["data_checks"].items() if not v["ok"]]
        unit = f" {result['currency']}" if result["currency"] else ""
        summary = (
            f"{len(moved)} of {sum(r['eligible'] for r in rows)} allocated campaigns would move; "
            f"total {result['total_budget']:.2f}{unit} a day, "
            f"{_TOTALS.get(result['total_source'], result['total_source'])}"
            + (
                (
                    ", cut to meet the target CPA"
                    if result["target_cpa_reached"]
                    else ", cut as far as the step limits allow toward the target CPA, which "
                    "is still not reached"
                )
                if result["capped_by_target_cpa"]
                else ""
            )
            + "."
        )
        paired = [
            r
            for r in rows
            if r["eligible"]
            and r["expected_conversions"] is not None
            and r["expected_conversions_now"] is not None
        ]
        if paired:
            now = sum(r["expected_conversions_now"] for r in paired)
            then = sum(r["expected_conversions"] for r in paired)
            summary += (
                f" Expected conversions {now:.1f} a day at current budgets and {then:.1f} as "
                f"recommended ({then - now:+.1f}), by the model's estimate."
            )
        target = (goals or {}).get("target_cpa")
        if result["expected_cpa"] is not None:
            summary += f" Expected CPA {result['expected_cpa']:.2f}" + (
                f" against a {target:.2f} target." if target else "; no target CPA is set."
            )
        if result["fallback_used"]:
            summary += f" Data checks failed ({', '.join(failed)}); the last good curves were used."
        result = {"summary": summary, "goals": goals, **result}
        result["campaigns"] = [
            {"reading": budget_reading(r, run.currency), **r} for r in rows[:MAX_CAMPAIGNS]
        ]
        result["campaign_count"] = len(rows)
        result["note"] = (
            "These are recommendations; nothing has changed. Moves are at most 25% per change "
            "and campaigns changed in the last 7 days are held. Explain them with the readings. "
            "To apply one, only when the user asks, call propose_change for that campaign's "
            "daily_budget with the recommended value and put the run_id in the reason; it then "
            "waits for approval like any change. expected_conversions is the model's estimate, "
            "not a promise."
        )
        return json.dumps(result)

    return ToolSpec(
        name=RECOMMEND_BUDGETS_TOOL,
        description=(
            "Recommend how to split one account's total daily budget across its campaigns to "
            "get the most conversions, from each campaign's spend response in stored history. "
            "Returns current and recommended budgets with reasons and limits. Read-only: it "
            "changes nothing. Use it for 'how should I split the budget', 'where should more "
            "budget go', and reallocation questions; sync or read the account first if history "
            "is missing."
        ),
        parameters=parameters_for(RecommendBudgetsArgs),
        handler=_run,
    )
