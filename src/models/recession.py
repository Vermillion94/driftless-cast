"""Flow-recession calibration used by forecast and backtest code.

The production forecast uses the same exponential form we validate in the
backtest: current discharge decays toward the day-of-year median. Class priors
are the fallback; per-reach fits are loaded from data/calibration when they
clear the sample-size and improvement gates in the fitting script.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Tuple

TAU_FREESTONE_H = 30.0
TAU_SPRING_H = 48.0
MIN_USABLE_N = 90

CALIBRATION_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "data"
    / "calibration"
    / "recession_fit.json"
)


def class_prior_tau_hours(spring_influenced: bool) -> float:
    return TAU_SPRING_H if spring_influenced else TAU_FREESTONE_H


def project_flow(q_now: float, q_med: float, tau_hours: float, hours_ahead: float) -> float:
    """Project discharge toward the day-of-year median with exponential decay."""
    tau = max(float(tau_hours), 1.0)
    return q_med + (q_now - q_med) * math.exp(-max(0.0, hours_ahead) / tau)


@lru_cache(maxsize=1)
def _load_calibration() -> Dict[str, object]:
    if not CALIBRATION_PATH.exists():
        return {}
    with CALIBRATION_PATH.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return payload if isinstance(payload, dict) else {}


def calibrated_tau_hours(
    reach_id: str,
    gauge_id: Optional[str],
    spring_influenced: bool,
) -> Tuple[float, str, Optional[Dict[str, object]]]:
    """Return tau hours plus source label and optional fit metadata."""
    default_tau = class_prior_tau_hours(spring_influenced)
    payload = _load_calibration()
    fits = payload.get("fits") if isinstance(payload, dict) else None
    by_reach = fits if isinstance(fits, dict) else {}
    fit = by_reach.get(reach_id)
    if not isinstance(fit, dict):
        return default_tau, "class_prior", None
    if gauge_id and fit.get("gauge_id") and str(fit.get("gauge_id")) != str(gauge_id):
        return default_tau, "class_prior", None
    try:
        n = int(fit.get("n", 0))
        tau = float(fit["tau_hours"])
    except (KeyError, TypeError, ValueError):
        return default_tau, "class_prior", None
    if n < MIN_USABLE_N or tau <= 0:
        return default_tau, "class_prior", None
    return tau, "per_gauge_fit", fit
