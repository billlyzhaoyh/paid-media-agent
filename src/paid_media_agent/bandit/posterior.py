"""The local model: one campaign's response curve, fit by Bayesian linear regression.

Each campaign's expected conversions follow

    log(y + 1) = kappa1 + kappa2 * log(x / u + 1) + weekday effect,

where x is spend and u is the campaign's trailing cost per conversion, so x / u is spend measured
in "conversions' worth". In that unit a campaign at its usual spend has x / u close to y, which
makes kappa1 = (1 - kappa2) * log(y + 1): the curve passes through zero conversions at zero spend
(kappa1 >= 0) exactly when it has diminishing returns (kappa2 <= 1), and the fitted kappa2 is the
campaign's elasticity of conversions to spend. In currency units both constraints cannot hold for
realistic costs per conversion.

The fit uses a normal-inverse-gamma prior (Han & Gabor, AdKDD 2020, section 3.2). Its data are
the campaign's lag-corrected history plus pseudo-samples from the global model at nearby spends.
When the result is not a valid curve, the prior precision on kappa2 is raised step by step
(0, 1, 4, 16, 64, 256), pulling kappa2 toward the prior mean, until kappa1 >= 0 and kappa2 plus or
minus `ladder_z` standard deviations lies inside (0, 1) (CBS section 6.2).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

LADDER = (0.0, 1.0, 4.0, 16.0, 64.0, 256.0)
PRIOR_KAPPA2 = 0.5
LADDER_Z = 1.0
WEEKDAY_PRECISION = 1.0
INTERCEPT_PRECISION = 1e-6
A0 = 1.0
B0 = 0.1


@dataclass(frozen=True)
class PowerCurve:
    """f(x) = exp(kappa1) * (x / unit + 1) ** kappa2 - 1 expected conversions at spend x."""

    kappa1: float
    kappa2: float
    unit: float

    def value(self, spend: float) -> float:
        return math.exp(self.kappa1) * math.pow(max(spend, 0.0) / self.unit + 1.0, self.kappa2) - 1

    def marginal(self, spend: float) -> float:
        scaled = max(spend, 0.0) / self.unit + 1.0
        return math.exp(self.kappa1) * self.kappa2 / self.unit * math.pow(scaled, self.kappa2 - 1)

    def spend_at_marginal(self, marginal: float) -> float:
        if marginal <= 0:
            return math.inf
        if self.kappa2 <= 0:
            return 0.0
        if self.kappa2 >= 1:
            return math.inf if self.marginal(0.0) >= marginal else 0.0
        ratio = marginal * self.unit / (math.exp(self.kappa1) * self.kappa2)
        log_scaled = math.log(ratio) / (self.kappa2 - 1)
        if log_scaled > 700:
            return math.inf
        return max(0.0, self.unit * (math.exp(log_scaled) - 1.0))


def _weekday_dummies(weekdays: np.ndarray) -> np.ndarray:
    """One column per weekday after Monday."""
    out = np.zeros((len(weekdays), 6))
    for row, weekday in enumerate(weekdays):
        if weekday > 0:
            out[row, int(weekday) - 1] = 1.0
    return out


@dataclass(frozen=True)
class Posterior:
    mean: np.ndarray
    """kappa1, kappa2, then weekday effects when the history had them."""
    cov: np.ndarray
    a: float
    b: float
    unit: float
    precision_kappa2: float
    """Prior precision on kappa2 the ladder needed; 0 means the data alone gave a valid curve."""
    n_history: int
    n_pseudo: int
    valid: bool
    weekday_means: np.ndarray = field(default_factory=lambda: np.zeros(0))

    @property
    def kappa_mean(self) -> np.ndarray:
        return self.mean[:2]

    @property
    def kappa_cov(self) -> np.ndarray:
        return self.cov[:2, :2]

    @property
    def kappa2_sd(self) -> float:
        return math.sqrt(max(float(self.cov[1, 1]), 0.0))

    @property
    def noise_variance(self) -> float:
        """Day-to-day variance of log(y + 1) around the curve; 0 when unknown (a stored curve)."""
        if not (math.isfinite(self.a) and math.isfinite(self.b)) or self.a <= 1:
            return 0.0
        return self.b / (self.a - 1)

    def curve(self, kappa: tuple[float, float] | None = None) -> PowerCurve:
        """The expected-conversions curve for `kappa` (default: the posterior mean).

        The regression fits log(y + 1), whose back-transform is the median day, not the mean; the
        lognormal correction exp(sigma^2 / 2) restores the mean, which is what allocation needs.
        """
        k1, k2 = kappa if kappa is not None else (float(self.mean[0]), float(self.mean[1]))
        return PowerCurve(k1 + self.noise_variance / 2, k2, self.unit)

    def predictive(
        self, spend: np.ndarray, weekdays: np.ndarray | None = None
    ) -> tuple[np.ndarray, np.ndarray]:
        """Centre and scale of log(y + 1) at each spend (normal approximation of the Student-t)."""
        x = np.column_stack([np.ones(len(spend)), np.log(np.asarray(spend) / self.unit + 1.0)])
        if len(self.mean) > 2:
            wd = (
                _weekday_dummies(weekdays) - self.weekday_means
                if weekdays is not None
                else np.zeros((len(spend), 6))
            )
            x = np.column_stack([x, wd])
        centre = x @ self.mean
        # cov = b / (a - 1) * inv(Lambda); the predictive variance is b / a * (1 + x' inv(Lambda) x).
        spread = np.einsum("ij,jk,ik->i", x, self.cov, x) * (self.a - 1) / self.a
        return centre, np.sqrt(self.b / self.a + spread)

    def as_record(self) -> dict[str, Any]:
        return {
            "post_mean": [round(float(v), 6) for v in self.kappa_mean],
            "post_cov": [[float(v) for v in row] for row in self.kappa_cov],
        }

    @classmethod
    def from_record(cls, mean: list[float], cov: list[list[float]], unit: float) -> Posterior:
        """A stored posterior (kappa only), reused when today's data fail their checks."""
        return cls(
            mean=np.asarray(mean, dtype=np.float64),
            cov=np.asarray(cov, dtype=np.float64),
            a=math.nan,
            b=math.nan,
            unit=unit,
            precision_kappa2=math.nan,
            n_history=0,
            n_pseudo=0,
            valid=True,
        )


def _nig(
    x: np.ndarray,
    y: np.ndarray,
    mu0: np.ndarray,
    lambda0: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, float, float]:
    """Normal-inverse-gamma update of the prior (mu0, lambda0) by the real days (x, y)."""
    precision = x.T @ x + lambda0
    mean = np.linalg.solve(precision, lambda0 @ mu0 + x.T @ y)
    a = A0 + len(y) / 2
    b = B0 + 0.5 * float(y @ y + mu0 @ lambda0 @ mu0 - mean @ precision @ mean)
    b = max(b, 1e-9)
    cov = b / (a - 1) * np.linalg.inv(precision)
    return mean, cov, a, b


def _with_pseudo(
    mu0: np.ndarray, lambda0: np.ndarray, x: np.ndarray, y: np.ndarray, weight: float
) -> tuple[np.ndarray, np.ndarray]:
    """Fold pseudo-samples into the prior, as if they had been observed before the real days.

    They inform the curve but not the noise estimate: they are noise-free predictions, and their
    misfit to a curve the ladder holds back says nothing about day-to-day variation.
    """
    precision = lambda0 + weight * x.T @ x
    mean = np.linalg.solve(precision, lambda0 @ mu0 + weight * x.T @ y)
    return mean, precision


def is_valid(kappa1: float, kappa2: float) -> bool:
    return kappa1 >= 0 and 0 <= kappa2 < 1


def fit_posterior(
    spend: np.ndarray,
    conversions: np.ndarray,
    weekdays: np.ndarray,
    unit: float,
    *,
    pseudo_spend: np.ndarray | None = None,
    pseudo_target: np.ndarray | None = None,
    pseudo_weight: float = 1.0,
    prior_kappa2: float = PRIOR_KAPPA2,
    ladder_z: float = LADDER_Z,
) -> Posterior:
    """Fit one campaign. `pseudo_target` is already log(y + 1), as the global model predicts it.

    Each pseudo-sample counts `pseudo_weight` times, so a coarse grid can stand for many samples.
    """
    if unit <= 0:
        raise ValueError("the spend unit must be positive")
    history = np.column_stack([np.ones(len(spend)), np.log(np.asarray(spend) / unit + 1.0)])
    use_weekdays = len(spend) >= 14 and len(set(weekdays.tolist())) == 7
    weekday_means = np.zeros(0)
    if use_weekdays:
        dummies = _weekday_dummies(weekdays)
        weekday_means = dummies.mean(axis=0)
        history = np.column_stack([history, dummies - weekday_means])
    target = np.log(np.asarray(conversions, dtype=np.float64) + 1.0)
    width = history.shape[1]
    pseudo: np.ndarray | None = None
    n_pseudo = 0
    if pseudo_spend is not None and pseudo_target is not None and len(pseudo_spend):
        points = len(pseudo_spend)
        n_pseudo = round(points * pseudo_weight)
        pseudo = np.column_stack([np.ones(points), np.log(np.asarray(pseudo_spend) / unit + 1.0)])
        if use_weekdays:
            pseudo = np.column_stack([pseudo, np.zeros((points, 6))])
    mu0 = np.zeros(width)
    mu0[1] = prior_kappa2
    valid = False
    for precision in LADDER:
        lambda0 = np.diag([INTERCEPT_PRECISION, precision] + [WEEKDAY_PRECISION] * (width - 2))
        prior_mean, prior_precision = mu0, lambda0
        if pseudo is not None and pseudo_target is not None:
            prior_mean, prior_precision = _with_pseudo(
                mu0, lambda0, pseudo, np.asarray(pseudo_target, dtype=np.float64), pseudo_weight
            )
        mean, cov, a, b = _nig(history, target, prior_mean, prior_precision)
        sd = math.sqrt(max(float(cov[1, 1]), 0.0))
        k1, k2 = float(mean[0]), float(mean[1])
        if k1 >= 0 and k2 - ladder_z * sd > 0 and k2 + ladder_z * sd < 1:
            valid = True
            break
    return Posterior(
        mean=mean,
        cov=cov,
        a=a,
        b=b,
        unit=unit,
        precision_kappa2=precision,
        n_history=len(spend),
        n_pseudo=n_pseudo,
        valid=valid or is_valid(float(mean[0]), float(mean[1])),
        weekday_means=weekday_means,
    )
