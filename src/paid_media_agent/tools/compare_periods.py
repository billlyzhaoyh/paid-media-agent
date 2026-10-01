"""Model-facing `compare_periods` tool: loads row artifacts and runs deterministic compute."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from paid_media_agent.analytics.goals import GoalLookup
from paid_media_agent.domain.analysis import ANALYSIS_SCHEMA_VERSION, PlatformComparison
from paid_media_agent.domain.common import DataQualityFlag, EntityType, Platform
from paid_media_agent.domain.metrics import MetricWindow, PerformanceRow
from paid_media_agent.domain.windows import (
    PRESET_HELP,
    Span,
    WindowPreset,
    resolve_preset,
    span_text,
)
from paid_media_agent.harness.tools import FailedRead, ToolContext, ToolSpec, parameters_for
from paid_media_agent.redaction import sanitize_exception
from paid_media_agent.tools.artifacts import ArtifactError, ArtifactStore
from paid_media_agent.tools.compute import (
    ComputeError,
    compare_periods,
    compare_platform,
    summarize,
)
from paid_media_agent.tools.goal_check import against_goals
from paid_media_agent.tools.normalize import NormalizationError
from paid_media_agent.tools.performance import (
    before_the_data,
    load_reads,
    require_days,
    uncovered,
)
from paid_media_agent.tools.summary import cross_platform_caveats

COMPARE_PERIODS_TOOL = "compare_periods"


class ComparePeriodsArgs(BaseModel):
    artifact_ids: list[str] = Field(
        description=(
            "performance_rows artifact ids returned by platform reads; several for one account "
            "(pages, or one call per day) are merged."
        )
    )
    window: WindowPreset | None = Field(default=None, description=PRESET_HELP)
    days: int = Field(default=7, ge=1, le=90, description="For last_n_days_of_data.")
    current_start: date | None = None
    current_end: date | None = None
    previous_start: date | None = None
    previous_end: date | None = None
    entity_type: EntityType = EntityType.CAMPAIGN


def data_through(rows_by_artifact: list[list[PerformanceRow]]) -> date:
    """The newest day every read covers: the end of 'the last N days of data' for all of them."""
    ends = []
    for rows in rows_by_artifact:
        complete = [r.window.start for r in rows if r.window.is_complete]
        ends.append(max(complete or [r.window.start for r in rows]))
    return min(ends)


def resolve_comparison(
    args: ComparePeriodsArgs, loaded: list[list[PerformanceRow]], today: date
) -> tuple[Span, Span, str]:
    """The two windows, from a preset or the dates given, and how they were chosen."""
    if args.window is not None:
        if args.current_start is not None or args.previous_start is not None:
            raise ComputeError("give a window preset or dates, not both")
        through = data_through(loaded)
        resolved = resolve_preset(args.window, today=today, data_through=through, days=args.days)
        rule = f"{args.window}, data through {through.isoformat()}"
        if resolved.notes:
            rule += f"; {resolved.notes}"
        return resolved.current, resolved.previous, rule
    if args.current_start is None or args.current_end is None:
        raise ComputeError("give a window preset (e.g. last_week) or current_start and current_end")
    current = (args.current_start, args.current_end)
    length = (current[1] - current[0]).days + 1
    if args.previous_start is None or args.previous_end is None:
        previous = (current[0] - timedelta(days=length), current[0] - timedelta(days=1))
        return current, previous, "dates given; the previous window is the same days before"
    return current, (args.previous_start, args.previous_end), "dates given"


def run_compare_periods(
    artifacts: ArtifactStore,
    args: ComparePeriodsArgs,
    *,
    goals: GoalLookup | None = None,
    today: date | None = None,
    unavailable: Sequence[str] = (),
    failures: Sequence[FailedRead] = (),
) -> dict[str, Any]:
    """`unavailable` names sources the host knows are missing; `failures` are this turn's failed
    reads, and those for accounts no artifact covers join them. Either suppresses the
    cross-platform total and stays visible."""
    reads = load_reads(artifacts, args.artifact_ids)
    unavailable = [*unavailable, *uncovered(reads, failures)]
    (cur_start, cur_end), (prev_start, prev_end), rule = resolve_comparison(
        args, [read.rows for read in reads], today or datetime.now(UTC).date()
    )
    if cur_end < cur_start or prev_end < prev_start:
        raise ComputeError("window end precedes start")
    current = MetricWindow(start=cur_start, end=cur_end, timezone="UTC", is_complete=True)
    previous = MetricWindow(start=prev_start, end=prev_end, timezone="UTC", is_complete=True)
    if not current.same_length(previous):
        raise ComputeError("comparison windows must have the same day count")
    platforms: list[PlatformComparison] = []
    by_account: dict[str, list[PerformanceRow]] = {}
    for read in reads:
        rows, first, latest = read.rows, read.first_day, read.last_day
        source = ", ".join(read.artifact_ids)
        if previous.end < first and read.requested_from is not None:
            # Nothing of the previous window exists: the comparison cannot be made at all, which
            # matters more than a current window that starts a day early.
            raise ComputeError(
                f"{read.platform}/{read.account}: the previous window "
                f"{previous.start.isoformat()}..{previous.end.isoformat()} is entirely before the "
                f"data, which starts {first.isoformat()}, so there is nothing to compare with. Say "
                "so plainly, and use summarize_window for the current window on its own."
            )
        for label, window in (("current", current), ("previous", previous)):
            if window.start < first:
                # The read did not cover this window; a partial total would look like a drop.
                raise before_the_data(read, label, window.start, window.end)
            if not any(window.start <= r.window.start <= window.end for r in rows):
                # An empty window is unavailable data, never zero spend.
                raise ComputeError(
                    f"{source}: no rows in the {label} window "
                    f"{window.start.isoformat()}..{window.end.isoformat()}; "
                    f"the source has data through {latest.isoformat()}"
                )
            require_days(read, window.start, min(window.end, latest))
        by_account.setdefault(read.account, []).extend(rows)
        tz = rows[0].window.timezone
        platforms.append(
            compare_platform(
                platform=Platform(read.platform),
                account_ref=read.account,
                rows=rows,
                current_window=MetricWindow(
                    start=current.start, end=current.end, timezone=tz, is_complete=True
                ),
                previous_window=MetricWindow(
                    start=previous.start, end=previous.end, timezone=tz, is_complete=True
                ),
                entity_type=args.entity_type,
                source_artifacts=read.artifact_ids,
                provider_totals=read.provider_totals,
                missing_fields=read.missing_fields,
            )
        )
    comparison = compare_periods(
        platforms=platforms,
        requested_current=current,
        requested_previous=previous,
        unavailable_sources=tuple(unavailable),
    )
    flags: set[DataQualityFlag] = set()
    for platform_comparison in platforms:
        flags.update(platform_comparison.quality_flags)
    if comparison.total_suppressed_reason:
        flags.add(DataQualityFlag.SUPPRESSED_TOTAL)
    metadata = artifacts.write_json(
        "analysis",
        comparison.model_dump(mode="json"),
        schema_version=ANALYSIS_SCHEMA_VERSION,
        row_count=sum(p.current.row_count + p.previous.row_count for p in platforms),
        entity_type=args.entity_type.value,
        requested_window=f"{current.start.isoformat()}..{current.end.isoformat()}",
        quality_flags=tuple(sorted(flags)),
        tool_name=COMPARE_PERIODS_TOOL,
    )
    summary = summarize(comparison, metadata.artifact_id).model_dump(mode="json")
    summary["resolved_windows"] = {
        "current": span_text((cur_start, cur_end)),
        "previous": span_text((prev_start, prev_end)),
        "rule": rule,
    }
    caveats = cross_platform_caveats(
        {p.platform.value for p in platforms},
        {f"{p.current_window.start}..{p.current_window.end}" for p in platforms},
    )
    if caveats:
        summary["caveats"] = caveats
    judged = against_goals(by_account, start=current.start, end=current.end, goals=goals)
    if judged:
        summary["against_goals"] = judged
    return summary


def build_compare_periods_tool(
    artifacts: ArtifactStore, goals: GoalLookup | None = None
) -> ToolSpec:
    def _run(kwargs: dict[str, Any], context: ToolContext) -> str:
        try:
            args = ComparePeriodsArgs.model_validate(kwargs)
            return json.dumps(
                run_compare_periods(artifacts, args, goals=goals, failures=context.failed_reads())
            )
        except (ComputeError, ArtifactError, NormalizationError, ValueError) as exc:
            return json.dumps({"error": True, "detail": sanitize_exception(exc)})

    return ToolSpec(
        name=COMPARE_PERIODS_TOOL,
        description=(
            "Deterministically compare a current window with a previous window of equal length across "
            "performance_rows artifacts. Returns a compact summary with an analysis artifact id. "
            "Missing metrics stay missing; a cross-platform total appears only when sources are compatible. "
            "against_goals judges the current window against each account's target CPA/ROAS. "
            "Reads that failed this turn are listed under unavailable_sources by the host."
        ),
        parameters=parameters_for(ComparePeriodsArgs),
        handler=_run,
    )
