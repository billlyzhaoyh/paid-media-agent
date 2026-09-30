"""Model-facing `check_anomalies` tool: which recent campaign-days fall outside their expected range.

It reads stored history, never the platforms, and returns flags with the observed value, the
expected value and range, the method used, and which days could not be checked yet.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from paid_media_agent.analytics.anomalies import check_anomalies
from paid_media_agent.analytics.goals import account_today
from paid_media_agent.config import AccountRegistry
from paid_media_agent.domain.windows import days_ago
from paid_media_agent.harness.tools import ToolContext, ToolSpec, parameters_for
from paid_media_agent.predict.protocol import Predictor
from paid_media_agent.redaction import sanitize_exception
from paid_media_agent.store.db import Store

CHECK_ANOMALIES_TOOL = "check_anomalies"
MAX_FLAGS = 25


class CheckAnomaliesArgs(BaseModel):
    account_alias: str | None = Field(
        default=None, description="Alias from list_accounts; omit to check every account."
    )
    window_days: int = Field(default=7, ge=1, le=28, description="Recent complete days to check.")


def _with_reading(flag: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """A code-written sentence per flag, so the answer cannot misstate which side of the range,
    or how long ago the day was (`today` is the account's own)."""
    side = "above" if flag["direction"] == "up" else "below"
    ago = ""
    if today is not None:
        count = days_ago(date.fromisoformat(str(flag["day"])), today)
        flag = {**flag, "days_ago": count}
        ago = f" ({count} day{'s' if count != 1 else ''} ago)"
    edge = flag["hi"] if flag["direction"] == "up" else flag["lo"]
    beyond = ""
    if isinstance(edge, int | float) and isinstance(flag["observed"], int | float):
        # In the metric's own units: band_distance read as a percentage is the usual misreading.
        gap = abs(flag["observed"] - edge)
        beyond = f", {gap:,.2f} {'above' if flag['direction'] == 'up' else 'below'} its edge {edge}"
    reading = (
        f"{flag['metric']} {flag['observed']} on {flag['day']}{ago} is {side} the expected range "
        f"{flag['lo']} to {flag['hi']}{beyond} (expected {flag['expected']}; {flag['method']})"
    )
    shown = {k: v for k, v in flag.items() if k != "score"}
    if "score" in flag:
        # "score" read as a percentage ("17% above"); name it for what it is.
        shown["band_distance"] = flag["score"]
    return {"reading": reading, **shown}


def build_check_anomalies_tool(
    store: Store, accounts: AccountRegistry, predictor: Predictor | None, *, band: float = 0.95
) -> ToolSpec:
    async def _run(kwargs: dict[str, Any], _context: ToolContext) -> str:
        try:
            args = CheckAnomaliesArgs.model_validate(kwargs)
            if args.account_alias is not None and accounts.resolve(args.account_alias) is None:
                raise ValueError(f"unknown account alias {args.account_alias}; call list_accounts")
            # One account is checked as of its own day; several, as of the UTC day.
            as_of = (
                account_today(accounts, args.account_alias)
                if args.account_alias is not None
                else datetime.now(UTC).date()
            )
            report = await check_anomalies(
                store,
                predictor,
                as_of=as_of,
                window_days=args.window_days,
                account_alias=args.account_alias,
                band=band,
            )
        except (ValidationError, ValueError) as exc:
            return json.dumps({"error": True, "detail": sanitize_exception(exc)})
        result = report.as_json()
        counts = {m: sum(1 for f in report.flags if f.metric == m) for m in report.methods}
        summary = "; ".join(
            f"{metric} checked {result['windows'][metric]}: {counts[metric]} outside the expected "
            f"range ({method})"
            for metric, method in report.methods.items()
        )
        result = {"summary": summary or "nothing checked; see notes", **result}
        result["flag_count"] = len(result["flags"])
        result["flags"] = [
            _with_reading(flag, account_today(accounts, flag["account_alias"]))
            for flag in result["flags"][:MAX_FLAGS]
        ]
        result["note"] = (
            "A flag is a prompt to investigate, not a finding. Conversions are checked on an "
            "earlier window than spend (see windows) because recent conversions are still "
            "arriving; state both windows. The method says how each flag was judged; "
            "dod_rule_fallback is the ±50% day-over-day rule. band_distance is how far outside "
            "the expected range a day fell, as a share of the range's width (0.2 = a fifth of a "
            "range beyond its edge); it is not a percentage change. Days never pulled into "
            "history are not checked."
        )
        return json.dumps(result)

    return ToolSpec(
        name=CHECK_ANOMALIES_TOOL,
        description=(
            "Check recent campaign-days in stored history for spend or conversions outside their "
            "expected range, given budget changes, spend, weekday, and recent level. Returns "
            "flags with observed, expected, range, direction, and method. Use it for 'anything "
            "unusual?', spikes, drops, and tracking breaks; sync or read the account first if "
            "history is missing."
        ),
        parameters=parameters_for(CheckAnomaliesArgs),
        handler=_run,
    )
