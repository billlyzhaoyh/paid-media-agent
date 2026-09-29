"""Synthetic campaigns with known ground truth, for testing prediction and budget allocation.

Each campaign's expected final conversions follow a power law in spend,

    E[y] = w(weekday) * exp(kappa1) * x ** kappa2,    x = spend, 0 < kappa2 < 1,

so zero spend buys nothing and each extra unit buys less (the budget bandit's assumptions).
Conversions are Poisson draws around it. Spend is the budget times a pacing ratio. Conversions are
reported with a delay, so a day's count grows over later pulls. Injected shocks are labelled, and
some campaigns start partway through (cold starts).

A scenario is fully determined by its parameters, including the seed. Each campaign-day draws its
noise from its own generator, so two runs that set different budgets still share pacing and
noise draws (common random numbers), which makes policies directly comparable.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import date, timedelta
from typing import Any, Literal

import numpy as np

from paid_media_agent.domain.common import Platform

ShockKind = Literal["tracking_outage", "spend_spike", "conversion_surge"]
SHOCK_EFFECTS: dict[ShockKind, tuple[float, float]] = {
    "tracking_outage": (1.0, 0.1),
    "spend_spike": (1.8, 1.0),
    "conversion_surge": (1.0, 2.5),
}
"""(spend multiplier, conversion multiplier) for each labelled shock."""


@dataclass(frozen=True)
class ScenarioParams:
    scenario_id: str = "baseline"
    seed: int = 7
    n_campaigns: int = 5
    days: int = 180
    start: date = date(2026, 1, 1)
    platform: Platform = Platform.GOOGLE_ADS
    currency: str = "USD"
    kappa2_range: tuple[float, float] = (0.55, 0.9)
    cpa_range: tuple[float, float] = (25.0, 80.0)
    """Cost per conversion at each campaign's base budget; it sets kappa1."""
    budget_range: tuple[float, float] = (80.0, 600.0)
    weekday_amplitude: float = 0.15
    pacing_mean: float = 0.93
    pacing_sd: float = 0.04
    lag_mean_days: float = 2.0
    max_lag_days: int = 21
    shock_rate: float = 0.01
    """Probability of a labelled shock per campaign-day."""
    cold_starts: int = 1
    budget_change_every: tuple[int, int] = (10, 28)
    budget_change_factor: tuple[float, float] = (0.7, 1.35)
    ceiling_share: float = 0.0
    """Share of campaigns whose spend stops at a ceiling (demand or a bid target), not the budget.
    Zero keeps every scenario exactly as before: ceilings use their own random numbers."""
    ceiling_range: tuple[float, float] = (0.5, 0.9)
    """Each ceiling as a multiple of the campaign's spend at its base budget."""
    target_share: float = 0.5
    """Of the capped campaigns, the share held back by a bid target rather than by demand."""
    emit_signals: bool = False
    """Also report Google-style impression-share losses and status reasons, derived from truth."""

    def as_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["start"] = self.start.isoformat()
        data["platform"] = self.platform.value
        return data


@dataclass(frozen=True)
class CampaignTruth:
    entity_ref: str
    name: str
    kappa1: float
    kappa2: float
    base_budget: float
    start_index: int
    cpm: float
    ctr: float
    aov: float
    weekday_peak: int
    ceiling: float | None = None
    """The most the campaign can spend a day, whatever its budget (None: the budget binds)."""
    limit: Literal["demand", "target"] | None = None
    target_cpa: float | None = None

    def response(self, spend: float) -> float:
        """Expected final conversions at `spend` on an average weekday."""
        return math.exp(self.kappa1) * math.pow(max(spend, 0.0), self.kappa2)

    def value(self, spend: float) -> float:
        """Expected conversions at a spend level it may not reach: flat above the ceiling."""
        return self.response(spend if self.ceiling is None else min(spend, self.ceiling))

    def marginal(self, spend: float) -> float:
        """Extra expected conversions per unit of extra spend at `spend`."""
        if self.ceiling is not None and spend >= self.ceiling:
            return 0.0
        return self.kappa2 * math.exp(self.kappa1) * math.pow(max(spend, 1e-9), self.kappa2 - 1)

    def spend_at_marginal(self, marginal: float) -> float:
        """The spend at which one more unit of spend buys `marginal` conversions."""
        cap = math.inf if self.ceiling is None else self.ceiling
        if marginal <= 0:
            return cap
        found = math.pow(marginal / (self.kappa2 * math.exp(self.kappa1)), 1 / (self.kappa2 - 1))
        return min(found, cap)


@dataclass(frozen=True)
class DayOutcome:
    entity_ref: str
    index: int
    day: date
    budget: float
    spend: float
    weekday_factor: float
    expected_conversions: float
    conversions: int
    conversion_value: float
    impressions: int
    clicks: int
    anomaly: ShockKind | None
    lags: tuple[int, ...] = field(repr=False)
    """Conversions by reporting delay in days; `lags[d]` arrived `d` days after the day."""
    limited_by: Literal["budget", "demand", "target"] = "budget"

    def reported_conversions(self, age_days: int) -> int:
        """Conversions visible to a pull made `age_days` after the day (delays below the age)."""
        return int(sum(self.lags[: max(0, age_days)]))


class Simulator:
    def __init__(self, params: ScenarioParams) -> None:
        self.params = params
        self._rng = np.random.default_rng(params.seed)
        self.campaigns = self._draw_ceilings(self._draw_campaigns())
        self.shocks = self._draw_shocks()
        self.outcomes: dict[tuple[str, int], DayOutcome] = {}
        delays = np.arange(params.max_lag_days + 1)
        p = 1.0 / (1.0 + params.lag_mean_days)
        weights = p * (1 - p) ** delays
        self._lag_weights = weights / weights.sum()

    def _draw_campaigns(self) -> tuple[CampaignTruth, ...]:
        p, rng = self.params, self._rng
        cold = set(rng.choice(p.n_campaigns, size=min(p.cold_starts, p.n_campaigns), replace=False))
        campaigns = []
        for i in range(p.n_campaigns):
            kappa2 = float(rng.uniform(*p.kappa2_range))
            budget = float(rng.uniform(*p.budget_range))
            cpa = float(rng.uniform(*p.cpa_range))
            spend = budget * p.pacing_mean
            kappa1 = math.log(spend / cpa) - kappa2 * math.log(spend)
            start = int(rng.integers(int(p.days * 0.5), int(p.days * 0.8))) if i in cold else 0
            campaigns.append(
                CampaignTruth(
                    entity_ref=f"sim-{i + 1:03d}",
                    name=f"Simulated campaign {i + 1}",
                    kappa1=kappa1,
                    kappa2=kappa2,
                    base_budget=round(budget, 2),
                    start_index=start,
                    cpm=float(rng.uniform(4.0, 25.0)),
                    ctr=float(rng.uniform(0.005, 0.04)),
                    aov=float(rng.uniform(40.0, 180.0)),
                    weekday_peak=int(rng.integers(0, 7)),
                )
            )
        return tuple(campaigns)

    def _draw_ceilings(self, campaigns: tuple[CampaignTruth, ...]) -> tuple[CampaignTruth, ...]:
        """Ceilings from their own generator, so the rest of a scenario never changes."""
        p = self.params
        if p.ceiling_share <= 0:
            return campaigns
        rng = np.random.default_rng([p.seed, 1_000_003])
        drawn = []
        for campaign in campaigns:
            capped = rng.random() < p.ceiling_share
            multiple = float(rng.uniform(*p.ceiling_range))
            by_target = rng.random() < p.target_share
            if not capped:
                drawn.append(campaign)
                continue
            ceiling = round(campaign.base_budget * p.pacing_mean * multiple, 2)
            drawn.append(
                replace(
                    campaign,
                    ceiling=ceiling,
                    limit="target" if by_target else "demand",
                    target_cpa=round(ceiling / campaign.response(ceiling), 2)
                    if by_target
                    else None,
                )
            )
        return tuple(drawn)

    def _draw_shocks(self) -> dict[tuple[str, int], ShockKind]:
        kinds: list[ShockKind] = list(SHOCK_EFFECTS)
        shocks: dict[tuple[str, int], ShockKind] = {}
        for campaign in self.campaigns:
            for t in range(campaign.start_index, self.params.days):
                if self._rng.random() < self.params.shock_rate:
                    shocks[(campaign.entity_ref, t)] = kinds[int(self._rng.integers(len(kinds)))]
        return shocks

    def day(self, index: int) -> date:
        return self.params.start + timedelta(days=index)

    def active(self, index: int) -> tuple[CampaignTruth, ...]:
        return tuple(c for c in self.campaigns if c.start_index <= index)

    def weekday_factor(self, campaign: CampaignTruth, index: int) -> float:
        angle = 2 * math.pi * (self.day(index).weekday() - campaign.weekday_peak) / 7
        return 1.0 + self.params.weekday_amplitude * math.cos(angle)

    def expected_conversions(self, campaign: CampaignTruth, spend: float, index: int) -> float:
        """The truth: expected final conversions for `spend` on day `index`, before shocks."""
        return self.weekday_factor(campaign, index) * campaign.response(spend)

    def step(self, index: int, budgets: Mapping[str, float]) -> list[DayOutcome]:
        """Run day `index` with the given daily budgets and return what really happened."""
        p = self.params
        outcomes = []
        for number, campaign in enumerate(self.campaigns):
            if campaign.start_index > index:
                continue
            rng = np.random.default_rng([p.seed, number, index])
            budget = float(budgets[campaign.entity_ref])
            anomaly = self.shocks.get((campaign.entity_ref, index))
            spend_mult, conv_mult = SHOCK_EFFECTS[anomaly] if anomaly else (1.0, 1.0)
            pacing = float(np.clip(rng.normal(p.pacing_mean, p.pacing_sd), 0.5, 1.0))
            spend = round(budget * pacing * spend_mult, 2)
            expected = self.expected_conversions(campaign, spend, index)
            conversions = int(rng.poisson(expected * conv_mult))
            lags = rng.multinomial(conversions, self._lag_weights)
            impressions = int(spend / campaign.cpm * 1000 * rng.lognormal(0.0, 0.05))
            clicks = int(rng.binomial(impressions, campaign.ctr))
            value = round(conversions * campaign.aov * float(rng.lognormal(0.0, 0.1)), 2)
            limited_by: Literal["budget", "demand", "target"] = "budget"
            if campaign.ceiling is not None:
                # What the campaign can win today; its own generator keeps the others unchanged.
                cap_rng = np.random.default_rng([p.seed, number, index, 1_000_003])
                cap = round(campaign.ceiling * float(cap_rng.lognormal(0.0, 0.08)), 2)
                if cap < spend:
                    share = cap / spend
                    spend, limited_by = cap, campaign.limit or "demand"
                    expected = self.expected_conversions(campaign, spend, index)
                    conversions = int(cap_rng.poisson(expected * conv_mult))
                    lags = cap_rng.multinomial(conversions, self._lag_weights)
                    impressions, clicks = int(impressions * share), int(clicks * share)
                    value = round(
                        conversions * campaign.aov * float(cap_rng.lognormal(0.0, 0.1)), 2
                    )
            outcome = DayOutcome(
                entity_ref=campaign.entity_ref,
                index=index,
                day=self.day(index),
                budget=budget,
                spend=spend,
                weekday_factor=self.weekday_factor(campaign, index),
                expected_conversions=expected,
                conversions=conversions,
                conversion_value=value,
                impressions=impressions,
                clicks=clicks,
                anomaly=anomaly,
                lags=tuple(int(n) for n in lags),
                limited_by=limited_by,
            )
            self.outcomes[(campaign.entity_ref, index)] = outcome
            outcomes.append(outcome)
        return outcomes

    def report(self, pulled_index: int, window_days: int) -> list[dict[str, Any]]:
        """Native Google Ads style rows as a pull on the morning of day `pulled_index` sees them.

        The window is the `window_days` days before the pull. Conversions are what has arrived so
        far; value arrives in proportion.
        """
        rows = []
        for index in range(max(0, pulled_index - window_days), pulled_index):
            for campaign in self.campaigns:
                outcome = self.outcomes.get((campaign.entity_ref, index))
                if outcome is None:
                    continue
                reported = outcome.reported_conversions(pulled_index - index)
                share = reported / outcome.conversions if outcome.conversions else 0.0
                rows.append(
                    {
                        "date": outcome.day.isoformat(),
                        "campaign_id": campaign.entity_ref,
                        "campaign_name": campaign.name,
                        "cost_micros": int(round(outcome.spend * 1_000_000)),
                        "impressions": outcome.impressions,
                        "clicks": outcome.clicks,
                        "conversions": reported,
                        "conversions_value": round(outcome.conversion_value * share, 2),
                    }
                )
        return rows


def budget_schedule(sim: Simulator) -> dict[str, list[float]]:
    """Budgets per campaign per day: step changes at random intervals, like a human operator.

    The variation is what makes each campaign's response curve identifiable from history.
    """
    p = sim.params
    rng = np.random.default_rng(p.seed + 1)
    schedule: dict[str, list[float]] = {}
    for campaign in sim.campaigns:
        budget = campaign.base_budget
        next_change = campaign.start_index + int(rng.integers(*p.budget_change_every))
        days = []
        for t in range(p.days):
            if t >= next_change:
                factor = float(rng.uniform(*p.budget_change_factor))
                budget = round(
                    float(
                        np.clip(
                            budget * factor, 0.3 * campaign.base_budget, 3 * campaign.base_budget
                        )
                    ),
                    2,
                )
                next_change = t + int(rng.integers(*p.budget_change_every))
            days.append(budget)
        schedule[campaign.entity_ref] = days
    return schedule
