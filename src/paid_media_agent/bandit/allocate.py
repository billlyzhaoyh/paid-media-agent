"""Split a total daily budget across campaigns to maximise expected conversions.

Each campaign has a concave response curve f(spend), and spend is its budget times its pacing
ratio. With diminishing returns, the best split gives every campaign the same marginal return per
unit of budget, λ, unless a bound stops it (water-filling). Bisection on λ finds the level at which
the budgets add up to the total. An optional CPIA cap stops a campaign where its next conversion
would cost more than `max_cpia`, even if budget is left over (CBS's profitability constraint).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

BISECTION_STEPS = 100
TOLERANCE = 1e-6


class Curve(Protocol):
    def value(self, spend: float) -> float:
        """Expected conversions per day at `spend`."""
        ...

    def marginal(self, spend: float) -> float:
        """Extra conversions per extra unit of spend at `spend`."""
        ...

    def spend_at_marginal(self, marginal: float) -> float:
        """The spend where the marginal return falls to `marginal` (inf if it never does)."""
        ...


@dataclass(frozen=True)
class Allocation:
    budgets: tuple[float, ...]
    binding: tuple[tuple[str, ...], ...]
    """Per campaign: which constraints stopped it (`lower`, `upper`, `cpia`), empty when none."""
    shadow_price: float
    """Conversions bought by the last unit of budget; 0 when the total does not bind."""
    feasible: bool
    """False when the minimum budgets alone exceed the total; every campaign is then at its minimum."""


def _budget_at(
    curve: Curve, pacing: float, lower: float, upper: float, price: float, cpia: float | None
) -> tuple[float, tuple[str, ...]]:
    # d f(p b) / db = p f'(p b); the campaign grows until that falls to `price`.
    marginal = price / pacing
    capped = False
    if cpia is not None and 1.0 / cpia > marginal:
        marginal, capped = 1.0 / cpia, True
    budget = curve.spend_at_marginal(marginal) / pacing
    if budget >= upper:
        return upper, ("upper",)
    if budget <= lower:
        return lower, ("lower",)
    return budget, ("cpia",) if capped else ()


def allocate(
    curves: Sequence[Curve],
    pacing: Sequence[float],
    lower: Sequence[float],
    upper: Sequence[float],
    total: float,
    *,
    max_cpia: Sequence[float | None] | None = None,
) -> Allocation:
    """Budgets within [lower, upper] summing to at most `total` that maximise Σ f(pacing·budget)."""
    n = len(curves)
    if not n == len(pacing) == len(lower) == len(upper):
        raise ValueError("one pacing ratio and bound pair per curve")
    if any(lo > hi + TOLERANCE for lo, hi in zip(lower, upper, strict=True)):
        raise ValueError("every lower bound must be at or below its upper bound")
    if any(p <= 0 for p in pacing):
        raise ValueError("pacing ratios must be positive")
    caps = list(max_cpia) if max_cpia is not None else [None] * n
    if n == 0:
        return Allocation((), (), 0.0, True)
    if sum(lower) > total + TOLERANCE:
        return Allocation(tuple(lower), tuple(("lower",) for _ in range(n)), math.inf, False)

    def at(price: float) -> list[tuple[float, tuple[str, ...]]]:
        return [
            _budget_at(curves[i], pacing[i], lower[i], upper[i], price, caps[i]) for i in range(n)
        ]

    free = at(0.0)
    if sum(b for b, _ in free) <= total + TOLERANCE:
        return Allocation(tuple(b for b, _ in free), tuple(why for _, why in free), 0.0, True)
    # Bracket λ: at `high` every campaign sits at its lower bound.
    low = 1e-15
    high = max(p * c.marginal(p * lo) for c, p, lo in zip(curves, pacing, lower, strict=True))
    high = max(high * 2.0, 1e-12)
    for _ in range(BISECTION_STEPS):
        mid = math.sqrt(low * high)
        if sum(b for b, _ in at(mid)) > total:
            low = mid
        else:
            high = mid
        if high / low < 1 + 1e-12:
            break
    chosen = at(high)
    return Allocation(tuple(b for b, _ in chosen), tuple(why for _, why in chosen), high, True)
