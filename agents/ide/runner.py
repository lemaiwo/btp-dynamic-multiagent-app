"""Run one IDE stage turn (a message, a revise or a report) and stream what
happens.

:func:`run_stage` is what the message, revise and report routes call. It reports
through ``emit(event, data)`` using the SSE vocabulary of the IDE contract
(``run``, ``text``, ``tool``, ``plan``, ``file``, ``artifact``, ``finding``,
``approval_required``, ``usage``, ``error``, ``done``); turning those into
frames is the route's job.

1. **Start.** In one transaction the session row is read (``SELECT ... FOR
   UPDATE`` on Postgres), :func:`agents.ide.stages.assert_can_run` checks
   *that* row, and a conditional ``UPDATE ... WHERE status != 'running'``
   takes the run lock, so two concurrent starts cannot both win even on
   SQLite. The user message (or the revise feedback) is saved in the same
   transaction. A refusal raises :class:`StageGateError` before anything is
   emitted.
2. **Run.** The agent runs in its own task (registered in ``_tasks`` for
   :func:`cancel`). That task binds the session workspace
   (``bound_workspace``), the run's activity recorder and the IDE progress
   sink, and resets all three itself: no ``ContextVar`` is ever set on one
   side of an SSE generator's ``yield`` and reset on the other. The task
   copies the caller's context, so ``current_jwt`` / ``current_principal``
   reach ``DestinationAuth(user_context=True)`` without being re-bound.
3. **Cap.** The session may spend ``IDE_SESSION_REQUEST_CAP`` model requests
   in total. The scope carries what is left; ``shared.run_usage_limits()``
   reads it, and since delegations and deep sub-agents run on the parent's
   ``usage`` and call that same function, the cap covers the whole run tree.
4. **Finish.** Whatever happened (success, error, usage exhausted, cancel)
   the assistant message is saved, requests are added to ``requests_used``,
   the lock is released (``status='idle'``, ``run_id=NULL``) and ``done`` is
   the last event. The finish runs to completion even when cancelled again,
   bounded by ``IDE_SAVE_TIMEOUT_S``. The release is retried once.
5. **Liveness.** A run touches its row every ``IDE_RUN_HEARTBEAT_S`` and is
   stopped after ``IDE_RUN_TIMEOUT_S``. A ``running`` row with no task in this
   process and older than ``IDE_RUN_STALE_S`` (at least three heartbeats) is
   stale: the next start reclaims it, and :func:`release_stale` does so for
   the cancel route.
   Error details stay in the log; the client gets a code and a generic text.
6. **Session type.** The scope is bound with the session's type, which
   selects the read-only policy. A session that is not a ``change`` session
   also gets a ``DiagnoseRun`` bound on ``current_diagnose`` (set and reset
   in the run task), carrying the answer of the one masking switch,
   ``agents.ide.diagnose.masking_required(conventions)``, taken when the run
   starts. Masking required (the target is not, or no longer, flagged
   ``non_production``, or its conventions could not be loaded): the run is
   **refused** with ``target_not_non_production`` -- by
   ``stages.assert_can_run`` and again by :func:`_start` from its own read
   of the conventions -- before anything is stored or emitted. Raw (a
   ``non_production`` target): nothing is masked and the activity is stored
   as produced. What runs behind :func:`_start` still honours
   ``_Start.masked`` (the guard masks every tool result, the stored
   activity carries no tool output, no finding detail is kept): it defaults
   to masked, so a start that did not say "raw" never leaks.
   Either way a diagnose workspace is never persisted.
   The top-level agent of such a run is :func:`diagnose_agent_name`
   (``IDE_DIAGNOSE_AGENT``), never the change orchestrator. When neither it
   nor a peer it can delegate to has the target's ARC-1 server, the run says
   so (``error`` event, code ``no_diagnose_server``) and carries on: without
   that note an empty answer reads as "there are no dumps". When the
   target's conventions could not be read at all the note is
   ``conventions_unavailable`` instead: the agent's servers are not to blame.
   A ``SAPDiagnose`` ``trace_start``/``trace_cancel`` call of any agent in
   the run is stored as a proposal by the guard, which emits
   ``approval_required`` through the run's ``emit``; a proposal that was not
   stored puts its code on the ``tool`` event. Nothing pauses.
7. **Report.** ``report=True`` (diagnose only) runs the stage with the app's
   own ``REPORT_REQUEST`` as the user text and captures the answer as the
   artifact ``report``. A diagnose message run captures nothing. Which
   artifact a run captures is decided once, at the start
   (``stages.artifact_kind``), and carried on the run.
8. **Findings.** What the guard noted on the diagnose run while its agents
   read dumps, traces and errors (``agents.ide.findings``) is stored when
   the run ends -- also when it failed or was cancelled -- and emitted as
   one ``finding`` event per row. The detail text of a finding is stored
   only when the run was not masked.

``instructions=`` is passed to ``Agent.run`` at run time, which pydantic-ai
1.72 treats as *additional* instructions: the specialist keeps its own. The
run uses ``Agent.run`` with an ``event_stream_handler`` (``Agent.iter`` has no
handler parameter): the handler streams the top-level agent's text deltas as
``text`` and hands every event on to the registry's progress handler, which
reports tool calls into the sink.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import PartDeltaEvent, PartStartEvent, TextPart, TextPartDelta
from pydantic_ai.usage import RunUsage
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agents.db import SessionLocal
from agents.destination_auth import DestinationUserRequired
from agents.ide import diagnose
from agents.ide.diagnose import (
    DiagnoseRun,
    current_diagnose,
    masking_required,
    strip_activity,
)
from agents.ide.findings import finding_out
from agents.ide.models import IdeMessage, IdeSession, utcnow
from agents.ide.readonly import CHANGE, REFUSED_PREFIX, proposal_refusal_code
from agents.ide.stages import (
    REPORT_REQUEST,
    SUBAGENTS_ALLOWED,
    Stage,
    StageGateError,
    artifact_kind,
    assert_can_run,
    build_prompt,
    lost_flag_error,
    request_cap,
)
from agents.ide.store import (
    add_artifact,
    add_message,
    get_conventions,
    upsert_findings,
)
from agents.ide.workspace import bound_workspace, save_timeout
from agents.progress import ProgressUpdate, current_progress
from agents.registry import _make_progress_handler, registry
from agents.run_activity import RunActivity, recording
from agents.shared import run_usage_limits

log = logging.getLogger(__name__)

Emit = Callable[[str, dict], None]

DEFAULT_AGENT = "abap-orchestrator"
DEFAULT_DIAGNOSE_AGENT = "abap-diagnostics"

# sid -> the task running that session's agent. One run per session (the
# status row is the lock), so the session id is the key.
_tasks: dict[str, asyncio.Task] = {}


class SessionNotFound(LookupError):
    """No session with this id belongs to the caller (the route answers 404)."""


def orchestrator_name() -> str:
    return os.environ.get("IDE_ORCHESTRATOR_AGENT", "").strip() or DEFAULT_AGENT


def diagnose_agent_name() -> str:
    """The top-level agent of a diagnose run (``IDE_DIAGNOSE_AGENT``)."""
    return os.environ.get("IDE_DIAGNOSE_AGENT", "").strip() or DEFAULT_DIAGNOSE_AGENT


def _agent_name(session_type: str) -> str:
    """Only a change session runs the orchestrator; there is no fallback
    from one to the other."""
    return orchestrator_name() if session_type == CHANGE else diagnose_agent_name()


def _safe(emit: Emit) -> Emit:
    """An emit that never breaks the run (a closed stream is not an error)."""

    def wrapped(kind: str, data: dict) -> None:
        try:
            emit(kind, data)
        except Exception:  # noqa: BLE001
            log.debug("IDE emit %s dropped", kind, exc_info=True)

    return wrapped


# --- start (lock) -------------------------------------------------------------


@dataclass
class _Start:
    sid: str
    run_id: str
    stage: Stage
    message_id: str
    requests_used: int
    owner: str = ""
    target: str = ""
    session_type: str = CHANGE
    # The target's ARC-1 destination (diagnose: recognises the target's server).
    destination: str = ""
    # False when the target's conventions could not be read (diagnose only):
    # the run is masked and nobody knows which server is the target's.
    conventions_known: bool = True
    # The run's answer of ``masking_required``. Masked unless ``_start`` says
    # otherwise, which it does only for a change session (never masked, as in
    # phase 1a) or a diagnose target the switch calls raw.
    masked: bool = True
    # What the run works with: the caller's text, masked when ``masked``.
    user_text: str | None = None
    feedback: str | None = None
    report: bool = False
    # What a successful run captures from its final answer, if anything.
    artifact_kind: str | None = None


def _is_postgres(db: AsyncSession) -> bool:
    return db.get_bind().dialect.name == "postgresql"


# --- run lock liveness -----------------------------------------------------
#
# The ``running`` row is the lock. A run that dies without releasing it (the
# release UPDATE failed, the process was killed) would otherwise refuse every
# later run with 409. A live run touches ``updated_at`` every
# ``IDE_RUN_HEARTBEAT_S``; a ``running`` row is *stale* when this process has
# no live task for the session AND the row is older than ``IDE_RUN_STALE_S``.
# The age check is what makes this safe with several app instances, which
# cannot see each other's tasks.

DEFAULT_RUN_TIMEOUT_S = 1800.0
DEFAULT_HEARTBEAT_S = 30.0


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


def run_timeout() -> float:
    """``IDE_RUN_TIMEOUT_S``: the longest one run may take (default 30 min)."""
    return _env_seconds("IDE_RUN_TIMEOUT_S", DEFAULT_RUN_TIMEOUT_S)


def heartbeat_interval() -> float:
    """``IDE_RUN_HEARTBEAT_S``: how often a live run touches its row."""
    return _env_seconds("IDE_RUN_HEARTBEAT_S", DEFAULT_HEARTBEAT_S)


STALE_HEARTBEATS = 3


def stale_after() -> float:
    """``IDE_RUN_STALE_S``: age after which a ``running`` row with no local
    task is reclaimed. Default: :func:`ghost_age` (``STALE_HEARTBEATS``
    heartbeats), the same age the startup reset uses: a live run anywhere
    touches its row every heartbeat, so an older row has no live run, and a
    crash does not lock the session for the whole run timeout. Never less
    than that: one late heartbeat must not make a live run look dead. The run
    timeout (``IDE_RUN_TIMEOUT_S``) is separate and only stops live runs."""
    return max(_env_seconds("IDE_RUN_STALE_S", ghost_age()), ghost_age())


def ghost_age() -> float:
    """Age past which a ``running`` row found at startup is a ghost: no live
    run anywhere (this instance's, or another one's during a rolling deploy)
    has touched it for ``STALE_HEARTBEATS`` heartbeats."""
    return STALE_HEARTBEATS * heartbeat_interval()


def _has_live_task(sid: str) -> bool:
    task = _tasks.get(sid)
    return task is not None and not task.done()


def _age_seconds(row: IdeSession) -> float:
    stamp = row.updated_at
    if stamp is None:
        return float("inf")
    if stamp.tzinfo is None:  # SQLite hands back naive UTC
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds()


def _is_stale(row: IdeSession) -> bool:
    return (
        row.status == "running"
        and not _has_live_task(row.id)
        and _age_seconds(row) > stale_after()
    )


async def _reclaim(db: AsyncSession, row: IdeSession) -> bool:
    """Release a stale lock in ``db``'s transaction (not committed). Only the
    run *as it was seen* is released: a new run has a new ``run_id``, and a
    heartbeat that landed after the read changed ``updated_at``."""
    result = await db.execute(
        update(IdeSession)
        .where(
            IdeSession.id == row.id,
            IdeSession.status == "running",
            IdeSession.run_id == row.run_id
            if row.run_id is not None
            else IdeSession.run_id.is_(None),
            IdeSession.updated_at == row.updated_at
            if row.updated_at is not None
            else IdeSession.updated_at.is_(None),
        )
        .values(status="idle", run_id=None)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False
    log.warning(
        "IDE session %s: reclaimed a stale run lock (run %s, %.0fs old)",
        row.id, row.run_id, _age_seconds(row),
    )
    await db.refresh(row)
    return True


async def release_stale(sid: str) -> bool:
    """Release the session's run lock if it is stale; True when released.

    For the cancel route: when :func:`cancel` finds no task here, this frees a
    lock left by a run that died. A row a live run keeps fresh, or that a run
    in this process holds, is left alone. The caller has already checked the
    session's owner.
    """
    async with SessionLocal() as db:
        stmt = select(IdeSession).where(IdeSession.id == sid)
        if _is_postgres(db):
            stmt = stmt.with_for_update()
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is None or not _is_stale(row):
            return False
        released = await _reclaim(db, row)
        await db.commit()
        return released


async def _diagnose_settings(target: str) -> tuple[bool, str, bool]:
    """``(masked, destination, known)`` of a diagnose run on ``target``.

    The row is loaded *and read* inside one ``try``: a missing row, a failing
    load and a row whose attributes cannot be read all end as "masked, no
    destination, not known". Read in a session of its own, so a failure
    cannot break the start transaction. ``known`` is False when there was no
    row to read: then nobody can say which server is the target's.
    """
    try:
        async with SessionLocal() as db:
            conventions = await get_conventions(db, target)
            masked = masking_required(conventions)
            destination = (getattr(conventions, "destination", "") or "").strip()
        return masked, destination, conventions is not None
    except Exception:  # noqa: BLE001 -- fail safe: unknown means masked
        log.warning(
            "IDE: conventions of target %r could not be read; masking", target,
            exc_info=True,
        )
        return True, "", False


async def _start(
    sid: str,
    owner: str,
    user_text: str | None,
    feedback: str | None,
    report: bool = False,
) -> _Start:
    async with SessionLocal() as db:
        stmt = select(IdeSession).where(
            IdeSession.id == sid, IdeSession.owner == owner
        )
        if _is_postgres(db):
            stmt = stmt.with_for_update()
        session = (await db.execute(stmt)).scalar_one_or_none()
        if session is None or not owner:
            raise SessionNotFound(sid)
        if _is_stale(session):
            await _reclaim(db, session)
        # The row just read (and locked on Postgres) is what the gate checks.
        await assert_can_run(
            db, session, revise=feedback is not None, report=report
        )
        stage = Stage(session.stage)
        run_id = str(uuid.uuid4())
        # The type comes from the row just read, never from the caller.
        session_type = session.session_type or CHANGE
        if report:
            user_text = REPORT_REQUEST
        masked, destination, conventions_known = True, "", True
        if session_type == CHANGE:
            masked = False
        else:
            masked, destination, conventions_known = await _diagnose_settings(
                session.target
            )
            if masked:
                # Defence in depth behind ``assert_can_run``: the target is
                # not (or no longer) flagged non-production, or nobody could
                # read its conventions. Such a session starts no run at all;
                # nothing was written yet, so the ``with`` block rolls back.
                raise lost_flag_error()
        # The conditional UPDATE is the lock on SQLite (no FOR UPDATE there)
        # and a no-op guard on Postgres.
        result = await db.execute(
            update(IdeSession)
            .where(
                IdeSession.id == sid,
                IdeSession.stage == stage.value,
                IdeSession.status != "running",
            )
            .values(status="running", run_id=run_id, updated_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            fresh = await db.get(IdeSession, sid, populate_existing=True)
            if fresh is not None and fresh.status == "running":
                raise StageGateError(
                    "run_in_progress", "A run is in progress for this session."
                )
            raise StageGateError(
                "stage_changed",
                "The session changed in the meantime; reload and try again.",
            )
        # The feedback of a revise is saved as the run's user message; the
        # prompt carries it once, in the revision block (stages.build_prompt).
        message = IdeMessage(
            session_id=sid,
            stage=stage.value,
            role="user",
            content=user_text if user_text is not None else feedback,
        )
        db.add(message)
        await db.commit()
        return _Start(
            sid=sid,
            run_id=run_id,
            stage=stage,
            message_id=message.id,
            requests_used=session.requests_used or 0,
            owner=owner,
            target=session.target,
            session_type=session_type,
            destination=destination,
            conventions_known=conventions_known,
            masked=masked,
            user_text=user_text,
            feedback=feedback,
            report=report,
            artifact_kind=artifact_kind(session_type, stage, report),
        )


# --- progress ----------------------------------------------------------------


class _EmitSink:
    """The progress sink of an IDE run: records into the run's activity (as
    ``agents.run_activity`` does for job runs) and emits each tool event and
    plan change as it happens."""

    # Nobody behind the stream can click a sign-in link mid-run.
    interactive = False

    def __init__(self, activity: RunActivity, emit: Emit) -> None:
        self.activity = activity
        self.emit = emit

    def _event(self, call_id: str) -> dict | None:
        for event in reversed(self.activity.events):
            if event.get("kind") == "tool" and event.get("id") == call_id:
                return event
        return None

    def __call__(self, update: ProgressUpdate) -> None:
        self.activity.record(update)
        if update.kind in ("tool_start", "tool_end") and update.tool_call_id:
            event = self._event(update.tool_call_id)
            if event is not None:
                if event.get("status") == "error" and REFUSED_PREFIX in str(
                    event.get("output") or ""
                ):
                    # The read-only guard refused the call: a distinct code,
                    # so a client can tell it from a failing tool. Set on the
                    # recorded event, so the stored activity carries it too.
                    event["code"] = "readonly_refused"
                elif update.kind == "tool_end" and (
                    code := proposal_refusal_code(event.get("output"))
                ):
                    # A trace proposal that was not stored (``too_many_
                    # pending``, ``unknown_trace_request``, ...).
                    event["code"] = code
                self.emit("tool", dict(event))
        if update.kind == "tool_start" and update.tool_name == "write_todos":
            self.emit("plan", {"todos": list(self.activity.plan)})


def _stream_handler(agent_name: str, emit: Emit, parts: list[str]):
    """Stream the top-level agent's text as ``text`` and pass every event on
    to the registry's progress handler (tool calls -> sink)."""
    progress = _make_progress_handler(agent_name)

    def _text(delta: str) -> None:
        if delta:
            parts.append(delta)
            emit("text", {"delta": delta})

    async def handler(ctx, events) -> None:
        async def tee():
            async for event in events:
                if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                    _text(event.part.content)
                elif isinstance(event, PartDeltaEvent) and isinstance(
                    event.delta, TextPartDelta
                ):
                    _text(event.delta.content_delta)
                yield event

        await progress(ctx, tee())

    return handler


# --- run --------------------------------------------------------------------


@dataclass
class _Outcome:
    final_text: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    cancelled: bool = False
    activity: dict | None = None
    changed: list[dict] | None = None
    # What the guard noted on the diagnose run (``agents.ide.findings``).
    findings: list[dict] | None = None


async def _complete(coro: Awaitable[Any], what: str, sid: str) -> bool:
    """Await ``coro`` to the end even if cancelled meanwhile (bounded by
    ``IDE_SAVE_TIMEOUT_S`` once cancelled). Returns True when a cancel was
    absorbed so the caller re-raises it."""
    task = asyncio.ensure_future(coro)
    try:
        await asyncio.shield(task)
        return False
    except asyncio.CancelledError:
        if task.done():
            raise
    loop = asyncio.get_running_loop()
    deadline = loop.time() + save_timeout()
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            task.cancel()
            log.error("IDE session %s: %s did not finish after a cancel", sid, what)
            return True
        try:
            done, _ = await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            continue
        if done:
            if not task.cancelled() and task.exception() is not None:
                log.error("IDE session %s: %s failed", sid, what,
                          exc_info=task.exception())
            return True


async def _release(start: _Start, usage: RunUsage) -> IdeSession | None:
    """Release the run lock and count the run's requests; the session row
    afterwards (None when the session is gone)."""
    async with SessionLocal() as db:
        await db.execute(
            update(IdeSession)
            .where(IdeSession.id == start.sid, IdeSession.run_id == start.run_id)
            .values(
                status="idle",
                run_id=None,
                requests_used=IdeSession.requests_used + usage.requests,
                updated_at=utcnow(),
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return await db.get(IdeSession, start.sid)


async def _heartbeat(sid: str, run_id: str) -> None:
    """Keep the lock row fresh while the run is live, so no instance takes
    a long run for a dead one."""
    while True:
        await asyncio.sleep(heartbeat_interval())
        try:
            async with SessionLocal() as db:
                await db.execute(
                    update(IdeSession)
                    .where(IdeSession.id == sid, IdeSession.run_id == run_id)
                    .values(updated_at=utcnow())
                    .execution_options(synchronize_session=False)
                )
                await db.commit()
        except Exception:  # noqa: BLE001
            log.warning("IDE session %s: heartbeat failed", sid, exc_info=True)


async def _save_findings(start: _Start, out: _Outcome, emit: Emit) -> None:
    """Store what a diagnose run found and emit one ``finding`` per row.

    Also after a failed or cancelled run: what was read was read. Runs while
    the session's run lock is still held, so there is one writer. A masked
    run stores metadata only -- the guard collects no detail text for it,
    and here it is dropped again and cleared on the rows (the target may
    have lost its ``non_production`` flag since an earlier run). Guarded: a
    failing write is logged and the run still ends normally.
    """
    if start.session_type == CHANGE or not out.findings:
        return
    try:
        items = [
            {k: v for k, v in item.items() if not (start.masked and k == "detail")}
            for item in out.findings
        ]
        async with SessionLocal() as db:
            rows = await upsert_findings(
                db, start.sid, items, clear_detail=start.masked
            )
            events = [finding_out(row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        # No traceback: SQL parameters would carry the finding's values.
        log.error(
            "IDE session %s: saving %d findings failed: %s",
            start.sid, len(out.findings), type(exc).__name__,
        )
        return
    for event in events:
        emit("finding", event)


async def _finish(start: _Start, out: _Outcome, usage: RunUsage, emit: Emit) -> None:
    """Persist the outcome, release the lock and emit the closing events.

    Each step is guarded: a failing write is logged and the lock is still
    released, and ``done`` is always emitted last.
    """
    sid = start.sid
    message_id = start.message_id
    stage_value = start.stage.value
    status = "idle"
    succeeded = out.final_text is not None and out.error_code is None \
        and not out.cancelled
    try:
        if out.error_code != "agent_missing":
            if succeeded:
                content = out.final_text or ""
            else:
                partial = (out.final_text or "").rstrip()
                tail = "(cancelled)" if out.cancelled else f"(error: {out.error_message})"
                content = f"{partial}\n\n{tail}" if partial else tail
            async with SessionLocal() as db:
                msg = await add_message(
                    db, sid, stage=stage_value, role="assistant", content=content,
                    activity_json=(
                        json.dumps(
                            strip_activity(out.activity) if start.masked
                            else out.activity
                        )
                        if out.activity is not None else None
                    ),
                )
                message_id = msg.id
                kind = start.artifact_kind
                if succeeded and kind and (out.final_text or "").strip():
                    art = await add_artifact(
                        db, sid, stage=stage_value, kind=kind, content=out.final_text
                    )
                    emit("artifact", {"id": art.id, "kind": art.kind,
                                      "version": art.version})
    except Exception:  # noqa: BLE001
        log.exception("IDE session %s: saving the run outcome failed", sid)

    for changed in out.changed or []:
        emit("file", {"path": changed["path"], "state": changed["state"]})

    await _save_findings(start, out, emit)

    row = None
    for attempt in (1, 2):
        try:
            row = await _release(start, usage)
            break
        except Exception:  # noqa: BLE001
            log.exception(
                "IDE session %s: releasing the run lock failed (attempt %d)",
                sid, attempt,
            )
    if row is None:
        # The lock stays until it is stale (no task here, no heartbeat) and
        # the next start or the cancel route reclaims it.
        status = "running"
    else:
        stage_value, status = row.stage, row.status
        if out.error_code != "agent_missing":
            emit("usage", {"requests_used": row.requests_used,
                           "request_cap": request_cap()})

    if out.error_code is not None:
        emit("error", {"message": out.error_message or out.error_code,
                       "code": out.error_code})
    emit("done", {"message_id": message_id, "stage": stage_value, "status": status})


def _reaches_target_server(build: Any, name: str, run: DiagnoseRun) -> bool:
    """Does agent ``name``, or a peer it can delegate to (transitively), have
    the MCP server of the run's target?

    An agent elsewhere in the build does not count: the run cannot reach it.
    If the check itself fails the answer is "yes" -- it only decides on a
    note to the user, and must not claim what it could not establish.
    """
    try:
        peers = {
            c.get("name"): c.get("peers") or ()
            for c in build.configs if isinstance(c, dict)
        }
        seen: set[str] = set()
        todo = [name]
        while todo:
            current = todo.pop()
            if current in seen:
                continue
            seen.add(current)
            agent = build.specialists.get(current)
            for toolset in getattr(agent, "toolsets", None) or ():
                # Looked up on the module at call time: the tests' seam.
                if diagnose.is_target_server(toolset, run):
                    return True
            todo.extend(p for p in peers.get(current, ()) if isinstance(p, str))
        return False
    except Exception:  # noqa: BLE001
        log.warning(
            "IDE session %s: could not check agent %r for the target's server",
            run.session_id, name, exc_info=True,
        )
        return True


async def _execute(start: _Start, emit: Emit) -> None:
    """The run task: everything bound here is reset here."""
    user_text, feedback = start.user_text, start.feedback
    out = _Outcome()
    usage = RunUsage()
    parts: list[str] = []
    deadline: asyncio.Timeout | None = None
    heartbeat = asyncio.create_task(_heartbeat(start.sid, start.run_id))
    try:
        name = _agent_name(start.session_type)
        try:
            build = registry.build
        except RuntimeError:
            build = None
        agent = build.specialists.get(name) if build is not None else None
        if agent is None:
            out.error_code = "agent_missing"
            out.error_message = f"The IDE agent {name!r} is not available."
            return

        async with SessionLocal() as db:
            session = await db.get(IdeSession, start.sid)
            extra, prompt = await build_prompt(
                db, session, user_text, feedback=feedback,
                exclude_message_id=start.message_id,
                report=start.report, masked=start.masked,
            )

        allow = SUBAGENTS_ALLOWED.get(start.stage, False)
        # The deep section is not added here: every deep agent resolves its
        # own from its row's config and the bound session
        # (deep.scoped_deep_instructions), so no `task` is advertised to an
        # agent built without it and delegates get the workspace text too.
        instructions = extra
        remaining = max(0, request_cap() - start.requests_used)
        build.in_flight.value += 1
        scope = None
        # A change session has no diagnose run. Bound in any case, so a
        # binding inherited from the caller's context never leaks in.
        diagnose_run = None
        if start.session_type != CHANGE:
            diagnose_run = DiagnoseRun(
                session_id=start.sid, owner=start.owner, target=start.target,
                run_id=start.run_id, destination=start.destination,
                masking=start.masked, emit=emit,
            )
            if not start.conventions_known:
                # Not the agent's fault: without the conventions there is no
                # destination to recognise any server by.
                log.warning(
                    "IDE session %s: conventions_unavailable -- the "
                    "conventions of target %r are missing or unreadable; the "
                    "run is masked and its diagnose reads may be refused",
                    start.sid, start.target,
                )
                emit("error", {
                    "code": "conventions_unavailable",
                    "message": (
                        "The settings of this session's system could not be "
                        "read, so dumps and traces may not be readable. Try "
                        "again, or ask an administrator to check the "
                        "system's conventions."
                    ),
                })
            elif not _reaches_target_server(build, name, diagnose_run):
                log.warning(
                    "IDE session %s: no_diagnose_server -- agent %r has no "
                    "MCP server for target %r (destination %r); diagnose "
                    "reads will be refused or missing",
                    start.sid, name, start.target, start.destination,
                )
                emit("error", {
                    "code": "no_diagnose_server",
                    "message": (
                        "The diagnose agent has no connection to this "
                        "session's system, so dumps and traces cannot be "
                        "read. Ask an administrator to check the agent's "
                        "servers."
                    ),
                })
        # The run deadline. A tool's own TimeoutError is the same class, so
        # only ``deadline.expired()`` tells the two apart.
        deadline = asyncio.timeout(run_timeout())
        try:
            # ``session_type`` picks the policy and whether the workspace is
            # saved: only a change session persists (D2).
            async with bound_workspace(
                start.sid, allow_subagents=allow, request_limit=remaining,
                session_type=start.session_type,
            ) as scope:
                with recording(start.run_id) as activity:
                    token = current_progress.set(_EmitSink(activity, emit))
                    diagnose_token = current_diagnose.set(diagnose_run)
                    try:
                        async with deadline:
                            result = await agent.run(
                                prompt,
                                instructions=instructions,
                                usage=usage,
                                usage_limits=run_usage_limits(),
                                event_stream_handler=_stream_handler(
                                    name, emit, parts
                                ),
                            )
                    finally:
                        current_diagnose.reset(diagnose_token)
                        current_progress.reset(token)
                        # Calls cut off by a cancel or an error are stored
                        # as interrupted, never as ``running``.
                        activity.close_open_calls()
                        out.activity = activity.to_dict()
                out.final_text = "" if result.output is None else str(result.output)
        finally:
            build.in_flight.value -= 1
            if scope is not None:
                out.changed = scope.changed_files
            if diagnose_run is not None:
                out.findings = list(diagnose_run.findings)
    except UsageLimitExceeded:
        log.info("IDE session %s: request cap reached", start.sid, exc_info=True)
        out.error_code = "usage_exhausted"
        out.error_message = "This session has used all of its model requests."
        out.final_text = "".join(parts) or None
    except TimeoutError:
        if deadline is not None and deadline.expired():
            log.warning("IDE session %s: run %s timed out", start.sid, start.run_id)
            out.error_code = "run_timeout"
            out.error_message = (
                f"The run took longer than {int(run_timeout())} seconds and was stopped."
            )
        else:
            # A timeout inside the run (a tool's HTTP call, say), not the deadline.
            log.error("IDE session %s: run %s failed", start.sid, start.run_id,
                      exc_info=True)
            out.error_code = "run_failed"
            out.error_message = (
                f"The run failed. Reference for the administrator: {start.run_id}."
            )
        out.final_text = "".join(parts) or None
    except asyncio.CancelledError:
        out.cancelled = True
        out.final_text = "".join(parts) or None
        raise
    except Exception as exc:  # noqa: BLE001
        # Exception text can carry hosts, URLs or tokens: it stays in the log.
        if _caused_by(exc, DestinationUserRequired):
            log.warning(
                "IDE session %s: run %s needs the user's token", start.sid,
                start.run_id, exc_info=True,
            )
            out.error_code = "user_token_required"
            out.error_message = (
                "This run calls SAP as the signed-in user, but no user token "
                "reached the backend. Sign in again and retry."
            )
        else:
            log.exception("IDE session %s: run %s failed", start.sid, start.run_id)
            out.error_code = "run_failed"
            out.error_message = (
                f"The run failed. Reference for the administrator: {start.run_id}."
            )
        out.final_text = "".join(parts) or None
    finally:
        heartbeat.cancel()
        if await _complete(_finish(start, out, usage, emit), "finish", start.sid):
            if not out.cancelled:
                raise asyncio.CancelledError()


def _caused_by(exc: BaseException, cls: type[BaseException]) -> bool:
    """``exc``, a leaf of an exception group or a link of its ``__cause__``
    chain is a ``cls``. Iterative with a seen-set: chains can loop."""
    seen: set[int] = set()
    todo: list[BaseException] = [exc]
    while todo:
        link = todo.pop()
        if id(link) in seen:
            continue
        seen.add(id(link))
        if isinstance(link, cls):
            return True
        if isinstance(link, BaseExceptionGroup):
            todo.extend(link.exceptions)
        # Only explicit causes (``raise ... from``): an implicit __context__
        # is whatever was being handled, not what caused this error.
        if link.__cause__ is not None:
            todo.append(link.__cause__)
    return False


async def run_stage(
    sid: str,
    owner: str,
    user_text: str | None,
    *,
    feedback: str | None,
    emit: Emit,
    report: bool = False,
) -> None:
    """Run one message (``user_text``), revise (``feedback``) or report
    (``report=True``, neither text) turn.

    A report run is for diagnose sessions only (``StageGateError``
    ``not_diagnose`` otherwise); its request is ``stages.REPORT_REQUEST`` and
    its answer is stored as the artifact ``report``.

    Raises :class:`SessionNotFound` or :class:`StageGateError` before any
    event when the run may not start. Otherwise returns after ``done`` was
    emitted; a run stopped through :func:`cancel` returns normally, while
    cancelling the caller cancels the run and re-raises after its cleanup.
    """
    if report:
        if user_text is not None or feedback is not None:
            raise ValueError("a report run takes neither user_text nor feedback")
    elif (user_text is None) == (feedback is None):
        raise ValueError("exactly one of user_text and feedback is required")
    emit = _safe(emit)
    start = await _start(sid, owner, user_text, feedback, report)
    emit("run", {"run_id": start.run_id, "stage": start.stage.value,
                 "message_id": start.message_id})
    task = asyncio.create_task(
        _execute(start, emit), name=f"ide-run:{sid}"
    )
    _tasks[sid] = task
    try:
        await task
    except asyncio.CancelledError:
        me = asyncio.current_task()
        if task.done() and not (me is not None and me.cancelling()):
            return  # stopped through cancel(sid); done was emitted
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise
    finally:
        if _tasks.get(sid) is task:
            del _tasks[sid]


async def cancel(sid: str) -> bool:
    """Cancel the session's run in this process and wait for its cleanup.

    Returns False when no run of this session is in flight here.
    """
    task = _tasks.get(sid)
    if task is None or task.done():
        return False
    task.cancel()
    await asyncio.wait({task}, timeout=save_timeout() * 2)
    return True


async def cancel_all() -> None:
    """Cancel every IDE run and wait for it to finish (lifespan shutdown)."""
    tasks = [t for t in _tasks.values() if not t.done()]
    if not tasks:
        return
    log.info("Cancelling %d in-flight IDE run(s) for shutdown", len(tasks))
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
