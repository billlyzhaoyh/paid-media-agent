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
    assert provider_refusal("HTTP 401 unauthorized") == "the key was refused (HTTP 401)"
    assert provider_refusal("HTTP 429 slow down") is None, "rate limits are retried, not fatal"


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
