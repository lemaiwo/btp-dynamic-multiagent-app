"""HTTP routes of personal agents and skills, the bundle and the admin
catalogue: ``/chat/api/catalog`` and ``/chat/api/my/...``.

Every route is behind ``require_user`` and owner-scoped (``store.caller``):
another user's agent or skill is a 404. A refused body is the app's 422
without the input (``agents/validation_errors.py``); a reference the caller
may not use is a 422 of the same shape, naming the field and index, never
the value. Other refusals are ``{"detail": "<fixed text>"}``.

The catalogue shows what the admin configured, read-only and by name and
description only: chat-usable admin agents (``user_agents.chat_usable``) and
every admin skill; never instructions, servers or skill content.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from agents.auth import require_user
from agents.db import SessionLocal, SkillConfig
from agents.multipart_form import FormRefused, FormSpec, read_multipart
from agents.webchat import my_models as m
from agents.webchat import my_store, run, store
from agents.webchat import user_agents as ua

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat/api", tags=["webchat"], dependencies=[Depends(require_user)])

CATALOG_DESCRIPTION_CHARS = 300
ORCHESTRATOR_DESCRIPTION = (
    "Routes each request to the right specialist and combines their answers."
)
AGENT_NOT_FOUND = "No such agent"
SKILL_NOT_FOUND = "No such skill"
AGENT_EXISTS = "an agent of this name exists"
SKILL_EXISTS = "a skill of this name exists"
AGENT_LIMIT = f"You have reached the limit of {m.MAX_AGENTS} personal agents"
SKILL_LIMIT = f"You have reached the limit of {m.MAX_SKILLS} personal skills"
MSG_REF_SKILL = "Value error, skills: not a skill you can attach"
MSG_REF_CONSULT = "Value error, consults: not an agent you may consult"
MSG_MODEL = "Value error, model: not an offered model"
BAD_IDS = "agents and skills must be comma-separated ids"
IMPORT_NOT_JSON = "The file is not a webchat bundle"
IMPORT_FORMAT = "The bundle is not of the format webchat-bundle/1"
IMPORT_TOO_MANY = (f"A bundle holds at most {m.MAX_AGENTS} agents and "
                   f"{m.MAX_SKILLS} skills")
IMPORT_TOO_LARGE = "The bundle is larger than 1 MiB"
IMPORT_NO_FILE = "No bundle file was sent"
IMPORT_TYPE = "Send the bundle as multipart/form-data (file) or as application/json"
IMPORT_CONFLICT_RULE = "on_conflict must be rename, replace or skip"
IMPORT_COLLIDED = "The import collided with a concurrent change; nothing was written"
MAX_EXPORT_IDS = m.MAX_AGENTS + m.MAX_SKILLS

_IMPORT_SPEC = FormSpec(
    text_fields={"on_conflict": 16},
    max_files=1,
    max_file_bytes=m.BUNDLE_MAX_BYTES,
    file_fields=("file",),
    msg_not_multipart=IMPORT_TYPE,
    msg_malformed="The request body is not a well-formed multipart form",
    msg_disconnected="The request body was not received completely",
    msg_unexpected_field="The form has a field the import does not take",
    msg_body_too_large=IMPORT_TOO_LARGE,
    msg_file_too_large=IMPORT_TOO_LARGE,
    msg_text_too_large="on_conflict is too long",
    msg_too_many_files="Send one bundle file at a time",
)


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------
def _catalog_text(text: str | None) -> str:
    return store.one_line(text or "", CATALOG_DESCRIPTION_CHARS)


@router.get("/catalog")
async def catalog() -> dict[str, Any]:
    async with SessionLocal() as db:
        agents = await ua.chat_agents(db)
        skills = (await db.execute(
            select(SkillConfig.name, SkillConfig.description).order_by(SkillConfig.name)
        )).all()
    return {
        "agents": [
            {"ref": f"{ua.ADMIN_PREFIX}{name}", "name": name,
             "description": _catalog_text(row.description),
             "source": "admin", "readonly": True}
            for name, row in sorted(agents.items())
        ],
        "skills": [
            {"name": name, "description": _catalog_text(description),
             "source": "admin", "readonly": True}
            for name, description in skills
        ],
        "orchestrator": {"ref": None, "name": ua.ORCHESTRATOR_NAME,
                         "description": ORCHESTRATOR_DESCRIPTION},
    }


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------
def _refused(problems: list[tuple[tuple[Any, ...], str]]) -> RequestValidationError:
    messages = {"skills": MSG_REF_SKILL, "consults": MSG_REF_CONSULT, "model": MSG_MODEL}
    return RequestValidationError([
        {"loc": ("body", *loc), "msg": messages[kind], "type": "value_error"}
        for loc, kind in problems
    ])


async def _checked_refs(db: Any, principal: str, body: m.AgentIn) -> my_store.Refs:
    refs = await my_store.resolve_refs(db, principal, body)
    if body.model is not None and body.model not in await run.catalog.names():
        refs.problems.append((("model",), "model"))
    if refs.problems:
        raise _refused(refs.problems)
    return refs


@router.get("/my/agents")
async def list_my_agents() -> list[dict[str, Any]]:
    async with SessionLocal() as db:
        rows = await my_store.list_agents(db, store.caller())
    return [my_store.agent_out(r) for r in rows]


@router.post("/my/agents", status_code=201)
async def create_my_agent(body: m.AgentIn) -> dict[str, Any]:
    principal = store.caller()
    async with SessionLocal() as db:
        if await my_store.agent_count(db, principal) >= m.MAX_AGENTS:
            raise HTTPException(status_code=422, detail=AGENT_LIMIT)
        refs = await _checked_refs(db, principal, body)
        try:
            row = await my_store.create_agent(db, principal, body, refs)
        except (my_store.NameTaken, IntegrityError):
            raise HTTPException(status_code=409, detail=AGENT_EXISTS) from None
        return my_store.agent_out(row)


async def _my_agent(db: Any, principal: str, aid: str) -> Any:
    row = await my_store.get_agent(db, principal, aid)
    if row is None:
        raise HTTPException(status_code=404, detail=AGENT_NOT_FOUND)
    return row


@router.get("/my/agents/{aid}")
async def get_my_agent(aid: str) -> dict[str, Any]:
    async with SessionLocal() as db:
        return my_store.agent_out(await _my_agent(db, store.caller(), aid))


@router.put("/my/agents/{aid}")
async def replace_my_agent(aid: str, body: m.AgentIn) -> dict[str, Any]:
    principal = store.caller()
    async with SessionLocal() as db:
        row = await _my_agent(db, principal, aid)
        refs = await _checked_refs(db, principal, body)
        try:
            row = await my_store.replace_agent(db, row, body, refs)
        except (my_store.NameTaken, IntegrityError):
            raise HTTPException(status_code=409, detail=AGENT_EXISTS) from None
        return my_store.agent_out(row)


@router.delete("/my/agents/{aid}", status_code=204)
async def delete_my_agent(aid: str) -> Response:
    principal = store.caller()
    async with SessionLocal() as db:
        await my_store.delete_agent(db, await _my_agent(db, principal, aid))
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------
@router.get("/my/skills")
async def list_my_skills() -> list[dict[str, Any]]:
    async with SessionLocal() as db:
        rows = await my_store.list_skills(db, store.caller())
    return [my_store.skill_out(r) for r in rows]


@router.post("/my/skills", status_code=201)
async def create_my_skill(body: m.SkillIn) -> dict[str, Any]:
    principal = store.caller()
    async with SessionLocal() as db:
        if await my_store.skill_count(db, principal) >= m.MAX_SKILLS:
            raise HTTPException(status_code=422, detail=SKILL_LIMIT)
        try:
            row = await my_store.create_skill(db, principal, body)
        except (my_store.NameTaken, IntegrityError):
            raise HTTPException(status_code=409, detail=SKILL_EXISTS) from None
        return my_store.skill_out(row)


async def _my_skill(db: Any, principal: str, sid: str) -> Any:
    row = await my_store.get_skill(db, principal, sid)
    if row is None:
        raise HTTPException(status_code=404, detail=SKILL_NOT_FOUND)
    return row


@router.get("/my/skills/{sid}")
async def get_my_skill(sid: str) -> dict[str, Any]:
    async with SessionLocal() as db:
        return my_store.skill_out(await _my_skill(db, store.caller(), sid))


@router.put("/my/skills/{sid}")
async def replace_my_skill(sid: str, body: m.SkillIn) -> dict[str, Any]:
    principal = store.caller()
    async with SessionLocal() as db:
        row = await _my_skill(db, principal, sid)
        try:
            row = await my_store.replace_skill(db, row, body)
        except (my_store.NameTaken, IntegrityError):
            raise HTTPException(status_code=409, detail=SKILL_EXISTS) from None
        return my_store.skill_out(row)


@router.delete("/my/skills/{sid}", status_code=204)
async def delete_my_skill(sid: str) -> Response:
    principal = store.caller()
    async with SessionLocal() as db:
        await my_store.delete_skill(db, await _my_skill(db, principal, sid))
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Export / import
# ---------------------------------------------------------------------------
def _ids(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    ids = [part.strip() for part in raw.split(",") if part.strip()]
    if len(ids) > MAX_EXPORT_IDS or any(len(i) > 36 for i in ids):
        raise HTTPException(status_code=422, detail=BAD_IDS)
    return list(dict.fromkeys(ids))


@router.get("/my/export")
async def export(
    agents: str | None = Query(default=None, max_length=8000),
    skills: str | None = Query(default=None, max_length=8000),
) -> Response:
    agent_ids, skill_ids = _ids(agents), _ids(skills)
    async with SessionLocal() as db:
        bundle = await my_store.export_bundle(db, store.caller(), agent_ids=agent_ids,
                                              skill_ids=skill_ids)
    filename = f"webchat-bundle-{date.today().isoformat()}.json"
    return Response(
        content=json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8"),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


async def _json_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > m.BUNDLE_MAX_BYTES:
        raise HTTPException(status_code=413, detail=IMPORT_TOO_LARGE)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > m.BUNDLE_MAX_BYTES:
            raise HTTPException(status_code=413, detail=IMPORT_TOO_LARGE)
    return bytes(body)


async def _bundle_bytes(request: Request) -> tuple[bytes, str | None]:
    """The bundle and the form's ``on_conflict`` (``None`` when not sent)."""
    ctype = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if ctype == "application/json":
        return await _json_body(request), None
    if ctype != "multipart/form-data":
        raise HTTPException(status_code=415, detail=IMPORT_TYPE)
    try:
        parts = await read_multipart(request, _IMPORT_SPEC, label="webchat import")
    except FormRefused as refused:
        raise HTTPException(status_code=refused.status, detail=refused.detail) from None
    data: bytes | None = None
    rule: str | None = None
    for part in parts:
        if part.filename is not None:
            data = bytes(part.data)
        elif part.name == "on_conflict":
            rule = bytes(part.data).decode("utf-8", "replace").strip()
    if data is None:
        raise HTTPException(status_code=422, detail=IMPORT_NO_FILE)
    return data, rule


def _parse_bundle(data: bytes) -> dict[str, Any]:
    try:
        bundle = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise HTTPException(status_code=422, detail=IMPORT_NOT_JSON) from None
    if not isinstance(bundle, dict):
        raise HTTPException(status_code=422, detail=IMPORT_NOT_JSON)
    if bundle.get("format") != m.BUNDLE_FORMAT:
        raise HTTPException(status_code=422, detail=IMPORT_FORMAT)
    agents, skills = bundle.get("agents", []), bundle.get("skills", [])
    if agents is None:
        agents = []
    if skills is None:
        skills = []
    if not isinstance(agents, list) or not isinstance(skills, list):
        raise HTTPException(status_code=422, detail=IMPORT_FORMAT)
    if len(agents) > m.MAX_AGENTS or len(skills) > m.MAX_SKILLS:
        raise HTTPException(status_code=422, detail=IMPORT_TOO_MANY)
    return {"agents": agents, "skills": skills}


@router.post("/my/import")
async def import_bundle(
    request: Request,
    on_conflict: Literal["rename", "replace", "skip"] | None = Query(default=None),
) -> dict[str, Any]:
    data, form_rule = await _bundle_bytes(request)
    rule = form_rule or on_conflict or "rename"
    if rule not in my_store.CONFLICT_RULES:
        raise HTTPException(status_code=422, detail=IMPORT_CONFLICT_RULE)
    bundle = _parse_bundle(data)
    models = set(await run.catalog.names())
    async with SessionLocal() as db:
        try:
            return await my_store.import_bundle(db, store.caller(), bundle,
                                                on_conflict=rule, models=models)
        except IntegrityError:
            await db.rollback()
            raise HTTPException(status_code=409, detail=IMPORT_COLLIDED) from None
