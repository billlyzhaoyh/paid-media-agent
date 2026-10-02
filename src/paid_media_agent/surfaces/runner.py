"""Run caller-owned conversations and resolve host-approved actions."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from paid_media_agent.domain.common import JsonValue
from paid_media_agent.domain.presentation import ProposalView, ReceiptView
from paid_media_agent.domain.proposals import ProposalState
from paid_media_agent.harness.loop import Agent, EventHandler, PlanResume, ResumePlan, RunEvent
from paid_media_agent.harness.messages import (
    AssistantMessage,
    Conversation,
    Message,
    ToolCall,
    ToolMessage,
)
from paid_media_agent.persistence.interfaces import ReceiptRepository, ThreadOwnershipStore
from paid_media_agent.tools.artifacts import ArtifactError, ArtifactStore
from paid_media_agent.tools.reports import RENDER_REPORT_TOOL
from paid_media_agent.tools.writes import (
    ProposalService,
    WriteDenied,
    WriteExecutor,
    is_host_thread,
)

__all__ = ["AgentRunner", "EventHandler", "RunEvent", "RunOutcome", "ThreadAccessDenied"]


@dataclass(frozen=True)
class RunOutcome:
    thread_id: str
    text: str
    interrupted: bool
    proposal: ProposalView | None
    receipt: ReceiptView | None


class ThreadAccessDenied(Exception):
    pass


def _content_text(content: str) -> str:
    return content.strip()


def _call_proposal(call: ToolCall) -> UUID | None:
    """The proposal a paused call executes, parsed as the gate parses it."""
    try:
        return UUID(str(call.args.get("proposal_id")))
    except (ValueError, TypeError):
        return None


def _last_assistant_text(messages: Sequence[Message]) -> str:
    """Prose from the most recent assistant message, so a pause does not discard it."""
    for message in reversed(messages):
        if isinstance(message, AssistantMessage) and message.content:
            return message.content[:2000]
    return ""


class AgentRunner:
    """Runs the agent for a caller-owned thread and exposes proposal actions."""

    def __init__(
        self,
        *,
        agent: Agent,
        service: ProposalService,
        receipts: ReceiptRepository,
        threads: ThreadOwnershipStore,
        executor: WriteExecutor | None = None,
    ) -> None:
        self._agent = agent
        self._service = service
        self._receipts = receipts
        self._threads = threads
        self._executor = executor

    def _claim(self, thread_id: str, caller_ref: str) -> None:
        if is_host_thread(thread_id):
            raise ThreadAccessDenied("thread ids starting with 'host:' are reserved")
        if not self._threads.claim(thread_id, caller_ref):
            raise ThreadAccessDenied("thread belongs to another caller")

    def latest_proposal(self, thread_id: str) -> ProposalView | None:
        records = self._service.proposals.list_for_thread(thread_id)
        return ProposalView.from_record(records[-1]) if records else None

    def _decided(self, thread_id: str, call: ToolCall) -> bool:
        """A paused call whose proposal can no longer be approved: it would hold the thread."""
        proposal_id = _call_proposal(call)
        record = self._service.get(proposal_id) if proposal_id is not None else None
        return (
            record is None
            or record.changeset.thread_id != thread_id
            or record.state is not ProposalState.AWAITING_APPROVAL
        )

    def _plan(
        self, thread_id: str, proposal_id: UUID, decide: Callable[[bool], None]
    ) -> PlanResume:
        """Resume this proposal's paused calls and deny any paused on a proposal already decided.

        It runs inside the thread's turn, so `decide(paused)` (creating the claim or recording the
        rejection) sees the calls exactly as they are when they resume.
        """

        def plan(paused: Sequence[ToolCall]) -> ResumePlan:
            ours = [c.id for c in paused if _call_proposal(c) == proposal_id]
            decide(bool(ours))
            stale = {
                c.id: "that proposal was already decided; propose the change again"
                for c in paused
                if c.id not in ours and self._decided(thread_id, c)
            }
            return ResumePlan(decide=ours, deny=stale)

        return plan

    def paused_proposal(self, thread_id: str) -> ProposalView | None:
        """The proposal the thread's first paused call would execute: what a reviewer decides."""
        for call in self._agent.conversation(thread_id).pending:
            if self._decided(thread_id, call):
                continue
            proposal_id = _call_proposal(call)
            record = self._service.get(proposal_id) if proposal_id is not None else None
            if record is not None:
                return ProposalView.from_record(record)
        return None

    async def report_files(
        self, *, thread_id: str, caller_ref: str, artifacts: ArtifactStore
    ) -> set[str]:
        if self._threads.owner(thread_id) != caller_ref:
            raise ThreadAccessDenied("thread belongs to another caller")
        files: set[str] = set()
        for message in self._agent.conversation(thread_id).messages:
            if not isinstance(message, ToolMessage) or message.name != RENDER_REPORT_TOOL:
                continue
            if message.status == "error":
                continue
            try:
                result = json.loads(message.content)
                if isinstance(result, dict) and result.get("offloaded"):
                    record = artifacts.read(result["artifact_id"])
                    if record.metadata.tool_name != RENDER_REPORT_TOOL or not isinstance(
                        record.payload, dict
                    ):
                        continue
                    result = json.loads(str(record.payload.get("content", "")))
            except (ValueError, KeyError, TypeError, ArtifactError):
                continue
            if isinstance(result, dict):
                for file in result.get("files", []):
                    if isinstance(file, dict) and isinstance(file.get("path"), str):
                        files.add(file["path"])
        return files

    def _outcome(
        self, thread_id: str, conversation: Conversation, decided: UUID | None = None
    ) -> RunOutcome:
        """`decided` is the proposal a reviewer just approved: its receipt is returned even when
        another change on the thread is still waiting, which is then the card shown."""
        messages = conversation.messages
        text = ""
        if messages and isinstance(messages[-1], AssistantMessage):
            text = messages[-1].content
        interrupted = conversation.awaiting_approval
        # The card shows what the paused call would execute, not merely the newest proposal.
        proposal = self.paused_proposal(thread_id) if interrupted else None
        if proposal is None and decided is not None:
            proposal = self.proposal(decided)
        proposal = proposal or self.latest_proposal(thread_id)
        receipt_for = decided or (proposal.proposal_id if proposal is not None else None)
        stored = self._receipts.get(receipt_for) if receipt_for is not None else None
        receipt = ReceiptView.from_receipt(stored) if stored else None
        if interrupted and proposal is not None:
            prose = _last_assistant_text(messages)
            if receipt is not None and decided is not None:
                prose = f"Approved; the change is {receipt.status}."
            text = (prose + "\n\n" if prose else "") + "A change is waiting for review."
        return RunOutcome(
            thread_id=thread_id,
            text=text,
            interrupted=interrupted,
            proposal=proposal,
            receipt=receipt,
        )

    async def send(
        self, *, thread_id: str, caller_ref: str, text: str, on_event: EventHandler | None = None
    ) -> RunOutcome:
        self._claim(thread_id, caller_ref)
        conversation = await self._agent.send(thread_id, caller_ref, text, on_event)
        return self._outcome(thread_id, conversation)

    async def resume(
        self,
        *,
        thread_id: str,
        caller_ref: str,
        decision: str,
        message: str = "",
        on_event: EventHandler | None = None,
        call_ids: list[str] | None = None,
        plan: PlanResume | None = None,
        decided: UUID | None = None,
    ) -> RunOutcome:
        self._claim(thread_id, caller_ref)
        conversation = await self._agent.resume(
            thread_id,
            caller_ref,
            "approve" if decision == "approve" else "reject",
            message,
            on_event,
            call_ids=call_ids,
            plan=plan,
        )
        return self._outcome(thread_id, conversation, decided)

    async def _approve_host(self, proposal_id: UUID, approver_ref: str) -> RunOutcome:
        if self._executor is None:
            raise WriteDenied("host_approval_unavailable", "this surface cannot execute changes")
        record = self._service.get(proposal_id)
        if record is not None and record.state is ProposalState.AWAITING_APPROVAL:
            self._service.approve(proposal_id, approver_ref=approver_ref)
        # A replay of an executed proposal returns its stored receipt.
        receipt = await self._executor.execute(proposal_id)
        return self._host_outcome(proposal_id, f"Approved; the change is {receipt.status}.")

    def _host_outcome(self, proposal_id: UUID, text: str) -> RunOutcome:
        record = self._service.get(proposal_id)
        stored = self._receipts.get(proposal_id)
        return RunOutcome(
            thread_id=record.changeset.thread_id if record else "",
            text=text,
            interrupted=False,
            proposal=ProposalView.from_record(record) if record else None,
            receipt=ReceiptView.from_receipt(stored) if stored else None,
        )

    def pending_proposals(self, limit: int = 50) -> list[ProposalView]:
        """Every proposal awaiting a decision; host proposals are only reviewable from here."""
        return [ProposalView.from_record(r) for r in self._service.proposals.list_awaiting(limit)]

    def proposal_by_routing_id(self, routing_id: str) -> ProposalView | None:
        record = self._service.proposals.get_by_routing_id(routing_id)
        return ProposalView.from_record(record) if record else None

    def proposal(self, proposal_id: UUID) -> ProposalView | None:
        record = self._service.get(proposal_id)
        return ProposalView.from_record(record) if record else None

    async def approve(
        self, *, proposal_id: UUID, approver_ref: str, on_event: EventHandler | None = None
    ) -> RunOutcome:
        """Host creates the claim, then the paused call resumes and the executor verifies it.

        A host proposal (the budget bandit's) has no conversation: the claim is created and the
        executor runs it directly, with the same claim, digest, catalog, gate, and readback checks.
        """
        proposal_id = UUID(str(proposal_id))  # callers may pass the id as text
        record = self._service.get(proposal_id)
        if record is None:
            raise WriteDenied("unknown_proposal")
        if is_host_thread(record.changeset.thread_id):
            return await self._approve_host(proposal_id, approver_ref)
        thread_id = record.changeset.thread_id

        def claim(paused: bool) -> None:
            if not paused:
                # No paused call executes this proposal (the conversation moved on, or another
                # proposal is paused), so a claim would go unused or reach the wrong change.
                raise WriteDenied(
                    "conversation_expired",
                    "this proposal is not paused for review; propose it again",
                )
            self._service.approve(proposal_id, approver_ref=approver_ref)

        return await self.resume(
            thread_id=thread_id,
            caller_ref=record.changeset.requester_ref,
            decision="approve",
            on_event=on_event,
            plan=self._plan(thread_id, proposal_id, claim),
            decided=proposal_id,
        )

    async def reject(
        self,
        *,
        proposal_id: UUID,
        actor_ref: str,
        message: str = "",
        on_event: EventHandler | None = None,
    ) -> RunOutcome:
        proposal_id = UUID(str(proposal_id))
        record = self._service.get(proposal_id)
        if record is None:
            raise WriteDenied("unknown_proposal")
        thread_id = record.changeset.thread_id
        if is_host_thread(thread_id):
            self._service.reject(proposal_id, actor_ref=actor_ref, message=message)
            return self._host_outcome(proposal_id, "Rejected; nothing was changed.")
        resumed = False

        def refuse(paused: bool) -> None:
            nonlocal resumed
            self._service.reject(proposal_id, actor_ref=actor_ref, message=message)
            resumed = paused

        outcome = await self.resume(
            thread_id=thread_id,
            caller_ref=record.changeset.requester_ref,
            decision="reject",
            message=message or "rejected by reviewer",
            on_event=on_event,
            plan=self._plan(thread_id, proposal_id, refuse),
        )
        return (
            outcome
            if resumed
            else self._host_outcome(proposal_id, "Rejected; nothing was changed.")
        )

    def edit(
        self, *, proposal_id: UUID, editor_ref: str, changes: dict[str, JsonValue]
    ) -> ProposalView:
        record = self._service.get(proposal_id)
        if record is None:
            raise WriteDenied("unknown_proposal")
        if record.state is not ProposalState.AWAITING_APPROVAL:
            raise WriteDenied("not_awaiting_approval", record.state.value)
        updated = self._service.revise(proposal_id, editor_ref=editor_ref, changes=changes)
        return ProposalView.from_record(updated)

    def receipt(self, proposal_id: UUID) -> ReceiptView | None:
        stored = self._receipts.get(proposal_id)
        return ReceiptView.from_receipt(stored) if stored else None
