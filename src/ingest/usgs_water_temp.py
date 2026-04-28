"""
Discover and pull historical paired air/water temperature data from USGS NWIS.

NorWeST doesn't cover the Midwest, so we refit Mohseni against the real
measurements: USGS NWIS gauges that carry parameter 00010 (water temperature)
and have enough daily-value history to correlate against air temperature from
Open-Meteo.

Output per site: a list of (date, air_temp_f, water_temp_f) pairs that
downstream code can use for Mohseni coefficient fitting.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import requests

NWIS_SITE = "https://waterservices.usgs.gov/nwis/site/"
NWIS_DV = "https://waterservices.usgs.gov/nwis/dv/"
LOG = logging.getLogger(__name__)

UPPER_MIDWEST_STATES = ["mn", "wi", "ia"]


def find_temp_sites(state_codes: List[str] = UPPER_MIDWEST_STATES) -> List[Dict[str, object]]:
    """Streams in the named states that report 00010 (water temp)."""
    out: List[Dict[str, object]] = []
    for state in state_codes:
        params = {
            "stateCd": state,
            "parameterCd": "00010",
            "siteType": "ST",
            "siteStatus": "active",
            "hasDataTypeCd": "dv",
            "format": "rdb",
            "siteOutput": "basic",
        }
        try:
            resp = requests.get(NWIS_SITE, params=params, timeout=45)
            resp.raise_for_status()
        except requests.RequestException as exc:
            LOG.warning("site-list fetch failed state=%s: %s", state, exc)
            continue
        lines = [ln for ln in resp.text.splitlines() if ln and not ln.startswith("#") and not ln.startswith("5s")]
        if len(lines) < 2:
            continue
        header = lines[0].split("\t")
        idx = {name: i for i, name in enumerate(header)}
        for row in lines[1:]:
            parts = row.split("\t")
            if len(parts) <= max(idx.values()):
                continue
            try:
                lat = float(parts[idx["dec_lat_va"]]) if parts[idx["dec_lat_va"]] else None
                lon = float(parts[idx["dec_long_va"]]) if parts[idx["dec_long_va"]] else None
            except (KeyError, ValueError):
                continue
            if lat is None or lon is None:
                continue
            out.append({
                "site_no": parts[idx["site_no"]],
                "station_nm": parts[idx["station_nm"]],
                "state_cd": state,
                "lat": lat,
                "lon": lon,
            })
    return out


def fetch_daily_water_temp_f(site_no: str, start: date, end: date) -> List[Tuple[date, float]]:
    """Daily-mean water temp in °F for the site over the interval."""
    params = {
        "sites": site_no,
        "parameterCd": "00010",
        "startDT": start.isoformat(),
        "endDT": end.isoformat(),
        "format": "json",
        "statCd": "00003",      # mean
    }
    try:
        resp = requests.get(NWIS_DV, params=params, timeout=45)
        resp.raise_for_status()
    except requests.RequestException as exc:
        LOG.warning("DV fetch failed site=%s: %s", site_no, exc)
        return []
    data = resp.json()
    out: List[Tuple[date, float]] = []
    for series in data.get("value", {}).get("timeSeries", []):
        values = series.get("values", [{}])[0].get("value", [])
        for v in values:
            raw = v.get("value")
            dt_iso = v.get("dateTime")
            if raw in (None, "", "-999999") or not dt_iso:
                continue
            try:
                t_c = float(raw)
            except ValueError:
                continue
            try:
                d = datetime.fromisoformat(dt_iso.split("T")[0]).date()
            except ValueError:
                continue
            out.append((d, t_c * 9.0 / 5.0 + 32.0))
    return out
