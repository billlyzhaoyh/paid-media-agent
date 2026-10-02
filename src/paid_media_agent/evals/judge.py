"""A second model grades each answer against its question, expectation, and the tool results.

It scores four criteria from 1 to 5 with a reason each and returns JSON only. A question passes
the judge when `correct` is at least 4 and nothing is below 3. The judge sees the tool calls and
results the agent saw (truncated), so it can tell a quoted figure from an invented one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from paid_media_agent.evals.checks import Transcript
from paid_media_agent.harness.messages import UserMessage
from paid_media_agent.harness.models import ChatModel

CRITERIA = ("correct", "grounded", "complete", "clear")
MAX_RESULT_CHARS = 40_000
PER_RESULT_CHARS = 8_000

RUBRIC = """You grade answers from a paid-media analyst agent. You get the user's question, what a
good answer must do (the expectation), the account context its instructions gave it (each
account's platform, currency, timezone, and goals such as a target CPA), every tool call the agent
made with its result, and the agent's final answer. Judge only the final answer, using the tool
results and the account context as ground truth.

Score each criterion from 1 (bad) to 5 (excellent):
- correct: the answer's claims agree with the tool results and meet the expectation.
- grounded: every number and fact comes from a tool result, the account context, or the
  question; nothing invented, and
  no arithmetic done in prose that the tools did not return. A `calculate` result is a tool
  result when the numbers in its expression came from other tool results; a calculation on
  numbers no tool returned grounds nothing.
- complete: it answers what was asked, with the caveats the data needs (missing days, conversions
  still arriving, attribution differences, noise), and says clearly when data is unavailable.
- clear: it leads with the answer, is concise and well organised, and would help a marketer act.

When the agent could not get data because a tool is not available, a clear honest statement of
that is correct. When a change was requested, proposing it and leaving it for approval is correct;
applying it without approval is wrong.

Reply with JSON only, no prose around it:
{"correct": {"score": 1-5, "reason": "..."}, "grounded": {...}, "complete": {...},
 "clear": {...}, "summary": "one sentence"}"""


@dataclass
class Verdict:
    scores: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    summary: str = ""
    passed: bool = False
    error: str | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "scores": self.scores,
            "reasons": self.reasons,
            "summary": self.summary,
            "passed": self.passed,
            "error": self.error,
            "cost_usd": self.cost_usd,
            "input_tokens": self.input_tokens,
        }


def _payload(question: dict[str, Any], transcript: Transcript) -> str:
    calls, used = [], 0
    for call in transcript.calls:
        result = call.result[:PER_RESULT_CHARS]
        if used + len(result) > MAX_RESULT_CHARS:
            result = result[: max(0, MAX_RESULT_CHARS - used)] + " ...[truncated]"
        used += len(result)
        calls.append(
            {"tool": call.name, "args": call.args, "status": call.status, "result": result}
        )
    return json.dumps(
        {
            "question": question["text"],
            "expectation": question["expect"],
            "account_context": transcript.context,
            "tool_calls": calls,
            "waiting_for_approval": transcript.paused,
            "final_answer": transcript.answer,
        },
        default=str,
    )


def parse_verdict(text: str) -> Verdict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        raise ValueError("no JSON object in the judge's reply")
    body = json.loads(match.group(0))
    verdict = Verdict(summary=str(body.get("summary", "")))
    for name in CRITERIA:
        item = body.get(name)
        if not isinstance(item, dict) or not isinstance(item.get("score"), int | float):
            raise ValueError(f"the judge gave no score for {name}")
        verdict.scores[name] = int(item["score"])
        verdict.reasons[name] = str(item.get("reason", ""))
    verdict.passed = verdict.scores["correct"] >= 4 and min(verdict.scores.values()) >= 3
    return verdict


async def judge(model: ChatModel, question: dict[str, Any], transcript: Transcript) -> Verdict:
    """Grade one answer; one retry when the reply is not the JSON asked for."""
    message = UserMessage(_payload(question, transcript))
    cost: float | None = None
    tokens: int | None = None
    last = "no reply"
    for _ in range(2):
        try:
            reply = await model.complete(system=RUBRIC, messages=[message], tools=[])
        except Exception as exc:
            last = f"{type(exc).__name__}: {str(exc)[:200]}"
            continue
        if reply.usage is not None:
            cost = (cost or 0.0) + (reply.usage.cost_usd or 0.0)
            tokens = (tokens or 0) + (reply.usage.input_tokens or 0)
        try:
            verdict = parse_verdict(reply.content)
        except (ValueError, KeyError) as exc:
            last = str(exc)
            continue
        verdict.cost_usd, verdict.input_tokens = cost, tokens
        return verdict
    return Verdict(error=last, cost_usd=cost, input_tokens=tokens)
