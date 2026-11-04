"""Admin API of the InA analytical queries: ``/admin/api/ina/...``.

Design addendum (k), part 2 point 4. ``agents/admin.py`` includes this router
under its ``/admin`` prefix, as it does the OData catalogue's; every route
carries ``require_admin`` itself, for the reason given in
``agents/odata/admin_routes.py`` (a route added later is visibly unprotected
in review, and ``tests/test_ina_admin.py`` checks every one).

The routes mirror the OData catalogue's: request bodies are read as raw JSON
and validated here through ``validate_ina_query``, never declared as pydantic
parameters (FastAPI's own 422 echoes each refused ``input``, and a
destination field is where a URL with a credential in it gets pasted by
mistake); a refusal names the field and the rule, never a value. The body
reader, the stamp parser and the reload contract are that module's, not
copies.

``Describe`` and ``Test`` are the two routes that reach SAP, through the
stored query's destination and identity (``agents.ina.admin_calls``: as
whom, one clock, nothing kept). What comes back is names and counts, never
a value of SAP's: a failure is the client's closed code
(``agents.ina.errors``) in ``X-Ina-Error`` with its fixed text, SAP's own
cleaned message for ``sap_error``, or the fixed text of a destination
refused as configured (``InaError.admin_text``).

Imports ``agents.auth``, ``agents.db``, the OData admin routes and the
``agents.ina`` modules, never ``agents.admin`` (which imports this module at
its end).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict

from agents.auth import current_principal, require_admin
from agents.db import (
    InaQuery,
    SessionLocal,
    StaleRow,
    _locked_ina_query,
    create_ina_query,
    delete_ina_query,
    get_ina_query,
    ina_query_referrers,
    list_ina_queries,
    update_ina_query,
)
from agents.odata import admin_routes as odata_routes
from agents.odata.admin_routes import (
    EXPECTED_FIELD,
    NOT_RELOADED,
    _expected_updated_at,
    _json_object,
    _model_body,
    _refuse,
)
from agents.odata.preview import plain

from . import admin_calls
from .errors import InaError
from .models import QUERY_NAME_RE, InaQueryPayload, validate_ina_query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ina", tags=["ina"])

_NOT_FOUND = "Query not found"
# A registration is a few short fields plus at most 250 described members of
# a few hundred characters each; checked before anything is parsed.
MAX_BODY_BYTES = 1024 * 1024
# Describe and Test take no argument: at most an empty object.
CALL_BODY_BYTES = 4 * 1024
# The stable code of a refused describe or test, next to the text in `detail`.
ERROR_HEADER = "X-Ina-Error"
# The reload outcome of a delete, whose 204 has no body.
RELOADED_HEADER = "X-Ina-Reloaded"
RELOAD_FAILED_HEADER = "X-Ina-Reload-Failed"
_EXPECTED_RULE = f"{EXPECTED_FIELD}: expected the updated_at this query was loaded with"
_STORE_TEXT = "SAP described the query in a form that cannot be stored"
# What a refused describe or test answers, by the client's code (`_refusal`).
_HTTP_STATUS = {
    "user_token_required": 424,
    "timeout": 504,
    "variable_required": 422,
    "invalid_filter": 422,
}
MAX_DETAIL_CHARS = 500


class NoArguments(BaseModel):
    """What Describe and Test may choose: nothing. The destination, the
    identity, the query, its variables and the measure tried are the stored
    query's; any key is refused."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------- helpers


def _query_name(name: str) -> str:
    """The path's ``name`` if it can be a query name, else the same 404 an
    unknown one gets -- before any lookup or log line (a NUL byte or an
    oversized value is a driver error on Postgres, whose text carries the
    bound parameter into the log)."""
    if not re.fullmatch(QUERY_NAME_RE, name):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return name


def _validated(data: dict[str, Any]) -> InaQueryPayload:
    try:
        return validate_ina_query(data)
    except ValueError as e:
        raise _refuse(str(e)) from None


def _stale_write(name: str) -> str:
    """The 409 text; the name matched the query-name pattern before."""
    return f"Query '{name}' was changed since it was loaded; reload it and save again"


def _detail(row: InaQuery, used_by: list[dict[str, Any]] | None) -> dict[str, Any]:
    """The record with its definition, ``described`` and the agents that
    name it. ``described`` follows the definition as it reads back: a
    stored text that is not one (a row written around the gate) is not."""
    data = row.to_dict(used_by)
    data["described"] = data["definition"] is not None
    return data


def _summary(row: InaQuery, used_by: list[dict[str, Any]] | None) -> dict[str, Any]:
    """`_detail` without the definition, for the list."""
    data = _detail(row, used_by)
    del data["definition"]
    return data


async def _stored(name: str) -> tuple[dict[str, Any], Any]:
    """The query as stored (``to_export``) and its ``updated_at``, or 404.
    The session is closed when SAP is asked: nothing is held meanwhile."""
    async with SessionLocal() as session:
        row = await get_ina_query(session, name)
        if row is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        return row.to_export(), row.updated_at


# ---------------------------------------------------------------------- reload


def _holds_ina_query(live: Any, name: str) -> bool:
    return live.holds_ina_query(name)


async def reload_if_in_use(names: Any, referrers: dict[str, Any], what: str) -> dict[str, bool]:
    """Rebuild the running agents after a COMMITTED change of the queries
    ``names`` when one of them is in use: ``{reloaded, reload_failed}``.

    The OData catalogue's rule and code (`odata_routes._reload_if_in_use`):
    the registry hands every ``builtin:ina`` toolset a copy of its queries
    when it is built, so an edit that closes something (a query disabled,
    another identity, a new description) would otherwise wait for somebody
    to press Reload. "In use" is an agent row naming the query
    (``referrers``, enabled or not) or the running build holding a copy of
    it. A rebuild that fails is ``reload_failed: true``, never raised; it
    reaches only this app instance.
    """
    return await odata_routes._reload_if_in_use(names, referrers, what, holds=_holds_ina_query)


async def reload_after_query_change(
    names: Any, referrers: dict[str, Any], what: str
) -> dict[str, bool]:
    """`reload_if_in_use`, then the data catalogue graph follows the changed
    queries (imported again, or dropped when no enabled agent names them any
    more; never raises)."""
    outcome = await reload_if_in_use(names, referrers, what)
    from agents.data_catalog.importer import rebuild_safely

    await rebuild_safely([("ina", name) for name in names], what=what)
    return outcome


def _busy() -> HTTPException:
    """429 while ``admin_calls.MAX_CONCURRENT_CALLS`` Describe or Test calls
    run: the code ``busy`` (the OData preview's), a fixed text."""
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=admin_calls.BUSY_TEXT,
        headers={ERROR_HEADER: "busy"},
    )


def _refusal(exc: InaError) -> HTTPException:
    """The answer of a refused describe or test: the client's code in
    ``X-Ina-Error``, its fixed text -- or the destination's admin text -- in
    ``detail``. 424 / 504 / 422 by the code, 502 for every other: the request
    reached the destination or SAP, which said no."""
    return HTTPException(
        status_code=_HTTP_STATUS.get(exc.code, 502),
        detail=plain(exc.admin_text or exc.message, MAX_DETAIL_CHARS),
        headers={ERROR_HEADER: exc.code},
    )


# ---------------------------------------------------------------------- routes


@router.get("/queries", dependencies=[Depends(require_admin)])
async def api_list_ina_queries() -> list[dict[str, Any]]:
    async with SessionLocal() as session:
        rows = await list_ina_queries(session)
        # One pass over the agents for the whole list.
        used_by = await ina_query_referrers(session)
        return [_summary(row, used_by.get(row.name)) for row in rows]


@router.post(
    "/queries", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin)]
)
async def api_create_ina_query(request: Request) -> dict[str, Any]:
    """Register a query; ``definition`` may come with it (an export), else it
    is described later. No reload: no agent can name an unregistered query."""
    payload = _validated(await _json_object(request, MAX_BODY_BYTES))
    async with SessionLocal() as session:
        try:
            row = await create_ina_query(session, payload)
        except ValueError as e:  # the name is taken
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from None
        used_by = await ina_query_referrers(session, row.name)
        return _detail(row, used_by.get(row.name))


@router.get("/queries/{name}", dependencies=[Depends(require_admin)])
async def api_get_ina_query(name: str) -> dict[str, Any]:
    name = _query_name(name)
    async with SessionLocal() as session:
        row = await get_ina_query(session, name)
        if row is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        used_by = await ina_query_referrers(session, name)
        return _detail(row, used_by.get(name))


@router.put("/queries/{name}", dependencies=[Depends(require_admin)])
async def api_update_ina_query(name: str, request: Request) -> dict[str, Any]:
    """Replace a query: its texts, its identity and, when the body has the
    key, its ``definition`` (without the key the stored description and its
    ``metadata_fetched_at`` are kept: an edit form never holds them).

    ``expected_updated_at`` as on the OData update route: a UI sends the
    ``updated_at`` it loaded and gets 409 when the row moved since
    (`update_ina_query` locks, compares and writes in one transaction);
    without it the replace is unconditional and logged as one WARNING with
    the name and the caller's principal. Order of the answers: payload
    refusals (422), the field's own 422, unknown query (404), another name in
    the body (409, it would detach every agent that names the query), stale
    (409).

    The answer is the record as THIS save wrote it, read inside the
    transaction before the commit, plus ``reloaded`` and ``reload_failed``:
    after the commit the running agents are rebuilt when the query is in use
    (`reload_after_query_change`), and its data catalogue source follows.
    """
    name = _query_name(name)
    body = await _json_object(request, MAX_BODY_BYTES)
    given = body.pop(EXPECTED_FIELD, None)
    payload = _validated(body)
    expected = _expected_updated_at(given, _EXPECTED_RULE)
    async with SessionLocal() as session:
        try:
            row = await update_ina_query(
                session, name, payload, expected_updated_at=expected, commit=False
            )
        except StaleRow:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=_stale_write(name)
            ) from None
        except ValueError as e:  # a different name in the body
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from None
        if row is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        if expected is None:
            logger.warning(
                "InA query '%s' replaced without %s (no stale-write check) by %s",
                name,
                EXPECTED_FIELD,
                current_principal.get() or "unknown principal",
            )
        used_by = await ina_query_referrers(session, name)
        answer = _detail(row, used_by.get(name))
        await session.commit()
    answer.update(await reload_after_query_change([name], used_by, f"InA query '{name}' updated"))
    return answer


@router.delete(
    "/queries/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
async def api_delete_ina_query(name: str, response: Response) -> None:
    """Delete a query no agent names (disabled agents count: turning one back
    on would break it).

    Lock, check, delete in one transaction, against the shared lock an agent
    save takes on the queries it names (`agents.db.check_ina_queries`), with
    the reasoning of the OData delete route. A deleted query has no
    referrers, but the running build may still hold a copy: then the agents
    are rebuilt after the commit, and the outcome is in the headers
    ``X-Ina-Reloaded`` and ``X-Ina-Reload-Failed`` (``true`` / ``false``).
    """
    name = _query_name(name)
    async with SessionLocal() as session:
        row = (await session.execute(_locked_ina_query(name))).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        referrers = (await ina_query_referrers(session, name)).get(name, [])
        if referrers:
            agents = ", ".join(f"'{r['agent']}'" for r in referrers)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Query '{name}' is used by agent(s) {agents}",
            )
        await delete_ina_query(session, row)
    outcome = await reload_after_query_change([name], {}, f"InA query '{name}' deleted")
    response.headers[RELOADED_HEADER] = str(outcome["reloaded"]).lower()
    response.headers[RELOAD_FAILED_HEADER] = str(outcome["reload_failed"]).lower()


@router.post("/queries/{name}/describe", dependencies=[Depends(require_admin)])
async def api_describe_ina_query(name: str, request: Request) -> dict[str, Any]:
    """Read the query's dimensions, measures and variables from SAP and store
    them as its definition, stamped now.

    Body: nothing, or an empty object. Answer: ``{dimensions, measures,
    variables}`` (counts), ``names`` (the technical names read, in that
    order), ``truncated`` (SAP described more members than a definition
    holds), ``skipped`` (how many odd names were left out),
    ``metadata_fetched_at``, and ``reloaded`` / ``reload_failed``: a new
    description of a query in use rebuilds the running agents, whose tools
    answer the stored one, and its data catalogue source; one equal to the
    stored description rebuilds nothing.

    The description is stored only over the row it was read for: when the
    query changed while SAP was asked (another destination, a save of the
    form) the answer is 409 and nothing is written; a query deleted
    meanwhile is 404. Refusals (``X-Ina-Error``): 429 ``busy`` (two calls
    run already), 424 ``user_token_required``, 504 ``timeout``, 502 for
    every other code of the client, and for a description the gate would
    not take (``sap_error``).
    """
    name = _query_name(name)
    stored, loaded_at = await _stored(name)
    _model_body(NoArguments, await _json_object(request, CALL_BODY_BYTES, empty_ok=True))
    try:
        description = await admin_calls.read_description(stored)
    except InaError as exc:
        raise _refusal(exc) from None
    except admin_calls.Busy:
        raise _busy() from None
    definition = description.definition
    described = definition.model_dump(mode="json")
    payload = {**stored, "definition": described}
    # No stamp: one is given now, the moment the definition is stored.
    payload["metadata_fetched_at"] = None
    async with SessionLocal() as session:
        try:
            row = await update_ina_query(
                session, name, payload, expected_updated_at=loaded_at, commit=False
            )
        except StaleRow:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Query '{name}' was changed while it was described; describe it again",
            ) from None
        except ValueError as e:
            # The gate names the field and the rule, never a value: the log
            # gets its text, the admin a fixed one.
            logger.warning(
                "ina describe: the description of query '%s' was refused: %s", name, e
            )
            raise _refusal(InaError("sap_error", _STORE_TEXT)) from None
        if row is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        used_by = await ina_query_referrers(session, name)
        fetched_at = row.to_export()["metadata_fetched_at"]
        await session.commit()
    if described == stored["definition"]:
        # SAP describes what the agents already hold: only the stamp moved,
        # which neither the tools nor the data catalogue read.
        outcome = dict(NOT_RELOADED)
    else:
        outcome = await reload_after_query_change(
            [name], used_by, f"InA query '{name}' described"
        )
    return {
        "dimensions": len(definition.dimensions),
        "measures": len(definition.measures),
        "variables": len(definition.variables),
        "names": [
            member.name
            for members in (definition.dimensions, definition.measures, definition.variables)
            for member in members
        ],
        "truncated": description.truncated,
        "skipped": len(description.skipped),
        "metadata_fetched_at": fetched_at,
        **outcome,
    }


@router.post("/queries/{name}/test", dependencies=[Depends(require_admin)])
async def api_test_ina_query(name: str, request: Request) -> dict[str, Any]:
    """Test a STORED query with one read: do its destination, identity and
    InA name work? A read and a test: nothing is stored, a disabled query
    may be tested (as an admin does before enabling it), and the answer
    never holds a value.

    Body: nothing, or an empty object. What is sent: the agent tools' own
    ``run`` of the first measure, no dimension, ``top`` 1 -- after a describe
    when the query has no stored description. A query with a mandatory
    variable is refused (422 ``variable_required``) before the read is sent.

    Answer, HTTP 200: ``{ok: true, status, duration_ms, columns}`` --
    ``status`` the HTTP status of SAP's last answer, ``columns`` the names
    of the result's columns. A test that failed is an error answer with the
    client's code in ``X-Ina-Error``, as for Describe (429 ``busy`` too).
    """
    name = _query_name(name)
    stored, _ = await _stored(name)
    _model_body(NoArguments, await _json_object(request, CALL_BODY_BYTES, empty_ok=True))
    try:
        return await admin_calls.run_test_read(stored)
    except InaError as exc:
        raise _refusal(exc) from None
    except admin_calls.Busy:
        raise _busy() from None
