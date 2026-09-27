"""Conversation history, tool calls paused for approval, and each thread's activated read tools."""

from __future__ import annotations

import json
from collections.abc import Sequence

from paid_media_agent.harness.messages import (
    AssistantMessage,
    Conversation,
    Message,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from paid_media_agent.store.db import Store, utc_now


def _calls_json(calls: Sequence[ToolCall]) -> str | None:
    if not calls:
        return None
    return json.dumps(
        [
            {"id": c.id, "name": c.name, "args": c.args, "invalid_arguments": c.invalid_arguments}
            for c in calls
        ]
    )


def _calls(raw: str | None) -> tuple[ToolCall, ...]:
    return tuple(ToolCall(**item) for item in json.loads(raw)) if raw else ()


class ConversationStore:
    def __init__(self, store: Store) -> None:
        self._store = store

    def append(self, thread_id: str, message: Message) -> None:
        tool_calls = provider_state = tool_call_id = tool_name = status = None
        if isinstance(message, AssistantMessage):
            tool_calls = _calls_json(message.tool_calls)
            provider_state = json.dumps(message.provider_state) if message.provider_state else None
        elif isinstance(message, ToolMessage):
            tool_call_id, tool_name, status = message.tool_call_id, message.name, message.status
        # The next sequence number is computed inside the insert, under the store's write lock.
        self._store.write(
            "INSERT INTO messages SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ?, ?, ?, ?, ? "
            "FROM messages WHERE thread_id = ?",
            [
                thread_id,
                message.role,
                message.content,
                tool_calls,
                tool_call_id,
                tool_name,
                status,
                provider_state,
                utc_now(),
                thread_id,
            ],
        )

    def messages(self, thread_id: str) -> tuple[Message, ...]:
        rows = self._store.fetch(
            "SELECT role, content, tool_calls, tool_call_id, tool_name, status, provider_state "
            "FROM messages WHERE thread_id = ? ORDER BY seq",
            [thread_id],
        )
        loaded: list[Message] = []
        for role, content, calls, call_id, name, status, state in rows:
            if role == "user":
                loaded.append(UserMessage(content))
            elif role == "assistant":
                loaded.append(
                    AssistantMessage(content, _calls(calls), json.loads(state) if state else None)
                )
            else:
                loaded.append(ToolMessage(call_id, name, content, status))
        return tuple(loaded)

    def pending(self, thread_id: str) -> tuple[ToolCall, ...]:
        rows = self._store.fetch(
            "SELECT tool_call_id, tool_name, args FROM pending_tool_calls "
            "WHERE thread_id = ? ORDER BY created_at, tool_call_id",
            [thread_id],
        )
        return tuple(ToolCall(call_id, name, json.loads(args)) for call_id, name, args in rows)

    def set_pending(self, thread_id: str, calls: Sequence[ToolCall]) -> None:
        now = utc_now()
        for call in calls:
            self._store.write(
                "INSERT INTO pending_tool_calls VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                [thread_id, call.id, call.name, json.dumps(call.args), now],
            )

    def clear_pending(self, thread_id: str, call_id: str) -> bool:
        """Remove one paused call. False if another resume already took it."""
        rows = self._store.write(
            "DELETE FROM pending_tool_calls WHERE thread_id = ? AND tool_call_id = ? "
            "RETURNING tool_call_id",
            [thread_id, call_id],
        )
        return bool(rows)

    def activated(self, thread_id: str) -> tuple[str, ...]:
        rows = self._store.fetch(
            "SELECT activated FROM thread_tools WHERE thread_id = ?", [thread_id]
        )
        return tuple(json.loads(rows[0][0])) if rows else ()

    def set_activated(self, thread_id: str, names: Sequence[str]) -> None:
        self._store.write(
            "INSERT INTO thread_tools VALUES (?, ?, ?) ON CONFLICT (thread_id) DO UPDATE SET "
            "activated = excluded.activated, updated_at = excluded.updated_at",
            [thread_id, json.dumps(list(names)), utc_now()],
        )

    def load(self, thread_id: str) -> Conversation:
        return Conversation(
            messages=self.messages(thread_id),
            pending=self.pending(thread_id),
            activated_tools=self.activated(thread_id),
        )
