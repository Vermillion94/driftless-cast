# Data sources and licensing

Every external input Driftless Cast uses, what it is used for, and whether
commercial use is clean. Reviewed October 2026.

**Summary:** production runs entirely on U.S. government public-domain data
plus permissively licensed libraries. A few one-off research scripts used
sources with non-commercial or per-record licenses. Their outputs are small
calibration constants, listed under [Research-only](#research-only-sources-not-called-in-production)
with what to re-run before a commercial launch.

## Live production sources

| Source | Used for | License / terms | Commercial use | Attribution shown |
|---|---|---|---|---|
| **USGS Water Data** (waterservices.usgs.gov) | Live flow and stage, daily-flow statistics (percentiles), water temperature | U.S. Government work, public domain | Yes | "USGS" in Data sources page |
| **NOAA National Weather Service API** (api.weather.gov) | Hourly forecast (air temp, cloud, wind, precip probability, thunderstorms); gridpoint QPF | Public domain. Requires a User-Agent with contact info (`DC_CONTACT`) | Yes | Data sources page |
| **NOAA National Water Prediction Service** (api.water.noaa.gov/nwps) | Gauges without USGS data; short-range flow forecasts | Public domain | Yes | Data sources page |
| **NOAA Regional Climate Centers — ACIS** (data.rcc-acis.org, NRCC interpolated grid) | Historical daily air temperature (water-temp model, degree-days, anomaly) | Public NOAA RCC service; grid is derived from NWS Cooperative Observer data | Yes. For very high volume, coordinate with NRCC as a courtesy. | Data sources page |
| **USGS The National Map** basemaps (Topo, Imagery Topo) | Map tiles | Public domain | Yes | Map attribution control |
| **Leaflet 1.9.4** (unpkg CDN, SRI-pinned) | Map library | BSD-2-Clause | Yes | Map attribution control |
| **Inter** font (Google Fonts) | UI typeface | SIL Open Font License 1.1 | Yes | — |

## One-off sources baked into seed data

| Source | Used for | License / terms | Commercial use |
|---|---|---|---|
| USGS NHDPlus High Resolution | Stream centerlines in `data/seed/reaches.json` | Public domain | Yes |
| WI DNR trout-regulation ArcGIS layer | Stream classification and regulation category for WI reaches | Wisconsin public records / open data | Yes (state open data); re-verify the layer's terms page |
| MN Rule 6262.0200, WI DNR, Iowa DNR season pages | Season logic in `src/models/regulations.py` | Public law / agency information | Yes |
| MN DNR SE Minnesota hatch chart | Hatch calendar windows | Agency publication; facts (dates, taxa) | Yes. Facts are not copyrightable; no text is reproduced |
| Driftless Angler reports, Coulee Region TU, other guide writing | Rules of thumb behind `tactics.py` and hatch timing | Copyrighted articles; **only facts and practices are used, no text is reproduced** | Yes, as research input. Cited in `docs/REFERENCES.md` |

## Research-only sources (not called in production)

| Source | Script | Output | Status |
|---|---|---|---|
| Open-Meteo archive (hourly precipitation) | `src/scripts/fit_runoff_response.py` | Per-reach rain thresholds in `data/calibration/runoff_response_fit.json` | Open-Meteo's free API is **non-commercial**. Before charging customers, re-run this fit with NOAA Stage IV / MRMS precipitation or a paid Open-Meteo plan. |
| Open-Meteo archive (daily air temp) | Earlier `refit_mohseni.py` runs | Region-wide Mohseni curve in `data/calibration/mohseni_fit.json` | The same `fetch_archive_daily_mean_f` now calls ACIS, so re-running the script produces a clean replacement. |
| iNaturalist, GBIF, iDigBio | `src/scripts/fit_dd_thresholds.py` | Degree-day threshold estimates in `data/seed/species.json` | Records carry per-record licenses, some CC BY-NC. The output is a few summary statistics, not the records, which is low risk. Re-fitting on CC0/CC BY records only would remove the question. |

Open-Meteo was removed from production in October 2026. It had supplied
historical air temperature (now ACIS) and pressure (now disabled; see
`forecast_builder.build_for_reach`).

## User data

- **Trip reports / journal** (`catch_log` table): private by default. The journal lives in the browser's local storage; the server stores stream, date, outcome, method, fly and a conditions snapshot, with name and notes stripped (`is_public = 0`). Calibration and the anonymous `/community` totals use all rows; the public feed shows only rows the angler chose to share. There are no accounts and no email addresses.
- **Location:** used client-side for "near me" and sent rounded to 3 decimals (~100 m). Not stored.
- **No analytics, ads or third-party trackers.**
