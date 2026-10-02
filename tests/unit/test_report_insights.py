"""Report panels: chart geometry, expected ranges, budget curves, and how they render."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from paid_media_agent.analytics.anomalies import check_anomalies
from paid_media_agent.domain.reports import BandDay, BandSeries, BudgetCurve
from paid_media_agent.predict.budget import GuardedPredictor
from paid_media_agent.predict.local import LocalPredictor
from paid_media_agent.predict.protocol import Prediction, PredictionRequest
from paid_media_agent.reports.charts import HEIGHT, WIDTH, band_chart, curve_chart, nice_ticks
from paid_media_agent.reports.insights import Truth, anomaly_panel, build_insights
from paid_media_agent.sim.scenario import run_scenario, scenario_binding
from paid_media_agent.sim.simulator import ScenarioParams, Simulator
from paid_media_agent.store import Store

PARAMS = ScenarioParams(
    scenario_id="panels", seed=8, n_campaigns=3, days=120, start=date(2026, 5, 4),
    shock_rate=0.06, cold_starts=0, emit_signals=True,
)  # fmt: skip
AS_OF = PARAMS.start + timedelta(days=PARAMS.days)
ALIAS = scenario_binding(PARAMS).alias


@pytest.fixture(scope="module")
def account() -> tuple[Store, Truth]:
    store = Store()
    run_scenario(store, PARAMS)
    sim = Simulator(PARAMS)
    truth = Truth(
        planted={(e, sim.day(i)): kind for (e, i), kind in sim.shocks.items()},
        curves={c.entity_ref: c for c in sim.campaigns},
    )
    return store, truth


class _FakeTabPFN:
    """Answers as TabPFN would be asked, without a network: a band around the naive level."""

    name = "tabpfn"

    def __init__(self) -> None:
        self.calls = 0

    async def estimate_tokens(self, request: PredictionRequest) -> int:
        return 10_000

    async def predict(self, request: PredictionRequest) -> Prediction:
        self.calls += 1
        naive = request.x_test[:, list(request.columns).index("naive")]
        values = np.vstack([naive * 0.7, naive, naive * 1.3])
        return Prediction(request.quantiles, values, self.name, "fake/1")


def _series(values: list[float], flagged: int | None = None) -> BandSeries:
    days = tuple(
        BandDay(
            day=date(2026, 8, 1) + timedelta(days=i),
            observed=v,
            expected=v,
            lo=v * 0.8,
            hi=v * 1.2,
            flagged=i == flagged,
            planted="spend_spike" if i == flagged else None,
        )  # fmt: skip
        for i, v in enumerate(values)
    )
    return BandSeries(entity_ref="c", entity_name="C", metric="spend", unit="USD", days=days)


def test_charts_stay_inside_their_frame_and_label_real_values() -> None:
    assert nice_ticks(0, 950) == [0, 250, 500, 750]
    assert nice_ticks(3.2, 3.2) == [3.2] and nice_ticks(0.1, 0.9, 4) == [0.2, 0.4, 0.6, 0.8]
    chart = band_chart(_series([100, 120, 480, 110, 105], flagged=2))
    xs = [m.x for m in chart.points]
    ys = [m.y for m in chart.points]
    assert min(xs) >= chart.frame.left and max(xs) <= chart.frame.right
    assert min(ys) >= chart.frame.top and max(ys) <= chart.frame.bottom
    assert chart.frame.width == WIDTH and chart.frame.height == HEIGHT
    (flag,) = chart.flags
    assert flag.y == min(ys) and "flagged" in flag.title and "planted spend spike" in flag.title
    assert len(chart.planted) == 1 and chart.area.startswith("M") and chart.area.endswith("Z")
    assert all(t.label.replace(",", "").isdigit() for t in chart.frame.y_ticks)
    # One day and a flat series place a point without dividing by zero.
    assert len(band_chart(_series([50.0])).points) == 1
    assert band_chart(_series([0.0, 0.0, 0.0])).observed
    curve = BudgetCurve(
        entity_ref="c", entity_name="C",
        history=((100.0, 4.0), (140.0, 6.0)), pseudo=((120.0, 5.0),),
        fitted=((80.0, 3.0), (200.0, 8.0)), truth=((80.0, 3.5), (200.0, 7.5)),
        current_budget=130.0, recommended_budget=160.0, current_spend=120.0,
        recommended_spend=150.0, expected_now=5.0, expected_recommended=6.2,
    )  # fmt: skip
    drawn = curve_chart(curve, "USD")
    assert drawn.current and drawn.recommended and drawn.recommended.x > drawn.current.x
    assert "Recommended budget 160 USD" in drawn.recommended.title
    assert drawn.fitted and drawn.truth and not drawn.alternative


async def test_every_judged_day_is_returned_with_its_range_and_flags_agree(
    account: tuple[Store, Truth],
) -> None:
    store, truth = account
    report = await check_anomalies(
        store, LocalPredictor(), as_of=AS_OF, window_days=14, account_alias=ALIAS, record=False
    )
    assert report.rows_checked["spend"] == 3 * 14 == sum(b.metric == "spend" for b in report.bands)
    flagged = {(b.entity_ref, b.day, b.metric) for b in report.bands if b.flagged}
    assert flagged == {(f.entity_ref, f.day, f.metric) for f in report.flags}
    assert "bands" not in report.as_json(), "the stored and returned check is unchanged"

    panel = await anomaly_panel(
        store, LocalPredictor(), account_alias=ALIAS, as_of=AS_OF, truth=truth, currency="USD"
    )
    assert panel is not None and panel.source == "local" and len(panel.series) == 6
    assert panel.label == "Local model, 95% expected range" and panel.planted == 4
    local, rule = panel.scores
    assert (local.flagged, local.caught) == (len({(e, d) for e, d, _ in flagged}), 3)
    assert rule.label.endswith("day-over-day rule") and rule.false_alarms > local.false_alarms  # type: ignore[operator]
    # A planted anomaly is drawn on the metric it moves: an outage on conversions, not spend.
    for series in panel.series:
        for day in series.days:
            if day.planted == "tracking_outage":
                assert series.metric == "conversions"


async def test_a_tabpfn_answer_is_labelled_and_replayed_from_the_cache(
    account: tuple[Store, Truth],
) -> None:
    store, truth = account
    fake = _FakeTabPFN()
    predictor = GuardedPredictor(fake, store, daily_tokens=1_000_000, monthly_tokens=5_000_000)
    first = await anomaly_panel(store, predictor, account_alias=ALIAS, as_of=AS_OF, truth=truth)
    assert first is not None and first.method == "tabpfn_band95" and first.source == "live"
    assert first.label == "TabPFN, 95% expected range" and fake.calls == 2
    assert [s.label for s in first.scores] == [
        "TabPFN, 95% expected range",
        "Local model, 95% expected range",
        "±50% day-over-day rule",
    ]
    again = await anomaly_panel(store, predictor, account_alias=ALIAS, as_of=AS_OF, truth=truth)
    assert again is not None and again.source == "cached" and fake.calls == 2, "no second call"
    assert again.series == first.series


async def test_budget_curves_carry_the_fit_the_truth_and_the_advice(
    account: tuple[Store, Truth],
) -> None:
    store, truth = account
    insights = await build_insights(
        store, LocalPredictor(), account_alias=ALIAS, as_of=AS_OF, truth=truth, currency="USD"
    )
    budgets = insights.budgets
    assert budgets is not None and budgets.source == "local" and len(budgets.curves) == 3
    assert budgets.label == "Pooled regression as the global model"
    assert budgets.alternative_label is None, "only a TabPFN fit is compared with the default"
    for curve in budgets.curves:
        assert curve.history and curve.pseudo and curve.fitted and curve.truth
        assert curve.limited_by == "budget" and curve.true_elasticity is not None
        assert curve.current_budget and curve.recommended_budget
        step = curve.recommended_budget / curve.current_budget - 1
        assert abs(step) <= 0.25 + 1e-9, "moves stay inside the per-change limit"
    assert budgets.total_now == pytest.approx(sum(c.current_budget or 0 for c in budgets.curves))


def test_the_report_draws_the_panels_only_when_it_has_them(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from paid_media_agent.domain.reports import (
        AnomalyPanel,
        BudgetPanel,
        ChangePanel,
        ReportInsights,
        ReportPayload,
        ReportProvenance,
        ReportScope,
    )  # fmt: skip
    from paid_media_agent.reports.render import ReportRenderer

    payload = ReportPayload(
        report_id="rpt_test", title="T",
        scope=ReportScope(accounts=("a",), platforms=(), current_window="2026-08-01..2026-08-07",
                          previous_window="2026-07-25..2026-07-31", currency="USD",
                          source_coverage="1 of 1"),
        executive_summary="S", scorecard=(), total_suppressed_reason=None, platform_sections=(),
        recommendations=(), data_quality=(), unavailable_sources=(),
        provenance=ReportProvenance(analysis_artifact_id="art_1", source_artifacts=(),
                                    analysis_version="v", analysis_schema_version="s",
                                    generated_at=datetime(2026, 8, 8, tzinfo=UTC)),
    )  # fmt: skip
    renderer = ReportRenderer(tmp_path)
    plain = renderer.render_html(payload)
    assert "Expected range and anomalies" not in plain and "<polyline" not in plain
    insights = ReportInsights(
        anomaly=AnomalyPanel(
            label="TabPFN, 95% expected range", method="tabpfn_band95", source="cached",
            windows={"spend": "2026-08-01..2026-08-05"},
            series=(_series([100, 120, 480, 110, 105], flagged=2),), planted=1,
        ),
        budgets=BudgetPanel(
            label="TabPFN as the global model", prior_source="tabpfn:v", source="cached",
            currency="USD", total_now=130.0, total_recommended=160.0,
            curves=(BudgetCurve(entity_ref="c", entity_name="<b>C</b>",
                                history=((100.0, 4.0),), fitted=((80.0, 3.0), (200.0, 8.0)),
                                current_budget=130.0, recommended_budget=160.0,
                                current_spend=120.0, recommended_spend=150.0,
                                expected_now=5.0, expected_recommended=6.2),),
        ),
        change=ChangePanel(account_ref="a", summary=("Proposed change:", "- x: 1 -> 2"),
                           receipt="status=verified"),
    )  # fmt: skip
    html = renderer.render_html(payload.model_copy(update={"insights": insights}))
    for heading in ("Expected range and anomalies", "Budget response and recommendation",
                    "A change, approved and verified"):  # fmt: skip
        assert heading in html
    assert (
        "from the stored cache" in html
        and "stored result of an identical earlier call" in html
        and "Planted in the simulation" in html
    )
    assert "&lt;b&gt;C&lt;/b&gt;" in html and "<b>C</b>" not in html, "names are escaped"
    assert html.count("<polyline") >= 3 and "status=verified" in html
