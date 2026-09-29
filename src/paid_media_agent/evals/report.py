"""Totals for a run and what changed against another: regressions, fixes, cost, and latency."""

from __future__ import annotations

import statistics
from typing import Any


def totals(results: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(results)
    passed = sum(1 for r in results if r["passed"])
    judged = [r for r in results if r.get("judge") and not r["judge"].get("error")]
    cost = [r["cost_usd"] for r in results if r.get("cost_usd") is not None]
    judge_cost = [r["judge_cost_usd"] for r in results if r.get("judge_cost_usd") is not None]
    inputs = sum(r.get("input_tokens") or 0 for r in results)
    cached = sum(r.get("cached_tokens") or 0 for r in results)
    seconds = [r["seconds"] for r in results if r.get("seconds") is not None]
    checks: dict[str, int] = {}
    for r in results:
        for c in r["checks"]:
            if not c["passed"]:
                checks[c["name"]] = checks.get(c["name"], 0) + 1
    return {
        "questions": n,
        "passed": passed,
        "pass_rate": round(passed / n, 3) if n else None,
        "errors": sum(1 for r in results if r.get("error") or _failed(r, "no_error")),
        "check_failures": checks,
        "judge_passed": sum(1 for r in judged if r["judge"]["passed"]),
        "judged": len(judged),
        "mean_scores": {
            k: round(statistics.mean(r["judge"]["scores"][k] for r in judged), 2)
            for k in ("correct", "grounded", "complete", "clear")
        }
        if judged
        else None,
        "cost_usd": round(sum(cost), 4) if cost else None,
        "judge_cost_usd": round(sum(judge_cost), 4) if judge_cost else None,
        "model_calls": sum(r.get("model_calls") or 0 for r in results),
        "input_tokens": inputs,
        "cache_hit_rate": round(cached / inputs, 3) if inputs else None,
        "p50_seconds": round(statistics.median(seconds), 1) if seconds else None,
        "max_seconds": max(seconds) if seconds else None,
    }


def _failed(result: dict[str, Any], name: str) -> bool:
    return any(c["name"] == name and not c["passed"] for c in result["checks"])


def compare(
    current: list[dict[str, Any]],
    against: list[dict[str, Any]],
    *,
    current_run: dict[str, Any] | None = None,
    against_run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Regressions, fixes, and deltas on the questions both runs asked. Runs graded differently
    (another question set or judge) are compared but marked as not like for like."""
    warnings = []
    if current_run and against_run:
        if current_run.get("questions_sha") != against_run.get("questions_sha"):
            warnings.append("the question set changed between the runs")
        if current_run.get("judge_model") != against_run.get("judge_model"):
            warnings.append(
                f"judged differently ({against_run.get('judge_model') or 'no judge'} vs "
                f"{current_run.get('judge_model') or 'no judge'})"
            )
    before = {r["question_id"]: r for r in against}
    now = {r["question_id"]: r for r in current}
    common = sorted(set(before) & set(now))
    a, b = totals([before[q] for q in common]), totals([now[q] for q in common])

    def delta(key: str) -> float | None:
        if a.get(key) is None or b.get(key) is None:
            return None
        return round(float(b[key]) - float(a[key]), 4)

    return {
        "comparable": not warnings,
        "warnings": warnings,
        "questions": len(common),
        "regressions": [q for q in common if before[q]["passed"] and not now[q]["passed"]],
        "fixes": [q for q in common if not before[q]["passed"] and now[q]["passed"]],
        "pass_rate": (a["pass_rate"], b["pass_rate"]),
        "cost_delta_usd": delta("cost_usd"),
        "p50_seconds_delta": delta("p50_seconds"),
        "cache_hit_rate": (a["cache_hit_rate"], b["cache_hit_rate"]),
    }


def why(result: dict[str, Any]) -> str:
    """One line on why a question failed: the failed checks, then the judge's lowest criterion."""
    parts = [f"{c['name']}: {c['detail']}" for c in result["checks"] if not c["passed"]]
    verdict = result.get("judge")
    if verdict and verdict.get("error"):
        parts.append(f"judge error: {verdict['error']}")
    elif verdict and not verdict.get("passed"):
        worst = min(verdict["scores"], key=lambda k: verdict["scores"][k])
        parts.append(f"judge {worst} {verdict['scores'][worst]}/5: {verdict['reasons'][worst]}")
    return "; ".join(parts)[:400]
