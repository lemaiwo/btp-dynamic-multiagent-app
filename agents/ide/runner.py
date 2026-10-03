"""Run one IDE stage turn (a message or a revise) and stream what happens.

:func:`run_stage` is what the message and revise routes call. It reports
through ``emit(event, data)`` using the SSE vocabulary of the IDE contract
(``run``, ``text``, ``tool``, ``plan``, ``file``, ``artifact``, ``usage``,
``error``, ``done``); turning those into frames is the route's job.

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
from agents.ide.models import IdeMessage, IdeSession, utcnow
from agents.ide.readonly import REFUSED_PREFIX
from agents.ide.stages import (
    ARTIFACT_KIND,
    SUBAGENTS_ALLOWED,
    Stage,
    StageGateError,
    assert_can_run,
    build_prompt,
    request_cap,
)
from agents.ide.store import add_artifact, add_message
from agents.ide.workspace import bound_workspace, save_timeout
from agents.progress import ProgressUpdate, current_progress
from agents.registry import _make_progress_handler, registry
from agents.run_activity import RunActivity, recording
from agents.shared import run_usage_limits

log = logging.getLogger(__name__)

Emit = Callable[[str, dict], None]

DEFAULT_AGENT = "abap-orchestrator"

# sid -> the task running that session's agent. One run per session (the
# status row is the lock), so the session id is the key.
_tasks: dict[str, asyncio.Task] = {}


class SessionNotFound(LookupError):
    """No session with this id belongs to the caller (the route answers 404)."""


def orchestrator_name() -> str:
    return os.environ.get("IDE_ORCHESTRATOR_AGENT", "").strip() or DEFAULT_AGENT


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


async def _start(
    sid: str, owner: str, user_text: str | None, feedback: str | None
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
        await assert_can_run(db, session, revise=feedback is not None)
        stage = Stage(session.stage)
        run_id = str(uuid.uuid4())
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
                        json.dumps(out.activity) if out.activity is not None else None
                    ),
                )
                message_id = msg.id
                kind = ARTIFACT_KIND.get(start.stage)
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


async def _execute(
    start: _Start, user_text: str | None, feedback: str | None, emit: Emit
) -> None:
    """The run task: everything bound here is reset here."""
    out = _Outcome()
    usage = RunUsage()
    parts: list[str] = []
    deadline: asyncio.Timeout | None = None
    heartbeat = asyncio.create_task(_heartbeat(start.sid, start.run_id))
    try:
        name = orchestrator_name()
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
        # The run deadline. A tool's own TimeoutError is the same class, so
        # only ``deadline.expired()`` tells the two apart.
        deadline = asyncio.timeout(run_timeout())
        try:
            async with bound_workspace(
                start.sid, allow_subagents=allow, request_limit=remaining
            ) as scope:
                with recording(start.run_id) as activity:
                    token = current_progress.set(_EmitSink(activity, emit))
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
) -> None:
    """Run one message (``user_text``) or revise (``feedback``) turn.

    Raises :class:`SessionNotFound` or :class:`StageGateError` before any
    event when the run may not start. Otherwise returns after ``done`` was
    emitted; a run stopped through :func:`cancel` returns normally, while
    cancelling the caller cancels the run and re-raises after its cleanup.
    """
    if (user_text is None) == (feedback is None):
        raise ValueError("exactly one of user_text and feedback is required")
    emit = _safe(emit)
    start = await _start(sid, owner, user_text, feedback)
    emit("run", {"run_id": start.run_id, "stage": start.stage.value,
                 "message_id": start.message_id})
    task = asyncio.create_task(
        _execute(start, user_text, feedback, emit), name=f"ide-run:{sid}"
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
