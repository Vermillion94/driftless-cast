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


def headline_score(
    nymph_score: Any,
    dry_score: Any,
    active_species: Any = None,
    regime: Any = None,
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

    if dry >= 0.30 or top_hatch >= 0.30:
        score += 0.08 * min(nymph, max(dry, top_hatch))

    # A nymph-only plateau can be a good day, but it should not look like a
    # boiling-rises day unless the surface model agrees.
    if dry < 0.15 and top_hatch < 0.15:
        score = min(score, 0.82)

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
) -> dict[str, Any]:
    """Human/audit-facing details for the headline score."""
    nymph = max(0.0, min(1.0, _as_float(nymph_score)))
    dry = max(0.0, min(1.0, _as_float(dry_score)))
    top_hatch = _top_species_probability(active_species)
    nymph_display = _compress_high_end(nymph, 0.84)
    dry_display = _compress_high_end(dry, 0.93)
    score = headline_score(nymph, dry, active_species, regime)
    source = "nymph"
    if dry_display > nymph_display:
        source = "dry"
    if dry < 0.15 and top_hatch < 0.15 and score <= 0.82:
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
        "alignment_bonus_possible": dry >= 0.30 or top_hatch >= 0.30,
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
