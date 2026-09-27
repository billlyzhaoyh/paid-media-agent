"""A scripted chat model: deterministic tool calls and prose, no network."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from paid_media_agent.harness.messages import AssistantMessage, Message, ToolCall, ToolMessage
from paid_media_agent.harness.models import ToolSchema

Step = Callable[[Sequence[Message]], AssistantMessage]


def tool_call_message(name: str, args: dict[str, Any], *, content: str = "") -> AssistantMessage:
    return AssistantMessage(
        content=content,
        tool_calls=(ToolCall(id=f"call_{uuid.uuid4().hex[:12]}", name=name, args=args),),
    )


def _parsed(message: ToolMessage) -> dict[str, Any] | None:
    try:
        parsed = json.loads(message.content)
    except json.JSONDecodeError:
        parsed = {"raw": message.content}
    if not isinstance(parsed, dict):
        return None
    parsed.setdefault("_tool_name", message.name)
    parsed.setdefault("_status", message.status)
    return parsed


def last_tool_results(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Parse JSON tool results that arrived since the last assistant message."""
    results: list[dict[str, Any]] = []
    for message in reversed(messages):
        if isinstance(message, AssistantMessage):
            break
        if isinstance(message, ToolMessage) and (parsed := _parsed(message)) is not None:
            results.append(parsed)
    results.reverse()
    return results


def all_tool_results(messages: Sequence[Message]) -> list[dict[str, Any]]:
    return [
        parsed
        for m in messages
        if isinstance(m, ToolMessage) and (parsed := _parsed(m)) is not None and "raw" not in parsed
    ]


@dataclass
class ScriptedChatModel:
    """Runs a fixed list of steps. Each step sees the conversation and returns one reply."""

    steps: list[Step]
    model_name: str = "demo"
    bound_tool_batches: list[list[dict[str, Any]]] = field(default_factory=list)
    call_count: int = 0

    @property
    def name(self) -> str:
        return f"scripted:{self.model_name}"

    async def complete(
        self,
        *,
        system: str,  # noqa: ARG002 - the script ignores the prompt
        messages: Sequence[Message],
        tools: Sequence[ToolSchema],
    ) -> AssistantMessage:
        self.bound_tool_batches.append([t.as_openai() for t in tools])
        index = self.call_count
        self.call_count += 1
        if index >= len(self.steps):
            return AssistantMessage(content="Script exhausted.")
        return self.steps[index](messages)
