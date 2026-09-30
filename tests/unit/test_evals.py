"""The eval harness offline: scripted models answer, a scripted judge grades, runs are stored."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from paid_media_agent.config import Settings
from paid_media_agent.evals import figures
from paid_media_agent.evals.checks import numbers_in
from paid_media_agent.evals.report import compare, totals
from paid_media_agent.evals.store import EvalStore
from paid_media_agent.evals.suite import load_questions, run_suite
from paid_media_agent.harness.messages import AssistantMessage, Message, ToolMessage, UserMessage
from paid_media_agent.harness.models import ModelError
from paid_media_agent.store import Store
from paid_media_agent.testing.scripted_model import tool_call_message
from tests.contract.helpers import execute_step, propose_step


def _question(messages: Sequence[Message]) -> str:
    return next(m.content for m in messages if isinstance(m, UserMessage))


def _results(messages: Sequence[Message]) -> list[dict[str, Any]]:
    return [json.loads(m.content) for m in messages if isinstance(m, ToolMessage)]


class Analyst:
    """Answers three eval questions the way a good agent would; `invent` adds a made-up figure."""

    name = "scripted:analyst"

    def __init__(self, *, invent: bool = False, fail: bool = False) -> None:
        self.invent, self.fail = invent, fail

    async def complete(self, *, system: str, messages: Sequence[Message], tools: Any) -> Any:
        if self.fail:
            raise ModelError("HTTP 400 bad request", transient=False)
        text, results = _question(messages), _results(messages)
        if "cost per conversion change" in text:
            if not results:
                return tool_call_message("explain_change", {"account_aliases": ["demo-google"]})
            reading = results[-1]["reports"][0]["reading"]
            extra = " We also saved 12,345.67 USD." if self.invent else ""
            return AssistantMessage(reading + extra)
        if "Increase the daily budget" in text:
            if not results:
                return propose_step(target_ref="g-101", changes={"daily_budget": 216})(messages)
            return execute_step(messages)
        return AssistantMessage("No CRM or warehouse is connected, so pipeline is not available.")


class Judge:
    name = "scripted:judge"

    def __init__(self, score: int = 5) -> None:
        self.score = score

    async def complete(self, *, system: str, messages: Sequence[Message], tools: Any) -> Any:
        payload = json.loads(messages[-1].content)
        assert payload["final_answer"] is not None and "tool_calls" in payload
        item = {"score": self.score, "reason": "matches the tool results"}
        body = {k: item for k in ("correct", "grounded", "complete", "clear")}
        return AssistantMessage("```json\n" + json.dumps({**body, "summary": "fine"}) + "\n```")


IDS = ["q16", "q09", "q08"]


async def _run(settings: Settings, project_root: Path, store: EvalStore, model: Any, judge: Any):
    return await run_suite(
        settings,
        project_root=project_root,
        model=model,
        model_spec=model.name,
        judge_model=judge,
        judge_spec=getattr(judge, "name", None),
        store=store,
        ids=IDS,
    )


async def test_a_good_run_passes_every_check_and_is_stored(
    settings: Settings, project_root: Path
) -> None:
    store = EvalStore(store=Store())
    run_id, results = await _run(settings, project_root, store, Analyst(), Judge())
    by_id = {r["question_id"]: r for r in results}
    assert all(r["passed"] for r in results), [r["checks"] for r in results]
    explain = by_id["q16_why_cpa"]
    assert explain["calls"][0]["name"] == "explain_change"
    assert "reports" in explain["calls"][0]["result"], "history was synced before the question"
    writes = {c["name"]: c for c in by_id["q09_budget_change"]["checks"]}
    assert writes["writes"]["detail"] == "paused for approval"
    assert by_id["q09_budget_change"]["judge"]["scores"]["correct"] == 5
    stored = store.results(run_id)
    assert [r["question_id"] for r in stored] == sorted(by_id)
    run = store.run(run_id)
    assert run["totals"]["passed"] == 3 and run["model"] == "scripted:analyst"
    judged = store.store.fetch(
        "SELECT count(*), count(input_tokens) FROM llm_calls WHERE purpose = 'eval_judge'"
    )
    assert judged == [(3, 0)], "scripted judges report no usage"

    from paid_media_agent.evals.suite import regrade

    again = regrade(store, run_id, project_root=project_root)
    regraded = {r["question_id"]: r for r in store.results(again)}
    assert all(r["passed"] for r in regraded.values()), "the same answers pass the same checks"
    assert regraded["q09_budget_change"]["judge"] == by_id["q09_budget_change"]["judge"]
    assert '"regraded_from"' in store.run(again)["settings"]


async def test_invented_figures_errors_and_regressions_are_caught(
    settings: Settings, project_root: Path
) -> None:
    store = EvalStore(store=Store())
    good_id, good = await _run(settings, project_root, store, Analyst(), Judge())
    store.set_baseline(good_id)
    _, bad = await _run(settings, project_root, store, Analyst(invent=True), Judge())
    invented = next(r for r in bad if r["question_id"] == "q16_why_cpa")
    grounded = next(c for c in invented["checks"] if c["name"] == "grounded")
    assert not grounded["passed"]
    assert "12,345.67" in grounded["detail"]
    diff = compare(bad, store.results(store.resolve("baseline")))
    assert diff["regressions"] == ["q16_why_cpa"] and diff["fixes"] == []

    _, failed = await _run(settings, project_root, store, Analyst(fail=True), None)
    assert not any(r["passed"] for r in failed)
    assert all(
        any(c["name"] == "no_error" and not c["passed"] for c in r["checks"]) for r in failed
    )
    assert totals(failed)["errors"] == 3

    _, low = await _run(settings, project_root, store, Analyst(), Judge(score=2))
    assert not any(r["passed"] for r in low), "a failing judge fails the question"


def test_question_set_and_figures_are_well_formed() -> None:
    questions = load_questions()
    assert len(questions) == 30 and len({q["id"] for q in questions}) == 30
    for q in questions:
        assert {"id", "category", "text", "expect", "tools_all", "tools_none"} <= set(q)
    assert [q["id"] for q in load_questions(["q01", "q30"])] == [
        "q01_wow_spend",
        "q30_platform_cpa",
    ]
    anchor = figures.SHIPPED_ANCHOR
    week = figures.expected("google_week_spend", anchor, anchor + timedelta(days=2))
    trailing = week[1]
    assert trailing[0] == figures.window_spend(
        "google_ads", (anchor - timedelta(days=6), anchor), anchor
    )
    from decimal import Decimal

    three = Decimal("3862.42")
    assert figures.present([(3862.42, 2)], [[three]]) == (True, [])
    assert figures.present([(3862.0, 0)], [[three]])[0], "rounded to a whole is fine"
    assert not figures.present([(13862.42, 2)], [[three]])[0], "a longer number is not a match"
    assert figures.present([(3863.0, 0)], [[Decimal("3862.50")]])[0], "rounding is half up"
    assert [
        n.raw for n in numbers_in("On 2026-08-28 g-101 spent $1,200.50 (+12.5%) in 3 days")
    ] == [
        "$1,200.50",
        "+12.5%",
    ]


def test_grounding_allows_durations_and_same_record_differences_but_not_invention() -> None:
    from paid_media_agent.evals.checks import _source_values, derived_values, grounded

    assert [n.raw for n in numbers_in("last 90 days, a 24-48 hour delay, reported 144 today")] == [
        "144"
    ], "'today' is not 'to day'"
    assert [n.value for n in numbers_in("$1.2M pipeline and 12.4k clicks")] == [1_200_000, 12_400]
    sources = [
        json.dumps({"rows": [{"day": "2026-09-14", "spend": 3862.42, "conversions": 128}]}),
        json.dumps({"campaign": "g-101", "budget_now": 180.0, "budget": 216.0}),
    ]
    values, derived = _source_values(sources), derived_values(sources)
    assert grounded(numbers_in("+$36.00")[0], values, derived), "180 -> 216 in one record"
    assert grounded(numbers_in("13.7%")[0], [0.1372]), "a share read as a percentage"
    for invented in ("$3,876.42", "148 conversions", "$12,345.67", "$101"):
        assert not grounded(numbers_in(invented)[0], values, derived), invented


def test_failed_calls_do_not_count_and_unlike_runs_are_flagged() -> None:
    from paid_media_agent.evals.checks import CallRecord, Transcript, check_tools

    question = {"tools_all": [["explain_change"]], "tools_none": ["execute_change"]}
    failed = Transcript(
        "q16",
        "answer",
        calls=[CallRecord("explain_change", {}, '{"error": true, "detail": "bad window"}')],
    )
    assert not check_tools(failed, question).passed
    paused = Transcript("q09", "", calls=[CallRecord("explain_change", {}, "", status="pending")])
    assert check_tools(paused, question).passed, "a paused call waits for approval: it counts"

    row = {"question_id": "q1", "passed": True, "checks": []}
    diff = compare(
        [row],
        [row],
        current_run={"questions_sha": "a", "judge_model": None},
        against_run={"questions_sha": "b", "judge_model": "judge"},
    )
    assert not diff["comparable"] and len(diff["warnings"]) == 2


async def test_a_run_stops_when_the_provider_refuses_the_account(
    settings: Settings, project_root: Path
) -> None:
    from paid_media_agent.evals.suite import provider_refusal

    class Broke:
        name = "scripted:broke"

        async def complete(self, *, system: str, messages: Any, tools: Any) -> Any:
            raise ModelError('model returned HTTP 402: {"error": "credits"}', transient=False)

    store = EvalStore(store=Store())
    run_id, results = await _run(settings, project_root, store, Broke(), None)
    assert results == [], "nothing after the refusal is graded"
    assert "out of credits (HTTP 402)" in store.run(run_id)["totals"]["aborted"]
    assert (
        provider_refusal("model returned HTTP 401: unauthorized")
        == "the key was refused (HTTP 401)"
    )
    assert provider_refusal("model returned HTTP 429: slow down") is None, "rate limits retry"
    assert provider_refusal("the Graph API returned HTTP 403 for act_123") is None, (
        "a platform's error quoted in an answer is not the model provider refusing"
    )

    from paid_media_agent.evals.suite import regrade

    again = store.run(regrade(store, run_id, project_root=project_root))
    assert "out of credits" in again["totals"]["aborted"], "a regrade stays incomplete"


async def test_repeats_are_stored_per_attempt_and_compared_by_majority(
    settings: Settings, project_root: Path
) -> None:
    store = EvalStore(store=Store())
    run_id, results = await run_suite(
        settings,
        project_root=project_root,
        model=Analyst(),
        model_spec="scripted:analyst",
        judge_model=None,
        judge_spec=None,
        store=store,
        ids=["q08"],
        repeat=3,
    )
    assert [r["question_id"] for r in results] == [f"q08_pipeline#{n}" for n in (1, 2, 3)]
    summary = store.run(run_id)["totals"]
    assert (summary["questions"], summary["attempts"], summary["questions_passed"]) == (1, 3, 1)

    def attempts(*passed: bool) -> list[dict[str, Any]]:
        return [
            {"question_id": f"q1#{n}", "passed": p, "checks": []}
            for n, p in enumerate(passed, start=1)
        ]

    assert compare(attempts(True, False, True), attempts(True, True, True))["regressions"] == []
    assert compare(attempts(False, False, True), attempts(True, True, False))["regressions"] == [
        "q1"
    ], "a majority flipping is a regression; one attempt is not"


async def test_an_eval_agent_never_writes_into_the_repository(
    settings: Settings, project_root: Path, tmp_path: Path
) -> None:
    from paid_media_agent.evals.runner import prepare

    runtime, _ = await prepare(
        settings, project_root=project_root, model=Analyst(), workdir=tmp_path,
        today=date.today(),
    )  # fmt: skip
    root = runtime.profile.skills_root
    assert root is not None and root.resolve() != project_root.resolve()
    assert (root / "instructions.md").exists() and (root / "skills").is_dir()
    tools = {t.name: t for t in runtime.components.tools}
    from paid_media_agent.harness.tools import ToolContext

    tools["write_file"].handler(
        {"file_path": "/workspace/report.md", "content": "x"}, ToolContext("t", "eval")
    )
    assert (root / "workspace" / "report.md").exists()
    assert not (project_root / "workspace" / "report.md").exists()


def test_grounding_reads_real_tool_shapes_and_every_way_figures_are_written() -> None:
    from paid_media_agent.evals.checks import (
        CallRecord,
        Transcript,
        check_grounded,
        numbers_in,
        sources_from,
        verdict,
    )

    def found(answer: str, *results: Any) -> list[str]:
        sources = sources_from([r if isinstance(r, str) else json.dumps(r) for r in results])
        return [verdict(n, sources) for n in numbers_in(answer)]

    proposal = {
        "proposal": {
            "target_ref": "g-101",
            "before": [{"field": "daily_budget", "value": 180.0}],
            "after": [{"field": "daily_budget", "value": 216.0}],
        }
    }
    assert found("+$36.00 a day", proposal) == ["found"], "before and after are siblings"
    whatif = {"baseline": {"spend": 900.0}, "forecast": {"spend": 990.0}}
    assert found("$90 more", whatif) == ["found"]
    assert found("$126 more", proposal) == ["missing"], "only the same field is paired"

    assert found("within the expected 95% band", {"method": "local_band95"}) != ["missing"]
    assert found("CPA was 189.84", {"cpa": 189.84207311683258}) == ["found"]
    assert found("$3.46", {"x": 3.4567890123456}) == ["found"], "long floats keep their digits"
    csv = "day,spend,clicks\n2026-09-21,162.64,183\n"
    assert found("$162.64 on 183 clicks", csv) == ["found", "found"]
    assert found("162.64183", csv) == ["missing"], "cells are never merged"
    assert found("1,234.50 spent", "total 1,234.50") == ["found"]

    rows = {"rows": [{"clicks": 23, "spend": 410.0}], "change": 0.118, "share": 12.5}
    assert found("spend rose 11.8%", rows) == ["found"], "a change fraction times 100"
    assert found("impression share 12.5%", rows) == ["found"], "a share key, in points"
    assert found("spend rose 23.0%", rows) == ["missing"], "clicks are not a percentage"
    assert found("spend rose 23%", rows) == ["unchecked"], "a whole percent is not confirmed"
    assert found("CPA rose 12.3% (reading)", {"reading": "CPA rose 12.3% week on week"}) == [
        "found"
    ]

    daily = {"days": [{"spend": 657.3}, {"spend": 834.1}, {"spend": 880.86}]}
    assert found("about 660 USD, roughly 830-880 USD", daily) == ["found", "found", "found"], (
        "a round number said to be approximate is checked at the precision it implies"
    )
    assert found("660 USD", daily) == ["missing"], "without 'about', 660 means 660"
    assert found("~$1,080/month", {"change": 36.0}) == ["missing"], "36 x 30 is prose arithmetic"
    stated = {
        n.raw: n for n in numbers_in("$190/day, ROAS 6.17x, a 2.4-3.9 range, g-101 on 2026-09-21")
    }
    assert set(stated) == {"$190", "6.17", "2.4", "3.9"}, stated
    money = {n.raw: n.money for n in numbers_in("save 7,400 USD, or €7,400, or 7,400 clicks")}
    assert money == {"7,400 USD": True, "€7,400": True, "7,400": False}

    answer = "You would save 7,400 USD."
    transcript = Transcript("q", answer, calls=[CallRecord("x", {}, json.dumps({"n": 1}))])
    check = check_grounded(transcript, {"grounded_min": 0.0}, "question")
    assert check.passed, "a zero minimum never fails"
    check = check_grounded(transcript, {}, "question")
    assert not check.passed and "money" in check.detail, "a currency code is money"


def test_a_calculation_grounds_its_result_only_from_sourced_inputs() -> None:
    from paid_media_agent.evals.checks import CallRecord, Transcript, check_grounded
    from paid_media_agent.harness.tools import ToolContext
    from paid_media_agent.tools.calculate import build_calculate_tool

    tool = build_calculate_tool()

    def calculated(*expressions: str) -> CallRecord:
        args = {
            "calculations": [
                {"label": f"c{i}", "expression": e, "format": "money"}
                for i, e in enumerate(expressions)
            ]
        }
        result = tool.handler(args, ToolContext("t", "u"))  # type: ignore[arg-type]
        return CallRecord("calculate", args, str(result))

    proposal = CallRecord(
        "propose_change",
        {},
        json.dumps({"before": [{"field": "daily_budget", "value": 180.0}],
                    "after": [{"field": "daily_budget", "value": 216.0}]}),
    )  # fmt: skip
    answer = "The budget rises $36.00 a day, about $1,080.00 a month; $1,110.00 with the rest."
    sourced = Transcript(
        "q09",
        answer,
        calls=[proposal, calculated("(216 - 180) * 30", "1080.00 + 30")],
    )
    check = check_grounded(sourced, {}, "question")
    assert check.passed, check.detail

    invented = Transcript(
        "q09", "That saves $7,400.00 a month.", calls=[proposal, calculated("7400 * 1")]
    )
    check = check_grounded(invented, {}, "question")
    assert not check.passed and "calculated from unsourced inputs" in check.detail
    assert "7400" in check.detail, "a calculation cannot make an invented number true"
    prose = Transcript("q09", "About $1,080.00 a month.", calls=[proposal])
    assert not check_grounded(prose, {}, "question").passed, "prose arithmetic still fails"
