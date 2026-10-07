"""What only Postgres can show about the IDE store's rewritten statements.

SKIPPED unless ``TEST_POSTGRES_URL`` names a throw-away database (see
``tests/pg.py``). Each test works in a schema of its own.

The statements here used ``UPDATE`` / ``DELETE ... RETURNING`` until SAP HANA
became a second database; they are now "select ``FOR UPDATE``, write under
the same conditions, read back". ``tests/test_hana_portable_sql.py`` shows
their Postgres spelling; these run the races (``tests/ide_concurrency.py``,
the same scenarios the HANA suite runs) on a real Postgres.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()

import agents.ide.models  # noqa: E402,F401  (registers the IDE tables)
from agents import db as agents_db  # noqa: E402
from tests import ide_concurrency, pg  # noqa: E402

pytestmark = pytest.mark.skipif(pg.postgres_url() is None, reason=pg.SKIP_REASON)


@pytest.fixture
async def sessions():
    async with pg.private_sessions() as maker:
        yield maker


@pytest.mark.parametrize("scenario", ide_concurrency.SCENARIOS, ids=lambda s: s.__name__)
async def test_race(sessions, scenario):
    await scenario(sessions)


async def test_the_row_lock_helpers_know_postgres(sessions):
    async with sessions() as db:
        assert agents_db.has_row_locks(db) is True
        # The compare stays in the UPDATE: no lock, no second read.
        assert agents_db.compares_lobs(db) is True
