"""
Forecast builder — per plan step 9.

For each reach over the next N hours, compose nymph and dry scores, active
species, recommended flies, and a human-readable explanation, and write them
to the `prediction` table.

The signals come from three sources:
  * USGS IV + stats            → current flow, water temp, flow percentile
  * NOAA NWPS (fallback)       → current flow/stage when USGS is decommissioned
  * NWS hourly forecast        → air temp, cloud cover, wind for the valid hour

Hatch activity is seasonal-calendar based for v1 (see `seasonal.py`); DD-based
hatch prediction lights up once per-reach water-temp history is backfilled.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from src.db import get_connection
from src.ingest import (
    discharge_percentile,
    fetch_daily,
    fetch_daily_stats,
    fetch_forecast_series,
    fetch_gridpoint,
    fetch_gridpoint_pressure_pa,
    fetch_gridpoint_qpf_mm,
    fetch_hourly_forecast,
    fetch_latest_iv,
    fetch_latest_nwps,
    parse_hourly_forecast,
)
from src.models.solar import bright_sun_dry_penalty, sun_altitude_deg
from src.ingest.openmeteo import fetch_archive_daily_mean_f, fetch_hourly_pressure_hpa
from src.models import anomaly as anomaly_mod
from src.models import dd_pipeline
from src.models import regime as regime_mod
from src.models import temp_estimator
from src.models.dry_score import hour_of_day_score, shift_window_for_air_temp
from src.models.hatch_predictor import species_activity_probability, weather_match_score
from src.models.fly_recommender import recommend_flies
from src.models.nymph_score import compute_nymph_score
from src.models.recession import (
    calibrated_tau_hours,
    project_flow,
)
from src.models.seasonal import TERRESTRIAL_SPECIES, seasonal_activity
from src.models.temp_estimator import estimate_water_series_f

LOG = logging.getLogger(__name__)

SPECIES_SEED = Path(__file__).resolve().parent.parent.parent / "data" / "seed" / "species.json"
FORECAST_HORIZON_H = 168  # 7 days — NWS hourly forecast covers 156 hours


def load_species() -> List[Dict[str, object]]:
    with SPECIES_SEED.open("r", encoding="utf-8") as fh:
        species = json.load(fh)
    for sp in species:
        if isinstance(sp.get("weather_prefs"), str):
            sp["weather_prefs"] = json.loads(sp["weather_prefs"])
        if isinstance(sp.get("fly_patterns"), str):
            sp["fly_patterns"] = json.loads(sp["fly_patterns"])
    return species


@dataclass
class ReachSignals:
    reach_id: str
    stream_name: str
    lat: float
    lon: float
    spring_influenced: bool
    usgs_gauge_id: Optional[str]
    gauge_source: Optional[str]       # "usgs" | "noaa" | None
    gauge_is_proxy: bool
    current_flow_cfs: Optional[float]
    local_flow_cfs: Optional[float]
    local_flow_source: Optional[str]
    water_temp_c: Optional[float]
    water_temp_source: Optional[str]
    flow_percentile: Optional[float]
    flow_stats: Optional[Dict[str, float]]       # p10..p90 so we can percentile-ize forecasted flow
    recent_flows: List[float]
    confidence_notes: List[str]
    # Forward-looking hydrology
    forecast_flow_by_hour: Optional[Dict[str, float]] = None  # ISO hour → forecast cfs (NOAA NWPS)
    qpf_mm_by_hour: Optional[Dict[str, float]] = None          # ISO hour → mm precip (NWS QPF)
    pressure_pa_by_hour: Optional[Dict[str, float]] = None     # ISO hour → air pressure (Pa)
    # Air/thermal augmentation
    anomaly_f: Optional[float] = None
    hatch_shift_days: float = 0.0
    dd_by_base_c: Optional[Dict[float, float]] = None


def _fetch_reach_signals(reach: Dict[str, object], species_base_temps_c: List[float]) -> ReachSignals:
    usgs_id = reach.get("usgs_gauge_id")
    noaa_lid = reach.get("noaa_lid")
    notes: List[str] = []
    flow_cfs: Optional[float] = None
    water_temp_c: Optional[float] = None
    water_temp_source: Optional[str] = None
    percentile: Optional[float] = None
    stats: Optional[Dict[str, float]] = None
    recent: List[float] = []
    source: Optional[str] = None
    spring_influenced = bool(reach.get("spring_influenced"))
    lat = float(reach["centroid_lat"])
    lon = float(reach["centroid_lon"])
    reach_id = str(reach["reach_id"])
    forecast_flow_by_hour: Dict[str, float] = {}
    local_flow_cfs: Optional[float] = None
    local_flow_source: Optional[str] = None

    if usgs_id:
        source = "usgs"
        try:
            readings = fetch_latest_iv(usgs_id)
            if "00060" in readings:
                flow_cfs = readings["00060"]["value"]
            # Water temp is local in a way flow isn't — a proxy gauge in a
            # different watershed can carry meaningful flow signal but its
            # water temperature is *not* representative of our reach. Force
            # Mohseni for proxied reaches; only inherit gauge water temp when
            # the reach owns the gauge.
            if "00010" in readings and not reach.get("gauge_is_proxy"):
                water_temp_c = readings["00010"]["value"]
                water_temp_source = "gauge"
            elif "00010" in readings and reach.get("gauge_is_proxy"):
                notes.append("proxy gauge reports water temp but skipping it (different watershed)")
        except requests.RequestException as exc:
            LOG.warning("USGS IV failed for %s: %s", usgs_id, exc)
            notes.append("live flow reading unavailable")
        today = datetime.now(timezone.utc).date()
        stats = fetch_daily_stats(usgs_id, today.month, today.day)
        if stats and flow_cfs is not None:
            percentile = discharge_percentile(flow_cfs, stats)
        elif flow_cfs is not None:
            notes.append("no long-term percentile available for this gauge")
        # Recent flow trend — used to drive flow_trend_score. Previously this
        # was a hardcoded [] so the trend factor was always the 0.75 neutral
        # default, making it dead signal. Last 4 days of daily values is
        # enough to see rising vs falling without hammering USGS.
        try:
            start = datetime.combine(today - timedelta(days=4), datetime.min.time(), tzinfo=timezone.utc)
            end = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
            dv_rows = fetch_daily(usgs_id, "00060", start_date=start, end_date=end)
            recent = [float(r["value"]) for r in dv_rows if r.get("value") is not None]
            if flow_cfs is not None:
                recent.append(float(flow_cfs))
        except requests.RequestException as exc:
            LOG.info("USGS daily-flow history failed for %s: %s", usgs_id, exc)
    if noaa_lid:
        try:
            noaa_readings = fetch_latest_nwps(noaa_lid)
            if "00060" in noaa_readings:
                local_flow_cfs = noaa_readings["00060"]["value"]
                local_flow_source = "noaa"
                if flow_cfs is None:
                    flow_cfs = local_flow_cfs
                    source = "noaa"
        except requests.RequestException as exc:
            LOG.warning("NWPS failed for %s: %s", noaa_lid, exc)
            if not usgs_id:
                notes.append("live flow reading unavailable")

    if usgs_id and noaa_lid and reach.get("gauge_is_proxy") and local_flow_cfs is not None:
        notes.append("local NOAA flow available; USGS proxy used for percentile climatology")
    elif noaa_lid and not usgs_id:
        source = "noaa"
        notes.append("flow percentile unavailable (NOAA gauge)")
    elif not usgs_id and not noaa_lid:
        notes.append("no gauge mapped")

    # NOAA streamflow forecast time series (not all gauges have one — small
    # tributaries usually don't get operational forecasts).
    if noaa_lid:
        try:
            fc_series = fetch_forecast_series(noaa_lid)
            for row in fc_series:
                sec = row.get("secondary")
                unit = (row.get("secondary_unit") or "").lower()
                valid = row.get("valid_at")
                if sec is None or valid is None:
                    continue
                if unit == "kcfs":
                    cfs = float(sec) * 1000.0
                elif unit == "cfs":
                    cfs = float(sec)
                else:
                    continue
                try:
                    dt = datetime.fromisoformat(valid.replace("Z", "+00:00"))
                except ValueError:
                    continue
                hour_iso = dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
                forecast_flow_by_hour[hour_iso] = cfs
            if forecast_flow_by_hour:
                notes.append(f"NOAA streamflow forecast available ({len(forecast_flow_by_hour)}h)")
        except Exception:
            LOG.exception("NWPS forecast series failed for %s", noaa_lid)

    if reach.get("gauge_is_proxy"):
        notes.append("flow from proxy gauge, not this reach")

    # Thermal augmentation — Open-Meteo historical air temp → Mohseni water temp.
    anomaly: Optional[anomaly_mod.Anomaly] = None
    shift = 0.0
    dd_by_base: Optional[Dict[float, float]] = None
    try:
        anomaly = anomaly_mod.compute(lat, lon)
        if anomaly is not None:
            shift = anomaly_mod.shift_days(anomaly.anomaly_f, spring_influenced)
    except Exception:
        LOG.exception("anomaly compute failed for %s", reach_id)

    try:
        dd_by_base = dd_pipeline.build_for_reach(
            reach_id=reach_id, lat=lat, lon=lon,
            spring_influenced=spring_influenced,
            species_base_temps_c=species_base_temps_c,
        )
    except Exception:
        LOG.exception("dd pipeline failed for %s", reach_id)

    # Estimate water temp when gauge doesn't report it.
    if water_temp_c is None:
        today = datetime.now(timezone.utc).date()
        try:
            end = today - timedelta(days=dd_pipeline.ARCHIVE_TRAIL_DAYS)
            start = end - timedelta(days=9)
            rows = fetch_archive_daily_mean_f(lat, lon, start, end)
            air_daily_f = [t for _d, t in rows]
            t_f = temp_estimator.estimate_current_water_f(air_daily_f, spring_influenced)
            if t_f is not None:
                water_temp_c = (t_f - 32.0) * 5.0 / 9.0
                water_temp_source = "estimate"
                notes.append("water temp estimated from air temp (Mohseni)")
        except requests.RequestException as exc:
            LOG.warning("openmeteo fetch failed for %s: %s", reach_id, exc)
            notes.append("no live water temperature; no Open-Meteo fallback")

    return ReachSignals(
        reach_id=reach_id,
        stream_name=str(reach["stream_name"]),
        lat=lat,
        lon=lon,
        spring_influenced=spring_influenced,
        usgs_gauge_id=str(usgs_id) if usgs_id else None,
        gauge_source=source,
        gauge_is_proxy=bool(reach.get("gauge_is_proxy")),
        current_flow_cfs=flow_cfs,
        local_flow_cfs=local_flow_cfs,
        local_flow_source=local_flow_source,
        water_temp_c=water_temp_c,
        water_temp_source=water_temp_source,
        flow_percentile=percentile,
        flow_stats=stats,
        recent_flows=recent,
        confidence_notes=notes,
        forecast_flow_by_hour=forecast_flow_by_hour or None,
        qpf_mm_by_hour=None,  # populated later once gridpoint is resolved
        anomaly_f=anomaly.anomaly_f if anomaly else None,
        hatch_shift_days=shift,
        dd_by_base_c=dd_by_base,
    )


def _fetch_qpf_by_hour(gridpoint: str) -> Dict[str, float]:
    """Hourly-keyed precipitation (mm) map. Same hour format as NOAA forecasts."""
    out: Dict[str, float] = {}
    try:
        periods = fetch_gridpoint_qpf_mm(gridpoint)
    except requests.RequestException as exc:
        LOG.warning("QPF fetch failed for %s: %s", gridpoint, exc)
        return out
    for dt, mm in periods:
        hour_iso = dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
        out[hour_iso] = out.get(hour_iso, 0.0) + float(mm)
    return out


def _fetch_pressure_by_hour(lat: float, lon: float) -> Dict[str, float]:
    """Hourly surface pressure (hPa = mb) keyed by UTC ISO hour, from Open-Meteo.
    NWS gridpoints API does not expose pressure for our region — we hit
    Open-Meteo's forecast endpoint which carries surface_pressure cleanly."""
    out: Dict[str, float] = {}
    try:
        periods = fetch_hourly_pressure_hpa(lat, lon, past_days=1, forecast_days=7)
    except requests.RequestException as exc:
        LOG.warning("pressure fetch failed for (%.3f, %.3f): %s", lat, lon, exc)
        return out
    for dt, hpa in periods:
        hour_iso = dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
        out[hour_iso] = float(hpa)
    return out


def _pressure_trend_factor(pressure_map: Optional[Dict[str, float]], valid_at: datetime) -> Tuple[float, Optional[str]]:
    """6-hour pressure trend at `valid_at`. Returns (multiplier, optional explanation note).

    Anglers consistently report falling barometer = great fishing right before a
    front, rising barometer post-frontal = slow recovery for ~24h. Magnitudes
    here are calibrated against guide consensus, not peer-reviewed lit.
    """
    if not pressure_map:
        return 1.0, None
    iso_now = valid_at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
    iso_6h_ago = (valid_at - timedelta(hours=6)).astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
    p_now = pressure_map.get(iso_now)
    p_then = pressure_map.get(iso_6h_ago)
    if p_now is None or p_then is None:
        return 1.0, None
    # Open-Meteo returns hPa (= mb) directly, no Pa→mb conversion.
    delta_mb = p_now - p_then
    if delta_mb <= -4:
        return 1.05, "barometer falling fast (pre-front feeding window)"
    if delta_mb <= -1.5:
        return 1.02, "barometer falling"
    if delta_mb >= 4:
        return 0.82, "barometer rising sharply (post-frontal slump)"
    if delta_mb >= 1.5:
        return 0.92, "barometer rising"
    return 1.0, None


# Stormflow recession time constants τ (hours) for the exponential decay
# Q(t) = Q_b + (Q₀ − Q_b) · exp(−t/τ).  Half-life t½ = τ · ln(2).
#
# These are *class-level priors*, not per-gauge fits. The right next step is
# Brutsaert–Nieber lower-envelope analysis on USGS daily-values history per
# gauge (see docs/REFERENCES.md#brutsaert_nieber_1977 and #tallaksen_1995).
# Until then:
#   - Driftless freestone watersheds (Apple, Kickapoo, Upper Iowa headwater
#     reaches) flash fast and recede in days because of steep dissected-valley
#     topography and shallow soils — see docs/REFERENCES.md#juckem_2008 for
#     the regional hydrologic context.
#   - Spring-fed / bedrock-aquifer-influenced reaches (Kinnickinnic, Whitewater
#     forks, Trout Run, Pine Cr., parts of Rush) recede more slowly because
#     groundwater contribution dampens the storm response — high baseflow
#     fraction documented in docs/REFERENCES.md#gebert_2011 and
#     #juckem_2008.
# Default priors are τ=30h freestone and τ=48h spring-fed. They live in
# src.models.recession so production, fitting, and backtests cannot quietly
# drift apart. The Pilgrim & Cordery (1992) range of 11–53 days applies to
# *baseflow* (slow tail), not the event-flow recession we model here.


# Diurnal water-temperature swing — amplitude (°F) of the daily sinusoid that
# rides on top of the Mohseni daily mean. Mohseni 1998 uses a 7-day rolling
# mean and intentionally smooths over within-day variation, so we model the
# diurnal cycle separately. Amplitudes and phase from:
#   - Caissie 2006 (Freshwater Biology, 51, 1389–1406) — reports typical
#     diurnal ranges of 1–10°C for temperate streams, with the low end for
#     groundwater-influenced reaches and the high end for unshaded reaches;
#     peaks at 14:00–18:00 local with a ~3–4h phase lag from solar noon.
#   - Sinokrot & Stefan 1993 (WRR, 29(7), 2299–2312) — hourly energy-balance
#     model confirming the sinusoidal diurnal pattern and phase-lag behavior.
# Our ±3°F freestone / ±1°F spring-fed half-amplitudes (so total peak-to-trough
# is 2× these) sit at the conservative low end of Caissie 2006's reported
# ranges. 17:00 peak phase fits the upper end of the empirical 14:00–18:00
# window for unshaded Driftless reaches.
DIURNAL_AMP_FREESTONE_F = 3.0
DIURNAL_AMP_SPRING_F    = 1.0
DIURNAL_PEAK_HOUR_LOCAL = 17

# Terrestrials need warm air to be active food; on a 55°F July day there are
# no beetles in the grass. Ramp 45°F → 65°F linearly, full credit above.
TERRESTRIAL_AIR_FLOOR_F = 45.0
TERRESTRIAL_AIR_FULL_F  = 65.0


def _flow_percentile_for_hour(
    signals: ReachSignals,
    valid_at: datetime,
    now: datetime,
) -> Tuple[float, Optional[float], Optional[str], Optional[float], str]:
    """Per-hour flow percentile with explanatory note.

    Priority:
      1. NOAA streamflow forecast (if gauge has one) — percentile-ize the
         forecasted flow against day-of-year stats. Authoritative when
         available; small Driftless gauges usually have no NWPS LID.
      2. Exponential recession of the current observed flow toward the
         day-of-year median (USGS p50), then add a precipitation-driven
         bump from preceding-24h QPF. Recession constants are class-level
         (freestone vs. spring-fed) — see TAU_* constants above and
         docs/REFERENCES.md#tallaksen_1995.
      3. Static current percentile when neither stats nor QPF are available.
    """
    hour_iso = valid_at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
    baseline_pct = signals.flow_percentile if signals.flow_percentile is not None else 0.5
    qpf_map = signals.qpf_mm_by_hour or {}

    # 1) NOAA NWPS streamflow forecast — preferred when gauge has one.
    fc_flow = (signals.forecast_flow_by_hour or {}).get(hour_iso)
    if fc_flow is not None and signals.flow_stats:
        if signals.gauge_is_proxy and signals.local_flow_cfs and signals.current_flow_cfs:
            # When NOAA is the nearby/local gauge but USGS is only a proxy
            # with historical percentiles, do not percentile-ize raw NOAA cfs
            # against the proxy gauge's climatology. Use the local forecast's
            # relative change to nudge the proxy baseline instead.
            ratio = fc_flow / max(float(signals.local_flow_cfs), 1.0)
            proxy_flow = float(signals.current_flow_cfs) * ratio
            pct = discharge_percentile(proxy_flow, signals.flow_stats)
            return pct, fc_flow, "local NOAA flow trend + USGS proxy percentile", None, "noaa_usgs_fused"
        return discharge_percentile(fc_flow, signals.flow_stats), float(fc_flow), None, None, "noaa_forecast"

    # 2a) Exponential recession of *flow* (not percentile — the percentile
    # mapping is non-linear, so decaying in flow space and re-percentile-izing
    # is the physically correct move). See Tallaksen 1995 §3 for the
    # exponential form and its limits.
    Q_now = signals.current_flow_cfs
    Q_med = (signals.flow_stats or {}).get("p50") if signals.flow_stats else None
    base_pct: float = baseline_pct
    Q_proj: Optional[float] = Q_now  # default to current obs when we can't recess
    tau_used: Optional[float] = None
    tau_source = "none"
    if Q_now is not None and Q_med is not None and signals.flow_stats:
        hours_ahead = max(0.0, (valid_at - now).total_seconds() / 3600.0)
        tau, tau_source, _fit_meta = calibrated_tau_hours(
            signals.reach_id,
            signals.usgs_gauge_id,
            signals.spring_influenced,
        )
        tau_used = tau
        # Decay deviation from median toward zero. Works in both directions:
        # current flow above median decays down toward median; current flow
        # below median rises up toward median (drought relief is symmetric in
        # this simple form — that's a known limitation, see honest_limits).
        Q_proj = project_flow(Q_now, Q_med, tau, hours_ahead)
        base_pct = discharge_percentile(Q_proj, signals.flow_stats)

    display_flow = Q_proj
    if signals.gauge_is_proxy and signals.local_flow_cfs is not None:
        # Score remains based on the proxy gauge's percentile climatology, but
        # the flow value shown to anglers should be the nearby/local gauge.
        display_flow = signals.local_flow_cfs

    # 2b) Precip bump on top of the recessed baseline. Driftless streams flash
    # fast: 12.7 mm (0.5") in the preceding 24h already moves the needle.
    if not qpf_map:
        return base_pct, display_flow, None, tau_used, tau_source
    preceding_mm = 0.0
    for h in range(1, 25):
        prior = (valid_at - timedelta(hours=h)).astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
        preceding_mm += qpf_map.get(prior, 0.0)
    if preceding_mm < 3.0:
        return base_pct, display_flow, None, tau_used, tau_source
    bump = min(0.45, (preceding_mm - 3.0) / 70.0)
    adjusted = min(0.98, base_pct + bump)
    note = None
    if preceding_mm >= 12.7:  # ≥ 0.5"
        inches = preceding_mm / 25.4
        note = f"rain in preceding 24h (~{inches:.1f}\")"
    return adjusted, display_flow, note, tau_used, tau_source


def _diurnal_water_temp_f(daily_mean_f: float, local_hour: int, spring_influenced: bool) -> float:
    """Add a diurnal sinusoid to the Mohseni daily mean so within-day variation
    actually shows up in the score. Phase peaks at 17:00 local, troughs at 05:00.
    Spring-fed reaches barely move (groundwater buffering); freestones swing.
    """
    import math
    amplitude = DIURNAL_AMP_SPRING_F if spring_influenced else DIURNAL_AMP_FREESTONE_F
    phase = (local_hour - DIURNAL_PEAK_HOUR_LOCAL) * 2.0 * math.pi / 24.0
    return daily_mean_f + amplitude * math.cos(phase)


def _terrestrial_air_factor(air_temp_f: Optional[float]) -> float:
    """0 below 45°F, linear ramp to 1.0 at 65°F. Above 65°F: full credit."""
    if air_temp_f is None:
        return 1.0
    if air_temp_f >= TERRESTRIAL_AIR_FULL_F:
        return 1.0
    if air_temp_f <= TERRESTRIAL_AIR_FLOOR_F:
        return 0.0
    span = TERRESTRIAL_AIR_FULL_F - TERRESTRIAL_AIR_FLOOR_F
    return (air_temp_f - TERRESTRIAL_AIR_FLOOR_F) / span


def _dry_warm_water_factor(water_temp_f: Optional[float]) -> float:
    """Dry-fly score scales down as water warms past the C&R ethics threshold.

    The nymph score already gets this dampening for free via the plateau curve
    in nymph_score.temperature_score. Dry score had no such gate — a hatch
    coded for 13:00 on a 72°F-water day still scored full credit. Wrong: fish
    are stressed, takes are sluggish, and we don't want to send anglers out
    at midday on 71°F water. Mirror the plateau slope (Wilkie 1996 threshold).
    """
    if water_temp_f is None:
        return 1.0
    if water_temp_f <= 64.0:
        return 1.0
    if water_temp_f <= 68.0:
        return 1.0 - (water_temp_f - 64.0) * (0.5 / 4.0)
    if water_temp_f <= 75.0:
        return 0.5 - (water_temp_f - 68.0) * (0.5 / 7.0)
    return 0.0


def _build_water_temp_by_date(
    signals: ReachSignals,
    periods: List[Dict[str, object]],
    now: datetime,
) -> Dict[date, float]:
    """Per-day water-temp °F across the forecast horizon.

    Mohseni (1998) tracks water temp as a logistic of the *7-day rolling mean*
    of air temp. The previous implementation ran this once on a historical
    window and used the result for all 168 forecast hours — so a forecasted
    warming trend never propagated to water. This builds a daily air-temp
    series from (historical Open-Meteo + forecasted NWS hourly aggregated to
    daily) and Mohseni-projects forward day-by-day.

    See docs/REFERENCES.md#mohseni_1998. Per-reach Mohseni coefficients still
    come from data/calibration/mohseni_fit.json or the class-level priors in
    `temp_estimator`.
    """
    today = now.astimezone(timezone.utc).date()
    air_by_date: Dict[date, float] = {}

    # Historical daily-mean air from Open-Meteo. ARCHIVE_TRAIL_DAYS lag is
    # because Open-Meteo's archive lags ~5 days behind realtime.
    try:
        hist_end = today - timedelta(days=dd_pipeline.ARCHIVE_TRAIL_DAYS)
        hist_start = hist_end - timedelta(days=12)
        rows = fetch_archive_daily_mean_f(signals.lat, signals.lon, hist_start, hist_end)
        for d, t in rows:
            air_by_date[d] = float(t)
    except Exception:
        LOG.exception("historical air fetch failed for %s", signals.reach_id)

    # Forecast: aggregate NWS hourly air temps into daily means. Days with no
    # hourly data are skipped (Mohseni's rolling mean fills the gap from
    # neighbors).
    by_day_temps: Dict[date, List[float]] = {}
    for p in periods:
        valid_iso = p.get("valid_at")
        air = p.get("air_temp_f")
        if not valid_iso or air is None:
            continue
        try:
            dt = _parse_valid_at(valid_iso)
        except ValueError:
            continue
        by_day_temps.setdefault(dt.date(), []).append(float(air))
    for d, temps in by_day_temps.items():
        if temps and d not in air_by_date:
            air_by_date[d] = sum(temps) / len(temps)

    if not air_by_date:
        return {}

    # Run Mohseni rolling-mean over the contiguous date range. We reindex to
    # fill any gaps with linear interpolation so the rolling mean isn't biased
    # by missing days.
    sorted_dates = sorted(air_by_date)
    start, end = sorted_dates[0], sorted_dates[-1]
    contiguous: List[date] = []
    air_series: List[float] = []
    d = start
    last_known = air_by_date[start]
    while d <= end:
        if d in air_by_date:
            last_known = air_by_date[d]
        contiguous.append(d)
        air_series.append(last_known)
        d += timedelta(days=1)

    water_series = estimate_water_series_f(air_series, signals.spring_influenced)
    return dict(zip(contiguous, water_series))


def _resolve_gridpoint(reach_id: str, lat: float, lon: float, cached: Optional[str]) -> Optional[str]:
    if cached:
        return cached
    try:
        point = fetch_gridpoint(lat, lon)
    except requests.RequestException as exc:
        LOG.warning("points fetch failed for %s: %s", reach_id, exc)
        return None
    props = (point or {}).get("properties", {})
    grid_id = props.get("gridId")
    grid_x = props.get("gridX")
    grid_y = props.get("gridY")
    if not grid_id or grid_x is None or grid_y is None:
        return None
    gridpoint = f"{grid_id}/{grid_x},{grid_y}"
    conn = get_connection()
    conn.execute("UPDATE reach SET nws_gridpoint = ? WHERE reach_id = ?", (gridpoint, reach_id))
    conn.commit()
    conn.close()
    return gridpoint


def _fetch_hourly_weather(gridpoint: str) -> List[Dict[str, object]]:
    try:
        forecast = fetch_hourly_forecast(gridpoint)
    except requests.RequestException as exc:
        LOG.warning("hourly forecast fetch failed for %s: %s", gridpoint, exc)
        return []
    parsed = parse_hourly_forecast(forecast or {})
    return parsed.get("periods", [])


def _parse_valid_at(valid_at: str) -> datetime:
    return datetime.fromisoformat(valid_at.replace("Z", "+00:00"))


def _f_to_c(temp_f: Optional[float]) -> Optional[float]:
    return None if temp_f is None else (temp_f - 32.0) * 5.0 / 9.0


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _format_flow_clause(percentile: Optional[float]) -> str:
    if percentile is None:
        return "flows: no historical context"
    pct_label = f"~{_ordinal(round(percentile * 100))} pct"
    if percentile < 0.10:
        return f"flows very low ({pct_label})"
    if percentile < 0.25:
        return f"flows low ({pct_label})"
    if percentile <= 0.75:
        return f"flows normal ({pct_label})"
    if percentile <= 0.90:
        return f"flows high ({pct_label})"
    return f"flows very high ({pct_label})"


def _format_water_clause(water_temp_f: Optional[float], source: Optional[str]) -> str:
    """Match the plateau curve in nymph_score.temperature_score so explanation
    copy and the score multiplier agree on what 'ideal' means."""
    if water_temp_f is None:
        return "no water-temp estimate available"
    if water_temp_f < 42:
        band = "very cold — fish lethargic"
    elif water_temp_f < 52:
        band = "cold but feeding — nymphing"
    elif water_temp_f <= 64:
        band = "ideal trout range"
    elif water_temp_f <= 68:
        band = "warm side of ideal"
    elif water_temp_f < 75:
        band = "stressful — fish early/late only"
    else:
        band = "too warm — do not stress fish"
    label = "water" if source == "gauge" else "water (est.)"
    return f"{label} {water_temp_f:.0f}°F, {band}"


def _score_hour(
    signals: ReachSignals,
    valid_at: datetime,
    air_temp_f: Optional[float],
    cloud_cover: Optional[float],
    wind_mph: Optional[float],
    precip_prob: Optional[float],
    short_forecast: Optional[str],
    species_list: List[Dict[str, object]],
    now: datetime,
    water_temp_f_by_date: Optional[Dict[date, float]] = None,
) -> Dict[str, object]:
    valid_hour = valid_at.hour
    valid_date = valid_at.date()
    # Per-day Mohseni projection gives the daily mean; add a diurnal sinusoid
    # so 2pm and 7pm on the same hot day score differently. Without this, the
    # whole hot day reads as one flat water temp — which is what produced the
    # "model says good midday, fishing was dead until 6pm" miss.
    water_temp_f: Optional[float] = None
    if water_temp_f_by_date and valid_date in water_temp_f_by_date:
        water_temp_f = _diurnal_water_temp_f(
            water_temp_f_by_date[valid_date], valid_hour, signals.spring_influenced
        )
    elif signals.water_temp_c is not None:
        water_temp_f = signals.water_temp_c * 9 / 5 + 32

    terrestrial_air = _terrestrial_air_factor(air_temp_f)

    species_by_id = {str(sp["species_id"]): sp for sp in species_list}
    dd_by_base = signals.dd_by_base_c or {}
    active_species_payload: List[Dict[str, object]] = []
    for sp in species_list:
        sp_id = str(sp["species_id"])
        is_terrestrial = sp_id in TERRESTRIAL_SPECIES
        # Seasonal (anomaly-shifted) — when does the Driftless calendar say to expect it?
        season = seasonal_activity(sp_id, valid_date, signals.hatch_shift_days)
        if season <= 0.05:
            continue
        # Calibrated DD readiness — is this season's heat actually accumulated?
        # When DD computation failed or threshold is absurdly off-calibration,
        # dd_factor defaults to 1.0 (neutral) so seasonal still drives scoring.
        base_c = float(sp.get("base_temp_c") or 5.0)
        dd_current = dd_by_base.get(base_c, 0.0)
        dd_mean = float(sp.get("dd_threshold_mean") or 0.0)
        dd_sd = float(sp.get("dd_threshold_sd") or 1.0)
        if dd_mean > 0 and dd_current > 0:
            dd_factor = species_activity_probability(dd_current, dd_mean, dd_sd)
            # Floor to 0.15 so a seasonal peak isn't entirely squashed when our
            # DD estimate is off — calibration data is sparse outside Hex.
            dd_factor = max(0.15, dd_factor)
        else:
            dd_factor = 1.0

        weather = weather_match_score(
            sp.get("weather_prefs") or {},
            cloud_cover if cloud_cover is not None else 0.5,
            wind_mph if wind_mph is not None else 10.0,
        )
        # Hot days push hatches into evening — slide the species window later
        # based on the *hour's* air temp from the NWS hourly forecast.
        sp_start = int(sp.get("emergence_hr_start") or 0)
        sp_end = int(sp.get("emergence_hr_end") or 23)
        shifted_start, shifted_end = shift_window_for_air_temp(sp_start, sp_end, air_temp_f)
        window = hour_of_day_score(valid_hour, shifted_start, shifted_end)
        temp_gate = 0.0 if (water_temp_f is not None and water_temp_f < 45) else 1.0
        air_gate = terrestrial_air if is_terrestrial else 1.0
        score = max(0.0, min(1.0, season * dd_factor * weather * window * temp_gate * air_gate))
        if score > 0.0:
            active_species_payload.append({
                "id": sp_id,
                "common_name": sp.get("common_name"),
                "scientific_name": sp.get("scientific_name"),
                "probability": score,
                "dd_progress": (dd_current / dd_mean) if dd_mean > 0 else None,
                "is_terrestrial": is_terrestrial,
            })

    # Aquatic priority: when a real aquatic hatch is firing, terrestrials are
    # background noise (fish that are eating BWOs aren't switching to a stray
    # beetle that drifts by). Halve terrestrial probabilities so they don't
    # win the fly_recommender's "primary" slot during an active hatch.
    aquatic_active = any(
        not s.get("is_terrestrial") and (s.get("probability") or 0) >= 0.30
        for s in active_species_payload
    )
    if aquatic_active:
        for s in active_species_payload:
            if s.get("is_terrestrial"):
                s["probability"] = (s.get("probability") or 0.0) * 0.5
        active_species_payload = [s for s in active_species_payload if (s.get("probability") or 0) > 0.05]

    best_dry = max((s["probability"] for s in active_species_payload), default=0.0)

    percentile, projected_flow_cfs, precip_note, tau_hours, tau_source = _flow_percentile_for_hour(signals, valid_at, now)
    nymph = compute_nymph_score(
        temp_f=water_temp_f,
        flow_percentile=percentile,
        recent_flows=signals.recent_flows,
        valid_at=valid_at.isoformat(),
        dd_current=0.0,
        species_thresholds=[],
    )
    # Capture each multiplier separately so the UI can show *why* a score is
    # what it is. This is the audit trail — every component a user clicks
    # through to should trace back to one of these numbers.
    from src.models.nymph_score import (
        flow_percentile_score as _flow_pct_score,
        flow_trend_score as _flow_trend_score,
        temperature_score as _temp_score,
    )
    breakdown_temp_score = _temp_score(water_temp_f) if water_temp_f is not None else 0.0
    breakdown_flow_pct_score = _flow_pct_score(percentile)
    breakdown_flow_trend_score = _flow_trend_score(signals.recent_flows)
    breakdown_top_species = None
    if active_species_payload:
        top = max(active_species_payload, key=lambda s: s.get("probability") or 0)
        breakdown_top_species = {
            "id": top.get("id"),
            "common_name": top.get("common_name"),
            "probability": top.get("probability"),
        }

    flies = recommend_flies(active_species_payload, species_by_id, valid_hour)

    # ── Bright-sun penalty on dry score: clear-water Driftless trout get spooky
    # under bright high-angle sun. Cloud cover or low sun angle removes the penalty.
    sun_alt = sun_altitude_deg(signals.lat, signals.lon, valid_at)
    sun_factor = bright_sun_dry_penalty(sun_alt, cloud_cover)
    best_dry *= sun_factor

    # Dry score didn't previously care about warm water — a hatch coded for
    # 1pm on 71°F water still scored full credit. Mirror the nymph plateau
    # slope so the dry score also falls past 68°F (Wilkie 1996 C&R threshold).
    warm_water_factor = _dry_warm_water_factor(water_temp_f)
    best_dry *= warm_water_factor

    # ── Barometric pressure trend factor on combined score (pre-front bump,
    # post-front slump). Whole bite is affected, not just surface. Clamp the
    # post-pressure values to [0, 1.0] — the pressure factor of 1.05 was
    # pushing displayed scores above 100 ("GO 102/100") whenever the
    # underlying nymph/dry hit the plateau. Score is a probability by
    # definition; nothing past 1.0 is meaningful.
    pressure_factor, pressure_note = _pressure_trend_factor(signals.pressure_pa_by_hour, valid_at)
    nymph_adj = max(0.0, min(1.0, nymph * pressure_factor))
    dry_adj = max(0.0, min(1.0, best_dry * pressure_factor))
    combined = max(nymph_adj, dry_adj)

    # ── Regime classification — what *kind* of fishing day is this? Runs after
    # the score so it can use post-pressure-adjusted dry/nymph values.
    regime = regime_mod.classify(
        valid_at=valid_at,
        flow_percentile=percentile,
        water_temp_f=water_temp_f,
        air_temp_f=air_temp_f,
        dry_score=dry_adj,
        nymph_score=nymph_adj,
        spring_influenced=signals.spring_influenced,
        qpf_map=signals.qpf_mm_by_hour,
        active_species=active_species_payload,
    )

    # BLOWOUT / HEAT_STRESS override combined score — telling someone to "go"
    # on a 6"-rain day is dangerous regardless of the model, and telling them
    # to fish a 90°F / 72°F-water midday hour is bad for the trout regardless
    # of how the underlying multipliers shook out. Cap combined low; nymph/dry
    # remain unmodified for transparency / debugging.
    if regime.code == "BLOWOUT":
        combined = min(combined, 0.10)
    elif regime.code == "HEAT_STRESS":
        combined = min(combined, 0.15)

    parts: List[str] = []
    # Use the per-hour percentile for the explanation so tomorrow's rain shows up.
    parts.append(_format_flow_clause(percentile))
    if precip_note:
        parts.append(precip_note)
    # Use the per-hour value (which reflects forecasted warming) rather than
    # signals.water_temp_c (which is a single now-snapshot).
    parts.append(_format_water_clause(water_temp_f, signals.water_temp_source))
    if pressure_note:
        parts.append(pressure_note)
    if sun_factor < 0.85:
        parts.append(f"bright midday sun (clear-water shyness)")
    if signals.anomaly_f is not None and abs(signals.anomaly_f) >= 2.0:
        direction = "warmer" if signals.anomaly_f > 0 else "cooler"
        parts.append(
            f"past 2 wks {abs(signals.anomaly_f):.1f}°F {direction} than normal "
            f"(hatches ~{abs(signals.hatch_shift_days):.0f}d {'early' if signals.anomaly_f>0 else 'late'})"
        )
    # Headline the top species only when its probability is high enough to
    # represent a real hatch event, not just "the species is broadly in season".
    # The previous unconditional headline produced lines like "tan-caddis in
    # window (3%)" — technically true, misleading because the 98 score next to
    # it was nymph-driven, not driven by 3% tan-caddis. 0.15 is a product
    # threshold (no peer-reviewed value defines what "a hatch" is), but it's
    # well above the 0.05 active-species floor and matches the threshold the
    # regime classifier uses for HATCH regime activation (≥0.30).
    SIGNIFICANT_HATCH_PROB = 0.15
    top_species = None
    if active_species_payload:
        top_species = sorted(active_species_payload, key=lambda s: s["probability"], reverse=True)[0]
    if top_species and (top_species.get("probability") or 0) >= SIGNIFICANT_HATCH_PROB:
        prog = top_species.get("dd_progress")
        prog_label = f" · DD {prog*100:.0f}% to peak" if prog is not None else ""
        parts.append(f"{top_species['id']} in window ({top_species['probability']*100:.0f}%{prog_label})")
    elif nymph > 0.4:
        parts.append("no significant hatch in window — nymphing play")
    if water_temp_f is not None and water_temp_f > 68:
        parts.append("WARNING: water stressful for trout — fish dawn/dusk only")
    explanation = "; ".join(parts)

    # 6h pressure delta (mb) — surface to UI as a chip when present, even
    # though the score multiplier is already baked in. Anglers want to know.
    pressure_delta_mb = None
    if signals.pressure_pa_by_hour:
        iso_now = valid_at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
        iso_then = (valid_at - timedelta(hours=6)).astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
        p_now = signals.pressure_pa_by_hour.get(iso_now)
        p_then = signals.pressure_pa_by_hour.get(iso_then)
        if p_now is not None and p_then is not None:
            pressure_delta_mb = p_now - p_then

    return {
        "reach_id": signals.reach_id,
        "valid_at": valid_at.isoformat(),
        "nymph_score": nymph_adj,
        "dry_score": dry_adj,
        "combined_score": combined,
        "active_species": json.dumps(active_species_payload),
        "recommended_flies": json.dumps(flies),
        "explanation": explanation,
        "water_temp_f": water_temp_f,
        "water_temp_source": signals.water_temp_source,
        "anomaly_f": signals.anomaly_f,
        "hatch_shift_days": signals.hatch_shift_days,
        "fish_stress": 1 if (water_temp_f is not None and water_temp_f > 68) else 0,
        "air_temp_f": air_temp_f,
        "cloud_cover": cloud_cover,
        "wind_mph": wind_mph,
        "flow_cfs": projected_flow_cfs if projected_flow_cfs is not None else signals.current_flow_cfs,
        "precip_prob": precip_prob,
        "short_forecast": short_forecast,
        "regime": json.dumps(regime_mod.regime_to_dict(regime)),
        "pressure_delta_mb": pressure_delta_mb,
        "score_breakdown": json.dumps({
            "temperature":     round(breakdown_temp_score, 3),
            "flow_percentile": round(breakdown_flow_pct_score, 3),
            "flow_trend":      round(breakdown_flow_trend_score, 3),
            "pressure_factor": round(pressure_factor, 3),
            "sun_factor":      round(sun_factor, 3),
            "top_species":     breakdown_top_species,
            "percentile_used": round(percentile, 3) if percentile is not None else None,
            "flow_tau_hours":  round(tau_hours, 1) if tau_hours is not None else None,
            "flow_tau_source": tau_source,
            "flow_display_source": "local_noaa_current" if (
                signals.gauge_is_proxy and signals.local_flow_cfs is not None
                and projected_flow_cfs == signals.local_flow_cfs
            ) else "model_projection",
        }),
    }


def build_for_reach(reach: Dict[str, object], species: List[Dict[str, object]]) -> int:
    species_base_temps_c = sorted({float(sp.get("base_temp_c") or 5.0) for sp in species})
    signals = _fetch_reach_signals(reach, species_base_temps_c)
    gridpoint = _resolve_gridpoint(
        str(reach["reach_id"]),
        float(reach["centroid_lat"]),
        float(reach["centroid_lon"]),
        reach.get("nws_gridpoint"),
    )
    if gridpoint:
        signals.qpf_mm_by_hour = _fetch_qpf_by_hour(gridpoint)
    # Pressure is fetched by lat/lon (Open-Meteo) regardless of gridpoint availability.
    signals.pressure_pa_by_hour = _fetch_pressure_by_hour(signals.lat, signals.lon)
    periods = _fetch_hourly_weather(gridpoint) if gridpoint else []

    now = datetime.now(timezone.utc)
    # Per-day water-temp projection — Mohseni applied to combined historical +
    # forecasted daily air-temp series, so a forecasted warming front actually
    # moves the water temp across the 7-day horizon.
    water_temp_f_by_date = _build_water_temp_by_date(signals, periods, now)
    horizon = now + timedelta(hours=FORECAST_HORIZON_H)
    computed_at = now.replace(microsecond=0).isoformat()
    rows: List[Dict[str, object]] = []

    selected = []
    for period in periods:
        valid_iso = period.get("valid_at")
        if not valid_iso:
            continue
        try:
            valid_at = _parse_valid_at(valid_iso)
        except ValueError:
            continue
        if valid_at < now - timedelta(hours=1) or valid_at > horizon:
            continue
        selected.append((valid_at, period))
    selected.sort(key=lambda x: x[0])

    for valid_at, period in selected:
        row = _score_hour(
            signals=signals,
            valid_at=valid_at,
            air_temp_f=period.get("air_temp_f"),
            cloud_cover=period.get("cloud_cover"),
            wind_mph=period.get("wind_mph"),
            precip_prob=period.get("precip_prob"),
            short_forecast=period.get("short_forecast"),
            species_list=species,
            now=now,
            water_temp_f_by_date=water_temp_f_by_date,
        )
        row["computed_at"] = computed_at
        rows.append(row)

    if not rows:
        # No weather — still write a single "now" prediction with whatever
        # we have so the UI doesn't go blank.
        valid_at = now.replace(microsecond=0)
        row = _score_hour(
            signals, valid_at, None, None, None, None, None, species, now,
            water_temp_f_by_date=water_temp_f_by_date,
        )
        row["computed_at"] = computed_at
        rows.append(row)

    conn = get_connection()
    conn.execute("DELETE FROM prediction WHERE reach_id = ?", (signals.reach_id,))
    conn.executemany(
        """
        INSERT INTO prediction (
            reach_id, valid_at, computed_at, nymph_score, dry_score,
            active_species, recommended_flies, explanation,
            water_temp_f, water_temp_source, anomaly_f, hatch_shift_days,
            fish_stress, air_temp_f, cloud_cover, wind_mph, flow_cfs,
            precip_prob, short_forecast, regime, pressure_delta_mb,
            score_breakdown
        ) VALUES (
            :reach_id, :valid_at, :computed_at, :nymph_score, :dry_score,
            :active_species, :recommended_flies, :explanation,
            :water_temp_f, :water_temp_source, :anomaly_f, :hatch_shift_days,
            :fish_stress, :air_temp_f, :cloud_cover, :wind_mph, :flow_cfs,
            :precip_prob, :short_forecast, :regime, :pressure_delta_mb,
            :score_breakdown
        )
        """,
        rows,
    )
    conn.commit()
    conn.close()
    return len(rows)


def build_all() -> Dict[str, int]:
    from src.db import load_reaches

    species = load_species()
    reaches = load_reaches()
    per_reach: Dict[str, int] = {}
    for reach in reaches:
        try:
            per_reach[reach["reach_id"]] = build_for_reach(reach, species)
        except Exception:
            LOG.exception("forecast build failed for %s", reach.get("reach_id"))
            per_reach[reach["reach_id"]] = 0
    return per_reach
