"""URL confinement for the OData built-in.

The host of every OData call comes from a BTP destination, and the
destination's credential (or the signed-in user's identity) travels with the
request. Anything an admin or a model can type is therefore only ever a
*path below that host*; these helpers refuse whatever could turn it into
another host or another endpoint.

Refusals never repeat the value: a path field is a place where a URL with a
token in its query gets pasted by mistake, and the message ends up in a 422.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING
from urllib.parse import unquote

if TYPE_CHECKING:  # models.py imports this module; the type is only a hint here
    from collections.abc import Mapping

    from .models import FieldDef

MAX_SERVICE_PATH = 512
MAX_SEGMENT = 1024
MAX_NEXT_LINK = 4096
MAX_FILTER_CHARS = 1000


def confine_service_path(value: str) -> str:
    """The service root below the destination's host, or ``ValueError``.

    A service path is stored once per catalogue service and every request of
    that service is built on it, so it is held to a stricter shape than a
    one-off request path: absolute, no query or fragment (``sap-client``
    belongs in the destination), no ``.``/``..`` segment, no empty segment,
    no trailing ``/`` (entity set names are appended with one). ``%`` and
    whitespace are refused as well: a real service root needs neither, and
    an encoded ``%2e%2e`` is a ``..`` to the server that decodes it.
    """
    if not isinstance(value, str):
        raise ValueError("service_path must be a string")
    if not value:
        raise ValueError("service_path is required")
    if len(value) > MAX_SERVICE_PATH:
        raise ValueError(f"service_path must be at most {MAX_SERVICE_PATH} characters")
    if any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in value):
        raise ValueError("service_path must not contain control characters")
    if any(ch.isspace() for ch in value):
        raise ValueError("service_path must not contain whitespace")
    if "\\" in value:
        raise ValueError("service_path must not contain a backslash")
    if "://" in value:
        raise ValueError("service_path is a path, not a URL; the host comes from the destination")
    if not value.startswith("/"):
        raise ValueError("service_path must start with '/'")
    if "?" in value or "#" in value:
        raise ValueError(
            "service_path must not contain '?' or '#'; query parameters belong in the destination"
        )
    if "%" in value:
        raise ValueError("service_path must not contain '%'")
    if value.endswith("/"):
        raise ValueError("service_path must not end with '/'")
    if "//" in value:
        raise ValueError("service_path must not contain '//'")
    if ".." in value or any(segment == "." for segment in value.split("/")):
        raise ValueError("service_path must not contain '.' or '..' segments")
    return value


def _has_control(text: str) -> bool:
    return any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in text)


_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
# Never part of one path segment, raw or decoded: they end the segment, end
# the path, or are a '/' to servers that normalise.
_SEGMENT_BREAKERS = ("/", "\\", "?", "#")


def _is_confined_segment(segment: object) -> bool:
    """Whether ``segment`` is exactly one path segment, to every reader.

    The segment is checked twice: as written, and as a server reads it after
    percent-decoding. The decoded form may not contain ``%`` at all, which
    rules out double encoding (``%252e%252e``) without guessing how often a
    proxy on the way decodes -- and the request does lose one level of
    encoding before it is sent, because ``DestinationAuth`` rebuilds the URL
    from the decoded path.
    """
    if not isinstance(segment, str) or not segment or len(segment) > MAX_SEGMENT:
        return False
    if not segment.isascii() or _has_control(segment) or any(ch.isspace() for ch in segment):
        return False
    if any(ch in segment for ch in _SEGMENT_BREAKERS):
        return False
    if "%" in _ESCAPE.sub("", segment):  # a '%' that is not an escape
        return False
    decoded = unquote(segment)
    if "%" in decoded or _has_control(decoded):
        return False
    if any(ch in decoded for ch in _SEGMENT_BREAKERS):
        return False
    return ".." not in decoded and decoded != "."


def join_path(service_path: str, *segments: str) -> str:
    """``service_path`` plus the given segments, or ``ValueError``.

    Every request path of the built-in is made here. A segment is an entity
    set, an entity set with its key predicate (already percent-encoded by the
    dialect), or a navigation name; it can be influenced by a model, so it
    must not be able to add a segment, a query or a fragment, or to climb out
    of the service root. The refusal never repeats the segment.
    """
    root = confine_service_path(service_path)
    for position, segment in enumerate(segments, start=1):
        if not _is_confined_segment(segment):
            raise ValueError(f"path segment {position} is not one confined URL segment")
    return "/".join((root, *segments))


_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")


def confine_next_link(link: str, service_path: str) -> str | None:
    """The paging link as a path (+ query) below ``service_path``, or ``None``.

    SAP returns ``__next`` / ``@odata.nextLink`` as an absolute URL that
    names *its* host, which the app never contacts directly: the host is
    dropped and only the path is kept, to be sent through the destination
    like every other request. A link is data from a response, so anything
    that does not stay under the service root is not followed (``None``); it
    is not an error, the caller pages with ``$skip`` instead.
    """
    if not isinstance(link, str) or not link or len(link) > MAX_NEXT_LINK:
        return None
    if _has_control(link) or any(ch.isspace() for ch in link) or "\\" in link or "#" in link:
        return None
    try:
        root = confine_service_path(service_path)
    except ValueError:
        return None
    scheme = _SCHEME.match(link)
    if scheme:
        if scheme.group(1).lower() not in ("http", "https"):
            return None
        rest = link[scheme.end() :]
        slash = rest.find("/")
        if slash < 0:
            return None
        authority = rest[:slash]
        if not authority or "?" in authority or "@" in authority:
            return None
        link = rest[slash:]
    elif link.startswith("//"):
        return None
    path, has_query, query = link.partition("?")
    full = path if path.startswith("/") else f"{root}/{path}"
    if not full.startswith(root + "/"):
        return None
    if not all(_is_confined_segment(segment) for segment in full[len(root) + 1 :].split("/")):
        return None
    return f"{full}?{query}" if has_query and query else full


class FilterError(ValueError):
    """A ``$filter`` expression the catalogue does not allow.

    ``code`` is one of the tool error codes (``unknown_field``,
    ``field_not_filterable``, ``invalid_argument``); ``message`` names at
    most a field or function name, never a literal from the expression.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_FILTER_WORDS = frozenset("and or not eq ne gt ge lt le null true false in has".split())
_FILTER_FUNCTIONS = frozenset(
    "substringof startswith endswith contains tolower toupper trim length indexof concat "
    "substring year month day hour minute second round floor ceiling".split()
)
# The alternatives are disjoint at every position, so the scan is linear.
_FILTER_TOKEN = re.compile(
    r"""
      (?P<space>[ ]+)
    | (?P<typed>(?:datetimeoffset|datetime|guid|time)'(?:[^']|'')*')
    | (?P<string>'(?:[^']|'')*')
    | (?P<number>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?[mMdDfFlL]?(?![A-Za-z0-9_.]))
    | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<punct>[(),])
    """,
    re.VERBOSE,
)
_SHOWN_NAME = 64


def check_filter(expr: str, fields: Mapping[str, FieldDef]) -> None:
    """Refuse a ``$filter`` that reads anything but filterable fields.

    A filter is an oracle: ``CreatedByUser eq 'X'`` answers a question about
    a field even when that field is never returned. So the expression is
    tokenised and every identifier in it must be a field the admin marked
    ``filterable``, an operator word or one of the listed functions. What is
    inside a quoted literal is a value and is not looked at.

    This is allowlisting, not parsing: anything the tokeniser does not know
    (``/`` navigation paths, ``&``, ``$``, ``;``, arithmetic words, an
    unterminated literal, non-ASCII outside a literal) is refused rather
    than passed on for SAP to judge. The expression still travels only as
    the *value* of the ``$filter`` parameter (the client sends it through
    ``params=``); this check is about fields, not about URL injection.
    """
    if not isinstance(expr, str):
        raise FilterError("invalid_argument", "filter must be a string")
    if len(expr) > MAX_FILTER_CHARS:
        raise FilterError(
            "invalid_argument", f"filter must be at most {MAX_FILTER_CHARS} characters"
        )
    if any(unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Zl", "Zp") for ch in expr):
        raise FilterError("invalid_argument", "filter must not contain control characters")
    # Tokenise the whole expression first, so that an expression the
    # tokeniser cannot read is refused as such whatever names it mentions.
    identifiers: list[tuple[str, bool]] = []  # (name, is a call)
    position = 0
    while position < len(expr):
        token = _FILTER_TOKEN.match(expr, position)
        if token is None:
            raise FilterError(
                "invalid_argument",
                f"filter has an unsupported character or an unfinished literal at position "
                f"{position + 1}; use field names, quoted values, and/or/not, comparison "
                f"operators and the supported functions",
            )
        position = token.end()
        if token.lastgroup == "ident":
            called = expr[position:].lstrip(" ").startswith("(")
            identifiers.append((token.group("ident"), called))
    for name, called in identifiers:
        shown = name if len(name) <= _SHOWN_NAME else name[:_SHOWN_NAME] + "..."
        # A field wins over an operator word or a function of the same name:
        # an entity set may have a field called `in`, `has` or `null`, and
        # reading the name as an operator would let it into a filter unseen.
        field = fields.get(name)
        if field is not None:
            if not field.filterable:
                raise FilterError(
                    "field_not_filterable", f"field {shown!r} cannot be used in a filter"
                )
            continue
        if name in _FILTER_WORDS:
            continue
        if called:
            if name not in _FILTER_FUNCTIONS:
                raise FilterError(
                    "invalid_argument", f"function {shown!r} is not supported in a filter"
                )
            continue
        raise FilterError("unknown_field", f"{shown!r} is not a field of this entity set")
