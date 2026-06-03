"""
Open-Meteo historical air-temp ingest.

Free archive API, no auth, hourly temperature for any lat/lon going back decades.
Used to backfill DD accumulation and compute a recent-temp anomaly baseline.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Tuple

import requests

ARCHIVE_BASE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_BASE = "https://api.open-meteo.com/v1/forecast"
LOG = logging.getLogger(__name__)


def fetch_archive_daily_mean_f(lat: float, lon: float, start: date, end: date) -> List[Tuple[date, float]]:
    """Daily-mean air temperature (°F) for every day in [start, end] inclusive.

    Open-Meteo archive returns hourly values; we collapse to daily mean so DD
    accumulation stays stable even when a day has missing hours.
    """
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": "temperature_2m_mean",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
    }
    resp = requests.get(ARCHIVE_BASE, params=params, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    daily = payload.get("daily", {})
    dates = daily.get("time", [])
    temps = daily.get("temperature_2m_mean", [])
    out: List[Tuple[date, float]] = []
    for iso, t in zip(dates, temps):
        if t is None:
            continue
        try:
            d = date.fromisoformat(iso)
        except ValueError:
            continue
        out.append((d, float(t)))
    return out


def fetch_hourly_pressure_hpa(lat: float, lon: float,
                               past_days: int = 1, forecast_days: int = 7) -> List[Tuple[datetime, float]]:
    """Hourly surface pressure (hPa = mb) covering recent history + forecast.

    Past days lets us compute pressure-trend deltas at the very start of the
    forecast horizon. Returned timestamps are timezone-aware UTC.
    """
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "hourly": "surface_pressure",
        "past_days": past_days,
        "forecast_days": forecast_days,
        "timezone": "UTC",
    }
    resp = requests.get(FORECAST_BASE, params=params, timeout=30)
    resp.raise_for_status()
    hourly = resp.json().get("hourly", {})
    times = hourly.get("time", [])
    press = hourly.get("surface_pressure", [])
    out: List[Tuple[datetime, float]] = []
    for t, p in zip(times, press):
        if p is None:
            continue
        try:
            dt = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        out.append((dt, float(p)))
    return out


def fetch_archive_hourly_precip_mm(
    lat: float,
    lon: float,
    start: date,
    end: date,
) -> Dict[datetime, float]:
    """Hourly precipitation (mm) keyed by timezone-aware UTC datetime."""
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "hourly": "precipitation",
        "timezone": "UTC",
    }
    resp = requests.get(ARCHIVE_BASE, params=params, timeout=45)
    resp.raise_for_status()
    hourly = resp.json().get("hourly", {})
    times = hourly.get("time", [])
    precip = hourly.get("precipitation", [])
    out: Dict[datetime, float] = {}
    for t, mm in zip(times, precip):
        if mm is None:
            continue
        try:
            dt = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
            out[dt] = float(mm)
        except (TypeError, ValueError):
            continue
    return out


def fetch_daily_mean_for_years(lat: float, lon: float, start_month: int, start_day: int,
                                end_month: int, end_day: int, years: List[int]) -> List[float]:
    """Daily-mean °F across requested years for a fixed month-day window.

    Used to build a poor-man's normal for anomaly detection without requiring
    a separate climate-normals service.
    """
    collected: List[float] = []
    for y in years:
        try:
            start = date(y, start_month, start_day)
            end = date(y, end_month, end_day)
        except ValueError:
            continue
        try:
            rows = fetch_archive_daily_mean_f(lat, lon, start, end)
        except requests.RequestException as exc:
            LOG.warning("open-meteo archive failed for year %d: %s", y, exc)
            continue
        collected.extend(t for _d, t in rows)
    return collected
