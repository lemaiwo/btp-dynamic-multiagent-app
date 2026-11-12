"""The ``builtin:hana`` checks of ``agents/hana/checks/inspection.py``:
the eight ``inspect*`` checks of Phase 2 (category ``database``).

This file pins the module's check names; the invariants every check meets
(valid, a known category, the guard passes with ``fixed=True``, names
unique) are the shared tests of ``tests/test_hanadb_checks.py``. Add a
check: add its name here, in registration order.
"""

from __future__ import annotations

from agents.hana import checks as registry
from agents.hana.checks import inspection

EXPECTED: tuple[str, ...] = ()


def test_the_inspection_checks_are_pinned_and_registered() -> None:
    assert tuple(c.name for c in inspection.CHECKS) == EXPECTED
    for check in inspection.CHECKS:
        assert registry.get_check(check.name) is check
        assert check.category == "database"
