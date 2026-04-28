import logging
from datetime import datetime, timezone
from src.ingest import fetch_continuous, fetch_gridpoint, fetch_hourly_forecast, parse_hourly_forecast
from src.db import get_connection

LOG = logging.getLogger(__name__)


def run_hourly_ingest() -> None:
    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.now(timezone.utc)
    # Example: read reaches and refresh NWS forecast for each reach.
    reach_rows = conn.execute("SELECT reach_id, centroid_lat, centroid_lon, nws_gridpoint FROM reach").fetchall()
    for reach in reach_rows:
        reach_id = reach["reach_id"]
        gridpoint = reach["nws_gridpoint"]
        if not gridpoint:
            point_data = fetch_gridpoint(reach["centroid_lat"], reach["centroid_lon"])
            properties = point_data.get("properties", {})
            gridpoint = properties.get("gridId") + "/" + str(properties.get("gridX")) + "," + str(properties.get("gridY"))
            cursor.execute("UPDATE reach SET nws_gridpoint = ? WHERE reach_id = ?", (gridpoint, reach_id))
        forecast = fetch_hourly_forecast(gridpoint)
        parsed = parse_hourly_forecast(forecast)
        forecast_at = now.replace(microsecond=0).isoformat()
        for period in parsed["periods"]:
            cursor.execute(
                "INSERT OR REPLACE INTO weather_forecast (reach_id, forecast_at, valid_at, air_temp_f, dewpoint_f, wind_mph, wind_dir, cloud_cover, precip_prob) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    reach_id,
                    forecast_at,
                    period["valid_at"],
                    period["air_temp_f"],
                    None,
                    period["wind_mph"],
                    period["wind_dir"],
                    None,
                    period["precip_prob"],
                ),
            )
    conn.commit()
    conn.close()
