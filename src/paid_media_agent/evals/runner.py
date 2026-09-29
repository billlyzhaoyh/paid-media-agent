"""Run one question on a fresh sample runtime with synced history and preset goals.

Each question gets its own in-memory state, so questions are independent: the sample accounts'
last 28 days are synced (as `sync` would, so history, settings, and delivery signals exist), the
goals are set, and the question is asked once in a new thread. The transcript keeps every call
with its arguments and result (offloaded results read back), whether a change waits for approval,
any provider mutation, and the model calls' usage.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any

from paid_media_agent.analytics.goals import GoalStore
from paid_media_agent.analytics.sync import run_sync
from paid_media_agent.config import Settings
from paid_media_agent.evals.checks import CallRecord, Transcript
from paid_media_agent.harness.messages import AssistantMessage, Message, ToolMessage
from paid_media_agent.harness.models import ChatModel, ToolSchema
from paid_media_agent.runtime.local import LocalRuntime, build_local_runtime
from paid_media_agent.runtime.profiles import fixture_profile
from paid_media_agent.store import Store
from paid_media_agent.tools.artifacts import ArtifactError
from paid_media_agent.tools.catalog import StaticCatalogProvider
from paid_media_agent.tools.fixtures import FakeWriteProvider, FixtureState, build_fixture_catalog

THREAD = "eval-{id}"
CALLER = "eval"
GOALS = {
    "demo-google": {"target_cpa": 30, "monthly_budget": 25_000},
    "demo-meta": {"target_roas": 3},
}
MAX_SOURCE_CHARS = 200_000


def eval_dates(today: date | None = None) -> tuple[date, date]:
    """(today, anchor): the sample data ends two days before today, as live platforms report."""
    current = today or datetime.now(UTC).date()
    return current, current - timedelta(days=2)


class Throttled:
    """A model that waits so calls stay under a requests-per-minute limit (shared by a run)."""

    def __init__(self, inner: ChatModel, rpm: int) -> None:
        self.inner = inner
        self._interval = 60.0 / rpm if rpm > 0 else 0.0
        self._next = 0.0
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self.inner.name

    def __getattr__(self, attr: str) -> Any:
        return getattr(self.inner, attr)

    async def complete(
        self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolSchema]
    ) -> AssistantMessage:
        async with self._lock:
            wait = self._next - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next = time.monotonic() + self._interval
        return await self.inner.complete(system=system, messages=messages, tools=tools)


@dataclass
class QuestionRun:
    transcript: Transcript
    runtime: LocalRuntime


async def prepare(
    settings: Settings, *, project_root: Path, model: ChatModel, workdir: Path, today: date
) -> tuple[LocalRuntime, FakeWriteProvider]:
    """A sample runtime with 28 synced days and the eval goals."""
    _, anchor = eval_dates(today)
    anchored = settings.model_copy(
        update={"paid_media_data_mode": "sample", "paid_media_fixture_anchor": anchor}
    )
    state = FixtureState(anchor)
    writes = FakeWriteProvider(state)
    catalog = build_fixture_catalog()
    provider = StaticCatalogProvider(catalog)
    profile = fixture_profile(
        anchored,
        project_root=project_root,
        catalog_provider=provider,
        fixture_state=state,
        write_provider=writes,
        workspace_root=workdir,
        store=Store(),
    )
    runtime = build_local_runtime(
        anchored,
        project_root=project_root,
        model=model,
        catalog=catalog,
        catalog_provider=provider,
        profile=profile,
        # The agent's date is the run's date, even if a long run crosses midnight.
        clock=lambda: datetime.combine(today, clock_time(12), tzinfo=UTC),
    )
    await run_sync(
        accounts=runtime.profile.accounts,
        catalog=runtime.catalog,
        dispatcher=runtime.components.read_dispatcher,
        end=today - timedelta(days=1),
    )
    goals = GoalStore(runtime.profile.store)
    for alias, values in GOALS.items():
        if runtime.profile.accounts.resolve(alias) is not None:
            goals.set(
                alias, dict(values), effective_from=anchor - timedelta(days=60), source="eval"
            )
    return runtime, writes


def _artifact_sources(runtime: LocalRuntime, calls: list[CallRecord]) -> list[str]:
    """Payloads of artifacts the results named: offloaded results and referenced reads."""
    ids: list[str] = []
    for call in calls:
        try:
            body = json.loads(call.result)
        except ValueError:
            continue
        if isinstance(body, dict):
            for key in ("artifact_id",):
                if isinstance(body.get(key), str):
                    ids.append(body[key])
    sources, size = [], 0
    for artifact_id in dict.fromkeys(ids):
        try:
            text = json.dumps(runtime.profile.artifacts.read(artifact_id).payload, default=str)
        except (ArtifactError, OSError, ValueError):
            continue
        if size + len(text) > MAX_SOURCE_CHARS:
            break
        sources.append(text)
        size += len(text)
    return sources


def transcript_of(
    question_id: str, messages: Sequence[Message], runtime: LocalRuntime
) -> Transcript:
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    calls = [
        CallRecord(
            name=call.name,
            args=call.args,
            result=results[call.id].content if call.id in results else "",
            status=results[call.id].status if call.id in results else "pending",
        )
        for m in messages
        if isinstance(m, AssistantMessage)
        for call in m.tool_calls
    ]
    answers = [m.content for m in messages if isinstance(m, AssistantMessage) and m.content]
    transcript = Transcript(
        question_id=question_id, answer=answers[-1] if answers else "", calls=calls
    )
    transcript.extra_sources = _artifact_sources(runtime, calls)
    return transcript


def _usage(store: Store, thread_id: str) -> dict[str, Any]:
    row = store.fetch_dicts(
        "SELECT count(*) AS model_calls, count(*) FILTER (WHERE status <> 'ok') AS failed_calls, "
        "sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens, "
        "sum(cached_tokens) AS cached_tokens, sum(cost_usd) AS cost_usd "
        "FROM llm_calls WHERE thread_id = ?",
        [thread_id],
    )[0]
    return {k: (float(v) if k == "cost_usd" and v is not None else v) for k, v in row.items()}


async def run_question(
    settings: Settings,
    question: dict[str, Any],
    *,
    project_root: Path,
    model: ChatModel,
    workdir: Path,
    today: date,
) -> QuestionRun:
    runtime, writes = await prepare(
        settings, project_root=project_root, model=model, workdir=workdir, today=today
    )
    thread = THREAD.format(id=question["id"])
    started = time.monotonic()
    try:
        conversation = await runtime.agent.send(thread, CALLER, question["text"])
    except Exception as exc:
        transcript = Transcript(question_id=question["id"], answer="")
        transcript.error = f"{type(exc).__name__}: {str(exc)[:300]}"
    else:
        transcript = transcript_of(question["id"], conversation.messages, runtime)
        transcript.paused = conversation.awaiting_approval
    transcript.seconds = round(time.monotonic() - started, 1)
    transcript.mutations = len(writes.mutation_calls)
    transcript.usage = _usage(runtime.profile.store, thread)
    return QuestionRun(transcript=transcript, runtime=runtime)
