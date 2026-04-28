"""
GBIF occurrence ingest for species phenology calibration.

GBIF aggregates iNat + museum specimens + academic collections + national
bio-survey programs (NEON, state DNRs, USGS BioData-feeding institutions)
under a single API. For our Ephemeroptera calibration, GBIF returns roughly
10–30× the observations we got from iNat alone.

Key difference vs iNat: we filter on `taxonKey` (canonical GBIF taxonomy) so
child taxa are included automatically. A Baetidae query picks up every genus
and species underneath it.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import requests

BASE = "https://api.gbif.org/v1"
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "calibration" / "gbif_cache"
PER_PAGE = 300           # GBIF's max
INTER_REQUEST_SLEEP_S = 0.15
LOG = logging.getLogger(__name__)

DRIFTLESS_BBOX = {
    "decimalLatitude": "43,45.5",
    "decimalLongitude": "-93.5,-90",
}
UPPER_MIDWEST_BBOX = {
    "decimalLatitude": "41,47",
    "decimalLongitude": "-96,-87",
}

# basisOfRecord filters worth using for phenology:
# - HUMAN_OBSERVATION: iNat-style sightings
# - PRESERVED_SPECIMEN: museum collections (dated + located)
# - MATERIAL_SAMPLE: NEON / USGS style bulk benthic sampling
# - MATERIAL_CITATION: rare, but date-stamped
PHENOLOGY_BASIS = "HUMAN_OBSERVATION;PRESERVED_SPECIMEN;MATERIAL_SAMPLE;MATERIAL_CITATION"


@dataclass
class GbifOccurrence:
    key: int
    taxon_key: int
    scientific_name: str
    event_date: str   # YYYY-MM-DD (we normalize)
    lat: float
    lon: float
    basis: str
    dataset: str


def _get(path: str, params) -> Dict[str, object]:
    # `params` may be a dict or a list of (key, value) tuples (for multi-value filters).
    time.sleep(INTER_REQUEST_SLEEP_S)
    resp = requests.get(f"{BASE}{path}", params=params, timeout=45)
    resp.raise_for_status()
    return resp.json()


def match_taxon(name: str, rank: Optional[str] = None) -> Optional[Dict[str, object]]:
    """Resolve a scientific name → canonical GBIF taxon metadata (incl. usageKey)."""
    params: Dict[str, object] = {"name": name}
    if rank:
        params["rank"] = rank.upper()
    data = _get("/species/match", params)
    if not data.get("usageKey"):
        return None
    return data


def _cache_path(taxon_key: int, year: int, bbox_tag: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"taxon_{taxon_key}_{year}_{bbox_tag}.json"


def _normalize_event_date(raw: object) -> Optional[str]:
    if not raw:
        return None
    s = str(raw)
    # GBIF returns ISO strings or ranges like "2019-06-02/2019-06-04".
    if "/" in s:
        s = s.split("/", 1)[0]
    # Drop time portion if present.
    if "T" in s:
        s = s.split("T", 1)[0]
    if len(s) < 10:
        return None
    try:
        date.fromisoformat(s[:10])
    except ValueError:
        return None
    return s[:10]


def fetch_occurrences_for_year(
    taxon_key: int, year: int,
    bbox: Dict[str, str] = UPPER_MIDWEST_BBOX,
    bbox_tag: str = "umw",
) -> List[GbifOccurrence]:
    cache = _cache_path(taxon_key, year, bbox_tag)
    if cache.exists():
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
            return [GbifOccurrence(**o) for o in payload]
        except (ValueError, TypeError) as exc:
            LOG.warning("bad cache %s: %s", cache, exc)

    raw: List[Dict[str, object]] = []
    fetch_succeeded = False
    offset = 0
    while True:
        # GBIF wants repeated params for multi-value filters (basisOfRecord, etc);
        # requests serializes a list as repeated keys, so pass a list.
        params = [
            ("taxonKey", taxon_key),
            ("country", "US"),
            ("year", str(year)),
            ("hasCoordinate", "true"),
            ("hasGeospatialIssue", "false"),
            ("basisOfRecord", "HUMAN_OBSERVATION"),
            ("basisOfRecord", "PRESERVED_SPECIMEN"),
            ("basisOfRecord", "MATERIAL_SAMPLE"),
            ("basisOfRecord", "MATERIAL_CITATION"),
            ("limit", PER_PAGE),
            ("offset", offset),
        ]
        params.extend(bbox.items())
        try:
            data = _get("/occurrence/search", params)
        except requests.RequestException as exc:
            LOG.warning("GBIF fetch failed taxon=%d year=%d offset=%d: %s",
                        taxon_key, year, offset, exc)
            break
        results = data.get("results", [])
        fetch_succeeded = True
        if not results:
            break
        raw.extend(results)
        if data.get("endOfRecords") or len(results) < PER_PAGE:
            break
        offset += PER_PAGE
        # GBIF caps offset at 100,000 via anonymous access — we won't hit it for mayflies.
        if offset >= 100_000:
            break

    occs: List[GbifOccurrence] = []
    for r in raw:
        event_date = _normalize_event_date(r.get("eventDate") or r.get("dateIdentified"))
        if not event_date:
            continue
        lat = r.get("decimalLatitude")
        lon = r.get("decimalLongitude")
        if lat is None or lon is None:
            continue
        occs.append(GbifOccurrence(
            key=int(r.get("key") or 0),
            taxon_key=int(r.get("taxonKey") or taxon_key),
            scientific_name=str(r.get("scientificName") or ""),
            event_date=event_date,
            lat=float(lat),
            lon=float(lon),
            basis=str(r.get("basisOfRecord") or ""),
            dataset=str(r.get("datasetKey") or ""),
        ))
    # Only cache when we know the response was real — otherwise a transient
    # 503 becomes a permanent "0 obs" in the cache.
    if fetch_succeeded:
        cache.write_text(
            json.dumps([o.__dict__ for o in occs]),
            encoding="utf-8",
        )
    return occs


def fetch_occurrences(
    taxon_key: int, start_year: int, end_year: int,
    bbox: Dict[str, str] = UPPER_MIDWEST_BBOX,
    bbox_tag: str = "umw",
) -> List[GbifOccurrence]:
    out: List[GbifOccurrence] = []
    for y in range(start_year, end_year + 1):
        out.extend(fetch_occurrences_for_year(taxon_key, y, bbox, bbox_tag))
    return out
