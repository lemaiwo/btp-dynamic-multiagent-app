"""The ARC-1 syntax dry run of a proposal, stored per file revision.

A reviewer looking at a proposed class wants to know whether it would even
compile. ARC-1's ``SAPDiagnose`` action ``syntax`` takes the proposed
``source`` and checks it against the system without writing or activating
anything. This module runs that check:

- at the end of a change run, after the base check (``agents.ide.runner``):
  :func:`check_syntax` checks every object file whose latest revision has
  not been checked yet, at most :data:`SYNTAX_CHECK_MAX` per run and
  :data:`SYNTAX_CHECK_TIMEOUT_S` in total; rows not reached stay unchecked
  for the next run;
- on demand (``POST /ide/api/sessions/{sid}/file/syntax``): :func:`check_one`
  re-checks one revision and overwrites what was stored.

**Fail safe.** A wrong "ok" would tell the reviewer that broken code
compiles. Only an answer :func:`parse_syntax` positively recognises as "no
messages" is ``ok``; a recognised list of messages is ``errors`` when one of
them is an error (warnings alone stay ``ok``, listed as items). Everything
else -- an ARC-1 error, a missing user token, no ARC-1 configuration, a
read-only refusal, a timeout, any other exception, plain text or a JSON
shape not listed below -- is stored as ``unavailable`` (the UI says "Syntax
not checked"). The real payload is unverified (plan assumption A5): until
the live check records it, an unknown shape costs the signal, never the
truth.

Every call goes through ``arc1.get_arc1_client(target, destination,
policy="change")``: the read-only check runs first, and SAP is called as the
signed-in user (the run task and the request carry the JWT). Results are
written through ``store.set_syntax_result`` only.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.db import SessionLocal
from agents.ide import arc1, store
from agents.ide.models import IdeFileRevision, IdeSession, IdeWorkspaceFile
from agents.ide.paths import object_for
from agents.ide.readonly import CHANGE

log = logging.getLogger(__name__)

SYNTAX_CHECK_MAX = 20
# The whole run-end check: it runs between the agent's last answer and
# ``done``, so the developer waits for it.
SYNTAX_CHECK_TIMEOUT_S = 120.0
# One dry run. Past it the revision is ``unavailable`` (stored): a check
# that hangs once would likely hang again.
SYNTAX_CALL_TIMEOUT_S = 45.0

OK, ERRORS, UNAVAILABLE = "ok", "errors", "unavailable"
_UNAVAILABLE: tuple[str, list[dict]] = (UNAVAILABLE, [])

# Where a JSON object may carry its message list; every one present counts.
_LIST_KEYS = ("messages", "errors", "findings", "results")
# An object that says "all good" without a list; counted only when ``is True``.
_OK_KEYS = ("ok", "success", "valid")
# Keys that describe the checked object and say nothing about the outcome.
# Any key outside these three sets makes the answer unrecognised: a
# ``truncated``, ``error``, ``status`` or ``timedOut`` next to an empty list
# must not read as "no messages".
_BENIGN_KEYS = ("name", "objectName", "objectType", "uri", "objectUri", "version")
_KNOWN_KEYS = frozenset(_LIST_KEYS + _OK_KEYS + _BENIGN_KEYS)
_TEXT_KEYS = ("message", "text", "shortText")
_SEVERITY_KEYS = ("severity", "type")
_ERROR = {"e", "error", "a", "x", "fatal", "abort"}
_WARNING = {"w", "warning", "i", "info", "information"}


def _message_text(item: dict) -> str | None:
    for key in _TEXT_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _severity(item: dict) -> str:
    """``error`` unless the item says warning or info: an unknown or
    missing severity must not read as harmless."""
    for key in _SEVERITY_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            word = value.strip().lower()
            if word in _WARNING:
                return "warning"
            return "error"
    return "error"


def _line(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        # ASCII only: "²".isdigit() is True and int("²") raises.
        if not (text.isascii() and text.isdigit()):
            return None
        value = int(text)
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _messages(data: Any) -> list | None:
    """The message list of a recognised shape; ``[]`` for an explicit "ok"
    without one; ``None`` when the shape is not recognised.

    Positive recognition only: a bare list is the whole answer; an object may
    hold only the known keys, every list key present must be a list (all of
    them are merged), and an ok flag that is present must be exactly
    ``True``."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return None
    if any(not isinstance(k, str) or k not in _KNOWN_KEYS for k in data):
        return None
    if any(k in data and data[k] is not True for k in _OK_KEYS):
        return None
    present = [k for k in _LIST_KEYS if k in data]
    merged: list = []
    for key in present:
        value = data[key]
        if not isinstance(value, list):
            return None
        merged.extend(value)
    if present:
        return merged
    if any(data.get(k) is True for k in _OK_KEYS):
        return []
    return None


def parse_syntax(text: str) -> tuple[str, list[dict]]:
    """``(status, items)`` of an ARC-1 syntax answer; fails safe.

    Recognised: a JSON list, or an object with lists under
    ``messages``/``errors``/``findings``/``results`` (all merged), of
    objects that each carry a message text (``message``/``text``/
    ``shortText``) and optionally ``line`` and ``severity``/``type``; or an
    object with ``ok``/``success``/``valid`` set to ``true`` and no list.
    An object with any other top-level key (see :data:`_BENIGN_KEYS` for the
    few allowed), or an ok flag that is not exactly ``true``, is not
    recognised. One item that
    is not such an object makes the whole answer ``unavailable``: a partly
    understood answer cannot vouch for the rest.

    Items: ``{line, message, severity}``, the message cut to 300 characters,
    at most 50; the status is computed over all of them.
    """
    if not isinstance(text, str) or not text.strip():
        return _UNAVAILABLE
    try:
        data = json.loads(text)
    except ValueError:
        return _UNAVAILABLE
    messages = _messages(data)
    if messages is None:
        return _UNAVAILABLE
    items: list[dict] = []
    for raw in messages:
        if not isinstance(raw, dict):
            return _UNAVAILABLE
        message = _message_text(raw)
        if message is None:
            return _UNAVAILABLE
        items.append({
            "line": _line(raw.get("line")),
            "message": message[: store.MAX_SYNTAX_MESSAGE],
            "severity": _severity(raw),
        })
    status = ERRORS if any(i["severity"] == "error" for i in items) else OK
    return status, items[: store.MAX_SYNTAX_ITEMS]


# A call that never reached SAP: every further call would end the same way.
_NOT_REACHED = (arc1.Arc1UserRequired, arc1.Arc1NotConfigured)


def _args(path: str, source: str) -> dict[str, Any]:
    type_, name, _include = object_for(path)  # type: ignore[misc]
    return {"action": "syntax", "type": type_, "name": name, "source": source}


async def _dry_run(client: Any, sid: str, path: str, source: str) -> tuple[str, list[dict]]:
    """One dry run; any failure is ``unavailable``. A cancel propagates.

    An empty or blank proposal is ``unavailable`` without a call: there is
    nothing to compile, and SAP could answer "no messages" for it. The
    answer is parsed inside the same guard, so a parser error costs this
    file's result, never the whole check or a 500 on the route."""
    if not isinstance(source, str) or not source.strip():
        log.info("IDE session %s: syntax check of %s skipped, empty proposal", sid, path)
        return _UNAVAILABLE
    try:
        async with asyncio.timeout(SYNTAX_CALL_TIMEOUT_S):
            text = await client.call("SAPDiagnose", _args(path, source))
        status, items = parse_syntax(text)
    except _NOT_REACHED:
        raise
    except arc1.Arc1Error as exc:
        log.warning("IDE session %s: syntax check of %s unavailable: %s (%s)",
                    sid, path, exc.code, exc.status_code)
        return _UNAVAILABLE
    except TimeoutError:
        # Our own per-call limit, or a timeout inside the client: either
        # way no answer.
        log.warning("IDE session %s: syntax check of %s timed out", sid, path)
        return _UNAVAILABLE
    except Exception as exc:  # noqa: BLE001 -- fail safe, never "ok"
        log.warning("IDE session %s: syntax check of %s failed: %s",
                    sid, path, exc.__class__.__name__)
        return _UNAVAILABLE
    if status == UNAVAILABLE:
        # The shape only: the text may hold source.
        log.warning("IDE session %s: syntax answer for %s not recognised (%s, %d chars)",
                    sid, path, text.__class__.__name__,
                    len(text) if isinstance(text, str) else 0)
    return status, items


async def _candidates(
    sid: str, paths: list[str] | None
) -> list[tuple[str, int, str, str | None, str]]:
    """``(path, revision, state, base_status, source)`` of each object file
    that holds a proposal whose latest revision is unchecked, in path order,
    at most :data:`SYNTAX_CHECK_MAX`."""
    async with SessionLocal() as db:
        query = (
            select(
                IdeWorkspaceFile.path,
                IdeWorkspaceFile.revision,
                IdeWorkspaceFile.state,
                IdeWorkspaceFile.base_status,
                IdeFileRevision.proposed_source,
            )
            .join(
                IdeFileRevision,
                and_(
                    IdeFileRevision.session_id == IdeWorkspaceFile.session_id,
                    IdeFileRevision.path == IdeWorkspaceFile.path,
                    IdeFileRevision.revision == IdeWorkspaceFile.revision,
                ),
            )
            .where(
                IdeWorkspaceFile.session_id == sid,
                IdeWorkspaceFile.revision > 0,
                # No proposal (e.g. the base check found it equal to SAP):
                # nothing to review, nothing to check.
                IdeWorkspaceFile.proposed_source.is_not(None),
                IdeFileRevision.syntax_status.is_(None),
            )
            .order_by(IdeWorkspaceFile.path)
        )
        rows = (await db.execute(query)).all()
    wanted = set(paths) if paths is not None else None
    out = [
        (p, r, s, b, src or "")
        for p, r, s, b, src in rows
        if object_for(p) is not None and (wanted is None or p in wanted)
    ]
    return out[:SYNTAX_CHECK_MAX]


async def _store(
    sid: str, path: str, revision: int, status: str, items: list[dict]
) -> bool:
    try:
        async with SessionLocal() as db:
            await store.set_syntax_result(db, sid, path, revision, status, items)
        return True
    except LookupError:
        return False  # the revision or the session went away meanwhile
    except Exception as exc:  # noqa: BLE001
        # No traceback: SQL parameters would carry message text.
        log.error("IDE session %s: storing the syntax result of %s failed: %s",
                  sid, path, type(exc).__name__)
        return False


async def check_syntax(
    sid: str,
    target: str,
    destination: str,
    emit: Callable[[str, dict], None] | None = None,
    paths: list[str] | None = None,
    on_start: Callable[[int], None] | None = None,
) -> list[dict]:
    """Dry-run the unchecked latest revision of the session's object files.

    ``paths`` limits the check to those paths. Returns
    ``[{path, state, revision, base_status, syntax_status}]`` for every
    revision it stored; the runner merges that into the run's changed files
    (one ``file`` event per path), a caller outside a run passes ``emit``.
    Missing token or ARC-1 configuration: no further call is sent, and every
    remaining candidate is stored ``unavailable`` as well -- that is what a
    call would have answered.
    """
    candidates = await _candidates(sid, paths)
    if not candidates:
        return []
    if on_start is not None:
        on_start(len(candidates))  # the runner shows the wait
    # Looked up on the module at call time: the tests' seam.
    client = arc1.get_arc1_client(target, destination, policy=CHANGE)
    changed: list[dict] = []
    not_reached = False
    try:
        async with asyncio.timeout(SYNTAX_CHECK_TIMEOUT_S) as budget:
            for path, revision, state, base_status, source in candidates:
                if not_reached:
                    status, items = _UNAVAILABLE
                else:
                    try:
                        status, items = await _dry_run(client, sid, path, source)
                    except _NOT_REACHED as exc:
                        log.warning(
                            "IDE session %s: syntax check not run, SAP not reached (%s)",
                            sid, exc.code,
                        )
                        not_reached = True
                        status, items = _UNAVAILABLE
                if not await _store(sid, path, revision, status, items):
                    continue
                entry = {"path": path, "state": state, "revision": revision,
                         "base_status": base_status, "syntax_status": status}
                changed.append(entry)
                if emit is not None:
                    emit("file", dict(entry))
    except TimeoutError:
        if not budget.expired():
            raise
        log.warning(
            "IDE session %s: syntax check stopped after %.0fs; %d of %d checked",
            sid, SYNTAX_CHECK_TIMEOUT_S, len(changed), len(candidates),
        )
    return changed


async def check_one(
    db: AsyncSession, session: IdeSession, path: str, revision: IdeFileRevision
) -> IdeFileRevision:
    """Dry-run one stored revision now and overwrite its result.

    The caller has checked the owner, the run lock and that ``path`` is an
    object file. ``Arc1UserRequired`` and every other failure are stored as
    ``unavailable``; ``LookupError`` if the revision is gone.
    """
    conv = await store.get_conventions(db, session.target)
    destination = (getattr(conv, "destination", "") or "").strip()
    client = arc1.get_arc1_client(session.target, destination, policy=CHANGE)
    try:
        status, items = await _dry_run(
            client, session.id, path, revision.proposed_source or ""
        )
    except _NOT_REACHED as exc:
        log.warning("IDE session %s: syntax check of %s not run (%s)",
                    session.id, path, exc.code)
        status, items = _UNAVAILABLE
    return await store.set_syntax_result(
        db, session.id, path, revision.revision, status, items
    )
