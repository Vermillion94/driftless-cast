"""
One-off enrichment: pull WI DNR trout regulations per WI reach, store a
summary on the reach table.

For MN and IA reaches, we'd do the same — except MN Geospatial Commons is
behind a Radware bot-manager captcha that blocks automated fetches. Manual
steps to integrate MN would be: download `env_trout_stream_kittle` shapefile
from the MN Geo Hub via a browser, convert the DBF attribute table to CSV,
commit that to `data/dnr_seed/mn_trout_streams.csv`, then extend this script
to look streams up in the CSV. Same shape for Iowa DNR.
"""
from __future__ import annotations

import json
import logging
import sys

from src.db import get_connection, initialize_database, load_reaches
from src.ingest.wi_dnr import fetch_trout_regs, summarize

LOG = logging.getLogger("enrich_dnr")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


def main() -> int:
    initialize_database()
    reaches = load_reaches()
    conn = get_connection()
    wi_count = 0
    other_count = 0
    for reach in reaches:
        rid = reach["reach_id"]
        if reach.get("state") != "WI":
            other_count += 1
            continue
        stream = reach.get("stream_name", "")
        rows = fetch_trout_regs(stream)
        summary = summarize(rows)
        if summary:
            LOG.info("%s (%s): %d segments, tier=%s, top=%s",
                     rid, stream, summary["segments_matched"],
                     summary["inferred_tier"], summary["top_reg_category"])
            conn.execute(
                "UPDATE reach SET dnr_summary = ? WHERE reach_id = ?",
                (json.dumps(summary), rid),
            )
            wi_count += 1
        else:
            LOG.info("%s (%s): no WI DNR match", rid, stream)
    conn.commit()
    conn.close()
    LOG.info("enriched %d WI reaches; %d non-WI skipped (MN/IA need manual shapefile import)",
             wi_count, other_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
