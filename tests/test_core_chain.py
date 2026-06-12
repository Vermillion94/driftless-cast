"""
Behavioral tests for the air→water→degree-day→hatch foundation.

These modules (Mohseni temp estimation, the seasonal hatch calendar, the
temperature-anomaly hatch shift, solar position, and forward degree-day
accumulation) were previously untested even though they sit underneath every
forecast. They are pure functions — no network — so they pin cheaply.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from src.models.temp_estimator import (
    estimate_current_water_f,
    estimate_water_series_f,
    mohseni,
    params_for_reach,
    rolling_mean,
)
from src.models.seasonal import seasonal_activity
from src.models.anomaly import shift_days
from src.models.solar import (
    bright_sun_dry_penalty,
    sun_altitude_deg,
    surface_brightness_factor,
)
from src.models.forecast_builder import _forward_dd_by_base_by_date


# ─── Mohseni air→water logistic ─────────────────────────────────────────────

def test_mohseni_is_monotonic_and_bounded():
    p = params_for_reach(spring_influenced=False)
    temps = [mohseni(t, p) for t in range(-10, 110, 5)]
    # Strictly non-decreasing in air temp.
    assert all(b >= a for a, b in zip(temps, temps[1:]))
    # Bounded by the logistic floor/ceiling.
    assert min(temps) >= p.mu_f - 0.5
    assert max(temps) <= p.alpha_f + 0.5


def test_mohseni_spring_creek_is_dampened_vs_freestone():
    """Spring-influenced reaches swing less than freestones across the same
    air-temp sweep — the whole reason we key Mohseni off the spring flag."""
    air = list(range(10, 95, 5))
    spring = estimate_water_series_f([float(t) for t in air], spring_influenced=True)
    free = estimate_water_series_f([float(t) for t in air], spring_influenced=False)
    spring_range = max(spring) - min(spring)
    free_range = max(free) - min(free)
    assert spring_range < free_range


def test_rolling_mean_smooths_and_preserves_length():
    series = [50.0, 60.0, 40.0, 70.0, 30.0]
    out = rolling_mean(series, window=3)
    assert len(out) == len(series)
    # Last value is the mean of the last 3.
    assert round(out[-1], 4) == round((40.0 + 70.0 + 30.0) / 3.0, 4)
    # Smoothed series has a smaller spread than the raw one.
    assert (max(out) - min(out)) < (max(series) - min(series))


def test_estimate_current_water_uses_recent_window():
    warm = estimate_current_water_f([70.0] * 7, spring_influenced=False)
    cold = estimate_current_water_f([35.0] * 7, spring_influenced=False)
    assert warm is not None and cold is not None
    assert warm > cold
    assert estimate_current_water_f([], spring_influenced=False) is None


# ─── Seasonal hatch calendar ────────────────────────────────────────────────

def test_seasonal_activity_peaks_at_calendar_peak():
    # Sulphur peaks ~June 1 in the Driftless seed calendar.
    peak = seasonal_activity("sulphur", date(2026, 6, 1))
    shoulder = seasonal_activity("sulphur", date(2026, 5, 1))
    offseason = seasonal_activity("sulphur", date(2026, 11, 1))
    assert peak == 1.0
    assert peak > shoulder > offseason
    assert offseason < 0.05


def test_seasonal_activity_unknown_species_is_zero():
    assert seasonal_activity("not-a-bug", date(2026, 6, 1)) == 0.0


def test_seasonal_activity_shift_moves_the_peak():
    # A negative shift (warm spring) pulls the peak earlier, so a date before
    # the nominal peak scores higher than it would unshifted.
    base = seasonal_activity("sulphur", date(2026, 5, 20), shift_days=0.0)
    earlier = seasonal_activity("sulphur", date(2026, 5, 20), shift_days=-10.0)
    assert earlier > base


# ─── Anomaly → hatch shift sign (regression: this was inverted pre-2026-05-18)

def test_warm_anomaly_pulls_hatches_earlier():
    # Warmer-than-normal water must produce a NEGATIVE shift (peak earlier).
    assert shift_days(5.0, spring_influenced=False) < 0
    assert shift_days(-5.0, spring_influenced=False) > 0


def test_spring_reaches_shift_less_than_freestone():
    warm = 6.0
    spring = abs(shift_days(warm, spring_influenced=True))
    free = abs(shift_days(warm, spring_influenced=False))
    assert spring < free


def test_shift_days_capped_at_two_weeks():
    assert shift_days(100.0, spring_influenced=False) == -14.0
    assert shift_days(-100.0, spring_influenced=False) == 14.0


# ─── Solar position ─────────────────────────────────────────────────────────

def test_sun_is_up_at_local_noon_and_down_at_midnight():
    lat, lon = 43.8, -91.7  # Driftless
    # ~18:00 UTC ≈ noon CDT — sun well up.
    noon = sun_altitude_deg(lat, lon, datetime(2026, 6, 21, 18, 0, tzinfo=timezone.utc))
    # ~06:00 UTC ≈ 1am CDT — sun below horizon.
    midnight = sun_altitude_deg(lat, lon, datetime(2026, 6, 21, 6, 0, tzinfo=timezone.utc))
    assert noon > 50.0
    assert midnight < 0.0


def test_bright_sun_penalty_only_bites_high_clear_sun():
    # High sun, clear sky → max penalty (~0.65).
    assert bright_sun_dry_penalty(70.0, 0.0) < 0.70
    # High sun but overcast → no penalty.
    assert bright_sun_dry_penalty(70.0, 1.0) == 1.0
    # Low sun → no penalty regardless of cloud.
    assert bright_sun_dry_penalty(2.0, 0.0) == 1.0
    assert surface_brightness_factor(2.0, 0.0) == 0.0


# ─── Forward degree-day accumulation across the horizon ─────────────────────

def test_forward_dd_accumulates_past_archive_boundary():
    archive_end = date(2026, 5, 10)
    # Three forecast days at a constant 59°F water (= 15°C).
    water_by_date = {
        date(2026, 5, 10): 59.0,  # at boundary — excluded
        date(2026, 5, 11): 59.0,
        date(2026, 5, 12): 59.0,
        date(2026, 5, 13): 59.0,
    }
    dd_at_end = {5.0: 200.0}
    out = _forward_dd_by_base_by_date(dd_at_end, water_by_date, archive_end, [5.0])
    series = out[5.0]
    # Boundary date is not re-counted.
    assert archive_end not in series
    # Each day adds (15°C - 5°C base) = 10 DD on top of the 200 starting total.
    assert round(series[date(2026, 5, 11)], 5) == 210.0
    assert round(series[date(2026, 5, 12)], 5) == 220.0
    assert round(series[date(2026, 5, 13)], 5) == 230.0
    # And it genuinely advances day to day (the old code held this constant).
    assert series[date(2026, 5, 13)] > series[date(2026, 5, 11)]


def test_forward_dd_never_subtracts_on_cold_days():
    archive_end = date(2026, 3, 1)
    # Water below the base temp must not decrement accumulated DD.
    water_by_date = {date(2026, 3, 2): 39.0, date(2026, 3, 3): 39.0}  # ~3.9°C < 5°C base
    out = _forward_dd_by_base_by_date({5.0: 50.0}, water_by_date, archive_end, [5.0])
    assert out[5.0][date(2026, 3, 2)] == 50.0
    assert out[5.0][date(2026, 3, 3)] == 50.0
