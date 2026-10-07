"""What SAP HANA would be sent, seen from the SQLite suite.

HANA refuses three things the other two databases accept: ``UPDATE`` /
``DELETE ... RETURNING``, any comparison on an ``NCLOB`` column (every
``Text`` column of the models) and ordering, grouping or ``DISTINCT`` on
one. None of that shows on SQLite, so :func:`recorded` watches the
statements a piece of code executes on the suite's own engine, compiles each
for the HANA dialect and collects what HANA would refuse. No HANA is needed:
the dialect only compiles.

Not a proof that a statement *runs* on HANA (``tests/test_hana_integration.py``
is, on a real container); it keeps the known refusals from coming back.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy import Text, event
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import operators, visitors
from sqlalchemy.sql.elements import BinaryExpression, ClauseElement, UnaryExpression
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.sql.selectable import Select
from sqlalchemy_hana.dialect import HANAHDBCLIDialect

HANA = HANAHDBCLIDialect()
# The only things HANA lets a statement ask of a LOB.
_NULL_TESTS = (operators.is_, operators.is_not)
# Refused by HANA and never sent to it, each for a stated reason. Exact
# texts: anything else that compares these columns is still reported.
NEVER_ON_HANA = frozenset({
    # agents.db._resync_ide_revisions: a repair for rows written by app
    # versions that never ran on HANA; init_db leaves it out there.
    "comparison on a LOB column: "
    "ide_file_revisions.proposed_source = ide_workspace_files.proposed_source",
})


def hana_sql(statement: ClauseElement) -> str:
    """``statement`` as the HANA dialect writes it, on one line."""
    return " ".join(str(statement.compile(dialect=HANA)).split())


def _is_lob(element: object) -> bool:
    return isinstance(getattr(element, "type", None), Text) and not isinstance(
        element, FunctionElement
    )


def refusals(statement: ClauseElement) -> list[str]:
    """Why HANA would refuse ``statement``; empty when it would not."""
    found: list[str] = []
    sql = hana_sql(statement)
    if " RETURNING " in f" {sql} ":
        found.append(f"RETURNING: {sql}")
    for element in visitors.iterate(statement):
        if isinstance(element, BinaryExpression) and element.operator not in _NULL_TESTS:
            if any(_is_guarded(side) for side in (element.left, element.right)):
                # agents.db.text_unchanged, on a database that compares a
                # LOB; on HANA it locks and compares in Python instead.
                continue
            if any(
                _is_lob(side) and getattr(side, "table", None) is not None
                for side in (element.left, element.right)
            ):
                found.append(f"comparison on a LOB column: {element}")
        if isinstance(element, Select):
            for clause in (*element._order_by_clauses, *element._group_by_clauses):
                inner = clause.element if isinstance(clause, UnaryExpression) else clause
                if _is_lob(inner):
                    found.append(f"ORDER BY / GROUP BY a LOB column: {inner}")
            if element._distinct and any(_is_lob(c) for c in element.selected_columns):
                found.append(f"DISTINCT over a LOB column: {sql}")
    return [reason for reason in found if reason not in NEVER_ON_HANA]


def _is_guarded(element: object) -> bool:
    from agents.db import ComparedText

    return isinstance(getattr(element, "type", None), ComparedText)


# --- every statement of the suite -----------------------------------------------

# What the statements executed since the last ``take_refused`` would be
# refused for. Filled by ``watch_engine``'s listener; tests/conftest.py
# empties and checks it around every test.
_refused: list[str] = []


def watch_engine() -> None:
    """Check every statement the suite's engine executes, for the whole
    process. Each one is compiled a second time, for HANA; measured on the
    whole suite that is a few percent of its run time."""
    from agents.db import engine

    def listen(conn, clauseelement, multiparams, params, execution_options):
        if not isinstance(clauseelement, ClauseElement):
            return
        try:
            _refused.extend(refusals(clauseelement))
        except Exception as exc:  # noqa: BLE001 -- e.g. no HANA spelling at all
            _refused.append(
                f"does not compile for HANA ({type(exc).__name__}): "
                f"{' '.join(str(clauseelement).split())[:200]}"
            )

    event.listen(engine.sync_engine, "before_execute", listen)


def take_refused() -> list[str]:
    """What was refused since the last call; empties the list."""
    found = list(dict.fromkeys(_refused))
    _refused.clear()
    return found


@dataclass
class Recorded:
    """The statements one block executed, as HANA would get them (``sql``)
    and as Postgres would (``postgres``: the suite has no Postgres either,
    so a statement that changed is at least read in both spellings)."""

    sql: list[str] = field(default_factory=list)
    postgres: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)

    def matching(self, *parts: str) -> list[str]:
        return [s for s in self.sql if all(p in s for p in parts)]

    def on_postgres(self, *parts: str) -> list[str]:
        return [s for s in self.postgres if all(p in s for p in parts)]


@contextmanager
def recorded() -> Iterator[Recorded]:
    """Record every statement executed on the suite's engine inside the block."""
    from agents.db import engine

    seen = Recorded()

    def listen(conn, clauseelement, multiparams, params, execution_options):
        if not isinstance(clauseelement, ClauseElement):
            return
        seen.sql.append(hana_sql(clauseelement))
        seen.postgres.append(
            " ".join(str(clauseelement.compile(dialect=postgresql.dialect())).split())
        )
        seen.refused.extend(refusals(clauseelement))

    event.listen(engine.sync_engine, "before_execute", listen)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_execute", listen)
