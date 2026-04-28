"""
Refit Mohseni air→water coefficients against real USGS 00010 data.

Step 15 (plan) was "air→water regression for ungauged reaches." Our v1 used
literature coefficients; this script replaces them with a pooled fit from
Upper Midwest USGS gauges that actually report water temperature.

Output:
  * data/calibration/mohseni_fit.json — per-class fit summary
  * logs on delta vs the literature coefficients
"""
from __future__ import annotations

import json
import logging
import math
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

from src.ingest import fetch_archive_daily_mean_f
from src.ingest.usgs_water_temp import fetch_daily_water_temp_f, find_temp_sites
from src.models import temp_estimator
from src.models.mohseni_fit import classify_stream, fit, prepare_pairs

ROOT = Path(__file__).resolve().parent.parent.parent
OUT_PATH = ROOT / "data" / "calibration" / "mohseni_fit.json"

LOG = logging.getLogger("refit_mohseni")
logging.basicConfig(level=logging.INFO,
                     format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# Upper-Midwest bbox sanity filter: USGS returns some out-of-state gauges at
# boundary segments, so keep only what's actually in our region of interest.
BBOX = {"swlat": 41.0, "swlng": -96.0, "nelat": 47.0, "nelng": -87.0}
YEARS_BACK = 4
MIN_PAIRS_PER_CLASS = 200


def in_bbox(lat: float, lon: float) -> bool:
    return BBOX["swlat"] <= lat <= BBOX["nelat"] and BBOX["swlng"] <= lon <= BBOX["nelng"]


def _as_date_pairs(series_f: List[Tuple[object, float]]) -> List[Tuple[str, float]]:
    out: List[Tuple[str, float]] = []
    for d, t in series_f:
        if isinstance(d, date):
            out.append((d.isoformat(), t))
        else:
            out.append((str(d), t))
    return out


def main() -> int:
    today = datetime.utcnow().date()
    start = today - timedelta(days=365 * YEARS_BACK)
    LOG.info("discovering USGS temp sites (states=%s)", ", ".join(["mn", "wi", "ia"]))
    sites = [s for s in find_temp_sites() if in_bbox(s["lat"], s["lon"])]
    LOG.info("  %d sites in bbox", len(sites))

    per_class_pairs: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    per_site: List[Dict[str, object]] = []
    for idx, s in enumerate(sites):
        site_no = s["site_no"]
        LOG.info("[%d/%d] %s  %s  (%.3f, %.3f)", idx + 1, len(sites),
                 site_no, (s.get("station_nm") or "")[:55], s["lat"], s["lon"])
        water = fetch_daily_water_temp_f(site_no, start, today)
        if len(water) < 60:
            LOG.info("  skip: only %d days of water temp", len(water))
            continue
        water_pairs = _as_date_pairs(water)
        dmin = min(d for d, _ in water_pairs)
        dmax = max(d for d, _ in water_pairs)
        try:
            air = fetch_archive_daily_mean_f(s["lat"], s["lon"],
                                             date.fromisoformat(dmin),
                                             date.fromisoformat(dmax))
        except Exception:
            LOG.exception("  archive fetch failed, skip")
            continue
        air_pairs = _as_date_pairs(air)
        pairs = prepare_pairs(air_pairs, water_pairs)
        if len(pairs) < 60:
            LOG.info("  skip: only %d paired days", len(pairs))
            continue
        klass = classify_stream(water_pairs)
        per_class_pairs[klass].extend(pairs)
        per_site.append({
            "site_no": site_no,
            "name": s.get("station_nm"),
            "lat": s["lat"], "lon": s["lon"],
            "n_pairs": len(pairs),
            "classification": klass,
            "water_min_f": min(t for _d, t in water_pairs),
            "water_max_f": max(t for _d, t in water_pairs),
        })
        LOG.info("  classified %s · %d pairs (total %d)", klass, len(pairs),
                 sum(len(v) for v in per_class_pairs.values()))

    report: Dict[str, object] = {
        "generated": datetime.utcnow().isoformat(),
        "years_back": YEARS_BACK,
        "sites_used": per_site,
        "classes": {},
    }

    lit_map = {
        "spring": temp_estimator.SPRING_INFLUENCED,
        "freestone": temp_estimator.FREESTONE,
    }

    for klass, pairs in per_class_pairs.items():
        if len(pairs) < MIN_PAIRS_PER_CLASS:
            LOG.info("[%s] skip fit: %d pairs (<%d)", klass, len(pairs), MIN_PAIRS_PER_CLASS)
            continue
        LOG.info("[%s] fitting on %d pairs", klass, len(pairs))
        result = fit(pairs)
        if result is None:
            continue
        params, rmse = result
        lit = lit_map.get(klass)
        LOG.info("  fit: mu=%.1f alpha=%.1f beta=%.1f gamma=%.3f  rmse=%.2f°F",
                 params.mu_f, params.alpha_f, params.beta_f, params.gamma, rmse)
        if lit is not None:
            LOG.info("  literature: mu=%.1f alpha=%.1f beta=%.1f gamma=%.3f",
                     lit.mu_f, lit.alpha_f, lit.beta_f, lit.gamma)
        report["classes"][klass] = {
            "n_pairs": len(pairs),
            "fit": {"mu_f": params.mu_f, "alpha_f": params.alpha_f,
                     "beta_f": params.beta_f, "gamma": params.gamma},
            "rmse_f": rmse,
            "literature": ({"mu_f": lit.mu_f, "alpha_f": lit.alpha_f,
                            "beta_f": lit.beta_f, "gamma": lit.gamma}
                           if lit is not None else None),
        }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    LOG.info("wrote %s", OUT_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
