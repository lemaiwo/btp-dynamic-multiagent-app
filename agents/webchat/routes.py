"""HTTP routes of the web chat: ``GET /chat`` and ``/chat/api/...``.

Every route is behind ``require_user`` and owner-scoped by the caller's
principal (``store.caller``): a conversation or file of another user is a
404, never a 403. Errors are ``{"detail": "<fixed text>"}``; a refused
request body is the app's 422 without the input
(``agents/validation_errors.py``). This module holds request handling only:
persistence is ``store``, the run is ``run``, the form is ``uploads``.

State-changing requests under ``/chat/api`` are covered by the app
middleware's Origin check (``app.py``), as ``/admin`` is.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from agents.auth import current_principal, require_user
from agents.db import SessionLocal
from agents.multipart_form import FormRefused
from agents.webchat import run, store, uploads

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat/api", tags=["webchat"], dependencies=[Depends(require_user)])
page_router = APIRouter(tags=["webchat"])

TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

APP_NAME = "SAP BTP Multi-Agent"
UPLOAD_TIMEOUT_SECONDS = 60.0
INLINE_TYPES = frozenset({uploads.PNG, uploads.JPEG, uploads.PDF})

NOT_FOUND = "No such conversation"
FILE_NOT_FOUND = "No such file"
RUNNING = "a run is in progress"
NOT_RUNNING = "not_running"
UNKNOWN_MODEL = "Unknown model"
BAD_BEFORE = "before must be a timestamp with a time zone"
FULL = "The conversation has reached its message limit"
UPLOAD_TIMEOUT = "The request body was not received in time"


class ConversationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=1000)
    model: str | None = Field(default=None, max_length=128)


@page_router.get("/chat", response_class=HTMLResponse, dependencies=[Depends(require_user)])
async def chat_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "webchat.html")


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail=NOT_FOUND)


async def _owned(cid: str, principal: str) -> Any:
    async with SessionLocal() as db:
        conv = await store.get_conversation(db, cid, principal)
    if conv is None:
        raise _not_found()
    return conv


async def _checked_model(name: str | None) -> str | None:
    """The model id as stored (``None`` = the default); 422 when unknown."""
    if name is None:
        return None
    try:
        await run.catalog.check(name)
    except run.UnknownModel:
        raise HTTPException(status_code=422, detail=UNKNOWN_MODEL) from None
    return name


@router.get("/me")
async def me() -> dict[str, Any]:
    return {
        "principal": current_principal.get() or store.LOCAL_PRINCIPAL,
        "models": await run.catalog.listing(),
        "limits": {
            "max_file_bytes": uploads.MAX_FILE_BYTES,
            "max_files": uploads.MAX_FILES,
            "media_types": list(uploads.MEDIA_TYPES),
        },
        "app_name": APP_NAME,
    }


def _before(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=BAD_BEFORE) from None
    if stamp.tzinfo is None:
        raise HTTPException(status_code=422, detail=BAD_BEFORE)
    return stamp.astimezone(timezone.utc)


@router.get("/conversations")
async def list_conversations(
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=store.DEFAULT_LIST, ge=1, le=store.MAX_LIST),
    before: str | None = Query(default=None, max_length=64),
) -> list[dict[str, Any]]:
    principal = store.caller()
    cutoff = _before(before)
    async with SessionLocal() as db:
        rows = await store.list_conversations(db, principal, q=(q or "").strip() or None,
                                              limit=limit, before=cutoff)
    return [
        {k: v for k, v in store.conversation_out(c).items() if k != "model"}
        for c in rows
    ]


@router.post("/conversations", status_code=201)
async def create_conversation(body: ConversationIn) -> dict[str, Any]:
    principal = store.caller()
    model = await _checked_model(body.model)
    async with SessionLocal() as db:
        conv = await store.create_conversation(db, principal, title=body.title, model=model)
    return store.conversation_out(conv)


@router.get("/conversations/{cid}")
async def get_conversation(cid: str) -> dict[str, Any]:
    principal = store.caller()
    async with SessionLocal() as db:
        conv = await store.get_conversation(db, cid, principal)
        if conv is None:
            raise _not_found()
        return await store.conversation_detail(db, conv)


@router.patch("/conversations/{cid}")
async def patch_conversation(cid: str, body: ConversationIn) -> dict[str, Any]:
    principal = store.caller()
    sent = body.model_fields_set
    model = await _checked_model(body.model) if "model" in sent else None
    async with SessionLocal() as db:
        conv = await store.get_conversation(db, cid, principal)
        if conv is None:
            raise _not_found()
        conv = await store.update_conversation(
            db, conv,
            title=body.title if "title" in sent and body.title is not None else None,
            model=model, clear_model="model" in sent and body.model is None,
        )
        return store.conversation_out(conv)


@router.delete("/conversations/{cid}", status_code=204)
async def delete_conversation(cid: str) -> Response:
    principal = store.caller()
    conv = await _owned(cid, principal)
    async with SessionLocal() as db:
        deleted = await store.delete_conversation(db, conv.id, principal)
    if not deleted:
        raise HTTPException(status_code=409, detail=RUNNING)
    return Response(status_code=204)


async def _release(cid: str) -> None:
    async with SessionLocal() as db:
        await store.release_run(db, cid)


@router.post("/conversations/{cid}/messages")
async def post_message(cid: str, request: Request) -> Response:
    principal = store.caller()
    conv = await _owned(cid, principal)
    async with SessionLocal() as db:
        if not await store.claim_run(db, conv.id, principal):
            return JSONResponse(status_code=409, content={"detail": RUNNING})
    started = False
    try:
        try:
            async with asyncio.timeout(UPLOAD_TIMEOUT_SECONDS):
                form = await uploads.read_chat_form(request)
        except FormRefused as refused:
            raise HTTPException(status_code=refused.status, detail=refused.detail) from None
        except TimeoutError:
            raise HTTPException(status_code=408, detail=UPLOAD_TIMEOUT) from None
        # The model of this turn: the form's, else the conversation's, both
        # checked before anything is stored (422 when no longer offered).
        picked = await _checked_model(form.model)
        model_name = await run.catalog.check(picked or conv.model_name)
        async with SessionLocal() as db:
            if await store.message_count(db, conv.id) + 2 > store.MAX_MESSAGES:
                raise HTTPException(status_code=422, detail=FULL)
            if picked is not None and picked != conv.model_name:
                conv = await store.update_conversation(db, conv, model=picked)
            rows = await store.start_turn(
                db, conv, form.text, [(u.name, u.media_type, u.data) for u in form.uploads]
            )
            history = run.load_history(await store.history_json(db, conv.id))
        data = run.TurnInput(
            conversation_id=conv.id,
            assistant_message_id=rows.assistant_message_id,
            principal=principal,
            prompt=run.build_prompt(form.text, form.uploads),
            history=history,
            model_name=model_name,
            title=store.title_from(form.text) if conv.title == store.DEFAULT_TITLE else None,
        )
        turn, queue = run.start_turn(data, {
            "user_message_id": rows.user_message_id,
            "assistant_message_id": rows.assistant_message_id,
            "files": rows.files,
        })
        started = True
    except run.UnknownModel:
        # The conversation's stored model is no longer offered.
        raise HTTPException(status_code=422, detail=UNKNOWN_MODEL) from None
    finally:
        if not started:
            await _release(conv.id)
    return StreamingResponse(run.sse(turn, queue), media_type="text/event-stream",
                             headers=run.SSE_HEADERS)


@router.post("/conversations/{cid}/cancel")
async def cancel(cid: str) -> dict[str, bool]:
    principal = store.caller()
    conv = await _owned(cid, principal)
    return {"canceled": run.cancel_turn(conv.id)}


@router.get("/conversations/{cid}/stream")
async def reattach(cid: str) -> Response:
    principal = store.caller()
    conv = await _owned(cid, principal)
    turn = run.live_turn(conv.id)
    if turn is None:
        return JSONResponse(status_code=409, content={"detail": NOT_RUNNING})
    queue = turn.subscribe()
    return StreamingResponse(run.sse(turn, queue), media_type="text/event-stream",
                             headers=run.SSE_HEADERS)


@router.get("/files/{fid}")
async def download(fid: str, inline: int = Query(default=0, ge=0, le=1)) -> Response:
    principal = store.caller()
    async with SessionLocal() as db:
        row = await store.get_file(db, fid, principal)
    if row is None:
        raise HTTPException(status_code=404, detail=FILE_NOT_FOUND)
    disposition = "inline" if inline and row.media_type in INLINE_TYPES else "attachment"
    return Response(
        content=row.data,
        media_type=row.media_type,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(row.name, safe='')}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )
