import logging
import re
from typing import Dict, Optional

import requests

BASE_URL = "https://api.weather.gov"
USER_AGENT = "driftless-cast/0.1 (contact: hello@example.com)"
LOG = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/geo+json",
}


def fetch_gridpoint(lat: float, lon: float) -> Optional[Dict[str, object]]:
    url = f"{BASE_URL}/points/{lat},{lon}"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json()


def fetch_hourly_forecast(gridpoint: str) -> Optional[Dict[str, object]]:
    url = f"{BASE_URL}/gridpoints/{gridpoint}/forecast/hourly"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _expand_gridpoint_periods(payload: dict, key: str, expand: str):
    """Generic expander for NWS gridpoint property values.

    NWS reports each property as a list of (validTime, value) where validTime
    is "<ISO>/<ISO 8601 duration>". We expand each period to per-hour entries.

    `expand` controls what the per-hour value is:
      - "block_total"    — divide total across each hour in the period (precip)
      - "instantaneous"  — repeat the period's value for each hour (pressure, etc.)
    """
    import re as _re
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    props = payload.get("properties", {})
    block = props.get(key, {})
    out = []
    for entry in block.get("values", []):
        vt = entry.get("validTime")
        val = entry.get("value")
        if not vt or val is None:
            continue
        try:
            start_s, dur_s = vt.split("/")
            start = _dt.fromisoformat(start_s)
            if start.tzinfo is None:
                start = start.replace(tzinfo=_tz.utc)
            m = _re.match(r"PT(\d+)H", dur_s)
            if not m:
                continue
            hours = int(m.group(1))
        except (ValueError, AttributeError):
            continue
        per_hour = float(val) / max(hours, 1) if expand == "block_total" else float(val)
        for i in range(hours):
            out.append((start + _td(hours=i), per_hour))
    return out


def fetch_gridpoint_qpf_mm(gridpoint: str):
    """Hourly precipitation (mm) forecast, expanded from NWS's variable-duration periods."""
    url = f"{BASE_URL}/gridpoints/{gridpoint}"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return _expand_gridpoint_periods(resp.json(), "quantitativePrecipitation", "block_total")


def fetch_gridpoint_pressure_pa(gridpoint: str):
    """Hourly air pressure (Pascals) forecast. Same gridpoint payload as QPF, so we can
    cache the raw response, but for now we just refetch — NWS is fast."""
    url = f"{BASE_URL}/gridpoints/{gridpoint}"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return _expand_gridpoint_periods(resp.json(), "pressure", "instantaneous")


def _parse_wind_speed(raw: object) -> Optional[float]:
    # NWS returns wind speed as "10 mph" or "5 to 10 mph"; use the upper bound.
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    numbers = re.findall(r"\d+(?:\.\d+)?", str(raw))
    if not numbers:
        return None
    return float(numbers[-1])


# Rough cloud-cover fraction from NWS shortForecast text. NWS hourly forecast
# doesn't include numeric skyCover in the /forecast/hourly payload (only in the
# raw /gridpoints endpoint), so we parse the text label. "Sunny" → 0.1, etc.
_CLOUD_COVER_HINTS = (
    ("overcast", 0.95),
    ("cloudy", 0.85),
    ("mostly cloudy", 0.75),
    ("partly sunny", 0.55),
    ("partly cloudy", 0.45),
    ("mostly sunny", 0.25),
    ("mostly clear", 0.20),
    ("sunny", 0.10),
    ("clear", 0.05),
    ("fair", 0.15),
    ("fog", 0.9),
    ("rain", 0.9),
    ("showers", 0.85),
    ("thunderstorm", 0.9),
    ("snow", 0.9),
    ("drizzle", 0.85),
)


def _parse_cloud_cover(short_forecast: Optional[str]) -> Optional[float]:
    if not short_forecast:
        return None
    text = short_forecast.lower()
    # Longest keyword wins so "mostly cloudy" beats "cloudy".
    best: Optional[tuple[int, float]] = None
    for key, value in _CLOUD_COVER_HINTS:
        if key in text and (best is None or len(key) > best[0]):
            best = (len(key), value)
    return best[1] if best else None


def parse_hourly_forecast(forecast: Dict[str, object]) -> Dict[str, object]:
    periods = forecast.get("properties", {}).get("periods", [])
    parsed = []
    for period in periods:
        short = period.get("shortForecast")
        parsed.append({
            "valid_at": period.get("startTime"),
            "air_temp_f": period.get("temperature"),
            "wind_mph": _parse_wind_speed(period.get("windSpeed")),
            "wind_dir": period.get("windDirection"),
            "short_forecast": short,
            "cloud_cover": _parse_cloud_cover(short),
            "precip_prob": period.get("probabilityOfPrecipitation", {}).get("value"),
        })
    return {"periods": parsed}
