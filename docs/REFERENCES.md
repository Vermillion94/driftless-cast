# References

The master bibliography for Driftless Cast. Every quantitative claim the model
makes should trace back to one of these references, OR be marked in the code
and in `data/seed/education.json` as a **heuristic** / **angler-consensus**
value with no formal study to back it.

The standard for inclusion: peer-reviewed papers, USGS / state-agency technical
reports, and reference texts for the field. We do **not** cite blog posts, fly
shop reports, or guide books as if they were peer-reviewed sources — when
they're the only basis for a parameter, that parameter is labeled
"guide-derived" in code and in the Learn topics.

## Conventions

- Each entry has a stable `id` matching the keys used in
  `data/seed/education.json` so help-buttons and topic pages can deep-link to
  the same canonical source.
- Where a parameter in code is keyed to a reference, the source comment looks
  like `# see docs/REFERENCES.md#mohseni1998`.
- Where a parameter is heuristic, comments use `# heuristic — no formal
  study; tuned against angler reports`.

---

## Hydrology — flow, recession, baseflow

**`brutsaert_nieber_1977`**
Brutsaert, W., & Nieber, J. L. (1977). Regionalized drought flow hydrographs
from a mature glaciated plateau. *Water Resources Research*, 13(3), 637–643.
DOI: 10.1029/WR013i003p00637.
- Foundational paper for streamflow recession analysis. Introduces the
  `−dQ/dt = f(Q)` lower-envelope method that's still standard today.
- Used in Driftless Cast as the methodological reference for *how* recession
  constants are estimated. We don't currently fit them per-gauge — that's
  follow-up work — but when we do, this is the method.

**`tallaksen_1995`**
Tallaksen, L. M. (1995). A review of baseflow recession analysis.
*Journal of Hydrology*, 165(1–4), 349–370.
DOI: 10.1016/0022-1694(94)02540-R.
- Comprehensive review of recession-analysis methods, including the
  exponential, hyperbolic, and double-exponential forms; segment selection;
  and characteristic-timescale derivation.
- Reference for the exponential form `Q(t) = Q_b + (Q₀ − Q_b)·exp(−t/τ)`
  used in `forecast_builder._flow_percentile_for_hour`.

**`maillet_1905`**
Maillet, E. (1905). *Essais d'hydraulique souterraine et fluviale.*
Hermann, Paris.
- Original derivation of the exponential recession for baseflow
  (`Q(t) = Q₀·e^(-αt)`). Cited here for completeness; modern readers should
  use Tallaksen 1995 for the full treatment.

**`juckem_2008`**
Juckem, P. F., Hunt, R. J., Anderson, M. P., & Robertson, D. M. (2008).
Effects of climate and land management change on streamflow in the Driftless
Area of Wisconsin. *Journal of Hydrology*, 355(1–4), 123–130.
DOI: 10.1016/j.jhydrol.2008.03.010.
- Driftless-specific. Demonstrates the high baseflow contribution of Driftless
  watersheds and a step-increase in baseflow around 1970, attributed to a
  combination of precipitation regime change and reduced agricultural
  intensity (more infiltration, less stormflow).
- Used here as the basis for assigning **slower** recession to spring-fed /
  bedrock-aquifer-influenced reaches (Kinnickinnic, Whitewater forks, Trout
  Run, Pine Cr) than to flashier freestone reaches (Apple, Kickapoo).

**`gebert_2011`**
Gebert, W. A., Walker, J. F., & Kennedy, J. L. (2011). Estimating 1970–99
average annual groundwater recharge in Wisconsin using streamflow data.
*USGS Open-File Report 2009-1210.*
URL: https://pubs.usgs.gov/of/2009/1210/.
- Annual-average baseflow estimates by gauge across Wisconsin including the
  Driftless. Establishes that recharge / baseflow contribution varies by
  factors of ~10× across the state, with the Driftless on the high end.
- Cited as supporting evidence for spring-fed vs. freestone recession-class
  separation. Specific gauge values used as a future calibration target,
  not as direct inputs to the current model.

**`pilgrim_cordery_1992`**
Pilgrim, D. H., & Cordery, I. (1992). Flood runoff. Chapter 9 in
*Handbook of Hydrology* (D. R. Maidment, ed.). McGraw-Hill, New York.
- Standard reference for typical recession-constant ranges in small
  watersheds. Reports baseflow recession constants of 11–53 days
  (mean ≈ 33 days) across published studies.
- Caveat: those are **baseflow** (slow) recession constants. The
  *stormflow* (event-flow) recession we model in Driftless Cast is much
  faster — hours to a few days. We use Pilgrim & Cordery as a sanity
  check on long-tail behavior, not for the prior on event recession.

**`usgs_waterservices`**
USGS Water Data Services — Daily Statistics endpoint.
URL: https://waterservices.usgs.gov/docs/statistics/.
- Source for per-gauge percentiles (p10, p25, p50, p75, p90) of discharge by
  calendar day-of-year. We use p50 (the day-of-year median) as the recession
  target ("flow that recedes toward normal").

---

## Stream temperature — Mohseni model and trout thermal limits

**`mohseni_1998`**
Mohseni, O., Stefan, H. G., & Erickson, T. R. (1998). A nonlinear regression
model for weekly stream temperatures. *Water Resources Research*, 34(10),
2685–2692. DOI: 10.1029/98WR01877.
- The four-parameter logistic equation we use to estimate water temperature
  from a 7-day rolling air-temperature mean:
  `T_w = μ + (α − μ) / (1 + exp(γ·(β − T_a)))`.
- Refit per stream class (freestone, mixed) against ~47k paired daily
  air/water observations from 48 USGS gauges in MN/WI/IA. Class fits are
  in `data/calibration/mohseni_fit.json`.

**`stefan_preudhomme_1993`**
Stefan, H. G., & Preud'homme, E. B. (1993). Stream temperature estimation
from air temperature. *JAWRA*, 29(1), 27–45.
DOI: 10.1111/j.1752-1688.1993.tb01502.x.
- Earlier linear-regression air → water work that Mohseni 1998 generalizes.
  Cited in `education.json#mohseni` for completeness; the model itself uses
  Mohseni's nonlinear form.

**`webb_nobilis_1997`**
Webb, B. W., & Nobilis, F. (1997). Long-term perspective on the nature of
the air–water temperature relationship. *Hydrological Processes*, 11(2),
137–147.
- Used in `education.json#mohseni` as supporting context for the air → water
  coupling assumption.

**`wehrly_2007`**
Wehrly, K. E., Wang, L., & Mitro, M. (2007). Field-based estimates of
thermal tolerance limits for trout. *Transactions of the American Fisheries
Society*, 136(2), 365–374. DOI: 10.1577/T05-189.1.
- Standard citation for the brown / brook / rainbow trout thermal-stress
  thresholds. Their Wisconsin-region brown-trout 7-day mean lethal
  temperature is approximately 24°C (75°F); chronic stress sets in at
  21–22°C (70–72°F); we use 20°C (68°F) as the conservative angling-ethics
  warning threshold.
- Directly relevant to our seed reaches because their dataset *is*
  Wisconsin trout streams.

**`elliott_1981`**
Elliott, J. M. (1981). Some aspects of thermal stress on freshwater
teleosts. In: Pickering, A. D. (ed.) *Stress and Fish*. Academic Press.
- Earlier salmonid thermal-physiology reference; supports the 68°F
  catch-and-release ethics threshold via cumulative-stress arguments.

**`wilkie_1996`**
Wilkie, M. P., et al. (1996). The physiological response of stream-dwelling
salmonids to angling-related stressors. *Reviews in Fish Biology and
Fisheries*, 6(2), 219–247.
- Catch-and-release mortality literature — supports the warning-banner copy
  about minimal handling above 68°F.

**`beschta_1987`**
Beschta, R. L., Bilby, R. E., Brown, G. W., Holtby, L. B., & Hofstra, T. D.
(1987). Stream temperature and aquatic habitat: fisheries and forestry
interactions. In *Streamside Management: Forestry and Fishery Interactions*,
University of Washington.
- Reference textbook chapter on stream-temperature regimes and salmonid
  habitat. Cited as supporting context for the temperature-zone Gaussian.

**`caissie_2006`**
Caissie, D. (2006). The thermal regime of rivers: a review. *Freshwater
Biology*, 51(8), 1389–1406.
DOI: 10.1111/j.1365-2427.2006.01597.x.
- Review of river thermal processes across diel, daily, and seasonal scales.
  Reports typical diurnal water-temperature ranges of 1–10°C for temperate
  streams (low end for groundwater-influenced reaches, high end for
  unshaded freestones); peaks at 14:00–18:00 local with a 3–4 hour phase
  lag from solar noon.
- Direct reference for the diurnal-swing sinusoid in
  `forecast_builder._diurnal_water_temp_f`. Our ±3°F freestone / ±1°F
  spring-fed half-amplitudes (peak-to-trough 2×) sit at the conservative
  low end of Caissie's reported ranges; 17:00 peak phase fits the upper
  end of the empirical window for unshaded Driftless reaches.

**`sinokrot_stefan_1993`**
Sinokrot, B. A., & Stefan, H. G. (1993). Stream temperature dynamics:
Measurements and modeling. *Water Resources Research*, 29(7), 2299–2312.
DOI: 10.1029/93WR00540.
- Hourly energy-balance model for stream temperature, formulated as an
  unsteady advection–dispersion equation with explicit terms for air
  temperature, solar radiation, humidity, cloud cover, wind, and bed
  conduction. Documents the sinusoidal diurnal pattern that the
  Mohseni 1998 7-day rolling mean intentionally averages over.
- Secondary reference for the diurnal-swing sinusoid in `forecast_builder`.

---

## Aquatic-insect emergence and degree-day phenology

**`sweeney_1984`**
Sweeney, B. W. (1984). Factors influencing life-history patterns of aquatic
insects. In: Resh, V. H., & Rosenberg, D. M. (eds.) *The Ecology of Aquatic
Insects*. Praeger Publishers.
- Foundational reference for degree-day–driven aquatic-insect emergence.
  Establishes that mayfly, stonefly, and caddisfly emergence is timed by
  accumulated thermal exposure above a species-specific base temperature.

**`vannote_sweeney_1980`**
Vannote, R. L., & Sweeney, B. W. (1980). Geographic analysis of thermal
equilibria. *American Naturalist*, 115(5), 667–695.
DOI: 10.1086/283591.
- The "thermal equilibrium hypothesis" for aquatic-insect distribution and
  emergence timing across latitudes. Supports the use of degree-days as a
  cross-site phenological currency.

**`brittain_1990`**
Brittain, J. E. (1990). Life history strategies in Ephemeroptera and
Plecoptera. In: *Mayflies and Stoneflies: Life Histories and Biology*.
Kluwer Academic.
- Reference text for mayfly / stonefly life-history parameters including
  emergence-temperature thresholds for several genera in our species list.

**`harper_peckarsky_2006`**
Harper, M. P., & Peckarsky, B. L. (2006). Emergence cues of a mayfly in a
high-altitude stream ecosystem. *Ecological Applications*, 16(2), 612–621.
- Source for the ~3-days-per-1°C anomaly-shift in mayfly emergence used
  in `education.json#anomaly_shift`. Note: their study is on water-temp
  anomaly; we use air-temp anomaly with a discount factor.

---

## Aquatic-insect drift behavior

**`waters_1972`**
Waters, T. F. (1972). The drift of stream insects. *Annual Review of
Entomology*, 17, 253–272.
DOI: 10.1146/annurev.en.17.010172.001345.
- Standard review of behavioral and constant drift in stream invertebrates.
  Establishes the dawn / dusk drift peaks used in our drift-window bonus.

**`elliott_1965`**
Elliott, J. M. (1965). Daily fluctuations of drift invertebrates in a
Dartmoor stream. *Nature*, 205(4976), 1127–1129.
- Early classic on the diel periodicity of invertebrate drift.

**`brittain_eikeland_1988`**
Brittain, J. E., & Eikeland, T. J. (1988). Invertebrate drift — a review.
*Hydrobiologia*, 166(1), 77–93.
DOI: 10.1007/BF00017485.
- Reviews flow-event-driven drift, supporting the post-rain "drift peak"
  bonus in nymph_score (6–48h after high flow).

---

## Mayfly taxonomy and biology references

**`edmunds_1976`**
Edmunds, G. F., Jensen, S. L., & Berner, L. (1976). *The Mayflies of North
and Central America*. University of Minnesota Press.
- Reference text for mayfly identification, emergence behavior, and
  phenological windows used to validate species-specific emergence-hour
  ranges.

**`lafontaine_1990`**
LaFontaine, G. (1990). *The Dry Fly: New Angles*. Greycliff Publishing.
- Guide reference (not peer-reviewed). Cited in `education.json#weather_match`
  to ground the cloud-cover-preference heuristics for BWO and other species.

---

## Habitat and trout-population biology

**`binns_eiserman_1979`**
Binns, N. A., & Eiserman, F. M. (1979). Quantification of fluvial trout
habitat in Wyoming. *Transactions of the American Fisheries Society*,
108(3), 215–228.
- Habitat Quality Index — supports the asymmetric flow-percentile scoring
  curve (low flow ≠ bad; high flow rapidly degrades wadeable habitat).

---

## Solar position

**`noaa_solar`**
NOAA Global Monitoring Laboratory — Solar Position Calculator equations
(declination, equation of time).
URL: https://gml.noaa.gov/grad/solcalc/solareqns.PDF.
- Pure-math implementation in `src/models/solar.py`. No API call. Used to
  compute sun altitude for the bright-sun penalty on the dry-fly score
  and to potentially refine the dawn / dusk drift window.

---

## Fish behavior — light and barometric pressure

**`helfman_1979`**
Helfman, G. S. (1979). Twilight activities of yellow perch, Perca
flavescens. *Journal of the Fisheries Research Board of Canada*, 36(2),
173–179.
- Early demonstration of strong light-level → fish-activity coupling.
  Cited for the broader biological basis of dawn / dusk feeding peaks;
  no Driftless-trout-specific paper exists at this resolution.

**`hoar_1942`**
Hoar, W. S. (1942). Diurnal variations in feeding activity of young
salmon and trout. *Journal of the Fisheries Research Board of Canada*,
6a(1), 90–101. DOI: 10.1139/f42-011.
- Salmonid-specific evidence (juvenile Atlantic salmon and brown/brook
  trout) that feeding peaks fall at dawn and dusk in spring/summer/autumn,
  with "little food from about 10 p.m. to about 5 a.m." Replicated by
  multiple later studies on Atlantic salmon and rainbow trout
  (crepuscular feeding pattern).
- Direct basis for the 5–22 local "fishable hours" filter in the
  frontend daily-peak picker (`web/map.js#isFishableHour`) — restricts
  the displayed daily peak to hours when salmonids are documented to
  feed, even when the underlying model scores a pre-dawn hour higher
  for cool-water + low-sun reasons.

**No formal reference for barometric pressure → trout feeding rate.**
The peer-reviewed literature on barometric-pressure effects on
*freshwater* trout feeding is thin and inconsistent — most controlled
studies focus on saltwater species. The pressure-trend multipliers in
`forecast_builder._pressure_trend_factor` are derived from fly-fishing
guide consensus (Charlie Meck, Ed Engle, Joe Humphreys). They are
labeled as such in code and in `education.json#barometric_pressure`,
not as literature-derived.

**Flow trend (rising vs. falling discharge) → feeding rate is also a
labeled heuristic, not a literature-derived multiplier.**
The angler folklore — "fish feed on falling water, scatter on rising
water" — is widely shared but the controlled peer-reviewed evidence is
mixed. We surveyed:

- Korman et al. (2026, *Polish Journal of Ecology*): a flume study of
  brown trout fry under stable vs. downramping flow regimes found "no
  detectable effect on total prey intake" under adequate prey
  availability. The treatment did not disrupt short-term feeding.
- Greenberg, L. A. (1992). The effect of discharge and predation on
  habitat use by wild and hatchery brown trout. *Regulated Rivers:
  Research & Management*, 7(2), 205–212. — reduced discharge displaced
  fish to less-shallow habitat, increasing intraspecific overlap and
  competition; the effect on use of habitat is mediated by predation
  risk, not directly by feeding rate.
- Higgins-Auvil, M., et al. (2024). Fine-scale movement response of
  juvenile brown trout to hydropeaking. *Science of the Total
  Environment*. — fish move laterally to refugia during hydropeaking
  ramps but resume feeding from the new position; the kinetic effect
  is on positioning, not on feeding cessation.

Net read: rising-flow effects are mediated through habitat
displacement (consistent with reduced feeding *opportunity*, not
feeding *physiology*), and the effect magnitude in short-duration
natural events is small. The `flow_trend_score` curve in
`src/models/nymph_score.py` is therefore kept in a narrow band
([0.70, 1.0]) and labeled as guide-derived heuristic with bounded
influence on the score, not a peer-reviewed multiplier.

---

## Public data sources (no-key APIs we live-fetch)

- **USGS Water Data Services** — instantaneous values, daily values, daily
  statistics. <https://waterservices.usgs.gov>
- **NWS API** — hourly forecast (air, wind, cloud, precip-prob) and raw
  gridpoints (QPF in mm). <https://www.weather.gov/documentation/services-web-api>
- **NOAA NWPS** — National Water Prediction Service. Live gauge metadata,
  observed time series, and short-range streamflow forecasts (where
  available; small Driftless gauges typically have no NWPS LID).
  <https://water.noaa.gov>
- **Open-Meteo Historical Weather API** — daily-mean air temperature for any
  lat/lon. Used for degree-day backfill, anomaly baseline, and Mohseni
  inputs. <https://open-meteo.com>
- **iNaturalist API** — research-grade citizen-science observations. Used
  for DD-threshold calibration for selected species.
  <https://www.inaturalist.org/pages/api+reference>
- **GBIF API** — Global Biodiversity Information Facility aggregator.
  <https://api.gbif.org>
- **iDigBio** — Integrated Digitized Biocollections (museum specimens,
  typically 5–17× the volume of GBIF for our target species).
  <https://search.idigbio.org>
- **WI DNR ArcGIS REST** — Wisconsin trout-stream classifications and
  regulations. <https://dnrmaps.wi.gov/arcgis/rest/services/FM_Trout>

---

## Forecast verification

**`brier_1950`**
Brier, G. W. (1950). Verification of forecasts expressed in terms of
probability. *Monthly Weather Review*, 78(1), 1–3.
- Foundational paper for probabilistic-forecast scoring. The Brier score
  computed in `src/db/queries.py#reliability_diagram` is the mean squared
  error between predicted score and reported success (normalized to [0, 1]).

**`murphy_1973`**
Murphy, A. H. (1973). A new vector partition of the probability score.
*Journal of Applied Meteorology*, 12(4), 595–600.
- Decomposition of the Brier score into reliability + resolution +
  uncertainty. We currently report just the aggregate Brier; the
  decomposition is the next step once we have ≥100 catch-log entries.

**`wilks_2011`**
Wilks, D. S. (2011). *Statistical Methods in the Atmospheric Sciences*
(3rd ed.). Academic Press.
- Reference text for forecast verification methodology — reliability
  diagrams, calibration, sharpness, ROC curves. Chapter 8.

---

## Validation infrastructure (this project)

The validation methodology is encoded in code so it can be re-run on every
model change. There is no "trust the modeler" path here.

**`src/scripts/backtest.py`** — hindcast CLI. Replays USGS daily flow and
Open-Meteo daily air history through the model and compares to actual.
Outputs `data/calibration/backtest_report.md` and exit-code 0/1 so it can
be wired into CI as a regression guard. Thresholds:
- Flow recession MAPE @ 24h ≤ 25%
- Flow recession MAPE @ 72h ≤ 40%
- Flow recession MAPE @ 168h ≤ 60%
- Mohseni RMSE ≤ 6°F (Mohseni 1998 reports ~3-4°F)
- Mohseni bias ≤ ±3°F

Current production performance (180-day window, 9 own-gauge reaches):
- MAPE @ 24h: 12.9% (range 5.1% Kinni → 20.5% Upper Iowa)
- MAPE @ 168h: 24.9%
- Mohseni (Eau Galle sentinel, n=178): RMSE 3.30°F, bias −1.95°F

**`src/scripts/fit_recession.py`** — per-gauge tau fitting CLI. Replays the
last 730 days of above-median USGS daily-flow events, searches tau values for
the exponential recession model, and writes
`data/calibration/recession_fit.json`. A fit reaches production only when it
has at least 90 samples, improves weighted +24/+72/+168h MAPE by at least 3%
over the freestone/spring prior, and does not hit the edge of the search grid.
This keeps shaky or unbounded fits as diagnostics instead of silently changing
forecasts.

**`tests/test_acceptance.py`** — synthetic acceptance suite. 29 pinned
assertions about model components. Test names like
`test_temperature_51_was_the_bug_fix` lock specific historical regressions
so they cannot recur silently.

**`/reliability` API endpoint + Learn-tab widget** — once catch_log has
≥30 entries, surfaces a reliability diagram (predicted vs reported per
bin) and the aggregate Brier score. Below 30 entries it shows a
gathering-data placeholder explaining why we can't score the model yet.

---

## What's *not* yet documented (calibration roadmap)

These are the model components that currently use literature priors or
heuristics and would benefit from per-watershed empirical calibration:

1. **Event-level recession separation.** First-pass per-gauge tau fitting is
   in production for gauges that beat the class prior. The next step is
   lower-envelope/event separation using continuous hydrographs where data
   density supports it ([brutsaert_nieber_1977](#brutsaert_nieber_1977)).
2. **Per-reach Mohseni coefficients.** Currently class-level (two classes).
   With reach-specific paired air/water datasets we could fit per reach.
3. **Per-species DD thresholds.** Hex is well-calibrated (n=287);
   Hendrickson and several others are still on literature priors.
4. **Regime-classifier thresholds.** All angler-consensus right now;
   should be calibrated against accumulated trip reports
   (`catch_log` table) once we have ≥30 reports per regime per reach.
5. **Wind / cloud penalty curves.** Tuned against a single validated
   angler day. A few dozen more reports on the same reach type would
   let us fit defensible curves.

When any of these is calibrated, **add a calibration entry here** with
the dataset size, method, and fitted parameters — not a verbal note in
a commit message.
