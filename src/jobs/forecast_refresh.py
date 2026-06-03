from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Dict, Optional

from src.db import initialize_database, load_reaches
from src.scripts.bootstrap_reaches import bootstrap_reaches

LOG = logging.getLogger(__name__)

_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_RUNNING = False
_LAST_STARTED_AT: Optional[str] = None
_LAST_FINISHED_AT: Optional[str] = None
_LAST_ERROR: Optional[str] = None
_LAST_COUNTS: Optional[Dict[str, int]] = None


def _set_state(**updates: object) -> None:
    global _RUNNING, _LAST_STARTED_AT, _LAST_FINISHED_AT, _LAST_ERROR, _LAST_COUNTS
    with _STATE_LOCK:
        if "running" in updates:
            _RUNNING = bool(updates["running"])
        if "last_started_at" in updates:
            _LAST_STARTED_AT = updates["last_started_at"]  # type: ignore[assignment]
        if "last_finished_at" in updates:
            _LAST_FINISHED_AT = updates["last_finished_at"]  # type: ignore[assignment]
        if "last_error" in updates:
            _LAST_ERROR = updates["last_error"]  # type: ignore[assignment]
        if "last_counts" in updates:
            _LAST_COUNTS = updates["last_counts"]  # type: ignore[assignment]


def refresh_state() -> Dict[str, object]:
    with _STATE_LOCK:
        counts = dict(_LAST_COUNTS or {})
        return {
            "running": _RUNNING,
            "last_started_at": _LAST_STARTED_AT,
            "last_finished_at": _LAST_FINISHED_AT,
            "last_error": _LAST_ERROR,
            "last_reach_count": len(counts),
            "last_hours_written": sum(counts.values()) if counts else 0,
        }


def run_forecast_refresh() -> Optional[Dict[str, int]]:
    started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if not _LOCK.acquire(blocking=False):
        LOG.info("forecast refresh already running; skipping duplicate trigger")
        return None
    _set_state(running=True, last_started_at=started_at, last_error=None)
    try:
        initialize_database()
        if not load_reaches():
            LOG.info("empty reach table — seeding from data/seed/reaches.json")
        bootstrap_reaches()
        from src.models.forecast_builder import build_all
        LOG.info("forecast build starting")
        counts = build_all()
        LOG.info("forecast build complete: %d reaches, %d rows", len(counts), sum(counts.values()))
        _set_state(
            running=False,
            last_finished_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            last_error=None,
            last_counts=counts,
        )
        return counts
    except Exception as exc:
        LOG.exception("forecast build failed")
        _set_state(
            running=False,
            last_finished_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            last_error=str(exc),
        )
        raise
    finally:
        _LOCK.release()


def trigger_forecast_refresh() -> bool:
    if refresh_state()["running"]:
        return False
    thread = threading.Thread(target=run_forecast_refresh, daemon=True)
    thread.start()
    return True
