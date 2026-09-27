"""Builders for real-loop contract tests: scripted models, local runtimes, and step helpers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paid_media_agent.config import Settings
from paid_media_agent.harness.loop import Decision
from paid_media_agent.harness.messages import AssistantMessage, Conversation, Message
from paid_media_agent.runtime.local import LocalRuntime, build_local_runtime
from paid_media_agent.runtime.profiles import RuntimeProfile, fixture_profile
from paid_media_agent.store import Store
from paid_media_agent.testing.scripted_model import (
    ScriptedChatModel,
    Step,
    last_tool_results,
    tool_call_message,
)
from paid_media_agent.tools.catalog import AuthorizedToolCatalog, StaticCatalogProvider
from paid_media_agent.tools.fixtures import FakeWriteProvider, FixtureState, build_fixture_catalog
from paid_media_agent.tools.writes import ApprovalPolicy


@dataclass(frozen=True)
class RunConfig:
    thread_id: str
    caller_ref: str


def config(thread_id: str = "t-1", caller: str = "local-user") -> RunConfig:
    return RunConfig(thread_id=thread_id, caller_ref=caller)


def propose_step(**overrides: Any) -> Step:
    args: dict[str, Any] = {
        "account_alias": "demo-google",
        "tool_name": "google_ads__update_campaign_budget",
        "target_ref": "g-103",
        "changes": {"daily_budget": 240},
        "reason": "Reduce budget after CPA rose.",
    }
    args.update(overrides)
    return lambda _messages: tool_call_message("propose_change", args)


def execute_step(messages: Sequence[Message]) -> AssistantMessage:
    proposal = next((r for r in last_tool_results(messages) if "proposal" in r), None)
    if proposal is None:
        return AssistantMessage(content=f"proposal failed: {last_tool_results(messages)}")
    view = proposal["proposal"]
    return tool_call_message(
        "execute_change", {"proposal_id": view["proposal_id"], "revision": view["revision"]}
    )


def final_step(messages: Sequence[Message]) -> AssistantMessage:
    results = last_tool_results(messages)
    return AssistantMessage(content=f"done: {results[-1] if results else 'no results'}")


def build_runtime(
    settings: Settings,
    project_root: Path,
    steps: list[Step],
    *,
    catalog: AuthorizedToolCatalog | None = None,
    catalog_provider: StaticCatalogProvider | None = None,
    fixture_state: FixtureState | None = None,
    write_provider: FakeWriteProvider | None = None,
    approval_policy: ApprovalPolicy | None = None,
    profile: RuntimeProfile | None = None,
    store: Store | None = None,
) -> tuple[LocalRuntime, ScriptedChatModel]:
    model = ScriptedChatModel(steps=steps)
    resolved_catalog = catalog or build_fixture_catalog()
    provider = catalog_provider or StaticCatalogProvider(resolved_catalog)
    state = fixture_state or FixtureState()
    resolved_profile = profile or fixture_profile(
        settings,
        project_root=project_root,
        catalog_provider=provider,
        workspace_root=settings.paid_media_workspace_root,
        fixture_state=state,
        write_provider=write_provider,
        approval_policy=approval_policy,
        store=store,
    )
    runtime = build_local_runtime(
        settings,
        project_root=project_root,
        model=model,
        catalog=resolved_catalog,
        catalog_provider=provider,
        profile=resolved_profile,
    )
    return runtime, model


async def run_until_interrupt(
    runtime: LocalRuntime, cfg: RunConfig, text: str = "Change the budget."
) -> Conversation:
    return await runtime.agent.send(cfg.thread_id, cfg.caller_ref, text)


async def resume(
    runtime: LocalRuntime, cfg: RunConfig, decision: Decision = "approve"
) -> Conversation:
    return await runtime.agent.resume(cfg.thread_id, cfg.caller_ref, decision)
