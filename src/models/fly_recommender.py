from typing import Dict, List


def choose_stage(species: Dict[str, object], valid_hour: int) -> str:
    start = int(species.get("emergence_hr_start") or 0)
    end = int(species.get("emergence_hr_end") or 23)
    if valid_hour < start + 2:
        return "dun"
    if valid_hour > end - 2:
        return "spinner"
    return "emerger"


def recommend_flies(active_species: List[Dict[str, object]], species_data: Dict[str, Dict[str, object]], valid_hour: int) -> Dict[str, object]:
    if not active_species:
        return {
            "primary": {"pattern": "Pheasant Tail", "size": 14},
            "dropper": {"pattern": "Hare's Ear", "size": 16},
            "backup": [],
        }
    active_species = sorted(active_species, key=lambda s: s["probability"], reverse=True)
    primary_species_id = active_species[0]["id"]
    species = species_data.get(primary_species_id, {})
    patterns = species.get("fly_patterns", []) or []
    primary = None
    dropper = None
    backup = []
    stage = choose_stage(species, valid_hour)
    for pattern in patterns:
        if pattern.get("stage") == stage and primary is None:
            primary = pattern
        elif dropper is None and pattern.get("stage") in {"nymph", "emerger"}:
            dropper = pattern
        elif len(backup) < 2:
            backup.append(pattern)
    if primary is None and patterns:
        primary = patterns[0]
    if dropper is None and patterns:
        dropper = patterns[-1]
    return {
        "primary": primary or {"pattern": "Attractor", "size": 14},
        "dropper": dropper or {"pattern": "Pheasant Tail", "size": 16},
        "backup": backup,
    }
