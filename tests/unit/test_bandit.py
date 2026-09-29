"""The budget bandit's parts: allocation, the local model, Thompson draws, and one recorded run."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

import numpy as np
import pytest

from paid_media_agent.bandit.allocate import allocate
from paid_media_agent.bandit.policy import GUARD_Z, draw_thompson
from paid_media_agent.bandit.posterior import A0, PowerCurve, fit_posterior
from paid_media_agent.bandit.prior import MAX_GRID, pseudo_samples
from paid_media_agent.bandit.recommend import BanditConfig, recommend
from paid_media_agent.predict.protocol import Prediction, PredictionRequest, PredictorUnavailable
from paid_media_agent.sim.scenario import run_scenario
from paid_media_agent.sim.simulator import CampaignTruth, ScenarioParams
from paid_media_agent.store import Store


def _truth(kappa2: float, spend: float, cpa: float) -> CampaignTruth:
    return CampaignTruth(
        entity_ref="t",
        name="t",
        kappa1=math.log(spend / cpa) - kappa2 * math.log(spend),
        kappa2=kappa2,
        base_budget=spend,
        start_index=0,
        cpm=10,
        ctr=0.01,
        aov=50,
        weekday_peak=0,
    )


def test_water_filling_meets_the_optimality_conditions() -> None:
    """Unbounded campaigns share one marginal return; bounded ones sit on the right side of it."""
    curves = [
        PowerCurve(1.0, 0.7, 40.0),
        PowerCurve(0.5, 0.8, 60.0),
        PowerCurve(1.5, 0.5, 30.0),
        PowerCurve(1.2, 0.6, 35.0),
    ]
    pacing = [0.9, 0.8, 1.0, 0.95]
    result = allocate(curves, pacing, [1.0] * 4, [10_000.0, 10_000.0, 10_000.0, 150.0], 900.0)
    assert result.feasible and sum(result.budgets) == pytest.approx(900.0, rel=1e-6)
    price = result.shadow_price
    seen = set()
    for c, p, b, why in zip(curves, pacing, result.budgets, result.binding, strict=True):
        marginal = p * c.marginal(p * b)
        if not why:
            assert marginal == pytest.approx(price, rel=1e-4)
        elif why == ("lower",):
            assert marginal <= price
        else:
            assert why == ("upper",) and marginal >= price
        seen.add(why)
    assert seen == {(), ("lower",), ("upper",)}


def test_allocation_matches_a_brute_force_search_on_true_curves() -> None:
    a, b = _truth(0.6, 300, 40), _truth(0.85, 200, 60)
    result = allocate([a, b], [1.0, 1.0], [50.0, 50.0], [600.0, 600.0], 500.0)
    grid = np.arange(50.0, 450.0 + 1e-9, 0.5)
    best = max(grid, key=lambda x: a.value(x) + b.value(500.0 - x))
    assert result.budgets[0] == pytest.approx(best, abs=0.5)


def test_bounds_caps_and_an_infeasible_total_are_reported() -> None:
    curves = [PowerCurve(1.0, 0.7, 40.0), PowerCurve(1.0, 0.7, 40.0)]
    slack = allocate(curves, [1.0, 1.0], [10.0, 10.0], [100.0, 200.0], 1_000.0)
    assert slack.budgets == (100.0, 200.0) and slack.shadow_price == 0.0
    assert slack.binding == (("upper",), ("upper",))

    capped = allocate(curves, [1.0, 1.0], [1.0, 1.0], [1e6, 1e6], 1e7, max_cpia=[80.0, None])
    assert 1 / curves[0].marginal(capped.budgets[0]) == pytest.approx(80.0, rel=1e-6)
    assert capped.binding[0] == ("cpia",) and capped.binding[1] == ("upper",)

    short = allocate(curves, [1.0, 1.0], [300.0, 300.0], [400.0, 400.0], 500.0)
    assert not short.feasible and short.budgets == (300.0, 300.0)
    with pytest.raises(ValueError, match="lower bound"):
        allocate(curves, [1.0, 1.0], [5.0, 5.0], [1.0, 9.0], 10.0)


def test_the_power_curve_inverts_its_marginal() -> None:
    curve = PowerCurve(0.8, 0.65, 45.0)
    for spend in (1.0, 50.0, 400.0, 5_000.0):
        assert curve.spend_at_marginal(curve.marginal(spend)) == pytest.approx(spend, rel=1e-9)
    assert curve.value(0.0) == pytest.approx(math.exp(0.8) - 1)


def _campaign_days(
    kappa2: float, spend: np.ndarray, cpa: float, seed: int
) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    truth = _truth(kappa2, float(np.median(spend)), cpa)
    weekdays = np.arange(len(spend)) % 7
    return rng.poisson([truth.response(x) for x in spend]).astype(float), weekdays


def test_the_local_model_recovers_elasticity_in_cost_per_conversion_units() -> None:
    rng = np.random.default_rng(3)
    spend = rng.uniform(600, 1_800, size=400)
    conversions, weekdays = _campaign_days(0.7, spend, cpa=20.0, seed=4)
    unit = float(spend.sum() / conversions.sum())
    post = fit_posterior(spend, conversions, weekdays, unit)
    assert post.valid and post.precision_kappa2 == 0.0
    assert post.kappa_mean[1] == pytest.approx(0.7, abs=0.06)
    assert post.kappa_mean[0] >= 0, "kappa1 >= 0 holds in cost-per-conversion units"
    assert post.kappa2_sd < 0.06


def test_the_ladder_and_pseudo_samples_decide_a_flat_history() -> None:
    spend = np.full(40, 500.0) * np.random.default_rng(1).normal(1, 0.01, size=40)
    conversions, weekdays = _campaign_days(0.8, spend, cpa=50.0, seed=2)
    unit = float(spend.sum() / conversions.sum())
    alone = fit_posterior(spend, conversions, weekdays, unit)
    assert alone.precision_kappa2 > 0, "no budget variation: the prior has to hold kappa2"
    assert alone.kappa_mean[1] == pytest.approx(0.5, abs=0.2)

    grid = np.linspace(300, 700, 16)
    level = math.log(10 + 1) - 0.8 * math.log(500 / unit + 1)
    target = level + 0.8 * np.log(grid / unit + 1)
    helped = fit_posterior(
        spend,
        conversions,
        weekdays,
        unit,
        pseudo_spend=grid,
        pseudo_target=target,
        pseudo_weight=32,
    )
    assert helped.kappa_mean[1] == pytest.approx(0.8, abs=0.05)
    assert helped.n_pseudo == 512
    assert helped.a == pytest.approx(A0 + 40 / 2), "pseudo-samples do not count as noise data"


def test_thompson_draws_stay_valid_and_inside_the_guardrails() -> None:
    spend = np.random.default_rng(5).uniform(200, 600, size=60)
    conversions, weekdays = _campaign_days(0.6, spend, cpa=40.0, seed=6)
    post = fit_posterior(spend, conversions, weekdays, float(spend.sum() / conversions.sum()))
    rng = np.random.default_rng(0)
    sd = np.sqrt(np.diag(post.kappa_cov))
    draws = [draw_thompson(post, rng)[0] for _ in range(300)]
    z = np.abs((np.asarray(draws) - post.kappa_mean) / sd)
    assert (z <= GUARD_Z + 1e-9).all()
    assert all(k1 >= 0 and 0 <= k2 < 1 for k1, k2 in draws)
    assert len({round(k2, 6) for _, k2 in draws}) > 250, "draws explore"

    impossible = type(post)(**{**post.__dict__, "mean": np.array([0.5, 1.4, *post.mean[2:]])})
    kappa, rejected = draw_thompson(impossible, rng)
    assert kappa == (0.5, 1.4) and rejected >= 1000


PARAMS = ScenarioParams(
    scenario_id="bandit", seed=4, n_campaigns=4, days=70, start=date(2026, 2, 2), cold_starts=0
)
AS_OF = PARAMS.start + timedelta(days=PARAMS.days)


@pytest.fixture(scope="module")
def account() -> Store:
    store = Store()
    run_scenario(store, PARAMS)
    return store


async def test_a_recommendation_respects_every_bound_and_is_recorded(account: Store) -> None:
    config = BanditConfig()
    run = await recommend(account, None, as_of=AS_OF, config=config, mode="simulate", seed=1)
    assert run.data_checks.passed and not run.fallback_used
    assert run.prior_source == "pooled:pooled-loglog/1"
    total = 0.0
    for d in run.decisions:
        current = float(d.arm.current_budget or 0)
        assert d.final_budget is not None and d.lower is not None and d.upper is not None
        assert d.lower - 1e-6 <= d.final_budget <= d.upper + 1e-6
        assert abs(d.final_budget / current - 1) <= config.max_step + 1e-6
        if "hold" in d.constrained_by:
            assert d.final_budget == current and (d.arm.days_since_change or 0) < 7
        total += d.final_budget
    assert total <= run.total_budget + 1e-6
    assert any(d.budget_thompson != d.budget_greedy for d in run.decisions if d.propensity)
    stored = account.fetch_dicts(
        "SELECT arm_key, final_budget, post_mean, n_pseudo, propensity FROM bandit_decisions "
        "WHERE run_id = ? ORDER BY arm_key",
        [run.run_id],
    )
    assert len(stored) == len(run.decisions) and all(r["n_pseudo"] == 512 for r in stored)
    assert all(len(json.loads(r["post_mean"])) == 2 for r in stored)
    shown = json.dumps(run.as_json())
    assert "provider_account_id" not in shown and "sim-bandit" in shown


async def test_failed_data_checks_reuse_the_last_good_curves(account: Store) -> None:
    await recommend(account, None, as_of=AS_OF, mode="simulate", seed=2)
    stale = await recommend(
        account, None, as_of=AS_OF + timedelta(days=20), mode="simulate", seed=3
    )
    assert not stale.data_checks.results["fresh"]["ok"] and stale.fallback_used
    assert stale.prior_source == "last_good"
    assert any("reusing the last good curves" in n for n in stale.notes)
    assert all(d.posterior is not None for d in stale.decisions if d.arm.eligible)
    assert account.fetch(
        "SELECT fallback_used FROM bandit_runs WHERE run_id = ?", [stale.run_id]
    ) == [(True,)]


class _GlobalModel:
    """Stands in for TabPFN: records the request and answers with a known elasticity."""

    name = "tabpfn"

    def __init__(self, fail: bool = False) -> None:
        self.requests: list[PredictionRequest] = []
        self.fail = fail

    async def estimate_tokens(self, request: PredictionRequest) -> int:
        return 10_000

    async def predict(self, request: PredictionRequest) -> Prediction:
        self.requests.append(request)
        if self.fail:
            raise PredictorUnavailable("token cap reached")
        units = request.x_test[:, request.columns.index("log_spend_units")]
        spread = np.asarray(request.quantiles)[:, None] - 0.5
        return Prediction(request.quantiles, 0.6 * units + spread, "tabpfn", "v3.5-test")


async def test_a_configured_global_model_supplies_the_pseudo_samples(account: Store) -> None:
    model = _GlobalModel()
    run = await recommend(account, model, as_of=AS_OF, record=False, seed=4)
    (request,) = model.requests
    eligible = [d for d in run.decisions if d.arm.eligible]
    assert request.n_test == len(eligible) * MAX_GRID * 7 and len(request.quantiles) == 19
    assert request.columns == ("log_cost_per_conversion", "weekday", "log_spend_units")
    assert run.prior_source == "tabpfn:v3.5-test"
    # The quantiles' mean is the pseudo-sample, so the model's elasticity (0.6) carries through.
    arms = [d.arm for d in eligible]
    pseudo = await pseudo_samples(arms, as_of=AS_OF, k=512, predictor=_GlobalModel())
    for i, arm in enumerate(arms):
        units = np.log(pseudo.spend[i] / arm.unit + 1)
        assert np.polyfit(units, pseudo.target[i], 1)[0] == pytest.approx(0.6)

    down = await recommend(account, _GlobalModel(fail=True), as_of=AS_OF, record=False, seed=4)
    assert down.prior_source.startswith("pooled")
    assert any("token cap reached; the pooled regression ran instead" in n for n in down.notes)


def test_a_campaign_that_stopped_spending_weeks_ago_is_not_allocated() -> None:
    """Its last settled days had spend, but not the account's last three days."""
    from datetime import datetime, time
    from decimal import Decimal

    from paid_media_agent.analytics.ingest import AnalyticsRecorder
    from paid_media_agent.bandit.arms import check_data, load_arms
    from paid_media_agent.domain.common import EntityType, Platform
    from paid_media_agent.domain.metrics import MetricWindow, PerformanceRow
    from paid_media_agent.sim.scenario import scenario_binding

    store = Store()
    run_scenario(store, PARAMS)
    binding = scenario_binding(PARAMS)
    recorder = AnalyticsRecorder(store)
    gone_until = AS_OF - timedelta(days=30)
    recorder.record_settings(
        source="simulator",
        binding=binding,
        tool_name="google_ads__list_campaigns",
        catalog_revision=None,
        payload={"campaigns": [{"id": "gone", "name": "gone", "daily_budget": 90.0}]},
        observed_at=datetime.combine(gone_until - timedelta(days=20), time(6)),
    )
    recorder.record_performance(
        source="simulator",
        binding=binding,
        tool_name="google_ads__get_campaign_performance",
        catalog_revision=None,
        rows=[
            PerformanceRow(
                platform=Platform.GOOGLE_ADS,
                account_ref=binding.alias,
                entity_type=EntityType.CAMPAIGN,
                entity_ref="gone",
                entity_name="gone",
                window=MetricWindow(start=day, end=day, timezone="UTC", is_complete=True),
                currency="USD",
                spend=Decimal("80"),
                conversions=Decimal("2"),
            )
            for day in (gone_until - timedelta(days=k) for k in range(20))
        ],
        pulled_at=datetime.combine(gone_until + timedelta(days=10), time(6)),
    )
    arms = {a.entity_ref: a for a in load_arms(store, as_of=AS_OF)}
    assert arms["gone"].n_history == 20 and not arms["gone"].eligible
    assert "last three complete days" in (arms["gone"].reason or "")
    assert check_data(list(arms.values()), AS_OF).passed


def test_implausible_pseudo_samples_cannot_inflate_the_noise_estimate() -> None:
    """A new campaign whose global-model curve is convex (seen from TabPFN on 4 days of history)."""
    spend = np.array([80.0, 95.0, 70.0, 88.0])
    conversions = np.array([1.0, 2.0, 1.0, 1.0])
    unit = float(spend.sum() / conversions.sum())
    grid = np.linspace(69, 115, 64)
    steep = 0.4 + 3.17 * np.log(grid / unit + 1) - 3.17 * math.log(69 / unit + 1)
    post = fit_posterior(
        spend,
        conversions,
        np.arange(4),
        unit,
        pseudo_spend=grid,
        pseudo_target=steep,
        pseudo_weight=8,
    )
    assert post.precision_kappa2 > 0 and 0 < post.kappa_mean[1] < 1
    assert post.noise_variance < 1.0, "the pseudo-samples' misfit is not day-to-day noise"
    assert post.curve().value(90.0) < 10 * conversions.mean()


class _ConvexModel(_GlobalModel):
    async def predict(self, request: PredictionRequest) -> Prediction:
        units = request.x_test[:, request.columns.index("log_spend_units")]
        values = np.repeat((3.0 * units - 5.0)[None, :], len(request.quantiles), axis=0)
        return Prediction(request.quantiles, values, "tabpfn", "v3.5-test")


async def test_a_global_curve_that_is_not_concave_is_dropped(account: Store) -> None:
    run = await recommend(account, _ConvexModel(), as_of=AS_OF, record=False, seed=5)
    assert any("outside 0 to 1, for" in n and "their own history only" in n for n in run.notes)
    assert all(d.posterior.n_pseudo == 0 for d in run.decisions if d.posterior is not None)


async def test_a_target_cpa_cuts_the_total_until_the_expected_cpa_meets_it(account: Store) -> None:
    greedy = BanditConfig(policy="greedy")
    free = await recommend(account, None, as_of=AS_OF, config=greedy, mode="simulate", seed=5)
    assert free.expected_cpa is not None and free.target_cpa_reached is None
    assert not free.capped_by_target_cpa and free.total_source == "current"

    generous = await recommend(
        account,
        None,
        as_of=AS_OF,
        config=BanditConfig(policy="greedy", target_cpa=free.expected_cpa * 2),
        mode="simulate",
        seed=5,
    )
    assert generous.budgets == free.budgets and generous.target_cpa_reached is True

    impossible = await recommend(
        account,
        None,
        as_of=AS_OF,
        config=BanditConfig(policy="greedy", target_cpa=0.01),
        mode="simulate",
        seed=5,
    )
    assert impossible.capped_by_target_cpa and impossible.target_cpa_reached is False
    movable = [d for d in impossible.decisions if d.lower != d.upper and d.arm.eligible]
    assert all(d.final_budget == pytest.approx(d.lower) for d in movable)
    floor_cpa = impossible.expected_cpa
    assert floor_cpa is not None and floor_cpa < free.expected_cpa

    target = (free.expected_cpa + floor_cpa) / 2  # reachable inside the step limits
    capped = await recommend(
        account,
        None,
        as_of=AS_OF,
        config=BanditConfig(policy="greedy", target_cpa=target),
        mode="simulate",
        seed=5,
    )
    assert capped.capped_by_target_cpa and capped.target_cpa_reached
    assert impossible.total_budget < capped.total_budget < free.total_budget
    assert capped.expected_cpa == pytest.approx(target, rel=1e-3)
    assert any("target_cpa" in d.constrained_by for d in capped.decisions)
    assert any("cut by" in note for note in capped.notes)


async def test_a_monthly_budget_scales_the_current_total(account: Store) -> None:
    base = await recommend(
        account, None, as_of=AS_OF, config=BanditConfig(policy="greedy"), mode="simulate", seed=6
    )
    scaled = await recommend(
        account,
        None,
        as_of=AS_OF,
        config=BanditConfig(policy="greedy"),
        mode="simulate",
        seed=6,
        budget_scale=0.9,
    )
    assert scaled.total_source == "monthly_budget"
    assert scaled.total_budget == pytest.approx(base.total_budget * 0.9)
    assert sum(scaled.budgets.values()) <= sum(base.budgets.values())
    explicit = await recommend(
        account,
        None,
        as_of=AS_OF,
        config=BanditConfig(policy="greedy"),
        total_budget=base.total_budget,
        mode="simulate",
        seed=6,
        budget_scale=0.5,
    )
    assert explicit.total_source == "explicit", "an explicit total wins over the monthly scale"
