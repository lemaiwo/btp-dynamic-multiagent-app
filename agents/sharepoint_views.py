"""The config of a ``builtin:sharepoint`` entry: the pinned file and its views.

An admin pins ONE workbook (``site``, ``library``, ``path``) and declares named
views of it; an agent can only name a view. This module is the one reading of
that config: the save-time gate (``agents.admin``), storage (``agents.db``) and
the toolset (``agents.sharepoint_tools``) all go through :func:`check_pins`
and :func:`parse_views`, so none of them can accept what another refuses.

Two kinds of view:

* ``table`` -- an Excel table by name, and the columns of it that may be
  returned (``columns``). Nothing else of the table reaches the model.
* ``calendar`` -- a grid with one column per day: ``date_row`` holds the
  dates from ``first_date_column`` on, the rows from ``first_row`` on hold one
  line per member, described by the ``labels`` columns (``member`` required,
  ``team`` and ``kind`` optional). ``sheet`` may contain ``{year}``.
  ``codes`` maps what a cell holds to the status the model gets; a value that
  is not listed never passes (see :mod:`agents.sharepoint_workbook`).
  ``stop_at`` ends the rows at a label, ``lookup`` adds pinned columns of a
  table view by member, ``conflict`` compares two kinds of row of one member.

Unknown keys are refused, not dropped: a typo in ``codes`` or ``stop_at``
would otherwise be a view that silently reads something else. A refusal names
the field and the rule, never a value; a view name is repeated only once it
has passed :data:`VIEW_NAME_RE`. No imports beyond the standard library.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CalendarView", "Conflict", "Lookup", "TableView", "ViewConfigError",
    "check_pins", "clean_views", "column_index", "parse_views", "site_host",
    "views_config",
]

MAX_VIEWS = 10
MAX_COLUMNS = 50
MAX_CODES = 100
MAX_ROW = 1_048_576
MAX_COLUMN = 16_384
MAX_PATH_CHARS = 400

VIEW_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
# What a status may look like. The vocabulary is the admin's (``unavailable``,
# ``half_day``, a guard group), so only the form is fixed.
STATUS_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
# What the tool itself reports for a value outside ``codes``.
RESERVED_STATUSES = frozenset({"unmapped"})
LABEL_KEYS = ("member", "team", "kind")
# The keys of a run as ``read_calendar`` returns it. A ``lookup`` adds its
# columns to that same object, so it must not add one of these names.
RUN_KEYS = ("member", "team", "kind", "status", "from", "to")

_COLUMN_RE = re.compile(r"[A-Z]{1,3}")
_TABLE_RE = re.compile(r"[A-Za-z_\\][A-Za-z0-9_.\\]{0,254}")
_SITE_RE = re.compile(
    r"([a-z0-9][a-z0-9-]{0,62}\.sharepoint\.com):((?:/[A-Za-z0-9._-]{1,128}){1,4})"
)
_SHEET_FORBIDDEN = frozenset("[]:*?/\\")
_PATH_FORBIDDEN = frozenset('\\%?#:*"<>|')
_YEAR = "{year}"


class ViewConfigError(ValueError):
    """A ``builtin:sharepoint`` config that is refused. The text names the
    field and the rule, never the value."""


@dataclass(frozen=True)
class TableView:
    name: str
    table: str
    columns: tuple[str, ...]
    kind: str = "table"


@dataclass(frozen=True)
class Lookup:
    view: str
    on: str
    add: tuple[str, ...]


@dataclass(frozen=True)
class Conflict:
    kind: str
    against: str
    when: tuple[str, ...]


@dataclass(frozen=True)
class CalendarView:
    name: str
    sheet: str
    date_row: int
    first_row: int
    first_date_column: str
    labels: dict[str, str] = field(default_factory=dict)
    codes: dict[str, str] = field(default_factory=dict)
    stop_at: str | None = None
    lookup: Lookup | None = None
    conflict: Conflict | None = None
    kind: str = "calendar"


def column_index(letters: str) -> int:
    """``A`` -> 1, ``XFD`` -> 16384."""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def _text(value: Any, where: str, limit: int) -> str:
    """A one-line string of 1 to ``limit`` characters, NFC, trimmed."""
    if not isinstance(value, str):
        raise ViewConfigError(f"{where}: must be a string")
    text = unicodedata.normalize("NFC", value).strip()
    if not text or len(text) > limit:
        raise ViewConfigError(f"{where}: must be 1 to {limit} characters")
    # Category C is every control and format character; the line and paragraph
    # separators (U+2028, U+2029) are category Z and end a line just the same.
    if any(unicodedata.category(ch).startswith("C")
           or unicodedata.category(ch) in ("Zl", "Zp") for ch in text):
        raise ViewConfigError(
            f"{where}: must be one line without control characters"
        )
    return text


def _exact(value: Any, where: str, limit: int) -> str:
    """:func:`_text` for a value the caller stores as it was given: refused
    unless it already is the checked form. ``check_pins`` returns nothing, so
    a pin that was only valid after trimming or composing would be stored and
    sent in the form that was never checked."""
    text = _text(value, where, limit)
    if text != value:
        raise ViewConfigError(
            f"{where}: must have no leading or trailing whitespace and use "
            "composed (NFC) characters"
        )
    return text


def _texts(value: Any, where: str, limit: int, each: int = 255) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > limit:
        raise ViewConfigError(f"{where}: must be a list of 1 to {limit} strings")
    out = tuple(_text(v, where, each) for v in value)
    if len(set(out)) != len(out):
        raise ViewConfigError(f"{where}: must not repeat a value")
    return out


def _column(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _COLUMN_RE.fullmatch(value) \
            or column_index(value) > MAX_COLUMN:
        raise ViewConfigError(f"{where}: must be a column letter from A to XFD")
    return value


def _row(value: Any, where: str) -> int:
    # `type(...) is int`: True is an int for isinstance, and is not a row.
    if type(value) is not int or not 1 <= value <= MAX_ROW:
        raise ViewConfigError(f"{where}: must be a row number from 1 to {MAX_ROW}")
    return value


def _only(raw: dict[str, Any], allowed: tuple[str, ...], where: str) -> None:
    if any(k not in allowed for k in raw):
        raise ViewConfigError(f"{where}: unknown key (allowed: {', '.join(allowed)})")


def _table(name: str, raw: dict[str, Any]) -> TableView:
    where = f"oauth.views.{name}"
    _only(raw, ("kind", "table", "columns"), where)
    table = raw.get("table")
    if not isinstance(table, str) or not _TABLE_RE.fullmatch(table):
        raise ViewConfigError(f"{where}.table: must be the name of an Excel table")
    return TableView(name, table, _texts(raw.get("columns"), f"{where}.columns", MAX_COLUMNS))


def _calendar(name: str, raw: dict[str, Any]) -> CalendarView:
    where = f"oauth.views.{name}"
    _only(raw, ("kind", "sheet", "date_row", "first_row", "first_date_column",
                "labels", "stop_at", "codes", "lookup", "conflict"), where)
    sheet = _text(raw.get("sheet"), f"{where}.sheet", 64)
    plain = sheet.replace(_YEAR, "", 1)
    if "{" in plain or "}" in plain or _SHEET_FORBIDDEN & set(plain) \
            or len(sheet.replace(_YEAR, "0000")) > 31:
        raise ViewConfigError(
            f"{where}.sheet: must be a sheet name of at most 31 characters, "
            "with {year} at most once"
        )
    date_row = _row(raw.get("date_row"), f"{where}.date_row")
    first_row = _row(raw.get("first_row"), f"{where}.first_row")
    if first_row <= date_row:
        raise ViewConfigError(f"{where}.first_row: must be below date_row")
    first_col = _column(raw.get("first_date_column"), f"{where}.first_date_column")

    labels_raw = raw.get("labels")
    if not isinstance(labels_raw, dict) or "member" not in labels_raw:
        raise ViewConfigError(f"{where}.labels: must be an object with a member column")
    _only(labels_raw, LABEL_KEYS, f"{where}.labels")
    labels = {k: _column(labels_raw[k], f"{where}.labels.{k}")
              for k in LABEL_KEYS if k in labels_raw}
    if len(set(labels.values())) != len(labels):
        raise ViewConfigError(f"{where}.labels: two labels must not share a column")
    if any(column_index(c) >= column_index(first_col) for c in labels.values()):
        raise ViewConfigError(f"{where}.labels: must be left of first_date_column")

    codes_raw = raw.get("codes")
    if not isinstance(codes_raw, dict) or not codes_raw or len(codes_raw) > MAX_CODES:
        raise ViewConfigError(f"{where}.codes: must be an object of 1 to {MAX_CODES} codes")
    codes: dict[str, str] = {}
    for code, status in codes_raw.items():
        key = _text(code, f"{where}.codes", 16)
        if not isinstance(status, str) or not STATUS_RE.fullmatch(status) \
                or status in RESERVED_STATUSES:
            raise ViewConfigError(
                f"{where}.codes: a status must be 1 to 32 letters, digits or '_', "
                "starting with a letter, and not 'unmapped'"
            )
        if key in codes:
            raise ViewConfigError(f"{where}.codes: must not repeat a code")
        codes[key] = status

    stop_at = None
    if raw.get("stop_at") is not None:
        stop_at = _text(raw["stop_at"], f"{where}.stop_at", 64)

    lookup = None
    if raw.get("lookup") is not None:
        lk = raw["lookup"]
        if not isinstance(lk, dict):
            raise ViewConfigError(f"{where}.lookup: must be an object")
        _only(lk, ("view", "on", "add"), f"{where}.lookup")
        target = lk.get("view")
        if not isinstance(target, str) or not VIEW_NAME_RE.fullmatch(target):
            raise ViewConfigError(f"{where}.lookup.view: must name a table view")
        add = _texts(lk.get("add"), f"{where}.lookup.add", 10)
        if any(column.casefold() in RUN_KEYS for column in add):
            raise ViewConfigError(
                f"{where}.lookup.add: must not add a column named like a run field "
                f"({', '.join(RUN_KEYS)})"
            )
        lookup = Lookup(target, _text(lk.get("on"), f"{where}.lookup.on", 255), add)

    conflict = None
    if raw.get("conflict") is not None:
        cf = raw["conflict"]
        if not isinstance(cf, dict):
            raise ViewConfigError(f"{where}.conflict: must be an object")
        _only(cf, ("kind", "against", "when"), f"{where}.conflict")
        if "kind" not in labels:
            raise ViewConfigError(f"{where}.conflict: needs labels.kind")
        when = _texts(cf.get("when"), f"{where}.conflict.when", 20, each=32)
        if any(w not in codes.values() for w in when):
            raise ViewConfigError(f"{where}.conflict.when: must list statuses of codes")
        conflict = Conflict(_text(cf.get("kind"), f"{where}.conflict.kind", 64),
                            _text(cf.get("against"), f"{where}.conflict.against", 64), when)
        if conflict.kind.casefold() == conflict.against.casefold():
            raise ViewConfigError(f"{where}.conflict: kind and against must differ")

    return CalendarView(name, sheet, date_row, first_row, first_col, labels, codes,
                        stop_at, lookup, conflict)


def parse_views(raw: Any) -> dict[str, TableView | CalendarView]:
    """The views of an entry, validated. Raises :class:`ViewConfigError`."""
    if not isinstance(raw, dict) or not raw or len(raw) > MAX_VIEWS:
        raise ViewConfigError(f"oauth.views: must be an object of 1 to {MAX_VIEWS} views")
    views: dict[str, TableView | CalendarView] = {}
    for name, body in raw.items():
        if not isinstance(name, str) or not VIEW_NAME_RE.fullmatch(name):
            raise ViewConfigError(
                "oauth.views: a view name must be 1 to 32 lower-case letters, digits "
                "or '_', starting with a letter"
            )
        if not isinstance(body, dict):
            raise ViewConfigError(f"oauth.views.{name}: must be an object")
        kind = body.get("kind")
        if kind == "table":
            views[name] = _table(name, body)
        elif kind == "calendar":
            views[name] = _calendar(name, body)
        else:
            raise ViewConfigError(f"oauth.views.{name}.kind: must be table or calendar")
    for view in views.values():
        if isinstance(view, CalendarView) and view.lookup is not None:
            where = f"oauth.views.{view.name}.lookup"
            target = views.get(view.lookup.view)
            if not isinstance(target, TableView):
                raise ViewConfigError(f"{where}.view: must name a table view")
            if view.lookup.on not in target.columns \
                    or any(c not in target.columns for c in view.lookup.add):
                raise ViewConfigError(
                    f"{where}: on and add must be columns of that table view"
                )
    return views


def views_config(views: dict[str, TableView | CalendarView]) -> dict[str, Any]:
    """The JSON form of validated views: what is stored and exported."""
    out: dict[str, Any] = {}
    for name, view in views.items():
        if isinstance(view, TableView):
            out[name] = {"kind": "table", "table": view.table, "columns": list(view.columns)}
            continue
        body: dict[str, Any] = {
            "kind": "calendar", "sheet": view.sheet, "date_row": view.date_row,
            "first_row": view.first_row, "first_date_column": view.first_date_column,
            "labels": dict(view.labels), "codes": dict(view.codes),
        }
        if view.stop_at is not None:
            body["stop_at"] = view.stop_at
        if view.lookup is not None:
            body["lookup"] = {"view": view.lookup.view, "on": view.lookup.on,
                              "add": list(view.lookup.add)}
        if view.conflict is not None:
            body["conflict"] = {"kind": view.conflict.kind, "against": view.conflict.against,
                                "when": list(view.conflict.when)}
        out[name] = body
    return out


def clean_views(raw: Any) -> dict[str, Any]:
    """``raw`` validated and in its stored form."""
    return views_config(parse_views(raw))


def site_host(site: str) -> str:
    """The SharePoint host of a checked ``site`` pin."""
    return site.split(":", 1)[0]


def check_pins(cfg: dict[str, Any]) -> None:
    """``site``, ``library`` and ``path`` of an entry. Raises
    :class:`ViewConfigError`.

    ``site`` is ``<tenant>.sharepoint.com:/sites/<site>``: the host is also
    the only one a download may come from. ``path`` is a file below the
    library root; the characters refused in it are those a rebuilt URL would
    read as something else (``agents.destination_auth`` rebuilds the URL from
    the decoded path).

    Nothing is returned, so the caller stores the values as they were given:
    a pin is accepted only in exactly the form that was checked (no edge
    whitespace, one line, NFC), never after a silent repair.
    """
    if not isinstance(cfg, dict):
        raise ViewConfigError("oauth: must be an object")
    site = cfg.get("site")
    match = _SITE_RE.fullmatch(site) if isinstance(site, str) else None
    if match is None or any(set(part) == {"."} for part in match.group(2).split("/") if part):
        raise ViewConfigError(
            "oauth.site: must be <tenant>.sharepoint.com:/sites/<site> "
            "(lower-case host, the site's server-relative path)"
        )
    library = _exact(cfg.get("library"), "oauth.library", 128)
    if "/" in library or "\\" in library:
        raise ViewConfigError("oauth.library: must be the name of a document library")
    path = _exact(cfg.get("path"), "oauth.path", MAX_PATH_CHARS)
    parts = path.split("/")
    if _PATH_FORBIDDEN & set(path) or any(p.strip() != p or p in ("", ".", "..") for p in parts) \
            or not path.lower().endswith(".xlsx"):
        raise ViewConfigError(
            "oauth.path: must be the path of an .xlsx file below the library root, "
            "without \\ % ? # : * \" < > |"
        )
