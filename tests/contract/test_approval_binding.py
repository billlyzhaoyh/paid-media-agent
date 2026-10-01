"""An approval decides exactly the proposal it names, and only people who may decide can."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr

from paid_media_agent.config import Settings
from paid_media_agent.domain.proposals import ProposalState
from paid_media_agent.store import Store
from paid_media_agent.surfaces.runner import AgentRunner
from paid_media_agent.testing.scripted_model import last_tool_results, tool_call_message
from paid_media_agent.tools.fixtures import FakeWriteProvider, FixtureState
from paid_media_agent.tools.writes import ApprovalPolicy, WriteDenied
from tests.contract.helpers import build_runtime, execute_step, final_step, propose_step


def _runner(runtime: Any) -> AgentRunner:
    return AgentRunner(
        agent=runtime.agent,
        service=runtime.components.proposal_service,
        receipts=runtime.profile.receipts,
        threads=Store().repositories.threads,
        executor=runtime.components.write_executor,
    )


@pytest.mark.parametrize("self_approval", [True, False])
async def test_approving_a_stale_proposal_never_applies_the_paused_one(
    settings: Settings, project_root: Path, self_approval: bool
) -> None:
    state = FixtureState()
    provider = FakeWriteProvider(state)
    policy = ApprovalPolicy(
        approver_refs=frozenset({"local-user", "reviewer-1"}), allow_self_approval=self_approval
    )
    steps = [
        propose_step(),
        execute_step,
        propose_step(changes={"daily_budget": 10}),
        execute_step,
        final_step,
    ]
    runtime, _ = build_runtime(
        settings, project_root, steps, fixture_state=state, write_provider=provider,
        approval_policy=policy,
    )  # fmt: skip
    runner = _runner(runtime)
    first = await runner.send(thread_id="t", caller_ref="local-user", text="cut budget")
    second = await runner.send(thread_id="t", caller_ref="local-user", text="actually cut to 10")
    p1, p2 = first.proposal.proposal_id, second.proposal.proposal_id
    assert p1 != p2 and second.interrupted
    with pytest.raises(WriteDenied, match="conversation_expired"):
        await runner.approve(proposal_id=p1, approver_ref="reviewer-1")
    service = runtime.components.proposal_service
    assert service.get(p2).state is ProposalState.AWAITING_APPROVAL, "P2 was not touched"
    assert provider.mutation_calls == [], "nothing was applied"
    assert runtime.agent.conversation("t").pending, "P2 is still paused for its own decision"


async def test_the_card_shows_and_approval_executes_the_paused_proposal(
    settings: Settings, project_root: Path
) -> None:
    first: dict[str, Any] = {}

    def remember(messages: Any) -> Any:
        first["p"] = last_tool_results(messages)[0]["proposal"]
        return propose_step(changes={"daily_budget": 10})(messages)

    def execute_first(_messages: Any) -> Any:
        view = first["p"]
        return tool_call_message(
            "execute_change", {"proposal_id": view["proposal_id"], "revision": view["revision"]}
        )

    state = FixtureState()
    provider = FakeWriteProvider(state)
    policy = ApprovalPolicy(approver_refs=frozenset({"reviewer-1"}), allow_self_approval=False)
    runtime, _ = build_runtime(
        settings, project_root, [propose_step(), remember, execute_first, final_step],
        fixture_state=state, write_provider=provider, approval_policy=policy,
    )  # fmt: skip
    runner = _runner(runtime)
    outcome = await runner.send(thread_id="t", caller_ref="local-user", text="x")
    paused = UUID(runtime.agent.conversation("t").pending[0].args["proposal_id"])
    assert outcome.proposal.proposal_id == paused, "the card shows what the paused call executes"
    latest = runner.latest_proposal("t").proposal_id
    assert latest != paused
    with pytest.raises(WriteDenied):
        await runner.approve(proposal_id=latest, approver_ref="reviewer-1")
    await runner.approve(proposal_id=paused, approver_ref="reviewer-1")
    service = runtime.components.proposal_service
    assert service.get(paused).state is ProposalState.VERIFIED
    assert service.get(latest).state is ProposalState.AWAITING_APPROVAL
    assert len(provider.mutation_calls) == 1


async def test_only_the_requester_or_an_approver_can_edit_or_reject(
    settings: Settings, project_root: Path
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from paid_media_agent.surfaces.api.app import create_app

    tokens = "tok-req:req,tok-rev:api-reviewer,tok-x:outsider"
    configured = settings.model_copy(update={"paid_media_api_tokens": SecretStr(tokens)})
    policy = ApprovalPolicy(approver_refs=frozenset({"api-reviewer"}), allow_self_approval=False)
    runtime, _ = build_runtime(
        configured, project_root, [propose_step(), execute_step, final_step, final_step],
        fixture_state=FixtureState(), approval_policy=policy,
    )  # fmt: skip

    class Holder:
        pass

    holder: Any = Holder()
    holder.settings = configured
    holder.agent = runtime.agent
    holder.components = runtime.components
    holder.profile = runtime.profile
    holder.catalog = runtime.catalog
    holder.threads = Store().repositories.threads
    holder.persistence = "memory"
    client = TestClient(create_app(holder))
    sent = client.post(
        "/threads/t/messages", json={"text": "x"}, headers={"Authorization": "Bearer tok-req"}
    )
    pid = sent.json()["proposal"]["proposal_id"]
    outsider = {"Authorization": "Bearer tok-x"}
    edit = client.post(
        f"/proposals/{pid}/edit", json={"changes": {"daily_budget": 1}}, headers=outsider
    )
    reject = client.post(f"/proposals/{pid}/reject", json={"message": "no"}, headers=outsider)
    assert (edit.status_code, reject.status_code) == (403, 403)
    record = runtime.components.proposal_service.get(UUID(pid))
    assert record.state is ProposalState.AWAITING_APPROVAL and record.changeset.revision == 1
    rejected = client.post(
        f"/proposals/{pid}/reject",
        json={"message": "no"},
        headers={"Authorization": "Bearer tok-rev"},
    )
    assert rejected.status_code == 200


async def test_a_pause_always_gives_the_reviewer_the_code_written_summary(
    settings: Settings, project_root: Path
) -> None:
    from paid_media_agent.harness.messages import AssistantMessage

    runtime, _ = build_runtime(
        settings, project_root, [propose_step(), execute_step, final_step],
        fixture_state=FixtureState(),
    )  # fmt: skip
    conversation = await runtime.agent.send("t", "local-user", "cut the PMax budget to 240")
    assert conversation.awaiting_approval
    paused = [m for m in conversation.messages if isinstance(m, AssistantMessage)][-1]
    assert paused.tool_calls and paused.tool_calls[0].name == "execute_change"
    text = paused.content
    assert text.startswith("Proposed change for review") and "daily_budget:" in text
    assert "-> 240" in text and "Risk:" in text and "Nothing changes until" in text

    def narrated(messages: Any) -> Any:
        call = execute_step(messages)
        return AssistantMessage("Proposing a cut to 230 a day.", tool_calls=call.tool_calls)

    runtime, _ = build_runtime(
        settings, project_root, [propose_step(), narrated, final_step],
        fixture_state=FixtureState(),
    )  # fmt: skip
    conversation = await runtime.agent.send("t", "local-user", "cut the PMax budget to 240")
    last = [m for m in conversation.messages if isinstance(m, AssistantMessage)][-1]
    assert last.content.startswith("Proposed change for review"), "the record, not a retyping"
    assert "230" not in last.content and "-> 240" in last.content


def _proposals(messages: Any) -> list[dict[str, Any]]:
    import json

    return [
        json.loads(m.content)["proposal"]
        for m in messages
        if getattr(m, "name", "") == "propose_change" and '"proposal"' in m.content
    ]


def _execute_both(messages: Any) -> Any:
    """Both proposals executed in one message, the first with its id upper-cased."""
    from paid_media_agent.harness.messages import AssistantMessage, ToolCall

    first, second = _proposals(messages)[-2:]
    return AssistantMessage(
        "",
        tool_calls=(
            ToolCall("call_a", "execute_change",
                     {"proposal_id": first["proposal_id"].upper(), "revision": 1}),
            ToolCall("call_b", "execute_change",
                     {"proposal_id": second["proposal_id"], "revision": 1}),
        ),
    )  # fmt: skip


def _two_paused(settings: Settings, project_root: Path, steps: list[Any]) -> tuple[Any, Any]:
    state = FixtureState()
    provider = FakeWriteProvider(state)
    policy = ApprovalPolicy(approver_refs=frozenset({"reviewer-1"}), allow_self_approval=False)
    runtime, _ = build_runtime(
        settings, project_root,
        [propose_step(), propose_step(target_ref="g-101", changes={"daily_budget": 150}),
         _execute_both, *steps],
        fixture_state=state, write_provider=provider, approval_policy=policy,
    )  # fmt: skip
    return runtime, provider


async def test_approving_one_of_two_paused_changes_returns_its_receipt(
    settings: Settings, project_root: Path
) -> None:
    runtime, provider = _two_paused(settings, project_root, [final_step])
    runner = _runner(runtime)
    await runner.send(thread_id="t", caller_ref="local-user", text="cut both")
    first, second = (
        UUID(p["proposal_id"]) for p in _proposals(runtime.agent.conversation("t").messages)
    )
    assert len(runtime.agent.conversation("t").pending) == 2
    outcome = await runner.approve(proposal_id=first, approver_ref="reviewer-1")
    assert outcome.receipt is not None and outcome.receipt.proposal_id == first, (
        "an upper-cased id still names the paused call, and its receipt comes back"
    )
    assert outcome.receipt.status == "verified" and len(provider.mutation_calls) == 1
    assert outcome.interrupted and outcome.proposal.proposal_id == second, "the next card"
    assert outcome.text.startswith("Approved; the change is verified.")


async def test_a_call_paused_on_a_decided_proposal_never_holds_the_thread(
    settings: Settings, project_root: Path
) -> None:
    import json

    runtime, provider = _two_paused(settings, project_root, [final_step])
    runner = _runner(runtime)
    await runner.send(thread_id="t", caller_ref="local-user", text="cut both")
    first, second = (
        UUID(p["proposal_id"]) for p in _proposals(runtime.agent.conversation("t").messages)
    )
    service = runtime.components.proposal_service
    service.reject(first, actor_ref="reviewer-1", message="decided elsewhere")
    assert runner.paused_proposal("t").proposal_id == second, "a decided proposal is no card"
    with pytest.raises(WriteDenied, match="not_awaiting_approval"):
        await runner.reject(proposal_id=first, actor_ref="reviewer-1")
    outcome = await runner.approve(proposal_id=second, approver_ref="reviewer-1")
    assert not outcome.interrupted and outcome.text.startswith("done:"), "the model ran on"
    results = {
        m.tool_call_id: json.loads(m.content)
        for m in runtime.agent.conversation("t").messages
        if getattr(m, "name", "") == "execute_change"
    }
    assert results["call_a"]["reason"] == "already_decided"
    assert results["call_b"]["receipt"]["status"] == "verified"
    assert len(provider.mutation_calls) == 1


async def test_executing_a_decided_proposal_is_refused_without_a_pause(
    settings: Settings, project_root: Path
) -> None:
    import json

    remembered: dict[str, Any] = {}

    def remember(messages: Any) -> Any:
        remembered["p"] = last_tool_results(messages)[0]["proposal"]
        return execute_step(messages)

    def execute_again(_messages: Any) -> Any:
        view = remembered["p"]
        return tool_call_message(
            "execute_change", {"proposal_id": view["proposal_id"], "revision": view["revision"]}
        )

    policy = ApprovalPolicy(approver_refs=frozenset({"reviewer-1"}), allow_self_approval=False)
    runtime, _ = build_runtime(
        settings, project_root, [propose_step(), remember, final_step, execute_again, final_step],
        fixture_state=FixtureState(), approval_policy=policy,
    )  # fmt: skip
    runner = _runner(runtime)
    await runner.send(thread_id="t", caller_ref="local-user", text="cut it")
    await runner.reject(proposal_id=remembered["p"]["proposal_id"], actor_ref="reviewer-1")
    again = await runner.send(thread_id="t", caller_ref="local-user", text="do it anyway")
    assert not again.interrupted, "a rejected proposal never pauses again"
    last = [
        m
        for m in runtime.agent.conversation("t").messages
        if getattr(m, "name", "") == "execute_change"
    ][-1]
    assert json.loads(last.content)["denied"] is True


async def test_an_approval_racing_a_new_message_leaves_no_unused_claim(
    settings: Settings, project_root: Path
) -> None:
    import asyncio

    policy = ApprovalPolicy(approver_refs=frozenset({"reviewer-1"}), allow_self_approval=False)
    runtime, _ = build_runtime(
        settings, project_root, [propose_step(), execute_step, final_step, final_step],
        fixture_state=FixtureState(), approval_policy=policy,
    )  # fmt: skip
    runner = _runner(runtime)
    paused = await runner.send(thread_id="t", caller_ref="local-user", text="cut it")
    pid = paused.proposal.proposal_id
    # A turn is still running; a new message and an approval queue behind it, in that order.
    turn = runtime.agent._turn("t")
    await turn.acquire()
    moving_on = asyncio.create_task(
        runner.send(thread_id="t", caller_ref="local-user", text="never mind")
    )
    await asyncio.sleep(0)
    approving = asyncio.create_task(runner.approve(proposal_id=pid, approver_ref="reviewer-1"))
    await asyncio.sleep(0)
    turn.release()
    moved_on, approved = await asyncio.gather(moving_on, approving, return_exceptions=True)
    assert not isinstance(moved_on, BaseException)
    assert isinstance(approved, WriteDenied) and approved.reason == "conversation_expired"
    service = runtime.components.proposal_service
    assert service.approvals.latest_unused(pid, 1) is None, "no claim was left for later"
    assert service.get(pid).state is ProposalState.AWAITING_APPROVAL
