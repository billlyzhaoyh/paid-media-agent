"""The visual demo: the report, on a simulated account, with expected ranges and budget curves.

A simulated account has what a demo needs and real data cannot give: known response curves and
planted anomalies, so the page can show what a model caught against what was really there. The
account is built once into its own state file and reused, with a fixed seed and date, so every
run asks the predictor the same question and a stored answer replays without a network.

TabPFN is used when its token is set or its answers are already stored; a recorded set of answers
ships with the package so a fresh clone shows them too. When neither is available the local model
runs, and the page says which one drew each panel.
"""

from __future__ import annotations

import json
import math
import webbrowser
from datetime import date, timedelta
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

import numpy as np

from paid_media_agent.config import Settings, project_root
from paid_media_agent.domain.common import EntityType, Platform
from paid_media_agent.domain.metrics import MetricWindow, PerformanceRow
from paid_media_agent.domain.presentation import ProposalView
from paid_media_agent.domain.reports import Recommendation, ReportInsights
from paid_media_agent.predict.budget import GuardedPredictor
from paid_media_agent.predict.local import LocalPredictor
from paid_media_agent.predict.protocol import (
    Prediction,
    PredictionRequest,
    Predictor,
    PredictorUnavailable,
)
from paid_media_agent.predict.tabpfn import TabPFNPredictor
from paid_media_agent.reports.insights import (
    RECORDED_MARK,
    Truth,
    build_insights,
    change_panel,
)
from paid_media_agent.sim.scenario import run_scenario, scenario_binding, scenario_path
from paid_media_agent.sim.simulator import ScenarioParams, Simulator
from paid_media_agent.store.db import Store
from paid_media_agent.testing.demo_script import run_demo
from paid_media_agent.tools.artifacts import ArtifactStore
from paid_media_agent.tools.compare_periods import ComparePeriodsArgs, run_compare_periods
from paid_media_agent.tools.normalize import ROWS_SCHEMA_VERSION, rows_to_payload
from paid_media_agent.tools.reports import RenderReportArgs, run_render_report

DEMO = ScenarioParams(
    scenario_id="demo",
    seed=8,
    n_campaigns=3,
    days=120,
    start=date(2026, 5, 4),
    shock_rate=0.06,
    cold_starts=0,
    emit_signals=True,
)
"""Fixed, so the account, its planted anomalies, and every predictor request are the same on
every run and every machine. The seed was chosen with the local model, before TabPFN saw the
account, for having several planted anomalies of different kinds in the checked days."""
AS_OF = DEMO.start + timedelta(days=DEMO.days)
WINDOW_DAYS = 14
REPORT_ID = "demo_report"
RECORDED = "demo_tabpfn_cache.json"
TABPFN = "tabpfn"


def recorded_path() -> Path:
    """The recorded TabPFN answers shipped with the package."""
    return Path(str(resources.files("paid_media_agent.fixtures.data").joinpath(RECORDED)))


def account_check(store: Store) -> dict[str, float]:
    """The simulated account in three numbers: a recording applies only to the same account."""
    rows, spend, conversions = store.fetch(
        "SELECT count(*), coalesce(sum(spend), 0), coalesce(sum(conversions), 0) "
        "FROM entity_daily_latest WHERE account_alias = ?",
        [scenario_binding(DEMO).alias],
    )[0]
    return {"rows": int(rows), "spend": float(spend), "conversions": float(conversions)}


def _same_account(recorded: dict[str, float], here: dict[str, float]) -> bool:
    return recorded.keys() == here.keys() and all(
        math.isclose(recorded[k], here[k], rel_tol=1e-9, abs_tol=1e-6) for k in here
    )


class RecordedTabPFN:
    """Answers the demo's fixed questions from TabPFN's recorded answers, with no network.

    A question is matched by its purpose and shape; the recording is used only for the account
    it was made on, so an answer is never applied to different data. Each answer's model version
    says it is recorded, and the page says so too.
    """

    name = TABPFN

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    async def estimate_tokens(self, request: PredictionRequest) -> int:
        del request  # a replay bills nothing
        return 0

    async def predict(self, request: PredictionRequest) -> Prediction:
        for call in self._calls:
            result = call["result"]
            if (
                call["purpose"] == request.purpose
                and call["n_train"] == request.n_train
                and call["n_test"] == request.n_test
                and call["n_features"] == len(request.columns)
                and [float(q) for q in result["quantiles"]] == [float(q) for q in request.quantiles]
            ):
                return Prediction(
                    quantiles=tuple(request.quantiles),
                    values=np.asarray(result["values"], dtype=np.float64),
                    provider=TABPFN,
                    model_version=f"{call['model_version']} {RECORDED_MARK}",
                )
        raise PredictorUnavailable(f"no recorded TabPFN answer for {request.purpose}")


def load_recorded(store: Store, path: Path | None = None) -> RecordedTabPFN | None:
    """The shipped recording, when it was made on the account in `store`; otherwise None."""
    source = path or recorded_path()
    if not source.exists():
        return None
    body = json.loads(source.read_text("utf-8"))
    if not _same_account(body.get("account_check", {}), account_check(store)):
        return None
    return RecordedTabPFN(body["calls"])


def export_recorded(store: Store, path: Path | None = None) -> int:
    """Write this state file's live TabPFN answers for the demo to the shipped file."""
    rows = store.fetch_dicts(
        "SELECT model_version, purpose, n_train, n_test, n_features, result "
        "FROM predictor_calls WHERE provider = ? AND status = 'ok' AND result IS NOT NULL "
        "AND model_version NOT LIKE ? "
        "QUALIFY row_number() OVER (PARTITION BY request_sha ORDER BY created_at DESC) = 1 "
        "ORDER BY purpose, n_test",
        [TABPFN, f"%{RECORDED_MARK}"],
    )
    for row in rows:
        result = json.loads(row["result"]) if isinstance(row["result"], str) else row["result"]
        # Six decimals is far below what a chart or a fit can tell apart, and a third the size.
        result["values"] = [[round(v, 6) for v in line] for line in result["values"]]
        row["result"] = result
    body = {
        "note": "TabPFN answers recorded for the visual demo's fixed simulated account. They are "
        "replayed, matched by question and shape, only when the account built here is the same "
        "one, so the demo shows TabPFN without a token.",
        "scenario": DEMO.as_json(),
        "as_of": AS_OF.isoformat(),
        "account_check": account_check(store),
        "calls": rows,
    }
    text = json.dumps(body, separators=(",", ":"))
    (path or recorded_path()).write_text(text + "\n", encoding="utf-8")
    return len(rows)


def demo_store(state_dir: Path) -> Store:
    """The simulated account's state file: built on first use, then reused with its cache."""
    path = scenario_path(state_dir, DEMO.scenario_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    wanted = json.dumps(DEMO.as_json(), sort_keys=True)
    if path.exists():
        store = Store(path)
        rows = store.fetch(
            "SELECT params FROM sim_scenarios WHERE scenario_id = ?", [DEMO.scenario_id]
        )
        if rows and json.dumps(json.loads(rows[0][0]), sort_keys=True) == wanted:
            return store
        store.close()
        for stale in (path, path.with_name(path.name + ".wal")):
            stale.unlink(missing_ok=True)
    store = Store(path)
    run_scenario(store, DEMO)
    return store


def demo_truth() -> Truth:
    sim = Simulator(DEMO)
    return Truth(
        planted={(entity, sim.day(index)): kind for (entity, index), kind in sim.shocks.items()},
        curves={c.entity_ref: c for c in sim.campaigns},
    )


def _guarded(settings: Settings, store: Store, inner: Predictor) -> Predictor:
    return GuardedPredictor(
        inner,
        store,
        daily_tokens=settings.paid_media_tabpfn_daily_tokens,
        monthly_tokens=settings.paid_media_tabpfn_monthly_tokens,
    )


def demo_predictors(settings: Settings, store: Store) -> list[Predictor]:
    """Predictors to try in order: TabPFN live or recorded, then the local model.

    With a token TabPFN is asked, and identical questions are answered from this state file's
    cache. Without one, the recording shipped with the package answers, when this account is the
    one it was made on.
    """
    token = settings.tabpfn_token.get_secret_value() if settings.tabpfn_token else ""
    local = _guarded(settings, store, LocalPredictor())
    if token:
        remote = TabPFNPredictor(token, base_url=settings.tabpfn_base_url)
        return [_guarded(settings, store, remote), local]
    recorded = load_recorded(store)
    return [_guarded(settings, store, recorded), local] if recorded is not None else [local]


def _performance_rows(store: Store, alias: str, start: date, end: date) -> list[PerformanceRow]:
    """The account's stored daily rows as performance rows, as a platform read would return."""
    found = store.fetch_dicts(
        "SELECT platform, entity_ref, entity_name, day, currency, spend, impressions, clicks, "
        "conversions, conversion_value FROM entity_daily_latest "
        "WHERE account_alias = ? AND day BETWEEN ? AND ? ORDER BY day, entity_ref",
        [alias, start, end],
    )
    return [
        PerformanceRow(
            platform=Platform(r["platform"]),
            account_ref=alias,
            entity_type=EntityType.CAMPAIGN,
            entity_ref=r["entity_ref"],
            entity_name=r["entity_name"],
            window=MetricWindow(start=r["day"], end=r["day"], timezone="UTC", is_complete=True),
            currency=r["currency"],
            spend=Decimal(str(r["spend"])),
            impressions=r["impressions"],
            clicks=r["clicks"],
            conversions=None if r["conversions"] is None else Decimal(str(r["conversions"])),
            conversion_value=None
            if r["conversion_value"] is None
            else Decimal(str(r["conversion_value"])),
        )
        for r in found
    ]


def _days(count: int) -> str:
    return f"{count} campaign-day{'' if count == 1 else 's'}"


def _summary(insights: ReportInsights, currency: str) -> str:
    """A code-written summary: only what the panels contain."""
    parts = ["A simulated account, so the true curves and the planted anomalies are known."]
    ranges, budgets = insights.anomaly, insights.budgets
    if ranges and ranges.scores:
        first = ranges.scores[0]
        text = f"{first.label} flagged {_days(first.flagged)}"
        if ranges.planted is not None:
            text += f", catching {first.caught} of {ranges.planted} planted anomalies"
        rule = next((s for s in ranges.scores if "rule" in s.label), None)
        if rule is not None and rule is not first:
            text += f"; the day-over-day rule flagged {rule.flagged}"
            if rule.false_alarms is not None and first.false_alarms is not None:
                text += f", with {rule.false_alarms} false alarms against {first.false_alarms}"
        parts.append(text + ".")
    if budgets:
        moved = [
            c for c in budgets.curves
            if c.current_budget and c.recommended_budget
            and abs(c.recommended_budget - c.current_budget) >= 1
        ]  # fmt: skip
        parts.append(
            f"The budget model recommends moving {len(moved)} of {len(budgets.curves)} "
            f"budgets, to {budgets.total_recommended:,.2f} {currency} a day in total from "
            f"{budgets.total_now:,.2f}."
        )
    parts.append("The change below was proposed, approved, and verified on a sample account.")
    return " ".join(parts)


def _recommendations(insights: ReportInsights, currency: str) -> list[Recommendation]:
    budgets = insights.budgets
    if budgets is None:
        return []
    moves = sorted(
        (c for c in budgets.curves if c.current_budget and c.recommended_budget),
        key=lambda c: -abs((c.recommended_budget or 0) - (c.current_budget or 0)),
    )
    found = []
    for curve in moves[:2]:
        now, advised = float(curve.current_budget or 0), float(curve.recommended_budget or 0)
        if abs(advised - now) < 1:
            continue
        gain = (
            f"about {curve.expected_now:.1f} to {curve.expected_recommended:.1f} conversions a day"
            if curve.expected_now is not None and curve.expected_recommended is not None
            else "see the budget curve"
        )
        found.append(
            Recommendation(
                target=curve.entity_name,
                action=f"Move the daily budget from {now:,.2f} to {advised:,.2f} {currency}.",
                evidence=(
                    f"Its fitted elasticity is {curve.elasticity:.2f}; the allocator moves budget "
                    "toward the campaigns where the next unit of spend buys the most."
                ),
                expected_effect=f"From {gain}, by the fitted curve.",
                confidence="medium",
                measurement="Compare matured conversions over the 7 days after the change with "
                "this expectation.",
                reversal=f"Restore the daily budget to {now:,.2f} {currency}.",
            )
        )
    return found


async def run_visual_demo(
    settings: Settings,
    *,
    root: Path | None = None,
    state_dir: Path | None = None,
    open_browser: bool = True,
) -> dict[str, Any]:
    """Build the simulated account, draw the report with its panels, and open it."""
    root = root or project_root()
    workspace = settings.paid_media_workspace_root
    workspace = workspace if workspace.is_absolute() else root / workspace
    base = await run_demo(settings, with_proposal=True, root=root)
    store = demo_store(state_dir or workspace / "state")
    try:
        binding = scenario_binding(DEMO)
        artifacts = ArtifactStore(workspace)
        end = AS_OF - timedelta(days=1)
        start = end - timedelta(days=2 * WINDOW_DAYS - 1)
        rows = _performance_rows(store, binding.alias, start, end)
        read = artifacts.write_json(
            "performance_rows",
            {**rows_to_payload(rows), "provider_totals": {}, "missing_fields": []},
            schema_version=ROWS_SCHEMA_VERSION,
            row_count=len(rows),
            platform=DEMO.platform.value,
            account_ref=binding.alias,
            entity_type=EntityType.CAMPAIGN.value,
            requested_window=f"{start.isoformat()}..{end.isoformat()}",
            actual_window=f"{start.isoformat()}..{end.isoformat()}",
            tool_name=f"{DEMO.platform.value}__get_campaign_performance",
        )
        analysis = run_compare_periods(
            artifacts,
            ComparePeriodsArgs(
                artifact_ids=[read.artifact_id],
                current_start=end - timedelta(days=WINDOW_DAYS - 1),
                current_end=end,
            ),
        )
        change = change_panel(
            ProposalView.model_validate(base["proposal"]), str(base["receipt_message"])
        )
        insights = None
        for predictor in demo_predictors(settings, store):
            insights = await build_insights(
                store,
                predictor,
                account_alias=binding.alias,
                as_of=AS_OF,
                window_days=WINDOW_DAYS,
                truth=demo_truth(),
                currency=DEMO.currency,
                change=change,
            )
            if insights.anomaly is not None:
                break  # a model drew the ranges; otherwise fall through to the next one
        assert insights is not None  # noqa: S101 - there is always at least one predictor
        rendered = run_render_report(
            artifacts,
            RenderReportArgs(
                analysis_artifact_id=str(analysis["artifact_id"]),
                title="Paid media report · simulated account",
                executive_summary=_summary(insights, DEMO.currency),
                recommendations=_recommendations(insights, DEMO.currency),
            ),
            insights=insights,
            report_id=REPORT_ID,
        )
        tokens = store.fetch(
            "SELECT coalesce(sum(tokens_estimated), 0) FROM predictor_calls "
            "WHERE provider = ? AND status IN ('ok', 'failed')",
            [TABPFN],
        )[0][0]
    finally:
        store.close()
    html = workspace / "out" / f"{REPORT_ID}.html"
    if open_browser:
        webbrowser.open(html.resolve().as_uri())
    ranges, budgets = insights.anomaly, insights.budgets
    return {
        "html": str(html),
        "pdf": rendered["pdf"],
        "ranges": None if ranges is None else {"label": ranges.label, "source": ranges.source},
        "budgets": None if budgets is None else {"label": budgets.label, "source": budgets.source},
        "tabpfn_tokens_billed": int(tokens),
        "receipt_message": base["receipt_message"],
    }
