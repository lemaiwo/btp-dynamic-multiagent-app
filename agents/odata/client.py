"""The OData client of the built-in: confined requests, normalised results.

One client serves one catalogue service for one call. It is handed an
``httpx.AsyncClient`` whose base URL is the destination placeholder
(``agents.destination_auth.destination_http_client``), so the host and the
credential are decided per request by ``DestinationAuth`` and never here:
this module only ever builds a *relative* URL below the service path
(``urls.join_path``) and passes query options through ``params=``.

The catalogue is enforced here a second time, on purpose. The tools check a
call against the catalogue to give the model a precise refusal; this client
checks again because it is the last code before the request leaves, and a
caller that forgot a check must not turn into a request that reads a field
the admin never released. Both directions are covered: what is asked
(``$select``, ``$filter``, ``$orderby``, ``$expand``, the navigation) and
what comes back (rows are cut to the selectable fields that were asked for,
whatever SAP sent).

Nothing a back end or a transport says reaches the caller verbatim except
the SAP error text of a proper OData error envelope: an HTML error page, a
sign-in page or an httpx exception text (which can carry a URL) becomes a
fixed message.

The dialect (``v2.V2Dialect``, ``v4.V4Dialect``) holds everything that
differs between protocol versions: literals, the key predicate, the query
option names, where an expanded entity's fields are named, and the payload
shapes. Which one a client gets is the caller's decision from the
catalogue's ``odata_version``; this module never looks at an answer to find
out, and the checks, their order and the row filter are the same code for
both.

Writes (``create``, ``update``, ``delete``) follow the same pattern with
their own gate, ``check_write``, and three rules of their own:

* The CSRF token and the SAP session cookies of a write come from the
  ``CsrfSessionStore`` entry of the identity *this request* runs as
  (destination + signed-in user, or the destination's technical entry) and
  are set as explicit headers on that one request. The shared HTTP client
  keeps no cookie; a read sends neither.
* A modifying request is repeated only after an answer that says it was
  refused before it was processed, never after a failure. This client
  repeats it exactly once after a 403 that SAP marks ``X-CSRF-Token:
  Required``. Below it, ``DestinationAuth`` re-sends a request once after a
  401 (its cached credential aged out), a POST included. The two multiply:
  one write is at most four sends of the change and two token fetches
  (calls of ``_fetch_session``, each of which ``DestinationAuth`` can
  itself re-send once after a 401), every repeat following a refusal
  (``tests/test_odata_v2_write.py`` pins the bound).
* A failure while the request is under way is never retried: whether SAP
  applied the change is unknown, and the caller is told so with the code
  ``write_outcome_unknown``. The same code answers a 2xx that is not the
  answer of a write (a sign-in page arriving as ``200 text/html``, a JSON
  body that is an error) and a bare 502/504 from a gateway: success is
  recognised positively. Cookies of an answer are taken into the stored
  session only after that verdict, and an answer that fails it ends the
  session: what a sign-in page sets is never sent with a later write. The
  same holds for an answer of 300 or above, SAP's own refusals included:
  its cookies are not taken and the session stays as it was sent. Should
  SAP have moved the session on with a refusal, the next write is refused
  with ``403`` / ``X-CSRF-Token: Required`` and renews once, as above.
* Every ``ODataError`` says whether the change left this app: ``sent`` is
  ``False`` for whatever is raised before the modifying request goes out
  (a catalogue refusal, a failed CSRF token request, a connection that was
  never made -- "nothing was changed") and ``True`` from the moment a
  modifying request was handed to an open connection, whatever came back.
  A ``DestinationError`` that passes through a write is always from before
  the first send.
* No token, cookie, body value or host appears in an error, a log line or a
  ``repr`` of this module.

Function imports (``call``, V2 only) have their own gate, ``check_call``.
In SAP the real business steps -- release, approve, post, cancel -- are
function imports, so a call is a WRITE unless the catalogue says otherwise
in so many words: only an operation marked ``changes_data: false`` that is
also a ``GET`` is sent as a read (``call_changes_data``). Every other call,
a ``GET`` included, goes out exactly like an entity write: the caller's own
CSRF session, one repeat after a 403 ``Required``, never a repeat after a
failure, success recognised positively. What comes back is shown only when
it can be tied to a catalogue entity set -- the one the operation's
``returns`` names, else the one it is ``bound_to`` -- so that the field
allowlist applies; otherwise the answer is a confirmation and nothing else
(``ODataClient.call``, ``_returned``).
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx
from pydantic import ValidationError

from agents.destination import DestinationError

from .models import (
    EDM_NAME_RE,
    EntitySetDef,
    NavigationDef,
    OperationDef,
    ServiceDefinition,
)
from .session import CsrfSession, NoCookieJar, cookies_from_response
from .urls import FilterError, check_filter, confine_next_link, confine_service_path, join_path

logger = logging.getLogger(__name__)

# How often a list call follows the server's paging link to fill one page.
MAX_NEXT_HOPS = 5
# What one answer may weigh before it is parsed: the body is read as a
# stream and dropped beyond this, so a wide `$expand` cannot exhaust memory.
MAX_RESPONSE_BYTES = 8_000_000
# A hard ceiling on `$top`, above the tools' own MAX_TOP.
MAX_PAGE_SIZE = 1000
MAX_MESSAGE_CHARS = 500
# What one write may weigh once it is encoded.
MAX_REQUEST_BYTES = 1_000_000
WRITE_OPERATIONS = ("create", "update", "delete")
# The result key under which a get, a create and an update hand the RAW ETag
# of the entity to the tool layer. It is never part of ``item``. The tool
# layer keeps it to itself and gives the model an opaque handle instead; it
# must not pass this value on.
RAW_ETAG_FIELD = "etag"

# One entity tag (RFC 9110), strong or weak. A list of tags and the wildcard
# `*` do not have this form, so `If-Match: *` ("overwrite whatever is there")
# can never be sent.
_ETAG = re.compile(r'(?:W/)?"[\x21\x23-\x7E]{0,500}"')
_CSRF_TOKEN = re.compile(r"[\x21-\x7E]{1,512}")

_ORDERBY = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)(?: +(asc|desc))?$")
_READ_FIRST_HINT = (
    "do not send the change again blindly; read the entity first to see whether it was applied"
)
# A create has no key to read back: the entity may exist under a key SAP chose.
_LIST_FIRST_HINT = (
    "do not send the create again blindly; list by the values you sent before trying "
    "again, to see whether the entity was created"
)


# A call has no entity of its own: what it changed is whatever it is about.
_CALL_FIRST_HINT = (
    "do not call the operation again blindly: it may have been carried out, and it "
    "cannot be undone; read the affected record first to see whether it was"
)
# How many parameters one call may carry (a catalogue never declares more).
MAX_CALL_PARAMS = 100
# The longest text a read-type call may return as its one scalar. Function
# imports that return a serialised document (JSON, XML) in an `Edm.String`
# exist; such a text would carry fields past the catalogue's allowlist.
MAX_CALL_VALUE_CHARS = 1000


def _outcome_hint(operation: str) -> str:
    """How to find out whether a write of unknown outcome was applied."""
    if operation == "call":
        return _CALL_FIRST_HINT
    return _LIST_FIRST_HINT if operation == "create" else _READ_FIRST_HINT


# The one text for "this app cannot call operations of this OData version".
CALL_NOT_AVAILABLE = "operations of a service of this OData version cannot be called yet"
# Why an operation cannot be called (`call_refusal`).
CALL_REFUSALS = (
    "operation_disabled",  # not enabled in the catalogue
    "calls_not_available",  # the service's OData version: not this app, not yet
    "write_not_allowed",  # it changes data and the agent may not
    "bound_set_missing",  # bound to an entity set the catalogue does not hold
    "key_not_declared",  # bound, and a key field is not one of its parameters
    "parameter_type",  # a parameter that is always sent has a type not written
)
_CATALOGUE_HINT = "this is a catalogue setting; tell the user instead of retrying"


def call_refusal(
    operation: OperationDef,
    bound_keys: Sequence[str] | None,
    dialect: Any,
    *,
    allow_write: bool = True,
) -> str | None:
    """Why ``operation`` cannot be called, or ``None`` when it can. No I/O.

    THE rule for "callable": ``ODataClient.check_call`` refuses by it, the
    search tool offers only what passes it, and the admin API lists the
    enabled operations that fail it. It is about the catalogue, the dialect
    and the agent's write switch -- never about the arguments of one call.

    ``bound_keys`` are the key field names of the entity set the operation
    is bound to (``None`` when that set is not in the catalogue; ignored for
    an operation that is not bound). ``dialect`` is the dialect of the
    service's OData version (``None``: none). First match of
    ``CALL_REFUSALS``, in that order.

    A bound call sends the key of its entity as parameters of the same
    names, so every key field must be a declared parameter. A parameter that
    is always sent -- a ``required`` one, or a key field -- must have a type
    the dialect writes (``sends_type``): a complex type, a collection,
    ``Edm.Binary`` or an unknown name can never be given a value. An
    optional parameter of such a type is merely never sent.
    """
    if operation.enabled is not True:
        return "operation_disabled"
    if getattr(dialect, "supports_call", False) is not True or operation.kind != "function_import":
        return "calls_not_available"
    if not allow_write and operation.is_write():
        return "write_not_allowed"
    always: set[str] = set()
    if operation.bound_to is not None:
        if bound_keys is None:
            return "bound_set_missing"
        declared = {p.name for p in operation.parameters}
        if any(name not in declared for name in bound_keys):
            return "key_not_declared"
        always = set(bound_keys)
    sends = getattr(dialect, "sends_type", None)
    for parameter in operation.parameters:
        if not (parameter.required or parameter.name in always):
            continue
        if not callable(sends) or sends(parameter.type) is not True:
            return "parameter_type"
    return None


def call_changes_data(operation: OperationDef) -> bool:
    """Whether calling ``operation`` is a write: the model's own rule
    (``OperationDef.is_write`` / ``models.operation_is_write``), which is
    also what ``has_write`` and the search tool go by."""
    return operation.is_write()


# Raised before a connection exists: nothing left this app.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_STATUS_HINTS = {
    401: "the destination's credential was not accepted by the back end",
    403: "the SAP user is not authorised for this service or entity",
    404: "the service path, the entity set or the entity does not exist "
    "(or the service is not activated)",
}


class ODataError(Exception):
    """A refusal or a failed call, in the shape the tools hand to the model.

    ``code`` is one of the tool error codes; ``message`` is short and safe
    to show (no URL, no token, no markup from an error page).

    ``sent`` is about writes and answers one question for an audit record:
    did a modifying request leave this app? ``False`` (the default, and
    always for a read) means it did not -- the error was raised before the
    modifying request went out, so SAP cannot have changed anything because
    of this call. ``True`` means a modifying request was handed to an open
    connection at least once, whatever happened next: SAP refused it, the
    connection broke, the answer was not recognisable. It says nothing about
    whether SAP applied the change; ``code`` does (``write_outcome_unknown``
    for "not known"). It is not part of ``to_dict``.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        hint: str | None = None,
        sent: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.hint = hint
        self.sent = sent

    def to_dict(self) -> dict[str, str]:
        out = {"code": self.code, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        return out


@dataclass
class ReadQuery:
    """The query options of a read. ``top=0`` means none (an entity read)."""

    select: list[str]
    filter: str | None
    expand: list[str]
    orderby: list[str]
    top: int
    skip: int
    count: bool = True


@dataclass(frozen=True)
class ReadPlan:
    """A read that passed every catalogue check (``ODataClient.check_read``).

    ``path`` is the confined, relative request path; ``target`` the entity
    set whose fields the answer has (the navigation's target, else the entity
    set itself); ``names`` the fields a row is cut to; ``expands`` the
    expanded navigations with their target entity sets; ``query`` what goes
    to the dialect's ``read_params`` (its ``select`` already carries the
    expanded targets' fields; ``top == 0`` for a ``get``).
    """

    operation: str
    path: str
    target: EntitySetDef
    names: list[str]
    expands: list[tuple[NavigationDef, EntitySetDef]]
    query: ReadQuery


@dataclass(frozen=True)
class WritePlan:
    """A write that passed every catalogue check (``ODataClient.check_write``).

    ``path`` is the confined, relative request path; ``body`` the encoded
    request body (``None`` for a delete); ``fields`` the names of the fields
    the body sets, in the caller's order -- names only, which is all an
    audit record may hold; ``etag`` the validated ``If-Match`` value or
    ``None``. The ``repr`` shows names only: the path carries the key
    values, and body and ETag are data.
    """

    operation: str
    path: str = field(repr=False)
    entity_set: EntitySetDef = field(repr=False)
    fields: tuple[str, ...]
    body: dict[str, Any] | None = field(default=None, repr=False)
    etag: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return (
            f"WritePlan(operation={self.operation!r}, "
            f"entity_set={self.entity_set.name!r}, fields={self.fields!r})"
        )


@dataclass(frozen=True)
class CallPlan:
    """A function import call that passed every check (``ODataClient.check_call``).

    ``name`` is the operation's catalogue name; ``method``, ``path`` (the
    confined, relative request path) and ``query`` (for ``params=``) are the
    request; ``fields`` the NAMES of the parameters of ``params`` that are
    SENT, in the caller's order (an optional parameter passed as null is
    left out of the request, and so of this) -- names only, which is all an
    audit record may hold; ``key`` the validated key of the bound entity or ``None``;
    ``changes`` whether the call is a write (``call_changes_data``);
    ``bound`` the entity set it is bound to, if any (whose KEY the call
    takes); ``result_set`` the entity set returned entities are checked
    against and cut to -- the one the catalogue's ``returns`` names, else
    ``bound`` -- and ``many`` what ``returns`` declares (``True`` a
    collection, ``False`` one entity, ``None`` when the catalogue does not
    say); ``etag`` the validated ``If-Match`` value or ``None``.
    ``operation`` and ``body`` make it the plan of a modifying request for
    ``_write``. The ``repr`` shows names only: the query carries the
    parameter and key values.
    """

    name: str
    method: str
    path: str = field(repr=False)
    query: dict[str, str] = field(repr=False)
    fields: tuple[str, ...]
    key: dict[str, Any] | None = field(default=None, repr=False)
    changes: bool = True
    bound: EntitySetDef | None = field(default=None, repr=False)
    result_set: EntitySetDef | None = field(default=None, repr=False)
    many: bool | None = field(default=None, repr=False)
    etag: str | None = field(default=None, repr=False)
    operation: str = "call"
    body: None = None

    def __repr__(self) -> str:
        return (
            f"CallPlan(name={self.name!r}, method={self.method!r}, "
            f"fields={self.fields!r}, changes={self.changes!r})"
        )


def _subject(plan: Any) -> str:
    """What a modifying request was about, for a log line: a catalogue name."""
    if isinstance(plan, CallPlan):
        return f"operation {plan.name}"
    return f"entity set {plan.entity_set.name}"


def _etag_of(*candidates: object) -> str | None:
    """The first candidate that is one well-formed entity tag."""
    for candidate in candidates:
        if isinstance(candidate, str) and _ETAG.fullmatch(candidate):
            return candidate
    return None


def _shown(name: object) -> str:
    """A name for a refusal: repeated only when it has the form of a name."""
    if isinstance(name, str) and re.fullmatch(EDM_NAME_RE, name) and len(name) <= 64:
        return repr(name)
    return "that name"


def _plain(text: object, limit: int = MAX_MESSAGE_CHARS) -> str:
    """One line of printable text (control and format characters dropped)."""
    if not isinstance(text, str):
        return ""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text[: limit * 4])
    return " ".join(cleaned.split())[:limit]


_DROP = object()


def _value(value: Any) -> Any:
    """A field value without the protocol's bookkeeping keys.

    V2 keeps them in ``__metadata`` / ``__deferred``; V4 in annotations,
    whose key is ``@name`` or ``Property@name`` (``@odata.type``,
    ``City@odata.type``, ``Items@odata.nextLink``). A property name can hold
    no ``@``, so every key with one is dropped.
    """
    if isinstance(value, dict):
        if "__deferred" in value:
            return _DROP
        cleaned = {
            k: _value(v)
            for k, v in value.items()
            if isinstance(k, str) and not k.startswith("__") and "@" not in k
        }
        return {k: v for k, v in cleaned.items() if v is not _DROP}
    if isinstance(value, list):
        return [v for v in (_value(item) for item in value) if v is not _DROP]
    return value


class ODataClient:
    """Reads and writes of one catalogue service through its destination."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        service: dict[str, Any],
        dialect: Any,
        *,
        sessions: Any = None,
    ) -> None:
        self._http = http
        self._dialect = dialect
        # The CSRF token store of the write path; a read neither needs nor touches it.
        self._sessions = sessions
        if sessions is not None and http is not None:
            # A client that writes handles SAP session cookies, and the HTTP
            # client is shared by every user of the agent: whatever jar it
            # came with, from here on it stores and sends nothing by itself.
            if not isinstance(http.cookies.jar, NoCookieJar):
                http.cookies = NoCookieJar()  # type: ignore[assignment]
        service = service if isinstance(service, dict) else {}
        # Whose SAP session a write uses: the catalogue service's own
        # settings, never an argument of a call.
        self._destination = service.get("destination")
        self._user_context = service.get("user_context")
        try:
            self._service_path = confine_service_path(service.get("service_path"))  # type: ignore[arg-type]
        except ValueError:
            raise ODataError("invalid_argument", "the service has no usable service_path") from None
        definition = service.get("definition") or {}
        if isinstance(definition, ServiceDefinition):
            self._definition = definition
        else:
            try:
                self._definition = ServiceDefinition.model_validate(definition)
            except ValidationError:
                # `from None`: the ValidationError carries the input in its repr.
                raise ODataError(
                    "invalid_argument", "the stored definition of the service is not valid"
                ) from None

    # -- what is asked ------------------------------------------------------
    def _navigation_target(
        self, source: EntitySetDef, name: object, operation: str | None
    ) -> tuple[NavigationDef, EntitySetDef]:
        """The navigation ``name`` of ``source`` and the entity set it leads to.

        The target must be in the catalogue (its fields decide what may be
        read) with the matching read enabled: ``list`` for a collection,
        ``get`` for a single entity. ``operation`` is what the caller is
        doing, or ``None`` for an ``$expand``.
        """
        nav = (
            next((n for n in source.navigations if n.name == name), None)
            if isinstance(name, str)
            else None
        )
        if nav is None:
            raise ODataError(
                "unknown_target",
                f"entity set {source.name!r} has no navigation {_shown(name)}",
            )
        needed = "list" if nav.collection else "get"
        if operation is not None and operation != needed:
            raise ODataError(
                "invalid_argument",
                f"navigation {nav.name!r} leads to "
                + (
                    "a collection; read it with 'list'"
                    if nav.collection
                    else "one entity; read it with 'get'"
                ),
            )
        target = self._definition.entity_set(nav.target)
        if target is None:
            raise ODataError(
                "unknown_target",
                f"the target of navigation {nav.name!r} is not in the catalogue",
            )
        if needed not in target.operations:
            raise ODataError(
                "operation_disabled",
                f"{needed!r} is not enabled for entity set {target.name!r}",
            )
        return nav, target

    def _resolve(
        self, entity_set: EntitySetDef, key: Any, navigation: Any, operation: str
    ) -> tuple[str, EntitySetDef]:
        """The request path and the entity set whose fields the answer has."""
        segment = entity_set.path or entity_set.name
        if navigation is None:
            if operation not in entity_set.operations:
                raise ODataError(
                    "operation_disabled",
                    f"{operation!r} is not enabled for entity set {entity_set.name!r}",
                )
            if operation == "list":
                if key is not None:
                    raise ODataError(
                        "invalid_argument",
                        "a key is only used with a navigation; read one entity with 'get'",
                    )
                segments = [segment]
            else:
                segments = [segment + self._dialect.key_segment(entity_set, key)]
            target = entity_set
        else:
            # Following a navigation by key reads *through* the parent entity:
            # whether it exists and what it links to is an answer about it.
            if "get" not in entity_set.operations:
                raise ODataError(
                    "operation_disabled",
                    f"'get' is not enabled for entity set {entity_set.name!r}, "
                    f"so its navigations cannot be followed",
                )
            keyed = segment + self._dialect.key_segment(entity_set, key)
            nav, target = self._navigation_target(entity_set, navigation, operation)
            segments = [keyed, nav.name]
        try:
            return join_path(self._service_path, *segments), target
        except ValueError:
            raise ODataError(
                "invalid_argument", "the request path could not be built from these arguments"
            ) from None

    def _projection(
        self, target: EntitySetDef, select: Any, expand: Any
    ) -> tuple[list[str], list[tuple[NavigationDef, EntitySetDef]], list[str], list[str]]:
        """``(fields to return, expanded navigations, $select, $expand)``.

        A read never asks for "everything": without a ``select`` it asks for
        the selectable fields, so a field the admin did not release is not
        even transported. An expanded navigation contributes its target's
        selectable fields, and nothing else of the target. How the two
        options carry that is the dialect's (``projection``): V2 names the
        fields as ``Nav/Field`` in ``$select``, V4 as ``Nav($select=..)`` in
        ``$expand``. An ``expand`` entry is only ever a navigation name; the
        nested options are written here, never taken from the caller.
        """
        if select is not None and not isinstance(select, list):
            raise ODataError("invalid_argument", "select must be a list of field names")
        if expand is not None and not isinstance(expand, list):
            raise ODataError("invalid_argument", "expand must be a list of navigation names")
        names: list[str] = []
        for name in select or []:
            field = target.field(name) if isinstance(name, str) else None
            if field is None:
                raise ODataError(
                    "unknown_field", f"entity set {target.name!r} has no field {_shown(name)}"
                )
            if not field.selectable:
                raise ODataError("field_not_selectable", f"field {field.name!r} cannot be read")
            if field.name not in names:
                names.append(field.name)
        if not names:
            names = target.selectable_names()
        if not names:
            raise ODataError(
                "operation_disabled", f"entity set {target.name!r} has no readable field"
            )
        expands: list[tuple[NavigationDef, EntitySetDef]] = []
        nested: list[tuple[str, list[str]]] = []
        for name in expand or []:
            nav, nav_target = self._navigation_target(target, name, None)
            if any(nav.name == seen.name for seen, _ in expands):
                continue
            readable = nav_target.selectable_names()
            if not readable:
                # Nothing of it could be returned, and `$expand` without a
                # `$select` path for it would transport the whole entity.
                raise ODataError(
                    "operation_disabled",
                    f"entity set {nav_target.name!r} has no readable field, "
                    f"so navigation {nav.name!r} cannot be expanded",
                )
            expands.append((nav, nav_target))
            nested.append((nav.name, readable))
        sent, expanded = self._dialect.projection(names, nested)
        return names, expands, sent, expanded

    @staticmethod
    def _orderby(target: EntitySetDef, orderby: Any) -> list[str]:
        if orderby is not None and not isinstance(orderby, list):
            raise ODataError("invalid_argument", "orderby must be a list like ['Field desc']")
        out: list[str] = []
        for entry in orderby or []:
            match = _ORDERBY.match(" ".join(entry.split())) if isinstance(entry, str) else None
            if match is None:
                raise ODataError(
                    "invalid_argument",
                    "each orderby entry is one field name, optionally followed by asc or desc",
                )
            field = target.field(match.group(1))
            if field is None:
                raise ODataError(
                    "unknown_field",
                    f"entity set {target.name!r} has no field {_shown(match.group(1))}",
                )
            # The order of rows tells about the field it is sorted by.
            if not field.selectable:
                raise ODataError(
                    "field_not_selectable", f"field {field.name!r} cannot be used to sort"
                )
            out.append(field.name + (f" {match.group(2)}" if match.group(2) else ""))
        return out

    def check_read(
        self,
        entity_set: EntitySetDef,
        operation: str,
        *,
        key: Any = None,
        navigation: Any = None,
        select: Any = None,
        expand: Any = None,
        orderby: Any = None,
        filter: Any = None,
        top: Any = None,
        skip: Any = None,
        count: bool = True,
    ) -> ReadPlan:
        """Every catalogue check of a read, without sending anything.

        ``list`` and ``get`` run exactly this before they send, and a caller
        that wants the refusal first (the execute tool) calls it directly, so
        the same call always gets the same refusal. The first failing check
        raises ``ODataError``; the order is fixed:

        1. ``operation`` is ``"list"`` or ``"get"`` (``invalid_argument``);
        2. the operation is enabled: without a navigation, ``operation`` on
           ``entity_set``; with one, ``get`` on ``entity_set`` -- following a
           navigation by key reads through that entity
           (``operation_disabled``);
        3. the key: exactly the key fields with valid values for a ``get``
           and for a navigation (``invalid_key``); none on a plain ``list``
           (``invalid_argument``);
        4. the navigation: one of ``entity_set`` (``unknown_target``), a
           collection for ``list`` / a single entity for ``get``
           (``invalid_argument``), its target in the catalogue
           (``unknown_target``) with that operation enabled
           (``operation_disabled``); from here on every name is checked
           against the *target* entity set;
        5. ``select``: known (``unknown_field``) and selectable
           (``field_not_selectable``) fields; empty means all selectable;
        6. ``expand``: navigations of the target (``unknown_target``) whose
           own target is in the catalogue with the matching read enabled and
           at least one selectable field (``operation_disabled``);
        7. ``orderby``: ``Field [asc|desc]`` (``invalid_argument``) on known
           (``unknown_field``), selectable (``field_not_selectable``) fields
           of a type the dialect can sort by (``invalid_argument``; V4: a
           recognised primitive type, so no complex or collection field);
        8. ``filter``: a string, checked by ``urls.check_filter`` in the
           grammar of the service's version (``invalid_argument``,
           ``unknown_field``, ``field_not_filterable``); it is stripped, and
           a blank one is no filter;
        9. ``top`` (1..``MAX_PAGE_SIZE``) and ``skip`` (>= 0, ``None`` = 0)
           (``invalid_argument``).

        For a ``get``, steps 7-9 are one rule: ``orderby``, ``filter``,
        ``top`` and ``skip`` must be absent (``invalid_argument``).
        """
        if operation not in ("list", "get"):
            raise ODataError("invalid_argument", "a read is either 'list' or 'get'")
        path, target = self._resolve(entity_set, key, navigation, operation)
        names, expands, sent, expand_names = self._projection(target, select, expand)
        if operation == "get":
            if orderby or (filter is not None and filter != "") or top is not None or skip:
                raise ODataError(
                    "invalid_argument", "orderby, filter, top and skip are only used with 'list'"
                )
            query = ReadQuery(
                select=sent,
                filter=None,
                expand=expand_names,
                orderby=[],
                top=0,
                skip=0,
                count=False,
            )
            return ReadPlan(operation, path, target, names, expands, query)
        ordered = self._orderby(target, orderby)
        for entry in ordered:
            sorted_by = target.field(entry.partition(" ")[0])
            if sorted_by is None or not self._dialect.comparable(sorted_by.type):
                raise ODataError(
                    "invalid_argument",
                    f"field {entry.partition(' ')[0]!r} has a type that cannot be used to sort",
                )
        if filter is not None and not isinstance(filter, str):
            raise ODataError("invalid_argument", "filter must be a string")
        filter = (filter or "").strip() or None
        if filter:
            try:
                check_filter(
                    filter, {f.name: f for f in target.fields}, version=self._dialect.version
                )
            except FilterError as exc:
                raise ODataError(exc.code, exc.message) from None
        skip = 0 if skip is None else skip
        for label, number, low in (("top", top, 1), ("skip", skip, 0)):
            if isinstance(number, bool) or not isinstance(number, int) or number < low:
                raise ODataError(
                    "invalid_argument", f"{label} must be an integer of at least {low}"
                )
        if top > MAX_PAGE_SIZE:
            raise ODataError("invalid_argument", f"top must be at most {MAX_PAGE_SIZE}")
        query = ReadQuery(
            select=sent,
            filter=filter,
            expand=expand_names,
            orderby=ordered,
            top=top,
            skip=skip,
            count=bool(count),
        )
        return ReadPlan(operation, path, target, names, expands, query)

    def check_write(
        self,
        entity_set: EntitySetDef,
        operation: str,
        *,
        key: Any = None,
        body: Any = None,
        etag: Any = None,
    ) -> WritePlan:
        """Every catalogue check of a write, without sending anything.

        ``create``, ``update`` and ``delete`` run exactly this before they
        ask for a CSRF token or send, and a caller that wants the refusal
        first (the execute tool) calls it directly. The first failing check
        raises ``ODataError``; the order is fixed. No refusal repeats a
        value, only names.

        1. ``operation`` is ``"create"``, ``"update"`` or ``"delete"``
           (``invalid_argument``);
        2. the operation is enabled on ``entity_set``, and the service's
           OData version is one this client can write (V2 today)
           (``operation_disabled``);
        3. the key: exactly the key fields with valid values for an update
           and a delete (``invalid_key``); none for a create
           (``invalid_argument``);
        4. the body: an object with at least one field for a create and an
           update; none for a delete (``invalid_argument``);
        5. every body field, in the caller's order: its name is a field of
           the entity set (``unknown_field``) that is marked ``writable``
           (``field_not_writable``); the refusal names the field;
        6. every body value is ``null`` or a JSON scalar that has the form
           of the field's EDM type (``invalid_argument``, naming the field
           and the type). An object or a list is refused: a deep insert is
           not supported;
        7. update only: a key field in the body must carry the key's own
           value (``invalid_argument``) -- a key cannot be changed. Such a
           field is then left out of the request, and a body that holds
           nothing else is refused (``invalid_argument``);
        8. the encoded body is at most ``MAX_REQUEST_BYTES``
           (``invalid_argument``);
        9. ``etag``: not used with a create; else absent or one entity tag
           as a get returned it. ``*`` and a list of tags are refused
           (``invalid_argument``): this client never sends ``If-Match: *``.
        """
        if operation not in WRITE_OPERATIONS:
            raise ODataError(
                "invalid_argument", "a write is one of 'create', 'update' or 'delete'"
            )
        if operation not in entity_set.operations:
            raise ODataError(
                "operation_disabled",
                f"{operation!r} is not enabled for entity set {entity_set.name!r}",
            )
        if getattr(self._dialect, "supports_write", False) is not True:
            # Before the key and the body are looked at, and long before a
            # CSRF token is asked for: nothing of this call is sent.
            raise ODataError(
                "operation_disabled",
                getattr(self._dialect, "write_refusal", None)
                or "writing is not supported for the OData version of this service",
            )
        segment = entity_set.path or entity_set.name
        if operation == "create":
            if key is not None:
                raise ODataError(
                    "invalid_argument",
                    "a create takes no key; key fields the service expects go in the body",
                )
        else:
            segment += self._dialect.key_segment(entity_set, key)
        try:
            path = join_path(self._service_path, segment)
        except ValueError:
            raise ODataError(
                "invalid_argument", "the request path could not be built from these arguments"
            ) from None
        encoded: dict[str, Any] | None = None
        names: list[str] = []
        if operation == "delete":
            if body is not None:
                raise ODataError("invalid_argument", "a delete takes no body")
        else:
            if not isinstance(body, dict) or not body:
                raise ODataError(
                    "invalid_argument",
                    f"{operation!r} needs a body: an object of field names and values",
                )
            for name in body:
                definition = entity_set.field(name) if isinstance(name, str) else None
                if definition is None:
                    raise ODataError(
                        "unknown_field",
                        f"entity set {entity_set.name!r} has no field {_shown(name)}",
                    )
                if not definition.writable:
                    raise ODataError(
                        "field_not_writable", f"field {definition.name!r} cannot be written"
                    )
                names.append(definition.name)
            encoded = self._dialect.encode_body(entity_set, body)
            if operation == "update":
                for definition in entity_set.keys:
                    if definition.name not in encoded:
                        continue
                    try:
                        same = self._dialect.literal(
                            definition.type, body[definition.name]
                        ) == self._dialect.literal(definition.type, key[definition.name])
                    except ODataError:
                        same = False
                    if not same:
                        raise ODataError(
                            "invalid_argument",
                            f"key field {definition.name!r} cannot be changed: leave it "
                            f"out of the body or give it the value of the key",
                        )
                    del encoded[definition.name]
                    names.remove(definition.name)
                if not encoded:
                    raise ODataError(
                        "invalid_argument", "the body holds nothing to update besides the key"
                    )
            try:
                size = len(json.dumps(encoded).encode("utf-8"))
            except (TypeError, ValueError):
                raise ODataError("invalid_argument", "the body cannot be sent as JSON") from None
            if size > MAX_REQUEST_BYTES:
                raise ODataError(
                    "invalid_argument", f"the body is larger than {MAX_REQUEST_BYTES} bytes"
                )
        if etag is not None:
            if operation == "create":
                raise ODataError("invalid_argument", "an etag is not used with a create")
            if not isinstance(etag, str) or not _ETAG.fullmatch(etag):
                raise ODataError(
                    "invalid_argument",
                    "the etag is not one entity tag as a read of the entity returned it",
                    hint="read the entity with 'get' and use the etag of that answer; "
                    "'*' is never accepted",
                )
        return WritePlan(operation, path, entity_set, tuple(names), encoded, etag)

    def check_call(
        self,
        operation: OperationDef,
        *,
        key: Any = None,
        params: Any = None,
        etag: Any = None,
    ) -> CallPlan:
        """Every catalogue check of a function import call; nothing is sent.

        ``call`` runs exactly this before it asks for a CSRF token or sends,
        and a caller that wants the refusal first (the execute tool) calls
        it directly. The first failing check raises ``ODataError``; the
        order is fixed. No refusal repeats a value, only names.

        1. ``operation`` is an operation of this service's definition
           (``unknown_target``);
        2. it can be called at all (``call_refusal``, the rule the search
           tool offers by): it is ``enabled`` (``operation_disabled``); the
           service's OData version is one whose operations this client can
           call, V2 function imports today (``not_available``, the code and
           text the execute tool answers); and the catalogue declares it
           callable -- a bound operation's entity set exists and every key
           field is a declared parameter (SAP V2 takes the key of a bound
           function import as parameters, and nothing is sent under a name
           the catalogue does not declare), and every parameter that is
           always sent has a type the dialect writes (``not_available``,
           with a hint that it is a catalogue setting);
        3. ``params``: absent or an object of at most ``MAX_CALL_PARAMS``
           entries, every name a parameter the catalogue declares for the
           operation (``invalid_argument``);
        4. the key: an operation bound to an entity set needs exactly that
           set's key, with valid values (``invalid_key``; the refusal says
           "this operation", never the set's name, which the agent may not
           be able to see), and no key field in ``params`` as well
           (``invalid_argument``). An operation that is not bound takes no
           key (``invalid_argument``);
        5. every ``required`` parameter is there, and every value is one
           JSON scalar with the form of the parameter's EDM type; an
           object, a list and a type the dialect does not know are refused
           (``invalid_argument``, naming the parameter and the type);
        6. ``etag``: only for a call that changes data and is bound, then
           absent or one entity tag as a get of that entity returned it;
           ``*`` and a list of tags are refused (``invalid_argument``).
        """
        known = (
            self._definition.operation(operation.name)
            if isinstance(operation, OperationDef)
            else None
        )
        if known is None or known != operation:
            raise ODataError("unknown_target", "the service has no such operation")
        bound: EntitySetDef | None = None
        if operation.bound_to is not None:
            bound = self._definition.entity_set(operation.bound_to)
        bound_keys = None if bound is None else [k.name for k in bound.keys]
        reason = call_refusal(operation, bound_keys, self._dialect)
        if reason is not None:
            raise self._not_callable(operation, reason, bound_keys or [])
        if params is None:
            params = {}
        if not isinstance(params, dict) or len(params) > MAX_CALL_PARAMS:
            raise ODataError(
                "invalid_argument", "params must be an object of parameter names and values"
            )
        declared = {p.name for p in operation.parameters}
        expected = ", ".join(p.name for p in operation.parameters) or "none"
        for name in params:
            if not isinstance(name, str) or name not in declared:
                raise ODataError(
                    "invalid_argument",
                    f"operation {operation.name!r} has no parameter {_shown(name)}; "
                    f"its parameters are: {expected}",
                )
        if bound is not None:
            # The rules of a key in a path, although this one travels in the
            # query: the same key names the same entity everywhere.
            try:
                self._dialect.key_segment(bound, key)
            except ODataError as exc:
                # The agent may not be able to see the entity set (search
                # then hides `bound_to`): the key is the operation's.
                raise ODataError(
                    exc.code,
                    exc.message.replace(f"entity set {bound.name!r}", "this operation"),
                    hint=exc.hint,
                ) from None
            for definition in bound.keys:
                if definition.name in params:
                    raise ODataError(
                        "invalid_argument",
                        f"key field {definition.name!r} goes in 'key' only, not in 'params'",
                    )
        elif key is not None:
            raise ODataError(
                "invalid_argument",
                f"operation {operation.name!r} is not bound to an entity; it takes no key",
            )
        changes = call_changes_data(operation)
        if etag is not None:
            if bound is None or not changes:
                raise ODataError(
                    "invalid_argument",
                    "an etag is used only with an operation that changes one entity",
                )
            if not isinstance(etag, str) or not _ETAG.fullmatch(etag):
                raise ODataError(
                    "invalid_argument",
                    "the etag is not one entity tag as a read of the entity returned it",
                    hint="read the entity with 'get' and use the etag of that answer; "
                    "'*' is never accepted",
                )
        returns = operation.returns
        method, path, query, _ = self._dialect.call_request(
            self._service_path, operation, key if bound is not None else None, params
        )
        return CallPlan(
            name=operation.name,
            method=method,
            path=path,
            query=query,
            # What was sent: an optional parameter passed as null is not.
            fields=tuple(name for name in params if name in query),
            key=dict(key) if bound is not None else None,
            changes=changes,
            bound=bound,
            # `returns` wins for the RESULT, `bound_to` stays the KEY.
            result_set=(
                bound if returns is None else self._definition.entity_set(returns.entity_set)
            ),
            many=None if returns is None else returns.collection,
            etag=etag,
        )

    @staticmethod
    def _not_callable(
        operation: OperationDef, reason: str, bound_keys: Sequence[str]
    ) -> ODataError:
        """The refusal of a ``call_refusal`` reason. Names only."""
        name = operation.name
        if reason == "operation_disabled":
            return ODataError("operation_disabled", f"operation {name!r} is not enabled")
        if reason == "calls_not_available":
            return ODataError("not_available", CALL_NOT_AVAILABLE)
        if reason == "bound_set_missing":
            why = "the entity set it is bound to is not in the catalogue"
        elif reason == "key_not_declared":
            declared = {p.name for p in operation.parameters}
            missing = next((k for k in bound_keys if k not in declared), "")
            why = (
                f"it declares no parameter for key field {missing!r}, so it cannot be "
                f"called for one entity"
            )
        else:
            why = "one of its parameters has a type this tool does not send"
        return ODataError(
            "not_available",
            f"operation {name!r} cannot be called as the catalogue declares it: {why}",
            hint=_CATALOGUE_HINT,
        )

    # -- the call -----------------------------------------------------------
    def _sap_error(self, status: int, content_type: str, body: bytes | None) -> ODataError:
        """The error of a non-2xx answer: SAP's own code and text, or the status."""
        snapshot = httpx.Response(status, headers={"content-type": content_type}, content=body)
        code, text = self._dialect.parse_error(snapshot)
        code, text = _plain(code, 80), _plain(text)
        message = f"{code}: {text}" if code and text else text
        return ODataError(
            "sap_error",
            message[:MAX_MESSAGE_CHARS] if message else f"HTTP {status} from the OData service",
            status=status,
            hint=_STATUS_HINTS.get(status),
        )

    def _headers(self, **extra: str) -> dict[str, str]:
        """The headers of a request: JSON, plus what the dialect always sends."""
        return {
            "Accept": "application/json",
            **getattr(self._dialect, "request_headers", {}),
            **extra,
        }

    async def _fetch(self, url: str, params: dict[str, str] | None) -> tuple[Any, httpx.Headers]:
        """GET ``url`` (relative) and return the decoded JSON and the headers."""
        payload, headers, _ = await self._fetch_status(url, params)
        return payload, headers

    async def _fetch_status(
        self, url: str, params: dict[str, str] | None
    ) -> tuple[Any, httpx.Headers, int]:
        """GET ``url`` (relative): the decoded JSON, the headers and the status.

        A redirect is never followed: the next hop would be chosen by the
        answer, not by the destination.
        """
        body = bytearray()
        try:
            async with self._http.stream(
                "GET",
                url,
                params=params,
                headers=self._headers(),
                follow_redirects=False,
            ) as response:
                status = response.status_code
                headers = response.headers
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ODataError(
                            "sap_error",
                            f"the answer of the OData service is larger than "
                            f"{MAX_RESPONSE_BYTES} bytes",
                            status=status,
                            hint="ask for fewer rows, fewer fields or no expand",
                        )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # The exception text can carry the URL; only its type is logged.
            logger.warning("odata: request failed (%s)", type(exc).__name__)
            raise ODataError(
                "destination_error", "the OData service could not be reached"
            ) from None
        if status >= 300:
            raise self._sap_error(status, headers.get("content-type", ""), bytes(body))
        if status == 204 or not body:
            return None, headers, status
        try:
            return json.loads(body), headers, status
        except ValueError:
            raise ODataError(
                "sap_error",
                f"the OData service did not answer with JSON (HTTP {status})",
                status=status,
                hint="a sign-in page instead of data usually means the destination's "
                "credential was not accepted",
            ) from None

    def _row(
        self,
        row: Any,
        target: EntitySetDef,
        names: list[str],
        expands: list[tuple[NavigationDef, EntitySetDef]],
    ) -> dict[str, Any]:
        """``row`` cut to the fields that were asked for and may be read."""
        if not isinstance(row, dict):
            return {}
        allowed = set(target.selectable_names())
        out: dict[str, Any] = {}
        for name in names:
            if name in allowed and name in row:
                value = _value(row[name])
                if value is not _DROP:
                    out[name] = value
        for nav, nav_target in expands:
            if nav.name not in row:
                continue
            value = row[nav.name]
            inner_names = nav_target.selectable_names()
            if nav.collection:
                rows = value.get("results") if isinstance(value, dict) else value
                if isinstance(rows, list):
                    out[nav.name] = [self._row(r, nav_target, inner_names, []) for r in rows]
            elif value is None:
                out[nav.name] = None
            elif isinstance(value, dict) and "__deferred" not in value:
                out[nav.name] = self._row(value, nav_target, inner_names, [])
        return out

    async def list(
        self,
        entity_set: EntitySetDef,
        query: ReadQuery,
        *,
        key: dict | None = None,
        navigation: str | None = None,
    ) -> dict:
        """One page of an entity set, or of a collection navigation of one entity.

        With ``navigation`` the fields, the filter and the sort order are
        those of the navigation's target entity set.

        When the back end pages on its own, its paging link is followed
        through ``urls.confine_next_link``: the link's host is dropped and
        only its path is used, so the follow-up request always goes through
        the destination like the first one, never to the host the link
        names. The link's query string is *not* trusted and not checked: it
        is the back end's own continuation of this resource (it may carry a
        different ``$select``), which is safe because every row, of every
        page, is cut to the catalogue's selectable fields that were asked
        for before it is returned.
        """
        plan = self.check_read(
            entity_set,
            "list",
            key=key,
            navigation=navigation,
            select=query.select,
            expand=query.expand,
            orderby=query.orderby,
            filter=query.filter,
            top=query.top,
            skip=query.skip,
            count=query.count,
        )
        path, target, names, expands = plan.path, plan.target, plan.names, plan.expands
        query = plan.query
        params = self._dialect.read_params(query)
        payload, _ = await self._fetch(path, params)
        rows, count, next_link = self._dialect.parse_list(payload)
        rows = list(rows)
        hops = 0
        # The back end may page on its own and return fewer rows than `$top`.
        # Its link is followed only when it is this very resource again.
        while len(rows) < query.top and next_link and hops < MAX_NEXT_HOPS:
            link = confine_next_link(next_link, self._service_path)
            if link is None or unquote(link.partition("?")[0]) != unquote(path):
                break
            hops += 1
            payload, _ = await self._fetch(link, None)
            more, _, next_link = self._dialect.parse_list(payload)
            if not more:
                next_link = None
                break
            rows.extend(more)
        items = [self._row(row, target, names, expands) for row in rows[: query.top]]
        result: dict[str, Any] = {"items": items}
        if count is not None:
            result["count"] = count
        reached = query.skip + len(items)
        if (
            len(rows) > query.top
            or next_link
            or (count is not None and reached < count)
            or (count is None and len(items) == query.top)
        ):
            result["next_skip"] = reached
        result["truncated"] = False
        return result

    async def get(
        self,
        entity_set: EntitySetDef,
        key: dict,
        *,
        select: list[str],
        expand: list[str],
        navigation: str | None = None,
    ) -> dict:
        """One entity by key, or the single entity a navigation leads to."""
        plan = self.check_read(
            entity_set, "get", key=key, navigation=navigation, select=select, expand=expand
        )
        path, target, names, expands = plan.path, plan.target, plan.names, plan.expands
        params = self._dialect.read_params(plan.query)
        payload, headers = await self._fetch(path, params)
        if payload is None or self._dialect.is_null_entity(payload):
            # A single-valued navigation that leads nowhere.
            return {"item": None, "truncated": False}
        row, etag = self._dialect.parse_entity(payload)
        result: dict[str, Any] = {"item": self._row(row, target, names, expands)}
        # The same rule a write applies to `If-Match`: what is handed out
        # here is always accepted there, and anything else is not an ETag.
        etag = _etag_of(etag, headers.get("etag"))
        if etag:
            result[RAW_ETAG_FIELD] = etag
        result["truncated"] = False
        return result

    # -- writes -------------------------------------------------------------
    def _session_key(self) -> tuple[Any, Any]:
        """The store and the session key of the identity this request runs as.

        Fails closed when the pieces do not line up: the key must describe
        the same identity the HTTP client's auth will send the request as,
        or a user's cookie could travel with another credential.
        """
        if self._sessions is None or self._http is None:
            raise ODataError("destination_error", "this service is not set up for writing")
        if (
            not isinstance(self._destination, str)
            or not self._destination
            or not isinstance(self._user_context, bool)
        ):
            raise ODataError(
                "destination_error", "the service names no destination to write through"
            )
        auth = self._http.auth
        if (
            getattr(auth, "user_context", None) is not self._user_context
            or getattr(auth, "destination_name", None) != self._destination
        ):
            raise ODataError(
                "destination_error",
                "the connection of this service does not match its destination settings",
            )
        # Raises DestinationUserRequired for a user-context service without a
        # signed-in user; there is no fall-back to the technical entry.
        return self._sessions, self._sessions.key(self._destination, self._user_context)

    async def _fetch_session(self) -> CsrfSession:
        """Ask SAP for a CSRF token: a GET on the service root, as the caller.

        Runs in the caller's own context (``CsrfSessionStore.get``), so the
        destination resolves the caller's credential and the token and the
        cookies that come back are that identity's. The body is not read.

        This is not the write: whatever goes wrong here, the change was not
        sent by it, and every error says "nothing was changed". A
        ``DestinationError`` (no signed-in user, the destination could not
        be resolved) passes through; a cancellation is not caught.
        """
        body = bytearray()
        try:
            async with self._http.stream(
                "GET",
                join_path(self._service_path) + "/",
                headers=self._headers(**{"X-CSRF-Token": "Fetch"}),
                follow_redirects=False,
            ) as response:
                status = response.status_code
                content_type = response.headers.get("content-type", "")
                token = response.headers.get("x-csrf-token", "")
                cookies = cookies_from_response(response)
                if status >= 300:
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_RESPONSE_BYTES:
                            break
        except DestinationError:
            raise
        except Exception as exc:  # noqa: BLE001 - not the write: nothing was changed
            # The exception text can carry the URL; only its type is logged.
            logger.warning("odata: CSRF token request failed (%s)", type(exc).__name__)
            raise ODataError(
                "destination_error",
                "the OData service could not be reached; nothing was changed",
            ) from None
        if status >= 300:
            error = self._sap_error(status, content_type, bytes(body))
            suffix = " (the CSRF token request was refused; nothing was changed)"
            error.message = error.message[: MAX_MESSAGE_CHARS - len(suffix)] + suffix
            error.args = (error.message,)
            raise error
        if not _CSRF_TOKEN.fullmatch(token) or token.lower() in ("required", "fetch"):
            raise ODataError(
                "sap_error",
                "the OData service did not hand out a CSRF token; nothing was changed",
                status=status,
            )
        return CsrfSession.fresh(token, cookies)

    async def _modify(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        content: bytes | None,
        *,
        operation: str = "",
        params: dict[str, str] | None = None,
    ) -> tuple[int, httpx.Headers, bytes | None]:
        """Send one modifying request. ``(status, headers, body)``.

        Never retried here. What can go wrong is told apart by whether the
        request can have reached SAP:

        * a ``DestinationError`` (the destination could not be resolved, no
          signed-in user) and a connection that was never made: nothing was
          sent, and the caller is told so (``sent`` stays ``False``);
        * any other failure before SAP's status line arrived -- an httpx
          error, a transport's own exception, an inner timeout -- leaves the
          outcome open: ``write_outcome_unknown`` with ``sent=True``;
        * once the status is known it stands; a body that could not be read
          or is too large is ``None``.

        A cancellation is not caught: it passes through, and leaving the
        ``stream`` block closes the response on the way out.
        """
        body: bytearray | None = bytearray()
        status: int | None = None
        answer = httpx.Headers()
        try:
            async with self._http.stream(
                method,
                url,
                params=params,
                headers=headers,
                content=content,
                follow_redirects=False,
            ) as response:
                status = response.status_code
                answer = response.headers
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        body = None
                        break
        except DestinationError:
            if status is None:
                raise
            body = None
        except Exception as exc:  # noqa: BLE001 - every failure of a write gets a verdict
            # The exception text can carry the URL; only its type is logged.
            logger.warning(
                "odata: modifying request failed (%s), status %s",
                type(exc).__name__,
                status if status is not None else "unknown",
            )
            if status is None:
                if isinstance(exc, _NOT_SENT):
                    raise ODataError(
                        "destination_error",
                        "the OData service could not be reached; nothing was changed",
                    ) from None
                raise ODataError(
                    "write_outcome_unknown",
                    "the connection failed while the change was being sent: it is not "
                    "known whether SAP applied it. The request was not repeated",
                    hint=_outcome_hint(operation),
                    sent=True,
                ) from None
            body = None
        return status, answer, None if body is None else bytes(body)

    def _is_entity(self, decoded: Any) -> bool:
        """Whether a decoded write answer is an entity as the dialect reads one.

        A JSON object with a top-level ``error`` key is an error, whatever
        else it holds and whatever status it came with.
        """
        if not isinstance(decoded, dict) or "error" in decoded:
            return False
        try:
            self._dialect.parse_entity(decoded)
        except ODataError:
            return False
        return True

    def _confirmed(self, plan: WritePlan | CallPlan, status: int, body: bytes | None) -> Any:
        """The decoded answer of a write that SAP confirmed, else an error.

        Success is recognised, not assumed. A status below 300 alone says
        little: a sign-in page or a SAML form arrives as ``200 text/html``,
        and a proxy can wrap an error in a 200.

        * create: 201, or 200 with the created entity. A body, when there is
          one, must be JSON the dialect reads as an entity;
        * update, delete: 204 without a body, or 200 with no body or with
          the entity. Any other body -- text, markup, JSON that is not an
          entity, JSON with a top-level ``error`` -- is not a confirmation.
        * call: 204 without a body, or 200/201 with no body or with a JSON
          object that has the ``d`` member of a V2 answer and no ``error``.

        Everything else below 300 was answered by something, but not
        recognisably by the write: ``write_outcome_unknown``.
        """
        decoded: Any = None
        ok = False
        if body:
            try:
                decoded = json.loads(body)
                readable = True
            except ValueError:
                readable = False
        else:
            readable = body is not None  # b"": nothing to read, nothing wrong
        if plan.operation == "call":
            if body:
                ok = (
                    status in (200, 201)
                    and isinstance(decoded, dict)
                    and "d" in decoded
                    and "error" not in decoded
                )
            else:
                ok = status == 204 or (status in (200, 201) and body is not None)
        elif plan.operation == "create":
            if decoded is not None:
                ok = status in (200, 201) and self._is_entity(decoded)
            else:
                # Created, and the echo is absent (or could not be read).
                ok = status == 201 and (readable or body is None)
        elif body:
            ok = status == 200 and self._is_entity(decoded)
        else:
            # No body. One that could not be read (`None`) counts only with
            # the status that never has one.
            ok = status == 204 or (status == 200 and body is not None)
        if not ok:
            raise ODataError(
                "write_outcome_unknown",
                f"the answer (HTTP {status}) is not the answer of a completed "
                f"{plan.operation}: it is not known whether SAP applied the change",
                status=status,
                hint=_outcome_hint(plan.operation)
                + "; a sign-in page instead of an answer usually means the "
                "destination's credential was not accepted",
            )
        return decoded

    async def _renew(self, sessions: Any, key: Any, used: CsrfSession) -> CsrfSession:
        """A session to retry with after SAP refused ``used`` as stale.

        The drop is conditional on ``used``: when another call of the same
        identity already replaced the session, that newer one is taken as it
        is instead of being thrown away again.
        """
        sessions.drop(key, used)
        return await sessions.get(key, self._fetch_session)

    async def _write(
        self,
        plan: WritePlan | CallPlan,
        method: str,
        extra: dict[str, str],
        *,
        params: dict[str, str] | None = None,
    ) -> tuple[int, httpx.Headers, Any]:
        """Send ``plan`` with the caller's CSRF session.

        ``(status, headers, decoded JSON body or None)`` of a write SAP
        confirmed (``_confirmed``); everything else raises. Every
        ``ODataError`` leaves with ``sent`` set: ``False`` until the first
        modifying request was handed to an open connection, ``True`` from
        then on -- also for a failure of the token renewal or of the second
        send after SAP refused the first one unprocessed (403 ``Required``):
        nothing was changed then, but a modifying request did go out.
        """
        sessions, key = self._session_key()
        headers = self._headers(**extra)
        content: bytes | None = None
        if plan.body is not None:
            content = json.dumps(plan.body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if plan.etag is not None:
            headers["If-Match"] = plan.etag
        session = await sessions.get(key, self._fetch_session)
        left = False  # a modifying request went out at least once
        try:
            attempt = 0
            while True:
                attempt += 1
                sent = dict(headers)
                sent["X-CSRF-Token"] = session.token
                cookie = session.cookie_header()
                if cookie:
                    # Explicitly, from this identity's own entry, for this request only.
                    sent["Cookie"] = cookie
                # `_modify` says itself whether its request got out (a
                # connection that was never made did not).
                status, answer, body = await self._modify(
                    method, plan.path, sent, content, operation=plan.operation, params=params
                )
                left = True
                stale = (
                    status == 403 and answer.get("x-csrf-token", "").strip().lower() == "required"
                )
                if not stale:
                    break
                if attempt == 2:
                    # Refused twice: the error below is the answer. The entry is
                    # of no use to the next call either (unless it is newer already).
                    sessions.drop(key, session)
                    break
                # SAP refused the request before processing it, so sending it
                # again changes nothing twice. Once.
                session = await self._renew(sessions, key, session)
            return status, answer, self._settle(plan, sessions, key, session, status, answer, body)
        except ODataError as exc:
            exc.sent = exc.sent or left
            raise
        except DestinationError as exc:
            if not left:
                raise  # before the first send: the caller maps it, as on a read
            # The first send was refused unprocessed, and the renewal or the
            # second send could not use the destination. The text can name
            # the destination or its URL: only the type is logged.
            logger.warning(
                "odata: destination failed after a refused %s (%s)",
                plan.operation,
                type(exc).__name__,
            )
            raise ODataError(
                "destination_error",
                "SAP asked for a new CSRF token and the destination could not be used "
                "to get one or to send the change again; nothing was changed",
                sent=True,
            ) from None

    def _settle(
        self,
        plan: WritePlan | CallPlan,
        sessions: Any,
        key: Any,
        session: CsrfSession,
        status: int,
        answer: httpx.Headers,
        body: bytes | None,
    ) -> Any:
        """The verdict on the answer of a modifying request that was sent.

        The decoded body of a confirmed write; every other answer raises.
        """
        stale = status == 403 and answer.get("x-csrf-token", "").strip().lower() == "required"
        logger.info("odata: %s on %s answered HTTP %s", plan.operation, _subject(plan), status)
        if status >= 300:
            # The session stays as it was sent: cookies of a refusal are not
            # taken (see the module docstring).
            error = self._sap_error(status, answer.get("content-type", ""), body)
            if status == 428:
                raise ODataError(
                    "etag_required",
                    "SAP changes this entity only with the etag of its current version",
                    status=status,
                    hint="read the entity with 'get' first, then repeat the call with "
                    "the etag of that answer"
                    if not isinstance(plan, CallPlan) or plan.bound is not None
                    else "this operation is not bound to an entity in the catalogue, so no "
                    "etag can be passed; tell the user instead of retrying",
                )
            if status == 412:
                error.hint = "read the entity again and retry with its etag"
            elif stale:
                error.hint = "SAP did not accept the CSRF token; nothing was changed"
            elif status in (502, 504) and error.message.startswith("HTTP "):
                # A gateway on the way gave up, not SAP: the request may have
                # arrived and been processed all the same.
                raise ODataError(
                    "write_outcome_unknown",
                    f"a gateway answered HTTP {status} for the change: it is not known "
                    f"whether SAP applied it. The request was not repeated",
                    status=status,
                    hint=_outcome_hint(plan.operation),
                )
            raise error
        try:
            decoded = self._confirmed(plan, status, body)
        except ODataError:
            # Something answered in SAP's place (a sign-in page, usually).
            # Its cookies are not this identity's session, and the session
            # the request went out with did not get the write through: it is
            # ended here (unless a newer one replaced it already), so the
            # next write starts from a fresh token.
            sessions.drop(key, session)
            raise
        # Only now, with the write confirmed, is the answer known to be
        # SAP's own: when it moved the session on, the next write of this
        # identity sends the new cookies. Done only while the store still
        # holds the session this request was sent with.
        rotated = cookies_from_response(httpx.Response(status, headers=answer))
        if any(session.cookies.get(name) != value for name, value in rotated.items()):
            sessions.update(key, session, {**session.cookies, **rotated})
        return decoded

    async def create(self, entity_set: EntitySetDef, body: dict) -> dict:
        """Create one entity. ``{"item", "status"}`` plus the raw ETag if any.

        ``item`` is what SAP answered, cut to the selectable fields of the
        entity set exactly like a read: a value the admin did not release
        does not come back just because the caller wrote the entity.
        """
        plan = self.check_write(entity_set, "create", body=body)
        status, headers, payload = await self._write(plan, "POST", {})
        item: dict[str, Any] = {}
        inner: str | None = None
        if payload is not None:
            row, inner = self._dialect.parse_entity(payload)  # accepted by _confirmed
            item = self._row(row, entity_set, entity_set.selectable_names(), [])
        result: dict[str, Any] = {"item": item, "status": status}
        etag = _etag_of(inner, headers.get("etag"))
        if etag:
            result[RAW_ETAG_FIELD] = etag
        return result

    async def update(
        self, entity_set: EntitySetDef, key: dict, body: dict, *, etag: str | None = None
    ) -> dict:
        """Change the given fields of one entity; the others stay as they are.

        ``etag`` is the value a read of the entity returned; it is sent as
        ``If-Match`` so a change made by somebody else in between is not
        overwritten. Returns ``{"ok", "status"}`` plus the entity's new raw
        ETag when SAP sends one.
        """
        plan = self.check_write(entity_set, "update", key=key, body=body, etag=etag)
        method, extra = self._dialect.update_request(plan.path, plan.body)
        status, headers, _ = await self._write(plan, method, dict(extra))
        result: dict[str, Any] = {"ok": True, "status": status}
        new_etag = _etag_of(headers.get("etag"))
        if new_etag:
            result[RAW_ETAG_FIELD] = new_etag
        return result

    async def delete(self, entity_set: EntitySetDef, key: dict, *, etag: str | None = None) -> dict:
        """Delete one entity by key. ``{"ok", "status"}``; no body comes back."""
        plan = self.check_write(entity_set, "delete", key=key, etag=etag)
        status, _, _ = await self._write(plan, "DELETE", {})
        return {"ok": True, "status": status}

    # -- function imports ---------------------------------------------------
    def _returned(self, plan: CallPlan, shape: str, value: Any) -> tuple[str, Any]:
        """``(returned, result)``: what of a call's answer may be shown.

        The catalogue's field allowlist is per entity set, so entity data is
        shown only when it can be tied to a catalogue entity set positively
        (``plan.result_set``): the set the operation's ``returns`` names,
        or, when the catalogue does not say what it returns, the set it is
        bound to. That set must have the read enabled that matches what
        came back -- ``get`` for one entity, ``list`` for a collection;
        with ``returns`` the answer must also have the declared shape,
        without it either read will do -- and EVERY returned entity must
        say in ``__metadata.type`` that it is of that set's entity type. It
        is then cut to the set's selectable fields exactly like a read
        (``_row``: no ``__metadata``, no ETag). Anything else that looks
        like data -- an entity of another or of no stated type, a set
        nobody may read, a complex type, a list of values -- is
        ``withheld``: the caller learns that the call worked, not what it
        returned.

        A single JSON scalar is passed on for a call that only reads (it is
        the whole point of such a function): a number or a boolean as it
        is, a text only when it is one printable line of at most
        ``MAX_CALL_VALUE_CHARS`` characters once the white space around it
        is dropped -- a longer or multi-line text is a document, not a
        value. Never for a call that changes data, whose answer is the
        confirmation.
        """
        if shape == "none":
            return "nothing", None
        if shape == "value":
            if plan.changes:
                return "withheld", None
            if isinstance(value, str):
                value = value.strip()
                if len(value) > MAX_CALL_VALUE_CHARS or not value.isprintable():
                    return "withheld", None
            return "value", value
        target = plan.result_set
        if shape not in ("entity", "collection") or target is None or not target.entity_type:
            return "withheld", None
        many = shape == "collection"
        if plan.many is None:
            reads = {"get", "list"}
        elif plan.many is not many:
            return "withheld", None  # one was declared and many came, or the reverse
        else:
            reads = {"list"} if many else {"get"}
        names = target.selectable_names()
        if not reads & set(target.operations) or not names:
            return "withheld", None
        rows = value if many else [value]
        for row in rows:
            metadata = row.get("__metadata") if isinstance(row, dict) else None
            if not isinstance(metadata, dict) or metadata.get("type") != target.entity_type:
                return "withheld", None
        cut = [self._row(row, target, names, []) for row in rows]
        return ("entities", cut) if many else ("entity", cut[0])

    async def call(
        self,
        operation: OperationDef,
        *,
        key: dict | None = None,
        params: dict | None = None,
        etag: str | None = None,
    ) -> dict:
        """Call one function import. ``{"ok", "status", "returned", "result"}``.

        ``returned`` says what ``result`` is: ``entity`` / ``entities`` (cut
        to the selectable fields of the entity set the operation returns,
        or is bound to), ``value`` (one scalar, read-type calls only), ``nothing`` (SAP
        sent no content) or ``withheld`` (SAP sent something that could not
        be tied to a catalogue entity set, so it is not passed on);
        ``result`` is ``None`` for the last two. See ``_returned``.

        A call that changes data (``call_changes_data``) is sent like an
        entity write, whatever its HTTP method: with the caller's CSRF
        session, never repeated after a failure, and confirmed positively
        (``_confirmed``) -- every ``ODataError`` then carries ``sent``. A
        call that only reads is a plain GET, like a read. No ETag of the
        answer is ever returned.
        """
        plan = self.check_call(operation, key=key, params=params, etag=etag)
        if not plan.changes:
            payload, _, status = await self._fetch_status(plan.path, plan.query)
            shape, value = self._dialect.parse_call(payload, plan.name)
        else:
            status, _, payload = await self._write(plan, plan.method, {}, params=plan.query)
            try:
                shape, value = self._dialect.parse_call(payload, plan.name)
            except ODataError:
                # Confirmed by `_confirmed`: SAP applied it. An answer that
                # cannot be read then is merely not shown.
                shape, value = "other", None
        returned, result = self._returned(plan, shape, value)
        return {"ok": True, "status": status, "returned": returned, "result": result}


# -- the size cap ---------------------------------------------------------------


def _size(result: dict) -> int:
    return len(json.dumps(result))


def _fit_list(result: dict, key: str, max_chars: int) -> int:
    """Drop trailing entries of ``result[key]`` until it fits; how many went."""
    rows = result[key]
    result[key] = []
    budget = max_chars - _size(result)
    kept = 0
    for row in rows:
        budget -= len(json.dumps(row)) + (2 if kept else 0)  # ", " between entries
        if budget < 0:
            break
        kept += 1
    result[key] = rows[:kept]
    while result[key] and _size(result) > max_chars:
        result[key] = result[key][:-1]
    return len(rows) - len(result[key])


def _fit_value(result: dict, key: str, max_chars: int) -> None:
    """Shrink one entity (or a scalar): nested lists first, then long texts."""
    value = result[key]
    if isinstance(value, str):
        while value and _size(result) > max_chars:
            value = value[: len(value) // 2]
            result[key] = value
        return
    if not isinstance(value, dict):
        return
    for name in sorted(
        (n for n, v in value.items() if isinstance(v, list)),
        key=lambda n: -len(json.dumps(value[n])),
    ):
        if _size(result) <= max_chars:
            return
        rows = value[name]
        value[name] = []
        over = _size(result) - max_chars
        if over < 0:
            holder = {"rows": rows}
            _fit_list(holder, "rows", -over + len(json.dumps({"rows": []})))
            value[name] = holder["rows"]
    while _size(result) > max_chars:
        texts = [n for n, v in value.items() if isinstance(v, str) and v]
        if not texts:
            return
        longest = max(texts, key=lambda n: len(value[n]))
        value[longest] = value[longest][: len(value[longest]) // 2]


def clip_result(result: dict, max_chars: int, *, skip: int | None = None) -> dict:
    """``result`` cut so that ``json.dumps`` of it fits ``max_chars``.

    A tool result goes into the model's context, so its size is capped where
    the unit is whole rows: trailing items are dropped and ``truncated`` is
    set, and ``next_skip`` is moved back so that the next page starts at the
    first dropped row. When the result had no ``next_skip`` (it was the last
    page) the cut page gets one only if the caller passes the ``skip`` it
    asked with. The input is not changed.
    """
    out = copy.deepcopy(result)
    out["truncated"] = bool(out.get("truncated", False))
    if _size(out) <= max_chars:
        return out
    out["truncated"] = True
    for key in ("items", "result"):
        if isinstance(out.get(key), list):
            dropped = _fit_list(out, key, max_chars)
            if key == "items" and dropped:
                if isinstance(out.get("next_skip"), int):
                    out["next_skip"] -= dropped
                elif skip is not None:
                    out["next_skip"] = skip + len(out[key])
    for key in ("item", "result"):
        if key in out and _size(out) > max_chars:
            _fit_value(out, key, max_chars)
            if _size(out) > max_chars:
                out[key] = None
    return out
