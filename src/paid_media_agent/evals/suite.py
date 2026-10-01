"""Run the question set against one model, grade every answer, and store the run."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import date, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

from paid_media_agent.config import Settings
from paid_media_agent.evals.checks import FAILED_REPLIES, Transcript, run_checks
from paid_media_agent.evals.checks import CallRecord as ToolCallRecord
from paid_media_agent.evals.judge import judge
from paid_media_agent.evals.report import totals
from paid_media_agent.evals.runner import eval_dates, run_question
from paid_media_agent.evals.store import EvalStore
from paid_media_agent.harness.messages import Usage
from paid_media_agent.harness.models import ChatModel
from paid_media_agent.harness.usage import CallRecord, LlmCallRecorder

ResultHandler = Callable[[dict[str, Any]], Awaitable[None] | None]
SKIPPED_JUDGEMENT: dict[str, Any] = {"skipped": "a check failed", "passed": None}
"""Stored for an answer not judged because it already failed a check."""
_REFUSED = re.compile(r"model returned HTTP (401|402|403)\b")
"""The model provider refused the key (auth) or the account (credits): later questions would only
measure that, so the run stops. Anchored on the model client's own error, so an answer quoting a
platform's HTTP 403 never stops a run."""


def provider_refusal(*texts: str | None) -> str | None:
    for text in texts:
        found = _REFUSED.search(text or "")
        if found:
            reason = {"401": "the key was refused", "402": "out of credits"}.get(
                found.group(1), "access was refused"
            )
            return f"{reason} (HTTP {found.group(1)})"
    return None


def questions_text() -> str:
    return resources.files("paid_media_agent.evals").joinpath("questions.json").read_text("utf-8")


def load_questions(ids: Sequence[str] | None = None) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = json.loads(questions_text())
    if not ids:
        return questions
    wanted = {i.strip() for i in ids if i.strip()}
    chosen = [q for q in questions if q["id"] in wanted or q["id"].split("_")[0] in wanted]
    unknown = wanted - {q["id"] for q in chosen} - {q["id"].split("_")[0] for q in chosen}
    if unknown:
        raise ValueError(f"unknown question ids: {sorted(unknown)}")
    return chosen


def git_sha(root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 - git on PATH
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


async def run_suite(
    settings: Settings,
    *,
    project_root: Path,
    model: ChatModel,
    model_spec: str,
    judge_model: ChatModel | None,
    judge_spec: str | None,
    store: EvalStore,
    ids: Sequence[str] | None = None,
    today: date | None = None,
    on_result: ResultHandler | None = None,
    repeat: int = 1,
    judge_all: bool = False,
) -> tuple[uuid.UUID, list[dict[str, Any]]]:
    """Ask every question `repeat` times; with repeats, attempts are stored as `q01...#2`.

    An answer that failed a deterministic check fails whatever the judge says, so it is not
    judged unless `judge_all`: the judge was a third of an eval's cost."""
    questions = load_questions(ids)
    current, anchor = eval_dates(today)
    run_id = store.start_run(
        model=model_spec,
        judge_model=judge_spec,
        anchor=anchor,
        questions_sha=hashlib.sha256(questions_text().encode()).hexdigest()[:12],
        git_sha=git_sha(project_root),
        settings={
            "ids": [q["id"] for q in questions],
            "prompt_cache": settings.paid_media_prompt_cache,
            "context_budget_tokens": settings.paid_media_context_budget_tokens,
            "max_model_calls": settings.paid_media_max_model_calls,
            "repeat": repeat,
            "judge_all": judge_all,
            "answer_repair": settings.paid_media_answer_repair,
        },
    )
    recorder = LlmCallRecorder(store.store)
    results: list[dict[str, Any]] = []
    aborted: str | None = None
    attempts = [(q, n) for q in questions for n in range(1, max(repeat, 1) + 1)]
    for question, attempt in attempts:
        with tempfile.TemporaryDirectory(prefix="pma-eval-") as workdir:
            run = await run_question(
                settings,
                question,
                project_root=project_root,
                model=model,
                workdir=Path(workdir),
                today=current,
            )
        transcript = run.transcript
        checks = run_checks(transcript, question, anchor=anchor, today=current)
        verdict = None
        skipped = judge_model is not None and not judge_all and not all(c.passed for c in checks)
        if judge_model is not None and not skipped:
            started = time.monotonic()
            verdict = await judge(judge_model, question, transcript)
            recorder.record(
                CallRecord(
                    thread_id=f"eval:{run_id}:{question['id']}",
                    caller_ref="eval",
                    purpose="eval_judge",
                    provider=getattr(judge_model, "provider", None),
                    model=judge_model.name,
                    attempt=1,
                    status="error" if verdict.error else "ok",
                    latency_ms=int((time.monotonic() - started) * 1000),
                    usage=Usage(input_tokens=verdict.input_tokens, cost_usd=verdict.cost_usd),
                    error=verdict.error,
                )
            )
        passed = all(c.passed for c in checks) and (verdict is None or verdict.passed)
        usage = transcript.usage
        row = {
            "question_id": question["id"] if repeat <= 1 else f"{question['id']}#{attempt}",
            "category": question["category"],
            "passed": passed,
            "checks": [c.__dict__ for c in checks],
            "judge": verdict.as_json()
            if verdict is not None
            else SKIPPED_JUDGEMENT
            if skipped
            else None,
            "answer": transcript.answer,
            "calls": [c.__dict__ for c in transcript.calls],
            "model_calls": usage.get("model_calls"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cached_tokens": usage.get("cached_tokens"),
            "cache_write_tokens": usage.get("cache_write_tokens"),
            "call_usage": usage.get("call_usage"),
            "cost_usd": usage.get("cost_usd"),
            "judge_cost_usd": verdict.cost_usd if verdict is not None else None,
            "seconds": transcript.seconds,
            "error": transcript.error,
            "draft": transcript.draft if transcript.repaired else None,
        }
        refused = provider_refusal(
            transcript.error,
            transcript.answer if transcript.answer.startswith(FAILED_REPLIES) else None,
            verdict.error if verdict is not None else None,
        )
        if refused:
            # The provider refused; this question measured that, not the agent. Stop here.
            aborted = f"stopped at {question['id']}: {refused}"
            break
        store.add_result(run_id, row)
        results.append(row)
        if on_result is not None:
            outcome = on_result(row)
            if outcome is not None:
                await outcome
    summary = totals(results)
    if aborted:
        summary["aborted"] = aborted
    store.finish_run(run_id, summary)
    return run_id, results


def regrade(store: EvalStore, run_id: uuid.UUID, *, project_root: Path) -> uuid.UUID:
    """Re-run the deterministic checks on a stored run's transcripts, keeping its judge verdicts.

    No model is called: the answers, calls, and results are the stored ones. Artifact payloads
    the results pointed to are not stored, so grounding sees only the results themselves. The
    new run records where it came from.
    """
    run = store.run(run_id)
    results = store.results(run_id)
    questions = {q["id"]: q for q in load_questions()}
    anchor: date = run["anchor"]
    today = anchor + timedelta(days=2)
    previous = json.loads(run["settings"]) if isinstance(run["settings"], str) else run["settings"]
    new_id = store.start_run(
        model=run["model"],
        judge_model=run["judge_model"],
        anchor=anchor,
        questions_sha=hashlib.sha256(questions_text().encode()).hexdigest()[:12],
        git_sha=git_sha(project_root),
        settings={**(previous or {}), "regraded_from": str(run_id)},
    )
    rows = []
    for old in results:
        question = questions.get(old["question_id"].split("#", 1)[0])
        if question is None:
            continue
        writes = next((c for c in old["checks"] if c["name"] == "writes"), None)
        detail = writes["detail"] if writes else ""
        attempted = re.match(r"(\d+) provider mutation", detail)
        mutations = int(attempted.group(1)) if attempted else 0
        transcript = Transcript(
            question_id=old["question_id"],
            answer=old["answer"] or "",
            calls=[ToolCallRecord(**call) for call in old["calls"]],
            paused=detail == "paused for approval",
            mutations=mutations,
            error=old["error"],
        )
        checks = run_checks(transcript, question, anchor=anchor, today=today)
        verdict = old["judge"]
        if verdict is not None and verdict.get("skipped"):
            verdict = None  # never judged; the checks alone decide, and totals count the skip
        row = {
            **old,
            "passed": all(c.passed for c in checks) and (verdict is None or verdict["passed"]),
            "checks": [c.__dict__ for c in checks],
        }
        store.add_result(new_id, row)
        rows.append(row)
    summary = totals(rows)
    aborted = (run.get("totals") or {}).get("aborted")
    if aborted:
        summary["aborted"] = aborted  # a regraded incomplete run is still incomplete
    store.finish_run(new_id, summary)
    return new_id
