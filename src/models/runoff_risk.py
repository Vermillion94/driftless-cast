"""Reach-aware runoff / clarity risk heuristic from forecast precipitation.

This is intentionally heuristic, but it is materially better than a single
global "0.5in in 24h" rule. Driftless storm response depends on:

* spring influence (slower, clearer, more buffered)
* practical reach size (small tributary vs bigger mainstem)
* antecedent flow state (already-high water needs less additional rain)
* event timing (0.5in in 6h is very different from 0.5in in 24h)

The output is used in two places:
1. nudge projected flow percentile upward when forecast rain should materially
   stain / swell the stream
2. explain to anglers when forecast precipitation likely pushes a reach past
   "good fishing" into streamer-only or blown-out conditions
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional


@dataclass(frozen=True)
class RunoffRisk:
    size_class: str
    hurt_threshold_6h_mm: float
    hurt_threshold_12h_mm: float
    hurt_threshold_24h_mm: float
    preceding_6h_mm: float
    preceding_12h_mm: float
    preceding_24h_mm: float
    response_ratio: float
    risk_level: str
    percentile_bump: float
    note: Optional[str]


def _preceding_qpf_mm(qpf_map: Optional[Dict[str, float]], valid_at: datetime, hours: int) -> float:
    if not qpf_map:
        return 0.0
    total = 0.0
    for h in range(1, hours + 1):
        prior = (valid_at - timedelta(hours=h)).astimezone(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        ).isoformat()
        total += qpf_map.get(prior, 0.0)
    return total


def _size_class(length_km: Optional[float]) -> str:
    try:
        km = float(length_km or 0.0)
    except (TypeError, ValueError):
        km = 0.0
    if km <= 8.0:
        return "small"
    if km <= 18.0:
        return "medium"
    return "large"


def _base_thresholds_mm(spring_influenced: bool, size_class: str) -> tuple[float, float]:
    # "hurt" threshold = where fishing usually starts degrading materially,
    # not necessarily a total blowout. Small freestones flash quickly; spring
    # creeks and larger valleys tolerate more rain before clarity/wading go bad.
    if spring_influenced:
        lookup = {
            "small": (16.0, 28.0),   # ~0.6" in 6h or ~1.1" in 24h
            "medium": (20.0, 34.0),  # ~0.8", ~1.3"
            "large": (24.0, 42.0),   # ~0.9", ~1.7"
        }
    else:
        lookup = {
            "small": (9.0, 17.0),    # ~0.35", ~0.7"
            "medium": (13.0, 23.0),  # ~0.5", ~0.9"
            "large": (18.0, 31.0),   # ~0.7", ~1.2"
        }
    return lookup[size_class]


def assess_runoff_risk(
    *,
    valid_at: datetime,
    qpf_map: Optional[Dict[str, float]],
    spring_influenced: bool,
    length_km: Optional[float],
    flow_percentile: Optional[float],
) -> RunoffRisk:
    size_class = _size_class(length_km)
    t6, t24 = _base_thresholds_mm(spring_influenced, size_class)
    t12 = (t6 + t24) / 2.0

    pct = 0.5 if flow_percentile is None else max(0.0, min(1.0, float(flow_percentile)))
    # Already-high water takes less extra rain to tip into poor clarity or
    # dangerous speed. Very low water needs a bit more rain before it hurts.
    if pct >= 0.75:
        modifier = 0.80
    elif pct >= 0.60:
        modifier = 0.90
    elif pct <= 0.35:
        modifier = 1.15
    else:
        modifier = 1.00

    t6 *= modifier
    t12 *= modifier
    t24 *= modifier

    p6 = _preceding_qpf_mm(qpf_map, valid_at, 6)
    p12 = _preceding_qpf_mm(qpf_map, valid_at, 12)
    p24 = _preceding_qpf_mm(qpf_map, valid_at, 24)

    response_ratio = max(
        p6 / max(t6, 1.0),
        p12 / max(t12, 1.0),
        p24 / max(t24, 1.0),
    )

    if response_ratio < 0.45:
        level = "low"
    elif response_ratio < 0.85:
        level = "watch"
    elif response_ratio < 1.20:
        level = "hurt"
    elif response_ratio < 1.60:
        level = "high"
    else:
        level = "blowout"

    bump = 0.0
    if response_ratio >= 0.35:
        bump = min(0.55, max(0.0, response_ratio - 0.35) * 0.32)

    note = None
    if level in {"hurt", "high", "blowout"}:
        note = (
            f"{size_class} {'spring' if spring_influenced else 'flashy'} reach: "
            f"~{p6/25.4:.1f}\"/6h and ~{p24/25.4:.1f}\"/24h "
            f"(hurt starts near {t6/25.4:.1f}\"/6h or {t24/25.4:.1f}\"/24h)"
        )
    elif level == "watch":
        note = (
            f"rain watch for this {size_class} reach "
            f"(~{p24/25.4:.1f}\" in preceding 24h)"
        )

    return RunoffRisk(
        size_class=size_class,
        hurt_threshold_6h_mm=t6,
        hurt_threshold_12h_mm=t12,
        hurt_threshold_24h_mm=t24,
        preceding_6h_mm=p6,
        preceding_12h_mm=p12,
        preceding_24h_mm=p24,
        response_ratio=response_ratio,
        risk_level=level,
        percentile_bump=bump,
        note=note,
    )
