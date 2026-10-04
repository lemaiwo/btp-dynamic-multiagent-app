"""Review routes of ``/ide/api``: comments, file revisions, message activity.

Why a module of its own: these routes are the review side of a session
(plan §1.1/§1.2) and were added while ``routes.py`` was being changed in
parallel; they reuse its dependencies and helpers rather than copying them.

Authorization (same rules as ``routes.py``):

- The router depends on ``require_developer``; every route loads the session
  with ``store.get_owned_session`` **first**, so another user's session is
  404 exactly like a missing one.
- Child rows are looked up by id *and* the owned session's id: a comment,
  revision or message of any other session is "not found" (404), never
  "forbidden", so ids cannot be probed across sessions.
- Comment text is user input stored and returned as **plain text** (JSON
  strings; the UI binds it as text). The store strips control characters
  and enforces 1..4000 characters; nothing here interprets it.
- A user can only make the user transitions of the comment state machine:
  ``CommentPatch.state`` accepts ``open`` and ``dismissed`` alone, and
  ``store.set_comment_state`` checks the source state in a conditional
  UPDATE. ``sent`` and ``addressed`` are reached only through the
  request-changes start and the ``resolve_comments`` tool.
- Comments belong to change sessions; a diagnose session refuses new ones
  (409 ``comments_not_allowed``, plan question 7: no comments on reports).

``POST /sessions/{sid}/request-changes`` (optional ``note``) replaces the old
``/revise``: it streams a run like ``.../messages`` (``routes._stream_run``),
which sends the session's open comments to the agent (``runner``). Refusals
come back as JSON before the stream, in the gate's order:
``revise_not_allowed`` (diagnose), ``stage_done``, ``run_in_progress``,
``revise_not_allowed`` (stage), ``usage_exhausted`` (429), then
``nothing_to_send`` (no open comment and no note).

``GET /sessions/{sid}/file`` (also with ``?revision=``) is ``routes.get_file``;
no route here shares a path with ``routes.router``, so the order in which
``app.py`` includes the two routers does not matter.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.auth import require_developer
from agents.ide import paths, runner, store
from agents.ide.models import IdeComment, IdeFileRevision, IdeMessage
from agents.ide.routes import (
    _caller,
    _db,
    _iso,
    _loads,
    _owned,
    _owned_file,
    _stream_run,
    _valid_items,
)
from agents.ide.schemas import (
    SSE_RESPONSES,
    ActivityOut,
    CommentAnchor,
    CommentCreate,
    CommentOut,
    CommentPatch,
    CommentState,
    DocumentKind,
    FileRevisionOut,
    RequestChangesBody,
    TodoOut,
    ToolEventOut,
    coerce_member,
)
from agents.ide.stages import DIAGNOSE

router = APIRouter(prefix="/ide/api", dependencies=[Depends(require_developer)])

# CommentError code -> HTTP status (anything unknown is a 409 refusal).
_COMMENT_STATUS = {
    "invalid_body": 422,
    "invalid_anchor": 422,
    "invalid_quote": 422,
    "comment_not_found": 404,
    "comment_not_editable": 409,
    "invalid_transition": 409,
}
_COMMENT_DETAIL = {
    "invalid_body": "The comment text must be 1 to 4000 characters",
    "invalid_anchor": "The comment does not point at a line range or paragraph "
                      "of this session",
    "invalid_quote": "The quote must be text",
    "comment_not_found": "Comment not found",
    "comment_not_editable": "Only an open comment can be edited or deleted",
    "invalid_transition": "The comment cannot move to that state",
}


def _refusal(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, "code": code})


def _comment_refusal(exc: store.CommentError) -> JSONResponse:
    # A fixed message per code: the exception text is not for clients.
    return _refusal(
        _COMMENT_STATUS.get(exc.code, 409),
        exc.code,
        _COMMENT_DETAIL.get(exc.code, "The comment operation was refused"),
    )


def _comment_out(row: IdeComment) -> CommentOut:
    return CommentOut(
        id=row.id,
        anchor=coerce_member(row.anchor, CommentAnchor, "document", "comment anchor"),
        path=row.path,
        revision=row.revision,
        line_start=row.line_start,
        line_end=row.line_end,
        kind=(
            None if row.kind is None
            else coerce_member(row.kind, DocumentKind, "note", "comment kind")
        ),
        version=row.version,
        paragraph=row.paragraph,
        body=row.body,
        quote=row.quote,
        state=coerce_member(row.state, CommentState, "dismissed", "comment state"),
        answer=row.answer,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


# --- comments ---------------------------------------------------------------


@router.get("/sessions/{sid}/comments", response_model=list[CommentOut])
async def list_comments(
    sid: str,
    state: CommentState | None = Query(default=None),
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    session = await _owned(db, sid, principal)
    return [_comment_out(c) for c in await store.list_comments(db, session.id, state)]


@router.post("/sessions/{sid}/comments", status_code=201, response_model=CommentOut)
async def create_comment(
    sid: str,
    body: CommentCreate,
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    session = await _owned(db, sid, principal)
    if (session.session_type or "") == DIAGNOSE:
        return _refusal(409, "comments_not_allowed",
                        "A diagnose session takes no review comments")
    fields: dict[str, Any] = body.root.model_dump()
    try:
        row = await store.add_comment(db, session.id, **fields)
    except store.CommentError as exc:
        return _comment_refusal(exc)
    return _comment_out(row)


@router.patch("/sessions/{sid}/comments/{cid}", response_model=CommentOut)
async def patch_comment(
    sid: str,
    cid: str,
    body: CommentPatch,
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    session = await _owned(db, sid, principal)
    try:
        if body.state is None:
            # An edit: a quote the request leaves out keeps the stored one,
            # an explicit null clears it.
            extra: dict[str, Any] = (
                {"quote": body.quote} if "quote" in body.model_fields_set else {}
            )
            row = await store.edit_comment(db, session.id, cid, body.body, **extra)
        else:
            row = await store.set_comment_state(db, session.id, cid, body.state)
    except store.CommentError as exc:
        return _comment_refusal(exc)
    return _comment_out(row)


@router.delete("/sessions/{sid}/comments/{cid}", status_code=204)
async def delete_comment(
    sid: str,
    cid: str,
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    session = await _owned(db, sid, principal)
    try:
        await store.delete_comment(db, session.id, cid)
    except store.CommentError as exc:
        return _comment_refusal(exc)
    return Response(status_code=204)


# --- request changes ----------------------------------------------------------


@router.post("/sessions/{sid}/request-changes", response_model=None, responses=SSE_RESPONSES)
async def post_request_changes(
    sid: str,
    body: RequestChangesBody | None = None,
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    """Rerun the stage on the open review comments (SSE, like messages).

    The note is the developer's own text and becomes part of the request;
    the comments go to the model as delimited data (``stages.build_prompt``).
    """
    note = body.note if body is not None else None
    return await _stream_run(
        db, sid, principal, user_text=None,
        request_changes=runner.RequestChanges(note=note),
    )


# --- file revisions ---------------------------------------------------------


@router.get("/sessions/{sid}/file/revisions", response_model=list[FileRevisionOut])
async def list_file_revisions(
    sid: str,
    path: str = Query(default="", max_length=paths.MAX_PATH_LENGTH),
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    """Newest first, without the source text (``chars`` is its length)."""
    session, row = await _owned_file(db, sid, path, principal)
    result = await db.execute(
        select(
            IdeFileRevision.revision,
            IdeFileRevision.run_id,
            IdeFileRevision.created_at,
            func.length(IdeFileRevision.proposed_source),
            IdeFileRevision.syntax_status,
        )
        .where(
            IdeFileRevision.session_id == session.id,
            IdeFileRevision.path == row.path,
        )
        .order_by(IdeFileRevision.revision.desc())
    )
    return [
        FileRevisionOut(
            revision=revision,
            run_id=run_id,
            created_at=_iso(created_at),
            chars=chars or 0,
            syntax_status=status if status in store.SYNTAX_STATUSES else None,
        )
        for revision, run_id, created_at, chars, status in result.all()
    ]


# --- message activity -------------------------------------------------------


@router.get("/sessions/{sid}/messages/{mid}/activity", response_model=ActivityOut)
async def get_message_activity(
    sid: str,
    mid: str,
    principal: str = Depends(_caller),
    db: AsyncSession = Depends(_db),
):
    session = await _owned(db, sid, principal)
    msg = (
        await db.execute(
            select(IdeMessage).where(
                IdeMessage.id == mid, IdeMessage.session_id == session.id
            )
        )
    ).scalar_one_or_none()
    if msg is None:
        return _refusal(404, "message_not_found", "Message not found")
    data = _loads(msg.activity_json)
    if not isinstance(data, dict):
        return _refusal(404, "no_activity", "This message has no run activity")
    dropped = data.get("dropped")
    return ActivityOut(
        events=_valid_items(data.get("events"), ToolEventOut),
        plan=_valid_items(data.get("plan"), TodoOut),
        dropped=dropped if isinstance(dropped, int) and not isinstance(dropped, bool)
        and dropped >= 0 else 0,
    )
