"""``builtin:hana`` checks of the category ``database`` (the ``inspect*`` checks).

Each check is data: ``register(Check(...))`` at import, with its SQL as a
constant and every variable part a ``?`` bind (see :mod:`.base`).

Phase 2: the eight ``inspect*`` checks, one object of one schema in detail
(columns, keys, indexes, dependencies, definition). Category ``database``,
like :mod:`.database`; kept in a module of their own so the two can be written
side by side. A definition is read with ``lob_chars=20_000``.

The target is on-premise SAP HANA 2.0 (SPS07); a ``# on-prem:`` comment per
check says how it differs from hana-cli and where it ran. No check yet: the
module is registered so the checks of this phase are added here only.
:data:`CHECKS` holds them in registration order; its test file
(``tests/test_hanadb_checks_inspection.py``) pins their names.
"""

from __future__ import annotations

from agents.hana.checks.base import Check, register

CHECKS: tuple[Check, ...] = ()

register(*CHECKS)
