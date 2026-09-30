"""What limits spend: the censored spend ceiling, the classifier, platform rules, and bounds."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pytest

from paid_media_agent.bandit.arms import Arm
from paid_media_agent.bandit.ceiling import CappedCurve, Ceiling, budget_limited, fit_ceiling
from paid_media_agent.bandit.constraints import Constraint, Signals, classify
from paid_media_agent.bandit.platform_rules import rules_for, strategy_family
from paid_media_agent.bandit.posterior import PowerCurve
from paid_media_agent.bandit.recommend import BanditConfig, budget_bounds
from paid_media_agent.domain.common import Platform


@pytest.mark.parametrize(("budget", "tolerance"), [(400.0, 0.03), (220.0, 0.05), (190.0, 0.10)])
def test_the_ceiling_is_recovered_through_heavy_censoring(budget: float, tolerance: float) -> None:
    rng = np.random.default_rng(1)
    demand = np.exp(rng.normal(math.log(200), 0.2, 120))
    spend = np.minimum(demand, 0.98 * budget)
    censored = np.array([budget_limited(s, budget, None) for s in spend])
    ceiling = fit_ceiling(spend, censored)
    assert ceiling is not None
    assert math.exp(ceiling.mu) == pytest.approx(200, rel=tolerance)
    assert ceiling.sigma == pytest.approx(0.2, abs=0.06)
    assert ceiling.high > ceiling.mean > math.exp(ceiling.mu)


def test_a_ceiling_needs_days_that_show_it() -> None:
    spend = np.full(30, 95.0)
    assert fit_ceiling(spend, np.ones(30, dtype=bool)) is None, "the budget always bound"
    assert fit_ceiling(spend[:4], np.zeros(4, dtype=bool)) is None, "too few observed days"
    assert budget_limited(80.0, 100.0, 0.10), "impressions lost to budget mark the day"
    assert not budget_limited(80.0, 100.0, 0.0) and budget_limited(96.0, 100.0, None)


def test_a_capped_curve_buys_nothing_above_its_ceiling() -> None:
    curve = CappedCurve(PowerCurve(0.0, 0.5, 1.0), cap=100.0)
    assert curve.value(400.0) == curve.value(100.0) == pytest.approx(math.sqrt(101) - 1, rel=0.05)
    assert curve.marginal(150.0) == 0.0 and curve.marginal(50.0) > 0
    assert curve.spend_at_marginal(1e-6) == 100.0


GOOGLE = rules_for(Platform.GOOGLE_ADS)


@pytest.mark.parametrize(
    ("signals", "strategy", "utilisation", "kind", "confidence"),
    [
        (Signals(bidding_status="LEARNING_BUDGET_CHANGE"), None, 1.0, "learning", "high"),
        (Signals(status_reasons=("BUDGET_CONSTRAINED",)), None, 0.5, "budget", "high"),
        (Signals(budget_lost_share=0.18, rank_lost_share=0.1), None, None, "budget", "high"),
        (Signals(status_reasons=("SEARCH_VOLUME_LIMITED",)), "TARGET_CPA", 1.0, "demand", "high"),
        (Signals(status_reasons=("BIDDING_STRATEGY_CONSTRAINED",)), None, None, "target", "high"),
        (Signals(budget_lost_share=0.0, rank_lost_share=0.45), "TARGET_CPA", 0.9, "target", "high"),
        (Signals(), "MAXIMIZE_CONVERSIONS", 0.99, "budget", "medium"),
        (Signals(), "TARGET_ROAS", 0.6, "target", "medium"),
        (Signals(), "MAXIMIZE_CONVERSIONS", 0.6, "demand", "medium"),
        (Signals(), None, 0.9, "unknown", "low"),
        (Signals(), None, None, "unknown", "low"),
    ],
)
def test_platform_signals_decide_before_spend_history(
    signals: Signals, strategy: str | None, utilisation: float | None, kind: str, confidence: str
) -> None:
    found = classify(
        rules=GOOGLE, signals=signals, bid_strategy=strategy, utilisation=utilisation, days=30
    )
    assert (found.kind, found.confidence) == (kind, confidence)
    assert found.evidence, "every classification says why"


def test_spending_the_budget_by_design_is_named_as_such() -> None:
    found = classify(
        rules=rules_for(Platform.META_ADS),
        signals=Signals(),
        bid_strategy="LOWEST_COST_WITHOUT_CAP",
        utilisation=1.02,
        days=30,
    )
    assert found.kind == "budget" and "by design" in " ".join(found.evidence)


def test_platform_rules_come_with_their_source() -> None:
    demand_gen = rules_for("google_ads", "demand_gen")
    assert demand_gen.max_step == 0.15 and demand_gen.verified and "16797388" in demand_gen.source
    assert rules_for(Platform.GOOGLE_ADS, "SEARCH") is GOOGLE, "falls back to the platform"
    assert strategy_family(rules_for("meta_ads"), "cost_cap") == "target"
    assert strategy_family(GOOGLE, "MAXIMIZE_CONVERSIONS") == "spend_everything"
    assert strategy_family(GOOGLE, "SOMETHING_NEW") is None
    assert "30.4x in a month" in (GOOGLE.pacing_note() or "")


def _arm(**fields: object) -> Arm:
    arm = Arm(
        platform="google_ads",
        provider_account_id="1",
        account_alias="acme",
        entity_ref="c1",
        entity_name="c1",
        currency="USD",
        current_budget=100.0,
        days=[date(2026, 9, 1)],
        spend=np.array([80.0]),
        rules=GOOGLE,
    )
    for key, value in fields.items():
        setattr(arm, key, value)
    return arm


def test_bounds_follow_the_ceiling_the_platform_and_learning() -> None:
    config = BanditConfig()
    capped = _arm(
        ceiling=Ceiling(mu=math.log(80), sigma=0.1, observed=20, censored=0),
        constraint=Constraint("demand", "high", ("platform: SEARCH_VOLUME_LIMITED",)),
        capped=True,
        rho=0.95,
    )
    lower, upper, why = budget_bounds(capped, config)
    assert upper == pytest.approx(capped.ceiling.high / 0.95) and upper < 100.0
    assert why == ["demand_ceiling"] and lower == 75.0 < upper
    low_ceiling = _arm(
        ceiling=Ceiling(mu=math.log(40), sigma=0.1, observed=20, censored=0),
        constraint=capped.constraint,
        capped=True,
    )
    assert budget_bounds(low_ceiling, config)[:2] == (75.0, 75.0), "one step at a time"
    assert capped.expected_spend(200.0) == pytest.approx(capped.ceiling.mean)

    assert budget_bounds(
        _arm(constraint=Constraint("learning", "high", ("LEARNING_NEW",))), config
    ) == (100.0, 100.0, ["learning"])
    _, upper, why = budget_bounds(_arm(rules=rules_for("google_ads", "DEMAND_GEN")), config)
    assert upper == pytest.approx(115.0) and why == ["platform_step"]
    tiktok = _arm(platform="tiktok_ads", rules=rules_for("tiktok_ads"), current_budget=22.0)
    lower, _, _ = budget_bounds(tiktok, config)
    assert lower == 20.0, "TikTok's minimum ad group budget"
    tiny = _arm(platform="tiktok_ads", rules=rules_for("tiktok_ads"), current_budget=10.0)
    assert budget_bounds(tiny, config) == (10.0, 10.0, ["platform_minimum"]), "never a silent +100%"
    capped_max = budget_bounds(_arm(), BanditConfig(max_budget=50.0))
    assert capped_max == (75.0, 75.0, ["max_budget"]), "one step toward a cap below the step"
    tiktok_cap = budget_bounds(tiktok, BanditConfig(max_budget=15.0))
    assert tiktok_cap[:2] == (20.0, 20.0), "never below the platform's minimum"
    assert budget_bounds(_arm(), BanditConfig(max_budget=90.0))[:2] == (75.0, 90.0)
    held = _arm(rules=rules_for("tiktok_ads"), days_since_change=1)
    assert budget_bounds(held, BanditConfig(hold_days=0))[2] == ["hold"], "two days between changes"
