"""Deterministic checks on one answer: tools, figures, grounded numbers, writes, and errors.

Grounding extracts the numbers an answer states (amounts, decimals, percentages) and looks for
each in the tool results the agent saw, at the answer's own precision, allowing a percentage to
match a fraction. Dates, campaign ids, years, and small whole counts are not checked. An answer
that does arithmetic in prose, or invents a figure, has numbers no tool returned: the check fails
when too few figures are grounded, or when any money figure ($ or two decimals) is not.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from paid_media_agent.evals import figures

FAILED_REPLIES = ("Model call failed", "Stopped after")
_NUMBER = re.compile(
    r"(?<![\w.\-/:])[-\u2212+]?(\$)?(\d[\d,]*(?:\.\d+)?)(?:\s?([kKmMbB])(?![A-Za-z]))?(%)?"
    r"(?:\s?[A-Z]{3}\b)?(?![\w\-/:]|\.\d)"
)
_SCALES = {"k": 1e3, "m": 1e6, "b": 1e9}
_DURATION = re.compile(
    r"\s*(?:-|\u2013|\bto\s+)?\s*(?:\d+\s*)?(?:hours?|days?|weeks?|months?|minutes?)\b", re.I
)
"""A whole number followed by a duration word ("90 days", "24-48 hours") is not a figure."""
_ANY_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE]-?\d+)?")
_NOT_FIGURES = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+Z?)?"  # dates and timestamps
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"  # UUIDs
    r"|\b[A-Za-z]+[-_][A-Za-z0-9]+\b"  # ids like g-101, art_1f3a, sim-003
    r"|\b[0-9a-f]{12,}\b"  # hashes
)
"""Dates, ids, and hashes are stripped from sources: their digits are not figures."""
MAX_RECORD_NUMBERS = 60


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
    scale: float = 1.0
    """1e3, 1e6, or 1e9 for a figure written as 12.4k, 1.2M, or 3B."""


def numbers_in(text: str) -> list[Number]:
    """Numbers an answer states, skipping dates, ids, years, and small whole counts."""
    found = []
    for match in _NUMBER.finditer(text):
        dollar, digits, suffix, percent = match.groups()
        clean = digits.replace(",", "")
        try:
            value = float(clean)
        except ValueError:
            continue
        decimals = len(clean.split(".")[1]) if "." in clean else 0
        scale = _SCALES[suffix.lower()] if suffix else 1.0
        whole = decimals == 0 and not dollar and not percent and not suffix
        if whole and (value < 32 or 1990 <= value <= 2100):
            continue
        if whole and _DURATION.match(text, match.end()):
            continue
        found.append(Number(value * scale, decimals, bool(percent), match.group(0).strip(), scale))
    return found


def _numbers(text: str) -> list[float]:
    values = []
    for token in _ANY_NUMBER.findall(_NOT_FIGURES.sub(" ", text).replace(",", "")):
        try:
            values.append(float(token))
        except ValueError:
            continue
    return values


def _source_values(sources: Sequence[str]) -> list[float]:
    """Every figure the tools returned, without the digits of dates, ids, and hashes."""
    return [v for text in sources for v in _numbers(text)]


def _record_numbers(record: dict[str, Any]) -> list[float]:
    values: list[float] = []
    for item in record.values():
        if isinstance(item, bool):
            continue
        if isinstance(item, int | float):
            values.append(float(item))
        elif isinstance(item, str) and len(item) <= 40:
            values += _numbers(item)[:1]
    return values[:MAX_RECORD_NUMBERS]


def _walk(value: Any, out: list[dict[str, Any]], depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(value, dict):
        out.append(value)
        for item in value.values():
            _walk(item, out, depth + 1)
    elif isinstance(value, list):
        for item in value[:500]:
            _walk(item, out, depth + 1)


def derived_values(sources: Sequence[str]) -> set[float]:
    """Differences and sums of two figures from the same record (a budget's before and after).

    Only pairs within one record: any two numbers anywhere would ground almost anything.
    """
    found: set[float] = set()
    for text in sources:
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        records: list[dict[str, Any]] = []
        _walk(parsed, records)
        for record in records:
            numbers = _record_numbers(record)
            for i, a in enumerate(numbers):
                for b in numbers[i + 1 :]:
                    found.add(abs(a - b))
                    found.add(abs(a + b))
    return found


def grounded(
    number: Number, values: Sequence[float], derived: set[float] | frozenset[float] = frozenset()
) -> bool:
    """Whether a tool returned this number at the answer's precision (a percent may be a share),
    or it is the difference or sum of two numbers in one returned record. No slack."""
    target = abs(number.value) / number.scale
    places = number.decimals
    for value in values:
        magnitude = abs(value) / number.scale
        if abs(_half_up(magnitude, places) - target) < 1e-9:
            return True
        if number.percent and abs(_half_up(magnitude * 100, places) - target) < 1e-9:
            return True
    if number.percent:
        return False
    return any(abs(_half_up(v / number.scale, places) - target) < 1e-9 for v in derived)


def _half_up(value: float, places: int) -> float:
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


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
    minimum = float(question.get("grounded_min", 0.8))
    stated = numbers_in(transcript.answer)
    if not stated:
        return Check("grounded", True, "no figures stated")
    sources = [c.result for c in transcript.calls] + transcript.extra_sources
    values = _source_values([*sources, text])
    derived = derived_values(sources)
    missing = [n for n in stated if not grounded(n, values, derived)]
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
