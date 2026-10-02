"""Budget recommendations for configured accounts: the path the CLI, the job, and the tool share.

Each account is allocated on its own (one currency, one total), on its own local date, and under
the goals in force for it: a target CPA caps the expected average CPA, and without an explicit
total a monthly budget scales the current total toward the spend that lands on it. A run is
recorded in
`bandit_runs` and `bandit_decisions` whether or not it proposes anything. With `propose`, the
campaigns it moves become proposals awaiting approval (`bandit/proposals.py`); nothing changes
until an approver approves one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime
from typing import Any

from paid_media_agent.analytics.goals import GoalStore, account_today
from paid_media_agent.analytics.pacing import compute_pacing
from paid_media_agent.bandit.proposals import MIN_CHANGE, propose_run
from paid_media_agent.bandit.recommend import BanditConfig, Policy, recommend
from paid_media_agent.config import AccountRegistry
from paid_media_agent.predict.protocol import Predictor
from paid_media_agent.redaction import sanitize_exception
from paid_media_agent.store.db import Store
from paid_media_agent.tools.writes import ProposalService


def live_config(policy: Policy) -> BanditConfig:
    return replace(BanditConfig(), policy=policy)


def goal_inputs(
    store: Store,
    alias: str,
    day: date,
    config: BanditConfig,
    *,
    total_budget: float | None,
    currency: str | None = None,
) -> tuple[BanditConfig, float | None, dict[str, Any] | None]:
    """The config and monthly-budget scale for one account from its goal in force on `day`."""
    goal = GoalStore(store).current(alias, day)
    if goal is None:
        return config, None, None
    config = replace(config, target_cpa=goal.target_cpa)
    scale: float | None = None
    if total_budget is None and goal.monthly_budget:
        pacing = compute_pacing(store, account_alias=alias, today=day, goal=goal, currency=currency)
        scale = pacing.budget_scale
    return config, scale, {**goal.values(), "effective_from": goal.effective_from.isoformat()}


async def allocate_accounts(
    store: Store,
    predictor: Predictor | None,
    *,
    aliases: Sequence[str],
    as_of: date | None = None,
    accounts: AccountRegistry | None = None,
    config: BanditConfig,
    total_budget: float | None = None,
    service: ProposalService | None = None,
    propose: bool = False,
    min_change: float = MIN_CHANGE,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """One recommendation per account alias, and proposals for the moves when `propose` is set.

    Without `as_of`, each account decides on its own local date (needs `accounts`).
    """
    if propose and service is None:
        raise ValueError("proposing needs the proposal service")
    if as_of is None and accounts is None:
        raise ValueError("pass as_of or the account registry")
    results: list[dict[str, Any]] = []
    for alias in aliases:
        day = as_of or account_today(accounts or AccountRegistry(), alias, now)
        binding = accounts.resolve(alias) if accounts else None
        try:
            account_config, scale, goals = goal_inputs(
                store,
                alias,
                day,
                config,
                total_budget=total_budget,
                currency=binding.currency if binding else None,
            )
            run = await recommend(
                store,
                predictor,
                as_of=day,
                config=account_config,
                total_budget=total_budget,
                account_alias=alias,
                mode="recommend",
                budget_scale=scale,
            )
        except ValueError as exc:
            results.append({"account_alias": alias, "error": sanitize_exception(exc)})
            continue
        summary: dict[str, Any] = {"account_alias": alias, "goals": goals, **run.as_json()}
        if propose and service is not None and any(d.arm.eligible for d in run.decisions):
            proposed = await propose_run(store, service, run, min_change=min_change)
            summary["proposals"] = proposed.as_json()
        results.append(summary)
    return results
