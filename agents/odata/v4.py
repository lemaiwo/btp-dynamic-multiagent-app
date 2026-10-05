"""What is specific to OData V4: literals, options, payloads. Reads only.

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

Writes and actions are not part of this module yet: ``supports_write`` is
``False`` and ``ODataClient.check_write`` refuses before anything is sent.

No refusal repeats a value. The payload shapes follow the OData 4.0 JSON
format and are not verified against a live system.
"""

from __future__ import annotations

import datetime
import math
import re
from typing import Any

import httpx

from . import common
from .client import ODataError, ReadQuery
from .common import refuse as _refuse
from .models import EntitySetDef
from .urls import V4_COMPARABLE_TYPES

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
_NOT_YET = "writing to an OData V4 service is not supported yet; only reads are"


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
    """OData V4 as SAP's RAP and Gateway V4 services speak it (reads)."""

    version = "v4"
    # `ODataClient.check_write` refuses a write while this is False, with
    # this text (its one source).
    supports_write = False
    write_refusal = _NOT_YET
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
            if isinstance(value, float) and math.isfinite(value):
                text = repr(value)
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

    # -- request bodies (the next task) -------------------------------------
    def update_request(self, path: str, body: dict | None) -> tuple[str, dict[str, str]]:
        raise ODataError("operation_disabled", _NOT_YET)

    def encode_body(self, entity_set: EntitySetDef, body: dict) -> dict:
        raise ODataError("operation_disabled", _NOT_YET)

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
