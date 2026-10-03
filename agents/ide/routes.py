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
- A **diagnose** session (``type`` on ``POST /sessions``) is created only on
  a target whose conventions carry ``non_production``. The flag is read from
  the conventions row on every create, never taken from the request: 422
  ``target_not_non_production`` otherwise. Only an admin can set the flag
  (``PUT /conventions/{target}``); the body takes a strict boolean.
- ``POST .../handover`` starts a change session for the **caller** on the
  **same target** as the owned diagnose session and copies its latest
  ``report`` artifact, nothing else (no messages, files, findings). The
  owner check comes first; then 409 ``not_diagnose`` / ``run_in_progress`` /
  ``missing_artifact``.
  A target that lost its ``non_production`` flag answers 409
  ``target_not_non_production``: the report of a raw run stays where it is.
- Every ``Session`` carries ``masked``: ``diagnose.masking_required`` of the
  target's conventions as they are now, so the UI words its banner from the
  same switch the run uses.
- **A diagnose session whose target lost the flag** (``masked`` is true: the
  flag was taken away, or the conventions row is gone) reads nothing from
  SAP any more. 409 ``target_not_non_production`` (``_refuse_lost_flag``)
  for: message and report runs (``stages.assert_can_run`` and the runner's
  own check, both before the stream opens), the finding detail -- stored
  text and live read alike -- and "open", and file refresh, lint and
  ``POST .../open``. Nothing is sent to ARC-1. Still served: the session,
  its messages, artifacts, files and the finding list (this app's own
  rows), the approval list, a *denial*, and delete. A change session never
  looks at the flag.
- **Findings** (``/sessions/{sid}/findings...``): the owner check comes
  first, then the finding is looked up by id *and* session id, so a finding
  of any other session is 404. The list is metadata only. The detail route
  answers only while the target is ``non_production`` (``masking_required``
  is false *now*; 409 otherwise, see above): the text stored with the
  finding, or -- when none is stored and on ``?refresh=true`` -- the text
  re-read live through ``Arc1Client`` with the diagnose policy and
  ``masking=masking_required(conventions)``, so the client's policy check
  always applies and it fails towards masking. The ARC-1
  arguments are built here from the finding's kind and ``ref_id`` alone. A
  detail read writes nothing. "Open" maps the finding's program/include to
  an object (``paths.resolve_include``) and reads it like ``POST .../open``;
  422 ``no_source`` when there is nothing to map.

- **Approvals** (``/sessions/{sid}/approvals...``): the list is owner-scoped
  like every session route. A decision is taken as the verified caller and
  carries nothing but ``approve`` or ``deny``; ``approvals.decide`` checks
  owner, session type, state, age and the target's flag at that moment and
  is the only caller of ARC-1's ``trace_start``/``trace_cancel``. No run is
  resumed (the agent proposes and its run ends; the next run reads the
  outcome from its instructions), so a decision is possible while a run is
  in progress. ``ApprovalError`` and ``Arc1Error`` answer ``{detail,
  code}`` with their own status.

- Workspace files, open, search and lint call ARC-1 through
  ``agents.ide.arc1`` as the signed-in user; the client checks
  ``readonly.check_call`` before every call. ``?path=`` goes through
  ``paths.clean_workspace_path`` (422) after the owner check, so another
  user's session is 404 whatever the path. ARC-1 failures answer
  ``{"detail", "code"}`` with the client's status (401 no user token, 403
  refused, 424 not configured or no user token, 502 ARC-1/destination
  failure or an ARC-1 error payload, 413 source larger than 1 MB).

Stage runs (``POST .../messages``, ``.../revise``, ``.../report``) stream server-sent
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
import re
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.auth import current_principal, get_validator, require_admin, require_developer
from agents.db import SessionLocal
from agents.ide import approvals, arc1, findings, paths, runner, sse, store
from agents.ide.diagnose import masking_required
from agents.ide.models import (
    IDE_CHILD_MODELS,
    IdeArtifact,
    IdeConventions,
    IdeFinding,
    IdeMessage,
    IdeSession,
    IdeWorkspaceFile,
)
from agents.ide.stages import (
    CHANGE,
    DIAGNOSE,
    REPORT_KIND,
    StageGateError,
    approve,
    initial_stage,
    lost_flag_error,
    request_cap,
)

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
    type: Literal["change", "diagnose"] = "change"

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
    # Strict: "yes" or 1 must not switch a target to raw diagnose data.
    non_production: StrictBool | None = None


class OpenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(pattern=r"^[A-Za-z]{4}$")
    name: str = Field(pattern=r"^\s*[A-Za-z0-9_/$]{1,40}\s*$")


# --- serialisers ------------------------------------------------------------


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _session_json(row: IdeSession, conventions: IdeConventions | None) -> dict[str, Any]:
    out = row.meta()
    out["requests_used"] = row.requests_used or 0
    out["request_cap"] = request_cap()
    out["masked"] = masking_required(conventions)
    return out


async def _session_out(db: AsyncSession, row: IdeSession) -> dict[str, Any]:
    """The ``Session`` JSON; ``masked`` follows the target's conventions as
    they are now (missing conventions read as masked)."""
    return _session_json(row, await store.get_conventions(db, row.target))


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
        "non_production": row.non_production is True,
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
    conventions = await store.list_conventions(db)
    return {
        "principal": principal,
        "is_admin": is_admin,
        "targets": [c.target for c in conventions],
        # The only targets a diagnose session is accepted on (a UI hint;
        # create_session checks the flag itself).
        "diagnose_targets": [
            c.target for c in conventions if c.non_production is True
        ],
    }


# --- sessions ---------------------------------------------------------------


@router.get("/sessions")
async def list_sessions(
    principal: str = Depends(_caller), db: AsyncSession = Depends(_db)
) -> list[dict[str, Any]]:
    rows = await store.list_owned_sessions(db, principal)
    by_target = {c.target: c for c in await store.list_conventions(db)}
    return [_session_json(s, by_target.get(s.target)) for s in rows]


@router.post("/sessions", status_code=201)
async def create_session(
    body: SessionCreate,
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
) -> dict[str, Any]:
    conv = await store.get_conventions(db, body.target)
    if conv is None:
        raise HTTPException(status_code=422, detail=f"Unknown target {body.target!r}")
    # The security control of diagnose sessions: runtime data (dumps, traces)
    # is only read on a target an admin flagged non-production. Read from the
    # conventions row just loaded; the body cannot carry the flag.
    if body.type == DIAGNOSE and conv.non_production is not True:
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Diagnose sessions are only available on targets "
                          "flagged non-production.",
                "code": "target_not_non_production",
            },
        )
    row = await store.create_session(
        db, owner=principal, title=body.title, target=body.target,
        session_type=body.type,
    )
    return _session_json(row, conv)


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
        **await _session_out(db, row),
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
    return await _session_out(db, row)


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
    report: bool = False,
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
        runner.run_stage(
            sid, principal, user_text, feedback=feedback, emit=emit, report=report
        ),
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


@router.post("/sessions/{sid}/report")
async def post_report(
    sid: str, principal: str = Depends(_caller), db: AsyncSession = Depends(_db)
):
    """Run the report turn of a diagnose session (SSE) and store its answer
    as the artifact ``report``. No body: the request text is the server's
    (``stages.REPORT_REQUEST``). The runner's gate answers 409
    ``not_diagnose`` for a change session before the stream opens."""
    return await _stream_run(
        db, sid, principal, user_text=None, feedback=None, report=True
    )


_NOT_DIAGNOSE = StageGateError(
    "not_diagnose", "Only a diagnose session can be handed over."
)
_NO_REPORT = StageGateError(
    "missing_artifact", "Create the report before handing the session over."
)
_HANDOVER_BUSY = StageGateError(
    "run_in_progress", "A run is in progress for this session."
)
_HANDOVER_MASKED = StageGateError(
    "target_not_non_production",
    "The target is no longer flagged non-production; its diagnose report "
    "cannot be handed over.",
)


@router.post("/sessions/{sid}/handover", status_code=201)
async def handover_session(
    sid: str, principal: str = Depends(_caller), db: AsyncSession = Depends(_db)
):
    """Start a change session from an owned diagnose session's report.

    Owner and target are taken from the server's rows -- the caller and the
    diagnose session's target -- never from the request, which has no body.
    Only the latest report is copied: the investigation's messages, findings
    and scratch files stay in the diagnose session (and its 14-day
    retention). Refusals, in order: 409 ``not_diagnose``,
    ``target_not_non_production``, ``run_in_progress``, ``missing_artifact``.
    The three rows are written in one transaction, so a failure
    leaves no change session without its report.
    """
    source = await _owned(db, sid, principal)
    if (source.session_type or CHANGE) != DIAGNOSE:
        return _gate_response(_NOT_DIAGNOSE)
    # The report was written from raw diagnose data while the target was
    # flagged non-production. If the flag is gone (or the conventions are),
    # the target is treated as production now and that text must not be
    # copied into a new session on it.
    conv = await store.get_conventions(db, source.target)
    if masking_required(conv):
        return _gate_response(_HANDOVER_MASKED)
    if source.status == "running":
        # A report run may be about to replace the report being copied.
        return _gate_response(_HANDOVER_BUSY)
    reports = await store.list_artifacts(db, source.id, REPORT_KIND)
    if not reports:
        return _gate_response(_NO_REPORT)
    report = max(reports, key=lambda a: a.version)
    stage = initial_stage(CHANGE).value
    new = IdeSession(
        owner=principal,
        title=f"Change: {source.title}"[:200],
        target=source.target,
        session_type=CHANGE,
        stage=stage,
    )
    db.add(new)
    await db.flush()
    db.add(IdeArtifact(
        session_id=new.id, stage=stage, kind=REPORT_KIND,
        content=report.content, version=1,
    ))
    db.add(IdeMessage(
        session_id=new.id, stage=stage, role="system",
        content=f"Started from diagnose session {source.title!r} "
                f"(report v{report.version}).",
    ))
    await db.commit()
    await db.refresh(new)
    return _session_json(new, conv)


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
                return await _session_out(db, now)
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
    return await _session_out(db, row)


@router.post("/sessions/{sid}/approve")
async def approve_session(
    sid: str, principal: str = Depends(_caller), db: AsyncSession = Depends(_db)
):
    row = await _owned(db, sid, principal)
    try:
        row = await approve(db, row)
    except StageGateError as exc:
        return _gate_response(exc)
    return await _session_out(db, row)


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


async def _client_for(
    db: AsyncSession, target: str, policy: str = "change"
) -> arc1.Arc1Client:
    """The ARC-1 client for a call on ``target``; ``policy`` is the type of
    the session the call belongs to. Results of a diagnose session are
    masked unless the target's conventions flag it ``non_production``."""
    conv = await store.get_conventions(db, target)
    if conv is None:
        raise HTTPException(status_code=422, detail=f"Unknown target {target!r}")
    # Looked up on the module at call time: the tests' seam.
    return arc1.get_arc1_client(
        target,
        destination=conv.destination or "",
        policy=policy,
        masking=masking_required(conv),
    )


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


async def _refuse_lost_flag(
    db: AsyncSession, session: IdeSession
) -> JSONResponse | None:
    """409 ``target_not_non_production`` for a diagnose session whose target
    is not flagged ``non_production`` *now*, else ``None``.

    The flag is enforced when the session is created, but an admin can take
    it away afterwards (or the conventions row can go): the target is
    production from then on, and a session opened for runtime data must not
    keep reading from it -- not even masked. Called by every route that
    would call ARC-1 for the session, after the owner check and before
    anything is sent. A change session is never refused here.
    """
    if (session.session_type or CHANGE) != DIAGNOSE:
        return None
    conv = await store.get_conventions(db, session.target)
    if masking_required(conv):
        return _gate_response(lost_flag_error())
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
    if (lost := await _refuse_lost_flag(db, session)) is not None:
        return lost
    if (busy := _refuse_while_running(session)) is not None:
        return busy
    obj = paths.object_for(row.path)
    if obj is None:
        raise HTTPException(status_code=422, detail="Not an ABAP object file")
    type_, name, include = obj
    client = await _client_for(db, session.target, session.session_type)
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
    if (lost := await _refuse_lost_flag(db, session)) is not None:
        return lost
    source = row.proposed_source or row.origin_source
    if not source:
        raise HTTPException(status_code=422, detail="The file has no source to lint")
    obj = paths.object_for(row.path)
    if obj is None:
        raise HTTPException(status_code=422, detail="Not an ABAP object file")
    name = obj[1]
    client = await _client_for(db, session.target, session.session_type)
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


async def _open_into_workspace(
    db: AsyncSession,
    session: IdeSession,
    type_: str,
    name: str,
    include: str | None = None,
) -> IdeWorkspaceFile | JSONResponse:
    """Read an object from SAP into the session's workspace as a ``read``
    file; shared by ``POST .../open`` and a finding's "open source".

    ``include`` is a class-local section (``paths.path_for``). The caller
    has checked the owner and that no run holds the workspace. Returns the
    file row, or the error response of a refused or failed read -- in which
    case nothing was stored.
    """
    try:
        path = paths.path_for(type_, name, include)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    client = await _client_for(db, session.target, session.session_type)
    try:
        source = await client.call("SAPRead", _read_args(type_, name, include))
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
    return row


@router.post("/sessions/{sid}/open")
async def open_object(
    sid: str,
    body: OpenBody,
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    session = await _owned(db, sid, principal)
    if (lost := await _refuse_lost_flag(db, session)) is not None:
        return lost
    if (busy := _refuse_while_running(session)) is not None:
        return busy
    type_, name = body.type.strip().upper(), body.name.strip().upper()
    opened = await _open_into_workspace(db, session, type_, name)
    if isinstance(opened, JSONResponse):
        return opened
    return _file_summary(opened)


# --- findings (diagnose) ----------------------------------------------------

_FINDINGS_NOT_DIAGNOSE = StageGateError(
    "not_diagnose", "Only a diagnose session has finding details."
)

# What a stored ``ref_id`` must look like before it goes back to ARC-1 as an
# argument. The reference came out of a tool result; it is checked again
# here rather than trusted because it was stored.
_REF_ID = re.compile(r"[A-Za-z0-9_/$=.:<>~\-]{1,255}")
_GATEWAY_PATH = re.compile(r"/sap/bc/adt/gw/errorlog/[A-Za-z0-9_/.\-~$=:]{1,200}")
_GATEWAY_TYPE = re.compile(r"[A-Za-z0-9_.\-]+(?: [A-Za-z0-9_.\-]+){0,5}")
_GATEWAY_ID = re.compile(r"[A-Za-z0-9_.\-]{1,100}")


def _detail_args(kind: str, ref_id: str) -> dict[str, Any] | None:
    """The ``SAPDiagnose`` arguments that re-read one finding, or ``None``
    when its kind has no detail read (authorization checks, OData calls) or
    its reference is not one this route sends.

    Built from the kind and the reference only -- never a ``user`` filter,
    never anything from the request.
    """
    # An id may hold ``/`` and ``.`` (ARC-1 puts it into an ADT path), so a
    # traversal or a second host is refused here as for gateway paths.
    # ``%`` and ``?`` are checked by name as well as by the patterns: a
    # percent-encoded traversal or a smuggled query must stay refused if a
    # pattern is ever widened.
    if "%" in ref_id or "?" in ref_id:
        return None
    plain = _REF_ID.fullmatch(ref_id) and ".." not in ref_id and "//" not in ref_id
    if kind == "dump" and plain:
        return {"action": "dumps", "id": ref_id}
    if kind == "trace" and plain:
        return {"action": "traces", "id": ref_id, "analysis": "hitlist"}
    if kind == "gateway_error":
        if ref_id.startswith("/"):
            # The detail link's path (stored without its query). Only the
            # ADT error log, no ``..`` and no second host.
            if (
                _GATEWAY_PATH.fullmatch(ref_id)
                and ".." not in ref_id
                and "//" not in ref_id
            ):
                return {"action": "gateway_errors", "detailUrl": ref_id}
            return None
        error_type, sep, ident = ref_id.partition(":")
        if sep and _GATEWAY_TYPE.fullmatch(error_type) and _GATEWAY_ID.fullmatch(ident):
            return {"action": "gateway_errors", "id": ident, "errorType": error_type}
    return None


def _detail_text(args: dict[str, Any], text: str) -> str:
    """A live detail result as the text the dialog shows: the same text a
    run would have stored for it (``findings.extract``), else the JSON
    indented, else the result as it is. ``text`` is what the client
    returned, so in a masked session it is already masked."""
    for item in findings.extract("SAPDiagnose", args, text, with_detail=True):
        detail = item.get("detail")
        if isinstance(detail, str) and detail:
            return detail
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return text[: findings.MAX_DETAIL]
    if isinstance(data, (dict, list)):
        text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    return text[: findings.MAX_DETAIL]


async def _owned_finding(
    db: AsyncSession, sid: str, fid: str, principal: str
) -> tuple[IdeSession, IdeFinding]:
    """The owner check first, then the finding inside that session only."""
    session = await _owned(db, sid, principal)
    row = await store.get_finding(db, session.id, fid)
    if row is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    return session, row


@router.get("/sessions/{sid}/findings")
async def list_findings(
    sid: str, principal: str = Depends(_caller), db: AsyncSession = Depends(_db)
) -> list[dict[str, Any]]:
    """Newest first; metadata only, never the detail text."""
    session = await _owned(db, sid, principal)
    return [findings.finding_out(f) for f in await store.list_findings(db, session.id)]


@router.get("/sessions/{sid}/findings/{fid}")
async def get_finding(
    sid: str,
    fid: str = Path(max_length=64),
    refresh: bool = Query(default=False),
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    """``{finding, detail}``: the text kept with the finding, or -- on
    ``refresh`` and when none is kept -- the text re-read from SAP as the
    signed-in user.

    The stored text is raw (written by a run on a ``non_production``
    target). It is served, and SAP is read, only while ``masking_required``
    is false for the target's conventions as they are at this moment; a
    target that lost its flag answers 409 ``target_not_non_production``
    whether or not text is stored. Nothing is written.
    """
    session, row = await _owned_finding(db, sid, fid, principal)
    if (session.session_type or CHANGE) != DIAGNOSE:
        return _gate_response(_FINDINGS_NOT_DIAGNOSE)
    if (lost := await _refuse_lost_flag(db, session)) is not None:
        return lost
    out = findings.finding_out(row)
    if not refresh and row.detail:
        return {"finding": out, "detail": row.detail}
    args = _detail_args(row.kind, row.ref_id)
    if args is None:
        return JSONResponse(
            status_code=422,
            content={
                "detail": "This finding has no detail that can be read from SAP.",
                "code": "no_detail",
            },
        )
    # Always the diagnose policy (the session is a diagnose session). The
    # client is built from the conventions as it reads them itself, so a
    # flag taken away since the check above still means a masked result.
    client = await _client_for(db, session.target, DIAGNOSE)
    # Release the connection before the network round trip. ``db`` must not
    # be used after this line: everything needed from it was read above.
    await db.close()
    try:
        text = await client.call("SAPDiagnose", args)
    except arc1.Arc1Error as exc:
        return _arc1_response(exc)
    return {"finding": out, "detail": _detail_text(args, text)}


def _line_hint(include: str | None, line: int | None) -> str | None:
    """Where the error is, for a line that does not count in the opened
    source: ``method include CM001, line 12``."""
    part = paths.class_include(include)
    if part is None:
        return None
    suffix = part[1]
    label = "method include" if suffix.startswith("CM") else "class include"
    return f"{label} {suffix}" + (f", line {line}" if line is not None else "")


@router.post("/sessions/{sid}/findings/{fid}/open")
async def open_finding(
    sid: str,
    fid: str = Path(max_length=64),
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    """Read the source a finding points at into the workspace.

    ``{file, line, hint}``: ``line`` is the finding's line when it counts
    in the opened source; otherwise ``null`` with a ``hint`` naming the
    include (a class method include is opened as the whole class).
    """
    session, row = await _owned_finding(db, sid, fid, principal)
    if (lost := await _refuse_lost_flag(db, session)) is not None:
        return lost
    if (busy := _refuse_while_running(session)) is not None:
        return busy
    resolved = paths.resolve_include(row.program, row.include)
    if resolved is None:
        return JSONResponse(
            status_code=422,
            content={
                "detail": "The finding has no source that can be opened.",
                "code": "no_source",
            },
        )
    type_, name, section, exact = resolved
    # A dump can name the method include as its program and no include.
    line, include = row.line, row.include or row.program
    opened = await _open_into_workspace(db, session, type_, name, section)
    if isinstance(opened, JSONResponse):
        return opened
    return {
        "file": _file_summary(opened),
        "line": line if exact else None,
        "hint": None if exact else _line_hint(include, line),
    }


# --- trace approvals (diagnose) -----------------------------------------------


class ApprovalDecision(BaseModel):
    """The whole body: a decision. Parameters are never taken from the
    client; what is armed is what the stored proposal says."""

    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["approve", "deny"]


@router.get("/sessions/{sid}/approvals")
async def list_approvals(
    sid: str, principal: str = Depends(_caller), db: AsyncSession = Depends(_db)
) -> list[dict[str, Any]]:
    """The session's trace proposals and what became of them, newest first.

    Both conditional updates below touch this (owned) session only. A
    pending approval older than ``store.approval_ttl_min()`` becomes
    ``expired``. An approval of this session that was approved but never got an outcome
    (the process died during the ARC-1 call) is closed first, once it is
    older than the arm timeout: the list then says ``failed`` /
    ``interrupted`` instead of an ``approved`` that nobody will finish.
    """
    session = await _owned(db, sid, principal)
    try:
        # Likewise a proposal nobody decided in time: listed as ``expired``,
        # which is what a decision on it would answer (410).
        await store.expire_pending_approvals(
            db, older_than_min=store.approval_ttl_min(), session_id=session.id
        )
        await approvals.sweep_interrupted(db, session.id)
    except Exception:  # noqa: BLE001 -- the list is still worth returning
        log.warning("IDE session %s: closing overdue approvals failed",
                    session.id, exc_info=True)
        await db.rollback()
    return [
        approvals.approval_json(a) for a in await store.list_approvals(db, session.id)
    ]


@router.post("/sessions/{sid}/approvals/{aid}")
async def decide_approval(
    body: ApprovalDecision,
    sid: str,
    aid: str = Path(max_length=64),
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    """Approve or deny one pending trace proposal as the signed-in developer.

    The only place a trace is armed or cancelled. ``principal`` is the
    verified caller (``require_developer`` and the token the middleware
    validated), never anything from the body. ``approvals.decide`` does the
    checks -- owner (404, as for a forged approval id), session type,
    pending and not expired (``store.approval_ttl_min()``), the target's
    ``non_production`` flag as it is now -- and calls ARC-1 at most once.

    Answers the ``Approval`` as it is afterwards: ``denied``; ``approved``
    with a ``result``; or ``failed`` with an ``error_code`` (for example
    ``arc1_timeout_unknown``, with a ``result.note``, or
    ``audit_unavailable``). Refusals are ``{detail, code}``: 404
    ``not_found``, 409 ``not_diagnose`` / ``approval_not_pending`` /
    ``unknown_trace_request``, 403 ``target_not_non_production``, 410
    ``approval_expired``, 424 ``user_token_required`` /
    ``arc1_not_configured`` (nothing was decided; sign in and retry).
    """
    try:
        row = await approvals.decide(
            db, sid=sid, aid=aid, principal=principal, decision=body.decision
        )
    except approvals.ApprovalError as exc:
        return JSONResponse(status_code=exc.status, content=exc.body())
    except arc1.Arc1Error as exc:
        return _arc1_response(exc)
    return approvals.approval_json(row)


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
