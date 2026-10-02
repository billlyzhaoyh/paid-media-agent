"""Versioned report payload. The model writes bounded narrative; code renders everything else."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from paid_media_agent.domain.common import Platform

REPORT_SCHEMA_VERSION = "report/1"

NARRATIVE_MAX = 1200


class ReportScope(BaseModel):
    model_config = ConfigDict(frozen=True)

    accounts: tuple[str, ...]
    platforms: tuple[Platform, ...]
    current_window: str
    previous_window: str
    currency: str | None
    source_coverage: str


class ScorecardRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    metric: str
    definition: str
    current: str
    previous: str
    change: str
    raw_current: str | None
    raw_previous: str | None


class PlatformSection(BaseModel):
    model_config = ConfigDict(frozen=True)

    platform: Platform
    account_ref: str
    currency: str
    rows: tuple[ScorecardRow, ...]
    drivers: tuple[str, ...]
    missing_fields: tuple[str, ...]
    quality_flags: tuple[str, ...]


class Recommendation(BaseModel):
    model_config = ConfigDict(frozen=True)

    target: str = Field(max_length=200)
    action: str = Field(max_length=400)
    evidence: str = Field(max_length=NARRATIVE_MAX)
    expected_effect: str = Field(max_length=400)
    confidence: str = Field(pattern=r"^(low|medium|high)$")
    measurement: str = Field(max_length=400)
    reversal: str = Field(max_length=400)


ResultSource = Literal["live", "cached", "recorded", "local", "rule"]
"""Where a model's figures came from: a live call, the stored result of an identical earlier call,
a recording made earlier on the same account, the local model, or the day-over-day rule when no
model ran."""


class BandDay(BaseModel):
    """One campaign-day judged against its expected range."""

    model_config = ConfigDict(frozen=True)

    day: date
    observed: float
    expected: float
    lo: float
    hi: float
    flagged: bool
    planted: str | None = None
    """On a simulated account, the anomaly that was really injected on this day."""
    note: str | None = None
    """What a reader should take from this day: 'tracking outage, caught', 'false alarm'."""


class BandSeries(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_ref: str
    entity_name: str
    metric: str
    unit: str
    days: tuple[BandDay, ...]


class MethodScore(BaseModel):
    """How one way of flagging did on the same days; caught and false alarms need known truth."""

    model_config = ConfigDict(frozen=True)

    label: str
    flagged: int
    caught: int | None = None
    false_alarms: int | None = None


class HowRow(BaseModel):
    """One row of the table a model was given, as a reader would see it."""

    model_config = ConfigDict(frozen=True)

    cells: tuple[str, ...]
    answer: str
    """The value the row carries; '?' on a row the model is asked about."""
    asked: bool = False
    predicted: str | None = None
    """What the model answered for an asked row, when it is shown beside the question."""


class RangeExample(BaseModel):
    """One judged day: the range the model returned for it and what was observed."""

    model_config = ConfigDict(frozen=True)

    entity_name: str
    day: date
    metric: str
    unit: str
    lo: float
    expected: float
    hi: float
    observed: float
    low_level: float
    """The quantile the low end is, as a percentage: 2.5 for a 95% range."""
    high_level: float
    note: str | None = None
    """What the flag turned out to be on a simulated account: 'spend spike, caught'."""


class RangeStep(BaseModel):
    """A budget change inside the checked days that the range followed, so no alert was raised."""

    model_config = ConfigDict(frozen=True)

    entity_name: str
    day: date
    unit: str
    budget_before: float
    budget_after: float
    expected_before: float
    expected_after: float


class RuleContrast(BaseModel):
    """A day the day-over-day rule flagged that the model left alone."""

    model_config = ConfigDict(frozen=True)

    entity_name: str
    day: date
    metric: str
    unit: str
    before: float
    observed: float
    change: float
    """Day-over-day change as a share: 2.0 is +200%."""
    lo: float
    hi: float
    others: int = 0
    """More days like this one in the same check."""
    planted_known: bool = False


class AnomalyHow(BaseModel):
    """How the model was applied to the unusual-days check, followed through one real day."""

    model_config = ConfigDict(frozen=True)

    model: str
    about: str
    requests: int
    history_rows: int
    judged_rows: int
    headers: tuple[str, ...]
    answer_header: str
    rows: tuple[HowRow, ...]
    other_columns: tuple[str, ...]
    example: RangeExample
    step: RangeStep | None = None
    contrast: RuleContrast | None = None


class AnomalyPanel(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    """Who drew the range, as a reader would say it: 'TabPFN, 95% expected range'."""
    headline: str = ""
    """The finding in a sentence, written from the scores."""
    hidden_series: int = 0
    """Campaign charts left out because nothing on them was flagged or planted."""
    method: str
    source: ResultSource
    windows: dict[str, str]
    series: tuple[BandSeries, ...]
    scores: tuple[MethodScore, ...] = ()
    planted: int | None = None
    notes: tuple[str, ...] = ()
    how: AnomalyHow | None = None


class BudgetCurve(BaseModel):
    """One campaign's spend response: what was seen, what the models suggest, what is advised."""

    model_config = ConfigDict(frozen=True)

    entity_ref: str
    entity_name: str
    history: tuple[tuple[float, float], ...]
    """(daily spend, conversions) for each day of history."""
    pseudo: tuple[tuple[float, float], ...] = ()
    """The global model's suggested (spend, conversions) points."""
    fitted: tuple[tuple[float, float], ...] = ()
    alternative: tuple[tuple[float, float], ...] = ()
    """The curve fitted with the other global model, for comparison."""
    truth: tuple[tuple[float, float], ...] = ()
    """The simulator's true curve, when there is one."""
    current_budget: float | None = None
    recommended_budget: float | None = None
    current_spend: float | None = None
    recommended_spend: float | None = None
    expected_now: float | None = None
    expected_recommended: float | None = None
    limited_by: str = "unknown"
    elasticity: float | None = None
    true_elasticity: float | None = None


class TrialRun(BaseModel):
    """One way of setting budgets over the same weeks of the same simulated account."""

    model_config = ConfigDict(frozen=True)

    key: Literal["static", "agent", "best"]
    label: str
    weekly: tuple[float, ...]
    """Expected conversions in each week, from the simulation's true curves."""
    per_day: float


class BudgetRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_ref: str
    entity_name: str
    start: float
    """Daily budget before the agent's first reallocation."""
    now: float
    recommended: float | None = None
    limited_by: str = "unknown"


class BudgetTrial(BaseModel):
    """What the reallocations bought, measured against the simulation's truth."""

    model_config = ConfigDict(frozen=True)

    weeks: tuple[date, ...]
    days: int
    runs: tuple[TrialRun, ...]
    gain: float
    """The agent's conversions against budgets left alone, as a share (0.06 is +6%)."""
    best_gain: float
    captured: float | None
    """Share of the available gain the agent captured; None when there was none to capture."""
    decisions: int
    rows: tuple[BudgetRow, ...]


class MarginalRow(BaseModel):
    """What a little more spend would buy in one campaign, by the fitted curve and the truth."""

    model_config = ConfigDict(frozen=True)

    entity_name: str
    start_budget: float | None = None
    now_budget: float
    start_predicted: float | None = None
    start_true: float | None = None
    now_predicted: float
    now_true: float | None = None
    moved: Literal["up", "down", "flat"] | None = None
    """Which way the budget went between the start and now."""
    next_move: Literal["up", "down", "flat"] = "flat"
    """Which way the next recommendation moves it."""


class BudgetHow(BaseModel):
    """How the global model was applied to budgets, followed through one campaign."""

    model_config = ConfigDict(frozen=True)

    model: str
    about: str
    history_rows: int
    campaigns: int
    levels: int
    """Spend levels the model was asked about, per campaign."""
    entity_name: str
    headers: tuple[str, ...]
    answer_header: str
    rows: tuple[HowRow, ...]
    tried: tuple[float, float] | None = None
    """The example campaign's lowest and highest daily spend in the days named by `tried_when`."""
    tried_when: str = ""
    reached: float | None = None
    """Its daily budget now, when that is outside what it had tried."""
    step: float
    """The extra daily spend the marginal figures are for."""
    marginals: tuple[MarginalRow, ...]
    headline: str = ""
    balance: str = ""
    """How far the budgets are from balanced, before and now."""


class BudgetPanel(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    headline: str = ""
    trial: BudgetTrial | None = None
    alternative_label: str | None = None
    prior_source: str
    source: ResultSource
    currency: str | None
    curves: tuple[BudgetCurve, ...]
    total_now: float
    total_recommended: float
    notes: tuple[str, ...] = ()
    how: BudgetHow | None = None


class ChangePanel(BaseModel):
    """A proposed change as its reviewer saw it, and what the readback found."""

    model_config = ConfigDict(frozen=True)

    account_ref: str
    summary: tuple[str, ...]
    receipt: str
    context: str = ""
    """What a reader should know about where this happened: 'sample account demo-google'."""


class ReportInsights(BaseModel):
    """Optional panels beyond the two-window comparison; each is drawn only when present."""

    model_config = ConfigDict(frozen=True)

    anomaly: AnomalyPanel | None = None
    budgets: BudgetPanel | None = None
    change: ChangePanel | None = None
    narrative_source: str | None = None
    """Who wrote the summary and next steps: 'the agent (model), recorded' or 'code'."""


class ReportProvenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    analysis_artifact_id: str
    source_artifacts: tuple[str, ...]
    analysis_version: str
    analysis_schema_version: str
    generated_at: datetime
    report_schema_version: str = REPORT_SCHEMA_VERSION


class ReportPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = REPORT_SCHEMA_VERSION
    report_id: str
    title: str = Field(max_length=160)
    scope: ReportScope
    executive_summary: str = Field(max_length=NARRATIVE_MAX)
    scorecard: tuple[ScorecardRow, ...]
    total_suppressed_reason: str | None
    platform_sections: tuple[PlatformSection, ...]
    recommendations: tuple[Recommendation, ...]
    data_quality: tuple[str, ...]
    unavailable_sources: tuple[str, ...]
    provenance: ReportProvenance
    insights: ReportInsights | None = None
    omit: tuple[Literal["charts", "scorecard", "platforms"], ...] = ()
    """Standard sections to leave out, for a page about one account's panels."""
