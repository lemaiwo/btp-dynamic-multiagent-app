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
- Every JSON route declares a ``response_model`` from ``agents.ide.schemas``
  (the contract the UI is tested against; a field a serialiser adds by
  mistake is filtered out, one it drops fails). Refusals are returned as
  ``JSONResponse`` and pass through unchanged as ``{detail, code}``. The
  message list carries ``has_activity`` instead of the activity itself.
- ``StageGateError`` maps to 409 ``{"detail", "code"}`` (429 for
  ``usage_exhausted``); unknown codes keep the server message.
- A **diagnose** session (``type`` on ``POST /sessions``) is created only on
  a target whose conventions carry ``non_production``. The flag is read from
  the conventions row on every create, never taken from the request: 422
  ``target_not_non_production`` otherwise. Only an admin can set the flag
  (``POST /conventions``, ``PUT /conventions/{target}``); the body takes a
  strict boolean.
- **Conventions writes** are admin-only (``require_admin`` on top of the
  router's ``require_developer``); a developer reads them. ``POST
  /conventions`` creates a target (201; 409 ``target_exists``; 422 for a
  name outside ``schemas.TARGET_PATTERN``, the one target rule).
  ``PUT /conventions/{target}`` never creates (404 ``unknown_target``,
  decision D4) and takes ``clear`` for text fields; a field both set and
  cleared is 422. ``non_production`` cannot be cleared, only set true or
  false; taking it away deletes nothing (the diagnose sessions on the
  target then refuse their runs, see below). Setting or changing the flag
  is audited in the same transaction (``IdeAuditLog`` action
  ``conventions_flag`` plus a line on the ``agents.ide.audit`` logger,
  old and new value only); so is setting, changing or clearing the
  ``destination`` of a target flagged before or after the write
  (``conventions_destination``: it names the server raw runtime data is
  read from).
- A refused request body is 422 ``{"detail": [{loc, msg, type}]}`` without
  the client's ``input`` (``install_validation_handler``).
- ``waiting``, ``open_comments`` and ``unresolved_comments`` on every
  ``Session`` come from ``store.waiting_for`` / ``store.count_open_comments``
  over the caller's own sessions: a fixed number of grouped queries for the
  list, never one per session.
- ``POST .../handover`` starts a change session for the **caller** on the
  **same target** as the owned diagnose session and copies its latest
  ``report`` artifact, nothing else (no messages, files, findings). The
  owner check comes first; then 409 ``not_diagnose`` / ``run_in_progress`` /
  ``missing_artifact``.
  A target that lost its ``non_production`` flag answers 409
  ``target_not_non_production``: the report of a raw run stays where it is.
- Every ``Session`` carries ``target_non_production``:
  ``diagnose.is_non_production`` of the target's conventions as they are
  now, so the UI words its banner from the same switch the run uses.
- **A diagnose session whose target lost the flag**
  (``target_non_production`` is false: the flag was taken away, or the
  conventions row is gone) reads nothing from SAP any more. 409
  ``target_not_non_production`` (``_refuse_lost_flag``, and again in
  ``_client_for`` from its own read of the conventions)
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
  answers only while the target is ``non_production`` (``is_non_production``
  is true *now*; 409 otherwise, see above): the text stored with the
  finding, or -- when none is stored and on ``?refresh=true`` -- the text
  re-read live through ``Arc1Client`` with the diagnose policy, so the
  client's policy check always applies. The ARC-1
  arguments are built here from the finding's kind and ``ref_id`` alone. A
  detail read writes nothing. "Open" maps the finding's program/include to
  an object (``paths.resolve_include``) and reads it like ``POST .../open``;
  422 ``no_source`` when there is nothing to map (no program; nothing is
  read). The same code with 502 means SAP answered a read with no source
  (``_no_source``, open/refresh and this route alike): the status tells
  the two apart, and the UI maps the 422 for findings.

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

Stage runs (``POST .../messages``, ``.../request-changes`` (in
``review_routes``), ``.../report``) stream server-sent
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
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import (
    BaseModel,
    ValidationError,
)
from sqlalchemy import delete, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from agents.auth import current_principal, get_validator, require_admin, require_developer
from agents.db import SessionLocal
from agents.ide import (
    approvals,
    arc1,
    basecheck,
    findings,
    paths,
    runner,
    seed,
    sse,
    store,
    syntaxcheck,
)
from agents.ide.diagnose import is_non_production
from agents.ide.models import (
    IDE_CHILD_MODELS,
    IdeArtifact,
    IdeConventions,
    IdeFileRevision,
    IdeFinding,
    IdeMessage,
    IdeSession,
    IdeWorkspaceFile,
    iso_utc,
)
from agents.ide.schemas import (
    SSE_RESPONSES,
    TARGET_PATTERN,
    AdminSessionRowOut,
    ApprovalDecision,
    ApprovalOut,
    ApproveBody,
    ArtifactOut,
    ArtifactSummaryOut,
    ConventionsCreate,
    ConventionsOut,
    ConventionsUpdate,
    DocumentKind,
    FileDetailOut,
    FileState,
    FileSummaryOut,
    FindingDetailOut,
    FindingOpenOut,
    FindingOut,
    LintFindingOut,
    MeOut,
    MessageBody,
    MessageOut,
    MessageRole,
    ObjectHitOut,
    OpenBody,
    PinsOut,
    SeedRefreshOut,
    SessionCreate,
    SessionDetailOut,
    SessionOut,
    SessionPatch,
    SessionStatus,
    SessionType,
    StageName,
    SyntaxItemOut,
    SyntaxResultOut,
    coerce_member,
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


# Request models live in ``agents.ide.schemas`` (the API contract).


# --- serialisers ------------------------------------------------------------


def _iso(value) -> str | None:
    return iso_utc(value)


def _meta(row: IdeSession) -> dict[str, Any]:
    """``row.meta()`` with its enum columns coerced to contract members."""
    meta = row.meta()
    meta["type"] = coerce_member(meta["type"], SessionType, CHANGE, "session type")
    meta["stage"] = coerce_member(meta["stage"], StageName, "done", "session stage")
    meta["status"] = coerce_member(meta["status"], SessionStatus, "idle", "session status")
    return meta


def _session_json(
    row: IdeSession,
    conventions: IdeConventions | None,
    waiting: str | None = None,
    comments: tuple[int, int] = (0, 0),
    objects: tuple[list[str], int, int] = ([], 0, 0),
    findings_count: int = 0,
) -> SessionOut:
    """``waiting``, ``comments`` (open, open + sent), ``objects`` (names
    shown, total, changed) and ``findings_count`` come from the store's
    batched helpers; the defaults fit a session that was just created."""
    shown, total, changed = objects
    return SessionOut(
        **_meta(row),
        requests_used=row.requests_used or 0,
        request_cap=request_cap(),
        target_non_production=is_non_production(conventions),
        pins=PinsOut.model_validate(store.pins_of(row)),
        waiting=waiting,
        open_comments=comments[0],
        unresolved_comments=comments[1],
        objects=list(shown),
        objects_total=total,
        changed_objects=changed,
        findings_count=(
            findings_count if (row.session_type or CHANGE) == DIAGNOSE else None
        ),
    )


async def _sessions_out(
    db: AsyncSession,
    rows: list[IdeSession],
    conventions: dict[str, IdeConventions | None],
) -> list[SessionOut]:
    """``Session`` JSON for several sessions of one owner: the worklist
    fields come from a fixed number of grouped queries, not one per row."""
    sids = [r.id for r in rows]
    waiting = await store.waiting_for(db, rows)
    counts = await store.count_open_comments(db, sids)
    objects = await store.object_summaries(db, sids)
    diagnose = [r.id for r in rows if (r.session_type or CHANGE) == DIAGNOSE]
    findings_counts = await store.count_findings(db, diagnose) if diagnose else {}
    return [
        _session_json(r, conventions.get(r.target), waiting.get(r.id),
                      counts.get(r.id, (0, 0)), objects.get(r.id, ([], 0, 0)),
                      findings_counts.get(r.id, 0))
        for r in rows
    ]


async def _session_out(db: AsyncSession, row: IdeSession) -> SessionOut:
    """The ``Session`` JSON; ``target_non_production`` follows the target's
    conventions as they are now (missing conventions read as production)."""
    conv = await store.get_conventions(db, row.target)
    return (await _sessions_out(db, [row], {row.target: conv}))[0]


def _loads(raw: str | None) -> Any:
    """Stored JSON, or ``None`` when absent or unreadable: a damaged column
    must read as "nothing there", not answer 500."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return None


def _based_on(raw: str | None) -> dict[str, int] | None:
    data = _loads(raw)
    if not isinstance(data, dict):
        return None
    clean = {
        k: v for k, v in data.items()
        if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
    }
    return clean or None


def _artifact_summary(row: IdeArtifact) -> ArtifactSummaryOut:
    return ArtifactSummaryOut(
        id=row.id,
        stage=coerce_member(row.stage, StageName, "done", "artifact stage"),
        kind=coerce_member(row.kind, DocumentKind, "note", "artifact kind"),
        version=row.version,
        created_at=_iso(row.created_at),
        based_on=_based_on(row.based_on_json),
    )


def _artifact_out(row: IdeArtifact) -> ArtifactOut:
    return ArtifactOut(**_artifact_summary(row).model_dump(), content=row.content)


def _message_out(row: IdeMessage) -> MessageOut:
    # The activity body is served on its own (``.../messages/{mid}/activity``):
    # a list of every message with every tool call in it grows without bound.
    return MessageOut(
        id=row.id,
        stage=coerce_member(row.stage, StageName, "done", "message stage"),
        role=coerce_member(row.role, MessageRole, "system", "message role"),
        content=row.content,
        created_at=_iso(row.created_at),
        has_activity=isinstance(_loads(row.activity_json), dict),
    )


def _one_of(value: Any, allowed: tuple[str, ...]) -> str | None:
    """A stored status if it is one the contract knows, else ``None``."""
    return value if isinstance(value, str) and value in allowed else None


_BASE_STATUSES = ("sap", "absent", "unknown")


def _file_summary(row: IdeWorkspaceFile, syntax_status: str | None = None) -> FileSummaryOut:
    """``syntax_status`` is the one of the file's latest revision (the
    caller looks it up; ``None`` = not checked or no revision yet)."""
    return FileSummaryOut(
        path=row.path,
        state=coerce_member(row.state, FileState, "read", "file state"),
        object_type=row.object_type,
        object_name=row.object_name,
        revision=row.revision or 0,
        base_status=_one_of(row.base_status, _BASE_STATUSES),
        syntax_status=_one_of(syntax_status, store.SYNTAX_STATUSES),
    )


async def _syntax_statuses(
    db: AsyncSession, sid: str, rows: list[IdeWorkspaceFile]
) -> dict[str, str | None]:
    """``path -> syntax_status`` of each file's latest revision, in one
    query per 500 paths (no source text is loaded)."""
    latest = {r.path: r.revision for r in rows if r.revision}
    out: dict[str, str | None] = {}
    pairs = list(latest.items())
    for i in range(0, len(pairs), 500):
        # Only the (path, latest revision) rows: a file with many revisions
        # must not load all of them to keep one.
        result = await db.execute(
            select(IdeFileRevision.path, IdeFileRevision.syntax_status).where(
                IdeFileRevision.session_id == sid,
                tuple_(IdeFileRevision.path, IdeFileRevision.revision).in_(
                    pairs[i : i + 500]
                ),
            )
        )
        for path, syntax_status in result.all():
            out[path] = syntax_status
    return out


async def _file_summaries(
    db: AsyncSession, sid: str, rows: list[IdeWorkspaceFile]
) -> list[FileSummaryOut]:
    statuses = await _syntax_statuses(db, sid, rows)
    return [_file_summary(r, statuses.get(r.path)) for r in rows]


async def _file_summary_one(db: AsyncSession, row: IdeWorkspaceFile) -> FileSummaryOut:
    return (await _file_summaries(db, row.session_id, [row]))[0]


def _conventions_out(row: IdeConventions) -> ConventionsOut:
    return ConventionsOut(
        target=row.target,
        label=row.label,
        destination=row.destination,
        namespace=row.namespace,
        package=row.package,
        atc_variant=row.atc_variant,
        clean_core_level=row.clean_core_level,
        free_text=row.free_text,
        non_production=row.non_production is True,
        updated_at=_iso(row.updated_at),
    )


# --- me ---------------------------------------------------------------------


@router.get("/me", response_model=MeOut)
async def me(
    claims: dict[str, Any] = Depends(require_developer),
    db: AsyncSession = Depends(_db),
):
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
        # Read from the store at request time: the value the purge uses.
        "diagnose_retention_days": store.IDE_DIAGNOSE_RETENTION_DAYS,
    }


# --- sessions ---------------------------------------------------------------


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(
    principal: str = Depends(_caller), db: AsyncSession = Depends(_db)
):
    rows = await store.list_owned_sessions(db, principal)
    by_target = {c.target: c for c in await store.list_conventions(db)}
    return await _sessions_out(db, rows, by_target)


@router.post("/sessions", status_code=201, response_model=SessionOut)
async def create_session(
    body: SessionCreate,
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
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


@router.get("/sessions/{sid}", response_model=SessionDetailOut)
async def get_session(
    sid: str, principal: str = Depends(_caller), db: AsyncSession = Depends(_db)
):
    row = await _owned(db, sid, principal)
    artifacts = await store.list_artifacts(db, row.id)
    files = (
        await db.execute(
            select(IdeWorkspaceFile)
            .where(IdeWorkspaceFile.session_id == row.id)
            .order_by(IdeWorkspaceFile.path)
        )
    ).scalars().all()
    return SessionDetailOut(
        **(await _session_out(db, row)).model_dump(),
        artifacts=[_artifact_summary(a) for a in artifacts],
        files=await _file_summaries(db, row.id, list(files)),
    )


@router.patch("/sessions/{sid}", response_model=SessionOut)
async def patch_session(
    sid: str,
    body: SessionPatch,
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
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


@router.get("/sessions/{sid}/messages", response_model=list[MessageOut])
async def list_messages(
    sid: str, principal: str = Depends(_caller), db: AsyncSession = Depends(_db)
):
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
    request_changes: runner.RequestChanges | None = None,
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
            sid, principal, user_text, emit=emit,
            request_changes=request_changes, report=report,
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


@router.post("/sessions/{sid}/messages", response_model=None, responses=SSE_RESPONSES)
async def post_message(
    sid: str,
    body: MessageBody,
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    return await _stream_run(db, sid, principal, user_text=body.text)


@router.post("/sessions/{sid}/report", response_model=None, responses=SSE_RESPONSES)
async def post_report(
    sid: str, principal: str = Depends(_caller), db: AsyncSession = Depends(_db)
):
    """Run the report turn of a diagnose session (SSE) and store its answer
    as the artifact ``report``. No body: the request text is the server's
    (``stages.REPORT_REQUEST``). The runner's gate answers 409
    ``not_diagnose`` for a change session before the stream opens."""
    return await _stream_run(db, sid, principal, user_text=None, report=True)


_NOT_DIAGNOSE = StageGateError(
    "not_diagnose", "Only a diagnose session can be handed over."
)
_NO_REPORT = StageGateError(
    "missing_artifact", "Create the report before handing the session over."
)
_HANDOVER_BUSY = StageGateError(
    "run_in_progress", "A run is in progress for this session."
)
_HANDOVER_LOST_FLAG = StageGateError(
    "target_not_non_production",
    "The target is no longer flagged non-production; its diagnose report "
    "cannot be handed over.",
)


@router.post(
    "/sessions/{sid}/handover", status_code=201, response_model=SessionOut
)
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
    if not is_non_production(conv):
        return _gate_response(_HANDOVER_LOST_FLAG)
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


@router.post("/sessions/{sid}/cancel", response_model=SessionOut)
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


@router.post("/sessions/{sid}/approve", response_model=SessionOut)
async def approve_session(
    sid: str,
    body: ApproveBody | None = None,
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    row = await _owned(db, sid, principal)
    try:
        row = await approve(
            db, row,
            version=body.version if body else None,
            revisions=body.revisions if body else None,
        )
    except StageGateError as exc:
        return _gate_response(exc)
    except store.PinConflict:
        return JSONResponse(
            status_code=409,
            content={"detail": "The session changed in the meantime; reload and "
                               "try again.", "code": "pin_conflict"},
        )
    return await _session_out(db, row)


# --- artifacts --------------------------------------------------------------


@router.get("/sessions/{sid}/artifacts", response_model=list[ArtifactOut])
async def list_artifacts(
    sid: str,
    kind: str | None = Query(default=None, max_length=16),
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    row = await _owned(db, sid, principal)
    return [_artifact_out(a) for a in await store.list_artifacts(db, row.id, kind)]


@router.get("/sessions/{sid}/artifacts/{aid}", response_model=ArtifactOut)
async def get_artifact(
    sid: str,
    aid: str,
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
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


@router.get("/conventions", response_model=list[ConventionsOut])
async def list_conventions(
    _: str = Depends(_caller), db: AsyncSession = Depends(_db)
):
    return [_conventions_out(c) for c in await store.list_conventions(db)]


@router.get("/conventions/{target}", response_model=ConventionsOut)
async def get_conventions(
    target: str, _: str = Depends(_caller), db: AsyncSession = Depends(_db)
):
    row = await store.get_conventions(db, target)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown target")
    return _conventions_out(row)


@router.post("/conventions", status_code=201, response_model=ConventionsOut)
async def create_conventions(
    body: ConventionsCreate,
    _admin: dict[str, Any] = Depends(require_admin),
    db: AsyncSession = Depends(_db),
):
    fields = body.model_dump(exclude_none=True, exclude={"target"})
    try:
        row = await store.create_conventions(
            db, body.target, actor=_principal(_admin), **fields
        )
    except store.ConventionsError as exc:
        return JSONResponse(
            status_code=409,
            content={"detail": "This target already exists.", "code": exc.code},
        )
    return _conventions_out(row)


@router.put("/conventions/{target}", response_model=ConventionsOut)
async def put_conventions(
    body: ConventionsUpdate,
    target: str = Path(pattern=TARGET_PATTERN),
    _admin: dict[str, Any] = Depends(require_admin),
    db: AsyncSession = Depends(_db),
):
    # Never creates (decision D4): a typo in the path must not add a target.
    row = await store.update_conventions(
        db, target, body.model_dump(exclude_none=True, exclude={"clear"}), body.clear,
        actor=_principal(_admin),
    )
    if row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "Unknown target", "code": "unknown_target"},
        )
    return _conventions_out(row)


# --- admin overview ---------------------------------------------------------


@router.get("/admin/sessions", response_model=list[AdminSessionRowOut])
async def admin_sessions(
    _admin: dict[str, Any] = Depends(require_admin),
    db: AsyncSession = Depends(_db),
):
    # Metadata only: IdeSession.meta() never carries messages, artifacts or sources.
    rows = (
        await db.execute(
            select(IdeSession).order_by(
                IdeSession.updated_at.desc(), IdeSession.created_at.desc()
            )
        )
    ).scalars().all()
    return [_meta(r) for r in rows]


@router.post("/admin/seed/refresh", response_model=SeedRefreshOut)
async def refresh_seed(_admin: dict[str, Any] = Depends(require_admin)):
    """Bring the IDE agents and skills up to the shipped seed (decision D7).

    Admin only, and a POST like every state-changing IDE route, so the
    approuter's CSRF check covers it. Rows still holding an earlier seed
    text are updated, admin-edited rows are reported and kept, missing ones
    are added (``agents.ide.seed.refresh_ide_seed``). When anything changed
    the registry and the chat app are rebuilt, as ``POST /admin/api/reload``
    does, so the next run uses the new prompts; a run in flight keeps the
    build it started with. The seed rows are committed before that rebuild:
    when the rebuild fails the change is still in the database, so the
    answer is the normal result with ``reload_failed: true`` (the error goes
    to the log only) rather than a 500 that would invite a pointless retry;
    the next ``POST /admin/api/reload`` or restart picks the rows up.
    """
    actor = _principal(_admin)
    try:
        # Looked up on the module at call time: the tests' seam.
        result = await seed.refresh_ide_seed(seed.IDE_SEED_FILE)
    except ValueError as exc:
        log.error("IDE seed refresh by %s failed: %s", actor, exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "The assistant seed file could not be read.",
                     "code": "seed_unreadable"},
        )
    log.info(
        "IDE seed refresh by %s: updated=%s skipped_edited=%s added=%s",
        actor, result["updated"], result["skipped_edited"], result["added"],
    )
    reload_failed = False
    if result["updated"] or result["added"]:
        # Imported here: the registry builds the IDE agents and imports
        # this package.
        from agents.chat_app import dynamic_chat_app
        from agents.registry import registry

        try:
            await registry.reload()
            dynamic_chat_app.refresh()
        except Exception:
            log.exception("IDE seed refresh by %s: rows saved, reload failed", actor)
            reload_failed = True
    return SeedRefreshOut(**result, reload_failed=reload_failed)


# --- workspace files over ARC-1 ---------------------------------------------


def _arc1_response(exc: arc1.Arc1Error) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body())


MAX_SOURCE_BYTES = 1024 * 1024


def _no_source(source: Any) -> JSONResponse | None:
    """502 ``no_source`` for a successful read that is no source (empty, or
    a one-line not-found text): stored as the base it would turn every
    proposal into a rewrite (``basecheck.usable_source``)."""
    if basecheck.usable_source(source):
        return None
    return JSONResponse(
        status_code=502,
        content={"detail": "ARC-1 returned no source for this object",
                 "code": "no_source"},
    )


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
) -> arc1.Arc1Client | JSONResponse:
    """The ARC-1 client for a call on ``target``; ``policy`` is the type of
    the session the call belongs to.

    A diagnose client is built only while the target's conventions -- as
    read here, not as the caller read them -- flag it ``non_production``;
    otherwise the 409 ``target_not_non_production`` response is returned
    instead, which the caller hands back. Defence in depth behind
    :func:`_refuse_lost_flag`: a flag taken away in between must not mean a
    read of runtime data from a production target.
    """
    conv = await store.get_conventions(db, target)
    if policy == DIAGNOSE and not is_non_production(conv):
        return _gate_response(lost_flag_error())
    if conv is None:
        raise HTTPException(status_code=422, detail=f"Unknown target {target!r}")
    # Looked up on the module at call time: the tests' seam.
    return arc1.get_arc1_client(
        target,
        destination=conv.destination or "",
        policy=policy,
    )


async def _file(db: AsyncSession, sid: str, path: str) -> IdeWorkspaceFile | None:
    """The file row as the database holds it now.

    ``populate_existing``: the session keeps loaded objects
    (``expire_on_commit=False``), and open/refresh read the row again under
    the session lock after a slow ARC-1 read. Without it that re-read would
    hand back the copy loaded before the read and drop a proposal a run
    wrote in between."""
    return (
        await db.execute(
            select(IdeWorkspaceFile)
            .where(IdeWorkspaceFile.session_id == sid, IdeWorkspaceFile.path == path)
            .execution_options(populate_existing=True)
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


async def _lock_for_base_write(
    db: AsyncSession, session: IdeSession
) -> JSONResponse | None:
    """Take the session row lock before open/refresh write a file's base,
    and check the run lock again under it; 409 ``run_in_progress`` (nothing
    written, transaction ended) when a run started meanwhile.

    The ``_refuse_while_running`` check before the ARC-1 read is not enough:
    the read can take seconds, a run can start in between, and writing the
    base then reverts SAP's change on the run's save. And these writes can
    drop a proposal (one byte-equal to SAP's source becomes ``read``), which
    changes what a ``propose`` approve pins; that approve holds this same
    lock (``stages.approve``), so the two are serialised on Postgres. The
    lock is taken *after* the reads, so it is never held across ARC-1.
    Callers read the file row again after this call."""
    await store.lock_session_row(db, session.id)
    await db.refresh(session)
    if (busy := _refuse_while_running(session)) is not None:
        await db.rollback()
        return busy
    return None


async def _refuse_lost_flag(
    db: AsyncSession, session: IdeSession
) -> JSONResponse | None:
    """409 ``target_not_non_production`` for a diagnose session whose target
    is not flagged ``non_production`` *now*, else ``None``.

    The flag is enforced when the session is created, but an admin can take
    it away afterwards (or the conventions row can go): the target is
    production from then on, and a session opened for runtime data must not
    keep reading from it. Called by every route that
    would call ARC-1 for the session, after the owner check and before
    anything is sent. A change session is never refused here.
    """
    if (session.session_type or CHANGE) != DIAGNOSE:
        return None
    conv = await store.get_conventions(db, session.target)
    if not is_non_production(conv):
        return _gate_response(lost_flag_error())
    return None


def _read_args(type_: str, name: str, include: str | None) -> dict[str, Any]:
    # SAPRead always carries an explicit type: the read-only guard requires it.
    args: dict[str, Any] = {"type": type_, "name": name}
    if include:
        args["include"] = include
    return args


def _valid_items(data: Any, model: type[BaseModel]) -> list[Any]:
    """The items of a stored JSON list that fit ``model``; others are left
    out, so one damaged entry does not turn the whole file into a 500."""
    out = []
    dropped = 0
    for item in data if isinstance(data, list) else []:
        try:
            out.append(model.model_validate(item))
        except ValidationError:
            dropped += 1
    if dropped:
        # The count and the model only: the items may hold source text.
        log.warning("Dropped %d stored %s item(s) that do not fit the model",
                    dropped, model.__name__)
    return out


def _lint_of(row: IdeWorkspaceFile) -> list[LintFindingOut]:
    return _valid_items(_loads(row.lint_json), LintFindingOut)


def _file_detail(
    row: IdeWorkspaceFile, revision: IdeFileRevision | None
) -> FileDetailOut:
    """The file with the syntax result of ``revision`` -- the revision
    served, which is the latest (``row.revision``) unless a caller asks for
    another one."""
    status = revision.syntax_status if revision is not None else None
    syntax = (
        _valid_items(_loads(revision.syntax_json), SyntaxItemOut)
        if revision is not None
        else []
    )
    return FileDetailOut(
        **_file_summary(row, status).model_dump(),
        origin_source=row.origin_source,
        proposed_source=row.proposed_source,
        origin_version=row.origin_version,
        lint=_lint_of(row),
        syntax=syntax,
    )


async def _file_detail_latest(db: AsyncSession, row: IdeWorkspaceFile) -> FileDetailOut:
    revision = (
        await store.get_revision(db, row.session_id, row.path, row.revision)
        if row.revision
        else None
    )
    return _file_detail(row, revision)


@router.get("/sessions/{sid}/files", response_model=list[FileSummaryOut])
async def list_files(
    sid: str, principal: str = Depends(_caller), db: AsyncSession = Depends(_db)
):
    session = await _owned(db, sid, principal)
    rows = (
        await db.execute(
            select(IdeWorkspaceFile)
            .where(IdeWorkspaceFile.session_id == session.id)
            .order_by(IdeWorkspaceFile.path)
        )
    ).scalars().all()
    return await _file_summaries(db, session.id, list(rows))


@router.get("/sessions/{sid}/file", response_model=FileDetailOut)
async def get_file(
    sid: str,
    path: str = Query(default="", max_length=paths.MAX_PATH_LENGTH),
    revision: str | None = Query(default=None, max_length=12),
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    """The file; with ``revision`` its ``proposed_source`` and syntax result
    are that revision's (``revision`` in the body stays the file's latest).

    ``revision`` is taken as text and parsed here: anything that is not a
    revision of this file -- ``0``, ``-1``, ``abc`` or a number past the
    latest -- answers 404 ``unknown_revision``, one answer for "no such
    revision" whatever the spelling."""
    session, row = await _owned_file(db, sid, path, principal)
    if revision is None:
        return await _file_detail_latest(db, row)
    number = int(revision) if revision.isascii() and revision.isdigit() else 0
    rev = (
        await store.get_revision(db, session.id, row.path, number)
        if number >= 1 else None
    )
    if rev is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "This file has no such revision",
                     "code": "unknown_revision"},
        )
    out = _file_detail(row, rev)
    out.proposed_source = rev.proposed_source
    return out


@router.post("/sessions/{sid}/file/refresh", response_model=FileDetailOut)
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
    if isinstance(client, JSONResponse):
        return client
    try:
        source = await client.call("SAPRead", _read_args(type_, name, include))
    except arc1.Arc1Error as exc:
        return _arc1_response(exc)
    if (refused := _no_source(source)) is not None:
        return refused
    if (refused := _too_large(source)) is not None:
        return refused
    # The diff base and its version marker, as ``open_object`` stores them.
    # A refresh never replaces a proposal with SAP's source: a differing
    # proposal is kept as it is; one byte-equal to the new SAP source is no
    # change any more and is dropped (state ``read``; its revisions stay).
    version = await arc1.read_version(client, type_, name)
    if (busy := await _lock_for_base_write(db, session)) is not None:
        return busy
    row = await _file(db, session.id, row.path)
    if row is None:
        await db.rollback()
        raise HTTPException(status_code=404, detail="File not found")
    basecheck.apply_base(row, source, version)
    await store.touch_session(db, session.id)
    await db.commit()
    await db.refresh(row)
    return await _file_detail_latest(db, row)


@router.post("/sessions/{sid}/file/lint", response_model=list[LintFindingOut])
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
    if isinstance(client, JSONResponse):
        return client
    try:
        out = await client.call(
            "SAPLint", {"action": "lint", "source": source, "name": name}
        )
    except arc1.Arc1Error as exc:
        return _arc1_response(exc)
    findings = arc1.parse_findings(out)
    # Session row first, then the file row (as refresh/open): a lint and a
    # refresh of the same file cannot deadlock on Postgres.
    await store.lock_session_row(db, session.id)
    row = await _file(db, session.id, row.path)
    if row is None:
        await db.rollback()
        raise HTTPException(status_code=404, detail="File not found")
    row.lint_json = json.dumps(findings)
    await store.touch_session(db, session.id)
    await db.commit()
    return findings


# Sessions with an on-demand syntax check in flight (this process): a second
# click while SAP is still answering would only race the first one's store.
# One process per app instance, and the check is idempotent, so an
# in-process guard is enough.
_SYNTAX_IN_FLIGHT: set[str] = set()


@router.post("/sessions/{sid}/file/syntax", response_model=SyntaxResultOut)
async def syntax_file(
    sid: str,
    path: str = Query(default="", max_length=paths.MAX_PATH_LENGTH),
    revision: str | None = Query(default=None, max_length=12),
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    """Run the ARC-1 syntax dry run of one revision now (default: the
    latest) and store it on that revision, overwriting an earlier result.

    Nothing is written to SAP: ``SAPDiagnose`` ``syntax`` checks the
    proposed source as the signed-in user. An ARC-1 failure is stored and
    answered as ``status: "unavailable"`` (200), never as ok
    (``agents.ide.syntaxcheck``).
    """
    session = await _owned(db, sid, principal)
    if (lost := await _refuse_lost_flag(db, session)) is not None:
        return lost
    if (busy := _refuse_while_running(session)) is not None:
        return busy
    clean = _clean_path(path)
    row = await _file(db, session.id, clean)
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    if paths.object_for(row.path) is None:
        return JSONResponse(
            status_code=422,
            content={"detail": "Not an ABAP object file", "code": "not_an_object"},
        )
    if revision is None:
        if not row.revision:
            return JSONResponse(
                status_code=422,
                content={"detail": "The file has no proposal to check",
                         "code": "no_proposal"},
            )
        number = row.revision
    else:
        number = int(revision) if revision.isascii() and revision.isdigit() else 0
    rev = (
        await store.get_revision(db, session.id, row.path, number)
        if number >= 1 else None
    )
    if rev is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "This file has no such revision",
                     "code": "unknown_revision"},
        )
    if session.id in _SYNTAX_IN_FLIGHT:
        return JSONResponse(
            status_code=409,
            content={"detail": "A syntax check of this session is already running",
                     "code": "syntax_check_running"},
        )
    _SYNTAX_IN_FLIGHT.add(session.id)
    try:
        # Looked up on the module at call time: the tests' seam.
        rev = await syntaxcheck.check_one(db, session, row.path, rev)
    except LookupError:
        return JSONResponse(
            status_code=404,
            content={"detail": "This file has no such revision",
                     "code": "unknown_revision"},
        )
    finally:
        _SYNTAX_IN_FLIGHT.discard(session.id)
    return SyntaxResultOut(
        path=row.path,
        revision=rev.revision,
        status=rev.syntax_status,
        items=_valid_items(_loads(rev.syntax_json), SyntaxItemOut),
        checked_at=_iso(rev.syntax_checked_at),
    )


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
    if isinstance(client, JSONResponse):
        return client
    try:
        source = await client.call("SAPRead", _read_args(type_, name, include))
    except arc1.Arc1Error as exc:
        return _arc1_response(exc)
    if (refused := _no_source(source)) is not None:
        return refused
    if (refused := _too_large(source)) is not None:
        return refused
    version = await arc1.read_version(client, type_, name)
    if (busy := await _lock_for_base_write(db, session)) is not None:
        return busy
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
    # Re-opening refreshes the diff base (with its SAP version marker, as
    # the ``open_object`` tool stores it); a differing proposal is kept, one
    # byte-equal to SAP's source is dropped (``basecheck.apply_base``).
    basecheck.apply_base(row, source, version)
    await store.touch_session(db, session.id)
    await db.commit()
    await db.refresh(row)
    return row


@router.post("/sessions/{sid}/open", response_model=FileSummaryOut)
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
    return await _file_summary_one(db, opened)


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
    returned."""
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


@router.get("/sessions/{sid}/findings", response_model=list[FindingOut])
async def list_findings(
    sid: str, principal: str = Depends(_caller), db: AsyncSession = Depends(_db)
):
    """Newest first; metadata only, never the detail text."""
    session = await _owned(db, sid, principal)
    return [findings.finding_out(f) for f in await store.list_findings(db, session.id)]


@router.get("/sessions/{sid}/findings/{fid}", response_model=FindingDetailOut)
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
    target). It is served, and SAP is read, only while ``is_non_production``
    is true for the target's conventions as they are at this moment; a
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
    # Always the diagnose policy (the session is a diagnose session).
    # ``_client_for`` reads the conventions again itself, so a flag taken
    # away since the check above is still refused.
    client = await _client_for(db, session.target, DIAGNOSE)
    if isinstance(client, JSONResponse):
        return client
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


@router.post("/sessions/{sid}/findings/{fid}/open", response_model=FindingOpenOut)
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
        "file": await _file_summary_one(db, opened),
        "line": line if exact else None,
        "hint": None if exact else _line_hint(include, line),
    }


# --- trace approvals (diagnose) -----------------------------------------------


@router.get("/sessions/{sid}/approvals", response_model=list[ApprovalOut])
async def list_approvals(
    sid: str, principal: str = Depends(_caller), db: AsyncSession = Depends(_db)
):
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


@router.post("/sessions/{sid}/approvals/{aid}", response_model=ApprovalOut)
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


@router.get("/objects/search", response_model=list[ObjectHitOut])
async def search_objects(
    target: str = Query(pattern=TARGET_PATTERN),
    q: str = Query(min_length=1, max_length=100),
    _: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    client = await _client_for(db, target)
    if isinstance(client, JSONResponse):  # change policy: not expected
        return client
    try:
        # Object mode only: searchType/source left to ARC-1's defaults
        # (object, adt), which the read-only policy allows.
        out = await client.call("SAPSearch", {"query": q.strip(), "maxResults": 50})
    except arc1.Arc1Error as exc:
        return _arc1_response(exc)
    return arc1.parse_search(out)


# --- refused request bodies ---------------------------------------------------

IDE_PATH_PREFIX = "/ide/api/"


def _validation_item(err: dict) -> dict[str, Any]:
    # ``loc``/``msg``/``type`` only: ``input`` (and ``ctx``, which can hold
    # it) is what the client sent -- a lone surrogate there cannot be
    # encoded, and echoing request data back serves no one.
    return {
        "loc": [p if isinstance(p, int) else str(p) for p in err.get("loc", ())],
        "msg": _encodable(str(err.get("msg", ""))),
        "type": str(err.get("type", "")),
    }


def _encodable(text: str) -> str:
    return text.encode("utf-8", "replace").decode("utf-8")


async def _ide_validation_error(request: Request, exc: RequestValidationError):
    """422 for a refused IDE request, without the client's input.

    FastAPI's own answer echoes each error's ``input``; one holding a lone
    surrogate (a field cut inside an emoji) fails to encode, and the refusal
    became a 500. The shape the UI reads field errors from (``detail[]``
    with ``loc``/``msg``) is kept. Other routes keep FastAPI's answer."""
    if not request.url.path.startswith(IDE_PATH_PREFIX):
        return await request_validation_exception_handler(request, exc)
    return JSONResponse(
        status_code=422,
        content={"detail": [_validation_item(e) for e in exc.errors()]},
    )


def install_validation_handler(app: FastAPI) -> None:
    """Register :func:`_ide_validation_error` on the app (a router cannot
    carry an exception handler); app.py calls it."""
    app.add_exception_handler(RequestValidationError, _ide_validation_error)
