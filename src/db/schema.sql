CREATE TABLE IF NOT EXISTS reach (
    reach_id         TEXT PRIMARY KEY,
    stream_name      TEXT NOT NULL,
    segment_name     TEXT,
    state            TEXT NOT NULL,
    trout_class      TEXT,
    geometry_geojson TEXT NOT NULL,
    centroid_lat     REAL NOT NULL,
    centroid_lon     REAL NOT NULL,
    length_km        REAL,
    mean_gradient    REAL,
    usgs_gauge_id    TEXT,
    noaa_lid         TEXT,
    gauge_is_proxy   INTEGER DEFAULT 0,
    proxy_distance_km REAL,
    nws_gridpoint    TEXT,
    spring_influenced INTEGER DEFAULT 0,
    notes            TEXT,
    dnr_summary      TEXT,
    region           TEXT,                          -- e.g. "Driftless", "North Shore MN"
    fishery          TEXT,                          -- JSON: tier/wild_population/density/etc
    model_caveat     TEXT                           -- e.g. "hatch model is Driftless-tuned"
);

CREATE TABLE IF NOT EXISTS gauge (
    gauge_id    TEXT PRIMARY KEY,
    name        TEXT,
    lat         REAL,
    lon         REAL,
    params      TEXT
);

CREATE TABLE IF NOT EXISTS observation (
    gauge_id       TEXT NOT NULL,
    observed_at    TEXT NOT NULL,
    parameter_code TEXT NOT NULL,
    value          REAL,
    PRIMARY KEY (gauge_id, observed_at, parameter_code)
);

CREATE TABLE IF NOT EXISTS weather_forecast (
    reach_id      TEXT NOT NULL,
    forecast_at   TEXT NOT NULL,
    valid_at      TEXT NOT NULL,
    air_temp_f    REAL,
    dewpoint_f    REAL,
    wind_mph      REAL,
    wind_dir      TEXT,
    cloud_cover   REAL,
    precip_prob   REAL,
    PRIMARY KEY (reach_id, valid_at)
);

CREATE TABLE IF NOT EXISTS species (
    species_id         TEXT PRIMARY KEY,
    common_name        TEXT NOT NULL,
    scientific_name    TEXT,
    base_temp_c        REAL NOT NULL,
    dd_threshold_mean  REAL NOT NULL,
    dd_threshold_sd    REAL NOT NULL,
    emergence_hr_start INTEGER,
    emergence_hr_end   INTEGER,
    weather_prefs      TEXT,
    fly_patterns       TEXT
);

CREATE TABLE IF NOT EXISTS dd_accumulation (
    reach_id      TEXT NOT NULL,
    date          TEXT NOT NULL,
    base_temp_c   REAL NOT NULL,
    accumulated   REAL NOT NULL,
    PRIMARY KEY (reach_id, date, base_temp_c)
);

CREATE TABLE IF NOT EXISTS prediction (
    reach_id             TEXT NOT NULL,
    valid_at             TEXT NOT NULL,
    computed_at          TEXT NOT NULL,
    nymph_score          REAL,
    dry_score            REAL,
    active_species       TEXT,
    recommended_flies    TEXT,
    explanation          TEXT,
    water_temp_f         REAL,
    water_temp_source    TEXT,
    anomaly_f            REAL,
    hatch_shift_days     REAL,
    fish_stress          INTEGER DEFAULT 0,
    air_temp_f           REAL,
    cloud_cover          REAL,
    wind_mph             REAL,
    flow_cfs             REAL,
    precip_prob          REAL,
    short_forecast       TEXT,
    regime               TEXT,                       -- JSON: regime classification
    pressure_delta_mb    REAL,                       -- 6h surface pressure trend
    score_breakdown      TEXT,                       -- JSON: per-multiplier components
    PRIMARY KEY (reach_id, valid_at)
);

CREATE TABLE IF NOT EXISTS catch_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    reach_id        TEXT NOT NULL,
    fished_at       TEXT NOT NULL,        -- ISO8601, when the trip happened (start time)
    success         INTEGER NOT NULL,     -- 0 skunked, 1 a few, 2 solid, 3 great
    reporter_name   TEXT,                 -- optional display name / alias
    method          TEXT,                 -- 'dry', 'nymph', 'streamer', 'mixed'
    session_window  TEXT,                 -- 'dawn', 'morning', 'afternoon', 'dusk', 'night'
    topwater_level  INTEGER,              -- 0 none, 1 occasional, 2 steady, 3 popping
    insect_activity TEXT,                 -- free text: PMD/caddis/spinners/etc
    species_caught  TEXT,                 -- JSON array of species_ids OR free text ('brown trout')
    worked          TEXT,                 -- what worked
    didnt_work      TEXT,                 -- what did not work
    notes           TEXT,                 -- free text
    fly_used        TEXT,                 -- e.g. 'Pheasant Tail #14'
    water_temp_f    REAL,                 -- optional reported water temp
    submitted_at    TEXT NOT NULL,        -- ISO8601, when the entry was logged
    predicted_score REAL,                 -- snapshot of model's score for this hour
    predicted_regime TEXT                 -- snapshot of regime code
);
CREATE INDEX IF NOT EXISTS idx_catch_log_reach ON catch_log (reach_id, fished_at);
