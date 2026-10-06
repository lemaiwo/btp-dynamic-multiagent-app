"""What the OData dialects share, kept apart from both.

``v2.V2Dialect`` and ``v4.V4Dialect`` differ in how a value is written and
in the payload shapes; the rules below are the same for both and live here
so that an edit made for one version cannot silently change the other:

* the key predicate -- the one place where a value chosen by a model becomes
  part of the request *path*. It leaves this module in a form that cannot be
  anything but one segment: every value is written by the dialect's
  ``literal`` (positive recognition of its EDM type), percent-encoded, and a
  string value that contains ``/ \\ % ? #`` or ``..`` is refused outright.
  The last rule is stricter than the protocol: ``DestinationAuth`` rebuilds
  the request URL from the *decoded* path, so an encoded ``%2F`` in a key
  would reach SAP as a real ``/`` and an encoded ``%`` would start an
  escape. ``urls.join_path`` checks the result again;
* reading an error envelope, and nothing but an envelope, from an answer;
* when a decimal given as a JSON NUMBER may be sent (``plain_float``): the
  one rule for a V2 or V4 body and a V2 or V4 URL literal.

No refusal repeats a value.
"""

from __future__ import annotations

import html
import math
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from .client import ODataError
from .models import EntitySetDef

MAX_KEY_VALUE_CHARS = 255
MAX_ERROR_BODY = 20_000
_KEY_BREAKERS = ("/", "\\", "%", "?", "#")

_XML_CODE = re.compile(r"<code[^<>]*>([^<]{0,200})</code>")
_XML_MESSAGE = re.compile(r"<message[^<>]*>([^<]{0,2000})</message>")


# Plain digits: no sign other than a leading minus, no exponent.
_PLAIN_DECIMAL = re.compile(r"-?[0-9]{1,40}(?:\.[0-9]{1,40})?")
# The most significant digits of a decimal passed as a NUMBER that are
# trusted: the JSON parser that read it has already rounded a longer one.
MAX_FLOAT_DIGITS = 15
DECIMAL_TEXT_HINT = (
    'pass it as text, for example "12.50": a number with more than 15 '
    "significant digits may already be rounded and is not sent"
)


def plain_float(value: float) -> str | None:
    """``repr(value)`` when a decimal given as a number may be sent as that
    text, else ``None``.

    THE rule for an ``Edm.Decimal`` that arrives as a JSON number, asked by
    both dialects for a body value and for a URL literal (a key, a function
    parameter): plain digits only (``repr`` switches to an exponent below
    0.0001 and from 1e16 on), and at most ``MAX_FLOAT_DIGITS`` significant
    digits; the ``.0`` that ``repr`` appends to a whole number is not one.
    A longer number was rounded by whoever parsed the JSON: in a key it
    could name another entity, in a body it would write another amount than
    the one meant. As text, every digit goes out as given.
    """
    text = repr(value) if math.isfinite(value) else ""
    if not _PLAIN_DECIMAL.fullmatch(text):
        return None
    digits = text.lstrip("-").removesuffix(".0").replace(".", "").lstrip("0")
    return text if len(digits) <= MAX_FLOAT_DIGITS else None


def refuse(edm_type: object) -> ODataError:
    """The refusal of a value that has not the form of ``edm_type``."""
    shown = (
        edm_type
        if isinstance(edm_type, str) and re.fullmatch(r"[A-Za-z0-9_.]{1,64}", edm_type)
        else "that type"
    )
    return ODataError("invalid_argument", f"the value is not a valid {shown} value")


def key_segment(
    literal: Callable[[str, Any], str],
    entity_set: EntitySetDef,
    key: Any,
    *,
    subject: str | None = None,
) -> str:
    """The key predicate, percent-encoded: ``('4500000001')`` or ``(A='x',B='00010')``.

    The key must name exactly the key fields of the entity set. ``literal``
    is the dialect's own (``V2Dialect.literal`` / ``V4Dialect.literal``).
    ``subject`` is what a refusal calls the owner of the key instead of
    ``entity set '<name>'``: the key of an operation bound to an entity set
    the caller cannot see must not name that set. Key field names are
    always said.
    """
    names = [k.name for k in entity_set.keys]
    whose = subject or f"entity set {entity_set.name!r}"
    if not names:
        raise ODataError("invalid_key", f"{whose} has no key")
    expected = ", ".join(names)
    if not isinstance(key, dict) or set(key) != set(names):
        raise ODataError(
            "invalid_key",
            f"the key of {whose} is exactly: {expected}",
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
            text = literal(definition.type, value)
        except ODataError:
            raise ODataError(
                "invalid_key",
                f"the value of key {definition.name!r} is not a valid {definition.type} value",
            ) from None
        parts.append(quote(text, safe="'"))
    if len(parts) == 1:
        return f"({parts[0]})"
    return "(" + ",".join(f"{name}={part}" for name, part in zip(names, parts)) + ")"


def read_error(response: httpx.Response, message_of: Callable[[Any], Any]) -> tuple[str, str]:
    """``(code, text)`` of an OData error envelope, or ``("", "")``.

    Only a real envelope is read: JSON ``{"error": {"code", "message"}}`` --
    ``message_of`` turns its ``message`` member into the text, which is
    where the versions differ -- or the XML form. An HTML error page (the
    ICF or a proxy answering instead of the service) yields nothing, so none
    of its markup, host names or dump text reaches a model.
    """
    content_type = response.headers.get("content-type", "").lower()
    if "html" in content_type:
        return "", ""
    try:
        text = response.text[:MAX_ERROR_BODY]
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
    except (ValueError, AttributeError, RecursionError):
        # RecursionError: a body nested deeper than the JSON parser goes
        # (`[[[[...`) is no envelope either, and must not become a 500.
        return "", ""
    if not isinstance(error, dict):
        return "", ""
    code = error.get("code")
    message = message_of(error.get("message"))
    return (code if isinstance(code, str) else "", message if isinstance(message, str) else "")
