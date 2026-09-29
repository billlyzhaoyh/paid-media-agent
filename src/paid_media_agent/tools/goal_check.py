"""Judge a window against each account's goals: CPA or ROAS against target, written by code.

Analyses carry this beside their period-over-period numbers, so "is 42 a good CPA" is answered by
the account's own target rather than a benchmark. Accounts without goals are left out, and the
answer says the judgement is directional.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from paid_media_agent.analytics.goals import GoalLookup, goal_line
from paid_media_agent.domain.metrics import PerformanceRow
from paid_media_agent.tools.compute import aggregate


def against_goals(
    rows_by_account: Mapping[str, Sequence[PerformanceRow]],
    *,
    start: date,
    end: date,
    goals: GoalLookup | None,
) -> list[dict[str, Any]]:
    """For each account with a target, its window CPA/ROAS against it (goal in force on `end`)."""
    if goals is None:
        return []
    results: list[dict[str, Any]] = []
    for account, rows in sorted(rows_by_account.items()):
        goal = goals(account, end)
        if goal is None or (goal.target_cpa is None and goal.target_roas is None):
            continue
        metrics = aggregate([r for r in rows if start <= r.window.start <= end])
        cpa = float(metrics.cpa) if metrics.cpa is not None else None
        roas = float(metrics.roas) if metrics.roas is not None else None
        lines = [
            line
            for line in (
                goal_line("CPA", cpa, goal.target_cpa, lower_is_better=True),
                goal_line("ROAS", roas, goal.target_roas, lower_is_better=False),
            )
            if line
        ]
        missing = [
            name
            for name, value, target in (
                ("CPA", cpa, goal.target_cpa),
                ("ROAS", roas, goal.target_roas),
            )
            if target is not None and value is None
        ]
        if missing:
            lines.append(f"{' and '.join(missing)} cannot be judged: the metric is missing")
        results.append(
            {
                "account_alias": account,
                "window": f"{start.isoformat()}..{end.isoformat()}",
                "cpa": None if cpa is None else round(cpa, 2),
                "target_cpa": goal.target_cpa,
                "roas": None if roas is None else round(roas, 4),
                "target_roas": goal.target_roas,
                "goal_effective_from": goal.effective_from.isoformat(),
                "reading": "; ".join(lines) + ".",
            }
        )
    return results
