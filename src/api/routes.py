import json
import logging
from pathlib import Path

import requests
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from src.db import (
    get_connection,
    get_reach,
    get_reach_predictions,
    insert_catch_log,
    list_catch_logs,
    list_reach_summaries,
    reach_residuals,
    reliability_diagram,
    scores_grid,
    top_windows,
)
from src.ingest import fetch_latest_iv, fetch_latest_nwps

LOG = logging.getLogger(__name__)
router = APIRouter()

_GAUGE_SEED = Path(__file__).resolve().parent.parent.parent / "data" / "seed" / "gauges.json"


def _load_gauge_names() -> Dict[str, str]:
    try:
        with _GAUGE_SEED.open("r", encoding="utf-8") as fh:
            return {g["gauge_id"]: g.get("name", g["gauge_id"]) for g in json.load(fh)}
    except (OSError, ValueError) as exc:
        LOG.warning("gauge seed load failed: %s", exc)
        return {}


_GAUGE_NAMES = _load_gauge_names()


@router.get("/reaches")
def get_reaches() -> List[dict]:
    return list_reach_summaries()


@router.get("/reach/{reach_id}")
def get_reach_detail(reach_id: str) -> dict:
    reach = get_reach(reach_id)
    if not reach:
        raise HTTPException(status_code=404, detail="Reach not found")
    return reach


@router.get("/conditions/{reach_id}")
def get_reach_conditions(reach_id: str) -> dict:
    reach = get_reach(reach_id)
    if not reach:
        raise HTTPException(status_code=404, detail="Reach not found")
    gauge_id = reach.get("usgs_gauge_id")
    noaa_lid = reach.get("noaa_lid")
    response = {
        "reach_id": reach_id,
        "stream_name": reach.get("stream_name"),
        "segment_name": reach.get("segment_name"),
        "state": reach.get("state"),
        "trout_class": reach.get("trout_class"),
        "spring_influenced": bool(reach.get("spring_influenced")),
        "notes": reach.get("notes"),
        "usgs_gauge_id": gauge_id,
        "noaa_lid": noaa_lid,
        "gauge_name": _GAUGE_NAMES.get(gauge_id) if gauge_id else _GAUGE_NAMES.get(noaa_lid) if noaa_lid else None,
        "gauge_source": "noaa" if noaa_lid else ("usgs" if gauge_id else None),
        "gauge_is_proxy": bool(reach.get("gauge_is_proxy")),
        "readings": None,
        "readings_error": None,
    }
    try:
        if noaa_lid:
            response["readings"] = fetch_latest_nwps(noaa_lid)
        elif gauge_id:
            response["readings"] = fetch_latest_iv(gauge_id)
    except requests.RequestException as exc:
        source = "NOAA" if noaa_lid else "USGS"
        LOG.warning("%s fetch failed for %s: %s", source, noaa_lid or gauge_id, exc)
        response["readings_error"] = f"{source} service unreachable"
    return response


@router.get("/forecast/{reach_id}")
def get_reach_forecast(reach_id: str, hours: int = Query(168, ge=1, le=168)) -> dict:
    reach = get_reach(reach_id)
    if not reach:
        raise HTTPException(status_code=404, detail="Reach not found")
    rows = get_reach_predictions(reach_id, hours)
    hours_payload = []
    computed_at = None
    for r in rows:
        computed_at = r.get("computed_at") or computed_at
        try:
            active = json.loads(r.get("active_species") or "[]")
        except ValueError:
            active = []
        try:
            flies = json.loads(r.get("recommended_flies") or "null")
        except ValueError:
            flies = None
        try:
            regime = json.loads(r.get("regime") or "null")
        except ValueError:
            regime = None
        try:
            score_breakdown = json.loads(r.get("score_breakdown") or "null")
        except ValueError:
            score_breakdown = None
        hours_payload.append({
            "valid_at": r["valid_at"],
            "nymph_score": r.get("nymph_score"),
            "dry_score": r.get("dry_score"),
            "combined_score": max(r.get("nymph_score") or 0.0, r.get("dry_score") or 0.0),
            "active_species": active,
            "flies": flies,
            "explanation": r.get("explanation"),
            "water_temp_f": r.get("water_temp_f"),
            "water_temp_source": r.get("water_temp_source"),
            "anomaly_f": r.get("anomaly_f"),
            "hatch_shift_days": r.get("hatch_shift_days"),
            "fish_stress": bool(r.get("fish_stress")),
            "air_temp_f": r.get("air_temp_f"),
            "cloud_cover": r.get("cloud_cover"),
            "wind_mph": r.get("wind_mph"),
            "flow_cfs": r.get("flow_cfs"),
            "precip_prob": r.get("precip_prob"),
            "short_forecast": r.get("short_forecast"),
            "regime": regime,
            "pressure_delta_mb": r.get("pressure_delta_mb"),
            "score_breakdown": score_breakdown,
        })
    try:
        dnr_summary = json.loads(reach["dnr_summary"]) if reach.get("dnr_summary") else None
    except (ValueError, TypeError):
        dnr_summary = None

    return {
        "reach_id": reach_id,
        "stream_name": reach.get("stream_name"),
        "segment_name": reach.get("segment_name"),
        "state": reach.get("state"),
        "trout_class": reach.get("trout_class"),
        "spring_influenced": bool(reach.get("spring_influenced")),
        "dnr_summary": dnr_summary,
        "computed_at": computed_at,
        "hours": hours_payload,
    }


@router.get("/scores-grid")
def get_scores_grid(hours: int = Query(168, ge=1, le=168)) -> dict:
    """Reach × hour score matrix for the map time scrubber."""
    return scores_grid(hours)


@router.get("/best-windows")
def get_best_windows(hours: int = Query(72, ge=1, le=168), limit: int = Query(10, ge=1, le=50)) -> List[dict]:
    rows = top_windows(hours, limit)
    # Collapse multi-hour clusters for the same reach into contiguous windows
    # so the list isn't 10 copies of the same river at different hours.
    seen: Dict[str, dict] = {}
    for r in rows:
        key = r["reach_id"]
        score = max(r.get("nymph_score") or 0.0, r.get("dry_score") or 0.0)
        existing = seen.get(key)
        if existing is None or score > existing["score"]:
            seen[key] = {
                "reach_id": key,
                "stream_name": r.get("stream_name"),
                "segment_name": r.get("segment_name"),
                "state": r.get("state"),
                "valid_at": r["valid_at"],
                "score": score,
                "nymph_score": r.get("nymph_score"),
                "dry_score": r.get("dry_score"),
                "explanation": r.get("explanation"),
            }
    ordered = sorted(seen.values(), key=lambda x: x["score"], reverse=True)[:limit]
    return ordered


@router.get("/calendar")
def get_hatch_calendar(reach_id: Optional[str] = None) -> dict:
    """Year-long emergence calendar.

    For each species we sample seasonal_activity at every day of the year,
    so the UI can render a continuous activity band per species. Reach-specific
    (if reach_id is given) shifts each species peak by the reach's anomaly —
    cheap when the anomaly is already cached on a recent prediction row.
    """
    from datetime import date, timedelta
    from src.models.seasonal import DRIFTLESS_PEAKS, seasonal_activity
    from src.models.forecast_builder import load_species

    shift = 0.0
    spring_influenced = False
    if reach_id:
        reach = get_reach(reach_id)
        if reach:
            spring_influenced = bool(reach.get("spring_influenced"))
            # Use the anomaly cached on the most recent prediction row.
            preds = get_reach_predictions(reach_id, 1)
            if preds and preds[0].get("hatch_shift_days") is not None:
                shift = float(preds[0]["hatch_shift_days"])

    species = load_species()
    species_by_id = {sp["species_id"]: sp for sp in species}
    today = datetime.now(timezone.utc).date()
    year = today.year
    days = []
    d = date(year, 1, 1)
    end = date(year, 12, 31)
    while d <= end:
        days.append(d)
        d += timedelta(days=1)

    out = []
    for sp_id, peak_info in DRIFTLESS_PEAKS.items():
        sp = species_by_id.get(sp_id, {})
        # Per-day activity; thinned to ~104 samples (every ~3.5d) for transport.
        sampled = []
        stride = max(1, len(days) // 104)
        for i in range(0, len(days), stride):
            day = days[i]
            act = seasonal_activity(sp_id, day, shift)
            sampled.append({"day": day.isoformat(), "doy": day.timetuple().tm_yday, "activity": round(act, 3)})
        peak_month, peak_day = peak_info["peak"]
        # When the species crosses 0.20 activity (rough "fishable presence" threshold)
        first_active = next((s for s in sampled if s["activity"] >= 0.20), None)
        last_active = next((s for s in reversed(sampled) if s["activity"] >= 0.20), None)
        out.append({
            "species_id": sp_id,
            "common_name": sp.get("common_name"),
            "scientific_name": sp.get("scientific_name"),
            "peak_month": peak_month,
            "peak_day": peak_day,
            "season_sd_days": peak_info["sd_days"],
            "type": "terrestrial" if (sp.get("dd_threshold_mean") or 0) == 0 and sp_id in {"hopper","ant","beetle","cricket"} else "aquatic",
            "first_present": first_active["day"] if first_active else None,
            "last_present": last_active["day"] if last_active else None,
            "fly_patterns": sp.get("fly_patterns") or [],
            "samples": sampled,
        })
    return {
        "year": year,
        "today_iso": today.isoformat(),
        "today_doy": today.timetuple().tm_yday,
        "reach_id": reach_id,
        "shift_days": shift,
        "spring_influenced": spring_influenced,
        "species": out,
    }


_EDUCATION_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "seed" / "education.json"
_EDUCATION_CACHE: Optional[dict] = None


def _load_education() -> dict:
    global _EDUCATION_CACHE
    if _EDUCATION_CACHE is None:
        try:
            _EDUCATION_CACHE = json.loads(_EDUCATION_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOG.warning("education content load failed: %s", exc)
            _EDUCATION_CACHE = {"topics": {}}
    return _EDUCATION_CACHE


@router.get("/education")
def list_education_topics() -> dict:
    data = _load_education()
    topics = data.get("topics", {})
    summary = [{"id": tid, "title": topic.get("title")} for tid, topic in topics.items()]
    return {"topics": summary, "meta": data.get("_meta", {})}


@router.get("/education/{topic_id}")
def get_education_topic(topic_id: str) -> dict:
    topics = _load_education().get("topics", {})
    topic = topics.get(topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail=f"Unknown topic: {topic_id}")
    return {"id": topic_id, **topic}


class CatchLogPayload(BaseModel):
    reach_id: str
    fished_at: str = Field(description="ISO8601 timestamp of when the trip happened")
    success: int = Field(ge=0, le=3, description="0 skunked, 1 a few, 2 solid, 3 great")
    method: Optional[str] = None
    species_caught: Optional[str] = None
    notes: Optional[str] = None
    fly_used: Optional[str] = None
    water_temp_f: Optional[float] = None


@router.post("/catch-log")
def post_catch_log(payload: CatchLogPayload) -> dict:
    if not get_reach(payload.reach_id):
        raise HTTPException(status_code=404, detail="Unknown reach")
    entry = payload.model_dump()
    entry["submitted_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    new_id = insert_catch_log(entry)
    return {"id": new_id, "ok": True}


@router.get("/catch-log")
def get_catch_log(reach_id: Optional[str] = None, limit: int = Query(50, ge=1, le=500)) -> List[dict]:
    return list_catch_logs(reach_id=reach_id, limit=limit)


@router.get("/reliability")
def get_reliability(bins: int = Query(5, ge=2, le=10)) -> dict:
    """Reliability diagram for the calibration dashboard.

    Returns calibration bins (predicted vs reported), Brier score, and per-reach
    counts. Until catch_log accumulates ~30+ entries, n will be small and the
    UI should show a 'gathering data' state.
    """
    return reliability_diagram(bins)


@router.get("/residuals")
def get_residuals() -> dict:
    """Per-reach learned offset from catch logs.

    Layer this on top of the physical score for display: shown = clamp(score + residual, 0, 1).
    The model itself is unchanged — this is a transparent calibration layer the
    angler can opt out of, not a replacement.
    """
    return {"reaches": reach_residuals()}


@router.post("/refresh")
def refresh_forecast() -> dict:
    # Manual kick for the forecast pipeline. APScheduler hookup pending
    # (plan step 12); this exists so a curl can rebuild after a code change.
    from src.models.forecast_builder import build_all
    counts = build_all()
    total = sum(counts.values())
    return {"reaches_updated": len(counts), "hours_written": total}
