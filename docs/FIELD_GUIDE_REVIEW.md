# Field Guide Review — October 2026

A full review of how Driftless Cast decides *when, where, how and why* to fish,
checked against stream hydrology, trout thermal biology, Driftless entomology
and a season of Driftless guide reports. It covers what was wrong, what changed
on the `field-guide-overhaul` branch, and what is still open.

The goal was this: an angler should be able to roll up to a stream on any day
of the year and know what is hatching, what conditions will do to the fish, and
how to make the most of it. When the honest answer is "skip today, go
tomorrow", the app should say that and say why.

---

## 1. How the model works (one-paragraph version)

Every hour for 7 days, per reach, `forecast_builder._score_hour` combines:
- **water temperature**: measured, or the Mohseni air→water estimate plus a
  diurnal swing;
- **flow percentile** against the day-of-year history, with exponential
  recession and a rain-driven bump;
- **hatch probability** per species: seasonal calendar × degree-day readiness ×
  weather match × emergence-hour window;
- **light** (sun altitude), **wind** and **pressure**.

These produce a **nymph** score and a **dry** score. A **regime** classifier
labels the kind of day it is. At read time, `score_calibration.headline_score`
turns nymph, dry and regime into the 0–100 headline. New in this branch,
`tactics.py` and `trip_verdict.py` turn the hourly rows into *how/where to
fish* and a *day-level go / skip call*.

---

## 2. What was wrong

### 2.1 The app said "Skip" for the whole Driftless winter
- `temperature_score` returned **0 below 42 °F**. Driftless creeks average
  **34–38 °F from December to February**. That figure comes from USGS daily
  records on Joos Valley, Eagle, Nederlo and Traverse Valley creeks, which the
  project had never used.
- With the temperature score at zero, every winter hour scored near zero.
- The real-world check says otherwise. The SE MN winter creel (MN DNR, Jan–Mar
  2013) measured about **1.4 trout per angler-hour**. Elliott's brown trout
  bioenergetics show feeding down to 0–1 °C. Cold water is a *slow* bite, not
  *no* bite.

### 2.2 Winter time-of-day was backwards
- The diel factor favoured dawn and dusk and damped 11am–3pm all year, so the
  model's "best" January hour was 7pm.
- Every Driftless winter source says the opposite: fish 10am–2pm as the water
  warms (Driftless Angler daily reports; Twin Cities TU).

### 2.3 Winter hatches could not exist
- A single **45 °F** hatch gate zeroed early black stoneflies. Those emerge
  in high-30s water.
- **Midges**, the winter staple, were not in the species table at all.

### 2.4 The spring-creek water-temperature curve wasn't fitted on spring creeks
- The production "spring" Mohseni curve fell back to a "mixed" fit from the
  Raccoon and Des Moines rivers, Black Earth Creek and others. The region-wide
  fit is dominated by large and urban rivers (Fox, Milwaukee, St. Louis,
  Minneapolis creeks).
- USGS holds years of daily water temperature on real Driftless trout streams
  that the project had never used. Refit on those records
  (`src/scripts/refit_mohseni_driftless.py`), the spring-fed sites (Traverse
  Valley S trib, N Fork Nederlo) come in at **2.1–2.3 °F RMSE vs 4.3–6.2 °F**
  for the production curve.
- The production curve runs **about 8 °F too warm in July** on genuinely
  spring-fed headwaters: a summer ceiling of 68 °F against a measured 60 °F.
- **The fix is deliberately narrow.** On the last 90 days of live data at the
  Eau Galle, Stillwell and Silver Creek sentinels, the new Driftless "mixed"
  and "freestone" fits ran 1.4–3.7 °F cool and lost to the existing curves.
  They were fit mostly on 1970s–2008 records, and the water may simply be
  warmer now. So only the spring curve was adopted, and only for 8 short
  (≤12 km), strongly spring-fed headwaters:
  - Trout Run
  - Garvin Brook
  - the Whitewater North and South Forks
  - Pine Creek
  - Beaver Creek
  - Hay Creek
  - Bishop Branch

  Every other reach keeps its previous curve. The backtest (CI) now validates
  on both spring-influenced sentinels, not just Eau Galle.

### 2.5 No legality, no spawning, no lightning, no dangerous cold
- There was no season logic. On **Oct 19 2026 the live site ranked a closed
  Wisconsin stream (Rush River) as its #1 best window**.
- MN closes Oct 16–Dec 31. WI closes Oct 16 until the first Saturday in
  January, and as of 2026 its harvest opener moved to the first Saturday in
  April. Iowa never closes.
- There was no spawning advisory, no lightning stop, no ice-in-the-guides stop
  and no high-wind stop.

### 2.6 Species table drifted from the Driftless
- **Hex and Isonychia** scored as major hatches. Neither is on the MN DNR SE
  hatch chart. Hex needs silt and marl beds plus 66–68 °F water that spring
  creeks rarely reach.
- **Hendrickson** carried full weight. Coulee Region TU (Freund) says it has
  declined, and Driftless Angler reported none in three Aprils.
- **"Grannom (Mother's Day caddis)"** was modelled as a dusk caddis. The
  Driftless grannom is *Brachycentrus americanus*, the "black" or "Coulee"
  caddis. It hatches mid-April, **11am–3pm, best on sunny days**. "Mother's
  Day" is the western species.
- **Fall BWOs** used the spring midday window. Fall olives come in the *last
  hours of daylight*.
- Missing entirely: midges, craneflies (*Antocha*), March Brown / Light Cahill,
  autumn sedge (*Neophylax*) and summer tiny olives.

### 2.7 Terrestrials and pressure were overweighted
- Summer middays scored about 99/100 on ants and hoppers.
  - In Valley Creek, MN, terrestrials were **~3 % of drift and ~10 % of trout
    diet**, against ~75 % for *Gammarus* scuds (USGS, Laudon et al. 2005).
  - Guides call bright summer middays on low, clear water the hardest fishing
    of the year.
- Barometric pressure moved scores by up to −18 % and carried 20 % of the
  aggression weight.
  - Stoner's (2004) catchability review found no controlled evidence for a
    pressure effect.
  - Temperature, light, current and prey dominate catchability instead.

### 2.8 Muddy water could still score "worth it"
- Dry and hatch scores ignored clarity, so a fall-BWO probability could carry a
  muddy-water day to a 0.62 headline.
- Turbidity halves trout reactive distance at about 10 NTU (Barrett et al.
  1992).

### 2.9 Operations
- **The forecast silently goes stale.** The hourly GitHub cron that keeps the
  Fly app warm was auto-disabled by GitHub for repo inactivity
  (`disabled_inactivity`, last run Aug 19). On Oct 19 `/status` reported data
  computed Oct 10 until a visit woke the machine. **Fixed:** `fly.toml` now
  keeps one machine running (`min_machines_running = 1`, about $2/month) so
  the in-process hourly rebuild never sleeps, and the dead cron workflow is
  removed.
- **`main` still contains the change that caused the June outage.** PR #38
  merged; the revert, PR #39, has been open since Jun 20. This branch is cut
  from the revert branch, so merging it into `main` also lands the revert.

---

## 3. What the research says (condensed)

Full source lists live in `docs/REFERENCES.md` (section "Field-guide layer").
Confidence: **S** = strong (peer-reviewed or agency), **M** = moderate
(limited studies or broad guide consensus), **F** = folklore.

| Factor | What to do with it | Conf. |
|---|---|---|
| Water temp (brown trout) | 0.25 at 32 °F → 1.0 at 52–62 °F → 0.55 at 68 °F → 0 at 73 °F. Warn at 66 °F (Keep Fish Wet brown-trout threshold); hard stop at 73 °F (MT FWP hoot-owl line). | S |
| Cold-season timing | Below about 48 °F water the 10am–3pm warm-up is the window; sun is a plus | M |
| Warm-season timing | Dawn and dusk drift peaks (Waters 1972; Elliott 1970); bright midday sun suppresses rises on clear water | S / M |
| Clarity | MN DNR scale: clear ≥2 ft, off-color 6 in–2 ft, muddy <6 in ("unfishable for most"). Light stain helps nymphs and streamers; muddy kills everything. Spring-fed headwaters clear first, often within 24–48 h. | M |
| Rising water | A short catastrophic-drift pulse in the first 2–6 h (Kennedy et al. 2014: Gammarus drift rises 10× on a doubling of flow) | S |
| Pressure | No controlled effect. Weight it about 0; its real signal is cloud, front and rain, which are scored separately. | F |
| Wind | 5–12 mph helps on clear water; 15–20 mph hurts casting; above 25 mph is futile. Coulees are sheltered. | M |
| Lightning | Thunder in the forecast hour → off the water; wait 30 min after the last thunder (NWS) | S |
| Cold hazard | Air below about 20 °F with wind, or below 10 °F: guides ice up; skip | M |
| Terrestrials | Real, but a minor share of diet on Driftless meadow streams | S (local) |
| Spawning | Brook trout late Sep–Oct, browns Oct–Nov (MN peak ~Oct 13–Nov 14); stay off redds | S |
| Moon / solunar | No support for stream trout; weight 0 | F |

**Hatch calendar.** Primary source is the MN DNR SE MN chart, cross-checked
against Driftless Angler field dates for 2024–26. See
`src/models/seasonal.py` and `data/seed/species.json` (`_driftless_note`).

| Period | Main food sources |
|---|---|
| Jan–Apr | Midges and winter black stones; the first BWOs about Mar 13–21 |
| Mid/late Apr | Grannom (black caddis), 11am–3pm; Hendricksons (sparse) |
| Late Apr–Jul | Craneflies |
| Mid-May–early Jul | Sulphurs; evenings by June |
| May–Jul | Tan caddis (*Hydropsyche*), morning and late evening |
| Late Jul–Sep | Tricos at dawn; hoppers, ants and beetles on the banks |
| Mid-Sep–Oct | Fall BWOs, late afternoon; autumn sedge |
| Year-round | Scuds, sowbugs, midges and leeches |

---

## 4. What changed (this branch)

| Area | Change | Where |
|---|---|---|
| Legality | MN / WI / IA season status (with WI's 2026 April opener), next-change date, reg links, spawning advisory. CLOSED regime caps the score at 0. | `src/models/regulations.py`, `regime.py` |
| Hard stops | LIGHTNING (thunder + PoP ≥30 %), HAZARD (air <10 °F, or <20 °F with wind >15 mph, or wind >25 mph), lethal water ≥73 °F at any hour. One cap table. | `regime.py`, `score_calibration.REGIME_CAPS` |
| Temperature biology | Knot-based curve with a cold floor; tests updated with rationale | `nymph_score.temperature_score` |
| Diel rhythm | Blends a cold-water midday curve with the warm-water dawn/dusk curve by water temperature; the drift bonus fades in cold water | `nymph_score.diel_activity_factor` |
| Sun | Season-aware: a penalty in warm, clear water; a small nymph bonus in cold water | `solar.seasonal_sun_factors` |
| Wind | General dry and nymph casting factor with coulee shelter | `forecast_builder._wind_factors` |
| Pressure | Multiplier cut to −5 %…+3 %; aggression weight cut from 20 % to 5 % | `forecast_builder`, `score_calibration` |
| Clarity | Clarity class from flow percentile and runoff risk; surface factor 1.0 / 0.8 / 0.45 / 0.2 | `tactics.estimate_clarity`, `forecast_builder` |
| Species | 5 new taxa; Driftless abundance weights; per-species minimum water temperature; corrected grannom, fall-BWO and sulphur timing; midge year-round floor | `species.json`, `seasonal.py`, `dry_score` |
| Terrestrials | Weighted 0.6 relative to aquatic hatches | `forecast_builder.TERRESTRIAL_SURFACE_WEIGHT` |
| Water temp | Driftless-fitted spring Mohseni curve for 8 short, strongly spring-fed headwaters; hand-tuned mean offsets off for those reaches only; two Driftless sentinels added to the CI backtest | `scripts/refit_mohseni_driftless.py`, `temp_estimator`, `thermal_profile`, `scripts/backtest.py` |
| How / where | Technique, rig, water types, flies, approach and watershed position from season × clarity × time × weather × hatch | `src/models/tactics.py` |
| Should I go? | Per-day verdict (Go / Worth it / Slow / Skip / Closed) from the best 3-h window, with reasons and "go Tuesday 4–6pm instead" | `src/models/trip_verdict.py` |
| API | `/forecast` gains `season`, `days` and per-hour `tactics`; `/reaches` gains `season`; new `/near?lat&lon` that ranks nearby open water with today's and tomorrow's call; closed reaches are dropped from best and hatch windows | `src/api/routes.py` |
| UI | "Where should I fish near me?" button; season and spawning banner; "Should I go?" 7-day strip; "How & where to fish" card; new Learn topic | `web/` |
| Tests | 113 passing, up from 86. Old tests that pinned the 0-below-42 °F behaviour were rewritten with their rationale. | `tests/test_field_guide.py` and others |

### Before → after on a synthetic year (spring creek, typical monthly weather)

| Hour | Before | After |
|---|---|---|
| Jan 15, 1pm | 0.00 (Skip) | 0.64; slow, deep midge and scud nymphing near springs, watch for midges on top |
| Jan 15, 7pm | 0.08 (the model's "best" winter hour) | 0.17 |
| Feb 15, 1pm | 0.00 | 0.65; early black stones show up |
| Mar 20, 1pm | 0.02 | 0.68; spring BWOs |
| Apr 30, 1pm | Hendrickson-led | Grannom / black caddis-led, 11am–3pm |
| Jun 28, 7pm | 0.90, led by Isonychia | 0.74, led by tan caddis and Cahills |
| Aug 10, 1pm | 0.99 ("hopper hatch") | 0.66; hopper-dropper in the shade |
| Aug 10, 8am | 0.74 | 0.73 (Trico) |
| Oct 10 | fall BWO window 11am–3pm | fall BWO window 2–6pm, plus autumn sedge |

### Live check (Oct 19 2026 build, all 39 reaches)
- MN and WI reaches read **Closed** with their reopening dates.
- The Upper Iowa and Yellow rivers rank as the open options. Both carry a
  spawning advisory and stained-water streamer tactics.
- `/near` from Decorah lists the open Iowa water first.

---

## 5. Still open (prioritised)

1. **Stream-specific regulations.**
   - Some MN state-park and city reaches, and some Iowa streams, have their own
     rules (e.g. South Pine Creek brook trout are C&R only).
   - `season_override` is wired in the reach seed but intentionally unset until
     each stream is verified.
2. **Use measured water temperature where it exists.** The active Stillwell,
   Silver, La Crosse and Turkey gauges could anchor nearby reaches and
   validate the Driftless fit live.
3. **Rising-water drift pulse.** Add a 2–6 h nymph bonus on the first rise
   while clarity is still ≥12 in. Needs hourly IV flow history, not the daily
   values used today.
4. **Post-front bluebird rule.** In spring, apply a one-tier downgrade 24–48 h
   after a ≥10–15 °F drop in the air high with clear sky. Halve it near
   springs.
5. **Season-dependent emergence hours.**
   - Sulphurs: mid-afternoon in May, evening by June.
   - Tricos: tied to air crossing 68 °F, drifting later through August.
6. **Brook-trout reaches.** Shift the warm-side breakpoints about 3 °F lower
   once reach species composition is in the seed.
7. **Calibrate against trips.**
   - The Driftless Angler daily-report archive (`driftlessangler.com/blog/page/N/`)
     ties each day's conditions to its tactics. It is the cheapest labelled
     dataset available for checking clarity, verdict and tactics calls.
   - Your own trip reports remain the ground truth.
