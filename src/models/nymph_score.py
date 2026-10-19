from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo


DRIFTLESS_TZ = ZoneInfo("America/Chicago")


# (water °F, score) knots, linearly interpolated. See temperature_score.
TEMPERATURE_KNOTS = (
    (32.0, 0.25),
    (36.0, 0.35),
    (40.0, 0.50),
    (45.0, 0.70),
    (50.0, 0.90),
    (52.0, 1.00),
    (62.0, 1.00),
    (65.0, 0.85),
    (68.0, 0.55),
    (73.0, 0.00),
)


def temperature_score(temp_f: float) -> float:
    """Brown-trout feeding opportunity vs water temperature, 0..1.

    Piecewise-linear over TEMPERATURE_KNOTS:
      32→36°F  0.25→0.35  cold: slow, deep, small flies — but trout DO feed
      36→52°F  ramps to 1.0 as intake rises roughly exponentially with temp
      52→62°F  1.0        peak feeding / growth band
      62→68°F  1.0→0.55   warm side; ≥66°F is the Keep Fish Wet brown-trout
                          angling threshold where C&R mortality risk climbs
      68→73°F  0.55→0     stress; ≥73°F is a hard stop (MT FWP hoot-owl)

    The previous curve returned 0 below 42°F, which zeroed every Driftless
    winter hour: USGS daily records on Driftless creeks (Joos Valley, Eagle,
    Nederlo, Traverse Valley) average 34–38°F in Dec–Feb, yet the SE MN winter
    creel (Snook & Dieterman, Jan–Mar 2013) measured ~1.4 trout/angler-hour.
    Elliott & Hurley's bioenergetics show feeding down to 0–1°C with growth
    from ~3.5°C (38°F), so cold water is a *slow* bite, not *no* bite.

    Sources: docs/REFERENCES.md#elliott_hurley_1999, #snook_dieterman_2013,
    #keep_fish_wet_2024, #wehrly_2007.
    """
    if temp_f is None:
        return 0.0
    t = float(temp_f)
    knots = TEMPERATURE_KNOTS
    if t <= knots[0][0]:
        return knots[0][1]
    if t >= knots[-1][0]:
        return 0.0
    for (t0, s0), (t1, s1) in zip(knots, knots[1:]):
        if t0 <= t <= t1:
            return s0 + (s1 - s0) * (t - t0) / (t1 - t0)
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


# Below COLD_DIEL_F the feeding window is the warmest part of the day; above
# WARM_DIEL_F it is the low-light dawn/dusk drift peak; in between we blend.
COLD_DIEL_F = 48.0
WARM_DIEL_F = 56.0


def _warm_weight(water_temp_f: Optional[float]) -> float:
    """0 = cold-season (midday) rhythm, 1 = warm-season (dawn/dusk) rhythm."""
    if water_temp_f is None:
        return 1.0
    t = float(water_temp_f)
    if t <= COLD_DIEL_F:
        return 0.0
    if t >= WARM_DIEL_F:
        return 1.0
    return (t - COLD_DIEL_F) / (WARM_DIEL_F - COLD_DIEL_F)


def _warm_diel(hour: int) -> float:
    if hour in {6, 7, 19, 20}:
        return 1.00
    if hour in {5, 8, 18, 21}:
        return 0.96
    if 11 <= hour <= 15:
        return 0.86
    if hour <= 3 or hour >= 23:
        return 0.82
    return 0.91


def _cold_diel(hour: int) -> float:
    """Cold water: the 10am–3pm warm-up is the window (Driftless Angler daily
    reports, TCTU winter guidance: "fish 10–2 during rising temperatures").
    Dawn is the coldest water of the day; after dark is a non-starter."""
    if 11 <= hour <= 15:
        return 1.00
    if hour in {10, 16}:
        return 0.94
    if hour in {9, 17}:
        return 0.84
    if hour in {8, 18}:
        return 0.74
    if 5 <= hour <= 20:
        return 0.66
    return 0.55


def drift_window_bonus(valid_at: str, water_temp_f: Optional[float] = None) -> float:
    """Dawn/dusk behavioral-drift bonus (Waters 1972). Fades out in cold water,
    where trout are not feeding hard at the coldest hours of the day."""
    hour = _local_hour(valid_at)
    if hour in {6, 7, 19, 20}:
        bonus = 0.08
    elif hour in {5, 8, 18, 21}:
        bonus = 0.04
    else:
        bonus = 0.0
    return bonus * _warm_weight(water_temp_f)


def diel_activity_factor(valid_at: str, water_temp_f: Optional[float] = None) -> float:
    """Local-time feeding rhythm for nymph/streamer opportunity.

    Water temperature and flow remain the hard gates. This factor breaks the
    broad "comfortable water + normal flow" plateau into practical windows.

    Season-aware: in warm water (≥56°F) low-light dawn/dusk periods carry the
    drift peak and bright midday is damped; in cold water (≤48°F) the midday
    warm-up is prime and dawn/dusk are the coldest, slowest hours. A single
    year-round dawn/dusk curve made the model recommend 7pm on January nights.
    """
    hour = _local_hour(valid_at)
    w = _warm_weight(water_temp_f)
    return w * _warm_diel(hour) + (1.0 - w) * _cold_diel(hour)


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
    diel = diel_activity_factor(valid_at, temp_f)
    bonus = drift_window_bonus(valid_at, temp_f) + prehatch_bonus(dd_current, species_thresholds)
    score = temp_score * flow_score * trend_score * diel + bonus
    return max(0.0, min(1.0, score))
