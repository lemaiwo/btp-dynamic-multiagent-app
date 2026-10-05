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

# -- OData V4 ---------------------------------------------------------------------
# The primitive types a V4 filter or sort order may compare. A field of any
# other type -- a complex type, a collection, a stream, a type this code does
# not know -- is no target: the type string comes verbatim from `$metadata`
# and is recognised positively, never guessed.
V4_COMPARABLE_TYPES = frozenset(
    "Edm.String Edm.Boolean Edm.Byte Edm.SByte Edm.Int16 Edm.Int32 Edm.Int64 Edm.Decimal "
    "Edm.Double Edm.Single Edm.Guid Edm.Date Edm.DateTimeOffset Edm.TimeOfDay Edm.Duration".split()
)
_V4_FILTER_WORDS = _FILTER_WORDS
# No `substringof` (V2 only), no `cast` / `isof` (they take a type name), no
# `any` / `all` (lambda operators) and no geo functions.
_V4_FILTER_FUNCTIONS = frozenset(
    "contains startswith endswith tolower toupper trim length indexof concat substring "
    "year month day hour minute second fractionalseconds date time totaloffsetminutes now "
    "round floor ceiling".split()
)
# What may not follow a bare literal: it would make it another token.
_V4_END = r"(?![A-Za-z0-9_.:'-])"
# V4 literals carry no type prefix and no type suffix: a GUID, a date and a
# timestamp are bare, a number is only a number. The two forms that do have a
# name before the quote are the duration and the enumeration member. Tried in
# this order, so a GUID or a date is never read as a number followed by text.
_V4_FILTER_TOKEN = re.compile(
    rf"""
      (?P<space>[ ]+)
    | (?P<string>'(?:[^']|'')*')
    | (?P<duration>duration'-?P[0-9DTHMS.]{{1,40}}')
    | (?P<enum>[A-Za-z_][A-Za-z0-9_]{{0,63}}(?:\.[A-Za-z_][A-Za-z0-9_]{{0,63}}){{1,8}}
        '[A-Za-z_][A-Za-z0-9_,]{{0,200}}')
    | (?P<guid>[0-9A-Fa-f]{{8}}(?:-[0-9A-Fa-f]{{4}}){{3}}-[0-9A-Fa-f]{{12}}{_V4_END})
    | (?P<stamp>[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}
        (?:T[0-9]{{2}}:[0-9]{{2}}(?::[0-9]{{2}}(?:\.[0-9]{{1,12}})?)?(?:Z|[+-][0-9]{{2}}:[0-9]{{2}}))?
        {_V4_END})
    | (?P<clock>[0-9]{{2}}:[0-9]{{2}}(?::[0-9]{{2}}(?:\.[0-9]{{1,12}})?)?{_V4_END})
    | (?P<number>-?[0-9]{{1,40}}(?:\.[0-9]{{1,40}})?(?:[eE][+-]?[0-9]{{1,4}})?{_V4_END})
    | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<punct>[(),])
    """,
    re.VERBOSE,
)
_SHOWN_NAME = 64
_GRAMMARS = {
    "v2": (_FILTER_TOKEN, _FILTER_WORDS, _FILTER_FUNCTIONS),
    "v4": (_V4_FILTER_TOKEN, _V4_FILTER_WORDS, _V4_FILTER_FUNCTIONS),
}


def check_filter(expr: str, fields: Mapping[str, FieldDef], *, version: str = "v2") -> None:
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

    ``version`` is the service's declared OData version and picks the
    grammar; the two are not mixed. ``"v4"`` reads the V4 literals (a bare
    GUID, date, timestamp and time of day, ``duration'P..'``, an enumeration
    member ``Namespace.Type'Member'``, numbers without a suffix) and the V4
    functions (``contains`` instead of ``substringof``), and refuses the V2
    forms (``guid'..'``, ``datetime'..'``, ``10.5M``). The lambda operators
    ``any`` / ``all`` and the path segments ``$it``, ``$root`` and ``$count``
    stay refused in both: a path leads to fields of another entity set, and
    nothing here could check those. In V4 a field must also have a type that
    can be compared (``V4_COMPARABLE_TYPES``): a complex or collection-valued
    field is no filter target, and a field of an enumeration type only where
    the expression holds a member literal of exactly that type.
    """
    grammar = _GRAMMARS.get(version) if isinstance(version, str) else None
    if grammar is None:
        raise FilterError("invalid_argument", "filters are not supported for this OData version")
    tokens, words, functions = grammar
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
    identifiers: list[tuple[str, bool, bool]] = []  # (name, is a call, is a literal prefix)
    enum_types: set[str] = set()
    position = 0
    while position < len(expr):
        token = tokens.match(expr, position)
        if token is None:
            raise FilterError(
                "invalid_argument",
                f"filter has an unsupported character or an unfinished literal at position "
                f"{position + 1}; use field names, quoted values, and/or/not, comparison "
                f"operators and the supported functions",
            )
        position = token.end()
        if token.lastgroup == "ident":
            rest = expr[position:]
            identifiers.append(
                (token.group("ident"), rest.lstrip(" ").startswith("("), rest.startswith("'"))
            )
        elif token.lastgroup == "enum":
            type_name = token.group("enum").partition("'")[0]
            if type_name.startswith("Edm."):
                raise FilterError(
                    "invalid_argument",
                    "an OData V4 value is written without a type name in front of it",
                )
            enum_types.add(type_name)
    for name, called, prefixes in identifiers:
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
            if version == "v4":
                edm_type = getattr(field, "type", "Edm.String")
                if edm_type not in V4_COMPARABLE_TYPES and edm_type not in enum_types:
                    raise FilterError(
                        "field_not_filterable",
                        f"field {shown!r} has a type that cannot be compared in a filter",
                    )
            continue
        if name in words:
            continue
        if version == "v4" and prefixes:
            # `guid'..'`, `datetime'..'`, `binary'..'`: the V2 way of writing a value.
            raise FilterError(
                "invalid_argument",
                "an OData V4 value is written without a type name in front of it "
                "(a GUID, a date and a timestamp are bare; a text is in single quotes)",
            )
        if called:
            if name not in functions:
                raise FilterError(
                    "invalid_argument", f"function {shown!r} is not supported in a filter"
                )
            continue
        raise FilterError("unknown_field", f"{shown!r} is not a field of this entity set")
