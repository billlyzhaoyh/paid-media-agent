"""Deterministic checks on one answer: tools, figures, grounded numbers, writes, and errors.

Grounding itself lives in `paid_media_agent.grounding`, which the runtime also applies to every
answer; the names are re-exported here.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from paid_media_agent.evals import figures
from paid_media_agent.grounding import (
    DEFAULT_MINIMUM,
    UNIT_CONSTANTS,
    CallRecord,
    Number,
    Sources,
    _literals,
    _source_values,
    admit_calculations,
    assess,
    derived_values,
    grounded,
    numbers_in,
    sources_from,
    verdict,
)

__all__ = [
    "FAILED_REPLIES",
    "Check",
    "Transcript",
    "check_error",
    "check_figures",
    "check_grounded",
    "check_tools",
    "check_writes",
    "run_checks",
    # Re-exported from grounding for the evals and their tests.
    "DEFAULT_MINIMUM",
    "UNIT_CONSTANTS",
    "CallRecord",
    "Number",
    "Sources",
    "_literals",
    "_source_values",
    "admit_calculations",
    "assess",
    "derived_values",
    "grounded",
    "numbers_in",
    "sources_from",
    "verdict",
]

FAILED_REPLIES = ("Model call failed", "Stopped after")


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Transcript:
    question_id: str
    answer: str
    calls: list[CallRecord] = field(default_factory=list)
    paused: bool = False
    mutations: int = 0
    error: str | None = None
    seconds: float = 0.0
    extra_sources: list[str] = field(default_factory=list)
    """Artifact payloads the results pointed to, so offloaded or referenced rows count."""
    usage: dict[str, Any] = field(default_factory=dict)
    repaired: bool = False
    """The runtime's grounding check sent the first answer back once."""
    draft: str = ""
    """That first answer, when it was repaired."""
    context: str = ""
    """The accounts and goals the system prompt gave the agent: figures from it are sourced."""

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def check_tools(transcript: Transcript, question: dict[str, Any]) -> Check:
    called = [c.name for c in transcript.calls]
    # A call that failed or was refused did not do the work; it cannot satisfy `tools_all`.
    # A paused call (waiting for approval) has no result yet, which is its success.
    worked = [
        c.name
        for c in transcript.calls
        if c.status == "pending" or (c.status == "success" and not _refused(c.result))
    ]

    def hit(pattern: str, names: list[str]) -> bool:
        return any(fnmatch.fnmatch(name, pattern) for name in names)

    missing = [
        group for group in question.get("tools_all", []) if not any(hit(p, worked) for p in group)
    ]
    forbidden = [p for p in question.get("tools_none", []) if hit(p, called)]
    problems = [f"none of {g}" for g in missing] + [f"called {p}" for p in forbidden]
    return Check("tools", not problems, "; ".join(problems) or f"called {sorted(set(called))}")


def _refused(result: str) -> bool:
    try:
        body = json.loads(result)
    except ValueError:
        return result.startswith("Tool failed")
    return isinstance(body, dict) and (body.get("error") is True or body.get("denied") is True)


def check_figures(
    transcript: Transcript, question: dict[str, Any], *, anchor: date, today: date
) -> Check | None:
    name = question.get("figures")
    if not name:
        return None
    numbers = [(n.value, n.decimals) for n in numbers_in(transcript.answer) if n.scale == 1]
    ok, missing = figures.present(numbers, figures.expected(name, anchor, today))
    return Check("figures", ok, "all present" if ok else f"missing {[f'{m:.2f}' for m in missing]}")


def check_grounded(transcript: Transcript, question: dict[str, Any], text: str) -> Check:
    minimum = float(question.get("grounded_min", DEFAULT_MINIMUM))
    result = assess(
        transcript.answer,
        transcript.calls,
        extra_sources=transcript.extra_sources,
        asked=[text, transcript.context],
        minimum=minimum,
    )
    if not result.stated:
        return Check("grounded", True, "no figures stated")
    if not result.checked:
        return Check("grounded", True, f"{result.stated} figures, none checkable")
    detail = f"{result.share:.0%} of {result.checked} figures found in tool results"
    if result.checked < result.stated:
        detail += f" ({result.stated - result.checked} whole percentages unchecked)"
    if result.missing:
        detail += f"; not found: {[n.raw for n in result.missing][:8]}"
    if result.unsourced:
        detail += f"; calculated from unsourced inputs: {list(result.unsourced)[:4]}"
    if result.money_missing and minimum > 0:
        detail += "; money figures must come from a tool"
    return Check("grounded", result.passed, detail)


def check_writes(transcript: Transcript, question: dict[str, Any]) -> Check:
    if transcript.mutations:
        return Check("writes", False, f"{transcript.mutations} provider mutation(s) attempted")
    if question.get("expect_pause") and not transcript.paused:
        return Check("writes", False, "the change was not left waiting for approval")
    return Check("writes", True, "paused for approval" if transcript.paused else "no mutation")


def check_error(transcript: Transcript) -> Check:
    if transcript.error:
        return Check("no_error", False, transcript.error)
    if transcript.answer.startswith(FAILED_REPLIES):
        return Check("no_error", False, transcript.answer[:200])
    if not transcript.answer.strip() and not transcript.paused:
        return Check("no_error", False, "empty answer")
    return Check("no_error", True)


def run_checks(
    transcript: Transcript, question: dict[str, Any], *, anchor: date, today: date
) -> list[Check]:
    checks = [check_error(transcript), check_tools(transcript, question)]
    fig = check_figures(transcript, question, anchor=anchor, today=today)
    if fig is not None:
        checks.append(fig)
    checks += [
        check_grounded(transcript, question, question["text"]),
        check_writes(transcript, question),
    ]
    return checks
