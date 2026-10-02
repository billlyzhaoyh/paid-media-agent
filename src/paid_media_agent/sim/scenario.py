"""Run a simulated scenario into a state store through the same ingest path as real reads.

Each simulated day: the morning settings observation records that day's budgets, the day runs,
and the next morning's pull re-reports the trailing window, so the store holds the same repeated
snapshots, maturing conversions, and settings history a daily sync would build. Ground truth goes
to `sim_truth`. Scenarios live in their own database file, never in the production state file.
Budgets come from an operator's schedule (`run_scenario`) or from a policy (`ScenarioDriver`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from paid_media_agent.analytics.ingest import AnalyticsRecorder
from paid_media_agent.config import AccountBinding
from paid_media_agent.domain.common import EntityType, JsonValue
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


def _bidding(campaign: CampaignTruth, params: ScenarioParams) -> dict[str, JsonValue]:
    """Bid strategy in the listing, only for scenarios with ceilings (others stay unchanged)."""
    if params.ceiling_share <= 0:
        return {}
    if campaign.limit == "target":
        return {"bid_strategy": "TARGET_CPA", "target_cpa": campaign.target_cpa}
    return {"bid_strategy": "MAXIMIZE_CONVERSIONS"}


class ScenarioDriver:
    """Runs a scenario one day at a time into `store`, with budgets chosen by the caller.

    Each day: the morning settings observation records that day's budgets, the day runs, and the
    next morning's pull re-reports the trailing window. `run_scenario` drives it with an operator's
    budget schedule; the bandit's closed-loop evaluation drives it with a policy.
    """

    def __init__(
        self, store: Store, params: ScenarioParams, *, window_days: int = SYNC_WINDOW_DAYS
    ) -> None:
        if not SCENARIO_ID.match(params.scenario_id):
            raise ValueError("scenario id must be lowercase letters, digits, '-' or '_'")
        self.store = store
        self.params = params
        self.sim = Simulator(params)
        self.binding = scenario_binding(params)
        self._recorder = AnalyticsRecorder(store)
        self._window_days = window_days
        self._tool = f"{params.platform.value}__get_campaign_performance"
        self.pulls = self.rows = self.truth = self.external = 0
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

    def run_day(self, index: int, budgets: Mapping[str, float]) -> list[DayOutcome]:
        """Record the morning's budgets, run day `index`, and pull the next morning."""
        p, sim, binding = self.params, self.sim, self.binding
        day = sim.day(index)
        active = sim.active(index)
        listing = {
            "campaigns": [
                {
                    "id": c.entity_ref,
                    "name": c.name,
                    "status": "ENABLED",
                    "daily_budget": round(float(budgets[c.entity_ref]), 2),
                    **_bidding(c, p),
                }
                for c in active
            ]
        }
        settings = self._recorder.record_settings(
            source="simulator",
            binding=binding,
            tool_name=f"{p.platform.value}__list_campaigns",
            catalog_revision=None,
            payload=listing,
            observed_at=_at(day),
        )
        if settings is not None:
            self.pulls += 1
            self.external += settings.external_changes
        outcomes = sim.step(
            index, {c.entity_ref: round(float(budgets[c.entity_ref]), 2) for c in active}
        )
        if outcomes:
            self.store.write(
                "INSERT INTO sim_truth SELECT ?, ?, * FROM (" + json_rows(_TRUTH_SHAPE) + ")",  # noqa: S608
                [p.scenario_id, p.platform.value, json.dumps(_truth_rows(outcomes, sim))],
            )
            self.truth += len(outcomes)
        if p.emit_signals and outcomes:
            self._record_signals(outcomes, day)
        reported = sim.report(index + 1, self._window_days)
        if not reported:
            return outcomes
        # The newest reported day is still settling, as on a real platform.
        normalized, _ = normalize_rows(
            platform=p.platform,
            account_ref=binding.alias,
            currency=p.currency,
            timezone="UTC",
            rows=reported,
            entity_type=EntityType.CAMPAIGN,
            data_complete_through=day - timedelta(days=1),
        )
        self._recorder.record_performance(
            source="simulator",
            binding=binding,
            tool_name=self._tool,
            catalog_revision=None,
            rows=normalized,
            requested=(sim.day(max(0, index + 1 - self._window_days)), day),
            data_complete_through=day - timedelta(days=1),
            pulled_at=_at(day + timedelta(days=1)),
        )
        self.pulls += 1
        self.rows += len(normalized)
        return outcomes

    def _record_signals(self, outcomes: list[DayOutcome], day: date) -> None:
        """Google-style auction shares and status reasons, derived from what limited each day."""
        shares = {"budget": (0.15, 0.10), "demand": (0.0, 0.05), "target": (0.0, 0.40)}
        reasons = {
            "budget": "BUDGET_CONSTRAINED",
            "demand": "SEARCH_VOLUME_LIMITED",
            "target": "BIDDING_STRATEGY_CONSTRAINED",
        }
        daily = [
            {
                "entity_ref": o.entity_ref,
                "day": o.day.isoformat(),
                "budget_lost_share": shares[o.limited_by][0],
                "rank_lost_share": shares[o.limited_by][1],
                "impression_share": round(1 - sum(shares[o.limited_by]), 2),
            }
            for o in outcomes
        ]
        status = [
            {
                "entity_ref": o.entity_ref,
                "channel_type": "SEARCH",
                "status_reasons": [reasons[o.limited_by]],
                "bidding_status": "ENABLED",
                "learning_status": None,
                "recommended_budget": None,
                "raw": {"limited_by": o.limited_by},
            }
            for o in outcomes
        ]
        self._recorder.record_signals(
            source="simulator",
            binding=self.binding,
            tool_name=f"{self.params.platform.value}__get_campaign_signals",
            catalog_revision=None,
            daily=daily,
            status=status,
            observed_at=_at(day + timedelta(days=1)),
        )

    def result(self) -> ScenarioRun:
        return ScenarioRun(
            params=self.params,
            campaigns=self.sim.campaigns,
            days=self.params.days,
            pulls=self.pulls,
            snapshot_rows=self.rows,
            truth_rows=self.truth,
            shocks=len(self.sim.shocks),
            external_changes=self.external,
        )


def run_scenario(
    store: Store, params: ScenarioParams, *, window_days: int = SYNC_WINDOW_DAYS
) -> ScenarioRun:
    """Simulate `params.days` days of an operator's budget schedule into `store`."""
    driver = ScenarioDriver(store, params, window_days=window_days)
    budgets = budget_schedule(driver.sim)
    for index in range(params.days):
        driver.run_day(index, {ref: days[index] for ref, days in budgets.items()})
    return driver.result()
