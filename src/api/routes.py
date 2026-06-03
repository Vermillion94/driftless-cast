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
    forecast_status,
    insert_catch_log,
    hatch_windows,
    list_catch_logs,
    list_reach_summaries,
    reach_residuals,
    reliability_diagram,
    scores_grid,
    top_windows,
)
from src.ingest import fetch_latest_iv, fetch_latest_nwps
from src.models.score_calibration import (
    confidence_score,
    headline_breakdown,
    headline_score,
    recommendation_rank_score,
)

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
        "proxy_distance_km": reach.get("proxy_distance_km"),
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
        score_model = headline_breakdown(
            r.get("nymph_score"), r.get("dry_score"), active, regime, score_breakdown
        )
        confidence_model = confidence_score(
            r.get("valid_at"), r.get("computed_at"), r.get("water_temp_source"),
            reach.get("gauge_is_proxy"), score_breakdown, reach.get("proxy_distance_km")
        )
        hours_payload.append({
            "valid_at": r["valid_at"],
            "nymph_score": r.get("nymph_score"),
            "dry_score": r.get("dry_score"),
            "combined_score": score_model["score"],
            "score_model": score_model,
            "confidence_model": confidence_model,
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

    # Staleness — `computed_at` is when the forecast was last built. The
    # rebuild loop runs hourly; anything past 90min is genuinely stale (NWS
    # has likely updated its hourly forecast in the meantime).
    stale_minutes: Optional[int] = None
    is_stale = False
    if computed_at:
        try:
            built = datetime.fromisoformat(computed_at.replace("Z", "+00:00"))
            if built.tzinfo is None:
                built = built.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - built
            stale_minutes = int(delta.total_seconds() // 60)
            is_stale = stale_minutes > 90
        except ValueError:
            pass

    return {
        "reach_id": reach_id,
        "stream_name": reach.get("stream_name"),
        "segment_name": reach.get("segment_name"),
        "state": reach.get("state"),
        "trout_class": reach.get("trout_class"),
        "spring_influenced": bool(reach.get("spring_influenced")),
        "gauge_is_proxy": bool(reach.get("gauge_is_proxy")),
        "proxy_distance_km": reach.get("proxy_distance_km"),
        "dnr_summary": dnr_summary,
        "computed_at": computed_at,
        "stale_minutes": stale_minutes,
        "is_stale": is_stale,
        "hours": hours_payload,
    }


@router.get("/scores-grid")
def get_scores_grid(hours: int = Query(168, ge=1, le=168)) -> dict:
    """Reach × hour score matrix for the map time scrubber."""
    return scores_grid(hours)


@router.get("/status")
def get_status() -> dict:
    """Forecast freshness and coverage for UI trust checks."""
    return forecast_status()


@router.get("/best-windows")
def get_best_windows(hours: int = Query(72, ge=1, le=168), limit: int = Query(10, ge=1, le=50)) -> List[dict]:
    rows = top_windows(hours, max(limit * 3, limit))
    # Collapse multi-hour clusters for the same reach into contiguous windows
    # so the list isn't 10 copies of the same river at different hours.
    seen: Dict[str, dict] = {}
    for r in rows:
        key = r["reach_id"]
        active = _loads_json(r.get("active_species"), [])
        regime = _loads_json(r.get("regime"), None)
        score_breakdown = _loads_json(r.get("score_breakdown"), None)
        score = r.get("combined_score")
        if score is None:
            score = headline_score(
                r.get("nymph_score"), r.get("dry_score"),
                active, regime, score_breakdown
            )
        confidence = r.get("confidence_score")
        if confidence is None:
            confidence = confidence_score(
                r.get("valid_at"), r.get("computed_at"), r.get("water_temp_source"),
                r.get("gauge_is_proxy"), score_breakdown, r.get("proxy_distance_km")
            )["score"]
        rank_score = r.get("rank_score")
        if rank_score is None:
            rank_score = recommendation_rank_score(score, confidence)
        existing = seen.get(key)
        if existing is None or rank_score > existing["rank_score"]:
            score_model = headline_breakdown(
                r.get("nymph_score"), r.get("dry_score"), active, regime, score_breakdown
            )
            seen[key] = {
                "reach_id": key,
                "stream_name": r.get("stream_name"),
                "segment_name": r.get("segment_name"),
                "state": r.get("state"),
                "valid_at": r["valid_at"],
                "score": score,
                "rank_score": rank_score,
                "confidence_score": confidence,
                "gauge_is_proxy": bool(r.get("gauge_is_proxy")),
                "proxy_distance_km": r.get("proxy_distance_km"),
                "nymph_score": r.get("nymph_score"),
                "dry_score": r.get("dry_score"),
                "aggression_score": score_model.get("aggression"),
                "regime": regime,
                "reason": _best_window_reason(r, score_model, score_breakdown, regime),
                "explanation": r.get("explanation"),
            }
    ordered = sorted(seen.values(), key=lambda x: x["rank_score"], reverse=True)
    return _diversify_windows_by_time(ordered, limit)


@router.get("/hatch-windows")
def get_hatch_windows(
    hours: int = Query(168, ge=1, le=168),
    limit: int = Query(6, ge=1, le=20),
    min_surface: float = Query(0.25, ge=0.0, le=1.0),
) -> List[dict]:
    rows = hatch_windows(hours, limit, min_surface)
    out: List[dict] = []
    for r in rows:
        active = _loads_json(r.get("active_species"), [])
        regime = _loads_json(r.get("regime"), None)
        score_breakdown = _loads_json(r.get("score_breakdown"), None)
        score_model = headline_breakdown(
            r.get("nymph_score"), r.get("dry_score"), active, regime, score_breakdown
        )
        top_species = _top_active_species(active)
        reason = ["surface signal"]
        if top_species:
            reason.append(str(top_species.get("common_name") or top_species.get("id") or "active hatch"))
        if isinstance(score_breakdown, dict) and (score_breakdown.get("sun_factor") or 1.0) < 0.90:
            reason.append("bright-sun drag")
        out.append({
            "reach_id": r["reach_id"],
            "stream_name": r.get("stream_name"),
            "segment_name": r.get("segment_name"),
            "state": r.get("state"),
            "valid_at": r["valid_at"],
            "score": r.get("combined_score"),
            "surface_signal": r.get("surface_signal"),
            "surface_rank_score": r.get("surface_rank_score"),
            "confidence_score": r.get("confidence_score"),
            "water_temp_f": r.get("water_temp_f"),
            "fish_stress": bool(r.get("fish_stress")),
            "top_species": top_species,
            "reason": reason[:3],
            "regime": regime,
            "explanation": r.get("explanation"),
            "score_model": score_model,
        })
    return _diversify_windows_by_time(out, limit, max_per_hour=3)


def _loads_json(value, default):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value if value is not None else default


def _best_window_reason(row: dict, score_model: dict, score_breakdown, regime) -> List[str]:
    sb = score_breakdown if isinstance(score_breakdown, dict) else {}
    regime_row = regime if isinstance(regime, dict) else {}
    reasons: List[str] = []
    if regime_row.get("code") and regime_row.get("code") != "NORMAL":
        reasons.append(str(regime_row.get("label") or regime_row.get("code")).lower())
    surface = score_model.get("surface_signal") or 0.0
    if surface >= 0.15:
        reasons.append("surface signal")
    elif (row.get("nymph_score") or 0.0) >= 0.45:
        reasons.append("nymphing play")
    thermal_label = str(sb.get("thermal_profile") or "")
    if thermal_label.startswith("spring-creek"):
        reasons.append("spring-buffered water")
    elif (sb.get("temperature") or 0.0) >= 0.95:
        reasons.append("ideal water")
    if (sb.get("diel_activity") or 0.0) >= 0.96:
        reasons.append("low light")
    if (sb.get("pressure_factor") or 1.0) >= 1.02:
        reasons.append("falling barometer")
    if (sb.get("flow_trend") or 0.0) >= 0.95:
        reasons.append("falling/stable flow")
    elif (sb.get("flow_percentile") or 0.0) >= 0.90:
        reasons.append("good flow band")
    runoff_level = str(sb.get("runoff_risk_level") or "")
    if runoff_level in {"hurt", "high"}:
        reasons.append("rain stain")
    elif runoff_level == "watch":
        reasons.append("rain watch")
    if (sb.get("sun_factor") or 1.0) < 0.90:
        reasons.append("bright-sun drag")
    if (row.get("dry_score") or 0.0) >= 0.30:
        reasons.append("dry-fly window")

    deduped: List[str] = []
    for reason in reasons:
        if reason not in deduped:
            deduped.append(reason)
    return deduped[:3]


def _top_active_species(active_species) -> Optional[dict]:
    if not isinstance(active_species, list):
        return None
    rows = [row for row in active_species if isinstance(row, dict)]
    if not rows:
        return None
    return max(rows, key=lambda row: row.get("probability") or 0.0)


def _window_hour_key(row: dict) -> str:
    valid_at = row.get("valid_at")
    if not valid_at:
        return ""
    try:
        dt = datetime.fromisoformat(str(valid_at).replace("Z", "+00:00"))
    except ValueError:
        return str(valid_at)[:13]
    return dt.strftime("%Y-%m-%dT%H")


def _diversify_windows_by_time(rows: List[dict], limit: int, max_per_hour: int = 4) -> List[dict]:
    """Keep the recommendation list from becoming one regional timestamp.

    When the whole Driftless region lines up, many reaches can legitimately
    peak at the same hour. The first few are useful; the tenth copy is not.
    Preserve rank order, cap exact-hour duplicates, then backfill if there
    are not enough distinct time slots.
    """
    picked: List[dict] = []
    skipped: List[dict] = []
    counts: Dict[str, int] = {}
    for row in rows:
        key = _window_hour_key(row)
        if counts.get(key, 0) < max_per_hour:
            picked.append(row)
            counts[key] = counts.get(key, 0) + 1
        else:
            skipped.append(row)
        if len(picked) >= limit:
            return picked
    for row in skipped:
        picked.append(row)
        if len(picked) >= limit:
            break
    return picked


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
    reporter_name: Optional[str] = None
    method: Optional[str] = None
    session_window: Optional[str] = None
    topwater_level: Optional[int] = Field(default=None, ge=0, le=3)
    insect_activity: Optional[str] = None
    species_caught: Optional[str] = None
    worked: Optional[str] = None
    didnt_work: Optional[str] = None
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
    # Manual/scheduled kick for the forecast pipeline. Keep this non-blocking
    # so waking a sleeping Fly machine does not turn into a long request-path
    # rebuild that the proxy reports as 503.
    from src.jobs.forecast_refresh import refresh_state, trigger_forecast_refresh
    started = trigger_forecast_refresh()
    state = refresh_state()
    return {"started": started, **state}
