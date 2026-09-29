"""Regressions caught while running the agent behind the self-hosted API."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from paid_media_agent.harness.files import build_file_tools
from paid_media_agent.harness.loop import Agent
from paid_media_agent.harness.messages import AssistantMessage, Message, ToolCall
from paid_media_agent.harness.models import ToolSchema, parse_assistant
from paid_media_agent.harness.tools import ToolContext, ToolDispatcher, ToolSpec
from paid_media_agent.store import Store
from paid_media_agent.store.conversations import ConversationStore
from paid_media_agent.surfaces.runner import _content_text, _last_assistant_text
from paid_media_agent.tools.artifacts import ArtifactStore
from paid_media_agent.tools.catalog import StaticCatalogProvider
from paid_media_agent.tools.fixtures import build_fixture_catalog


def _dispatcher(tmp_path: Path, tools: Sequence[ToolSpec] = (), **kwargs: int) -> ToolDispatcher:
    return ToolDispatcher(
        tools={t.name: t for t in tools},
        catalog_provider=StaticCatalogProvider(build_fixture_catalog()),
        artifacts=ArtifactStore(tmp_path / "workspace"),
        **kwargs,
    )


class _Recorder:
    """Records the system prompt of each call; optionally stalls first."""

    def __init__(self, delay: float = 0.0) -> None:
        self.systems: list[str] = []
        self._delay = delay

    @property
    def name(self) -> str:
        return "recorder"

    async def complete(
        self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolSchema]
    ) -> AssistantMessage:
        self.systems.append(system)
        await asyncio.sleep(self._delay)
        return AssistantMessage(content="answer")


def _agent(tmp_path: Path, model: _Recorder, **kwargs: object) -> Agent:
    return Agent(
        model=model,
        system_prompt="Base instructions.",
        dispatcher=_dispatcher(tmp_path),
        conversations=ConversationStore(Store()),
        gate=lambda _call, _context: False,
        **kwargs,  # type: ignore[arg-type]
    )


def test_outcome_text_is_the_assistant_prose() -> None:
    assert _content_text("  plain  ") == "plain"
    assert _last_assistant_text([AssistantMessage(content="demo-google spent the most.")]) == (
        "demo-google spent the most."
    )


def test_non_text_content_blocks_never_reach_a_client() -> None:
    """The API returned hidden block content; only the text may reach a client."""
    parsed = parse_assistant(
        {
            "content": [
                {"type": "text", "text": "demo-google spent the most."},
                {"type": "reasoning", "text": "hidden"},
            ]
        }
    )
    assert parsed.content == "demo-google spent the most."


async def test_current_date_is_appended_per_model_call(tmp_path: Path) -> None:
    """Relative windows must resolve from today, not from the model's training-time year."""
    model = _Recorder()
    agent = _agent(tmp_path, model, clock=lambda: datetime(2026, 9, 2, 12, tzinfo=UTC))
    await agent.send("t-1", "local-user", "What happened last week?")
    assert model.systems[-1].startswith("Base instructions.")
    assert "Today is Wednesday 2026-09-02 (UTC)" in model.systems[-1]
    assert "last week (Monday to Sunday): 2026-08-24 to 2026-08-30" in model.systems[-1]


async def test_offload_leaves_paged_filesystem_tools_alone(tmp_path: Path) -> None:
    """A read_file result over the budget was offloaded into a new artifact, which the model then
    had to read, which was offloaded again."""
    big = "x" * 500
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / "big.txt").write_text(big)
    read = ToolSpec(
        name="google_ads__get_campaign_performance",
        description="read",
        parameters={"type": "object", "properties": {}},
        handler=lambda _args, _ctx: big,
        kind="read",
    )
    dispatcher = _dispatcher(tmp_path, [*build_file_tools(tmp_path), read], offload_chars=100)
    context = ToolContext("t-1", "local-user")
    kept = await dispatcher.dispatch(
        ToolCall("call-1", "read_file", {"file_path": "/workspace/big.txt"}), context
    )
    assert big in kept.content and "offloaded" not in kept.content
    offloaded = await dispatcher.dispatch(ToolCall("call-2", read.name, {}), context)
    assert '"offloaded": true' in offloaded.content


async def test_model_timeout_turns_a_stalled_call_into_an_error(tmp_path: Path) -> None:
    stalled = _agent(tmp_path, _Recorder(delay=1), model_timeout_seconds=0.05, model_attempts=1)
    conversation = await stalled.send("t-1", "local-user", "hi")
    reply = conversation.messages[-1]
    assert isinstance(reply, AssistantMessage)
    assert reply.content.startswith("Model call failed after 1 attempts with TimeoutError")
    quick = _agent(tmp_path, _Recorder(), model_timeout_seconds=0.05, model_attempts=1)
    assert (await quick.send("t-1", "local-user", "hi")).messages[-1].content == "answer"


async def test_an_offloaded_result_keeps_its_meaning_and_can_be_read_in_pages(
    tmp_path: Path,
) -> None:
    import json

    from paid_media_agent.tools.artifact_read import build_read_artifact_tool

    body = json.dumps(
        {"headline": [{"platform": "google_ads", "spend": 1}, {"platform": "meta_ads", "spend": 2}],
         "caveats": ["attribution differs"], "rows": ["x" * 50] * 400}
    )  # fmt: skip
    big = ToolSpec(
        name="summarize_window",
        description="big",
        parameters={"type": "object", "properties": {}},
        handler=lambda _args, _ctx: body,
    )
    dispatcher = _dispatcher(tmp_path, [big], offload_chars=1000)
    reader = build_read_artifact_tool(dispatcher.artifacts)
    dispatcher.tools[reader.name] = reader
    context = ToolContext("t-1", "local-user")
    stub = json.loads(
        (await dispatcher.dispatch(ToolCall("c1", "summarize_window", {}), context)).content
    )
    assert stub["offloaded"] and [h["platform"] for h in stub["headline"]] == [
        "google_ads",
        "meta_ads",
    ]
    assert stub["caveats"] == ["attribution differs"] and "read_artifact" in stub["note"]
    first = json.loads(
        (
            await dispatcher.dispatch(
                ToolCall("c2", "read_artifact", {"artifact_id": stub["artifact_id"]}), context
            )
        ).content
    )
    assert first["total_chars"] == len(body) and first["text"] == body[:5000]
    rest = json.loads(
        (await dispatcher.dispatch(
            ToolCall("c3", "read_artifact", {"artifact_id": stub["artifact_id"], "offset": first["next_offset"]}),
            context,
        )).content
    )  # fmt: skip
    assert rest["text"] == body[5000:10000] and "offloaded" not in rest
    bad = json.loads(
        (
            await dispatcher.dispatch(
                ToolCall("c4", "read_artifact", {"artifact_id": "../.env"}), context
            )
        ).content
    )
    assert bad["error"] is True
