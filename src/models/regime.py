"""
Regime classifier — what *kind* of fishing day is this?

The dry/nymph score answers "how good?" — but on a blown-out chocolate-milk
day, both scores collapse to "skip" when in reality streamers are prime. On a
38°F January afternoon both scores are zero but #20 zebra midges still produce.
Regime tells the angler what *play* to make, separately from the score.

Priorities are deliberately ordered: the first matching regime wins.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from src.models.runoff_risk import assess_runoff_risk
from src.models.regulations import season_status

# Hard-stop thresholds. Hours that hit these are capped in
# score_calibration.REGIME_CAPS regardless of how good the bite would be.
LETHAL_WATER_F = 73.0          # MT FWP hoot-owl / brown-trout lethal-stress zone
COLD_HAZARD_AIR_F = 10.0       # ice in the guides within minutes, frostbite risk
COLD_WINDY_AIR_F = 20.0        # …or below 20°F with real wind
COLD_WINDY_WIND_MPH = 15.0
HIGH_WIND_MPH = 25.0           # sustained; casting a fly line becomes futile
THUNDER_MIN_PRECIP_PROB = 30.0 # NWS PoP (%) when the hour's forecast mentions thunder


@dataclass
class Regime:
    code: str                  # stable identifier (BLOWOUT, STREAMER, …)
    label: str                 # human-readable headline
    detail: str                # one-line explanation for the chip tooltip
    fly_hint: Optional[str] = None  # short pattern hint, optional
    severity: str = "info"     # "info" | "warn" | "alert" — drives chip color

def classify(
    *,
    valid_at: datetime,
    reach_id: Optional[str] = None,
    flow_percentile: Optional[float],
    water_temp_f: Optional[float],
    air_temp_f: Optional[float],
    dry_score: float,
    nymph_score: float,
    spring_influenced: bool,
    length_km: Optional[float],
    qpf_map: Optional[Dict[str, float]],
    active_species: List[Dict[str, object]],
    state: Optional[str] = None,
    season_override: Optional[str] = None,
    wind_mph: Optional[float] = None,
    short_forecast: Optional[str] = None,
    precip_prob: Optional[float] = None,
) -> Regime:
    """Return the dominant regime. Order matters — first match wins.

    Inputs are all optional so the classifier degrades gracefully when a signal
    is missing. The fallback regime is NORMAL.
    """
    pct = flow_percentile if flow_percentile is not None else 0.5
    month = valid_at.month
    has_hatch = any((s.get("probability") or 0) >= 0.30 for s in (active_species or []))
    runoff = assess_runoff_risk(
        valid_at=valid_at,
        reach_id=reach_id,
        qpf_map=qpf_map,
        spring_influenced=spring_influenced,
        length_km=length_km,
        flow_percentile=pct,
    )

    # 0a) CLOSED — the state season is closed for this reach. Legal before biology.
    if state:
        season = season_status(state, valid_at.date(), season_override)
        if season.is_closed:
            reopen = f" — reopens {season.next_change}" if season.next_change else ""
            return Regime(
                code="CLOSED",
                label="Closed season",
                detail=f"{season.detail}{reopen}",
                fly_hint="Iowa streams are open year-round",
                severity="alert",
            )

    # 0b) LIGHTNING — a fly rod in a valley stream is a classic strike exposure.
    # NWS: "when thunder roars, go indoors"; wait 30 min after the last thunder.
    text = (short_forecast or "").lower()
    if "thunder" in text and (precip_prob is None or precip_prob >= THUNDER_MIN_PRECIP_PROB):
        return Regime(
            code="LIGHTNING",
            label="Thunderstorms",
            detail="thunderstorms forecast this hour — get off the water; wait 30 min after the last thunder",
            severity="alert",
        )

    # 0c) HAZARD — dangerous cold or wind. Not a fish problem; an angler one.
    if air_temp_f is not None and (
        air_temp_f < COLD_HAZARD_AIR_F
        or (air_temp_f < COLD_WINDY_AIR_F and (wind_mph or 0) > COLD_WINDY_WIND_MPH)
    ):
        return Regime(
            code="HAZARD",
            label="Too cold to fish",
            detail=f"air {air_temp_f:.0f}°F{f', wind {wind_mph:.0f} mph' if wind_mph else ''} — guides ice up in minutes; wait for a 25°F+ afternoon",
            severity="alert",
        )
    if wind_mph is not None and wind_mph > HIGH_WIND_MPH:
        return Regime(
            code="HAZARD",
            label="Blowing too hard",
            detail=f"sustained wind ~{wind_mph:.0f} mph — casting is futile; look for a sheltered coulee or wait",
            severity="alert",
        )

    # 1) BLOWOUT — heavy rain + high flow = unsafe + unfishable. Hard stop.
    if runoff.risk_level == "blowout" and pct >= 0.78:
        return Regime(
            code="BLOWOUT",
            label="Blown out",
            detail=runoff.note or "forecast rain likely pushes flow and clarity past fishable limits",
            severity="alert",
        )

    # 2) HEAT_STRESS — hot air, stressed water, midday hours. Tells the angler
    # *not* to fish midday regardless of score, and steers them to dawn/dusk.
    # `air_temp_f` was previously dead in this function; this is the regime
    # behind the "0 action until 6pm" guidance the model used to miss.
    local_hour = valid_at.hour
    if water_temp_f is not None and water_temp_f >= LETHAL_WATER_F:
        return Regime(
            code="HEAT_STRESS",
            label="Lethal water temperature",
            detail=f"water ~{water_temp_f:.0f}°F — at or past the lethal-stress line for trout; don't fish",
            fly_hint="find a spring-fed tributary or go after smallmouth",
            severity="alert",
        )
    if (
        air_temp_f is not None and air_temp_f >= 88
        and water_temp_f is not None and water_temp_f >= 68
        and 11 <= local_hour <= 17
    ):
        return Regime(
            code="HEAT_STRESS",
            label="Heat-stressed water",
            detail=f"air {air_temp_f:.0f}°F, water {water_temp_f:.0f}°F — fish dawn/dusk only",
            fly_hint="rest the river midday; evening sulphur/caddis hatch is the play",
            severity="alert",
        )

    # 4) STREAMER — high or rising-and-stained water but still wadeable; or
    #    cold-water aggressive feeding window. This is the regime that recovers
    #    "Skip" days that are actually streamer-prime.
    if pct >= 0.78 or (runoff.risk_level in {"high", "hurt"} and pct >= 0.65):
        return Regime(
            code="STREAMER",
            label="Streamer day",
            detail=runoff.note or "elevated flow, stained water — fish are looking up for big silhouettes",
            fly_hint="olive/black streamer, large bugger, sculpin pattern",
            severity="warn",
        )
    if water_temp_f is not None and 42 <= water_temp_f <= 50 and pct >= 0.55:
        return Regime(
            code="STREAMER",
            label="Cold-water streamers",
            detail="cold, slightly elevated water — slow swung streamer wins",
            fly_hint="weighted bugger or leech, slow strip",
            severity="info",
        )

    # 5) HATCH — meaningful active-species probability. Defer to the existing
    #    fly recommender for the actual pattern; just flag the regime.
    if has_hatch and dry_score >= 0.30:
        top = max(active_species, key=lambda s: s.get("probability") or 0)
        name = top.get("common_name") or top.get("id") or "active hatch"
        return Regime(
            code="HATCH",
            label=f"Match the hatch · {name}",
            detail="dry-fly window open — match active species",
            severity="info",
        )

    # 6) TERRESTRIAL — summer afternoons, no aquatic hatch firing. The Driftless
    #    grasshopper bite is the most underrated fishery in the region.
    if month in {7, 8, 9} and (water_temp_f or 0) >= 58 and dry_score < 0.20:
        return Regime(
            code="TERRESTRIAL",
            label="Terrestrial bite",
            detail="warm afternoon, no hatch — hoppers, ants, beetles in the grass",
            fly_hint="hopper-dropper: #10 hopper + #16 PT or scud",
            severity="info",
        )

    # 7) MIDGE — winter / very cold water. Trout still feed, just on bugs you
    #    can't see. Without this regime the model says "Skip" all winter.
    if water_temp_f is not None and water_temp_f < 50 and dry_score < 0.15 and not has_hatch:
        return Regime(
            code="MIDGE",
            label="Midge & micro",
            detail="cold water, no aquatic emergence — go small",
            fly_hint="zebra midge #20–22, griffith's gnat, RS2",
            severity="info",
        )

    # 8) SCUD — spring-creek default when nothing else applies. Driftless
    #    limestoners run scud-rich year-round; this is a real regime, not a
    #    fallback.
    if spring_influenced and not has_hatch and nymph_score < 0.55:
        return Regime(
            code="SCUD",
            label="Spring creek nymphing",
            detail="limestone reach with no active hatch — scuds, sowbugs, midges",
            fly_hint="orange/pink scud #14, sowbug, pheasant tail",
            severity="info",
        )

    # 9) NORMAL nymphing day.
    return Regime(
        code="NORMAL",
        label="Nymph & swing",
        detail="no special conditions — standard searching nymph rig",
        fly_hint="pheasant tail #14 + hare's ear #16 dropper",
        severity="info",
    )


def regime_to_dict(r: Regime) -> Dict[str, str]:
    return {
        "code": r.code,
        "label": r.label,
        "detail": r.detail,
        "fly_hint": r.fly_hint or "",
        "severity": r.severity,
    }
