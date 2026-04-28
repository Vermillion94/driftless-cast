"""
Mohseni coefficient fitting from paired (air_temp_f, water_temp_f) samples.

We avoid adding scipy as a dependency: a coarse grid search over the plausible
parameter space is fast enough for our sample sizes (~500–5000 pairs per fit)
and makes the search explicitly bounded at values that make physical sense.
"""
from __future__ import annotations

import logging
from math import exp
from typing import List, Optional, Tuple

from src.models.temp_estimator import MohseniParams, rolling_mean

LOG = logging.getLogger(__name__)

# Grid — physically plausible ranges for small Midwestern streams.
MU_GRID     = [32, 34, 36, 38, 40, 42, 44, 46]     # winter floor °F
ALPHA_GRID  = [60, 64, 68, 72, 76, 80, 84]         # summer ceiling °F
BETA_GRID   = [48, 52, 56, 60, 64, 68]             # inflection °F
GAMMA_GRID  = [0.06, 0.08, 0.10, 0.12, 0.14, 0.16, 0.20]


def _rmse(pairs: List[Tuple[float, float]], params: MohseniParams) -> float:
    if not pairs:
        return float("inf")
    total = 0.0
    for air_f, water_f in pairs:
        pred = params.mu_f + (params.alpha_f - params.mu_f) / (1.0 + exp(params.gamma * (params.beta_f - air_f)))
        total += (pred - water_f) ** 2
    return (total / len(pairs)) ** 0.5


def prepare_pairs(air_series: List[Tuple[str, float]],
                   water_series: List[Tuple[str, float]],
                   window: int = 7) -> List[Tuple[float, float]]:
    """Join on date, apply the same 7-day rolling mean Mohseni uses."""
    air_map = {d: t for d, t in air_series}
    water_map = {d: t for d, t in water_series}
    shared = sorted(set(air_map) & set(water_map))
    if len(shared) < window:
        return []
    air_values = [air_map[d] for d in shared]
    smoothed = rolling_mean(air_values, window)
    return [(smoothed[i], water_map[shared[i]]) for i in range(len(shared))]


def fit(pairs: List[Tuple[float, float]]) -> Optional[Tuple[MohseniParams, float]]:
    """Grid search returning best params and its RMSE. None if pairs are too thin."""
    if len(pairs) < 30:
        return None
    best: Optional[Tuple[MohseniParams, float]] = None
    for mu in MU_GRID:
        for alpha in ALPHA_GRID:
            if alpha <= mu + 10:
                continue
            for beta in BETA_GRID:
                if not (mu <= beta <= alpha):
                    continue
                for gamma in GAMMA_GRID:
                    p = MohseniParams(mu_f=mu, alpha_f=alpha, beta_f=beta, gamma=gamma)
                    err = _rmse(pairs, p)
                    if best is None or err < best[1]:
                        best = (p, err)
    return best


def classify_stream(water_series: List[Tuple[str, float]]) -> str:
    """Rough spring-fed vs freestone label from the annual swing of water temp.

    Spring-fed streams in the Driftless typically swing <25°F across the year
    (dampened by groundwater); freestone swings 40°F+.
    """
    if len(water_series) < 30:
        return "unknown"
    temps = [t for _d, t in water_series]
    swing = max(temps) - min(temps)
    if swing < 26:
        return "spring"
    if swing > 38:
        return "freestone"
    return "mixed"
