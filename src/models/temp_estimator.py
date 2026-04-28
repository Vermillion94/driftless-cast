"""
Mohseni-style air→water temperature estimator.

Mohseni, Stefan, Erickson (1998) fit a four-parameter logistic:

    T_water = mu + (alpha - mu) / (1 + exp(gamma * (beta - T_air_weekly)))

where T_air_weekly is a rolling mean (we use 7-day) of air temperature.
Coefficients vary by stream class; we key off the `spring_influenced` flag
that's already on each reach.

Literature sources (TU Driftless Area Restoration reports, Selbig/Bannerman
USGS studies of WI spring creeks, Mohseni 1998): limestone spring creeks in
the Driftless hold 45–65°F year-round; freestone mainstems (Kickapoo,
lower Root, Upper Iowa) swing 32–78°F.

These defaults are published-literature ballpark, not reach-specific fits.
Per-reach calibration is the follow-on when we have enough paired air/water.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from math import exp
from pathlib import Path
from typing import List, Optional

LOG = logging.getLogger(__name__)

FIT_OVERRIDE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "calibration" / "mohseni_fit.json"


# Mohseni coefficients in °F (converted from °C literature values).
@dataclass(frozen=True)
class MohseniParams:
    mu_f: float      # winter floor
    alpha_f: float   # summer ceiling
    beta_f: float    # inflection
    gamma: float     # steepness (per °F)


# Spring-influenced limestone creek — groundwater dominates, dampened swing.
SPRING_INFLUENCED = MohseniParams(mu_f=41.0, alpha_f=65.0, beta_f=54.0, gamma=0.075)

# Freestone / mainstem — air-tracking, full seasonal range.
FREESTONE = MohseniParams(mu_f=34.0, alpha_f=77.0, beta_f=60.0, gamma=0.095)


def _load_fit_override() -> dict:
    """Runtime override pulled from data/calibration/mohseni_fit.json if present.

    `refit_mohseni` writes this file after fitting against USGS 00010 data.
    Falls back to the literature defaults above when no fit is available.
    """
    if not FIT_OVERRIDE_PATH.exists():
        return {}
    try:
        data = json.loads(FIT_OVERRIDE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOG.warning("mohseni fit override read failed: %s", exc)
        return {}
    out = {}
    for klass, entry in (data.get("classes") or {}).items():
        fit = entry.get("fit")
        if fit and all(k in fit for k in ("mu_f", "alpha_f", "beta_f", "gamma")):
            out[klass] = MohseniParams(
                mu_f=float(fit["mu_f"]),
                alpha_f=float(fit["alpha_f"]),
                beta_f=float(fit["beta_f"]),
                gamma=float(fit["gamma"]),
            )
    return out


_FIT_OVERRIDE = _load_fit_override()
if _FIT_OVERRIDE:
    LOG.info("Mohseni override loaded for classes: %s", sorted(_FIT_OVERRIDE))


def params_for_reach(spring_influenced: bool) -> MohseniParams:
    # Prefer an override from the USGS-fit curve. For spring-influenced reaches,
    # "spring" is the ideal match; if the fit dataset didn't have any strictly
    # spring-regime gauges, fall back to "mixed" (moderate dampening) before
    # resorting to the literature constants.
    if spring_influenced:
        return (_FIT_OVERRIDE.get("spring")
                or _FIT_OVERRIDE.get("mixed")
                or SPRING_INFLUENCED)
    return _FIT_OVERRIDE.get("freestone", FREESTONE)


def mohseni(t_air_f: float, p: MohseniParams) -> float:
    """Pointwise estimate. Input should already be the 7-day rolling mean."""
    return p.mu_f + (p.alpha_f - p.mu_f) / (1.0 + exp(p.gamma * (p.beta_f - t_air_f)))


def rolling_mean(series: List[float], window: int) -> List[float]:
    """Backwards-looking rolling mean; first `window-1` values use partial windows."""
    out: List[float] = []
    for i in range(len(series)):
        start = max(0, i - window + 1)
        chunk = series[start:i + 1]
        out.append(sum(chunk) / len(chunk))
    return out


def estimate_water_series_f(air_daily_f: List[float], spring_influenced: bool, window: int = 7) -> List[float]:
    """Convert a daily-mean air-temp °F series to daily-mean water-temp °F."""
    p = params_for_reach(spring_influenced)
    smoothed = rolling_mean(air_daily_f, window)
    return [mohseni(t, p) for t in smoothed]


def estimate_current_water_f(recent_air_daily_f: List[float], spring_influenced: bool) -> Optional[float]:
    """Current-day water-temp estimate from the last ~7 days of air temp."""
    if not recent_air_daily_f:
        return None
    p = params_for_reach(spring_influenced)
    window = recent_air_daily_f[-7:]
    avg = sum(window) / len(window)
    return mohseni(avg, p)
