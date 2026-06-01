from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from src.models.hatch_predictor import species_activity_probability, weather_match_score
from src.models.solar import sun_altitude_deg


def hour_of_day_score(hour: int, start: int, end: int) -> float:
    """Trapezoid: full credit inside [start, end], ramp 2h either side.

    Real emergence is not an on/off switch, but surface feeding is a short
    behavioral event. The previous 0.30 all-day floor made a species that was
    seasonally plausible add a broad dry-fly signal even at dawn/midnight,
    which flattened the forecast. Use a low 0.05 background for stray adults
    and a 2h shoulder around the actual emergence/spinner window.
    """
    BACKGROUND = 0.05
    if start is None or end is None:
        return 0.5
    if start <= hour <= end:
        return 1.0
    if hour < start:
        gap = start - hour
    else:
        gap = hour - end
    if gap >= 2:
        return BACKGROUND
    return BACKGROUND + (1.0 - BACKGROUND) * (1.0 - gap / 2.0)


def _species_id(species: Dict[str, object] | str | None) -> str:
    if isinstance(species, dict):
        return str(species.get("species_id") or "")
    return str(species or "")


def _window_profile(species: Dict[str, object] | str | None) -> str:
    if isinstance(species, dict):
        profile = str(species.get("timing_profile") or "").strip()
        if profile:
            return profile
    sp_id = _species_id(species)
    fallback = {
        "sulphur": "evening_mayfly",
        "isonychia": "evening_mayfly",
        "tan-caddis": "dusk_caddis",
        "grannom-caddis": "dusk_caddis",
        "hex": "night",
        "trico": "morning",
        "early-black-stone": "crawler",
    }
    return fallback.get(sp_id, "default")


def shift_window_for_air_temp(start: int, end: int, air_temp_f: float | None, species: Dict[str, object] | str | None = None) -> tuple[int, int]:
    """Hot days push hatches into evening — slide the window later.

    Driftless guide consensus: above ~80°F air, bugs delay emergence; above
    ~88°F it's often a 2–3h shift entirely into the cool of evening. Pinning
    a sulphur to 1pm on a 95°F day is the bug behind the user's "0 action
    until 6pm" report.
    """
    if air_temp_f is None or air_temp_f < 78:
        return start, end
    profile = _window_profile(species)
    if air_temp_f >= 92:
        shift = 4
    elif air_temp_f >= 88:
        shift = 3
    elif air_temp_f >= 84:
        shift = 2
    elif air_temp_f >= 80:
        shift = 1
    else:
        shift = 0

    if profile == "night":
        shift = min(shift + 1, 4)
        compress = 1
    elif profile in {"dusk_caddis", "evening_mayfly"}:
        compress = 1 if air_temp_f >= 84 else 0
        if air_temp_f >= 90:
            compress += 1
    elif profile == "morning":
        shift = max(0, shift - 1)
        compress = 0
    else:
        compress = 0

    shifted_start = min(start + shift, 23)
    shifted_end = min(end + shift, 23)
    if compress > 0 and shifted_end > shifted_start:
        shifted_start = min(shifted_start + compress // 2, shifted_end)
        shifted_end = max(shifted_start, shifted_end - (compress - compress // 2))
    return shifted_start, shifted_end


def solar_timing_factor(species: Dict[str, object] | str | None, sun_alt_deg: float) -> float:
    """Species-specific low-light timing multiplier.

    Clock hours alone miss a lot in late spring and summer. Evening caddis and
    PMD/sulphur-type mayflies routinely bunch around low-light periods, and
    some activity carries into after-dark. This keeps midday from reading like
    the same surface opportunity as dusk on otherwise similar days.
    """
    profile = _window_profile(species)
    if profile == "night":
        if -12 <= sun_alt_deg <= 4:
            return 1.25
        if -18 <= sun_alt_deg < -12 or 4 < sun_alt_deg <= 12:
            return 1.0
        if sun_alt_deg > 30:
            return 0.45
        return 0.75
    if profile == "dusk_caddis":
        if -10 <= sun_alt_deg <= 6:
            return 1.20
        if -16 <= sun_alt_deg < -10 or 6 < sun_alt_deg <= 16:
            return 1.0
        if sun_alt_deg > 40:
            return 0.55
        return 0.80
    if profile in {"evening_mayfly", "crawler"}:
        if -6 <= sun_alt_deg <= 10:
            return 1.15
        if -12 <= sun_alt_deg < -6 or 10 < sun_alt_deg <= 22:
            return 1.0
        if sun_alt_deg > 45:
            return 0.60
        return 0.82
    if profile == "morning":
        if 4 <= sun_alt_deg <= 18:
            return 1.15
        if 18 < sun_alt_deg <= 30:
            return 1.0
        if sun_alt_deg < -2:
            return 0.45
        if sun_alt_deg > 45:
            return 0.65
        return 0.85
    return 1.0


def species_window_score(
    species: Dict[str, object] | str | None,
    valid_hour: int,
    start: int,
    end: int,
    air_temp_f: float | None = None,
    lat: float | None = None,
    lon: float | None = None,
    valid_at: datetime | None = None,
) -> tuple[float, tuple[int, int], float]:
    shifted_start, shifted_end = shift_window_for_air_temp(start, end, air_temp_f, species)
    window = hour_of_day_score(valid_hour, shifted_start, shifted_end)
    solar_factor = 1.0
    if valid_at is not None and lat is not None and lon is not None:
        solar_factor = solar_timing_factor(species, sun_altitude_deg(lat, lon, valid_at))
    return max(0.0, min(1.0, window * solar_factor)), (shifted_start, shifted_end), solar_factor


def compute_species_dry_score(
    dd_current: float,
    species: Dict[str, object],
    cloud_cover: Optional[float],
    wind_mph: Optional[float],
    water_temp_f: Optional[float],
    valid_hour: int,
) -> float:
    threshold_mean = float(species.get("dd_threshold_mean", 0.0))
    threshold_sd = float(species.get("dd_threshold_sd", 1.0))
    weather_prefs = species.get("weather_prefs") or {}
    activity = species_activity_probability(dd_current, threshold_mean, threshold_sd)
    weather_score = weather_match_score(weather_prefs, cloud_cover or 0.5, wind_mph or 10.0)
    window, _emergence_window, _solar_factor = species_window_score(
        species=species,
        valid_hour=valid_hour,
        start=int(species.get("emergence_hr_start") or 0),
        end=int(species.get("emergence_hr_end") or 23),
    )
    if water_temp_f is not None and water_temp_f < 45:
        return 0.0
    return max(0.0, min(1.0, activity * weather_score * window))


def compute_dry_score(
    dd_current: float,
    species_list: List[Dict[str, object]],
    cloud_cover: Optional[float],
    wind_mph: Optional[float],
    water_temp_f: Optional[float],
    valid_hour: int,
) -> Dict[str, object]:
    scores = []
    active_species = []
    for species in species_list:
        score = compute_species_dry_score(dd_current, species, cloud_cover, wind_mph, water_temp_f, valid_hour)
        if score > 0:
            active_species.append({"id": species["species_id"], "probability": score})
        scores.append(score)
    return {
        "dry_score": max(scores) if scores else 0.0,
        "active_species": active_species,
    }
