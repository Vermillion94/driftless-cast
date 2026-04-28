"""
Solar altitude — degrees of the sun above the horizon at (lat, lon, datetime).

Standard NOAA solar position approximation (declination + equation-of-time).
Accuracy is well within ±1° at our latitudes — fine for fishing-quality
modulation, where we only care about coarse bands (low / mid / high sun).
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import asin, cos, degrees, pi, radians, sin


def sun_altitude_deg(lat: float, lon: float, dt: datetime) -> float:
    """Sun altitude in degrees above the horizon. Negative = below horizon."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_utc = dt.astimezone(timezone.utc)
    doy = dt_utc.timetuple().tm_yday
    decl = radians(23.45 * sin(radians(360.0 * (284 + doy) / 365.0)))
    # Equation of time (minutes); minor correction
    b = radians(360.0 * (doy - 81) / 365.0)
    eot = 9.87 * sin(2 * b) - 7.53 * cos(b) - 1.5 * sin(b)
    utc_hours = dt_utc.hour + dt_utc.minute / 60.0 + dt_utc.second / 3600.0
    solar_time = utc_hours + lon / 15.0 + eot / 60.0
    hour_angle = radians(15.0 * (solar_time - 12.0))
    lat_r = radians(lat)
    altitude = asin(sin(lat_r) * sin(decl) + cos(lat_r) * cos(decl) * cos(hour_angle))
    return degrees(altitude)


def surface_brightness_factor(sun_alt_deg: float, cloud_cover: float) -> float:
    """0.0 = effectively dark; 1.0 = full bright high sun, no cover.

    Below 5° altitude: nearly dark regardless of cloud (sun near/below horizon).
    Linear ramp to 1.0 at 70° altitude.
    Multiplied by (1 - cloud_cover) so overcast days never feel "bright."
    """
    if sun_alt_deg < 5:
        return 0.0
    alt_norm = min(1.0, max(0.0, (sun_alt_deg - 5) / 65.0))
    cc = 0.5 if cloud_cover is None else max(0.0, min(1.0, cloud_cover))
    return alt_norm * (1.0 - cc)


def bright_sun_dry_penalty(sun_alt_deg: float, cloud_cover: float) -> float:
    """Multiplier (≤ 1.0) applied to dry-fly score under bright high-angle sun.

    Driftless trout (especially in clear spring-creek water) get reluctant to
    rise to flies in bright midday sun. Cloud cover or low sun mitigates this.

    Returns:
      1.0 = no penalty (low sun OR overcast)
      0.65 = max penalty (high sun + clear sky)
    """
    b = surface_brightness_factor(sun_alt_deg, cloud_cover)
    return 1.0 - 0.35 * b
