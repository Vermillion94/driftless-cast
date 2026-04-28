from datetime import datetime, timedelta
from typing import Dict, Iterable, List


def temperature_score(temp_f: float) -> float:
    """Plateau model anchored to the literature on brown-trout feeding range.

    Replaces the previous Gaussian (peak 58°F, σ=6) which collapsed to ~0.5 at
    51°F — a temperature where Driftless spring-creek browns are actively
    feeding. The Gaussian disagreed with both Wehrly et al. (2007) and our own
    `_format_water_clause` copy, which both treat 50–65°F as the active band.

    Curve:
      < 42°F           → 0.0   (lethargic, refusing presentations)
      42°F → 52°F      → linear ramp 0 → 1
      52°F → 64°F      → 1.0   (peak feeding zone)
      64°F → 68°F      → linear ramp 1.0 → 0.5  (warm but still feeding)
      68°F → 75°F      → linear ramp 0.5 → 0    (stress; ethics threshold ≥68°F)
      ≥ 75°F           → 0.0   (lethal-stress zone)

    Sources: docs/REFERENCES.md#wehrly_2007 (brown trout active feeding 50–65°F),
    #elliott_1981 (thermal stress), #wilkie_1996 (catch-and-release mortality
    above 68°F).
    """
    if temp_f is None:
        return 0.0
    t = float(temp_f)
    if t < 42.0:
        return 0.0
    if t < 52.0:
        return (t - 42.0) / 10.0
    if t <= 64.0:
        return 1.0
    if t <= 68.0:
        return 1.0 - (t - 64.0) * (0.5 / 4.0)  # 64→1.0, 68→0.5
    if t <= 75.0:
        return 0.5 - (t - 68.0) * (0.5 / 7.0)  # 68→0.5, 75→0
    return 0.0


def flow_percentile_score(percentile: float) -> float:
    """Asymmetric — low flow is fishable; high flow is genuinely problematic.

    Trout streams often fish well at low flow (clear water, concentrated fish,
    easy presentations). The old symmetric cliff at <20th pct was killing
    nymph scores on small Driftless creeks running normally low for the date.

    High flow is steeper because turbidity, dangerous wading, and fish hugging
    cover all degrade the experience together.
    """
    if percentile is None:
        return 0.5
    p = max(0.0, min(1.0, percentile))
    # Plateau at peak fishing percentile range (35–50th pct)
    if 0.35 <= p <= 0.50:
        return 1.0
    if p < 0.35:
        # Gentle linear: 1.0 at 0.35, 0.55 at 0.0
        return max(0.55, 0.55 + (p / 0.35) * 0.45)
    # p > 0.50 — high flow penalty starts mild, gets steep past 80th
    if p <= 0.80:
        return 1.0 - (p - 0.50)              # 0.50→1.0, 0.80→0.70
    if p <= 0.95:
        return 0.70 - (p - 0.80) * (0.40 / 0.15)  # 0.80→0.70, 0.95→0.30
    return 0.20


def flow_trend_score(recent_values: Iterable[float]) -> float:
    recent = list(recent_values)
    if len(recent) < 2:
        return 0.75
    if recent[-1] < recent[0]:
        return 1.0
    return 0.6


def drift_window_bonus(valid_at: str) -> float:
    try:
        dt = datetime.fromisoformat(valid_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    hour = dt.hour
    if hour in {5, 6, 19, 20}:
        return 0.1
    return 0.0


def prehatch_bonus(dd_current: float, thresholds: Iterable[float]) -> float:
    bonuses = [0.1 for threshold in thresholds if threshold - 100 <= dd_current < threshold]
    return max(bonuses) if bonuses else 0.0


def compute_nymph_score(
    temp_f: float,
    flow_percentile: float,
    recent_flows: Iterable[float],
    valid_at: str,
    dd_current: float,
    species_thresholds: Iterable[float],
) -> float:
    temp_score = temperature_score(temp_f)
    flow_score = flow_percentile_score(flow_percentile)
    trend_score = flow_trend_score(recent_flows)
    bonus = drift_window_bonus(valid_at) + prehatch_bonus(dd_current, species_thresholds)
    score = temp_score * flow_score * trend_score + bonus
    return max(0.0, min(1.0, score))
