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
