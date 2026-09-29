"""Run the question set against one model, grade every answer, and store the run."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Any

from paid_media_agent.config import Settings
from paid_media_agent.evals.checks import run_checks
from paid_media_agent.evals.judge import judge
from paid_media_agent.evals.report import totals
from paid_media_agent.evals.runner import eval_dates, run_question
from paid_media_agent.evals.store import EvalStore
from paid_media_agent.harness.models import ChatModel
from paid_media_agent.harness.usage import CallRecord, LlmCallRecorder

ResultHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


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
) -> tuple[uuid.UUID, list[dict[str, Any]]]:
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
        },
    )
    recorder = LlmCallRecorder(store.store)
    results: list[dict[str, Any]] = []
    for question in questions:
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
        if judge_model is not None:
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
                    error=verdict.error,
                )
            )
        passed = all(c.passed for c in checks) and (verdict is None or verdict.passed)
        usage = transcript.usage
        row = {
            "question_id": question["id"],
            "category": question["category"],
            "passed": passed,
            "checks": [c.__dict__ for c in checks],
            "judge": verdict.as_json() if verdict is not None else None,
            "answer": transcript.answer,
            "calls": [c.__dict__ for c in transcript.calls],
            "model_calls": usage.get("model_calls"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cached_tokens": usage.get("cached_tokens"),
            "cost_usd": usage.get("cost_usd"),
            "judge_cost_usd": verdict.cost_usd if verdict is not None else None,
            "seconds": transcript.seconds,
            "error": transcript.error,
        }
        store.add_result(run_id, row)
        results.append(row)
        if on_result is not None:
            outcome = on_result(row)
            if outcome is not None:
                await outcome
    store.finish_run(run_id, totals(results))
    return run_id, results
