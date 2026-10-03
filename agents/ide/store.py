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
"""

from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agents.ide.models import (
    IDE_CHILD_MODELS,
    IdeApproval,
    IdeArtifact,
    IdeAuditLog,
    IdeConventions,
    IdeFinding,
    IdeMessage,
    IdeSession,
    utcnow,
)

log = logging.getLogger(__name__)


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
AUDIT_ACTIONS = ("trace_arm", "trace_cancel", "trace_deny", "trace_failed")


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
    n = len(result.scalars().all())
    await db.commit()
    return n


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
    db: AsyncSession, sid: str, *, stage: str, kind: str, content: str
) -> IdeArtifact:
    """Store a new version of ``kind`` for the session (max + 1).

    One session runs one stage at a time (the ``status`` lock), so two
    concurrent writers of the same (session, kind) do not occur.
    """
    current = (
        await db.execute(
            select(func.max(IdeArtifact.version)).where(
                IdeArtifact.session_id == sid, IdeArtifact.kind == kind
            )
        )
    ).scalar()
    row = IdeArtifact(
        session_id=sid,
        stage=stage,
        kind=kind,
        content=content,
        version=(current or 0) + 1,
    )
    db.add(row)
    await touch_session(db, sid)
    await db.commit()
    await db.refresh(row)
    return row


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
    db: AsyncSession, target: str, **fields: Any
) -> IdeConventions:
    """Create or update a target's conventions; only given fields change."""
    unknown = set(fields) - set(_CONVENTION_FIELDS)
    if unknown:
        raise ValueError(f"Unknown conventions fields: {sorted(unknown)}")
    row = await db.get(IdeConventions, target)
    if row is None:
        row = IdeConventions(target=target)
        db.add(row)
    for key, value in fields.items():
        if value is not None:
            setattr(row, key, value)
    await db.commit()
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
    *,
    clear_detail: bool = False,
) -> list[IdeFinding]:
    """Insert or update findings on ``(session_id, kind, ref_id)``.

    ``detail`` (raw text of a detail read) is written when an item carries
    it and otherwise left as stored: a list read after a detail read does
    not wipe the text. ``clear_detail=True`` is what a masked run passes:
    no detail is written, and the stored text of every finding touched is
    removed, so a target that lost its ``non_production`` flag does not
    keep serving raw text for what is being looked at.

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
        if clear_detail:
            fields["detail"] = None
        elif fields["detail"] is None and earlier is not None:
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
                if name == "detail" and value is None and not clear_detail:
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
    """Append one audit entry. Deliberately the only audit helper here."""
    if not principal:
        raise ValueError("principal is required")
    if not isinstance(action, str) or action not in AUDIT_ACTIONS:
        raise ValueError(f"Unknown audit action: {action!r}")
    if not isinstance(params, dict):
        raise ValueError("audit params must be a dict")
    row = IdeAuditLog(
        principal=principal[:255],
        session_id=session_id[:36],
        target=target[:64],
        action=action,
        params_json=json.dumps(params, sort_keys=True),
        outcome=outcome[:16],
        request_id=request_id[:100] if request_id else None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row
