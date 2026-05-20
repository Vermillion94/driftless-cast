import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

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
        "dnr_summary": "TEXT",
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
    conn.commit()
    conn.close()


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def upsert_reach(reach: Dict[str, Any]) -> None:
    reach = {"gauge_is_proxy": 0, "noaa_lid": None, **reach}
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO reach (reach_id, stream_name, segment_name, state, trout_class, geometry_geojson, centroid_lat, centroid_lon, length_km, mean_gradient, usgs_gauge_id, noaa_lid, gauge_is_proxy, nws_gridpoint, spring_influenced, notes)
        VALUES (:reach_id, :stream_name, :segment_name, :state, :trout_class, :geometry_geojson, :centroid_lat, :centroid_lon, :length_km, :mean_gradient, :usgs_gauge_id, :noaa_lid, :gauge_is_proxy, :nws_gridpoint, :spring_influenced, :notes)
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
            nws_gridpoint = excluded.nws_gridpoint,
            spring_influenced = excluded.spring_influenced,
            notes = excluded.notes;
        """,
        reach,
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


def list_reach_summaries() -> List[Dict[str, Any]]:
    # Joined with the most recent prediction per reach so the map can color
    # markers without a second round trip.
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT
            r.reach_id, r.stream_name, r.segment_name, r.state, r.trout_class,
            r.centroid_lat, r.centroid_lon, r.usgs_gauge_id, r.noaa_lid, r.gauge_is_proxy,
            r.geometry_geojson, r.spring_influenced,
            p.nymph_score, p.dry_score, p.valid_at AS prediction_valid_at
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
        nymph = d.pop("nymph_score", None)
        dry = d.pop("dry_score", None)
        if nymph is not None or dry is not None:
            d["combined_score"] = max(nymph or 0.0, dry or 0.0)
        else:
            d["combined_score"] = None
        summaries.append(d)
    return summaries


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
    scrubs forward without N round-trips. Score = max(nymph, dry) — same
    convention the UI uses for "combined".
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT reach_id, valid_at,
               MAX(nymph_score, dry_score) AS combined
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
        by_reach.setdefault(r["reach_id"], {})[v] = float(r["combined"]) if r["combined"] is not None else 0.0
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
        SELECT MAX(nymph_score, dry_score) AS combined, regime
        FROM prediction
        WHERE reach_id = ?
        ORDER BY ABS(strftime('%s', valid_at) - strftime('%s', ?)) ASC
        LIMIT 1
        """,
        (entry["reach_id"], entry["fished_at"]),
    ).fetchone()
    if snap:
        entry = {**entry, "predicted_score": snap["combined"], "predicted_regime": snap["regime"]}
    else:
        entry = {**entry, "predicted_score": None, "predicted_regime": None}
    cur = conn.execute(
        """
        INSERT INTO catch_log (
            reach_id, fished_at, success, method, species_caught, notes,
            fly_used, water_temp_f, submitted_at, predicted_score, predicted_regime
        ) VALUES (
            :reach_id, :fished_at, :success, :method, :species_caught, :notes,
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
            SELECT id, reach_id, fished_at, success, method, species_caught,
                   notes, fly_used, water_temp_f, submitted_at,
                   predicted_score, predicted_regime
            FROM catch_log
            WHERE reach_id = ?
            ORDER BY fished_at DESC
            LIMIT ?
            """,
            (reach_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, reach_id, fished_at, success, method, species_caught,
                   notes, fly_used, water_temp_f, submitted_at,
                   predicted_score, predicted_regime
            FROM catch_log
            ORDER BY fished_at DESC
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
        WITH ranked AS (
            SELECT p.*, MAX(p.nymph_score, p.dry_score) AS peak
            FROM prediction p
            WHERE p.valid_at >= datetime('now', '-1 hour')
              AND p.valid_at <= datetime('now', '+' || ? || ' hours')
        ),
        best AS (
            SELECT reach_id, MAX(peak) AS peak_score
            FROM ranked
            GROUP BY reach_id
        )
        SELECT p.reach_id, r.stream_name, r.segment_name, r.state,
               p.valid_at, p.nymph_score, p.dry_score, p.explanation
        FROM ranked p
        JOIN best b ON b.reach_id = p.reach_id AND b.peak_score = p.peak
        JOIN reach r ON r.reach_id = p.reach_id
        GROUP BY p.reach_id
        ORDER BY p.peak DESC
        LIMIT ?
        """,
        (hours, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
