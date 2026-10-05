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
