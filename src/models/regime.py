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
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional


@dataclass
class Regime:
    code: str                  # stable identifier (BLOWOUT, STREAMER, …)
    label: str                 # human-readable headline
    detail: str                # one-line explanation for the chip tooltip
    fly_hint: Optional[str] = None  # short pattern hint, optional
    severity: str = "info"     # "info" | "warn" | "alert" — drives chip color


def _preceding_qpf_mm(qpf_map: Optional[Dict[str, float]], valid_at: datetime, hours: int) -> float:
    if not qpf_map:
        return 0.0
    total = 0.0
    for h in range(1, hours + 1):
        prior = (valid_at - timedelta(hours=h)).astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
        total += qpf_map.get(prior, 0.0)
    return total


def classify(
    *,
    valid_at: datetime,
    flow_percentile: Optional[float],
    water_temp_f: Optional[float],
    air_temp_f: Optional[float],
    dry_score: float,
    nymph_score: float,
    spring_influenced: bool,
    qpf_map: Optional[Dict[str, float]],
    active_species: List[Dict[str, object]],
) -> Regime:
    """Return the dominant regime. Order matters — first match wins.

    Inputs are all optional so the classifier degrades gracefully when a signal
    is missing. The fallback regime is NORMAL.
    """
    # 24h preceding rain — Driftless loess-soil watersheds blow out fast.
    preceding_24 = _preceding_qpf_mm(qpf_map, valid_at, 24)
    pct = flow_percentile if flow_percentile is not None else 0.5
    month = valid_at.month
    has_hatch = any((s.get("probability") or 0) >= 0.30 for s in (active_species or []))

    # 1) BLOWOUT — heavy rain + high flow = unsafe + unfishable. Hard stop.
    if preceding_24 >= 25.0 and pct >= 0.85:
        return Regime(
            code="BLOWOUT",
            label="Blown out",
            detail=f"~{preceding_24/25.4:.1f}\" rain in last 24h, water muddy and dangerous",
            severity="alert",
        )

    # 2) STREAMER — high or rising-and-stained water but still wadeable; or
    #    cold-water aggressive feeding window. This is the regime that recovers
    #    "Skip" days that are actually streamer-prime.
    if pct >= 0.78 or (preceding_24 >= 12.7 and pct >= 0.65):
        return Regime(
            code="STREAMER",
            label="Streamer day",
            detail="elevated flow, stained water — fish are looking up for big silhouettes",
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

    # 3) HATCH — meaningful active-species probability. Defer to the existing
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

    # 4) TERRESTRIAL — summer afternoons, no aquatic hatch firing. The Driftless
    #    grasshopper bite is the most underrated fishery in the region.
    if month in {7, 8, 9} and (water_temp_f or 0) >= 58 and dry_score < 0.20:
        return Regime(
            code="TERRESTRIAL",
            label="Terrestrial bite",
            detail="warm afternoon, no hatch — hoppers, ants, beetles in the grass",
            fly_hint="hopper-dropper: #10 hopper + #16 PT or scud",
            severity="info",
        )

    # 5) MIDGE — winter / very cold water. Trout still feed, just on bugs you
    #    can't see. Without this regime the model says "Skip" all winter.
    if water_temp_f is not None and water_temp_f < 50 and dry_score < 0.15 and not has_hatch:
        return Regime(
            code="MIDGE",
            label="Midge & micro",
            detail="cold water, no aquatic emergence — go small",
            fly_hint="zebra midge #20–22, griffith's gnat, RS2",
            severity="info",
        )

    # 6) SCUD — spring-creek default when nothing else applies. Driftless
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

    # 7) NORMAL nymphing day.
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
