"""The agent loop: call the model, run its tools, pause for approval, resume. State lives in DuckDB.

Every assistant tool call gets exactly one tool result before the next model call. A gated call
(execute_change on a proposal this thread staged) is persisted as pending and the run stops; the
other calls in the same batch still run. A resume re-checks the gate, then runs the call on
approve or answers it with a rejection. Paused calls survive a restart because they are stored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import weakref
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from paid_media_agent.harness.context import ContextView, fit_to_budget
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
from paid_media_agent.harness.usage import CallLog, CallRecord
from paid_media_agent.redaction import sanitize_exception
from paid_media_agent.store.conversations import ConversationStore

Decision = Literal["approve", "reject"]


@dataclass(frozen=True)
class ResumePlan:
    """Which paused calls a resume decides, and which it denies unrun with a reason."""

    decide: Collection[str]
    deny: Mapping[str, str] = dataclass_field(default_factory=dict)


PlanResume = Callable[[Sequence[ToolCall]], ResumePlan]
"""Chooses a resume's calls from the paused ones, inside the thread's turn. Raising aborts it."""
BACKOFF_SECONDS = (1.0, 2.0)
INTERRUPTED = "No result was recorded: the run stopped before this tool finished."
logger = logging.getLogger(__name__)
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
PauseSummary = Callable[[Sequence[ToolCall], ToolContext], str]
"""What a reviewer needs to read beside a pause, written by code from the proposals."""


async def _emit(handler: EventHandler | None, event: RunEvent) -> None:
    """Progress is best effort: a failing surface callback never stops or drops a run."""
    if handler is None:
        return
    try:
        await handler(event)
    except Exception:
        logger.warning("progress callback failed", exc_info=True)


def _span(start: date, end: date) -> str:
    return f"{start.isoformat()} to {end.isoformat()}"


def calendar_lines(today: date) -> str:
    """Today and the windows people ask about, resolved in code: models miscount dates."""
    yesterday = today - timedelta(days=1)
    this_monday = today - timedelta(days=today.weekday())
    last_monday = this_monday - timedelta(days=7)
    month_start = today.replace(day=1)
    last_month_end = month_start - timedelta(days=1)
    lines = [
        f"Today is {today:%A} {today.isoformat()} (UTC; each tool counts in its account's own "
        "timezone and returns the dates it used). Resolved windows:",
        f"- yesterday: {yesterday.isoformat()} ({yesterday:%A})",
        f"- last week (Monday to Sunday): {_span(last_monday, last_monday + timedelta(days=6))}",
        f"- the week before: {_span(last_monday - timedelta(days=7), last_monday - timedelta(days=1))}",
        f"- this month to date: {_span(month_start, today)}",
        f"- last month: {_span(last_month_end.replace(day=1), last_month_end)}",
        f"- 7 and 28 calendar days to yesterday: {_span(today - timedelta(days=7), yesterday)} "
        f"and {_span(today - timedelta(days=28), yesterday)}",
        "Use these for calendar windows. 'The last N days' is different: platforms report a "
        "day or two late, so it ends on the date each platform's data runs through (every read "
        "says it), never on yesterday or today. Any days of a window without data are named, "
        "never compared as if complete.",
    ]
    return "\n".join(lines)


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
        call_log: CallLog | None = None,
        context_budget_tokens: int = 0,
        pause_summary: PauseSummary | None = None,
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
        self._call_log = call_log
        self._context_budget = context_budget_tokens
        self._pause_summary = pause_summary
        self._turns: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        """One turn at a time per thread: two at once would give a call two results. A lock
        lives while a turn holds or awaits it, so idle threads leave nothing behind."""

    def _turn(self, thread_id: str) -> asyncio.Lock:
        lock = self._turns.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._turns[thread_id] = lock
        return lock

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
        async with self._turn(thread_id):
            return await self._send(thread_id, caller_ref, text, on_event)

    async def _send(
        self, thread_id: str, caller_ref: str, text: str, on_event: EventHandler | None
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
        *,
        call_ids: Collection[str] | None = None,
        plan: PlanResume | None = None,
    ) -> Conversation:
        """Decide paused calls, then continue the run. `call_ids` limits the decision to those
        calls (a reviewer approves one proposal, never whatever else is paused); the rest stay
        paused. `plan`, when given, picks them instead, inside the thread's turn, so the calls it
        sees are still paused when they are decided; it can also deny stale calls unrun."""
        async with self._turn(thread_id):
            deny: Mapping[str, str] = {}
            if plan is not None:
                chosen = plan(self.conversations.pending(thread_id))
                call_ids, deny = chosen.decide, chosen.deny
            return await self._resume(
                thread_id, caller_ref, decision, message, on_event, call_ids, deny
            )

    async def _resume(
        self,
        thread_id: str,
        caller_ref: str,
        decision: Decision,
        message: str,
        on_event: EventHandler | None,
        call_ids: Collection[str] | None,
        deny: Mapping[str, str],
    ) -> Conversation:
        paused = self.conversations.pending(thread_id)
        stale = [c for c in paused if c.id in deny]
        pending = [c for c in paused if (call_ids is None or c.id in call_ids) and c.id not in deny]
        if not pending and not stale:
            return self.conversations.load(thread_id)
        await _emit(on_event, RunEvent("start"))
        context = self._context(thread_id, caller_ref)
        for call in stale:
            if self.conversations.clear_pending(thread_id, call.id):
                denial = {"denied": True, "reason": "already_decided", "detail": deny[call.id]}
                self.conversations.append(
                    thread_id, ToolMessage(call.id, call.name, json.dumps(denial), status="error")
                )
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
        if self.conversations.pending(thread_id):
            # Other calls still wait for their own decision; every call needs a result before
            # the model runs again.
            return self.conversations.load(thread_id)
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
        return f"{self.system_prompt}\n\n{calendar_lines(self._clock().date())}".strip()

    def _record(
        self,
        thread_id: str,
        caller_ref: str,
        view: ContextView,
        *,
        attempt: int,
        started: float,
        status: str,
        reply: AssistantMessage | None = None,
        error: BaseException | None = None,
    ) -> None:
        if self._call_log is None:
            return
        try:
            self._log_call(thread_id, caller_ref, view, attempt, started, status, reply, error)
        except Exception:
            logger.warning("could not record a model call", exc_info=True)

    def _log_call(
        self,
        thread_id: str,
        caller_ref: str,
        view: ContextView,
        attempt: int,
        started: float,
        status: str,
        reply: AssistantMessage | None,
        error: BaseException | None,
    ) -> None:
        assert self._call_log is not None  # noqa: S101 - checked by the caller
        self._call_log.record(
            CallRecord(
                thread_id=thread_id,
                caller_ref=caller_ref,
                purpose="agent",
                provider=getattr(self.model, "provider", None),
                model=self.model.name,
                attempt=attempt + 1,
                status=status,
                latency_ms=int((time.monotonic() - started) * 1000),
                usage=reply.usage if reply is not None else None,
                error=sanitize_exception(error) if error is not None else None,
                messages_sent=len(view.messages),
                est_prompt_tokens=view.est_tokens,
                stubbed_results=view.stubbed,
                cache_requested=bool(getattr(self.model, "cache_requested", False)),
            )
        )

    async def _complete(self, thread_id: str, caller_ref: str = "") -> AssistantMessage:
        system = self._system()
        tools = self.bound_tools(thread_id)
        view = fit_to_budget(
            system, self.conversations.messages(thread_id), tools, self._context_budget
        )
        last: BaseException | None = None
        for attempt in range(self._model_attempts):
            started = time.monotonic()
            try:
                reply = await asyncio.wait_for(
                    self.model.complete(system=system, messages=view.messages, tools=tools),
                    timeout=self._model_timeout,
                )
            except (TimeoutError, ModelError) as exc:
                last = exc
                status = "timeout" if isinstance(exc, TimeoutError) else "error"
                self._record(
                    thread_id, caller_ref, view, attempt=attempt, started=started,
                    status=status, error=exc,
                )  # fmt: skip
                if isinstance(exc, ModelError) and not exc.transient:
                    break
            except Exception as exc:
                last = exc
                self._record(
                    thread_id, caller_ref, view, attempt=attempt, started=started,
                    status="error", error=exc,
                )  # fmt: skip
                break
            else:
                self._record(
                    thread_id, caller_ref, view, attempt=attempt, started=started,
                    status="ok", reply=reply,
                )  # fmt: skip
                return reply
            if attempt + 1 < self._model_attempts:
                await asyncio.sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
        attempts = attempt + 1
        detail = sanitize_exception(last or RuntimeError("unknown"))
        return AssistantMessage(
            content=f"Model call failed after {attempts} attempts with {detail}"
        )

    def _with_pause_summary(
        self, reply: AssistantMessage, context: ToolContext
    ) -> AssistantMessage:
        """A reply that pauses for approval with no text of its own gets the code-written
        summary, so every model leaves the reviewer the before, after, risk, and reversal."""
        if reply.content.strip() or self._pause_summary is None:
            return reply
        paused = [
            call
            for call in reply.tool_calls
            if (spec := self.dispatcher.tools.get(call.name)) is not None
            and spec.gated
            and self._gate(call, context)
        ]
        if not paused:
            return reply
        try:
            summary = self._pause_summary(paused, context)
        except Exception:
            logger.warning("could not write the pause summary", exc_info=True)
            return reply
        return replace(reply, content=summary) if summary else reply

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
            reply = await self._complete(thread_id, caller_ref)
            reply = self._with_pause_summary(reply, context)
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
