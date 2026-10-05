"""What is specific to OData V2 (SAP Gateway): literals, options, payloads.

Every value is written as a typed literal by positive recognition (a quote
inside a string is doubled, every other type must match its exact form).
How a key predicate is built from those literals, and how an error envelope
is read, is the same for V2 and V4 and lives in ``common``.

No refusal repeats a value.
"""

from __future__ import annotations

import calendar
import math
import re
from typing import Any

import httpx

from . import common
from .client import ODataError, ReadQuery
from .common import refuse as _refuse
from .models import EntitySetDef, OperationDef
from .urls import join_path

MAX_KEY_VALUE_CHARS = common.MAX_KEY_VALUE_CHARS
# What one parameter value of a function import may weigh: it travels in the
# query string, and a URL is not the place for a document.
MAX_PARAM_VALUE_CHARS = 1000
# What a function import answered, as `V2Dialect.parse_call` names it.
CALL_SHAPES = ("none", "value", "entity", "collection", "other")

# Used with `fullmatch` only, and with `[0-9]`: `$` also matches before a
# trailing newline, and `\d` matches every Unicode decimal digit.
_INTEGER = re.compile(r"-?[0-9]{1,19}")
_DECIMAL = re.compile(r"-?[0-9]{1,40}(?:\.[0-9]{1,40})?")
_GUID = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
_CLOCK = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,7})?)?"
_DATETIME = re.compile(_CLOCK)
_DATETIMEOFFSET = re.compile(_CLOCK + r"(?:Z|[+-][0-9]{2}:[0-9]{2})")
_TIME = re.compile(r"PT(?:[0-9]{1,2}H)?(?:[0-9]{1,2}M)?(?:[0-9]{1,2}(?:\.[0-9]{1,7})?S)?")
# The most significant digits of a decimal passed as a NUMBER that are
# trusted in a literal (the same rule as `v4._plain_float`).
_MAX_FLOAT_DIGITS = 15
_INTEGER_TYPES = {
    "Edm.Byte": "",
    "Edm.SByte": "",
    "Edm.Int16": "",
    "Edm.Int32": "",
    "Edm.Int64": "L",
}
_FLOAT_TYPES = {"Edm.Double": "d", "Edm.Single": "f"}
_PATTERN_TYPES = {
    "Edm.DateTime": ("datetime", _DATETIME),
    "Edm.DateTimeOffset": ("datetimeoffset", _DATETIMEOFFSET),
    "Edm.Time": ("time", _TIME),
    "Edm.Guid": ("guid", _GUID),
}

# -- request bodies -------------------------------------------------------------
_INTEGER_RANGES = {
    "Edm.Byte": (0, 255),
    "Edm.SByte": (-128, 127),
    "Edm.Int16": (-(2**15), 2**15 - 1),
    "Edm.Int32": (-(2**31), 2**31 - 1),
    "Edm.Int64": (-(2**63), 2**63 - 1),
}
# The V2 JSON date as a read returns it, which a model hands back unchanged.
_JSON_DATE = re.compile(r"/Date\(-?[0-9]{1,15}\)/")
_JSON_DATE_OFFSET = re.compile(r"/Date\(-?[0-9]{1,15}(?:[+-][0-9]{4})?\)/")
_DATE_HINTS = {
    "Edm.DateTime": "write it as 2026-10-05T00:00:00 (date and time, no time zone), "
    "or hand back the /Date(...)/ value a read returned",
    "Edm.DateTimeOffset": "write it in UTC as 2026-10-05T12:00:00Z (no other offset is "
    "accepted), or hand back the /Date(...)/ value a read returned",
}
_PARAM_HINTS = {
    "Edm.DateTime": "write it as 2026-10-05T00:00:00 (date and time, no time zone)",
    "Edm.DateTimeOffset": "write it as 2026-10-05T12:00:00Z",
    "Edm.Time": "write it as an ISO 8601 duration, for example PT12H30M",
    "Edm.Guid": "write it as 8-4-4-4-12 hexadecimal digits",
}
_ISO = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.([0-9]{1,7}))?)?"
    r"(Z|[+-][0-9]{2}:[0-9]{2})?"
)

class V2Dialect:
    """OData V2 as SAP Gateway speaks it."""

    version = "v2"
    supports_write = True
    # Function imports can be called (`call_request`, `parse_call`).
    supports_call = True
    # Sent with every request of this version; V2 needs none.
    request_headers: dict[str, str] = {}

    # -- literals -----------------------------------------------------------
    def literal(self, edm_type: str, value: Any) -> str:
        """``value`` as a V2 URI literal of ``edm_type`` (not percent-encoded).

        Deny by default: a type this method does not know, or a value that
        does not have the exact form of its type, is refused -- a literal is
        never built by pasting text between quotes, except for
        ``Edm.String``, where the quote is doubled.
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
        if edm_type in _INTEGER_TYPES:
            text = str(value) if isinstance(value, int) else value
            if not isinstance(text, str) or not _INTEGER.fullmatch(text):
                raise _refuse(edm_type)
            return text + _INTEGER_TYPES[edm_type]
        if edm_type == "Edm.Decimal":
            text = str(value) if isinstance(value, int) else value
            if isinstance(value, float):
                # A number with more digits was rounded by whoever parsed the
                # JSON: in a key it could name another entity than the one
                # meant. As text, every digit goes out as given. (The `.0`
                # that `repr` appends to a whole number is not a digit.)
                text = repr(value) if math.isfinite(value) else ""
                digits = text.lstrip("-").removesuffix(".0").replace(".", "").lstrip("0")
                if len(digits) > _MAX_FLOAT_DIGITS:
                    raise _refuse(edm_type)
            if not isinstance(text, str) or not _DECIMAL.fullmatch(text):
                raise _refuse(edm_type)
            return text + "M"
        if edm_type in _FLOAT_TYPES:
            if isinstance(value, str) and _DECIMAL.fullmatch(value):
                return value + _FLOAT_TYPES[edm_type]
            try:
                if isinstance(value, (int, float)) and math.isfinite(value):
                    return repr(float(value)) + _FLOAT_TYPES[edm_type]
            except OverflowError:  # an integer no float can hold
                pass
            raise _refuse(edm_type)
        if edm_type in _PATTERN_TYPES:
            prefix, pattern = _PATTERN_TYPES[edm_type]
            if not isinstance(value, str) or not pattern.fullmatch(value):
                raise _refuse(edm_type)
            return f"{prefix}'{value}'"
        raise _refuse(edm_type)

    def key_segment(self, entity_set: EntitySetDef, key: dict) -> str:
        """The key predicate, percent-encoded: ``('4500000001')`` or ``(A='x',B='00010')``.

        The key must name exactly the key fields of the entity set
        (``common.key_segment``, with this dialect's literals).
        """
        return common.key_segment(self.literal, entity_set, key)

    # -- function imports ---------------------------------------------------
    def sends_type(self, edm_type: str) -> bool:
        """Whether ``literal`` can write a value of ``edm_type`` at all.

        The types of ``literal``, said once for ``client.call_refusal``: a
        parameter of any other type (a complex type, a collection,
        ``Edm.Binary``, an unknown name) can never be given a value.
        """
        return (
            edm_type in ("Edm.String", "Edm.Boolean", "Edm.Decimal")
            or edm_type in _INTEGER_TYPES
            or edm_type in _FLOAT_TYPES
            or edm_type in _PATTERN_TYPES
        )

    def call_literal(self, edm_type: str, value: Any) -> str:
        """``value`` as the URI literal of a function import parameter.

        ``literal`` with what a parameter adds: an object or a list is never
        a parameter value, an integer must fit its type, and a text has a
        length a query string can carry. A type ``literal`` does not know
        (a complex type, a collection, ``Edm.Binary``) is refused there.

        The length is checked on the text that is sent: a number passed for
        an ``Edm.String`` is turned into its digits first. (An integer too
        large to have that few digits is refused by its size, before any
        conversion: Python refuses to print a very long one.)
        """
        if value is None or isinstance(value, (dict, list, tuple, set)):
            raise _refuse(edm_type)
        if isinstance(value, int) and not isinstance(value, bool):
            # 1,000 decimal digits are fewer than 3,322 bits.
            if value.bit_length() > 4 * MAX_PARAM_VALUE_CHARS:
                raise _refuse(edm_type)
            if edm_type == "Edm.String":
                value = str(value)
        if isinstance(value, str) and len(value) > MAX_PARAM_VALUE_CHARS:
            raise _refuse(edm_type)
        text = self.literal(edm_type, value)
        if edm_type in _INTEGER_RANGES:
            low, high = _INTEGER_RANGES[edm_type]
            if not low <= int(text.rstrip("L")) <= high:
                raise _refuse(edm_type)
        return text

    def call_request(
        self,
        service_path: str,
        operation: OperationDef,
        key: dict | None,
        params: dict | None,
    ) -> tuple[str, str, dict[str, str], Any]:
        """``(HTTP method, path, query, JSON body)`` of a function import call.

        V2: the path is the service path plus the function import's own name
        from the catalogue, and every parameter is a typed literal in the
        query string -- also for a POST, which has no body (``None``). A
        bound function import gets the key of its entity as parameters of
        the same names, so ``key`` is merged into ``params``; only names the
        catalogue's parameter list declares are ever sent, each written with
        the declared type. The query is returned as a mapping for ``params=``
        (never as URL text), so a value cannot add an option, a fragment or
        a path segment. ``$format=json`` asks for the one answer format the
        result handling recognises.

        The caller (``ODataClient.check_call``) has checked the arguments
        against the catalogue; what does not fit is refused here again, by
        name and never with a value.
        """
        declared = {p.name: p for p in operation.parameters}
        given: dict[str, Any] = {}
        for source in (params or {}, key or {}):
            if not isinstance(source, dict):
                raise ODataError("invalid_argument", "parameters must be an object of values")
            for name, value in source.items():
                if not isinstance(name, str) or name not in declared or name in given:
                    raise ODataError(
                        "invalid_argument",
                        f"operation {operation.name!r} takes each of its declared "
                        f"parameters once and no other",
                    )
                given[name] = value
        query: dict[str, str] = {}
        for name, definition in declared.items():
            if given.get(name) is None:
                if definition.required:
                    raise ODataError(
                        "invalid_argument",
                        f"operation {operation.name!r} needs parameter {name!r}",
                    )
                continue  # optional and not given: left out, never sent as null
            try:
                query[name] = self.call_literal(definition.type, given[name])
            except ODataError:
                shown = (
                    definition.type
                    if re.fullmatch(r"[A-Za-z0-9_.]{1,64}", definition.type)
                    else "its type"
                )
                raise ODataError(
                    "invalid_argument",
                    f"the value of parameter {name!r} is not a single valid {shown} value "
                    f"(objects, lists and types this tool does not know are not sent)",
                    hint=_PARAM_HINTS.get(definition.type),
                ) from None
        query["$format"] = "json"
        try:
            path = join_path(service_path, operation.name)
        except ValueError:
            raise ODataError(
                "invalid_argument", "the request path could not be built for this operation"
            ) from None
        return operation.http_method, path, query, None

    def parse_call(self, payload: Any, name: str) -> tuple[str, Any]:
        """``(shape, value)`` of what a function import answered.

        One of ``CALL_SHAPES``, recognised positively:

        * ``none``: no content, or ``{"d": null}``;
        * ``value``: one JSON scalar, as ``{"d": {"<name>": scalar}}`` (how
          Gateway returns a primitive) or ``{"d": scalar}``;
        * ``entity``: one object with ``__metadata``, directly under ``d`` or
          under ``d.<name>``;
        * ``collection``: a list, as ``d.results``, ``d`` or ``d.<name>``
          (with or without ``results``);
        * ``other``: anything else under ``d`` (a complex type, say). The
          value is ``None``: the caller has nothing it could check.

        Whether an entity or a collection may be shown is not decided here.
        An answer without ``d``, or with an ``error``, is not the answer of
        a function import (``sap_error``).
        """
        if payload is None:
            return "none", None
        if not isinstance(payload, dict) or "d" not in payload or "error" in payload:
            raise ODataError("sap_error", "the OData service answered in an unexpected shape")
        d = payload["d"]
        if isinstance(d, dict) and "__metadata" not in d and set(d) == {name}:
            d = d[name]  # the wrapper Gateway puts around a function import's result
        if d is None:
            return "none", None
        if isinstance(d, (str, int, float)):  # bool is an int
            return "value", d
        if isinstance(d, list):
            return "collection", d
        if isinstance(d, dict):
            if "__metadata" in d:
                return "entity", d
            if isinstance(d.get("results"), list):
                return "collection", d["results"]
        return "other", None

    # -- request bodies -----------------------------------------------------
    def update_request(self, path: str, body: dict | None) -> tuple[str, dict[str, str]]:
        """``(HTTP method, extra headers)`` of a partial update.

        V2 on SAP Gateway: ``POST`` tunnelling ``MERGE``, which changes the
        fields in the body and leaves the others alone (``PUT`` would reset
        them). It lives in the dialect so that V4 can answer ``PATCH``.
        """
        return "POST", {"X-HTTP-Method": "MERGE"}

    @staticmethod
    def _json_date(match: re.Match[str], with_offset: bool) -> str:
        """``/Date(ms)/`` (or ``/Date(ms+mmmm)/``) of an ISO timestamp."""
        year, month, day, hour, minute = (int(match.group(i)) for i in range(1, 6))
        second = int(match.group(6) or 0)
        if not (1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60 and second < 60):
            raise ValueError("not a timestamp")
        # timegm refuses nothing; a day the month does not have is caught here.
        if day > calendar.monthrange(year, month)[1] or year < 1:
            raise ValueError("not a date")
        millis = calendar.timegm((year, month, day, hour, minute, second)) * 1000
        millis += int((match.group(7) or "0").ljust(3, "0")[:3])
        if not with_offset:
            return f"/Date({millis})/"
        # UTC only. Whether Gateway reads the ticks of `/Date(ms+mmmm)/` as
        # the UTC instant or as local time is not verified against a live
        # system, and a wrong guess would silently store a shifted timestamp.
        # With a zero offset both readings are the same instant.
        if match.group(8) not in ("Z", "+00:00"):
            raise ValueError("only UTC is accepted")
        return f"/Date({millis}+0000)/"

    def _json_value(self, edm_type: str, value: Any) -> Any:
        """``value`` in the form V2 JSON carries ``edm_type`` in a request.

        By positive recognition, like ``literal``: an unknown type or a value
        without the form of its type is refused. ``None`` (clear the field)
        passes for every known type.
        """
        if isinstance(value, (dict, list, tuple, set)):
            raise _refuse(edm_type)
        if edm_type == "Edm.String":
            if value is None:
                return None
            if isinstance(value, int) and not isinstance(value, bool):
                return str(value)
            if not isinstance(value, str) or not all(
                ch.isprintable() or ch in "\n\r\t" for ch in value
            ):
                raise _refuse(edm_type)
            return value
        if edm_type == "Edm.Boolean":
            if value is None:
                return None
            if value is True or value == "true":
                return True
            if value is False or value == "false":
                return False
            raise _refuse(edm_type)
        if isinstance(value, bool):
            raise _refuse(edm_type)
        known = (
            edm_type in _INTEGER_RANGES
            or edm_type in _FLOAT_TYPES
            or edm_type in _PATTERN_TYPES
            or edm_type == "Edm.Decimal"
        )
        if not known:
            raise _refuse(edm_type)
        if value is None:
            return None
        if edm_type in _INTEGER_RANGES:
            text = str(value) if isinstance(value, int) else value
            if not isinstance(text, str) or not _INTEGER.fullmatch(text):
                raise _refuse(edm_type)
            low, high = _INTEGER_RANGES[edm_type]
            number = int(text)
            if not low <= number <= high:
                raise _refuse(edm_type)
            # V2 JSON: Int64 travels as a string, the smaller integers as numbers.
            return str(number) if edm_type == "Edm.Int64" else number
        if edm_type == "Edm.Decimal" or edm_type in _FLOAT_TYPES:
            text = value
            if isinstance(value, int):
                text = str(value)
            elif isinstance(value, float) and math.isfinite(value):
                text = repr(value)
            if not isinstance(text, str) or not _DECIMAL.fullmatch(text):
                raise _refuse(edm_type)
            return text  # V2 JSON carries Decimal, Double and Single as strings
        if not isinstance(value, str):
            raise _refuse(edm_type)
        if edm_type in ("Edm.DateTime", "Edm.DateTimeOffset"):
            with_offset = edm_type == "Edm.DateTimeOffset"
            if (_JSON_DATE_OFFSET if with_offset else _JSON_DATE).fullmatch(value):
                return value
            match = _ISO.fullmatch(value)
            if match is None or bool(match.group(8)) is not with_offset:
                raise _refuse(edm_type)
            try:
                return self._json_date(match, with_offset)
            except (ValueError, OverflowError):
                raise _refuse(edm_type) from None
        _, pattern = _PATTERN_TYPES[edm_type]  # Edm.Time, Edm.Guid
        if not value or not pattern.fullmatch(value):
            raise _refuse(edm_type)
        return value

    def encode_body(self, entity_set: EntitySetDef, body: dict) -> dict:
        """``body`` as the JSON object of a V2 create or update request.

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
                shown = (
                    definition.type
                    if re.fullmatch(r"[A-Za-z0-9_.]{1,64}", definition.type)
                    else "its type"
                )
                raise ODataError(
                    "invalid_argument",
                    f"the value of field {definition.name!r} is not a valid {shown} value",
                    hint=_DATE_HINTS.get(definition.type),
                ) from None
        return out

    # -- query options ------------------------------------------------------
    def comparable(self, edm_type: str) -> bool:
        """Whether a field of ``edm_type`` may be a sort target: in V2, any.

        The V4 dialect narrows this to the types it recognises; V2 keeps the
        rule it always had (the field must be selectable).
        """
        return True

    def projection(
        self, names: list[str], expands: list[tuple[str, list[str]]]
    ) -> tuple[list[str], list[str]]:
        """``($select entries, $expand entries)`` of a read.

        ``expands`` pairs each navigation with the fields of its target that
        may be read. V2 returns an expanded entity only when ``$select``
        names it, so those fields travel as ``Nav/Field`` paths in
        ``$select`` and ``$expand`` holds the bare navigation names.
        """
        select = list(names)
        for nav, fields in expands:
            select.extend(f"{nav}/{field}" for field in fields)
        return select, [nav for nav, _ in expands]

    def read_params(self, query: ReadQuery) -> dict[str, str]:
        """The query options of a read, as parameters (never as URL text)."""
        params = {"$format": "json"}
        if query.select:
            params["$select"] = ",".join(query.select)
        filter_ = query.filter.strip() if isinstance(query.filter, str) else ""
        if filter_:  # a blank filter is no filter: `$filter=` alone is an error to SAP
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
            params["$inlinecount"] = "allpages"
        return params

    # -- payloads -----------------------------------------------------------
    def parse_list(self, payload: dict) -> tuple[list[dict], int | None, str | None]:
        """``(rows, count, next link)`` of ``{"d": {"results": [...]}}``."""
        d = payload.get("d") if isinstance(payload, dict) else None
        if isinstance(d, list):  # the pre-2.0 verbose form
            return d, None, None
        if not isinstance(d, dict) or not isinstance(d.get("results"), list):
            raise ODataError("sap_error", "the OData service answered in an unexpected shape")
        raw_count = d.get("__count")
        count: int | None = None
        if isinstance(raw_count, int) and not isinstance(raw_count, bool):
            count = raw_count
        elif isinstance(raw_count, str) and raw_count.isdigit() and len(raw_count) <= 18:
            count = int(raw_count)
        next_link = d.get("__next")
        return d["results"], count, next_link if isinstance(next_link, str) and next_link else None

    def parse_entity(self, payload: dict) -> tuple[dict, str | None]:
        """``(row, etag)`` of ``{"d": {...}}``; the ETag is ``__metadata.etag``."""
        d = payload.get("d") if isinstance(payload, dict) else None
        if not isinstance(d, dict):
            raise ODataError("sap_error", "the OData service answered in an unexpected shape")
        metadata = d.get("__metadata")
        etag = metadata.get("etag") if isinstance(metadata, dict) else None
        return d, etag if isinstance(etag, str) and etag else None

    def is_null_entity(self, payload: Any) -> bool:
        """Whether ``payload`` says "no entity": ``{"d": null}``."""
        return isinstance(payload, dict) and payload.get("d", ...) is None

    def parse_error(self, response: httpx.Response) -> tuple[str, str]:
        """``(code, text)`` of an OData error envelope, or ``("", "")``.

        The V2 envelope is ``{"error": {"code", "message": {"value"}}}``
        (``common.read_error`` explains what is not read).
        """
        return common.read_error(
            response, lambda message: message.get("value") if isinstance(message, dict) else message
        )
