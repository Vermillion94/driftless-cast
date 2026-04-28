import json
from pathlib import Path
from src.db import initialize_database, upsert_reach

SEED_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "seed" / "reaches.json"


def bootstrap_reaches() -> None:
    initialize_database()
    with open(SEED_PATH, "r", encoding="utf-8") as handle:
        reaches = json.load(handle)
    for reach in reaches:
        upsert_reach(reach)


if __name__ == "__main__":
    bootstrap_reaches()
