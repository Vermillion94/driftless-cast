import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

NWPS_BASE = "https://api.water.noaa.gov/nwps/v1"
STALE_CUTOFF_DAYS = 30
LOG = logging.getLogger(__name__)


def _normalize_discharge(value: float, unit: str) -> tuple[float, str]:
    # NWPS reports either "cfs" or "kcfs" for flow; collapse to cfs.
    if unit == "kcfs":
        return value * 1000.0, "cfs"
    return value, unit


def fetch_latest_nwps(lid: str) -> Dict[str, Dict[str, object]]:
    resp = requests.get(f"{NWPS_BASE}/gauges/{lid.lower()}", timeout=30)
    resp.raise_for_status()
    data = resp.json()
    obs = data.get("status", {}).get("observed", {}) or {}
    valid_time = obs.get("validTime")
    if not valid_time:
        return {}
    try:
        observed_dt = datetime.fromisoformat(valid_time.replace("Z", "+00:00"))
    except ValueError:
        return {}
    if (datetime.now(timezone.utc) - observed_dt) > timedelta(days=STALE_CUTOFF_DAYS):
        return {}

    out: Dict[str, Dict[str, object]] = {}
    primary = obs.get("primary")
    primary_unit = obs.get("primaryUnit") or ""
    secondary = obs.get("secondary")
    secondary_unit = obs.get("secondaryUnit") or ""

    def include(value, unit, label_map):
        if value in (None, "", -999, -999.0):
            return
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return
        if unit in ("kcfs", "cfs"):
            numeric, unit = _normalize_discharge(numeric, unit)
            out["00060"] = {
                "label": "discharge_cfs",
                "value": numeric,
                "unit": unit,
                "observed_at": valid_time,
                "source": "nwps",
            }
        elif unit == "ft":
            out["00065"] = {
                "label": "gauge_height_ft",
                "value": numeric,
                "unit": unit,
                "observed_at": valid_time,
                "source": "nwps",
            }

    include(primary, primary_unit, None)
    include(secondary, secondary_unit, None)
    return out


def fetch_forecast_series(lid: str) -> List[Dict[str, object]]:
    """Streamflow forecast time series from NWS NWPS if issued for this gauge.

    Returns [] when no forecast exists (common for small tributaries — NWS
    doesn't run the National Water Model operationally on every creek).
    Each entry: {"valid_at": ISO str, "primary": number, "secondary": number, "unit_secondary": str}
    """
    try:
        resp = requests.get(f"{NWPS_BASE}/gauges/{lid.lower()}/stageflow/forecast", timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        LOG.warning("NWPS forecast fetch failed for %s: %s", lid, exc)
        return []
    payload = resp.json()
    data = payload.get("data") or []
    secondary_unit = payload.get("secondaryUnits") or ""
    primary_unit = payload.get("primaryUnits") or ""
    out: List[Dict[str, object]] = []
    for p in data:
        valid = p.get("validTime")
        if not valid:
            continue
        out.append({
            "valid_at": valid,
            "primary": p.get("primary"),
            "secondary": p.get("secondary"),
            "primary_unit": primary_unit,
            "secondary_unit": secondary_unit,
        })
    return out


def fetch_nwps_metadata(lid: str) -> Optional[Dict[str, object]]:
    try:
        resp = requests.get(f"{NWPS_BASE}/gauges/{lid.lower()}", timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        LOG.warning("NWPS metadata fetch failed for %s: %s", lid, exc)
        return None
    data = resp.json()
    return {
        "lid": data.get("lid"),
        "name": data.get("name"),
        "latitude": data.get("latitude"),
        "longitude": data.get("longitude"),
        "usgs_id": data.get("usgsId") or None,
    }
