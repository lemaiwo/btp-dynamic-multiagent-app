"""Admin API of the OData catalogue: ``/admin/api/odata/...``.

``agents/admin.py`` includes this router under its ``/admin`` prefix. Every
route carries ``require_admin`` itself rather than inheriting it from the
include: a route added here later is then visibly unprotected in review and
caught by ``tests/test_odata_admin_api.py``, instead of depending on how the
router happens to be mounted.

Request bodies are read as raw JSON and validated here, not declared as
pydantic parameters. FastAPI's own 422 echoes each refused ``input``, and a
catalogue field (a path, a destination name) is exactly where a URL with a
credential in it gets pasted by mistake. ``validate_odata_service`` names the
field and the rule, never the value; the same holds for every refusal below.

This module imports ``agents.auth`` and ``agents.db`` only, never
``agents.admin`` (which imports it at the end of the module).
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from agents.auth import require_admin
from agents.db import (
    SessionLocal,
    create_odata_service,
    delete_odata_service,
    get_odata_service,
    list_odata_services,
    odata_service_referrers,
    update_odata_service,
    validate_odata_service,
)
from agents.odata.models import DESTINATION_NAME_RE, SERVICE_NAME_RE

router = APIRouter(prefix="/api/odata", tags=["odata"])

_NOT_FOUND = "Service not found"
_TITLE_MAX = 120  # ODataServicePayload.title
_COPY_SUFFIX = " (copy)"
_MAX_REPORTED_ERRORS = 20


class DuplicateBody(BaseModel):
    """What a copy may differ in: its identity, never its definition."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(pattern=SERVICE_NAME_RE)]
    title: (
        Annotated[
            str, StringConstraints(strip_whitespace=True, min_length=1, max_length=_TITLE_MAX)
        ]
        | None
    ) = None
    destination: Annotated[str, StringConstraints(pattern=DESTINATION_NAME_RE)] | None = None
    user_context: bool | None = None


def _refuse(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


async def _json_object(request: Request) -> dict[str, Any]:
    """The request body as a JSON object, or a 422 that says only that."""
    try:
        data = json.loads(await request.body())
    except (ValueError, RecursionError):
        # ValueError covers JSONDecodeError and a body that is not UTF-8;
        # RecursionError is a body nested deeper than the parser goes.
        raise _refuse("body: expected a JSON object") from None
    if not isinstance(data, dict):
        raise _refuse("body: expected a JSON object")
    return data


def _validated(data: dict[str, Any]) -> dict[str, Any]:
    try:
        return validate_odata_service(data)
    except ValueError as e:
        raise _refuse(str(e)) from None


def _duplicate_body(data: dict[str, Any]) -> DuplicateBody:
    try:
        return DuplicateBody.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        lines = [
            f"{'.'.join(str(part) for part in err['loc']) or 'body'}: {err['msg']}"
            for err in errors[:_MAX_REPORTED_ERRORS]
        ]
        # `from None`: the ValidationError carries the input in its repr.
        raise _refuse("; ".join(lines)) from None


def _copy_title(title: str) -> str:
    """``<title> (copy)``, shortened so it still fits the title limit."""
    return title[: _TITLE_MAX - len(_COPY_SUFFIX)].rstrip() + _COPY_SUFFIX


@router.get("/services", dependencies=[Depends(require_admin)])
async def api_list_odata_services() -> list[dict[str, Any]]:
    async with SessionLocal() as session:
        rows = await list_odata_services(session)
        # One pass over the agents for the whole list.
        used_by = await odata_service_referrers(session)
        return [row.to_summary(used_by.get(row.name)) for row in rows]


@router.post(
    "/services",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def api_create_odata_service(request: Request) -> dict[str, Any]:
    data = _validated(await _json_object(request))
    async with SessionLocal() as session:
        try:
            row = await create_odata_service(session, data)
        except ValueError as e:  # the name is taken
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from None
        used_by = await odata_service_referrers(session, row.name)
        return row.to_dict(used_by.get(row.name))


@router.get("/services/{name}", dependencies=[Depends(require_admin)])
async def api_get_odata_service(name: str) -> dict[str, Any]:
    async with SessionLocal() as session:
        row = await get_odata_service(session, name)
        if row is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        used_by = await odata_service_referrers(session, name)
        return row.to_dict(used_by.get(name))


@router.put("/services/{name}", dependencies=[Depends(require_admin)])
async def api_update_odata_service(name: str, request: Request) -> dict[str, Any]:
    data = _validated(await _json_object(request))
    async with SessionLocal() as session:
        row = await get_odata_service(session, name)
        if row is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        try:
            row = await update_odata_service(session, row, data)
        except ValueError as e:  # a different name in the body
            raise _refuse(str(e)) from None
        used_by = await odata_service_referrers(session, name)
        return row.to_dict(used_by.get(name))


@router.delete(
    "/services/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
async def api_delete_odata_service(name: str) -> None:
    """Delete a service nobody attaches.

    Unlike a skill, a service is not detached from its agents: an agent
    that lost its only service would be saved in a state it cannot run in.
    The admin removes it from the agents first; disabled agents count,
    because turning one back on would break it.
    """
    async with SessionLocal() as session:
        row = await get_odata_service(session, name)
        if row is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        referrers = (await odata_service_referrers(session, name)).get(name, [])
        if referrers:
            agents = ", ".join(f"'{r['agent']}'" for r in referrers)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Service '{name}' is used by agent(s) {agents}",
            )
        await delete_odata_service(session, row)


@router.post(
    "/services/{name}/duplicate",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def api_duplicate_odata_service(name: str, request: Request) -> dict[str, Any]:
    """Copy a service under a new name, e.g. the same definition through a
    technical-user destination for scheduled runs. The copy goes through the
    same gate as a new service."""
    body = _duplicate_body(await _json_object(request))
    async with SessionLocal() as session:
        source = await get_odata_service(session, name)
        if source is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        data = source.to_export()
        data["name"] = body.name
        data["title"] = body.title if body.title is not None else _copy_title(source.title)
        if body.destination is not None:
            data["destination"] = body.destination
        if body.user_context is not None:
            data["user_context"] = body.user_context
        try:
            row = await create_odata_service(session, _validated(data))
        except ValueError as e:  # the name is taken
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from None
        return row.to_dict([])
