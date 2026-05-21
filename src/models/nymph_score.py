from datetime import datetime, timedelta
from typing import Dict, Iterable, List
from zoneinfo import ZoneInfo


DRIFTLESS_TZ = ZoneInfo("America/Chicago")


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
    """Soft graded curve over fractional flow change across the window.

    Heuristic — guide-derived, weak peer-reviewed basis. The angler folklore
    "fish bite on falling water, scatter on rising water" is widely shared but
    controlled studies don't strongly back it for *feeding rate*:
      - Korman et al. (2026, Pol. J. Ecol.): downramping had no detectable
        short-term effect on brown-trout-fry drift feeding under adequate prey.
      - Greenberg 1992 (Reg. Rivers): reduced discharge displaces fish to
        less-shallow habitat with more competition, not direct feeding loss.
      - Higgins-Auvil 2024 (STOTEN): hydropeaking induces lateral relocation
        but fish resume feeding from the new position.
    The direction (rising worse than falling) is consistent with habitat
    displacement; the *magnitude* should be modest. We keep the influence to
    the [0.70, 1.0] band rather than the previous binary [0.60, 1.0] cliff,
    and label this clearly as a heuristic in code and REFERENCES.md.
    """
    recent = list(recent_values)
    if len(recent) < 2:
        return 0.75
    delta = recent[-1] - recent[0]
    ref = max(abs(recent[0]), 1.0)
    pct_change = delta / ref
    if pct_change <= -0.15:
        return 1.0
    if pct_change <= 0.0:
        # Linear ramp: 0% change → 0.85, -15% → 1.0.
        return 0.85 + (-pct_change / 0.15) * 0.15
    if pct_change <= 0.30:
        # Linear ramp: 0% → 0.85, +30% → 0.70.
        return 0.85 - (pct_change / 0.30) * 0.15
    return 0.70


def _local_hour(valid_at: str) -> int:
    try:
        dt = datetime.fromisoformat(valid_at.replace("Z", "+00:00"))
    except ValueError:
        return 12
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=DRIFTLESS_TZ)
    return dt.astimezone(DRIFTLESS_TZ).hour


def drift_window_bonus(valid_at: str) -> float:
    hour = _local_hour(valid_at)
    if hour in {6, 7, 19, 20}:
        return 0.08
    if hour in {5, 8, 18, 21}:
        return 0.04
    return 0.0


def diel_activity_factor(valid_at: str) -> float:
    """Local-time feeding rhythm for nymph/streamer opportunity.

    Water temperature and flow remain the hard gates. This factor only breaks
    the broad "comfortable water + normal flow" plateau into practical fishing
    windows: low-light morning/evening periods get the best drift/cover
    advantage, overnight is workable but less visually/presentation friendly,
    and high midday light is slightly damped.

    The mechanism is trout light sensitivity and crepuscular feeding behavior;
    the exact magnitude is intentionally small because it is a behavioral
    product calibration, not a lab-derived constant.
    """
    hour = _local_hour(valid_at)
    if hour in {6, 7, 19, 20}:
        return 1.00
    if hour in {5, 8, 18, 21}:
        return 0.96
    if 11 <= hour <= 15:
        return 0.86
    if hour <= 3 or hour >= 23:
        return 0.82
    return 0.91


def drift_window_bonus_utc_legacy(valid_at: str) -> float:
    """Kept only as a readable comparison for old backtest notebooks."""
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
    diel = diel_activity_factor(valid_at)
    bonus = drift_window_bonus(valid_at) + prehatch_bonus(dd_current, species_thresholds)
    score = temp_score * flow_score * trend_score * diel + bonus
    return max(0.0, min(1.0, score))
