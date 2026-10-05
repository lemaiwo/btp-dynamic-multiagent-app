"""The one place a test process chooses its database.

``agents.db`` reads ``DATABASE_URL`` when it is imported and builds its engine
then, so a process has exactly one database however many test modules it
loads. Test modules used to name a file under ``tests/`` each and delete it at
import (``TEST_DB.unlink()``) to start fresh. Importing is all pytest's
collection does, so any second process that merely *collected* the same files
(another run, ``--collect-only``, an editor's test discovery) deleted the file
a running process had open. SQLite keeps reading the orphaned inode and
answers every later write with "attempt to write a readonly database".

So no test module names or deletes a database file any more. Each calls
:func:`use_test_database` before its first ``agents`` import:

* under pytest, ``tests/conftest.py`` has already called it and bound the
  engine, and the call in the module is a no-op;
* run as a script (``python tests/test_x.py``), the first call gives that
  process a fresh database.

Either way the file lives in a directory made for this process alone, which
is removed when the process exits. No name is shared, so nothing another
process does can touch it.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

# Holds the pid that owns DATABASE_URL. A pid rather than a flag: a child
# process inherits the environment, and a script-style suite started from a
# test must get a database of its own, not its parent's.
_OWNER = "AGENTS_TEST_DB_OWNER"
_PREFIX = "sqlite+aiosqlite:///"


def use_test_database() -> Path:
    """Point ``DATABASE_URL`` at this process's own SQLite file; return its path.

    Idempotent per process. Overrides rather than defaults: a developer's
    shell may export a ``DATABASE_URL`` that the tests must never write to.
    """
    url = os.environ.get("DATABASE_URL", "")
    if os.environ.get(_OWNER) == str(os.getpid()) and url.startswith(_PREFIX):
        return Path(url[len(_PREFIX):])
    directory = Path(tempfile.mkdtemp(prefix="agents-tests-"))
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    path = directory / "test.db"
    os.environ["DATABASE_URL"] = f"{_PREFIX}{path}"
    os.environ[_OWNER] = str(os.getpid())
    return path
