"""Fit per-reach stormflow recession constants from USGS daily history.

This is an empirical calibration layer on top of the scientific model form:
Q(t) = Q_med + (Q_now - Q_med) * exp(-t/tau). We only publish a per-gauge tau
when it has enough samples and beats the class prior by a configured margin.
Otherwise production keeps using the regional prior.
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Dict, Iterable, List, Optional, Tuple

import requests

from src.db import load_reaches
from src.ingest.usgs import STAT_BASE, fetch_daily
from src.models.recession import (
    CALIBRATION_PATH,
    class_prior_tau_hours,
    project_flow,
)

LOG = logging.getLogger(__name__)
LEADS_H = (24, 72, 168)
WEIGHTS = {24: 0.55, 72: 0.35, 168: 0.10}
TAU_GRID_H = tuple(float(x) for x in range(12, 241, 2))


@dataclass
class FitResult:
    reach_id: str
    gauge_id: str
    spring_influenced: bool
    tau_hours: float
    n: int
    weighted_mape_pct: float
    prior_tau_hours: float
    prior_weighted_mape_pct: float
    improvement_pct: float
    used: bool
    reason: str


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


def _daily_flow_map(gauge_id: str, days: int) -> Dict[date, float]:
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days)
    records = fetch_daily(gauge_id, "00060", start_date=start, end_date=end)
    out: Dict[date, float] = {}
    for row in records:
        try:
            observed = datetime.fromisoformat(str(row["observed_at"]).replace("Z", "+00:00")).date()
            out[observed] = float(row["value"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _samples(
    flows: Dict[date, float],
    stats: Dict[Tuple[int, int], Dict[str, float]],
) -> List[Tuple[float, float, float, int]]:
    out: List[Tuple[float, float, float, int]] = []
    dates = sorted(flows)
    if not dates:
        return out
    last = dates[-1]
    for trigger in dates:
        if (last - trigger).days < max(LEADS_H) // 24:
            continue
        stat = stats.get((trigger.month, trigger.day))
        q_med = (stat or {}).get("p50")
        if q_med is None:
            continue
        q_now = flows[trigger]
        # Fit the recession limb, not drought recovery. Below-median flows are
        # governed by rainfall/baseflow recharge timing and make the symmetric
        # exponential form choose unrealistically slow tau values.
        if q_now < q_med:
            continue
        for lead_h in LEADS_H:
            target = trigger + timedelta(days=lead_h // 24)
            q_actual = flows.get(target)
            if q_actual is None:
                continue
            out.append((q_now, q_med, q_actual, lead_h))
    return out


def _weighted_mape(samples: Iterable[Tuple[float, float, float, int]], tau_h: float) -> float:
    per_lead: Dict[int, List[float]] = {lead: [] for lead in LEADS_H}
    for q_now, q_med, q_actual, lead_h in samples:
        pred = project_flow(q_now, q_med, tau_h, lead_h)
        per_lead[lead_h].append(abs(pred - q_actual) / max(q_actual, 1.0))
    total = 0.0
    weight_sum = 0.0
    for lead_h, errors in per_lead.items():
        if not errors:
            continue
        weight = WEIGHTS[lead_h]
        total += weight * mean(errors)
        weight_sum += weight
    if weight_sum == 0:
        return float("inf")
    return 100.0 * total / weight_sum


def fit_reach(reach: Dict[str, object], days: int, min_n: int, min_improvement_pct: float) -> Optional[FitResult]:
    gauge_id = reach.get("usgs_gauge_id")
    if not gauge_id or reach.get("gauge_is_proxy"):
        return None
    gauge = str(gauge_id)
    flows = _daily_flow_map(gauge, days)
    stats = _daily_stats_table(gauge)
    samples = _samples(flows, stats)
    spring = bool(reach.get("spring_influenced"))
    prior_tau = class_prior_tau_hours(spring)
    if not samples:
        return FitResult(str(reach["reach_id"]), gauge, spring, prior_tau, 0, float("inf"), prior_tau, float("inf"), 0.0, False, "no_samples")
    scored = [(tau, _weighted_mape(samples, tau)) for tau in TAU_GRID_H]
    tau, err = min(scored, key=lambda item: item[1])
    prior_err = _weighted_mape(samples, prior_tau)
    improvement = 100.0 * (prior_err - err) / prior_err if prior_err and prior_err != float("inf") else 0.0
    hit_boundary = tau >= max(TAU_GRID_H)
    used = len(samples) >= min_n and improvement >= min_improvement_pct and not hit_boundary
    if len(samples) < min_n:
        reason = f"thin_sample_n_{len(samples)}"
    elif hit_boundary:
        reason = "best_tau_hit_grid_boundary"
    elif improvement < min_improvement_pct:
        reason = f"improvement_{improvement:.1f}_pct_below_gate"
    else:
        reason = "beats_prior"
    return FitResult(
        reach_id=str(reach["reach_id"]),
        gauge_id=gauge,
        spring_influenced=spring,
        tau_hours=tau,
        n=len(samples),
        weighted_mape_pct=err,
        prior_tau_hours=prior_tau,
        prior_weighted_mape_pct=prior_err,
        improvement_pct=improvement,
        used=used,
        reason=reason,
    )


def _as_json(result: FitResult) -> Dict[str, object]:
    def finite(value: float) -> Optional[float]:
        if value == float("inf"):
            return None
        return round(value, 2)

    return {
        "gauge_id": result.gauge_id,
        "spring_influenced": result.spring_influenced,
        "tau_hours": round(result.tau_hours, 1),
        "n": result.n,
        "weighted_mape_pct": finite(result.weighted_mape_pct),
        "prior_tau_hours": round(result.prior_tau_hours, 1),
        "prior_weighted_mape_pct": finite(result.prior_weighted_mape_pct),
        "improvement_pct": round(result.improvement_pct, 2),
        "used": result.used,
        "reason": result.reason,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--reach", action="append", help="Reach id to fit; repeatable")
    parser.add_argument("--min-n", type=int, default=90)
    parser.add_argument("--min-improvement-pct", type=float, default=3.0)
    parser.add_argument("--output", type=Path, default=CALIBRATION_PATH)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    wanted = set(args.reach or [])
    results: List[FitResult] = []
    for reach in load_reaches():
        if wanted and str(reach["reach_id"]) not in wanted:
            continue
        try:
            result = fit_reach(reach, args.days, args.min_n, args.min_improvement_pct)
        except Exception as exc:
            LOG.warning("fit failed for %s: %s", reach.get("reach_id"), exc)
            continue
        if result:
            results.append(result)
            LOG.info(
                "%s tau=%.0fh err=%.1f%% prior=%.1f%% improvement=%.1f%% %s",
                result.reach_id,
                result.tau_hours,
                result.weighted_mape_pct,
                result.prior_weighted_mape_pct,
                result.improvement_pct,
                result.reason,
            )

    payload = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "method": "grid search over exponential event-flow recession toward USGS day-of-year p50",
        "days": args.days,
        "lead_weights": WEIGHTS,
        "min_n": args.min_n,
        "min_improvement_pct": args.min_improvement_pct,
        "fits": {
            r.reach_id: _as_json(r)
            for r in results
            if r.used
        },
        "diagnostics": {
            r.reach_id: _as_json(r)
            for r in results
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, allow_nan=False)
        fh.write("\n")
    print(f"Wrote {args.output} with {len(payload['fits'])} production fits / {len(results)} diagnostics")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
