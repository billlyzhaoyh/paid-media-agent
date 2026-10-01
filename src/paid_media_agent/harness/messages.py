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
    origin: Literal["user", "host"] = "user"
    """`host` for a note the runtime adds within a turn (an answer repair). The model reads it;
    people never see it, and it does not start a new turn."""


@dataclass(frozen=True)
class Usage:
    """What one model call used, as the provider reported it. Never stored in the thread."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    """Input tokens read from the provider's prompt cache."""
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None
    """The provider's own figure (OpenRouter reports one); None when it reports none."""
    response_model: str | None = None
    generation_id: str | None = None


@dataclass(frozen=True)
class AssistantMessage:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    provider_state: dict[str, Any] | None = None
    """Opaque provider data (reasoning blocks, signatures) replayed unmodified on the next call."""
    role: Literal["assistant"] = "assistant"
    usage: Usage | None = field(default=None, compare=False, repr=False)
    """The call that produced this reply; recorded in `llm_calls`, never replayed or stored."""


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
