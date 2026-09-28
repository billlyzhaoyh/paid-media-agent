"""Run a simulated scenario into a state store through the same ingest path as real reads.

Each simulated day: the morning settings observation records that day's budgets, the day runs,
and the next morning's pull re-reports the trailing window, so the store holds the same repeated
snapshots, maturing conversions, and settings history a daily sync would build. Ground truth goes
to `sim_truth`. Scenarios live in their own database file, never in the production state file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from paid_media_agent.analytics.ingest import AnalyticsRecorder
from paid_media_agent.config import AccountBinding
from paid_media_agent.domain.common import EntityType
from paid_media_agent.sim.simulator import (
    CampaignTruth,
    DayOutcome,
    ScenarioParams,
    Simulator,
    budget_schedule,
)
from paid_media_agent.store.db import Store, json_rows, utc_now
from paid_media_agent.tools.normalize import normalize_rows

SCENARIO_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
PULL_HOUR_UTC = 6
SYNC_WINDOW_DAYS = 28
_TRUTH_SHAPE = {
    "entity_ref": "VARCHAR",
    "day": "DATE",
    "budget": "DOUBLE",
    "spend": "DOUBLE",
    "expected_conversions": "DOUBLE",
    "conversions": "INTEGER",
    "kappa1": "DOUBLE",
    "kappa2": "DOUBLE",
    "weekday_factor": "DOUBLE",
    "injected_anomaly": "VARCHAR",
}


@dataclass(frozen=True)
class ScenarioRun:
    params: ScenarioParams
    campaigns: tuple[CampaignTruth, ...]
    days: int
    pulls: int
    snapshot_rows: int
    truth_rows: int
    shocks: int
    external_changes: int


def scenario_path(state_dir: Path, scenario_id: str) -> Path:
    if not SCENARIO_ID.match(scenario_id):
        raise ValueError("scenario id must be lowercase letters, digits, '-' or '_'")
    return state_dir / f"sim-{scenario_id}.duckdb"


def scenario_binding(params: ScenarioParams) -> AccountBinding:
    return AccountBinding(
        alias=f"sim-{params.scenario_id}",
        platform=params.platform,
        provider_account_id=f"sim-{params.scenario_id}",
        currency=params.currency,
        timezone="UTC",
    )


def _at(day: date) -> datetime:
    return datetime.combine(day, time(PULL_HOUR_UTC))


def _truth_rows(outcomes: list[DayOutcome], sim: Simulator) -> list[dict[str, object]]:
    by_ref = {c.entity_ref: c for c in sim.campaigns}
    return [
        {
            "entity_ref": o.entity_ref,
            "day": o.day.isoformat(),
            "budget": o.budget,
            "spend": o.spend,
            "expected_conversions": o.expected_conversions,
            "conversions": o.conversions,
            "kappa1": by_ref[o.entity_ref].kappa1,
            "kappa2": by_ref[o.entity_ref].kappa2,
            "weekday_factor": o.weekday_factor,
            "injected_anomaly": o.anomaly,
        }
        for o in outcomes
    ]


def run_scenario(
    store: Store, params: ScenarioParams, *, window_days: int = SYNC_WINDOW_DAYS
) -> ScenarioRun:
    """Simulate `params.days` days into `store` and return what was written."""
    if not SCENARIO_ID.match(params.scenario_id):
        raise ValueError("scenario id must be lowercase letters, digits, '-' or '_'")
    sim = Simulator(params)
    budgets = budget_schedule(sim)
    binding = scenario_binding(params)
    recorder = AnalyticsRecorder(store)
    store.write(
        "INSERT INTO sim_scenarios VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            params.scenario_id,
            params.seed,
            params.n_campaigns,
            params.days,
            params.start,
            json.dumps(params.as_json()),
            utc_now(),
        ],
    )
    pulls = rows = truth = external = 0
    tool = f"{params.platform.value}__get_campaign_performance"
    for index in range(params.days):
        day = sim.day(index)
        listing = {
            "campaigns": [
                {
                    "id": c.entity_ref,
                    "name": c.name,
                    "status": "ENABLED",
                    "daily_budget": budgets[c.entity_ref][index],
                }
                for c in sim.active(index)
            ]
        }
        settings = recorder.record_settings(
            source="simulator",
            binding=binding,
            tool_name=f"{params.platform.value}__list_campaigns",
            catalog_revision=None,
            payload=listing,
            observed_at=_at(day),
        )
        if settings is not None:
            pulls += 1
            external += settings.external_changes
        outcomes = sim.step(index, {ref: days[index] for ref, days in budgets.items()})
        if outcomes:
            store.write(
                "INSERT INTO sim_truth SELECT ?, ?, * FROM (" + json_rows(_TRUTH_SHAPE) + ")",  # noqa: S608
                [params.scenario_id, params.platform.value, json.dumps(_truth_rows(outcomes, sim))],
            )
            truth += len(outcomes)
        reported = sim.report(index + 1, window_days)
        if not reported:
            continue
        # The newest reported day is still settling, as on a real platform.
        normalized, _ = normalize_rows(
            platform=params.platform,
            account_ref=binding.alias,
            currency=params.currency,
            timezone="UTC",
            rows=reported,
            entity_type=EntityType.CAMPAIGN,
            data_complete_through=day - timedelta(days=1),
        )
        recorder.record_performance(
            source="simulator",
            binding=binding,
            tool_name=tool,
            catalog_revision=None,
            rows=normalized,
            requested=(sim.day(max(0, index + 1 - window_days)), day),
            data_complete_through=day - timedelta(days=1),
            pulled_at=_at(day + timedelta(days=1)),
        )
        pulls += 1
        rows += len(normalized)
    return ScenarioRun(
        params=params,
        campaigns=sim.campaigns,
        days=params.days,
        pulls=pulls,
        snapshot_rows=rows,
        truth_rows=truth,
        shocks=len(sim.shocks),
        external_changes=external,
    )
