"""
iNaturalist ingest for species phenology calibration.

Plan step 14 / data source 3: one-off historical dump of research-grade
observations in the Driftless bbox, used to fit per-species DD thresholds.

The API is rate-limited to ~100 req/min. We paginate at 200/page, cache the
raw responses on disk, and respect a small inter-request sleep to stay polite.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests

BASE = "https://api.inaturalist.org/v1"
USER_AGENT = "driftless-cast/0.1 (https://github.com/local; contact: hello@example.com)"
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "calibration" / "inat_cache"
PER_PAGE = 200
INTER_REQUEST_SLEEP_S = 0.6
LOG = logging.getLogger(__name__)

DRIFTLESS_BBOX = {
    "swlat": 43.0, "swlng": -93.5,
    "nelat": 45.5, "nelng": -90.0,
}

# Wider Upper Midwest bbox for calibration: degree-day thresholds are
# location-agnostic (each obs gets DD computed at its own coords), so we
# can pool observations across the whole region to get enough signal.
UPPER_MIDWEST_BBOX = {
    "swlat": 41.0, "swlng": -96.0,
    "nelat": 47.0, "nelng": -87.0,
}


@dataclass
class Observation:
    id: int
    taxon_id: int
    taxon_name: str
    observed_on: str   # YYYY-MM-DD
    lat: float
    lon: float
    quality_grade: str


def _headers() -> Dict[str, str]:
    return {"User-Agent": USER_AGENT, "Accept": "application/json"}


def _get(path: str, params: Dict[str, object]) -> Dict[str, object]:
    time.sleep(INTER_REQUEST_SLEEP_S)
    resp = requests.get(f"{BASE}{path}", headers=_headers(), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def resolve_taxon(query: str, rank: Optional[str] = None) -> Optional[Dict[str, object]]:
    """First search hit for `query`. Pass `rank='species'` to narrow."""
    params: Dict[str, object] = {"q": query, "per_page": 5}
    if rank:
        params["rank"] = rank
    data = _get("/taxa", params)
    results = data.get("results", [])
    if not results:
        return None
    # iNat ranks hits by relevance; for our purposes the top exact-name match wins.
    low = query.lower()
    for r in results:
        if (r.get("name", "").lower() == low) or (r.get("preferred_common_name", "").lower() == low):
            return r
    return results[0]


def _cache_path(taxon_id: int, year: int) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"taxon_{taxon_id}_{year}.json"


def fetch_observations_for_year(taxon_id: int, year: int, bbox: Dict[str, float] = DRIFTLESS_BBOX) -> List[Observation]:
    """All research-grade observations of `taxon_id` in year/bbox, cached to disk."""
    cache = _cache_path(taxon_id, year)
    if cache.exists():
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
            return [Observation(**o) for o in payload]
        except (ValueError, TypeError) as exc:
            LOG.warning("bad cache %s: %s", cache, exc)

    raw: List[Dict[str, object]] = []
    page = 1
    while True:
        params = {
            "taxon_id": taxon_id,
            "d1": f"{year}-01-01",
            "d2": f"{year}-12-31",
            "quality_grade": "research",
            "per_page": PER_PAGE,
            "page": page,
            **bbox,
        }
        try:
            data = _get("/observations", params)
        except requests.RequestException as exc:
            LOG.warning("iNat fetch failed taxon=%d year=%d page=%d: %s", taxon_id, year, page, exc)
            break
        results = data.get("results", [])
        if not results:
            break
        raw.extend(results)
        if len(results) < PER_PAGE:
            break
        page += 1
        # iNat caps offset-pagination at page 100 (200 * 100 = 20k). For our
        # Driftless-bbox queries this is more than enough; guard anyway.
        if page > 100:
            break

    observations: List[Observation] = []
    for r in raw:
        observed_on = r.get("observed_on") or r.get("observed_on_details", {}).get("date")
        if not observed_on:
            continue
        geo = r.get("geojson") or {}
        coords = geo.get("coordinates")
        if not coords or len(coords) < 2:
            lat = r.get("latitude")
            lon = r.get("longitude")
        else:
            lon, lat = coords[0], coords[1]
        if lat is None or lon is None:
            continue
        taxon = r.get("taxon") or {}
        observations.append(Observation(
            id=int(r.get("id") or 0),
            taxon_id=int(taxon.get("id") or taxon_id),
            taxon_name=str(taxon.get("name") or ""),
            observed_on=str(observed_on),
            lat=float(lat),
            lon=float(lon),
            quality_grade=str(r.get("quality_grade") or "casual"),
        ))
    cache.write_text(
        json.dumps([o.__dict__ for o in observations], indent=0),
        encoding="utf-8",
    )
    return observations


def fetch_observations(taxon_id: int, start_year: int, end_year: int,
                       bbox: Dict[str, float] = DRIFTLESS_BBOX) -> List[Observation]:
    out: List[Observation] = []
    for y in range(start_year, end_year + 1):
        out.extend(fetch_observations_for_year(taxon_id, y, bbox))
    return out
