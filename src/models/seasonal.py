"""
Seasonal hatch approximation for the Driftless Area.

v1 bypass for the DD-based hatch_predictor: without per-reach water-temperature
history we can't accumulate true degree-days, so we fall back to typical
MN/WI/IA emergence windows. Every reach in the region shares these dates
roughly (spring creeks can run a week or two ahead/behind).

Plan-open-question #2 — ungauged reaches get a "degraded confidence" flag;
this module is the seasonal fallback for everyone until DD calibration ships.
"""
from datetime import date
from math import exp
from typing import Dict

# peak (month, day) and sd in days. From local hatch calendars / Kiap-TU-Wish,
# Root River Rod Co., Orvis MN guides. Tuned conservative — a broad window
# beats a narrow miss.
DRIFTLESS_PEAKS: Dict[str, Dict[str, object]] = {
    # Mayflies / caddisflies / stoneflies (DD-gated)
    "early-black-stone": {"peak": (2, 25), "sd_days": 21},
    "hendrickson":     {"peak": (4, 25), "sd_days": 12},
    "bwo-spring":      {"peak": (4, 20), "sd_days": 28},
    "sulphur":         {"peak": (6,  1), "sd_days": 14},
    "grannom-caddis":  {"peak": (5,  1), "sd_days": 14},
    "tan-caddis":      {"peak": (6, 15), "sd_days": 25},
    "isonychia":       {"peak": (7, 15), "sd_days": 30},
    "trico":           {"peak": (8,  5), "sd_days": 22},
    "hex":             {"peak": (6, 30), "sd_days": 10},
    "bwo-fall":        {"peak": (10, 1), "sd_days": 28},
    # Terrestrials. Previous sd_days of 35–45 let them rank above active
    # aquatic hatches in May (validated against a real trip: model said
    # beetles, river had BWOs/sulphurs). Tightened windows + air-temp gate
    # in forecast_builder._score_hour are the joint fix.
    "hopper":          {"peak": (8, 15), "sd_days": 22},
    "ant":             {"peak": (7, 25), "sd_days": 22},
    "beetle":          {"peak": (7, 15), "sd_days": 18},
    "cricket":         {"peak": (8, 20), "sd_days": 20},
}

TERRESTRIAL_SPECIES = frozenset({"hopper", "ant", "beetle", "cricket"})


def _doy(d: date) -> int:
    return d.timetuple().tm_yday


def seasonal_activity(species_id: str, on_date: date, shift_days: float = 0.0) -> float:
    """Probability that the species is in its emergence window on `on_date`.

    `shift_days` pulls the peak earlier (negative) or later (positive) based
    on the local temperature anomaly — see `anomaly.shift_days`.
    """
    entry = DRIFTLESS_PEAKS.get(species_id)
    if not entry:
        return 0.0
    peak_month, peak_day = entry["peak"]
    sd = entry["sd_days"]
    peak_doy = _doy(date(on_date.year, peak_month, peak_day)) + shift_days
    current_doy = _doy(on_date)
    raw = abs(current_doy - peak_doy)
    delta = min(raw, 365 - raw)
    return exp(-0.5 * (delta / sd) ** 2)
