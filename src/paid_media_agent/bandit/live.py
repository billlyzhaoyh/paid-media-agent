"""Budget recommendations for configured accounts: the path the CLI, the job, and the tool share.

Each account is allocated on its own (one currency, one total). A run is recorded in
`bandit_runs` and `bandit_decisions` whether or not it proposes anything. With `propose`, the
campaigns it moves become proposals awaiting approval (`bandit/proposals.py`); nothing changes
until an approver approves one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from typing import Any

from paid_media_agent.bandit.proposals import MIN_CHANGE, propose_run
from paid_media_agent.bandit.recommend import BanditConfig, Policy, recommend
from paid_media_agent.predict.protocol import Predictor
from paid_media_agent.redaction import sanitize_exception
from paid_media_agent.store.db import Store
from paid_media_agent.tools.writes import ProposalService


def live_config(policy: Policy) -> BanditConfig:
    return replace(BanditConfig(), policy=policy)


async def allocate_accounts(
    store: Store,
    predictor: Predictor | None,
    *,
    aliases: Sequence[str],
    as_of: date,
    config: BanditConfig,
    total_budget: float | None = None,
    service: ProposalService | None = None,
    propose: bool = False,
    min_change: float = MIN_CHANGE,
) -> list[dict[str, Any]]:
    """One recommendation per account alias, and proposals for the moves when `propose` is set."""
    if propose and service is None:
        raise ValueError("proposing needs the proposal service")
    results: list[dict[str, Any]] = []
    for alias in aliases:
        try:
            run = await recommend(
                store,
                predictor,
                as_of=as_of,
                config=config,
                total_budget=total_budget,
                account_alias=alias,
                mode="recommend",
            )
        except ValueError as exc:
            results.append({"account_alias": alias, "error": sanitize_exception(exc)})
            continue
        summary: dict[str, Any] = {"account_alias": alias, **run.as_json()}
        if propose and service is not None and any(d.arm.eligible for d in run.decisions):
            proposed = await propose_run(store, service, run, min_change=min_change)
            summary["proposals"] = proposed.as_json()
        results.append(summary)
    return results
