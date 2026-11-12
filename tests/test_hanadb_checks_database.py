"""The ``builtin:hana`` checks of ``agents/hana/checks/database.py``:
the catalogue checks of Phase 2 (tables, views, procedures, sequences, ...).

This file pins the module's check names; the invariants every check meets
(valid, a known category, the guard passes with ``fixed=True``, names
unique) are the shared tests of ``tests/test_hanadb_checks.py``. Add a
check: add its name here, in registration order.
"""

from __future__ import annotations

from agents.hana import checks as registry
from agents.hana.checks import database

EXPECTED: tuple[str, ...] = ()


def test_the_database_checks_are_pinned_and_registered() -> None:
    assert tuple(c.name for c in database.CHECKS) == EXPECTED
    for check in database.CHECKS:
        assert registry.get_check(check.name) is check
        assert check.category == "database"
