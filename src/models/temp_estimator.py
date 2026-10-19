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
DRIFTLESS_FIT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "calibration" / "mohseni_fit_driftless.json"

# Only short, strongly spring-fed headwater reaches use the Driftless "spring"
# curve (see thermal_profile.from_reach). Spring dominance fades downstream, so
# long Class I mainstems keep the previous curve.
STRONG_SPRING_STRENGTH = 0.80
STRONG_SPRING_MAX_KM = 12.0


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


def _load_driftless_fit() -> dict:
    """Curves fit on historical USGS daily water temp from Driftless trout
    streams (src/scripts/refit_mohseni_driftless.py). Preferred over the
    region-wide fit, which is dominated by large and urban rivers."""
    if not DRIFTLESS_FIT_PATH.exists():
        return {}
    try:
        data = json.loads(DRIFTLESS_FIT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOG.warning("driftless mohseni fit read failed: %s", exc)
        return {}
    out = {}
    for klass, entry in (data.get("classes") or {}).items():
        fit = (entry or {}).get("fit")
        if fit and all(k in fit for k in ("mu_f", "alpha_f", "beta_f", "gamma")):
            out[klass] = MohseniParams(float(fit["mu_f"]), float(fit["alpha_f"]),
                                       float(fit["beta_f"]), float(fit["gamma"]))
    return out


DRIFTLESS_FIT = _load_driftless_fit()
# Only the "spring" class is adopted from the Driftless fit. On the last 90
# days of live data (Stillwell, Silver, Eau Galle sentinels, Oct 2026) the
# Driftless "mixed" and "freestone" curves — fit mostly on 1970s–2008 records —
# ran 1.4–3.7°F cool and lost to the region-wide curves, so those stay. The
# spring class is different in kind: on genuinely spring-fed sites the
# region-wide curve misses by 4.3–6.2°F RMSE vs 2.1–2.3°F for the Driftless fit.
DRIFTLESS_SPRING = DRIFTLESS_FIT.get("spring")


def params_for_reach(spring_influenced: bool, spring_curve: bool = False) -> MohseniParams:
    """Mohseni curve for a reach.

    `spring_curve=True` (set by thermal_profile for short, strongly spring-fed
    headwaters) selects the Driftless-fit spring curve. Otherwise: region-wide
    USGS fit → literature constants, by spring influence.
    """
    if spring_influenced:
        if spring_curve and DRIFTLESS_SPRING is not None:
            return DRIFTLESS_SPRING
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


def estimate_water_series_f(air_daily_f: List[float], spring_influenced: bool, window: int = 7,
                            spring_curve: bool = False) -> List[float]:
    """Convert a daily-mean air-temp °F series to daily-mean water-temp °F."""
    p = params_for_reach(spring_influenced, spring_curve)
    smoothed = rolling_mean(air_daily_f, window)
    return [mohseni(t, p) for t in smoothed]


def estimate_current_water_f(recent_air_daily_f: List[float], spring_influenced: bool,
                             spring_curve: bool = False) -> Optional[float]:
    """Current-day water-temp estimate from the last ~7 days of air temp."""
    if not recent_air_daily_f:
        return None
    p = params_for_reach(spring_influenced, spring_curve)
    window = recent_air_daily_f[-7:]
    avg = sum(window) / len(window)
    return mohseni(avg, p)
