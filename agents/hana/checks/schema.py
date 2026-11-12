"""``builtin:hana`` checks of the category ``schema``.

Each check is data: ``register(Check(...))`` at import, with its SQL as a
constant and every variable part a ``?`` bind (see :mod:`.base`).

Phase 3: checks across one or two schemas; a check that compares two declares
``other_schema`` (same rule as ``schema``, bound as given).

The target is on-premise SAP HANA 2.0 (SPS07); a ``# on-prem:`` comment per
check says how it differs from hana-cli and where it ran. No check yet: the
module is registered so the checks of this phase are added here only.
:data:`CHECKS` holds them in registration order; its test file
(``tests/test_hanadb_checks_schema.py``) pins their names.
"""

from __future__ import annotations

from agents.hana.checks.base import Check, register

CHECKS: tuple[Check, ...] = ()

register(*CHECKS)
