import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.models.score_calibration import (
    confidence_score,
    headline_breakdown,
    headline_score,
    recommendation_rank_score,
)

# Default: project root next to source. In containers, set DC_DB_PATH to a
# location backed by a persistent volume (e.g. /app/data/driftless_cast.db on
# Fly.io). The path is resolved at import time so SIGHUP-style env changes
# don't take effect — restart the process.
_DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "driftless_cast.db"
DB_PATH = Path(os.environ.get("DC_DB_PATH", str(_DEFAULT_DB)))


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_database(db_path: Optional[Path] = None) -> None:
    conn = get_connection(db_path)
    schema_path = Path(__file__).resolve().parent / "schema.sql"
    with open(schema_path, "r", encoding="utf-8") as schema_file:
        conn.executescript(schema_file.read())
    # Plan says no migrations system, but SQLite's CREATE IF NOT EXISTS
    # doesn't add new columns to an existing table. Inline the handful of
    # additions so old DBs pick them up without a manual migration.
    _ensure_columns(conn, "reach", {
        "noaa_lid": "TEXT",
        "gauge_is_proxy": "INTEGER DEFAULT 0",
        "proxy_distance_km": "REAL",
        "dnr_summary": "TEXT",
        "region": "TEXT",
        "fishery": "TEXT",
        "model_caveat": "TEXT",
    })
    _ensure_columns(conn, "prediction", {
        "water_temp_f": "REAL",
        "water_temp_source": "TEXT",
        "anomaly_f": "REAL",
        "hatch_shift_days": "REAL",
        "fish_stress": "INTEGER DEFAULT 0",
        "air_temp_f": "REAL",
        "cloud_cover": "REAL",
        "wind_mph": "REAL",
        "flow_cfs": "REAL",
        "precip_prob": "REAL",
        "short_forecast": "TEXT",
        "regime": "TEXT",
        "pressure_delta_mb": "REAL",
        "score_breakdown": "TEXT",
    })
    _ensure_columns(conn, "catch_log", {
        "reporter_name": "TEXT",
        "session_window": "TEXT",
        "topwater_level": "INTEGER",
        "insect_activity": "TEXT",
        "worked": "TEXT",
        "didnt_work": "TEXT",
    })
    conn.commit()
    conn.close()


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def upsert_reach(reach: Dict[str, Any]) -> None:
    payload = {
        "gauge_is_proxy": 0, "noaa_lid": None, "proxy_distance_km": None,
        "region": None, "fishery": None, "model_caveat": None,
        **reach,
    }
    # `fishery` may be supplied as a dict/list from the seed JSON; store as JSON text.
    if isinstance(payload.get("fishery"), (dict, list)):
        payload["fishery"] = json.dumps(payload["fishery"])
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO reach (
            reach_id, stream_name, segment_name, state, trout_class,
            geometry_geojson, centroid_lat, centroid_lon, length_km,
            mean_gradient, usgs_gauge_id, noaa_lid, gauge_is_proxy,
            proxy_distance_km, nws_gridpoint, spring_influenced, notes,
            region, fishery, model_caveat
        )
        VALUES (
            :reach_id, :stream_name, :segment_name, :state, :trout_class,
            :geometry_geojson, :centroid_lat, :centroid_lon, :length_km,
            :mean_gradient, :usgs_gauge_id, :noaa_lid, :gauge_is_proxy,
            :proxy_distance_km, :nws_gridpoint, :spring_influenced, :notes,
            :region, :fishery, :model_caveat
        )
        ON CONFLICT(reach_id) DO UPDATE SET
            stream_name = excluded.stream_name,
            segment_name = excluded.segment_name,
            state = excluded.state,
            trout_class = excluded.trout_class,
            geometry_geojson = excluded.geometry_geojson,
            centroid_lat = excluded.centroid_lat,
            centroid_lon = excluded.centroid_lon,
            length_km = excluded.length_km,
            mean_gradient = excluded.mean_gradient,
            usgs_gauge_id = excluded.usgs_gauge_id,
            noaa_lid = excluded.noaa_lid,
            gauge_is_proxy = excluded.gauge_is_proxy,
            proxy_distance_km = excluded.proxy_distance_km,
            nws_gridpoint = excluded.nws_gridpoint,
            spring_influenced = excluded.spring_influenced,
            notes = excluded.notes,
            region = excluded.region,
            fishery = excluded.fishery,
            model_caveat = excluded.model_caveat;
        """,
        payload,
    )
    conn.commit()
    conn.close()


def load_reaches() -> List[Dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM reach").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_reach(reach_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    row = conn.execute("SELECT * FROM reach WHERE reach_id = ?", (reach_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# NHDPlus High-Res centerlines ship with ~14 decimal places of coordinate
# precision (sub-nanometer) — pointless on a map and the bulk of the /reaches
# payload. Rounding to 5 places is ~1m precision, invisible at any zoom, and
# cuts the raw geometry ~50% (and far more after gzip, since fewer unique
# digits compress better). Geometry never changes within a process, so the
# rounded string is cached per reach.
_GEOM_PRECISION = 5
_ROUNDED_GEOM_CACHE: Dict[str, str] = {}


def _round_geojson(geojson_str: str) -> str:
    cached = _ROUNDED_GEOM_CACHE.get(geojson_str)
    if cached is not None:
        return cached

    def _rnd(o: Any) -> Any:
        if isinstance(o, float):
            return round(o, _GEOM_PRECISION)
        if isinstance(o, list):
            return [_rnd(x) for x in o]
        if isinstance(o, dict):
            return {k: _rnd(v) for k, v in o.items()}
        return o

    try:
        rounded = json.dumps(_rnd(json.loads(geojson_str)), separators=(",", ":"))
    except (ValueError, TypeError):
        rounded = geojson_str
    _ROUNDED_GEOM_CACHE[geojson_str] = rounded
    return rounded


def list_reach_summaries() -> List[Dict[str, Any]]:
    # Joined with the most recent prediction per reach so the map can color
    # markers without a second round trip.
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT
            r.reach_id, r.stream_name, r.segment_name, r.state, r.trout_class,
            r.centroid_lat, r.centroid_lon, r.usgs_gauge_id, r.noaa_lid, r.gauge_is_proxy,
            r.proxy_distance_km,
            r.geometry_geojson, r.spring_influenced,
            r.region, r.fishery, r.model_caveat,
            p.nymph_score, p.dry_score, p.active_species, p.regime, p.score_breakdown,
            p.valid_at AS prediction_valid_at
        FROM reach r
        LEFT JOIN (
            SELECT p1.*
            FROM prediction p1
            JOIN (
                SELECT reach_id, MIN(valid_at) AS earliest
                FROM prediction
                WHERE valid_at >= datetime('now', '-1 hour')
                GROUP BY reach_id
            ) p2 ON p1.reach_id = p2.reach_id AND p1.valid_at = p2.earliest
        ) p ON p.reach_id = r.reach_id
        """
    ).fetchall()
    conn.close()
    summaries = []
    for row in rows:
        d = dict(row)
        if d.get("geometry_geojson"):
            d["geometry_geojson"] = _round_geojson(d["geometry_geojson"])
        nymph = d.pop("nymph_score", None)
        dry = d.pop("dry_score", None)
        active_species = d.pop("active_species", None)
        regime = d.pop("regime", None)
        score_breakdown = d.pop("score_breakdown", None)
        if nymph is not None or dry is not None:
            d["combined_score"] = headline_score(nymph, dry, active_species, regime, score_breakdown)
        else:
            d["combined_score"] = None
        d["fishery"] = _parse_fishery(d.get("fishery"))
        summaries.append(d)
    return summaries


def _parse_fishery(value: Any) -> Optional[Dict[str, Any]]:
    """Decode the stored `fishery` JSON blob into a dict for API responses."""
    if isinstance(value, str) and value:
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def get_reach_predictions(reach_id: str, hours: int) -> List[Dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT reach_id, valid_at, computed_at, nymph_score, dry_score,
               active_species, recommended_flies, explanation,
               water_temp_f, water_temp_source, anomaly_f, hatch_shift_days,
               fish_stress, air_temp_f, cloud_cover, wind_mph, flow_cfs,
               precip_prob, short_forecast, regime, pressure_delta_mb,
               score_breakdown
        FROM prediction
        WHERE reach_id = ? AND valid_at >= datetime('now', '-1 hour')
        ORDER BY valid_at ASC
        LIMIT ?
        """,
        (reach_id, hours),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def scores_grid(hours: int) -> Dict[str, Any]:
    """All reaches × all hours score matrix for the time scrubber.

    Returns a hour-aligned grid so the map can re-color reaches as the user
    scrubs forward without N round-trips.
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT reach_id, valid_at, nymph_score, dry_score, active_species, regime, score_breakdown
        FROM prediction
        WHERE valid_at >= datetime('now', '-1 hour')
          AND valid_at <= datetime('now', '+' || ? || ' hours')
        ORDER BY valid_at ASC
        """,
        (hours,),
    ).fetchall()
    conn.close()
    hour_set: List[str] = []
    seen: Dict[str, int] = {}
    by_reach: Dict[str, Dict[str, float]] = {}
    for r in rows:
        v = r["valid_at"]
        if v not in seen:
            seen[v] = len(hour_set)
            hour_set.append(v)
        by_reach.setdefault(r["reach_id"], {})[v] = headline_score(
            r["nymph_score"], r["dry_score"], r["active_species"], r["regime"], r["score_breakdown"]
        )
    scores: Dict[str, List[Optional[float]]] = {}
    for reach_id, m in by_reach.items():
        scores[reach_id] = [m.get(h) for h in hour_set]
    return {"hours": hour_set, "scores": scores}


def forecast_status(stale_after_minutes: int = 90) -> Dict[str, Any]:
    """Freshness summary for the currently cached forecast rows."""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS row_count,
            COUNT(DISTINCT reach_id) AS reach_count,
            MIN(computed_at) AS oldest_computed_at,
            MAX(computed_at) AS newest_computed_at,
            MIN(valid_at) AS first_valid_at,
            MAX(valid_at) AS last_valid_at
        FROM prediction
        """
    ).fetchone()
    conn.close()
    payload = dict(row) if row else {}
    newest = payload.get("newest_computed_at")
    stale_minutes: Optional[int] = None
    is_stale = True
    if newest:
        from datetime import datetime, timezone

        try:
            dt = datetime.fromisoformat(str(newest).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            stale_minutes = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
            is_stale = stale_minutes > stale_after_minutes
        except ValueError:
            stale_minutes = None
            is_stale = True
    payload["stale_minutes"] = stale_minutes
    payload["stale_after_minutes"] = stale_after_minutes
    payload["is_stale"] = is_stale
    return payload


def insert_catch_log(entry: Dict[str, Any]) -> int:
    """Save a user-reported trip. Returns the new row id.

    The "predicted_score" / "predicted_regime" snapshot is taken from the
    closest prediction row at submit time so calibration can later compare
    what the model said vs. what the angler experienced.
    """
    conn = get_connection()
    # Snapshot the model's call for this reach at the time of fishing. Falls
    # back to the latest prediction if no exact-hour row matches.
    snap = conn.execute(
        """
        SELECT nymph_score, dry_score, active_species, regime, score_breakdown
        FROM prediction
        WHERE reach_id = ?
        ORDER BY ABS(strftime('%s', valid_at) - strftime('%s', ?)) ASC
        LIMIT 1
        """,
        (entry["reach_id"], entry["fished_at"]),
    ).fetchone()
    if snap:
        entry = {
            **entry,
            "predicted_score": headline_score(
                snap["nymph_score"], snap["dry_score"], snap["active_species"],
                snap["regime"], snap["score_breakdown"]
            ),
            "predicted_regime": snap["regime"],
        }
    else:
        entry = {**entry, "predicted_score": None, "predicted_regime": None}
    cur = conn.execute(
        """
        INSERT INTO catch_log (
            reach_id, fished_at, success, reporter_name, method, session_window,
            topwater_level, insect_activity, species_caught, worked, didnt_work, notes,
            fly_used, water_temp_f, submitted_at, predicted_score, predicted_regime
        ) VALUES (
            :reach_id, :fished_at, :success, :reporter_name, :method, :session_window,
            :topwater_level, :insect_activity, :species_caught, :worked, :didnt_work, :notes,
            :fly_used, :water_temp_f, :submitted_at, :predicted_score, :predicted_regime
        )
        """,
        entry,
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def list_catch_logs(reach_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_connection()
    if reach_id:
        rows = conn.execute(
            """
            SELECT c.id, c.reach_id, c.fished_at, c.success, c.reporter_name, c.method,
                   c.session_window, c.topwater_level, c.insect_activity, c.species_caught,
                   c.worked, c.didnt_work, c.notes, c.fly_used, c.water_temp_f, c.submitted_at,
                   predicted_score, predicted_regime
                   , r.stream_name, r.segment_name
            FROM catch_log c
            LEFT JOIN reach r ON r.reach_id = c.reach_id
            WHERE c.reach_id = ?
            ORDER BY c.fished_at DESC
            LIMIT ?
            """,
            (reach_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT c.id, c.reach_id, c.fished_at, c.success, c.reporter_name, c.method,
                   c.session_window, c.topwater_level, c.insect_activity, c.species_caught,
                   c.worked, c.didnt_work, c.notes, c.fly_used, c.water_temp_f, c.submitted_at,
                   predicted_score, predicted_regime
                   , r.stream_name, r.segment_name
            FROM catch_log c
            LEFT JOIN reach r ON r.reach_id = c.reach_id
            ORDER BY c.fished_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def reliability_diagram(bin_count: int = 5) -> Dict[str, Any]:
    """Calibration diagram from accumulated catch-log entries.

    For each bin of predicted score, compute mean reported success. Perfect
    calibration sits on y = x. Coarse 5-bin default — good enough for the
    sample sizes we're realistically going to have.

    Returns:
        {
          "n": <int>,
          "bins": [{"lo": 0.0, "hi": 0.2, "n": 3, "mean_predicted": 0.13, "mean_reported": 0.33}, ...],
          "by_reach_n": {reach_id: int},
          "brier_score": <float | null>,
        }
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT reach_id, success, predicted_score
        FROM catch_log
        WHERE predicted_score IS NOT NULL AND success IS NOT NULL
        """
    ).fetchall()
    conn.close()
    if not rows:
        return {"n": 0, "bins": [], "by_reach_n": {}, "brier_score": None}

    edges = [i / bin_count for i in range(bin_count + 1)]  # [0, 0.2, 0.4, 0.6, 0.8, 1.0]
    bins: List[Dict[str, Any]] = []
    by_reach: Dict[str, int] = {}
    sse = 0.0  # Brier-style sum of squared errors
    n = 0
    for i in range(bin_count):
        lo, hi = edges[i], edges[i + 1]
        bucket = []
        for r in rows:
            p = float(r["predicted_score"])
            if (lo <= p < hi) or (i == bin_count - 1 and p == hi):
                bucket.append(r)
        if not bucket:
            bins.append({"lo": lo, "hi": hi, "n": 0, "mean_predicted": None, "mean_reported": None})
            continue
        mean_pred = sum(float(b["predicted_score"]) for b in bucket) / len(bucket)
        # success is 0..3; normalize to 0..1 to match predicted_score space.
        mean_rep = sum(float(b["success"]) / 3.0 for b in bucket) / len(bucket)
        bins.append({
            "lo": lo, "hi": hi, "n": len(bucket),
            "mean_predicted": round(mean_pred, 3),
            "mean_reported": round(mean_rep, 3),
        })
    for r in rows:
        rid = r["reach_id"]
        by_reach[rid] = by_reach.get(rid, 0) + 1
        p = float(r["predicted_score"])
        rep = float(r["success"]) / 3.0
        sse += (p - rep) ** 2
        n += 1
    brier = sse / n if n else None
    return {
        "n": n,
        "bins": bins,
        "by_reach_n": by_reach,
        "brier_score": round(brier, 4) if brier is not None else None,
    }


def reach_residuals() -> Dict[str, Dict[str, float]]:
    """Per-reach mean residual from catch_log: (reported - predicted).

    Buckets by reach_id; n must be >= 3 for a residual to be returned. The
    returned offset is in score-space (0..1) and is intended to layer on top
    of the physical score, not replace it.
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT reach_id,
               COUNT(*) AS n,
               AVG((success / 3.0) - predicted_score) AS residual
        FROM catch_log
        WHERE predicted_score IS NOT NULL
          AND success IS NOT NULL
        GROUP BY reach_id
        HAVING COUNT(*) >= 3
        """
    ).fetchall()
    conn.close()
    out: Dict[str, Dict[str, float]] = {}
    for r in rows:
        # Clamp the residual to ±0.20 — we let catch logs nudge the displayed
        # score, not override it. Mistakes (one bad day biasing future calls)
        # decay as more reports come in.
        residual = max(-0.20, min(0.20, float(r["residual"]) if r["residual"] is not None else 0.0))
        out[r["reach_id"]] = {
            "n": int(r["n"]),
            "residual": residual,
        }
    return out


def top_windows(hours: int, limit: int = 10) -> List[Dict[str, Any]]:
    # Pick each reach's single best hour inside the window, then rank across
    # reaches. Naive ORDER BY over all rows biases toward reaches that have
    # many high hours — we want one row per reach.
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT p.reach_id, r.stream_name, r.segment_name, r.state,
               r.gauge_is_proxy, r.proxy_distance_km,
               p.valid_at, p.computed_at, p.nymph_score, p.dry_score,
               p.active_species, p.regime, p.score_breakdown, p.explanation,
               p.water_temp_source
        FROM prediction p
        JOIN reach r ON r.reach_id = p.reach_id
        WHERE p.valid_at >= datetime('now', '-1 hour')
          AND p.valid_at <= datetime('now', '+' || ? || ' hours')
        """,
        (hours,),
    ).fetchall()
    conn.close()
    best_by_reach: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        d = dict(row)
        d["combined_score"] = headline_score(
            d.get("nymph_score"), d.get("dry_score"), d.get("active_species"),
            d.get("regime"), d.get("score_breakdown")
        )
        conf = confidence_score(
            d.get("valid_at"), d.get("computed_at"), d.get("water_temp_source"),
            d.get("gauge_is_proxy"), d.get("score_breakdown"), d.get("proxy_distance_km")
        )
        d["confidence_score"] = conf["score"]
        d["rank_score"] = recommendation_rank_score(d["combined_score"], conf["score"])
        existing = best_by_reach.get(d["reach_id"])
        if existing is None or d["rank_score"] > existing["rank_score"]:
            best_by_reach[d["reach_id"]] = d
    ranked = sorted(best_by_reach.values(), key=lambda r: r["rank_score"], reverse=True)
    return ranked[:limit]


def hatch_windows(hours: int, limit: int = 6, min_surface: float = 0.25) -> List[Dict[str, Any]]:
    """Top surface/hatch windows, ranked separately from all-around fishing score.

    A hatch watch is not necessarily a high-probability catching window. It is
    the best evidence of surface opportunity: modeled hatch readiness, timing,
    and weather. Keeping this separate avoids burying dry-fly intel behind
    high but nymph-only scores.
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT p.reach_id, r.stream_name, r.segment_name, r.state,
               r.gauge_is_proxy, r.proxy_distance_km,
               p.valid_at, p.computed_at, p.nymph_score, p.dry_score,
               p.active_species, p.regime, p.score_breakdown, p.explanation,
               p.water_temp_source, p.water_temp_f, p.fish_stress
        FROM prediction p
        JOIN reach r ON r.reach_id = p.reach_id
        WHERE p.valid_at >= datetime('now', '-1 hour')
          AND p.valid_at <= datetime('now', '+' || ? || ' hours')
          AND COALESCE(p.fish_stress, 0) = 0
        """,
        (hours,),
    ).fetchall()
    conn.close()

    best_by_reach: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        d = dict(row)
        score_model = headline_breakdown(
            d.get("nymph_score"), d.get("dry_score"), d.get("active_species"),
            d.get("regime"), d.get("score_breakdown")
        )
        surface = float(score_model.get("surface_signal") or 0.0)
        if surface < min_surface:
            continue
        conf = confidence_score(
            d.get("valid_at"), d.get("computed_at"), d.get("water_temp_source"),
            d.get("gauge_is_proxy"), d.get("score_breakdown"), d.get("proxy_distance_km")
        )
        d["combined_score"] = score_model["score"]
        d["surface_signal"] = surface
        d["confidence_score"] = conf["score"]
        # Surface signal leads. Overall score is a secondary nudge so truly bad
        # fishing conditions do not outrank similarly strong, more fishable hatches.
        d["surface_rank_score"] = surface * (0.80 + 0.20 * conf["score"]) * (
            0.75 + 0.25 * d["combined_score"]
        )
        existing = best_by_reach.get(d["reach_id"])
        if existing is None or d["surface_rank_score"] > existing["surface_rank_score"]:
            best_by_reach[d["reach_id"]] = d

    ranked = sorted(best_by_reach.values(), key=lambda r: r["surface_rank_score"], reverse=True)
    return ranked[:limit]
