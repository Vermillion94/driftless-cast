"""Reach-level thermal modifiers for Driftless trout streams.

This is intentionally conservative. Mohseni supplies the air→water curve; this
module only nudges that curve for reach morphology we already know from seed
data: spring influence, trout class, gradient, and short spring-creek length.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional


@dataclass(frozen=True)
class ThermalProfile:
    spring_strength: float
    warm_season_cooling_f: float
    cold_season_warming_f: float
    swing_damping: float
    diurnal_amp_factor: float
    label: str


DEFAULT_PROFILE = ThermalProfile(
    spring_strength=0.0,
    warm_season_cooling_f=0.0,
    cold_season_warming_f=0.0,
    swing_damping=0.0,
    diurnal_amp_factor=1.0,
    label="class-level thermal model",
)


def _float(row: Mapping[str, object], key: str, default: float = 0.0) -> float:
    try:
        return float(row.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def from_reach(reach: Mapping[str, object]) -> ThermalProfile:
    """Build a reach-level modifier from existing seed metadata."""
    if not bool(reach.get("spring_influenced")):
        return DEFAULT_PROFILE

    trout_class = str(reach.get("trout_class") or "").lower()
    gradient = _float(reach, "mean_gradient")
    length = _float(reach, "length_km")

    strength = 0.45
    if "i" in trout_class and "ii" not in trout_class and "iii" not in trout_class:
        strength += 0.18
    strength += max(0.0, min(0.22, (gradient - 3.0) / 3.0 * 0.22))
    if 0 < length <= 8.0:
        strength += 0.10
    strength = max(0.0, min(1.0, strength))

    return ThermalProfile(
        spring_strength=strength,
        warm_season_cooling_f=1.8 * strength,
        cold_season_warming_f=0.8 * strength,
        swing_damping=0.16 * strength,
        diurnal_amp_factor=1.0 - 0.35 * strength,
        label=f"spring-creek thermal damping ({strength:.2f})",
    )


def apply_profile(daily_mean_f: Optional[float], profile: ThermalProfile) -> Optional[float]:
    if daily_mean_f is None:
        return None
    if profile.spring_strength <= 0:
        return daily_mean_f
    anchor = 50.0
    damped = anchor + (daily_mean_f - anchor) * (1.0 - profile.swing_damping)
    if daily_mean_f >= anchor:
        return damped - profile.warm_season_cooling_f
    return damped + profile.cold_season_warming_f


def apply_profile_series(series_f: list[float], profile: ThermalProfile) -> list[float]:
    return [float(apply_profile(t, profile)) for t in series_f]
