"""Report panels beyond the two-window comparison: expected ranges, budget curves, a change.

Each panel is built from stored history with the same functions the agent's tools use, so the
report shows what `check_anomalies` and `recommend_budgets` would say, drawn. A panel names the
model that produced it and whether its figures came from a live call, a stored result, or a
fallback. On a simulated account the known truth (planted anomalies, true curves) is drawn too.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np

from paid_media_agent.analytics.anomalies import DEFAULT_BAND, RULE, AnomalyReport, check_anomalies
from paid_media_agent.bandit.policy import greedy
from paid_media_agent.bandit.recommend import BanditConfig, BanditRun, recommend
from paid_media_agent.domain.presentation import ProposalView, proposal_summary
from paid_media_agent.domain.reports import (
    AnomalyPanel,
    BandDay,
    BandSeries,
    BudgetCurve,
    BudgetPanel,
    ChangePanel,
    MethodScore,
    ReportInsights,
    ResultSource,
)
from paid_media_agent.predict.local import LocalPredictor
from paid_media_agent.predict.protocol import Predictor
from paid_media_agent.sim.simulator import SHOCK_EFFECTS, CampaignTruth, ShockKind
from paid_media_agent.store.db import Store, utc_now

MAX_SERIES = 6
MAX_CURVES = 6
HISTORY_DAYS = 60
CURVE_POINTS = 40
PSEUDO_POINTS = 32
MODEL_NAMES = {"tabpfn": "TabPFN", "local": "Local model"}
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


def _method_label(method: str) -> tuple[str, str]:
    """(provider, what a reader calls it) from a method such as `tabpfn_band95`."""
    if method == RULE:
        return "rule", "±50% day-over-day rule"
    provider, _, band = method.partition("_band")
    return provider, f"{MODEL_NAMES.get(provider, provider)}, {band}% expected range"


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
    grouped: dict[tuple[str, str], list[BandDay]] = {}
    names: dict[str, str] = {}
    for point in sorted(report.bands, key=lambda p: p.day):
        names[point.entity_ref] = point.entity_name
        kind = planted.get((point.entity_ref, point.day)) if planted else None
        if kind is not None and not _moves(kind, point.metric):
            kind = None  # a tracking outage is drawn on conversions, not on spend
        grouped.setdefault((point.entity_ref, point.metric), []).append(
            BandDay(
                day=point.day,
                observed=round(point.observed, 2),
                expected=round(point.expected, 2),
                lo=round(point.lo, 2),
                hi=round(point.hi, 2),
                flagged=point.flagged,
                planted=kind,
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
    # What happened first: series with a flag or a planted anomaly, then the rest.
    series.sort(key=lambda s: (-sum(d.flagged or bool(d.planted) for d in s.days), s.entity_ref))
    notes = list(report.notes)
    if len(series) > MAX_SERIES:
        notes.append(f"{len(series) - MAX_SERIES} more campaign charts are not shown")
    scores = [_score(label, report, planted)]
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
    return AnomalyPanel(
        label=label,
        method=methods[0],
        source="rule" if provider == "rule" else _source(store, provider, "anomaly:%", since),
        windows=dict(report.windows),
        series=tuple(series[:MAX_SERIES]),
        scores=tuple(scores),
        planted=len(planted) if planted is not None else None,
        notes=tuple(notes),
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


def _prior_label(prior_source: str) -> tuple[str, str]:
    """(provider, what a reader calls the global model) from `tabpfn:v3.5_default`."""
    provider = prior_source.split(":", 1)[0]
    if provider == "tabpfn":
        return provider, "TabPFN as the global model"
    if provider == "pooled":
        return "local", "Pooled regression as the global model"
    return "local", f"{prior_source} curves"


async def budget_panel(
    store: Store,
    predictor: Predictor | None,
    *,
    account_alias: str,
    as_of: date,
    truth: Truth | None = None,
    config: BanditConfig | None = None,
) -> BudgetPanel | None:
    """Each campaign's spend response and the budget split the curves recommend."""
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
    return BudgetPanel(
        label=label,
        alternative_label="Pooled regression as the global model" if other else None,
        prior_source=run.prior_source,
        source=_source(store, provider, "bandit:%", since),
        currency=run.currency,
        curves=tuple(curves[:MAX_CURVES]),
        total_now=round(sum(float(c.current_budget or 0.0) for c in curves), 2),
        total_recommended=round(sum(float(c.recommended_budget or 0.0) for c in curves), 2),
        notes=tuple(notes),
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
) -> ReportInsights:
    """The panels the stored history supports; one that cannot be built is left out."""
    return ReportInsights(
        anomaly=await anomaly_panel(
            store, predictor, account_alias=account_alias, as_of=as_of, window_days=window_days,
            truth=truth, currency=currency,
        ),
        budgets=await budget_panel(
            store, predictor, account_alias=account_alias, as_of=as_of, truth=truth
        ),
        change=change,
    )  # fmt: skip
