"""Simplified stream geometry, built once per process and served as one
cacheable GeoJSON document.

NHDPlus HR centerlines are stored at ~14 decimal places with a vertex every
few meters; shipped inside /reaches on every page load and 5-minute refresh
they were 2.9 MB of JSON, serialized on a 256 MB machine that kept getting
OOM-killed. Geometry never changes at runtime, so: simplify once
(Douglas–Peucker, ~25 m tolerance — invisible at fishing zooms), round to
5 decimals (~1 m), pre-encode to bytes, and let the browser cache it.
"""
from __future__ import annotations

import hashlib
import json
import threading
from typing import Dict, List, Optional, Sequence, Tuple

from src.db import get_connection

TOLERANCE_DEG = 0.00025   # ~25 m at Driftless latitudes (<2 px at zoom 13)
PRECISION = 5

_lock = threading.Lock()
_cache: Optional[Tuple[bytes, str]] = None

Point = Sequence[float]


def _perp_dist(p: Point, a: Point, b: Point) -> float:
    (x, y), (x1, y1), (x2, y2) = p, a, b
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
    px, py = x1 + t * dx, y1 + t * dy
    return ((x - px) ** 2 + (y - py) ** 2) ** 0.5


def simplify(points: List[Point], tol: float = TOLERANCE_DEG) -> List[Point]:
    """Iterative Douglas–Peucker (no recursion limit on long reaches)."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        best, idx = 0.0, -1
        for k in range(i + 1, j):
            d = _perp_dist(points[k], points[i], points[j])
            if d > best:
                best, idx = d, k
        if idx >= 0 and best > tol:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [p for p, k in zip(points, keep) if k]


def _clean(geom: Dict) -> Optional[Dict]:
    def line(coords):
        pts = simplify([(float(x), float(y)) for x, y, *_ in coords])
        return [[round(x, PRECISION), round(y, PRECISION)] for x, y in pts]

    t = geom.get("type")
    if t == "LineString":
        return {"type": "LineString", "coordinates": line(geom["coordinates"])}
    if t == "MultiLineString":
        parts = [line(seg) for seg in geom["coordinates"] if len(seg) >= 2]
        return {"type": "MultiLineString", "coordinates": parts}
    return None


def _build() -> Tuple[bytes, str]:
    conn = get_connection()
    rows = conn.execute("SELECT reach_id, geometry_geojson FROM reach").fetchall()
    conn.close()
    features = []
    for r in rows:
        raw = r["geometry_geojson"]
        if not raw:
            continue
        try:
            geom = _clean(json.loads(raw))
        except (ValueError, TypeError, KeyError):
            geom = None
        if geom:
            features.append({"type": "Feature", "id": r["reach_id"], "geometry": geom,
                             "properties": {"reach_id": r["reach_id"]}})
    body = json.dumps({"type": "FeatureCollection", "features": features},
                      separators=(",", ":")).encode("utf-8")
    etag = '"' + hashlib.sha1(body).hexdigest()[:16] + '"'
    return body, etag


def geometry_payload() -> Tuple[bytes, str]:
    global _cache
    with _lock:
        if _cache is None:
            _cache = _build()
        return _cache


def reset_cache() -> None:
    global _cache
    with _lock:
        _cache = None
