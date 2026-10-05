"""What is specific to OData V2 (SAP Gateway): literals, options, payloads.

The key predicate is the one place where a value chosen by a model becomes
part of the request *path*. It therefore leaves this module in a form that
cannot be anything but one segment: every value is written as a typed
literal by positive recognition (a quote inside a string is doubled, every
other type must match its exact form), percent-encoded, and string values
that contain ``/ \\ % ? #`` are refused outright. The last rule is stricter
than the protocol: ``DestinationAuth`` rebuilds the request URL from the
*decoded* path, so an encoded ``%2F`` in a key would reach SAP as a real
``/`` and an encoded ``%`` would start an escape. ``urls.join_path`` checks
the result again.

No refusal repeats a value.
"""

from __future__ import annotations

import calendar
import html
import math
import re
from typing import Any
from urllib.parse import quote

import httpx

from .client import ODataError, ReadQuery
from .models import EntitySetDef

MAX_KEY_VALUE_CHARS = 255
_MAX_ERROR_BODY = 20_000

# Used with `fullmatch` only, and with `[0-9]`: `$` also matches before a
# trailing newline, and `\d` matches every Unicode decimal digit.
_INTEGER = re.compile(r"-?[0-9]{1,19}")
_DECIMAL = re.compile(r"-?[0-9]{1,40}(?:\.[0-9]{1,40})?")
_GUID = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
_CLOCK = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,7})?)?"
_DATETIME = re.compile(_CLOCK)
_DATETIMEOFFSET = re.compile(_CLOCK + r"(?:Z|[+-][0-9]{2}:[0-9]{2})")
_TIME = re.compile(r"PT(?:[0-9]{1,2}H)?(?:[0-9]{1,2}M)?(?:[0-9]{1,2}(?:\.[0-9]{1,7})?S)?")
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
_KEY_BREAKERS = ("/", "\\", "%", "?", "#")

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
_ISO = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.([0-9]{1,7}))?)?"
    r"(Z|[+-][0-9]{2}:[0-9]{2})?"
)

_XML_CODE = re.compile(r"<code[^<>]*>([^<]{0,200})</code>")
_XML_MESSAGE = re.compile(r"<message[^<>]*>([^<]{0,2000})</message>")


def _refuse(edm_type: str) -> ODataError:
    shown = (
        edm_type
        if isinstance(edm_type, str) and re.fullmatch(r"[A-Za-z0-9_.]{1,64}", edm_type)
        else "that type"
    )
    return ODataError("invalid_argument", f"the value is not a valid {shown} value")


class V2Dialect:
    """OData V2 as SAP Gateway speaks it."""

    version = "v2"

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
            if isinstance(value, float) and math.isfinite(value):
                text = repr(value)
            if not isinstance(text, str) or not _DECIMAL.fullmatch(text):
                raise _refuse(edm_type)
            return text + "M"
        if edm_type in _FLOAT_TYPES:
            if isinstance(value, str) and _DECIMAL.fullmatch(value):
                return value + _FLOAT_TYPES[edm_type]
            if isinstance(value, (int, float)) and math.isfinite(value):
                return repr(float(value)) + _FLOAT_TYPES[edm_type]
            raise _refuse(edm_type)
        if edm_type in _PATTERN_TYPES:
            prefix, pattern = _PATTERN_TYPES[edm_type]
            if not isinstance(value, str) or not pattern.fullmatch(value):
                raise _refuse(edm_type)
            return f"{prefix}'{value}'"
        raise _refuse(edm_type)

    def key_segment(self, entity_set: EntitySetDef, key: dict) -> str:
        """The key predicate, percent-encoded: ``('4500000001')`` or ``(A='x',B='00010')``.

        The key must name exactly the key fields of the entity set.
        """
        names = [k.name for k in entity_set.keys]
        if not names:
            raise ODataError("invalid_key", f"entity set {entity_set.name!r} has no key")
        expected = ", ".join(names)
        if not isinstance(key, dict) or set(key) != set(names):
            raise ODataError(
                "invalid_key",
                f"the key of entity set {entity_set.name!r} is exactly: {expected}",
            )
        parts: list[str] = []
        for definition in entity_set.keys:
            value = key[definition.name]
            if isinstance(value, str) and (
                not value
                or len(value) > MAX_KEY_VALUE_CHARS
                or ".." in value
                or any(ch in value for ch in _KEY_BREAKERS)
            ):
                raise ODataError(
                    "invalid_key",
                    f"the value of key {definition.name!r} is empty, too long or contains "
                    f"one of / \\ % ? # or '..', which a key in a URL path cannot carry here",
                )
            try:
                literal = self.literal(definition.type, value)
            except ODataError:
                raise ODataError(
                    "invalid_key",
                    f"the value of key {definition.name!r} is not a valid {definition.type} value",
                ) from None
            parts.append(quote(literal, safe="'"))
        if len(parts) == 1:
            return f"({parts[0]})"
        return "(" + ",".join(f"{name}={part}" for name, part in zip(names, parts)) + ")"

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
        zone = match.group(8)
        minutes = 0
        if zone != "Z":
            hours, mins = int(zone[1:3]), int(zone[4:6])
            if hours > 14 or mins > 59:
                raise ValueError("not an offset")
            minutes = (hours * 60 + mins) * (-1 if zone[0] == "-" else 1)
        # The ticks are the UTC instant; the offset says where it was meant.
        millis -= minutes * 60_000
        return f"/Date({millis}{'-' if minutes < 0 else '+'}{abs(minutes):04d})/"

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
                ) from None
        return out

    # -- query options ------------------------------------------------------
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

    def parse_error(self, response: httpx.Response) -> tuple[str, str]:
        """``(code, text)`` of an OData error envelope, or ``("", "")``.

        Only a real envelope is read: JSON ``{"error": {"code", "message":
        {"value"}}}`` or its XML form. An HTML error page (the ICF or a
        proxy answering instead of Gateway) yields nothing, so none of its
        markup, host names or dump text reaches a model.
        """
        content_type = response.headers.get("content-type", "").lower()
        if "html" in content_type:
            return "", ""
        try:
            text = response.text[:_MAX_ERROR_BODY]
        except Exception:  # noqa: BLE001 - an undecodable body is no envelope
            return "", ""
        if "xml" in content_type:
            code, message = _XML_CODE.search(text), _XML_MESSAGE.search(text)
            if not message:
                return "", ""
            return (
                html.unescape(code.group(1)).strip() if code else "",
                html.unescape(message.group(1)).strip(),
            )
        try:
            error = response.json().get("error")
        except (ValueError, AttributeError):
            return "", ""
        if not isinstance(error, dict):
            return "", ""
        code = error.get("code")
        message = error.get("message")
        if isinstance(message, dict):
            message = message.get("value")
        return (code if isinstance(code, str) else "", message if isinstance(message, str) else "")
