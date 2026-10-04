"""The visual demo: one simulated store, replayed day by day, and the report the agent wrote.

A simulated account has what a demo needs and real data cannot give: known response curves and
planted anomalies, so the page can show what a model caught against what was really there, and
what a budget split produced against the best possible one. The store ("Northwind") runs six
weeks under its operator's budgets and then eight in which the agent works each week: it checks
the week just gone for unusual days and reallocates the budgets.

Two pages are written. The demo page (`demo.html`, `testing/demo_story.py`) replays those eight
weeks and explains the two jobs in diagrams. The report (`demo_report.html`) is the agent's own
output for the last fortnight.

Neither calls TabPFN or a model by itself: they replay a recording shipped with the package (the
weekly budget decisions TabPFN's predictions led to, its answers for the weekly checks and the
final ones, and the text the agent wrote). The recorded decisions rebuild the same store on any
machine, and the recording is used only if they did; otherwise the pooled and local models run
and the text is written by code. The pages say which. `--record` makes the whole recording and
`--record-watch` adds only the answers it lacks; both bill TabPFN tokens.
"""

from __future__ import annotations

import json
import math
import webbrowser
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

import numpy as np

from paid_media_agent.analytics.anomalies import RULE, AnomalyReport, check_anomalies
from paid_media_agent.bandit.evaluate import CampaignDay, LoopResult, run_closed_loop
from paid_media_agent.bandit.recommend import BanditConfig
from paid_media_agent.config import Settings, project_root
from paid_media_agent.domain.common import EntityType, Platform
from paid_media_agent.domain.metrics import MetricWindow, PerformanceRow
from paid_media_agent.domain.presentation import ProposalView
from paid_media_agent.domain.reports import BudgetTrial, ReportInsights
from paid_media_agent.harness.models import ChatModel
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
    build_trial,
    change_panel,
    method_label,
    result_source,
)
from paid_media_agent.sim.scenario import scenario_binding, scenario_path
from paid_media_agent.sim.simulator import ScenarioParams, Simulator
from paid_media_agent.store.db import Store, utc_now
from paid_media_agent.testing import demo_narrative
from paid_media_agent.testing.demo_narrative import Narrative, agent_narrative, template_narrative
from paid_media_agent.testing.demo_script import run_demo
from paid_media_agent.testing.demo_story import Watch, build_story, render_story
from paid_media_agent.tools.artifacts import ArtifactStore
from paid_media_agent.tools.compare_periods import ComparePeriodsArgs, run_compare_periods
from paid_media_agent.tools.normalize import ROWS_SCHEMA_VERSION, rows_to_payload
from paid_media_agent.tools.reports import RenderReportArgs, run_render_report

ACCOUNT = "Northwind"
WARMUP_DAYS = 42
LOOP_DAYS = 56
DEMO = ScenarioParams(
    scenario_id="northwind",
    seed=4,
    n_campaigns=4,
    days=WARMUP_DAYS + LOOP_DAYS,
    start=date(2026, 5, 4),
    shock_rate=0.05,
    cold_starts=0,
    emit_signals=True,
    campaign_names=("Brand Search", "Non-Brand Search", "Shopping", "Retargeting"),
)
"""Fixed, so the store, its planted anomalies, and every predictor request are the same on every
run. The seed was chosen with the pooled model, before TabPFN saw the store, for a clear gap
between budgets left alone and the best possible split and several planted anomalies."""
AS_OF = DEMO.start + timedelta(days=DEMO.days)
WINDOW_DAYS = 14
WATCH_DAYS = 7
REPORT_ID = "demo_report"
DEMO_PAGE = "demo.html"
BUILT_VERSION = 2
"""Raised when what is kept beside the state file changes, so an older file is rebuilt."""
RECORDED = "demo_tabpfn_cache.json"
TABPFN = "tabpfn"
CONFIG = BanditConfig(policy="greedy")
MEAN_ONLY = "bandit:global"
"""This purpose's answers are 19 quantiles the fit averages into one mean; the recording keeps
the mean, a twentieth of the size, and replays it as every quantile."""


def scenario() -> dict[str, Any]:
    """The demo's scenario as JSON reads it back (lists, not tuples), for comparing with files."""
    body: dict[str, Any] = json.loads(json.dumps(DEMO.as_json()))
    return body


def recorded_path() -> Path:
    """The recorded TabPFN decisions and answers shipped with the package."""
    return Path(str(resources.files("paid_media_agent.fixtures.data").joinpath(RECORDED)))


def account_check(store: Store) -> dict[str, float]:
    """The simulated store in three numbers: a recording applies only to the same store."""
    rows, spend, conversions = store.fetch(
        "SELECT count(*), coalesce(sum(spend), 0), coalesce(sum(conversions), 0) "
        "FROM entity_daily_latest WHERE account_alias = ?",
        [scenario_binding(DEMO).alias],
    )[0]
    return {"rows": int(rows), "spend": float(spend), "conversions": float(conversions)}


def same_account(recorded: dict[str, float], here: dict[str, float]) -> bool:
    return (
        bool(here)
        and recorded.keys() == here.keys()
        and all(math.isclose(recorded[k], here[k], rel_tol=1e-9, abs_tol=1e-6) for k in here)
    )


def target_key(request: PredictionRequest) -> int | None:
    """Tells apart requests of one purpose and shape: the training targets' sum in hundredths.

    The weekly checks ask the same kind of question about different weeks. Their targets are
    money (two decimals) or counts, so the sum in hundredths is a whole number on any machine.
    Other purposes have no key and are matched by shape alone."""
    if not request.purpose.startswith("anomaly:"):
        return None
    return round(100 * float(np.nansum(request.y_train)))


class RecordedTabPFN:
    """Answers the demo's fixed questions from TabPFN's recorded answers, with no network.

    A question is matched by its purpose, its shape and, where the recording has one, its target
    key. Each answer's model version says it is recorded, and the pages say so too.
    """

    name = TABPFN

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    async def estimate_tokens(self, request: PredictionRequest) -> int:
        del request  # a replay bills nothing
        return 0

    def find(self, request: PredictionRequest) -> dict[str, Any] | None:
        key = target_key(request)
        for call in self._calls:
            if (
                call["purpose"] == request.purpose
                and call["n_train"] == request.n_train
                and call["n_test"] == request.n_test
                and call["n_features"] == len(request.columns)
                and call.get("targets") in (None, key)
            ):
                return call
        return None

    async def predict(self, request: PredictionRequest) -> Prediction:
        call = self.find(request)
        if call is None:
            raise PredictorUnavailable(f"no recorded TabPFN answer for {request.purpose}")
        rows = [call["mean"]] * len(request.quantiles) if "mean" in call else call["values"]
        return Prediction(
            quantiles=tuple(request.quantiles),
            values=np.asarray(rows, dtype=np.float64),
            provider=TABPFN,
            model_version=f"{call['model_version']} {RECORDED_MARK}",
        )


class Recorder:
    """Answers from the recording where it can and from TabPFN live where it cannot, and keeps
    every answer as the shipped file stores it. Only a recording run uses it."""

    name = TABPFN

    def __init__(self, recorded: RecordedTabPFN | None, live: Predictor) -> None:
        self._recorded = recorded
        self._live = live
        self.calls: list[dict[str, Any]] = []
        self.asked_live = 0

    async def estimate_tokens(self, request: PredictionRequest) -> int:
        if self._recorded is not None and self._recorded.find(request) is not None:
            return 0
        return await self._live.estimate_tokens(request)

    async def predict(self, request: PredictionRequest) -> Prediction:
        known = self._recorded.find(request) if self._recorded is not None else None
        if known is not None and self._recorded is not None:
            prediction = await self._recorded.predict(request)
            entry = dict(known)
        else:
            prediction = await self._live.predict(request)
            self.asked_live += 1
            values = np.asarray(prediction.values, dtype=np.float64)
            entry = {
                "model_version": prediction.model_version,
                "purpose": request.purpose,
                "n_train": request.n_train,
                "n_test": request.n_test,
                "n_features": len(request.columns),
            }
            if request.purpose == MEAN_ONLY:
                entry["mean"] = [round(float(v), 6) for v in values.mean(axis=0)]
            else:
                entry["values"] = [[round(float(v), 6) for v in line] for line in values]
        key = target_key(request)
        if key is not None:
            entry["targets"] = key
        if entry not in self.calls:
            self.calls.append(entry)
        return prediction


def load_recording(path: Path | None = None) -> dict[str, Any] | None:
    source = path or recorded_path()
    if not source.exists():
        return None
    body: dict[str, Any] = json.loads(source.read_text("utf-8"))
    return body if body.get("scenario") == scenario() else None


def export_recording(
    store: Store, built: Built, calls: list[dict[str, Any]], path: Path | None = None
) -> int:
    """Write the weekly decisions and TabPFN's answers to the shipped file. Returns the answers
    written."""
    body = {
        "note": "Recorded for the visual demo's fixed simulated store: the weekly budgets the "
        "agent set with TabPFN's predictions, and TabPFN's answers for the weekly checks and "
        "the final ones. The decisions rebuild the same store anywhere; the answers are "
        "replayed only if they did.",
        "scenario": DEMO.as_json(),
        "as_of": AS_OF.isoformat(),
        "agent_label": built.agent_label,
        "decisions": [
            {"day": day.isoformat(), "budgets": budgets} for day, budgets in built.decisions
        ],
        "account_check": account_check(store),
        "calls": sorted(calls, key=lambda c: (c["purpose"], c["n_test"], c["n_train"])),
    }
    text = json.dumps(body, separators=(",", ":"))
    (path or recorded_path()).write_text(text + "\n", encoding="utf-8")
    return len(calls)


@dataclass
class Built:
    """What building the store produced, kept beside its state file for the next run."""

    trial: BudgetTrial | None
    agent_label: str
    decisions: list[tuple[date, dict[str, float]]]
    """The budgets the agent set at each weekly decision."""
    source: str
    """`live`, `recorded`, or `local`: who made the weekly predictions."""
    runs: dict[str, list[CampaignDay]] = field(default_factory=dict)
    """Every campaign-day of the three runs (`static`, `agent`, `best`), for the replay."""

    def as_json(self) -> dict[str, Any]:
        return {
            "version": BUILT_VERSION,
            "scenario": DEMO.as_json(),
            "trial": None if self.trial is None else self.trial.model_dump(mode="json"),
            "agent_label": self.agent_label,
            "decisions": [{"day": d.isoformat(), "budgets": b} for d, b in self.decisions],
            "source": self.source,
            "runs": {
                key: [{**asdict(row), "day": row.day.isoformat()} for row in rows]
                for key, rows in self.runs.items()
            },
        }

    @classmethod
    def from_json(cls, body: dict[str, Any]) -> Built:
        return cls(
            trial=None if body["trial"] is None else BudgetTrial.model_validate(body["trial"]),
            agent_label=body["agent_label"],
            decisions=[(date.fromisoformat(d["day"]), d["budgets"]) for d in body["decisions"]],
            source=body["source"],
            runs={
                key: [CampaignDay(**{**row, "day": date.fromisoformat(row["day"])}) for row in rows]
                for key, rows in body["runs"].items()
            },
        )


def _guarded(settings: Settings, store: Store, inner: Predictor) -> Predictor:
    return GuardedPredictor(
        inner,
        store,
        daily_tokens=settings.paid_media_tabpfn_daily_tokens,
        monthly_tokens=settings.paid_media_tabpfn_monthly_tokens,
    )


def _token(settings: Settings) -> str:
    return settings.tabpfn_token.get_secret_value() if settings.tabpfn_token else ""


def _paths(state_dir: Path) -> tuple[Path, Path]:
    state = scenario_path(state_dir, DEMO.scenario_id)
    return state, state.with_name(state.name + ".demo.json")


def _remove(state_dir: Path) -> None:
    state, built = _paths(state_dir)
    for stale in (state, state.with_name(state.name + ".wal"), built):
        stale.unlink(missing_ok=True)


async def _loop(
    policy: str,
    store: Store | None = None,
    predictor: Predictor | None = None,
    replay: list[dict[str, float]] | None = None,
) -> LoopResult:
    return await run_closed_loop(
        DEMO,
        policy,  # type: ignore[arg-type]
        warmup_days=WARMUP_DAYS,
        days=LOOP_DAYS,
        config=CONFIG,
        predictor=predictor,
        store=store,
        replay=replay,
    )


async def build_store(settings: Settings, state_dir: Path, *, live: bool) -> tuple[Store, Built]:
    """The store as it lived under the agent's weekly reallocations, and what they bought.

    `live` asks TabPFN for each week's predictions. Otherwise the recorded decisions are applied
    when there is a recording, and the pooled model decides when there is not.
    """
    state, built_path = _paths(state_dir)
    state.parent.mkdir(parents=True, exist_ok=True)
    recording = None if live else load_recording()
    store = Store(state)
    names = {c.entity_ref: c.name for c in Simulator(DEMO).campaigns}
    if live:
        remote = TabPFNPredictor(_token(settings), base_url=settings.tabpfn_base_url)
        agent = await _loop("greedy", store, _guarded(settings, store, remote))
        tabpfn = any(
            "tabpfn" in str(row[0]) for row in store.fetch("SELECT prior_source FROM bandit_runs")
        )
        label, source = ("The agent, with TabPFN", "live") if tabpfn else ("The agent", "local")
    elif recording is not None:
        replay = [dict(d["budgets"]) for d in recording["decisions"]]
        agent = await _loop("greedy", store, replay=replay)
        label, source = recording["agent_label"], "recorded"
        if not same_account(recording["account_check"], account_check(store)):
            # The recorded decisions did not rebuild the recorded store here: start again and
            # let the pooled model decide, rather than show answers about other data.
            store.close()
            _remove(state_dir)
            store = Store(state)
            agent = await _loop("greedy", store)
            label, source = "The agent", "local"
    else:
        agent = await _loop("greedy", store)
        label, source = "The agent", "local"
    static, best = await _loop("static"), await _loop("oracle")
    built = Built(
        trial=build_trial(static, agent, best, names=names, agent_label=label),
        agent_label=label,
        decisions=[
            (day, {k: round(v, 2) for k, v in budgets.items()})
            for day, budgets in agent.budgets[1:]
        ],
        source=source,
        runs={
            "static": static.campaign_days,
            "agent": agent.campaign_days,
            "best": best.campaign_days,
        },
    )
    built_path.write_text(json.dumps(built.as_json()), encoding="utf-8")
    return store, built


async def demo_store(
    settings: Settings, state_dir: Path, *, live: bool = False, rebuild: bool = False
) -> tuple[Store, Built]:
    """The demo's store and what building it produced: reused when it is already there."""
    state, built_path = _paths(state_dir)
    if rebuild:
        _remove(state_dir)
    if state.exists() and built_path.exists():
        body = json.loads(built_path.read_text("utf-8"))
        if body.get("scenario") == scenario() and body.get("version") == BUILT_VERSION:
            return Store(state), Built.from_json(body)
    _remove(state_dir)
    return await build_store(settings, state_dir, live=live)


def demo_truth() -> Truth:
    sim = Simulator(DEMO)
    return Truth(
        planted={(entity, sim.day(index)): kind for (entity, index), kind in sim.shocks.items()},
        curves={c.entity_ref: c for c in sim.campaigns},
    )


def demo_predictors(
    settings: Settings, store: Store, built: Built, *, recorder: Recorder | None = None
) -> list[Predictor]:
    """Predictors for the checks, in the order to try them.

    Only a recording run (`recorder`) may ask TabPFN over the network. Every other run answers
    from this state file's cache or the shipped recording, so the demo never bills tokens by
    itself, with or without a token set. The local model is always the fallback.
    """
    local = _guarded(settings, store, LocalPredictor())
    if recorder is not None:
        return [_guarded(settings, store, recorder), local]
    if built.source == "local":
        return [local]
    recording = load_recording()
    if recording is not None and same_account(recording["account_check"], account_check(store)):
        return [_guarded(settings, store, RecordedTabPFN(recording["calls"])), local]
    # No recording for this store: a tokenless TabPFN answers only what the cache already holds.
    return [_guarded(settings, store, TabPFNPredictor("")), local]


def check_days() -> list[date]:
    """The days the agent checks the week just gone: each later decision day, and the day after
    the last week."""
    first = DEMO.start + timedelta(days=WARMUP_DAYS)
    return [first + timedelta(days=WATCH_DAYS * k) for k in range(1, LOOP_DAYS // WATCH_DAYS + 1)]


async def _checks(store: Store, predictor: Predictor | None) -> list[AnomalyReport]:
    """One check per week, each reading the history as it stood on its check day."""
    alias = scenario_binding(DEMO).alias
    return [
        await check_anomalies(
            store, predictor, record=False, as_of=day, window_days=WATCH_DAYS, account_alias=alias
        )
        for day in check_days()
    ]


async def weekly_watch(store: Store, predictors: list[Predictor]) -> Watch:
    """Run the weekly checks with the first predictor that can judge every one of them."""
    since = utc_now()
    reports: list[AnomalyReport] = []
    for predictor in predictors:
        reports = await _checks(store, predictor)
        methods = {m for report in reports for m in report.methods.values()}
        if methods and RULE not in methods:
            break  # a model judged every check; a partly recorded watch is not shown
    judged_by = sorted({m for report in reports for m in report.methods.values()} - {RULE})
    provider, label = method_label(judged_by[0] if judged_by else RULE)
    local = reports if provider == "local" else await _checks(store, LocalPredictor())
    return Watch(
        label=label,
        source="rule" if provider == "rule" else result_source(store, provider, "anomaly:%", since),
        model=reports,
        local=local,
        rule=await _checks(store, None),
    )


def _performance_rows(store: Store, alias: str, start: date, end: date) -> list[PerformanceRow]:
    """The store's daily rows as performance rows, as a platform read would return them."""
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


async def _narrative(
    insights: ReportInsights, check: dict[str, float], *, model: ChatModel | None, root: Path
) -> Narrative:
    """The agent's text: written now when a model is given, else the recording for this store,
    else the code template."""
    if model is not None:
        written = await agent_narrative(model, insights, account=ACCOUNT, root=root)
        if written is not None:
            demo_narrative.save_recorded(written, check)
            return written
        return template_narrative(insights, DEMO.currency)
    recorded = demo_narrative.load_recorded()
    if recorded is not None and same_account(recorded[1], check):
        return recorded[0]
    return template_narrative(insights, DEMO.currency)


async def run_visual_demo(
    settings: Settings,
    *,
    root: Path | None = None,
    state_dir: Path | None = None,
    open_browser: bool = True,
    record: bool = False,
    record_watch: bool = False,
    narrative_model: ChatModel | None = None,
) -> dict[str, Any]:
    """Build the simulated store, write the demo page and the agent's report, and open the demo.

    `record` rebuilds the store with TabPFN live, has `narrative_model` write the text, and
    rewrites both shipped recordings. `record_watch` keeps the recorded decisions and answers
    and asks TabPFN only for the answers the recording lacks.
    """
    root = root or project_root()
    workspace = settings.paid_media_workspace_root
    workspace = workspace if workspace.is_absolute() else root / workspace
    state_dir = state_dir or workspace / "state"
    base = await run_demo(settings, with_proposal=True, root=root)
    recording_run = record or record_watch
    if recording_run and not _token(settings):
        raise ValueError("recording needs TABPFN_TOKEN")
    store, built = await demo_store(settings, state_dir, live=record, rebuild=recording_run)
    recorder = None
    if recording_run:
        earlier = load_recording() if record_watch else None
        if record_watch and (earlier is None or built.source != "recorded"):
            store.close()
            raise ValueError("--record-watch adds to an existing recording; use --record first")
        recorder = Recorder(
            RecordedTabPFN(earlier["calls"]) if earlier is not None else None,
            TabPFNPredictor(_token(settings), base_url=settings.tabpfn_base_url),
        )
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
        ).model_copy(update={"context": f"sample account {base['proposal']['account_ref']}"})
        insights = None
        predictors = demo_predictors(settings, store, built, recorder=recorder)
        for predictor in predictors:
            insights = await build_insights(
                store,
                predictor,
                account_alias=binding.alias,
                as_of=AS_OF,
                window_days=WINDOW_DAYS,
                truth=demo_truth(),
                currency=DEMO.currency,
                change=change,
                trial=built.trial,
                config=CONFIG,
            )
            if insights.anomaly is not None:
                break  # a model drew the ranges; otherwise fall through to the next one
        assert insights is not None  # noqa: S101 - there is always at least one predictor
        watch = await weekly_watch(store, predictors)
        check = account_check(store)
        narrative = await _narrative(
            insights, check, model=narrative_model if record else None, root=root
        )
        insights = insights.model_copy(update={"narrative_source": narrative.source})
        rendered = run_render_report(
            artifacts,
            RenderReportArgs(
                analysis_artifact_id=str(analysis["artifact_id"]),
                title=f"{ACCOUNT} · paid media review (simulated store)",
                executive_summary=narrative.summary,
                recommendations=narrative.recommendations,
            ),
            insights=insights,
            report_id=REPORT_ID,
            omit=("charts", "scorecard", "platforms"),
        )
        story = build_story(
            runs=built.runs,
            trial=built.trial,
            agent_label=built.agent_label,
            weekly_source=built.source,
            check_days=check_days(),
            watch=watch,
            truth=demo_truth(),
            insights=insights,
            narrative=narrative,
            report_href=f"{REPORT_ID}.html",
            account=ACCOUNT,
            currency=DEMO.currency,
        )
        out = workspace / "out"
        out.mkdir(parents=True, exist_ok=True)
        (out / DEMO_PAGE).write_text(render_story(story), encoding="utf-8")
        recorded_calls = None
        if recorder is not None:
            recorded_calls = export_recording(store, built, recorder.calls)
        tokens = store.fetch(
            "SELECT coalesce(sum(tokens_estimated), 0) FROM predictor_calls "
            "WHERE provider = ? AND status IN ('ok', 'failed')",
            [TABPFN],
        )[0][0]
    finally:
        store.close()
    html = workspace / "out" / f"{REPORT_ID}.html"
    page = workspace / "out" / DEMO_PAGE
    if open_browser:
        webbrowser.open(page.resolve().as_uri())
    ranges, budgets = insights.anomaly, insights.budgets
    trial = budgets.trial if budgets else None
    return {
        "demo": str(page),
        "watch": {"label": watch.label, "source": watch.source, "scores": story["scores"]},
        "asked_live": None if recorder is None else recorder.asked_live,
        "html": str(html),
        "pdf": rendered["pdf"],
        "ranges": None if ranges is None else {"label": ranges.label, "source": ranges.source},
        "budgets": None if budgets is None else {"label": budgets.label, "source": budgets.source},
        "weekly_predictions": built.source,
        "trial": None
        if trial is None
        else {
            "conversions_a_day": {run.label: run.per_day for run in trial.runs},
            "gain": trial.gain,
            "best_gain": trial.best_gain,
            "captured": trial.captured,
        },
        "narrative": narrative.source,
        "tabpfn_tokens_billed": int(tokens),
        "recorded_calls": recorded_calls,
        "receipt_message": base["receipt_message"],
    }
