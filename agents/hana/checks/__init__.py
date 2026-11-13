"""The closed registry of ``builtin:hana`` checks.

An agent runs a check by name (``run_check``); it never writes SQL. The data
model, the argument rules and the registry live in :mod:`.base`; each category
module (:mod:`.monitoring`, :mod:`.performance`, :mod:`.system`,
:mod:`.backup`, :mod:`.database`, :mod:`.inspection` (category ``database``),
:mod:`.schema`, :mod:`.security`) registers its checks at import, which this
package does below.
``python -m agents.hana.checks --dump`` prints the statements in the form
``scripts/probe_hana_connectivity.py --checks-file`` reads.

The SQL of these checks is derived from SAP hana-cli 4.202609.0
(https://github.com/SAP-samples/hana-developer-cli-tool-example), bin/<command>.js and
utils/*.js, Copyright SAP SE or an SAP affiliate company, licensed under the Apache
License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0). Changes: rewritten
as Python constants, literals replaced by bind parameters, adapted to on-premise SAP
HANA 2.0 views where the landscape run showed a difference. Each check names its
source file in `Check.source`.
"""

from __future__ import annotations

# The category modules register their checks at import.
from agents.hana.checks import (  # noqa: F401
    backup,
    database,
    inspection,
    monitoring,
    performance,
    schema,
    security,
    system,
)
from agents.hana.checks.base import (
    ARGS,
    CATEGORIES,
    Arg,
    Check,
    InvalidArgument,
    Statement,
    all_checks,
    bind_values,
    build_answer,
    dump_entries,
    get_check,
    lookup,
    register,
    resolve_args,
    visible_checks,
)

__all__ = [
    "ARGS",
    "CATEGORIES",
    "Arg",
    "Check",
    "InvalidArgument",
    "Statement",
    "all_checks",
    "bind_values",
    "build_answer",
    "dump_entries",
    "get_check",
    "lookup",
    "register",
    "resolve_args",
    "visible_checks",
]
