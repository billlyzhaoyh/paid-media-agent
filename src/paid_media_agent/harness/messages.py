"""Conversation messages. Provider adapters translate to and from these; nothing else does."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ToolStatus = Literal["success", "error"]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]
    invalid_arguments: str | None = None
    """The raw argument text when the model sent something that is not a JSON object."""


@dataclass(frozen=True)
class UserMessage:
    content: str
    role: Literal["user"] = "user"


@dataclass(frozen=True)
class AssistantMessage:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    provider_state: dict[str, Any] | None = None
    """Opaque provider data (reasoning blocks, signatures) replayed unmodified on the next call."""
    role: Literal["assistant"] = "assistant"


@dataclass(frozen=True)
class ToolMessage:
    tool_call_id: str
    name: str
    content: str
    status: ToolStatus = "success"
    role: Literal["tool"] = "tool"


Message = UserMessage | AssistantMessage | ToolMessage


@dataclass(frozen=True)
class Conversation:
    """A thread's history plus any tool calls paused for human approval."""

    messages: tuple[Message, ...] = ()
    pending: tuple[ToolCall, ...] = ()
    activated_tools: tuple[str, ...] = field(default_factory=tuple)

    @property
    def awaiting_approval(self) -> bool:
        return bool(self.pending)
