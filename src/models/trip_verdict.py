"""Day-level trip verdict: go, go later, go tomorrow, or skip — and why.

Hourly scores answer "how good is 3pm?". An angler deciding whether to drive
to a stream needs one call per day: is today worth it, when is the window, and
if not, which day is better and what changes. This module rolls the hourly
payload (as built by the API, headline already calibrated) into that call.

It runs at read time on persisted predictions, so it adds no schema.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional

FISHABLE_START_H = 5
FISHABLE_END_H = 21

GO = "go"
GOOD = "good"
SLOW = "slow"
SKIP = "skip"
CLOSED = "closed"

VERDICT_LABELS = {
    GO: "Go",
    GOOD: "Worth it",
    SLOW: "Slow day",
    SKIP: "Skip",
    CLOSED: "Closed",
}

GO_MIN = 0.70
GOOD_MIN = 0.50
SKIP_MAX = 0.30
# A later day has to beat today by this much (3-hour mean) to be called out
# as "go then instead" — smaller gaps are inside model noise.
BETTER_DAY_MARGIN = 0.12

HARD_STOP_REASONS = {
    "CLOSED": "season is closed",
    "LIGHTNING": "thunderstorms",
    "BLOWOUT": "blown out / muddy water",
    "HAZARD": "dangerous cold or wind",
    "HEAT_STRESS": "water too warm for trout",
}


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))


def _score(h: Mapping[str, Any]) -> float:
    try:
        return float(h.get("combined_score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _code(h: Mapping[str, Any]) -> Optional[str]:
    r = h.get("regime")
    return r.get("code") if isinstance(r, Mapping) else None


def _best_window(hours: List[Mapping[str, Any]], width: int = 3):
    if not hours:
        return None
    scores = [_score(h) for h in hours]
    if len(hours) < width:
        i = max(range(len(scores)), key=scores.__getitem__)
        return i, i, scores[i]
    best_i, best_m = 0, -1.0
    for i in range(len(hours) - width + 1):
        m = sum(scores[i:i + width]) / width
        if m > best_m:
            best_i, best_m = i, m
    return best_i, best_i + width - 1, best_m


def _fmt_hour(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    return f"{h}{'am' if dt.hour < 12 else 'pm'}"


def _top_hatch(hours: Iterable[Mapping[str, Any]]) -> Optional[str]:
    best = (0.0, None)
    for h in hours:
        for s in h.get("active_species") or []:
            p = float(s.get("probability") or 0.0)
            if p > best[0] and not s.get("is_terrestrial"):
                best = (p, s.get("common_name") or s.get("id"))
    return best[1] if best[0] >= 0.25 else None


def _day_reasons(day_hours: List[Mapping[str, Any]], window: List[Mapping[str, Any]]) -> List[str]:
    reasons: List[str] = []
    codes = [_code(h) for h in day_hours]
    n = max(1, len(codes))
    for code, text in HARD_STOP_REASONS.items():
        share = sum(1 for c in codes if c == code) / n
        if share >= 0.5:
            reasons.append(f"{text} most of the day")
        elif share > 0 and code in {"LIGHTNING", "HEAT_STRESS", "HAZARD"}:
            reasons.append(f"{text} for part of the day")
    hatch = _top_hatch(window or day_hours)
    if hatch:
        reasons.append(f"{hatch} expected")
    temps = [h.get("water_temp_f") for h in (window or day_hours) if h.get("water_temp_f") is not None]
    if temps:
        t = sum(temps) / len(temps)
        if t < 42:
            reasons.append(f"cold water (~{t:.0f}°F) — slow, deep, midday bite")
        elif 50 <= t <= 62:
            reasons.append(f"prime water temperature (~{t:.0f}°F)")
        elif t >= 66:
            reasons.append(f"warm water (~{t:.0f}°F) — fish early")
    tac = (window[0].get("tactics") if window else None) or {}
    clarity = tac.get("clarity")
    if clarity in {"stained", "off"}:
        reasons.append(f"{tac.get('clarity_label')} water — streamer/big-fish chance")
    elif clarity == "muddy":
        reasons.append("muddy water")
    elif clarity == "low_clear":
        reasons.append("low, clear water — stealth matters")
    return reasons[:4]


def build_days(hours: List[Dict[str, Any]], season: Optional[Mapping[str, Any]] = None) -> List[Dict[str, Any]]:
    """Group hourly payload rows by local date and attach a verdict to each."""
    by_day: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for h in hours:
        try:
            dt = _parse(h["valid_at"])
        except (KeyError, ValueError):
            continue
        by_day.setdefault(dt.date().isoformat(), []).append(h)

    days: List[Dict[str, Any]] = []
    for day, rows in by_day.items():
        fishable = [h for h in rows if FISHABLE_START_H <= _parse(h["valid_at"]).hour <= FISHABLE_END_H]
        if len(fishable) < 3:
            # A forecast-horizon stub (e.g. the last hour or two of the run) or
            # an overnight-only fragment — not a fishing day.
            continue
        pick = fishable
        win = _best_window(pick)
        if win is None:
            continue
        i0, i1, mean = win
        window = pick[i0:i1 + 1]
        codes = [_code(h) for h in rows]
        all_closed = codes and all(c == "CLOSED" for c in codes)
        if all_closed:
            verdict = CLOSED
        elif mean >= GO_MIN:
            verdict = GO
        elif mean >= GOOD_MIN:
            verdict = GOOD
        elif mean > SKIP_MAX:
            verdict = SLOW
        else:
            verdict = SKIP
        start, end = _parse(window[0]["valid_at"]), _parse(window[-1]["valid_at"])
        days.append({
            "date": day,
            "weekday": start.strftime("%a"),
            "verdict": verdict,
            "verdict_label": VERDICT_LABELS[verdict],
            "best_window": {
                "start": window[0]["valid_at"],
                "end": window[-1]["valid_at"],
                "label": f"{_fmt_hour(start)}–{_fmt_hour(end)}",
                "score": round(mean, 3),
                "method": (window[len(window) // 2].get("tactics") or {}).get("method"),
            },
            "reasons": [HARD_STOP_REASONS["CLOSED"]] if all_closed else _day_reasons(rows, window),
        })

    _attach_advice(days, season)
    return days


# Reasons that describe what is *good* about a day; a Skip must cite a limiter.
_POSITIVE_MARKERS = ("prime water", "expected", "streamer/big-fish chance")


def _limiting_reason(reasons: List[str]) -> str:
    for r in reasons:
        if not any(m in r for m in _POSITIVE_MARKERS):
            return r
    return "conditions are poor"


def _attach_advice(days: List[Dict[str, Any]], season: Optional[Mapping[str, Any]]) -> None:
    """One plain-English sentence per day; flags a clearly better day ahead."""
    for idx, d in enumerate(days):
        score = d["best_window"]["score"]
        later = days[idx + 1: idx + 4]
        better = max(later, key=lambda x: x["best_window"]["score"], default=None)
        better_by = (better["best_window"]["score"] - score) if better else 0.0
        if d["verdict"] == CLOSED:
            reopen = (season or {}).get("next_change")
            d["advice"] = f"Season closed{f' — reopens {reopen}' if reopen else ''}. Iowa streams stay open all year."
            continue
        if d["verdict"] == SKIP:
            why = _limiting_reason(d["reasons"])
            if better and better["verdict"] in {GO, GOOD}:
                d["advice"] = (f"Skip — {why}. Go {better['weekday']} "
                               f"{better['best_window']['label']} instead.")
            else:
                d["advice"] = f"Skip — {why}."
            continue
        if better and better_by >= BETTER_DAY_MARGIN and better["verdict"] in {GO, GOOD}:
            d["better_day"] = better["date"]
            d["advice"] = (f"{d['verdict_label']} {d['best_window']['label']}, but "
                           f"{better['weekday']} {better['best_window']['label']} looks clearly better.")
            continue
        method = d["best_window"].get("method")
        d["advice"] = (f"{d['verdict_label']} — best {d['best_window']['label']}"
                       f"{f' ({method.lower()})' if method else ''}.")
