"""What is specific to OData V4: literals, options, bodies, calls, payloads.

The same seams as ``v2.V2Dialect``, so ``ODataClient`` and its one gate
(``check_read``) stay the same code for both versions. Which dialect a
service gets is decided by the ``odata_version`` the catalogue declares; no
answer is ever looked at to find out, and an answer in the other version's
shape is an error, not data.

What differs from V2:

* A key predicate holds V4 literals: a string in single quotes with a quote
  doubled, everything else bare -- no ``guid'..'`` / ``datetime'..'`` prefix
  and no ``L`` / ``M`` / ``d`` suffix. As in V2 every value is written by
  positive recognition of its EDM type; the predicate itself is built by
  ``common.key_segment``, the same code for both versions. A type this
  module does not know -- an
  enumeration, a complex type, a collection, ``Edm.Binary`` -- is refused,
  never guessed: the type string comes verbatim from ``$metadata``, and its
  name alone does not say what kind of type it is.
* ``$count=true`` and ``@odata.count`` instead of ``$inlinecount``; rows in
  ``value``; no ``$format`` (JSON is asked for with ``Accept``).
* ``$expand`` carries its own ``$select`` (``Nav($select=A,B)``): the
  expanded entity's fields are named there, limited to what the catalogue
  marks selectable on the target, and not as ``Nav/Field`` in ``$select``.
* The error envelope is ``{"error": {"code", "message", "details"}}`` with a
  plain string as the message. Only code and message are read; ``details``
  and ``innererror`` can name hosts, users and internal objects.
* Every request says ``OData-MaxVersion: 4.0``, so a service that could
  answer in a later format does not. That is a request header, not a way of
  finding out the version.

Writes and calls:

* An update is a plain ``PATCH`` (no ``X-HTTP-Method`` tunnel); the ETag of
  an entity is ``@odata.etag``.
* A request body carries JSON values of the declared type: numbers stay
  numbers (``Edm.Decimal`` and ``Edm.Int64`` too -- this client does not ask
  for ``IEEE754Compatible``), given as a number or as its text. A decimal
  with more digits than a JSON number carries exactly is refused, never
  rounded.
* An action is a ``POST`` with its parameters as a JSON object; a function
  a ``GET`` with its parameters as literals behind its name,
  ``Name(P1='x',P2=5)``. An operation bound to an entity follows the key
  predicate of that entity under its namespace-qualified name
  (``Set('1')/NS.Name``); an unbound one is addressed by its import name.
  Every piece of the path is one confined segment built from catalogue
  names and typed literals.
* What a call answers is recognised positively (``parse_call``), and an
  entity is tied to a catalogue entity set by the ``@odata.context`` of the
  answer (``returned_rows_are_of``); anything else is not shown.

No refusal repeats a value. The payload shapes follow the OData 4.0 JSON
format and are not verified against a live system.
"""

from __future__ import annotations

import datetime
import decimal
import math
import re
from typing import Any
from urllib.parse import quote

import httpx

from . import common
from .client import ODataError, ReadQuery
from .common import refuse as _refuse
from .models import EDM_NAME_RE, EntitySetDef, OperationDef
from .urls import MAX_SEGMENT, V4_COMPARABLE_TYPES, join_path

# What one parameter value of a function may weigh: it travels in the path.
MAX_PARAM_VALUE_CHARS = common.MAX_KEY_VALUE_CHARS
# What a call answered, as `V4Dialect.parse_call` names it (as in V2).
CALL_SHAPES = ("none", "value", "entity", "collection", "other")
# The longest `@odata.context` that is looked at.
MAX_CONTEXT_CHARS = 2000

# Used with `fullmatch` only, and with `[0-9]`: `$` also matches before a
# trailing newline, and `\d` matches every Unicode decimal digit.
_INTEGER = re.compile(r"-?[0-9]{1,19}")
_DECIMAL = re.compile(r"-?[0-9]{1,40}(?:\.[0-9]{1,40})?")
_GUID = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
_DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_TIME_OF_DAY = re.compile(r"([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.[0-9]{1,12})?)?")
_OFFSET = re.compile(r"Z|[+-]([0-9]{2}):([0-9]{2})")
_DURATION = re.compile(
    r"-?P(?:[0-9]{1,6}D)?(?:T(?:[0-9]{1,2}H)?(?:[0-9]{1,2}M)?(?:[0-9]{1,2}(?:\.[0-9]{1,12})?S)?)?"
)
_INTEGER_RANGES = {
    "Edm.Byte": (0, 255),
    "Edm.SByte": (-128, 127),
    "Edm.Int16": (-(2**15), 2**15 - 1),
    "Edm.Int32": (-(2**31), 2**31 - 1),
    "Edm.Int64": (-(2**63), 2**63 - 1),
}
_FLOAT_TYPES = ("Edm.Double", "Edm.Single")
_STRING_FORMS = ("Edm.Guid", "Edm.Date", "Edm.DateTimeOffset", "Edm.TimeOfDay", "Edm.Duration")
# What a text in a path segment may not hold, as for a key (`common.key_segment`).
_PATH_BREAKERS = ("/", "\\", "%", "?", "#")
# `Set(A,B)`: the select list a context may carry behind the entity set.
_SELECT_LIST = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*(?:,[A-Za-z_][A-Za-z0-9_.]*)*")
_METADATA = "$metadata#"
_ENTITY = "/$entity"
_VALUE_HINTS = {
    "Edm.Date": "write it as 2026-10-05",
    "Edm.DateTimeOffset": "write it with its offset, for example 2026-10-05T12:00:00Z",
    "Edm.TimeOfDay": "write it as 12:30:00",
    "Edm.Duration": "write it as an ISO 8601 duration, for example P1DT2H",
    "Edm.Guid": "write it as 8-4-4-4-12 hexadecimal digits",
    "Edm.Decimal": 'pass it as text, for example "12.50", with at most 15 significant '
    "digits; no value between -0.0001 and 0.0001 other than 0 can be sent; a value "
    "that cannot be sent exactly in plain digits is not rounded",
}
# In a URL (a function parameter) a decimal given as text goes out as its
# own digits, so only a NUMBER has a limit there.
_LITERAL_HINTS = {
    **_VALUE_HINTS,
    "Edm.Decimal": common.DECIMAL_TEXT_HINT,
}


def _type_shown(edm_type: str) -> str:
    """A type for a refusal: repeated only when it has the form of a name."""
    return edm_type if re.fullmatch(r"[A-Za-z0-9_.]{1,64}", edm_type) else "its type"


def _is_date(text: str) -> bool:
    match = _DATE.fullmatch(text)
    if match is None:
        return False
    try:
        datetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return False
    return True


def _is_time_of_day(text: str) -> bool:
    match = _TIME_OF_DAY.fullmatch(text)
    return bool(
        match
        and int(match.group(1)) < 24
        and int(match.group(2)) < 60
        and int(match.group(3) or 0) < 60
    )


def _is_timestamp(text: str) -> bool:
    """``<date>T<time><offset>``; the offset is required in V4."""
    day, separator, rest = text.partition("T")
    if not separator or not _is_date(day):
        return False
    cut = max(rest.rfind("Z"), rest.rfind("+"), rest.rfind("-"))
    if cut <= 0:
        return False
    offset = _OFFSET.fullmatch(rest[cut:])
    if offset is None or not _is_time_of_day(rest[:cut]):
        return False
    return offset.group(1) is None or (int(offset.group(1)) < 15 and int(offset.group(2)) < 60)


class V4Dialect:
    """OData V4 as SAP's RAP and Gateway V4 services speak it."""

    version = "v4"
    supports_write = True
    # Actions and functions can be called (`call_request`, `parse_call`).
    supports_call = True
    call_kinds = ("action", "function")
    # The key of a bound operation is the key predicate of its entity, in
    # the path; `call_request` therefore takes the entity set as `bound=`.
    key_in_path = True
    # Sent with every request of this version.
    request_headers: dict[str, str] = {"OData-MaxVersion": "4.0"}

    # -- literals -----------------------------------------------------------
    def literal(self, edm_type: str, value: Any) -> str:
        """``value`` as a V4 URI literal of ``edm_type`` (not percent-encoded).

        Deny by default: a type this method does not know, or a value that
        does not have the exact form of its type, is refused. Only an
        ``Edm.String`` is quoted (with the quote doubled); every other value
        is bare and can therefore be nothing but what its pattern allows.
        """
        if edm_type == "Edm.String":
            if isinstance(value, int) and not isinstance(value, bool):
                value = str(value)  # a model writes a document number as a number
            if not isinstance(value, str) or not value.isprintable():
                raise _refuse(edm_type)
            return "'" + value.replace("'", "''") + "'"
        if edm_type == "Edm.Boolean":
            if value is True or value == "true":
                return "true"
            if value is False or value == "false":
                return "false"
            raise _refuse(edm_type)
        if isinstance(value, bool):
            raise _refuse(edm_type)
        if edm_type in _INTEGER_RANGES:
            text = str(value) if isinstance(value, int) else value
            if not isinstance(text, str) or not _INTEGER.fullmatch(text):
                raise _refuse(edm_type)
            low, high = _INTEGER_RANGES[edm_type]
            if not low <= int(text) <= high:
                raise _refuse(edm_type)
            return text
        if edm_type == "Edm.Decimal":
            text = str(value) if isinstance(value, int) else value
            if isinstance(value, float):
                # A long number was rounded by whoever parsed the JSON: in a
                # key it could name another entity than the one meant.
                text = common.plain_float(value)
            if not isinstance(text, str) or not _DECIMAL.fullmatch(text):
                raise _refuse(edm_type)
            return text
        if edm_type in ("Edm.Double", "Edm.Single"):
            if isinstance(value, str) and _DECIMAL.fullmatch(value):
                return value
            try:
                if isinstance(value, (int, float)) and math.isfinite(value):
                    text = repr(float(value))
                    if _DECIMAL.fullmatch(text):  # no exponent form in a path segment
                        return text
            except OverflowError:  # an integer no float can hold
                pass
            raise _refuse(edm_type)
        if not isinstance(value, str):
            raise _refuse(edm_type)
        if edm_type == "Edm.Guid" and _GUID.fullmatch(value):
            return value
        if edm_type == "Edm.Date" and _is_date(value):
            return value
        if edm_type == "Edm.DateTimeOffset" and _is_timestamp(value):
            return value
        if edm_type == "Edm.TimeOfDay" and _is_time_of_day(value):
            return value
        if (
            edm_type == "Edm.Duration"
            and _DURATION.fullmatch(value)
            and value.lstrip("-") not in ("P", "PT")
            and not value.endswith("T")
        ):
            # The one V4 literal that keeps its name (`duration'P1DT2H'`).
            return f"duration'{value}'"
        raise _refuse(edm_type)

    def key_segment(self, entity_set: EntitySetDef, key: dict) -> str:
        """The key predicate, percent-encoded: ``('1')`` or ``(A='x',B=2026-10-05)``.

        The key must name exactly the key fields of the entity set
        (``common.key_segment``, with this dialect's literals).
        """
        return common.key_segment(self.literal, entity_set, key)

    def comparable(self, edm_type: str) -> bool:
        """Whether a field of ``edm_type`` may be a sort (or filter) target.

        Only a primitive type that is positively recognised: a complex or
        collection-valued field has no order, and an unknown type string is
        not guessed at.
        """
        return edm_type in V4_COMPARABLE_TYPES

    # -- request bodies -----------------------------------------------------
    def update_request(self, path: str, body: dict | None) -> tuple[str, dict[str, str]]:
        """``(HTTP method, extra headers)`` of a partial update: a plain ``PATCH``.

        It changes the fields in the body and leaves the others alone; V4
        needs no ``X-HTTP-Method`` tunnel.
        """
        return "PATCH", {}

    def _json_value(self, edm_type: str, value: Any) -> Any:
        """``value`` in the form V4 JSON carries ``edm_type`` in a request.

        By positive recognition, like ``literal``: an unknown type or a value
        without the form of its type is refused. ``None`` (clear the field)
        passes for every known type. Numbers stay numbers, whether they were
        given as a number or as its text; a boolean is never a number.

        ``Edm.Int64`` and ``Edm.Decimal`` are sent as JSON numbers, which is
        the V4 default (strings need ``IEEE754Compatible=true``, which this
        client does not ask for). An integer is exact at any size. A decimal
        with a fraction goes through a float (``_decimal_number``) and is
        refused when that cannot be exact -- a silently rounded amount is
        worse than a refusal.
        """
        if isinstance(value, (dict, list, tuple, set)) or edm_type not in V4_COMPARABLE_TYPES:
            raise _refuse(edm_type)
        if value is None:
            return None
        if edm_type == "Edm.String":
            if isinstance(value, int) and not isinstance(value, bool):
                if value.bit_length() > 4 * 1000:  # Python refuses to print a longer one
                    raise _refuse(edm_type)
                return str(value)
            if not isinstance(value, str) or not all(
                ch.isprintable() or ch in "\n\r\t" for ch in value
            ):
                raise _refuse(edm_type)
            return value
        if edm_type == "Edm.Boolean":
            if value is True or value == "true":
                return True
            if value is False or value == "false":
                return False
            raise _refuse(edm_type)
        if isinstance(value, bool):
            raise _refuse(edm_type)
        if isinstance(value, int) and value.bit_length() > 140:  # beyond 40 digits
            raise _refuse(edm_type)
        if edm_type in _INTEGER_RANGES:
            text = str(value) if isinstance(value, int) else value
            if not isinstance(text, str) or not _INTEGER.fullmatch(text):
                raise _refuse(edm_type)
            low, high = _INTEGER_RANGES[edm_type]
            number = int(text)
            if not low <= number <= high:
                raise _refuse(edm_type)
            return number
        if edm_type == "Edm.Decimal":
            return self._decimal_number(value)
        if edm_type in _FLOAT_TYPES:
            if isinstance(value, str) and not _DECIMAL.fullmatch(value):
                raise _refuse(edm_type)
            try:
                number = float(value) if isinstance(value, (int, float, str)) else math.nan
            except OverflowError:  # an integer no float can hold
                raise _refuse(edm_type) from None
            if not math.isfinite(number):
                raise _refuse(edm_type)
            return number
        # The types whose JSON form is their text: checked as a literal is.
        # (A duration is the bare ISO text in JSON; only its URI literal is
        # wrapped in `duration'..'`.)
        if not isinstance(value, str) or edm_type not in _STRING_FORMS:
            raise _refuse(edm_type)
        self.literal(edm_type, value)
        return value

    @staticmethod
    def _decimal_number(value: Any) -> int | float:
        """An ``Edm.Decimal`` as the JSON number that is sent, or a refusal.

        An OData decimal is plain digits: no exponent. ``json.dumps`` prints
        a float with ``repr``, which switches to exponent form below 0.0001
        and from 1e16 on, so a float is sent only when its ``repr`` is plain
        digits. On top of that:

        * given as text, the float must read back as exactly the digits
          given (``12.50`` and ``12.5`` are the same number);
        * given as a number, it passes ``common.plain_float`` (at most 15
          significant digits): whoever parsed the JSON has already rounded
          a longer one, and what was meant cannot be told any more.

        An integer (a number, or text without a point) is exact at any size.
        """
        edm_type = "Edm.Decimal"
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, float):
            if common.plain_float(value) is None:
                raise _refuse(edm_type)
            return value
        if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
            raise _refuse(edm_type)
        if "." not in value:
            return int(value)
        number = float(value)
        sent = repr(number)
        if not _DECIMAL.fullmatch(sent) or decimal.Decimal(sent) != decimal.Decimal(value):
            raise _refuse(edm_type)
        return number

    def encode_body(self, entity_set: EntitySetDef, body: dict) -> dict:
        """``body`` as the JSON object of a V4 create or update request.

        Every name must be a field of the entity set and every value a
        scalar (or ``None``) with the form of that field's EDM type; an
        object or a list -- a deep insert -- is refused. Whether a field may
        be written is the client's check (``ODataClient.check_write``), which
        runs first. A refusal names the field and the type, never the value.
        """
        if not isinstance(body, dict):
            raise ODataError("invalid_argument", "the body must be an object of field values")
        out: dict[str, Any] = {}
        for name, value in body.items():
            definition = entity_set.field(name) if isinstance(name, str) else None
            if definition is None:
                raise ODataError(
                    "unknown_field", f"entity set {entity_set.name!r} has no such field"
                )
            if isinstance(value, (dict, list, tuple, set)):
                raise ODataError(
                    "invalid_argument",
                    f"the value of field {definition.name!r} must be a single value; "
                    f"nested entities and lists cannot be written",
                )
            try:
                out[definition.name] = self._json_value(definition.type, value)
            except ODataError:
                raise ODataError(
                    "invalid_argument",
                    f"the value of field {definition.name!r} is not a valid "
                    f"{_type_shown(definition.type)} value",
                    hint=_VALUE_HINTS.get(definition.type),
                ) from None
        return out

    # -- actions and functions ----------------------------------------------
    def sends_type(self, edm_type: str) -> bool:
        """Whether a parameter of ``edm_type`` can be given a value at all.

        The types of ``literal`` and ``_json_value``, said once for
        ``client.call_refusal``: a parameter of any other type (a complex
        type, an enumeration, a collection, ``Edm.Binary``, a V2-only type,
        an unknown name) is never sent.
        """
        return edm_type in V4_COMPARABLE_TYPES

    def call_literal(self, edm_type: str, value: Any) -> str:
        """``value`` as the percent-encoded URI literal of a function parameter.

        ``literal`` with what a place in the path adds, the rules of a key
        value (``common.key_segment``): an object or a list is never a
        parameter value, and a text is short and holds no slash, backslash,
        ``%``, ``?``, ``#`` or ``..`` -- it could not become a second segment (a quote is
        doubled, the rest is percent-encoded and ``urls.join_path`` checks
        the segment again), but SAP does not read such a value reliably
        from a path either.
        """
        if value is None or isinstance(value, (dict, list, tuple, set)):
            raise _refuse(edm_type)
        if isinstance(value, int) and not isinstance(value, bool):
            if value.bit_length() > 4 * MAX_PARAM_VALUE_CHARS:
                raise _refuse(edm_type)
        if isinstance(value, str) and (
            len(value) > MAX_PARAM_VALUE_CHARS
            or ".." in value
            or any(ch in value for ch in _PATH_BREAKERS)
        ):
            raise _refuse(edm_type)
        text = self.literal(edm_type, value)
        if len(text) > MAX_PARAM_VALUE_CHARS + 2:
            raise _refuse(edm_type)
        return quote(text, safe="'")

    def call_request(
        self,
        service_path: str,
        operation: OperationDef,
        key: dict | None,
        params: dict | None,
        *,
        bound: EntitySetDef | None = None,
    ) -> tuple[str, str, dict[str, str], Any]:
        """``(HTTP method, path, query, JSON body)`` of an action or function call.

        * action: ``POST``, the parameters as a JSON object (``{}`` without
          any), each in the JSON form of its declared type;
        * function: ``GET``, the parameters as typed literals behind the
          name, ``Name(P1='x',P2=5)`` (``Name()`` without any);
        * bound (``bound`` is the entity set ``operation.bound_to`` names,
          ``key`` the key of the entity): ``Set(<key>)/<qualified_name>``;
          not bound: the operation's import name directly below the service.

        The query is always empty. Only names the catalogue declares are
        sent: the entity set's path, the operation's name (re-checked here
        to be an EDM name; a bound one must be namespace-qualified) and its
        declared parameters. An optional parameter that is not given, or
        given as null, is left out. Every segment goes through
        ``urls.join_path``.

        The caller (``ODataClient.check_call``) has checked the arguments
        against the catalogue; what does not fit is refused here again, by
        name and never with a value.
        """
        name = operation.name
        method = {"action": "POST", "function": "GET"}.get(operation.kind)
        if method is None or operation.http_method != method:
            raise ODataError(
                "invalid_argument",
                f"operation {name!r} is not an action (POST) or a function (GET) "
                f"of an OData V4 service",
            )
        if params is None:
            params = {}
        declared = {p.name: p for p in operation.parameters}
        if not isinstance(params, dict) or any(
            not isinstance(given, str) or given not in declared for given in params
        ):
            raise ODataError(
                "invalid_argument",
                f"operation {name!r} takes each of its declared parameters once and no other",
            )
        segments: list[str] = []
        if operation.bound_to is not None:
            if bound is None or bound.name != operation.bound_to:
                raise ODataError(
                    "invalid_argument",
                    f"operation {name!r} is bound to an entity set that was not given",
                )
            segments.append(
                (bound.path or bound.name)
                + common.key_segment(self.literal, bound, key, subject="this operation")
            )
            address = operation.qualified_name
            qualified = "." in address.strip(".")
        else:
            if key is not None:
                raise ODataError(
                    "invalid_argument",
                    f"operation {name!r} is not bound to an entity; it takes no key",
                )
            address, qualified = name, True
        if not qualified or not re.fullmatch(EDM_NAME_RE, address):
            raise ODataError(
                "invalid_argument",
                f"operation {name!r} cannot be addressed: the catalogue does not hold "
                f"its namespace-qualified name",
            )
        sent: dict[str, Any] = {}
        for parameter, definition in declared.items():
            if params.get(parameter) is None:
                if definition.required:
                    raise ODataError(
                        "invalid_argument", f"operation {name!r} needs parameter {parameter!r}"
                    )
                continue  # optional and not given: left out, never sent as null
            value = params[parameter]
            try:
                if isinstance(value, (dict, list, tuple, set)):
                    raise _refuse(definition.type)
                sent[parameter] = (
                    self._json_value(definition.type, value)
                    if method == "POST"
                    else self.call_literal(definition.type, value)
                )
            except ODataError:
                raise ODataError(
                    "invalid_argument",
                    f"the value of parameter {parameter!r} is not a single valid "
                    f"{_type_shown(definition.type)} value (objects, lists and types this "
                    f"tool does not know are not sent)",
                    hint=(_VALUE_HINTS if method == "POST" else _LITERAL_HINTS).get(
                        definition.type
                    ),
                ) from None
        body: dict[str, Any] | None = None
        if method == "POST":
            body = sent
        else:
            if not all(re.fullmatch(EDM_NAME_RE, parameter) for parameter in sent):
                raise ODataError(
                    "invalid_argument",
                    f"a parameter of operation {name!r} has a name a URL cannot carry",
                )
            address += "(" + ",".join(f"{p}={text}" for p, text in sent.items()) + ")"
            if len(address) > MAX_SEGMENT:
                raise ODataError(
                    "invalid_argument",
                    f"the parameters of operation {name!r} are together too long for one call",
                    hint="pass shorter values, or leave out optional parameters",
                )
        segments.append(address)
        try:
            path = join_path(service_path, *segments)
        except ValueError:
            raise ODataError(
                "invalid_argument", "the request path could not be built for this operation"
            ) from None
        return method, path, {}, body

    def parse_call(self, payload: Any, name: str) -> tuple[str, Any]:
        """``(shape, value)`` of what an action or function answered.

        One of ``CALL_SHAPES``, recognised positively:

        * ``none``: no content, ``@odata.null``, or a ``value`` of ``null``;
        * ``value``: one JSON scalar, as ``{"@odata.context": .., "value":
          scalar}``;
        * ``collection``: a list under ``value``;
        * ``entity``: an object with properties of its own -- an entity or a
          complex value; the two look alike here, and
          ``returned_rows_are_of`` tells them apart;
        * ``other``: anything else (an object without a property, an object
          under ``value``). The value is ``None``.

        A ``value`` that is the only property is the wrapper, unless the
        context says the answer is one entity (``.../$entity``): an entity
        type may have a single property of that name, and it must not pass
        as a scalar. A scalar under ``value`` is a ``value`` only when the
        context names a primitive type (``$metadata#Edm.<Type>``): without a
        context, or with one that names a complex or entity type, it is
        ``other`` and never shown. Whether an entity or a collection may be shown is not
        decided here. An answer that is not a JSON object, or has a
        top-level ``error``, is not the answer of a call (``sap_error``).
        """
        if payload is None:
            return "none", None
        if not isinstance(payload, dict) or "error" in payload:
            raise ODataError("sap_error", "the OData service answered in an unexpected shape")
        if self._control(payload, "null") is True:
            return "none", None
        properties = self._properties(payload)
        context = self._control(payload, "context")
        one_entity = isinstance(context, str) and context.endswith(_ENTITY)
        if properties == ["value"] and not one_entity:
            value = payload["value"]
            if value is None:
                return "none", None
            if isinstance(value, (str, int, float)):  # bool is an int
                _, found, fragment = (
                    context.rpartition(_METADATA) if isinstance(context, str) else ("", "", "")
                )
                if found and fragment.startswith("Edm."):
                    return "value", value
                return "other", None
            if isinstance(value, list):
                return "collection", value
            return "other", None
        if properties:
            return "entity", payload
        return "other", None

    def returned_rows_are_of(
        self, payload: Any, rows: list, target: EntitySetDef, many: bool
    ) -> bool:
        """Whether a call's answer says its rows are entities of ``target``.

        V4 says what an answer holds once, in its ``@odata.context``:
        ``<..>$metadata#<fragment>``. The rows are taken as entities of
        ``target`` only for a fragment that names it and the right number:

        * the entity set: ``Set/$entity`` (one) or ``Set`` (many), each with
          an optional select list ``Set(A,B)``;
        * the entity type: ``NS.Type`` (one) or ``Collection(NS.Type)``.

        A row that names a type of its own (``@odata.type``, which SAP sends
        for a derived type) must name the set's type. No context, a context
        that names another set, a key or a navigation path, a complex type,
        or a list of values: no -- the caller then shows nothing.
        """
        context = self._control(payload, "context") if isinstance(payload, dict) else None
        if not isinstance(context, str) or len(context) > MAX_CONTEXT_CHARS:
            return False
        _, found, fragment = context.rpartition(_METADATA)
        if not found or not target.name or not target.entity_type:
            return False
        if fragment in (target.entity_type, f"Collection({target.entity_type})"):
            named_many = fragment.startswith("Collection(")
        else:
            named_many = not fragment.endswith(_ENTITY)
            if not named_many:
                fragment = fragment[: -len(_ENTITY)]
            set_name, paren, rest = fragment.partition("(")
            if paren and not (rest.endswith(")") and _SELECT_LIST.fullmatch(rest[:-1])):
                return False
            if set_name != target.name:
                return False
        if named_many is not many:
            return False
        for row in rows:
            if not isinstance(row, dict):
                return False
            own = self._control(row, "type")
            if own is not None and own != "#" + target.entity_type:
                return False
        return True

    # -- query options ------------------------------------------------------
    def projection(
        self, names: list[str], expands: list[tuple[str, list[str]]]
    ) -> tuple[list[str], list[str]]:
        """``($select entries, $expand entries)`` of a read.

        ``expands`` pairs each navigation with the fields of its target that
        may be read. In V4 they are a ``$select`` inside the ``$expand``
        item; without it SAP would transport the whole expanded entity.
        Every name comes from the catalogue (the client checked it), so the
        item is built from EDM names only.
        """
        return list(names), [f"{nav}($select={','.join(fields)})" for nav, fields in expands]

    def read_params(self, query: ReadQuery) -> dict[str, str]:
        """The query options of a read, as parameters (never as URL text)."""
        params: dict[str, str] = {}
        if query.select:
            params["$select"] = ",".join(query.select)
        filter_ = query.filter.strip() if isinstance(query.filter, str) else ""
        if filter_:  # a blank filter is no filter
            params["$filter"] = filter_
        if query.expand:
            params["$expand"] = ",".join(query.expand)
        if query.orderby:
            params["$orderby"] = ",".join(query.orderby)
        if query.top > 0:
            params["$top"] = str(query.top)
        if query.skip > 0:
            params["$skip"] = str(query.skip)
        if query.count:
            params["$count"] = "true"
        return params

    # -- payloads -----------------------------------------------------------
    @staticmethod
    def _control(payload: dict, name: str) -> Any:
        """A control information value: ``@odata.<name>``, or 4.01's ``@<name>``."""
        value = payload.get(f"@odata.{name}")
        return payload.get(f"@{name}") if value is None else value

    @staticmethod
    def _properties(payload: dict) -> list[str]:
        """The keys of ``payload`` that are properties, not annotations."""
        return [k for k in payload if isinstance(k, str) and "@" not in k]

    def _is_error(self, payload: dict) -> bool:
        """Whether ``payload`` is an error envelope and nothing else.

        An entity type may have a property called ``error``, so the key
        alone does not decide for a read: the envelope is an *object* under
        ``error`` with no other property beside it. (A write is stricter,
        ``ODataClient._is_entity``: there a wrong "yes" claims a change.)
        """
        return isinstance(payload.get("error"), dict) and self._properties(payload) == ["error"]

    def parse_list(self, payload: Any) -> tuple[list[dict], int | None, str | None]:
        """``(rows, count, next link)`` of ``{"value": [...]}``."""
        # A collection has no properties of its own, so `error` beside
        # `value` is never one: refused here whatever it holds.
        if (
            not isinstance(payload, dict)
            or "error" in payload
            or not isinstance(payload.get("value"), list)
        ):
            raise ODataError("sap_error", "the OData service answered in an unexpected shape")
        raw_count = self._control(payload, "count")
        count: int | None = None
        if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 0:
            count = raw_count
        elif (
            isinstance(raw_count, str)
            and raw_count.isascii()
            and raw_count.isdigit()
            and len(raw_count) <= 18
        ):
            count = int(raw_count)
        next_link = self._control(payload, "nextLink")
        return (
            payload["value"],
            count,
            next_link if isinstance(next_link, str) and next_link else None,
        )

    def parse_entity(self, payload: Any) -> tuple[dict, str | None]:
        """``(row, etag)`` of one entity; the ETag is ``@odata.etag``.

        A V4 entity is a bare JSON object, so it is told apart by what it is
        not: an error envelope, a collection (``value`` holding a list), or
        an object without a single property.
        """
        if not isinstance(payload, dict) or self._is_error(payload):
            raise ODataError("sap_error", "the OData service answered in an unexpected shape")
        properties = self._properties(payload)
        if not properties or (properties == ["value"] and isinstance(payload["value"], list)):
            raise ODataError("sap_error", "the OData service answered in an unexpected shape")
        etag = self._control(payload, "etag")
        return payload, etag if isinstance(etag, str) and etag else None

    def is_null_entity(self, payload: Any) -> bool:
        """Whether ``payload`` says "no entity" (a navigation that leads nowhere).

        V4 answers that with ``204 No Content``, which the client sees as no
        payload at all. Two other forms are in use: ``@odata.null``, and the
        single-value wrapper ``{"@odata.context": .., "value": null}`` --
        told apart from an entity with a property called ``value`` by the
        context annotation beside a ``value`` that is the only property.
        """
        if not isinstance(payload, dict):
            return False
        if self._control(payload, "null") is True:
            return True
        return (
            self._properties(payload) == ["value"]
            and payload["value"] is None
            and self._control(payload, "context") is not None
        )

    def parse_error(self, response: httpx.Response) -> tuple[str, str]:
        """``(code, text)`` of an OData error envelope, or ``("", "")``.

        The V4 envelope is ``{"error": {"code", "message"}}`` with a string
        as the message; ``details`` and ``innererror`` are not read
        (``common.read_error`` explains what else is not).
        """
        return common.read_error(response, lambda message: message)
