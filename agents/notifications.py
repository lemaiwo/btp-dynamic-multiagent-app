"""Finished-run notifications for the admin UI: ``/admin/api/notifications``.

Nothing is written when a run ends and the runners know nothing of this
module: the list is derived from the run tables (``job_runs``,
``workflow_runs``) at the moment it is asked for, so a run that finished
while no instance was looking, or on another instance, is listed all the
same. The only stored state is one read marker per admin
(``admin_notification_state``): up to when that admin has read the list. A
run is unread when it finished after the marker.

``agents/admin.py`` includes this router under its ``/admin`` prefix. Each
route carries ``require_admin`` itself, as the OData catalogue routes do.

Only the columns the answer holds are selected. A run's summary, report,
error and activity can hold what an agent read; they stay behind the run
routes and never pass through here.

The caller is ``current_principal`` (bound by the app's middleware from the
validated token), never anything of the request. The body of ``seen`` is
read as raw JSON and validated here, so a refusal is one fixed text and
never echoes what was sent.

This module imports ``agents.auth`` and ``agents.db`` only, never
``agents.admin`` (which imports it at the end of the module).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agents.auth import current_principal, require_admin
from agents.db import AdminNotificationState, JobRun, SessionLocal, WorkflowRun

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

# How far back a finished run is a notification, and how many one answer
# lists. `unread_count` covers the whole window, not only what is listed.
WINDOW = timedelta(days=7)
MAX_ITEMS = 50
# The marker's key when no principal is bound (a local run without XSUAA).
LOCAL_PRINCIPAL = "local"
# The `seen` request is one timestamp. Checked while reading, so an admin
# token cannot make the worker buffer and parse a body of any size.
SEEN_BODY_BYTES = 1024
_UP_TO = "up_to"
_REFUSED = "up_to must be a timestamp with a time zone"

# (kind, model, the column that holds the run's name)
_SOURCES = (
    ("agent", JobRun, JobRun.agent_name),
    ("workflow", WorkflowRun, WorkflowRun.workflow_name),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    # SQLite and SAP HANA hand a timestamp back naive; it was written as UTC.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _principal() -> str:
    return current_principal.get() or LOCAL_PRINCIPAL


async def _read_marker(session: AsyncSession, principal: str) -> datetime | None:
    seen_at = (
        await session.execute(
            select(AdminNotificationState.seen_at).where(
                AdminNotificationState.principal == principal
            )
        )
    ).scalar_one_or_none()
    return None if seen_at is None else _utc(seen_at)


async def _marker(session: AsyncSession, principal: str, now: datetime) -> datetime:
    """The caller's marker; a caller without one gets it set to ``now``.

    So the first look at the list shows nothing as unread, instead of a week
    of runs that finished before this admin ever opened it. Two first calls
    of one admin at once both find no row: the second insert fails on the
    primary key and that call reads the row the first one committed.
    """
    seen_at = await _read_marker(session, principal)
    if seen_at is not None:
        return seen_at
    try:
        await session.execute(
            insert(AdminNotificationState).values(principal=principal, seen_at=now)
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        seen_at = await _read_marker(session, principal)
        if seen_at is None:
            raise
        return seen_at
    return now


async def _finished(session: AsyncSession, cutoff: datetime) -> list[dict[str, Any]]:
    """The newest finished runs of both kinds, without ``unread``.

    One statement per table, merged here: each is cut to ``MAX_ITEMS``, so
    the newest ``MAX_ITEMS`` of both together are among them.
    """
    found: list[tuple[datetime, str, dict[str, Any]]] = []
    for kind, model, name in _SOURCES:
        rows = await session.execute(
            select(
                model.id, name, model.status, model.trigger, model.created_by,
                model.finished_at,
            )
            .where(model.finished_at.is_not(None), model.finished_at >= cutoff)
            .order_by(model.finished_at.desc(), model.id.desc())
            .limit(MAX_ITEMS)
        )
        for run_id, run_name, status, trigger, created_by, finished_at in rows:
            finished = _utc(finished_at)
            found.append(
                (
                    finished,
                    run_id,
                    {
                        "kind": kind,
                        "run_id": run_id,
                        "name": run_name,
                        "status": status,
                        "trigger": trigger,
                        "created_by": created_by,
                        "finished_at": finished.isoformat(),
                    },
                )
            )
    found.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    return [{**item, "unread": finished} for finished, _, item in found[:MAX_ITEMS]]


async def _unread_count(session: AsyncSession, cutoff: datetime, seen_at: datetime) -> int:
    total = 0
    for _, model, _name in _SOURCES:
        total += (
            await session.execute(
                select(func.count())
                .select_from(model)
                .where(
                    model.finished_at.is_not(None),
                    model.finished_at >= cutoff,
                    model.finished_at > seen_at,
                )
            )
        ).scalar_one()
    return total


@router.get("", dependencies=[Depends(require_admin)])
async def list_notifications() -> dict[str, Any]:
    """The finished runs of the last 7 days, newest first, and how many of
    them the caller has not read."""
    principal = _principal()
    now = _now()
    cutoff = now - WINDOW
    async with SessionLocal() as session:
        seen_at = await _marker(session, principal, now)
        items = await _finished(session, cutoff)
        unread_count = await _unread_count(session, cutoff, seen_at)
    for item in items:
        # `_finished` left the instant here for this comparison.
        item["unread"] = item["unread"] > seen_at
    return {"items": items, "unread_count": unread_count, "seen_at": seen_at.isoformat()}


async def _up_to(request: Request) -> datetime:
    """The body's ``up_to`` as a UTC instant, or the one refusal.

    Every way of being wrong gets the same fixed text: the body is what a
    client sent and none of it goes back.
    """
    refused = HTTPException(status_code=422, detail=_REFUSED)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > SEEN_BODY_BYTES:
            raise refused
        chunks.append(chunk)
    try:
        data = json.loads(b"".join(chunks))
    except (ValueError, RecursionError):
        # ValueError covers JSONDecodeError and a body that is not UTF-8.
        raise refused from None
    if not isinstance(data, dict) or set(data) != {_UP_TO}:
        raise refused
    value = data[_UP_TO]
    if not isinstance(value, str):
        raise refused
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            raise refused
        # OverflowError: an instant that leaves the calendar once in UTC.
        return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise refused from None


@router.post("/seen", dependencies=[Depends(require_admin)])
async def mark_seen(request: Request) -> dict[str, str]:
    """Move the caller's marker to ``min(up_to, now)``, forward only.

    ``up_to`` is the ``finished_at`` of the newest notification the admin
    was shown, not "now": a run that finished between the list and this call
    stays unread. The clamp keeps a client from marking the future as read.
    """
    up_to = await _up_to(request)
    principal = _principal()
    now = _now()
    target = min(up_to, now)
    async with SessionLocal() as session:
        await _marker(session, principal, now)
        # Conditional, so of two calls the later instant wins whatever order
        # they arrive in.
        await session.execute(
            update(AdminNotificationState)
            .where(
                AdminNotificationState.principal == principal,
                AdminNotificationState.seen_at < target,
            )
            .values(seen_at=target)
        )
        await session.commit()
        seen_at = await _read_marker(session, principal)
    return {"seen_at": (seen_at or target).isoformat()}
