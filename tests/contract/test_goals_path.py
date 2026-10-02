"""Account goals through the real paths: the agent proposes, an approver approves, code applies.

Also: goals in `list_accounts`, `check_pacing`, analyses judged against goals, and the CLI.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from paid_media_agent.analytics.goals import GoalStore, account_today
from paid_media_agent.analytics.sync import run_sync
from paid_media_agent.config import Settings
from paid_media_agent.harness.messages import AssistantMessage
from paid_media_agent.runtime.local import LocalRuntime
from paid_media_agent.store import Store
from paid_media_agent.surfaces.runner import AgentRunner
from paid_media_agent.testing.scripted_model import last_tool_results, tool_call_message
from paid_media_agent.tools.fixtures import FakeWriteProvider, FixtureState
from paid_media_agent.tools.host_writes import SET_ACCOUNT_GOALS
from paid_media_agent.tools.writes import ApprovalPolicy, WriteDenied
from tests.contract.helpers import build_runtime, config, execute_step, final_step, propose_step

TODAY = datetime.now(UTC).date()
POLICY = ApprovalPolicy(approver_refs=frozenset({"reviewer"}), allow_self_approval=False)


def _goal_proposal(**changes: Any) -> Any:
    return propose_step(
        account_alias="demo-meta",
        tool_name=SET_ACCOUNT_GOALS,
        target_ref="demo-meta",
        changes=changes or {"target_cpa": 40},
        reason="The user set a new target CPA.",
    )


def _runner(runtime: LocalRuntime) -> AgentRunner:
    return AgentRunner(
        agent=runtime.agent,
        service=runtime.components.proposal_service,
        receipts=runtime.profile.receipts,
        threads=Store().repositories.threads,
        executor=runtime.components.write_executor,
    )


def _runtime(settings: Settings, project_root: Path, steps: list[Any], **kwargs: Any) -> Any:
    state = FixtureState()
    provider = FakeWriteProvider(state)
    runtime, _ = build_runtime(
        settings,
        project_root,
        steps,
        fixture_state=state,
        write_provider=provider,
        approval_policy=POLICY,
        **kwargs,
    )
    return runtime, provider


async def test_the_agent_proposes_a_goal_and_it_applies_once_after_approval(
    settings: Settings, project_root: Path
) -> None:
    runtime, provider = _runtime(
        settings, project_root, [_goal_proposal(), execute_step, final_step]
    )
    store = runtime.profile.store
    goals = GoalStore(store)
    goals.set(
        "demo-meta",
        {"monthly_budget": 9000},
        effective_from=TODAY - timedelta(days=40),
        source="cli",
    )
    cfg = config()
    paused = await runtime.agent.send(cfg.thread_id, cfg.caller_ref, "Make our Meta target CPA 40.")
    assert paused.pending, "the goal change waits for approval"
    view = json.loads(
        next(m.content for m in paused.messages if getattr(m, "name", "") == "propose_change")
    )["proposal"]
    assert view["tool_name"] == SET_ACCOUNT_GOALS and view["target_ref"] == "demo-meta"
    assert [(v["field"], v["value"]) for v in view["before"]] == [("target_cpa", None)]
    assert [(v["field"], v["value"]) for v in view["after"]] == [("target_cpa", 40.0)]
    assert "goal_change" in view["risk_flags"]
    assert goals.current("demo-meta", TODAY).target_cpa is None, "nothing applied yet"

    runner = _runner(runtime)
    with pytest.raises(WriteDenied, match="approver_policy"):
        await runner.approve(proposal_id=view["proposal_id"], approver_ref="local-user")
    outcome = await runner.approve(proposal_id=view["proposal_id"], approver_ref="reviewer")
    assert outcome.receipt is not None and outcome.receipt.status == "verified"

    today = account_today(runtime.profile.accounts, "demo-meta")
    goal = goals.current("demo-meta", today)
    assert goal is not None and goal.target_cpa == 40.0 and goal.monthly_budget == 9000.0
    assert goal.source == "proposal" and str(goal.proposal_id) == view["proposal_id"]
    assert provider.mutation_calls == [], "a goal change never reaches a platform"

    receipt = await runtime.components.write_executor.execute(goal.proposal_id)
    assert receipt.status == "verified" and len(goals.history("demo-meta")) == 2, "applied once"
    events = store.fetch(
        "SELECT status, entity_type, field, after_value FROM change_events "
        "WHERE proposal_id = ? ORDER BY occurred_at, status",
        [goal.proposal_id],
    )
    assert [e[0] for e in events] == ["proposed", "approved", "verified"]
    assert {e[1] for e in events} == {"account"} and events[-1][2:] == ("target_cpa", "40.0")


async def test_goal_proposals_are_validated_rejectable_and_stopped_by_the_kill_switch(
    settings: Settings, project_root: Path, tmp_path: Path
) -> None:
    steps = [
        _goal_proposal(target_cpa=-5),
        lambda _m: tool_call_message(
            "propose_change",
            {
                "account_alias": "demo-meta",
                "tool_name": "host__delete_everything",
                "target_ref": "demo-meta",
                "changes": {"x": 1},
                "reason": "no",
            },
        ),
        propose_step(
            account_alias="demo-meta",
            tool_name=SET_ACCOUNT_GOALS,
            target_ref="demo-google",
            changes={"target_cpa": 25},
            reason="Wrong target.",
        ),
        lambda messages: AssistantMessage(json.dumps(last_tool_results(messages))),
    ]
    runtime, _ = _runtime(settings, project_root, steps)
    conversation = await runtime.agent.send("t-v", "local-user", "Set goals.")
    denials = [
        json.loads(m.content)
        for m in conversation.messages
        if getattr(m, "name", "") == "propose_change"
    ]
    assert [d.get("reason") for d in denials] == [
        "invalid_change",
        "unknown_tool",
        "invalid_target",
    ]

    kill = tmp_path / "KILL"
    runtime, _ = _runtime(
        settings.model_copy(update={"paid_media_kill_switch_path": kill}),
        project_root,
        [_goal_proposal(), execute_step, final_step, _goal_proposal(), execute_step, final_step],
    )
    runner = _runner(runtime)
    first = await runtime.agent.send("t-k", "local-user", "Target CPA 40.")
    pid = json.loads(
        next(m.content for m in first.messages if getattr(m, "name", "") == "propose_change")
    )["proposal"]["proposal_id"]
    rejected = await runner.reject(proposal_id=pid, actor_ref="reviewer", message="not yet")
    assert rejected.proposal is not None and rejected.proposal.state.value == "rejected"

    second = await runtime.agent.send("t-k", "local-user", "Try again.")
    pid = json.loads(
        [m.content for m in second.messages if getattr(m, "name", "") == "propose_change"][-1]
    )["proposal"]["proposal_id"]
    kill.write_text("incident")
    outcome = await runner.approve(proposal_id=pid, approver_ref="reviewer")
    assert outcome.receipt is not None and outcome.receipt.status == "rejected"
    assert "kill_switch" in outcome.receipt.reason
    assert GoalStore(runtime.profile.store).history("demo-meta") == []


async def test_goals_reach_list_accounts_pacing_and_period_comparisons(
    settings: Settings, project_root: Path
) -> None:
    steps = [
        lambda _m: tool_call_message("list_accounts", {}),
        lambda _m: tool_call_message("check_pacing", {"account_alias": "demo-google"}),
        lambda _m: tool_call_message("check_pacing", {"account_alias": "nobody"}),
        lambda messages: AssistantMessage(json.dumps(last_tool_results(messages))),
    ]
    anchored = settings.model_copy(update={"paid_media_fixture_anchor": TODAY - timedelta(days=2)})
    runtime, _ = build_runtime(
        anchored,
        project_root,
        steps,
        fixture_state=FixtureState(anchored.paid_media_fixture_anchor),
    )
    store = runtime.profile.store
    await run_sync(
        accounts=runtime.profile.accounts,
        catalog=runtime.catalog,
        dispatcher=runtime.components.read_dispatcher,
        end=TODAY - timedelta(days=1),
    )
    GoalStore(store).set(
        "demo-google",
        {"target_cpa": 30, "monthly_budget": 25000},
        effective_from=TODAY - timedelta(days=60),
        source="cli",
    )
    conversation = await runtime.agent.send("g-1", "local-user", "How are we pacing?")
    results = {
        getattr(m, "name", ""): json.loads(m.content)
        for m in conversation.messages
        if getattr(m, "name", "") in ("list_accounts",)
    }
    accounts = {a["alias"]: a for a in results["list_accounts"]["accounts"]}
    assert accounts["demo-google"]["goals"]["target_cpa"] == 30.0
    assert accounts["demo-meta"]["goals"] is None
    pacing, unknown = [
        json.loads(m.content)
        for m in conversation.messages
        if getattr(m, "name", "") == "check_pacing"
    ]
    report = pacing["accounts"][0]
    assert report["monthly_budget"] == 25000.0 and report["target_cpa"] == 30.0
    if report["data_through"] is None:
        # Data ends two days ago: on a month's first two days, no day of it is in yet.
        assert report["reading"].startswith("No spend recorded")
    else:
        assert "against a 30.00 target" in report["reading"]
    assert "fixture-" not in json.dumps(pacing)
    assert unknown["error"] is True and "unknown account alias" in unknown["detail"]

    from paid_media_agent.tools.compare_periods import ComparePeriodsArgs, run_compare_periods

    read = await runtime.components.read_dispatcher.execute(
        "google_ads__get_campaign_performance",
        {
            "account_alias": "demo-google",
            "start_date": (TODAY - timedelta(days=15)).isoformat(),
            "end_date": (TODAY - timedelta(days=2)).isoformat(),
        },
    )
    args = ComparePeriodsArgs(
        artifact_ids=[read.artifact_id],
        current_start=TODAY - timedelta(days=8),
        current_end=TODAY - timedelta(days=2),
        previous_start=TODAY - timedelta(days=15),
        previous_end=TODAY - timedelta(days=9),
    )
    judged = run_compare_periods(runtime.profile.artifacts, args, goals=GoalStore(store).current)
    (line,) = judged["against_goals"]
    assert line["account_alias"] == "demo-google" and line["target_cpa"] == 30.0
    assert line["reading"].startswith("CPA ") and "30.00 target" in line["reading"]
    assert "against_goals" not in run_compare_periods(runtime.profile.artifacts, args)


def test_the_cli_sets_shows_and_paces_and_the_accounts_file_keeps_every_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAID_MEDIA_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("PAID_MEDIA_STATE_PATH", str(tmp_path / "state" / "pma.duckdb"))
    monkeypatch.setenv("PAID_MEDIA_DATA_MODE", "sample")
    monkeypatch.setenv("PAID_MEDIA_FIXTURE_ANCHOR", (TODAY - timedelta(days=2)).isoformat())
    runner = CliRunner()
    assert runner.invoke(main_cli(), ["sync"]).exit_code == 0
    made = runner.invoke(
        main_cli(),
        [
            "goals",
            "set",
            "--alias",
            "demo-google",
            "--target-cpa",
            "30",
            "--monthly-budget",
            "25000",
        ],
    )
    assert made.exit_code == 0, made.output
    assert "target CPA 30.00, monthly budget 25,000.00" in made.output
    cleared = runner.invoke(
        main_cli(), ["goals", "set", "--alias", "demo-google", "--clear", "target_cpa"]
    )
    assert "monthly budget 25,000.00" in cleared.output and "target CPA" not in cleared.output
    assert (
        runner.invoke(
            main_cli(), ["goals", "set", "--alias", "nobody", "--target-cpa", "1"]
        ).exit_code
        != 0
    )
    shown = json.loads(runner.invoke(main_cli(), ["goals", "show", "--json"]).output)
    google = next(g for g in shown["goals"] if g["account_alias"] == "demo-google")
    assert google["current"]["target_cpa"] is None and len(google["history"]) == 1, "same day"
    paced = json.loads(
        runner.invoke(main_cli(), ["pacing", "--alias", "demo-google", "--json"]).output
    )
    assert paced["accounts"][0]["monthly_budget"] == 25000.0
    history = runner.invoke(main_cli(), ["history", "--view", "goals"])
    assert "demo-google" in history.output and "cli" in history.output

    created = runner.invoke(main_cli(), ["context", "init"])
    skill = tmp_path / "workspace" / "skills" / "company-context" / "SKILL.md"
    assert created.exit_code == 0 and skill.exists() and "goals set" in skill.read_text()
    skill.write_text("mine")
    assert "left unchanged" in runner.invoke(main_cli(), ["context", "init"]).output
    assert skill.read_text() == "mine"

    from paid_media_agent.admin.accounts_file import read_accounts, render_accounts, write_accounts
    from paid_media_agent.config import AccountBinding, AccountRegistry
    from paid_media_agent.domain.common import Platform

    path = tmp_path / "accounts.toml"
    binding = AccountBinding(
        alias="shop",
        platform=Platform.META_ADS,
        provider_account_id="act_1",
        currency="USD",
        timezone="UTC",
        conversion_action="offsite_conversion.fb_pixel_purchase",
    )
    write_accounts(path, AccountRegistry(bindings=(binding,)))
    assert read_accounts(path).resolve("shop") == binding
    assert 'conversion_action = "offsite_conversion.fb_pixel_purchase"' in render_accounts(
        read_accounts(path)
    )


def main_cli() -> Any:
    from paid_media_agent.cli import main

    return main


def test_the_api_sets_goals_for_approvers_only_and_reports_pacing(
    settings: Settings, project_root: Path
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from paid_media_agent.surfaces.api.app import create_app

    runtime, _ = _runtime(settings, project_root, [])

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
    reviewer, other = {"Authorization": "Bearer tok-rev"}, {"Authorization": "Bearer tok-other"}
    body = {"account_alias": "demo-google", "target_roas": 4, "monthly_budget": 12000}

    assert client.post("/goals", json=body, headers=other).status_code == 403
    assert (
        client.post("/goals", json={**body, "account_alias": "x"}, headers=reviewer).status_code
        == 422
    )
    saved = client.post("/goals", json=body, headers=reviewer)
    assert saved.status_code == 200 and saved.json()["goal"]["source"] == "api:reviewer"
    listed = client.get("/goals?alias=demo-google", headers=other).json()["goals"][0]
    assert listed["current"]["target_roas"] == 4.0 and len(listed["history"]) == 1
    assert client.get("/goals?alias=nobody", headers=other).status_code == 404
    paced = client.get("/pacing?alias=demo-google", headers=other).json()["accounts"][0]
    assert paced["monthly_budget"] == 12000.0 and paced["target_roas"] == 4.0
    assert client.get("/goals").status_code == 401


async def test_the_accounts_and_goals_are_in_the_prompt_and_change_only_with_them(
    settings: Settings, project_root: Path
) -> None:
    from paid_media_agent.testing.scripted_model import ScriptedChatModel

    runtime, model = build_runtime(settings, project_root, [lambda _m: AssistantMessage("ok")] * 3)
    assert isinstance(model, ScriptedChatModel)
    await runtime.agent.send("p-1", "local-user", "hello")
    await runtime.agent.send("p-2", "local-user", "hello again")
    first, second = model.systems
    assert first == second, "the same prompt on every call while nothing changes: it caches"
    assert "- demo-google: google_ads, USD" in first and "goals: none set" in first
    GoalStore(runtime.profile.store).set(
        "demo-google", {"target_cpa": 30}, effective_from=TODAY - timedelta(days=1), source="cli"
    )
    await runtime.agent.send("p-3", "local-user", "and now?")
    assert "demo-google: google_ads, USD" in model.systems[-1]
    assert "goals: target_cpa 30.0" in model.systems[-1], "a goal change shows on the next call"
