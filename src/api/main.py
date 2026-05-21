import logging
import os
import random
import threading
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.api import routes

LOG = logging.getLogger(__name__)

app = FastAPI(title="Driftless Cast")
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


def _build_forecast_background() -> None:
    try:
        # Ensure new columns / new tables (e.g. catch_log, regime) exist on
        # an already-initialized DB before the forecast build hits them.
        from src.db import initialize_database, load_reaches
        initialize_database()
        # Re-seed reach metadata on boot. This is intentionally idempotent:
        # it keeps gauge mappings, proxy distances, and notes in sync with
        # data/seed/reaches.json on persistent Fly volumes.
        from src.scripts.bootstrap_reaches import bootstrap_reaches
        if not load_reaches():
            LOG.info("empty reach table — seeding from data/seed/reaches.json")
        bootstrap_reaches()
        from src.models.forecast_builder import build_all
        LOG.info("forecast build starting")
        counts = build_all()
        LOG.info("forecast build complete: %d reaches, %d rows", len(counts), sum(counts.values()))
    except Exception:
        LOG.exception("forecast build failed")


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
        _build_forecast_background()


@app.on_event("startup")
def _kick_initial_forecast() -> None:
    # Fire and forget — don't block startup on ~21 reaches × NWS latency.
    threading.Thread(target=_build_forecast_background, daemon=True).start()
    threading.Thread(target=_rebuild_loop, daemon=True).start()


def main() -> None:
    import uvicorn
    uvicorn.run("src.api.main:app", host="0.0.0.0", port=8000, reload=True)
