"""``builtin:hana`` checks of the category ``database``.

Each check is data: ``register(Check(...))`` at import, with its SQL as a
constant and every variable part a ``?`` bind (see :mod:`.base`).

Phase 2: catalogue reads (tables, views, procedures, functions, sequences,
indexes, ...) of a schema the caller names, as hana-cli's catalogue commands
read them. An object's definition (view or procedure source) is read with
``lob_chars=20_000``; the answer names a cut column in ``truncated_columns``.

The target is on-premise SAP HANA 2.0 (SPS07); a ``# on-prem:`` comment per
check says how it differs from hana-cli and where it ran. No check yet: the
module is registered so the checks of this phase are added here only.
:data:`CHECKS` holds them in registration order; its test file
(``tests/test_hanadb_checks_database.py``) pins their names.
"""

from __future__ import annotations

from agents.hana.checks.base import Check, register

CHECKS: tuple[Check, ...] = ()

register(*CHECKS)
