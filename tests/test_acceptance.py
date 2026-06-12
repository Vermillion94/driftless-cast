"""
Synthetic acceptance tests — known-correct cases for the model components.

Catches structural regressions: the kind of bug we hit twice in this project
where the function returned the right answer in isolation but production
served stale code, or where two functions disagreed about what "ideal" meant.

Each test pins a single fact about the model that should NEVER drift without
an intentional change. If a test fails because behavior intentionally changed,
update the test AND document the change in `docs/REFERENCES.md` so future
readers know why the threshold moved.

Run with:
    .venv/Scripts/python -m pytest tests/test_acceptance.py -v

Or as a plain script if pytest isn't installed:
    .venv/Scripts/python tests/test_acceptance.py
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.models.dry_score import hour_of_day_score
from src.models.fly_recommender import recommend_flies
from src.models.nymph_score import (
    compute_nymph_score,
    diel_activity_factor,
    drift_window_bonus,
    flow_percentile_score,
    flow_trend_score,
    temperature_score,
)
from src.models import regime as regime_mod
from src.models import runoff_risk as runoff_mod
from src.models.runoff_risk import assess_runoff_risk


# ─── Temperature plateau curve ──────────────────────────────────────────────

def test_temperature_below_42_is_zero():
    """Below 42°F trout are lethargic and refuse — score must be 0."""
    assert temperature_score(35.0) == 0.0
    assert temperature_score(41.0) == 0.0
    assert temperature_score(42.0) == 0.0


def test_temperature_plateau_full_credit_52_to_64():
    """Plateau zone (Wehrly 2007 active feeding range) — score is 1.0."""
    for t in (52.0, 55.0, 58.0, 61.0, 64.0):
        assert temperature_score(t) == 1.0, f"plateau expected 1.0 at {t}°F"


def test_temperature_51_was_the_bug_fix():
    """Pinning the regression we hit: 51°F should give ~0.9, not the old
    Gaussian's 0.51 which made every cool spring day read 'Skip'."""
    assert temperature_score(51.0) == 0.9
    assert temperature_score(50.0) == 0.8
    assert temperature_score(47.0) == 0.5


def test_temperature_warning_threshold_at_68():
    """The 68°F catch-and-release ethics threshold (Wilkie 1996) — half credit."""
    assert temperature_score(68.0) == 0.5


def test_temperature_lethal_above_75():
    """Above 75°F is lethal-stress territory — zero score."""
    assert temperature_score(75.0) == 0.0
    assert temperature_score(80.0) == 0.0


# ─── Flow percentile curve (asymmetric) ────────────────────────────────────

def test_flow_percentile_peak_is_35_to_50():
    """The asymmetric curve gives full credit in the 35–50th percentile band."""
    assert flow_percentile_score(0.35) == 1.0
    assert flow_percentile_score(0.42) == 1.0
    assert flow_percentile_score(0.50) == 1.0


def test_flow_percentile_low_flows_still_fishable():
    """Low flow on a clear spring creek — clear water and concentrated fish.
    Should be gently penalized, not collapsed to zero."""
    assert flow_percentile_score(0.10) > 0.55
    assert flow_percentile_score(0.20) > 0.65


def test_flow_percentile_high_flows_steep_penalty():
    """Flooded — wading dangerous, fish hugging cover. Steep penalty.
    At 92nd pct = 0.38, 95th = 0.30, 97th = 0.20. The slope past p80 is
    deliberately steep to mark unsafe wading conditions."""
    assert flow_percentile_score(0.85) < 0.60
    assert flow_percentile_score(0.92) <= 0.40
    assert flow_percentile_score(0.95) <= 0.31  # float-precision bump
    assert flow_percentile_score(0.97) <= 0.21


def test_flow_percentile_default_when_unknown():
    """No stats → neutral 0.5, not zero."""
    assert flow_percentile_score(None) == 0.5


# ─── Flow trend bonus ──────────────────────────────────────────────────────

def test_flow_trend_falling_full_credit():
    """Recently elevated flow that's now stable or falling — full credit."""
    assert flow_trend_score([100.0, 80.0, 60.0]) == 1.0


def test_flow_trend_rising_penalty():
    """Rising flow — fish move to bank cover and stop feeding."""
    assert flow_trend_score([60.0, 80.0, 100.0]) < 1.0


def test_flow_trend_default_when_no_history():
    """No history → neutral 0.75, not zero."""
    assert flow_trend_score([]) == 0.75
    assert flow_trend_score([50.0]) == 0.75


# ─── Composite nymph score ──────────────────────────────────────────────────

def test_nymph_score_obvious_go():
    """Saturday-Kinni archetype: 56°F water, 50th-pct flow, falling flow trend.
    This is the case the user flagged that was scoring 0.36 before the
    plateau curve fix. Must be solidly in 'Go' territory."""
    score = compute_nymph_score(
        temp_f=56.0,
        flow_percentile=0.50,
        recent_flows=[140.0, 120.0, 110.0],
        valid_at="2026-05-02T13:00:00+00:00",
        dd_current=0.0,
        species_thresholds=[],
    )
    assert 0.60 <= score <= 1.0, f"expected solid Go for ideal conditions, got {score:.3f}"


def test_nymph_score_obvious_skip():
    """38°F water + 95th-pct flow — winter blowout. Must score below 0.35."""
    score = compute_nymph_score(
        temp_f=38.0,
        flow_percentile=0.95,
        recent_flows=[200.0, 250.0, 300.0],
        valid_at="2026-01-15T13:00:00+00:00",
        dd_current=0.0,
        species_thresholds=[],
    )
    assert score < 0.35, f"expected Skip for cold blowout, got {score:.3f}"


def test_nymph_score_bounded_to_unit_interval():
    """All factor combinations must produce score in [0, 1]."""
    for temp in (30, 50, 60, 70, 80):
        for pct in (0.0, 0.25, 0.5, 0.75, 0.95):
            for trend in ([], [100.0, 90.0], [50.0, 100.0]):
                s = compute_nymph_score(temp, pct, trend, "2026-05-01T13:00:00Z", 0.0, [])
                assert 0.0 <= s <= 1.0


def test_nymph_score_temperature_dominates():
    """Temperature is THE dominant factor; even perfect flow can't rescue
    sub-42°F lethargy."""
    perfect_flow = compute_nymph_score(
        temp_f=40.0, flow_percentile=0.50, recent_flows=[100, 90, 80],
        valid_at="2026-05-02T13:00:00Z", dd_current=0.0, species_thresholds=[],
    )
    assert perfect_flow < 0.20, f"sub-42°F must collapse score, got {perfect_flow:.3f}"


def test_diel_activity_uses_driftless_local_time():
    """UTC timestamps must convert to Driftless local time before dawn/evening
    windows are applied. 00:00Z in May is 7pm CDT, not midnight fishing."""
    assert diel_activity_factor("2026-05-21T00:00:00+00:00") == 1.0
    assert drift_window_bonus("2026-05-21T00:00:00+00:00") == 0.08
    assert diel_activity_factor("2026-05-21T18:00:00+00:00") == 0.86


def test_nymph_score_has_within_day_shape_on_plateau():
    """Identical temp/flow inputs should still show practical ebbs and flows:
    evening low light beats bright midday, while both remain fishable when
    water and flow are right."""
    evening = compute_nymph_score(
        temp_f=56.0, flow_percentile=0.45, recent_flows=[120.0, 100.0, 90.0],
        valid_at="2026-05-21T00:00:00+00:00", dd_current=0.0, species_thresholds=[],
    )
    midday = compute_nymph_score(
        temp_f=56.0, flow_percentile=0.45, recent_flows=[120.0, 100.0, 90.0],
        valid_at="2026-05-21T18:00:00+00:00", dd_current=0.0, species_thresholds=[],
    )
    assert evening > midday
    assert evening - midday >= 0.12
    assert midday >= 0.60


# ─── Cross-component symmetry ──────────────────────────────────────────────

def test_two_reaches_same_inputs_same_score():
    """Two reaches with identical inputs MUST produce identical scores.
    This pins the structural correctness of the scoring math — no hidden
    reach-specific state should leak in."""
    inputs = dict(
        temp_f=55.0,
        flow_percentile=0.45,
        recent_flows=[120.0, 100.0, 90.0],
        valid_at="2026-05-02T13:00:00Z",
        dd_current=200.0,
        species_thresholds=[260.0],
    )
    a = compute_nymph_score(**inputs)
    b = compute_nymph_score(**inputs)
    assert a == b


# ─── Hour-of-day window ────────────────────────────────────────────────────

def test_emergence_window_inside_full_credit():
    """Hendrickson emergence noon-3pm — score 1.0 inside, 0.3 outside."""
    assert hour_of_day_score(13, 12, 15) == 1.0
    assert hour_of_day_score(12, 12, 15) == 1.0
    assert hour_of_day_score(15, 12, 15) == 1.0


def test_emergence_window_outside_partial_credit():
    """Far outside the window: only background adult presence, not a hatch."""
    assert hour_of_day_score(8, 12, 15) == 0.05
    assert hour_of_day_score(20, 12, 15) == 0.05


def test_emergence_window_shoulder_is_short():
    """The 2h shoulder preserves near-miss eats without flattening all day."""
    assert 0.50 < hour_of_day_score(11, 12, 15) < 1.0
    assert 0.50 < hour_of_day_score(16, 12, 15) < 1.0


# ─── Regime classifier ─────────────────────────────────────────────────────

def _qpf_for(valid_at: datetime, mm_per_hour: float, hours: int = 24) -> dict:
    out = {}
    for h in range(1, hours + 1):
        prior = (valid_at - timedelta(hours=h)).astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
        out[prior] = mm_per_hour
    return out


def test_regime_blowout_detection():
    """Heavy rain + high flow = BLOWOUT (caps score regardless of other factors)."""
    valid = datetime(2026, 5, 15, 14, tzinfo=timezone.utc)
    r = regime_mod.classify(
        valid_at=valid, flow_percentile=0.92, water_temp_f=58.0, air_temp_f=70.0,
        dry_score=0.0, nymph_score=0.0, spring_influenced=False,
        length_km=6.0,
        qpf_map=_qpf_for(valid, 1.5),  # 36mm in 24h
        active_species=[],
    )
    assert r.code == "BLOWOUT"
    assert r.severity == "alert"


def test_regime_streamer_high_flow():
    """Elevated flow, no rain — STREAMER day."""
    r = regime_mod.classify(
        valid_at=datetime(2026, 5, 15, 14, tzinfo=timezone.utc),
        flow_percentile=0.82, water_temp_f=55.0, air_temp_f=65.0,
        dry_score=0.0, nymph_score=0.3, spring_influenced=False,
        length_km=10.0,
        qpf_map=None, active_species=[],
    )
    assert r.code == "STREAMER"


def test_regime_hatch_when_dry_score_meaningful():
    """Active hatch + dry score ≥ 0.30 → HATCH regime (defers to fly_recommender)."""
    r = regime_mod.classify(
        valid_at=datetime(2026, 4, 25, 13, tzinfo=timezone.utc),
        flow_percentile=0.45, water_temp_f=58.0, air_temp_f=66.0,
        dry_score=0.6, nymph_score=0.5, spring_influenced=True,
        length_km=8.0,
        qpf_map=None,
        active_species=[{"common_name": "Hendrickson", "probability": 0.55}],
    )
    assert r.code == "HATCH"


def test_regime_terrestrial_summer_no_hatch():
    """August warm afternoon, no aquatic hatch — TERRESTRIAL."""
    r = regime_mod.classify(
        valid_at=datetime(2026, 8, 10, 15, tzinfo=timezone.utc),
        flow_percentile=0.30, water_temp_f=64.0, air_temp_f=85.0,
        dry_score=0.10, nymph_score=0.3, spring_influenced=False,
        length_km=12.0,
        qpf_map=None, active_species=[],
    )
    assert r.code == "TERRESTRIAL"


def test_regime_midge_winter_cold():
    """Winter cold-water — MIDGE regime activates instead of generic 'Skip'."""
    r = regime_mod.classify(
        valid_at=datetime(2026, 1, 15, 13, tzinfo=timezone.utc),
        flow_percentile=0.45, water_temp_f=38.0, air_temp_f=28.0,
        dry_score=0.0, nymph_score=0.1, spring_influenced=True,
        length_km=6.0,
        qpf_map=None, active_species=[],
    )
    assert r.code == "MIDGE"


def test_regime_scud_spring_creek_default():
    """Spring-influenced reach with no other regime firing — SCUD."""
    r = regime_mod.classify(
        valid_at=datetime(2026, 5, 5, 10, tzinfo=timezone.utc),
        flow_percentile=0.40, water_temp_f=55.0, air_temp_f=60.0,
        dry_score=0.10, nymph_score=0.45, spring_influenced=True,
        length_km=5.0,
        qpf_map=None, active_species=[],
    )
    assert r.code == "SCUD"


def test_regime_normal_default():
    """Default regime — generic searching nymph day."""
    r = regime_mod.classify(
        valid_at=datetime(2026, 5, 5, 10, tzinfo=timezone.utc),
        flow_percentile=0.40, water_temp_f=55.0, air_temp_f=60.0,
        dry_score=0.20, nymph_score=0.65, spring_influenced=False,
        length_km=12.0,
        qpf_map=None, active_species=[],
    )
    assert r.code == "NORMAL"


def test_regime_priority_blowout_over_hatch():
    """BLOWOUT must win over HATCH even if DD says species are emerging.
    Real-world rationale: a 6\" rain day overrides any hatch call."""
    valid = datetime(2026, 4, 25, 13, tzinfo=timezone.utc)
    r = regime_mod.classify(
        valid_at=valid, flow_percentile=0.95, water_temp_f=55.0, air_temp_f=65.0,
        dry_score=0.5, nymph_score=0.5, spring_influenced=True,
        length_km=8.0,
        qpf_map=_qpf_for(valid, 1.5),
        active_species=[{"common_name": "Hendrickson", "probability": 0.6}],
    )
    assert r.code == "BLOWOUT"


def test_runoff_risk_small_freestone_more_sensitive_than_spring_creek():
    valid = datetime(2026, 5, 15, 14, tzinfo=timezone.utc)
    qpf = _qpf_for(valid, 1.25, hours=6)  # 7.5 mm in 6h
    flashy = assess_runoff_risk(
        valid_at=valid, qpf_map=qpf, spring_influenced=False, length_km=6.0, flow_percentile=0.45
    )
    spring = assess_runoff_risk(
        valid_at=valid, qpf_map=qpf, spring_influenced=True, length_km=6.0, flow_percentile=0.45
    )
    assert flashy.response_ratio > spring.response_ratio
    assert flashy.hurt_threshold_6h_mm < spring.hurt_threshold_6h_mm


def test_runoff_risk_high_antecedent_flow_lowers_rain_tolerance():
    valid = datetime(2026, 5, 15, 14, tzinfo=timezone.utc)
    qpf = _qpf_for(valid, 1.5, hours=6)  # 9 mm in 6h
    low = assess_runoff_risk(
        valid_at=valid, qpf_map=qpf, spring_influenced=False, length_km=10.0, flow_percentile=0.30
    )
    high = assess_runoff_risk(
        valid_at=valid, qpf_map=qpf, spring_influenced=False, length_km=10.0, flow_percentile=0.80
    )
    assert high.hurt_threshold_6h_mm < low.hurt_threshold_6h_mm
    assert high.response_ratio > low.response_ratio


def test_runoff_risk_uses_historical_fit_when_present():
    valid = datetime(2026, 5, 15, 14, tzinfo=timezone.utc)
    qpf = _qpf_for(valid, 2.0, hours=6)  # 12 mm in 6h
    original = runoff_mod._RUNOFF_FITS
    runoff_mod._RUNOFF_FITS = {
        "test-reach": {
            "hurt_threshold_6h_mm": 24.0,
            "hurt_threshold_12h_mm": 30.0,
            "hurt_threshold_24h_mm": 38.0,
        }
    }
    try:
        fitted = assess_runoff_risk(
            valid_at=valid, reach_id="test-reach", qpf_map=qpf,
            spring_influenced=False, length_km=6.0, flow_percentile=0.45
        )
    finally:
        runoff_mod._RUNOFF_FITS = original
    assert fitted.hurt_threshold_6h_mm == 24.0
    assert fitted.hurt_threshold_24h_mm == 38.0
    assert fitted.threshold_source == "historical_fit"


# ─── Fly recommender ───────────────────────────────────────────────────────

def test_fly_recommender_default_searching_pattern():
    """No active species — default to a Pheasant Tail / Hare's Ear searching rig."""
    rec = recommend_flies([], {}, valid_hour=13)
    assert rec["primary"]["pattern"] == "Pheasant Tail"
    assert rec["dropper"]["pattern"] == "Hare's Ear"


def test_fly_recommender_picks_highest_probability():
    """Active species sorted by probability — recommender uses the top one."""
    species_data = {
        "hendrickson": {"fly_patterns": [
            {"pattern": "Hendrickson Parachute", "size": 14, "stage": "dun"},
            {"pattern": "Pheasant Tail BH",     "size": 14, "stage": "nymph"},
        ]},
        "bwo-spring": {"fly_patterns": [
            {"pattern": "BWO Comparadun", "size": 18, "stage": "dun"},
        ]},
    }
    active = [
        {"id": "bwo-spring", "probability": 0.30},
        {"id": "hendrickson", "probability": 0.65},
    ]
    rec = recommend_flies(active, species_data, valid_hour=13)
    assert rec["primary"]["pattern"] == "Hendrickson Parachute"


# ─── Driver: simple runner so the suite works without pytest installed ─────

def _run_all() -> int:
    failures: list = []
    passed = 0
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for fn in fns:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except AssertionError as exc:
            failures.append((fn.__name__, str(exc)))
            print(f"  FAIL  {fn.__name__}: {exc}")
        except Exception as exc:
            failures.append((fn.__name__, f"{type(exc).__name__}: {exc}"))
            print(f"  ERR   {fn.__name__}: {type(exc).__name__}: {exc}")
    print()
    print(f"{passed}/{len(fns)} passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
