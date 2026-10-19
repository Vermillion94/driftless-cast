"""How and where to fish an hour — technique, rig, water, flies, approach.

The scores answer "how good?"; the regime answers "what kind of day?". This
module answers what an angler standing at the stream actually needs: what to
tie on, which water to fish, and how to approach it.

Rules are distilled from a season of Driftless guide reports and agency
guidance (see docs/FIELD_GUIDE_REVIEW.md §Tactics for the full source table):
  * Driftless Angler (Viroqua) daily reports, 2025–26 — conditions → tactics
  * MN DNR Lanesboro stream-conditions clarity scale (clear ≥2 ft, "off"
    6 in–2 ft, muddy <6 in)
  * Coulee Region TU "Seasons of the Driftless"; Trout Fool winter guide;
    TCTU winter tips; Midwest Outdoors "Nymphing Trout in Dirty Water";
    Field & Stream (Merwin) on high dirty water; TU (Mark Taylor) terrestrials.

Everything here is guide-derived practice, not measured biology, and is
labelled as such in the UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional

# ── Clarity ───────────────────────────────────────────────────────────────

CLEAR_LOW = "low_clear"
CLEAR = "clear"
OFF = "off"            # light stain, 12–24 in visibility — often the best day
STAINED = "stained"    # 6–12 in
MUDDY = "muddy"        # < 6 in — unfishable for most anglers

CLARITY_LABELS = {
    CLEAR_LOW: "low & clear",
    CLEAR: "clear",
    OFF: "slightly off-color",
    STAINED: "stained",
    MUDDY: "muddy",
}


# Surface-feeding multiplier by clarity. Turbidity halves trout reactive
# distance by ~10 NTU (Barrett et al. 1992; Sweka & Hartman 2001) and rising,
# coloring water pulls fish down and to the edges, so dries suffer first.
# Nymphing already takes its high-flow penalty via flow_percentile_score.
SURFACE_CLARITY_FACTOR = {
    "low_clear": 1.0,
    "clear": 1.0,
    "off": 0.8,
    "stained": 0.45,
    "muddy": 0.2,
}


def estimate_clarity(flow_percentile: Optional[float], runoff_risk_level: Optional[str]) -> str:
    """Rough clarity class from flow percentile + forecast-rain runoff risk.

    There are no turbidity sensors on Driftless trout water, so this is a
    proxy: Driftless runoff off ag fields colors water fast, and colour tracks
    flow above the seasonal median closely enough for a 5-way class.
    """
    pct = 0.5 if flow_percentile is None else float(flow_percentile)
    level = runoff_risk_level or "low"
    if level == "blowout" or pct >= 0.93:
        return MUDDY
    if level in {"high", "hurt"} or pct >= 0.82:
        return STAINED
    if level == "watch" or pct >= 0.68:
        return OFF
    if pct < 0.28:
        return CLEAR_LOW
    return CLEAR


# ── Season buckets ───────────────────────────────────────────────────────

WINTER = "winter"
SPRING = "spring"
EARLY_SUMMER = "early_summer"
SUMMER = "summer"
FALL = "fall"


def season_bucket(on: datetime, water_temp_f: Optional[float]) -> str:
    m, d = on.month, on.day
    if m in (12, 1, 2) or (m == 3 and d < 15):
        return WINTER
    if m in (3, 4) or (m == 5 and d < 15):
        return SPRING
    if m in (5, 6):
        return EARLY_SUMMER
    if m in (7, 8):
        return SUMMER
    if m == 11 and water_temp_f is not None and water_temp_f < 44:
        return WINTER
    return FALL


def time_bucket(hour: int) -> str:
    if hour < 9:
        return "morning"
    if hour < 15:
        return "midday"
    if hour < 19:
        return "afternoon"
    return "evening"


# ── Output ───────────────────────────────────────────────────────────────

@dataclass
class Tactics:
    method: str                       # headline technique
    rig: str                          # leader/tippet/indicator detail
    water: List[str] = field(default_factory=list)      # where in the stream
    flies: List[str] = field(default_factory=list)      # ordered picks
    approach: List[str] = field(default_factory=list)   # how to fish it
    where: Optional[str] = None       # which part of the watershed
    clarity: str = CLEAR
    season: str = SPRING
    evidence: str = "guide-derived practice"

    def to_dict(self) -> Dict[str, object]:
        d = asdict(self)
        d["clarity_label"] = CLARITY_LABELS.get(self.clarity, self.clarity)
        return d


def _dedupe(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for it in items:
        key = it.lower()
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


def _hatch_flies(top_species: Optional[Dict[str, object]], flies: Optional[Dict[str, object]]) -> List[str]:
    out: List[str] = []
    if not flies:
        return out
    for slot in ("primary", "dropper"):
        f = flies.get(slot) if isinstance(flies, dict) else None
        if isinstance(f, dict) and f.get("pattern"):
            size = f.get("size")
            out.append(f"{f['pattern']} #{size}" if size else str(f["pattern"]))
    return out


def recommend_tactics(
    *,
    valid_at: datetime,
    water_temp_f: Optional[float],
    flow_percentile: Optional[float],
    runoff_risk_level: Optional[str],
    cloud_cover: Optional[float],
    wind_mph: Optional[float],
    air_temp_f: Optional[float],
    regime_code: Optional[str],
    top_species: Optional[Dict[str, object]] = None,
    flies: Optional[Dict[str, object]] = None,
    spring_influenced: bool = True,
    surface_signal: float = 0.0,
) -> Tactics:
    """Pick a technique + water + flies for one hour. First matching rule wins."""
    clarity = estimate_clarity(flow_percentile, runoff_risk_level)
    season = season_bucket(valid_at, water_temp_f)
    hour = valid_at.hour
    tod = time_bucket(hour)
    cloudy = cloud_cover is not None and cloud_cover >= 0.65
    bright = cloud_cover is not None and cloud_cover <= 0.3 and 10 <= hour <= 16
    windy = wind_mph is not None and wind_mph >= 15
    cold_water = water_temp_f is not None and water_temp_f < 45
    warm_water = water_temp_f is not None and water_temp_f >= 65
    hatch_flies = _hatch_flies(top_species, flies)
    hatch_name = (top_species or {}).get("common_name") if top_species else None
    is_terrestrial = bool((top_species or {}).get("is_terrestrial"))

    def t(**kw) -> Tactics:
        kw.setdefault("clarity", clarity)
        kw.setdefault("season", season)
        kw["flies"] = _dedupe(kw.get("flies", []))
        return Tactics(**kw)

    # ── Hard stops: still tell the angler something useful ──────────────
    if regime_code == "CLOSED":
        return t(method="Season closed", rig="—",
                 approach=["Iowa trout streams are open year-round — NE Iowa spring creeks are the winter play",
                           "Use the time to tie scuds, Pink Squirrels and midges for the opener"],
                 where="an open-season stream")
    if regime_code == "LIGHTNING":
        return t(method="Off the water", rig="—",
                 approach=["Wait 30 minutes after the last thunder before wading back in",
                           "The first rise as the water colors after the storm can be the best big-fish window of the day"])
    if regime_code == "HAZARD":
        return t(method="Wait for a warmer, calmer window", rig="—",
                 approach=["Below ~20°F air the guides ice up in minutes",
                           "The 10am–2pm warm-up on a 25°F+ day is the realistic winter window"])
    if regime_code == "HEAT_STRESS" or (water_temp_f is not None and water_temp_f >= 68):
        return t(method="Rest the fish", rig="—",
                 approach=["Water is past the catch-and-release safety line — fish dawn until ~10am only, land fish fast, keep them wet",
                           "Or go to a small, cold, spring-fed tributary"],
                 where="small spring-fed headwater creeks (coldest water in the system)")

    # ── Muddy: skip the main stem, find clear water ───────────────────────
    if clarity == MUDDY:
        return t(method="Find clearer water",
                 rig="0X–2X, heavy tungsten or split shot if you fish it at all",
                 water=["soft inside seams and slow edges", "tail of the slowest pools"],
                 flies=["Squirmy / San Juan Worm #10–12", "black or olive leech", "Woolly Bugger #6–8"],
                 approach=["Muddy (<6 in visibility) is unfishable for most anglers",
                           "Small spring-fed headwaters clear first — often within 24–48 h — scout by driving valley to valley"],
                 where="short spring-fed headwater tributaries; skip lower main stems for 2–5 days")

    # ── Stained / off-color: worm, leech, streamer; big-fish window ───────
    if clarity in (STAINED, OFF) and not (surface_signal >= 0.30 and clarity == OFF):
        streamer = season in (SPRING, FALL) or (water_temp_f is not None and 48 <= water_temp_f <= 65)
        flies_ = ["Squirmy Worm #12", "micro leech", "orange or tan scud #14", "Pink Squirrel #14"]
        if streamer:
            flies_ = ["olive or black sculpin / articulated streamer 2–4 in", "Woolly Bugger #6–8"] + flies_
        approach = ["Stained water lowers trout wariness — big browns feed more boldly",
                    "Fish heavier, thicker, flashier patterns than on clear days"]
        if streamer:
            approach.append("Strip-pause streamers tight to banks; go deeper and pause longer once the sun comes out")
        if cold_water:
            approach.append("Below 50°F slow everything down — dead-drift or crawl it along the bottom")
        return t(method="Streamer or worm-and-leech nymphing" if streamer else "Worm & leech nymphing",
                 rig="3X–4X, indicator or tight-line, weight to tick bottom",
                 water=["soft inside seams", "undercut banks and current edges", "heads of riffles and slots"],
                 flies=flies_, approach=approach,
                 where="the lower and middle main stem — 'read the high water as a new river'" if clarity == OFF
                       else "middle reaches as they clear; headwaters if the main stem is still coffee-colored")

    # ── Active aquatic hatch ──────────────────────────────────────────────
    # Winter midges and stones are eaten mostly subsurface; a modest surface
    # signal shouldn't flip the advice to dries (guides nymph first, then look up).
    winter_micro = (
        (season == WINTER or cold_water)
        and str((top_species or {}).get("id") or "") in {"midge", "early-black-stone"}
        and surface_signal < 0.5
    )
    if surface_signal >= 0.30 and hatch_name and not is_terrestrial and not winter_micro:
        approach = [f"{hatch_name} window — watch for rises before you commit to dries",
                    "Fish the emerger in the film first; switch to the dun when you see fish taking off the top",
                    "Even during hatches many fish keep eating subsurface — a dropper is cheap insurance"]
        if bright:
            approach.append("Bright sun: work shaded banks and riffle seams; fish are spooky on flat water")
        return t(method=f"Match the hatch — {hatch_name}",
                 rig="9–12 ft leader to 5X–6X; dry-dropper with a small emerger or soft hackle",
                 water=["riffle tailouts and seams where duns collect", "slick water below riffles", "foam lines and eddies"],
                 flies=hatch_flies + ["Partridge & Orange soft hackle #16", "Pheasant Tail #16–18"],
                 approach=approach,
                 where="meadow / Class I reaches with long, smooth glides")

    # ── Winter ───────────────────────────────────────────────────────────
    if season == WINTER or cold_water:
        approach = ["Fish slow and deep — trout are holding, not chasing",
                    "Best window is 10am–3pm as the water warms a degree or two; sunny days fish best"]
        if tod in ("morning", "evening"):
            approach.insert(0, "Early and late are the coldest water of the day — expect a slow bite until midday")
        if not cloudy and tod == "midday":
            approach.append("Sun + calm: look for midges and winter stones on the surface; a Griffith's Gnat can work")
        return t(method="Slow, deep nymphing (midge & scud)",
                 rig="small indicator or tight-line, 6X–7X fluorocarbon, two-fly rig",
                 water=["deep, slow pools and the slow water just below riffles", "pool tailouts",
                        "just downstream of springs and seeps (bright-green watercress = spring water)"],
                 flies=["Zebra Midge #18–22", "tan or olive scud #14–18", "Pink Squirrel #14–16 (jig)",
                        "Rainbow Warrior #18–20", "black winter stone #16–18", "Griffith's Gnat #20 (if midges are on top)"],
                 approach=approach,
                 where="spring-fed reaches — the water nearest the springs is the warmest in winter")

    # ── Summer heat / bright low-clear ──────────────────────────────────
    if season == SUMMER:
        if tod == "morning":
            return t(method="Dawn tricos & midges, then terrestrials",
                     rig="12 ft leader to 6X–7X for tricos; 3X–4X hopper-dropper later",
                     water=["flat glides and pool tails for trico spinners", "riffles for the dropper"],
                     flies=hatch_flies + ["Trico spinner #22–24", "Griffith's Gnat #20", "thin perdigon #16–18"],
                     approach=["Spinner fall is short — be in position at first light",
                               "Water is coolest now; the bite usually fades by 10–11am"],
                     where="small, cold spring creeks — larger creeks can run too warm by midday")
        if tod == "evening":
            return t(method="Evening dries & caddis",
                     rig="9 ft leader to 5X",
                     water=["riffles and riffle tails", "undercut banks as the light goes"],
                     flies=hatch_flies + ["Elk Hair Caddis #14–16 (skitter it)", "Parachute Adams #12–14", "mouse after dark (big browns)"],
                     approach=["Last two hours of light are the window", "After dark: swing a mouse across flat glides for the biggest browns (new moon best)"])
        return t(method="Hopper-dropper along the banks",
                 rig="7.5–9 ft leader to 3X–4X to turn over foam and pull out of grass",
                 water=["undercut banks and overhanging grass", "shade lines", "riffles (beetle + bead-head)"],
                 flies=["hopper #10–14 (Pink Morris / Chubby)", "foam ground beetle #12–14", "black ant #16",
                        "Pheasant Tail / Spanish Bullet #16 dropper", "micro leech dropper"],
                 approach=["Midday sun on low, clear water is the hardest fishing of the year — shade is key",
                           "Slap the hopper tight to the bank; if it's going to eat, it eats on the first drift — move on",
                           "Skip terrestrials when the grass is still wet from rain"] +
                          (["Cloud cover in summer can make the whole day fish well"] if cloudy else []),
                 where="cold spring-fed tributaries and shaded reaches")

    # ── Default by season: dry-dropper / nymphing ────────────────────────
    clear_low = clarity == CLEAR_LOW
    thin = "thin, dark perdigon #16–18" if (tod == "morning" or clear_low) else "flashier bead-head (Pearl Jam, Rock Grinder) #14–16"
    flies_ = hatch_flies + [thin, "Gray Coulee / tan scud #14–16", "Pink Squirrel #14–16", "Pheasant Tail #16–18"]
    approach = []
    if clear_low:
        approach += ["Low clear water: stay low, keep your shadow off the water, approach from downstream",
                     "Lengthen the leader and go down a tippet size"]
    if bright and season in (EARLY_SUMMER, FALL):
        approach.append("Bright sun puts fish in a 'foul mood' — keep flies tight to the banks and structure")
    if cloudy and season in (SPRING, FALL):
        approach.append("Overcast is BWO weather — carry #18–20 olive emergers and watch the soft seams")
    if season == EARLY_SUMMER and tod == "evening":
        approach.append("Sulphur and Cahill duns/spinners come in the last two hours of light")
    if season == FALL:
        approach.append("Spawning season: stay off gravel redds in the tailouts and leave paired fish alone")
    if windy:
        approach.append("Wind: shorten the leader, fish a heavier dropper, and pick sheltered coulee bends")
    if tod == "afternoon" and season in (SPRING, EARLY_SUMMER):
        flies_.append("caddis pupa #16 / skittered Elk Hair Caddis")
    return t(method="Dry-dropper or tight-line nymphing",
             rig="9–10 ft leader to 5X; small foam indicator or a buoyant dry over a bead-head",
             water=["riffles and the drop-off just below them", "seams and undercut banks", "pool heads"],
             flies=flies_, approach=approach or ["Cover water — fish the riffle-to-pool transitions upstream"],
             where="meadow and Class I spring-creek reaches")
