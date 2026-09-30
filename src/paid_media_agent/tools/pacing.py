"""Model-facing `check_pacing` tool: month-to-date spend against the monthly budget, and targets.

It reads stored history and the account's goals, never the platforms, and returns code-written
readings with every number already computed.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from paid_media_agent.analytics.pacing import account_pacing
from paid_media_agent.config import AccountRegistry
from paid_media_agent.harness.tools import ToolContext, ToolSpec, parameters_for
from paid_media_agent.redaction import sanitize_exception
from paid_media_agent.store.db import Store

CHECK_PACING_TOOL = "check_pacing"


class CheckPacingArgs(BaseModel):
    account_alias: str | None = Field(
        default=None, description="Alias from list_accounts; omit to check every account."
    )


def build_check_pacing_tool(store: Store, accounts: AccountRegistry) -> ToolSpec:
    def _run(kwargs: dict[str, Any], _context: ToolContext) -> str:
        try:
            args = CheckPacingArgs.model_validate(kwargs)
            aliases = [args.account_alias] if args.account_alias else list(accounts.aliases())
            reports = [account_pacing(store, accounts, alias) for alias in aliases]
        except (ValidationError, ValueError) as exc:
            return json.dumps({"error": True, "detail": sanitize_exception(exc)})
        return json.dumps(
            {
                "accounts": [r.as_json() for r in reports],
                "note": (
                    "Month to date in each account's own timezone, from stored history (sync "
                    "first if data_through is old). Present the readings; do not recompute. "
                    "conversions_expected and conversion_value_expected add conversions and "
                    "value still arriving; cpa and roas use them. conversions and "
                    "conversion_value are as reported: divide like with like. "
                    "Without a monthly budget or targets, say they are not set; the user can set "
                    "them with `paid-media-agent goals set` or ask you to propose a change "
                    "(host__set_account_goals)."
                ),
            },
            default=str,
        )

    return ToolSpec(
        name=CHECK_PACING_TOOL,
        description=(
            "Monthly pacing per account: month-to-date spend against the monthly budget, the "
            "projected month-end spend, the daily spend that lands on budget, and CPA/ROAS "
            "against targets. Read-only; uses stored history. Use for 'are we on budget', 'how "
            "much can we spend', and 'are we hitting target' questions."
        ),
        parameters=parameters_for(CheckPacingArgs),
        handler=_run,
    )
