"""Shared pytest setup.

Point the app at an isolated, freshly-migrated SQLite database so tests never
depend on (or mutate) the developer's real `driftless_cast.db` and always run
against the current schema. `DC_DB_PATH` is read at import time in
`src.db.queries`, so this must run before any `src.db` import — putting it at
module top-level in conftest guarantees that (conftest is imported before the
test modules that pull in `src.db`).
"""
import os
import tempfile
from pathlib import Path

_TEST_DB = Path(tempfile.gettempdir()) / "driftless_cast_test.db"
os.environ.setdefault("DC_DB_PATH", str(_TEST_DB))

import pytest

from src.db.queries import initialize_database


@pytest.fixture(scope="session", autouse=True)
def _init_test_db():
    # Idempotent: CREATE IF NOT EXISTS + additive column migrations, so a temp
    # DB left over from a previous run is brought up to the current schema.
    initialize_database()
    yield
