"""The global model: what every campaign's history says about each one, as pseudo-samples.

One model learns log(conversions + 1) from all campaigns' days at once, so a campaign with little
budget variation (or little history) borrows the spend response seen elsewhere. For each campaign
it predicts `k` points at evenly spaced spends around the campaign's own recent range, averaged
over weekdays; these pseudo-samples join the campaign's history in its local model (CBS section
3.4, "transfer process").

Two global models exist. TabPFN (when `PAID_MEDIA_PREDICTOR=tabpfn`) sees campaign, platform,
weekday, day index, and log spend, with no assumed shape. Otherwise a pooled regression runs
locally: a level per campaign, weekday effects, and one spend elasticity shared by all campaigns,
with recent days weighted more (half-life `half_life_days`; CBS section 6.1).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from paid_media_agent.bandit.arms import Arm
from paid_media_agent.predict.protocol import PredictionRequest, Predictor, PredictorUnavailable

POOLED = "pooled"
MAX_GRID = 64
"""Pseudo-samples beyond this many spends are carried as weights on a 64-point grid."""
RIDGE = 1e-3
MIN_ROWS = 10
_COLUMNS = ("entity", "platform", "weekday", "t", "log_spend")


@dataclass(frozen=True)
class Query:
    arm: int
    spend: float
    weekday: int


@dataclass
class GlobalPredictions:
    values: np.ndarray
    """log(conversions + 1) per query; NaN where the model could not predict."""
    source: str
    model_version: str
    notes: list[str] = field(default_factory=list)


def pseudo_grid(arm: Arm, k: int) -> np.ndarray:
    """`k` spends across the campaign's recent range, widened to at least ±25% of its median."""
    if k <= 0 or not len(arm.spend):
        return np.zeros(0)
    recent = arm.spend[-28:]
    median = float(np.median(recent))
    low = min(float(np.quantile(recent, 0.1)), 0.75 * median)
    high = max(float(np.quantile(recent, 0.9)), 1.25 * median)
    return np.linspace(max(low, 1e-6), max(high, 2e-6), k)


def _pooled(
    arms: Sequence[Arm], queries: Sequence[Query], as_of: date, half_life_days: float
) -> GlobalPredictions:
    n = len(arms)
    rows, targets, weights = [], [], []
    for i, arm in enumerate(arms):
        for day, spent, converted in zip(arm.days, arm.spend, arm.conversions, strict=True):
            features = np.zeros(n + 7)
            features[i] = 1.0
            if day.weekday() > 0:
                features[n + day.weekday() - 1] = 1.0
            features[-1] = math.log(spent / arm.unit + 1.0)
            rows.append(features)
            targets.append(math.log(converted + 1.0))
            weights.append(0.5 ** ((as_of - day).days / half_life_days))
    if len(rows) < MIN_ROWS:
        return GlobalPredictions(
            np.full(len(queries), np.nan),
            POOLED,
            "pooled-loglog/1",
            [f"{len(rows)} settled campaign-days; the global model needs {MIN_ROWS}"],
        )
    x, y, w = np.asarray(rows), np.asarray(targets), np.asarray(weights)
    beta = np.linalg.solve((x.T * w) @ x + RIDGE * np.eye(x.shape[1]), (x.T * w) @ y)
    values = np.empty(len(queries))
    for j, query in enumerate(queries):
        arm = arms[query.arm]
        weekday = beta[n + query.weekday - 1] if query.weekday > 0 else 0.0
        if not arm.n_history:
            values[j] = np.nan
            continue
        values[j] = beta[query.arm] + weekday + beta[-1] * math.log(query.spend / arm.unit + 1.0)
    return GlobalPredictions(values, POOLED, "pooled-loglog/1")


async def _tabpfn(
    arms: Sequence[Arm], queries: Sequence[Query], as_of: date, predictor: Predictor
) -> GlobalPredictions:
    first = min((d for a in arms for d in a.days), default=as_of)
    platforms = {p: i for i, p in enumerate(sorted({a.platform for a in arms}))}
    train, target = [], []
    for i, arm in enumerate(arms):
        for day, spent, converted in zip(arm.days, arm.spend, arm.conversions, strict=True):
            train.append(
                [i, platforms[arm.platform], day.weekday(), (day - first).days, math.log(spent + 1)]
            )
            target.append(math.log(converted + 1.0))
    if len(train) < MIN_ROWS:
        raise PredictorUnavailable(f"{len(train)} settled campaign-days; needs {MIN_ROWS}")
    t_now = (as_of - first).days
    test = [
        [q.arm, platforms[arms[q.arm].platform], q.weekday, t_now, math.log(q.spend + 1)]
        for q in queries
    ]
    prediction = await predictor.predict(
        PredictionRequest(
            purpose="bandit:global",
            columns=_COLUMNS,
            x_train=np.asarray(train, dtype=np.float64),
            y_train=np.asarray(target, dtype=np.float64),
            x_test=np.asarray(test, dtype=np.float64),
            quantiles=(0.5,),
            kind="amount",
        )
    )
    return GlobalPredictions(prediction.at(0.5), prediction.provider, prediction.model_version)


async def global_predict(
    arms: Sequence[Arm],
    queries: Sequence[Query],
    *,
    as_of: date,
    predictor: Predictor | None,
    half_life_days: float = 28.0,
) -> GlobalPredictions:
    """log(conversions + 1) for each query, from TabPFN when configured, else pooled regression."""
    if predictor is not None and predictor.name != "local" and queries:
        try:
            return await _tabpfn(arms, queries, as_of, predictor)
        except PredictorUnavailable as exc:
            pooled = _pooled(arms, queries, as_of, half_life_days)
            pooled.notes.insert(0, f"global model: {exc}; the pooled regression ran instead")
            return pooled
    return _pooled(arms, queries, as_of, half_life_days)


@dataclass
class PseudoSamples:
    spend: dict[int, np.ndarray]
    target: dict[int, np.ndarray]
    weight: float
    """How many pseudo-samples each grid point stands for."""
    source: str
    model_version: str
    notes: list[str]


async def pseudo_samples(
    arms: Sequence[Arm],
    *,
    as_of: date,
    k: int,
    predictor: Predictor | None,
    half_life_days: float = 28.0,
) -> PseudoSamples:
    """`k` pseudo-samples per campaign at spends around its recent range, averaged over weekdays."""
    points = min(k, MAX_GRID)
    grids = {i: pseudo_grid(arm, points) for i, arm in enumerate(arms)}
    queries = [
        Query(i, float(spend), weekday)
        for i, grid in grids.items()
        for spend in grid
        for weekday in range(7)
    ]
    predicted = await global_predict(
        arms, queries, as_of=as_of, predictor=predictor, half_life_days=half_life_days
    )
    spends: dict[int, np.ndarray] = {}
    targets: dict[int, np.ndarray] = {}
    offset = 0
    for i, grid in grids.items():
        block = predicted.values[offset : offset + 7 * len(grid)].reshape(len(grid), 7)
        offset += 7 * len(grid)
        values = block.mean(axis=1)
        keep = np.isfinite(values)
        spends[i], targets[i] = grid[keep], values[keep]
        if keep.sum() >= 2:
            slope = float(np.polyfit(np.log(spends[i] / arms[i].unit + 1.0), targets[i], 1)[0])
            if not 0 <= slope <= 1:
                predicted.notes.append(
                    f"{arms[i].entity_ref}: the global model's curve has elasticity "
                    f"{slope:.2f}, outside 0 to 1; its pseudo-samples were dropped"
                )
                spends[i], targets[i] = np.zeros(0), np.zeros(0)
    return PseudoSamples(
        spends,
        targets,
        k / points if points else 1.0,
        predicted.source,
        predicted.model_version,
        predicted.notes,
    )
