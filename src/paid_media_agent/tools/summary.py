"""Model-facing `summarize_window` tool: one window, per-entity totals, daily series, and pacing.

`compare_periods` answers "what changed between two windows". This answers "what happened in one
window": which entities carried the spend, how each day moved, and whether spend ran ahead of the
configured daily budget. All arithmetic is here so the model never derives a figure in prose.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from paid_media_agent.analytics.goals import GoalLookup
from paid_media_agent.bandit.platform_rules import rules_for
from paid_media_agent.domain.analysis import verdict
from paid_media_agent.domain.common import JsonValue
from paid_media_agent.domain.metrics import PerformanceRow
from paid_media_agent.domain.windows import PRESET_HELP, WindowPreset, resolve_preset
from paid_media_agent.harness.tools import ToolContext, ToolSpec, parameters_for
from paid_media_agent.redaction import sanitize_exception
from paid_media_agent.tools.artifacts import ArtifactError, ArtifactStore
from paid_media_agent.tools.compute import ComputeError, aggregate
from paid_media_agent.tools.goal_check import against_goals
from paid_media_agent.tools.normalize import NormalizationError, rows_from_payload

SUMMARIZE_WINDOW_TOOL = "summarize_window"
SUMMARY_SCHEMA_VERSION = "window-summary/2"
_MONEY = Decimal("0.01")
_RATIO = Decimal("0.0001")


class SummarizeWindowArgs(BaseModel):
    artifact_ids: list[str] = Field(
        description="performance_rows artifact ids, one per platform and account."
    )
    window: WindowPreset | None = Field(
        default=None,
        description=PRESET_HELP.replace(
            " The previous window is the same number of days immediately before.", ""
        ),
    )
    days: int = Field(default=7, ge=1, le=90, description="For last_n_days_of_data.")
    start_date: date | None = None
    end_date: date | None = None
    budgets_artifact_ids: list[str] = Field(
        default_factory=list,
        description="provider_result artifacts from list_campaigns; supplies daily budgets for pacing.",
    )


def _money(value: Decimal) -> str:
    return str(value.quantize(_MONEY, rounding=ROUND_HALF_UP))


def _ratio_str(numerator: Decimal | int | None, denominator: Decimal | int | None) -> str | None:
    if numerator is None or not denominator:
        return None
    return str((Decimal(numerator) / Decimal(denominator)).quantize(_RATIO, rounding=ROUND_HALF_UP))


def _change(current: Decimal | None, previous: Decimal | None) -> Decimal | None:
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / previous


def budgets_from_payload(payload: dict[str, JsonValue]) -> dict[str, Decimal]:
    """Daily budgets in account currency, keyed by entity id, from a campaign listing.

    The contract's normalised `settings` come first: a live provider's own `result` holds
    Google micros or Meta minor units. Only a host fixture's result is already in currency.
    """
    settings = payload.get("settings")
    result = payload.get("result")
    source = settings if isinstance(settings, dict) else result
    campaigns = source.get("campaigns") if isinstance(source, dict) else None
    budgets: dict[str, Decimal] = {}
    for campaign in campaigns or []:
        if isinstance(campaign, dict) and campaign.get("daily_budget") not in (None, ""):
            budgets[str(campaign.get("id"))] = Decimal(str(campaign["daily_budget"]))
    return budgets


def cross_platform_caveats(platforms: set[str], windows: set[str]) -> list[str]:
    """What a reader must hear before comparing platforms or adding them up."""
    caveats: list[str] = []
    if len(platforms) > 1:
        caveats.append(
            "Each platform counts conversions with its own attribution; they are not "
            "deduplicated, so platform conversions and CPAs are not comparable one to one and "
            "do not add up to a total."
        )
    if len(windows) > 1:
        caveats.append(
            "Platforms cover different days (" + ", ".join(sorted(windows)) + "); name the "
            "days each is missing."
        )
    return caveats


def summarize_rows(
    rows: list[PerformanceRow],
    *,
    start: date,
    end: date,
    budgets: dict[str, Decimal],
) -> dict[str, Any]:
    selected = [r for r in rows if start <= r.window.start <= end]
    if not selected:
        raise ComputeError("no rows fall inside the window")
    days = sorted({r.window.start for r in selected})
    total = aggregate(selected)
    entities: list[dict[str, Any]] = []
    for ref in sorted({r.entity_ref for r in selected}):
        own = [r for r in selected if r.entity_ref == ref]
        metrics = aggregate(own)
        active_days = len({r.window.start for r in own})
        average_daily = metrics.spend / active_days
        budget = budgets.get(ref)
        by_day: dict[date, Decimal] = {}
        for r in own:
            by_day[r.window.start] = by_day.get(r.window.start, Decimal(0)) + r.spend
        worst = max(by_day, key=lambda d: by_day[d])
        over = [d for d, spent in by_day.items() if budget and spent > budget]
        entities.append(
            {
                "entity_ref": ref,
                "entity_name": own[0].entity_name,
                "spend": _money(metrics.spend),
                "share_of_spend": _ratio_str(metrics.spend, total.spend),
                "conversions": None if metrics.conversions is None else str(metrics.conversions),
                "cpa": None if metrics.cpa is None else str(metrics.cpa),
                "roas": None if metrics.roas is None else str(metrics.roas),
                "ctr": None if metrics.ctr is None else str(metrics.ctr),
                "active_days": active_days,
                "average_daily_spend": _money(average_daily),
                "daily_budget": None if budget is None else _money(budget),
                "pacing": _ratio_str(average_daily, budget),
                # Day by day, not on average: an average under budget can hide days over it.
                "days_over_budget": None if not budget else len(over),
                "highest_day": {
                    "date": worst.isoformat(),
                    "spend": _money(by_day[worst]),
                    "to_budget": _ratio_str(by_day[worst], budget),
                },
            }
        )
    entities.sort(key=lambda e: Decimal(e["spend"]), reverse=True)
    daily: list[dict[str, Any]] = []
    previous: dict[str, Decimal | None] | None = None
    for day in days:
        day_metrics = aggregate([r for r in selected if r.window.start == day])
        point = {"spend": day_metrics.spend, "conversions": day_metrics.conversions}
        entry: dict[str, Any] = {
            "date": day.isoformat(),
            "spend": _money(day_metrics.spend),
            "conversions": None if point["conversions"] is None else str(point["conversions"]),
        }
        if previous is not None:
            for metric in ("spend", "conversions"):
                change = _change(point[metric], previous[metric])
                entry[f"{metric}_change"] = None if change is None else _ratio_str(change, 1)
        daily.append(entry)
        previous = point
    return {
        "requested_window": f"{start.isoformat()}..{end.isoformat()}",
        "covered_window": f"{days[0].isoformat()}..{days[-1].isoformat()}",
        "days_covered": len(days),
        "currency": selected[0].currency,
        "totals": {
            "spend": _money(total.spend),
            "impressions": total.impressions,
            "clicks": total.clicks,
            "conversions": None if total.conversions is None else str(total.conversions),
            "cpa": None if total.cpa is None else str(total.cpa),
            "roas": None if total.roas is None else str(total.roas),
            "ctr": None if total.ctr is None else str(total.ctr),
        },
        "entities": entities,
        "daily": daily,
        "over_budget": [
            e["entity_ref"] for e in entities if e["pacing"] and Decimal(e["pacing"]) > 1
        ],
        "over_budget_days": [
            f"{e['entity_name']} [{e['entity_ref']}]: {e['days_over_budget']} of "
            f"{e['active_days']} days above its {e['daily_budget']} daily budget; highest "
            f"{e['highest_day']['spend']} on {e['highest_day']['date']} "
            f"({e['highest_day']['to_budget']}x)"
            for e in entities
            if e["days_over_budget"]
        ],
    }


def _window(
    artifacts: ArtifactStore, args: SummarizeWindowArgs, today: date
) -> tuple[date, date, str]:
    if args.window is None:
        if args.start_date is None or args.end_date is None:
            raise ComputeError("give a window preset (e.g. last_week) or start_date and end_date")
        return args.start_date, args.end_date, "dates given"
    if args.start_date is not None or args.end_date is not None:
        raise ComputeError("give a window preset or dates, not both")
    ends = []
    for artifact_id in args.artifact_ids:
        rows = rows_from_payload(artifacts.read(artifact_id).payload)
        complete = [r.window.start for r in rows if r.window.is_complete]
        if rows:
            ends.append(max(complete or [r.window.start for r in rows]))
    if not ends:
        raise ComputeError("no rows to resolve the window from")
    through = min(ends)
    (start, end), _ = resolve_preset(args.window, today=today, data_through=through, days=args.days)
    return start, end, f"{args.window}, data through {through.isoformat()}"


def platform_comparisons(headline: list[dict[str, Any]]) -> list[str]:
    """How each pair of accounts compares on CPA and ROAS, both ways, with the verdict.

    'X is 46% lower than Y' and 'Y is 86% higher than X' are the same gap; a model picking one
    base by hand gets it wrong, so both are written here.
    """
    lines: list[str] = []
    for i, a in enumerate(headline):
        for b in headline[i + 1 :]:
            name_a, name_b = (
                f"{a['platform']} ({a['account']})",
                f"{b['platform']} ({b['account']})",
            )
            if a["currency"] != b["currency"]:
                lines.append(
                    f"{name_a} and {name_b} report in different currencies "
                    f"({a['currency']}, {b['currency']}): not compared"
                )
                continue
            days = (
                ""
                if a["covered_window"] == b["covered_window"]
                else (
                    f" (they cover different days: {a['covered_window']} and {b['covered_window']})"
                )
            )
            for metric, label in (("cpa", "CPA"), ("roas", "ROAS")):
                if a.get(metric) in (None, "") or b.get(metric) in (None, ""):
                    continue
                x, y = Decimal(str(a[metric])), Decimal(str(b[metric]))
                if x == 0 or y == 0:
                    continue
                unit = f" {a['currency']}" if metric == "cpa" else ""
                ab, ba = x / y - 1, y / x - 1
                lines.append(
                    f"{label}: {name_a} {x:,.2f}{unit} is {abs(ab):.0%} "
                    f"{'lower' if ab < 0 else 'higher'} ({verdict(metric, ab)}) than {name_b} "
                    f"{y:,.2f}{unit}, a gap of {abs(x - y):,.2f}{unit}; {name_b}'s is "
                    f"{abs(ba):.0%} {'lower' if ba < 0 else 'higher'} ({verdict(metric, ba)}){days}"
                )
    return lines


def run_summarize_window(
    artifacts: ArtifactStore,
    args: SummarizeWindowArgs,
    *,
    goals: GoalLookup | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    start, end, rule = _window(artifacts, args, today or datetime.now(UTC).date())
    if end < start:
        raise ComputeError("window end precedes start")
    budgets: dict[tuple[str, str], dict[str, Decimal]] = {}
    for artifact_id in args.budgets_artifact_ids:
        record = artifacts.read(artifact_id)
        if (
            record.metadata.kind != "provider_result"
            or not record.metadata.platform
            or not record.metadata.account_ref
        ):
            raise ComputeError(
                f"{artifact_id} must be a provider_result with platform and account scope"
            )
        scope = (record.metadata.platform, record.metadata.account_ref)
        budgets.setdefault(scope, {}).update(budgets_from_payload(record.payload))
    platforms: dict[str, dict[str, Any]] = {}
    by_account: dict[str, list[PerformanceRow]] = {}
    for artifact_id in args.artifact_ids:
        record = artifacts.read(artifact_id)
        if record.metadata.kind != "performance_rows":
            raise ComputeError(
                f"{artifact_id} is a {record.metadata.kind} artifact, not performance rows: pass "
                "the artifact_id of a platform performance read (read_result with "
                "artifact_kind performance_rows); for stored history use query_history, "
                "explain_change, or check_pacing instead"
            )
        rows = rows_from_payload(record.payload)
        if not rows:
            raise ComputeError(f"{artifact_id} contains no rows")
        platform = record.metadata.platform or rows[0].platform.value
        account = record.metadata.account_ref or rows[0].account_ref
        by_account.setdefault(account, []).extend(rows)
        accounts = platforms.setdefault(platform, {})
        if account in accounts:
            raise ComputeError(f"provide one performance_rows artifact for {platform}/{account}")
        summary = summarize_rows(
            rows,
            start=start,
            end=end,
            budgets=budgets.get((platform, account), {}),
        )
        summary["source_artifact"] = artifact_id
        if summary["over_budget_days"]:
            # A day over budget can be the platform's own allowance, not overspend.
            allowance = rules_for(platform).pacing_note()
            if allowance:
                summary["over_budget_note"] = allowance
        summary["missing_fields"] = list(record.payload.get("missing_fields") or [])
        accounts[account] = summary
    metadata = artifacts.write_json(
        "analysis",
        {"schema_version": SUMMARY_SCHEMA_VERSION, "platforms": platforms},
        schema_version=SUMMARY_SCHEMA_VERSION,
        requested_window=f"{start.isoformat()}..{end.isoformat()}",
        tool_name=SUMMARIZE_WINDOW_TOOL,
    )
    headline = [
        {
            "platform": platform,
            "account": account,
            "covered_window": summary["covered_window"],
            "days_covered": summary["days_covered"],
            "currency": summary["currency"],
            **{k: summary["totals"][k] for k in ("spend", "conversions", "cpa", "roas")},
        }
        for platform, accounts in platforms.items()
        for account, summary in accounts.items()
    ]
    # The headline and the verdicts come first, so even a partial view of the result has them.
    result: dict[str, Any] = {"headline": headline}
    judged = against_goals(by_account, start=start, end=end, goals=goals)
    if judged:
        result["against_goals"] = judged
    compared = platform_comparisons(headline)
    if compared:
        result["comparisons"] = compared
    result["resolved_window"] = {"window": f"{start.isoformat()}..{end.isoformat()}", "rule": rule}
    caveats = cross_platform_caveats(
        {h["platform"] for h in headline},
        {h["covered_window"] for h in headline},
    )
    if caveats:
        result["caveats"] = caveats
    result["artifact_id"] = metadata.artifact_id
    result["platforms"] = platforms
    return result


def build_summarize_window_tool(
    artifacts: ArtifactStore, goals: GoalLookup | None = None
) -> ToolSpec:
    def _run(kwargs: dict[str, Any], _context: ToolContext) -> str:
        try:
            args = SummarizeWindowArgs.model_validate(kwargs)
            return json.dumps(run_summarize_window(artifacts, args, goals=goals))
        except (
            ArtifactError,
            ComputeError,
            NormalizationError,
            ValidationError,
            ValueError,
        ) as exc:
            return json.dumps({"error": True, "detail": sanitize_exception(exc)})

    return ToolSpec(
        name=SUMMARIZE_WINDOW_TOOL,
        description=(
            "Summarize one window from performance_rows artifacts: totals, per-entity spend share, "
            "CPA, ROAS, CTR, average daily spend and pacing against daily budgets (pass the "
            "list_campaigns artifacts), plus a daily series with day-over-day changes. Use it for "
            "pacing and top-N questions inside a single window; use compare_periods for "
            "period-over-period change and check_anomalies for unusual days. over_budget is "
            "on average; over_budget_days counts each day above the daily budget. comparisons "
            "state how accounts compare, both ways, with better or worse."
        ),
        parameters=parameters_for(SummarizeWindowArgs),
        handler=_run,
    )
