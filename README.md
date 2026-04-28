# Driftless Cast

A hyperlocal fly-fishing forecast for the Driftless Area (SE MN, W WI, NE IA).
Predicts per-reach, per-hour fishing quality from public no-key data sources.
Live at **<https://driftless-cast.fly.dev/>**.

## What it does

For each of 21 trout streams, every hour out to 7 days, computes:

- **Combined score** (0–1) shown as Go / Fair / Skip on the map
- **Nymph and dry-fly scores** as separate components, with full audit-trail multipliers
- **Active hatches** (BWO, Hendrickson, Sulphur, Caddis, Hex, Trico, Iso, terrestrials, early-black stones)
- **Recommended flies** keyed to the dominant species and time of day
- **Fishing regime** — STREAMER / HATCH / TERRESTRIAL / MIDGE / SCUD / NORMAL — so a "low score" day still tells you what gear to bring
- **Honest disclosure** — every signal links to a Learn topic that explains how it's computed and cites the underlying paper

## How it gets the inputs

All public, no-key APIs:

- **USGS Water Data** — instantaneous flow + stage; daily-statistics percentiles by day-of-year
- **NWS API** — hourly forecast (air temp, wind, cloud, precip-prob) + raw gridpoint QPF
- **NOAA NWPS** — gauges that don't have USGS, plus short-range streamflow forecasts
- **Open-Meteo** — historical daily air temp + forecasted hourly air temp + surface pressure
- **USGS NHDPlus High-Resolution** — real stream centerlines (one-off, run via script)
- **iNaturalist + GBIF + iDigBio** — degree-day calibration for selected species (one-off)
- **WI / MN / IA DNR** — trout-stream classifications (one-off)

## How it computes a score

**Nymph** (subsurface): `temperature × flow_percentile × flow_trend + drift-window-bonus + prehatch-bonus`. Then multiplied by a barometric-pressure factor.

**Dry** (surface): `max over species of (seasonal × DD-readiness × weather-match × emergence-hour-window)`. Then multiplied by pressure and a sun-angle penalty for clear-water shyness.

**Combined** = max(nymph, dry). Capped at 0.10 in BLOWOUT regime.

The full math is documented in `data/seed/education.json` (rendered as the in-app Learn tab) and every reference traces to `docs/REFERENCES.md`.

## Project structure

```
src/
  api/             FastAPI app, routes, static-mount of web/
  db/              SQLite schema + queries
  ingest/          USGS, NWS, NOAA NWPS, Open-Meteo, iNat, GBIF, iDigBio, WI DNR
  models/          forecast_builder, regime, nymph_score, dry_score,
                   degree_days, mohseni temp_estimator, fly_recommender
  scripts/         bootstrap_reaches, fetch_nhdplus_geometry, backtest,
                   fit_dd_thresholds, refit_mohseni
data/
  seed/            reaches.json, species.json, gauges.json, education.json
  calibration/     model artifacts (mohseni_fit.json, dd_fit_diagnostics.json)
web/               index.html, map.js, styles.css — vanilla, no build step
tests/             test_acceptance.py — 29 pinned synthetic cases
docs/              REFERENCES.md (master bibliography), DEPLOYMENT.md
```

## Running locally

```bash
# Install deps (Python 3.13 recommended)
poetry install

# Initialize DB + seed reaches
python -m src.scripts.bootstrap_reaches

# Start API + serve frontend (single port — production-like)
uvicorn src.api.main:app --reload

# Open http://localhost:8000
```

For separate-port dev (where map.js auto-detects and points at `http://localhost:8000`):

```bash
# Terminal 1
uvicorn src.api.main:app --reload --port 8000

# Terminal 2
python serve_static.py     # serves web/ on :8080

# Open http://localhost:8080
```

## Validation

Three independent paths, all wired:

```bash
# Hindcast against historical USGS + Open-Meteo data — flow recession + Mohseni
python -m src.scripts.backtest

# Synthetic acceptance suite — 29 pinned model facts (no pytest needed)
python -m tests.test_acceptance

# Reliability dashboard (live in the Learn tab) — calibrates against accumulated trip reports
```

Pass thresholds in `src/scripts/backtest.py#THRESHOLDS`. Methodology in `data/seed/education.json#validation`.

## Deploying

Deploy guide for Fly.io / Render / DigitalOcean / Pi: see [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

The Fly setup is one-time, then `fly deploy` (or git push to main once the Actions workflow is wired) for every change.

## Contributing

The model is wrong about something? Two ways to help:

1. **Tap an emoji.** After any trip, hit "Just got off the water?" on the reach you fished — picks the model's residual toward your reported success at ≥3 reports per reach.
2. **Open an issue or PR.** Especially welcomed: better stream proxies, better DD thresholds, fixes to the heuristic curves (wind, cloud, sun-angle, regimes — all marked "guide-derived" and hungry for data).

## License

MIT (per `pyproject.toml`).
