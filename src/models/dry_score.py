from typing import Dict, List, Optional

from src.models.hatch_predictor import species_activity_probability, weather_match_score


def hour_of_day_score(hour: int, start: int, end: int) -> float:
    if start is None or end is None:
        return 0.5
    if start <= hour <= end:
        return 1.0
    return 0.3


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
    window = hour_of_day_score(valid_hour, int(species.get("emergence_hr_start") or 0), int(species.get("emergence_hr_end") or 23))
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
