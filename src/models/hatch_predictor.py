from math import exp, pi, sqrt
from typing import Dict


def gaussian_probability(x: float, mean: float, sd: float) -> float:
    if sd <= 0:
        return 0.0
    z = (x - mean) / sd
    return exp(-0.5 * z * z) / (sd * sqrt(2 * pi))


def peak_probability(x: float, mean: float, sd: float) -> float:
    if sd <= 0:
        return 0.0
    return exp(-0.5 * ((x - mean) / sd) ** 2)


def species_activity_probability(dd_current: float, threshold_mean: float, threshold_sd: float) -> float:
    return peak_probability(dd_current, threshold_mean, threshold_sd)


# Floor for the degree-day readiness gate. Several DD thresholds in
# species.json are coarse observed medians ("manually accepted observed
# median; literature prior superseded"), not firm literature values, so the
# gate is allowed to *dampen* a calendar-driven hatch but never erase it.
DD_GATE_FLOOR = 0.30


def dd_readiness_gate(
    dd_current: float,
    threshold_mean: float,
    threshold_sd: float,
    floor: float = DD_GATE_FLOOR,
) -> float:
    """One-sided degree-day *readiness* gate, in [floor, 1.0].

    This replaces `species_activity_probability` (a symmetric Gaussian) as the
    DD term in the forecast. The Gaussian decayed on *both* sides of the
    threshold, so a reach that had blown well past a species' degree-day
    threshold (hatch already over) scored the same low factor as a reach that
    had not yet warmed up to it (not ready). Worse, multiplying that bell by
    the seasonal-calendar bell in `seasonal.seasonal_activity` double-counted
    phenology around the same date.

    The correct division of labor: degree-days answer "has this reach actually
    accumulated enough heat for emergence to be *possible*?" and the seasonal
    calendar answers "is this the time of year, and is the season winding
    down?". So this term should rise as accumulated heat approaches the
    threshold and then *stay high* — the calendar, not DD, closes the window.

    Logistic onset centered roughly one standard deviation below the threshold
    mean (emergence begins as accumulated heat approaches the threshold and is
    essentially complete by the mean), scaled into [floor, 1.0].
    """
    if threshold_mean <= 0:
        return 1.0
    scale = max(1.0, threshold_sd) / 2.0
    center = threshold_mean - max(1.0, threshold_sd)
    # Guard the exponent against overflow for very cold reaches far below threshold.
    z = (dd_current - center) / scale
    if z < -60.0:
        logistic = 0.0
    else:
        logistic = 1.0 / (1.0 + exp(-z))
    return floor + (1.0 - floor) * logistic


def weather_match_score(preferences: Dict[str, str], cloud_cover: float, wind_mph: float) -> float:
    score = 1.0
    clouds = preferences.get("clouds")
    if clouds == "high":
        score *= _cloud_penalty(cloud_cover, prefer_high=True)
    elif clouds == "low":
        score *= _cloud_penalty(cloud_cover, prefer_high=False)
    wind_pref = preferences.get("wind")
    if wind_pref and wind_pref.startswith("<"):
        try:
            threshold = float(wind_pref.lstrip("<"))
            score *= _wind_penalty(wind_mph, threshold)
        except ValueError:
            pass
    return max(0.0, min(score, 1.0))


def _cloud_penalty(cloud_cover: float, prefer_high: bool) -> float:
    """Soft cloud ramp instead of a linear 0→1 multiplier.

    Anglers and entomologists agree: BWOs hatch in sun too — fish just rise
    less aggressively. Treating sunny days as a ~0 multiplier was killing the
    BWO score even when fish were eating BWO emergers (validated against a
    real fishing day, 4/22). Now: full credit at the preferred end, linear
    ramp through the middle, floor at 0.4 at the wrong end.
    """
    if cloud_cover is None:
        return 0.5
    cc = max(0.0, min(1.0, cloud_cover))
    if prefer_high:
        if cc >= 0.75:
            return 1.0
        if cc <= 0.25:
            return 0.4
        # Linear from 0.4 at 0.25 to 1.0 at 0.75
        return 0.4 + 1.2 * (cc - 0.25)
    else:
        if cc <= 0.25:
            return 1.0
        if cc >= 0.75:
            return 0.4
        return 1.0 - 1.2 * (cc - 0.25)


def _wind_penalty(wind_mph: float, threshold: float) -> float:
    """Soft wind ramp instead of a cliff at `threshold`.

    Literature + guide consensus: wind doesn't block emergence, it hurts the
    feeding response. Full credit under threshold, linear falloff between
    threshold and 2x threshold, floor at 0.4 beyond that (still-fishable if
    you can cast).
    """
    if wind_mph is None:
        return 0.5
    if wind_mph <= threshold:
        return 1.0
    if wind_mph >= threshold * 2:
        return 0.4
    # Linear from 1.0 at threshold to 0.4 at 2*threshold.
    return 1.0 - 0.6 * (wind_mph - threshold) / threshold
