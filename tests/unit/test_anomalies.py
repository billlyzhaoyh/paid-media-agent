"""Anomaly checks on simulated history with injected problems, as a live check would see it."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from paid_media_agent.analytics.anomalies import RULE, check_anomalies
from paid_media_agent.analytics.ingest import AnalyticsRecorder
from paid_media_agent.domain.common import EntityType, Platform
from paid_media_agent.domain.metrics import MetricWindow, PerformanceRow
from paid_media_agent.predict.local import LocalPredictor
from paid_media_agent.predict.protocol import Prediction, PredictionRequest, PredictorUnavailable
from paid_media_agent.sim.scenario import run_scenario, scenario_binding
from paid_media_agent.sim.simulator import ScenarioParams
from paid_media_agent.store import Store

PARAMS = ScenarioParams(
    scenario_id="anomaly",
    seed=11,
    n_campaigns=5,
    days=90,
    start=date(2026, 3, 2),
    shock_rate=0.0,
    cold_starts=0,
)
AS_OF = PARAMS.start + timedelta(days=PARAMS.days)  # the morning after the last simulated day
LAST_DAY = AS_OF - timedelta(days=1)
LAST_COMPLETE = LAST_DAY - timedelta(days=1)  # the newest day is still settling, as on a platform


def _latest(store: Store, ref: str, day: date) -> tuple[float, float]:
    spend, conversions = store.fetch(
        "SELECT spend::DOUBLE, conversions::DOUBLE FROM entity_daily_latest "
        "WHERE entity_ref = ? AND day = ?",
        [ref, day],
    )[0]
    return spend, conversions


def _restate(store: Store, ref: str, day: date, *, spend: float, conversions: float) -> None:
    """A later pull on the check date that reports different numbers for one campaign-day."""
    AnalyticsRecorder(store).record_performance(
        source="sync",
        binding=scenario_binding(PARAMS),
        tool_name="google_ads__get_campaign_performance",
        catalog_revision=None,
        rows=[
            PerformanceRow(
                platform=Platform.GOOGLE_ADS,
                account_ref=scenario_binding(PARAMS).alias,
                entity_type=EntityType.CAMPAIGN,
                entity_ref=ref,
                entity_name=ref,
                window=MetricWindow(start=day, end=day, timezone="UTC", is_complete=True),
                currency="USD",
                spend=Decimal(str(spend)),
                conversions=Decimal(str(conversions)),
            )
        ],
        pulled_at=datetime.combine(AS_OF, datetime.min.time()).replace(hour=7),
    )


@pytest.fixture(scope="module")
def history() -> Store:
    store = Store()
    run_scenario(store, PARAMS)
    busiest = store.fetch(
        "SELECT entity_ref FROM entity_daily_latest GROUP BY 1 ORDER BY sum(conversions) DESC"
    )[0][0]
    spike_day, outage_day = LAST_DAY - timedelta(days=2), LAST_DAY - timedelta(days=5)
    spend, conversions = _latest(store, "sim-002", spike_day)
    _restate(store, "sim-002", spike_day, spend=spend * 2.5, conversions=conversions)
    spend, _ = _latest(store, busiest, outage_day)
    _restate(store, busiest, outage_day, spend=spend, conversions=0)
    store.write("CREATE TABLE test_meta AS SELECT ? AS busiest", [busiest])
    return store


async def test_the_local_band_finds_an_overspend_and_a_tracking_break(history: Store) -> None:
    busiest = history.fetch("SELECT busiest FROM test_meta")[0][0]
    report = await check_anomalies(history, LocalPredictor(), as_of=AS_OF, record=False)

    assert report.window_end == LAST_COMPLETE and report.methods == {
        "spend": "local_band95",
        "conversions": "local_band95",
    }
    found = {(f.entity_ref, f.day, f.metric, f.direction) for f in report.flags}
    assert ("sim-002", LAST_DAY - timedelta(days=2), "spend", "up") in found
    assert (busiest, LAST_DAY - timedelta(days=5), "conversions", "down") in found
    assert report.flags[0].score >= report.flags[-1].score
    assert len(report.flags) <= 6, [(f.entity_ref, f.day, f.metric) for f in report.flags]


async def test_the_check_reads_history_as_it_stood_on_the_date(history: Store) -> None:
    earlier = await check_anomalies(
        history, LocalPredictor(), as_of=AS_OF - timedelta(days=1), record=False
    )
    assert earlier.window_end == LAST_COMPLETE - timedelta(days=1)
    spike = [f for f in earlier.flags if f.entity_ref == "sim-002" and f.metric == "spend"]
    assert not any(f.observed > 1.5 * (f.expected or 0) for f in spike), "the restatement is later"


async def test_without_a_predictor_the_labelled_rule_runs(history: Store) -> None:
    report = await check_anomalies(history, None, as_of=AS_OF, record=False)
    assert report.predictor == "none" and set(report.methods.values()) == {RULE}
    assert report.flags and all(f.method == RULE for f in report.flags)


class _Down:
    name = "down"

    async def estimate_tokens(self, request: PredictionRequest) -> int:
        return 0

    async def predict(self, request: PredictionRequest) -> Prediction:
        raise PredictorUnavailable("token cap reached")


async def test_an_unavailable_predictor_falls_back_and_says_why(history: Store) -> None:
    report = await check_anomalies(history, _Down(), as_of=AS_OF, record=False)
    assert set(report.methods.values()) == {RULE}
    assert any("token cap reached; the day-over-day rule ran instead" in n for n in report.notes)


async def test_a_check_is_recorded_by_alias_without_provider_ids(history: Store) -> None:
    report = await check_anomalies(history, LocalPredictor(), as_of=AS_OF)
    stored = history.fetch(
        "SELECT flag_count, rows_checked, predictor FROM anomaly_checks WHERE check_id = ?",
        [report.check_id],
    )
    assert stored == [(len(report.flags), sum(report.rows_checked.values()), "local")]
    flags = history.fetch(
        "SELECT account_alias, provider_account_id, method FROM anomaly_flags WHERE check_id = ?",
        [report.check_id],
    )
    assert len(flags) == len(report.flags) and flags[0][:2] == ("sim-anomaly", "sim-anomaly")
    assert "provider_account_id" not in json.dumps(report.as_json())


async def test_no_history_and_short_history_are_explained() -> None:
    empty = await check_anomalies(Store(), LocalPredictor(), as_of=AS_OF, record=False)
    assert empty.window_end is None and "no stored history" in empty.notes[0]

    short = Store()
    run_scenario(short, ScenarioParams(scenario_id="short", days=12, n_campaigns=1, cold_starts=0))
    report = await check_anomalies(short, LocalPredictor(), as_of=date(2026, 1, 13), record=False)
    assert report.methods.get("spend") == RULE
    assert any("the model needs 20" in n for n in report.notes)


def test_budget_steps_are_explained_rather_than_flagged(history: Store) -> None:
    """Operator budget changes in the window move spend without being anomalies."""
    import asyncio

    report = asyncio.run(check_anomalies(history, LocalPredictor(), as_of=AS_OF, record=False))
    changed = {
        (ref, when.date())
        for ref, when in history.fetch(
            "SELECT entity_ref, occurred_at FROM change_events WHERE occurred_at >= ?",
            [datetime.combine(LAST_DAY - timedelta(days=7), datetime.min.time())],
        )
    }
    assert changed, "the scenario changes budgets inside the window"
    spend_flags = {(f.entity_ref, f.day) for f in report.flags if f.metric == "spend"}
    assert not (spend_flags & changed)
    assert np.isfinite([f.score for f in report.flags]).all()
