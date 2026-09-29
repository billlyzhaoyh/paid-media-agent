"""Model-facing `what_if_budgets` tool: what a different set of daily budgets would buy.

It forecasts spend, conversions and CPA for one account at today's budgets and at the scenario,
from the same curves and spend limits a budget recommendation uses, with 80% ranges. It changes
nothing: a scenario the user likes is applied as normal proposals that wait for approval.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from paid_media_agent.bandit.recommend import BanditConfig
from paid_media_agent.bandit.whatif import BudgetChange, Scenario, what_if_account
from paid_media_agent.config import AccountRegistry
from paid_media_agent.harness.tools import ToolContext, ToolSpec, parameters_for
from paid_media_agent.predict.protocol import Predictor
from paid_media_agent.redaction import sanitize_exception
from paid_media_agent.store.db import Store

WHAT_IF_BUDGETS_TOOL = "what_if_budgets"


class CampaignBudgetChange(BaseModel):
    campaign: str = Field(description="Campaign id as shown by other tools (e.g. g-101).")
    budget: float | None = Field(
        default=None, ge=0, description="New daily budget in account currency; 0 pauses."
    )
    change: float | None = Field(
        default=None, ge=-1, description="Relative change: 0.2 is +20%, -0.3 is -30%."
    )


class WhatIfBudgetsArgs(BaseModel):
    account_alias: str = Field(description="Alias from list_accounts; one account per call.")
    changes: list[CampaignBudgetChange] | None = Field(
        default=None, description="Budget changes to named campaigns; others stay as they are."
    )
    total_daily_budget: float | None = Field(
        default=None, gt=0, description="Or: a new total daily budget for the account."
    )
    total_change: float | None = Field(
        default=None, gt=-1, description="Or: a relative change to the account's total."
    )
    split: Literal["proportional", "best"] = Field(
        default="proportional",
        description="For a new total: in proportion to today's budgets, or as the curves say "
        "is best.",
    )
    horizon_days: int = Field(default=7, ge=1, le=92, description="Days to total the change over.")

    def scenario(self) -> Scenario:
        return Scenario(
            changes=tuple(
                BudgetChange(c.campaign, budget=c.budget, change=c.change)
                for c in self.changes or []
            ),
            total_daily_budget=self.total_daily_budget,
            total_change=self.total_change,
            split=self.split,
            horizon_days=self.horizon_days,
        )


def build_what_if_budgets_tool(
    store: Store, accounts: AccountRegistry, predictor: Predictor | None, *, config: BanditConfig
) -> ToolSpec:
    async def _run(kwargs: dict[str, Any], _context: ToolContext) -> str:
        try:
            args = WhatIfBudgetsArgs.model_validate(kwargs)
            report = await what_if_account(
                store, accounts, predictor, args.account_alias, args.scenario(), config=config
            )
        except (ValidationError, ValueError) as exc:
            return json.dumps({"error": True, "detail": sanitize_exception(exc)})
        result = report.as_json()
        result["note"] = (
            "A forecast from each campaign's fitted spend response; nothing has changed. Quote "
            "the reading; do not recompute. Figures are per day unless under over_horizon. "
            "incremental_cpa is what each extra conversion costs (or each lost one saves). "
            "Treat campaigns flagged outside_history as guesses; capped ones cannot spend more "
            "because demand or a bid target limits them. To act on a scenario, only when the user "
            "asks, call propose_change per campaign; each waits for approval."
        )
        return json.dumps(result, default=str)

    return ToolSpec(
        name=WHAT_IF_BUDGETS_TOOL,
        description=(
            "Forecast what different daily budgets would do for one account: spend, "
            "conversions and CPA against today, with 80% ranges, the cost of each extra "
            "conversion, what campaigns cannot spend, how many steps a big change takes, the "
            "best split of the same total, and the effect on targets and the monthly budget. "
            "Read-only. Use for 'what if I raise X by 20%', 'what would another 5k a month "
            "buy', and 'what happens if I cut brand' questions."
        ),
        parameters=parameters_for(WhatIfBudgetsArgs),
        handler=_run,
    )
