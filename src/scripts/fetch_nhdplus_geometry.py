"""
Replace the placeholder 2-point reach LineStrings with real stream centerlines
from USGS NHDPlus High-Resolution flowlines.

The seed data started with hand-picked endpoints for each reach (e.g.
`[[-92.40, 44.62], [-92.33, 44.57]]`), which renders as a straight line that
doesn't follow the actual stream. This script queries NHDPlus HR's
NetworkNHDFlowline layer (id=3) by GNIS_NAME within a bbox around the reach
centroid, and replaces `geometry_geojson` with a MultiLineString of the
matched segments. The rendered polyline now traces the river the way it
actually flows.

Source: USGS NHDPlus High Resolution
  https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer/3
  See docs/REFERENCES.md (NHDPlus is the "official" stream-geometry source).

Usage:
    python -m src.scripts.fetch_nhdplus_geometry              # all reaches
    python -m src.scripts.fetch_nhdplus_geometry --reach rush-maiden-rock
    python -m src.scripts.fetch_nhdplus_geometry --dry-run    # preview only

Re-runnable. Reaches whose NHDPlus query returns nothing keep their existing
geometry. The script writes a backup of the previous reaches.json next to it
on every run.
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

LOG = logging.getLogger(__name__)
SEED_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "seed" / "reaches.json"
NHDPLUS_URL = "https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer/3/query"

# Stream-name aliases for cases where the reach's `stream_name` doesn't
# exactly match the NHDPlus GNIS_NAME field. Maps reach.stream_name → list of
# GNIS substrings to match.
NAME_ALIASES: Dict[str, List[str]] = {
    "Kinnickinnic River":            ["Kinnickinnic"],
    "Rush River":                    ["Rush River"],
    "Willow River":                  ["Willow River"],
    "Apple River":                   ["Apple River"],
    "Kickapoo River":                ["Kickapoo"],
    "Upper Iowa River":              ["Upper Iowa"],
    "Yellow River":                  ["Yellow River"],
    "Root River":                    ["Root River"],
    "South Fork Root River":         ["South Fork Root", "S Fk Root", "South Branch Root"],
    "Rush Creek":                    ["Rush Creek"],
    "Whitewater River":              ["Whitewater"],
    "North Fork Whitewater River":   ["North Fork Whitewater", "N Fk Whitewater", "North Branch Whitewater"],
    "South Fork Whitewater River":   ["South Fork Whitewater", "S Fk Whitewater", "Middle Fork Whitewater"],
    "Trout Run Creek":               ["Trout Run"],
    "Pine Creek":                    ["Pine Creek"],
    "Beaver Creek":                  ["Beaver Creek"],
    "Winnebago Creek":               ["Winnebago Creek"],
    "Garvin Brook":                  ["Garvin Brook"],
    "Hay Creek":                     ["Hay Creek"],
    "West Fork Kickapoo River":      ["West Fork Kickapoo", "W Fork Kickapoo", "West Branch Kickapoo"],
    "Coon Creek":                    ["Coon Creek"],
    "Timber Coulee Creek":           ["Timber Coulee", "Timber Cooley"],
}

# Bounding-box radius (degrees) around the centroid for the NHDPlus query.
# 0.15° ≈ 17 km — generous enough to cover most reaches with a few-mile buffer
# but not so wide that we pull in unrelated same-name streams. (Many "Beaver
# Creek"s exist in MN/WI; the bbox keeps us local.)
BBOX_RADIUS_DEG = 0.15


def _build_where_clause(aliases: List[str]) -> str:
    """Match any of the alias substrings against gnis_name (case-insensitive)."""
    parts = [f"UPPER(gnis_name) LIKE UPPER('%{a.replace(chr(39), chr(39)+chr(39))}%')" for a in aliases]
    return " OR ".join(parts)


def _query_nhdplus(centroid_lat: float, centroid_lon: float, where: str) -> List[Dict]:
    bbox = {
        "xmin": centroid_lon - BBOX_RADIUS_DEG,
        "ymin": centroid_lat - BBOX_RADIUS_DEG,
        "xmax": centroid_lon + BBOX_RADIUS_DEG,
        "ymax": centroid_lat + BBOX_RADIUS_DEG,
    }
    params = {
        "where": where,
        "geometry": json.dumps(bbox),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "gnis_name,lengthkm,streamorde,nhdplusid",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    resp = requests.get(NHDPLUS_URL, params=params, timeout=45)
    resp.raise_for_status()
    return resp.json().get("features") or []


def _extract_linestrings(features: List[Dict], aliases: List[str]) -> List[List[List[float]]]:
    """Return only LineString coordinate arrays for features whose gnis_name
    actually matches one of our aliases (defense against the bbox returning
    same-named-but-distant or unrelated streams)."""
    alias_lower = [a.lower() for a in aliases]
    out: List[List[List[float]]] = []
    for f in features:
        props = f.get("properties") or {}
        name = (props.get("gnis_name") or "").lower()
        if not any(a in name for a in alias_lower):
            continue
        geom = f.get("geometry") or {}
        if geom.get("type") == "LineString":
            out.append(geom["coordinates"])
        elif geom.get("type") == "MultiLineString":
            out.extend(geom["coordinates"])
    return out


def fetch_reach_geometry(reach: Dict) -> Optional[Tuple[Dict, int, float]]:
    """Returns (geometry_geojson_dict, segment_count, total_km) or None."""
    stream_name = reach.get("stream_name")
    if not stream_name:
        return None
    aliases = NAME_ALIASES.get(stream_name, [stream_name])
    where = _build_where_clause(aliases)
    try:
        feats = _query_nhdplus(
            float(reach["centroid_lat"]),
            float(reach["centroid_lon"]),
            where,
        )
    except Exception as exc:
        LOG.warning("NHDPlus query failed for %s: %s", reach["reach_id"], exc)
        return None

    lines = _extract_linestrings(feats, aliases)
    if not lines:
        return None
    total_km = sum(_polyline_length_km(l) for l in lines)
    if len(lines) == 1:
        geometry = {"type": "LineString", "coordinates": lines[0]}
    else:
        geometry = {"type": "MultiLineString", "coordinates": lines}
    return geometry, len(lines), total_km


def _polyline_length_km(coords: List[List[float]]) -> float:
    """Great-circle approximation by summing haversine distances between
    successive vertices. Good enough for reach-sized lines (km, not km×1000)."""
    import math
    R = 6371.0
    total = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
        total += 2 * R * math.asin(math.sqrt(a))
    return total


def _bbox_centroid_check(geom: Dict, claimed: Tuple[float, float]) -> Tuple[bool, float]:
    """Returns (centroid is on/near the geometry, distance km).
    The reach's stored centroid should be near the polyline; if it's >5km away,
    the seed centroid is suspect (and the polyline is probably wrong stream)."""
    import math
    R = 6371.0
    lat0, lon0 = claimed
    coords: List[List[float]] = []
    if geom["type"] == "LineString":
        coords = geom["coordinates"]
    elif geom["type"] == "MultiLineString":
        for line in geom["coordinates"]:
            coords.extend(line)
    if not coords:
        return (False, 0.0)
    best = float("inf")
    for lon, lat in coords:
        phi1, phi2 = math.radians(lat0), math.radians(lat)
        dphi = math.radians(lat - lat0)
        dlam = math.radians(lon - lon0)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
        d = 2 * R * math.asin(math.sqrt(a))
        if d < best:
            best = d
    return (best < 5.0, best)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--reach", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-bbox-km", type=float, default=5.0,
                        help="Centroid must be within this km of the new polyline (sanity check).")
    args = parser.parse_args()

    with SEED_PATH.open("r", encoding="utf-8") as fh:
        reaches = json.load(fh)

    targets = reaches if not args.reach else [r for r in reaches if r["reach_id"] == args.reach]
    if args.reach and not targets:
        print(f"unknown reach: {args.reach}")
        return 1

    updated = 0
    skipped: List[Tuple[str, str]] = []
    for reach in targets:
        rid = reach["reach_id"]
        result = fetch_reach_geometry(reach)
        if not result:
            skipped.append((rid, "no NHDPlus match"))
            print(f"  {rid:<32}  SKIP — no flowline match")
            continue
        geometry, n_segs, total_km = result
        on_centroid, dist = _bbox_centroid_check(
            geometry,
            (float(reach["centroid_lat"]), float(reach["centroid_lon"])),
        )
        if not on_centroid:
            skipped.append((rid, f"centroid {dist:.1f}km from polyline"))
            print(f"  {rid:<32}  SKIP — centroid {dist:.1f}km from new polyline (suspicious; double-check)")
            continue

        old = reach.get("geometry_geojson", "")
        new = json.dumps(geometry)
        if old == new:
            print(f"  {rid:<32}  unchanged ({n_segs} segs, {total_km:.1f}km)")
            continue
        reach["geometry_geojson"] = new
        updated += 1
        print(f"  {rid:<32}  OK  {n_segs:>2} segs  {total_km:>5.1f}km  centroid {dist:.2f}km away")

    if updated and not args.dry_run:
        # Backup previous seed
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = SEED_PATH.with_suffix(f".{ts}.bak.json")
        shutil.copy(SEED_PATH, backup)
        with SEED_PATH.open("w", encoding="utf-8") as fh:
            json.dump(reaches, fh, indent=2, ensure_ascii=False)
        print()
        print(f"Wrote {updated} updated geometries to {SEED_PATH}")
        print(f"Backup: {backup}")
        print(f"Run `python -m src.scripts.bootstrap_reaches` to push to DB.")
    else:
        print()
        print(f"{updated} would be updated (dry run)" if args.dry_run else f"No changes ({updated} matched but identical to existing)")

    if skipped:
        print()
        print(f"Skipped {len(skipped)}:")
        for rid, reason in skipped:
            print(f"  {rid}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
