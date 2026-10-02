"""Report panels beyond the two-window comparison: expected ranges, budget curves, a change.

Each panel is built from stored history with the same functions the agent's tools use, so the
report shows what `check_anomalies` and `recommend_budgets` would say, drawn. A panel names the
model that produced it and whether its figures came from a live call, a stored result, or a
fallback. On a simulated account the known truth (planted anomalies, true curves) is drawn too.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

import numpy as np

from paid_media_agent.analytics.anomalies import (
    DEFAULT_BAND,
    RULE,
    AnomalyReport,
    InputRow,
    check_anomalies,
)
from paid_media_agent.bandit.arms import Arm
from paid_media_agent.bandit.evaluate import LoopResult
from paid_media_agent.bandit.policy import greedy
from paid_media_agent.bandit.posterior import PowerCurve
from paid_media_agent.bandit.recommend import ArmDecision, BanditConfig, BanditRun, recommend
from paid_media_agent.domain.presentation import ProposalView, proposal_summary
from paid_media_agent.domain.reports import (
    AnomalyHow,
    AnomalyPanel,
    BandDay,
    BandSeries,
    BudgetCurve,
    BudgetHow,
    BudgetPanel,
    BudgetRow,
    BudgetTrial,
    ChangePanel,
    HowRow,
    MarginalRow,
    MethodScore,
    RangeExample,
    RangeStep,
    ReportInsights,
    ResultSource,
    RuleContrast,
    TrialRun,
)
from paid_media_agent.predict.local import LocalPredictor
from paid_media_agent.predict.protocol import Predictor
from paid_media_agent.sim.simulator import SHOCK_EFFECTS, CampaignTruth, ShockKind
from paid_media_agent.store.db import Store, utc_now

MAX_SERIES = 4
MAX_CURVES = 6
HISTORY_DAYS = 60
CURVE_POINTS = 40
PSEUDO_POINTS = 32
MODEL_NAMES = {"tabpfn": "TabPFN", "local": "Local model"}
ABOUT_RANGES = {
    "tabpfn": "TabPFN is a model pretrained on synthetic tables. The history rows and the rows "
    "to judge go to it in one request and the ranges come back; nothing is trained or tuned for "
    "this account.",
    "local": "The local model starts from the simple estimate in each row and widens it by how "
    "far real days landed from that estimate in the history. Nothing leaves the machine.",
}
ABOUT_BUDGETS = {
    "tabpfn": "TabPFN is a model pretrained on synthetic tables. Every campaign's days and the "
    "questions go to it in one request and the predictions come back; it assumes no curve shape "
    "and nothing is trained for this account.",
    "pooled": "The pooled regression runs locally: a level per campaign, weekday effects, and "
    "one spend response shared by every campaign.",
}
MIN_BUDGET_STEP = 0.1
"""A budget change at least this large, as a share, is worth showing the range following."""
RECORDED_MARK = "(recorded)"
"""Ends the model version of an answer replayed from a recording made earlier."""


@dataclass(frozen=True)
class Truth:
    """What a simulated account really did: only a simulation knows this."""

    planted: Mapping[tuple[str, date], ShockKind] = field(default_factory=dict)
    curves: Mapping[str, CampaignTruth] = field(default_factory=dict)


def _source(store: Store, provider: str, purpose: str, since: datetime) -> ResultSource:
    """Whether this build's model figures were computed now, read from an earlier identical
    call, or come from a recording."""
    if provider == "local":
        return "local"
    rows = store.fetch(
        "SELECT status, model_version FROM predictor_calls WHERE provider = ? AND purpose LIKE ? "
        "AND created_at >= ?",
        [provider, purpose, since],
    )
    if any(str(version or "").endswith(RECORDED_MARK) for _status, version in rows):
        return "recorded"
    return "live" if any(status == "ok" for status, _version in rows) else "cached"


def _span(text: str) -> tuple[date, date]:
    start, end = text.split("..")
    return date.fromisoformat(start), date.fromisoformat(end)


def _moves(kind: ShockKind, metric: str) -> bool:
    spend, conversions = SHOCK_EFFECTS[kind]
    return (spend if metric == "spend" else conversions) != 1.0


def _relevant(report: AnomalyReport, truth: Truth) -> dict[tuple[str, date], ShockKind]:
    """Planted anomalies a check could have caught: on a checked day, in a metric it moves."""
    spans = {metric: _span(window) for metric, window in report.windows.items()}
    found = {}
    for (entity, day), kind in truth.planted.items():
        for metric, (first, last) in spans.items():
            if _moves(kind, metric) and first <= day <= last:
                found[(entity, day)] = kind
    return found


def _score(
    label: str, report: AnomalyReport, planted: Mapping[tuple[str, date], str] | None
) -> MethodScore:
    flagged = {(f.entity_ref, f.day) for f in report.flags}
    if planted is None:
        return MethodScore(label=label, flagged=len(flagged))
    return MethodScore(
        label=label,
        flagged=len(flagged),
        caught=len(flagged & planted.keys()),
        false_alarms=len(flagged - planted.keys()),
    )


def _alert_headline(scores: Sequence[MethodScore], truth_known: bool) -> str:
    """The finding in a sentence, from the scores alone."""
    if not scores:
        return ""
    first = scores[0]
    name = first.label.split(",")[0]
    alerts = f"{first.flagged} alert{'' if first.flagged == 1 else 's'}"
    rule = next((s for s in scores[1:] if "rule" in s.label), None)
    if not truth_known or first.caught is None:
        text = f"{name} raised {alerts}."
        if rule is not None:
            text += f" A ±50% day-over-day rule raised {rule.flagged} for the same days."
        return text
    text = (
        f"{name} raised {alerts}; {first.caught} "
        f"{'was a real problem' if first.caught == 1 else 'were real problems'}."
    )
    if rule is not None and rule.false_alarms is not None:
        text += (
            f" A ±50% day-over-day rule raised {rule.flagged} for the same days, "
            f"{rule.false_alarms} of them false alarms."
        )
    return text


def _method_label(method: str) -> tuple[str, str]:
    """(provider, what a reader calls it) from a method such as `tabpfn_band95`."""
    if method == RULE:
        return "rule", "±50% day-over-day rule"
    provider, _, band = method.partition("_band")
    return provider, f"{MODEL_NAMES.get(provider, provider)}, {band}% expected range"


def _amount(value: float, metric: str) -> str:
    if not math.isfinite(value):
        return "none"
    return f"{value:,.0f}" if metric == "spend" else f"{value:,.1f}".removesuffix(".0")


def _anomaly_how(
    report: AnomalyReport,
    rule: AnomalyReport | None,
    planted: Mapping[tuple[str, date], ShockKind] | None,
    *,
    provider: str,
    band: float,
    currency: str | None,
) -> AnomalyHow | None:
    """One flagged day followed through the model: the rows that went in, the range that came
    out, and two days that show why a range conditioned on the row beats a fixed rule."""
    flagged = {(f.entity_ref, f.day) for f in report.flags}
    flags = [f for f in report.flags if f.metric in report.inputs and f.lo is not None]
    real = [
        f
        for f in flags
        if planted and (kind := planted.get((f.entity_ref, f.day))) and _moves(kind, f.metric)
    ]
    chosen = (real or flags)[:1]  # flags are sorted most extreme first
    if not chosen:
        return None
    flag = chosen[0]
    given = report.inputs[flag.metric]
    col = {name: i for i, name in enumerate(given.columns)}
    extra = "budget" if flag.metric == "spend" else "spend"
    unit = (currency or "") if flag.metric == "spend" else "conversions"

    def cells(row: InputRow) -> tuple[str, ...]:
        return (
            row.entity_name,
            f"{row.day:%a, %b} {row.day.day}",
            _amount(row.features[col["med7"]], flag.metric),
            _amount(row.features[col["lag7"]], flag.metric),
            _amount(row.features[col[extra]], "spend"),
        )

    judged = next(
        (r for r in given.judged if (r.entity_ref, r.day) == (flag.entity_ref, flag.day)), None
    )
    if judged is None:
        return None
    before = sorted(
        (r for r in given.history if r.entity_ref == flag.entity_ref), key=lambda r: r.day
    )[-2:]
    rows = [HowRow(cells=cells(r), answer=_amount(r.target, flag.metric)) for r in before]
    rows.append(HowRow(cells=cells(judged), answer="?", asked=True))
    kind = planted.get((flag.entity_ref, flag.day)) if planted else None
    note = None
    if planted is not None:
        note = f"{kind.replace('_', ' ')}, caught" if kind else "false alarm"
    tail = 100 * (1 - band) / 2
    bands = {(b.entity_ref, b.day, b.metric): b for b in report.bands}

    step = None
    spend = report.inputs.get("spend")
    if spend is not None:
        at = spend.columns.index("budget")
        by_day = {(r.entity_ref, r.day): r for r in spend.judged}
        best = (False, 0.0)
        for (entity, day), row in by_day.items():
            earlier = by_day.get((entity, date.fromordinal(day.toordinal() - 1)))
            now_band = bands.get((entity, day, "spend"))
            if earlier is None or now_band is None or (entity, day) in flagged:
                continue
            then_band = bands.get((entity, earlier.day, "spend"))
            old, new = earlier.features[at], row.features[at]
            if then_band is None or not (math.isfinite(old) and math.isfinite(new)) or old <= 0:
                continue
            change = abs(new / old - 1)
            # The example's own campaign first, so the reader can find the step on its chart.
            rank = (entity == flag.entity_ref, change)
            if change >= MIN_BUDGET_STEP and rank > best:
                best = rank
                step = RangeStep(
                    entity_name=row.entity_name,
                    day=day,
                    unit=currency or "",
                    budget_before=round(old, 2),
                    budget_after=round(new, 2),
                    expected_before=round(then_band.expected, 2),
                    expected_after=round(now_band.expected, 2),
                )

    contrast = None
    if rule is not None:
        quiet = []
        for alarm in rule.flags:
            judged_as = bands.get((alarm.entity_ref, alarm.day, alarm.metric))
            if (
                judged_as is None
                or alarm.expected is None
                or (alarm.entity_ref, alarm.day) in flagged
                or (planted and (alarm.entity_ref, alarm.day) in planted)
                # A day whose conversions are still arriving is scaled up by the rule; keep the
                # days where the rule and the model judged the same number.
                or abs(alarm.observed - judged_as.observed) > 0.01
            ):
                continue
            quiet.append((alarm, judged_as))
        if quiet:
            alarm, judged_as = quiet[0]  # the rule's flags are sorted most extreme first
            contrast = RuleContrast(
                entity_name=alarm.entity_name,
                day=alarm.day,
                metric=alarm.metric,
                unit=(currency or "") if alarm.metric == "spend" else "conversions",
                before=round(float(alarm.expected or 0.0), 2),
                observed=round(judged_as.observed, 2),
                change=round(judged_as.observed / float(alarm.expected or 1.0) - 1, 3),
                lo=round(judged_as.lo, 2),
                hi=round(judged_as.hi, 2),
                others=len({(a.entity_ref, a.day) for a, _ in quiet}) - 1,
                planted_known=planted is not None,
            )

    return AnomalyHow(
        model=MODEL_NAMES.get(provider, provider),
        about=ABOUT_RANGES.get(provider, ""),
        requests=len(report.inputs),
        history_rows=len(given.history),
        judged_rows=len(given.judged),
        headers=(
            "Campaign",
            "Day",
            "Usual of last 7 days",
            "A week before",
            "Budget" if extra == "budget" else "Spend",
        ),
        answer_header="Spend" if flag.metric == "spend" else "Conversions",
        rows=tuple(rows),
        other_columns=(
            "the platform",
            "the day number, for trend",
            "a simple estimate: the usual level scaled by the "
            + ("budget change" if extra == "budget" else "day's spend"),
        ),
        example=RangeExample(
            entity_name=flag.entity_name,
            day=flag.day,
            metric=flag.metric,
            unit=unit,
            lo=round(float(flag.lo or 0.0), 2),
            expected=round(float(flag.expected or 0.0), 2),
            hi=round(float(flag.hi or 0.0), 2),
            observed=round(flag.observed, 2),
            low_level=round(tail, 2),
            high_level=round(100 - tail, 2),
            note=note,
        ),
        step=step,
        contrast=contrast,
    )


async def anomaly_panel(
    store: Store,
    predictor: Predictor | None,
    *,
    account_alias: str,
    as_of: date,
    window_days: int = 14,
    band: float = DEFAULT_BAND,
    truth: Truth | None = None,
    currency: str | None = None,
) -> AnomalyPanel | None:
    """Every checked day against its expected range, and how other methods judged the same days."""
    since = utc_now()
    report = await check_anomalies(
        store, predictor, record=False, band=band, as_of=as_of, window_days=window_days,
        account_alias=account_alias,
    )  # fmt: skip
    if not report.bands:
        return None  # no model drew a range (too little history, or it was unavailable)
    methods = sorted(set(report.methods.values()), key=lambda m: m == RULE)
    provider, label = _method_label(methods[0])
    planted = _relevant(report, truth) if truth is not None else None
    flagged_days = {(f.entity_ref, f.day) for f in report.flags}
    grouped: dict[tuple[str, str], list[BandDay]] = {}
    names: dict[str, str] = {}
    for point in sorted(report.bands, key=lambda p: p.day):
        names[point.entity_ref] = point.entity_name
        kind = planted.get((point.entity_ref, point.day)) if planted else None
        if kind is not None and not _moves(kind, point.metric):
            kind = None  # a tracking outage is drawn on conversions, not on spend
        note = None
        if planted is not None:
            # Judged per campaign-day, as the scores are: a day is caught if either metric flags.
            day_flagged = (point.entity_ref, point.day) in flagged_days
            really = planted.get((point.entity_ref, point.day))
            if point.flagged and really:
                note = f"{really.replace('_', ' ')}, caught"
            elif point.flagged:
                note = "false alarm"
            elif kind is not None and not day_flagged:
                note = f"{kind.replace('_', ' ')}, missed"
        grouped.setdefault((point.entity_ref, point.metric), []).append(
            BandDay(
                day=point.day,
                observed=round(point.observed, 2),
                expected=round(point.expected, 2),
                lo=round(point.lo, 2),
                hi=round(point.hi, 2),
                flagged=point.flagged,
                planted=kind,
                note=note,
            )
        )
    series = [
        BandSeries(
            entity_ref=entity,
            entity_name=names[entity],
            metric=metric,
            unit=(currency or "") if metric == "spend" else "conversions",
            days=tuple(days),
        )
        for (entity, metric), days in grouped.items()
    ]
    # Only charts with something to see, most eventful first; the quiet ones are counted.
    total = len(series)
    eventful = [s for s in series if any(d.flagged or d.planted for d in s.days)]
    if eventful:
        series = eventful
    series.sort(key=lambda s: (-sum(d.flagged or bool(d.planted) for d in s.days), s.entity_ref))
    notes = list(report.notes)
    scores = [_score(label, report, planted)]
    by_rule: AnomalyReport | None = None
    others: list[tuple[str, Predictor | None]] = []
    if provider not in ("local", "rule"):
        others.append(("local", LocalPredictor()))
    if provider != "rule":
        others.append(("rule", None))
    for _name, other in others:
        compared = await check_anomalies(
            store, other, record=False, band=band, as_of=as_of, window_days=window_days,
            account_alias=account_alias,
        )  # fmt: skip
        other_methods = sorted(set(compared.methods.values()), key=lambda m: m == RULE)
        if other_methods:
            scores.append(_score(_method_label(other_methods[0])[1], compared, planted))
        if _name == "rule":
            by_rule = compared
    return AnomalyPanel(
        label=label,
        headline=_alert_headline(scores, planted is not None),
        hidden_series=total - len(series[:MAX_SERIES]),
        method=methods[0],
        source="rule" if provider == "rule" else _source(store, provider, "anomaly:%", since),
        windows=dict(report.windows),
        series=tuple(series[:MAX_SERIES]),
        scores=tuple(scores),
        planted=len(planted) if planted is not None else None,
        notes=tuple(notes),
        how=_anomaly_how(report, by_rule, planted, provider=provider, band=band, currency=currency),
    )


def _line(value: Callable[[float], float], grid: np.ndarray) -> tuple[tuple[float, float], ...]:
    """A curve sampled on a spend grid, as (spend, conversions) points."""
    return tuple((round(float(s), 2), round(float(value(float(s))), 3)) for s in grid)


def _curves(run: BanditRun, truth: Truth | None, other: BanditRun | None) -> list[BudgetCurve]:
    alternatives = {d.arm.key: d.posterior for d in other.decisions} if other else {}
    curves = []
    for decision in run.decisions:
        arm, post = decision.arm, decision.posterior
        if not arm.eligible or post is None or not len(arm.spend):
            continue
        mean = greedy(post)
        if mean is None:
            continue
        fitted = post.curve(mean)
        now = arm.expected_spend(float(arm.current_budget or 0.0))
        advised = (
            arm.expected_spend(float(decision.final_budget))
            if decision.final_budget is not None
            else None
        )
        recent = arm.spend[-HISTORY_DAYS:]
        low = max(0.0, 0.6 * float(recent.min()))
        high = 1.25 * max(float(recent.max()), now, advised or 0.0)
        grid = np.linspace(low, high, CURVE_POINTS)
        real = truth.curves.get(arm.entity_ref) if truth else None
        alt_post = alternatives.get(arm.key)
        alt_mean = greedy(alt_post) if alt_post is not None else None
        alt = alt_post.curve(alt_mean) if alt_post is not None and alt_mean is not None else None
        spend, target = run.pseudo.get(arm.key, (np.zeros(0), np.zeros(0)))
        step = max(1, math.ceil(len(spend) / PSEUDO_POINTS))

        curves.append(
            BudgetCurve(
                entity_ref=arm.entity_ref,
                entity_name=arm.entity_name,
                history=tuple(
                    (round(float(s), 2), round(float(c), 2))
                    for s, c in zip(recent, arm.conversions[-HISTORY_DAYS:], strict=True)
                ),
                pseudo=tuple(
                    (round(float(s), 2), round(math.exp(float(t)) - 1, 3))
                    for s, t in zip(spend[::step], target[::step], strict=True)
                ),
                fitted=_line(fitted.value, grid),
                alternative=_line(alt.value, grid) if alt is not None else (),
                truth=_line(real.value, grid) if real is not None else (),
                current_budget=arm.current_budget,
                recommended_budget=decision.final_budget,
                current_spend=round(now, 2),
                recommended_spend=None if advised is None else round(advised, 2),
                expected_now=decision.expected_conversions_now,
                expected_recommended=decision.expected_conversions,
                limited_by=arm.constraint.kind,
                elasticity=round(float(post.kappa_mean[1]), 3),
                true_elasticity=None if real is None else round(real.kappa2, 3),
            )
        )
    return curves


def _weekly(run: LoopResult) -> tuple[list[date], list[float]]:
    """Whole weeks of the run, from its first scored day: each week's start and conversions."""
    weeks, totals = [], []
    for start in range(0, len(run.daily) - len(run.daily) % 7, 7):
        block = run.daily[start : start + 7]
        weeks.append(block[0][0])
        totals.append(round(sum(value for _day, value in block), 2))
    return weeks, totals


def build_trial(
    static: LoopResult,
    agent: LoopResult,
    best: LoopResult,
    *,
    names: Mapping[str, str],
    agent_label: str,
) -> BudgetTrial | None:
    """What the agent's reallocations bought over the same weeks of one simulated account,
    against budgets left alone and the best possible split. All three are scored by the
    simulation's true curves, so the comparison does not depend on any fitted model."""
    days = len(agent.daily)
    if not days or not agent.budgets or len(static.daily) != days or len(best.daily) != days:
        return None
    weeks, _ = _weekly(agent)
    runs = []
    for key, label, run in (
        ("static", "Budgets left alone", static),
        ("agent", agent_label, agent),
        ("best", "Best possible", best),
    ):
        _, weekly = _weekly(run)
        runs.append(
            TrialRun(
                key=key,
                label=label,
                weekly=tuple(weekly),
                per_day=round(run.expected_conversions / days, 2),
            )
        )
    left, with_agent, possible = (r.expected_conversions for r in (static, agent, best))
    start, now = agent.budgets[0][1], agent.budgets[-1][1]
    return BudgetTrial(
        weeks=tuple(weeks),
        days=days,
        runs=tuple(runs),
        gain=round(with_agent / left - 1, 4) if left else 0.0,
        best_gain=round(possible / left - 1, 4) if left else 0.0,
        captured=round((with_agent - left) / (possible - left), 3) if possible > left else None,
        decisions=agent.decisions,
        rows=tuple(
            BudgetRow(
                entity_ref=ref,
                entity_name=names.get(ref, ref),
                start=round(float(start.get(ref, 0.0)), 2),
                now=round(float(budget), 2),
            )
            for ref, budget in sorted(now.items())
        ),
    )


def _trial_headline(trial: BudgetTrial) -> str:
    by = {run.key: run for run in trial.runs}
    weeks = len(trial.weeks)
    text = (
        f"Over {weeks} weeks the agent's weekly reallocations produced "
        f"{by['agent'].per_day:.1f} conversions a day, against {by['static'].per_day:.1f} with "
        f"the budgets left alone: {trial.gain:+.1%} at the same total budget."
    )
    if trial.captured is not None:
        text += (
            f" The best possible split would have produced {by['best'].per_day:.1f}, so the "
            f"agent captured {trial.captured:.0%} of the gain there was to have."
        )
    return text


def _prior_label(prior_source: str) -> tuple[str, str]:
    """(provider, what a reader calls the global model) from `tabpfn:v3.5_default`."""
    provider = prior_source.split(":", 1)[0]
    if provider == "tabpfn":
        return provider, "TabPFN as the global model"
    if provider == "pooled":
        return "local", "Pooled regression as the global model"
    return "local", f"{prior_source} curves"


def _direction(before: float | None, after: float | None) -> Literal["up", "down", "flat"]:
    if not before or after is None or abs(after / before - 1) < 0.02:
        return "flat"
    return "up" if after > before else "down"


def _budget_how(
    run: BanditRun, truth: Truth | None, trial: BudgetTrial | None, *, provider: str
) -> BudgetHow | None:
    """One campaign followed through the global model: its days, the spends it was asked about,
    the answers, and what the fitted curves then say a little more spend buys in each campaign."""
    fitted = [
        (d, d.posterior.curve(mean))
        for d in run.decisions
        if d.arm.eligible
        and d.posterior is not None
        and len(d.arm.spend)
        and (mean := greedy(d.posterior)) is not None
    ]
    asked = {d.arm.key: run.pseudo[d.arm.key] for d, _ in fitted if d.arm.key in run.pseudo}
    if not fitted or not asked:
        return None
    start = {row.entity_ref: row.start for row in trial.rows} if trial else {}

    def moved(decision: ArmDecision) -> float:
        now = float(decision.arm.current_budget or 0.0)
        before = start.get(decision.arm.entity_ref)
        if before:
            return abs(now / before - 1)
        return abs(float(decision.final_budget or now) / now - 1) if now else 0.0

    example = max((d for d, _ in fitted if d.arm.key in asked), key=moved)
    arm = example.arm
    spends, targets = asked[arm.key]
    order = np.argsort(spends)
    seen = list(zip(arm.days, arm.spend, arm.conversions, strict=True))
    # Recent days are scaled up for conversions still arriving; show days that are simply counted.
    whole = [row for row in seen if abs(float(row[2]) - round(float(row[2]))) < 1e-6]
    rows = [
        HowRow(
            cells=(arm.entity_name, f"{day:%a, %b} {day.day}", f"{float(spent):,.0f}"),
            answer=_amount(float(converted), "conversions"),
        )
        for day, spent, converted in (whole if len(whole) >= 2 else seen)[-2:]
    ]
    for i in dict.fromkeys((int(order[0]), int(order[-1]))):
        rows.append(
            HowRow(
                cells=(arm.entity_name, "any weekday", f"{float(spends[i]):,.0f}"),
                answer="?",
                asked=True,
                predicted=f"{math.exp(float(targets[i])) - 1:,.1f}",
            )
        )
    tried, when = None, "in its history"
    if trial is not None and trial.weeks:
        earlier = [
            float(s) for day, s in zip(arm.days, arm.spend, strict=True) if day < trial.weeks[0]
        ]
        if earlier:
            tried, when = (round(min(earlier), 2), round(max(earlier), 2)), "before the agent"
    if tried is None and len(arm.spend):
        tried = (round(float(arm.spend.min()), 2), round(float(arm.spend.max()), 2))
    now_budget = float(arm.current_budget or 0.0)
    reached = (
        now_budget if tried and not tried[0] <= arm.expected_spend(now_budget) <= tried[1] else None
    )

    budgets = sorted(float(d.arm.current_budget or 0.0) for d, _ in fitted)
    middle = budgets[len(budgets) // 2] or 1.0
    step = float(10 ** max(0, round(math.log10(middle) - 0.5)))
    marginals = []
    for decision, curve in fitted:
        a = decision.arm
        real = truth.curves.get(a.entity_ref) if truth else None
        now = float(a.current_budget or 0.0)
        before = start.get(a.entity_ref)

        def buys(budget: float, a: Arm = a, curve: PowerCurve = curve) -> float:
            return round(step * curve.marginal(a.expected_spend(budget)), 2)

        def really(budget: float, a: Arm = a, real: CampaignTruth | None = real) -> float | None:
            return (
                None if real is None else round(step * real.marginal(a.expected_spend(budget)), 2)
            )

        marginals.append(
            MarginalRow(
                entity_name=a.entity_name,
                start_budget=before,
                now_budget=round(now, 2),
                start_predicted=buys(before) if before else None,
                start_true=really(before) if before else None,
                now_predicted=buys(now),
                now_true=really(now),
                moved=_direction(before, now) if before else None,
                next_move=_direction(now, decision.final_budget),
            )
        )
    headline = f"Budget goes to where the next {step:,.0f} a day buys the most."
    with_start = [m for m in marginals if m.start_predicted is not None]
    if with_start:
        top = max(with_start, key=lambda m: m.start_predicted or 0.0)
        headline += (
            f" Before the agent that was {top.entity_name}: "
            f"{top.start_predicted:.2f} more conversions a day by the fitted curve"
        )
        if top.start_true is not None:
            headline += f", {top.start_true:.2f} by the true one"
        headline += "."
    balance = ""
    if with_start and len(with_start) == len(marginals) > 1:

        def gap(values: Sequence[float | None]) -> float | None:
            known = [v for v in values if v is not None]
            return round(max(known) - min(known), 2) if len(known) == len(values) else None

        was, now_gap = (
            gap([m.start_predicted for m in marginals]),
            gap([m.now_predicted for m in marginals]),
        )
        balance = (
            "A split is balanced when the next unit buys about the same everywhere. The gap "
            f"between the best and the worst campaign went from {was:.2f} to {now_gap:.2f}"
        )
        true_was, true_now = (
            gap([m.start_true for m in marginals]),
            gap([m.now_true for m in marginals]),
        )
        if true_was is not None and true_now is not None:
            balance += f" (by the true curves, from {true_was:.2f} to {true_now:.2f})"
        balance += "."
    name = "TabPFN" if provider == "tabpfn" else "Pooled regression"
    return BudgetHow(
        model=name,
        about=ABOUT_BUDGETS.get(provider, ABOUT_BUDGETS["pooled"]),
        history_rows=sum(len(d.arm.spend) for d, _ in fitted),
        campaigns=len(fitted),
        levels=len(spends),
        entity_name=arm.entity_name,
        headers=("Campaign", "Day", "Spend"),
        answer_header="Conversions",
        rows=tuple(rows),
        tried=tried,
        tried_when=when,
        reached=round(reached, 2) if reached is not None else None,
        step=step,
        marginals=tuple(marginals),
        headline=headline,
        balance=balance,
    )


async def budget_panel(
    store: Store,
    predictor: Predictor | None,
    *,
    account_alias: str,
    as_of: date,
    truth: Truth | None = None,
    config: BanditConfig | None = None,
    trial: BudgetTrial | None = None,
) -> BudgetPanel | None:
    """Each campaign's spend response and the budget split the curves recommend. `trial` is what
    earlier reallocations bought on a simulated account; its rows gain the next recommendation."""
    since = utc_now()
    config = config or BanditConfig(policy="greedy")
    run = await recommend(
        store, predictor, record=False, seed=7, as_of=as_of, account_alias=account_alias,
        config=config,
    )  # fmt: skip
    provider, label = _prior_label(run.prior_source)
    other = None
    if provider == "tabpfn":
        # The default global model on the same history, so a reader can compare the two.
        other = await recommend(
            store, None, record=False, seed=7, as_of=as_of, account_alias=account_alias,
            config=config,
        )  # fmt: skip
    curves = _curves(run, truth, other)
    if not curves:
        return None
    notes = list(run.notes)
    if len(curves) > MAX_CURVES:
        notes.append(f"{len(curves) - MAX_CURVES} more campaigns are not shown")
    if trial is not None:
        by_ref = {c.entity_ref: c for c in curves}
        trial = trial.model_copy(
            update={
                "rows": tuple(
                    row.model_copy(
                        update={
                            "recommended": by_ref[row.entity_ref].recommended_budget,
                            "limited_by": by_ref[row.entity_ref].limited_by,
                        }
                    )
                    if row.entity_ref in by_ref
                    else row
                    for row in trial.rows
                )
            }
        )
    return BudgetPanel(
        label=label,
        headline=_trial_headline(trial) if trial is not None else "",
        trial=trial,
        alternative_label="Pooled regression as the global model" if other else None,
        prior_source=run.prior_source,
        source=_source(store, provider, "bandit:%", since),
        currency=run.currency,
        curves=tuple(curves[:MAX_CURVES]),
        total_now=round(sum(float(c.current_budget or 0.0) for c in curves), 2),
        total_recommended=round(sum(float(c.recommended_budget or 0.0) for c in curves), 2),
        notes=tuple(notes),
        how=_budget_how(run, truth, trial, provider=run.prior_source.split(":", 1)[0]),
    )


def change_panel(proposal: ProposalView, receipt: str) -> ChangePanel:
    """A change as its reviewer saw it, from the record, and what its readback found."""
    return ChangePanel(
        account_ref=proposal.account_ref,
        summary=tuple(proposal_summary(proposal).splitlines()),
        receipt=receipt,
    )


async def build_insights(
    store: Store,
    predictor: Predictor | None,
    *,
    account_alias: str,
    as_of: date,
    window_days: int = 14,
    truth: Truth | None = None,
    currency: str | None = None,
    change: ChangePanel | None = None,
    trial: BudgetTrial | None = None,
    config: BanditConfig | None = None,
) -> ReportInsights:
    """The panels the stored history supports; one that cannot be built is left out."""
    return ReportInsights(
        anomaly=await anomaly_panel(
            store, predictor, account_alias=account_alias, as_of=as_of, window_days=window_days,
            truth=truth, currency=currency,
        ),
        budgets=await budget_panel(
            store, predictor, account_alias=account_alias, as_of=as_of, truth=truth, trial=trial,
            config=config,
        ),
        change=change,
    )  # fmt: skip
