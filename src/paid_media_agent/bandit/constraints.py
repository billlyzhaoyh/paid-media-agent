"""What limits a campaign's spend: its budget, demand, its bid target, or a learning phase.

Raising a budget only buys more where the budget is what binds. The platform's own signals decide
first (Google status reasons, bid-strategy status, impression share lost to budget or to rank);
without them, how much of its budget the campaign spends, and which kind of bid strategy it runs,
decide. Every result carries its evidence, so a recommendation can say why.

Precedence:
1. learning: the bid strategy is learning; its budget is held.
2. platform signals (high confidence).
3. spend against budget and bid strategy (medium confidence).
4. otherwise unknown (low confidence), treated as before.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from paid_media_agent.bandit.platform_rules import PlatformRules, strategy_family

Kind = Literal["budget", "demand", "target", "learning", "unknown"]
Confidence = Literal["high", "medium", "low"]

BUDGET_LOST_BINDS = 0.10
"""A median share of impressions lost to budget at or above this means the budget binds."""
BUDGET_LOST_NONE = 0.05
RANK_LOST_BINDS = 0.30
SPENDS_BUDGET = 0.95
UNDER_SPENDS = 0.85
_BUDGET_REASONS = {"BUDGET_CONSTRAINED", "LIMITED_BY_BUDGET"}
_DEMAND_REASONS = {"SEARCH_VOLUME_LIMITED", "LIMITED_BY_INVENTORY"}
_TARGET_REASONS = {"BIDDING_STRATEGY_CONSTRAINED"}


@dataclass(frozen=True)
class Signals:
    """What the platform reported for one campaign, as of the decision day."""

    status_reasons: tuple[str, ...] = ()
    bidding_status: str | None = None
    learning_status: str | None = None
    channel_type: str | None = None
    recommended_budget: float | None = None
    budget_lost_share: float | None = None
    """Median over the last 14 reported days."""
    rank_lost_share: float | None = None
    impression_share: float | None = None

    @property
    def reasons(self) -> set[str]:
        values = {r.upper() for r in self.status_reasons}
        if self.bidding_status:
            values.add(self.bidding_status.upper())
        return values


@dataclass(frozen=True)
class Constraint:
    kind: Kind
    confidence: Confidence
    evidence: tuple[str, ...] = field(default_factory=tuple)

    def as_record(self) -> dict[str, object]:
        return {"kind": self.kind, "confidence": self.confidence, "evidence": list(self.evidence)}


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def classify(
    *,
    rules: PlatformRules,
    signals: Signals,
    bid_strategy: str | None,
    utilisation: float | None,
    days: int,
) -> Constraint:
    """What limits spend, from platform signals first and spend against budget second."""
    reasons = signals.reasons
    family = strategy_family(rules, bid_strategy)
    strategy = f"bid strategy {bid_strategy}" if bid_strategy else "bid strategy unknown"
    shares = (
        f"{_pct(signals.budget_lost_share)} of impressions lost to budget, "
        f"{_pct(signals.rank_lost_share)} to rank"
        if signals.budget_lost_share is not None or signals.rank_lost_share is not None
        else None
    )

    learning = signals.learning_status and signals.learning_status.upper() == "LEARNING"
    if learning or any(r.startswith("LEARNING") for r in reasons):
        found = sorted(r for r in reasons if r.startswith("LEARNING")) or ["LEARNING"]
        return Constraint("learning", "high", (f"bid strategy learning ({', '.join(found)})",))

    if reasons & _BUDGET_REASONS or (
        signals.budget_lost_share is not None and signals.budget_lost_share >= BUDGET_LOST_BINDS
    ):
        evidence = (
            [f"platform: {', '.join(sorted(reasons & _BUDGET_REASONS))}"]
            if reasons & _BUDGET_REASONS
            else []
        )
        if shares:
            evidence.append(shares)
        return Constraint("budget", "high", tuple(evidence))
    if reasons & _DEMAND_REASONS:
        return Constraint(
            "demand",
            "high",
            (
                f"platform: {', '.join(sorted(reasons & _DEMAND_REASONS))}",
                *([shares] if shares else []),
            ),
        )
    target_by_rank = (
        family == "target"
        and signals.rank_lost_share is not None
        and signals.rank_lost_share >= RANK_LOST_BINDS
        and (signals.budget_lost_share or 0.0) < BUDGET_LOST_NONE
    )
    if reasons & _TARGET_REASONS or target_by_rank:
        evidence = (
            [f"platform: {', '.join(sorted(reasons & _TARGET_REASONS))}"]
            if reasons & _TARGET_REASONS
            else []
        )
        return Constraint("target", "high", (*evidence, strategy, *([shares] if shares else [])))

    if utilisation is None or days < 7:
        return Constraint("unknown", "low", ("too little spend history against a budget",))
    spent = f"spends {utilisation:.0%} of its budget (median of the last 14 days)"
    if utilisation >= SPENDS_BUDGET:
        if family == "spend_everything":
            return Constraint(
                "budget", "medium", (spent, f"{strategy} spends its budget by design")
            )
        return Constraint("budget", "medium", (spent,))
    if utilisation < UNDER_SPENDS:
        if family == "target":
            return Constraint("target", "medium", (spent, strategy))
        return Constraint("demand", "medium", (spent, strategy))
    return Constraint("unknown", "low", (spent,))


def median_share(values: Sequence[float | None]) -> float | None:
    known = sorted(v for v in values if v is not None)
    if not known:
        return None
    mid = len(known) // 2
    return known[mid] if len(known) % 2 else (known[mid - 1] + known[mid]) / 2
