"""Agent tools of an IDE session (plan §1.4): ``submit_document``,
``open_object`` and ``resolve_comments``.

Why a tool: a stage document used to be captured from the run's final
answer, so a clarifying question or a half-finished answer became "the
design" the developer could approve. Now a version exists only when an
agent deliberately calls ``submit_document(kind, content)``; approve pins
the version the developer saw (``agents.ide.stages.approve``).

Visibility. :func:`ide_session_toolset` is one ``FunctionToolset`` wrapped
with ``.filtered(...)``. ``registry.build_orchestrator`` attaches it to
every specialist, so delegates of an IDE run have it too; deep sub-agents do
not (they get their parent's MCP servers only). A tool is listed only while
both bindings of an IDE run are present -- the session workspace
(``agents.deep.current_workspace``) and :data:`current_ide_run`, for the
same session -- and the run's stage takes a document
(``stages.document_kind``). Chat, A2A, jobs and workflows bind neither, so
for them the toolset is empty. The tool re-checks all of it when called: a
model that names a tool it was not shown gets ``Error:``, never a write.

``resolve_comments`` closes the review loop of a request-changes run: the
comments the run sent (``sent``) become ``addressed`` with the agent's
one-line answer. Only a ``sent`` comment of this session can be resolved,
and since a run returns what it left ``sent`` to ``open`` when it ends, that
means a comment of the request-changes run in flight -- so the tool is
listed only in a request-changes run of a change session (narrower than
plan §1.4's "change sessions": elsewhere it could never succeed). The
answer is model output stored as plain text (control characters stripped,
at most 500 characters) and shown to the developer as text.

``open_object`` brings an object into a change session's workspace with its
SAP base: the source read from SAP as the signed-in user
(``arc1.get_arc1_client(target, destination, policy="change")``, so the
read-only check runs first and nothing is written to SAP), the version
marker (``arc1.read_version``; ``None`` when unknown) and
``base_status="sap"``. A proposal already in the workspace -- stored, or
written earlier in this run -- is kept; only its base moves. A diagnose
session never sees it: its workspace is never stored.

Data, not instructions: the content is stored verbatim as model-written
text and later rendered inside the "session documents" data section of a
prompt (``stages._documents``), never into the instructions.

The session comes from the bindings, never from an argument, and the tool
writes in its own ``SessionLocal()`` after loading the session through
``store.get_owned_session`` (owner check) and checking that the session's
``run_id`` is still the bound run's: a run whose lock was reaped writes
nothing.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, Field
from pydantic_ai import RunContext
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset
from sqlalchemy import select

from agents.db import SessionLocal
from agents.deep import ScratchpadError, current_workspace
from agents.ide import arc1, basecheck
from agents.ide.models import IdeFileRevision, IdeWorkspaceFile
from agents.ide.paths import EXT_BY_TYPE, path_for
from agents.ide.stages import CHANGE, Stage, based_on, document_kind
from agents.ide.store import (
    add_artifact,
    get_owned_session,
    lock_session_row,
    touch_session,
)
from agents.ide.store import resolve_comments as store_resolve_comments

log = logging.getLogger(__name__)

MAX_DOCUMENT_CHARS = 200_000


@dataclass
class IdeRunContext:
    """What the session tools need to know about the run in progress.

    Set on :data:`current_ide_run` by ``agents.ide.runner._execute`` and
    reset there, in the run task (never across an SSE ``yield``)."""

    sid: str
    owner: str
    target: str
    destination: str
    session_type: str
    stage: str
    report: bool
    run_id: str
    emit: Callable[[str, dict], None]
    # A request-changes run: the only kind that has ``sent`` comments.
    request_changes: bool = False
    # Serialises this run's submissions: parallel tool calls in one model
    # response must not both compute the same next version.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


current_ide_run: ContextVar[IdeRunContext | None] = ContextVar(
    "current_ide_run", default=None
)


def _bound_run() -> IdeRunContext | None:
    """The run, if this context is inside a bound IDE session run."""
    run = current_ide_run.get()
    scope = current_workspace.get()
    if run is None or scope is None or scope.session_id != run.sid:
        return None
    return run


def _kind_for(run: IdeRunContext) -> str | None:
    try:
        stage = Stage(run.stage)
    except ValueError:
        return None
    return document_kind(run.session_type, stage, run.report)


async def submit_document(kind: str, content: str) -> str:
    """Submit this stage's document (design, plan, note, review or report).

    Only a submitted document can be reviewed and approved; your chat answer
    is not one. Submit the whole document each time: every call stores a new
    version.

    Args:
        kind: the document kind this stage takes (named in your instructions).
        content: the complete document in markdown.
    """
    run = _bound_run()
    if run is None:
        return "Error: submit_document is only available in an IDE session."
    expected = _kind_for(run)
    if expected is None:
        return "Error: this stage takes no document."
    if kind != expected:
        return (
            f"Error: this stage takes a {expected!r} document, not {str(kind)[:20]!r}."
        )
    if not isinstance(content, str) or not content.strip():
        return "Error: the document is empty."
    if len(content) > MAX_DOCUMENT_CHARS:
        return (
            f"Error: the document has {len(content)} characters; the limit is "
            f"{MAX_DOCUMENT_CHARS}. Shorten it and submit again."
        )
    async with run.lock:
        try:
            async with SessionLocal() as db:
                session = await get_owned_session(db, run.sid, run.owner)
                if session is None or session.run_id != run.run_id:
                    # The lock was reaped (or taken by a newer run): this run
                    # no longer owns the session.
                    return "Error: this run no longer holds the session."
                if session.stage != run.stage:
                    return "Error: this session is no longer in that stage."
                art = await add_artifact(
                    db, run.sid, stage=run.stage, kind=kind, content=content,
                    based_on=await based_on(db, session, kind),
                )
                event = {"id": art.id, "kind": art.kind, "version": art.version}
        except Exception as exc:  # noqa: BLE001
            # No traceback: SQL parameters would carry the document.
            log.error(
                "IDE session %s: storing a submitted %s failed: %s",
                run.sid, kind, type(exc).__name__,
            )
            return "Error: the document could not be stored; try again."
    try:
        run.emit("artifact", event)
    except Exception:  # noqa: BLE001 -- a closed stream is not the tool's error
        log.debug("IDE session %s: artifact event dropped", run.sid, exc_info=True)
    return f"Submitted {kind} version {event['version']}."


# What ``open_object`` accepts before anything is sent to ARC-1: a plain
# ABAP object name (namespaces included) and, for a class, one of the
# class-local sections ARC-1's ``SAPRead`` knows.
# At least one letter or digit: "/" or "$" alone is no object name.
_OBJECT_NAME = re.compile(r"(?=[A-Z0-9_/$]*[A-Z0-9])[A-Z0-9_/$]{1,40}")
CLASS_SECTIONS = frozenset({"definitions", "implementations", "macros", "testclasses"})
_ERROR_DETAIL_CHARS = 300
# Object files one session may hold: each one is read from SAP again at the
# end of a run when its base is unknown, and shown in the worklist.
MAX_SESSION_OBJECTS = 50


def _invalid(reason: str) -> str:
    return f"Error: invalid_object: {reason}"


def _object_args(
    type_: Any, name: Any, include: Any
) -> tuple[str, str, str | None] | str:
    """``(TYPE, NAME, include)`` cleaned, or the ``Error:`` text."""
    if not isinstance(type_, str) or not isinstance(name, str):
        return _invalid("type and name must be text.")
    t, n = type_.strip().upper(), name.strip().upper()
    if t not in EXT_BY_TYPE:
        return _invalid(
            f"unsupported type {t[:10]!r}; use one of {', '.join(sorted(EXT_BY_TYPE))}."
        )
    if not _OBJECT_NAME.fullmatch(n):
        return _invalid("the name must be an ABAP object name (A-Z, 0-9, _, /, $).")
    section: str | None = None
    if include is not None:
        if not isinstance(include, str):
            return _invalid("include must be text.")
        section = include.strip().lower() or None
        if section is not None and (t != "CLAS" or section not in CLASS_SECTIONS):
            return _invalid(
                "include is only for a class section: "
                + ", ".join(sorted(CLASS_SECTIONS)) + "."
            )
    return t, n, section


async def _too_many_objects(sid: str, path: str) -> bool:
    """Would opening ``path`` take the session past
    :data:`MAX_SESSION_OBJECTS`? A path already in the session never does."""
    async with SessionLocal() as db:
        rows = await db.execute(
            select(IdeWorkspaceFile.path).where(
                IdeWorkspaceFile.session_id == sid,
                IdeWorkspaceFile.object_type.is_not(None),
            )
        )
        held = {p for (p,) in rows.all()}
    return path not in held and len(held) >= MAX_SESSION_OBJECTS


async def open_object(type: str, name: str, include: str | None = None) -> str:
    """Open an ABAP object from SAP into the session workspace.

    Reads the current source from SAP and stores it as the file's base, so
    your changes are reviewed as changes to what is in SAP. Open an object
    before you change it. A proposal you already wrote for it is kept.

    Args:
        type: object type, e.g. CLAS, INTF, PROG, INCL, FUNC, DDLS, DCLS,
            DDLX, BDEF, SRVD.
        name: the object name, e.g. ZCL_EXAMPLE.
        include: only for a class: definitions, implementations, macros or
            testclasses (a class-local section); leave empty otherwise.
    """
    run = _bound_run()
    if run is None or run.session_type != CHANGE:
        return "Error: open_object is only available in an IDE change session."
    parsed = _object_args(type, name, include)
    if isinstance(parsed, str):
        return parsed
    type_, name_, section = parsed
    path = path_for(type_, name_, section)
    try:
        if await _too_many_objects(run.sid, path):
            return (
                f"Error: too_many_objects: this session already holds "
                f"{MAX_SESSION_OBJECTS} objects; work with those, or start a "
                "new session."
            )
    except Exception as exc:  # noqa: BLE001
        # ``type`` is this tool's parameter here, not the builtin.
        log.error("IDE session %s: counting objects failed: %s",
                  run.sid, exc.__class__.__name__)
        return "Error: the workspace could not be read; try again."
    # Looked up on the module at call time: the tests' seam.
    client = arc1.get_arc1_client(run.target, run.destination, policy=CHANGE)
    try:
        source = await client.call(
            "SAPRead", basecheck.read_args(type_, name_, section)
        )
    except arc1.Arc1Error as exc:
        detail = str(exc.message)[:_ERROR_DETAIL_CHARS]
        return f"Error: {exc.code}: {detail}"
    except Exception as exc:  # noqa: BLE001
        # Exception text can carry hosts or URLs: it stays in the log.
        log.warning("IDE session %s: open_object %s failed: %s",
                    run.sid, path, exc.__class__.__name__, exc_info=True)
        return "Error: arc1_unavailable: SAP could not be reached; try again later."
    if not basecheck.usable_source(source):
        return (
            f"Error: no_source: ARC-1 returned no source for {type_} {name_} "
            "(empty, or a not-found message); it is not stored."
        )
    if basecheck.too_large(source):
        return (
            "Error: source_too_large: the object is larger than the workspace "
            "can hold; read the parts you need with the SAP tools instead."
        )
    version = await arc1.read_version(client, type_, name_)
    scope = current_workspace.get()
    async with run.lock:
        try:
            async with SessionLocal() as db:
                # Session row first, then the file row (as lint/refresh/open).
                await lock_session_row(db, run.sid)
                session = await get_owned_session(db, run.sid, run.owner)
                if session is None or session.run_id != run.run_id:
                    return "Error: this run no longer holds the session."
                row = (
                    await db.execute(
                        select(IdeWorkspaceFile).where(
                            IdeWorkspaceFile.session_id == run.sid,
                            IdeWorkspaceFile.path == path,
                        )
                    )
                ).scalar_one_or_none()
                previous_origin = row.origin_source if row is not None else None
                stored_proposal = row is not None and row.proposed_source is not None
                if row is None:
                    row = IdeWorkspaceFile(
                        session_id=run.sid, path=path, object_type=type_,
                        object_name=name_, state="read", revision=0,
                    )
                    db.add(row)
                basecheck.apply_base(row, source, version)
                await touch_session(db, run.sid)
                await db.commit()
                syntax_status = None
                if row.revision:
                    syntax_status = (
                        await db.execute(
                            select(IdeFileRevision.syntax_status).where(
                                IdeFileRevision.session_id == run.sid,
                                IdeFileRevision.path == path,
                                IdeFileRevision.revision == row.revision,
                            )
                        )
                    ).scalar_one_or_none()
                event = {
                    "path": path, "state": row.state, "revision": row.revision or 0,
                    "base_status": row.base_status, "syntax_status": syntax_status,
                }
        except Exception as exc:  # noqa: BLE001
            # No traceback: SQL parameters would carry the source.
            log.error(
                "IDE session %s: storing the base of %s failed: %s",
                run.sid, path, exc.__class__.__name__,
            )
            return "Error: the object could not be stored; try again."
    # The scratchpad: a proposal -- stored, or written this run and not yet
    # saved (content that is not the old base) -- is the agent's work and is
    # kept; otherwise the file now holds SAP's source.
    current = scope.state.files.get(path) if scope is not None else None
    kept = stored_proposal or (current is not None and current != previous_origin)
    note = ""
    if kept:
        note = " Your proposal for it is kept; only its SAP base was updated."
    elif scope is not None:
        try:
            scope.state.put(path, source)
        except ScratchpadError as exc:
            note = f" It could not be loaded into the scratchpad: {exc}"
    try:
        run.emit("file", event)
    except Exception:  # noqa: BLE001 -- a closed stream is not the tool's error
        log.debug("IDE session %s: file event dropped", run.sid, exc_info=True)
    lines = len(source.splitlines())
    return f"Opened {path} ({lines} lines, version {version or 'unknown'}).{note}"


MAX_RESOLVE_ITEMS = 200
_ID_ECHO_CHARS = 40


class CommentAnswer(BaseModel):
    """One resolved comment: its id and a one-line answer."""

    id: str = Field(description="The comment id from the review comments section.")
    answer: str = Field(description="One line: what you changed, or why not.")


def _ids(values: list[str]) -> str:
    return ", ".join(str(v)[:_ID_ECHO_CHARS] for v in values) or "none"


async def resolve_comments(items: list[CommentAnswer]) -> str:
    """Mark review comments you addressed in this run as resolved.

    Call it once you reworked the output, with every comment id from the
    review comments section and a one-line answer saying what you changed
    (or why you did not change anything).

    Any agent of the run may call it -- the top-level agent, a delegate or a
    peer reached by delegation (the session toolset is attached to every
    specialist and bound to the run, not to one agent) -- but only comments
    *this* run sent (``sent_run_id``) can be resolved.

    Args:
        items: one entry per comment: its ``id`` and a one-line ``answer``.
    """
    run = _bound_run()
    if run is None or run.session_type != CHANGE or not run.request_changes:
        return (
            "Error: resolve_comments is only available in a request-changes "
            "run of an IDE change session."
        )
    if not isinstance(items, list) or not items:
        return "Error: name at least one comment id."
    if len(items) > MAX_RESOLVE_ITEMS:
        return f"Error: at most {MAX_RESOLVE_ITEMS} comments per call."
    pairs: list[tuple[Any, Any]] = []
    for item in items:
        if isinstance(item, CommentAnswer):
            pairs.append((item.id, item.answer))
        elif isinstance(item, dict):
            pairs.append((item.get("id"), item.get("answer")))
        else:
            pairs.append((None, None))
    try:
        async with SessionLocal() as db:
            session = await get_owned_session(db, run.sid, run.owner)
            if session is None or session.run_id != run.run_id:
                return "Error: this run no longer holds the session."
            resolved, rejected = await store_resolve_comments(
                db, run.sid, pairs, run_id=run.run_id
            )
    except Exception as exc:  # noqa: BLE001
        # No traceback: SQL parameters would carry the answers.
        log.error(
            "IDE session %s: resolving comments failed: %s",
            run.sid, type(exc).__name__,
        )
        return "Error: the comments could not be updated; try again."
    if resolved:
        try:
            run.emit("comments", {"ids": list(resolved), "state": "addressed"})
        except Exception:  # noqa: BLE001 -- a closed stream is not the tool's error
            log.debug("IDE session %s: comments event dropped", run.sid, exc_info=True)
    out = f"Resolved: {_ids(resolved)}."
    if rejected:
        out += f" Not resolved (not sent or unknown): {_ids(rejected)}."
    return out


def _visible(ctx: RunContext[Any], tool_def: ToolDefinition) -> bool:
    run = _bound_run()
    if run is None:
        return False
    if tool_def.name == "submit_document":
        return _kind_for(run) is not None
    if tool_def.name == "open_object":
        return run.session_type == CHANGE
    if tool_def.name == "resolve_comments":
        return run.session_type == CHANGE and run.request_changes
    return False  # deny by default: a tool added here must say when it shows


def ide_session_toolset() -> AbstractToolset[Any]:
    """The session tools, visible only inside a bound IDE session run."""
    tools: FunctionToolset[Any] = FunctionToolset(id="ide-session")
    tools.add_function(submit_document, takes_ctx=False)
    tools.add_function(open_object, takes_ctx=False)
    tools.add_function(resolve_comments, takes_ctx=False)
    return tools.filtered(_visible)
