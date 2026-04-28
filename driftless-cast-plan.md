# Driftless Cast — Project Plan

A hyperlocal fly-fishing forecast tool for the Driftless Area (SE MN, W WI, NE IA). Predicts per-reach, per-hour fishing quality, hatch activity, and recommended flies from public data only.

## Guiding principles
- **No scraping.** Every data source is an official public API with documented endpoints.
- **Reach resolution, not statewide.** Forecast is per-stream-segment, not per-state.
- **Explainable scoring.** No black-box ML in v1; every score is a function of observable inputs and can be decomposed.
- **MVP ships in a week of evenings.** Polish comes later.
- **Runs on a $5 VPS or a Pi.** SQLite, Python, static frontend.

---

## Stack (pre-decided — do not relitigate)

- **Language:** Python 3.12, managed with `uv`
- **DB:** SQLite (file-based, zero-ops). Migrate to Postgres only if needed.
- **Backend:** FastAPI + Uvicorn
- **Scheduling:** APScheduler in-process (simpler than cron for dev; easy to switch later)
- **Data:** Polars for ingestion/transform, raw SQL via `sqlite3` for storage
- **Frontend:** Vanilla HTML + Leaflet + lightweight JS (no build step). Chart.js for time series.
- **Deployment:** Docker Compose; one container for API+scheduler, nginx serving static frontend
- **Testing:** pytest, with recorded API fixtures (vcr.py) so tests don't hit live APIs

---

## Repository layout

```
driftless-cast/
├── README.md
├── pyproject.toml
├── docker-compose.yml
├── Dockerfile
├── data/
│   ├── seed/
│   │   ├── reaches.json            # hand-curated reach definitions (see below)
│   │   ├── species.json            # target species + DD params + fly patterns
│   │   └── gauges.json             # USGS gauge → reach mappings
│   └── calibration/
│       └── inat_driftless.parquet  # historical iNat dump for DD fitting
├── src/
│   ├── ingest/
│   │   ├── usgs.py                 # continuous values + daily values
│   │   ├── nws.py                  # weather forecast
│   │   ├── inat.py                 # iNaturalist observations
│   │   └── nhdplus.py              # stream geometry (one-off)
│   ├── models/
│   │   ├── degree_days.py          # DD accumulator
│   │   ├── hatch_predictor.py      # per-species emergence probability
│   │   ├── nymph_score.py          # nymphing quality score
│   │   ├── dry_score.py            # dry fly quality score
│   │   └── fly_recommender.py      # maps active hatches → fly patterns
│   ├── db/
│   │   ├── schema.sql
│   │   ├── migrations/
│   │   └── queries.py
│   ├── api/
│   │   ├── main.py
│   │   └── routes.py               # /reaches, /forecast/{reach_id}, /conditions/{reach_id}
│   ├── jobs/
│   │   ├── hourly_ingest.py        # USGS + NWS refresh
│   │   └── nightly_rebuild.py      # recompute DD accumulations, rebuild forecasts
│   └── scripts/
│       ├── bootstrap_reaches.py    # builds reaches.json from NHDPlus + DNR data
│       ├── fit_dd_thresholds.py    # calibrates species thresholds from iNat history
│       └── backfill_temps.py       # air→water temp regression for ungauged reaches
├── web/
│   ├── index.html
│   ├── map.js
│   ├── forecast.js
│   └── styles.css
└── tests/
    ├── fixtures/
    └── test_*.py
```

---

## Data sources

### 1. USGS Water Data API (real-time flow, stage, water temp)

Base: `https://api.waterdata.usgs.gov/ogcapi/v0/`

Endpoints we use:
- `GET /collections/continuous/items` — live observations, filter by `monitoring_location_id` and `parameter_code`
- `GET /collections/daily/items` — historical daily values for percentile/normal calculations
- `GET /collections/monitoring-locations/items` — station metadata

Parameter codes we care about:
- `00060` — discharge (cfs)
- `00065` — gage height (ft)
- `00010` — water temperature (°C)

Target Driftless gauges (seed with these 8; expand later):
- `05355325` — Rush River near Maiden Rock, WI
- `05342000` — Kinnickinnic River near River Falls, WI
- `05356000` — Willow River near Willow River, WI
- `05407000` — Kickapoo River at Ontario, WI
- `05388250` — Upper Iowa River near Dorchester, IA
- `05376000` — South Fork Root River near Houston, MN
- `05377500` — Whitewater River near Beaver, MN
- `05385500` — Root River near Lanesboro, MN

Refresh cadence: hourly for continuous values.

### 2. NWS API (weather forecast)

Base: `https://api.weather.gov/`. No auth, no key. Rate-limit yourself politely (<60 req/min).

Flow:
1. For a reach centroid `(lat, lon)`: `GET /points/{lat},{lon}` → returns gridpoint URLs
2. Cache gridpoint ID per reach (stable, only needs refresh every ~1 yr)
3. `GET /gridpoints/{office}/{x},{y}/forecast/hourly` → 156-hour hourly forecast
4. Parse: temperature, dewpoint, wind speed/direction, cloud cover (from `shortForecast` text), precip probability, pressure is NOT in this endpoint — pull from `/gridpoints/{office}/{x},{y}` raw endpoint if needed.

Set User-Agent header to `driftless-cast/0.1 (contact: your-email)` — required by NWS.

Refresh cadence: every 3 hours.

### 3. iNaturalist API (species phenology ground truth)

Base: `https://api.inaturalist.org/v1/`

Primary endpoint: `GET /observations` with query params:
- `taxon_id` — specific species or order
- `quality_grade=research`
- `d1`, `d2` — date range (YYYY-MM-DD)
- `nelat`, `nelng`, `swlat`, `swlng` — Driftless bbox: roughly SW(43.0, -93.5) / NE(45.5, -90.0)
- `per_page=200`, paginate

Key taxon IDs (verify via `/taxa?q=`):
- Ephemeroptera (order): `47158`
- Ephemerella subvaria (Hendrickson): search
- Baetidae (BWOs): `51878`
- Brachycentrus (grannom caddis): search
- Hexagenia (Hex): search

Usage pattern: **one-off historical dump** for calibration, not live polling. Pull 10 years of research-grade observations, store in `data/calibration/inat_driftless.parquet`. Rerun annually.

### 4. NHDPlus High Resolution (stream geometry)

One-off ingestion. Use USGS NHDPlus HR feature services via REST:
`https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer`

Query by HUC-8 for the 4-5 HUCs covering the Driftless. Get flowline geometry + attributes (StreamOrder, GNIS_Name, LengthKM, MinElevSmo, MaxElevSmo → gradient).

Store geometry as GeoJSON in `reaches.json`. Do not live-query.

### 5. State DNR classifications (trout stream quality)

- WI DNR trout stream classifications: downloadable shapefile from WI DNR Open Data portal
- MN DNR Designated Trout Streams: similar, via MN Geospatial Commons
- IA DNR: Iowa GIS Library

Intersect with NHDPlus reaches to tag each reach with `trout_class` (I/II/III).

One-off, stored in reach seed data.

### 6. PRISM climate normals (for air→water regression on ungauged reaches)

`https://prism.oregonstate.edu/explorer/` — download 30-year monthly normals as raster. Sample at reach centroids. One-off.

---

## Core data model

```sql
CREATE TABLE reach (
    reach_id         TEXT PRIMARY KEY,         -- e.g. 'rush-el-paso-maiden-rock'
    stream_name      TEXT NOT NULL,            -- 'Rush River'
    segment_name     TEXT,                     -- 'El Paso to Maiden Rock'
    state            TEXT NOT NULL,
    trout_class      TEXT,                     -- 'I', 'II', 'III', NULL
    geometry_geojson TEXT NOT NULL,            -- LineString
    centroid_lat     REAL NOT NULL,
    centroid_lon     REAL NOT NULL,
    length_km        REAL,
    mean_gradient    REAL,                     -- m/km
    usgs_gauge_id    TEXT,                     -- nearest/representative gauge
    nws_gridpoint    TEXT,                     -- cached
    spring_influenced INTEGER DEFAULT 0,       -- bool, for thermal regime
    notes            TEXT
);

CREATE TABLE gauge (
    gauge_id    TEXT PRIMARY KEY,
    name        TEXT,
    lat         REAL,
    lon         REAL,
    params      TEXT                           -- JSON array of parameter codes
);

CREATE TABLE observation (
    gauge_id       TEXT NOT NULL,
    observed_at    TEXT NOT NULL,              -- ISO8601 UTC
    parameter_code TEXT NOT NULL,
    value          REAL,
    PRIMARY KEY (gauge_id, observed_at, parameter_code)
);

CREATE TABLE weather_forecast (
    reach_id      TEXT NOT NULL,
    forecast_at   TEXT NOT NULL,               -- when forecast was issued
    valid_at      TEXT NOT NULL,               -- timestamp being forecasted
    air_temp_f    REAL,
    dewpoint_f    REAL,
    wind_mph      REAL,
    wind_dir      TEXT,
    cloud_cover   REAL,                        -- 0-1
    precip_prob   REAL,                        -- 0-1
    PRIMARY KEY (reach_id, valid_at)
);

CREATE TABLE species (
    species_id         TEXT PRIMARY KEY,       -- 'hendrickson', 'bwo-spring', etc
    common_name        TEXT NOT NULL,
    scientific_name    TEXT,
    base_temp_c        REAL NOT NULL,          -- DD base
    dd_threshold_mean  REAL NOT NULL,          -- DD-C at peak emergence
    dd_threshold_sd    REAL NOT NULL,          -- spread of emergence
    emergence_hr_start INTEGER,                -- local hour, window start
    emergence_hr_end   INTEGER,
    weather_prefs      TEXT,                   -- JSON: {"clouds": "high", "wind": "low"}
    fly_patterns       TEXT                    -- JSON array of recommended flies
);

CREATE TABLE dd_accumulation (
    reach_id      TEXT NOT NULL,
    date          TEXT NOT NULL,
    base_temp_c   REAL NOT NULL,
    accumulated   REAL NOT NULL,               -- degree-days since Jan 1
    PRIMARY KEY (reach_id, date, base_temp_c)
);

CREATE TABLE prediction (
    reach_id             TEXT NOT NULL,
    valid_at             TEXT NOT NULL,
    computed_at          TEXT NOT NULL,
    nymph_score          REAL,                 -- 0-1
    dry_score            REAL,                 -- 0-1
    active_species       TEXT,                 -- JSON array of species_ids
    recommended_flies    TEXT,                 -- JSON array
    explanation          TEXT,                 -- human-readable string
    PRIMARY KEY (reach_id, valid_at)
);
```

---

## Seed data: `reaches.json`

Start with 8-12 hand-curated reaches. Example schema:

```json
{
  "reach_id": "rush-el-paso-maiden-rock",
  "stream_name": "Rush River",
  "segment_name": "El Paso to Maiden Rock",
  "state": "WI",
  "trout_class": "I",
  "usgs_gauge_id": "05355325",
  "centroid_lat": 44.625,
  "centroid_lon": -92.20,
  "length_km": 12.4,
  "spring_influenced": 1,
  "notes": "Class I wild brown trout; Kiap-TU-Wish restoration reaches"
}
```

Claude Code should populate the rest from NHDPlus via `scripts/bootstrap_reaches.py`.

## Seed data: `species.json`

Target these 6 for MVP (expand later):

```json
[
  {
    "species_id": "hendrickson",
    "common_name": "Hendrickson",
    "scientific_name": "Ephemerella subvaria",
    "base_temp_c": 5.0,
    "dd_threshold_mean": 260,
    "dd_threshold_sd": 40,
    "emergence_hr_start": 12,
    "emergence_hr_end": 15,
    "weather_prefs": {"clouds": "any", "wind": "<15mph"},
    "fly_patterns": [
      {"pattern": "Hendrickson Parachute", "size": 14, "stage": "dun"},
      {"pattern": "Pheasant Tail BH", "size": 14, "stage": "nymph"},
      {"pattern": "Rusty Spinner", "size": 14, "stage": "spinner"}
    ]
  },
  {
    "species_id": "bwo-spring",
    "common_name": "Blue-Winged Olive (spring)",
    "scientific_name": "Baetis tricaudatus",
    "base_temp_c": 3.0,
    "dd_threshold_mean": 180,
    "dd_threshold_sd": 60,
    "emergence_hr_start": 11,
    "emergence_hr_end": 16,
    "weather_prefs": {"clouds": "high", "wind": "<10mph"},
    "fly_patterns": [
      {"pattern": "BWO Comparadun", "size": 18, "stage": "dun"},
      {"pattern": "WD-40", "size": 18, "stage": "emerger"},
      {"pattern": "Pheasant Tail", "size": 18, "stage": "nymph"}
    ]
  }
  // ... sulphur, grannom caddis, trico, hex
]
```

Initial DD thresholds come from literature; calibration refines them.

---

## Scoring logic

### Degree-day accumulator (`models/degree_days.py`)

Given hourly water temp series for a reach, compute cumulative DD from Jan 1:

```
DD_day = max(0, ((T_max + T_min) / 2) - base_temp_c)
```

If water temp is missing for a reach, fall back to air→water regression fit per reach from historical data where both are available. Simple linear regression of `T_water = a * T_air_7day_avg + b` is fine for v1.

### Nymph score (`models/nymph_score.py`)

Five components, multiplicative where noted:

1. **Water temp zone** (Gaussian, peak 58°F, σ=6): always applies
2. **Flow regime** — compute current flow percentile vs. historical for this day-of-year. Score peaks at 30-60th percentile (stable normal flow); penalize extremes.
3. **Flow trend (24h)** — bonus for flow that was elevated 6-48h ago and is now falling stable. Penalty for rising flow.
4. **Behavioral drift window** — bonus for hours within 1h of civil sunrise or sunset.
5. **Pre-hatch drift bonus** — if current DD is within 100 DD-C below any species `dd_threshold_mean`, add bonus (nymphs become restless pre-emergence).

```
nymph_score = (temp_score * flow_score * flow_trend_score) + drift_window_bonus + prehatch_bonus
# clip to [0, 1]
```

### Dry fly score (`models/dry_score.py`)

For each species:
1. **In window?** Compute `P(in emergence window)` as Gaussian over current DD vs `dd_threshold_mean` with `dd_threshold_sd`.
2. **Daily conditions gate** — within window, score = probability * weather_match. Weather match from species `weather_prefs`:
   - Cloud cover match (e.g. BWOs love clouds)
   - Wind under species threshold
   - Water temp in species-specific emergence zone
3. **Time of day** — peaks within species `emergence_hr_start/end`.

Dry score for the reach = max across species. Store per-species probabilities in `active_species` for the API response.

### Fly recommender (`models/fly_recommender.py`)

Given active species list and their probabilities:
- For each species with probability > 0.3, emit fly suggestions from `fly_patterns`
- Pick stage based on time: dun early in window, emerger/spinner late
- Pair dry suggestion with matching nymph pattern as dropper
- If no dry species active but nymph_score is good: default to "searching" pattern (Pheasant Tail + Hare's Ear in 14-16)

Output format:
```json
{
  "primary": {"pattern": "Hendrickson Parachute", "size": 14},
  "dropper": {"pattern": "Pheasant Tail BH", "size": 14},
  "backup": [{"pattern": "BWO Comparadun", "size": 18}]
}
```

---

## API design

All JSON. No auth on MVP.

### `GET /reaches`
Returns list of reach summaries with current score. Payload small enough for a single map render.

### `GET /reach/{reach_id}`
Full reach detail: geometry, metadata, current conditions, nearest gauge info.

### `GET /forecast/{reach_id}?hours=48`
Hourly predictions. Returns:
```json
{
  "reach_id": "...",
  "computed_at": "...",
  "hours": [
    {
      "valid_at": "2026-04-22T10:00:00Z",
      "nymph_score": 0.72,
      "dry_score": 0.54,
      "air_temp_f": 68,
      "water_temp_f": 55,
      "flow_cfs": 82,
      "active_species": [{"id": "hendrickson", "probability": 0.54}],
      "flies": {...},
      "explanation": "Warming into Hendrickson window; behavioral drift starting; flows stable."
    }
  ]
}
```

### `GET /best-windows?hours=72`
Cross-reach ranking. Returns top 10 `(reach_id, window_start, window_end, combined_score)` tuples over the next N hours.

---

## Calibration script: `scripts/fit_dd_thresholds.py`

One-off, run after iNat dump is fresh.

1. For each target species, load research-grade iNat observations in Driftless bbox
2. Filter out observations clearly outside emergence window (bad IDs, stored specimens)
3. For each observation date/location, compute DD accumulated to that date using the nearest USGS water temp gauge (or air→water fallback)
4. Fit normal distribution over DD values at observation dates
5. Write `dd_threshold_mean` and `dd_threshold_sd` back to `species.json`
6. Save a diagnostic plot per species to `data/calibration/plots/`

Sanity check: threshold means should be ordered BWO < Hendrickson < sulphur < grannom < trico < Hex. If not, something's wrong with the fit.

---

## Jobs

### Hourly (`jobs/hourly_ingest.py`)
1. Pull latest USGS observations for all tracked gauges (last 2 hours to catch late-arriving data)
2. Pull NWS hourly forecast for any reach where cached forecast is >3h old
3. Upsert into `observation` and `weather_forecast`
4. Trigger forecast recompute for affected reaches

### Nightly (`jobs/nightly_rebuild.py`)
1. Recompute DD accumulations for all reaches (base temps 3, 5, 10°C)
2. Full 7-day forecast rebuild for every reach
3. Update `prediction` table
4. Vacuum SQLite

---

## Frontend (`web/`)

Single page, Leaflet-based.

**Layout:**
- Full-screen Leaflet map centered on Driftless (44.5, -91.5)
- Reaches rendered as polylines, color-coded by current combined score (red → yellow → green gradient)
- Sidebar (collapsible on mobile): default shows "Best windows next 48h" list
- Click a reach → sidebar switches to reach detail: current conditions, 48h score sparkline (Chart.js), active hatches, recommended flies, explanation text
- Date/time scrubber at bottom: shows map colored by score at selected time (scrub forward 7 days)

**No build step.** ES modules, CDN imports of Leaflet and Chart.js. Fine for MVP.

---

## Build order (actual execution plan for Claude Code)

Work through these in order. Each step should be a separate commit.

1. **Project scaffold** — pyproject.toml, Docker setup, empty module structure, CI skeleton
2. **DB schema + migrations** — create `schema.sql`, migration runner, basic queries module
3. **USGS ingest** — implement `ingest/usgs.py` with tests (use vcr.py). Pull 30 days of history for the 8 seed gauges on first run.
4. **NWS ingest** — `ingest/nws.py`. Cache gridpoint IDs.
5. **Reach bootstrap** — hand-write 8 reaches in `reaches.json` for MVP, one per seed gauge. Skip NHDPlus auto-population until v2.
6. **Degree-day model** — implement accumulator with tests. Compute DD for all reaches.
7. **Species seed + hardcoded thresholds** — populate `species.json` with literature-derived values. Calibration comes later.
8. **Hatch predictor + scoring** — implement nymph_score, dry_score, fly_recommender. Heavy unit test coverage here; this is the core model.
9. **Forecast builder** — end-to-end: for each reach, for each hour in next 48h, compute prediction and write to DB.
10. **API** — FastAPI endpoints. Return shapes match the spec above.
11. **Frontend** — Leaflet map, sidebar, date scrubber. Use the live API.
12. **Scheduler** — wire up APScheduler, run hourly + nightly jobs
13. **Docker Compose** — single-file deploy
14. **Calibration script** — iNat pull + DD threshold fitting. Refine species.json.
15. **Air→water regression** — for reaches without temp gauges, fit `T_water = f(T_air_7day_avg)` from historical. Backfill.

Stop here. Ship it. Then:

**Phase 2 (later):**
- NHDPlus auto-bootstrap for full Driftless reach coverage
- Community catch logging for ground-truth validation
- Alerts (Telegram bot — you already have the Rook infra)
- Expand species list to 15-20
- Mobile-optimized frontend

**Phase 3 (someday):**
- ML scoring trained on logged outcomes
- Hatch camera integration
- Integration with MN DNR / WI DNR macroinvertebrate survey data

---

## Things Claude Code should NOT do

- Don't add auth. MVP is public read-only.
- Don't add a frontend build system (webpack, vite). Static files only.
- Don't use an ORM. Raw SQL is fine at this scale.
- Don't mock iNat data for tests; use vcr.py cassettes of real responses.
- Don't scrape Whacking Fatties or any other fishing site. Public APIs only.
- Don't make the scoring function configurable via JSON/YAML rules. Hardcode it in Python with clear comments; easier to maintain and debug.

---

## Open questions to flag

Claude Code should stop and ask before resolving these:

1. **Reach definition granularity.** Is a single 12km reach on the Rush the right resolution, or should we split into upper/middle/lower? (Recommend: start whole, split later if data justifies.)
2. **How to handle gauges without water temp.** Air→water regression is the plan, but v1 might just mark those reaches "degraded confidence" in the UI.
3. **Time zone handling.** Store UTC in DB, convert to America/Chicago in API responses.
4. **Where to host.** Fine on a $5 DigitalOcean droplet; also fits on the existing Rook Pi if that's preferred.
