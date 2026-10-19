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
    # Peaks/widths from the MN DNR SE Minnesota hatch chart (Snook, Lanesboro),
    # cross-checked against Driftless Angler daily-report field dates 2024-26
    # and Coulee Region TU (Freund). See docs/FIELD_GUIDE_REVIEW.md.
    # `floor` keeps year-round foods (midges) from ever reading as absent.
    "midge":             {"peak": (1, 25), "sd_days": 55, "floor": 0.35},
    "early-black-stone": {"peak": (2, 25), "sd_days": 28},   # Jan - Apr
    "bwo-spring":        {"peak": (4, 10), "sd_days": 22},   # mid-Mar - mid-May
    "hendrickson":       {"peak": (4, 18), "sd_days": 10},   # early/mid - late Apr
    "grannom-caddis":    {"peak": (4, 25), "sd_days": 13},   # mid-Apr (WI) - May (MN)
    "cranefly":          {"peak": (6,  1), "sd_days": 24},   # late Apr - Jul
    "sulphur":           {"peak": (6,  5), "sd_days": 18},   # ~May 20 - early Jul
    "tan-caddis":        {"peak": (6, 10), "sd_days": 30},   # early May - Jul, sparse to Oct
    "cahill":            {"peak": (6, 20), "sd_days": 16},   # Jun - mid-Jul
    "isonychia":         {"peak": (7, 15), "sd_days": 30},   # minor/absent (weighted ~0)
    "hex":               {"peak": (6, 30), "sd_days": 10},   # minor/absent (weighted ~0)
    "tiny-olive":        {"peak": (8,  1), "sd_days": 35},   # mid-Jun - Sep
    "trico":             {"peak": (8, 10), "sd_days": 22},   # ~Jul 20 - mid/late Sep
    "bwo-fall":          {"peak": (10, 1), "sd_days": 20},   # mid-Sep - Oct
    "autumn-sedge":      {"peak": (10, 5), "sd_days": 12},   # October
    # Terrestrials: windows from the MN DNR chart (ants/beetles mid-Apr - Sep,
    # hoppers mid-Jul - Sep) and DA reports (hopper-dropper from ~Jul 18,
    # still working early Oct). Their *weight* is limited separately in
    # forecast_builder (TERRESTRIAL_SURFACE_WEIGHT, air-temp gate, aquatic
    # priority) - the earlier May over-call was a weighting problem.
    "beetle":            {"peak": (7,  1), "sd_days": 30},
    "ant":               {"peak": (7, 20), "sd_days": 30},
    "hopper":            {"peak": (8, 20), "sd_days": 25},
    "cricket":           {"peak": (8, 25), "sd_days": 20},
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
    return max(float(entry.get("floor", 0.0)), exp(-0.5 * (delta / sd) ** 2))
