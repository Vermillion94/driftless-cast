"""Expand Wisconsin Driftless reach seed data from WI DNR trout geometries.

The initial MVP seed under-covered Wisconsin badly. This one-off helper adds a
curated batch of well-known western-WI trout streams by:

1. Querying WI DNR trout-regulation polylines for an exact stream name match
2. Converting the returned linework to GeoJSON stored in `reaches.json`
3. Pairing each new reach to either a local USGS gauge or an explicit proxy
4. Backfilling `gauges.json` with any missing gauge metadata

The goal is practical map coverage first, not perfect hydrologic truth. Proxy
gauges are called out in notes so we can revisit them later as we split streams
into finer sub-reaches.
"""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

LOG = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
REACH_SEED = ROOT / "data" / "seed" / "reaches.json"
GAUGE_SEED = ROOT / "data" / "seed" / "gauges.json"

DNR_QUERY_URL = "https://dnrmaps.wi.gov/arcgis/rest/services/FM_Trout/FM_TROUT_REGS_WTM_Ext/MapServer/0/query"
USGS_SITE_URL = "https://waterservices.usgs.gov/nwis/site/"

# Rough western/southwestern Wisconsin Driftless envelope. This keeps the DNR
# exact-name fetch from pulling same-named waters elsewhere in the state.
DRIFTLESS_BBOX = {
    "xmin": -92.9,
    "ymin": 42.5,
    "xmax": -90.3,
    "ymax": 44.95,
    "spatialReference": {"wkid": 4326},
}


@dataclass(frozen=True)
class ReachSpec:
    reach_id: str
    stream_name: str
    dnr_stream_name: str
    segment_name: str
    trout_class: str
    spring_influenced: int
    mean_gradient: float
    usgs_gauge_id: Optional[str] = None
    noaa_lid: Optional[str] = None
    gauge_is_proxy: int = 0


WI_REACH_SPECS: List[ReachSpec] = [
    ReachSpec("rush-esdaile", "Rush River", "Rush River", "near Esdaile", "I", 1, 4.2, usgs_gauge_id="05355322"),
    ReachSpec("buffalo-mondovi", "Buffalo River", "Buffalo River", "near Mondovi", "II", 0, 2.6, usgs_gauge_id="05371920"),
    ReachSpec("plum-ella", "Plum Creek", "Plum Creek", "near Ella", "I", 1, 4.5, usgs_gauge_id="05371050"),
    ReachSpec("waumandee-waumandee", "Waumandee Creek", "Waumandee Creek", "near Waumandee", "I", 1, 5.0, usgs_gauge_id="05371920", gauge_is_proxy=1),
    ReachSpec("coon-coon-valley", "Coon Creek", "Coon Creek", "at Coon Valley", "I", 1, 5.1, usgs_gauge_id="05386500"),
    ReachSpec("spring-coulee-coon-valley", "Spring Coulee Creek", "Spring Coulee Creek", "near Coon Valley", "I", 1, 5.6, usgs_gauge_id="05386490"),
    ReachSpec("timber-coulee-cashton", "Timber Coulee Creek", "Timber Coulee Creek", "near Cashton", "I", 1, 5.4, usgs_gauge_id="05386500", gauge_is_proxy=1),
    ReachSpec("bohemian-valley-coon", "Coon Creek", "Coon Creek (Bohemian Valley)", "Bohemian Valley", "I", 1, 5.2, usgs_gauge_id="05386500", gauge_is_proxy=1),
    ReachSpec("reads-readstown", "Reads Creek", "Reads Creek", "near Readstown", "I", 1, 5.0, usgs_gauge_id="05409270"),
    ReachSpec("bishop-branch-viroqua", "Bishop Branch", "Bishop Branch", "near Viroqua", "I", 1, 5.8, usgs_gauge_id="05409270", gauge_is_proxy=1),
    ReachSpec("tainter-westby", "Tainter Creek", "Tainter Creek", "near Westby", "I", 1, 5.0, usgs_gauge_id="05409668", gauge_is_proxy=1),
    ReachSpec("north-fork-bad-axe-genoa", "North Fork Bad Axe River", "North Fork Bad Axe River", "near Genoa", "I", 1, 4.8, usgs_gauge_id="05387100"),
    ReachSpec("south-fork-bad-axe-westby", "South Fork Bad Axe River", "South Fork Bad Axe River", "near Westby", "I", 1, 4.8, usgs_gauge_id="05387100", gauge_is_proxy=1),
    ReachSpec("north-branch-bad-axe-esofea", "North Branch Bad Axe River", "North Branch Bad Axe River (Esofea Branch)", "Esofea Branch", "I", 1, 5.0, usgs_gauge_id="05387100", gauge_is_proxy=1),
    ReachSpec("west-fork-kickapoo-cashton", "West Fork Kickapoo River", "West Fork Kickapoo River", "at Cashton", "II", 1, 3.3, usgs_gauge_id="05408476"),
    ReachSpec("kickapoo-steuben", "Kickapoo River", "Kickapoo River", "near Steuben", "III", 0, 1.5, usgs_gauge_id="05410490"),
    ReachSpec("trempealeau-arcadia", "Trempealeau River", "Trempealeau River", "near Arcadia", "II", 0, 2.1, usgs_gauge_id="05379400"),
    ReachSpec("la-crosse-sparta", "La Crosse River", "La Crosse River", "at Sparta", "II", 0, 1.7, usgs_gauge_id="05382325"),
]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    phi1 = radians(lat1)
    phi2 = radians(lat2)
    dphi = radians(lat2 - lat1)
    dlam = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlam / 2) ** 2
    return 2 * radius * asin(sqrt(a))


def _polyline_length_km(coords: List[List[float]]) -> float:
    total = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
        total += _haversine_km(lat1, lon1, lat2, lon2)
    return total


def _all_vertices(lines: Iterable[List[List[float]]]) -> List[List[float]]:
    verts: List[List[float]] = []
    for line in lines:
        verts.extend(line)
    return verts


def _bbox_center(vertices: List[List[float]]) -> Tuple[float, float]:
    lons = [v[0] for v in vertices]
    lats = [v[1] for v in vertices]
    return ((min(lats) + max(lats)) / 2.0, (min(lons) + max(lons)) / 2.0)


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def _backup(path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_suffix(f".{ts}.bak{path.suffix}")
    shutil.copy(path, backup)
    return backup


def _escape_sql(value: str) -> str:
    return value.replace("'", "''")


def fetch_dnr_geometry(stream_name: str) -> Tuple[Dict[str, Any], float, float, int]:
    params = {
        "where": f"STREAM = '{_escape_sql(stream_name)}'",
        "geometry": json.dumps(DRIFTLESS_BBOX),
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "STREAM",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json",
        "resultRecordCount": 1000,
    }
    resp = requests.get(DNR_QUERY_URL, params=params, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    features = payload.get("features") or []
    if not features:
        raise RuntimeError(f"no WI DNR geometry for exact stream name {stream_name!r}")

    lines: List[List[List[float]]] = []
    for feature in features:
        geom = feature.get("geometry") or {}
        for path in geom.get("paths") or []:
            if len(path) >= 2:
                lines.append(path)
    if not lines:
        raise RuntimeError(f"WI DNR returned features but no paths for {stream_name!r}")

    verts = _all_vertices(lines)
    centroid_lat, centroid_lon = _bbox_center(verts)
    length_km = sum(_polyline_length_km(line) for line in lines)
    geometry: Dict[str, Any]
    if len(lines) == 1:
        geometry = {"type": "LineString", "coordinates": lines[0]}
    else:
        geometry = {"type": "MultiLineString", "coordinates": lines}
    return geometry, centroid_lat, centroid_lon, len(features)


def fetch_usgs_site_info(site_no: str) -> Optional[Dict[str, Any]]:
    params = {"format": "rdb", "sites": site_no}
    try:
        text = requests.get(USGS_SITE_URL, params=params, timeout=30).text
    except requests.RequestException as exc:
        LOG.warning("USGS site lookup failed for %s: %s", site_no, exc)
        return None
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    if len(lines) < 3:
        return None
    header = lines[0].split("\t")
    values = lines[2].split("\t")
    idx = {name: i for i, name in enumerate(header)}
    try:
        return {
            "gauge_id": values[idx["site_no"]],
            "name": values[idx["station_nm"]],
            "source": "usgs",
            "lat": float(values[idx["dec_lat_va"]]),
            "lon": float(values[idx["dec_long_va"]]),
            "params": ["00060", "00065"],
        }
    except (KeyError, IndexError, ValueError):
        return None


def sync_gauge_seed(specs: List[ReachSpec]) -> Dict[str, Dict[str, Any]]:
    gauges = _load_json(GAUGE_SEED)
    by_id = {str(g["gauge_id"]): g for g in gauges}
    changed = False
    for spec in specs:
        gauge_id = spec.usgs_gauge_id
        if not gauge_id or gauge_id in by_id:
            continue
        info = fetch_usgs_site_info(gauge_id)
        if not info:
            raise RuntimeError(f"unable to fetch metadata for USGS gauge {gauge_id}")
        gauges.append(info)
        by_id[gauge_id] = info
        changed = True
    if changed:
        _backup(GAUGE_SEED)
        gauges.sort(key=lambda g: str(g["gauge_id"]))
        _write_json(GAUGE_SEED, gauges)
    return by_id


def build_note(spec: ReachSpec, gauge_meta: Optional[Dict[str, Any]], matched_features: int) -> str:
    if spec.gauge_is_proxy and gauge_meta:
        return (
            f"Bootstrapped from WI DNR trout-regulation geometry ({matched_features} mapped segments). "
            f"Flow proxy: {gauge_meta['name']} (USGS {gauge_meta['gauge_id']}); "
            "water temperature remains reach-estimated rather than inherited from the proxy watershed."
        )
    if gauge_meta:
        return (
            f"Bootstrapped from WI DNR trout-regulation geometry ({matched_features} mapped segments) "
            f"and paired to local USGS gauge {gauge_meta['gauge_id']}."
        )
    return f"Bootstrapped from WI DNR trout-regulation geometry ({matched_features} mapped segments)."


def build_reach_row(spec: ReachSpec, gauge_lookup: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    geometry, lat, lon, matched = fetch_dnr_geometry(spec.dnr_stream_name)
    gauge_meta = gauge_lookup.get(spec.usgs_gauge_id or "")
    proxy_distance_km = None
    if spec.gauge_is_proxy and gauge_meta:
        proxy_distance_km = round(_haversine_km(lat, lon, gauge_meta["lat"], gauge_meta["lon"]), 1)

    return {
        "reach_id": spec.reach_id,
        "stream_name": spec.stream_name,
        "segment_name": spec.segment_name,
        "state": "WI",
        "trout_class": spec.trout_class,
        "geometry_geojson": json.dumps(geometry),
        "centroid_lat": round(lat, 4),
        "centroid_lon": round(lon, 4),
        "length_km": round(sum(_polyline_length_km(line) for line in (geometry["coordinates"] if geometry["type"] == "MultiLineString" else [geometry["coordinates"]])), 1),
        "mean_gradient": spec.mean_gradient,
        "usgs_gauge_id": spec.usgs_gauge_id,
        "noaa_lid": spec.noaa_lid,
        "gauge_is_proxy": spec.gauge_is_proxy,
        "proxy_distance_km": proxy_distance_km,
        "nws_gridpoint": None,
        "spring_influenced": spec.spring_influenced,
        "notes": build_note(spec, gauge_meta, matched),
    }


def upsert_reaches(specs: List[ReachSpec]) -> int:
    reaches = _load_json(REACH_SEED)
    by_id = {str(r["reach_id"]): r for r in reaches}
    gauge_lookup = sync_gauge_seed(specs)
    changed = 0
    for spec in specs:
        row = build_reach_row(spec, gauge_lookup)
        previous = by_id.get(spec.reach_id)
        if previous != row:
            by_id[spec.reach_id] = row
            changed += 1
            LOG.info("prepared %s", spec.reach_id)
    if changed:
        ordered = list(by_id.values())
        ordered.sort(key=lambda r: (r.get("state", ""), r.get("stream_name", ""), r.get("segment_name", ""), r["reach_id"]))
        _backup(REACH_SEED)
        _write_json(REACH_SEED, ordered)
    return changed


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    changed = upsert_reaches(WI_REACH_SPECS)
    reaches = _load_json(REACH_SEED)
    wi_total = sum(1 for r in reaches if r.get("state") == "WI")
    LOG.info("updated %d reach rows; Wisconsin total is now %d", changed, wi_total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
