"""Inland stream-trout season status by state and date.

"Should I fish today?" starts with "is it legal today?" — a forecast that ranks
a Wisconsin creek as the best window on October 20 is worse than useless.

Rules encoded here are the *general* inland stream-trout seasons. Individual
streams carry special regulations (year-round C&R stretches, artificial-only,
slot limits, refuges) that this module cannot know about, so every status
carries a "verify the current regulations" note and a link to the agency page.
A reach can override the state default with a `season_override` seed field once
a stream-specific rule has been verified.

Sources (checked Oct 2026):
  * MN DNR fishing seasons — SE MN stream trout: catch-and-release Jan 1 until
    the harvest opener (2nd Saturday in April); harvest through Sep 14;
    catch-and-release Sep 15 – Oct 15; closed Oct 16 – Dec 31.
    https://dnr.state.mn.us/fishing/seasons.html
  * WI DNR inland trout seasons — early catch-and-release opens the 1st
    Saturday in January; starting in 2026 the regular (harvest) season opens the
    1st Saturday in April (previously the 1st Saturday in May) and runs through
    Oct 15; closed after.
    https://dnr.wisconsin.gov/topic/fishing/seasons/trout.html
    https://dnr.wisconsin.gov/newsroom/release/120206
  * Iowa DNR — trout streams are open year-round.
    https://www.iowadnr.gov/fishing/trout-fishing
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, timedelta
from typing import Mapping, Optional

OPEN = "open"
CATCH_AND_RELEASE = "catch_and_release"
CLOSED = "closed"

REG_LINKS = {
    "MN": "https://dnr.state.mn.us/fishing/seasons.html",
    "WI": "https://dnr.wisconsin.gov/topic/fishing/seasons/trout.html",
    "IA": "https://www.iowadnr.gov/fishing/trout-fishing",
}

VERIFY_NOTE = "general state season — special regulations vary by stream; check current regs"


@dataclass(frozen=True)
class SeasonStatus:
    state: Optional[str]
    status: str                 # open | catch_and_release | closed | unknown
    label: str                  # short human label
    detail: str
    next_change: Optional[str]  # ISO date the status next changes, if known
    next_status: Optional[str]
    source_url: Optional[str]

    @property
    def is_closed(self) -> bool:
        return self.status == CLOSED

    def to_dict(self) -> dict:
        d = asdict(self)
        d["is_closed"] = self.is_closed
        return d


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th (1-based) `weekday` (Mon=0 … Sun=6) of a month."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


SATURDAY = 5


def _mn_periods(year: int):
    harvest_open = nth_weekday(year, 4, SATURDAY, 2)
    return [
        (date(year, 1, 1), CATCH_AND_RELEASE, "early catch-and-release season"),
        (harvest_open, OPEN, "harvest season"),
        (date(year, 9, 15), CATCH_AND_RELEASE, "fall catch-and-release season"),
        (date(year, 10, 16), CLOSED, "closed until Jan 1"),
    ]


def _wi_periods(year: int):
    early_open = nth_weekday(year, 1, SATURDAY, 1)
    # 2026 rule change moved the harvest opener from May to April.
    harvest_month = 4 if year >= 2026 else 5
    harvest_open = nth_weekday(year, harvest_month, SATURDAY, 1)
    return [
        (date(year, 1, 1), CLOSED, "closed until the early season opener"),
        (early_open, CATCH_AND_RELEASE, "early catch-and-release season"),
        (harvest_open, OPEN, "regular season"),
        (date(year, 10, 16), CLOSED, "closed until the early season opener"),
    ]


_PERIOD_BUILDERS = {"MN": _mn_periods, "WI": _wi_periods}


def season_status(state: Optional[str], on_date: date, override: Optional[str] = None) -> SeasonStatus:
    """Season status for a reach in `state` on `on_date`.

    `override` is a reach-level seed value: "year_round_cr" (catch-and-release
    all year), "year_round" (open all year) or None for the state default.
    """
    st = (state or "").upper() or None
    link = REG_LINKS.get(st or "")
    if override == "year_round":
        return SeasonStatus(st, OPEN, "Open year-round", f"stream-specific year-round season; {VERIFY_NOTE}", None, None, link)
    if override == "year_round_cr":
        return SeasonStatus(st, CATCH_AND_RELEASE, "Catch & release (year-round)",
                            f"stream-specific year-round catch-and-release; {VERIFY_NOTE}", None, None, link)
    if st == "IA":
        return SeasonStatus(st, OPEN, "Open year-round", f"Iowa trout streams are open all year; {VERIFY_NOTE}", None, None, link)
    builder = _PERIOD_BUILDERS.get(st or "")
    if builder is None:
        return SeasonStatus(st, "unknown", "Season unknown", "no season rules encoded for this state", None, None, link)

    periods = builder(on_date.year) + builder(on_date.year + 1)
    idx = max(i for i, (start, _s, _d) in enumerate(periods) if start <= on_date)
    current = periods[idx]
    # Next *change* of status — skip periods that keep the same status (WI's
    # Oct 16 closure runs straight into the Jan 1 "closed" entry).
    nxt = None
    for later in periods[idx + 1:]:
        if later[1] != current[1]:
            nxt = later
            break
    _, status, desc = current
    label = {
        OPEN: "Open season",
        CATCH_AND_RELEASE: "Catch & release only",
        CLOSED: "Closed season",
    }[status]
    detail = f"{st} {desc}; {VERIFY_NOTE}"
    return SeasonStatus(
        st, status, label, detail,
        nxt[0].isoformat() if nxt else None,
        nxt[1] if nxt else None,
        link,
    )


def season_for_reach(reach: Mapping[str, object], on_date: date) -> SeasonStatus:
    override = reach.get("season_override") if isinstance(reach, Mapping) else None
    return season_status(str(reach.get("state") or ""), on_date, override if isinstance(override, str) else None)


# ── Spawning ────────────────────────────────────────────────────────────────
# MN brown trout spawn early Oct – Nov (peak ~Oct 13 – Nov 14; Valley Creek
# Oct 12 – Nov 22); brook trout run a little earlier. Fishing to spawners and
# wading on redds is broadly considered unethical. This is an advisory, not a
# score gate — on water that is open (Iowa all fall; MN/WI until Oct 15) the
# angler should simply avoid gravel tailouts and paired-up fish.
SPAWN_START = (9, 25)
SPAWN_END = (11, 30)


def spawning_advisory(on_date: date) -> Optional[str]:
    start = date(on_date.year, *SPAWN_START)
    end = date(on_date.year, *SPAWN_END)
    if start <= on_date <= end:
        return "Spawning season — stay off gravel redds in pool tailouts."
    return None
