"""Fit every campaign's response curve for one account, as a budget decision or a forecast needs.

Steps 1-3 of a bandit run (`recommend`), on their own so a what-if forecast uses exactly the
curves a recommendation would: load each campaign's lag-corrected history as it stood on the day,
check it, ask the global model for pseudo-samples, and fit each campaign's posterior. When the
data checks fail, the last good posteriors are reused and the notes say so.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from paid_media_agent.bandit.arms import Arm, DataChecks, check_data, load_arms
from paid_media_agent.bandit.policy import greedy
from paid_media_agent.bandit.posterior import Posterior, PowerCurve, fit_posterior
from paid_media_agent.bandit.prior import pseudo_samples
from paid_media_agent.predict.protocol import Predictor
from paid_media_agent.store.db import Store

if TYPE_CHECKING:
    from paid_media_agent.bandit.recommend import BanditConfig


@dataclass
class FittedAccount:
    as_of: date
    arms: list[Arm]
    checks: DataChecks
    posteriors: dict[str, Posterior] = field(default_factory=dict)
    """Arm key to its posterior; eligible arms only, and only those with a fit or a last good one."""
    prior_source: str = "none"
    notes: list[str] = field(default_factory=list)

    @property
    def eligible(self) -> list[Arm]:
        return [a for a in self.arms if a.eligible]

    @property
    def currency(self) -> str | None:
        return next((a.currency for a in self.eligible if a.currency), None)

    def curve(self, arm: Arm) -> PowerCurve | None:
        """The arm's mean curve (mean-corrected), or None when it has no valid one."""
        post = self.posteriors.get(arm.key)
        mean = greedy(post) if post is not None else None
        return post.curve(mean) if post is not None and mean is not None else None


def last_good(store: Store, before: date, keys: set[str]) -> dict[str, tuple[Posterior, float]]:
    """The newest posterior per arm from a run whose data checks passed, before `before`."""
    rows = store.fetch(
        """
        SELECT d.arm_key, d.post_mean, d.post_cov, d.spend_unit
        FROM bandit_decisions d JOIN bandit_runs r USING (run_id)
        WHERE NOT r.fallback_used AND d.post_mean IS NOT NULL AND r.decision_day < ?
        QUALIFY row_number() OVER (
            PARTITION BY d.arm_key ORDER BY r.decision_day DESC, r.created_at DESC
        ) = 1
        """,
        [before],
    )
    found = {}
    for key, mean, cov, unit in rows:
        if key in keys:
            found[key] = (
                Posterior.from_record(json.loads(mean), json.loads(cov), float(unit)),
                float(unit),
            )
    return found


async def fit_account(
    store: Store,
    predictor: Predictor | None,
    *,
    as_of: date,
    config: BanditConfig,
    account_alias: str | None = None,
) -> FittedAccount:
    """Arms and posteriors for one account's campaigns on `as_of`; nothing is recorded."""
    arms = load_arms(store, as_of=as_of, train_days=config.train_days, account_alias=account_alias)
    for arm in arms:
        # The ceiling decides only where demand or a bid target, not the budget, limits spend.
        arm.capped = (
            config.spend_model == "ceiling"
            and arm.ceiling is not None
            and arm.constraint.kind in ("demand", "target")
        )
    accounts = {(a.platform, a.provider_account_id) for a in arms if a.eligible}
    if len(accounts) > 1:
        raise ValueError("budgets are allocated within one account; pass its alias")
    fitted = FittedAccount(as_of=as_of, arms=arms, checks=check_data(arms, as_of))
    eligible = fitted.eligible
    if not eligible:
        return fitted
    if fitted.checks.passed:
        pseudo = await pseudo_samples(
            eligible,
            as_of=as_of,
            k=config.pseudo_samples,
            predictor=predictor,
            half_life_days=config.prior_half_life_days,
        )
        fitted.prior_source = f"{pseudo.source}:{pseudo.model_version}"
        fitted.notes += pseudo.notes
        for i, arm in enumerate(eligible):
            fitted.posteriors[arm.key] = fit_posterior(
                arm.spend,
                arm.conversions,
                arm.weekdays,
                arm.unit,
                pseudo_spend=pseudo.spend.get(i),
                pseudo_target=pseudo.target.get(i),
                pseudo_weight=pseudo.weight,
                prior_kappa2=config.prior_kappa2,
                ladder_z=config.ladder_z,
            )
    else:
        failed = ", ".join(k for k, v in fitted.checks.results.items() if not v["ok"])
        fitted.notes.append(f"data checks failed ({failed}); reusing the last good curves")
        previous = last_good(store, as_of, {a.key for a in eligible})
        fitted.prior_source = "last_good"
        for arm in eligible:
            if arm.key in previous:
                fitted.posteriors[arm.key], arm.unit = previous[arm.key]
    return fitted
