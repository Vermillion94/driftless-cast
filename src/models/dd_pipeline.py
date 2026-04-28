"""
Degree-day accumulation from estimated water temperatures.

Pipeline:
  1. Fetch daily-mean air temp from Open-Meteo for Mar 1 → today.
  2. Convert to daily-mean water temp via Mohseni (stream-class keyed).
  3. Accumulate DD for every species base temp (°C).
  4. Persist into the `dd_accumulation` table (keyed by reach + date + base_c).

The table already exists in schema.sql; we're just finally populating it.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

import requests

from src.db import get_connection
from src.ingest import fetch_archive_daily_mean_f
from src.models.temp_estimator import estimate_water_series_f, rolling_mean

LOG = logging.getLogger(__name__)

SEASON_START_MONTH = 3   # Mar 1 — before any Driftless mayfly emergence
SEASON_START_DAY = 1
ARCHIVE_TRAIL_DAYS = 5   # Open-Meteo archive trails real-time by ~5 days


def _f_to_c(t_f: float) -> float:
    return (t_f - 32.0) * 5.0 / 9.0


def _f_to_c_series(series: Iterable[float]) -> List[float]:
    return [_f_to_c(t) for t in series]


def season_start(today: date) -> date:
    return date(today.year, SEASON_START_MONTH, SEASON_START_DAY)


def accumulate_dd(water_temps_c: List[float], base_c: float) -> List[float]:
    """Standard agricultural DD accumulator — no negative contributions."""
    running = 0.0
    out: List[float] = []
    for t_c in water_temps_c:
        running += max(0.0, t_c - base_c)
        out.append(running)
    return out


def build_for_reach(
    reach_id: str,
    lat: float,
    lon: float,
    spring_influenced: bool,
    species_base_temps_c: List[float],
    today: Optional[date] = None,
) -> Dict[float, float]:
    """Refresh dd_accumulation rows for `reach_id` and return current-day DD by base."""
    today = today or datetime.now(timezone.utc).date()
    start = season_start(today)
    # Open-Meteo archive trails real-time; request up to a safe end date.
    end = today - timedelta(days=ARCHIVE_TRAIL_DAYS)
    if end < start:
        return {b: 0.0 for b in species_base_temps_c}

    try:
        rows = fetch_archive_daily_mean_f(lat, lon, start, end)
    except requests.RequestException as exc:
        LOG.warning("archive fetch failed for %s: %s", reach_id, exc)
        return {b: 0.0 for b in species_base_temps_c}
    if not rows:
        return {b: 0.0 for b in species_base_temps_c}

    dates = [d for d, _t in rows]
    air_f = [t for _d, t in rows]
    water_f = estimate_water_series_f(air_f, spring_influenced)
    water_c = _f_to_c_series(water_f)

    per_base: Dict[float, List[float]] = {}
    for base_c in species_base_temps_c:
        per_base[base_c] = accumulate_dd(water_c, base_c)

    # Upsert daily accumulations. A single DELETE + bulk INSERT is fine at
    # this volume (~60 days × ~5 bases × 21 reaches = 6000 rows worst case).
    conn = get_connection()
    conn.execute(
        "DELETE FROM dd_accumulation WHERE reach_id = ? AND date >= ?",
        (reach_id, start.isoformat()),
    )
    rows_to_write = []
    for base_c, accum in per_base.items():
        for d, a in zip(dates, accum):
            rows_to_write.append((reach_id, d.isoformat(), base_c, a))
    conn.executemany(
        "INSERT INTO dd_accumulation (reach_id, date, base_temp_c, accumulated) VALUES (?, ?, ?, ?)",
        rows_to_write,
    )
    conn.commit()
    conn.close()

    return {b: per_base[b][-1] if per_base[b] else 0.0 for b in species_base_temps_c}


def latest_water_temp_f(reach_id: str) -> Optional[float]:
    """Most recent water-temp estimate we've stored (derived from DD accumulation)."""
    # We don't store water temp directly; recompute from the last few DD deltas
    # at base 0°C. Simpler: fetch air temp and re-estimate at call time. Callers
    # that need this should use estimate_current_water_f directly — retained here
    # as a placeholder in case we start caching full water-temp series.
    return None
