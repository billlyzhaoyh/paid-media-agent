"""The agent loop: call the model, run its tools, pause for approval, resume. State lives in DuckDB.

Every assistant tool call gets exactly one tool result before the next model call. A gated call
(execute_change on a proposal this thread staged) is persisted as pending and the run stops; the
other calls in the same batch still run. A resume re-checks the gate, then runs the call on
approve or answers it with a rejection. Paused calls survive a restart because they are stored.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from paid_media_agent.harness.messages import (
    AssistantMessage,
    Conversation,
    Message,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from paid_media_agent.harness.models import ChatModel, ModelError, ToolSchema
from paid_media_agent.harness.tools import ToolContext, ToolDispatcher
from paid_media_agent.redaction import sanitize_exception
from paid_media_agent.store.conversations import ConversationStore

Decision = Literal["approve", "reject"]
BACKOFF_SECONDS = (1.0, 2.0)
INTERRUPTED = "No result was recorded: the run stopped before this tool finished."
ABANDONED = "Not executed: the conversation continued without an approval decision."


@dataclass(frozen=True)
class RunEvent:
    kind: Literal["start", "text", "tool"]
    text: str = ""
    id: str = ""
    name: str = ""
    status: Literal["in_progress", "complete", "error"] = "in_progress"


EventHandler = Callable[[RunEvent], Awaitable[None]]
ApprovalGate = Callable[[ToolCall, ToolContext], bool]


async def _emit(handler: EventHandler | None, event: RunEvent) -> None:
    if handler is not None:
        await handler(event)


def unanswered_calls(messages: Sequence[Message]) -> list[ToolCall]:
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    return [
        call
        for m in messages
        if isinstance(m, AssistantMessage)
        for call in m.tool_calls
        if call.id not in answered
    ]


class Agent:
    def __init__(
        self,
        *,
        model: ChatModel,
        system_prompt: str,
        dispatcher: ToolDispatcher,
        conversations: ConversationStore,
        gate: ApprovalGate,
        max_model_calls: int = 40,
        model_attempts: int = 3,
        model_timeout_seconds: float = 120,
        max_active_reads: int = 6,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.dispatcher = dispatcher
        self.conversations = conversations
        self._gate = gate
        self._max_model_calls = max_model_calls
        self._model_attempts = model_attempts
        self._model_timeout = model_timeout_seconds
        self._max_active_reads = max_active_reads
        self._clock = clock

    # ------------------------------------------------------------------ public

    def conversation(self, thread_id: str) -> Conversation:
        return self.conversations.load(thread_id)

    def bound_tools(self, thread_id: str) -> list[ToolSchema]:
        """Core, write, and file tools always; read tools once discover_tools activated them."""
        activated = set(self.conversations.activated(thread_id))
        return [
            spec.schema
            for spec in self.dispatcher.tools.values()
            if spec.kind != "read" or spec.name in activated
        ]

    async def send(
        self, thread_id: str, caller_ref: str, text: str, on_event: EventHandler | None = None
    ) -> Conversation:
        conversation = self.conversations.load(thread_id)
        for call in conversation.pending:
            if self.conversations.clear_pending(thread_id, call.id):
                self.conversations.append(
                    thread_id, ToolMessage(call.id, call.name, ABANDONED, status="error")
                )
        for call in unanswered_calls(self.conversations.messages(thread_id)):
            self.conversations.append(
                thread_id, ToolMessage(call.id, call.name, INTERRUPTED, status="error")
            )
        self.conversations.append(thread_id, UserMessage(text))
        await _emit(on_event, RunEvent("start"))
        return await self._run(thread_id, caller_ref, on_event)

    async def resume(
        self,
        thread_id: str,
        caller_ref: str,
        decision: Decision,
        message: str = "",
        on_event: EventHandler | None = None,
    ) -> Conversation:
        pending = self.conversations.pending(thread_id)
        if not pending:
            return self.conversations.load(thread_id)
        await _emit(on_event, RunEvent("start"))
        context = self._context(thread_id, caller_ref)
        for call in pending:
            if not self.conversations.clear_pending(thread_id, call.id):
                continue  # another resume took this call
            if decision == "approve":
                await self._run_tool(thread_id, call, context, on_event)
            else:
                detail = f"; {message}" if message else ""
                result = json.dumps(
                    {
                        "denied": True,
                        "reason": "rejected",
                        "detail": f"rejected by reviewer{detail}",
                    }
                )
                self.conversations.append(
                    thread_id, ToolMessage(call.id, call.name, result, status="error")
                )
                await _emit(on_event, RunEvent("tool", id=call.id, name=call.name, status="error"))
        return await self._run(thread_id, caller_ref, on_event)

    # ------------------------------------------------------------------ loop

    def _context(self, thread_id: str, caller_ref: str) -> ToolContext:
        def activate(names: Sequence[str]) -> None:
            current = [n for n in self.conversations.activated(thread_id) if n not in names]
            known = [
                n
                for n in names
                if self.dispatcher.tools.get(n) and self.dispatcher.tools[n].kind == "read"
            ]
            self.conversations.set_activated(
                thread_id, (current + known)[-self._max_active_reads :]
            )

        return ToolContext(thread_id=thread_id, caller_ref=caller_ref, activate=activate)

    def _system(self) -> str:
        today = self._clock().date().isoformat()
        return f"{self.system_prompt}\n\nThe current date is {today} (UTC).".strip()

    async def _complete(self, thread_id: str) -> AssistantMessage:
        messages = self.conversations.messages(thread_id)
        tools = self.bound_tools(thread_id)
        last: BaseException | None = None
        for attempt in range(self._model_attempts):
            try:
                return await asyncio.wait_for(
                    self.model.complete(system=self._system(), messages=messages, tools=tools),
                    timeout=self._model_timeout,
                )
            except (TimeoutError, ModelError) as exc:
                last = exc
                if isinstance(exc, ModelError) and not exc.transient:
                    break
            except Exception as exc:
                last = exc
                break
            if attempt + 1 < self._model_attempts:
                await asyncio.sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
        attempts = attempt + 1
        detail = sanitize_exception(last or RuntimeError("unknown"))
        return AssistantMessage(
            content=f"Model call failed after {attempts} attempts with {detail}"
        )

    async def _run_tool(
        self, thread_id: str, call: ToolCall, context: ToolContext, on_event: EventHandler | None
    ) -> None:
        await _emit(on_event, RunEvent("tool", id=call.id, name=call.name))
        result = await self.dispatcher.dispatch(call, context)
        self.conversations.append(thread_id, result)
        status: Literal["complete", "error"] = "error" if result.status == "error" else "complete"
        await _emit(on_event, RunEvent("tool", id=call.id, name=call.name, status=status))

    async def _run(
        self, thread_id: str, caller_ref: str, on_event: EventHandler | None
    ) -> Conversation:
        context = self._context(thread_id, caller_ref)
        for _ in range(self._max_model_calls):
            reply = await self._complete(thread_id)
            self.conversations.append(thread_id, reply)
            if reply.content:
                await _emit(on_event, RunEvent("text", text=reply.content))
            if not reply.tool_calls:
                break
            paused: list[ToolCall] = []
            for call in reply.tool_calls:
                spec = self.dispatcher.tools.get(call.name)
                if spec is not None and spec.gated and self._gate(call, context):
                    paused.append(call)
                    await _emit(on_event, RunEvent("tool", id=call.id, name=call.name))
                    continue
                await self._run_tool(thread_id, call, context, on_event)
            if paused:
                self.conversations.set_pending(thread_id, paused)
                break
        else:
            self.conversations.append(
                thread_id,
                AssistantMessage(
                    content=f"Stopped after {self._max_model_calls} model calls without finishing."
                ),
            )
        return self.conversations.load(thread_id)
