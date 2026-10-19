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

    # Copy style: field-guide terse. Method ≤ 4 words, tips are short
    # imperatives, at most three; the "why" lives in the Learn tab.

    # ── Hard stops: still tell the angler something useful ──────────────
    if regime_code == "CLOSED":
        return t(method="Season closed", rig="—",
                 approach=["Iowa streams stay open all winter", "Tie scuds and midges for the opener"],
                 where="an open-season stream")
    if regime_code == "LIGHTNING":
        return t(method="Off the water", rig="—",
                 approach=["Wait 30 min after the last thunder", "The first rise after a storm can fish well"])
    if regime_code == "HAZARD":
        return t(method="Too cold or windy", rig="—",
                 approach=["Guides ice up below ~20°F", "Aim for a 25°F+ day, 10am–2pm"])
    if regime_code == "HEAT_STRESS" or (water_temp_f is not None and water_temp_f >= 68):
        return t(method="Rest the fish", rig="—",
                 approach=["Fish dawn to 10am only", "Land fish fast, keep them wet"],
                 where="small spring-fed headwaters")

    # ── Muddy: skip the main stem, find clear water ───────────────────────
    if clarity == MUDDY:
        return t(method="Find clearer water",
                 rig="0X–2X, heavy weight",
                 water=["Soft inside edges", "Slow pool tails"],
                 flies=["San Juan Worm #10–12", "Black leech", "Woolly Bugger #6–8"],
                 approach=["Mainstems need 2–5 days to clear", "Small spring creeks clear in 24–48 h"],
                 where="short spring-fed headwater tributaries")

    # ── Stained / off-color: worm, leech, streamer; big-fish window ───────
    if clarity in (STAINED, OFF) and not (surface_signal >= 0.30 and clarity == OFF):
        streamer = season in (SPRING, FALL) or (water_temp_f is not None and 48 <= water_temp_f <= 65)
        flies_ = ["Squirmy Worm #12", "Micro leech", "Tan scud #14", "Pink Squirrel #14"]
        if streamer:
            flies_ = ["Sculpin streamer 2–4\"", "Woolly Bugger #6–8"] + flies_
        approach = ["Go bigger and flashier than usual"]
        if streamer:
            approach.append("Strip-pause tight to the banks")
        approach.append("Slow it down below 50°F" if cold_water else "Big browns feed boldly in stain")
        return t(method="Streamers & worms" if streamer else "Worm & leech nymphing",
                 rig="3X–4X, weight to tick bottom",
                 water=["Soft inside seams", "Undercut banks", "Heads of riffles"],
                 flies=flies_, approach=approach,
                 where="main stem" if clarity == OFF else "middle reaches as they clear")

    # ── Active aquatic hatch ──────────────────────────────────────────────
    # Winter midges and stones are eaten mostly subsurface; a modest surface
    # signal shouldn't flip the advice to dries (guides nymph first, then look up).
    winter_micro = (
        (season == WINTER or cold_water)
        and str((top_species or {}).get("id") or "") in {"midge", "early-black-stone"}
        and surface_signal < 0.5
    )
    if surface_signal >= 0.30 and hatch_name and not is_terrestrial and not winter_micro:
        approach = ["Watch for rises before switching to dries", "Start with the emerger in the film",
                    "Fish shade on bright days" if bright else "Keep a dropper on — many eat below"]
        return t(method=f"Match the hatch — {hatch_name}",
                 rig="9–12 ft leader to 5X–6X",
                 water=["Riffle tailouts", "Slick water below riffles", "Foam lines"],
                 flies=hatch_flies + ["Partridge & Orange #16", "Pheasant Tail #16–18"],
                 approach=approach,
                 where="meadow reaches with long glides")

    # ── Winter ───────────────────────────────────────────────────────────
    if season == WINTER or cold_water:
        approach = ["Fish slow and deep", "Best 10am–3pm, sunny days"]
        if not cloudy and tod == "midday":
            approach.append("Watch for midges on top")
        elif tod in ("morning", "evening"):
            approach.append("Expect a slow bite until midday")
        return t(method="Midge & scud nymphing",
                 rig="Small indicator, 6X–7X fluoro",
                 water=["Deep, slow pools", "Pool tailouts", "Below springs (look for watercress)"],
                 flies=["Zebra Midge #18–22", "Tan scud #14–18", "Pink Squirrel #14–16",
                        "Rainbow Warrior #18–20", "Winter stone #16–18", "Griffith's Gnat #20"],
                 approach=approach,
                 where="spring-fed reaches — warmest water in winter")

    # ── Summer heat / bright low-clear ──────────────────────────────────
    if season == SUMMER:
        if tod == "morning":
            return t(method="Dawn tricos, then hoppers",
                     rig="12 ft to 6X–7X for tricos",
                     water=["Flat glides", "Pool tails", "Riffles for the dropper"],
                     flies=hatch_flies + ["Trico spinner #22–24", "Griffith's Gnat #20", "Perdigon #16–18"],
                     approach=["Be on the water at first light", "Bite fades by 10–11am"],
                     where="small, cold spring creeks")
        if tod == "evening":
            return t(method="Evening dries & caddis",
                     rig="9 ft leader to 5X",
                     water=["Riffles and tails", "Undercut banks at dusk"],
                     flies=hatch_flies + ["Elk Hair Caddis #14–16", "Parachute Adams #12–14", "Mouse (after dark)"],
                     approach=["Last two hours of light fish best", "Swing a mouse after dark for big browns"])
        return t(method="Hopper-dropper on the banks",
                 rig="7.5–9 ft leader to 3X–4X",
                 water=["Undercut banks", "Overhanging grass", "Shade lines"],
                 flies=["Hopper #10–14", "Foam beetle #12–14", "Black ant #16", "Pheasant Tail #16 dropper"],
                 approach=["Work the shade — midday sun is tough",
                           "Slap it tight to the bank; one drift, move on",
                           "Cloud cover can fish all day" if cloudy else "Skip hoppers if the grass is wet"],
                 where="cold spring-fed tributaries")

    # ── Default by season: dry-dropper / nymphing ────────────────────────
    clear_low = clarity == CLEAR_LOW
    thin = "Thin perdigon #16–18" if (tod == "morning" or clear_low) else "Flashy bead-head #14–16"
    flies_ = hatch_flies + [thin, "Tan scud #14–16", "Pink Squirrel #14–16", "Pheasant Tail #16–18"]
    if tod == "afternoon" and season in (SPRING, EARLY_SUMMER):
        flies_.append("Caddis pupa #16")
    tips = []
    if clear_low:
        tips += ["Stay low, approach from downstream", "Longer leader, finer tippet"]
    if bright and season in (EARLY_SUMMER, FALL):
        tips.append("Keep flies tight to banks in sun")
    if cloudy and season in (SPRING, FALL):
        tips.append("Overcast — watch for BWOs")
    if season == EARLY_SUMMER and tod == "evening":
        tips.append("Sulphurs come in the last light")
    if windy:
        tips.append("Shorten the leader; find sheltered bends")
    if season == FALL:
        tips.append("Stay off gravel redds")
    return t(method="Dry-dropper or nymphing",
             rig="9–10 ft leader to 5X",
             water=["Riffles and the drop below", "Seams and undercuts", "Pool heads"],
             flies=flies_, approach=(tips or ["Work riffle-to-pool transitions upstream"])[:3],
             where="meadow and Class I reaches")
