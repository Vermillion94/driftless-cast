"""
Per-observation DD calculation for calibration.

For each iNat observation (date, lat, lon) we:
  1. Fetch Jan 1 → observation date daily-mean air temp from Open-Meteo archive
  2. Run Mohseni to get daily-mean water temp
  3. Accumulate DD at the species' base temperature
  4. Return the DD-C value on the observation date

Air-temp archives are cached per (lat_cell, lon_cell, year) so adjacent
observations share one fetch. Cells are 0.5° — roughly 35 miles across, still
finer than MOHSENI's "same climate regime" assumption.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from src.ingest import fetch_archive_daily_mean_f
from src.models.temp_estimator import estimate_water_series_f
from src.models.dd_pipeline import accumulate_dd

LOG = logging.getLogger(__name__)

GRID = 0.5  # degrees — cache grid cell size
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "calibration" / "air_cache"


def _cell(lat: float, lon: float) -> Tuple[float, float]:
    return (round(lat / GRID) * GRID, round(lon / GRID) * GRID)


def _cache_path(lat_cell: float, lon_cell: float, year: int) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"air_{lat_cell:+.1f}_{lon_cell:+.1f}_{year}.json"


def _f_to_c(t_f: float) -> float:
    return (t_f - 32.0) * 5.0 / 9.0


def fetch_year_air(lat: float, lon: float, year: int) -> List[Tuple[date, float]]:
    """Daily-mean °F for Jan 1 → Dec 31 of `year` at the 0.5° grid cell containing (lat, lon)."""
    la, lo = _cell(lat, lon)
    cache = _cache_path(la, lo, year)
    if cache.exists():
        try:
            raw = json.loads(cache.read_text(encoding="utf-8"))
            return [(date.fromisoformat(d), float(t)) for d, t in raw]
        except (ValueError, TypeError) as exc:
            LOG.warning("bad cache %s: %s", cache, exc)
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    try:
        rows = fetch_archive_daily_mean_f(la, lo, start, end)
    except requests.RequestException as exc:
        LOG.warning("archive fetch failed for (%s, %s, %d): %s", la, lo, year, exc)
        return []
    cache.write_text(
        json.dumps([(d.isoformat(), t) for d, t in rows]),
        encoding="utf-8",
    )
    return rows


def dd_for_observation(
    obs_date: date,
    lat: float,
    lon: float,
    base_temp_c: float,
    spring_influenced: bool = False,
) -> Optional[float]:
    """Accumulated DD-C at the obs location, from Jan 1 to obs_date."""
    year_rows = fetch_year_air(lat, lon, obs_date.year)
    if not year_rows:
        return None
    up_to = [(d, t) for d, t in year_rows if d <= obs_date]
    if not up_to:
        return None
    air_f = [t for _d, t in up_to]
    water_f = estimate_water_series_f(air_f, spring_influenced)
    water_c = [_f_to_c(t) for t in water_f]
    # Use freestone-style accumulation by default: iNat observations come from
    # random sites across the Driftless, we don't know spring-influence per obs.
    # For calibration, freestone is the less-biased prior.
    series = accumulate_dd(water_c, base_temp_c)
    return series[-1] if series else None
