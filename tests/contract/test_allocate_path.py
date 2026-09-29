"""Budget recommendations on sample accounts, and bandit proposals approved without a conversation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from paid_media_agent.analytics.sync import run_sync
from paid_media_agent.bandit.live import allocate_accounts, live_config
from paid_media_agent.config import Settings
from paid_media_agent.domain.common import Platform
from paid_media_agent.harness.messages import AssistantMessage
from paid_media_agent.runtime.local import LocalRuntime
from paid_media_agent.store import Store
from paid_media_agent.surfaces.runner import AgentRunner, ThreadAccessDenied
from paid_media_agent.testing.scripted_model import last_tool_results, tool_call_message
from paid_media_agent.tools.fixtures import FakeWriteProvider, FixtureState
from paid_media_agent.tools.writes import ApprovalPolicy
from tests.contract.helpers import build_runtime

# Pulls are stamped with the real time, so the sample data ends two days ago and decisions are
# made today, as they would be live.
TODAY = datetime.now(UTC).date()


async def _synced(
    settings: Settings,
    project_root: Path,
    steps: list[Any] | None = None,
    *,
    approval_policy: ApprovalPolicy | None = None,
) -> tuple[LocalRuntime, FixtureState, FakeWriteProvider]:
    anchored = settings.model_copy(update={"paid_media_fixture_anchor": TODAY - timedelta(days=2)})
    state = FixtureState(anchored.paid_media_fixture_anchor)
    provider = FakeWriteProvider(state)
    runtime, _ = build_runtime(
        anchored,
        project_root,
        steps or [],
        fixture_state=state,
        write_provider=provider,
        approval_policy=approval_policy,
    )
    await run_sync(
        accounts=runtime.profile.accounts,
        catalog=runtime.catalog,
        dispatcher=runtime.components.read_dispatcher,
        end=TODAY - timedelta(days=1),
    )
    return runtime, state, provider


async def _propose(runtime: LocalRuntime) -> dict[str, Any]:
    (run,) = await allocate_accounts(
        runtime.profile.store,
        None,
        aliases=("demo-google",),
        as_of=TODAY,
        config=live_config("greedy"),
        service=runtime.components.proposal_service,
        propose=True,
    )
    return run


async def test_a_bandit_proposal_is_approved_through_the_api_and_applied_once(
    settings: Settings, project_root: Path
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from paid_media_agent.surfaces.api.app import create_app

    policy = ApprovalPolicy(approver_refs=frozenset({"reviewer"}), allow_self_approval=False)
    runtime, state, provider = await _synced(settings, project_root, approval_policy=policy)
    run = await _propose(runtime)
    assert run["data_checks"]["fresh"]["ok"] and not run["fallback_used"]
    proposed = run["proposals"]["proposals"]
    assert proposed, run
    ref, proposal_id = next(iter(proposed.items()))
    decision = next(d for d in run["decisions"] if d["entity_ref"] == ref)
    original = (state.campaign(Platform.GOOGLE_ADS, ref) or {})["daily_budget"]
    assert original == decision["current_budget"] and provider.mutation_calls == []

    class Holder:
        pass

    holder: Any = Holder()
    holder.settings = settings.model_copy(
        update={"paid_media_api_tokens": SecretStr("tok-rev:reviewer,tok-other:someone")}
    )
    holder.agent = runtime.agent
    holder.components = runtime.components
    holder.profile = runtime.profile
    holder.catalog = runtime.catalog
    holder.threads = Store().repositories.threads
    holder.persistence = "memory"
    client = TestClient(create_app(holder))
    reviewer = {"Authorization": "Bearer tok-rev"}
    other = {"Authorization": "Bearer tok-other"}

    assert client.get("/proposals", headers=other).status_code == 403, "approvers only"
    listed = client.get("/proposals", headers=reviewer).json()["proposals"]
    assert proposal_id in {p["proposal_id"] for p in listed}
    assert all(p["requester_ref"] == "bandit" for p in listed)
    assert client.post(f"/proposals/{proposal_id}/approve", headers=other).status_code == 403

    approved = client.post(f"/proposals/{proposal_id}/approve", headers=reviewer)
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["receipt"]["status"] == "verified" and body["interrupted"] is False
    applied = (state.campaign(Platform.GOOGLE_ADS, ref) or {})["daily_budget"]
    assert applied == pytest.approx(round(decision["final_budget"], 2))
    assert len(provider.mutation_calls) == 1

    replay = client.post(f"/proposals/{proposal_id}/approve", headers=reviewer)
    assert replay.status_code == 200 and replay.json()["receipt"]["status"] == "verified"
    assert len(provider.mutation_calls) == 1, "one mutation, however often it is approved"
    statuses = [
        s
        for (s,) in runtime.profile.store.fetch(
            "SELECT status FROM change_events WHERE proposal_id = ? ORDER BY occurred_at",
            [proposal_id],
        )
    ]
    assert statuses == ["proposed", "approved", "verified"]
    assert runtime.profile.store.fetch(
        "SELECT count(*) FROM bandit_decisions WHERE proposal_id = ?", [proposal_id]
    ) == [(1,)]

    # The next decision sees the approved budget and holds it, before any sync observes it.
    again = await _propose(runtime)
    row = next(d for d in again["decisions"] if d["entity_ref"] == ref)
    assert row["current_budget"] == pytest.approx(round(decision["final_budget"], 2))
    assert row["constrained_by"] == ["hold"] and ref not in again["proposals"]["proposals"]


async def test_host_threads_are_reserved_and_rejects_need_no_conversation(
    settings: Settings, project_root: Path
) -> None:
    runtime, _, provider = await _synced(settings, project_root)
    first = await _propose(runtime)
    runner = AgentRunner(
        agent=runtime.agent,
        service=runtime.components.proposal_service,
        receipts=runtime.profile.receipts,
        threads=Store().repositories.threads,
        executor=runtime.components.write_executor,
    )
    with pytest.raises(ThreadAccessDenied, match="reserved"):
        await runner.send(thread_id="host:bandit:x", caller_ref="local-user", text="hi")

    ref, proposal_id = next(iter(first["proposals"]["proposals"].items()))
    rejected = await runner.reject(proposal_id=proposal_id, actor_ref="local-user", message="no")
    assert rejected.proposal is not None and rejected.proposal.state.value == "rejected"
    assert provider.mutation_calls == []

    # A newer run supersedes the older run's proposals that still await a decision.
    second = await _propose(runtime)
    third = await _propose(runtime)
    assert set(third["proposals"]["superseded"]) == set(second["proposals"]["proposals"].values())
    pending = {v.proposal_id for v in runner.pending_proposals()}
    assert {str(p) for p in pending} == set(third["proposals"]["proposals"].values())

    # A run that holds every campaign (no move is big enough) leaves no stale proposal behind.
    (held,) = await allocate_accounts(
        runtime.profile.store,
        None,
        aliases=("demo-google",),
        as_of=TODAY,
        config=live_config("greedy"),
        service=runtime.components.proposal_service,
        propose=True,
        min_change=0.99,
    )
    assert held["proposals"]["proposals"] == {}
    assert set(held["proposals"]["superseded"]) == set(third["proposals"]["proposals"].values())
    assert runner.pending_proposals() == []


async def test_the_agent_recommends_budgets_without_changing_anything(
    settings: Settings, project_root: Path
) -> None:
    steps = [
        lambda _m: tool_call_message("recommend_budgets", {"account_alias": "demo-google"}),
        lambda _m: tool_call_message("recommend_budgets", {"account_alias": "nobody"}),
        lambda messages: AssistantMessage(json.dumps(last_tool_results(messages))),
    ]
    runtime, _, provider = await _synced(settings, project_root, steps)
    conversation = await runtime.agent.send("a-1", "local-user", "How should I split the budget?")
    known, unknown = [
        json.loads(m.content)
        for m in conversation.messages
        if getattr(m, "name", "") == "recommend_budgets"
    ]
    assert known["total_budget"] == pytest.approx(900.0)
    assert (
        "Current budgets total 900.00 USD a day; the recommended total is 900.00 USD"
        in (known["summary"])
    ), "never leaves the model to guess which total is today's"
    readings = [c["reading"] for c in known["campaigns"]]
    assert len(readings) == 3 and all(r.startswith("g-10") for r in readings)
    assert "nothing has changed" in known["note"]
    assert "Expected conversions" in known["summary"] and "as recommended" in known["summary"]
    moved = [c for c in known["campaigns"] if c["change"]]
    assert all("at the current budget" in c["reading"] for c in moved)
    # The fixture's own signals say what limits each campaign; the readings say it back.
    limits = {c["entity_ref"]: c["constraint"]["kind"] for c in known["campaigns"]}
    assert limits == {"g-101": "demand", "g-102": "budget", "g-103": "target"}
    readings = {c["entity_ref"]: c["reading"] for c in known["campaigns"]}
    assert (
        "limited by its budget (high confidence: platform: BUDGET_CONSTRAINED" in readings["g-102"]
    )
    assert "loosen the target rather than the budget" in readings["g-103"]
    assert "cannot spend more of their budget" in " ".join(known["notes"])
    from paid_media_agent.analytics.history import query_history

    kinds, _ = query_history(runtime.profile.store, "constraints", account_alias="demo-google")
    assert {r["entity_ref"]: r["kind"] for r in kinds} == limits
    shares, _ = query_history(runtime.profile.store, "signals", entity_ref="g-102", limit=1)
    assert shares[0]["budget_lost_share"] == 0.24
    assert "fixture-" not in json.dumps(known), "provider ids stay host-side"
    assert unknown["error"] is True and "unknown account alias" in unknown["detail"]
    assert provider.mutation_calls == []
    assert runtime.profile.store.fetch("SELECT count(*), any_value(mode) FROM bandit_runs") == [
        (1, "recommend")
    ]


async def test_the_allocate_job_recommends_and_proposes_only_when_enabled(
    settings: Settings, project_root: Path, tmp_path: Path
) -> None:
    from paid_media_agent.runtime.self_hosted import build_self_hosted_runtime
    from paid_media_agent.scheduler import Scheduler, build_jobs
    from paid_media_agent.testing.scripted_model import ScriptedChatModel

    for propose, expected in ((False, 0), (True, 1)):
        runtime = build_self_hosted_runtime(
            settings.model_copy(
                update={
                    "paid_media_data_mode": "sample",
                    "paid_media_fixture_anchor": TODAY - timedelta(days=2),
                    "paid_media_bandit_propose": propose,
                }
            ),
            project_root=project_root,
            model=ScriptedChatModel(steps=[]),
            store=Store(tmp_path / f"state-{propose}.duckdb"),
        )
        scheduler = Scheduler(runtime.store, build_jobs(runtime))
        assert (await scheduler.run_now("sync")).status == "ok"
        run = await scheduler.run_now("allocate")
        assert run.status == "ok", run.detail
        accounts = {a["account_alias"]: a for a in run.detail["accounts"]}
        assert set(accounts) == {"demo-google", "demo-meta", "demo-reddit"}
        proposing = [a for a in accounts.values() if a.get("proposals")]
        assert (len(proposing) > 0) == bool(expected)
        runtime.store.close()


def test_the_cli_allocates_proposes_and_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from paid_media_agent.cli import main

    monkeypatch.setenv("PAID_MEDIA_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("PAID_MEDIA_STATE_PATH", str(tmp_path / "state" / "pma.duckdb"))
    monkeypatch.setenv("PAID_MEDIA_DATA_MODE", "sample")
    monkeypatch.setenv("PAID_MEDIA_FIXTURE_ANCHOR", (TODAY - timedelta(days=2)).isoformat())
    monkeypatch.setenv("PAID_MEDIA_APPROVAL_SIGNING_KEY", "test-signing-key-with-enough-bytes")
    runner = CliRunner()
    assert runner.invoke(main, ["sync"]).exit_code == 0
    shown = runner.invoke(main, ["allocate", "--alias", "demo-meta"])
    assert shown.exit_code == 0, shown.output
    assert "m-203 Video Views - Awareness: not allocated (status PAUSED)." in shown.output
    assert "proposed" not in shown.output
    assert runner.invoke(main, ["proposals", "list"]).output.strip() == (
        "No proposals awaiting approval."
    )

    made = runner.invoke(main, ["allocate", "--alias", "demo-google", "--propose", "--json"])
    assert made.exit_code == 0, made.output
    (run,) = json.loads(made.output)
    ids = set(run["proposals"]["proposals"].values())
    listed = json.loads(runner.invoke(main, ["proposals", "list", "--json"]).output)
    assert {p["proposal_id"] for p in listed["proposals"]} == ids
    assert runner.invoke(main, ["allocate", "--alias", "nobody"]).exit_code != 0
