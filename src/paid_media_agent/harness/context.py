"""Keep a thread inside a token budget by stubbing old tool results in the view sent to the model.

Stored history is never rewritten: this only shapes what one model call sees, so a restarted agent
builds the same view from the same thread. Tool results are the bulk of a long thread; the oldest
are replaced first by a short stub that keeps any artifact ids (so earlier reads can still be
compared or rendered) and the first words of the result. Results since the latest user message are
kept, unless that turn alone is over budget, in which case its newest few are kept. Every tool call
still has exactly one result, and assistant messages are untouched.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from paid_media_agent.harness.messages import AssistantMessage, Message, ToolMessage, UserMessage
from paid_media_agent.harness.models import ToolSchema

CHARS_PER_TOKEN = 3.5
"""A conservative characters-per-token ratio for JSON-heavy threads."""
KEEP_IN_TURN = 4
SUMMARY_CHARS = 200
STUB_NOTE = "earlier result removed to save context; call the tool again if you need it"


@dataclass(frozen=True)
class ContextView:
    messages: list[Message]
    est_tokens: int
    stubbed: int = 0


def _chars(message: Message) -> int:
    if isinstance(message, AssistantMessage):
        calls = sum(len(json.dumps(c.args)) + len(c.name) for c in message.tool_calls)
        state = len(json.dumps(message.provider_state)) if message.provider_state else 0
        return len(message.content) + calls + state
    return len(message.content)


def estimate_tokens(system: str, messages: Sequence[Message], tools: Sequence[ToolSchema]) -> int:
    chars = len(system) + sum(len(json.dumps(t.as_openai())) for t in tools)
    chars += sum(_chars(m) for m in messages)
    return int(chars / CHARS_PER_TOKEN)


def _artifact_ids(value: Any, found: list[str], depth: int = 0) -> None:
    if depth > 3:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "artifact_id" and isinstance(item, str):
                found.append(item)
            elif key == "artifact_ids" and isinstance(item, list):
                found.extend(str(i) for i in item)
            else:
                _artifact_ids(item, found, depth + 1)
    elif isinstance(value, list):
        for item in value[:50]:
            _artifact_ids(item, found, depth + 1)


def stub(message: ToolMessage) -> ToolMessage:
    """A short stand-in for a tool result, keeping its artifact ids and its first words."""
    summary = message.content[:SUMMARY_CHARS]
    ids: list[str] = []
    try:
        parsed = json.loads(message.content)
    except ValueError:
        parsed = None
    if parsed is not None:
        _artifact_ids(parsed, ids)
        if isinstance(parsed, dict):
            for key in ("reading", "summary", "detail"):
                if isinstance(parsed.get(key), str):
                    summary = parsed[key][:SUMMARY_CHARS]
                    break
    body: dict[str, Any] = {"stubbed": True, "tool": message.name}
    if ids:
        body["artifact_ids"] = list(dict.fromkeys(ids))
    body["summary"] = summary
    body["note"] = STUB_NOTE
    return replace(message, content=json.dumps(body))


def fit_to_budget(
    system: str, messages: Sequence[Message], tools: Sequence[ToolSchema], budget: int
) -> ContextView:
    """The messages to send, with the oldest tool results stubbed until the estimate fits."""
    view = list(messages)
    estimate = estimate_tokens(system, view, tools)
    if budget <= 0 or estimate <= budget:
        return ContextView(view, estimate)
    last_user = max((i for i, m in enumerate(view) if isinstance(m, UserMessage)), default=-1)
    before = [i for i, m in enumerate(view) if isinstance(m, ToolMessage) and i < last_user]
    current = [i for i, m in enumerate(view) if isinstance(m, ToolMessage) and i > last_user]
    candidates = before + current[: max(len(current) - KEEP_IN_TURN, 0)]
    stubbed = 0
    for index in candidates:
        if estimate <= budget:
            break
        original = view[index]
        assert isinstance(original, ToolMessage)  # noqa: S101 - candidates are tool results
        replacement = stub(original)
        saved = len(original.content) - len(replacement.content)
        if saved <= 0:
            continue
        view[index] = replacement
        estimate -= int(saved / CHARS_PER_TOKEN)
        stubbed += 1
    return ContextView(view, estimate, stubbed)
