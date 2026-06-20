import logging
import os
import random
import threading
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from src.api import routes
from src.jobs.forecast_refresh import trigger_forecast_refresh

LOG = logging.getLogger(__name__)

app = FastAPI(title="Driftless Cast")
# Compress responses for clients that accept it. The dominant payload — the
# /reaches stream geometry — is highly repetitive JSON and shrinks ~7x; the
# static map.js / styles.css shrink ~4x. Without this, every page load pulled
# ~3MB of uncompressed geometry. minimum_size skips tiny bodies where the gzip
# header overhead isn't worth it.
app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.include_router(routes.router)

# Serve the static frontend from the same process when SERVE_STATIC=1 (or when
# the web/ directory is alongside the source). This makes single-port hosting
# work — the same Fly.io / Render / Railway service handles both API and UI,
# eliminating CORS issues and the need for a separate nginx container in
# production. Local dev can still point at it via http://localhost:8000.
_WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
if _WEB_DIR.exists() and os.getenv("SERVE_STATIC", "1") != "0":
    app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")

# Forecast refresh cadence. NWS hourly forecast updates ~hourly; QPF and
# Open-Meteo pressure similarly. 60 min is the natural sync interval. Jitter
# stops multiple instances from stampeding USGS / NWS at exactly :00.
REBUILD_INTERVAL_SECONDS = int(os.getenv("DC_REBUILD_INTERVAL_SECONDS", "3600"))
REBUILD_JITTER_SECONDS = 120


def _rebuild_loop() -> None:
    """Periodic forecast rebuild. Without this, build_all only runs on app
    startup — on Fly's `auto_stop_machines = stop` setup the forecast happens
    to refresh on cold-starts, but during a continuously-active day it goes
    stale for hours behind the upstream NWS data. The `/refresh` endpoint
    used to be the only way to force this; now it's automatic.
    """
    while True:
        sleep = REBUILD_INTERVAL_SECONDS + random.uniform(0, REBUILD_JITTER_SECONDS)
        time.sleep(sleep)
        trigger_forecast_refresh()


@app.on_event("startup")
def _kick_initial_forecast() -> None:
    # Fire and forget — don't block startup on the full seeded reach set × NWS latency.
    trigger_forecast_refresh()
    threading.Thread(target=_rebuild_loop, daemon=True).start()


def main() -> None:
    import uvicorn
    uvicorn.run("src.api.main:app", host="0.0.0.0", port=8000, reload=True)
