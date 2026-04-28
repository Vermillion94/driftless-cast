"""
iDigBio specimen ingest for species-phenology calibration.

iDigBio aggregates U.S. natural-history museum collections directly. For our
target Driftless mayflies and caddisflies, iDigBio typically holds 4–17× the
records GBIF reports (some museums push to GBIF on a delay; iDigBio harvests
directly). All entries are PRESERVED_SPECIMEN equivalents — properly
identified by entomologists, with collection-date and locality.

Cached on disk per (scientific_name, bbox_tag) — bulk pulls (no year filter)
since records are static.
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

BASE = "https://search.idigbio.org/v2/search/records/"
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "calibration" / "idigbio_cache"
PER_PAGE = 1000
INTER_REQUEST_SLEEP_S = 0.4
LOG = logging.getLogger(__name__)

DRIFTLESS_BBOX = {
    "top_left":     {"lat": 45.5, "lon": -93.5},
    "bottom_right": {"lat": 43.0, "lon": -90.0},
}
UPPER_MIDWEST_BBOX = {
    "top_left":     {"lat": 47.0, "lon": -96.0},
    "bottom_right": {"lat": 41.0, "lon": -87.0},
}


@dataclass
class IDigBioOccurrence:
    uuid: str
    scientific_name: str
    event_date: str       # YYYY-MM-DD (we normalize)
    lat: float
    lon: float
    recordset: Optional[str]


def _cache_path(scientific_name: str, bbox_tag: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in scientific_name.lower())
    return CACHE_DIR / f"{safe}_{bbox_tag}.json"


def _normalize_date(raw: object) -> Optional[str]:
    if not raw:
        return None
    s = str(raw)
    if "/" in s:
        s = s.split("/", 1)[0]   # iDigBio sometimes returns ranges "YYYY-MM-DD/YYYY-MM-DD"
    if "T" in s:
        s = s.split("T", 1)[0]
    if len(s) < 10:
        return None
    try:
        date.fromisoformat(s[:10])
    except ValueError:
        return None
    return s[:10]


def fetch_records(scientific_name: str,
                   bbox: Dict[str, Dict[str, float]] = UPPER_MIDWEST_BBOX,
                   bbox_tag: str = "umw") -> List[IDigBioOccurrence]:
    """All iDigBio records matching `scientific_name` within the bbox."""
    cache = _cache_path(scientific_name, bbox_tag)
    if cache.exists():
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
            return [IDigBioOccurrence(**o) for o in payload]
        except (ValueError, TypeError) as exc:
            LOG.warning("bad cache %s: %s", cache, exc)

    raw_items: List[dict] = []
    offset = 0
    fetch_succeeded = False
    while True:
        body = {
            "rq": {
                "scientificname": scientific_name,
                "geopoint": {
                    "type": "geo_bounding_box",
                    "top_left": bbox["top_left"],
                    "bottom_right": bbox["bottom_right"],
                },
            },
            "limit": PER_PAGE,
            "offset": offset,
        }
        time.sleep(INTER_REQUEST_SLEEP_S)
        try:
            resp = requests.post(BASE, json=body, timeout=45)
            resp.raise_for_status()
        except requests.RequestException as exc:
            LOG.warning("iDigBio fetch failed (%s offset=%d): %s",
                        scientific_name, offset, exc)
            break
        data = resp.json()
        fetch_succeeded = True
        items = data.get("items", [])
        if not items:
            break
        raw_items.extend(items)
        if len(items) < PER_PAGE:
            break
        offset += PER_PAGE
        if offset >= 50000:
            break

    occurrences: List[IDigBioOccurrence] = []
    for item in raw_items:
        d = item.get("data") or {}
        idx = item.get("indexTerms") or {}
        # iDigBio prefers Darwin Core fields under data; index terms are also useful
        event_date = _normalize_date(d.get("dwc:eventDate") or idx.get("datecollected"))
        if not event_date:
            continue
        lat = d.get("dwc:decimalLatitude") or idx.get("geopoint", {}).get("lat")
        lon = d.get("dwc:decimalLongitude") or idx.get("geopoint", {}).get("lon")
        try:
            lat_f = float(lat) if lat is not None else None
            lon_f = float(lon) if lon is not None else None
        except (TypeError, ValueError):
            continue
        if lat_f is None or lon_f is None:
            continue
        occurrences.append(IDigBioOccurrence(
            uuid=str(item.get("uuid") or ""),
            scientific_name=str(d.get("dwc:scientificName") or idx.get("scientificname") or scientific_name),
            event_date=event_date,
            lat=lat_f,
            lon=lon_f,
            recordset=item.get("indexTerms", {}).get("recordset"),
        ))
    if fetch_succeeded:
        cache.write_text(
            json.dumps([o.__dict__ for o in occurrences]),
            encoding="utf-8",
        )
    return occurrences
