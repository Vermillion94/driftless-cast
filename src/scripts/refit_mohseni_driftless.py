"""
Refit Mohseni air→water coefficients on *Driftless* trout-stream records.

`refit_mohseni` fits on whatever Upper-Midwest gauges report water temp today
(St. Louis, Fox, Milwaukee, Des Moines rivers, Minneapolis urban creeks…). None
of those behave like a groundwater-fed Driftless spring creek, and the "spring"
class never had enough sites, so production fell back to a "mixed" fit drawn
from the Raccoon and Des Moines rivers.

USGS holds years of *historical* daily water temperature on genuine Driftless
trout streams (Joos Valley, Eagle, Traverse Valley, Nederlo, Bloody Run,
Rattlesnake, Kuenster, Stillwell, Silver, Whitewater, Root…). Open-Meteo's
archive reaches back to 1940, so we can pair those records with air temps from
the same days and fit curves that actually describe this region.

Output: data/calibration/mohseni_fit_driftless.json — per-site fits, per-class
pooled fits (classified by annual swing), and RMSE of the current production
curve on the same data for comparison.

Usage:  python -m src.scripts.refit_mohseni_driftless
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from src.ingest import fetch_archive_daily_mean_f
from src.models import temp_estimator
from src.models.mohseni_fit import _rmse, fit, prepare_pairs

ROOT = Path(__file__).resolve().parent.parent.parent
OUT_PATH = ROOT / "data" / "calibration" / "mohseni_fit_driftless.json"
NWIS_DV = "https://waterservices.usgs.gov/nwis/dv/"

LOG = logging.getLogger("refit_mohseni_driftless")

# (site_no, name, lat, lon) — Driftless trout streams with USGS daily 00010.
SITES: List[Tuple[str, str, float, float]] = [
    ("05378183", "Joos Valley Creek nr Fountain City WI", 44.1641, -91.6715),
    ("05378185", "Eagle Creek nr Fountain City WI", 44.1594, -91.6896),
    ("053793305", "Traverse Valley Cr N trib nr Independence WI", 44.3714, -91.4199),
    ("053793306", "Traverse Valley Cr S trib nr Independence WI", 44.3681, -91.4202),
    ("05409870", "Nederlo Creek nr Gays Mills WI", 43.3175, -90.8696),
    ("05409860", "South Fork Nederlo Creek nr Gays Mills WI", 43.3108, -90.8771),
    ("05409842", "North Fork Nederlo Cr at mouth nr Gays Mills WI", 43.3205, -90.8740),
    ("05389400", "Bloody Run Creek nr Marquette IA", 43.0464, -91.2199),
    ("05413449", "Rattlesnake Creek nr North Andover WI", 42.8169, -90.9524),
    ("054134435", "Kuenster Creek nr North Andover WI", 42.8336, -90.9346),
    ("05382255", "Stillwell Creek nr Tomah WI", 43.9939, -90.6572),
    ("05382257", "Stillwell Creek at Yard Rd nr Tomah WI", 44.0006, -90.6811),
    ("05382284", "Silver Creek nr Angelo WI", 43.9672, -90.7650),
    ("05376800", "Whitewater River nr Beaver MN", 44.1544, -92.0010),
    ("05385000", "Root River nr Houston MN", 43.7686, -91.5693),
    ("05387500", "Upper Iowa River at Decorah IA", 43.3047, -91.7957),
]

MAX_SPAN_DAYS = 365 * 8   # cap per-site request size; plenty for a fit


def _fetch_water_daily_f(site: str) -> List[Tuple[str, float]]:
    """Daily mean water temp (°F). Falls back to (max+min)/2 when a site only
    published daily extremes."""
    def get(stat: str) -> Dict[str, float]:
        resp = requests.get(NWIS_DV, params={
            "sites": site, "parameterCd": "00010", "statCd": stat,
            "startDT": "1960-01-01", "endDT": date.today().isoformat(), "format": "json",
        }, timeout=90)
        resp.raise_for_status()
        out: Dict[str, float] = {}
        for ts in resp.json().get("value", {}).get("timeSeries", []):
            for block in ts.get("values", []):
                for v in block.get("value", []):
                    try:
                        c = float(v["value"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if c <= -900:  # NWIS no-data sentinel
                        continue
                    out[v["dateTime"][:10]] = c * 9.0 / 5.0 + 32.0
        return out

    mean = get("00003")
    if len(mean) < 200:
        mx, mn = get("00001"), get("00002")
        both = sorted(set(mx) & set(mn))
        mean = {d: (mx[d] + mn[d]) / 2.0 for d in both} or mean
    return sorted(mean.items())


def _classify(water: List[Tuple[str, float]]) -> Tuple[str, float, float, float]:
    """Class by the *typical* annual cycle (5th–95th pct), not raw extremes,
    so a single storm spike doesn't make a spring creek look freestone."""
    temps = sorted(t for _d, t in water)
    lo = temps[int(0.05 * (len(temps) - 1))]
    hi = temps[int(0.95 * (len(temps) - 1))]
    swing = hi - lo
    if swing < 22:
        klass = "spring"
    elif swing < 32:
        klass = "mixed"
    else:
        klass = "freestone"
    return klass, swing, lo, hi


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    per_site = []
    pooled: Dict[str, List[Tuple[float, float]]] = {"spring": [], "mixed": [], "freestone": []}
    for site, name, lat, lon in SITES:
        try:
            water = _fetch_water_daily_f(site)
        except requests.RequestException as exc:
            LOG.warning("%s water fetch failed: %s", site, exc)
            continue
        if len(water) < 200:
            LOG.info("%s %s: only %d days, skipping", site, name, len(water))
            continue
        # Most recent contiguous-ish chunk, capped in length.
        water = water[-MAX_SPAN_DAYS:]
        start = date.fromisoformat(water[0][0])
        end = date.fromisoformat(water[-1][0])
        try:
            air = fetch_archive_daily_mean_f(lat, lon, start, end)
        except requests.RequestException as exc:
            LOG.warning("%s air fetch failed: %s", site, exc)
            continue
        time.sleep(1.0)  # be polite to Open-Meteo
        pairs = prepare_pairs([(d.isoformat(), t) for d, t in air], water)
        if len(pairs) < 200:
            continue
        klass, swing, lo, hi = _classify(water)
        best = fit(pairs)
        prod_spring = _rmse(pairs, temp_estimator.params_for_reach(True))
        prod_free = _rmse(pairs, temp_estimator.params_for_reach(False))
        entry = {
            "site_no": site, "name": name, "lat": lat, "lon": lon,
            "first": water[0][0], "last": water[-1][0], "n_pairs": len(pairs),
            "class": klass, "p05_f": round(lo, 1), "p95_f": round(hi, 1), "swing_f": round(swing, 1),
            "fit": None if not best else {**best[0].__dict__, "rmse_f": round(best[1], 2)},
            "production_spring_rmse_f": round(prod_spring, 2),
            "production_freestone_rmse_f": round(prod_free, 2),
        }
        LOG.info("%s %-45s %-9s swing %.0f°F  fit %s  rmse %.2f (prod spring %.2f / free %.2f)",
                 site, name, klass, swing, best[0] if best else None, best[1] if best else float("nan"),
                 prod_spring, prod_free)
        per_site.append(entry)
        pooled[klass].extend(pairs)

    classes = {}
    for klass, pairs in pooled.items():
        if len(pairs) < 200:
            classes[klass] = {"n_pairs": len(pairs), "fit": None}
            continue
        best = fit(pairs)
        classes[klass] = {
            "n_pairs": len(pairs),
            "fit": None if not best else {**best[0].__dict__},
            "rmse_f": None if not best else round(best[1], 2),
            "production_spring_rmse_f": round(_rmse(pairs, temp_estimator.params_for_reach(True)), 2),
            "production_freestone_rmse_f": round(_rmse(pairs, temp_estimator.params_for_reach(False)), 2),
        }
        LOG.info("POOLED %-9s n=%d fit=%s rmse=%s", klass, len(pairs), classes[klass]["fit"], classes[klass]["rmse_f"])

    OUT_PATH.write_text(json.dumps({
        "generated": datetime.utcnow().isoformat(timespec="seconds"),
        "method": "grid-search Mohseni on 7-day rolling air mean (Open-Meteo archive) vs USGS daily water temp",
        "sites": per_site,
        "classes": classes,
    }, indent=2) + "\n", encoding="utf-8")
    LOG.info("wrote %s", OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
