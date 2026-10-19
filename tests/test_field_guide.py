"""Field-guide layer: season legality, hard-stop regimes, tactics, day verdicts.

Each case pins a call an experienced Driftless angler would make; the comment
says which guide/agency rule it encodes so a failure points at the source.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.models import regime as regime_mod
from src.models.nymph_score import diel_activity_factor, temperature_score
from src.models.regulations import (
    CATCH_AND_RELEASE, CLOSED, OPEN, nth_weekday, season_status, spawning_advisory,
)
from src.models.score_calibration import headline_score
from src.models.solar import seasonal_sun_factors
from src.models.tactics import MUDDY, STAINED, estimate_clarity, recommend_tactics
from src.models.trip_verdict import build_days

CT = ZoneInfo("America/Chicago")


# ── Regulations ──────────────────────────────────────────────────────────

def test_mn_season_phases_2026():
    # MN Rule 6262.0200: C&R Jan 1 → 2nd Sat Apr; harvest → Sep 14; C&R → Oct 15.
    assert nth_weekday(2026, 4, 5, 2) == date(2026, 4, 11)
    assert season_status("MN", date(2026, 2, 1)).status == CATCH_AND_RELEASE
    assert season_status("MN", date(2026, 4, 11)).status == OPEN
    assert season_status("MN", date(2026, 9, 20)).status == CATCH_AND_RELEASE
    closed = season_status("MN", date(2026, 10, 19))
    assert closed.status == CLOSED and closed.next_change == "2027-01-01"


def test_wi_2026_april_opener_and_closure():
    # WI 2026 change: harvest opens 1st Sat of April (was May); closed Oct 16.
    assert season_status("WI", date(2026, 1, 2)).status == CLOSED      # before 1st Sat Jan
    assert season_status("WI", date(2026, 1, 3)).status == CATCH_AND_RELEASE
    assert season_status("WI", date(2026, 4, 4)).status == OPEN
    assert season_status("WI", date(2025, 4, 10)).status == CATCH_AND_RELEASE  # old May opener
    assert season_status("WI", date(2026, 10, 16)).status == CLOSED


def test_iowa_open_year_round():
    for d in (date(2026, 1, 1), date(2026, 10, 19), date(2026, 12, 31)):
        assert season_status("IA", d).status == OPEN


def test_spawning_advisory_window():
    assert spawning_advisory(date(2026, 10, 19))
    assert spawning_advisory(date(2026, 7, 1)) is None


# ── Hard-stop regimes ────────────────────────────────────────────────────

def _classify(**kw):
    base = dict(
        valid_at=datetime(2026, 6, 10, 13, tzinfo=CT), flow_percentile=0.45,
        water_temp_f=58.0, air_temp_f=70.0, dry_score=0.2, nymph_score=0.7,
        spring_influenced=True, length_km=8.0, qpf_map=None, active_species=[],
    )
    base.update(kw)
    return regime_mod.classify(**base)


def test_closed_season_beats_everything():
    r = _classify(valid_at=datetime(2026, 10, 20, 13, tzinfo=CT), state="WI")
    assert r.code == "CLOSED"
    assert headline_score(1.0, 1.0, [{"probability": 1.0}], {"code": "CLOSED"}) == 0.0


def test_lightning_gate():
    # NWS: "when thunder roars, go indoors".
    r = _classify(short_forecast="Chance Showers And Thunderstorms", precip_prob=50)
    assert r.code == "LIGHTNING"
    assert _classify(short_forecast="Slight Chance Thunderstorms", precip_prob=10).code != "LIGHTNING"


def test_cold_and_wind_hazards():
    # Driftless Angler: below ~20°F "too cold to fish effectively".
    assert _classify(air_temp_f=8.0, water_temp_f=38.0).code == "HAZARD"
    assert _classify(air_temp_f=18.0, wind_mph=20.0, water_temp_f=38.0).code == "HAZARD"
    assert _classify(air_temp_f=28.0, wind_mph=8.0, water_temp_f=38.0).code != "HAZARD"
    assert _classify(wind_mph=30.0).code == "HAZARD"


def test_lethal_water_any_hour():
    r = _classify(valid_at=datetime(2026, 7, 20, 7, tzinfo=CT), water_temp_f=74.0, air_temp_f=70.0)
    assert r.code == "HEAT_STRESS"


# ── Cold-season biology ──────────────────────────────────────────────────

def test_winter_midday_beats_winter_dawn_and_dusk():
    # Winter: "fish 10–2 during rising temperatures" (Driftless Angler, TCTU).
    noon = diel_activity_factor("2026-01-15T13:00:00-06:00", 37.0)
    dawn = diel_activity_factor("2026-01-15T07:00:00-06:00", 37.0)
    dusk = diel_activity_factor("2026-01-15T19:00:00-06:00", 37.0)
    assert noon > dawn and noon > dusk


def test_summer_still_favors_low_light():
    noon = diel_activity_factor("2026-07-15T13:00:00-05:00", 62.0)
    dusk = diel_activity_factor("2026-07-15T20:00:00-05:00", 62.0)
    assert dusk > noon


def test_sun_helps_in_cold_water_hurts_in_warm():
    dry_cold, nymph_cold = seasonal_sun_factors(35.0, 0.0, 38.0)
    dry_warm, nymph_warm = seasonal_sun_factors(60.0, 0.0, 62.0)
    assert dry_cold == 1.0 and nymph_cold > 1.0
    assert dry_warm < 0.75 and nymph_warm == 1.0


def test_winter_water_is_a_slow_bite_not_closed():
    assert 0.3 < temperature_score(37.0) < 0.5


# ── Tactics ──────────────────────────────────────────────────────────────

def test_clarity_classes():
    assert estimate_clarity(0.95, "low") == MUDDY
    assert estimate_clarity(0.5, "blowout") == MUDDY
    assert estimate_clarity(0.85, "low") == STAINED


def _tac(**kw):
    base = dict(valid_at=datetime(2026, 6, 10, 13, tzinfo=CT), water_temp_f=58.0, flow_percentile=0.45,
                runoff_risk_level="low", cloud_cover=0.5, wind_mph=6.0, air_temp_f=70.0, regime_code="NORMAL")
    base.update(kw)
    return recommend_tactics(**base)


def test_tactics_winter_is_slow_deep_midge_scud():
    t = _tac(valid_at=datetime(2026, 1, 20, 12, tzinfo=CT), water_temp_f=37.0, air_temp_f=28.0, regime_code="MIDGE")
    assert "midge" in t.method.lower()
    assert any("Zebra Midge" in f for f in t.flies)
    assert any("spring" in w.lower() for w in t.water)


def test_tactics_stained_water_goes_big():
    t = _tac(valid_at=datetime(2026, 4, 25, 13, tzinfo=CT), water_temp_f=52.0, flow_percentile=0.85)
    assert "streamer" in t.method.lower() or "worm" in t.method.lower()


def test_tactics_muddy_points_to_headwaters():
    t = _tac(flow_percentile=0.96)
    assert "headwater" in (t.where or "").lower()


def test_tactics_summer_midday_is_hopper_dropper_in_the_shade():
    t = _tac(valid_at=datetime(2026, 8, 10, 13, tzinfo=CT), water_temp_f=61.0, air_temp_f=82.0, cloud_cover=0.1)
    assert "hopper" in t.method.lower()
    assert any("shade" in a.lower() for a in t.approach)


def test_tactics_hatch_uses_hatch_flies():
    t = _tac(top_species={"common_name": "Grannom / Black Caddis", "probability": 0.8},
             flies={"primary": {"pattern": "CDC & Elk", "size": 16}}, surface_signal=0.8)
    assert "Grannom" in t.method
    assert t.flies[0].startswith("CDC & Elk")


# ── Day verdicts ─────────────────────────────────────────────────────────

def _hour(day, h, score, code="NORMAL"):
    return {"valid_at": datetime(2026, 5, day, h, tzinfo=CT).isoformat(), "combined_score": score,
            "regime": {"code": code}, "active_species": [], "water_temp_f": 56.0, "tactics": {}}


def test_verdict_flags_clearly_better_tomorrow():
    today = [_hour(20, h, 0.45) for h in range(6, 21)]
    tomorrow = [_hour(21, h, 0.80 if 17 <= h <= 19 else 0.5) for h in range(6, 21)]
    days = build_days(today + tomorrow)
    assert days[0]["verdict"] == "slow"
    assert days[0].get("better_day") == "2026-05-21"
    assert days[1]["verdict"] == "go"
    assert days[1]["best_window"]["label"] == "5pm–7pm"


def test_verdict_skip_names_the_reason():
    blown = [_hour(20, h, 0.08, "BLOWOUT") for h in range(6, 21)]
    days = build_days(blown)
    assert days[0]["verdict"] == "skip"
    assert "blown out" in days[0]["advice"]


def test_verdict_closed():
    closed = [_hour(20, h, 0.0, "CLOSED") for h in range(6, 21)]
    days = build_days(closed, {"next_change": "2027-01-01"})
    assert days[0]["verdict"] == "closed"
    assert "2027-01-01" in days[0]["advice"]


def test_muddy_water_cannot_be_carried_by_a_hatch():
    # Fish don't rise to olives in chocolate milk (turbidity halves reactive
    # distance by ~10 NTU): the surface factor must crush dries when muddy.
    from src.models.tactics import SURFACE_CLARITY_FACTOR
    assert SURFACE_CLARITY_FACTOR["muddy"] <= 0.2
    assert SURFACE_CLARITY_FACTOR["clear"] == 1.0


def test_verdict_skips_partial_trailing_day():
    full = [_hour(20, h, 0.6) for h in range(6, 21)]
    stub = [_hour(21, h, 0.3) for h in (0, 1)]
    days = build_days(full + stub)
    assert [d["date"] for d in days] == ["2026-05-20"]


def test_winter_midge_signal_keeps_nymph_first_tactic():
    t = _tac(valid_at=datetime(2026, 1, 15, 13, tzinfo=CT), water_temp_f=37.0, air_temp_f=28.0,
             regime_code="HATCH", top_species={"id": "midge", "common_name": "Midges", "probability": 0.34},
             surface_signal=0.34)
    assert "nymphing" in t.method.lower()


def test_skip_cites_the_limiter_not_a_positive():
    rows = [_hour(20, h, 0.2) for h in range(6, 21)]
    for r in rows:
        r["tactics"] = {"clarity": "muddy", "clarity_label": "muddy"}
    days = build_days(rows)
    assert days[0]["verdict"] == "skip"
    assert "muddy" in days[0]["advice"]
    assert "prime" not in days[0]["advice"]
