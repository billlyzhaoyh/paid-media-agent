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
    assert panel is not None and panel.source == "local"
    assert len(panel.series) == 4 and panel.hidden_series == 2, "only charts with something on"
    assert all(any(d.flagged or d.planted for d in s.days) for s in panel.series)
    assert panel.headline.startswith("Local model raised 5 alerts; 3 were real problems.")
    assert "day-over-day rule raised 15 for the same days, 11 of them false alarms" in (
        panel.headline
    )
    notes = {d.note for s in panel.series for d in s.days if d.note}
    assert "false alarm" in notes and any(n.endswith(", caught") for n in notes)
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
    assert "What was unusual" not in plain and "<polyline" not in plain
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
    for heading in ("What was unusual", "Budget response and recommendation",
                    "A change, approved and verified"):  # fmt: skip
        assert heading in html
    assert (
        "from the stored cache" in html
        and "stored result of an identical earlier call" in html
        and "Planted in the simulation" in html
    )
    assert "&lt;b&gt;C&lt;/b&gt;" in html and "<b>C</b>" not in html, "names are escaped"
    assert html.count("<polyline") >= 3 and "status=verified" in html


def test_alert_bars_budget_rows_and_the_trial_chart_share_one_scale_each() -> None:
    from paid_media_agent.domain.reports import BudgetRow, BudgetTrial, MethodScore, TrialRun
    from paid_media_agent.reports.charts import WIDE_WIDTH, alert_bars, budget_bars, trial_chart

    bars = alert_bars(
        [
            MethodScore(label="TabPFN", flagged=4, caught=3, false_alarms=1),
            MethodScore(label="Rule", flagged=24, caught=2, false_alarms=22),
        ]
    )
    assert [(b.caught_width, b.false_width) for b in bars] == [(12.5, 4.17), (8.33, 91.67)]
    assert alert_bars([MethodScore(label="No truth", flagged=3)]) == (), "nothing to split"
    rows = budget_bars(
        [
            BudgetRow(entity_ref="a", entity_name="A", start=400, now=100, recommended=80),
            BudgetRow(entity_ref="b", entity_name="B", start=500, now=1000),
        ]
    )
    assert (rows[0].start, rows[0].now, rows[0].recommended, rows[0].change) == (40, 10, 8, "-75%")
    assert (rows[1].now, rows[1].recommended, rows[1].change) == (100, None, "+100%")
    trial = BudgetTrial(
        weeks=tuple(date(2026, 6, 15) + timedelta(days=7 * i) for i in range(3)),
        days=21,
        runs=(
            TrialRun(key="static", label="Budgets left alone", weekly=(260, 259, 261), per_day=37.1),
            TrialRun(key="agent", label="The agent", weekly=(264, 277, 281), per_day=39.1),
            TrialRun(key="best", label="Best possible", weekly=(264, 279, 282), per_day=39.3),
        ),
        gain=0.054, best_gain=0.059, captured=0.9, decisions=3, rows=(),
    )  # fmt: skip
    chart = trial_chart(trial)
    assert chart.frame.width == WIDE_WIDTH and [line.key for line in chart.lines] == [
        "static", "agent", "best",
    ]  # fmt: skip
    ends = {line.key: line.end.y for line in chart.lines}
    assert ends["best"] < ends["agent"] < ends["static"], "more conversions are drawn higher"
    labels = sorted(label.y for label in chart.labels)
    assert all(b - a >= 14 for a, b in zip(labels, labels[1:], strict=False)), "no overprinting"
    assert all(line.end.x <= chart.frame.right for line in chart.lines)


async def test_the_trial_scores_three_runs_of_one_store_by_its_true_curves() -> None:
    from paid_media_agent.bandit.evaluate import run_closed_loop
    from paid_media_agent.bandit.recommend import BanditConfig
    from paid_media_agent.reports.insights import build_trial

    params = ScenarioParams(
        scenario_id="trial", seed=4, n_campaigns=4, days=70, start=date(2026, 5, 4),
        cold_starts=0, campaign_names=("Brand Search", "Shopping"),
    )  # fmt: skip
    runs = {
        policy: await run_closed_loop(
            params,
            policy,
            warmup_days=42,
            days=28,
            config=BanditConfig(policy="greedy"),  # type: ignore[arg-type]
        )
        for policy in ("static", "greedy", "oracle")
    }
    names = {c.entity_ref: c.name for c in Simulator(params).campaigns}
    assert names["sim-001"] == "Brand Search" and names["sim-003"] == "Simulated campaign 3"
    trial = build_trial(
        runs["static"], runs["greedy"], runs["oracle"], names=names, agent_label="The agent"
    )
    assert trial is not None and len(trial.weeks) == 4 and trial.days == 28
    assert all(len(run.weekly) == 4 for run in trial.runs) and trial.decisions == 4
    left, agent, best = (run.per_day for run in trial.runs)
    assert left <= agent <= best + 1e-6, "the best possible split bounds the agent's"
    assert trial.captured is not None and 0 <= trial.captured <= 1
    assert sum(r.start for r in trial.rows) == pytest.approx(
        sum(r.now for r in trial.rows), abs=0.1
    )

    # The same decisions, replayed without the policy, rebuild the same run.
    decisions = [budgets for _day, budgets in runs["greedy"].budgets[1:]]
    replayed = await run_closed_loop(
        params, "greedy", warmup_days=42, days=28, config=BanditConfig(policy="greedy"),
        replay=decisions,
    )  # fmt: skip
    assert replayed.expected_conversions == pytest.approx(runs["greedy"].expected_conversions)


async def test_the_model_is_followed_through_one_flagged_day(
    account: tuple[Store, Truth],
) -> None:
    from paid_media_agent.reports.charts import RANGE_WIDTH, range_bar

    store, truth = account
    report = await check_anomalies(
        store, LocalPredictor(), record=False, as_of=AS_OF, window_days=14, account_alias=ALIAS
    )
    given = report.inputs["spend"]
    assert given.columns[-1] == "budget" and len(given.quantiles) == 3
    assert len(given.judged) == report.rows_checked["spend"] and len(given.history) >= 20
    assert all(len(row.features) == len(given.columns) for row in given.history + given.judged)
    assert max(r.day for r in given.history) < min(r.day for r in given.judged)
    by_rule = await check_anomalies(
        store, None, record=False, as_of=AS_OF, window_days=14, account_alias=ALIAS
    )
    assert by_rule.inputs == {}, "the rule is given no table"

    panel = await anomaly_panel(
        store, LocalPredictor(), account_alias=ALIAS, as_of=AS_OF, truth=truth, currency="USD"
    )
    assert panel is not None and panel.how is not None
    how = panel.how
    assert how.model == "Local model" and "simple estimate" in how.about and how.requests == 2
    example = how.example
    assert example.note is not None and example.note.endswith(", caught"), "a real problem"
    assert not example.lo <= example.observed <= example.hi
    assert (example.low_level, example.high_level) == (2.5, 97.5)
    *history, judged = how.rows
    assert judged.asked and judged.answer == "?" and len(judged.cells) == len(how.headers)
    assert len(history) == 2 and not any(row.asked for row in history)
    assert {row.cells[0] for row in how.rows} == {example.entity_name}, "one campaign's rows"
    assert how.history_rows == len(report.inputs[example.metric].history)

    flagged = {(f.entity_name, f.day) for f in report.flags}
    alarms = {(f.entity_name, f.day) for f in by_rule.flags}
    names = {f.entity_ref: f.entity_name for f in by_rule.flags}
    planted = {(names.get(entity), day) for (entity, day) in truth.planted}
    contrast = how.contrast
    assert contrast is not None and (contrast.entity_name, contrast.day) in alarms - flagged
    assert (contrast.entity_name, contrast.day) not in planted and contrast.planted_known
    assert abs(contrast.change) >= 0.5 and contrast.lo <= contrast.observed <= contrast.hi
    if how.step is not None:
        assert abs(how.step.budget_after / how.step.budget_before - 1) >= 0.1
        assert (how.step.entity_name, how.step.day) not in flagged

    bar = range_bar(example)
    assert bar.lo < bar.expected < bar.hi and bar.outside
    assert 0 <= bar.observed <= RANGE_WIDTH and not bar.lo <= bar.observed <= bar.hi
    inside = range_bar(example.model_copy(update={"observed": example.expected}))
    assert not inside.outside and inside.lo < inside.observed < inside.hi

    # With no truth the example is still a flag, and nothing claims what it turned out to be.
    plain = await anomaly_panel(
        store, LocalPredictor(), account_alias=ALIAS, as_of=AS_OF, currency="USD"
    )
    assert plain is not None and plain.how is not None and plain.how.example.note is None
    assert plain.how.contrast is None or not plain.how.contrast.planted_known


async def test_the_global_model_is_followed_through_one_campaign(
    account: tuple[Store, Truth],
) -> None:
    from paid_media_agent.reports.insights import budget_panel

    store, truth = account
    panel = await budget_panel(store, None, account_alias=ALIAS, as_of=AS_OF, truth=truth)
    assert panel is not None and panel.how is not None
    how = panel.how
    assert how.model == "Pooled regression" and how.campaigns == len(how.marginals) >= 2
    assert how.history_rows > 0 and how.levels > 0 and how.step > 0
    seen = [row for row in how.rows if not row.asked]
    asked = [row for row in how.rows if row.asked]
    assert len(seen) == 2 and 1 <= len(asked) <= 2
    assert all(row.answer == "?" and row.predicted for row in asked)
    assert all(row.cells[1] == "any weekday" for row in asked)
    assert {row.cells[0] for row in how.rows} == {how.entity_name}
    for row in how.marginals:
        assert row.now_predicted > 0 and row.now_true is not None and row.now_true > 0
        assert row.start_predicted is None and row.moved is None, "no trial, so no 'before'"
    assert how.headline.startswith("Budget goes to where the next") and how.balance == ""

    plain = await budget_panel(store, None, account_alias=ALIAS, as_of=AS_OF)
    assert plain is not None and plain.how is not None
    assert all(row.now_true is None for row in plain.how.marginals), "no truth, no true column"
    assert {row.next_move for row in plain.how.marginals} <= {"up", "down", "flat"}
