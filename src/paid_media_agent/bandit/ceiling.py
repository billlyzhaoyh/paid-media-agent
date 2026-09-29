"""How much a campaign can spend, whatever its budget: its spend ceiling D, from censored history.

Spend is the smaller of what the budget lets through and what the campaign can win at its bid
target: spend = min(rho * budget, D) (Karande et al., WSDM 2013; autobidding with ROI constraints;
Nuara et al., AAAI 2018). A day that spent below its budget shows D. A day that spent its budget
(or lost impressions to budget) only shows that D is at least what it spent: the ceiling is
censored there, as unmet demand is when a shop sells out.

D is modelled as log-normal across days and fitted by maximum likelihood with right censoring
(a Tobit model on log spend), by expectation-maximisation. Too few uncensored days means the
ceiling is unknown, and the campaign is treated as limited by its budget, as before.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

import numpy as np

MIN_OBSERVED = 5
"""Uncensored days needed before a ceiling is trusted."""
SIGMA_FLOOR = 0.05
BUDGET_BOUND = 0.95
"""Spend at or above this share of the budget in force counts as limited by the budget that day."""
BUDGET_LOST = 0.02
"""A day losing more than this share of impressions to budget is limited by the budget."""
P90 = 1.2816


@dataclass(frozen=True)
class Ceiling:
    mu: float
    sigma: float
    observed: int
    censored: int

    @property
    def mean(self) -> float:
        """Expected daily spend the campaign can reach (account currency)."""
        return math.exp(self.mu + self.sigma**2 / 2)

    @property
    def high(self) -> float:
        """The 90th percentile: a day it can reach one day in ten."""
        return math.exp(self.mu + P90 * self.sigma)

    def as_record(self) -> dict[str, float | int]:
        return {
            "mean": round(self.mean, 2),
            "p90": round(self.high, 2),
            "observed_days": self.observed,
            "budget_limited_days": self.censored,
        }


_erfc = np.frompyfunc(math.erfc, 1, 1)


def _tail(z: np.ndarray) -> np.ndarray:
    """P(Z > z) for a standard normal, elementwise."""
    return np.asarray(_erfc(z / math.sqrt(2)), dtype=np.float64) / 2


def _pdf(z: np.ndarray) -> np.ndarray:
    return np.exp(-(z**2) / 2) / math.sqrt(2 * math.pi)


def budget_limited(spend: float, budget: float | None, budget_lost_share: float | None) -> bool:
    """Whether a day's spend was held back by the budget, so it only bounds the ceiling."""
    if budget_lost_share is not None and budget_lost_share > BUDGET_LOST:
        return True
    return budget is not None and budget > 0 and spend >= BUDGET_BOUND * budget


def fit_ceiling(
    spend: np.ndarray, censored: np.ndarray, *, iterations: int = 200
) -> Ceiling | None:
    """Log-normal D from daily spend, where `censored` marks days that only bound it from below."""
    spend = np.asarray(spend, dtype=np.float64)
    censored = np.asarray(censored, dtype=bool)
    keep = spend > 0
    y, cens = np.log(spend[keep]), censored[keep]
    observed = int((~cens).sum())
    if observed < MIN_OBSERVED:
        return None
    mu = float(y[~cens].mean())
    sigma = max(float(y[~cens].std()), SIGMA_FLOOR)
    for _ in range(iterations):
        z = (y[cens] - mu) / sigma
        tail = np.maximum(_tail(z), 1e-12)
        hazard = _pdf(z) / tail
        # Moments of D given D >= the censored value, under the current fit.
        first = mu + sigma * hazard
        second = sigma**2 * (1 + z * hazard) + 2 * mu * sigma * hazard + mu**2
        e1 = np.concatenate([y[~cens], first])
        e2 = np.concatenate([y[~cens] ** 2, second])
        new_mu = float(e1.mean())
        new_sigma = max(math.sqrt(max(float(e2.mean()) - new_mu**2, 0.0)), SIGMA_FLOOR)
        done = abs(new_mu - mu) < 1e-7 and abs(new_sigma - sigma) < 1e-7
        mu, sigma = new_mu, new_sigma
        if done:
            break
    return Ceiling(mu=mu, sigma=sigma, observed=observed, censored=int(cens.sum()))


class _Curve(Protocol):
    def value(self, spend: float) -> float: ...
    def marginal(self, spend: float) -> float: ...
    def spend_at_marginal(self, marginal: float) -> float: ...


@dataclass(frozen=True)
class CappedCurve:
    """A response curve that stops at the spend ceiling: budget above it buys nothing."""

    curve: _Curve
    cap: float

    def value(self, spend: float) -> float:
        return self.curve.value(min(spend, self.cap))

    def marginal(self, spend: float) -> float:
        return 0.0 if spend >= self.cap else self.curve.marginal(spend)

    def spend_at_marginal(self, marginal: float) -> float:
        return min(self.curve.spend_at_marginal(marginal), self.cap)
