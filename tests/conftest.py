"""Global pytest configuration.

Ensures every test session uses a temporary database directory, so integration
tests that write to the DB can never accidentally touch data/jobagent.db.

Individual tests that need a fresh isolated DB should use the ``tmp_db``
fixture defined in their own test file, which creates a per-test temp file.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True, scope="session")
def _guard_production_db(tmp_path_factory):
    """Redirect the DB to a session-scoped temp file for the whole test run.

    This ensures that even tests which don't use the ``tmp_db`` fixture (e.g.
    unit tests that indirectly call get_session()) never write to the real DB.
    The per-test ``tmp_db`` fixture can still override this for full isolation.
    """
    session_db = tmp_path_factory.mktemp("session_db") / "session_test.db"
    os.environ["JOBAGENT_DB_PATH"] = str(session_db)

    # Reset cached singletons so they pick up the new path
    import jobagent.db.engine as eng
    import jobagent.config as cfg
    eng._engine = None  # type: ignore[attr-defined]
    cfg.settings = cfg.Settings()

    from jobagent.db.engine import create_tables
    create_tables()

    yield

    # Cleanup: reset to defaults so post-test code (if any) doesn't break
    os.environ.pop("JOBAGENT_DB_PATH", None)
    eng._engine = None  # type: ignore[attr-defined]
    cfg.settings = cfg.Settings()
