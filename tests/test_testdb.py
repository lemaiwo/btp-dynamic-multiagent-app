"""The database rule of the test suite (see ``tests/testdb.py``).

A test module that names a database file and deletes it at import breaks
every other process that has the file open: importing is all a second
``pytest`` (or an editor's test discovery) needs to do.

Run:  python -m pytest tests/test_testdb.py -q
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.testdb import use_test_database  # noqa: E402

use_test_database()

TESTS = ROOT / "tests"
# A header may still default to an in-memory database (it names no file and
# is a no-op under pytest); nothing else may set the variable in os.environ.
SETS_URL = re.compile(
    r"""os\.environ\[["']DATABASE_URL["']\]\s*="""
    r"""|os\.environ\.setdefault\(\s*["']DATABASE_URL["'](?![^)]*:memory:)""",
)
DELETES_DB = re.compile(r"""\.db["']?\s*\)*\.unlink\(|\bTEST_DB\.unlink\(""")


def _headers() -> list[Path]:
    return sorted(p for p in TESTS.glob("test_*.py") if p.name != "test_testdb.py")


@pytest.mark.parametrize("path", _headers(), ids=lambda p: p.name)
def test_no_test_module_sets_or_deletes_a_database_file(path):
    source = path.read_text(encoding="utf-8")
    assert not SETS_URL.search(source), (
        f"{path.name} sets DATABASE_URL itself; call "
        "tests.testdb.use_test_database() before the first agents import instead"
    )
    assert not DELETES_DB.search(source), (
        f"{path.name} deletes a database file; another process may have it open"
    )


def test_the_engine_is_bound_to_this_process_own_file():
    from agents import db

    path = use_test_database()
    assert str(db.engine.url.database) == str(path)
    assert path.parent.name.startswith("agents-tests-")
    assert TESTS not in path.parents
    assert os.environ["DATABASE_URL"].endswith(str(path))


def test_a_second_call_keeps_the_database():
    assert use_test_database() == use_test_database()


def test_a_child_process_gets_its_own_database_and_removes_it():
    """A script-style suite started while this one runs inherits the
    environment; it must not write to (or delete) this process's file."""
    code = (
        "import os, sqlite3\n"
        "from tests.testdb import use_test_database\n"
        "p = use_test_database()\n"
        "assert use_test_database() == p\n"
        "sqlite3.connect(p).execute('create table t (a)').connection.commit()\n"
        "print(p)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=dict(os.environ),
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    child = Path(out.stdout.strip())
    assert child != use_test_database()
    assert not child.parent.exists(), "the child's directory outlived the child"


def test_collecting_in_a_second_process_leaves_this_database_writable():
    """The defect: a second process that only *collected* the suite deleted
    the file this process had open, and every later write failed with
    "attempt to write a readonly database"."""
    path = use_test_database()
    con = sqlite3.connect(path)
    try:
        con.execute("create table if not exists _testdb_probe (a)")
        con.commit()
        inode = path.stat().st_ino
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q",
             "-p", "no:cacheprovider", str(TESTS)],
            cwd=ROOT, env=dict(os.environ), capture_output=True, text=True, timeout=300,
        )
        assert " collected" in out.stdout, out.stdout[-2000:] + out.stderr[-2000:]
        assert path.exists() and path.stat().st_ino == inode
        con.execute("insert into _testdb_probe values (1)")
        con.commit()
    finally:
        con.execute("drop table if exists _testdb_probe")
        con.commit()
        con.close()
