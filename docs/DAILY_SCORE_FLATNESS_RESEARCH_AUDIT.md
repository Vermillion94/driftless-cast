# Daily Score Flatness Research Audit

Date: 2026-05-22

## Question

The app still looks too flat inside a day. Is that biologically wrong, and where does the model diverge from the best available stream ecology, aquatic entomology, and trout-behavior evidence?

## Bottom Line

Yes, the app is still too flat for the product promise. The current model is defensible as a "can you catch fish by nymphing?" model, but not yet sharp enough as a "find the best 2-3 hour window" model.

The strongest research-backed intraday signals are:

1. Diel invertebrate drift and trout activity, especially dusk/dawn and low-light periods.
2. Insect emergence/spinner-fall timing by taxon, water temperature, and light.
3. Diel water-temperature cycles, with smaller and lagged cycles in spring/hyporheic-influenced reaches.
4. Method-specific opportunity: nymphing can stay workable for many hours, while dry-fly/rise windows are much shorter.

The current app includes these ideas, but the displayed headline score is still often dominated by broad nymph suitability. On 2026-05-22 cached predictions, several spring-influenced reaches only vary about 0.07-0.12 in headline score across 24 hours even though their raw nymph score varies about 0.21-0.28. That is the flatness Ben is seeing.

## Evidence Review

### Trout Do Not Behave Like A Flat Daily Plateau

Brown trout activity is strongly diel, but not with one universal curve. Ovidio et al. radio-tracked brown trout at 10-minute intervals through 26 full-day cycles and found activity duration/intensity were mainly proportional to water temperature and day length. Trout were most active at dusk in all seasons; spring/summer became more homogeneous, but still with dusk predominance. The same abstract warns that individuals and microhabitats vary substantially.

Model implication: we should keep a baseline nymphing score available during favorable water, but headline "best window" should have stronger dusk/low-light separation and should avoid implying every hour is nearly equivalent.

Source: Ovidio, Baras, Goffaux, Giroux & Philippart, 2002, Hydrobiologia. https://orbi.uliege.be/handle/2268/5837

### Drift Is A Real Diel Food-Availability Signal

Waters' Annual Review of Entomology paper is still a canonical drift reference. It distinguishes constant, behavioral, and catastrophic drift, and describes diel periodicity as a recurrent 24-hour field pattern. Behavioral drift can occur in large quantities during consistent periods of the day.

Model implication: the current `diel_activity_factor` and `drift_window_bonus` are pointing the right way, but they are probably too gentle for a product whose promise is hourly window selection.

Source: Waters, 1972, The Drift of Stream Insects. https://www.ephemeroptera-galactica.com/pubs/pub_w/pubwaterst1972p253.pdf

### Feeding Timing Follows Drift, Temperature, And Season

A brown/rainbow trout feeding study summarized on PubMed reports that water temperature affected food consumption and meal frequency via gastric evacuation, while drift availability determined time of feeding. The key point is not "always dawn/dusk"; it is that feeding time is tied to prey drift and thermal physiology.

Model implication: we should model "subsurface reliability" separately from "activation/aggression." Comfortable water all day means the stream is fishable all day; it does not mean all hours deserve the same headline score.

Source: Elliott, 1973, Oecologia. https://pubmed.ncbi.nlm.nih.gov/28308235/

### Insect Emergence Is Species-Specific, Temperature-Driven, And Often Synchronized

Aquatic insect emergence phenology generally responds to water temperature, but responses differ by taxon. The Andrews Forest synthesis found caddis responded predictably earlier in warmer streams/years, while a mayfly and stonefly synchronized within years but shifted substantially earlier in warmer years.

Model implication: degree-day phenology is appropriate, but emergence windows need local/species calibration and should produce narrow surface windows, not a broad all-day dry signal.

Source: Andrews Forest Research Program, Stream Temperature and Insect Emergence. https://andrewsforest.oregonstate.edu/research/highlights/stream-temperature-and-insect-emergence

### Diel Water Temperature Matters, But Driftless Springs Can Mute It

Caissie 2006 reviews river thermal variability across diel, daily, seasonal, and spatial scales. Arrigoni et al. 2008 found hyporheic/spring-channel temperatures can have smaller diel ranges, compressed by 2-6 C, and phase offsets from the main channel.

Model implication: flat temperature opportunity on spring creeks can be real. Flat total fishing quality is not necessarily real, because light, drift, and hatch timing still vary.

Sources:
- Caissie, 2006, Freshwater Biology. https://doi.org/10.1111/j.1365-2427.2006.01597.x
- Arrigoni et al., 2008, Water Resources Research. https://digitalcommons.unl.edu/natrespapers/637/

## Current Model Alignment

Strong alignment:

- `temperature_score` uses a broad brown-trout active band rather than an overly narrow Gaussian. This is good for nymphing realism.
- `hour_of_day_score` now uses a low outside-window background for dries. That matches the idea that emergence/rise activity is short-lived.
- Solar altitude is calculated locally and used for a bright-sun dry penalty.
- `aggression_score` correctly separates baseline opportunity from activation.
- Pressure and flow-trend are labeled as weaker/heuristic signals instead of being treated as hard science.

Partial alignment:

- The nymph score has diel factors, but the factor range is small: 1.00 at prime hours, 0.96 shoulders, 0.91 ordinary, 0.86 midday, 0.82 late night.
- `drift_window_bonus` adds only 0.04-0.08 in the main dawn/dusk windows.
- The headline score compresses and caps nymph-only hours, but many spring-fed reaches still land in a narrow 0.72-0.80 band for most of the day.

Main divergence:

- The displayed score is still closer to "nymphing conditions are broadly fine" than "this is the best short biological window."
- Surface and aggression signals do not yet dominate the time series unless dry score exceeds the 0.15 significance threshold.
- The model lacks a true "method-specific daily curve": nymphing, dry-fly, streamer, terrestrial, and night/big-fish opportunities should not share one flat blended score.

## Cached Forecast Reality Check

Using local `driftless_cast.db` cached predictions on 2026-05-22:

- Whitewater North Fork Elba: headline 0.727-0.799, range 0.072.
- Trout Run Creek Chatfield: headline 0.727-0.799, range 0.072.
- Root Lanesboro: headline 0.712-0.799, range 0.088.
- Kinnickinnic River Falls: headline 0.686-0.799, range 0.113.
- Hay Creek Red Wing: headline 0.686-0.797, range 0.111.

Those are the exact reaches where the app will feel visually flat. Other reaches with stronger flow/temperature stress already show larger daily swings, often 0.40-0.48.

Interpretation: the model can produce intraday range, but when spring-creek nymphing is broadly good, the display collapses the day into a narrow band. That is biologically plausible for "you can catch fish," but weak for "which hours are best?"

## Recommended Next Work

1. Add separate displayed lanes for `baseline_nymph`, `surface_window`, and `activation/aggression`.
   - Keep headline score, but show why a 0.76 all-day nymph plateau is not the same as an 0.76 hatch window.

2. Recalibrate headline score around "best 2-3 hour window."
   - Baseline nymphing should probably sit lower visually, around good/okay, while activation and surface windows punch higher.

3. Strengthen the diel/low-light component for the headline and aggression model, not necessarily raw nymph score.
   - Keep raw nymph stable enough for "fishable," but let the user-facing score show stronger dawn/dusk contrast.

4. Add an entomology pass on species windows.
   - Split emergence, spinner fall, and egg-laying where relevant.
   - Treat caddis, mayflies, stoneflies, and terrestrials differently.

5. Add local calibration targets.
   - A small hand-labeled dataset of real Driftless reports with date, stream/reach, method, rise intensity, bugs, and time-of-day would be more valuable now than another generic multiplier.

## Recommendation

Do not blindly make the diel curve more dramatic everywhere. The research says flat-ish nymphing opportunity can be real on stable spring creeks, especially in comfortable water. The product problem is that the single headline score fails to communicate the difference between:

- "you can catch fish all day if you nymph well"
- "fish are especially vulnerable/active now"
- "surface feeding is likely now"

The next PR should therefore separate and display those dimensions, then tune the headline score to reward short, biologically meaningful activation windows.
