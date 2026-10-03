"""Masking of ``SAPDiagnose`` results in diagnose sessions.

Dumps, traces, authorization traces and gateway errors carry personal data:
SAP user names, e-mail addresses, bank accounts, business partner numbers and
the raw values of program variables. The IDE shows them to an LLM and to the
browser, so every diagnose-data result passes through :func:`mask_result`
first, and every message a user types in a diagnose session through
:func:`mask_text`.

Design rules, all load-bearing:

* **Allowlist first.** A payload is masked field by field only when it
  matches a *known shape* (``SHAPES``), and inside a known shape only the
  fields that shape lists pass; everything else is dropped and counted
  (``dropped_fields``). A listed field has a *kind*: an identifier (object
  name, kept after the user-name check), a number, a time, a
  system-generated id, or free text (redacted). A pattern list can only
  remove what somebody thought of; an allowlist fails towards "no data".
* **Fail safe.** Not JSON, a list, an unknown action, missing required keys,
  an oversized input or any exception while masking: metadata only (the
  character count and the *number* of top-level keys -- never key names,
  which may themselves be values).
* **Two passes.** The first pass learns user names anywhere in the payload:
  user-ish keys (matched by pattern), names used as dict keys under such a
  key, free-text mentions (``User....... NAME``, ``sap-user=``,
  ``SY-UNAME``) and strings that are themselves JSON. The second pass
  replaces every learned name everywhere, so a name learned from the last
  field is also gone from the first.
* **Variable values** of a dump are never shown: the variable *name* stays,
  the value becomes ``[VALUE len=N]`` unless it is a harmless scalar (a
  flag, a return code, an integer of at most six digits). Both ST22 layouts
  are parsed: ``name = value`` and name line followed by value lines.
* **Quoted literals** in free text (SQL, messages) become ``'[LITERAL]'``.
* **Pseudonyms** (``USER_1``, ``USER_2`` ...) are stable within one
  :class:`Masker`, which lives for one agent run or one request. The map is
  held in memory only: the class refuses pickling and copying, has no
  ``__dict__``, and its ``repr`` shows a count only.
* **Bounded work.** Every pattern is linear on a single long token (bounded
  quantifiers, no ``\\s*`` between optional parts), every string is capped
  before redaction, and the whole input has a size limit.
* **Pure.** No I/O. Logging records the shape name and the character count,
  never a value, an argument or an exception message.

Known limit, by design: in identifier fields (``program``, ``include`` ...)
a user name is replaced only as a whole ``_``-delimited word, so
``ZTEST_<NAME>`` keeps its real name there -- a finding has to open that
object. In free text ``_`` is a separator and the name is replaced.

The shapes come from the ARC-1 ``SAPDiagnose`` tool description; live
payloads are re-validated in a later task.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

logger = logging.getLogger(__name__)

__all__ = ["KEEP_DIGITS", "SHAPES", "USER_KEYS", "Masker", "mask_result", "mask_text"]

# --------------------------------------------------------------------------
# Contract constants (§1.5)
# --------------------------------------------------------------------------

# The minimum set of user keys. Matching is by pattern (``_is_user_key``),
# which covers these and their relatives (userId, modifiedBy, creator ...).
USER_KEYS: frozenset[str] = frozenset(
    {
        "user", "uname", "username", "userName", "sapUser", "traceUser", "createdBy",
        "changedBy", "lastChangedBy", "requestUser", "owner", "sy-uname", "syUname", "bname",
    }
)

# Keys whose values are measurements, times or system ids rather than
# business numbers. Each is listed with a kind in ``_BASE`` below; the kind,
# not this set, decides what is kept.
KEEP_DIGITS: frozenset[str] = frozenset(
    {
        "timestamp", "date", "time", "datetime", "created", "occurredAt", "line", "id",
        "traceId", "requestId", "duration", "grossTime", "netTime", "count", "executions",
        "hits",
    }
)

_MAX_INPUT = 2_000_000  # chars; larger results are metadata only
_MAX_STRING = 20_000  # every string is cut to this before redaction
_MAX_VALUE = 2_000  # plain values
_MAX_IDENT = 200
_MAX_JSON_DEPTH = 3  # strings that are JSON, nested

# Field kinds
_IDENT, _NUM, _TIME, _ID, _TEXT, _LONG, _BOX, _CHAPTERS, _FORMATTED = (
    "ident", "num", "time", "id", "text", "long", "box", "chapters", "formatted",
)


def _norm(key: str) -> str:
    """Key match is case-insensitive and ignores ``-``/``_`` (``sy-uname``
    and ``syUname`` are the same key)."""
    return key.lower().replace("-", "").replace("_", "")


def _kinds(kind: str, *names: str) -> dict[str, str]:
    return {_norm(n): kind for n in names}


# Scalar fields every shape may carry. Keys are normalised.
_BASE: dict[str, str] = {
    **_kinds(
        _IDENT,
        "program", "include", "class", "className", "method", "exception", "runtimeError",
        "tcode", "transaction", "objectType", "authObject", "table", "service",
        "processType", "type", "client", "callingProgram", "calledProgram",
        "package", "component", "functionModule", "report", "analysis", "kind", "state",
        *(f"field{i}" for i in range(1, 11)),
        *(f"value{i}" for i in range(1, 11)),
    ),
    **_kinds(
        _NUM,
        "line", "duration", "grossTime", "netTime", "hits", "executions", "count", "total",
        "size", "rc", "status", "level", "maxExecutions", "active", "aggregate", "sqlTrace",
        "records",
    ),
    **_kinds(
        _TIME,
        "timestamp", "date", "time", "datetime", "created", "createdAt", "changedAt",
        "occurredAt", "expires", "expiresAt", "startTime", "endTime",
    ),
    **_kinds(_ID, "id", "traceId", "requestId"),
    **_kinds(
        _TEXT,
        "shortText", "description", "title", "message", "statement", "url", "detailUrl",
        "errorType", "objectName",  # "Frontend Error"; an object name or a URL
    ),
    **_kinds(_LONG, "text", "content", "lines"),
}


@dataclass(frozen=True)
class Shape:
    """A known payload: which call produces it, which keys prove it and
    which fields may pass.

    ``variant`` is ``list`` for a call without ``id``, ``detail`` for one
    with ``id``, and for ``traces`` with ``id`` the ``analysis`` value
    (default ``hitlist``). ``required`` holds alternatives: the payload
    matches when *any* of the key sets is fully present. ``fields`` maps a
    normalised key to its kind, at any depth of the payload.
    """

    action: str
    variant: str
    required: tuple[frozenset[str], ...]
    fields: Mapping[str, str]


def _shape(
    action: str, variant: str, required: tuple[set[str], ...], **extra: str
) -> Shape:
    fields = dict(_BASE)
    fields.update({_norm(k): v for k, v in extra.items()})
    return Shape(
        action, variant, tuple(frozenset(r) for r in required), MappingProxyType(fields)
    )


SHAPES: dict[str, Shape] = {
    "dumps_list": _shape("dumps", "list", ({"dumps"},), dumps=_BOX),
    "dump_detail": _shape(
        "dumps",
        "detail",
        ({"chapters"}, {"sections"}, {"formattedText"}),
        chapters=_CHAPTERS,
        sections=_CHAPTERS,
        formattedText=_FORMATTED,
    ),
    "traces_list": _shape("traces", "list", ({"traces"},), traces=_BOX),
    "trace_hitlist": _shape("traces", "hitlist", ({"hitlist"},), hitlist=_BOX),
    "trace_statements": _shape(
        "traces", "statements", ({"statements"},), statements=_BOX, children=_BOX
    ),
    "trace_dbaccesses": _shape("traces", "dbAccesses", ({"dbAccesses"},), dbAccesses=_BOX),
    "authorization_trace": _shape("authorization_trace", "list", ({"rows"},), rows=_BOX),
    "gateway_errors_list": _shape("gateway_errors", "list", ({"errors"},), errors=_BOX),
    "gateway_error_detail": _shape(
        "gateway_errors", "detail", ({"errorType"},), callStack=_BOX
    ),
    "odata_perf": _shape("odata_perf", "list", ({"requests"},), requests=_BOX),
    "trace_requests": _shape(
        "trace_requests", "list", ({"traceRequests"},), traceRequests=_BOX
    ),
    "sql_trace_state": _shape("sql_trace_state", "list", ({"active"}, {"state"})),
    "sql_trace_directory": _shape(
        "sql_trace_directory",
        "list",
        ({"files"}, {"traces"}, {"directory"}),
        files=_BOX,
        traces=_BOX,
        directory=_BOX,
    ),
}

_TOOL = "SAPDiagnose"
_VAR_CHAPTER_IDS = frozenset({"kap8", "kap9"})

# --------------------------------------------------------------------------
# Patterns. Each is linear on one long token: bounded quantifiers, a literal
# or a lookbehind as left anchor, ``[ \t]`` instead of ``\s``.
# --------------------------------------------------------------------------

# User-ish keys, on the normalised key. ``author`` must not match
# ``authorization``.
_USER_KEY = re.compile(
    r"user|uname|bname|owner|creator|author(?!i[sz])"
    r"|(?:created|changed|modified|executed|started|triggered)by"
)

# Zero-width and soft-hyphen characters split a name or an address in two.
_INVISIBLE = dict.fromkeys(
    [0x00AD, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0x2061, 0x2062, 0x2063, 0xFEFF]
)

_AT = re.compile(r"@|%(?:25){0,8}40")
_DOMAIN = re.compile(r"[\w\-]{1,63}(?:\.[\w\-]{1,63}){1,8}")
_LOCAL_EXTRA = frozenset("._%+-")
_IBAN = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]{2}\d{2}(?:[ .\-]?[A-Za-z0-9]{4}){2,7}(?:[ .\-]?\d{1,3})?"
    r"(?![A-Za-z0-9])"
)
_DIGITS = re.compile(r"(?<!\d)\+?\d(?:[ .\-/]?\d){8,}(?!\d)")
_LITERAL = re.compile(
    r"(?<![A-Za-z0-9])'([^'\n]{0,200})'|\"([^\"\n]{0,200})\"|`([^`\n]{0,200})`"
)
_PSEUDONYM = re.compile(r"USER_\d+")

# Free-text user mentions: keyword, separators (dot leader, colon, equals,
# quotes), then the name.
_FREE_USER = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?i:sap-user|sy-uname|user(?:[ _\-]?(?:name|id))?|uname|bname|benutzer(?:name)?"
    r"|(?:created|changed|modified|executed|started|triggered)[ _]?by|owner|creator|author)"
    r"(?![A-Za-z0-9])"
    r"([ \t.:=\"'>]{1,60})"
    r"([A-Za-z0-9_$][A-Za-z0-9_.$\-]{2,11})(?![A-Za-z0-9_$])"
)
_NOT_NAMES = frozenset(
    {
        "NAME", "ID", "EXIT", "COMMAND", "TYPE", "DATA", "NOT", "AND", "THE", "FOR", "WITH",
        "TRUE", "FALSE", "NULL", "NONE", "INITIAL", "SPACE", "HAS", "WAS", "MASTER", "GROUP",
        "ROLE", "PROFILE", "INTERFACE", "PARAMETER", "PARAMETERS", "SESSION", "CONTEXT",
        "DEFINED", "SPECIFIC", "AUTHORIZATION", "ERROR", "EXCEPTION", "UNKNOWN", "SYSTEM",
        "ANONYMOUS", "N/A",
    }
)
# Under a user key these are kept as they are: they name nobody.
_PLACEHOLDERS = frozenset(
    {"", "-", "*", "N/A", "NA", "NONE", "NULL", "UNKNOWN", "SYSTEM", "ANONYMOUS", "INITIAL"}
)

_IDENT_OK = re.compile(r"[A-Za-z0-9_/$*.\-=<>~:]{1,%d}" % _MAX_IDENT)
_TIME_OK = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d{1,9})?)?)?(?:Z|[+\-]\d{2}:?\d{2})?"
    r"|\d{2}\.\d{2}\.\d{4}|\d{2}:\d{2}(?::\d{2})?|20\d{6,18}"
)
_NUM_OK = re.compile(r"-?\d{1,20}(?:[.,]\d{1,9})?")
_STAMP = re.compile(r"20\d{6,18}")
_UUID = re.compile(r"[0-9A-Fa-f]{8}(?:-?[0-9A-Fa-f]{4}){3}-?[0-9A-Fa-f]{12}")
_SECTION_KEY = re.compile(r"[A-Za-z0-9_]{1,20}")

# Dump variables. A name is one token (``LV_X``, ``<FS>-F``, ``ME->ATTR``,
# ``ZCL=>ATTR``, ``ITAB[1]-F``); ``=(?!=)`` keeps ``ZCL_X=====CP`` out.
_VAR_NAME = r"(?:[A-Za-z0-9_<>\-\[\]()~/%*+.$&:]|=>){1,80}"
_ASSIGN = re.compile(rf"([ \t]{{0,40}}{_VAR_NAME}[ \t]{{0,40}}=(?![=>])[ \t]{{0,40}})(.*)")
_NAME_ONLY = re.compile(_VAR_NAME)
_STRONG_NAME = re.compile(r"[A-Z0-9_<>\-\[\]()~/%*+.$&:=]*[_\-<\[][A-Z0-9_<>\-\[\]()~/%*+.$&:=]*")
_HARMLESS_INT = re.compile(r"[+\-]?\d{1,6}")
_HARMLESS_WORDS = frozenset({"abap_true", "abap_false", "initial", "space", "true", "false"})


def _is_user_key(keyn: str) -> bool:
    return _USER_KEY.search(keyn) is not None


def _replaceable(name: str) -> bool:
    """A name that may be replaced wherever it occurs. Short, numeric and
    placeholder values would rewrite unrelated text (``-``, ``1``)."""
    return len(name) >= 3 and not name.isdigit() and name not in _NOT_NAMES and bool(
        re.search(r"[A-Za-z]", name)
    ) and not _PSEUDONYM.fullmatch(name)


# --------------------------------------------------------------------------
# Masker
# --------------------------------------------------------------------------


class Masker:
    """One run's pseudonym map. In memory only, never serialised or logged."""

    __slots__ = ("_map", "_names", "_free", "_ident", "_size")

    def __init__(self) -> None:
        self._map: dict[str, str] = {}
        self._names: list[str] = []  # the replaceable subset of _map
        self._free: re.Pattern[str] | None = None
        self._ident: re.Pattern[str] | None = None
        self._size = -1

    def __repr__(self) -> str:
        return f"<Masker users={len(self._map)}>"

    __str__ = __repr__

    def __reduce_ex__(self, protocol: object) -> Any:
        raise TypeError("Masker holds personal data and cannot be serialised")

    def __copy__(self) -> Masker:
        raise TypeError("Masker cannot be copied")

    def __deepcopy__(self, memo: object) -> Masker:
        raise TypeError("Masker cannot be copied")

    def pseudonym(self, name: str) -> str:
        """``USER_n`` for ``name`` (case-insensitive), assigned on first
        sight. A placeholder (``-``, ``SYSTEM``) is returned unchanged."""
        key = name.translate(_INVISIBLE).strip().upper()
        if key in _PLACEHOLDERS:
            return name
        if key not in self._map:
            self._map[key] = f"USER_{len(self._map) + 1}"
            if _replaceable(key):
                self._names.append(key)
        return self._map[key]

    def replace_names(self, text: str, identifier: bool = False) -> str:
        """Replace every known name in ``text``. In free text ``_`` is a
        separator (``ZTEST_NAME``); in an identifier field it is part of
        the word, so an object name stays openable."""
        if not self._names or not text:
            return text
        if self._size != len(self._names):
            alternation = "|".join(
                re.escape(n) for n in sorted(self._names, key=len, reverse=True)
            )
            self._free = re.compile(
                rf"(?<![A-Za-z0-9])(?:{alternation})(?![A-Za-z0-9])", re.IGNORECASE
            )
            self._ident = re.compile(
                rf"(?<![A-Za-z0-9_])(?:{alternation})(?![A-Za-z0-9_])", re.IGNORECASE
            )
            self._size = len(self._names)
        pattern = self._ident if identifier else self._free
        assert pattern is not None
        return pattern.sub(lambda m: self._map[m.group(0).upper()], text)

    def collect_free_text(self, text: str) -> None:
        """Register names that free text names as a user."""
        for m in _FREE_USER.finditer(text):
            seps, name = m.group(1), m.group(2).rstrip(".-/")
            upper = name.upper()
            if not _replaceable(upper):
                continue
            # ``user=x``, ``User "x"``: explicit. Otherwise prose follows the
            # keyword ("user not found"), so the name must look like one.
            strong = any(c in seps for c in "=\"'")
            looks_like_user = name == upper or any(c.isdigit() for c in name)
            if strong or looks_like_user:
                self.pseudonym(name)


# --------------------------------------------------------------------------
# String rules
# --------------------------------------------------------------------------


def _clean(text: str) -> str:
    return text.translate(_INVISIBLE)


def _redact_emails(text: str) -> str:
    """Find each ``@`` (also ``%40``, ``%2540``) and widen to the address.
    Scanning from the ``@`` is linear; a pattern starting at the local part
    is quadratic on a long token."""
    out: list[str] = []
    done = 0
    for m in _AT.finditer(text):
        if m.start() < done:
            continue
        domain = _DOMAIN.match(text, m.end())
        if domain is None:
            continue
        start = m.start()
        while start > done and (text[start - 1].isalnum() or text[start - 1] in _LOCAL_EXTRA):
            start -= 1
        if start == m.start():
            continue
        out.append(text[done:start])
        out.append("[EMAIL]")
        done = domain.end()
    if not out:
        return text
    out.append(text[done:])
    return "".join(out)


def _redact(text: str, digits: bool = True) -> str:
    text = _redact_emails(text)
    text = _IBAN.sub("[IBAN]", text)
    if digits:
        text = _DIGITS.sub("[NUMBER]", text)
    return text


def _harmless(value: str) -> bool:
    """A variable value that cannot identify anybody: empty, one character,
    a flag, a return code, a small integer or one of our pseudonyms."""
    v = value.strip()
    return (
        len(v) <= 1
        or _HARMLESS_INT.fullmatch(v) is not None
        or v.lower() in _HARMLESS_WORDS
        or _PSEUDONYM.fullmatch(v) is not None
    )


def _literal(m: re.Match[str]) -> str:
    inner = next(g for g in m.groups() if g is not None)
    return m.group(0) if _harmless(inner) else "'[LITERAL]'"


def _literals(text: str) -> str:
    return _LITERAL.sub(_literal, text)


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"[… truncated {len(text) - limit} chars]"


def _mask_value(value: str) -> str:
    """A variable value with its indentation kept."""
    if _harmless(value):
        return value
    stripped = value.strip()
    lead = value[: len(value) - len(value.lstrip())]
    return f"{lead}[VALUE len={len(stripped)}]"


def _unexpected(value: str) -> str:
    """A typed field (identifier, number, time, id) whose value does not
    look like its type. The field was allowlisted for that type only, so the
    value is not shown."""
    return f"[VALUE len={len(value)}]" if value else value


def _is_variables_heading(line: str) -> bool:
    s = line.strip(" \t|-=*#:")
    return 0 < len(s) <= 60 and "variable" in s.lower() and "=" not in s


def _mask_variables(lines: list[str]) -> list[str]:
    """Name/value pairs of a dump's variable listing. Handles ``name =
    value`` and the ST22 layout (name line, then indented value lines).
    Anything that is not clearly a name is a value."""
    out: list[str] = []
    expect_value = False
    for i, line in enumerate(lines):
        if not line.strip():
            out.append(line)
            continue
        if _is_variables_heading(line):
            out.append(_redact(line))
            expect_value = False
            continue
        if expect_value:
            # Fail safe: the line after a name is its value, whatever it looks like.
            out.append(_mask_value(line))
            expect_value = False
            continue
        m = _ASSIGN.fullmatch(line)
        if m:
            out.append(_redact(m.group(1)) + _mask_value(m.group(2)))
            continue
        bare = line.rstrip()
        if not line[0].isspace() and _NAME_ONLY.fullmatch(bare):
            nxt = next((x for x in lines[i + 1 : i + 4] if x.strip()), "")
            if _STRONG_NAME.fullmatch(bare) or nxt[:1].isspace():
                out.append(_redact(bare))
                expect_value = True
                continue
        out.append(_mask_value(line))
    return out


def _mask_assignments(lines: list[str]) -> list[str]:
    """Outside a variables section only ``name = value`` lines are cut."""
    out = []
    for line in lines:
        m = _ASSIGN.fullmatch(line)
        out.append(m.group(1) + _mask_value(m.group(2)) if m else line)
    return out


def _digit_count(number: int | float) -> int:
    return sum(c.isdigit() for c in repr(number))


# --------------------------------------------------------------------------
# Structured masking
# --------------------------------------------------------------------------


def _is_variables_chapter(chapter_id: Any, title: Any) -> bool:
    if isinstance(chapter_id, str) and chapter_id.strip().lower() in _VAR_CHAPTER_IDS:
        return True
    return isinstance(title, str) and "variable" in title.lower()


def _embedded_json(text: str) -> Any:
    """The parsed value when ``text`` is itself a JSON object or array."""
    s = text.strip()
    if len(s) < 2 or len(s) > _MAX_STRING or s[0] not in "{[" or s[-1] not in "}]":
        return None
    try:
        data = json.loads(s)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, (dict, list)) else None


class _Run:
    """One ``mask_result`` pass over a parsed payload."""

    def __init__(self, masker: Masker, fields: Mapping[str, str]) -> None:
        self.m = masker
        self.fields = fields
        self.dropped = 0

    # ---- pass 1: learn every user name before anything is replaced -------

    def collect(self, value: Any, under_user: bool = False, depth: int = 0) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if under_user:
                    self.m.pseudonym(k)  # {"byUser": {"NAME": 3}}
                    if v is None or isinstance(v, (int, float, bool)):
                        continue  # a count per user, not a name
                is_user = under_user or _is_user_key(_norm(k))
                self.collect(v, is_user, depth)
        elif isinstance(value, list):
            for v in value:
                self.collect(v, under_user, depth)
        elif isinstance(value, str):
            if under_user:
                self.m.pseudonym(value)
                return
            text = _clean(value)
            self.m.collect_free_text(text)
            if depth < _MAX_JSON_DEPTH:
                inner = _embedded_json(text)
                if inner is not None:
                    self.collect(inner, False, depth + 1)
        elif under_user and isinstance(value, (int, float)) and not isinstance(value, bool):
            self.m.pseudonym(repr(value))

    # ---- pass 2: rewrite --------------------------------------------------

    def users(self, value: Any) -> Any:
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                # Under a user key a dict is keyed by user; its numbers are counts.
                keep = isinstance(v, (int, float, bool)) or v is None
                out[self.m.pseudonym(k)] = v if keep else self.users(v)
            return out
        if isinstance(value, list):
            return [self.users(v) for v in value]
        if isinstance(value, str):
            return self.m.pseudonym(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return self.m.pseudonym(repr(value))
        return value

    def text(self, raw: str, limit: int = _MAX_VALUE) -> str:
        text = _clean(raw)
        # A string that is itself JSON is masked as a record of this shape,
        # with the same allowlist. Nesting ends: each level is a shorter string.
        inner = _embedded_json(text)
        if inner is not None:
            masked = self.any(inner, _TEXT, False)
            return _cap(json.dumps(masked, ensure_ascii=False), limit)
        text = self.m.replace_names(text)  # before the cut: no half name left behind
        total = len(text)
        text = _redact(_literals(text[:_MAX_STRING]))
        if len(text) > limit or total > _MAX_STRING:
            kept = text[:limit]
            return kept + f"[… truncated {max(total, len(text)) - len(kept)} chars]"
        return text

    def variables(self, raw: str) -> str:
        text = self.m.replace_names(_clean(raw))
        cut = len(text) - _MAX_STRING
        lines = _mask_variables(text[:_MAX_STRING].split("\n"))
        if cut > 0:
            lines.append(f"[… truncated {cut} chars]")
        return "\n".join(lines)

    def formatted(self, raw: str) -> str:
        text = self.m.replace_names(_clean(raw))
        cut = len(text) - _MAX_STRING
        lines = text[:_MAX_STRING].split("\n")
        start = next((i for i, ln in enumerate(lines) if _is_variables_heading(ln)), len(lines))
        head = _redact(_literals("\n".join(_mask_assignments(lines[:start]))))
        out = ([head] if start else []) + _mask_variables(lines[start:])
        if cut > 0:
            out.append(f"[… truncated {cut} chars]")
        return "\n".join(out)

    def ident(self, raw: str) -> str:
        text = _clean(raw)
        if not _IDENT_OK.fullmatch(text):
            return _unexpected(text)  # not an object name after all
        return _redact(self.m.replace_names(text, identifier=True))

    def ident_id(self, raw: str) -> str:
        text = _clean(raw)
        if not _IDENT_OK.fullmatch(text):
            return _unexpected(text)
        text = self.m.replace_names(text, identifier=True)
        if text.isdigit():
            # A bare number is an id only when it is short or a timestamp;
            # otherwise it may be a business key.
            return text if _STAMP.fullmatch(text) or len(text) <= 6 else "[NUMBER]"
        system = re.search(r"[A-Za-z]", text) is not None or bool(_UUID.fullmatch(text))
        return _redact(text, digits=not system)

    def scalar(self, value: Any, kind: str, variables: bool) -> Any:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            if kind in (_NUM, _TIME):
                return value
            digits = _digit_count(value)
            if kind == _ID and isinstance(value, int) and _STAMP.fullmatch(str(value)):
                return value
            limit = 7 if kind == _ID else 9
            return "[NUMBER]" if digits >= limit else value
        if not isinstance(value, str):
            return None  # not a JSON type; cannot happen after json.loads
        if kind == _IDENT:
            return self.ident(value)
        if kind == _ID:
            return self.ident_id(value)
        if kind == _NUM:
            return value if _NUM_OK.fullmatch(value) else _unexpected(value)
        if kind == _TIME:
            return value if _TIME_OK.fullmatch(value) else _unexpected(value)
        if kind == _FORMATTED:
            return self.formatted(value)
        if kind == _LONG:
            return self.variables(value) if variables else self.text(value, _MAX_STRING)
        return self.text(value)

    def any(self, value: Any, kind: str, variables: bool) -> Any:
        if isinstance(value, dict):
            return self.record(value, variables)
        if isinstance(value, list):
            if kind == _LONG and variables and all(isinstance(v, str) for v in value):
                # A chapter as a list of lines: pair names and values across lines.
                return self.variables("\n".join(value)).split("\n")
            return [self.any(v, kind, variables) for v in value]
        return self.scalar(value, _TEXT if kind in (_BOX, _CHAPTERS) else kind, variables)

    def chapters(self, value: Any) -> Any:
        if isinstance(value, list):
            out = []
            for ch in value:
                if isinstance(ch, dict):
                    flag = _is_variables_chapter(ch.get("id"), ch.get("title"))
                    out.append(self.record(ch, flag))
                else:
                    out.append(self.any(ch, _LONG, False))
            return out
        if isinstance(value, dict):
            out_d: dict[str, Any] = {}
            for k, v in value.items():
                if not _SECTION_KEY.fullmatch(k):
                    self.dropped += 1
                    continue
                title = v.get("title") if isinstance(v, dict) else None
                flag = _is_variables_chapter(k, title)
                key = self.m.replace_names(k, identifier=True)
                out_d[key] = self.record(v, flag) if isinstance(v, dict) else self.any(
                    v, _LONG, flag
                )
            return out_d
        return self.any(value, _LONG, False)

    def record(self, value: dict[str, Any], variables: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in value.items():
            kn = _norm(k)
            if _is_user_key(kn):
                out[_redact(self.m.replace_names(k))] = self.users(v)
                continue
            kind = self.fields.get(kn)
            if kind is None:
                self.dropped += 1
                continue
            if kind == _CHAPTERS:
                out[k] = self.chapters(v)
            else:
                out[k] = self.any(v, kind, variables)
        return out


def _variant(action: str, args: dict[str, Any]) -> str | None:
    has_id = args.get("id") not in (None, "")
    if not has_id:
        return "list"
    if action == "traces":
        analysis = args.get("analysis", "hitlist")
        return analysis if isinstance(analysis, str) and analysis else None
    return "detail"


def _is_diagnose(tool: Any) -> bool:
    """``SAPDiagnose``, also behind an MCP server prefix (``arc1_SAPDiagnose``)."""
    return isinstance(tool, str) and (tool == _TOOL or tool.endswith("_" + _TOOL))


def _match_shape(tool: Any, args: Any, data: Any) -> str | None:
    if not _is_diagnose(tool) or not isinstance(args, dict) or not isinstance(data, dict):
        return None
    action = args.get("action")
    if not isinstance(action, str):
        return None
    variant = _variant(action, args)
    for name, shape in SHAPES.items():
        if shape.action != action or shape.variant != variant:
            continue
        if any(req <= data.keys() for req in shape.required):
            return name
    return None


def _metadata(text: Any, data: Any) -> str:
    """The fail-safe answer. Key names are not returned: in an unknown
    payload a key can be a value (``{"<user name>": ...}``)."""
    chars = len(text) if isinstance(text, str) else 0
    keys = len(data) if isinstance(data, dict) else 0
    return json.dumps({"masked": True, "shape": "unknown", "chars": chars, "keys": keys})


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


# Public names for ``agents.ide.findings``, which reads the same shapes from
# the same (size-capped) results.
match_shape = _match_shape
MAX_INPUT = _MAX_INPUT


def mask_result(tool: str, args: dict[str, Any], text: str, masker: Masker) -> str:
    """Mask one diagnose-data tool result; metadata only when unrecognised."""
    if not isinstance(text, str):
        logger.debug("diagnose result masked: shape=unknown chars=0")
        return _metadata(text, None)
    if len(text) > _MAX_INPUT:
        logger.debug("diagnose result masked: shape=oversized chars=%d", len(text))
        return _metadata(text, None)
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        logger.debug("diagnose result masked: shape=unknown chars=%d", len(text))
        return _metadata(text, None)
    shape = _match_shape(tool, args, data)
    if shape is None:
        logger.debug("diagnose result masked: shape=unknown chars=%d", len(text))
        return _metadata(text, data)
    try:
        run = _Run(masker, SHAPES[shape].fields)
        run.collect(data)
        masked = run.record(data)
        if run.dropped:
            masked["dropped_fields"] = run.dropped
        result = json.dumps(masked, ensure_ascii=False)
    except Exception as exc:  # fail safe: never fall through with raw data
        logger.warning("diagnose masking failed: shape=%s error=%s", shape, type(exc).__name__)
        return _metadata(text, data)
    logger.debug("diagnose result masked: shape=%s chars=%d", shape, len(text))
    return result


def mask_text(text: str, masker: Masker | None = None) -> str:
    """For user-typed diagnose messages: free-text user names become
    pseudonyms, then e-mail, IBAN and long numbers are redacted. Nothing is
    cut and quoted text stays: it is the user's own question."""
    m = masker if masker is not None else Masker()
    text = _clean(text)
    m.collect_free_text(text)
    return _redact(m.replace_names(text))
