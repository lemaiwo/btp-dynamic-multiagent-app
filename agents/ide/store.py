"""Owner-scoped persistence helpers for IDE sessions.

Every session-level read takes ``owner`` and filters on it in SQL, so a
caller can never reach another user's session by guessing an id. Functions
that take only a session id (``list_messages``, ``add_artifact``, ...) expect
the caller to have loaded that session through ``get_owned_session`` first.

``list_all_sessions_meta`` is the admin overview: metadata only, never
messages, artifacts or sources.

Diagnose sessions (phase 1c) add findings and approvals. Both are session
children and every read or decision filters on ``session_id`` as well as the
row id, so a guessed finding/approval id of another session returns ``None``;
the caller still has to load the session through ``get_owned_session``
first. Findings hold metadata, and the detail text only for a
``non_production`` target (the only kind a diagnose session reads from).
The audit log is append-only and not a session child:
it survives a session delete or purge, and this module offers no helper to
change or delete an entry (retention has its own purge).

File revisions, review comments, pins and the worklist marker follow the
same rule: every helper is scoped by session id (comments and revisions by
their id *and* the session id) and requires a session the caller loaded
through ``get_owned_session``. Comment state changes are conditional
UPDATEs, so a race cannot leave a comment in a state the machine forbids.
"""

from __future__ import annotations

import json
import logging
import os
import unicodedata
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agents.ide.models import (
    IDE_CHILD_MODELS,
    IdeApproval,
    IdeArtifact,
    IdeAuditLog,
    IdeComment,
    IdeConventions,
    IdeFileRevision,
    IdeFinding,
    IdeMessage,
    IdeSession,
    IdeWorkspaceFile,
    utcnow,
)

log = logging.getLogger(__name__)
audit_logger = logging.getLogger("agents.ide.audit")


def env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """An integer setting from the environment that cannot fail the import.

    These settings are read when the module is imported, so a bare ``int()``
    on a typo (``"14 days"``) would keep the whole app from starting. An
    unset or blank value is the default; one that is not an integer is the
    default too, with a warning that names the variable but not its value.
    ``minimum`` raises a value that is too low.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s is not an integer; using the default %d", name, default)
        return default
    return value if minimum is None else max(minimum, value)


# Days an idle session is kept; 0 disables the purge.
IDE_SESSION_RETENTION_DAYS = env_int("IDE_SESSION_RETENTION_DAYS", 90)
# Diagnose sessions hold raw incident data of a non-production target: a hard
# cap from creation. 0 disables the purge -- that text is then kept until the
# session is deleted by hand; app.py says so at startup.
IDE_DIAGNOSE_RETENTION_DAYS = env_int("IDE_DIAGNOSE_RETENTION_DAYS", 14)
# The trace-arming audit trail outlives its sessions, on its own clock.
IDE_AUDIT_RETENTION_DAYS = env_int("IDE_AUDIT_RETENTION_DAYS", 365)
# A pending approval older than this can no longer be approved.
# 0 disables only the expiry sweep, never the age check on a decision.
APPROVAL_TTL_MIN = env_int("IDE_APPROVAL_TTL_MIN", 15)


def approval_ttl_min() -> int:
    """The approval lifetime for ``decide_approval``: at least one minute, so
    ``IDE_APPROVAL_TTL_MIN=0`` cannot make every approval unusable."""
    return max(1, APPROVAL_TTL_MIN)

_CONVENTION_FIELDS = (
    "label",
    "destination",
    "namespace",
    "package",
    "atc_variant",
    "clean_core_level",
    "free_text",
    "non_production",
)

SESSION_TYPES = ("change", "diagnose")
FINDING_KINDS = ("dump", "trace", "gateway_error", "auth_check", "odata_call")
APPROVAL_ACTIONS = ("trace_start", "trace_cancel")
# States a pending approval can be decided into; ``expired`` is set only by
# ``expire_pending_approvals``.
APPROVAL_DECISIONS = ("approved", "denied", "failed")
AUDIT_ACTIONS = (
    "trace_arm", "trace_cancel", "trace_deny", "trace_failed",
    # An admin set or changed a target's ``non_production`` flag: the switch
    # that lets diagnose sessions read raw runtime data from that target.
    "conventions_flag",
    # The ``destination`` of a target flagged ``non_production`` changed (set,
    # changed or cleared): it names the server that raw runtime data is read
    # from.
    "conventions_destination",
)
# ``IdeAuditLog.session_id`` is NOT NULL; a conventions change has no session.
NO_SESSION = "-"


# --- sessions -------------------------------------------------------------


async def touch_session(db: AsyncSession, sid: str) -> None:
    """Bump the session's activity clock (``updated_at``) without committing.

    Purge and list ordering read ``updated_at``; every write to a child row
    calls this in the same transaction so an active session never ages out.
    """
    await db.execute(
        update(IdeSession).where(IdeSession.id == sid).values(updated_at=utcnow())
    )


async def create_session(
    db: AsyncSession,
    *,
    owner: str,
    title: str,
    target: str,
    session_type: str = "change",
) -> IdeSession:
    if not owner:
        raise ValueError("owner is required")
    if not isinstance(session_type, str) or session_type not in SESSION_TYPES:
        raise ValueError(f"Unknown session type: {session_type!r}")
    # Imported here: ``stages`` imports this module.
    from agents.ide.stages import initial_stage

    row = IdeSession(
        owner=owner,
        title=title,
        target=target,
        session_type=session_type,
        stage=initial_stage(session_type).value,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def get_owned_session(
    db: AsyncSession, sid: str, owner: str
) -> IdeSession | None:
    if not sid or not owner:
        return None
    return (
        await db.execute(
            select(IdeSession).where(IdeSession.id == sid, IdeSession.owner == owner)
        )
    ).scalar_one_or_none()


async def list_owned_sessions(db: AsyncSession, owner: str) -> list[IdeSession]:
    """The caller's sessions, most recently touched first."""
    if not owner:
        return []
    rows = await db.execute(
        select(IdeSession)
        .where(IdeSession.owner == owner)
        .order_by(IdeSession.updated_at.desc(), IdeSession.created_at.desc())
    )
    return list(rows.scalars().all())


async def list_all_sessions_meta(db: AsyncSession) -> list[dict[str, Any]]:
    rows = await db.execute(
        select(IdeSession).order_by(
            IdeSession.updated_at.desc(), IdeSession.created_at.desc()
        )
    )
    return [s.meta() for s in rows.scalars().all()]


async def _delete_children(db: AsyncSession, sids: list[str]) -> None:
    # SQLite ignores ON DELETE CASCADE without PRAGMA foreign_keys=ON.
    for model in IDE_CHILD_MODELS:
        await db.execute(delete(model).where(model.session_id.in_(sids)))


async def delete_owned_session(db: AsyncSession, sid: str, owner: str) -> bool:
    row = await get_owned_session(db, sid, owner)
    if row is None:
        return False
    await _delete_children(db, [row.id])
    await db.execute(
        delete(IdeSession).where(IdeSession.id == row.id, IdeSession.owner == owner)
    )
    await db.commit()
    return True


async def purge_sessions_older_than(
    db: AsyncSession,
    days: int,
    *,
    session_type: str = "change",
    by: str = "updated_at",
) -> int:
    """Delete idle sessions of one type (and their children) older than ``days``.

    ``by`` picks the clock: ``updated_at`` for change sessions (activity keeps
    them alive), ``created_at`` for diagnose sessions (a hard data limit that
    activity cannot extend, plan D3). The type, cutoff and ``running``
    exclusion sit in the DELETE itself, so a session that became active
    between selecting and deleting is kept; only the children of rows the
    DELETE actually removed are deleted. The audit log is never touched.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    if session_type not in SESSION_TYPES:
        raise ValueError(f"Unknown session type: {session_type!r}")
    if by not in ("updated_at", "created_at"):
        raise ValueError(f"Unknown retention clock: {by!r}")
    cutoff = utcnow() - timedelta(days=days)
    result = await db.execute(
        delete(IdeSession)
        .where(
            getattr(IdeSession, by) < cutoff,
            IdeSession.session_type == session_type,
            IdeSession.status != "running",
        )
        .returning(IdeSession.id)
        # The WHERE is the check; SQLite's naive datetimes cannot be compared
        # with an aware cutoff by the in-Python evaluator.
        .execution_options(synchronize_session=False)
    )
    sids = list(result.scalars().all())
    if sids:
        await _delete_children(db, sids)
    await db.commit()
    return len(sids)


async def purge_audit_older_than(db: AsyncSession, days: int) -> int:
    """Delete audit entries older than ``days``; the only audit deleter, and
    it works on age alone, never on a session."""
    if days < 1:
        raise ValueError("days must be >= 1")
    result = await db.execute(
        delete(IdeAuditLog)
        .where(IdeAuditLog.ts < utcnow() - timedelta(days=days))
        .returning(IdeAuditLog.id)
        .execution_options(synchronize_session=False)
    )
    n = len(result.scalars().all())
    await db.commit()
    return n


async def reset_running_ide_sessions(db: AsyncSession, *, min_age_s: float = 0.0) -> int:
    """Set sessions still ``running`` back to ``idle`` (startup only).

    A freshly started process owns no run, so a ``running`` row nobody has
    touched for ``min_age_s`` seconds is a ghost from a crash or redeploy;
    ``runner.cancel`` cannot reach it and the purge skips it. A fresher row
    may be a live run of another instance (rolling or blue-green deploy,
    several instances), whose heartbeat keeps it fresh: it is left alone.
    Returns the number of rows reset.
    """
    where = [IdeSession.status == "running"]
    if min_age_s > 0:
        where.append(IdeSession.updated_at < utcnow() - timedelta(seconds=min_age_s))
    result = await db.execute(
        update(IdeSession)
        .where(*where)
        .values(status="idle", run_id=None)
        .returning(IdeSession.id)
    )
    ids = list(result.scalars().all())
    # A comment is ``sent`` only while its run is in flight; the ghost run
    # will never resolve it, so it goes back to ``open`` with the lock.
    for chunk in _chunks(ids):
        await db.execute(
            update(IdeComment)
            .where(IdeComment.session_id.in_(chunk), IdeComment.state == "sent")
            .values(state="open", updated_at=utcnow())
            .execution_options(synchronize_session=False)
        )
    await db.commit()
    return len(ids)


# --- messages -------------------------------------------------------------


async def add_message(
    db: AsyncSession,
    sid: str,
    *,
    stage: str,
    role: str,
    content: str,
    activity_json: str | None = None,
) -> IdeMessage:
    row = IdeMessage(
        session_id=sid,
        stage=stage,
        role=role,
        content=content,
        activity_json=activity_json,
    )
    db.add(row)
    await touch_session(db, sid)
    await db.commit()
    await db.refresh(row)
    return row


async def list_messages(db: AsyncSession, sid: str) -> list[IdeMessage]:
    rows = await db.execute(
        select(IdeMessage)
        .where(IdeMessage.session_id == sid)
        .order_by(IdeMessage.created_at.asc())
    )
    return list(rows.scalars().all())


# --- artifacts ------------------------------------------------------------


async def add_artifact(
    db: AsyncSession,
    sid: str,
    *,
    stage: str,
    kind: str,
    content: str,
    based_on: dict[str, int] | None = None,
) -> IdeArtifact:
    """Store a new version of ``kind`` for the session (max + 1).

    The run lock and ``submit_document``'s per-run lock make two writers of
    the same (session, kind) rare, but not impossible (the handover route,
    a reaped run): the unique index ``uq_ide_artifacts_version`` refuses a
    duplicate number, and the insert is retried with a fresh max.
    ``based_on`` is the approved versions the document was written from
    (``{"design": 2}`` on a plan), stored as JSON.
    """
    for attempt in range(1, ADD_ARTIFACT_ATTEMPTS + 1):
        row = IdeArtifact(
            session_id=sid,
            stage=stage,
            kind=kind,
            content=content,
            version=await _next_version(db, sid, kind),
            based_on_json=json.dumps(based_on, sort_keys=True) if based_on else None,
        )
        try:
            db.add(row)
            await db.flush()  # the unique index answers here, or at commit
            await touch_session(db, sid)
            await db.commit()
        except IntegrityError:
            await db.rollback()
            if attempt == ADD_ARTIFACT_ATTEMPTS:
                raise
            log.info("IDE session %s: %s version taken, retrying", sid, kind)
            continue
        await db.refresh(row)
        return row
    raise AssertionError("unreachable")  # pragma: no cover


ADD_ARTIFACT_ATTEMPTS = 5


async def _next_version(db: AsyncSession, sid: str, kind: str) -> int:
    current = (
        await db.execute(
            select(func.max(IdeArtifact.version)).where(
                IdeArtifact.session_id == sid, IdeArtifact.kind == kind
            )
        )
    ).scalar()
    return (current or 0) + 1


async def lock_session_row(db: AsyncSession, sid: str) -> None:
    """``SELECT ... FOR UPDATE`` on the session row, in the caller's
    transaction. Serialises approve with comment writes on Postgres (READ
    COMMITTED: a NOT EXISTS in approve's UPDATE would not see a comment
    committed after its snapshot). SQLAlchemy drops the clause on SQLite,
    whose single writer already serialises them.

    Lock order: every writer that touches both takes the session row FIRST
    and comments second (``runner._start``/``_reclaim``/``_release``, the
    comment writers here). The reverse order in one of them would let two
    requests each hold the row the other waits for -- a deadlock, one
    request failing -- on Postgres."""
    await db.execute(
        select(IdeSession.id).where(IdeSession.id == sid).with_for_update()
    )


async def list_artifacts(
    db: AsyncSession, sid: str, kind: str | None = None
) -> list[IdeArtifact]:
    """Artifacts of a session, latest version first."""
    stmt = select(IdeArtifact).where(IdeArtifact.session_id == sid)
    if kind:
        stmt = stmt.where(IdeArtifact.kind == kind)
    stmt = stmt.order_by(IdeArtifact.created_at.desc(), IdeArtifact.version.desc())
    return list((await db.execute(stmt)).scalars().all())


# --- conventions ----------------------------------------------------------


async def get_conventions(db: AsyncSession, target: str) -> IdeConventions | None:
    return await db.get(IdeConventions, target)


async def target_is_non_production(db: AsyncSession, target: str) -> bool:
    """The target's ``non_production`` flag as it is in the database now, not
    as an earlier read left it in this session's identity map. No row, or
    anything but a real ``True``, is "no"."""
    row = (
        await db.execute(
            select(IdeConventions)
            .where(IdeConventions.target == target)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    return row is not None and row.non_production is True


async def list_conventions(db: AsyncSession) -> list[IdeConventions]:
    rows = await db.execute(select(IdeConventions).order_by(IdeConventions.target))
    return list(rows.scalars().all())


async def upsert_conventions(
    db: AsyncSession, target: str, *, actor: str | None = None, **fields: Any
) -> IdeConventions:
    """Create or update a target's conventions; only given fields change.

    A plain helper (seeding, tests) with no write path of its own: it goes
    through :func:`create_conventions` / :func:`update_conventions`, so a
    ``non_production`` change is audited exactly as the admin routes audit
    it and needs ``actor`` (``ValueError`` otherwise). No helper can flip
    the flag without an audit row.
    """
    _check_convention_fields(fields)
    if await db.get(IdeConventions, target) is None:
        try:
            return await create_conventions(db, target, actor=actor, **fields)
        except ConventionsError:
            pass  # created concurrently: update it instead
    row = await update_conventions(db, target, fields, actor=actor)
    if row is None:  # deleted concurrently
        return await create_conventions(db, target, actor=actor, **fields)
    return row


# Text fields an admin may clear. ``clean_core_level`` and ``non_production``
# are not among them: both always hold a value, and "cleared" would have to
# invent one (the flag in particular must only ever change on purpose).
CLEARABLE_CONVENTION_FIELDS = (
    "label",
    "destination",
    "namespace",
    "package",
    "atc_variant",
    "free_text",
)


class ConventionsError(Exception):
    """A refused conventions write; ``code`` is the stable API error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _check_convention_fields(fields: dict[str, Any]) -> None:
    unknown = set(fields) - set(_CONVENTION_FIELDS)
    if unknown:
        raise ValueError(f"Unknown conventions fields: {sorted(unknown)}")


def _flag_audit(
    db: AsyncSession, actor: str | None, target: str, old: bool | None, new: bool
) -> dict[str, Any]:
    """Stage the audit row of a ``non_production`` change in the caller's
    transaction, so the change and its record commit (or roll back)
    together. Only the flag's old and new value are kept: never the free
    text, destination or anything else of the request body."""
    if not actor:
        raise ValueError("an actor is required to change non_production")
    params = {"non_production": {"old": old, "new": new}}
    db.add(_audit_row(principal=actor, session_id=NO_SESSION, target=target,
                      action="conventions_flag", params=params, outcome="ok"))
    return params


def _destination_audit(
    db: AsyncSession, actor: str | None, target: str, old: str | None,
    new: str | None,
) -> dict[str, Any]:
    """Stage the audit row of a ``destination`` change on a target flagged
    ``non_production`` (before or after the write), in the caller's
    transaction like :func:`_flag_audit`: the destination decides which
    server a diagnose session reads raw runtime data from. Old and new
    destination name only (a name, not a credential); no destination is
    ``None`` on both sides, never ``""``."""
    old, new = old or None, new or None
    if not actor:
        raise ValueError("an actor is required to change the destination "
                         "of a non_production target")
    params = {"destination": {"old": old, "new": new}}
    db.add(_audit_row(principal=actor, session_id=NO_SESSION, target=target,
                      action="conventions_destination", params=params,
                      outcome="ok"))
    return params


def _log_flag(
    actor: str, target: str, params: dict[str, Any],
    action: str = "conventions_flag",
) -> None:
    """The log line of a committed conventions audit (after the commit: a
    rolled back change must not appear in the audit log stream)."""
    audit_logger.info(
        "principal=%s session=%s target=%s action=%s outcome=%s params=%s",
        actor, NO_SESSION, target, action, "ok",
        json.dumps(params, sort_keys=True),
    )


def _log_audits(actor: str | None, target: str, audits: list[tuple[str, dict]]) -> None:
    for action, params in audits:
        _log_flag(actor or "", target, params, action)


async def create_conventions(
    db: AsyncSession, target: str, *, actor: str | None = None, **fields: Any
) -> IdeConventions:
    """Create a target's conventions (``POST /conventions``, decision D4).

    Never updates: an existing target -- also one inserted by a concurrent
    request between the read and the commit (primary key) -- is
    ``ConventionsError("target_exists")``, so a create cannot silently
    overwrite what another admin maintains. ``None`` values take the
    column defaults. Creating a target already flagged ``non_production``
    is audited (old value ``None``; its destination too, when one is given)
    and then needs ``actor``.
    """
    _check_convention_fields(fields)
    flagged = fields.get("non_production") is True
    if flagged and not actor:
        # Refused before anything is staged in the caller's session.
        raise ValueError("an actor is required to change non_production")
    if await db.get(IdeConventions, target) is not None:
        raise ConventionsError("target_exists")
    row = IdeConventions(target=target)
    for key, value in fields.items():
        if value is not None:
            setattr(row, key, value)
    db.add(row)
    audits: list[tuple[str, dict]] = []
    if flagged:
        audits.append(("conventions_flag", _flag_audit(db, actor, target, None, True)))
        if fields.get("destination"):
            audits.append(("conventions_destination", _destination_audit(
                db, actor, target, None, fields["destination"])))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ConventionsError("target_exists") from None
    _log_audits(actor, target, audits)
    await db.refresh(row)
    return row


async def update_conventions(
    db: AsyncSession,
    target: str,
    fields: dict[str, Any],
    clear: list[str] | tuple[str, ...] | set[str] = (),
    *,
    actor: str | None = None,
) -> IdeConventions | None:
    """Update an existing target; ``None`` when it does not exist (the route
    answers 404 instead of creating it, decision D4).

    ``fields`` with a ``None`` value are left as stored. ``clear`` names text
    fields to empty: the columns are ``NOT NULL`` with ``""`` as their empty
    value, so a cleared field is stored as ``""`` (what a never-set field
    holds). A field both set and cleared is the caller's contradiction and
    a ``ValueError``; so is clearing anything outside
    ``CLEARABLE_CONVENTION_FIELDS``.

    A ``non_production`` value different from the stored one is audited in
    the same transaction (old value read under ``FOR UPDATE`` on Postgres,
    so two concurrent flips record what each really replaced) and needs
    ``actor``; an unchanged value writes nothing. So is a ``destination``
    set, changed or cleared while the target is flagged before or after
    this write (``conventions_destination``), and -- even unchanged -- the
    destination in force when the flag is set or removed.
    """
    _check_convention_fields(fields)
    clear = set(clear)
    bad = clear - set(CLEARABLE_CONVENTION_FIELDS)
    if bad:
        raise ValueError(f"Conventions fields that cannot be cleared: {sorted(bad)}")
    both = clear & {k for k, v in fields.items() if v is not None}
    if both:
        raise ValueError(f"Conventions fields both set and cleared: {sorted(both)}")
    row = (
        await db.execute(
            select(IdeConventions)
            .where(IdeConventions.target == target)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    new_flag = fields.get("non_production")
    old_flag = row.non_production is True
    audits: list[tuple[str, dict]] = []
    if new_flag is not None and new_flag is not old_flag:
        audits.append(("conventions_flag",
                       _flag_audit(db, actor, target, old_flag, new_flag)))
    old_dest = row.destination or None
    new_dest = (
        None if "destination" in clear
        else (fields.get("destination") or None)
        if fields.get("destination") is not None
        else old_dest
    )
    flag_changed = bool(audits)
    # Audited on a flagged target (before or after) when the destination
    # changes, and whenever the flag changes on a target that has one: the
    # log then names the server in force when the flag was set or removed.
    if (old_flag or new_flag is True) and (
        new_dest != old_dest or (flag_changed and (old_dest or new_dest))
    ):
        audits.append(("conventions_destination",
                       _destination_audit(db, actor, target, old_dest, new_dest)))
    for key, value in fields.items():
        if value is not None:
            setattr(row, key, value)
    for key in clear:
        setattr(row, key, "")
    await db.commit()
    _log_audits(actor, target, audits)
    await db.refresh(row)
    return row


# --- findings (diagnose) --------------------------------------------------


# Characters of detail text kept per finding.
MAX_FINDING_DETAIL = 200_000


def _opt_str(value: Any, limit: int) -> str | None:
    """A stored optional text field: strings are cut, anything else dropped."""
    if isinstance(value, str) and value:
        return value[:limit]
    return None


def _finding_fields(item: dict[str, Any]) -> dict[str, Any]:
    """The §1.6 metadata of one finding, plus ``detail`` when it is a
    non-empty string; every other key is ignored.

    ``kind`` and ``ref_id`` form the identity, so they are validated rather
    than coerced: a bad one raises ``ValueError``.
    """
    kind = item.get("kind")
    if not isinstance(kind, str) or kind not in FINDING_KINDS:
        raise ValueError(f"Unknown finding kind: {kind!r}")
    ref_id = item.get("ref_id")
    if not isinstance(ref_id, str) or not ref_id:
        raise ValueError("finding ref_id must be a non-empty string")
    line = item.get("line")
    if isinstance(line, bool) or not isinstance(line, int):
        line = None
    title = item.get("title")
    return {
        "kind": kind,
        "ref_id": ref_id[:255],
        "title": title[:200] if isinstance(title, str) else "",
        "program": _opt_str(item.get("program"), 40),
        "include": _opt_str(item.get("include"), 40),
        "line": line,
        "occurred_at": _opt_str(item.get("occurred_at"), 32),
        "detail": _opt_str(item.get("detail"), MAX_FINDING_DETAIL),
    }


# Metadata a later, poorer read must not wipe: a list read after a detail
# read does not know the include or the line, it does not know them to be
# absent. ``None`` (and the empty title of an item without one) means
# "unknown" and keeps what is stored.
_KEEP_WHEN_UNKNOWN = ("title", "program", "include", "line", "occurred_at")


def _unknown(value: Any) -> bool:
    return value is None or value == ""


async def upsert_findings(
    db: AsyncSession,
    sid: str,
    items: list[dict[str, Any]],
) -> list[IdeFinding]:
    """Insert or update findings on ``(session_id, kind, ref_id)``.

    ``detail`` (raw text of a detail read) is written when an item carries
    it and otherwise left as stored: a list read after a detail read does
    not wipe the text.

    Metadata (title, program, include, line, occurred_at) is replaced only
    by a value: ``None`` in an item keeps the stored one, across runs and
    within one call.

    Returns the rows in input order (a key repeated in ``items`` appears
    once, the last known value of each field wins). All items are validated before anything is
    written. One session runs one stage at a time (the ``status`` lock), so
    two concurrent writers of the same session do not occur; the unique
    constraint still catches it if they did.
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        fields = _finding_fields(item)
        key = (fields["kind"], fields["ref_id"])
        earlier = merged.get(key)
        if earlier is not None:
            for name in _KEEP_WHEN_UNKNOWN:
                if _unknown(fields[name]):
                    fields[name] = earlier[name]
        if fields["detail"] is None and earlier is not None:
            fields["detail"] = earlier["detail"]
        merged[key] = fields
    if not merged:
        return []
    existing: dict[tuple[str, str], IdeFinding] = {}
    keys = list(merged)
    for i in range(0, len(keys), 500):
        chunk = keys[i : i + 500]
        rows = await db.execute(
            select(IdeFinding).where(
                IdeFinding.session_id == sid,
                IdeFinding.ref_id.in_({ref for _, ref in chunk}),
            )
        )
        for row in rows.scalars().all():
            existing[(row.kind, row.ref_id)] = row
    out: list[IdeFinding] = []
    now = utcnow()
    for key, fields in merged.items():
        row = existing.get(key)
        if row is None:
            row = IdeFinding(session_id=sid, **fields)
            db.add(row)
        else:
            for name, value in fields.items():
                if name == "detail" and value is None:
                    continue  # keep the stored text
                if name in _KEEP_WHEN_UNKNOWN and _unknown(value):
                    continue  # unknown to this read, not absent
                setattr(row, name, value)
            row.updated_at = now
        out.append(row)
    await touch_session(db, sid)
    await db.commit()
    for row in out:
        await db.refresh(row)
    return out


async def list_findings(db: AsyncSession, sid: str) -> list[IdeFinding]:
    """A session's findings, newest first."""
    rows = await db.execute(
        select(IdeFinding)
        .where(IdeFinding.session_id == sid)
        .order_by(IdeFinding.created_at.desc(), IdeFinding.id.desc())
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


async def get_finding(db: AsyncSession, sid: str, fid: str) -> IdeFinding | None:
    if not sid or not fid:
        return None
    return (
        await db.execute(
            select(IdeFinding).where(
                IdeFinding.id == fid, IdeFinding.session_id == sid
            ).execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


# --- approvals (diagnose) -------------------------------------------------


async def add_approval(
    db: AsyncSession,
    sid: str,
    *,
    run_id: str | None,
    tool_call_id: str | None,
    action: str,
    params: dict[str, Any],
) -> IdeApproval:
    if not isinstance(action, str) or action not in APPROVAL_ACTIONS:
        raise ValueError(f"Unknown approval action: {action!r}")
    if not isinstance(params, dict):
        raise ValueError("approval params must be a dict")
    row = IdeApproval(
        session_id=sid,
        run_id=run_id[:36] if run_id else None,
        tool_call_id=tool_call_id[:100] if tool_call_id else None,
        action=action,
        params_json=json.dumps(params, sort_keys=True),
        status="pending",
    )
    db.add(row)
    await touch_session(db, sid)
    await db.commit()
    await db.refresh(row)
    return row


async def get_approval(db: AsyncSession, sid: str, aid: str) -> IdeApproval | None:
    if not sid or not aid:
        return None
    return (
        await db.execute(
            select(IdeApproval)
            .where(IdeApproval.id == aid, IdeApproval.session_id == sid)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def list_approvals(db: AsyncSession, sid: str) -> list[IdeApproval]:
    """A session's approvals, newest first."""
    rows = await db.execute(
        select(IdeApproval)
        .where(IdeApproval.session_id == sid)
        .order_by(IdeApproval.created_at.desc(), IdeApproval.id.desc())
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


async def decide_approval(
    db: AsyncSession,
    sid: str,
    aid: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
    max_age_min: int,
) -> IdeApproval | None:
    """Move a pending approval to ``status`` exactly once.

    The state check lives in the UPDATE itself (``status='pending'``, and the
    age, ``max_age_min`` being required so no caller can skip the expiry
    check), so two concurrent approve clicks, or
    an approve racing the expiry sweep, cannot both win: the loser sees
    ``rowcount != 1`` and gets ``None``. The caller tells "not pending" from
    "expired" by reading the row with ``get_approval`` afterwards.
    """
    if status not in APPROVAL_DECISIONS:
        raise ValueError(f"Unknown approval decision: {status!r}")
    if not sid or not aid:
        return None
    where = [
        IdeApproval.id == aid,
        IdeApproval.session_id == sid,
        IdeApproval.status == "pending",
    ]
    if max_age_min < 1:
        raise ValueError("max_age_min must be >= 1")
    where.append(IdeApproval.created_at >= utcnow() - timedelta(minutes=max_age_min))
    res = await db.execute(
        update(IdeApproval)
        .where(*where)
        .values(
            status=status,
            decided_at=utcnow(),
            result_json=None if result is None else json.dumps(result, sort_keys=True),
            error_code=error_code[:64] if error_code else None,
        )
        # The WHERE is the state check; there is nothing to sync in memory
        # (reads use populate_existing), and SQLite returns naive datetimes
        # the in-Python evaluator cannot compare with an aware cutoff.
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        # Nothing changed; commit (not rollback) so loaded rows stay usable.
        await db.commit()
        return None
    await touch_session(db, sid)
    await db.commit()
    return await get_approval(db, sid, aid)


async def expire_pending_approvals(
    db: AsyncSession, *, older_than_min: int, session_id: str | None = None
) -> int:
    """Mark approvals still pending after ``older_than_min`` minutes expired.

    Conditional on ``status='pending'`` in the UPDATE, so an approval decided
    meanwhile keeps its decision. All sessions (the retention pass) unless
    ``session_id`` narrows it to one (the owner's list). Returns the number
    of rows expired.
    """
    if older_than_min < 1:
        raise ValueError("older_than_min must be >= 1")
    cutoff = utcnow() - timedelta(minutes=older_than_min)
    where = [IdeApproval.status == "pending", IdeApproval.created_at < cutoff]
    if session_id is not None:
        where.append(IdeApproval.session_id == session_id)
    res = await db.execute(
        update(IdeApproval)
        .where(*where)
        .values(status="expired", decided_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return res.rowcount or 0


async def expire_approval(db: AsyncSession, sid: str, aid: str) -> bool:
    """pending -> expired for one overdue approval; True if this call did it.

    The age check (``approval_ttl_min``) is in the UPDATE, as in
    ``decide_approval``, so an approval decided meanwhile keeps its decision.
    """
    cutoff = utcnow() - timedelta(minutes=approval_ttl_min())
    res = await db.execute(
        update(IdeApproval)
        .where(
            IdeApproval.id == aid,
            IdeApproval.session_id == sid,
            IdeApproval.status == "pending",
            IdeApproval.created_at < cutoff,
        )
        .values(status="expired", decided_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return res.rowcount == 1


async def record_approval_outcome(
    db: AsyncSession,
    sid: str,
    aid: str,
    *,
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> bool:
    """Record what the ARC-1 call did on an *approved* approval, once.

    Success keeps ``approved`` and stores ``result``; a failure moves it to
    ``failed`` with ``error_code`` (and a ``result`` note, if there is one).
    ``decided_at`` stays the moment the user approved. Conditional on
    ``approved`` without a result, so the request path and the sweep cannot
    both record: the loser gets ``False``.
    """
    values: dict[str, Any] = {}
    if error_code:
        values.update(status="failed", error_code=error_code[:64])
    if result is not None or not error_code:
        values["result_json"] = json.dumps(result or {}, sort_keys=True)
    res = await db.execute(
        update(IdeApproval)
        .where(
            IdeApproval.id == aid,
            IdeApproval.session_id == sid,
            IdeApproval.status == "approved",
            IdeApproval.result_json.is_(None),
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount == 1:
        await touch_session(db, sid)
    await db.commit()
    return res.rowcount == 1


async def count_pending_approvals(db: AsyncSession, sid: str) -> int:
    """Proposals of a session still waiting for a decision (not overdue)."""
    cutoff = utcnow() - timedelta(minutes=approval_ttl_min())
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(IdeApproval)
                .where(
                    IdeApproval.session_id == sid,
                    IdeApproval.status == "pending",
                    IdeApproval.created_at >= cutoff,
                )
            )
        ).scalar_one()
    )


async def armed_trace_ids(db: AsyncSession, sid: str) -> set[str]:
    """Trace request ids that ``trace_start`` approvals of this session armed."""
    rows = await db.execute(
        select(IdeApproval.result_json).where(
            IdeApproval.session_id == sid,
            IdeApproval.action == "trace_start",
            IdeApproval.status == "approved",
            IdeApproval.result_json.is_not(None),
        )
    )
    out: set[str] = set()
    for (text,) in rows.all():
        try:
            data = json.loads(text) if text else None
        except ValueError:
            data = None
        rid = data.get("trace_request_id") if isinstance(data, dict) else None
        if isinstance(rid, str) and rid:
            out.add(rid)
    return out


async def list_unfinished_approvals(
    db: AsyncSession,
    *,
    older_than_s: float,
    session_id: str | None = None,
    limit: int = 200,
) -> list[IdeApproval]:
    """Approvals the developer approved more than ``older_than_s`` seconds
    ago that still have no outcome: what a process dying during the ARC-1
    call leaves behind. Across all sessions (the sweep) unless
    ``session_id`` narrows it to one; oldest first."""
    cutoff = utcnow() - timedelta(seconds=max(0.0, older_than_s))
    where = [
        IdeApproval.status == "approved",
        IdeApproval.result_json.is_(None),
        IdeApproval.decided_at.is_not(None),
        IdeApproval.decided_at < cutoff,
    ]
    if session_id is not None:
        where.append(IdeApproval.session_id == session_id)
    rows = await db.execute(
        select(IdeApproval)
        .where(*where)
        .order_by(IdeApproval.decided_at, IdeApproval.id)
        .limit(limit)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


# --- audit (append-only) --------------------------------------------------


async def add_audit(
    db: AsyncSession,
    *,
    principal: str,
    session_id: str,
    target: str,
    action: str,
    params: dict[str, Any],
    outcome: str,
    request_id: str | None = None,
) -> IdeAuditLog:
    """Append one audit entry and commit it."""
    row = _audit_row(principal=principal, session_id=session_id, target=target,
                     action=action, params=params, outcome=outcome,
                     request_id=request_id)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def _audit_row(
    *,
    principal: str,
    session_id: str,
    target: str,
    action: str,
    params: dict[str, Any],
    outcome: str,
    request_id: str | None = None,
) -> IdeAuditLog:
    """One validated, not yet committed audit row; the single place that
    checks an entry, for ``add_audit`` and for writes that record their
    audit in their own transaction."""
    if not principal:
        raise ValueError("principal is required")
    if not isinstance(action, str) or action not in AUDIT_ACTIONS:
        raise ValueError(f"Unknown audit action: {action!r}")
    if not isinstance(params, dict):
        raise ValueError("audit params must be a dict")
    return IdeAuditLog(
        principal=principal[:255],
        session_id=session_id[:36],
        target=target[:64],
        action=action,
        params_json=json.dumps(params, sort_keys=True),
        outcome=outcome[:16],
        request_id=request_id[:100] if request_id else None,
    )


# --- file revisions ---------------------------------------------------------
#
# Revisions are written by ``agents.ide.workspace.save_state``. Like every
# helper below that takes only a session id, these expect a session the
# caller has already loaded through ``get_owned_session`` (owner check); the
# session id in every WHERE keeps one session's rows out of another's.

SYNTAX_STATUSES = ("ok", "errors", "unavailable")
MAX_SYNTAX_ITEMS = 50
MAX_SYNTAX_MESSAGE = 300


async def list_revisions(db: AsyncSession, sid: str, path: str) -> list[IdeFileRevision]:
    """A path's revisions, newest first."""
    rows = await db.execute(
        select(IdeFileRevision)
        .where(IdeFileRevision.session_id == sid, IdeFileRevision.path == path)
        .order_by(IdeFileRevision.revision.desc())
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


async def get_revision(
    db: AsyncSession, sid: str, path: str, revision: int
) -> IdeFileRevision | None:
    if not sid or not path or isinstance(revision, bool) or not isinstance(revision, int):
        return None
    return (
        await db.execute(
            select(IdeFileRevision)
            .where(
                IdeFileRevision.session_id == sid,
                IdeFileRevision.path == path,
                IdeFileRevision.revision == revision,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def _syntax_items(items: Any) -> list[dict[str, Any]]:
    """Normalise syntax messages to the stored shape, at most 50.

    An item without a string message is dropped; any severity other than
    ``warning`` is stored as ``error`` (an unknown severity must not read
    as harmless); a line that is not a real int is ``None``.
    """
    out: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if len(out) >= MAX_SYNTAX_ITEMS:
            break
        if not isinstance(item, dict):
            continue
        message = item.get("message")
        if not isinstance(message, str) or not message:
            continue
        line = item.get("line")
        if isinstance(line, bool) or not isinstance(line, int):
            line = None
        out.append({
            "line": line,
            "message": _plain_text(message)[:MAX_SYNTAX_MESSAGE],
            "severity": "warning" if item.get("severity") == "warning" else "error",
        })
    return out


async def set_syntax_result(
    db: AsyncSession,
    sid: str,
    path: str,
    revision: int,
    status: str,
    items: list[dict[str, Any]],
) -> IdeFileRevision:
    """Store a syntax check on one revision; ``LookupError`` if there is no
    such revision in this session."""
    if not isinstance(status, str) or status not in SYNTAX_STATUSES:
        raise ValueError(f"Unknown syntax status: {status!r}")
    # Session row first, then the revision row (``lock_session_row``): a
    # session delete holds the session row and cascades to this row.
    await lock_session_row(db, sid)
    res = await db.execute(
        update(IdeFileRevision)
        .where(
            IdeFileRevision.session_id == sid,
            IdeFileRevision.path == path,
            IdeFileRevision.revision == revision,
        )
        .values(
            syntax_status=status,
            syntax_json=json.dumps(_syntax_items(items)),
            syntax_checked_at=utcnow(),
        )
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        await db.commit()
        raise LookupError("unknown_revision")
    await touch_session(db, sid)
    await db.commit()
    row = await get_revision(db, sid, path, revision)
    if row is None:  # the session was deleted after the UPDATE committed
        raise LookupError("unknown_revision")
    return row


# --- review comments --------------------------------------------------------
#
# State machine (server-enforced, plan §1.1):
#   create -> open; open -> sent (request-changes run start, mark_comments_sent);
#   sent -> addressed (resolve_comments tool); open|addressed -> dismissed and
#   addressed|dismissed -> open (the user, set_comment_state). Body edit and
#   delete only while open. Every transition is a conditional UPDATE/DELETE on
#   (id, session_id, allowed source states), so two racing requests cannot
#   both move a comment, nor move it out of a state it already left.

MAX_COMMENT_CHARS = 4000
# The selected text a comment quotes: one line, cut (not refused) at this
# length, because a selection is context for the model, not the request.
MAX_QUOTE_CHARS = 200
MAX_ANSWER_CHARS = 500
COMMENT_STATES = ("open", "sent", "addressed", "dismissed")
DOCUMENT_KINDS = ("design", "plan", "review", "note", "report")
# target state -> states the user may move a comment from
_USER_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "open": ("addressed", "dismissed"),
    "dismissed": ("open", "addressed"),
}
# Control characters other than tab/newline/CR: Postgres TEXT rejects NUL,
# and none of them belong in a plain-text review comment.
_CONTROL = {c: None for c in range(32) if c not in (9, 10, 13)}
_CONTROL[127] = None


class CommentError(Exception):
    """A refused comment operation; ``code`` is the stable API error code
    (``invalid_anchor``, ``invalid_body``, ``invalid_quote``, ``comment_not_found``,
    ``comment_not_editable``, ``invalid_transition``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _plain_text(value: str) -> str:
    # Unicode format characters (category Cf: bidi overrides and isolates,
    # zero-width spaces and joiners, soft hyphen, BOM) are invisible to the
    # developer but not to a model: one inside ``</comment>`` would hide a
    # tag from the prompt's neutralisation while a reader still sees it.
    # Lone surrogates (category Cs: half a UTF-16 pair, e.g. a quote a client
    # cut inside an emoji) cannot be encoded as UTF-8, so storing one would
    # fail the DB write or the JSON answer that echoes it.
    text = value.translate(_CONTROL)
    return "".join(ch for ch in text if unicodedata.category(ch) not in ("Cf", "Cs"))


def plain_text(value: str) -> str:
    """``value`` without control characters other than tab/newline/CR,
    without Unicode format characters and without lone surrogates (the rule
    comment bodies follow; also used for the request-changes note and the
    ``resolve_comments`` answer)."""
    return _plain_text(value)


def _comment_body(body: Any) -> str:
    """The body as stored: plain text without control characters,
    1..MAX_COMMENT_CHARS characters, not only whitespace."""
    if not isinstance(body, str):
        raise CommentError("invalid_body")
    text = _plain_text(body)
    if not text.strip() or len(text) > MAX_COMMENT_CHARS:
        raise CommentError("invalid_body")
    return text


def _comment_quote(quote: Any) -> str | None:
    """The quote as stored: the body's plain-text rule, whitespace runs
    (newlines included) collapsed to one space, cut to MAX_QUOTE_CHARS.
    Nothing left means no quote; anything but a string is ``invalid_quote``."""
    if quote is None:
        return None
    if not isinstance(quote, str):
        raise CommentError("invalid_quote")
    text = " ".join(_plain_text(quote).split())[:MAX_QUOTE_CHARS].rstrip()
    return text or None


def _line_count(text: str) -> int:
    """Lines as the UI numbers them: an empty text still has line 1.

    Splits on ``\\r\\n``, ``\\r`` and ``\\n`` only and drops a final empty
    piece, like ``splitLines`` in ``ui5-ide/webapp/model/sourceView.ts``.
    ``str.splitlines()`` would also break on form feeds, ``\\u2028`` and
    friends, so a line the developer selected in the UI could be refused.
    """
    pieces = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if pieces[-1] == "":
        pieces.pop()
    return max(1, len(pieces))


def _int(value: Any, minimum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


async def add_comment(
    db: AsyncSession,
    sid: str,
    *,
    anchor: str,
    body: str,
    path: str | None = None,
    revision: int | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    kind: str | None = None,
    version: int | None = None,
    paragraph: int | None = None,
    quote: str | None = None,
) -> IdeComment:
    """A new ``open`` comment on a file revision or a document paragraph.

    The anchor must exist in *this* session: a file comment names a
    workspace path and a revision up to the file's latest, with a 1-based
    inclusive line range; a document comment names an artifact kind and
    version and a 0-based paragraph. Fields of the other anchor type must be
    unset. Anything else is ``CommentError("invalid_anchor")``. ``quote``
    is optional (see ``_comment_quote``).
    """
    text = _comment_body(body)
    quoted = _comment_quote(quote)
    # Before any read: an approve in flight finishes (or waits) first.
    await lock_session_row(db, sid)
    if anchor == "file":
        if kind is not None or version is not None or paragraph is not None:
            raise CommentError("invalid_anchor")
        if not isinstance(path, str) or not path:
            raise CommentError("invalid_anchor")
        if not (_int(revision, 1) and _int(line_start, 1) and _int(line_end, 1)):
            raise CommentError("invalid_anchor")
        if line_end < line_start:  # type: ignore[operator]
            raise CommentError("invalid_anchor")
        # The anchored revision itself, not the file's latest number: the
        # lines must exist in the text the comment was written on.
        text_of = (
            await db.execute(
                select(IdeFileRevision.proposed_source).where(
                    IdeFileRevision.session_id == sid,
                    IdeFileRevision.path == path,
                    IdeFileRevision.revision == revision,
                )
            )
        ).scalar_one_or_none()
        if text_of is None or line_end > _line_count(text_of):  # type: ignore[operator]
            raise CommentError("invalid_anchor")
    elif anchor == "document":
        if any(v is not None for v in (path, revision, line_start, line_end)):
            raise CommentError("invalid_anchor")
        if not isinstance(kind, str) or kind not in DOCUMENT_KINDS:
            raise CommentError("invalid_anchor")
        if not (_int(version, 1) and _int(paragraph, 0)):
            raise CommentError("invalid_anchor")
        found = (
            await db.execute(
                select(IdeArtifact.id).where(
                    IdeArtifact.session_id == sid,
                    IdeArtifact.kind == kind,
                    IdeArtifact.version == version,
                ).limit(1)
            )
        ).scalar_one_or_none()
        if found is None:
            raise CommentError("invalid_anchor")
    else:
        raise CommentError("invalid_anchor")
    row = IdeComment(
        session_id=sid,
        anchor=anchor,
        path=path,
        revision=revision,
        line_start=line_start,
        line_end=line_end,
        kind=kind,
        version=version,
        paragraph=paragraph,
        body=text,
        quote=quoted,
        state="open",
    )
    db.add(row)
    await touch_session(db, sid)
    await db.commit()
    await db.refresh(row)
    return row


async def list_comments(
    db: AsyncSession, sid: str, state: str | None = None
) -> list[IdeComment]:
    """A session's comments, oldest first, optionally of one state."""
    if state is not None and state not in COMMENT_STATES:
        raise ValueError(f"Unknown comment state: {state!r}")
    stmt = select(IdeComment).where(IdeComment.session_id == sid)
    if state is not None:
        stmt = stmt.where(IdeComment.state == state)
    stmt = stmt.order_by(IdeComment.created_at.asc(), IdeComment.id.asc())
    rows = await db.execute(stmt.execution_options(populate_existing=True))
    return list(rows.scalars().all())


async def get_comment(db: AsyncSession, sid: str, cid: str) -> IdeComment | None:
    """A comment by id *and* session id: another session's id is ``None``."""
    if not sid or not cid:
        return None
    return (
        await db.execute(
            select(IdeComment)
            .where(IdeComment.id == cid, IdeComment.session_id == sid)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _refused(db: AsyncSession, sid: str, cid: str, code: str) -> CommentError:
    """Why a conditional write matched nothing: the comment is not in this
    session (``comment_not_found``) or not in a state that allows it."""
    exists = await get_comment(db, sid, cid)
    await db.commit()
    return CommentError(code if exists is not None else "comment_not_found")


_KEEP: Any = object()


async def edit_comment(
    db: AsyncSession, sid: str, cid: str, body: str | None, *, quote: Any = _KEEP
) -> IdeComment:
    """Replace the body and/or the quote of an ``open`` comment.

    ``body=None`` keeps the body; a ``quote`` left out keeps the stored one,
    ``quote=None`` clears it. Changing neither is ``invalid_body``."""
    values: dict[str, Any] = {}
    if body is not None:
        values["body"] = _comment_body(body)
    if quote is not _KEEP:
        values["quote"] = _comment_quote(quote)
    if not values:
        raise CommentError("invalid_body")
    # Session row first, comments second: the order ``runner._start`` uses
    # (see ``lock_session_row``), so the two never deadlock on Postgres.
    await lock_session_row(db, sid)
    res = await db.execute(
        update(IdeComment)
        .where(
            IdeComment.id == cid,
            IdeComment.session_id == sid,
            IdeComment.state == "open",
        )
        .values(**values, updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        raise await _refused(db, sid, cid, "comment_not_editable")
    await touch_session(db, sid)
    await db.commit()
    row = await get_comment(db, sid, cid)
    if row is None:  # deleted between the UPDATE's commit and this read
        raise CommentError("comment_not_found")
    return row


async def set_comment_state(
    db: AsyncSession, sid: str, cid: str, state: str
) -> IdeComment:
    """A user transition: dismiss (from open/addressed) or reopen (from
    addressed/dismissed). ``sent`` and ``addressed`` are reached only through
    ``mark_comments_sent`` and ``resolve_comments``."""
    sources = _USER_TRANSITIONS.get(state) if isinstance(state, str) else None
    if sources is None:
        if await get_comment(db, sid, cid) is None:
            raise CommentError("comment_not_found")
        raise CommentError("invalid_transition")
    # Session row first, comments second: the order ``runner._start`` uses
    # (see ``lock_session_row``), so the two never deadlock on Postgres.
    await lock_session_row(db, sid)
    res = await db.execute(
        update(IdeComment)
        .where(
            IdeComment.id == cid,
            IdeComment.session_id == sid,
            IdeComment.state.in_(sources),
        )
        .values(state=state, updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        raise await _refused(db, sid, cid, "invalid_transition")
    await touch_session(db, sid)
    await db.commit()
    row = await get_comment(db, sid, cid)
    if row is None:  # deleted between the UPDATE's commit and this read
        raise CommentError("comment_not_found")
    return row


async def delete_comment(db: AsyncSession, sid: str, cid: str) -> None:
    """Delete an ``open`` comment; once sent it is part of the review trail."""
    # Session row first, comments second: the order ``runner._start`` uses
    # (see ``lock_session_row``), so the two never deadlock on Postgres.
    await lock_session_row(db, sid)
    res = await db.execute(
        delete(IdeComment)
        .where(
            IdeComment.id == cid,
            IdeComment.session_id == sid,
            IdeComment.state == "open",
        )
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        raise await _refused(db, sid, cid, "comment_not_editable")
    await touch_session(db, sid)
    await db.commit()


# What one request-changes run sends at most (oldest first); the rest stay
# ``open`` for the next round. Bounds the prompt: 200 comments of up to 4000
# characters each would be 800 000 characters of user text.
MAX_SENT_COMMENTS = 200
MAX_SENT_CHARS = 100_000


async def mark_comments_sent(db: AsyncSession, sid: str, run_id: str) -> list[IdeComment]:
    """open -> sent for the session's open comments, tagged with the run, in
    the caller's transaction: the request-changes start commits it together
    with the run lock, or rolls both back. Returns the comments moved,
    oldest first.

    At most :data:`MAX_SENT_COMMENTS` comments and :data:`MAX_SENT_CHARS`
    characters of bodies are sent, oldest first, stopping at the first
    comment that would pass a limit (the order the developer wrote them in is
    kept). The rest stay ``open``; the caller counts them for the prompt and
    the ``comments`` event."""
    # The run start already holds it (FOR UPDATE in ``runner._start``);
    # taken again so the helper is safe on its own.
    await lock_session_row(db, sid)
    candidates = (
        await db.execute(
            select(IdeComment.id, func.length(IdeComment.body))
            .where(IdeComment.session_id == sid, IdeComment.state == "open")
            .order_by(IdeComment.created_at, IdeComment.id)
        )
    ).all()
    chosen: list[str] = []
    chars = 0
    for cid, length in candidates:
        length = length or 0
        if len(chosen) >= MAX_SENT_COMMENTS or chars + length > MAX_SENT_CHARS:
            break
        chosen.append(cid)
        chars += length
    ids: list[str] = []
    for chunk in _chunks(chosen):
        res = await db.execute(
            update(IdeComment)
            .where(
                IdeComment.session_id == sid,
                IdeComment.state == "open",
                IdeComment.id.in_(chunk),
            )
            .values(state="sent", sent_run_id=run_id[:36] if run_id else None,
                    updated_at=utcnow())
            .returning(IdeComment.id)
            .execution_options(synchronize_session=False)
        )
        ids.extend(res.scalars().all())
    if not ids:
        return []
    await touch_session(db, sid)
    out: list[IdeComment] = []
    for chunk in _chunks(ids):
        rows = await db.execute(
            select(IdeComment)
            .where(IdeComment.session_id == sid, IdeComment.id.in_(chunk))
            .execution_options(populate_existing=True)
        )
        out.extend(rows.scalars().all())
    out.sort(key=lambda c: (c.created_at, c.id))
    return out


async def reopen_sent_comments(
    db: AsyncSession, sid: str, *, run_id: str | None
) -> list[str]:
    """sent -> open for the comments run ``run_id`` sent and left ``sent``, in
    the caller's transaction (not committed). Called when a request-changes
    run ends -- whatever the outcome -- together with the run-lock release,
    and when a dead run's lock is reclaimed: ``sent`` exists only while its
    run is in flight. Scoped to the run: a reclaimed run that finishes late
    must not reopen what a newer run sent and is still working on. Returns
    the ids moved, oldest first."""
    tag = IdeComment.sent_run_id == run_id[:36] if run_id else (
        IdeComment.sent_run_id.is_(None)
    )
    res = await db.execute(
        update(IdeComment)
        .where(IdeComment.session_id == sid, IdeComment.state == "sent", tag)
        .values(state="open", updated_at=utcnow())
        .returning(IdeComment.id, IdeComment.created_at)
        .execution_options(synchronize_session=False)
    )
    rows = sorted(res.all(), key=lambda r: (r[1], r[0]))
    return [r[0] for r in rows]


async def resolve_comments(
    db: AsyncSession, sid: str, items: list[tuple[str, str]], *, run_id: str
) -> tuple[list[str], list[str]]:
    """sent -> addressed with a one-line answer, for the ``resolve_comments``
    tool. An id that is not a ``sent`` comment of this session sent by run
    ``run_id`` (unknown, foreign, open, dismissed, already addressed -- also
    earlier in the same call -- or sent by another run) is rejected, never an
    error. The answer is model output stored as
    plain text, cut to ``MAX_ANSWER_CHARS``; a non-string answer rejects the
    item. Returns ``(resolved, rejected)`` ids in input order."""
    # Session row first, comments second: the order ``runner._start`` uses
    # (see ``lock_session_row``), so the two never deadlock on Postgres.
    await lock_session_row(db, sid)
    resolved: list[str] = []
    rejected: list[str] = []
    for cid, answer in items:
        if not isinstance(cid, str) or not cid or not isinstance(answer, str):
            rejected.append(cid if isinstance(cid, str) else str(cid))
            continue
        res = await db.execute(
            update(IdeComment)
            .where(
                IdeComment.id == cid,
                IdeComment.session_id == sid,
                IdeComment.state == "sent",
                IdeComment.sent_run_id == (run_id or "")[:36],
            )
            .values(
                state="addressed",
                answer=_plain_text(answer)[:MAX_ANSWER_CHARS],
                updated_at=utcnow(),
            )
            .execution_options(synchronize_session=False)
        )
        (resolved if res.rowcount == 1 else rejected).append(cid)
    if resolved:
        await touch_session(db, sid)
    await db.commit()
    return resolved, rejected


def _chunks(values: list[str], size: int = 500) -> list[list[str]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


# How many object names a worklist row shows; ``objects_total`` says how
# many there are.
OBJECTS_SHOWN = 5
_CHANGED_STATES = ("new", "modified")


async def object_summaries(
    db: AsyncSession, sids: list[str]
) -> dict[str, tuple[list[str], int, int]]:
    """``sid -> (first OBJECTS_SHOWN object names, total, changed)`` for the
    worklist: the distinct object names of each session's files in path
    order (scratch notes have none), and how many of them have a ``new`` or
    ``modified`` file. One query per 500 sessions; only names and states are
    loaded, never source."""
    names: dict[str, list[str]] = {sid: [] for sid in sids}
    seen: dict[str, set[str]] = {sid: set() for sid in sids}
    changed: dict[str, set[str]] = {sid: set() for sid in sids}
    for chunk in _chunks(list(names)):
        rows = await db.execute(
            select(
                IdeWorkspaceFile.session_id,
                IdeWorkspaceFile.object_name,
                IdeWorkspaceFile.state,
            )
            .where(
                IdeWorkspaceFile.session_id.in_(chunk),
                IdeWorkspaceFile.object_name.is_not(None),
            )
            .order_by(IdeWorkspaceFile.session_id, IdeWorkspaceFile.path)
        )
        for sid, name, state in rows.all():
            if name not in seen[sid]:  # set lookup: no O(n^2) per session
                seen[sid].add(name)
                names[sid].append(name)
            if state in _CHANGED_STATES:
                changed[sid].add(name)
    return {
        sid: (found[:OBJECTS_SHOWN], len(found), len(changed[sid]))
        for sid, found in names.items()
    }


async def count_findings(db: AsyncSession, sids: list[str]) -> dict[str, int]:
    """``sid -> number of findings``, one grouped query per 500 sessions."""
    out = {sid: 0 for sid in sids}
    for chunk in _chunks(list(out)):
        rows = await db.execute(
            select(IdeFinding.session_id, func.count())
            .where(IdeFinding.session_id.in_(chunk))
            .group_by(IdeFinding.session_id)
        )
        for sid, n in rows.all():
            out[sid] = n
    return out


async def count_open_comments(
    db: AsyncSession, sids: list[str]
) -> dict[str, tuple[int, int]]:
    """``sid -> (open, open + sent)`` for every given session, in one grouped
    query per 500 ids. The second number blocks approve."""
    out = {sid: (0, 0) for sid in sids}
    for chunk in _chunks(list(out)):
        rows = await db.execute(
            select(IdeComment.session_id, IdeComment.state, func.count())
            .where(
                IdeComment.session_id.in_(chunk),
                IdeComment.state.in_(("open", "sent")),
            )
            .group_by(IdeComment.session_id, IdeComment.state)
        )
        for sid, state, n in rows.all():
            opened, unresolved = out[sid]
            if state == "open":
                opened += n
            out[sid] = (opened, unresolved + n)
    return out


# --- pins -------------------------------------------------------------------


class PinConflict(Exception):
    """The pins kept changing under a compare-and-set; routes answer 409."""

#
# What the developer approved: a version per document kind and a revision
# per file. Written only by the approve path (``set_pin``/``set_file_pins``),
# never by a run or a tool.

PIN_KINDS = ("design", "plan", "review", "report")


def pins_of(session: IdeSession | None) -> dict[str, Any]:
    """The session's pins; unreadable JSON or entries are left out, so a
    legacy or damaged row reads as "nothing pinned"."""
    return _parse_pins(getattr(session, "pins_json", None))


def _parse_pins(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        return {}
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {k: data[k] for k in PIN_KINDS if _int(data.get(k), 1)}
    files = data.get("files")
    if isinstance(files, dict):
        clean = {p: r for p, r in files.items() if isinstance(p, str) and _int(r, 1)}
        if clean:
            out["files"] = clean
    return out


async def _update_pins(db: AsyncSession, session: IdeSession, change) -> None:
    """Read-modify-write of ``pins_json`` as compare-and-set: the UPDATE
    only applies while the column still holds what was read, so two approve
    requests cannot overwrite each other's pin. Retried on a lost race."""
    for _ in range(5):
        current = (
            await db.execute(
                select(IdeSession.pins_json).where(IdeSession.id == session.id)
            )
        ).scalar_one_or_none()
        pins = _parse_pins(current)
        change(pins)
        guard = (
            IdeSession.pins_json.is_(None)
            if current is None
            else IdeSession.pins_json == current
        )
        new = json.dumps(pins, sort_keys=True)
        res = await db.execute(
            update(IdeSession)
            .where(IdeSession.id == session.id, guard)
            .values(pins_json=new, updated_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if res.rowcount == 1:
            session.pins_json = new
            return
    raise PinConflict("pins changed concurrently; try again")


async def set_pin(db: AsyncSession, session: IdeSession, kind: str, version: int) -> None:
    """Pin ``version`` of a document kind (approve path only)."""
    if not isinstance(kind, str) or kind not in PIN_KINDS:
        raise ValueError(f"Unknown pin kind: {kind!r}")
    if not _int(version, 1):
        raise ValueError("pin version must be an int >= 1")
    await _update_pins(db, session, lambda pins: pins.__setitem__(kind, version))


def _check_file_pins(revisions: Any) -> None:
    if not isinstance(revisions, dict):
        raise ValueError("file pins must be a dict")
    for path, rev in revisions.items():
        if not isinstance(path, str) or not path or not _int(rev, 1):
            raise ValueError("file pins map paths to revisions >= 1")


async def set_file_pins(
    db: AsyncSession, session: IdeSession, revisions: dict[str, int]
) -> None:
    """Pin file revisions; merged into the stored ``files`` map, given paths
    win. The approve of ``propose`` uses :func:`pins_after` with
    ``files=`` instead, which replaces the map."""
    _check_file_pins(revisions)

    def change(pins: dict[str, Any]) -> None:
        pins["files"] = {**pins.get("files", {}), **revisions}

    await _update_pins(db, session, change)


def pins_after(
    raw: str | None,
    *,
    kind: str | None = None,
    version: int | None = None,
    files: dict[str, int] | None = None,
) -> str:
    """``pins_json`` after pinning ``version`` of ``kind`` and/or replacing
    the file pins with ``files`` -- computed, not written, so the approve
    can store it in the same conditional UPDATE that moves the stage.

    ``files`` *replaces* the map: it is every object path proposed now, so a
    pin on a path that is no longer proposed does not linger."""
    pins = _parse_pins(raw)
    if kind is not None:
        if kind not in PIN_KINDS:
            raise ValueError(f"Unknown pin kind: {kind!r}")
        if not _int(version, 1):
            raise ValueError("pin version must be an int >= 1")
        pins[kind] = version
    if files is not None:
        _check_file_pins(files)
        if files:
            pins["files"] = dict(files)
        else:
            pins.pop("files", None)
    return json.dumps(pins, sort_keys=True)


# --- worklist marker ----------------------------------------------------------

_DOCUMENT_STAGE_KIND = {"design": "design", "plan": "plan", "review": "review"}


async def waiting_for(
    db: AsyncSession, sessions: list[IdeSession]
) -> dict[str, str | None]:
    """Why each session waits for its developer (plan §1.2), first match wins:

    ``approval`` (diagnose, a pending approval within its TTL), ``comments``
    (a comment ``addressed``), ``changes`` (idle change session in
    ``propose`` with an ABAP object proposal -- not a scratch note -- whose
    revision is not the pinned one),
    ``document`` (idle change session in design/plan/review whose latest
    artifact of that kind is not the pinned version), else ``None``.
    A session in stage ``done`` never waits: it is finished, and a comment
    the agent addressed there has no step left to be answered in.

    A fixed number of grouped queries (per 500 sessions), never one per
    session; a query is skipped when no session can match its rule.
    """
    out: dict[str, str | None] = {s.id: None for s in sessions}
    if not sessions:
        return out
    diag = [s.id for s in sessions if s.session_type == "diagnose"]
    idle_change = [
        s for s in sessions if s.session_type == "change" and s.status == "idle"
    ]
    propose = [s.id for s in idle_change if s.stage == "propose"]
    documents = [s.id for s in idle_change if s.stage in _DOCUMENT_STAGE_KIND]

    approvals: set[str] = set()
    cutoff = utcnow() - timedelta(minutes=approval_ttl_min())
    for chunk in _chunks(diag):
        rows = await db.execute(
            select(IdeApproval.session_id).distinct().where(
                IdeApproval.session_id.in_(chunk),
                IdeApproval.status == "pending",
                IdeApproval.created_at >= cutoff,
            )
        )
        approvals.update(rows.scalars().all())

    addressed: set[str] = set()
    for chunk in _chunks(list(out)):
        rows = await db.execute(
            select(IdeComment.session_id).distinct().where(
                IdeComment.session_id.in_(chunk), IdeComment.state == "addressed"
            )
        )
        addressed.update(rows.scalars().all())

    proposals: dict[str, list[tuple[str, int]]] = {}
    for chunk in _chunks(propose):
        rows = await db.execute(
            select(
                IdeWorkspaceFile.session_id,
                IdeWorkspaceFile.path,
                IdeWorkspaceFile.revision,
            ).where(
                IdeWorkspaceFile.session_id.in_(chunk),
                IdeWorkspaceFile.proposed_source.is_not(None),
                # Scratch notes are reviewable but are not changes to approve.
                IdeWorkspaceFile.object_type.is_not(None),
            )
        )
        for sid, path, rev in rows.all():
            proposals.setdefault(sid, []).append((path, rev))

    latest: dict[tuple[str, str], int] = {}
    for chunk in _chunks(documents):
        rows = await db.execute(
            select(IdeArtifact.session_id, IdeArtifact.kind, func.max(IdeArtifact.version))
            .where(
                IdeArtifact.session_id.in_(chunk),
                IdeArtifact.kind.in_(tuple(_DOCUMENT_STAGE_KIND.values())),
            )
            .group_by(IdeArtifact.session_id, IdeArtifact.kind)
        )
        for sid, kind, version in rows.all():
            latest[(sid, kind)] = version

    propose_set, documents_set = set(propose), set(documents)
    for s in sessions:
        if s.stage == "done":
            continue
        if s.id in approvals:
            out[s.id] = "approval"
        elif s.id in addressed:
            out[s.id] = "comments"
        elif s.id in propose_set:
            pinned = pins_of(s).get("files", {})
            if any(pinned.get(path) != rev for path, rev in proposals.get(s.id, [])):
                out[s.id] = "changes"
        elif s.id in documents_set:
            kind = _DOCUMENT_STAGE_KIND[s.stage]
            version = latest.get((s.id, kind))
            if version is not None and version != pins_of(s).get(kind):
                out[s.id] = "document"
    return out
