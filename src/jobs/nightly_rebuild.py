import logging
from datetime import datetime, timezone
from src.db import get_connection

LOG = logging.getLogger(__name__)


def run_nightly_rebuild() -> None:
    conn = get_connection()
    cursor = conn.cursor()
    # Placeholder: mark rebuild time and vacuum the DB.
    cursor.execute("VACUUM")
    conn.commit()
    conn.close()
    LOG.info("Nightly rebuild completed at %s", datetime.now(timezone.utc).isoformat())
