"""The demo page's story: eight weeks of one simulated store as data a page can replay.

Everything the page draws comes from here, and everything here comes from the runs, the weekly
checks, the simulation's truth and the agent's text. The page itself only draws: a clock moves
through the days and each panel shows what was known by then.

Time is counted in days elapsed: at `t` the first `t` days are over. The agent works at the start
of a week, so at `t = 7k` it has checked the week just gone and set the budgets for the next.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from importlib import resources
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from paid_media_agent.analytics.anomalies import AnomalyReport
from paid_media_agent.bandit.evaluate import CampaignDay
from paid_media_agent.domain.reports import BudgetTrial, ReportInsights, ResultSource
from paid_media_agent.reports.insights import Truth, _moves, _relevant
from paid_media_agent.testing.demo_narrative import Narrative

RUN_LABELS = {"static": "Budgets left alone", "best": "Best possible"}
STEP = 100.0
"""The extra daily spend the 'what more buys' figures are for."""
EVIDENCE: dict[str, Any] = {
    "budget": {
        "title": "Error in the spend response TabPFN implies",
        "unit": "mean absolute elasticity error, lower is better",
        "bars": [
            {"label": "Raw spend, median prediction", "value": 0.32},
            {"label": "Spend in cost-per-conversion units, mean prediction", "value": 0.18},
        ],
        "note": "Project backtest on 30 campaign cut-offs, not this run.",
        "source": "docs/architecture/budget-bandit.md",
    },
    "anomaly": {
        "title": "False alarms caused by a budget change",
        "unit": "per simulated account of about 450 campaign-days",
        "bars": [
            {"label": "±50% day-over-day rule", "low": 17, "value": 21},
            {"label": "Range with the budget as a feature", "low": 2, "value": 4},
        ],
        "note": "Project backtest on three simulated accounts, not this run.",
        "source": "docs/architecture/history-and-simulation.md",
    },
}
"""Measured in the project's backtests and recorded in the docs named; shown as such."""


@dataclass
class Watch:
    """The weekly checks over the agent's eight weeks: by a model, and by the local model and
    the day-over-day rule on the same days for comparison."""

    label: str
    source: ResultSource
    model: list[AnomalyReport]
    local: list[AnomalyReport]
    rule: list[AnomalyReport]


def _day(value: date) -> str:
    return f"{value:%a %b} {value.day}"


def _num(value: float, metric: str) -> str:
    return f"{value:,.0f}" if metric == "spend" else f"{value:,.1f}".removesuffix(".0")


def _against(observed: float, lo: float, hi: float, metric: str) -> str:
    """'323 against 261 to 315': with a decimal when whole numbers would hide which side it is."""
    close = metric == "spend" and round(observed) in (round(lo), round(hi))

    def text(value: float) -> str:
        return f"{value:,.1f}" if close else _num(value, metric)

    return f"{text(observed)} against {text(lo)} to {text(hi)}"


def _by_run(rows: Sequence[CampaignDay], days: Mapping[date, int], refs: Sequence[str]) -> Any:
    """One run as per-day lists: the account's expected conversions and each campaign's budget."""
    daily = [0.0] * len(days)
    budgets = {ref: [0.0] * len(days) for ref in refs}
    for row in rows:
        i = days[row.day]
        daily[i] += row.expected_conversions
        budgets[row.entity_ref][i] = round(row.budget, 2)
    return [round(v, 3) for v in daily], budgets


def _flag_days(reports: Sequence[AnomalyReport], days: Mapping[date, int]) -> set[tuple[str, date]]:
    return {(f.entity_ref, f.day) for report in reports for f in report.flags if f.day in days}


def _score(
    label: str, flagged: set[tuple[str, date]], planted: Mapping[Any, str]
) -> dict[str, Any]:
    return {
        "label": label,
        "alerts": len(flagged),
        "real": len(flagged & planted.keys()),
        "false": len(flagged - planted.keys()),
    }


def build_story(
    *,
    runs: Mapping[str, Sequence[CampaignDay]],
    trial: BudgetTrial | None,
    agent_label: str,
    weekly_source: str,
    check_days: Sequence[date],
    watch: Watch,
    truth: Truth,
    insights: ReportInsights,
    narrative: Narrative,
    report_href: str,
    account: str,
    currency: str,
) -> dict[str, Any]:
    """Everything the demo page shows, as one JSON-ready document."""
    agent = runs["agent"]
    order = sorted({row.day for row in agent})
    days = {day: i for i, day in enumerate(order)}
    refs = sorted({row.entity_ref for row in agent})
    names = {ref: truth.curves[ref].name for ref in refs}
    labels = {**RUN_LABELS, "agent": agent_label}

    series, budgets = {}, {}
    for key in ("static", "agent", "best"):
        series[key], budgets[key] = _by_run(runs[key], days, refs)

    # What the next STEP a day truly buys in each campaign, at the budget in force that day.
    returns = {
        ref: [round(STEP * truth.curves[ref].marginal(b), 3) for b in budgets["agent"][ref]]
        for ref in refs
    }
    before = {
        ref: round(STEP * truth.curves[ref].marginal(budgets["static"][ref][0]), 3) for ref in refs
    }

    log: list[dict[str, Any]] = []
    decisions = []
    for i in range(len(order)):
        previous = (
            {ref: budgets["static"][ref][0] for ref in refs}
            if i == 0
            else {ref: budgets["agent"][ref][i - 1] for ref in refs}
        )
        moves = [
            {"ref": ref, "from": previous[ref], "to": budgets["agent"][ref][i]}
            for ref in refs
            if abs(budgets["agent"][ref][i] - previous[ref]) >= 1
        ]
        if not moves:
            continue
        decisions.append({"t": i, "day": order[i].isoformat(), "moves": moves})
        ranked = sorted(moves, key=lambda m: -abs(m["to"] - m["from"]))
        text = ", ".join(
            f"{names[m['ref']]} {'↑' if m['to'] > m['from'] else '↓'} {abs(m['to'] - m['from']):,.0f}"
            for m in ranked
        )
        log.append(
            {"t": i, "kind": "move", "day": _day(order[i]), "text": f"Budgets moved: {text}"}
        )

    # Planted anomalies a weekly check could have caught, inside the replayed days.
    planted: dict[tuple[str, date], str] = {}
    for report in watch.model:
        planted.update({k: v for k, v in _relevant(report, truth).items() if k[1] in days})
    flagged = {
        "model": _flag_days(watch.model, days),
        "local": _flag_days(watch.local, days),
        "rule": _flag_days(watch.rule, days),
    }
    points, alarms = [], []
    for k, report in enumerate(watch.model):
        reveal = days.get(check_days[k], len(order))
        rule_k = {(f.entity_ref, f.day) for f in watch.rule[k].flags if f.day in days}
        found = 0
        for point in sorted(report.bands, key=lambda p: (p.day, p.entity_ref, p.metric)):
            if point.day not in days:
                continue
            kind = planted.get((point.entity_ref, point.day))
            note = None
            if point.flagged:
                note = f"{kind.replace('_', ' ')}, caught" if kind else "false alarm"
                found += 1
                log.append(
                    {
                        "t": reveal,
                        "kind": "alert" if kind else "false",
                        "day": _day(check_days[k]),
                        "text": f"{point.entity_name} {point.metric} on {_day(point.day)}: "
                        f"{_against(point.observed, max(point.lo, 0.0), point.hi, point.metric)} "
                        f"expected. {note.capitalize()}.",
                    }
                )
            elif (
                kind
                and _moves(kind, point.metric)  # type: ignore[arg-type]
                and (point.entity_ref, point.day) not in flagged["model"]
            ):
                note = f"{kind.replace('_', ' ')}, missed"
            points.append(
                {
                    "ref": point.entity_ref,
                    "metric": point.metric,
                    "i": days[point.day],
                    "reveal": reveal,
                    "observed": round(point.observed, 2),
                    "expected": round(point.expected, 2),
                    "lo": round(max(point.lo, 0.0), 2),  # neither metric can be negative
                    "hi": round(point.hi, 2),
                    "flagged": point.flagged,
                    "note": note,
                }
            )
        log.append(
            {
                "t": reveal,
                "kind": "check",
                "day": _day(check_days[k]),
                "text": f"Checked the week: {found} alert{'' if found == 1 else 's'}. "
                f"The ±50% rule would have raised {len(rule_k)}.",
            }
        )
        for method, reports in (("rule", watch.rule), ("local", watch.local)):
            for flag in reports[k].flags:
                if flag.day in days:
                    alarms.append(
                        {
                            "method": method,
                            "ref": flag.entity_ref,
                            "metric": flag.metric,
                            "i": days[flag.day],
                            "reveal": reveal,
                            "real": (flag.entity_ref, flag.day) in planted,
                            "lo": None if flag.lo is None else round(flag.lo, 2),
                            "hi": None if flag.hi is None else round(flag.hi, 2),
                        }
                    )
    # Within a day the agent checks the week, then moves the budgets; the page lists newest first.
    order_of = {"false": 0, "alert": 1, "check": 2, "move": 3}
    log.sort(key=lambda entry: (entry["t"], order_of[entry["kind"]]))

    model_name = watch.label.split(",")[0]
    scores = [_score(watch.label, flagged["model"], planted)]
    if flagged["local"] != flagged["model"] or watch.local is not watch.model:
        scores.append(_score("Local model, 95% expected range", flagged["local"], planted))
    scores.append(_score("±50% day-over-day rule", flagged["rule"], planted))
    if watch.local is watch.model:
        scores = [scores[0], scores[-1]]

    # The series that best shows a fixed rule tripping on noise: most rule-only alerts.
    quiet: dict[tuple[str, str], int] = {}
    for alarm in alarms:
        if alarm["method"] == "rule" and not alarm["real"]:
            which = (str(alarm["ref"]), str(alarm["metric"]))
            quiet[which] = quiet.get(which, 0) + 1
    noisy = max(quiet, key=lambda k: quiet[k]) if quiet else (refs[0], "conversions")

    # Two true curves for the budget intuition: the campaign raised most and the one cut most.
    change = {ref: budgets["agent"][ref][-1] - budgets["static"][ref][0] for ref in refs}
    pair = [max(refs, key=lambda r: change[r]), min(refs, key=lambda r: change[r])]
    top = max(max(budgets["agent"][ref]) for ref in pair) * 1.15
    curves = [
        {
            "ref": ref,
            "points": [
                [round(top * j / 60, 2), round(truth.curves[ref].value(top * j / 60), 3)]
                for j in range(61)
            ],
            "start": budgets["static"][ref][0],
            "now": budgets["agent"][ref][-1],
            "start_value": round(truth.curves[ref].value(budgets["static"][ref][0]), 3),
            "now_value": round(truth.curves[ref].value(budgets["agent"][ref][-1]), 3),
            "start_return": before[ref],
            "now_return": returns[ref][-1],
        }
        for ref in pair
    ]

    totals = {key: round(sum(values), 1) for key, values in series.items()}
    how_ranges = insights.anomaly.how if insights.anomaly else None
    how_budgets = insights.budgets.how if insights.budgets else None
    change_panel = insights.change
    return {
        "meta": {
            "account": account,
            "currency": currency,
            "weeks": len(order) // 7,
            "step": STEP,
            "model": model_name,
            "labels": labels,
            "sources": {
                "budgets": weekly_source,
                "watch": watch.source,
                "watch_label": watch.label,
                "text": narrative.source,
            },
            "report": report_href,
        },
        "days": [day.isoformat() for day in order],
        "campaigns": [{"ref": ref, "name": names[ref]} for ref in refs],
        "series": series,
        "budgets": budgets,
        "returns": {"before": before, "agent": returns},
        "decisions": decisions,
        "points": points,
        "alarms": alarms,
        "planted": [
            {"ref": ref, "i": days[day], "kind": kind.replace("_", " ")}
            for (ref, day), kind in sorted(planted.items(), key=lambda kv: kv[0][1])
        ],
        "scores": scores,
        "log": log,
        "intuition": {"noisy": {"ref": noisy[0], "metric": noisy[1]}, "curves": curves},
        "features": {
            "ranges": None if how_ranges is None else how_ranges.model_dump(mode="json"),
            "budgets": None if how_budgets is None else how_budgets.model_dump(mode="json"),
            "evidence": EVIDENCE,
        },
        "result": {
            "totals": totals,
            "per_day": {key: round(value / len(order), 2) for key, value in totals.items()},
            "gain": None if trial is None else trial.gain,
            "best_gain": None if trial is None else trial.best_gain,
            "captured": None if trial is None else trial.captured,
        },
        "change": None if change_panel is None else change_panel.model_dump(mode="json"),
        "next_steps": [
            {
                "target": r.target,
                "action": r.action.split(". ")[0].rstrip(".") + ".",
                "confidence": r.confidence,
            }
            for r in narrative.recommendations
        ],
        "summary": narrative.summary,
    }


def _asset(name: str) -> str:
    return (
        resources.files("paid_media_agent.testing").joinpath("templates", name).read_text("utf-8")
    )


def render_story(story: Mapping[str, Any]) -> str:
    """The demo page as one self-contained HTML file: styles, script and data are inline."""
    env = Environment(
        loader=FileSystemLoader(
            str(resources.files("paid_media_agent.testing").joinpath("templates"))
        ),
        autoescape=select_autoescape(default=True, default_for_string=True),
    )
    payload = json.dumps(story, separators=(",", ":")).replace("</", "<\\/")
    return env.get_template("demo.html.j2").render(
        story=story,
        css=Markup(_asset("demo.css")),  # noqa: S704 - the package's own stylesheet
        script=Markup(_asset("demo.js")),  # noqa: S704 - the package's own script
        payload=Markup(payload),  # noqa: S704 - JSON with "</" escaped
    )
