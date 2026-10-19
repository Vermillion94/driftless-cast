# Architecture

Driftless Cast is a single Python service, a SQLite file and a static
frontend with no build step. It is deliberately small: one person can run it
for about $2/month, and it scales to many more regions before any of that has
to change.

```
                 hourly, in-process                      read time
 public APIs ──► ingest/ ──► models/ ──► prediction ──► api/routes ──► web/ (Leaflet SPA)
 (USGS, NWS,     fetchers    scoring      table          headline,       map, verdicts,
  NWPS, ACIS)                 pipeline    (SQLite)        tactics,        tactics, PWA
                                                          day verdicts
```

## Components

| Path | Role |
|---|---|
| `src/ingest/` | Thin clients for each upstream API. One module per source, with timeouts and a same-day cache for ACIS. `identity.py` holds the User-Agent. |
| `src/models/forecast_builder.py` | Orchestrates one rebuild. For each reach it gathers signals (flow, percentiles, water temp, degree-days, anomaly, hourly weather) and scores 168 hours into the `prediction` table. |
| `src/models/nymph_score.py`, `dry_score.py`, `hatch_predictor.py`, `seasonal.py` | Subsurface and surface scoring: temperature curve, diel rhythm, hatch calendar × degree-day gate × weather × emergence hour. |
| `src/models/temp_estimator.py`, `thermal_profile.py`, `dd_pipeline.py` | Air → water temperature (Mohseni), spring-creek damping, degree-day accumulation. |
| `src/models/runoff_risk.py`, `recession.py` | Rain → flow/clarity risk; flow recession toward the seasonal median. |
| `src/models/regime.py` | Day type (CLOSED, LIGHTNING, HAZARD, BLOWOUT, HEAT_STRESS, STREAMER, HATCH, …). The hard-stop regimes are capped in `score_calibration.REGIME_CAPS`. |
| `src/models/regulations.py` | MN / WI / IA season status and spawning advisory. |
| `src/models/score_calibration.py` | Read-time headline score, lanes, aggression, confidence. |
| `src/models/tactics.py` | How and where to fish for an hour. |
| `src/models/trip_verdict.py` | Per-day Go / Worth it / Slow / Skip / Closed verdicts with reasons. |
| `src/api/` | FastAPI app: routes, security headers, `/healthz`, rate-limited writes, static mount of `web/`. |
| `src/jobs/forecast_refresh.py` | Single-flight rebuild runner, used by startup, the hourly loop and `POST /refresh`. |
| `web/` | Vanilla JS + Leaflet SPA: `map.js`, `styles.css` (token-based design system, light/dark), `sw.js` (offline shell + last-good API), manifest and icons. |
| `data/seed/` | Reaches (geometry, gauges, class), species (hatch biology), gauges, Learn content. |
| `data/calibration/` | Fitted model constants (Mohseni, recession, runoff, DD diagnostics). |

## Key design decisions

- **Score at build time, calibrate at read time.** Raw nymph and dry scores, the regime and the breakdown are persisted. The headline, tactics and day verdicts are computed per request from those columns. Changing calibration needs no rebuild; changing physics does.
- **Hard stops are regimes.** Legality, lightning, cold/wind hazard and lethal water become a regime code with a cap in one table, so every surface (map, lists, verdicts) agrees.
- **Every constant is sourced or labeled heuristic.** See `docs/REFERENCES.md`, `docs/FIELD_GUIDE_REVIEW.md` and the in-app Learn tab.
- **Region-specific knowledge lives in data, not code.** Hatch timing (`seasonal.DRIFTLESS_PEAKS`, `species.json`), season rules (`regulations.py`), reaches (`reaches.json`) and calibration files. A new region means a new set of these plus validation; the engine is shared.

## Expanding to a new region

1. Reaches: geometry (NHDPlus), gauge mapping, spring influence, trout class.
2. Season rules for the state(s) in `regulations.py`.
3. A hatch calendar and species weights from the state agency chart plus local guide reports.
4. Water-temperature calibration: run `refit_mohseni_driftless.py` on that region's USGS temperature history and validate it in `backtest.py`.
5. Runoff and recession fits per gauge (`fit_runoff_response.py`, `fit_recession.py`).
6. Field validation through trip reports, tracked by `/reliability`.

## Quality gates

- `pytest tests/`: 117 tests covering model curves, regimes, legality, tactics, verdicts, API hardening and the core chain.
- CI (`.github/workflows/backtest.yml`): the acceptance suite, a JS syntax check, and a live hindcast that gates on flow-recession MAPE and water-temperature RMSE/bias. Both checks are required on `main`.
- `scripts/ui_smoke.py`: headless-browser click-through of every view in light and dark mode. It fails on any JS error. Run it before deploys that touch `web/`.
