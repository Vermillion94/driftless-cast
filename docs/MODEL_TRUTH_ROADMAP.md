# Model Truth Roadmap

Driftless Cast should be able to answer: "Why is this score 72 and not 88?"
The answer must be inspectable, falsifiable, and honest about which parts are
science-backed versus heuristic.

## Product Promise

Find the highest-probability 2-3 hour fishing windows on Driftless trout
streams, and explain the biological and physical reasons behind each call.

## Score Dimensions

- **Headline score**: calibrated trip-planning score shown on the map.
- **Subsurface reliability**: water temperature, flow percentile, flow trend,
  drift timing, and pressure.
- **Surface activity**: hatch readiness, emergence hour, cloud/wind match, sun
  angle, water temperature ethics, and pressure.
- **Aggression potential**: whether independent signals are changing in the
  same direction: falling/stabilizing flow, falling pressure, warming/cooling
  trend into preferred thermal band, and low-light/cloud cover.
- **Confidence**: measured-vs-estimated water temp, proxy gauge distance,
  availability of flow percentiles, forecast lead time, and whether the signal
  is literature-backed or heuristic.

## Evidence Standard

Every quantitative multiplier belongs to exactly one category:

- **Peer-reviewed or agency-backed**: trout thermal limits, Mohseni water-temp
  model, hydrologic recession form, daily flow percentiles, degree-day
  phenology, drift timing.
- **Reference-text / expert practice**: emergence-hour windows, fly pattern
  recommendations, broad hatch behavior.
- **Guide-derived heuristic**: wind penalty, pressure multipliers, regime
  thresholds, clear-water sun penalty, top-end headline calibration.
- **Local calibration**: per-reach residuals and future trip-report tuning.

Heuristics are allowed, but they must be labeled and testable.

## Near-Term Model Work

1. Keep splitting "one score" into explicit dimensions:
   headline, nymph, surface, aggression, and confidence.
2. Validate the first aggression and confidence models against local reports; aggression currently
   rewards changing conditions, not merely comfortable conditions, but its
   weights are heuristic, while confidence is an input-quality score.
3. Fit per-gauge recession constants from USGS history using Brutsaert-Nieber
   lower-envelope methods instead of class priors.
4. Backfill a manually labeled trip/outfitter report dataset for the Driftless:
   date, stream/area, rise intensity, aggression, method, bugs, rough success.
5. Run calibration checks by season and regime so "May sulphur window" errors
   do not get hidden inside annual averages.
6. Surface the full audit trail in the UI: inputs, multipliers, references,
   and which assumptions are heuristic.

## Paid-Product Bar

The tool becomes worth $5-10/month when it repeatedly does three things better
than a generic weather app or fly-shop report:

- Chooses the best short window, not just the best day.
- Warns when a high-looking weather day is likely to fish soft.
- Explains exactly what signal would need to change for the call to improve.
