"""IDE REST API under ``/ide/api``: sessions, approve, artifacts, conventions.

Authorization rules (contract §1.2):

- Every route depends on ``require_developer`` (router-level and through
  ``_caller``); conventions writes and the admin overview also depend on
  ``require_admin``.
- Every ``/sessions/{sid}`` route first loads the session with
  ``store.get_owned_session(sid, principal)``. A session that belongs to
  someone else answers **404**, exactly like one that does not exist, so ids
  cannot be probed.
- Child rows (artifacts) are only reached through an owned session and are
  checked against its id.
- ``GET /admin/sessions`` returns ``IdeSession.meta()`` rows: metadata only,
  never messages, artifacts or sources.
- ``StageGateError`` maps to 409 ``{"detail", "code"}`` (429 for
  ``usage_exhausted``); unknown codes keep the server message.

- Workspace files, open, search and lint call ARC-1 through
  ``agents.ide.arc1`` as the signed-in user; the client checks
  ``readonly.check_call`` before every call. ``?path=`` goes through
  ``paths.clean_workspace_path`` (422) after the owner check, so another
  user's session is 404 whatever the path. ARC-1 failures answer
  ``{"detail", "code"}`` with the client's status (401 no user token, 403
  refused, 424 not configured or no user token, 502 ARC-1/destination
  failure or an ARC-1 error payload, 413 source larger than 1 MB).

Stage runs (``POST .../messages``, ``.../revise``) stream server-sent
events (contract §1.3, ``agents.ide.sse``). Ownership is checked and the DB
session closed before the run starts; ``runner.run_stage`` then runs in its
own task, created inside the request context so ``current_jwt`` /
``current_principal`` reach ``DestinationAuth``. Its gate check and lock
happen before its first event (``run``), and the route waits for that event:
``SessionNotFound`` answers 404 and ``StageGateError`` 409/429 as JSON, never
as a stream. A client disconnect only closes the relay; the run goes on to
completion and persists its results. ``POST .../cancel`` (owner only) stops a
run in this process, or else lets ``runner.release_stale`` free a stale
``running`` lock (a run lost in a crash). Runner ``error`` events
(``run_failed``, ``run_timeout``, ``usage_exhausted``, ``agent_missing``) are
relayed as they are.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.auth import current_principal, get_validator, require_admin, require_developer
from agents.db import SessionLocal
from agents.ide import arc1, paths, runner, sse, store
from agents.ide.models import (
    IDE_CHILD_MODELS,
    IdeArtifact,
    IdeConventions,
    IdeMessage,
    IdeSession,
    IdeWorkspaceFile,
)
from agents.ide.stages import StageGateError, approve, request_cap

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ide/api", dependencies=[Depends(require_developer)])

_TARGET_PATTERN = r"^[A-Za-z0-9_.\-]{1,64}$"


# --- dependencies -----------------------------------------------------------


def _principal(claims: dict[str, Any]) -> str:
    """The caller's identity: the middleware's principal, else the token's user."""
    principal = current_principal.get() or (claims or {}).get("user_name")
    if not principal:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="No user identity"
        )
    return str(principal)


def _caller(claims: dict[str, Any] = Depends(require_developer)) -> str:
    return _principal(claims)


async def _db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as db:
        yield db


async def _owned(db: AsyncSession, sid: str, principal: str) -> IdeSession:
    row = await store.get_owned_session(db, sid, principal)
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return row


def _gate_response(exc: StageGateError) -> JSONResponse:
    code = 429 if exc.code == "usage_exhausted" else 409
    return JSONResponse(status_code=code, content={"detail": exc.message, "code": exc.code})


# --- bodies -----------------------------------------------------------------


def _clean_title(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("title must not be empty")
    if len(value) > 200:
        raise ValueError("title is at most 200 characters")
    return value


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    target: str = Field(pattern=_TARGET_PATTERN)

    _title = field_validator("title")(_clean_title)


class SessionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str

    _title = field_validator("title")(_clean_title)


MAX_MESSAGE_CHARS = 20000


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)

    _text = field_validator("text")(_not_blank)


class ReviseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feedback: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)

    _feedback = field_validator("feedback")(_not_blank)


class ConventionsBody(BaseModel):
    """Upsert body; omitted fields keep their stored value."""

    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, max_length=120)
    destination: str | None = Field(default=None, max_length=200)
    namespace: str | None = Field(default=None, max_length=30)
    package: str | None = Field(default=None, max_length=30)
    atc_variant: str | None = Field(default=None, max_length=30)
    clean_core_level: str | None = Field(default=None, pattern=r"^[A-D]$")
    free_text: str | None = Field(default=None, max_length=20000)


class OpenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(pattern=r"^[A-Za-z]{4}$")
    name: str = Field(pattern=r"^\s*[A-Za-z0-9_/$]{1,40}\s*$")


# --- serialisers ------------------------------------------------------------


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _session_out(row: IdeSession) -> dict[str, Any]:
    out = row.meta()
    out["requests_used"] = row.requests_used or 0
    out["request_cap"] = request_cap()
    return out


def _artifact_summary(row: IdeArtifact) -> dict[str, Any]:
    return {
        "id": row.id,
        "stage": row.stage,
        "kind": row.kind,
        "version": row.version,
        "created_at": _iso(row.created_at),
    }


def _artifact_out(row: IdeArtifact) -> dict[str, Any]:
    return {**_artifact_summary(row), "content": row.content}


def _message_out(row: IdeMessage) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row.id,
        "stage": row.stage,
        "role": row.role,
        "content": row.content,
        "created_at": _iso(row.created_at),
    }
    if row.activity_json:
        try:
            out["activity"] = json.loads(row.activity_json)
        except ValueError:
            pass
    return out


def _file_summary(row: IdeWorkspaceFile) -> dict[str, Any]:
    return {
        "path": row.path,
        "state": row.state,
        "object_type": row.object_type,
        "object_name": row.object_name,
    }


def _conventions_out(row: IdeConventions) -> dict[str, Any]:
    return {
        "target": row.target,
        "label": row.label,
        "destination": row.destination,
        "namespace": row.namespace,
        "package": row.package,
        "atc_variant": row.atc_variant,
        "clean_core_level": row.clean_core_level,
        "free_text": row.free_text,
        "updated_at": _iso(row.updated_at),
    }


# --- me ---------------------------------------------------------------------


@router.get("/me")
async def me(
    claims: dict[str, Any] = Depends(require_developer),
    db: AsyncSession = Depends(_db),
) -> dict[str, Any]:
    principal = _principal(claims)
    validator = get_validator()
    # A UI hint only; admin routes enforce require_admin themselves.
    is_admin = True if validator is None else bool(validator.has_scope(claims, "admin"))
    targets = [c.target for c in await store.list_conventions(db)]
    return {"principal": principal, "is_admin": is_admin, "targets": targets}


# --- sessions ---------------------------------------------------------------


@router.get("/sessions")
async def list_sessions(
    principal: str = Depends(_caller), db: AsyncSession = Depends(_db)
) -> list[dict[str, Any]]:
    return [_session_out(s) for s in await store.list_owned_sessions(db, principal)]


@router.post("/sessions", status_code=201)
async def create_session(
    body: SessionCreate,
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
) -> dict[str, Any]:
    if await store.get_conventions(db, body.target) is None:
        raise HTTPException(status_code=422, detail=f"Unknown target {body.target!r}")
    row = await store.create_session(
        db, owner=principal, title=body.title, target=body.target
    )
    return _session_out(row)


@router.get("/sessions/{sid}")
async def get_session(
    sid: str, principal: str = Depends(_caller), db: AsyncSession = Depends(_db)
) -> dict[str, Any]:
    row = await _owned(db, sid, principal)
    artifacts = await store.list_artifacts(db, row.id)
    files = (
        await db.execute(
            select(IdeWorkspaceFile)
            .where(IdeWorkspaceFile.session_id == row.id)
            .order_by(IdeWorkspaceFile.path)
        )
    ).scalars().all()
    return {
        **_session_out(row),
        "artifacts": [_artifact_summary(a) for a in artifacts],
        "files": [_file_summary(f) for f in files],
    }


@router.patch("/sessions/{sid}")
async def patch_session(
    sid: str,
    body: SessionPatch,
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
) -> dict[str, Any]:
    row = await _owned(db, sid, principal)
    row.title = body.title
    await db.commit()
    await db.refresh(row)
    return _session_out(row)


_RUN_IN_PROGRESS = StageGateError(
    "run_in_progress", "A run is in progress for this session; cancel it first."
)


@router.delete("/sessions/{sid}", status_code=204)
async def delete_session(
    sid: str, principal: str = Depends(_caller), db: AsyncSession = Depends(_db)
):
    row = await _owned(db, sid, principal)
    # The running check sits in the DELETE itself, so a run that started
    # after the load still blocks it.
    result = await db.execute(
        delete(IdeSession)
        .where(
            IdeSession.id == row.id,
            IdeSession.owner == principal,
            IdeSession.status != "running",
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        return _gate_response(_RUN_IN_PROGRESS)
    # SQLite ignores ON DELETE CASCADE without PRAGMA foreign_keys=ON.
    for model in IDE_CHILD_MODELS:
        await db.execute(delete(model).where(model.session_id == row.id))
    await db.commit()
    return Response(status_code=204)


@router.get("/sessions/{sid}/messages")
async def list_messages(
    sid: str, principal: str = Depends(_caller), db: AsyncSession = Depends(_db)
) -> list[dict[str, Any]]:
    row = await _owned(db, sid, principal)
    return [_message_out(m) for m in await store.list_messages(db, row.id)]


# --- stage runs (SSE) -------------------------------------------------------

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

# The stream tasks of this process, by session: a strong reference (so a
# run is never garbage-collected after its client left) and what cancel
# looks at while a run is still starting.
_streams: dict[str, asyncio.Task] = {}


def _forget(sid: str, task: asyncio.Task) -> None:
    if _streams.get(sid) is task:
        del _streams[sid]
    if not task.cancelled() and (exc := task.exception()) is not None:
        if not isinstance(exc, (runner.SessionNotFound, StageGateError)):
            log.error("IDE session %s: run task failed", sid, exc_info=exc)


async def _stream_run(
    db: AsyncSession,
    sid: str,
    principal: str,
    *,
    user_text: str | None,
    feedback: str | None,
):
    session = await _owned(db, sid, principal)
    sid = session.id
    # Release the connection (and SQLite's read lock) before the run writes.
    await db.close()

    queue: asyncio.Queue = asyncio.Queue()

    def emit(event: str, data: dict) -> None:
        queue.put_nowait((event, data))

    # Created here, inside the request: the task copies this context, so
    # current_jwt / current_principal are the caller's for the whole run.
    # Nothing awaits or cancels it on disconnect; it ends with the run.
    task = asyncio.create_task(
        runner.run_stage(sid, principal, user_text, feedback=feedback, emit=emit),
        name=f"ide-stream:{sid}",
    )
    _streams[sid] = task
    task.add_done_callback(lambda t: queue.put_nowait(sse.END))
    task.add_done_callback(lambda t: _forget(sid, t))

    first = await queue.get()
    if first is sse.END:
        # Refused before the first event: answer JSON, not a stream.
        exc = None if task.cancelled() else task.exception()
        if isinstance(exc, runner.SessionNotFound):
            raise HTTPException(status_code=404, detail="Session not found")
        if isinstance(exc, StageGateError):
            return _gate_response(exc)
        raise HTTPException(status_code=500, detail="The run could not be started")
    return StreamingResponse(
        sse.relay(queue, first=first),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/sessions/{sid}/messages")
async def post_message(
    sid: str,
    body: MessageBody,
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    return await _stream_run(db, sid, principal, user_text=body.text, feedback=None)


@router.post("/sessions/{sid}/revise")
async def post_revise(
    sid: str,
    body: ReviseBody,
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    return await _stream_run(db, sid, principal, user_text=None, feedback=body.feedback)


CANCEL_START_WAIT_S = 2.0


async def _cancel_here(sid: str) -> bool:
    """Cancel this process's run of ``sid``; True when one was found."""
    if await runner.cancel(sid):
        return True
    # A stream task that has not registered its run yet (between taking the
    # lock and starting the agent): give it a moment, then cancel the run.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CANCEL_START_WAIT_S
    while (stream := _streams.get(sid)) is not None and not stream.done():
        if await runner.cancel(sid):
            return True
        if loop.time() >= deadline:
            break
        await asyncio.sleep(0.02)
    return False


@router.post("/sessions/{sid}/cancel")
async def cancel_session(
    sid: str, principal: str = Depends(_caller), db: AsyncSession = Depends(_db)
):
    row = await _owned(db, sid, principal)
    sid, running, seen_run = row.id, row.status == "running", row.run_id
    # End the read transaction: the run's cleanup writes this row.
    await db.rollback()
    if running and not await _cancel_here(sid):
        # No task in this process holds the run. The runner frees the lock
        # only when the row is stale (a run lost in a crash or restart), so
        # a live run on another instance keeps it.
        if await runner.release_stale(sid):
            log.warning("IDE session %s: released a stale run lock", sid)
        else:
            now = await db.get(IdeSession, sid, populate_existing=True)
            if now is None:
                raise HTTPException(status_code=404, detail="Session not found")
            if now.status != "running" or now.run_id != seen_run:
                # The run the caller saw ended meanwhile: nothing to cancel.
                return _session_out(now)
            # A live run held by another app instance: this instance cannot
            # reach its task, so nothing was cancelled. Say so.
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "The run is on another app instance and cannot be "
                              "cancelled from here; try again shortly.",
                    "code": "run_on_other_instance",
                },
            )
    row = await db.get(IdeSession, sid, populate_existing=True)
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return _session_out(row)


@router.post("/sessions/{sid}/approve")
async def approve_session(
    sid: str, principal: str = Depends(_caller), db: AsyncSession = Depends(_db)
):
    row = await _owned(db, sid, principal)
    try:
        row = await approve(db, row)
    except StageGateError as exc:
        return _gate_response(exc)
    return _session_out(row)


# --- artifacts --------------------------------------------------------------


@router.get("/sessions/{sid}/artifacts")
async def list_artifacts(
    sid: str,
    kind: str | None = Query(default=None, max_length=16),
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
) -> list[dict[str, Any]]:
    row = await _owned(db, sid, principal)
    return [_artifact_out(a) for a in await store.list_artifacts(db, row.id, kind)]


@router.get("/sessions/{sid}/artifacts/{aid}")
async def get_artifact(
    sid: str,
    aid: str,
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
) -> dict[str, Any]:
    row = await _owned(db, sid, principal)
    art = (
        await db.execute(
            select(IdeArtifact).where(
                IdeArtifact.id == aid, IdeArtifact.session_id == row.id
            )
        )
    ).scalar_one_or_none()
    if art is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return _artifact_out(art)


# --- conventions ------------------------------------------------------------


@router.get("/conventions")
async def list_conventions(
    _: str = Depends(_caller), db: AsyncSession = Depends(_db)
) -> list[dict[str, Any]]:
    return [_conventions_out(c) for c in await store.list_conventions(db)]


@router.get("/conventions/{target}")
async def get_conventions(
    target: str, _: str = Depends(_caller), db: AsyncSession = Depends(_db)
) -> dict[str, Any]:
    row = await store.get_conventions(db, target)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown target")
    return _conventions_out(row)


@router.put("/conventions/{target}")
async def put_conventions(
    body: ConventionsBody,
    target: str = Path(pattern=_TARGET_PATTERN),
    _admin: dict[str, Any] = Depends(require_admin),
    db: AsyncSession = Depends(_db),
) -> dict[str, Any]:
    row = await store.upsert_conventions(db, target, **body.model_dump(exclude_none=True))
    return _conventions_out(row)


# --- admin overview ---------------------------------------------------------


@router.get("/admin/sessions")
async def admin_sessions(
    _admin: dict[str, Any] = Depends(require_admin),
    db: AsyncSession = Depends(_db),
) -> list[dict[str, Any]]:
    # Metadata only: IdeSession.meta() never carries messages, artifacts or sources.
    return await store.list_all_sessions_meta(db)


# --- workspace files over ARC-1 ---------------------------------------------


def _arc1_response(exc: arc1.Arc1Error) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body())


MAX_SOURCE_BYTES = 1024 * 1024


def _too_large(source: str) -> JSONResponse | None:
    if len(source.encode("utf-8", errors="replace")) <= MAX_SOURCE_BYTES:
        return None
    return JSONResponse(
        status_code=413,
        content={
            "detail": f"The object source is larger than {MAX_SOURCE_BYTES} bytes",
            "code": "source_too_large",
        },
    )


def _clean_path(path: str) -> str:
    try:
        return paths.clean_workspace_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _client_for(db: AsyncSession, target: str) -> arc1.Arc1Client:
    conv = await store.get_conventions(db, target)
    if conv is None:
        raise HTTPException(status_code=422, detail=f"Unknown target {target!r}")
    # Looked up on the module at call time: the tests' seam.
    return arc1.get_arc1_client(target, destination=conv.destination or "")


async def _file(db: AsyncSession, sid: str, path: str) -> IdeWorkspaceFile | None:
    return (
        await db.execute(
            select(IdeWorkspaceFile).where(
                IdeWorkspaceFile.session_id == sid, IdeWorkspaceFile.path == path
            )
        )
    ).scalar_one_or_none()


async def _owned_file(
    db: AsyncSession, sid: str, path: str, principal: str
) -> tuple[IdeSession, IdeWorkspaceFile]:
    session = await _owned(db, sid, principal)
    clean = _clean_path(path)
    row = await _file(db, session.id, clean)
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    return session, row


def _refuse_while_running(session: IdeSession) -> JSONResponse | None:
    """Open and refresh change a file's diff base (``origin_source``). While a
    run holds the workspace, its save would then store the old origin it
    loaded as a "modified" proposal that reverts the change in SAP."""
    if session.status == "running":
        return _gate_response(_RUN_IN_PROGRESS)
    return None


def _read_args(type_: str, name: str, include: str | None) -> dict[str, Any]:
    # SAPRead always carries an explicit type: the read-only guard requires it.
    args: dict[str, Any] = {"type": type_, "name": name}
    if include:
        args["include"] = include
    return args


def _lint_of(row: IdeWorkspaceFile) -> list[dict[str, Any]]:
    try:
        data = json.loads(row.lint_json) if row.lint_json else []
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _file_detail(row: IdeWorkspaceFile) -> dict[str, Any]:
    return {
        "path": row.path,
        "state": row.state,
        "origin_source": row.origin_source,
        "proposed_source": row.proposed_source,
        "lint": _lint_of(row),
    }


@router.get("/sessions/{sid}/files")
async def list_files(
    sid: str, principal: str = Depends(_caller), db: AsyncSession = Depends(_db)
) -> list[dict[str, Any]]:
    session = await _owned(db, sid, principal)
    rows = (
        await db.execute(
            select(IdeWorkspaceFile)
            .where(IdeWorkspaceFile.session_id == session.id)
            .order_by(IdeWorkspaceFile.path)
        )
    ).scalars().all()
    return [_file_summary(f) for f in rows]


@router.get("/sessions/{sid}/file")
async def get_file(
    sid: str,
    path: str = Query(default="", max_length=paths.MAX_PATH_LENGTH),
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
) -> dict[str, Any]:
    _, row = await _owned_file(db, sid, path, principal)
    return _file_detail(row)


@router.post("/sessions/{sid}/file/refresh")
async def refresh_file(
    sid: str,
    path: str = Query(default="", max_length=paths.MAX_PATH_LENGTH),
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    session, row = await _owned_file(db, sid, path, principal)
    if (busy := _refuse_while_running(session)) is not None:
        return busy
    obj = paths.object_for(row.path)
    if obj is None:
        raise HTTPException(status_code=422, detail="Not an ABAP object file")
    type_, name, include = obj
    client = await _client_for(db, session.target)
    try:
        source = await client.call("SAPRead", _read_args(type_, name, include))
    except arc1.Arc1Error as exc:
        return _arc1_response(exc)
    if (refused := _too_large(source)) is not None:
        return refused
    row.origin_source = source  # the diff base; the proposal stays as it is
    await store.touch_session(db, session.id)
    await db.commit()
    await db.refresh(row)
    return _file_detail(row)


@router.post("/sessions/{sid}/file/lint")
async def lint_file(
    sid: str,
    path: str = Query(default="", max_length=paths.MAX_PATH_LENGTH),
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    session, row = await _owned_file(db, sid, path, principal)
    source = row.proposed_source or row.origin_source
    if not source:
        raise HTTPException(status_code=422, detail="The file has no source to lint")
    obj = paths.object_for(row.path)
    if obj is None:
        raise HTTPException(status_code=422, detail="Not an ABAP object file")
    name = obj[1]
    client = await _client_for(db, session.target)
    try:
        out = await client.call(
            "SAPLint", {"action": "lint", "source": source, "name": name}
        )
    except arc1.Arc1Error as exc:
        return _arc1_response(exc)
    findings = arc1.parse_findings(out)
    row.lint_json = json.dumps(findings)
    await store.touch_session(db, session.id)
    await db.commit()
    return findings


@router.post("/sessions/{sid}/open")
async def open_object(
    sid: str,
    body: OpenBody,
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    session = await _owned(db, sid, principal)
    if (busy := _refuse_while_running(session)) is not None:
        return busy
    type_, name = body.type.strip().upper(), body.name.strip().upper()
    try:
        path = paths.path_for(type_, name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    client = await _client_for(db, session.target)
    try:
        source = await client.call("SAPRead", _read_args(type_, name, None))
    except arc1.Arc1Error as exc:
        return _arc1_response(exc)
    if (refused := _too_large(source)) is not None:
        return refused
    row = await _file(db, session.id, path)
    if row is None:
        row = IdeWorkspaceFile(
            session_id=session.id,
            path=path,
            object_type=type_,
            object_name=name,
            state="read",
        )
        db.add(row)
    # Re-opening refreshes the diff base; a proposal (modified/new) is kept.
    row.origin_source = source
    await store.touch_session(db, session.id)
    await db.commit()
    await db.refresh(row)
    return _file_summary(row)


@router.get("/objects/search")
async def search_objects(
    target: str = Query(pattern=_TARGET_PATTERN),
    q: str = Query(min_length=1, max_length=100),
    _: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    client = await _client_for(db, target)
    try:
        # Object mode only: searchType/source left to ARC-1's defaults
        # (object, adt), which the read-only policy allows.
        out = await client.call("SAPSearch", {"query": q.strip(), "maxResults": 50})
    except arc1.Arc1Error as exc:
        return _arc1_response(exc)
    return arc1.parse_search(out)
