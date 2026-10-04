"""Load and save an IDE session's workspace around a run.

The session's files (``IdeWorkspaceFile``) and plan (``IdeSession.todos_json``)
are loaded into a fresh :class:`agents.deep.DeepState` before a run and
written back after it. A fresh state on every load means asyncio semaphores
are never persisted or shared across event loops.

File states after a save:

- a row with ``origin_source`` whose content differs from it -> ``modified``
  (``proposed_source`` holds the content);
- a row whose content equals its origin -> ``read`` (``proposed_source``
  cleared, so reverting an edit undoes the proposal);
- a path with no origin (new object or scratch note) -> ``new``, with
  ``object_type``/``object_name`` from :func:`agents.ide.paths.object_for`.

Each new proposal of a path is also kept as an ``IdeFileRevision`` (1, 2,
...; ``IdeWorkspaceFile.revision`` is the latest), at most one per path per
save, so review comments keep pointing at the text they were made on.

Paths missing from the scratchpad are kept: the scratchpad has no delete
tool, so a missing path means the state was trimmed (caps), not a decision.

Functions here take only a session id: the caller has already loaded the
session through ``store.get_owned_session`` (owner check).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agents.db import SessionLocal
from agents.deep import (
    DeepState,
    ScratchpadError,
    TodoItem,
    WorkspaceScope,
    current_workspace,
)
from agents.ide.models import IdeFileRevision, IdeSession, IdeWorkspaceFile
from agents.ide.paths import clean_workspace_path, object_for
from agents.ide.store import lock_session_row, touch_session

log = logging.getLogger(__name__)

_TODOS = TypeAdapter(list[TodoItem])
_MAX_OBJECT_NAME = 40  # IdeWorkspaceFile.object_name column length


class RevisionConflict(RuntimeError):
    """A file revision this save wanted to write already exists.

    The session's run lock (``IdeSession.status``) is the guard: only one
    run, and so one save, writes a session's workspace at a time, so this
    means that guard was bypassed. The save is rolled back as a whole.
    """


def run_id_for(sid: str) -> str:
    return f"ide:{sid}"


def _parse_todos(raw: str | None, sid: str) -> list[TodoItem]:
    if not raw:
        return []
    try:
        return _TODOS.validate_json(raw)
    except ValidationError:
        log.warning("IDE session %s: unreadable todos_json ignored", sid)
        return []


async def _rows(db: AsyncSession, sid: str) -> list[IdeWorkspaceFile]:
    result = await db.execute(
        select(IdeWorkspaceFile)
        .where(IdeWorkspaceFile.session_id == sid)
        .order_by(IdeWorkspaceFile.path)
    )
    return list(result.scalars().all())


async def load_state(db: AsyncSession, sid: str) -> DeepState:
    """A fresh DeepState holding the session's files and todos."""
    state = DeepState(run_id=run_id_for(sid))
    for row in await _rows(db, sid):
        content = (
            row.proposed_source if row.proposed_source is not None else row.origin_source
        )
        if content is None:
            continue
        try:
            state.put(row.path, content)
        except ScratchpadError as exc:
            log.warning("IDE session %s: %s not loaded: %s", sid, row.path, exc)
    session = await db.get(IdeSession, sid)
    if session is not None:
        state.todos = _parse_todos(session.todos_json, sid)
    return state


def _object_columns(path: str) -> tuple[str | None, str | None]:
    obj = object_for(path)
    if obj is None or len(obj[1]) > _MAX_OBJECT_NAME:
        return None, None
    return obj[0], obj[1]


def _cleaned_files(sid: str, state: DeepState) -> dict[str, str]:
    """Scratchpad files keyed by their cleaned workspace path.

    Invalid paths are logged and dropped. When two raw paths clean to the
    same one, the already-clean key wins (it is the one loaded from the DB).
    """
    out: dict[str, str] = {}
    for raw, content in state.files.items():
        try:
            path = clean_workspace_path(raw)
        except ValueError as exc:
            log.warning("IDE session %s: %r not saved: %s", sid, raw, exc)
            continue
        if path not in out or raw == path:
            out[path] = content
    return out


async def _latest_revision_source(
    db: AsyncSession, sid: str, row: IdeWorkspaceFile
) -> str | None:
    """``proposed_source`` of the row's latest revision (None: there is none)."""
    if row.revision < 1:
        return None
    return (
        await db.execute(
            select(IdeFileRevision.proposed_source).where(
                IdeFileRevision.session_id == sid,
                IdeFileRevision.path == row.path,
                IdeFileRevision.revision == row.revision,
            )
        )
    ).scalar_one_or_none()


async def save_state(
    db: AsyncSession, sid: str, state: DeepState, run_id: str | None = None
) -> list[dict]:
    """Write the state back; return ``[{path, state, revision, base_status}]``
    for changed files.

    A changed file whose proposal differs from its latest revision (or that
    has none yet) gets a new ``IdeFileRevision`` tagged with ``run_id``:
    comments point at a revision, so the text they were written on must
    stay readable after the next run rewrites the file. An unchanged file,
    a revert to the origin and a proposal equal to the latest revision add
    nothing. Scratch notes get revisions too (they are reviewable). The
    session's run lock keeps a second save from racing this one; if it
    happens anyway, the unique revision key turns it into
    ``RevisionConflict`` and nothing of this save is written.

    A session deleted mid-run is not resurrected: nothing is written.

    The session row is locked first (``store.lock_session_row``), then the
    file rows: the order every other writer of these rows uses (lint,
    refresh, open), so none of them can deadlock with a save on Postgres.
    """
    await lock_session_row(db, sid)
    session = await db.get(IdeSession, sid)
    if session is None:
        log.warning("IDE session %s: gone before its workspace was saved", sid)
        return []
    try:
        run_tag = run_id[:36] if run_id else None
        existing = {row.path: row for row in await _rows(db, sid)}
        changed: list[dict] = []
        for path, content in sorted(_cleaned_files(sid, state).items()):
            row = existing.get(path)
            if row is None:
                object_type, object_name = _object_columns(path)
                row = IdeWorkspaceFile(
                    session_id=sid,
                    path=path,
                    object_type=object_type,
                    object_name=object_name,
                    origin_source=None,
                    proposed_source=content,
                    state="new",
                    revision=1,
                )
                db.add(row)
                db.add(IdeFileRevision(
                    session_id=sid, path=path, revision=1,
                    proposed_source=content, run_id=run_tag,
                ))
                changed.append(
                    {"path": path, "state": "new", "revision": 1, "base_status": None}
                )
                continue

            if row.origin_source is None:
                new_state, proposed = "new", content
            elif content == row.origin_source:
                new_state, proposed = "read", None
            else:
                new_state, proposed = "modified", content
            if row.state != new_state or row.proposed_source != proposed:
                row.state = new_state
                row.proposed_source = proposed
                if proposed is not None and proposed != await _latest_revision_source(
                    db, sid, row
                ):
                    row.revision = (row.revision or 0) + 1
                    db.add(IdeFileRevision(
                        session_id=sid, path=path, revision=row.revision,
                        proposed_source=proposed, run_id=run_tag,
                    ))
                changed.append({
                    "path": path,
                    "state": new_state,
                    "revision": row.revision,
                    "base_status": row.base_status,
                })

        if changed:
            # Purge reads updated_at as the activity clock; file edits count.
            await touch_session(db, sid)
        todos_json = (
            json.dumps([t.model_dump() for t in state.todos]) if state.todos else None
        )
        if session.todos_json != todos_json:
            session.todos_json = todos_json
        await db.commit()
    except IntegrityError:
        # uq_ide_file_revision (or the file's own path key): a second writer
        # saved the same revision or path. No exception text or chaining:
        # the statement parameters hold source.
        await db.rollback()
        log.error(
            "IDE session %s: file revision or path already exists; workspace save "
            "rolled back (concurrent save despite the run lock)", sid,
        )
        raise RevisionConflict(
            f"IDE session {sid}: concurrent workspace save, revision conflict"
        ) from None
    return changed


async def _load(sid: str) -> DeepState:
    async with SessionLocal() as db:
        return await load_state(db, sid)


async def _save(sid: str, state: DeepState, run_id: str | None = None) -> list[dict]:
    async with SessionLocal() as db:
        return await save_state(db, sid, state, run_id=run_id)


DEFAULT_SAVE_TIMEOUT_S = 30.0

# The one session type whose workspace is written back. Everything else --
# ``diagnose`` and any type added later -- stays in memory until reviewed.
_PERSISTED_SESSION_TYPE = "change"


def save_timeout() -> float:
    """``IDE_SAVE_TIMEOUT_S``: how long a cancelled run waits for its save."""
    raw = os.environ.get("IDE_SAVE_TIMEOUT_S", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_SAVE_TIMEOUT_S
    except ValueError:
        return DEFAULT_SAVE_TIMEOUT_S
    return value if value > 0 else DEFAULT_SAVE_TIMEOUT_S


async def _save_to_completion(
    sid: str, state: DeepState, run_id: str | None = None
) -> tuple[bool, list[dict]]:
    """Run the save in its own task and wait for it to finish.

    Returns ``(cancelled, changed)``. A cancellation while the save runs is
    absorbed so the save is not cut short, and ``cancelled`` tells the caller
    to re-raise it. Once cancelled, the wait is bounded by
    :func:`save_timeout`: a hung database call must not make the run
    uncancellable, so on timeout the save is abandoned (cancelled and logged)
    and ``(True, [])`` returned. The save's own error propagates.
    """
    task = asyncio.ensure_future(_save(sid, state, run_id))
    try:
        return False, await asyncio.shield(task)
    except asyncio.CancelledError:
        if task.done():  # the save itself was cancelled
            raise
    loop = asyncio.get_running_loop()
    deadline = loop.time() + save_timeout()
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            task.cancel()
            log.error(
                "IDE session %s: workspace save did not finish within %.0fs "
                "after the run was cancelled; abandoned",
                sid, save_timeout(),
            )
            return True, []
        try:
            done, _ = await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            continue  # yet another cancel: keep waiting, within the bound
        if done:
            return True, task.result()


@asynccontextmanager
async def bound_workspace(
    sid: str,
    *,
    allow_subagents: bool,
    request_limit: int | None,
    session_type: str = "change",
    persist: bool | None = None,
    run_id: str | None = None,
) -> AsyncIterator[WorkspaceScope]:
    """Bind the session's workspace on ``current_workspace`` for one run.

    The state is saved when the block ends, also on error and cancellation,
    and before the binding is reset: a generator finalised in another
    Context (SSE disconnect, ``aclose``) makes ``reset`` raise, which must
    never skip the save. A cancel arriving during the save waits for the
    save (at most ``IDE_SAVE_TIMEOUT_S``) and is re-raised afterwards. A save
    failure after a failing block is logged and the block's error wins. The
    save's ``[{path, state}]`` list is left on ``scope.changed_files``.

    ``session_type`` lands on ``scope.session_type`` and selects the
    read-only policy (``agents.ide.readonly``). It also decides whether the
    state is saved: only a ``change`` session persists by default. A
    ``diagnose`` scratchpad may hold dump or trace text, which must never
    reach ``ide_workspace_files`` (plan 1c, D2), so it is never saved and
    ``persist=True`` with that type raises ``ValueError`` instead of being
    honoured. ``persist=False`` skips the save (files and todos) for any
    type and leaves ``changed_files`` empty.
    """
    if persist is None:
        persist = session_type == _PERSISTED_SESSION_TYPE
    elif persist and session_type != _PERSISTED_SESSION_TYPE:
        raise ValueError(
            f"an IDE session of type {session_type!r} never persists its "
            "workspace (diagnose data must not be stored)"
        )
    scope = WorkspaceScope(
        session_id=sid,
        state=await _load(sid),
        allow_subagents=allow_subagents,
        request_limit=request_limit,
        session_type=session_type,
    )
    token = current_workspace.set(scope)
    failed = False
    try:
        yield scope
    except BaseException:
        failed = True
        raise
    finally:
        cancelled_again = False
        save_error: BaseException | None = None
        if persist:
            try:
                cancelled_again, scope.changed_files = await _save_to_completion(
                    sid, scope.state, run_id
                )
            except BaseException as exc:  # noqa: BLE001 -- re-raised below
                save_error = exc
        try:
            current_workspace.reset(token)
        except ValueError:
            # Token from another Context; that Context's binding dies with it.
            log.debug("IDE session %s: workspace reset in a foreign context", sid)
        if save_error is not None:
            if not failed:
                raise save_error
            log.error(
                "IDE session %s: workspace save failed", sid, exc_info=save_error
            )
        elif cancelled_again and not failed:
            raise asyncio.CancelledError()
