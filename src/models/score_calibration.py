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
    """Dynamic top-end cap for hours without meaningful surface signal.

    The nymph lane can report excellent subsurface reliability, but the
    headline score is a best-window promise. Without hatch/surface activity,
    broad spring-creek nymph plateaus should read as solid, not peak.
    """
    return 0.68 + 0.10 * max(0.0, min(1.0, aggression))


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


def _headline_diel_factor(score_breakdown: Any) -> float:
    """Display-lane timing factor derived from the raw nymph diel multiplier.

    Raw nymph score keeps a broad "fishable" baseline. The headline lane is
    about useful angling windows, so late night and bright midday should sit
    visibly lower than dawn/dusk even when water and flow are excellent.
    """
    diel = _breakdown_value(score_breakdown, "diel_activity", 0.91)
    if diel >= 0.98:
        return 1.00
    if diel >= 0.96:
        return 0.98
    if diel >= 0.91:
        return 0.93
    if diel >= 0.86:
        return 0.86
    return 0.74


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
    proxy_distance_km: Any = None,
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
        distance = _as_float(proxy_distance_km, -1.0)
        if distance >= 0:
            if distance <= 15:
                proxy_factor = 0.90
            elif distance <= 30:
                proxy_factor = 0.82
            elif distance <= 45:
                proxy_factor = 0.74
            else:
                proxy_factor = 0.65
            flow_label = f"proxy gauge flow context (~{distance:.0f} km)"
        else:
            proxy_factor = 0.75
            flow_label = "proxy gauge flow context"
        flow *= proxy_factor
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


def recommendation_rank_score(score: Any, confidence: Any) -> float:
    """Quality score used for ordering recommendations, with uncertainty drag.

    The displayed fishing-quality score remains separate. This rank score only
    breaks recommendation ties and nudges shaky proxy/long-lead windows below
    similarly good, better-instrumented windows.
    """
    quality = max(0.0, min(1.0, _as_float(score)))
    conf = max(0.0, min(1.0, _as_float(confidence, 0.65)))
    return quality * (0.82 + 0.18 * conf)


def score_lanes(
    nymph_score: Any,
    dry_score: Any,
    active_species: Any = None,
    regime: Any = None,
    score_breakdown: Any = None,
) -> dict[str, Any]:
    """Method-specific score lanes used by the UI and headline calibration.

    baseline_nymph answers "can you catch fish subsurface?"
    surface_window answers "is a hatch/rise/terrestrial window firing?"
    activation answers "are fish especially vulnerable right now?"
    """
    nymph = max(0.0, min(1.0, _as_float(nymph_score)))
    dry = max(0.0, min(1.0, _as_float(dry_score)))
    top_hatch = _top_species_probability(active_species)
    surface_signal = max(dry, top_hatch)
    aggression = aggression_score(nymph, dry, active_species, regime, score_breakdown)

    diel_window = _headline_diel_factor(score_breakdown)
    baseline_nymph = _compress_high_end(nymph, 0.78) * diel_window
    surface_window = _compress_high_end(surface_signal, 0.94)
    if surface_signal >= 0.15:
        surface_window = min(1.0, surface_window + 0.10 * min(nymph, surface_signal))

    # Activation is intentionally not a pure quality score. It lets short
    # low-light / drift / pressure windows stand apart from a steady nymph
    # baseline without pretending every activated hour has surface feeding.
    activation = nymph * (0.25 + 0.40 * aggression + 0.20 * diel_window)
    if surface_signal >= 0.15:
        activation = max(activation, 0.30 + 0.55 * aggression)
    activation = max(0.0, min(1.0, activation))

    code = _regime_code(regime)
    cap = None
    if code == "BLOWOUT":
        cap = 0.10
    elif code == "HEAT_STRESS":
        cap = 0.15
    if cap is not None:
        baseline_nymph = min(baseline_nymph, cap)
        surface_window = min(surface_window, cap)
        activation = min(activation, cap)

    return {
        "baseline_nymph": baseline_nymph,
        "surface_window": surface_window,
        "activation": activation,
        "surface_signal": surface_signal,
        "top_hatch_probability": top_hatch,
        "aggression": aggression,
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
    lanes = score_lanes(nymph_score, dry_score, active_species, regime, score_breakdown)
    surface_signal = lanes["surface_signal"]
    aggression = lanes["aggression"]

    if surface_signal >= 0.15:
        score = max(lanes["baseline_nymph"], lanes["surface_window"], lanes["activation"])
    else:
        # Keep nymph-only hours visibly below true hatch / activation windows.
        score = min(
            max(lanes["baseline_nymph"], lanes["activation"]),
            _nymph_only_ceiling(aggression),
        )

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
    lanes = score_lanes(nymph, dry, active_species, regime, score_breakdown)
    nymph_display = lanes["baseline_nymph"]
    dry_display = _compress_high_end(dry, 0.94)
    score = headline_score(nymph, dry, active_species, regime, score_breakdown)
    aggression = lanes["aggression"]
    source = "nymph"
    surface_signal = lanes["surface_signal"]
    if lanes["surface_window"] > nymph_display:
        source = "surface"
    if lanes["activation"] > max(nymph_display, lanes["surface_window"]):
        source = "activation"
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
        "lanes": {
            "baseline_nymph": lanes["baseline_nymph"],
            "surface_window": lanes["surface_window"],
            "activation": lanes["activation"],
        },
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
