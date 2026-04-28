"""
Wisconsin DNR fisheries enrichment — queries trout regulation categories
and gear restrictions per stream name, which are the strongest publicly
available proxies for "is this a wild/managed trout stream."

WI DNR's ArcGIS REST service for trout regulations:
    https://dnrmaps.wi.gov/arcgis/rest/services/FM_Trout/FM_TROUT_REGS_WTM_Ext/MapServer/0

Results are cached on disk since the data changes only on annual reg cycles.
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

import requests

BASE = "https://dnrmaps.wi.gov/arcgis/rest/services/FM_Trout/FM_TROUT_REGS_WTM_Ext/MapServer/0/query"
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "dnr_cache" / "wi"
LOG = logging.getLogger(__name__)


def _cache_path(stream_slug: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in stream_slug.lower())
    return CACHE_DIR / f"{safe}.json"


def fetch_trout_regs(stream_name: str) -> List[Dict[str, object]]:
    """All WI DNR trout-reg segment rows whose STREAM field contains `stream_name` (case-insensitive)."""
    cache = _cache_path(stream_name)
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            pass

    # ArcGIS where clause — escape single quotes.
    safe = stream_name.replace("'", "''")
    where = f"UPPER(STREAM) LIKE UPPER('%{safe}%')"
    params = {
        "where": where,
        "outFields": "STREAM,REGCAT,GEAR_RESTRICTIONS,SEASON_TXT,EARLY_SEASON_TXT,BAG_LMT,SPECIALREG1",
        "returnGeometry": "false",
        "f": "json",
        "resultRecordCount": 200,
    }
    try:
        resp = requests.get(BASE, params=params, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        LOG.warning("WI DNR fetch failed for %s: %s", stream_name, exc)
        return []
    payload = resp.json()
    features = payload.get("features", [])
    rows = [f.get("attributes", {}) for f in features]
    cache.write_text(json.dumps(rows, indent=0), encoding="utf-8")
    return rows


def summarize(rows: List[Dict[str, object]]) -> Optional[Dict[str, object]]:
    """Collapse raw rows into a short UI-friendly summary."""
    if not rows:
        return None
    regcats = Counter(r.get("REGCAT") for r in rows if r.get("REGCAT"))
    gear = Counter(r.get("GEAR_RESTRICTIONS") for r in rows if r.get("GEAR_RESTRICTIONS"))
    specials = [r.get("SPECIALREG1") for r in rows if r.get("SPECIALREG1")]
    top_regcat = regcats.most_common(1)[0][0] if regcats else None
    top_gear = gear.most_common(1)[0][0] if gear else None

    # Very rough class proxy from regcat text — WI class I streams typically
    # carry more restrictive bag limits (3 or fewer) and artificial-only gear.
    text = " ".join(filter(None, [top_regcat or "", top_gear or "", " ".join(specials)])).upper()
    tier = None
    if "ARTIFICIAL" in text or "FLIES ONLY" in text:
        tier = "managed wild (artificial-only)"
    elif "3 TROUT" in (top_regcat or "").upper() or "5 TROUT" in (top_regcat or "").upper():
        # Middle-ground regulation — mixed / stocked-supported
        tier = "mixed fishery"
    elif regcats:
        tier = "stocked-supported"

    return {
        "segments_matched": len(rows),
        "top_reg_category": top_regcat,
        "top_gear_restrictions": top_gear,
        "inferred_tier": tier,
    }
