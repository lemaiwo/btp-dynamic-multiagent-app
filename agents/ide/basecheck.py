"""The SAP base of a proposed object: is it new, or a change to SAP?

A run can write ``src/CLAS/zcl_x.clas.abap`` without opening it first. The
workspace then saves it as ``new`` with no ``origin_source``, and the
developer would review a whole class as an addition while it really changes
one that exists. :func:`check_bases` runs at the end of a change run, after
the workspace save and while the run still holds the session lock, and
asks SAP for every such file:

- found -> ``origin_source`` (the diff base), ``origin_version``,
  ``base_status="sap"``; the state becomes ``modified`` (``read`` when the
  proposal equals SAP's source);
- ``arc1.is_not_found`` -> ``base_status="absent"``; the state stays ``new``;
- any other ARC-1 error, or an answer that is no source (empty, or a
  one-line "does not exist"/"not found" text, :func:`usable_source`) ->
  ``base_status="unknown"`` (the UI says the base could not be read);
- a call that never reached SAP -- no user token, no ARC-1 configuration,
  a read-only refusal -- says nothing about the object: the row stays
  unchecked (``None``) and the next run tries again. The first such answer
  ends the check, since every further call would get the same one.

Reads go through ``arc1.get_arc1_client(target, destination,
policy="change")``, so the read-only check runs first and SAP is read as the
signed-in user (the run task carries the request's JWT). Nothing is written
to SAP. At most :data:`BASE_CHECK_MAX` rows per run and
:data:`BASE_CHECK_TIMEOUT_S` in total: rows left over stay unchecked for the
next run. ``absent`` and ``unknown`` are not final: a row whose
``base_checked_at`` is older than its latest revision is asked again
(unchecked rows first), so a wrong answer does not stick once the file
changes. Each write is a conditional UPDATE (still no origin, the base
status it was selected with, same revision), so a row that got its base in
between is left alone.

:func:`apply_base` is the one place that sets a row's base from a SAP read;
the ``open_object`` tool and the open/refresh routes use it too.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import and_, select, update

from agents import deep
from agents.db import SessionLocal
from agents.ide import arc1
from agents.ide.models import IdeFileRevision, IdeWorkspaceFile, utcnow
from agents.ide.paths import object_for
from agents.ide.readonly import CHANGE

log = logging.getLogger(__name__)

BASE_CHECK_MAX = 20
# The whole check, not one call: it runs between the agent's last answer and
# ``done``, so the developer waits for it.
BASE_CHECK_TIMEOUT_S = 120.0

SAP, ABSENT, UNKNOWN = "sap", "absent", "unknown"


def read_args(type_: str, name: str, include: str | None) -> dict[str, Any]:
    """``SAPRead`` arguments; the type is always explicit (the read-only
    guard requires it)."""
    args: dict[str, Any] = {"type": type_, "name": name}
    if include:
        args["include"] = include
    return args


def base_values(
    proposed_source: str | None, source: str, version: str | None
) -> dict[str, Any]:
    """The columns a SAP read sets: the base, and the state that follows
    from it (as ``workspace.save_state`` would compute it)."""
    values: dict[str, Any] = {
        "origin_source": source,
        "origin_version": version,
        "base_status": SAP,
        "base_checked_at": utcnow(),
    }
    if proposed_source is None or proposed_source == source:
        values.update(state="read", proposed_source=None)
    else:
        values.update(state="modified")
    return values


def apply_base(row: IdeWorkspaceFile, source: str, version: str | None) -> None:
    """Set ``row``'s base from a SAP read. A proposal is never replaced by
    SAP's source: a differing one is kept; one byte-equal to it is no change
    and is dropped (state ``read``; its revisions stay in the history)."""
    for key, value in base_values(row.proposed_source, source, version).items():
        setattr(row, key, value)


# A "source" that is really a one-line message from ARC-1 or SAP.
_NOT_A_SOURCE = re.compile(r"does\s+not\s+exist|not\s+found", re.IGNORECASE)
# An HTML error page (a gateway or the identity provider answering in
# ARC-1's place). No ABAP or CDS source starts like this.
_ERROR_PAGE = ("<html", "<!doctype", "<h1>")


def usable_source(source: Any) -> bool:
    """Is a successful ``SAPRead`` answer a source to keep as the base?

    Not when it is empty or blank, an HTML error page (``<html``,
    ``<!doctype``, ``<h1>``), or a single line that is a message rather
    than a source: one saying the object does not exist / was not found,
    one starting with ``Error:``, a JSON object or array, or a one-line
    ``<?xml`` envelope. Stored as the base, such a text would make the
    proposal look like a rewrite of a two-word "object". A real source
    that mentions "not found" in a comment has more than one line, and so
    does a real XML-typed source; ABAP and CDS never parse as JSON.
    """
    if not isinstance(source, str) or not source.strip():
        return False
    text = source.strip()
    if text[:9].lower().startswith(_ERROR_PAGE):
        return False
    if "\n" in text:
        return True
    return not (
        _NOT_A_SOURCE.search(text)
        or text[:6].lower() == "error:"
        or text[:5].lower() == "<?xml"
        or _json_container(text)
    )


def _json_container(text: str) -> bool:
    """Does one line parse as a JSON object or array (an error payload)?"""
    if text[:1] not in "{[":
        return False
    try:
        return isinstance(json.loads(text), (dict, list))
    except ValueError:
        return False


def too_large(source: str) -> bool:
    """A source the scratchpad could not hold is not stored as a base: the
    diff would be against text the agent never saw."""
    return len(source.encode("utf-8", errors="replace")) > deep.MAX_FILE_BYTES


# A call that never reached SAP: it says nothing about the object.
_NOT_REACHED = (arc1.Arc1UserRequired, arc1.Arc1NotConfigured)


# Answers that may be wrong and are asked again once the file changed.
_RECHECK = (ABSENT, UNKNOWN)
# How much later than its check a row without a revision row must have been
# written to count as changed since (see ``_candidates``).
_UNDATED_SLACK = timedelta(seconds=2)


def _utc(value: datetime | None) -> datetime | None:
    """Comparable across SQLite (naive) and Postgres (aware) values."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


async def _candidates(sid: str) -> list[tuple[str, int, str | None]]:
    """``(path, revision, base_status)`` to check: object rows without an
    origin that were never checked, then ``absent``/``unknown`` rows checked
    before their latest revision was written, then ``absent``/``unknown``
    rows that have no revision row at all; at most
    :data:`BASE_CHECK_MAX`, each group in path order."""
    async with SessionLocal() as db:
        rows = await db.execute(
            select(
                IdeWorkspaceFile.path,
                IdeWorkspaceFile.revision,
                IdeWorkspaceFile.base_status,
                IdeWorkspaceFile.base_checked_at,
                IdeFileRevision.created_at,
                IdeWorkspaceFile.updated_at,
            )
            .outerjoin(
                IdeFileRevision,
                and_(
                    IdeFileRevision.session_id == IdeWorkspaceFile.session_id,
                    IdeFileRevision.path == IdeWorkspaceFile.path,
                    IdeFileRevision.revision == IdeWorkspaceFile.revision,
                ),
            )
            .where(
                IdeWorkspaceFile.session_id == sid,
                IdeWorkspaceFile.origin_source.is_(None),
                IdeWorkspaceFile.base_status.is_(None)
                | IdeWorkspaceFile.base_status.in_(_RECHECK),
            )
            .order_by(IdeWorkspaceFile.path)
        )
        unchecked: list[tuple[str, int, str | None]] = []
        stale: list[tuple[str, int, str | None]] = []
        undated: list[tuple[str, int, str | None]] = []
        for path, revision, status, checked_at, written_at, touched_at in rows.all():
            if object_for(path) is None:
                continue
            if status is None:
                unchecked.append((path, revision or 0, None))
                continue
            checked, written = _utc(checked_at), _utc(written_at)
            if checked is None or (written is not None and checked < written):
                stale.append((path, revision or 0, status))
            elif written is None:
                # No revision row to compare with (a row from before
                # revisions): the row's own update time stands in. Storing
                # the check touches the row a moment after base_checked_at,
                # so only a write well after the check counts -- otherwise
                # such a row would be read from SAP again on every run.
                touched = _utc(touched_at)
                if touched is not None and touched - checked > _UNDATED_SLACK:
                    undated.append((path, revision or 0, status))
    return (unchecked + stale + undated)[:BASE_CHECK_MAX]


async def _store(
    sid: str, path: str, revision: int, status: str,
    source: str | None = None, version: str | None = None,
    previous: str | None = None,
) -> dict | None:
    """Write one result with a conditional UPDATE; the ``file`` event data,
    or None when the row changed in between (or is gone)."""
    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(IdeWorkspaceFile).where(
                    IdeWorkspaceFile.session_id == sid,
                    IdeWorkspaceFile.path == path,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        if status == SAP and source is not None:
            values = base_values(row.proposed_source, source, version)
        else:
            values = {"base_status": status, "base_checked_at": utcnow()}
        result = await db.execute(
            update(IdeWorkspaceFile)
            .where(
                IdeWorkspaceFile.id == row.id,
                IdeWorkspaceFile.origin_source.is_(None),
                (
                    IdeWorkspaceFile.base_status.is_(None)
                    if previous is None
                    else IdeWorkspaceFile.base_status == previous
                ),
                IdeWorkspaceFile.revision == revision,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            return None
        await db.commit()
        return {
            "path": path,
            "state": values.get("state", row.state),
            "revision": revision,
            "base_status": values["base_status"],
        }


async def check_bases(
    sid: str,
    target: str,
    destination: str,
    emit: Callable[[str, dict], None] | None = None,
    on_start: Callable[[int], None] | None = None,
) -> list[dict]:
    """Check the SAP base of the session's unchecked object files.

    ``on_start(n)`` is called once before the first read when there is
    anything to check (the runner shows the wait).

    Returns ``[{path, state, revision, base_status}]`` for every row it
    changed. The runner merges that into the run's changed files, so each
    path gets one ``file`` event with its final state; a caller outside a run
    passes ``emit`` to get one ``file`` event per row instead.
    """
    candidates = await _candidates(sid)
    if not candidates:
        return []
    if on_start is not None:
        on_start(len(candidates))
    # Looked up on the module at call time: the tests' seam.
    client = arc1.get_arc1_client(target, destination, policy=CHANGE)
    changed: list[dict] = []
    try:
        async with asyncio.timeout(BASE_CHECK_TIMEOUT_S) as budget:
            for path, revision, previous in candidates:
                type_, name, include = object_for(path)  # type: ignore[misc]
                source: Any = None
                version: str | None = None
                try:
                    source = await client.call(
                        "SAPRead", read_args(type_, name, include)
                    )
                except _NOT_REACHED as exc:
                    log.warning(
                        "IDE session %s: base check stopped, SAP not reached (%s)",
                        sid, exc.code,
                    )
                    break
                except arc1.Arc1Refused as exc:
                    log.warning(
                        "IDE session %s: base check of %s refused: %s",
                        sid, path, exc.code,
                    )
                    continue
                except arc1.Arc1Error as exc:
                    status = ABSENT if arc1.is_not_found(exc) else UNKNOWN
                    log.info(
                        "IDE session %s: base of %s is %s (%s, %s)",
                        sid, path, status, exc.status_code, exc.code,
                    )
                else:
                    if not usable_source(source) or too_large(source):
                        status = UNKNOWN
                    else:
                        status = SAP
                        version = await arc1.read_version(client, type_, name)
                try:
                    entry = await _store(
                        sid, path, revision, status,
                        source if status == SAP else None, version, previous,
                    )
                except Exception as exc:  # noqa: BLE001
                    # No traceback: SQL parameters would carry source.
                    log.error(
                        "IDE session %s: storing the base of %s failed: %s",
                        sid, path, type(exc).__name__,
                    )
                    continue
                if entry is None:
                    continue
                changed.append(entry)
                if emit is not None:
                    emit("file", {**entry, "syntax_status": None})
    except TimeoutError:
        if not budget.expired():
            raise
        log.warning(
            "IDE session %s: base check stopped after %.0fs; %d of %d checked",
            sid, BASE_CHECK_TIMEOUT_S, len(changed), len(candidates),
        )
    return changed


def merge_changed(first: list[dict] | None, later: list[dict]) -> list[dict]:
    """The run's changed files with ``later`` results applied per path
    (fields of ``later`` win; order of first appearance kept)."""
    out: dict[str, dict] = {}
    for item in [*(first or []), *later]:
        out[item["path"]] = {**out.get(item["path"], {}), **item}
    return list(out.values())
