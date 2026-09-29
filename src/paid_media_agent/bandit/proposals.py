"""Turn a bandit run into budget proposals that wait for an approver.

Each campaign whose recommended budget moves enough becomes one proposal through the normal
`ProposalService`: the same admitted operations, schema checks, live "before" read, digest, and
risk flags as a change the agent proposes. The proposals live in the host thread
`host:bandit:<run_id>` with requester `bandit`; an approver approves them from the API or the CLI,
and the executor then applies each one once and reads it back. Nothing is applied here.

A newer run supersedes an older one: the older run's proposals still awaiting a decision for the
same campaigns are rejected, so an approver never applies a stale recommendation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from uuid import UUID

from paid_media_agent.bandit.policy import greedy
from paid_media_agent.bandit.recommend import ArmDecision, BanditRun
from paid_media_agent.domain.proposals import ProposalState
from paid_media_agent.store.db import Store
from paid_media_agent.tools.writes import HOST_THREAD_PREFIX, ProposalService, WriteDenied

logger = logging.getLogger(__name__)

REQUESTER = "bandit"
BUDGET_FIELD = "daily_budget"
MIN_CHANGE = 0.05
"""Skip moves smaller than this share of the current budget; they are noise, not decisions."""


def run_thread(run: BanditRun) -> str:
    return f"{HOST_THREAD_PREFIX}bandit:{run.run_id}"


@dataclass
class ProposedRun:
    proposals: dict[str, UUID] = field(default_factory=dict)
    """Entity ref to proposal id."""
    skipped: dict[str, str] = field(default_factory=dict)
    """Entity ref to why no proposal was made."""
    superseded: list[UUID] = field(default_factory=list)

    def as_json(self) -> dict[str, object]:
        return {
            "proposals": {ref: str(pid) for ref, pid in self.proposals.items()},
            "skipped": self.skipped,
            "superseded": [str(pid) for pid in self.superseded],
        }


def _budget_operations(service: ProposalService) -> dict[str, str]:
    """Platform to the admitted operation that edits a campaign's daily budget."""
    found: dict[str, str] = {}
    for op in service.admitted_operations():
        fields = op.get("editable_fields")
        if isinstance(fields, list) and BUDGET_FIELD in fields:
            found.setdefault(str(op["platform"]), str(op["tool_name"]))
    return found


def _reason(run: BanditRun, decision: ArmDecision, new_budget: float) -> str:
    arm, post = decision.arm, decision.posterior
    current = float(arm.current_budget or 0.0)
    parts = [
        f"Budget bandit run {run.run_id} ({run.policy}, {run.decision_day.isoformat()}): "
        f"daily budget {current:.2f} -> {new_budget:.2f} ({new_budget / current - 1:+.0%})."
    ]
    mean = greedy(post) if post is not None else None
    if post is not None and mean is not None:
        curve = post.curve(mean)
        before = curve.value(arm.expected_spend(current))
        after = curve.value(arm.expected_spend(new_budget))
        parts.append(
            f"Expected conversions {before:.1f} -> {after:.1f} a day; elasticity "
            f"{mean[1]:.2f} ± {post.kappa2_sd:.2f} from {arm.n_history} days of history."
        )
    bounds = [c for c in decision.constrained_by if c not in ("ineligible",)]
    if bounds:
        parts.append(f"Bounds: {', '.join(bounds)}.")
    constraint = arm.constraint
    if constraint.kind != "unknown":
        parts.append(
            f"Spend is limited by {constraint.kind} ({constraint.confidence} confidence: "
            f"{'; '.join(constraint.evidence)})."
        )
    parts.append(f"Account total {run.total_budget:.2f} ({run.total_source}).")
    return " ".join(parts)[:2000]


def _supersede(
    store: Store,
    service: ProposalService,
    run: BanditRun,
    entity_refs: set[str],
    *,
    keep: UUID | None = None,
) -> list[UUID]:
    rows = store.fetch(
        "SELECT d.entity_ref, d.proposal_id FROM bandit_decisions d JOIN bandit_runs r "
        "USING (run_id) WHERE d.proposal_id IS NOT NULL AND r.run_id <> ?",
        [run.run_id],
    )
    superseded = []
    for entity_ref, proposal_id in rows:
        if entity_ref not in entity_refs or proposal_id == keep:
            continue
        record = service.get(proposal_id)
        if record is None or record.state is not ProposalState.AWAITING_APPROVAL:
            continue
        try:
            service.reject(
                proposal_id, actor_ref=REQUESTER, message=f"superseded by bandit run {run.run_id}"
            )
            superseded.append(proposal_id)
        except WriteDenied as exc:  # a concurrent decision got there first
            logger.info("could not supersede %s: %s", proposal_id, exc.reason)
    return superseded


async def propose_run(
    store: Store,
    service: ProposalService,
    run: BanditRun,
    *,
    min_change: float = MIN_CHANGE,
) -> ProposedRun:
    """One proposal per campaign the run moves by at least `min_change`; none are applied."""
    operations = _budget_operations(service)
    result = ProposedRun()
    moving = []
    for decision in run.decisions:
        arm = decision.arm
        current = float(arm.current_budget or 0.0)
        new_budget = round(float(decision.final_budget or 0.0), 2)
        if not arm.eligible or current <= 0:
            continue
        change = abs(new_budget / current - 1)
        if change < min_change:
            result.skipped[arm.entity_ref] = f"change {change:.1%} is below {min_change:.0%}"
            continue
        if abs(new_budget - current) < 1.0:
            result.skipped[arm.entity_ref] = (
                f"change of {abs(new_budget - current):.2f} is below 1.00 in account currency"
            )
            continue
        if arm.platform not in operations:
            result.skipped[arm.entity_ref] = (
                f"no admitted daily-budget operation for {arm.platform}"
            )
            continue
        moving.append((decision, new_budget))
    # This run's view replaces older recommendations for every campaign it decided on: a held or
    # barely-moved campaign's older proposal is stale now. A moved one's is superseded only once
    # its replacement exists, so an approver is never left with neither.
    moved = {d.arm.entity_ref for d, _ in moving}
    held = {d.arm.entity_ref for d in run.decisions if d.arm.eligible} - moved
    result.superseded = _supersede(store, service, run, held)
    for decision, new_budget in moving:
        arm = decision.arm
        try:
            record = await service.propose(
                thread_id=run_thread(run),
                requester_ref=REQUESTER,
                account_alias=arm.account_alias,
                tool_name=operations[arm.platform],
                target_ref=arm.entity_ref,
                changes={BUDGET_FIELD: new_budget},
                reason=_reason(run, decision, new_budget),
                measurement_plan=(
                    "Compare matured conversions and spend over the 7-day hold with the "
                    "expected conversions above (bandit_outcomes)."
                ),
                reversal_plan=f"Restore the daily budget to {float(arm.current_budget or 0):.2f}.",
            )
        except WriteDenied as exc:
            result.skipped[arm.entity_ref] = f"proposal refused: {exc.reason}"
            continue
        proposal_id = record.changeset.proposal_id
        result.proposals[arm.entity_ref] = proposal_id
        result.superseded += _supersede(store, service, run, {arm.entity_ref}, keep=proposal_id)
        store.write(
            "UPDATE bandit_decisions SET proposal_id = ? WHERE run_id = ? AND arm_key = ?",
            [proposal_id, run.run_id, arm.key],
        )
    return result
