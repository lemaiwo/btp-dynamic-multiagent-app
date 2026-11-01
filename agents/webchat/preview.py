"""The viewer's preview of a chat file: ``GET /chat/api/files/{fid}/preview``.

Pure functions from a stored file (its media type, name and bytes) to the
JSON the viewer renders, typed by ``kind`` (design addendum (f), decision 1):

* ``.xlsx`` -> ``sheets``: cached values only, through the guarded archive
  the upload extraction uses (``check_office_archive``, then the SharePoint
  reader's ``check_archive`` / ``open_workbook``, which is openpyxl read-only
  with ``data_only=True`` on an archive that inflates no member beyond its
  declared size). At most ``MAX_SHEETS`` sheets of ``MAX_ROWS`` x
  ``MAX_COLUMNS`` cells; ``truncated`` and ``row_count`` say what was left
  out. ``columns`` are the spreadsheet column letters of what is shown: the
  first row is data like any other (a sheet need not have a header row).
* ``.docx`` -> ``document``: headings, paragraphs, lists and tables, parsed
  with defusedxml after ``check_office_archive``; caps ``MAX_BLOCKS``,
  ``MAX_DOC_CHARS`` and ``MAX_TABLE_ROWS`` x ``MAX_TABLE_COLUMNS``.
* text, markdown, CSV, JSON -> ``text``: UTF-8, cut at ``MAX_TEXT_CHARS``.
* PNG, JPEG, PDF -> ``inline``: the existing inline download renders them.
* a dashboard (``DASHBOARD_MEDIA_TYPE``, design addendum (i)) ->
  ``dashboard``: the stored spec, checked again by ``parse_dashboard``. It
  is never ``inline``: the front end draws it from this JSON.

Anything else is :class:`PreviewError` 415, bytes that do not read as their
type 422; both texts are fixed and name neither the file nor a value. The
parse is CPU work on bytes somebody else may have written: the route runs
:func:`build_preview` in a worker thread. Nothing of a preview is stored.
"""

from __future__ import annotations

import io
import logging
import math
import re
import zipfile
import zlib
from datetime import date, datetime, time, timedelta
from typing import Any

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from openpyxl.utils import get_column_letter

from agents.multipart_form import FormRefused
from agents.webchat import uploads
from agents.webchat.dashboard import DASHBOARD_MEDIA_TYPE, parse_dashboard
from agents.webchat.uploads import (
    CSV,
    DOCX,
    JPEG,
    JSON,
    MARKDOWN,
    PDF,
    PNG,
    TEXT,
    XLSX,
    _paragraph_text,
    check_office_archive,
)

logger = logging.getLogger(__name__)

MSG_NO_PREVIEW = "No preview for this file type"
MSG_UNREADABLE = "The file could not be read"

MAX_SHEETS = 10
MAX_ROWS = 200
MAX_COLUMNS = 50
MAX_BLOCKS = 2_000
MAX_DOC_CHARS = 200_000
MAX_TABLE_ROWS = 100
MAX_TABLE_COLUMNS = 20
MAX_TEXT_CHARS = 200_000

INLINE_TYPES = frozenset({PNG, JPEG, PDF})
TEXT_LANGUAGES = {TEXT: "text", MARKDOWN: "markdown", CSV: "csv", JSON: "json"}
ORDERED_FORMATS = frozenset({"decimal", "lowerLetter", "upperLetter", "lowerRoman",
                             "upperRoman"})

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_HEADING = re.compile(r"^heading ?([1-6])$", re.IGNORECASE)


class PreviewError(Exception):
    """A refused preview: ``status`` 415 or 422 and one of the fixed texts."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _unreadable() -> PreviewError:
    return PreviewError(422, MSG_UNREADABLE)


# ---------------------------------------------------------------------------
# Workbook
# ---------------------------------------------------------------------------
def _cell(value: Any) -> Any:
    """A cached cell value as JSON: dates as ISO text, a ``=`` string stays a
    string (a formula is never read, only what the saving application
    stored), anything else unknown as its text."""
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        if value.time() == time() and value.tzinfo is None:
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    return str(value)


def _sheet(ws: Any) -> dict[str, Any]:
    shown: list[list[Any]] = []
    row_count = 0
    wider = False
    width = 0
    # One column more than is shown: a value there says columns were left out.
    for index, row in enumerate(ws.iter_rows(max_col=MAX_COLUMNS + 1, values_only=True)):
        cells = [_cell(v) for v in row]
        if len(cells) > MAX_COLUMNS:
            if any(v is not None and v != "" for v in cells[MAX_COLUMNS:]):
                wider = True
            cells = cells[:MAX_COLUMNS]
        used = max((i + 1 for i, v in enumerate(cells) if v is not None and v != ""),
                   default=0)
        if used:
            row_count = index + 1
        if index < MAX_ROWS:
            width = max(width, used)
            shown.append(cells)
    declared = getattr(ws, "max_column", None)
    if isinstance(declared, int) and declared > MAX_COLUMNS:
        wider = True
    shown = shown[:min(row_count, MAX_ROWS)]
    rows = [(r + [None] * width)[:width] for r in shown]
    return {
        "name": str(ws.title),
        "columns": [get_column_letter(i + 1) for i in range(width)],
        "rows": rows,
        "row_count": row_count,
        "truncated": row_count > MAX_ROWS or wider,
    }


def preview_xlsx(data: bytes, name: str) -> dict[str, Any]:
    """The first sheets of a workbook (CPU work: call it in a thread)."""
    from agents.sharepoint_workbook import Refused, check_archive, open_workbook

    try:
        check_office_archive(data)
        check_archive(data)
        book = open_workbook(data)
    except (FormRefused, Refused):
        raise _unreadable() from None
    try:
        worksheets = list(book.worksheets)
        sheets = [_sheet(ws) for ws in worksheets[:MAX_SHEETS]]
    except PreviewError:
        raise
    except Exception as exc:  # noqa: BLE001 - one code for whatever the parser says
        logger.info("webchat: workbook preview failed (%s)", type(exc).__name__)
        raise _unreadable() from None
    finally:
        try:
            book.close()
        except Exception:  # noqa: BLE001
            pass
    return {
        "kind": "sheets",
        "name": name,
        "sheets": sheets,
        "truncated": len(worksheets) > MAX_SHEETS or any(s["truncated"] for s in sheets),
    }


# ---------------------------------------------------------------------------
# Word document
# ---------------------------------------------------------------------------
def _read_part(zf: zipfile.ZipFile, part: str) -> bytes | None:
    """A part read under the upload path's cap; ``None`` when it is absent.
    A member that inflates beyond the cap is refused, not read on."""
    try:
        info = zf.getinfo(part)
    except KeyError:
        return None
    if info.file_size > uploads.MAX_PART_BYTES:
        raise _unreadable()
    with zf.open(info) as stream:
        xml = stream.read(uploads.MAX_PART_BYTES + 1)
    if len(xml) > uploads.MAX_PART_BYTES:
        raise _unreadable()
    return xml


def _docx_roots(data: bytes) -> tuple[Any, Any | None]:
    try:
        check_office_archive(data)
    except FormRefused:
        raise _unreadable() from None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            document = _read_part(zf, "word/document.xml")
            numbering = _read_part(zf, "word/numbering.xml")
        if document is None:
            raise _unreadable()
        root = SafeET.fromstring(document, forbid_dtd=True)
        numbering_root = (SafeET.fromstring(numbering, forbid_dtd=True)
                          if numbering is not None else None)
    except PreviewError:
        raise
    except (zipfile.BadZipFile, ValueError, OSError, SafeET.ParseError,
            DefusedXmlException, zlib.error, EOFError):
        raise _unreadable() from None
    return root, numbering_root


def _ordered_levels(numbering: Any | None) -> dict[tuple[str, str], bool]:
    """``(numId, ilvl) -> ordered`` from ``word/numbering.xml``: a level is
    ordered only for a known counting format; anything else is a bullet."""
    if numbering is None:
        return {}
    abstract: dict[str, dict[str, bool]] = {}
    for node in numbering.findall(f"{_W}abstractNum"):
        levels: dict[str, bool] = {}
        for lvl in node.findall(f"{_W}lvl"):
            fmt = lvl.find(f"{_W}numFmt")
            levels[lvl.get(f"{_W}ilvl", "0")] = (
                fmt is not None and fmt.get(f"{_W}val", "") in ORDERED_FORMATS)
        abstract[node.get(f"{_W}abstractNumId", "")] = levels
    out: dict[tuple[str, str], bool] = {}
    for num in numbering.findall(f"{_W}num"):
        num_id = num.get(f"{_W}numId", "")
        ref = num.find(f"{_W}abstractNumId")
        levels = dict(abstract.get(ref.get(f"{_W}val", "") if ref is not None else "", {}))
        for override in num.findall(f"{_W}lvlOverride"):
            lvl = override.find(f"{_W}lvl")
            fmt = lvl.find(f"{_W}numFmt") if lvl is not None else None
            if fmt is not None:
                levels[override.get(f"{_W}ilvl", "0")] = (
                    fmt.get(f"{_W}val", "") in ORDERED_FORMATS)
        for ilvl, ordered in levels.items():
            out[(num_id, ilvl)] = ordered
    return out


def _heading_level(p: Any) -> int | None:
    style = p.find(f"{_W}pPr/{_W}pStyle")
    value = style.get(f"{_W}val", "").strip() if style is not None else ""
    if value.lower() == "title":
        return 1
    match = _HEADING.match(value)
    return int(match.group(1)) if match else None


def _numbering(p: Any) -> tuple[str, str] | None:
    num = p.find(f"{_W}pPr/{_W}numPr")
    if num is None:
        return None
    num_id = num.find(f"{_W}numId")
    ilvl = num.find(f"{_W}ilvl")
    value = num_id.get(f"{_W}val", "") if num_id is not None else ""
    if not value or value == "0":  # numId 0 switches numbering off
        return None
    return value, (ilvl.get(f"{_W}val", "0") if ilvl is not None else "0")


def _cell_text(tc: Any) -> str:
    # Every paragraph of the cell, those of a nested table included: a nested
    # table is flattened into the cell's text.
    parts = (_paragraph_text(p).strip() for p in tc.iter(f"{_W}p"))
    return "\n".join(t for t in parts if t)


def _body_children(parent: Any) -> Any:
    """Paragraphs and tables of the body, through content controls."""
    for child in parent:
        if child.tag == f"{_W}sdt":
            content = child.find(f"{_W}sdtContent")
            if content is not None:
                yield from _body_children(content)
        elif child.tag in (f"{_W}p", f"{_W}tbl"):
            yield child


class _Document:
    """The blocks under the caps: ``add`` answers False once a cap is hit."""

    def __init__(self, levels: dict[tuple[str, str], bool]) -> None:
        self.levels = levels
        self.blocks: list[dict[str, Any]] = []
        self.chars = 0
        self.truncated = False
        self.open_list: dict[str, Any] | None = None

    def take(self, text: str) -> str | None:
        """``text`` within the character budget; ``None`` when nothing fits."""
        left = MAX_DOC_CHARS - self.chars
        if len(text) > left:
            self.truncated = True
            text = text[:left]
        if not text:
            return None
        self.chars += len(text)
        return text

    def full(self) -> bool:
        if self.chars >= MAX_DOC_CHARS or len(self.blocks) >= MAX_BLOCKS:
            self.truncated = True
            return True
        return False

    def add(self, block: dict[str, Any]) -> None:
        self.blocks.append(block)

    def paragraph(self, p: Any) -> None:
        text = _paragraph_text(p).strip()
        if not text:
            return
        numbering = _numbering(p)
        if numbering is not None:
            ordered = self.levels.get(numbering, False)
            if self.open_list is None or self.open_list["ordered"] != ordered:
                if self.full():
                    return
                self.open_list = {"type": "list", "ordered": ordered, "items": []}
                self.add(self.open_list)
            kept = self.take(text)
            if kept is not None:
                self.open_list["items"].append(kept)
            return
        self.open_list = None
        if self.full():
            return
        kept = self.take(text)
        if kept is None:
            return
        level = _heading_level(p)
        if level is not None:
            self.add({"type": "heading", "level": level, "text": kept})
        else:
            self.add({"type": "paragraph", "text": kept})

    def table(self, tbl: Any) -> None:
        self.open_list = None
        if self.full():
            return
        rows: list[list[str]] = []
        for index, tr in enumerate(tbl.findall(f"{_W}tr")):
            if index >= MAX_TABLE_ROWS:
                self.truncated = True
                break
            cells: list[str] = []
            for col, tc in enumerate(tr.findall(f"{_W}tc")):
                if col >= MAX_TABLE_COLUMNS:
                    self.truncated = True
                    break
                cells.append(self.take(_cell_text(tc)) or "")
            rows.append(cells)
            if self.chars >= MAX_DOC_CHARS:
                break
        if rows:
            self.add({"type": "table", "rows": rows})


def preview_docx(data: bytes, name: str) -> dict[str, Any]:
    """The blocks of a ``.docx`` (CPU work: call it in a thread)."""
    root, numbering = _docx_roots(data)
    doc = _Document(_ordered_levels(numbering))
    body = root.find(f"{_W}body")
    for child in _body_children(body) if body is not None else ():
        if child.tag == f"{_W}tbl":
            doc.table(child)
        else:
            doc.paragraph(child)
        if doc.truncated and doc.full():
            break
    return {"kind": "document", "name": name, "blocks": doc.blocks,
            "truncated": doc.truncated}


# ---------------------------------------------------------------------------
# Text and dispatch
# ---------------------------------------------------------------------------
def preview_text(data: bytes, name: str, media_type: str) -> dict[str, Any]:
    """A UTF-8 text file, cut at ``MAX_TEXT_CHARS``."""
    language = TEXT_LANGUAGES.get(media_type)
    if language is None:
        raise PreviewError(415, MSG_NO_PREVIEW)
    try:
        text = uploads.decode_text(data)
    except FormRefused:
        raise _unreadable() from None
    truncated = len(text) > MAX_TEXT_CHARS
    return {"kind": "text", "name": name, "language": language,
            "text": text[:MAX_TEXT_CHARS], "truncated": truncated}


def build_preview(media_type: str, name: str, data: bytes, fid: str) -> dict[str, Any]:
    """The preview of a stored file by its stored media type (never by the
    name's extension); :class:`PreviewError` 415 / 422 otherwise."""
    if media_type in INLINE_TYPES:
        return {"kind": "inline", "name": name, "media_type": media_type,
                "url": f"/chat/api/files/{fid}?inline=1"}
    if (media_type not in (XLSX, DOCX, DASHBOARD_MEDIA_TYPE)
            and media_type not in TEXT_LANGUAGES):
        raise PreviewError(415, MSG_NO_PREVIEW)
    try:
        if media_type == DASHBOARD_MEDIA_TYPE:
            return {"kind": "dashboard", "name": name, "dashboard": parse_dashboard(data)}
        if media_type == XLSX:
            return preview_xlsx(data, name)
        if media_type == DOCX:
            return preview_docx(data, name)
        return preview_text(data, name, media_type)
    except PreviewError:
        raise
    except Exception as exc:  # noqa: BLE001 - one code for whatever the parser says
        logger.info("webchat: preview failed (%s)", type(exc).__name__)
        raise _unreadable() from None
