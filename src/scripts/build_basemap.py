"""
Build the app's own basemap assets: Driftless hillshade tiles + town labels.

Why our own: the USGS Topo basemap is a busy road map, and the clean
commercial basemaps (Esri, Mapbox, CARTO) carry license terms. USGS 3DEP
elevation and Census TIGER places are public domain. Rendering the hillshade
once and serving it statically makes the map fast, distinctive (the Driftless
is defined by its carved terrain) and fully ours.

Outputs (committed, served from web/):
  web/tiles/hillshade/{z}/{x}/{y}.jpg   zooms MIN_Z..MAX_Z over BBOX
  web/data/towns.json                   [{name, state, lat, lon, tier}]

Usage:  python -m src.scripts.build_basemap [--towns-only | --tiles-only]
Re-runnable; existing tiles are skipped unless --force.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import math
import time
from pathlib import Path

import requests
from PIL import Image, ImageEnhance

from src.ingest.identity import USER_AGENT

LOG = logging.getLogger("build_basemap")
ROOT = Path(__file__).resolve().parent.parent.parent
TILE_DIR = ROOT / "web" / "tiles" / "hillshade"
TOWNS_PATH = ROOT / "web" / "data" / "towns.json"

# Driftless Area plus margin (lon/lat). Low zooms cover a wider frame so the
# terrain never ends inside the viewport on a large screen.
BBOX = (-93.7, 42.2, -89.6, 45.3)
WIDE_BBOX = (-96.5, 40.8, -86.8, 46.7)
WIDE_MAX_Z = 9
MIN_Z, MAX_Z = 6, 11
BLOCK = 8  # tiles per side per WMS request (2048 px)

WMS = "https://elevation.nationalmap.gov/arcgis/services/3DEPElevation/ImageServer/WMSServer"
TIGER = ("https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
         "Places_CouSub_ConCity_SubMCD/MapServer/4/query")
STATE_FIPS = {"MN": "27", "WI": "55", "IA": "19"}

# tier 1 = regional anchors (shown zoomed out), tier 2 = towns anglers drive to.
TOWNS = {
    1: [("Rochester", "MN"), ("La Crosse", "WI"), ("Winona", "MN"), ("Decorah", "IA"),
        ("Eau Claire", "WI"), ("Dubuque", "IA"), ("Red Wing", "MN"), ("Prairie du Chien", "WI")],
    2: [("Lanesboro", "MN"), ("Preston", "MN"), ("Chatfield", "MN"), ("Caledonia", "MN"),
        ("Houston", "MN"), ("Rushford", "MN"), ("Spring Grove", "MN"), ("Harmony", "MN"),
        ("Elba", "MN"), ("St. Charles", "MN"), ("Wabasha", "MN"), ("Lake City", "MN"),
        ("Zumbrota", "MN"), ("Viroqua", "WI"), ("Westby", "WI"), ("Coon Valley", "WI"),
        ("Cashton", "WI"), ("La Farge", "WI"), ("Readstown", "WI"), ("Richland Center", "WI"),
        ("Sparta", "WI"), ("Arcadia", "WI"), ("Mondovi", "WI"), ("River Falls", "WI"),
        ("Ellsworth", "WI"), ("Menomonie", "WI"), ("Durand", "WI"), ("Alma", "WI"),
        ("Black River Falls", "WI"), ("Gays Mills", "WI"), ("Soldiers Grove", "WI"),
        ("Ontario", "WI"), ("Boscobel", "WI"), ("Fennimore", "WI"), ("Plum City", "WI"),
        ("Waukon", "IA"), ("Elkader", "IA"), ("McGregor", "IA"), ("Cresco", "IA"),
        ("Postville", "IA"), ("Guttenberg", "IA"), ("Lansing", "IA")],
}

S = requests.Session()
S.headers["User-Agent"] = USER_AGENT


def build_towns() -> None:
    out = []
    for tier, names in TOWNS.items():
        for name, st in names:
            where = f"BASENAME='{name.replace(chr(39), chr(39) * 2)}' AND STATE='{STATE_FIPS[st]}'"
            r = S.get(TIGER, params={"where": where, "outFields": "BASENAME,INTPTLAT,INTPTLON",
                                     "returnGeometry": "false", "f": "json"}, timeout=30)
            r.raise_for_status()
            feats = r.json().get("features") or []
            if not feats:
                LOG.warning("town not found: %s, %s", name, st)
                continue
            a = feats[0]["attributes"]
            out.append({"name": name, "state": st, "tier": tier,
                        "lat": round(float(a["INTPTLAT"]), 4), "lon": round(float(a["INTPTLON"]), 4)})
    TOWNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TOWNS_PATH.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, indent=1)
        fh.write("\n")
    LOG.info("wrote %d towns → %s", len(out), TOWNS_PATH)


def _tile_xy(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y


def _tile_bounds_3857(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    world = 20037508.342789244
    size = 2 * world / (2 ** z)
    minx = -world + x * size
    maxy = world - y * size
    return minx, maxy - size, minx + size, maxy


def _style(img: Image.Image) -> Image.Image:
    # Lift and soften: a quiet backdrop the stream colors sit on, not a feature.
    img = img.convert("L")
    img = ImageEnhance.Contrast(img).enhance(1.15)
    img = ImageEnhance.Brightness(img).enhance(1.10)
    return img


def build_tiles(force: bool = False) -> None:
    for z in range(MIN_Z, MAX_Z + 1):
        bb = WIDE_BBOX if z <= WIDE_MAX_Z else BBOX
        x0, y0 = _tile_xy(bb[0], bb[3], z)
        x1, y1 = _tile_xy(bb[2], bb[1], z)
        count = 0
        for bx in range(x0, x1 + 1, BLOCK):
            for by in range(y0, y1 + 1, BLOCK):
                nx = min(BLOCK, x1 - bx + 1)
                ny = min(BLOCK, y1 - by + 1)
                targets = [(bx + i, by + j) for i in range(nx) for j in range(ny)]
                if not force and all((TILE_DIR / str(z) / str(tx) / f"{ty}.jpg").exists() for tx, ty in targets):
                    continue
                minx, _, _, maxy = _tile_bounds_3857(bx, by, z)
                _, miny, maxx, _ = _tile_bounds_3857(bx + nx - 1, by + ny - 1, z)
                params = {
                    "service": "WMS", "request": "GetMap", "version": "1.3.0",
                    "layers": "3DEPElevation:Hillshade Gray", "styles": "", "crs": "EPSG:3857",
                    "bbox": f"{minx},{miny},{maxx},{maxy}", "width": 256 * nx, "height": 256 * ny,
                    "format": "image/png",
                }
                for attempt in range(4):
                    try:
                        r = S.get(WMS, params=params, timeout=180)
                        r.raise_for_status()
                        mosaic = _style(Image.open(io.BytesIO(r.content)))
                        break
                    except (requests.RequestException, OSError) as exc:
                        LOG.warning("z%d block %d,%d attempt %d failed: %s", z, bx, by, attempt + 1, exc)
                        time.sleep(5 * (attempt + 1))
                else:
                    raise RuntimeError(f"hillshade block z{z} {bx},{by} failed")
                for tx, ty in targets:
                    px, py = (tx - bx) * 256, (ty - by) * 256
                    tile = mosaic.crop((px, py, px + 256, py + 256))
                    path = TILE_DIR / str(z) / str(tx) / f"{ty}.jpg"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tile.save(path, "JPEG", quality=78, optimize=True, progressive=True)
                    count += 1
        LOG.info("z%d: wrote %d tiles (x %d..%d, y %d..%d)", z, count, x0, x1, y0, y1)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--towns-only", action="store_true")
    ap.add_argument("--tiles-only", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if not a.tiles_only:
        build_towns()
    if not a.towns_only:
        build_tiles(force=a.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
