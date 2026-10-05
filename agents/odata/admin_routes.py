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

The same holds for what a remote system said: the ``$metadata`` preview
route answers with fixed texts or SAP's own short code and message
(``agents.odata.preview``), never a host, a URL or a page.

This module imports ``agents.auth``, ``agents.db`` and the ``agents.odata``
modules only, never ``agents.admin`` (which imports it at the end of the
module).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StrictStr,
    StringConstraints,
    ValidationError,
    field_validator,
)
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.auth import current_principal, require_admin
from agents.db import (
    ODATA_AUDIT_OUTCOMES,
    ODataAuditLog,
    ODataService,
    SessionLocal,
    begin_exclusive_write,
    create_odata_service,
    delete_odata_service,
    get_odata_service,
    list_odata_audit,
    list_odata_services,
    odata_service_referrers,
    update_odata_service,
    validate_odata_service,
)
from agents.odata import preview
from agents.odata.models import (
    DESTINATION_NAME_RE,
    MAX_DEFINITION_BYTES,
    SERVICE_NAME_RE,
    loc_path,
)
from agents.odata.urls import confine_service_path

logger = logging.getLogger(__name__)

_Body = TypeVar("_Body", bound=BaseModel)

router = APIRouter(prefix="/api/odata", tags=["odata"])

_NOT_FOUND = "Service not found"
_TITLE_MAX = 120  # ODataServicePayload.title
_COPY_SUFFIX = " (copy)"
_MAX_REPORTED_ERRORS = 20
# The largest definition the gate accepts plus room for the other fields.
# Checked before anything is parsed: without it an admin token could make the
# worker buffer and parse a body of any size.
MAX_BODY_BYTES = MAX_DEFINITION_BYTES + 64 * 1024
_TOO_LARGE = "Request body too large"
# The preview request is five short fields.
METADATA_BODY_BYTES = 16 * 1024
# The id column of the audit table is a 32-bit integer on Postgres; a larger
# bound parameter is a driver error there, i.e. a 500.
AUDIT_MAX_ID = 2_147_483_647
# The stable code of a refused preview, next to the text in `detail`.
ERROR_HEADER = "X-OData-Error"
# Optimistic concurrency of the update route: the `updated_at` the client
# loaded. Not a stored field, so never part of the payload gate.
EXPECTED_FIELD = "expected_updated_at"
_EXPECTED_NO_STRING = f"{EXPECTED_FIELD}: Input should be a valid string"
_EXPECTED_RULE = f"{EXPECTED_FIELD}: expected the updated_at this service was loaded with"
# What `to_dict()` / `to_summary()` emit: `datetime.isoformat()`, with or
# without a fraction, with an offset on Postgres and without one on SQLite
# (both UTC). `Z` is accepted for a client that normalised it.
_EXPECTED_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})?", re.ASCII
)
# The audit route: how many rows one answer holds.
AUDIT_DEFAULT_LIMIT = 100
AUDIT_MAX_LIMIT = 500
_AUDIT_PARAMS = ("service", "sent_as", "outcome", "since", "until", "before_id", "limit")


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
    # Strict: this flag decides whose identity reaches SAP, so only a JSON
    # boolean sets it -- never "true", 1 or anything else that coerces.
    user_context: StrictBool | None = None


class MetadataBody(BaseModel):
    """What the ``$metadata`` preview is asked for: where to read, and as whom.

    No URL, no query, no host: the host is the destination's, and the path
    is held to the rule of a stored service path.
    """

    model_config = ConfigDict(extra="forbid")

    destination: Annotated[str, StringConstraints(pattern=DESTINATION_NAME_RE)]
    service_path: StrictStr
    odata_version: Literal["v2", "v4"]
    # Strict, as in `DuplicateBody`: it decides whose identity reaches SAP.
    user_context: StrictBool = False
    # The stored service to compare with. Its pattern is checked in the
    # handler, where a name that cannot be one is the 404 of an unknown one.
    service: StrictStr | None = None

    @field_validator("service_path")
    @classmethod
    def _confined(cls, value: str) -> str:
        return confine_service_path(value)


def _refuse(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def _service_name(name: str) -> str:
    """The path's ``name`` if it can be a service name, else the same 404 an
    unknown name gets.

    Checked before any lookup or log line: a NUL byte or an oversized value
    is a driver error on Postgres, i.e. a 500 whose SQLAlchemy text carries
    the bound parameter into the log.
    """
    if not re.fullmatch(SERVICE_NAME_RE, name):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return name


async def _bounded_body(request: Request, limit: int = MAX_BODY_BYTES) -> bytes:
    """The raw body, at most ``limit`` bytes; 413 beyond that.

    The declared length is refused before a byte is read. The stream is
    counted as well, because a chunked body declares nothing and a declared
    length is only a claim.
    """
    too_large = HTTPException(status_code=413, detail=_TOO_LARGE)
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise too_large
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise too_large
        chunks.append(chunk)
    return b"".join(chunks)


async def _json_object(request: Request, limit: int = MAX_BODY_BYTES) -> dict[str, Any]:
    """The request body as a JSON object, or a 422 that says only that."""
    raw = await _bounded_body(request, limit)
    try:
        data = json.loads(raw)
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


def _body_loc(parts: tuple[Any, ...]) -> str:
    """Where a refused body field is. A key is the client's own text -- an
    unknown key arrives here like any other, and a URL pasted where a key
    belongs would come back -- so it is named only when it has the form of
    a field name; the rule of ``agents.loc_fields``."""
    return loc_path(parts, "body")


def _model_body(model: type[_Body], data: dict[str, Any]) -> _Body:
    """``data`` as ``model``, or a 422 naming each field and its rule, never
    a value."""
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        lines = [f"{_body_loc(err['loc'])}: {err['msg']}" for err in errors[:_MAX_REPORTED_ERRORS]]
        # `from None`: the ValidationError carries the input in its repr.
        raise _refuse("; ".join(lines)) from None


def _duplicate_body(data: dict[str, Any]) -> DuplicateBody:
    return _model_body(DuplicateBody, data)


def _stale_write(name: str) -> str:
    """The 409 text. The name is safe to repeat: it matched the service-name
    pattern before any lookup (`_service_name`)."""
    return f"Service '{name}' was changed since it was loaded; reload it and save again"


def _expected_updated_at(value: Any) -> datetime | None:
    """A given ``expected_updated_at`` as an aware UTC instant, or a 422.

    ``None`` (the field missing, or JSON ``null``) means the client asks for
    no check. Otherwise only a string in the format the API emits
    ``updated_at`` in; the refusal names the field and the rule, never the
    value.

    A value without an offset is UTC, as the stored timestamps are (SQLite
    hands them back naive, and the API emits them that way).
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise _refuse(_EXPECTED_NO_STRING)
    if not _EXPECTED_RE.fullmatch(value):
        raise _refuse(_EXPECTED_RULE)
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)
    except (ValueError, OverflowError):  # month 13, offset +99:00, year 1 minus an offset
        raise _refuse(_EXPECTED_RULE) from None


def _is_stale(row: ODataService, expected: datetime | None) -> bool:
    """Whether ``row`` moved on since the client loaded it.

    Instants are compared, not texts: the same moment is written with an
    offset by Postgres and without one by SQLite, and a client may hand back
    either. ``expected`` None means the client did not ask for the check.
    Only meaningful on a row read under the lock that the write then keeps.
    """
    if expected is None:
        return False
    stored = row.updated_at
    if stored is None:
        return True
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    return stored.astimezone(timezone.utc) != expected


def _locked_service_query(name: str) -> Select[tuple[ODataService]]:
    """The service row, with an exclusive row lock where the database has one.

    No dialect switch here, as on the agent-save side
    (`agents.db.existing_odata_service_names`): SQLAlchemy sends ``FOR
    UPDATE`` to Postgres and leaves it out for SQLite, which has no row
    locks and serialises writers anyway.
    """
    return select(ODataService).where(ODataService.name == name).with_for_update()


async def _lock_odata_service(session: AsyncSession, name: str) -> ODataService | None:
    """Load the service and hold its row until the session's transaction ends."""
    return (await session.execute(_locked_service_query(name))).scalar_one_or_none()


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
    name = _service_name(name)
    async with SessionLocal() as session:
        row = await get_odata_service(session, name)
        if row is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        used_by = await odata_service_referrers(session, name)
        return row.to_dict(used_by.get(name))


@router.put("/services/{name}", dependencies=[Depends(require_admin)])
async def api_update_odata_service(name: str, request: Request) -> dict[str, Any]:
    """Replace a service: its definition and its identity (``destination``,
    ``user_context``: as whom agents reach SAP).

    The body is the service payload plus an optional top-level
    ``expected_updated_at``: the ``updated_at`` string exactly as the list
    or detail answer the client is editing carried it. **A UI must send
    it.** When it is there, the service is replaced only if it still has
    that ``updated_at`` (compared as instants, so the string a client got
    back always matches an unchanged row); otherwise the answer is 409
    (`_stale_write`) and nothing changes -- a save from an older tab cannot
    replace what a newer one stored, nor put an identity change on top of a
    state the admin never saw. Without the field, or with ``null``, the
    route replaces unconditionally, as it always did (API clients,
    scripts); each such save is one INFO line with the service and the
    caller's principal (never the body), so a check that is silently off
    shows in the log. A value that is no string, or no such timestamp, is a
    422 naming the field.

    Order of the answers: payload refusals (422), then the field's own 422,
    unknown service (404), another name in the body (422), stale (409).

    The answer is the service as THIS save wrote it, with its NEW
    ``updated_at`` (always later than the previous one, also for a save
    that changed no field), to be sent as ``expected_updated_at`` of the
    next save. It is read back -- the row and its referrers -- inside the
    transaction, under the lock, and only then committed. Read after the
    commit it could be another writer's state: the client would hold a
    valid stamp for content it never saw and overwrite it without a 409
    (and a delete in between would turn a stored save into a 500). An error
    while reading it is an error answer for a write that was rolled back.

    Lock, compare, write, in one transaction, like the delete route below:
    the row is read ``FOR UPDATE`` and the comparison is made on that read.
    Race-free on Postgres under READ COMMITTED: of two saves that loaded the
    same ``updated_at``, the second waits at the locked read until the first
    commits, and is then handed the committed row (a locking read re-reads a
    row it waited for; the ``name`` it selects by never changes), so it
    compares against the first save's ``updated_at`` and answers 409. A
    conditional ``UPDATE ... WHERE updated_at = :expected`` would need the
    client's text to equal the stored value exactly, which it does not on
    SQLite (the column default writes whole seconds as text). SQLite has no
    row locks; there `begin_exclusive_write` makes the transaction a writer
    before the read, to the same effect.

    No deadlock, for the reason given at the delete route: this transaction
    waits for this one row before it holds anything.
    """
    name = _service_name(name)
    body = await _json_object(request)
    given = body.pop(EXPECTED_FIELD, None)
    data = _validated(body)
    expected = _expected_updated_at(given)
    async with SessionLocal() as session:
        await begin_exclusive_write(session)
        row = await _lock_odata_service(session, name)
        if row is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        if data["name"] != row.name:
            # Before the stale answer: a rename is wrong whatever was loaded.
            raise _refuse("name cannot be changed; duplicate the service instead")
        if _is_stale(row, expected):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_stale_write(name))
        if expected is None:
            logger.info(
                "odata service '%s' replaced without %s (no stale-write check) by %s",
                name,
                EXPECTED_FIELD,
                current_principal.get() or "unknown principal",
            )
        try:
            # Flushed and re-read from the database, not committed yet.
            row = await update_odata_service(session, row, data, commit=False)
        except ValueError as e:  # a different name in the body
            raise _refuse(str(e)) from None
        used_by = await odata_service_referrers(session, name)
        answer = row.to_dict(used_by.get(name))
        await session.commit()
        return answer


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

    Lock, check, delete, in one transaction. An agent save takes a shared
    lock on the services it attaches before it writes the agent
    (`agents.admin._unknown_odata_services`), so the two exclude each other
    on the service row:

    - the save got there first: the lock below waits for its commit, and the
      referrer check, a new statement, then sees that agent -> 409;
    - the delete got there first: the save waits, then no longer finds the
      row and refuses the agent ("unknown OData service").

    Checking before locking would let a delete that had passed its check
    wait out the save and still go through. The check relies on READ
    COMMITTED (the Postgres default, which the engine keeps): under a
    transaction-wide snapshot it would not see the agent it waited for.

    No deadlock: this transaction waits only for this one row, before it
    holds anything. Once it has the lock, the agent read takes no row lock
    and the delete is of the row it already holds, so nothing a save holds
    can make it wait while a save waits for it.

    No ``expected_updated_at`` here: the stale-write check is the update
    route's alone.
    """
    name = _service_name(name)
    async with SessionLocal() as session:
        row = await _lock_odata_service(session, name)
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
    name = _service_name(name)
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


@router.post("/metadata", dependencies=[Depends(require_admin)])
async def api_preview_odata_metadata(request: Request) -> dict[str, Any]:
    """Read a service's ``$metadata`` and answer what it offers. A PREVIEW:
    nothing is stored and no catalogue service changes; the admin picks from
    the answer and saves through the create or update route.

    Body (JSON object, at most ``METADATA_BODY_BYTES``; any other key is a
    422, and a refusal names the field and the rule, never the value):
    ``destination`` (a destination name), ``service_path`` (the rule of a
    stored service path: absolute, no query, fragment, ``..`` or scheme),
    ``odata_version`` (``v2`` | ``v4``), ``user_context`` (a JSON boolean,
    default false) and optionally ``service``: the name of a stored
    catalogue service to compare with (``null`` = none).

    The app then sends ONE request, ``GET <destination><service_path>/
    $metadata`` -- no URL, query or host from the client, no redirect
    followed, no retry, nothing cached (``agents.odata.preview``). With
    ``user_context`` true it is sent as the admin who calls this route (the
    request's bound JWT through the destination); without a bound JWT the
    answer is 424 and nothing is sent. With ``user_context`` false the
    destination's own credential is used. When ``user_context`` is true
    but the destination's authentication type propagates no user, the
    answer says so in ``warnings`` (code ``technical_credential``).

    Answer (``preview.build_preview``): ``{fetched_at, entity_sets,
    operations, skipped, removed_entity_sets, removed_operations,
    removed_complete, skipped_stored_entity_sets, summary, truncated,
    totals, warnings}``: names, types and labels. ``removed_entity_sets``,
    ``removed_operations`` and their counts in ``summary`` are ``null``
    when ``removed_complete`` is false (the document was not read to its
    end: unknown, not none). ``skipped_stored_entity_sets`` (``{name,
    reason}``) are stored entity sets the document still declares but the
    parser left out; they are not "removed". A ``skipped`` entry is
    ``{kind, entity_set, position, reason, entity_type}``. What the
    service DECLARES sits under ``declared``
    (per field: ``filterable`` / ``creatable`` / ``updatable``; per entity
    set: ``creatable`` / ``updatable`` / ``deletable``), and an operation's
    ``suggested: {changes_data, known}`` is a suggestion (``known`` false:
    nothing is known, treated as changing). No key of a field, entity set
    or operation is called ``selectable``, ``filterable``, ``writable`` or
    ``enabled``: nothing is enabled here. With ``service``, each entity set
    has a ``status`` (``new`` | ``in_service`` | ``changed``) with
    ``new_fields``, ``removed_fields``, ``changed_types`` (field names) and
    ``changed_keys``, and the top level names stored entity sets and
    operations that are gone from the document -- all compared with the
    STORED definition read here, whose hints, examples and switches are
    never part of the answer.

    Refusals, in this order: 413 / 422 body (an unknown key is named only
    when it looks like a field name); 404 ``Service not found``
    (``service`` names none, or cannot be a name) -- before anything is
    fetched; then, each with a stable code in the ``X-OData-Error`` header:
    429 ``busy`` (``preview.MAX_CONCURRENT_PREVIEWS`` are running); 422
    ``invalid_path``; 424 ``user_token_required``; 502
    ``on_premise_unavailable``, ``destination_error``, ``unreachable``,
    ``redirect``, ``sap_error`` (SAP's short code and message),
    ``not_xml`` (a sign-in page, a compressed answer), ``too_large``; 504
    ``timeout`` (fetch plus parse, ``preview.PREVIEW_BUDGET_SECONDS``); 422
    ``invalid_metadata`` (the parser's fixed text, e.g. a version mismatch
    or a document over its work budget); 500 ``preview_failed`` (a defect:
    fixed text, the exception's type in the log).

    ``preview.run_preview`` writes the one log line: what was asked, the
    caller's principal, how the destination was resolved, the outcome,
    bytes and duration -- no content.

    The body is parsed whatever its ``Content-Type``; the cross-site guard
    of this route is the Origin check of ``JWTBindingMiddleware``.
    """
    body = _model_body(MetadataBody, await _json_object(request, METADATA_BODY_BYTES))
    stored: dict[str, Any] | None = None
    if body.service is not None:
        name = _service_name(body.service)
        async with SessionLocal() as session:
            row = await get_odata_service(session, name)
            if row is None:
                raise HTTPException(status_code=404, detail=_NOT_FOUND)
            stored = row.definition
    try:
        return await preview.run_preview(
            body.destination, body.service_path, body.odata_version, body.user_context, stored
        )
    except preview.PreviewError as exc:
        raise HTTPException(
            status_code=exc.status, detail=exc.detail, headers={ERROR_HEADER: exc.code}
        ) from None


def _audit_moment(name: str, raw: str) -> datetime:
    """An ISO 8601 query value as a UTC moment, or a 422 that names ``name``."""
    try:
        if len(raw) > 40 or not raw.isascii():
            raise ValueError
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        # Without an offset it is UTC, like the stored timestamps.
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        # Inside the try: a date at the edge of the calendar with an offset
        # (0001-01-01T00:00:00+14:00) overflows here, with OverflowError.
        return moment.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise _refuse(
            f"{name}: an ISO 8601 date or date-time, e.g. 2026-01-31T00:00:00Z"
        ) from None


def _audit_filters(request: Request) -> dict[str, Any]:
    """The query parameters of the audit route, checked one by one.

    Read from the request and validated here, like the bodies above: a
    refusal names the parameter and the rule, never the value.
    """
    params = request.query_params
    if any(name not in _AUDIT_PARAMS for name in params.keys()):
        raise _refuse("query: only " + ", ".join(_AUDIT_PARAMS) + " are known")
    if any(len(params.getlist(name)) > 1 for name in _AUDIT_PARAMS):
        raise _refuse("query: a parameter may be given once")
    filters: dict[str, Any] = {"limit": AUDIT_DEFAULT_LIMIT}
    service = params.get("service")
    if service is not None:
        if not re.fullmatch(SERVICE_NAME_RE, service):
            raise _refuse("service: not a service name")
        filters["service"] = service
    sent_as = params.get("sent_as")
    if sent_as is not None:
        width = ODataAuditLog.__table__.c.sent_as.type.length
        if not 1 <= len(sent_as) <= width or not sent_as.isprintable():
            raise _refuse(f"sent_as: 1 to {width} printable characters")
        filters["sent_as"] = sent_as
    outcome = params.get("outcome")
    if outcome is not None:
        if outcome not in ODATA_AUDIT_OUTCOMES:
            raise _refuse("outcome: one of " + ", ".join(ODATA_AUDIT_OUTCOMES))
        filters["outcome"] = outcome
    for name in ("since", "until"):
        raw = params.get(name)
        if raw is not None:
            filters[name] = _audit_moment(name, raw)
    before_id = params.get("before_id")
    if before_id is not None:
        # At most 10 digits and no more than the id column holds on
        # Postgres (`AUDIT_MAX_ID`): a larger value is a driver error there.
        if not (before_id.isascii() and before_id.isdigit() and len(before_id) <= 10) or not (
            1 <= int(before_id) <= AUDIT_MAX_ID
        ):
            raise _refuse(
                f"before_id: the id of an audit row (an integer from 1 to {AUDIT_MAX_ID})"
            )
        filters["before_id"] = int(before_id)
    limit = params.get("limit")
    if limit is not None:
        if not (limit.isascii() and limit.isdigit() and len(limit) <= 4) or not (
            1 <= int(limit) <= AUDIT_MAX_LIMIT
        ):
            raise _refuse(f"limit: an integer from 1 to {AUDIT_MAX_LIMIT}")
        filters["limit"] = int(limit)
    return filters


@router.get("/audit", dependencies=[Depends(require_admin)])
async def api_list_odata_audit(request: Request) -> dict[str, Any]:
    """The write audit log of ``builtin:odata``, newest first. Read-only:
    there is no route that writes, changes or deletes an audit row.

    Query parameters, all optional: the exact matches ``service`` (a
    service name), ``sent_as`` (whose credential SAP saw) and ``outcome``
    (``intent``, ``ok``, ``refused``, ``sap_error``, ``unknown``,
    ``cancelled``); ``since`` / ``until`` (ISO 8601; rows recorded at or
    after / at or before it, UTC when it has no offset); ``before_id``
    (rows with a smaller id; 1 to ``AUDIT_MAX_ID``, what the id column
    holds) and ``limit`` (1 to ``AUDIT_MAX_LIMIT``, default
    ``AUDIT_DEFAULT_LIMIT``). Any other parameter is a 422.
    ``sent_as`` matches the STORED form: a value longer than its column is
    stored cut (``agents.odata.audit._fit``: a prefix, ``~`` and a digest),
    and such a row is found only by that stored, cut text -- not by the
    full value and not by a prefix of it.
    An unencoded ``+`` in ``since`` / ``until`` arrives as a space and is
    refused: write the offset as ``%2B02:00``, or use ``Z``.

    Answer: ``{"items": [row], "limit": n, "more": bool}``, newest first by
    id (the order in which the intents were recorded). Paging: while
    ``more`` is true, ask again with the same filters and ``before_id`` =
    the ``id`` of the last item; every row of a filter is reachable that
    way, however many there are. One exception, on Postgres: ids are handed
    out when a row is inserted, not when it commits, so a row committed
    late can have a smaller id than rows a page already showed, and
    ``before_id`` paging that had passed its place does not come back to
    it. To be sure of a complete picture, read again from the top (without
    ``before_id``).
    A row is ``ODataAuditLog.to_dict()``: ids and timestamps, agent, run id,
    service, target, operation, ``key`` and ``created_key`` WITH their
    values, the body field NAMES, phase, outcome, HTTP status, ``sent_as``,
    ``run_principal`` and the token digest. A row whose outcome is still
    ``intent`` has ``phase: null``: the write MAY have been sent (see the
    docstring of ``agents.odata.audit``). A call that ends as "audit not
    confirmed" (its intent could not be stored in time) can take up to the
    intent timeout plus the bound of the close that follows
    (``agents.odata.tools.AUDIT_INTENT_TIMEOUT_SECONDS`` +
    ``AUDIT_RESULT_TIMEOUT_SECONDS``) before its row reads ``refused``;
    until then it lists as ``intent``.

    The table can hold personal data -- key values name an entity (which
    can be a person's), principals name users -- so the route is for admins
    only and the rows are purged by age (``ODATA_AUDIT_RETENTION_DAYS``).
    """
    filters = _audit_filters(request)
    limit = filters.pop("limit")
    async with SessionLocal() as session:
        rows = await list_odata_audit(session, **filters, limit=limit + 1)
        return {
            "items": [row.to_dict() for row in rows[:limit]],
            "limit": limit,
            "more": len(rows) > limit,
        }
