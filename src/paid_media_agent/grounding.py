"""Whether an answer's figures came from its tools: used by evals and checked on every answer.

Grounding extracts the numbers an answer states (amounts, decimals, percentages) and looks for
each in the tool results the agent saw, at the answer's own precision, allowing a percentage to
match a fraction. Dates, campaign ids, years, and small whole counts are not checked. An answer
that does arithmetic in prose, or invents a figure, has numbers no tool returned: it fails when
too few figures are grounded, or when any money figure ($ or two decimals) is not.

Nothing here knows about questions or expected values, so the runtime and the evals apply the
same rule to the same answer.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from paid_media_agent.harness.messages import AssistantMessage, Message, ToolMessage, UserMessage
from paid_media_agent.tools.artifacts import ArtifactError, ArtifactStore
from paid_media_agent.tools.calculate import CALCULATE_TOOL

CURRENCIES = (
    "USD|EUR|GBP|JPY|CAD|AUD|NZD|CHF|SEK|NOK|DKK|PLN|CZK|HUF|INR|BRL|MXN|COP|CLP|ARS|CRC|"
    "SGD|HKD|TWD|KRW|CNY|IDR|THB|MYR|PHP|ZAR|TRY|AED|SAR|ILS"
)
_NUMBER = re.compile(
    # Not inside a word, a decimal, a path, or a time; a hyphen counts only after a digit (a
    # range such as 2.4-3.9), never after a letter (an id such as g-101).
    r"(?<![\w.:/])(?<![^\d\s$€£¥]-)"
    r"[-−+]?([$€£¥])?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?([kKmMbB])(?![A-Za-z]))?(%)?"
    rf"(?:\s?(?:({CURRENCIES})|[A-Z]{{3}})\b)?"
    # Not followed by more digits or a word, except a multiple (6.17x), a rate ($190/day), or
    # the second half of a range.
    r"(?!\.\d|:|-(?![\d$])|/(?![A-Za-z])|(?!x\b)\w)"
)
_SCALES = {"k": 1e3, "m": 1e6, "b": 1e9}
_APPROX = re.compile(
    r"(?:\b(?:about|around|roughly|approximately|approx\.?|nearly|almost|some)|~|\u2248)\s*"
    r"(?:[$€£¥]?\d[\d,.]*\s*(?:-|\u2013|to)\s*)?[-\u2212+]?[$€£¥]?$",
    re.I,
)
"""'about 660' or 'roughly 830-880': a round number said to be approximate."""
_DURATION = re.compile(
    r"\s*(?:-|–|\bto\s+)?\s*(?:\d+\s*)?(?:hours?|days?|weeks?|months?|minutes?)\b", re.I
)
"""A whole number followed by a duration word ("90 days", "24-48 hours") is not a figure."""
_ANY_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE]-?\d+)?")
_PERCENT_IN_TEXT = re.compile(r"(-?\d+(?:\.\d+)?)\s?%")
_THOUSANDS = re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{3})+(?![\d,])")
_NOT_FIGURES = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+Z?)?"  # dates and timestamps
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"  # UUIDs
    r"|\b(?:[A-Za-z]{1,4}-\d+|art_[0-9a-f]+|act_\d+)\b"  # ids like g-101, sim-003, art_1f3a
    r"|\b(?=[0-9a-f]*[a-f])[0-9a-f]{12,}\b"  # hashes (a long run of digits is a figure)
)
"""Dates, ids, and hashes are stripped from source text: their digits are not figures."""
_PERCENT_KEYS = re.compile(r"pct|percent|share|rate|change|ratio|ctr|cvr|roas", re.I)
"""Keys whose values may already be in points, so they can match a percentage as they are."""
MAX_FRACTION = 10.0
"""A value this size or smaller can be a share, stated as up to 1,000%."""
MAX_RECORD_NUMBERS = 60


@dataclass
class CallRecord:
    name: str
    args: dict[str, Any]
    result: str
    status: str = "success"


@dataclass(frozen=True)
class Number:
    value: float
    decimals: int
    percent: bool
    raw: str
    scale: float = 1.0
    """1e3, 1e6, or 1e9 for a figure written as 12.4k, 1.2M, or 3B."""
    money: bool = False
    """Written with a currency symbol or code, or with exactly two decimals."""
    places: int | None = None
    """The precision it is checked at, when not `decimals`: 'about 660' is to the nearest ten."""


def numbers_in(text: str) -> list[Number]:
    """Numbers an answer states, skipping dates, ids, years, and small whole counts."""
    # Ids, UUIDs, hashes, and dates are blanked in place, so offsets and context still line up:
    # the "979b" inside a proposal id is not 979 billion.
    text = _NOT_FIGURES.sub(lambda m: " " * len(m.group(0)), text)
    found = []
    for match in _NUMBER.finditer(text):
        symbol, digits, suffix, percent, currency = match.groups()
        clean = digits.replace(",", "")
        try:
            value = float(clean)
        except ValueError:
            continue
        decimals = len(clean.split(".")[1]) if "." in clean else 0
        scale = _SCALES[suffix.lower()] if suffix else 1.0
        money = bool(symbol or currency) or (decimals == 2 and not percent)
        whole = decimals == 0 and not money and not percent and not suffix
        if whole and (value < 32 or 1990 <= value <= 2100):
            continue
        if whole and _DURATION.match(text, match.end()):
            continue
        raw = match.group(0).strip()
        places = None
        if decimals == 0 and _APPROX.search(text[max(0, match.start() - 40) : match.start()]):
            significant = clean.rstrip("0")
            places = -(len(clean) - len(significant)) if significant else None
        found.append(Number(value * scale, decimals, bool(percent), raw, scale, money, places))
    return found


@dataclass
class Sources:
    """What the tools returned, as numbers: every value, the values a percentage may match, and
    the differences and sums of related pairs."""

    values: list[float] = field(default_factory=list)
    percents: list[float] = field(default_factory=list)
    derived: set[float] = field(default_factory=set)


def _text_numbers(text: str, into: Sources) -> None:
    """Figures in free text, without the digits of dates, ids, and hashes."""
    stripped = _NOT_FIGURES.sub(" ", text)
    stripped = _THOUSANDS.sub(lambda m: m.group(0).replace(",", ""), stripped)
    for token in _ANY_NUMBER.findall(stripped):
        try:
            value = float(token)
        except ValueError:
            continue
        into.values.append(value)
        if abs(value) <= MAX_FRACTION:
            into.percents.append(value * 100)
    for token in _PERCENT_IN_TEXT.findall(stripped):
        into.percents.append(float(token))


def _json_numbers(value: Any, into: Sources, key: str = "", depth: int = 0) -> None:
    """Figures in a parsed result, read as numbers rather than as text."""
    if depth > 12:
        return
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int | float):
        number = float(value)
        into.values.append(number)
        if abs(number) <= MAX_FRACTION:
            into.percents.append(number * 100)
        if _PERCENT_KEYS.search(key):
            into.percents.append(number)
    elif isinstance(value, str):
        _text_numbers(value, into)
    elif isinstance(value, dict):
        for name, item in value.items():
            _json_numbers(item, into, str(name), depth + 1)
    elif isinstance(value, list):
        for item in value[:2000]:
            _json_numbers(item, into, key, depth + 1)


def sources_from(texts: Sequence[str]) -> Sources:
    found = Sources()
    for text in texts:
        try:
            parsed = json.loads(text)
        except ValueError:
            _text_numbers(text, found)
            continue
        _json_numbers(parsed, found)
    found.derived = derived_values(texts)
    return found


def _source_values(sources: Sequence[str]) -> list[float]:
    """Every figure the tools returned, without the digits of dates, ids, and hashes."""
    return sources_from(sources).values


def _record_numbers(record: dict[str, Any]) -> list[float]:
    values: list[float] = []
    for item in record.values():
        if isinstance(item, bool):
            continue
        if isinstance(item, int | float):
            values.append(float(item))
        elif isinstance(item, str) and len(item) <= 40:
            found = Sources()
            _text_numbers(item, found)
            values += found.values[:1]
    return values[:MAX_RECORD_NUMBERS]


def _keyed(item: Any) -> dict[str, float]:
    """A child's numbers by name: a dict's numeric fields, or a list of {field, value} items."""
    if isinstance(item, dict):
        return {
            str(k): float(v)
            for k, v in item.items()
            if isinstance(v, int | float) and not isinstance(v, bool)
        }
    if isinstance(item, list):
        return {
            str(i["field"]): float(i["value"])
            for i in item[:50]
            if isinstance(i, dict)
            and "field" in i
            and isinstance(i.get("value"), int | float)
            and not isinstance(i.get("value"), bool)
        }
    return {}


def _sibling_pairs(record: dict[str, Any]) -> list[tuple[float, float]]:
    """The same field under two children of one record: a proposal's `before` and `after`, or a
    forecast's `baseline` and `forecast`."""
    children = {name: keyed for name, item in record.items() if (keyed := _keyed(item))}
    names = list(children)
    pairs: list[tuple[float, float]] = []
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            pairs += [(children[a][k], children[b][k]) for k in children[a].keys() & children[b]]
    return pairs


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
    """Differences and sums of two related figures: two in the same record (a budget's now and
    next), or the same field in two sibling records (a proposal's before and after).

    Only related pairs: any two numbers anywhere would ground almost anything.
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
            pairs = [(a, b) for i, a in enumerate(numbers) for b in numbers[i + 1 :]]
            for a, b in pairs + _sibling_pairs(record):
                found.add(abs(a - b))
                found.add(abs(a + b))
    return found


def _matches(target: float, candidates: Collection[float], number: Number) -> bool:
    places = number.decimals if number.places is None else number.places
    return any(abs(_half_up(abs(v) / number.scale, places) - target) < 1e-9 for v in candidates)


def grounded(
    number: Number,
    values: Sequence[float],
    derived: set[float] | frozenset[float] = frozenset(),
    percents: Sequence[float] | None = None,
) -> bool:
    """Whether a tool returned this number at the answer's precision, or it is the difference or
    sum of two related returned figures. A percentage matches only a share times 100, a value
    under a rate or share key, or a percentage written in a result. No slack."""
    target = abs(number.value) / number.scale
    if number.percent:
        shares = (
            percents
            if percents is not None
            else [v * 100 for v in values if abs(v) <= MAX_FRACTION]
        )
        return _matches(target, shares, number)
    return _matches(target, values, number) or _matches(target, derived, number)


def verdict(number: Number, sources: Sources) -> str:
    """found, missing, or unchecked: a whole percentage that matches only some unrelated figure
    (a count, a method named local_band95) can be neither confirmed nor ruled out."""
    if grounded(number, sources.values, sources.derived, sources.percents):
        return "found"
    if (
        number.percent
        and number.decimals == 0
        and grounded(replace(number, percent=False), sources.values)
    ):
        return "unchecked"
    return "missing"


def _half_up(value: float, places: int) -> float:
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


UNIT_CONSTANTS = frozenset({100.0, 1000.0, 365.0, 52.0, 30.4})
"""Numbers an expression may use without a source: units and calendar lengths (and whole numbers
below 32, as in `numbers_in`)."""


def _literals(expression: str) -> list[Number]:
    """The numbers written in a `calculate` expression, at the precision they were written."""
    try:
        tree = ast.parse(expression.replace("\u2212", "-"), mode="eval")
    except (SyntaxError, ValueError):
        return []
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            if isinstance(node.value, bool):
                continue
            raw = ast.get_source_segment(expression, node) or str(node.value)
            decimals = len(raw.split(".")[1]) if "." in raw else 0
            found.append(Number(float(node.value), decimals, False, raw))
    return found


def admit_calculations(calls: Sequence[CallRecord], sources: Sources) -> list[str]:
    """Add each `calculate` result to `sources` when every number it used is sourced.

    Results are walked in order, so one calculation may build on an earlier one. A calculation
    on a number no tool returned grounds nothing: it is named, and its result is left out.
    """
    unsourced: list[str] = []
    for call in calls:
        if call.name != CALCULATE_TOOL or call.status != "success":
            continue
        try:
            items = json.loads(call.result).get("results") or []
        except (ValueError, AttributeError):
            continue
        for item in items:
            if not isinstance(item, dict) or item.get("error") or "value" not in item:
                continue
            missing = [
                n.raw
                for n in _literals(str(item.get("expression", "")))
                if not (
                    (n.decimals == 0 and abs(n.value) < 32)
                    or abs(n.value) in UNIT_CONSTANTS
                    or grounded(n, sources.values, sources.derived)
                )
            ]
            if missing:
                unsourced.append(f"{item.get('label', '?')}: {', '.join(missing)}")
                continue
            value = float(item["value"])
            sources.values.append(value)
            if abs(value) <= MAX_FRACTION:
                sources.percents.append(value * 100)
    return unsourced


@dataclass(frozen=True)
class Assessment:
    """How well an answer's figures are grounded, and which are not."""

    passed: bool
    stated: int
    checked: int
    missing: tuple[Number, ...] = ()
    unsourced: tuple[str, ...] = ()
    """`calculate` results built on numbers no tool returned."""

    @property
    def share(self) -> float:
        return 1 - len(self.missing) / self.checked if self.checked else 1.0

    @property
    def money_missing(self) -> tuple[Number, ...]:
        return tuple(n for n in self.missing if n.money)


DEFAULT_MINIMUM = 0.8


def assess(
    answer: str,
    calls: Sequence[CallRecord],
    *,
    extra_sources: Sequence[str] = (),
    asked: Sequence[str] = (),
    minimum: float = DEFAULT_MINIMUM,
) -> Assessment:
    """Fails when under `minimum` of the checkable figures are found in the tool results (and
    `extra_sources`, the payloads they point to), or when any money figure is not. Figures in the
    user's own words (`asked`) count as sourced."""
    stated = numbers_in(answer)
    if not stated:
        return Assessment(True, 0, 0)
    plain = [c.result for c in calls if c.name != CALCULATE_TOOL]
    sources = sources_from([*plain, *extra_sources])
    said = sources_from(list(asked))
    sources.values += [*said.values, *UNIT_CONSTANTS]  # "30.4 days a month" needs no tool
    sources.percents += said.percents
    unsourced = admit_calculations(calls, sources)
    verdicts = [(n, verdict(n, sources)) for n in stated]
    checked = [n for n, v in verdicts if v != "unchecked"]
    missing = tuple(n for n, v in verdicts if v == "missing")
    if not checked:
        return Assessment(True, len(stated), 0, unsourced=tuple(unsourced))
    share = 1 - len(missing) / len(checked)
    # Money no tool returned is invented or computed in prose; either fails on its own.
    money = any(n.money for n in missing) and minimum > 0
    return Assessment(
        share >= minimum - 1e-9 and not money,
        len(stated),
        len(checked),
        missing,
        tuple(unsourced),
    )


MAX_SOURCE_CHARS = 200_000
"""Artifact text read back as sources for one answer, at most."""


def calls_of(messages: Sequence[Message]) -> list[CallRecord]:
    """Every tool call in a thread with its result; a call still waiting has status pending."""
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    return [
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


def asked_in(messages: Sequence[Message]) -> list[str]:
    """What the user wrote: figures they gave count as sourced. Host notes do not."""
    return [m.content for m in messages if isinstance(m, UserMessage) and m.origin == "user"]


def artifact_sources(artifacts: ArtifactStore, calls: Sequence[CallRecord]) -> list[str]:
    """Payloads of artifacts the results named: offloaded results and referenced reads."""
    ids: list[str] = []
    for call in calls:
        try:
            body = json.loads(call.result)
        except ValueError:
            continue
        if isinstance(body, dict):
            named = [body.get("artifact_id"), *(body.get("artifact_ids") or [])]
            ids += [i for i in named if isinstance(i, str)]
    sources, size = [], 0
    for artifact_id in dict.fromkeys(ids):
        try:
            text = json.dumps(artifacts.read(artifact_id).payload, default=str)
        except (ArtifactError, OSError, ValueError):
            continue
        if size + len(text) > MAX_SOURCE_CHARS:
            break
        sources.append(text)
        size += len(text)
    return sources


def answer_check(
    artifacts: ArtifactStore, context: Callable[[], str] | None = None
) -> Callable[[Sequence[Message], str], tuple[str, ...]]:
    """The runtime's check: the figures in `answer` no tool in the thread returned, when there
    are enough of them to fail the eval rule; empty when the answer passes. `context` is what the
    system prompt told the agent (the accounts and their goals): its figures are sourced too."""

    def check(messages: Sequence[Message], answer: str) -> tuple[str, ...]:
        calls = calls_of(messages)
        given = [context()] if context is not None else []
        result = assess(
            answer,
            calls,
            extra_sources=artifact_sources(artifacts, calls),
            asked=[*asked_in(messages), *given],
        )
        if result.passed:
            return ()
        return tuple(dict.fromkeys(n.raw for n in result.missing))

    return check
