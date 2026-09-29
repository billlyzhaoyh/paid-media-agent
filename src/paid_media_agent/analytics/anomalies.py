"""Anomaly checks over stored history: is a day's spend or conversions outside its expected range?

For each campaign-day in the check window, a predictor learns from the preceding weeks what the
metric should be given the campaign, weekday, recent level, a week earlier, and (for spend) the
budget in force or (for conversions) that day's spend. A value outside the predicted 95% band is
flagged. Budget changes therefore explain spend steps, and spend explains conversion moves.

Recent conversions are still arriving. The conversions window trails the spend window by the
days it takes most conversions to arrive (from each account's lag curve), so every day is checked
once; its value is compared with the band scaled by the share that has arrived by then.
Without a predictor, or when it is unavailable, the ±50% day-over-day rule runs instead and every
flag says so.

The history is read as it stood on `as_of`, so a backtest sees exactly what a live check would.
"""

from __future__ import annotations

import json
import math
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

import numpy as np

from paid_media_agent.analytics.panel import (
    PanelRow,
    completeness,
    lag_curves,
    load_panel,
    maturity_days,
)
from paid_media_agent.predict.protocol import (
    PredictionRequest,
    Predictor,
    PredictorUnavailable,
    TargetKind,
)
from paid_media_agent.store.db import Store, utc_now

Metric = Literal["spend", "conversions"]
METRICS: tuple[Metric, ...] = ("spend", "conversions")
DEFAULT_BAND = 0.95
"""Share of normal days the expected range should contain; wider catches more and alarms more."""
TRAIN_DAYS = 90
MIN_TRAIN_ROWS = 20
MIN_COMPLETENESS = 0.75
"""A recent day's conversions are checked once the lag curve says this share has arrived."""
MIN_COUNT_DEVIATION = 2.0
"""A conversion count must also differ from the expectation by at least this many to be flagged."""
SPEND_ELASTICITY = 0.8
DOD_THRESHOLD = 0.5
RULE = "dod_rule_fallback"
_COLUMNS: dict[Metric, tuple[str, ...]] = {
    "spend": ("entity", "platform", "weekday", "t", "med7", "lag7", "naive", "budget"),
    "conversions": ("entity", "platform", "weekday", "t", "med7", "lag7", "naive", "spend"),
}
_KIND: dict[Metric, TargetKind] = {"spend": "amount", "conversions": "count"}


@dataclass(frozen=True)
class AnomalyFlag:
    account_alias: str
    platform: str
    entity_ref: str
    entity_name: str
    day: date
    metric: Metric
    observed: float
    expected: float | None
    lo: float | None
    hi: float | None
    direction: Literal["up", "down"]
    score: float
    method: str


@dataclass
class AnomalyReport:
    check_id: uuid.UUID
    as_of: date
    window_start: date | None
    window_end: date | None
    predictor: str
    methods: dict[str, str] = field(default_factory=dict)
    windows: dict[str, str] = field(default_factory=dict)
    """Days checked per metric; conversions trail spend while late conversions arrive."""
    rows_checked: dict[str, int] = field(default_factory=dict)
    flags: list[AnomalyFlag] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "check_id": str(self.check_id),
            "as_of": self.as_of.isoformat(),
            "window": None
            if self.window_start is None or self.window_end is None
            else f"{self.window_start.isoformat()}..{self.window_end.isoformat()}",
            "predictor": self.predictor,
            "methods": self.methods,
            "windows": self.windows,
            "rows_checked": self.rows_checked,
            "flags": [
                {
                    **{k: v for k, v in asdict(f).items() if k != "day"},
                    "day": f.day.isoformat(),
                    "observed": round(f.observed, 2),
                    "expected": None if f.expected is None else round(f.expected, 2),
                    "lo": None if f.lo is None else round(f.lo, 2),
                    "hi": None if f.hi is None else round(f.hi, 2),
                    "score": round(f.score, 2),
                }
                for f in self.flags
            ],
            "notes": self.notes,
        }


@dataclass
class _Series:
    """One metric's rows with features, targets, and per-row metadata."""

    rows: list[PanelRow] = field(default_factory=list)
    features: list[list[float]] = field(default_factory=list)
    targets: list[float] = field(default_factory=list)
    completeness: list[float] = field(default_factory=list)


def _median(values: Sequence[float]) -> float:
    finite = [v for v in values if math.isfinite(v)]
    return float(np.median(finite)) if len(finite) >= 3 else math.nan


def _conversion_delay(
    panel: Sequence[PanelRow], maturity: dict[str, int], curves: dict[str, dict[int, float]]
) -> int:
    """Days until most conversions have arrived, for the slowest account in scope."""
    delays = []
    for account, platform in {(r.provider_account_id, r.platform) for r in panel}:
        curve = curves.get(account, {})
        ready = [age for age, share in curve.items() if share >= MIN_COMPLETENESS]
        delays.append(min(ready) if ready else maturity.get(platform, 7))
    return max(delays, default=0)


def _build(
    panel: Sequence[PanelRow],
    metric: Metric,
    *,
    window: tuple[date, date],
    maturity: dict[str, int],
    curves: dict[str, dict[int, float]],
) -> tuple[_Series, _Series, int]:
    """Training rows before the window, test rows inside it, and how many test days were skipped."""
    start, end = window
    first_day = min(r.day for r in panel)
    entities = {k: i for i, k in enumerate(sorted({r.key for r in panel}))}
    platforms = {p: i for i, p in enumerate(sorted({r.platform for r in panel}))}
    by_entity: dict[tuple[str, str, str], dict[date, PanelRow]] = defaultdict(dict)
    for row in panel:
        by_entity[row.key][row.day] = row

    def share_of(row: PanelRow) -> float | None:
        return completeness(row, maturity, curves)

    def value(row: PanelRow | None, of: Metric) -> float:
        if row is None:
            return math.nan
        if of == "spend":
            return row.spend
        if row.conversions is None:
            return math.nan
        share = share_of(row)
        if share is None:
            return row.conversions
        return row.conversions / share if share >= 0.3 else math.nan

    train, test, skipped = _Series(), _Series(), 0
    for key, days in by_entity.items():
        for day, row in days.items():
            if not row.is_complete or day > end or day < start - timedelta(days=TRAIN_DAYS):
                continue
            previous = [days.get(day - timedelta(days=k)) for k in range(1, 8)]
            med7 = _median([value(p, metric) for p in previous])
            if not math.isfinite(med7):
                continue
            lag7 = value(days.get(day - timedelta(days=7)), metric)
            if metric == "spend":
                budgets = [p.budget for p in previous if p is not None and p.budget]
                naive = med7
                if row.budget and budgets:
                    naive = med7 * row.budget / float(np.median(budgets))
                extra = row.budget if row.budget is not None else math.nan
                target = row.spend
                share = 1.0
            else:
                if row.conversions is None:
                    continue
                spend_med7 = _median([value(p, "spend") for p in previous])
                naive = med7
                if spend_med7 > 0 and row.spend > 0:
                    naive = med7 * (row.spend / spend_med7) ** SPEND_ELASTICITY
                extra = row.spend
                target = row.conversions
                share = share_of(row) or 0.0
            features = [
                float(entities[key]),
                float(platforms[row.platform]),
                float(day.weekday()),
                float((day - first_day).days),
                med7,
                lag7,
                naive,
                extra,
            ]
            if day < start:
                if metric == "conversions" and share < 1.0:
                    continue  # train only on matured conversions
                series = train
            else:
                if metric == "conversions" and share < MIN_COMPLETENESS:
                    skipped += 1
                    continue
                series = test
            series.rows.append(row)
            series.features.append(features)
            series.targets.append(target)
            series.completeness.append(share)
    return train, test, skipped


def _band_flags(
    test: _Series, metric: Metric, lo: np.ndarray, mid: np.ndarray, hi: np.ndarray, method: str
) -> list[AnomalyFlag]:
    flags = []
    for i, row in enumerate(test.rows):
        share = test.completeness[i]
        low, centre, high = lo[i] * share, mid[i] * share, hi[i] * share
        observed = test.targets[i]
        if not all(math.isfinite(v) for v in (low, centre, high)):
            continue
        if low <= observed <= high:
            continue
        if metric == "conversions" and abs(observed - centre) < MIN_COUNT_DEVIATION:
            continue
        up = observed > high
        edge, width = (high, high - centre) if up else (low, centre - low)
        flags.append(
            AnomalyFlag(
                account_alias=row.account_alias,
                platform=row.platform,
                entity_ref=row.entity_ref,
                entity_name=row.entity_name,
                day=row.day,
                metric=metric,
                observed=observed,
                expected=centre,
                lo=low,
                hi=high,
                direction="up" if up else "down",
                score=abs(observed - edge) / max(width, 1e-6),
                method=method,
            )
        )
    return flags


def _rule_flags(
    test: _Series,
    metric: Metric,
    panel: Sequence[PanelRow],
    maturity: dict[str, int],
    curves: dict[str, dict[int, float]],
) -> list[AnomalyFlag]:
    """The ±50% day-over-day rule on the same eligible rows, both days corrected for lag."""
    index = {(r.key, r.day): r for r in panel}
    flags = []
    for i, row in enumerate(test.rows):
        prior = index.get((row.key, row.day - timedelta(days=1)))
        if prior is None:
            continue
        observed = test.targets[i] / max(test.completeness[i], 1e-6)
        before = prior.spend if metric == "spend" else prior.conversions
        if not before:
            continue
        if metric == "conversions":
            # The previous day's conversions are still arriving too; compare like with like.
            share = completeness(prior, maturity, curves)
            before = before / max(share, 1e-6) if share is not None else before
        change = observed / before - 1
        if abs(change) < DOD_THRESHOLD:
            continue
        flags.append(
            AnomalyFlag(
                account_alias=row.account_alias,
                platform=row.platform,
                entity_ref=row.entity_ref,
                entity_name=row.entity_name,
                day=row.day,
                metric=metric,
                observed=observed,
                expected=before,
                lo=before * (1 - DOD_THRESHOLD),
                hi=before * (1 + DOD_THRESHOLD),
                direction="up" if change > 0 else "down",
                score=abs(change) / DOD_THRESHOLD - 1,
                method=RULE,
            )
        )
    return flags


async def check_anomalies(
    store: Store,
    predictor: Predictor | None,
    *,
    as_of: date,
    window_days: int = 7,
    account_alias: str | None = None,
    clock: Callable[[], datetime] = utc_now,
    record: bool = True,
    band: float = DEFAULT_BAND,
) -> AnomalyReport:
    """Check the latest `window_days` complete days before `as_of` and record the result."""
    if not 0.5 <= band < 1:
        raise ValueError("band must be at least 0.5 and below 1")
    tail = round((1 - band) / 2, 6)
    quantiles = (tail, 0.5, round(1 - tail, 6))
    report = AnomalyReport(
        check_id=uuid.uuid4(),
        as_of=as_of,
        window_start=None,
        window_end=None,
        predictor=predictor.name if predictor else "none",
    )
    panel = load_panel(
        store,
        as_of=as_of,
        since=as_of - timedelta(days=TRAIN_DAYS + window_days + 14),
        account_alias=account_alias,
    )
    complete_days = [r.day for r in panel if r.is_complete and r.day < as_of]
    if not complete_days:
        report.notes.append("no stored history for this scope; run `sync` or read the account")
    else:
        end = max(complete_days)
        start = end - timedelta(days=window_days - 1)
        report.window_start, report.window_end = start, end
        maturity, curves = maturity_days(store), lag_curves(store)
        for metric in METRICS:
            delay = _conversion_delay(panel, maturity, curves) if metric == "conversions" else 0
            # Ages count from the pull, which is a day after the newest complete day.
            shift = timedelta(days=max(0, delay - 1))
            report.windows[metric] = f"{(start - shift).isoformat()}..{(end - shift).isoformat()}"
            train, test, skipped = _build(
                panel,
                metric,
                window=(start - shift, end - shift),
                maturity=maturity,
                curves=curves,
            )
            if skipped:
                report.notes.append(
                    f"{skipped} recent {metric} days not checked: conversions are still arriving"
                )
            report.rows_checked[metric] = len(test.rows)
            if not test.rows:
                continue
            method = RULE
            if predictor is None:
                pass
            elif len(train.rows) < MIN_TRAIN_ROWS:
                report.notes.append(
                    f"{metric}: {len(train.rows)} days of usable history before the window; "
                    f"the model needs {MIN_TRAIN_ROWS}, so the day-over-day rule ran"
                )
            else:
                request = PredictionRequest(
                    purpose=f"anomaly:{metric}",
                    columns=_COLUMNS[metric],
                    x_train=np.asarray(train.features, dtype=np.float64),
                    y_train=np.asarray(train.targets, dtype=np.float64),
                    x_test=np.asarray(test.features, dtype=np.float64),
                    quantiles=quantiles,
                    kind=_KIND[metric],
                )
                try:
                    prediction = await predictor.predict(request)
                except PredictorUnavailable as exc:
                    report.notes.append(f"{metric}: {exc}; the day-over-day rule ran instead")
                else:
                    method = f"{prediction.provider}_band{round(band * 100)}"
                    report.flags += _band_flags(
                        test,
                        metric,
                        prediction.at(quantiles[0]),
                        prediction.at(quantiles[1]),
                        prediction.at(quantiles[2]),
                        method,
                    )
            if method == RULE:
                report.flags += _rule_flags(test, metric, panel, maturity, curves)
            report.methods[metric] = method
    report.flags.sort(key=lambda f: f.score, reverse=True)
    if record:
        _record(store, report, account_alias, clock(), panel)
    return report


def _record(
    store: Store,
    report: AnomalyReport,
    account_alias: str | None,
    at: datetime,
    panel: Sequence[PanelRow],
) -> None:
    accounts = {(r.platform, r.account_alias): r.provider_account_id for r in panel}
    with store.transaction() as cursor:
        cursor.execute(
            "INSERT INTO anomaly_checks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                report.check_id,
                account_alias,
                report.window_start,
                report.window_end,
                report.as_of,
                report.predictor,
                json.dumps(report.methods),
                sum(report.rows_checked.values()),
                len(report.flags),
                json.dumps(report.notes),
                at,
            ],
        )
        for flag in report.flags:
            cursor.execute(
                "INSERT INTO anomaly_flags VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    uuid.uuid4(),
                    report.check_id,
                    flag.platform,
                    accounts[(flag.platform, flag.account_alias)],
                    flag.account_alias,
                    flag.entity_ref,
                    flag.entity_name,
                    flag.day,
                    flag.metric,
                    flag.observed,
                    flag.expected,
                    flag.lo,
                    flag.hi,
                    flag.direction,
                    flag.score,
                    flag.method,
                    at,
                ],
            )
