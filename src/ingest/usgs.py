import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

STALE_CUTOFF_DAYS = 60

USGS_BASE = "https://api.waterdata.usgs.gov/ogcapi/v0"
IV_BASE = "https://waterservices.usgs.gov/nwis/iv/"
STAT_BASE = "https://waterservices.usgs.gov/nwis/stat/"
LOG = logging.getLogger(__name__)

PARAM_LABELS = {
    "00060": "discharge_cfs",
    "00065": "gauge_height_ft",
    "00010": "water_temp_c",
}


def _extract_latest(payload: Dict[str, object], source: str) -> Dict[str, Dict[str, object]]:
    # USGS IV/DV sometimes returns archived data from discontinued gauges
    # (e.g. a site's last reading from 2008) — drop anything older than STALE_CUTOFF_DAYS.
    now = datetime.now(timezone.utc)
    out: Dict[str, Dict[str, object]] = {}
    for series in payload.get("value", {}).get("timeSeries", []):
        variable = series.get("variable", {})
        code = variable.get("variableCode", [{}])[0].get("value")
        if code not in PARAM_LABELS:
            continue
        values = series.get("values", [{}])[0].get("value", [])
        if not values:
            continue
        latest = values[-1]
        raw = latest.get("value")
        try:
            numeric = float(raw) if raw not in (None, "", "-999999") else None
        except (TypeError, ValueError):
            numeric = None
        if numeric is None:
            continue
        observed_at = latest.get("dateTime")
        try:
            observed_dt = datetime.fromisoformat(observed_at)
        except (TypeError, ValueError):
            observed_dt = None
        if observed_dt and (now - observed_dt) > timedelta(days=STALE_CUTOFF_DAYS):
            continue
        out[code] = {
            "label": PARAM_LABELS.get(code, code),
            "value": numeric,
            "unit": variable.get("unit", {}).get("unitCode"),
            "observed_at": observed_at,
            "source": source,
        }
    return out


def fetch_daily_stats(gauge_id: str, month: int, day: int) -> Optional[Dict[str, float]]:
    # USGS stat service returns long-run daily-value percentiles per calendar day
    # for discharge (00060). Returns None if the gauge has no stats (decommissioned
    # without a long DV record, or NOAA-only).
    params = {
        "sites": gauge_id,
        "statReportType": "daily",
        "parameterCd": "00060",
        "statTypeCd": "p10,p25,p50,p75,p90",
        "format": "rdb",
    }
    try:
        resp = requests.get(STAT_BASE, params=params, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        LOG.info("stat fetch failed for %s: %s", gauge_id, exc)
        return None
    lines = [ln for ln in resp.text.splitlines() if ln and not ln.startswith("#") and not ln.startswith("5s")]
    if len(lines) < 2:
        return None
    header = lines[0].split("\t")
    col = {name: idx for idx, name in enumerate(header)}
    def maybe_float(idx_key: str, parts: List[str]) -> Optional[float]:
        i = col.get(idx_key)
        if i is None or i >= len(parts):
            return None
        v = parts[i].strip()
        if not v:
            return None
        try:
            return float(v)
        except ValueError:
            return None

    for row in lines[1:]:
        parts = row.split("\t")
        try:
            if int(parts[col["month_nu"]]) != month or int(parts[col["day_nu"]]) != day:
                continue
        except (KeyError, ValueError, IndexError):
            continue
        result: Dict[str, float] = {}
        for key in ("p10", "p25", "p50", "p75", "p90"):
            v = maybe_float(f"{key}_va", parts)
            if v is not None:
                result[key] = v
        # Need at least 3 knots for any sensible interpolation.
        return result if len(result) >= 3 else None
    return None


def discharge_percentile(current_cfs: Optional[float], stats: Optional[Dict[str, float]]) -> float:
    # Interpolate between whichever percentiles USGS published to locate current flow.
    if not stats or current_cfs is None:
        return 0.5
    pct_for_key = {"p10": 0.10, "p25": 0.25, "p50": 0.50, "p75": 0.75, "p90": 0.90}
    knots = sorted((pct_for_key[k], v) for k, v in stats.items() if k in pct_for_key)
    if len(knots) < 2:
        return 0.5
    if current_cfs <= knots[0][1]:
        return max(0.0, knots[0][0] * current_cfs / max(knots[0][1], 0.01))
    if current_cfs >= knots[-1][1]:
        tail = 1.0 - knots[-1][0]
        return min(1.0, knots[-1][0] + tail * min(1.0, (current_cfs - knots[-1][1]) / max(knots[-1][1], 1.0)))
    for (lo_p, lo_v), (hi_p, hi_v) in zip(knots, knots[1:]):
        if lo_v <= current_cfs <= hi_v and hi_v > lo_v:
            frac = (current_cfs - lo_v) / (hi_v - lo_v)
            return lo_p + frac * (hi_p - lo_p)
    return 0.5


def fetch_latest_iv(gauge_id: str, parameter_codes: Optional[List[str]] = None) -> Dict[str, Dict[str, object]]:
    # Instantaneous Values; fall back to Daily Values (last 7 days) when IV is empty.
    codes = parameter_codes or list(PARAM_LABELS.keys())
    iv_params = {"sites": gauge_id, "parameterCd": ",".join(codes), "format": "json"}
    iv_resp = requests.get(IV_BASE, params=iv_params, timeout=20)
    iv_resp.raise_for_status()
    readings = _extract_latest(iv_resp.json(), "iv")
    if readings:
        return readings

    dv_url = "https://waterservices.usgs.gov/nwis/dv/"
    dv_params = {"sites": gauge_id, "parameterCd": ",".join(codes), "format": "json", "period": "P14D"}
    dv_resp = requests.get(dv_url, params=dv_params, timeout=20)
    dv_resp.raise_for_status()
    return _extract_latest(dv_resp.json(), "dv")


def _ogc_location_id(gauge_id: str) -> str:
    """The OGC API requires the `USGS-` agency prefix; bare site numbers 400."""
    return gauge_id if gauge_id.startswith("USGS-") else f"USGS-{gauge_id}"


def _fetch_ogc(
    collection: str,
    gauge_id: str,
    parameter_code: str,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    time_format: str = "%Y-%m-%d",
) -> List[Dict[str, object]]:
    """Fetch features from a USGS OGC-API collection (`continuous` or `daily`).

    The new OGC API uses `datetime=<start>/<end>` for time filtering (ISO 8601
    interval), responds in FeatureCollection format with `properties.time` /
    `properties.value`, and requires the `USGS-` prefix on the location id.
    Returns up to ~10000 results via pagination.
    """
    params = {
        "f": "json",
        "monitoring_location_id": _ogc_location_id(gauge_id),
        "parameter_code": parameter_code,
        "limit": 10000,
    }
    if start and end:
        params["datetime"] = f"{start.strftime(time_format)}/{end.strftime(time_format)}"
    elif start:
        params["datetime"] = f"{start.strftime(time_format)}/.."
    elif end:
        params["datetime"] = f"../{end.strftime(time_format)}"
    url = f"{USGS_BASE}/collections/{collection}/items"
    resp = requests.get(url, params=params, timeout=45)
    resp.raise_for_status()
    data = resp.json()
    features = data.get("features") or data.get("items") or []
    results: List[Dict[str, object]] = []
    for feature in features:
        props = feature.get("properties") or feature  # tolerate the older shape
        observed_at = props.get("time") or props.get("phenomenonTime")
        raw_value = props.get("value", props.get("result"))
        if observed_at is None or raw_value in (None, "", "-999999"):
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        results.append({
            "gauge_id": gauge_id,
            "observed_at": observed_at,
            "parameter_code": parameter_code,
            "value": value,
        })
    return results


def fetch_continuous(gauge_id: str, parameter_code: str, start_time: Optional[datetime] = None, end_time: Optional[datetime] = None) -> List[Dict[str, object]]:
    return _fetch_ogc(
        collection="continuous",
        gauge_id=gauge_id,
        parameter_code=parameter_code,
        start=start_time,
        end=end_time,
        time_format="%Y-%m-%dT%H:%M:%SZ",
    )


def fetch_daily(gauge_id: str, parameter_code: str, start_date: Optional[datetime] = None, end_date: Optional[datetime] = None) -> List[Dict[str, object]]:
    return _fetch_ogc(
        collection="daily",
        gauge_id=gauge_id,
        parameter_code=parameter_code,
        start=start_date,
        end=end_date,
        time_format="%Y-%m-%d",
    )


def latest_observation(gauge_id: str, parameter_code: str) -> Optional[Dict[str, object]]:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    items = fetch_continuous(gauge_id, parameter_code, start_time=now - timedelta(hours=2), end_time=now)
    return items[-1] if items else None
