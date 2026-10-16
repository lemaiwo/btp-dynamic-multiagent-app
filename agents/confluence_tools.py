"""In-process Confluence tools over the Confluence REST API (``builtin:confluence``).

Attached when an agent lists the pseudo-URL ``builtin:confluence``. Like
``builtin:jira`` it holds no credential: the site URL and the ``Authorization``
header come from a BTP destination, resolved at call time, as the
destination's technical user. Read-only, three tools: ``search_pages``,
``get_page`` and ``list_children``.

Scope is configuration, never a tool argument: ``spaces`` pins the space keys
the agent may read (the CQL carries that clause, and a page outside the pin
is refused before its body is returned); ``api_base`` is the REST prefix
(``/wiki/rest/api`` on Confluence Cloud, ``/rest/api`` on Server / Data
Center). The model supplies words and page ids only; the CQL is built here.

Every refusal is ``{"error": {code, message, hint?}}`` with a fixed text,
never an exception and never Confluence's own text; a log line holds the
step, the status and the exception class only. Page text reaches the model
as plain text (:mod:`agents.confluence_text`) and is data written by other
people, never an instruction.

Setup: an HTTP destination whose URL is the bare Atlassian site
(``https://<site>.atlassian.net``) with ``BasicAuthentication`` (an account's
e-mail address and an API token that may read the spaces) -- the same
destination a Jira Cloud entry would use. ``spaces`` names the space keys;
``api_base`` stays blank on Cloud.
"""

from __future__ import annotations

import html
import logging
import re
import unicodedata
from typing import Any

from agents.jira_tools import normalize_api_base as _jira_api_base
from agents.jira_tools import normalize_csv_list

logger = logging.getLogger(__name__)

BUILTIN_CONFLUENCE_URL = "builtin:confluence"
# Confluence Cloud's REST prefix, and the default for `api_base`.
CONFLUENCE_API = "/wiki/rest/api"

MAX_BODY_CHARS = 60_000
MAX_QUERY_CHARS = 200
MAX_EXCERPT_CHARS = 300
MAX_TITLE_CHARS = 300
DEFAULT_SEARCH_LIMIT = 10
MAX_SEARCH_LIMIT = 25
DEFAULT_CHILD_LIMIT = 100
MAX_CHILD_LIMIT = 100
MAX_CHILD_START = 10_000
MAX_ANCESTORS = 20
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
SPACE_CACHE_SECONDS = 300
MAX_SPACE_CACHE = 1_000
TIMEOUT_SECONDS = 30.0

ERROR_CODES = frozenset({
    "destination_error", "unauthorized", "forbidden", "not_found", "throttled",
    "confluence_error", "unreachable", "not_json", "invalid_id", "invalid_query",
    "space_not_allowed", "too_large",
})

_SPACE_KEY_RE = re.compile(r"^[A-Za-z0-9_~-]{1,255}$")
_PAGE_ID_RE = re.compile(r"^[0-9]{1,20}$")
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$"
)
_TAG_RE = re.compile(r"<[^>]*>")
_HL_RE = re.compile(r"@@@(?:end)?hl@@@")
_SPACES_RE = re.compile(r"\s+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ConfluenceError(Exception):
    """A refusal; the tool answers it as ``{"error": {...}}``."""

    def __init__(self, code: str, message: str, hint: str | None = None) -> None:
        assert code in ERROR_CODES, code
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def as_result(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            error["hint"] = self.hint
        return {"error": error}


def normalize_spaces(value: Any) -> list[str]:
    """The pinned space keys as an ordered list of distinct keys.

    A JSON list or a comma-separated string (`normalize_csv_list`); empty means
    no pin. A key is interpolated into CQL, so one that is not 1-255 letters,
    digits, '_', '~' or '-' is a ValueError naming the rule, never the value:
    a quote or a parenthesis could otherwise close the clause.
    """
    keys = normalize_csv_list(value)
    for key in keys:
        if not _SPACE_KEY_RE.match(key):
            raise ValueError(
                "oauth.spaces: a Confluence space key is 1-255 letters, digits, "
                "'_', '~' or '-' (comma-separated for several)"
            )
    return keys


def normalize_api_base(value: Any) -> str:
    """The REST prefix, default ``/wiki/rest/api`` (Confluence Cloud).

    Jira's rules (`agents.jira_tools.normalize_api_base`: a path starting
    with '/', no host, query, fragment or '..') with this module's default;
    '/' alone means the site root.
    """
    text = str(value or "").strip()
    if not text:
        return CONFLUENCE_API
    checked = _jira_api_base(text)
    return checked if text.rstrip("/") else ""


def _cql_quote(value: str) -> str:
    """A CQL string literal: backslash first, then the quote."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _plain(value: Any) -> str:
    text = _CONTROL_RE.sub("", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return _SPACES_RE.sub(" ", text).strip()


def clean_query(text: Any) -> str:
    """The words of a search, or ``invalid_query``.

    Control and format characters go, whitespace is collapsed; `build_cql`
    quotes the rest, so nothing a model writes can add a clause.
    """
    cleaned = _plain(text)
    if not cleaned:
        raise ConfluenceError(
            "invalid_query", "the search needs 1 to 200 characters of text",
            hint="search for the object name or a few distinctive words",
        )
    if len(cleaned) > MAX_QUERY_CHARS:
        raise ConfluenceError(
            "invalid_query", "the search text is over 200 characters",
            hint="search for the object name or a few distinctive words",
        )
    return cleaned


def build_cql(query: str, spaces: list[str], *, in_title: bool = False) -> str:
    """The CQL of a search. Built here, never accepted from the model: raw
    CQL could address every space the credential can see."""
    field = "title" if in_title else "text"
    clauses = ["type = page", f"{field} ~ {_cql_quote(query)}"]
    if spaces:
        clauses.append("space in (" + ", ".join(_cql_quote(s) for s in spaces) + ")")
    return " AND ".join(clauses) + " ORDER BY lastmodified DESC"


def confine_page_id(value: Any) -> str:
    """A page id, or ``invalid_id``. Digits only: the id reaches an f-string
    that builds a path, and httpx normalises dot segments."""
    text = str(value if value is not None else "").strip()
    if not _PAGE_ID_RE.match(text):
        raise ConfluenceError(
            "invalid_id", "a page id is 1 to 20 digits",
            hint="use the id a search result or a child listing gave",
        )
    return text


def _one_line(value: Any, limit: int = MAX_TITLE_CHARS) -> str:
    return _plain(value)[:limit]


def _excerpt(value: Any) -> str:
    text = html.unescape(_TAG_RE.sub(" ", _HL_RE.sub("", str(value or ""))))
    return _one_line(text, MAX_EXCERPT_CHARS)


def _timestamp(value: Any) -> str:
    text = str(value or "").strip()
    return text if _TIMESTAMP_RE.match(text) else ""


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
