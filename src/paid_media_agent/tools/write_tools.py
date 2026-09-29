"""Model-facing write tools over ProposalService and WriteExecutor: propose, inspect, execute."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from paid_media_agent.domain.common import JsonValue
from paid_media_agent.domain.presentation import ProposalView, ReceiptView
from paid_media_agent.harness.messages import ToolCall
from paid_media_agent.harness.tools import ToolContext, ToolSpec, parameters_for
from paid_media_agent.tools.discovery import _NoArgs
from paid_media_agent.tools.writes import (
    DISCOVER_WRITE_OPERATIONS_TOOL,
    EXECUTE_CHANGE_TOOL,
    GET_PROPOSAL_TOOL,
    PROPOSE_CHANGE_TOOL,
    ProposalService,
    WriteDenied,
    WriteExecutor,
)


class ProposeChangeArgs(BaseModel):
    account_alias: str = Field(description="Configured account alias from list_accounts.")
    tool_name: str = Field(
        description="Admitted mutation from discover_write_operations, e.g. google_ads__update_campaign_budget."
    )
    target_ref: str = Field(
        description="Provider entity id being changed, e.g. a campaign id from a read."
    )
    changes: dict[str, JsonValue] = Field(
        description="Field -> new value. Only fields the policy admits."
    )
    reason: str = Field(min_length=1, max_length=2000)
    measurement_plan: str = Field(default="", max_length=800)
    reversal_plan: str = Field(default="", max_length=800)


class ProposalIdArgs(BaseModel):
    proposal_id: str = Field(description="UUID of the proposal returned by propose_change.")


class ExecuteChangeArgs(BaseModel):
    proposal_id: str = Field(description="UUID of the proposal returned by propose_change.")
    revision: int = Field(
        ge=1, description="Revision number of the proposal exactly as you presented it."
    )


NEVER_AVAILABLE = (
    "deleting or archiving campaigns, ad groups, or ads (pause instead)",
    "creating campaigns, ad groups, ads, or audiences",
    "changing bids or bid targets such as target CPA or target ROAS (not admitted yet)",
    "raw or arbitrary API mutations",
    "applying any change without a human approval",
)
"""What the agent refuses, whatever the catalog offers, so it never offers them."""


def _proposal_id(args: dict[str, Any]) -> UUID | None:
    try:
        return UUID(str(args.get("proposal_id")))
    except (ValueError, TypeError):
        return None


def build_execute_gate(service: ProposalService) -> Callable[[ToolCall, ToolContext], bool]:
    """Pause only for a proposal that exists on this thread; anything else runs and is denied.

    The gate is evaluated again on resume, so it must not depend on state that the reviewer's
    decision changes (a rejection is persisted before the resume). Existence on the thread is
    stable; the executor still refuses anything that is not awaiting approval.
    """

    def gate(call: ToolCall, context: ToolContext) -> bool:
        proposal_id = _proposal_id(call.args)
        return proposal_id is not None and service.belongs_to(proposal_id, context.thread_id)

    return gate


def build_write_tools(service: ProposalService, executor: WriteExecutor) -> list[ToolSpec]:
    async def _propose(args: dict[str, Any], context: ToolContext) -> str:
        parsed = ProposeChangeArgs.model_validate(args)
        try:
            record = await service.propose(
                thread_id=context.thread_id,
                requester_ref=context.caller_ref,
                account_alias=parsed.account_alias,
                tool_name=parsed.tool_name,
                target_ref=parsed.target_ref,
                changes=parsed.changes,
                reason=parsed.reason,
                measurement_plan=parsed.measurement_plan,
                reversal_plan=parsed.reversal_plan,
            )
        except WriteDenied as exc:
            return json.dumps({"denied": True, "reason": exc.reason, "detail": exc.detail})
        view = ProposalView.from_record(record)
        return json.dumps(
            {
                "proposal": view.model_dump(mode="json"),
                "next_step": "Write the proposal summary and call execute_change with proposal_id and revision in the same message. The runtime pauses for human approval.",
            }
        )

    async def _execute(args: dict[str, Any], context: ToolContext) -> str:
        """Runs only after the reviewer approved the paused call.

        Surfaces that create the signed claim themselves (the API, the Slack adapter, the demo)
        pass straight through. Without one, the approval given to the paused call is recorded as
        a claim for the acting caller, but only while the proposal is still the revision that was
        presented: `revision` is frozen in the tool call, so an edit made in between is refused
        instead of executing unseen.
        """
        pid = _proposal_id(args)
        if pid is None:
            return json.dumps({"denied": True, "reason": "invalid_proposal_id"})
        record = service.get(pid)
        if record is None or not service.belongs_to(pid, context.thread_id):
            return json.dumps({"denied": True, "reason": "unknown_proposal"})
        current = record.changeset.revision
        try:
            if service.approvals.latest_unused(pid, current) is None:
                if current != int(args["revision"]):
                    return json.dumps(
                        {
                            "denied": True,
                            "reason": "proposal_revised",
                            "detail": f"revision {current} is current; present it again",
                        }
                    )
                service.approve(pid, approver_ref=context.caller_ref)
            receipt = await executor.execute(pid)
        except WriteDenied as exc:
            return json.dumps({"denied": True, "reason": exc.reason, "detail": exc.detail})
        return json.dumps({"receipt": ReceiptView.from_receipt(receipt).model_dump(mode="json")})

    def _get(args: dict[str, Any], context: ToolContext) -> str:
        pid = _proposal_id(args)
        record = service.get(pid) if pid is not None else None
        if pid is None:
            return json.dumps({"denied": True, "reason": "invalid_proposal_id"})
        if record is None or not service.belongs_to(pid, context.thread_id):
            return json.dumps({"denied": True, "reason": "unknown_proposal"})
        return json.dumps({"proposal": ProposalView.from_record(record).model_dump(mode="json")})

    def _discover(_args: dict[str, Any], _context: ToolContext) -> str:
        return json.dumps(
            {
                "operations": service.admitted_operations(),
                "never_available": list(NEVER_AVAILABLE),
                "execution_gate": executor.gate.describe(),
                "note": (
                    "Only these operations can be proposed. Each needs a human approval before "
                    "one execution attempt. Anything in never_available cannot be done here: say "
                    "so plainly and offer the closest admitted operation (usually pausing)."
                ),
            }
        )

    return [
        ToolSpec(
            name=DISCOVER_WRITE_OPERATIONS_TOOL,
            description="List the admitted mutation operations, their editable fields, units, risk, and the current execution gate.",
            parameters=parameters_for(_NoArgs),
            handler=_discover,
            kind="write",
        ),
        ToolSpec(
            name=PROPOSE_CHANGE_TOOL,
            description=(
                "Stage a typed change proposal for one admitted mutation. Reads current provider state for the "
                "before value, computes the digest, and persists it. Nothing is executed."
            ),
            parameters=parameters_for(ProposeChangeArgs),
            handler=_propose,
            kind="write",
        ),
        ToolSpec(
            name=EXECUTE_CHANGE_TOOL,
            description=(
                "Request execution of a staged proposal. The runtime pauses for human approval; the host verifies "
                "the signed approval, runs one mutation attempt, and reads back the result."
            ),
            parameters=parameters_for(ExecuteChangeArgs),
            handler=_execute,
            kind="write",
            gated=True,
        ),
        ToolSpec(
            name=GET_PROPOSAL_TOOL,
            description="Read the current persisted state of a proposal by id.",
            parameters=parameters_for(ProposalIdArgs),
            handler=_get,
            kind="write",
        ),
    ]
