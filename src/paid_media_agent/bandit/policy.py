"""Thompson sampling over each campaign's curve, restricted to CBS's production guardrails.

A draw of (kappa1, kappa2) from the posterior is one plausible curve; allocating as if the drawn
curves were true explores budgets in proportion to how uncertain each campaign is. Draws outside
the valid region (kappa1 >= 0, 0 <= kappa2 < 1) are rejected, and so are draws outside the middle
quartiles of each parameter (|z| <= 0.674), which bounds how far one decision can explore
(Han & Gabor, AdKDD 2020, section 5). Greedy uses the posterior mean.
"""

from __future__ import annotations

import numpy as np

from paid_media_agent.bandit.posterior import Posterior, is_valid

GUARD_Z = 0.674
"""Middle quartiles of a normal distribution."""
MAX_DRAWS = 1000
_BATCH = 256


def draw_thompson(
    posterior: Posterior, rng: np.random.Generator, *, guard_z: float = GUARD_Z
) -> tuple[tuple[float, float], int]:
    """One valid (kappa1, kappa2) inside the guardrails, and how many draws were rejected.

    Falls back to the posterior mean when no draw is accepted (then every draw was rejected).
    """
    mean = posterior.kappa_mean
    cov = posterior.kappa_cov
    sd = np.sqrt(np.clip(np.diag(cov), 1e-18, None))
    rejected = 0
    while rejected < MAX_DRAWS:
        draws = rng.multivariate_normal(mean, cov, size=_BATCH, method="eigh")
        z = np.abs((draws - mean) / sd)
        ok = (
            (draws[:, 0] >= 0)
            & (draws[:, 1] >= 0)
            & (draws[:, 1] < 1)
            & (z[:, 0] <= guard_z)
            & (z[:, 1] <= guard_z)
        )
        hits = np.flatnonzero(ok)
        if len(hits):
            first = int(hits[0])
            return (float(draws[first, 0]), float(draws[first, 1])), rejected + first
        rejected += _BATCH
    return (float(mean[0]), float(mean[1])), rejected


def greedy(posterior: Posterior) -> tuple[float, float] | None:
    """The posterior-mean curve, or None when it is not a valid curve."""
    k1, k2 = float(posterior.kappa_mean[0]), float(posterior.kappa_mean[1])
    return (k1, k2) if is_valid(k1, k2) else None
