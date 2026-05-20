"""Display-score calibration.

The component models answer narrower questions:
  * nymph_score: are subsurface conditions workable?
  * dry_score: is a surface/hatch window firing?

The headline app score is a user promise. A nymph-only 1.0 should mean
"excellent nymphing conditions," not "fish are going wild." This module
compresses broad nymph plateaus and reserves the top end for windows where
multiple signals line up.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _loads_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _compress_high_end(score: float, ceiling: float) -> float:
    """Leave low/mid scores alone, squeeze the saturated top band."""
    s = max(0.0, min(1.0, score))
    knee = 0.65
    if s <= knee:
        return s
    return knee + (s - knee) * ((ceiling - knee) / (1.0 - knee))


def _nymph_only_ceiling(aggression: float) -> float:
    """Dynamic top-end cap for hours without meaningful surface signal."""
    return 0.76 + 0.06 * max(0.0, min(1.0, aggression))


def _top_species_probability(active_species: Any) -> float:
    rows = _loads_json(active_species) or []
    if not isinstance(rows, Iterable):
        return 0.0
    best = 0.0
    for row in rows:
        if isinstance(row, Mapping):
            best = max(best, _as_float(row.get("probability")))
    return best


def _regime_code(regime: Any) -> str | None:
    row = _loads_json(regime)
    if isinstance(row, Mapping):
        code = row.get("code")
        return str(code) if code is not None else None
    return None


def _breakdown_value(score_breakdown: Any, key: str, default: float) -> float:
    row = _loads_json(score_breakdown)
    if isinstance(row, Mapping):
        return _as_float(row.get(key), default)
    return default


def aggression_score(
    nymph_score: Any,
    dry_score: Any,
    active_species: Any = None,
    regime: Any = None,
    score_breakdown: Any = None,
) -> float:
    """0..1 estimate of how *activated* fish are likely to be.

    This is intentionally not the same as "can you catch fish?" Comfortable
    water and normal flow create baseline opportunity. Aggression needs change
    or vulnerability: hatch/surface readiness, falling pressure, favorable
    drift/flow trend, and low-light/cloud protection.

    Evidence strength varies by factor:
      - surface signal: entomology + modelled hatch readiness
      - flow trend: invertebrate drift / post-event stabilization literature
      - light protection: well-supported fish-behavior mechanism, exact weight heuristic
      - pressure: angler-consensus heuristic, weak trout-specific literature
    """
    nymph = max(0.0, min(1.0, _as_float(nymph_score)))
    dry = max(0.0, min(1.0, _as_float(dry_score)))
    surface = max(dry, _top_species_probability(active_species))

    flow_trend = max(0.70, min(1.0, _breakdown_value(score_breakdown, "flow_trend", 0.85)))
    flow_change = (flow_trend - 0.70) / 0.30

    pressure = _breakdown_value(score_breakdown, "pressure_factor", 1.0)
    if pressure >= 1.05:
        pressure_change = 1.0
    elif pressure >= 1.02:
        pressure_change = 0.75
    elif pressure >= 1.0:
        pressure_change = 0.45
    elif pressure >= 0.92:
        pressure_change = 0.15
    else:
        pressure_change = 0.05

    sun_factor = max(0.65, min(1.0, _breakdown_value(score_breakdown, "sun_factor", 1.0)))
    light_protection = (sun_factor - 0.65) / 0.35

    # Surface activity should dominate aggression. Nymphing can be productive
    # on low aggression days, but "hot" usually needs bugs/risers or a strong
    # change signal.
    surface_component = min(1.0, surface / 0.35)
    score = (
        0.45 * surface_component
        + 0.20 * flow_change
        + 0.20 * pressure_change
        + 0.15 * light_protection
    )
    score *= 0.60 + 0.40 * nymph

    code = _regime_code(regime)
    if code == "BLOWOUT":
        score = min(score, 0.10)
    elif code == "HEAT_STRESS":
        score = min(score, 0.15)

    return max(0.0, min(1.0, score))


def _hours_between(start_iso: Any, end_iso: Any) -> float | None:
    if not start_iso or not end_iso:
        return None
    try:
        start = datetime.fromisoformat(str(start_iso).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0.0, (end - start).total_seconds() / 3600.0)


def confidence_score(
    valid_at: Any,
    computed_at: Any,
    water_temp_source: Any,
    gauge_is_proxy: Any = False,
    score_breakdown: Any = None,
) -> dict[str, Any]:
    """How much trust to put in this hour's score inputs.

    Confidence is not fishing quality. It reports input quality: measured vs
    estimated water temp, real vs proxy flow context, forecast lead time, and
    whether auxiliary forecast signals are present.
    """
    source = str(water_temp_source or "").lower()
    if source == "gauge":
        temp = 1.0
        temp_label = "measured water temp"
    elif source == "estimate":
        temp = 0.72
        temp_label = "estimated water temp"
    else:
        temp = 0.35
        temp_label = "no water-temp signal"

    percentile = _breakdown_value(score_breakdown, "percentile_used", -1.0)
    flow = 1.0 if percentile >= 0 else 0.55
    if bool(gauge_is_proxy):
        flow *= 0.75
        flow_label = "proxy gauge flow context"
    else:
        flow_label = "local gauge flow context" if percentile >= 0 else "limited flow context"

    pressure_factor = _breakdown_value(score_breakdown, "pressure_factor", -1.0)
    pressure = 1.0 if pressure_factor >= 0 else 0.70

    lead_h = _hours_between(computed_at, valid_at)
    if lead_h is None:
        lead = 0.65
    elif lead_h <= 24:
        lead = 1.0
    elif lead_h <= 72:
        lead = 0.85
    elif lead_h <= 120:
        lead = 0.68
    else:
        lead = 0.52

    score = 0.35 * temp + 0.30 * flow + 0.20 * lead + 0.15 * pressure
    return {
        "score": max(0.0, min(1.0, score)),
        "temperature": temp,
        "flow": flow,
        "lead_time": lead,
        "pressure": pressure,
        "lead_hours": round(lead_h, 1) if lead_h is not None else None,
        "notes": [temp_label, flow_label],
    }


def headline_score(
    nymph_score: Any,
    dry_score: Any,
    active_species: Any = None,
    regime: Any = None,
    score_breakdown: Any = None,
) -> float:
    """Calibrated 0..1 score shown to anglers.

    Nymph-only hours are intentionally capped below "drop everything" territory.
    A real hatch/surface signal can outrank them, and a window where both
    subsurface and surface signals are strong gets a small alignment bonus.
    """
    nymph = max(0.0, min(1.0, _as_float(nymph_score)))
    dry = max(0.0, min(1.0, _as_float(dry_score)))
    top_hatch = _top_species_probability(active_species)

    nymph_display = _compress_high_end(nymph, 0.84)
    dry_display = _compress_high_end(dry, 0.93)
    score = max(nymph_display, dry_display)

    surface_signal = max(dry, top_hatch)
    aggression = aggression_score(nymph, dry, active_species, regime, score_breakdown)
    if surface_signal >= 0.15:
        # Rise activity is the scarce signal users are paying to find. A weak
        # hatch should not make a day "electric", but once surface probability
        # clears the significance threshold used in forecast_builder, let it
        # separate an otherwise flat nymph plateau. This is a product
        # calibration heuristic; the surface signal itself is the entomology.
        score += 0.12 * min(nymph, surface_signal)

    if aggression >= 0.70:
        # A short window with change stacked in its favor should stand out
        # from a flat "comfortable nymphing" plateau.
        score += 0.06 * (aggression - 0.70) / 0.30

    # A nymph-only plateau can be a good day, but it should not look like a
    # boiling-rises day unless the surface model agrees. The cap is dynamic so
    # good but soft nymphing does not tie with a change-stacked nymph window.
    if surface_signal < 0.15:
        score = min(score, _nymph_only_ceiling(aggression))

    code = _regime_code(regime)
    if code == "BLOWOUT":
        score = min(score, 0.10)
    elif code == "HEAT_STRESS":
        score = min(score, 0.15)

    return max(0.0, min(1.0, score))


def headline_breakdown(
    nymph_score: Any,
    dry_score: Any,
    active_species: Any = None,
    regime: Any = None,
    score_breakdown: Any = None,
) -> dict[str, Any]:
    """Human/audit-facing details for the headline score."""
    nymph = max(0.0, min(1.0, _as_float(nymph_score)))
    dry = max(0.0, min(1.0, _as_float(dry_score)))
    top_hatch = _top_species_probability(active_species)
    nymph_display = _compress_high_end(nymph, 0.84)
    dry_display = _compress_high_end(dry, 0.93)
    score = headline_score(nymph, dry, active_species, regime, score_breakdown)
    aggression = aggression_score(nymph, dry, active_species, regime, score_breakdown)
    source = "nymph"
    if dry_display > nymph_display:
        source = "dry"
    surface_signal = max(dry, top_hatch)
    if surface_signal < 0.15 and score <= _nymph_only_ceiling(aggression):
        source = "nymph_capped"
    code = _regime_code(regime)
    if code in {"BLOWOUT", "HEAT_STRESS"}:
        source = code.lower()
    return {
        "score": score,
        "source": source,
        "nymph_raw": nymph,
        "dry_raw": dry,
        "nymph_display": nymph_display,
        "dry_display": dry_display,
        "top_hatch_probability": top_hatch,
        "surface_signal": surface_signal,
        "aggression": aggression,
        "aggression_factors": {
            "surface": surface_signal,
            "flow_change": round((max(0.70, min(1.0, _breakdown_value(score_breakdown, "flow_trend", 0.85))) - 0.70) / 0.30, 3),
            "pressure_factor": _breakdown_value(score_breakdown, "pressure_factor", 1.0),
            "light_protection": round((max(0.65, min(1.0, _breakdown_value(score_breakdown, "sun_factor", 1.0))) - 0.65) / 0.35, 3),
        },
        "alignment_bonus_possible": surface_signal >= 0.15,
        "evidence": [
            "water_temp_zone",
            "flow_percentile",
            "flow_trend",
            "degree_days",
            "weather_match",
            "emergence_hour",
            "barometric_pressure",
            "sun_angle",
        ],
    }
