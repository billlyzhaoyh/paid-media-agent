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


class AnomalyPanel(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    """Who drew the range, as a reader would say it: 'TabPFN, 95% expected range'."""
    method: str
    source: ResultSource
    windows: dict[str, str]
    series: tuple[BandSeries, ...]
    scores: tuple[MethodScore, ...] = ()
    planted: int | None = None
    notes: tuple[str, ...] = ()


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


class BudgetPanel(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    alternative_label: str | None = None
    prior_source: str
    source: ResultSource
    currency: str | None
    curves: tuple[BudgetCurve, ...]
    total_now: float
    total_recommended: float
    notes: tuple[str, ...] = ()


class ChangePanel(BaseModel):
    """A proposed change as its reviewer saw it, and what the readback found."""

    model_config = ConfigDict(frozen=True)

    account_ref: str
    summary: tuple[str, ...]
    receipt: str


class ReportInsights(BaseModel):
    """Optional panels beyond the two-window comparison; each is drawn only when present."""

    model_config = ConfigDict(frozen=True)

    anomaly: AnomalyPanel | None = None
    budgets: BudgetPanel | None = None
    change: ChangePanel | None = None


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
