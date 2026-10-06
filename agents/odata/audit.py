"""Where the writes of ``builtin:odata`` are recorded: the ``odata_audit_log`` table.

``StoredWriteRecorder`` is the ``WriteRecorder`` of ``agents.odata.tools``
that stores. ``agents.builtins`` hands the process-wide one
(``stored_recorder()``) to every ``builtin:odata`` toolset; a toolset
without a storing recorder sends no write at all (``audit_not_configured``).

Two steps per modifying call, each in a short transaction of its own on its
own database session (``agents.db.SessionLocal``), never the caller's:

1. ``intent`` INSERTS one row with outcome ``intent`` and returns its id.
   It runs after every check of the write and BEFORE a client is built or
   anything is sent. If the row cannot be written, the write is not sent.
   ``phase`` is NULL in that row: an open intent says nothing about
   whether a modifying request left.
2. ``result`` UPDATES exactly that row -- outcome, phase, HTTP status,
   created key, ``finished_at`` -- and only while its outcome is still
   ``intent``. A row is therefore finalised exactly once; a second result
   for the same row changes nothing and is logged.

Two statements update a row, and no other: ``result`` (above), and
``abandon``, which closes the intent of a call whose write was never sent
(see below) as ``refused`` / ``token``. Both are conditional on the outcome
still being ``intent``, so whichever comes first is the only one that
changes the row. Only the retention purge (``agents.db.purge_odata_audit``)
deletes rows.

How to read a row that is still ``intent``
------------------------------------------
* Normally: **the write may have been sent; the process stopped, or the run
  was cancelled, before the outcome was recorded.** Nobody knows whether
  SAP applied it -- look at the entity (the row names it: service, target,
  key). Its ``phase`` is NULL: the row does not claim that nothing left.
  The same holds when the result could not be stored (after one retry):
  then the ``agents.odata.audit`` logger carries an ERROR line "result NOT
  recorded" or "result not recorded within ..." with the row's ``call_id``
  and the outcome that was known.
* An intent whose write was never sent -- the run was cancelled while the
  intent was being stored, or storing it failed or took too long although
  the row was committed -- is closed where possible (``abandon``): the row
  then reads **``refused`` with phase ``token`` and no HTTP status, which
  after an interrupted intent means "never sent"**. Only when that close
  fails too does such a row stay ``intent``; the ``agents.odata.audit``
  logger then has, for its ``call_id``, the line **"intent interrupted,
  the write was not sent"** or "intent not confirmed ...; the write was not
  sent" (logs are short-lived, which is why the row is closed first).

What a row holds
----------------
Names, and the key of the entity: agent, run id, service, entity set,
operation, the key the call named and (for a create) the key SAP answered,
the NAMES of the body fields, ``sent_as`` (whose credential SAP saw) and
``run_principal`` (the principal of the run; in a job the run-as user, who
can differ from the owner of the token the run carries), the SHA-256 of the
token sent. Never a body value, a token, a cookie or an ETag. Key values
and principals can be personal data: the table is read through the
admin-only route ``GET /admin/api/odata/audit`` and purged by age.

Retention
---------
``ODATA_AUDIT_RETENTION_DAYS`` (default 365). ``0`` keeps the rows forever
(``app.py`` logs that as a WARNING at start-up). Any other value is raised
to ``RETENTION_MIN_DAYS`` and capped at ``RETENTION_MAX_DAYS``; a negative
value or one that is not an integer is a mistake and falls back to the
default with a warning -- a typo must neither switch the purge off nor
empty the log. The purge runs with the IDE's retention pass in ``app.py``
(at start-up and once a day), in a session of its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import update

from agents import db as agents_db
from agents.db import ODATA_AUDIT_MAX_RETENTION_DAYS, ODataAuditLog
from agents.ide.store import env_int

from .tools import WRITE_OUTCOMES, WriteAudit, audit_logger
from .tools import _audit_text as audit_text

__all__ = [
    "DRAIN_TIMEOUT_SECONDS",
    "ODATA_AUDIT_RETENTION_DAYS",
    "RETENTION_DEFAULT_DAYS",
    "RETENTION_MAX_DAYS",
    "RETENTION_MIN_DAYS",
    "StoredWriteRecorder",
    "drain",
    "retention_days",
    "stored_recorder",
]

RETENTION_ENV = "ODATA_AUDIT_RETENTION_DAYS"
RETENTION_DEFAULT_DAYS = 365
RETENTION_MIN_DAYS = 7
RETENTION_MAX_DAYS = ODATA_AUDIT_MAX_RETENTION_DAYS
# How long a retired registry build or the shutdown waits for results that
# are still being stored. Each is one UPDATE; this only bounds a database
# that hangs.
DRAIN_TIMEOUT_SECONDS = 10.0
# Storing a result is tried twice: a database that blinked must not turn a
# known outcome into an open intent.
RESULT_RETRY_DELAY_SECONDS = 0.5

_PHASES = ("token", "write")


def retention_days() -> int:
    """The effective retention in days; ``0`` = keep forever.

    Read with the IDE's own parser (``agents.ide.store.env_int``): unset or
    blank is the default, and so is a value that is not an integer, with a
    warning that names the variable but not its value. See the module
    docstring for the floor, the cap and negative values.
    """
    value = env_int(RETENTION_ENV, RETENTION_DEFAULT_DAYS)
    if value == 0:
        return 0
    if value < 0:
        audit_logger.warning(
            "%s is negative; using the default %d", RETENTION_ENV, RETENTION_DEFAULT_DAYS
        )
        return RETENTION_DEFAULT_DAYS
    if value < RETENTION_MIN_DAYS:
        audit_logger.warning(
            "%s is below the minimum; using %d days", RETENTION_ENV, RETENTION_MIN_DAYS
        )
        return RETENTION_MIN_DAYS
    if value > RETENTION_MAX_DAYS:
        audit_logger.warning(
            "%s is above the maximum; using %d days", RETENTION_ENV, RETENTION_MAX_DAYS
        )
        return RETENTION_MAX_DAYS
    return value


# Read at import, like the IDE's retention settings.
ODATA_AUDIT_RETENTION_DAYS = retention_days()


_CUT_MARK = "~"
_CUT_DIGEST_CHARS = 16
_cut_warned: set[str] = set()


def _fit(value: str | None, column: Any) -> str | None:
    """``value`` as it fits its column; a longer one is cut, visibly.

    SQLite ignores a VARCHAR limit, Postgres refuses the row -- and a
    refused intent row refuses the write. A principal of any length must
    not lock its user out, so it is cut: prefix + ``~`` + the first 16 hex
    digits of the SHA-256 of the whole value, together exactly the column
    width. Two long values with a common prefix therefore stay distinct,
    and a cut value is recognisable as one. Logged once per column (the
    column's name, never the value).

    A column no wider than the mark and digest (17 characters) has no room
    for a prefix: there the value is cut plainly to the width -- a negative
    slice would otherwise return a text LONGER than the column, which
    Postgres refuses.
    """
    if value is None:
        return None
    text_value = str(value)
    width = column.type.length
    if len(text_value) <= width:
        return text_value
    if width <= len(_CUT_MARK) + _CUT_DIGEST_CHARS:
        if column.name not in _cut_warned:
            _cut_warned.add(column.name)
            audit_logger.warning(
                "odata audit: a value for column '%s' is longer than %d characters; it is "
                "stored cut plainly (the column has no room for a digest)",
                column.name,
                width,
            )
        return text_value[:width]
    if column.name not in _cut_warned:
        _cut_warned.add(column.name)
        audit_logger.warning(
            "odata audit: a value for column '%s' is longer than %d characters; it is "
            "stored cut, ending in '%s' and a digest of the whole value",
            column.name,
            width,
            _CUT_MARK,
        )
    digest = hashlib.sha256(text_value.encode("utf-8", "surrogatepass")).hexdigest()
    tail = _CUT_MARK + digest[:_CUT_DIGEST_CHARS]
    return text_value[: width - len(tail)] + tail


def _call_id(record: WriteAudit) -> str:
    """The call id, never cut: it is what ties the result to its intent."""
    width = ODataAuditLog.__table__.c.call_id.type.length
    if not isinstance(record.call_id, str) or not 1 <= len(record.call_id) <= width:
        raise ValueError("the call id does not fit the audit row")
    return record.call_id


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StoredWriteRecorder:
    """The ``WriteRecorder`` that writes ``odata_audit_log`` rows.

    It also owns the toolsets' result tasks (``tasks``): a result that is
    still being stored when its caller has gone (a timeout, a second
    cancel) is held here, not in a toolset's closure, so that a retired
    registry build and the shutdown can wait for it (``drain``).
    """

    # The marker `agents.odata.tools` asks for before it sends any write.
    records_writes = True

    def __init__(self, session_factory: Callable[[], Any] | None = None) -> None:
        self._session_factory = session_factory
        self.tasks: set[asyncio.Task[None]] = set()

    def _session(self) -> Any:
        # Looked up per call: the app's own session maker, a new session
        # each time, never one a caller holds.
        factory = self._session_factory or agents_db.SessionLocal
        return factory()

    async def intent(self, record: WriteAudit) -> int:
        """Insert the intent row and return its id. Raises when it cannot."""
        columns = ODataAuditLog.__table__.c
        row = ODataAuditLog(
            created_at=_utcnow(),
            call_id=_call_id(record),
            agent=_fit(record.agent, columns.agent) or "",
            run_id=_fit(record.run_id, columns.run_id),
            sent_as=_fit(record.sent_as, columns.sent_as) or "",
            run_principal=_fit(record.run_principal, columns.run_principal),
            token_digest=_fit(record.token_digest, columns.token_digest),
            service=_fit(record.service, columns.service) or "",
            target=_fit(record.target, columns.target) or "",
            operation=_fit(record.operation, columns.operation) or "",
            key_json=_json(record.key),
            body_fields_json=_json(list(record.fields)),
            # Not "token": an open intent must not claim that nothing left.
            phase=None,
            outcome="intent",
        )
        async with self._session() as session:
            session.add(row)
            await session.flush()
            row_id = row.id
            await session.commit()
        if not isinstance(row_id, int):
            raise RuntimeError("the audit row got no id")
        return row_id

    async def _close(self, statement: Any) -> int:
        """Run one conditional UPDATE in its own session; the rows changed."""
        async with self._session() as session:
            done = await session.execute(statement)
            await session.commit()
        return int(done.rowcount or 0)

    async def result(self, token: Any, record: WriteAudit) -> None:
        """Finalise the intent row ``token``: once, and only that row.

        A failure to store is retried once after
        ``RESULT_RETRY_DELAY_SECONDS`` (the UPDATE is conditional, so a
        first attempt that did land makes the second a no-op that is
        recognised below); the second failure is raised, and the toolset
        writes the record to the log.
        """
        if not isinstance(token, int) or isinstance(token, bool):
            raise TypeError("the audit token is not a row id")
        if record.outcome not in WRITE_OUTCOMES or record.phase not in _PHASES:
            raise ValueError("not a result record")
        # `type(...) is int`: True is an int too, and no HTTP status.
        status = record.status if type(record.status) is int else None
        statement = (
            update(ODataAuditLog)
            .where(
                ODataAuditLog.id == token,
                ODataAuditLog.call_id == record.call_id,
                ODataAuditLog.outcome == "intent",
            )
            .values(
                outcome=record.outcome,
                phase=record.phase,
                http_status=status,
                created_key_json=_json(record.created_key),
                finished_at=_utcnow(),
            )
            .execution_options(synchronize_session=False)
        )
        retried = False
        try:
            changed = await self._close(statement)
        except Exception:  # noqa: BLE001 - once more, then the caller logs the record
            retried = True
            await asyncio.sleep(RESULT_RETRY_DELAY_SECONDS)
            changed = await self._close(statement)
        if changed != 1:
            if retried and await self._is_result_of(token, record):
                # The first attempt was committed although it raised.
                changed = 1
        if changed != 1:
            # Already finalised (a row is finalised once), purged, or not
            # this call's row. Nothing was changed; the record is kept here.
            audit_logger.error(
                "odata audit: result NOT recorded, row %d is not this call's open intent: %s",
                token,
                audit_text(record),
            )
            return
        audit_logger.info("odata audit: row %d: %s", token, audit_text(record))

    async def _is_result_of(self, token: int, record: WriteAudit) -> bool:
        async with self._session() as session:
            row = await session.get(ODataAuditLog, token)
            return (
                row is not None
                and row.call_id == record.call_id
                and row.outcome == record.outcome
                and row.phase == record.phase
            )

    async def abandon(self, record: WriteAudit) -> bool:
        """Close the intent row of a call whose write was NOT sent.

        For an intent that was interrupted or not confirmed: the toolset
        sent nothing, but the row may have been committed. By ``call_id``
        (there is no row id to go by) and only while the row is still an
        intent: it becomes ``refused`` / ``token`` -- "never sent". True
        when a row was closed; False when there was none to close.
        """
        statement = (
            update(ODataAuditLog)
            .where(
                ODataAuditLog.call_id == _call_id(record),
                ODataAuditLog.outcome == "intent",
            )
            .values(outcome="refused", phase="token", finished_at=_utcnow())
            .execution_options(synchronize_session=False)
        )
        closed = await self._close(statement) == 1
        if closed:
            audit_logger.info(
                "odata audit: unsent intent closed as refused/token: call_id=%s", record.call_id
            )
        return closed

    async def drain(self, timeout: float = DRAIN_TIMEOUT_SECONDS) -> int:
        """Wait up to ``timeout`` seconds for the results still being stored.

        Returns how many were still running when the time was up (they go
        on; nothing is cancelled). Never raises, so a retire or a shutdown
        is not held up by more than the timeout.
        """
        loop = asyncio.get_running_loop()
        pending = [t for t in self.tasks if not t.done() and t.get_loop() is loop]
        if not pending:
            return 0
        _done, left = await asyncio.wait(pending, timeout=max(0.0, timeout))
        if left:
            audit_logger.warning(
                "odata audit: %d result(s) still being stored after %ss", len(left), timeout
            )
        return len(left)


_stored = StoredWriteRecorder()


def stored_recorder() -> StoredWriteRecorder:
    """The recorder of this process: every ``builtin:odata`` toolset the
    registry builds records through it, across reloads."""
    return _stored


async def drain(timeout: float = DRAIN_TIMEOUT_SECONDS) -> int:
    """``stored_recorder().drain(timeout)``, for the app's shutdown."""
    return await _stored.drain(timeout)
