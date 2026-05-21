from datetime import datetime, timedelta, timezone

from src.models.degree_days import daily_degree_day, accumulate_degree_days
from src.models.nymph_score import compute_nymph_score
from src.models.dry_score import compute_dry_score
from src.models.forecast_builder import ReachSignals, _flow_percentile_for_hour
from src.models.thermal_profile import apply_profile, from_reach
from src.models.score_calibration import (
    aggression_score,
    confidence_score,
    headline_breakdown,
    headline_score,
    recommendation_rank_score,
)
from src.models.recession import (
    class_prior_tau_hours,
    project_flow,
)


def test_daily_degree_day():
    temps = [10, 12, 14, 16]
    assert daily_degree_day(temps, 5.0) == 8.0


def test_accumulate_degree_days():
    temps = [5, 7, 10]
    result = accumulate_degree_days(temps, 3.0)
    assert result == [2.0, 6.0, 13.0]


def test_nymph_score_bounds():
    score = compute_nymph_score(55.0, 0.4, [100.0, 90.0], "2026-04-22T10:00:00Z", 150.0, [180.0, 260.0])
    assert 0.0 <= score <= 1.0


def test_dry_score_with_species():
    species_list = [
        {
            "species_id": "hendrickson",
            "dd_threshold_mean": 260,
            "dd_threshold_sd": 40,
            "weather_prefs": {"clouds": "any", "wind": "<15"},
            "emergence_hr_start": 12,
            "emergence_hr_end": 15,
        }
    ]
    result = compute_dry_score(260, species_list, 0.2, 5.0, 55.0, 13)
    assert result["dry_score"] >= 0.0
    assert isinstance(result["active_species"], list)


def test_headline_score_compresses_nymph_only_plateau():
    score = headline_score(1.0, 0.05, [], {"code": "NYMPH"})
    assert score < 0.82


def test_headline_score_rewards_aligned_hatch_window():
    active = [{"id": "sulphur", "probability": 0.55}]
    score = headline_score(0.90, 0.70, active, {"code": "HATCH"})
    assert score > headline_score(0.90, 0.05, [], {"code": "NYMPH"})


def test_headline_score_separates_meaningful_surface_signal():
    weak = headline_score(1.0, 0.05, [], {"code": "NYMPH"})
    active = [{"id": "grannom-caddis", "probability": 0.20}]
    better = headline_score(1.0, 0.16, active, {"code": "HATCH"})
    assert better > weak
    assert better < 0.90


def test_nymph_only_cap_keeps_some_aggression_contrast():
    soft = headline_score(
        1.0, 0.05, [], {"code": "NYMPH"},
        {"flow_trend": 0.80, "pressure_factor": 1.0, "sun_factor": 0.70},
    )
    changing = headline_score(
        1.0, 0.05, [], {"code": "NYMPH"},
        {"flow_trend": 1.0, "pressure_factor": 1.05, "sun_factor": 1.0},
    )
    assert changing > soft
    assert changing <= 0.82


def test_aggression_score_rewards_change_stacked_window():
    active = [{"id": "hendrickson", "probability": 0.30}]
    flat = aggression_score(
        1.0, 0.05, [], {"code": "NYMPH"},
        {"flow_trend": 0.85, "pressure_factor": 1.0, "sun_factor": 0.75},
    )
    hot = aggression_score(
        1.0, 0.28, active, {"code": "HATCH"},
        {"flow_trend": 1.0, "pressure_factor": 1.05, "sun_factor": 1.0},
    )
    assert hot > flat
    assert hot >= 0.75


def test_confidence_score_prefers_measured_local_short_lead():
    high = confidence_score(
        "2026-05-20T18:00:00+00:00",
        "2026-05-20T12:00:00+00:00",
        "gauge",
        False,
        {"percentile_used": 0.45, "pressure_factor": 1.0},
    )
    low = confidence_score(
        "2026-05-27T18:00:00+00:00",
        "2026-05-20T12:00:00+00:00",
        "estimate",
        True,
        {"pressure_factor": 1.0},
    )
    assert high["score"] > low["score"]
    assert high["score"] > 0.90
    assert low["score"] < 0.75


def test_confidence_score_uses_proxy_distance():
    near = confidence_score(
        "2026-05-20T18:00:00+00:00",
        "2026-05-20T12:00:00+00:00",
        "estimate",
        True,
        {"percentile_used": 0.45, "pressure_factor": 1.0},
        12.0,
    )
    far = confidence_score(
        "2026-05-20T18:00:00+00:00",
        "2026-05-20T12:00:00+00:00",
        "estimate",
        True,
        {"percentile_used": 0.45, "pressure_factor": 1.0},
        42.0,
    )
    assert near["score"] > far["score"]
    assert "~12 km" in near["notes"][1]


def test_recommendation_rank_score_penalizes_uncertainty_without_changing_quality():
    strong_low_conf = recommendation_rank_score(0.82, 0.60)
    slightly_lower_high_conf = recommendation_rank_score(0.81, 0.95)
    assert slightly_lower_high_conf > strong_low_conf
    assert recommendation_rank_score(0.90, 0.60) > recommendation_rank_score(0.81, 0.95)


def test_recession_projects_toward_median():
    q = project_flow(100.0, 50.0, 24.0, 24.0)
    assert 50.0 < q < 100.0
    assert round(project_flow(50.0, 100.0, 24.0, 24.0), 2) == round(100.0 - (q - 50.0), 2)


def test_recession_priors_keep_spring_reaches_slower():
    assert class_prior_tau_hours(True) > class_prior_tau_hours(False)


def test_fused_noaa_usgs_flow_uses_relative_local_change():
    now = datetime(2026, 5, 20, 12, tzinfo=timezone.utc)
    target = now + timedelta(hours=24)
    signals = ReachSignals(
        reach_id="whitewater-main-beaver",
        stream_name="Whitewater",
        lat=44.0,
        lon=-92.0,
        spring_influenced=True,
        usgs_gauge_id="05384500",
        gauge_source="usgs",
        gauge_is_proxy=True,
        current_flow_cfs=100.0,
        local_flow_cfs=50.0,
        local_flow_source="noaa",
        water_temp_c=None,
        water_temp_source=None,
        flow_percentile=0.5,
        flow_stats={"p10": 40.0, "p25": 70.0, "p50": 100.0, "p75": 140.0, "p90": 200.0},
        recent_flows=[],
        confidence_notes=[],
        forecast_flow_by_hour={
            target.replace(minute=0, second=0, microsecond=0).isoformat(): 75.0
        },
    )
    pct, projected, note, tau, source = _flow_percentile_for_hour(signals, target, now)
    assert projected == 75.0
    assert note == "local NOAA flow trend + USGS proxy percentile"
    assert tau is None
    assert source == "noaa_usgs_fused"
    assert 0.75 < pct < 0.90


def test_proxy_reach_displays_local_noaa_flow_when_no_forecast():
    now = datetime(2026, 5, 20, 12, tzinfo=timezone.utc)
    signals = ReachSignals(
        reach_id="trout-run-creek-chatfield",
        stream_name="Trout Run",
        lat=44.0,
        lon=-92.0,
        spring_influenced=True,
        usgs_gauge_id="05383950",
        gauge_source="usgs",
        gauge_is_proxy=True,
        current_flow_cfs=450.0,
        local_flow_cfs=110.0,
        local_flow_source="noaa",
        water_temp_c=None,
        water_temp_source=None,
        flow_percentile=0.5,
        flow_stats={"p10": 250.0, "p25": 350.0, "p50": 450.0, "p75": 600.0, "p90": 800.0},
        recent_flows=[],
        confidence_notes=[],
        forecast_flow_by_hour=None,
    )
    pct, displayed_flow, _note, tau, source = _flow_percentile_for_hour(
        signals, now + timedelta(hours=24), now
    )
    assert displayed_flow == 110.0
    assert tau is not None
    assert source in {"class_prior", "per_gauge_fit"}
    assert 0.25 < pct < 0.75


def test_spring_creek_thermal_profile_cools_warm_water_and_damps_diurnal():
    profile = from_reach({
        "spring_influenced": 1,
        "trout_class": "I",
        "mean_gradient": 6.0,
        "length_km": 7.0,
    })
    assert profile.spring_strength > 0.85
    assert apply_profile(62.0, profile) < 60.0
    assert apply_profile(42.0, profile) > 42.0
    assert profile.diurnal_amp_factor < 0.75


def test_freestone_thermal_profile_is_neutral():
    profile = from_reach({
        "spring_influenced": 0,
        "trout_class": "II",
        "mean_gradient": 3.0,
        "length_km": 12.0,
    })
    assert profile.spring_strength == 0.0
    assert apply_profile(62.0, profile) == 62.0


def test_headline_score_preserves_hard_regime_caps():
    assert headline_score(1.0, 1.0, [], {"code": "BLOWOUT"}) == 0.10
    assert headline_score(1.0, 1.0, [], {"code": "HEAT_STRESS"}) == 0.15


def test_headline_breakdown_explains_nymph_cap():
    breakdown = headline_breakdown(1.0, 0.05, [], {"code": "NYMPH"})
    assert breakdown["source"] == "nymph_capped"
    assert breakdown["score"] == headline_score(1.0, 0.05, [], {"code": "NYMPH"})
    assert breakdown["surface_signal"] == 0.05
    assert "aggression" in breakdown
    assert "water_temp_zone" in breakdown["evidence"]
