"""
Backtest CLI — hindcast our flow & water-temp models against historical truth.

Purpose: answer the question "are our estimates correct?" with a method that
doesn't require any user-reported trip data. Every component the model uses
that has a measurable ground truth in USGS or Open-Meteo can be validated by
replaying historical inputs and comparing model output to what actually
happened.

Methodology summary (see docs/REFERENCES.md for the underlying papers):

  * Flow recession (Tallaksen 1995, Brutsaert & Nieber 1977)
      For each historical day t with full USGS daily-values record, pretend
      we're at t with the model's class-level recession constants. Project
      flow forward N days. Compare to actual flow at t+N. Aggregate per
      lead-time. This validates τ_freestone = 30h and τ_spring = 48h directly.

  * Mohseni water temp (Mohseni 1998)
      For each gauge that reports 00010 (water temp), pull paired
      Open-Meteo air + USGS water history. Run the configured Mohseni curve
      on the air series and compare to actual water. Reports RMSE per gauge,
      MAE, and bias.

The output is a markdown report at data/calibration/backtest_report.md plus a
CSV per validation type. Re-run after model changes to track whether the
model is getting better or worse.

Usage:
    python -m src.scripts.backtest                    # full backtest, last 180 days
    python -m src.scripts.backtest --days 365         # last year
    python -m src.scripts.backtest --reach kinnickinnic-river-falls  # one reach
    python -m src.scripts.backtest --skip mohseni     # flow only

Exit code 0 if all validation metrics are within configured thresholds,
1 if any fail (so this can be wired into CI as a regression guard).
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Dict, List, Optional, Tuple

from src.db import load_reaches
from src.ingest.openmeteo import fetch_archive_daily_mean_f
from src.ingest.usgs import fetch_daily, fetch_daily_stats, fetch_latest_iv
from src.models.recession import (
    TAU_FREESTONE_H,
    TAU_SPRING_H,
    calibrated_tau_hours,
    project_flow,
)
from src.models.temp_estimator import params_for_reach, mohseni, rolling_mean

LOG = logging.getLogger(__name__)
OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "calibration"

# ─── Validation thresholds (CI guard) ────────────────────────────────────────
# These are the minimum-bar values; the model is "passing" when all metrics
# beat them. Tighten as the model improves; never loosen without a very
# good reason that gets recorded in REFERENCES.md.
THRESHOLDS = {
    "flow_recession_mape_24h_pct": 25.0,    # ≤25% MAPE at 24h lead
    "flow_recession_mape_72h_pct": 40.0,    # ≤40% MAPE at 72h lead
    "flow_recession_mape_168h_pct": 60.0,   # ≤60% MAPE at 168h lead
    "mohseni_rmse_f": 6.0,                  # ≤6°F RMSE (Mohseni 1998 reports ~3-4°F)
    "mohseni_bias_f": 3.0,                  # ≤±3°F mean bias
}

# Mohseni validation gauges: USGS sites that historically reported 00010 in
# our region, used as "calibration sentinels" even when not wired to any reach
# in the seed. Eau Galle is an active Driftless freestone gauge with paired
# 00010 — a known-good Mohseni validator. Add more here as found.
MOHSENI_VALIDATION_GAUGES = [
    {
        "gauge_id": "05370000",
        "label": "Eau Galle River at Spring Valley, WI",
        "lat": 44.85,
        "lon": -92.24,
        "spring_influenced": False,  # freestone class
    },
]


@dataclass
class FlowEvalRow:
    reach_id: str
    gauge_id: str
    spring_influenced: bool
    trigger_date: date
    lead_hours: int
    q_now: float
    q_actual: float
    q_predicted: float
    pct_actual: Optional[float]
    pct_predicted: Optional[float]


@dataclass
class MohseniEvalRow:
    gauge_id: str
    obs_date: date
    air_7day_mean_f: float
    water_actual_f: float
    water_predicted_f: float
    spring_influenced: bool


@dataclass
class FlowSummary:
    reach_id: str
    gauge_id: str
    n: int
    mae_cfs: Dict[int, float] = field(default_factory=dict)        # lead_h → MAE
    mape_pct: Dict[int, float] = field(default_factory=dict)        # lead_h → MAPE
    bias_cfs: Dict[int, float] = field(default_factory=dict)        # lead_h → mean(predicted - actual)


# ─── Flow recession backtest ────────────────────────────────────────────────

def _flow_recession_predict(
    q_now: float,
    q_med: float,
    spring_influenced: bool,
    hours_ahead: float,
    reach_id: str = "",
    gauge_id: Optional[str] = None,
) -> float:
    """Mirror the prediction logic in forecast_builder._flow_percentile_for_hour.
    Kept independent here so the backtest fails loudly if the production formula
    drifts away from the validated one without an intentional update."""
    tau, _source, _fit = calibrated_tau_hours(reach_id, gauge_id, spring_influenced)
    return project_flow(q_now, q_med, tau, hours_ahead)


def _percentile_from_stats(q: float, stats: Optional[Dict[str, float]]) -> Optional[float]:
    if not stats:
        return None
    pct_for_key = {"p10": 0.10, "p25": 0.25, "p50": 0.50, "p75": 0.75, "p90": 0.90}
    knots = sorted((pct_for_key[k], v) for k, v in stats.items() if k in pct_for_key)
    if len(knots) < 2:
        return None
    if q <= knots[0][1]:
        return max(0.0, knots[0][0] * q / max(knots[0][1], 0.01))
    if q >= knots[-1][1]:
        tail = 1.0 - knots[-1][0]
        return min(1.0, knots[-1][0] + tail * min(1.0, (q - knots[-1][1]) / max(knots[-1][1], 1.0)))
    for (lo_p, lo_v), (hi_p, hi_v) in zip(knots, knots[1:]):
        if lo_v <= q <= hi_v and hi_v > lo_v:
            frac = (q - lo_v) / (hi_v - lo_v)
            return lo_p + frac * (hi_p - lo_p)
    return None


def backtest_flow(reach: Dict, days: int) -> Tuple[List[FlowEvalRow], FlowSummary]:
    gauge_id = reach.get("usgs_gauge_id")
    if not gauge_id or reach.get("gauge_is_proxy"):
        return [], FlowSummary(reach["reach_id"], gauge_id or "", n=0)

    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days)
    try:
        records = fetch_daily(gauge_id, "00060", start_date=start, end_date=end)
    except Exception as exc:
        LOG.warning("fetch_daily failed for %s: %s", gauge_id, exc)
        return [], FlowSummary(reach["reach_id"], gauge_id, n=0)

    if not records:
        return [], FlowSummary(reach["reach_id"], gauge_id, n=0)

    by_date: Dict[date, float] = {}
    for r in records:
        try:
            d = datetime.fromisoformat(r["observed_at"].replace("Z", "+00:00")).date()
        except (KeyError, ValueError):
            continue
        by_date[d] = float(r["value"])

    if len(by_date) < 30:
        return [], FlowSummary(reach["reach_id"], gauge_id, n=len(by_date))

    sorted_dates = sorted(by_date)
    spring = bool(reach.get("spring_influenced"))
    rows: List[FlowEvalRow] = []
    leads = [24, 72, 168]  # hours

    # Cache day-of-year stats so we don't hit USGS once per row.
    stats_cache: Dict[Tuple[int, int], Optional[Dict[str, float]]] = {}

    def stats_for(d: date) -> Optional[Dict[str, float]]:
        key = (d.month, d.day)
        if key not in stats_cache:
            try:
                stats_cache[key] = fetch_daily_stats(gauge_id, d.month, d.day)
            except Exception:
                stats_cache[key] = None
        return stats_cache[key]

    for trigger in sorted_dates:
        # Need enough downstream observations for all lead times.
        if (sorted_dates[-1] - trigger).days * 24 < max(leads):
            continue
        q_now = by_date[trigger]
        stats_now = stats_for(trigger)
        q_med = (stats_now or {}).get("p50") if stats_now else None
        if q_med is None:
            continue

        for lead_h in leads:
            target_date = trigger + timedelta(days=lead_h // 24)
            if target_date not in by_date:
                continue
            q_actual = by_date[target_date]
            q_pred = _flow_recession_predict(q_now, q_med, spring, lead_h, reach["reach_id"], gauge_id)
            stats_target = stats_for(target_date)
            pct_actual = _percentile_from_stats(q_actual, stats_target)
            pct_predicted = _percentile_from_stats(q_pred, stats_target)
            rows.append(FlowEvalRow(
                reach_id=reach["reach_id"],
                gauge_id=gauge_id,
                spring_influenced=spring,
                trigger_date=trigger,
                lead_hours=lead_h,
                q_now=q_now,
                q_actual=q_actual,
                q_predicted=q_pred,
                pct_actual=pct_actual,
                pct_predicted=pct_predicted,
            ))

    summary = FlowSummary(reach_id=reach["reach_id"], gauge_id=gauge_id, n=len(rows))
    for lead_h in leads:
        bucket = [r for r in rows if r.lead_hours == lead_h]
        if not bucket:
            continue
        abs_errors = [abs(r.q_predicted - r.q_actual) for r in bucket]
        # MAPE clamps tiny denominators so a near-zero baseflow doesn't blow up.
        pct_errors = [
            abs(r.q_predicted - r.q_actual) / max(r.q_actual, 1.0)
            for r in bucket
        ]
        biases = [r.q_predicted - r.q_actual for r in bucket]
        summary.mae_cfs[lead_h] = mean(abs_errors)
        summary.mape_pct[lead_h] = 100.0 * mean(pct_errors)
        summary.bias_cfs[lead_h] = mean(biases)

    return rows, summary


# ─── Mohseni water-temp backtest ────────────────────────────────────────────

def _gauge_has_water_temp(gauge_id: str) -> bool:
    try:
        readings = fetch_latest_iv(gauge_id)
    except Exception:
        return False
    return "00010" in readings


def backtest_mohseni(reach: Dict, days: int) -> Tuple[List[MohseniEvalRow], Dict[str, float]]:
    gauge_id = reach.get("usgs_gauge_id")
    if not gauge_id:
        return [], {}
    # We can validate Mohseni against ANY gauge that historically reported
    # 00010, even if the structural water-temp guard now blocks the gauge as
    # a proxy for some reach. The validation question is "does Mohseni
    # produce the right answer when the air→water relationship can be
    # checked?"
    if not _gauge_has_water_temp(gauge_id):
        return [], {}

    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days)
    try:
        water_records = fetch_daily(gauge_id, "00010", start_date=start, end_date=end)
    except Exception as exc:
        LOG.warning("fetch_daily 00010 failed for %s: %s", gauge_id, exc)
        return [], {}
    if not water_records:
        return [], {}

    water_by_date: Dict[date, float] = {}
    for r in water_records:
        try:
            d = datetime.fromisoformat(r["observed_at"].replace("Z", "+00:00")).date()
        except (KeyError, ValueError):
            continue
        try:
            t_c = float(r["value"])
        except (TypeError, ValueError):
            continue
        water_by_date[d] = t_c * 9 / 5 + 32  # store as °F to match Mohseni-fit space

    lat = float(reach["centroid_lat"])
    lon = float(reach["centroid_lon"])
    air_pairs = fetch_archive_daily_mean_f(
        lat, lon,
        min(water_by_date) - timedelta(days=10),
        max(water_by_date),
    )
    air_by_date = {d: t for d, t in air_pairs}
    if not air_by_date or not water_by_date:
        return [], {}

    sorted_air_dates = sorted(air_by_date)
    air_series = [air_by_date[d] for d in sorted_air_dates]
    smoothed = rolling_mean(air_series, 7)
    air7_by_date = dict(zip(sorted_air_dates, smoothed))

    spring = bool(reach.get("spring_influenced"))
    p = params_for_reach(spring)
    rows: List[MohseniEvalRow] = []
    for d, t_actual_f in water_by_date.items():
        a7 = air7_by_date.get(d)
        if a7 is None:
            continue
        t_pred_f = mohseni(a7, p)
        rows.append(MohseniEvalRow(
            gauge_id=gauge_id,
            obs_date=d,
            air_7day_mean_f=a7,
            water_actual_f=t_actual_f,
            water_predicted_f=t_pred_f,
            spring_influenced=spring,
        ))

    if not rows:
        return [], {}

    diffs = [r.water_predicted_f - r.water_actual_f for r in rows]
    abs_diffs = [abs(x) for x in diffs]
    rmse = math.sqrt(mean(x * x for x in diffs))
    summary = {
        "n": len(rows),
        "rmse_f": rmse,
        "mae_f": mean(abs_diffs),
        "bias_f": mean(diffs),
        "median_abs_f": median(abs_diffs),
    }
    return rows, summary


# ─── Reporting ──────────────────────────────────────────────────────────────

def _write_csv(path: Path, rows: List[Dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _flow_row_to_dict(r: FlowEvalRow) -> Dict:
    return {
        "reach_id": r.reach_id,
        "gauge_id": r.gauge_id,
        "spring_influenced": int(r.spring_influenced),
        "trigger_date": r.trigger_date.isoformat(),
        "lead_hours": r.lead_hours,
        "q_now_cfs": round(r.q_now, 2),
        "q_actual_cfs": round(r.q_actual, 2),
        "q_predicted_cfs": round(r.q_predicted, 2),
        "abs_error_cfs": round(abs(r.q_predicted - r.q_actual), 2),
        "pct_error": round(abs(r.q_predicted - r.q_actual) / max(r.q_actual, 1.0), 4),
        "pct_actual": None if r.pct_actual is None else round(r.pct_actual, 3),
        "pct_predicted": None if r.pct_predicted is None else round(r.pct_predicted, 3),
    }


def _mohseni_row_to_dict(r: MohseniEvalRow) -> Dict:
    return {
        "gauge_id": r.gauge_id,
        "obs_date": r.obs_date.isoformat(),
        "spring_influenced": int(r.spring_influenced),
        "air_7day_mean_f": round(r.air_7day_mean_f, 2),
        "water_actual_f": round(r.water_actual_f, 2),
        "water_predicted_f": round(r.water_predicted_f, 2),
        "error_f": round(r.water_predicted_f - r.water_actual_f, 2),
    }


def _format_flow_table(summaries: List[FlowSummary]) -> str:
    lines = [
        "| reach | gauge | spring? | n | MAE@24h | MAPE@24h | MAE@72h | MAPE@72h | MAE@168h | MAPE@168h |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        if s.n == 0:
            continue
        spring_tag = "✓" if any(r.spring_influenced for r in []) else "?"
        # Pull spring flag from the first applicable summary row — fallback "?"
        # if not available.
        def fmt_mae(lead_h: int) -> str:
            v = s.mae_cfs.get(lead_h)
            return "—" if v is None else f"{v:.1f}"
        def fmt_mape(lead_h: int) -> str:
            v = s.mape_pct.get(lead_h)
            return "—" if v is None else f"{v:.1f}%"
        lines.append(
            f"| {s.reach_id} | {s.gauge_id} | — | {s.n} | "
            f"{fmt_mae(24)} | {fmt_mape(24)} | "
            f"{fmt_mae(72)} | {fmt_mape(72)} | "
            f"{fmt_mae(168)} | {fmt_mape(168)} |"
        )
    return "\n".join(lines)


def _aggregate_flow_metrics(summaries: List[FlowSummary]) -> Dict[str, float]:
    """Across-reach aggregate. We weight each reach equally so a single
    high-volume gauge doesn't dominate the metric."""
    out: Dict[str, float] = {}
    for lead_h in (24, 72, 168):
        mapes = [s.mape_pct[lead_h] for s in summaries if lead_h in s.mape_pct]
        if mapes:
            out[f"mape_{lead_h}h"] = mean(mapes)
    return out


def _format_mohseni_table(rows_by_gauge: Dict[str, Dict[str, float]]) -> str:
    if not rows_by_gauge:
        return "_No gauges with 00010 history found in this run._"
    lines = [
        "| gauge | n | RMSE (°F) | MAE (°F) | Bias (°F) | Median |abs| (°F) |",
        "|---|---|---|---|---|---|",
    ]
    for gauge_id, summary in rows_by_gauge.items():
        lines.append(
            f"| {gauge_id} | {int(summary['n'])} | "
            f"{summary['rmse_f']:.2f} | {summary['mae_f']:.2f} | "
            f"{summary['bias_f']:+.2f} | {summary['median_abs_f']:.2f} |"
        )
    return "\n".join(lines)


def _verdict(metrics: Dict[str, float]) -> Tuple[bool, List[str]]:
    """Compare aggregate metrics to THRESHOLDS. Returns (all_passed, messages)."""
    failures: List[str] = []
    if "mape_24h" in metrics:
        target = THRESHOLDS["flow_recession_mape_24h_pct"]
        if metrics["mape_24h"] > target:
            failures.append(f"flow MAPE@24h {metrics['mape_24h']:.1f}% > {target}% target")
    if "mape_72h" in metrics:
        target = THRESHOLDS["flow_recession_mape_72h_pct"]
        if metrics["mape_72h"] > target:
            failures.append(f"flow MAPE@72h {metrics['mape_72h']:.1f}% > {target}% target")
    if "mape_168h" in metrics:
        target = THRESHOLDS["flow_recession_mape_168h_pct"]
        if metrics["mape_168h"] > target:
            failures.append(f"flow MAPE@168h {metrics['mape_168h']:.1f}% > {target}% target")
    if "mohseni_rmse_f" in metrics:
        target = THRESHOLDS["mohseni_rmse_f"]
        if metrics["mohseni_rmse_f"] > target:
            failures.append(f"Mohseni RMSE {metrics['mohseni_rmse_f']:.2f}°F > {target}°F target")
    if "mohseni_bias_f" in metrics:
        target = THRESHOLDS["mohseni_bias_f"]
        if abs(metrics["mohseni_bias_f"]) > target:
            failures.append(f"Mohseni bias {metrics['mohseni_bias_f']:+.2f}°F > ±{target}°F target")
    return len(failures) == 0, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--days", type=int, default=180, help="Backtest window in days (default 180).")
    parser.add_argument("--reach", type=str, default=None, help="Backtest just one reach by id.")
    parser.add_argument("--skip", type=str, default="", help="Comma-separated tasks to skip: flow,mohseni")
    args = parser.parse_args()
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    reaches = load_reaches()
    if args.reach:
        reaches = [r for r in reaches if r["reach_id"] == args.reach]
        if not reaches:
            print(f"unknown reach: {args.reach}")
            return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    flow_rows: List[FlowEvalRow] = []
    flow_summaries: List[FlowSummary] = []
    if "flow" not in skip:
        for reach in reaches:
            print(f"  flow backtest {reach['reach_id']:32}", end="", flush=True)
            rows, summary = backtest_flow(reach, args.days)
            flow_rows.extend(rows)
            flow_summaries.append(summary)
            print(f"  n={summary.n}")

    mohseni_rows: List[MohseniEvalRow] = []
    mohseni_per_gauge: Dict[str, Dict[str, float]] = {}
    if "mohseni" not in skip:
        seen_gauges = set()
        # 1) Each reach's gauge — usually empty since most of our gauges only
        # report flow + stage. Still try in case future gauges add water temp.
        for reach in reaches:
            gauge_id = reach.get("usgs_gauge_id")
            if not gauge_id or gauge_id in seen_gauges:
                continue
            seen_gauges.add(gauge_id)
            print(f"  mohseni backtest {gauge_id:10}", end="", flush=True)
            rows, summary = backtest_mohseni(reach, args.days)
            print(f"  n={summary.get('n', 0)}")
            if rows:
                mohseni_rows.extend(rows)
                mohseni_per_gauge[gauge_id] = summary
        # 2) Calibration sentinel gauges — known to report 00010 historically,
        # validates Mohseni regardless of seed membership.
        for sentinel in MOHSENI_VALIDATION_GAUGES:
            gid = sentinel["gauge_id"]
            if gid in seen_gauges:
                continue
            seen_gauges.add(gid)
            pseudo_reach = {
                "reach_id": f"sentinel:{gid}",
                "usgs_gauge_id": gid,
                "centroid_lat": sentinel["lat"],
                "centroid_lon": sentinel["lon"],
                "spring_influenced": int(sentinel["spring_influenced"]),
            }
            print(f"  mohseni sentinel  {gid:10} ({sentinel['label'][:40]})", end="", flush=True)
            rows, summary = backtest_mohseni(pseudo_reach, args.days)
            print(f"  n={summary.get('n', 0)}")
            if rows:
                mohseni_rows.extend(rows)
                mohseni_per_gauge[gid] = summary

    # CSVs
    if flow_rows:
        _write_csv(OUTPUT_DIR / "backtest_flow.csv", [_flow_row_to_dict(r) for r in flow_rows])
    if mohseni_rows:
        _write_csv(OUTPUT_DIR / "backtest_mohseni.csv", [_mohseni_row_to_dict(r) for r in mohseni_rows])

    # Aggregate metrics for verdict
    metrics: Dict[str, float] = {}
    metrics.update(_aggregate_flow_metrics(flow_summaries))
    if mohseni_per_gauge:
        all_diffs: List[float] = []
        for r in mohseni_rows:
            all_diffs.append(r.water_predicted_f - r.water_actual_f)
        metrics["mohseni_rmse_f"] = math.sqrt(mean(x * x for x in all_diffs))
        metrics["mohseni_bias_f"] = mean(all_diffs)

    passed, failures = _verdict(metrics)

    # Markdown report
    when = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    lines: List[str] = [
        "# Backtest report",
        "",
        f"_Generated {when} · window={args.days}d · {len(reaches)} reaches_",
        "",
        "## Verdict",
        "",
        f"**{'✅ PASS' if passed else '❌ FAIL'}** — see thresholds in `src/scripts/backtest.py#THRESHOLDS`.",
        "",
    ]
    if failures:
        lines.append("Threshold misses:")
        for f in failures:
            lines.append(f"- {f}")
        lines.append("")
    if metrics:
        lines.append("Aggregate metrics across reaches:")
        for k, v in sorted(metrics.items()):
            unit = "%" if "mape" in k else ("°F" if "_f" in k else "")
            lines.append(f"- `{k}` = {v:.2f}{unit}")
        lines.append("")
    lines += [
        "## Flow recession (per reach, lead-time errors)",
        "",
        "Method: for each historical day, replay our class-level recession model "
        f"(τ_freestone = {TAU_FREESTONE_H:.0f}h, τ_spring = {TAU_SPRING_H:.0f}h) and compare "
        "to actual USGS daily flow at +24h, +72h, +168h. See "
        "[docs/REFERENCES.md#tallaksen_1995](../../docs/REFERENCES.md#tallaksen_1995).",
        "",
        _format_flow_table(flow_summaries),
        "",
        "## Mohseni air → water temperature (per gauge)",
        "",
        "Method: for every gauge that reports 00010 (water temp), pair Open-Meteo "
        "daily-mean air with USGS daily-mean water for the same dates. Run the "
        "configured Mohseni curve for the reach's class. Compare predicted to "
        "actual. See [docs/REFERENCES.md#mohseni_1998](../../docs/REFERENCES.md#mohseni_1998).",
        "",
        _format_mohseni_table(mohseni_per_gauge),
        "",
        "## Output files",
        "",
        "- `data/calibration/backtest_flow.csv` — every (reach, trigger date, lead) row",
        "- `data/calibration/backtest_mohseni.csv` — every (gauge, date) row",
        "- `data/calibration/backtest_metrics.json` — aggregate metrics for CI",
    ]

    report_path = OUTPUT_DIR / "backtest_report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    metrics_path = OUTPUT_DIR / "backtest_metrics.json"
    metrics_path.write_text(json.dumps({
        "generated_at": when,
        "window_days": args.days,
        "metrics": metrics,
        "passed": passed,
        "failures": failures,
    }, indent=2), encoding="utf-8")

    print()
    print(f"Report: {report_path}")
    print(f"Metrics: {metrics_path}")
    print(f"Verdict: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
