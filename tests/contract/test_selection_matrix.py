"""Tool selection: platform reads are bound only after discover_tools activates them."""

from __future__ import annotations

import json
from pathlib import Path

from paid_media_agent.assembly import CORE_TOOLS, FILESYSTEM_TOOLS, TOOL_SELECTION, WRITE_TOOLS
from paid_media_agent.config import Settings
from paid_media_agent.harness.messages import AssistantMessage, ToolMessage
from paid_media_agent.testing.scripted_model import ScriptedChatModel, tool_call_message
from paid_media_agent.tools.fixtures import build_fixture_catalog
from tests.contract.helpers import build_runtime, config, run_until_interrupt

ALWAYS_BOUND = {*CORE_TOOLS, *WRITE_TOOLS, *FILESYSTEM_TOOLS}
CATALOG = build_fixture_catalog()
READ_NAMES = {e.qualified_name for e in CATALOG.read_entries()}
MUTATION_NAMES = {e.qualified_name for e in CATALOG.mutation_entries()}


def _names(batch: list[dict]) -> set[str]:  # type: ignore[type-arg]
    return {t["function"]["name"] for t in batch}


def _platform(batch: list[dict]) -> set[str]:  # type: ignore[type-arg]
    return _names(batch) & READ_NAMES


def _discover(query: str, platform: str | None = None):  # type: ignore[no-untyped-def]
    args: dict[str, str] = {"query": query}
    if platform:
        args["platform"] = platform
    return lambda _m: tool_call_message("discover_tools", args)


def _done(_messages: object) -> AssistantMessage:
    return AssistantMessage(content="ok")


async def test_nothing_platform_specific_is_bound_before_discovery(
    settings: Settings, project_root: Path
) -> None:
    runtime, model = build_runtime(settings, project_root, [_done])
    assert runtime.components.metadata.selection == TOOL_SELECTION == "discover_tools"
    await run_until_interrupt(runtime, config(), "Which campaigns need attention?")
    batch = model.bound_tool_batches[-1]
    assert _names(batch) == ALWAYS_BOUND
    assert not _platform(batch)
    assert not _names(batch) & MUTATION_NAMES
    assert not {"task", "execute", "delete"} & _names(batch)
    # Every authorized read is registered even though none is bound yet.
    assert READ_NAMES <= set(runtime.components.metadata.tool_names)


async def test_discover_tools_activates_matching_reads_for_the_thread(
    settings: Settings, project_root: Path
) -> None:
    runtime, model = build_runtime(
        settings,
        project_root,
        [_discover("campaign performance", "google_ads"), _done, _done],
    )
    conversation = await run_until_interrupt(runtime, config(), "Compare campaign performance.")
    found = json.loads(next(m for m in conversation.messages if isinstance(m, ToolMessage)).content)
    expected = {t["name"] for t in found["tools"]}
    assert expected and all(name.startswith("google_ads__") for name in expected)
    assert not _platform(model.bound_tool_batches[0]), "the discovery call itself binds nothing"
    assert _platform(model.bound_tool_batches[1]) == expected
    assert set(conversation.activated_tools) == expected
    assert not _names(model.bound_tool_batches[1]) & MUTATION_NAMES

    # Activation persists for the thread and does not leak into another thread.
    await run_until_interrupt(runtime, config(), "And again.")
    assert _platform(model.bound_tool_batches[2]) == expected
    assert runtime.agent.conversation("other").activated_tools == ()
    assert not {s.name for s in runtime.agent.bound_tools("other")} & READ_NAMES


async def test_activation_is_capped_and_drops_the_oldest(
    settings: Settings, project_root: Path
) -> None:
    capped = settings.model_copy(update={"paid_media_max_selected_tools": 2})
    runtime, model = build_runtime(
        capped,
        project_root,
        [_discover("campaign performance"), _discover("list campaigns", "reddit_ads"), _done],
    )
    assert runtime.components.metadata.max_active_reads == 2
    conversation = await run_until_interrupt(runtime, config(), "Look at everything.")
    first = json.loads(next(m for m in conversation.messages if isinstance(m, ToolMessage)).content)
    assert len(first["tools"]) > 2, "the search found more tools than the cap"
    after_first = _platform(model.bound_tool_batches[1])
    assert after_first == {t["name"] for t in first["tools"][-2:]}
    after_second = model.bound_tool_batches[2]
    assert len(_platform(after_second)) == 2
    assert "reddit_ads__list_campaigns" in _platform(after_second)
    assert len(conversation.activated_tools) == 2
    assert _names(after_second) >= ALWAYS_BOUND, "the cap never drops core, write, or file tools"


async def test_unknown_names_never_widen_the_bound_set(
    settings: Settings, project_root: Path
) -> None:
    runtime, model = build_runtime(
        settings,
        project_root,
        [
            _discover("zzzz nothing matches this"),
            lambda _m: tool_call_message(
                "google_ads__update_campaign_budget",
                {"account_alias": "demo-google", "campaign_id": "g-101", "daily_budget": 1},
            ),
            lambda _m: tool_call_message("google_ads__made_up_report", {}),
            _done,
        ],
    )
    conversation = await run_until_interrupt(runtime, config(), "Change budgets.")
    for batch in model.bound_tool_batches:
        assert not _platform(batch)
        assert _names(batch) == ALWAYS_BOUND
    assert conversation.activated_tools == ()
    denied = [
        json.loads(m.content)
        for m in conversation.messages
        if isinstance(m, ToolMessage) and m.status == "error"
    ]
    assert [d["tool"] for d in denied] == [
        "google_ads__update_campaign_budget",
        "google_ads__made_up_report",
    ]
    assert runtime.profile.write_provider.mutation_calls == []  # type: ignore[attr-defined]

    # A stored activation naming a mutation or an unknown tool binds nothing either.
    runtime.agent.conversations.set_activated(
        "t-1", ["google_ads__update_campaign_budget", "made_up__tool", "google_ads__list_campaigns"]
    )
    bound = {s.name for s in runtime.agent.bound_tools("t-1")}
    assert bound & (READ_NAMES | MUTATION_NAMES | {"made_up__tool"}) == {
        "google_ads__list_campaigns"
    }


async def test_authorized_reads_are_callable_without_being_bound(
    settings: Settings, project_root: Path
) -> None:
    runtime, model = build_runtime(
        settings,
        project_root,
        [
            lambda _m: tool_call_message(
                "google_ads__get_campaign_performance",
                {
                    "account_alias": "demo-google",
                    "start_date": "2026-08-01",
                    "end_date": "2026-08-28",
                },
            ),
            _done,
        ],
    )
    conversation = await run_until_interrupt(runtime, config(), "Read Google.")
    result = json.loads(
        next(m for m in conversation.messages if isinstance(m, ToolMessage)).content
    )
    assert result["kind"] == "read_result", result
    assert len(runtime.components.read_dispatcher.audit) == 1
    assert not runtime.components.dispatcher.denials
    assert all(not _platform(batch) for batch in model.bound_tool_batches)


async def test_every_model_spec_exposes_the_same_authorized_surface(
    settings: Settings, project_root: Path
) -> None:
    surfaces = []
    for spec in ("anthropic:claude-sonnet-4-6", "google_genai:gemini-3-flash", "openai:gpt-5.5"):
        runtime, _ = build_runtime(
            settings.model_copy(update={"paid_media_model": spec}), project_root, [_done]
        )
        assert isinstance(runtime.agent.model, ScriptedChatModel)
        surfaces.append(
            (
                runtime.components.metadata.selection,
                set(runtime.components.metadata.tool_names),
                runtime.components.metadata.catalog_revision,
            )
        )
    assert all(surface == surfaces[0] for surface in surfaces)
