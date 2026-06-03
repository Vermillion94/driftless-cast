"""Fit first-pass historical rain -> flow-response thresholds per gauged reach.

This is intentionally pragmatic rather than hydrologically perfect. We use:

* Open-Meteo hourly historical precipitation at the mapped gauge location
* USGS continuous discharge aggregated to hourly flow
* Local peak events from the hourly hydrograph

For each directly gauged reach we learn approximate "hurt starts near X rain in
6h / 12h / 24h" thresholds from historical events that actually pushed the
gauge into meaningfully elevated flow. Reaches without enough evidence keep the
class prior heuristic at runtime.
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Dict, List, Optional, Tuple

import requests

from src.ingest.openmeteo import fetch_archive_hourly_precip_mm
from src.ingest.usgs import STAT_BASE, discharge_percentile, fetch_continuous
from src.models.runoff_risk import CALIBRATION_PATH, assess_runoff_risk

ROOT = Path(__file__).resolve().parent.parent.parent
REACHES_PATH = ROOT / "data" / "seed" / "reaches.json"
GAUGES_PATH = ROOT / "data" / "seed" / "gauges.json"
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class PeakEvent:
    observed_at: datetime
    baseline_cfs: float
    peak_cfs: float
    rise_cfs: float
    rise_ratio: float
    peak_percentile: float
    rain_6h_mm: float
    rain_12h_mm: float
    rain_24h_mm: float


def _load_seed(path: Path) -> List[Dict[str, object]]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _daily_stats_table(gauge_id: str) -> Dict[Tuple[int, int], Dict[str, float]]:
    params = {
        "sites": gauge_id,
        "statReportType": "daily",
        "parameterCd": "00060",
        "statTypeCd": "p10,p25,p50,p75,p90",
        "format": "rdb",
    }
    resp = requests.get(STAT_BASE, params=params, timeout=45)
    resp.raise_for_status()
    lines = [ln for ln in resp.text.splitlines() if ln and not ln.startswith("#") and not ln.startswith("5s")]
    if len(lines) < 2:
        return {}
    header = lines[0].split("\t")
    col = {name: idx for idx, name in enumerate(header)}
    table: Dict[Tuple[int, int], Dict[str, float]] = {}
    for row in lines[1:]:
        parts = row.split("\t")
        try:
            key = (int(parts[col["month_nu"]]), int(parts[col["day_nu"]]))
        except (KeyError, ValueError, IndexError):
            continue
        stats: Dict[str, float] = {}
        for pct in ("p10", "p25", "p50", "p75", "p90"):
            idx = col.get(f"{pct}_va")
            if idx is None or idx >= len(parts) or not parts[idx].strip():
                continue
            try:
                stats[pct] = float(parts[idx])
            except ValueError:
                continue
        if len(stats) >= 3:
            table[key] = stats
    return table


def _hourly_flow_map(gauge_id: str, start: datetime, end: datetime) -> Dict[datetime, float]:
    rows = fetch_continuous(gauge_id, "00060", start_time=start, end_time=end)
    hourly: Dict[datetime, float] = {}
    for row in rows:
        try:
            observed = datetime.fromisoformat(str(row["observed_at"])).astimezone(timezone.utc)
            hour = observed.replace(minute=0, second=0, microsecond=0)
            hourly[hour] = float(row["value"])
        except (KeyError, TypeError, ValueError):
            continue
    return hourly


def _sum_precip_mm(precip: Dict[datetime, float], valid_at: datetime, hours: int) -> float:
    return sum(precip.get(valid_at - timedelta(hours=h), 0.0) for h in range(1, hours + 1))


def _quantile(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    vals = sorted(values)
    if len(vals) == 1:
        return vals[0]
    pos = max(0.0, min(1.0, q)) * (len(vals) - 1)
    lo = int(pos)
    hi = min(len(vals) - 1, lo + 1)
    frac = pos - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac


def _peak_events(
    flow: Dict[datetime, float],
    precip: Dict[datetime, float],
    stats: Dict[Tuple[int, int], Dict[str, float]],
) -> List[PeakEvent]:
    hours = sorted(set(flow) & set(precip))
    if len(hours) < 72:
        return []
    events: List[PeakEvent] = []
    for i in range(24, len(hours) - 24):
        observed_at = hours[i]
        peak = flow[observed_at]
        prev = [flow.get(hours[j], peak) for j in range(i - 6, i)]
        nxt = [flow.get(hours[j], peak) for j in range(i + 1, i + 7)]
        if not prev or not nxt:
            continue
        if peak < max(prev + nxt):
            continue
        if flow.get(hours[i - 1], peak) == peak:
            continue
        baseline_window = [flow.get(observed_at - timedelta(hours=h)) for h in range(12, 25)]
        baseline_candidates = [v for v in baseline_window if v is not None]
        if len(baseline_candidates) < 6:
            continue
        baseline = min(baseline_candidates)
        if baseline <= 0:
            continue
        rise_cfs = peak - baseline
        rise_ratio = peak / baseline
        if rise_cfs <= 0:
            continue
        peak_pct = discharge_percentile(peak, stats.get((observed_at.month, observed_at.day)))
        events.append(
            PeakEvent(
                observed_at=observed_at,
                baseline_cfs=baseline,
                peak_cfs=peak,
                rise_cfs=rise_cfs,
                rise_ratio=rise_ratio,
                peak_percentile=peak_pct,
                rain_6h_mm=_sum_precip_mm(precip, observed_at, 6),
                rain_12h_mm=_sum_precip_mm(precip, observed_at, 12),
                rain_24h_mm=_sum_precip_mm(precip, observed_at, 24),
            )
        )
    return events


def _prior_thresholds_mm(reach: Dict[str, object]) -> tuple[float, float, float]:
    prior = assess_runoff_risk(
        valid_at=datetime.now(timezone.utc),
        reach_id=None,
        qpf_map=None,
        spring_influenced=bool(reach.get("spring_influenced")),
        length_km=reach.get("length_km"),
        flow_percentile=0.50,
    )
    return prior.hurt_threshold_6h_mm, prior.hurt_threshold_12h_mm, prior.hurt_threshold_24h_mm


def fit_reach(
    reach: Dict[str, object],
    gauge_meta: Dict[str, object],
    days: int,
    min_events: int,
    min_hurt_events: int,
) -> Optional[Dict[str, object]]:
    gauge_id = str(reach.get("usgs_gauge_id") or "")
    if not gauge_id or bool(reach.get("gauge_is_proxy")):
        return None
    lat = gauge_meta.get("lat")
    lon = gauge_meta.get("lon")
    if lat is None or lon is None:
        return None

    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)

    flow = _hourly_flow_map(gauge_id, start, end)
    precip = fetch_archive_hourly_precip_mm(float(lat), float(lon), start.date(), end.date())
    stats = _daily_stats_table(gauge_id)
    events = _peak_events(flow, precip, stats)
    hurt_events = [
        e for e in events
        if e.peak_percentile >= 0.75 and e.rise_ratio >= 1.15 and e.rain_24h_mm >= 1.0
    ]

    prior_6h, prior_12h, prior_24h = _prior_thresholds_mm(reach)
    if len(events) < min_events or len(hurt_events) < min_hurt_events:
        return {
            "gauge_id": gauge_id,
            "used": False,
            "reason": f"thin_events_total_{len(events)}_hurt_{len(hurt_events)}",
            "event_count": len(events),
            "hurt_event_count": len(hurt_events),
            "hurt_threshold_6h_mm": round(prior_6h, 1),
            "hurt_threshold_12h_mm": round(prior_12h, 1),
            "hurt_threshold_24h_mm": round(prior_24h, 1),
            "cfs_rise_per_in_6h": None,
            "cfs_rise_per_in_24h": None,
            "median_hurt_rise_cfs": round(median([e.rise_cfs for e in hurt_events]), 1) if hurt_events else None,
            "median_hurt_rise_ratio": round(median([e.rise_ratio for e in hurt_events]), 3) if hurt_events else None,
        }

    raw_6h = _quantile([e.rain_6h_mm for e in hurt_events if e.rain_6h_mm > 0.0], 0.40) or prior_6h
    raw_12h = _quantile([e.rain_12h_mm for e in hurt_events if e.rain_12h_mm > 0.0], 0.40) or prior_12h
    raw_24h = _quantile([e.rain_24h_mm for e in hurt_events if e.rain_24h_mm > 0.0], 0.40) or prior_24h

    weight = min(1.0, len(hurt_events) / 12.0)
    fit_6h = max(4.0, prior_6h * (1.0 - weight) + raw_6h * weight)
    fit_12h = max(fit_6h + 1.0, prior_12h * (1.0 - weight) + raw_12h * weight)
    fit_24h = max(fit_12h + 1.0, prior_24h * (1.0 - weight) + raw_24h * weight)

    rise_per_in_6h = [e.rise_cfs / (e.rain_6h_mm / 25.4) for e in hurt_events if e.rain_6h_mm >= 2.0]
    rise_per_in_24h = [e.rise_cfs / (e.rain_24h_mm / 25.4) for e in hurt_events if e.rain_24h_mm >= 2.0]
    return {
        "gauge_id": gauge_id,
        "used": True,
        "reason": "historical_peak_fit",
        "event_count": len(events),
        "hurt_event_count": len(hurt_events),
        "hurt_threshold_6h_mm": round(fit_6h, 1),
        "hurt_threshold_12h_mm": round(fit_12h, 1),
        "hurt_threshold_24h_mm": round(fit_24h, 1),
        "cfs_rise_per_in_6h": round(median(rise_per_in_6h), 1) if rise_per_in_6h else None,
        "cfs_rise_per_in_24h": round(median(rise_per_in_24h), 1) if rise_per_in_24h else None,
        "median_hurt_rise_cfs": round(median([e.rise_cfs for e in hurt_events]), 1),
        "median_hurt_rise_ratio": round(median([e.rise_ratio for e in hurt_events]), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--reach", action="append", help="Reach id to fit; repeatable")
    parser.add_argument("--min-events", type=int, default=6)
    parser.add_argument("--min-hurt-events", type=int, default=3)
    parser.add_argument("--output", type=Path, default=CALIBRATION_PATH)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    reaches = _load_seed(REACHES_PATH)
    gauges = {str(g["gauge_id"]): g for g in _load_seed(GAUGES_PATH)}
    wanted = set(args.reach or [])
    diagnostics: Dict[str, Dict[str, object]] = {}
    fits: Dict[str, Dict[str, object]] = {}

    for reach in reaches:
        reach_id = str(reach.get("reach_id") or "")
        if wanted and reach_id not in wanted:
            continue
        gauge_id = str(reach.get("usgs_gauge_id") or "")
        gauge_meta = gauges.get(gauge_id)
        if not gauge_meta:
            continue
        try:
            result = fit_reach(reach, gauge_meta, args.days, args.min_events, args.min_hurt_events)
        except Exception as exc:
            LOG.warning("runoff fit failed for %s: %s", reach_id, exc)
            continue
        if not result:
            continue
        diagnostics[reach_id] = result
        if result.get("used"):
            fits[reach_id] = result
        LOG.info(
            "%s %s 6h=%.1fmm 24h=%.1fmm events=%s hurt=%s",
            reach_id,
            result.get("reason"),
            float(result["hurt_threshold_6h_mm"]),
            float(result["hurt_threshold_24h_mm"]),
            result.get("event_count"),
            result.get("hurt_event_count"),
        )

    payload = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "method": "historical peak-event calibration from Open-Meteo hourly precipitation + USGS hourly discharge",
        "days": args.days,
        "min_events": args.min_events,
        "min_hurt_events": args.min_hurt_events,
        "fits": fits,
        "diagnostics": diagnostics,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    print(f"wrote {len(fits)} fits to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
