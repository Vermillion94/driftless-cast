"""
Temperature anomaly — how warm/cold is the last ~2 weeks vs a local baseline?

Feeds the seasonal hatch calendar a correction so hatches shift forward in
warm springs and back in cold ones. Literature consensus: mayfly emergence
timing shifts roughly 3 days per 1°C of thermal anomaly. We use a slightly
smaller sensitivity since our anomaly is *air* temp and spring-fed water
only partially tracks it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

import requests

from src.ingest import fetch_archive_daily_mean_f, fetch_daily_mean_for_years

LOG = logging.getLogger(__name__)

WINDOW_DAYS = 14
BASELINE_YEARS = 5             # most recent N years as the normal
ARCHIVE_TRAIL_DAYS = 5         # Open-Meteo archive lags real-time

# Days of emergence shift per °F of 14-day air anomaly.
#   Freestone (air-tracking water)      ~1.5 days / °F
#   Spring-influenced (dampened water)  ~0.7 days / °F
SHIFT_DAYS_PER_F_FREESTONE = 1.5
SHIFT_DAYS_PER_F_SPRING = 0.7


@dataclass
class Anomaly:
    current_mean_f: float
    baseline_mean_f: float
    anomaly_f: float           # current - baseline (positive = warmer than normal)
    sample_days: int


def compute(lat: float, lon: float, today: Optional[date] = None) -> Optional[Anomaly]:
    today = today or datetime.now(timezone.utc).date()
    window_end = today - timedelta(days=ARCHIVE_TRAIL_DAYS)
    window_start = window_end - timedelta(days=WINDOW_DAYS - 1)

    try:
        current = fetch_archive_daily_mean_f(lat, lon, window_start, window_end)
    except requests.RequestException as exc:
        LOG.warning("anomaly current-window fetch failed: %s", exc)
        return None
    if len(current) < 5:
        return None
    current_mean = sum(t for _d, t in current) / len(current)

    baseline_years = list(range(today.year - BASELINE_YEARS, today.year))
    baseline_samples = fetch_daily_mean_for_years(
        lat, lon,
        window_start.month, window_start.day,
        window_end.month, window_end.day,
        baseline_years,
    )
    if len(baseline_samples) < 10:
        return None
    baseline_mean = sum(baseline_samples) / len(baseline_samples)

    return Anomaly(
        current_mean_f=current_mean,
        baseline_mean_f=baseline_mean,
        anomaly_f=current_mean - baseline_mean,
        sample_days=len(current),
    )


def shift_days(anomaly_f: float, spring_influenced: bool) -> float:
    sensitivity = SHIFT_DAYS_PER_F_SPRING if spring_influenced else SHIFT_DAYS_PER_F_FREESTONE
    # Cap at ±14 days — literature shifts beyond two weeks are unusual and
    # probably mean our baseline is off, not that hatches are actually that far shifted.
    raw = anomaly_f * sensitivity
    return max(-14.0, min(14.0, raw))
