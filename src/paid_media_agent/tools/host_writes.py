"""Host operations: changes to this deployment's own data that still need a human's approval.

A host operation is proposed, approved, executed once, and read back through the same
`ProposalService` and `WriteExecutor` as a provider mutation, so the pause, approval cards, signed
claims, replay refusal, receipts, and change log all apply. It never touches a provider: it lives
outside the provider catalog, and its tool name uses the reserved `host__` prefix, which no
catalog entry can have.

The one operation today sets an account's goals (`host__set_account_goals`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from paid_media_agent.analytics.goals import GOAL_FIELDS, GoalError, GoalStore, validate_changes
from paid_media_agent.domain.common import JsonValue, RiskLevel
from paid_media_agent.domain.proposals import canonical_json

HOST_TOOL_PREFIX = "host__"
SET_ACCOUNT_GOALS = "host__set_account_goals"
ALIAS_ARG = "account_alias"


class HostOperationError(ValueError):
    pass


@dataclass(frozen=True)
class HostOperation:
    tool_name: str
    description: str
    editable_fields: tuple[str, ...]
    risk: RiskLevel
    read: Callable[[str], dict[str, JsonValue]]
    """Current values of the editable fields for an account alias."""
    apply: Callable[[str, dict[str, JsonValue], UUID], None]
    validate: Callable[[Mapping[str, JsonValue]], dict[str, JsonValue]]
    units: dict[str, str] = field(default_factory=dict)

    def digest(self) -> str:
        material = {
            "tool_name": self.tool_name,
            "editable_fields": list(self.editable_fields),
            "risk": self.risk.value,
            "units": self.units,
        }
        return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()[:16]

    def risk_flags(
        self, changes: Mapping[str, JsonValue], before: Mapping[str, JsonValue]
    ) -> tuple[str, ...]:
        flags = ["goal_change"]
        new, old = changes.get("monthly_budget"), before.get("monthly_budget")
        if isinstance(new, (int, float)) and (not isinstance(old, (int, float)) or new > old):
            flags.append("budget_increase")
        return tuple(flags)


def is_host_tool(tool_name: str) -> bool:
    return tool_name.startswith(HOST_TOOL_PREFIX)


def goals_operation(goals: GoalStore, today: Callable[[str], date]) -> HostOperation:
    """Set an account's target CPA, target ROAS, or monthly budget from today (account-local)."""

    def read(alias: str) -> dict[str, JsonValue]:
        goal = goals.current(alias, today(alias))
        values = goal.values() if goal else dict.fromkeys(GOAL_FIELDS)
        return {k: v for k, v in values.items()}

    def validate(changes: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        try:
            return dict(validate_changes(changes))
        except GoalError as exc:
            raise HostOperationError(str(exc)) from None

    def apply(alias: str, changes: dict[str, JsonValue], proposal_id: UUID) -> None:
        goals.set(
            alias,
            changes,
            effective_from=today(alias),
            source="proposal",
            proposal_id=proposal_id,
        )

    return HostOperation(
        tool_name=SET_ACCOUNT_GOALS,
        description=(
            "Set the account's goals from today: target_cpa and monthly_budget in the account's "
            "currency, target_roas as conversion value per unit of spend. null clears a goal. "
            "target_ref is the account alias."
        ),
        editable_fields=GOAL_FIELDS,
        risk=RiskLevel.MEDIUM,
        read=read,
        apply=apply,
        validate=validate,
        units={
            "target_cpa": "account currency per conversion",
            "target_roas": "conversion value per unit of spend",
            "monthly_budget": "account currency per calendar month",
        },
    )
