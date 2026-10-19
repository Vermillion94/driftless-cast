"""
Daily air temperature from NOAA's Applied Climate Information System (ACIS).

ACIS is the Regional Climate Centers' public data service. We use its NRCC
interpolated daily grid (grid "1", ~5 km, built from NWS Cooperative Observer
and other station observations) to get a daily mean air temperature for any
lat/lon, back to the 1950s, with only ~1 day of lag.

Why not Open-Meteo: its free API is licensed for non-commercial use only.
ACIS/NOAA data carries no such restriction, and its ~1-day lag beats the
Open-Meteo archive's ~5 days, so the "recent air" window that drives water
temperature and degree-days is fresher.
See docs/DATA_SOURCES.md.

Endpoint: https://data.rcc-acis.org/GridData  (POST JSON, no key)
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, timedelta
from typing import Dict, List, Tuple

import requests

from src.ingest.identity import USER_AGENT

LOG = logging.getLogger(__name__)

GRID_DATA_URL = "https://data.rcc-acis.org/GridData"
GRID = "1"                 # NRCC interpolated daily grid
MISSING = -999.0
TRAIL_DAYS = 1             # yesterday is the last complete day
CACHE_TTL_S = 6 * 3600     # daily climate values don't change within a day
_TIMEOUT_S = 30

_cache: Dict[Tuple[float, float, str, str], Tuple[float, List[Tuple[date, float]]]] = {}
_cache_lock = threading.Lock()


def _key(lat: float, lon: float, start: date, end: date):
    # Round to ~1 km so nearby reaches sharing a grid cell share a cache entry.
    return (round(lat, 2), round(lon, 2), start.isoformat(), end.isoformat())


def fetch_archive_daily_mean_f(lat: float, lon: float, start: date, end: date) -> List[Tuple[date, float]]:
    """Daily-mean air temperature (°F) for each day in [start, end] with data.

    Drop-in replacement for the former Open-Meteo archive call: same
    signature, same (date, °F) output, missing days omitted.
    Raises requests.RequestException on transport errors (callers handle it).
    """
    if end < start:
        return []
    key = _key(lat, lon, start, end)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_S:
            return list(hit[1])

    payload = {
        "loc": f"{lon:.4f},{lat:.4f}",
        "sdate": start.isoformat(),
        "edate": end.isoformat(),
        "grid": GRID,
        "elems": [{"name": "avgt"}],
    }
    resp = requests.post(GRID_DATA_URL, json=payload, timeout=_TIMEOUT_S,
                         headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise requests.RequestException(f"ACIS error: {body['error']}")
    out: List[Tuple[date, float]] = []
    for row in body.get("data", []):
        try:
            d = date.fromisoformat(row[0])
            t = float(row[1])
        except (IndexError, TypeError, ValueError):
            continue
        if t <= MISSING + 1:
            continue
        out.append((d, t))

    with _cache_lock:
        _cache[key] = (now, out)
        if len(_cache) > 4000:  # bound memory on a 256 MB machine
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:1000]:
                _cache.pop(k, None)
    return list(out)


def fetch_daily_mean_for_years(lat: float, lon: float, start_month: int, start_day: int,
                               end_month: int, end_day: int, years: List[int]) -> List[float]:
    """Daily-mean °F across `years` for a fixed month-day window (a local
    "normal" for the anomaly calculation)."""
    collected: List[float] = []
    for y in years:
        try:
            start = date(y, start_month, start_day)
            end = date(y, end_month, end_day)
        except ValueError:
            continue
        if end < start:  # window wraps the new year
            end = date(y + 1, end_month, end_day)
        try:
            rows = fetch_archive_daily_mean_f(lat, lon, start, end)
        except requests.RequestException as exc:
            LOG.warning("ACIS fetch failed for year %d: %s", y, exc)
            continue
        collected.extend(t for _d, t in rows)
    return collected


def latest_complete_day(today: date) -> date:
    return today - timedelta(days=TRAIL_DAYS)
