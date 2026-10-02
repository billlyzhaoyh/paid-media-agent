"""Model-facing `explain_change` tool: why CPA, conversions or ROAS moved between two windows.

It reads stored history (never the platforms), splits the change exactly into spend moving
between campaigns and each campaign's funnel rates (cost per thousand impressions, click-through
rate, conversion rate, value per conversion), checks each moving campaign's rate change against
what its own response curve expects from its change in spend, and lists the settings changes in
the windows. Every number is computed in code; the readings are code-written.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from paid_media_agent.analytics.drivers import explain_accounts
from paid_media_agent.bandit.recommend import BanditConfig
from paid_media_agent.config import AccountRegistry
from paid_media_agent.harness.tools import ToolContext, ToolSpec, parameters_for
from paid_media_agent.predict.protocol import Predictor
from paid_media_agent.redaction import sanitize_exception
from paid_media_agent.store.db import Store

EXPLAIN_CHANGE_TOOL = "explain_change"


class ExplainChangeArgs(BaseModel):
    account_aliases: list[str] | None = Field(
        default=None,
        description="Aliases from list_accounts; omit for every account. Accounts in one "
        "currency are explained together, as one total.",
    )
    metric: Literal["cpa", "conversions", "roas"] = Field(
        default="cpa", description="The KPI whose change to explain."
    )
    current_start: date | None = Field(default=None, description="Omit for the newest 7 days.")
    current_end: date | None = None
    previous_start: date | None = Field(
        default=None, description="Omit for the same number of days just before current_start."
    )
    previous_end: date | None = None


def build_explain_change_tool(
    store: Store, accounts: AccountRegistry, predictor: Predictor | None, *, config: BanditConfig
) -> ToolSpec:
    async def _run(kwargs: dict[str, Any], _context: ToolContext) -> str:
        try:
            args = ExplainChangeArgs.model_validate(kwargs)
            reports = await explain_accounts(
                store,
                accounts,
                args.account_aliases,
                metric=args.metric,
                predictor=predictor,
                config=config,
                current_start=args.current_start,
                current_end=args.current_end,
                previous_start=args.previous_start,
                previous_end=args.previous_end,
            )
        except (ValidationError, ValueError) as exc:
            return json.dumps({"error": True, "detail": sanitize_exception(exc)})
        return json.dumps(
            {
                "reports": [r.as_json() for r in reports],
                "note": (
                    "From stored history (sync first if the windows look old). Quote the "
                    "readings; do not recompute. 'points' are percentage points of the headline's "
                    "relative change and add up to it exactly; 'amount' is the same part in the "
                    "metric's own units (currency for CPA). Never read points as money. A driver 'within_noise' may be chance. "
                    "expected_from_spend_points is the part of a campaign's rate change its own "
                    "response curve expects from spending more or less (diminishing returns), "
                    "not a new problem. known_changes are settings changes seen in the windows."
                ),
            },
            default=str,
        )

    return ToolSpec(
        name=EXPLAIN_CHANGE_TOOL,
        description=(
            "Explain why CPA, conversions or ROAS changed between two windows: an exact split "
            "into spend moving between campaigns and each campaign's cost per thousand "
            "impressions, click-through rate, conversion rate and value per conversion, with "
            "whether each move is more than noise, whether a campaign's own spend change "
            "explains it (diminishing returns), and the settings changes in the windows. "
            "Read-only; uses stored history. Use for 'why did CPA go up', 'what drove the "
            "drop in conversions', and 'what changed' questions."
        ),
        parameters=parameters_for(ExplainChangeArgs),
        handler=_run,
    )
