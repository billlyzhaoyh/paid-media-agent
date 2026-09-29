"""Deterministic checks on one answer: tools, figures, grounded numbers, writes, and errors.

Grounding extracts the numbers an answer states (amounts, decimals, percentages) and looks for
each in the tool results the agent saw, at the answer's own precision, allowing a percentage to
match a fraction. Dates, campaign ids, years, and small whole counts are not checked. An answer
that does arithmetic in prose, or invents a figure, has numbers no tool returned: the check fails
when too few figures are grounded, or when any money figure ($ or two decimals) is not.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from paid_media_agent.evals import figures

FAILED_REPLIES = ("Model call failed", "Stopped after")
_NUMBER = re.compile(r"(?<![\w.\-/:])[-\u2212+]?(\$)?(\d[\d,]*(?:\.\d+)?)(%)?(?![\w\-/:]|\.\d)")
_DURATION = re.compile(
    r"\s*(?:-|\u2013|to)?\s*(?:\d+\s*)?(?:hours?|days?|weeks?|months?|minutes?)\b", re.I
)
"""A whole number followed by a duration word ("90 days", "24-48 hours") is not a figure."""
_ANY_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE]-?\d+)?")


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class CallRecord:
    name: str
    args: dict[str, Any]
    result: str
    status: str = "success"


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

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Number:
    value: float
    decimals: int
    percent: bool
    raw: str


def numbers_in(text: str) -> list[Number]:
    """Numbers an answer states, skipping dates, ids, years, and small whole counts."""
    found = []
    for match in _NUMBER.finditer(text):
        dollar, digits, percent = match.groups()
        clean = digits.replace(",", "")
        try:
            value = float(clean)
        except ValueError:
            continue
        decimals = len(clean.split(".")[1]) if "." in clean else 0
        whole = decimals == 0 and not dollar and not percent
        if whole and (value < 32 or 1990 <= value <= 2100):
            continue
        if whole and _DURATION.match(text, match.end()):
            continue
        found.append(Number(value, decimals, bool(percent), match.group(0)))
    return found


def _source_values(sources: Sequence[str]) -> list[float]:
    values: list[float] = []
    for text in sources:
        for token in _ANY_NUMBER.findall(text.replace(",", "")):
            try:
                values.append(float(token))
            except ValueError:
                continue
    return values


def grounded(number: Number, values: Sequence[float], derived: bool = True) -> bool:
    """Whether a tool returned this number at the answer's precision (a percent may be a share).

    With `derived`, the difference or sum of two returned numbers also counts (a budget moving
    180 -> 216 is "+36"): simple enough to check, and what a reader expects an answer to state.
    """
    target = abs(number.value)
    places = number.decimals
    for value in values:
        magnitude = abs(value)
        if abs(round(magnitude, places) - target) < 1e-9:
            return True
        if number.percent and abs(round(magnitude * 100, places) - target) < 1e-9:
            return True
    if not derived or number.percent:
        return False
    rounded = {round(abs(v), places) for v in values}
    step = 10 ** (-places)
    for value in {abs(v) for v in values}:
        for other in (value - target, value + target, target - value):
            candidate = round(other, places)
            if candidate > 0 and any(
                round(candidate + d * step, places) in rounded for d in (-1, 0, 1)
            ):
                return True
    return False


def check_tools(transcript: Transcript, question: dict[str, Any]) -> Check:
    called = [c.name for c in transcript.calls]

    def hit(pattern: str) -> bool:
        return any(fnmatch.fnmatch(name, pattern) for name in called)

    missing = [group for group in question.get("tools_all", []) if not any(hit(p) for p in group)]
    forbidden = [p for p in question.get("tools_none", []) if hit(p)]
    problems = [f"none of {g}" for g in missing] + [f"called {p}" for p in forbidden]
    return Check("tools", not problems, "; ".join(problems) or f"called {sorted(set(called))}")


def check_figures(
    transcript: Transcript, question: dict[str, Any], *, anchor: date, today: date
) -> Check | None:
    name = question.get("figures")
    if not name:
        return None
    ok, missing = figures.present(transcript.answer, figures.expected(name, anchor, today))
    return Check("figures", ok, "all present" if ok else f"missing {[m[0] for m in missing]}")


def check_grounded(transcript: Transcript, question: dict[str, Any], text: str) -> Check:
    minimum = float(question.get("grounded_min", 0.8))
    stated = numbers_in(transcript.answer)
    if not stated:
        return Check("grounded", True, "no figures stated")
    values = _source_values(
        [c.result for c in transcript.calls] + transcript.extra_sources + [text]
    )
    missing = [n for n in stated if not grounded(n, values)]
    share = 1 - len(missing) / len(stated)
    # Money no tool returned is invented or computed in prose; either fails on its own.
    money = [n.raw for n in missing if n.raw.lstrip("-\u2212+").startswith("$") or n.decimals == 2]
    detail = f"{share:.0%} of {len(stated)} figures found in tool results"
    if missing:
        detail += f"; not found: {[n.raw for n in missing][:8]}"
    if money and minimum > 0:
        detail += "; money figures must come from a tool"
    return Check("grounded", share >= minimum - 1e-9 and not (money and minimum > 0), detail)


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
