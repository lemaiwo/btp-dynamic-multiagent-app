"""Owner-scoped persistence helpers for IDE sessions.

Every session-level read takes ``owner`` and filters on it in SQL, so a
caller can never reach another user's session by guessing an id. Functions
that take only a session id (``list_messages``, ``add_artifact``, ...) expect
the caller to have loaded that session through ``get_owned_session`` first.

``list_all_sessions_meta`` is the admin overview: metadata only, never
messages, artifacts or sources.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agents.ide.models import (
    IDE_CHILD_MODELS,
    IdeArtifact,
    IdeConventions,
    IdeMessage,
    IdeSession,
    utcnow,
)

# Days an idle session is kept; 0 disables the purge.
IDE_SESSION_RETENTION_DAYS = int(os.environ.get("IDE_SESSION_RETENTION_DAYS", "90"))

_CONVENTION_FIELDS = (
    "label",
    "destination",
    "namespace",
    "package",
    "atc_variant",
    "clean_core_level",
    "free_text",
)


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
    db: AsyncSession, *, owner: str, title: str, target: str
) -> IdeSession:
    if not owner:
        raise ValueError("owner is required")
    row = IdeSession(owner=owner, title=title, target=target)
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


async def purge_sessions_older_than(db: AsyncSession, days: int) -> int:
    """Delete idle sessions (and their children) not updated for ``days`` days.

    The cutoff and the ``running`` exclusion sit in the DELETE itself, so a
    session that became active between selecting and deleting is kept; only
    the children of rows the DELETE actually removed are deleted.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    cutoff = utcnow() - timedelta(days=days)
    result = await db.execute(
        delete(IdeSession)
        .where(IdeSession.updated_at < cutoff, IdeSession.status != "running")
        .returning(IdeSession.id)
    )
    sids = list(result.scalars().all())
    if sids:
        await _delete_children(db, sids)
    await db.commit()
    return len(sids)


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
