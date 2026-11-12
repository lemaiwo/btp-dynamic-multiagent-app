"""The ``builtin:hana`` checks of ``agents/hana/checks/schema.py``:
the schema checks of Phase 3.

This file pins the module's check names; the invariants every check meets
(valid, a known category, the guard passes with ``fixed=True``, names
unique) are the shared tests of ``tests/test_hanadb_checks.py``. Add a
check: add its name here, in registration order.
"""

from __future__ import annotations

from agents.hana import checks as registry
from agents.hana.checks import schema

EXPECTED: tuple[str, ...] = ()


def test_the_schema_checks_are_pinned_and_registered() -> None:
    assert tuple(c.name for c in schema.CHECKS) == EXPECTED
    for check in schema.CHECKS:
        assert registry.get_check(check.name) is check
        assert check.category == "schema"
