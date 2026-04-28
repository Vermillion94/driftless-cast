"""
Calibrate per-species DD emergence thresholds from iNaturalist observations.

For each target species:
  1. Resolve iNat taxon.
  2. Pull N years of research-grade observations in the Driftless bbox.
  3. Filter to each species' plausible flight-window months.
  4. For each observation, compute DD accumulated to the observed date using
     our Mohseni water-temp estimator + Open-Meteo archive (src.models.dd_calibration).
  5. Fit a Gaussian — write mean/sd back into data/seed/species.json.
  6. Save a diagnostic dump per species.

Heavy script. First run is slow (many Open-Meteo calls); reruns hit disk cache.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from datetime import date as _date

from src.ingest import (
    fetch_idigbio_records,
    fetch_observations,
    fetch_occurrences,
    match_taxon,
    resolve_taxon,
)
from src.ingest.inat import UPPER_MIDWEST_BBOX
from src.ingest.idigbio import UPPER_MIDWEST_BBOX as IDIG_BBOX
from src.models.dd_calibration import dd_for_observation

ROOT = Path(__file__).resolve().parent.parent.parent
SPECIES_PATH = ROOT / "data" / "seed" / "species.json"
DIAGNOSTICS_DIR = ROOT / "data" / "calibration"

LOG = logging.getLogger("fit_dd")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


# Flight-window filters: (first_month, last_month) inclusive. Anything outside
# these months is almost certainly a mis-ID, a stored specimen, or a nymph —
# not an emergence event.
FLIGHT_WINDOWS: Dict[str, tuple[int, int]] = {
    "hendrickson":    (4, 5),
    "bwo-spring":     (3, 5),     # multi-brooded: clip to spring brood
    "sulphur":        (5, 7),
    "grannom-caddis": (4, 6),
    "trico":          (7, 10),
    "hex":            (6, 7),
    "bwo-fall":       (9, 11),
    "isonychia":      (6, 9),
    "tan-caddis":     (5, 9),
}

# Per-species taxonomic targets. We prefer species-level over genus/family
# because broader queries conflate emergence times of multiple species. iDigBio
# substantially complements GBIF here, so we accept smaller GBIF samples than
# we would otherwise.
TAXON_HINTS: Dict[str, List[Dict[str, str]]] = {
    "hendrickson":    [{"query": "Ephemerella subvaria",     "rank": "species"},
                       {"query": "Ephemerellidae",            "rank": "family"}],
    "bwo-spring":     [{"query": "Baetis tricaudatus",       "rank": "species"},
                       {"query": "Baetidae",                  "rank": "family"}],
    "sulphur":        [{"query": "Ephemerella invaria",      "rank": "species"},
                       {"query": "Ephemerella dorothea",     "rank": "species"},
                       {"query": "Ephemerella",               "rank": "genus"}],
    "grannom-caddis": [{"query": "Brachycentrus numerosus",  "rank": "species"},
                       {"query": "Brachycentrus americanus", "rank": "species"},
                       {"query": "Brachycentrus",             "rank": "genus"}],
    "trico":          [{"query": "Tricorythodes",            "rank": "genus"}],
    "hex":            [{"query": "Hexagenia limbata",        "rank": "species"}],
    "bwo-fall":       [{"query": "Baetis tricaudatus",       "rank": "species"},
                       {"query": "Baetidae",                  "rank": "family"}],
    "isonychia":      [{"query": "Isonychia bicolor",        "rank": "species"},
                       {"query": "Isonychia",                 "rank": "genus"}],
    "tan-caddis":     [{"query": "Hydropsyche",              "rank": "genus"},
                       {"query": "Hydropsychidae",            "rank": "family"}],
}


@dataclass
class UnifiedObs:
    date: _date
    lat: float
    lon: float
    source: str       # "inat" | "gbif"
    taxon_name: str

MIN_OBS_FOR_FIT = 15       # fewer → keep literature thresholds
YEARS_BACK = 10


def _load_species() -> List[Dict[str, object]]:
    with SPECIES_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _save_species(species: List[Dict[str, object]]) -> None:
    with SPECIES_PATH.open("w", encoding="utf-8") as fh:
        json.dump(species, fh, indent=2)


def _resolve_inat_chain(species_id: str) -> List[Tuple[int, str, str]]:
    hints = TAXON_HINTS.get(species_id, [])
    resolved: List[Tuple[int, str, str]] = []
    for h in hints:
        taxon = resolve_taxon(h["query"], rank=h.get("rank"))
        if taxon and taxon.get("id"):
            resolved.append((int(taxon["id"]), str(taxon.get("name", "")), h.get("rank", "")))
    return resolved


def _resolve_gbif_chain(species_id: str) -> List[Tuple[int, str, str]]:
    hints = TAXON_HINTS.get(species_id, [])
    resolved: List[Tuple[int, str, str]] = []
    for h in hints:
        taxon = match_taxon(h["query"], rank=h.get("rank"))
        if taxon and taxon.get("usageKey"):
            resolved.append((int(taxon["usageKey"]),
                            str(taxon.get("canonicalName") or taxon.get("scientificName", "")),
                            h.get("rank", "")))
    return resolved


def _unify_inat(taxon_id: int, start_year: int, end_year: int) -> List[UnifiedObs]:
    obs = fetch_observations(taxon_id, start_year, end_year, bbox=UPPER_MIDWEST_BBOX)
    out: List[UnifiedObs] = []
    for o in obs:
        try:
            d = date.fromisoformat(o.observed_on[:10])
        except (ValueError, TypeError):
            continue
        out.append(UnifiedObs(date=d, lat=o.lat, lon=o.lon, source="inat", taxon_name=o.taxon_name))
    return out


def _unify_gbif(taxon_key: int, start_year: int, end_year: int) -> List[UnifiedObs]:
    occs = fetch_occurrences(taxon_key, start_year, end_year)
    out: List[UnifiedObs] = []
    for o in occs:
        try:
            d = date.fromisoformat(o.event_date)
        except (ValueError, TypeError):
            continue
        out.append(UnifiedObs(date=d, lat=o.lat, lon=o.lon, source="gbif", taxon_name=o.scientific_name))
    return out


IDIGBIO_MIN_YEAR = 1940  # Open-Meteo historical archive lower bound. Older records exist
                          # but we can't compute air-temp DD for them.


def _unify_idigbio(scientific_name: str, start_year: int, end_year: int) -> List[UnifiedObs]:
    """Specimen records. We don't year-filter to recent years (phenology is
    stable on multi-decade scales) but do require ≥2000 so the climate baseline
    we estimate DD against isn't pre-warming. Open-Meteo archive supports back
    to 1940 — but earlier records would compare modern Mohseni against historical
    weather and bias the fit.
    """
    recs = fetch_idigbio_records(scientific_name)
    out: List[UnifiedObs] = []
    for r in recs:
        try:
            d = date.fromisoformat(r.event_date)
        except (ValueError, TypeError):
            continue
        if d.year < IDIGBIO_MIN_YEAR or d.year > end_year + 1:
            continue
        out.append(UnifiedObs(date=d, lat=r.lat, lon=r.lon, source="idigbio", taxon_name=r.scientific_name))
    return out


def _gather(species_id: str, start_year: int, end_year: int,
             first_month: int, last_month: int) -> Tuple[List[UnifiedObs], Dict[str, object]]:
    """Union of iNat + GBIF + iDigBio obs within flight window, plus diagnostic tallies.

    Walks the taxon chain (species → genus → family) on all three sources in
    parallel; keeps the narrowest level that yields ≥ MIN_OBS_FOR_FIT in window.
    iDigBio dominates volume for our target species (5–17× GBIF) so we always
    query it.
    """
    hints = TAXON_HINTS.get(species_id, [])
    inat_chain = _resolve_inat_chain(species_id)
    gbif_chain = _resolve_gbif_chain(species_id)

    merged: List[UnifiedObs] = []
    tally = {"inat_pulled": 0, "gbif_pulled": 0, "idigbio_pulled": 0, "chain_used": None}

    n_steps = max(len(inat_chain), len(gbif_chain), len(hints))
    for step in range(n_steps):
        inat_h = inat_chain[step] if step < len(inat_chain) else None
        gbif_h = gbif_chain[step] if step < len(gbif_chain) else None
        scientific_name_for_idigbio = hints[step]["query"] if step < len(hints) else None

        inat_obs = []
        gbif_obs = []
        idigbio_obs = []

        if inat_h:
            inat_obs = _unify_inat(inat_h[0], start_year, end_year)
            LOG.info("  iNat %s %s (id=%d): %d obs", inat_h[2], inat_h[1], inat_h[0], len(inat_obs))
            tally["inat_pulled"] += len(inat_obs)
        if gbif_h:
            gbif_obs = _unify_gbif(gbif_h[0], start_year, end_year)
            LOG.info("  GBIF %s %s (key=%d): %d obs", gbif_h[2], gbif_h[1], gbif_h[0], len(gbif_obs))
            tally["gbif_pulled"] += len(gbif_obs)
        if scientific_name_for_idigbio:
            idigbio_obs = _unify_idigbio(scientific_name_for_idigbio, start_year, end_year)
            LOG.info("  iDigBio  %s: %d obs", scientific_name_for_idigbio, len(idigbio_obs))
            tally["idigbio_pulled"] += len(idigbio_obs)

        union = inat_obs + gbif_obs + idigbio_obs
        in_window = [o for o in union if first_month <= o.date.month <= last_month]
        LOG.info("  -> %d in flight window %s", len(in_window), (first_month, last_month))
        if len(in_window) >= MIN_OBS_FOR_FIT:
            merged = in_window
            tally["chain_used"] = {
                "inat": inat_h[1] if inat_h else None,
                "gbif": gbif_h[1] if gbif_h else None,
                "idigbio": scientific_name_for_idigbio,
                "rank": (inat_h or gbif_h)[2] if (inat_h or gbif_h) else (hints[step].get("rank") if step < len(hints) else None),
            }
            break
        merged = in_window
    return merged, tally


def _compute_dd_values(obs_list: List[UnifiedObs], base_temp_c: float) -> List[float]:
    values: List[float] = []
    for o in obs_list:
        dd = dd_for_observation(o.date, o.lat, o.lon, base_temp_c, spring_influenced=False)
        if dd is not None and dd >= 0:
            values.append(dd)
    return values


def _robust_fit(values: List[float], prior_mean: float, prior_sd: float) -> Optional[tuple[float, float, Dict[str, float], str]]:
    """Bayesian-flavored update: shrink the observed median toward the literature prior.

    Free-fitting on small samples produced wild numbers (Hendrickson 45 from 16
    obs); fully trusting literature ignores genuine regional shifts. We compromise:
      * prior weight = 30 (rough pseudo-count of literature confidence)
      * observed median + observed n compete with the prior
      * final mean = (prior_mean * prior_w + obs_median * n) / (prior_w + n)
      * final sd  = same blend on the spread side
    Heavily weighted toward literature unless we have hundreds of observations.
    """
    if not values:
        return None
    vs = sorted(values)
    n = len(values)
    # Trim 10/90 tails on the observation side
    lo = int(n * 0.10)
    hi = int(n * 0.90) or 1
    core = vs[lo:hi] if hi > lo else vs
    obs_median = statistics.median(core)
    obs_sd = statistics.pstdev(core) if len(core) > 1 else prior_sd
    diagnostics = {
        "n_raw": n,
        "n_core": len(core),
        "min": vs[0],
        "p10": vs[int(n * 0.10)] if n >= 10 else vs[0],
        "p50": vs[n // 2],
        "p90": vs[int(n * 0.90)] if n >= 10 else vs[-1],
        "max": vs[-1],
        "obs_median": obs_median,
        "obs_sd": obs_sd,
    }
    if n < MIN_OBS_FOR_FIT:
        # Below the floor: keep literature unchanged but record that we tried.
        return prior_mean, prior_sd, diagnostics, "kept_prior_below_threshold"

    prior_weight = 30.0  # rough pseudo-count: lit needs 30 disagreeing observations to fully overturn
    blended_mean = (prior_mean * prior_weight + obs_median * n) / (prior_weight + n)
    blended_sd = max((prior_sd * prior_weight + obs_sd * n) / (prior_weight + n), 5.0)

    # Discard if observed median is wildly outside any plausible prior range
    # (4σ from prior). That's a sign the taxonomy or sampling went wrong.
    if abs(obs_median - prior_mean) > 4 * prior_sd:
        return prior_mean, prior_sd, diagnostics, "kept_prior_outlier_obs"

    return blended_mean, blended_sd, diagnostics, "blended"


def main() -> int:
    species = _load_species()
    diagnostics: Dict[str, object] = {
        "run_at": datetime.now().isoformat(),
        "years_back": YEARS_BACK,
        "species": {},
    }
    current_year = date.today().year
    start_year = current_year - YEARS_BACK

    for sp in species:
        sp_id = str(sp["species_id"])
        base_c = float(sp.get("base_temp_c") or 5.0)
        LOG.info("=== %s (base %.1f°C) ===", sp_id, base_c)

        window = FLIGHT_WINDOWS.get(sp_id, (1, 12))
        in_window, tally = _gather(sp_id, start_year, current_year, *window)
        if not in_window:
            LOG.warning("  no observations found across iNat+GBIF, keeping literature")
            diagnostics["species"][sp_id] = {
                "status": "no_observations",
                "iNat_pulled": tally["inat_pulled"],
                "gbif_pulled": tally["gbif_pulled"],
            }
            continue

        dd_values = _compute_dd_values(in_window, base_c)
        LOG.info("  %d DD values computed", len(dd_values))

        prior_mean = float(sp.get("dd_threshold_mean") or 0.0)
        prior_sd = float(sp.get("dd_threshold_sd") or 30.0)
        fit = _robust_fit(dd_values, prior_mean, prior_sd) if prior_mean > 0 else None
        if fit is None:
            LOG.warning("  no observations and no literature prior; keeping current values")
            diagnostics["species"][sp_id] = {
                "status": "no_prior",
                "chain_used": tally.get("chain_used"),
                "iNat_pulled": tally["inat_pulled"],
                "gbif_pulled": tally["gbif_pulled"],
                "idigbio_pulled": tally["idigbio_pulled"],
                "n_in_window": len(in_window),
                "n_dd_computed": len(dd_values),
            }
            continue

        mean, sd, stats_d, fit_status = fit
        old_mean = sp.get("dd_threshold_mean")
        old_sd = sp.get("dd_threshold_sd")
        sp["dd_threshold_mean"] = round(mean, 1)
        sp["dd_threshold_sd"] = round(sd, 1)
        LOG.info("  %s: mean=%.1f sd=%.1f  (was %s/%s)  obs_median=%.1f n=%d",
                 fit_status, mean, sd, old_mean, old_sd, stats_d["obs_median"], stats_d["n_raw"])
        diagnostics["species"][sp_id] = {
            "status": fit_status,
            "chain_used": tally.get("chain_used"),
            "iNat_pulled": tally["inat_pulled"],
            "gbif_pulled": tally["gbif_pulled"],
            "idigbio_pulled": tally["idigbio_pulled"],
            "n_in_window": len(in_window),
            "n_dd_computed": len(dd_values),
            "old_mean": old_mean,
            "old_sd": old_sd,
            "new_mean": sp["dd_threshold_mean"],
            "new_sd": sp["dd_threshold_sd"],
            **stats_d,
        }

    _save_species(species)
    DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
    (DIAGNOSTICS_DIR / "dd_fit_diagnostics.json").write_text(
        json.dumps(diagnostics, indent=2), encoding="utf-8")
    LOG.info("wrote updated species.json and diagnostics")
    return 0


if __name__ == "__main__":
    sys.exit(main())
