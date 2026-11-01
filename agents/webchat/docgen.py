"""The document tools of a web chat run: Word, PDF, Excel and dashboards.

Four tools, attached per run with ``toolsets=`` on the orchestrator
(``agents/webchat/run.py``), never in the registry build: only a web chat
run has a conversation to put a file in.

* ``create_word_document(filename, markdown, title?)`` -- a ``.docx``.
  Written here as plain WordprocessingML with the standard library
  (``zipfile`` and escaped XML strings). ``python-docx`` is not used: it
  depends on ``lxml``, and with ``lxml`` installed openpyxl parses the
  workbooks ``builtin:sharepoint`` downloads with lxml instead of the
  defused parser (``tests/test_sharepoint_requirements.py``).
* ``create_pdf_document(filename, markdown, title?)`` -- a PDF through
  reportlab's platypus, A4, the built-in Helvetica / Courier fonts. Those
  are Latin-1: any other character becomes ``?`` (a documented limit until
  a TTF font is vendored).
* ``create_excel_workbook(filename, sheets)`` -- an ``.xlsx`` through
  openpyxl (write-only). A text cell that starts with ``=`` is stored as
  text, never as a formula, and nothing is calculated.
* ``create_dashboard(filename, title, widgets, description?)`` -- charts,
  KPI tiles and tables for the viewer (design addendum (i)). The stored
  file is the checked spec as JSON (``agents/webchat/dashboard.py``), media
  type ``DASHBOARD_MEDIA_TYPE``, named ``<filename>.dashboard.json``; the
  front end draws it. Its answer adds ``title`` and ``widgets`` (the count).

The markdown is the subset of ``agents/webchat/md.py``. Where a file goes
is never the model's choice: the tools write through the
:class:`ChatRunContext` the run binds (``current_chat_run``), which knows
the conversation, the assistant message and the owner. The model chooses a
file name only, and that is cleaned.

Every tool answers a dict: the stored file (``file_id``, ``name``,
``media_type``, ``size``, ``download_url``) or ``{"error": {code,
message}}`` with a fixed text (``too_large``, ``too_many_files``,
``invalid_argument``, ``invalid_name``). Nothing is raised to the model
except pydantic-ai's own argument validation.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import re
import unicodedata
import zipfile
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

from pydantic import BaseModel, Field
from pydantic_ai.toolsets import FunctionToolset

from agents.webchat import md
from agents.webchat.dashboard import (
    DASHBOARD_EXTENSION,
    DASHBOARD_MEDIA_TYPE,
    DashboardError,
    build_dashboard,
    validate_spec,
)

logger = logging.getLogger(__name__)

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF = "application/pdf"

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILES_PER_RUN = 10
MAX_MARKDOWN_CHARS = md.MAX_MARKDOWN_CHARS
MAX_TITLE_CHARS = 200
MAX_SHEETS = 50
MAX_ROWS = 10_000
MAX_COLUMNS = 100
# Not in the design's limits: 50 x 10,000 x 100 cells would be 50 million
# Python objects. No model writes that much into one call; this keeps a
# call that tries from taking the worker's memory.
MAX_CELLS = 500_000
MAX_CELL_CHARS = 32_767  # Excel's own limit for a cell
MAX_NAME_CHARS = 100
MAX_COLUMN_WIDTH = 60

TOOL_NAMES = ("create_word_document", "create_pdf_document", "create_excel_workbook",
              "create_dashboard")

# Characters XML 1.0 cannot hold (and so neither docx, xlsx nor reportlab's
# paragraph markup): C0 controls other than tab / LF / CR, lone surrogates,
# U+FFFE / U+FFFF.
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_NAME_FORBIDDEN = re.compile(r'[<>:"/\\|?*]')


# ---------------------------------------------------------------------------
# The run's context
# ---------------------------------------------------------------------------
SaveFile = Callable[[str, str, bytes], Awaitable[dict[str, Any]]]


@dataclass
class ChatRunContext:
    """Where the files of one web chat run go.

    Bound by the run (``current_chat_run``) in the run's own task;
    ``save`` stores a file for the run's conversation and assistant message
    and answers its metadata. ``created`` counts the files of this run.
    """

    conversation_id: str
    message_id: str
    principal: str
    save: SaveFile
    created: int = 0
    files: list[dict[str, Any]] = field(default_factory=list)


current_chat_run: ContextVar[ChatRunContext | None] = ContextVar(
    "current_chat_run", default=None
)


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


ERR_NO_CONTEXT = _error("invalid_argument", "Documents can only be created in the web chat.")
ERR_TOO_MANY = _error(
    "too_many_files", f"At most {MAX_FILES_PER_RUN} files can be created per answer.")
ERR_TOO_LARGE = _error(
    "too_large", f"The file would be larger than {MAX_FILE_BYTES // (1024 * 1024)} MiB.")
ERR_MARKDOWN_TOO_LARGE = _error(
    "too_large", f"The markdown is longer than {MAX_MARKDOWN_CHARS} characters.")
ERR_NAME = _error("invalid_name", "The file name must be a non-empty text.")
ERR_FAILED = _error("invalid_argument", "The document could not be created from these arguments.")


# ---------------------------------------------------------------------------
# Names and text
# ---------------------------------------------------------------------------
def clean_filename(raw: Any, extension: str) -> str | None:
    """A file name without path, control or reserved characters, ending in
    ``extension`` (``.docx``); ``None`` when nothing usable is left."""
    if not isinstance(raw, str):
        return None
    name = unicodedata.normalize("NFC", raw)
    name = "".join(
        ch for ch in name
        if not unicodedata.category(ch).startswith("C")
        and unicodedata.category(ch) not in ("Zl", "Zp")
    )
    # Only the last path segment counts: a/b/c.docx is c.docx.
    name = re.split(r"[/\\]", name)[-1]
    name = _NAME_FORBIDDEN.sub("_", name).strip().strip(".").strip()
    stem, dot, ext = name.rpartition(".")
    if dot and ext.lower() in ("docx", "doc", "pdf", "xlsx", "xls", "csv", "txt", "md"):
        name = stem.strip().strip(".").strip()
    name = name[: MAX_NAME_CHARS - len(extension)].strip()
    if not name:
        return None
    return name + extension


def xml_text(text: str) -> str:
    """``text`` without characters XML cannot hold."""
    return _XML_ILLEGAL.sub("", text)


def latin1(text: str) -> str:
    """``text`` in what the built-in PDF fonts can draw; anything else is ``?``."""
    return xml_text(text).encode("latin-1", "replace").decode("latin-1")


# ---------------------------------------------------------------------------
# Word
# ---------------------------------------------------------------------------
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_CODE_FONT = "Consolas"

_XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_OOXML = "application/vnd.openxmlformats-"
_PKG = "http://schemas.openxmlformats.org/package/2006"
_OFFICE_RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _override(part: str, content_type: str) -> str:
    return f'<Override PartName="{part}" ContentType="{_OOXML}{content_type}"/>'


def _relationship(rid: str, kind: str, target: str) -> str:
    return f'<Relationship Id="{rid}" Type="{kind}" Target="{target}"/>'


_CONTENT_TYPES = (
    _XML_HEAD
    + f'<Types xmlns="{_PKG}/content-types">'
    + f'<Default Extension="rels" ContentType="{_OOXML}package.relationships+xml"/>'
    + '<Default Extension="xml" ContentType="application/xml"/>'
    + _override("/word/document.xml",
                "officedocument.wordprocessingml.document.main+xml")
    + _override("/word/styles.xml", "officedocument.wordprocessingml.styles+xml")
    + _override("/docProps/core.xml", "package.core-properties+xml")
    + "</Types>"
)
_ROOT_RELS = (
    _XML_HEAD
    + f'<Relationships xmlns="{_PKG}/relationships">'
    + _relationship("rId1", f"{_OFFICE_RELS}/officeDocument", "word/document.xml")
    + _relationship("rId2", f"{_PKG}/relationships/metadata/core-properties",
                    "docProps/core.xml")
    + "</Relationships>"
)
_DOC_RELS = (
    _XML_HEAD
    + f'<Relationships xmlns="{_PKG}/relationships">'
    + _relationship("rId1", f"{_OFFICE_RELS}/styles", "styles.xml")
    + "</Relationships>"
)


def _style(style_id: str, name: str, size: int, *, bold: bool = False,
           outline: int | None = None, font: str | None = None,
           before: int = 0, after: int = 120) -> str:
    keep = "<w:keepNext/>" if outline is not None else ""
    ppr = f'<w:pPr>{keep}<w:spacing w:before="{before}" w:after="{after}"/>'
    if outline is not None:
        ppr += f'<w:outlineLvl w:val="{outline}"/>'
    ppr += "</w:pPr>"
    rpr = "<w:rPr>"
    if font:
        rpr += f'<w:rFonts w:ascii="{font}" w:hAnsi="{font}" w:cs="{font}"/>'
    if bold:
        rpr += "<w:b/>"
    rpr += f'<w:sz w:val="{size}"/></w:rPr>'
    return (f'<w:style w:type="paragraph" w:styleId="{style_id}"><w:name w:val="{name}"/>'
            f'<w:basedOn w:val="Normal"/><w:qFormat/>{ppr}{rpr}</w:style>')


_STYLES = (
    _XML_HEAD
    + f'<w:styles xmlns:w="{_W_NS}">'
    '<w:docDefaults><w:rPrDefault><w:rPr>'
    '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
    '<w:sz w:val="22"/></w:rPr></w:rPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
    '<w:name w:val="Normal"/><w:qFormat/>'
    '<w:pPr><w:spacing w:after="120"/></w:pPr></w:style>'
    + _style("Title", "Title", 44, bold=True, after=240)
    + _style("Heading1", "heading 1", 32, bold=True, outline=0, before=240)
    + _style("Heading2", "heading 2", 28, bold=True, outline=1, before=200)
    + _style("Heading3", "heading 3", 24, bold=True, outline=2, before=160)
    + _style("Heading4", "heading 4", 22, bold=True, outline=3, before=120)
    + _style("Code", "Code", 19, font=_CODE_FONT, after=0)
    + "</w:styles>"
)


def _run(span: md.Span, *, bold: bool = False) -> str:
    rpr = ""
    if span.code:
        rpr += f'<w:rFonts w:ascii="{_CODE_FONT}" w:hAnsi="{_CODE_FONT}" w:cs="{_CODE_FONT}"/>'
    if span.bold or bold:
        rpr += "<w:b/>"
    if span.italic:
        rpr += "<w:i/>"
    rpr = f"<w:rPr>{rpr}</w:rPr>" if rpr else ""
    text = _xml_escape(xml_text(span.text))
    return f'<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r>'


def _para(spans: list[md.Span], *, style: str | None = None, indent: str = "",
          bold: bool = False, prefix: str = "") -> str:
    ppr = ""
    if style:
        ppr += f'<w:pStyle w:val="{style}"/>'
    ppr += indent
    ppr = f"<w:pPr>{ppr}</w:pPr>" if ppr else ""
    runs = _run(md.Span(prefix)) if prefix else ""
    runs += "".join(_run(span, bold=bold) for span in spans)
    return f"<w:p>{ppr}{runs}</w:p>"


_BORDER = '<w:{0} w:val="single" w:sz="4" w:space="0" w:color="999999"/>'
_TABLE_BORDERS = "<w:tblBorders>" + "".join(
    _BORDER.format(side) for side in ("top", "left", "bottom", "right", "insideH", "insideV")
) + "</w:tblBorders>"


def _table(table: md.Table) -> str:
    width = max(1, len(table.header))
    grid = "".join('<w:gridCol w:w="{}"/>'.format(9000 // width) for _ in range(width))
    out = [f'<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/>{_TABLE_BORDERS}</w:tblPr>'
           f"<w:tblGrid>{grid}</w:tblGrid>"]
    for index, row in enumerate([table.header, *table.rows]):
        out.append("<w:tr>")
        for cell in row:
            out.append(f"<w:tc>{_para(cell, bold=index == 0)}</w:tc>")
        out.append("</w:tr>")
    out.append("</w:tbl><w:p/>")
    return "".join(out)


def _list(block: md.ListBlock) -> str:
    out: list[str] = []
    counters: dict[int, int] = {}
    for item in block.items:
        for deeper in [lvl for lvl in counters if lvl > item.level]:
            del counters[deeper]
        if block.ordered:
            counters[item.level] = counters.get(
                item.level, (block.start if item.level == 0 else 1) - 1) + 1
            prefix = f"{counters[item.level]}.\t"
        else:
            prefix = ("•", "◦", "▪", "–")[item.level % 4] + "\t"
        left = 360 * (item.level + 1)
        indent = (f'<w:tabs><w:tab w:val="left" w:pos="{left}"/></w:tabs>'
                  f'<w:spacing w:after="40"/><w:ind w:left="{left}" w:hanging="360"/>')
        out.append(_para(item.spans, indent=indent, prefix=prefix))
    return "".join(out)


def _body(blocks: list[md.Block], title: str | None) -> str:
    out: list[str] = []
    if title:
        out.append(_para([md.Span(title)], style="Title"))
    for block in blocks:
        if isinstance(block, md.Heading):
            out.append(_para(block.spans, style=f"Heading{block.level}"))
        elif isinstance(block, md.Paragraph):
            out.append(_para(block.spans))
        elif isinstance(block, md.ListBlock):
            out.append(_list(block))
        elif isinstance(block, md.Table):
            out.append(_table(block))
        elif isinstance(block, md.CodeBlock):
            for line in block.text.split("\n") or [""]:
                out.append(_para([md.Span(line.replace("\t", "    "))], style="Code"))
            out.append("<w:p/>")
        elif isinstance(block, md.Rule):
            out.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
    return "".join(out)


def _core(title: str | None) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    title_xml = f"<dc:title>{_xml_escape(xml_text(title))}</dc:title>" if title else ""
    return (
        _XML_HEAD
        + f'<cp:coreProperties xmlns:cp="{_PKG}/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"{title_xml}"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
        "</cp:coreProperties>"
    )


def build_docx(markdown: str, title: str | None = None) -> bytes:
    """A ``.docx`` of the markdown subset (CPU work: call it in a thread)."""
    blocks = md.parse(markdown)
    document = (
        _XML_HEAD
        + f'<w:document xmlns:w="{_W_NS}"><w:body>{_body(blocks, title)}'
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1418" w:right="1418" w:bottom="1418" w:left="1418" '
        'w:header="709" w:footer="709" w:gutter="0"/></w:sectPr>'
        "</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _ROOT_RELS)
        zf.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/styles.xml", _STYLES)
        zf.writestr("docProps/core.xml", _core(title))
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def _markup(spans: list[md.Span], *, bold: bool = False) -> str:
    """Spans as reportlab paragraph markup; the text itself is escaped."""
    out: list[str] = []
    for span in spans:
        text = _xml_escape(latin1(span.text))
        if span.code:
            text = f'<font face="Courier">{text}</font>'
        if span.italic:
            text = f"<i>{text}</i>"
        if span.bold or bold:
            text = f"<b>{text}</b>"
        out.append(text)
    return "".join(out)


def build_pdf(markdown: str, title: str | None = None) -> bytes:
    """An A4 PDF of the markdown subset (CPU work: call it in a thread)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        HRFlowable,
        Paragraph,
        Preformatted,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    sheet = getSampleStyleSheet()
    body = sheet["BodyText"]
    code = ParagraphStyle("Code", parent=sheet["Code"], fontName="Courier", fontSize=8.5,
                          leading=10.5)
    cell = ParagraphStyle("Cell", parent=body, fontSize=9, leading=11)
    headings = {1: sheet["Heading1"], 2: sheet["Heading2"], 3: sheet["Heading3"],
                4: sheet["Heading4"]}

    story: list[Any] = []
    if title:
        story.append(Paragraph(_xml_escape(latin1(title)), sheet["Title"]))
    width = A4[0] - 4 * cm
    for block in md.parse(markdown):
        if isinstance(block, md.Heading):
            story.append(Paragraph(_markup(block.spans), headings[block.level]))
        elif isinstance(block, md.Paragraph):
            story.append(Paragraph(_markup(block.spans), body))
        elif isinstance(block, md.ListBlock):
            counters: dict[int, int] = {}
            for item in block.items:
                for deeper in [lvl for lvl in counters if lvl > item.level]:
                    del counters[deeper]
                if block.ordered:
                    counters[item.level] = counters.get(
                        item.level, (block.start if item.level == 0 else 1) - 1) + 1
                    bullet = f"{counters[item.level]}."
                else:
                    bullet = "•" if item.level % 2 == 0 else "-"
                style = ParagraphStyle(f"List{item.level}", parent=body,
                                       leftIndent=14 * (item.level + 1),
                                       bulletIndent=14 * item.level)
                story.append(Paragraph(_markup(item.spans), style, bulletText=bullet))
        elif isinstance(block, md.Table):
            columns = max(1, len(block.header))
            data = [[Paragraph(_markup(c, bold=True), cell) for c in block.header]]
            data += [[Paragraph(_markup(c), cell) for c in row] for row in block.rows]
            table = Table(data, colWidths=[width / columns] * columns, repeatRows=1)
            table.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef5")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            story.append(table)
            story.append(Spacer(1, 6))
        elif isinstance(block, md.CodeBlock):
            story.append(Preformatted(latin1(block.text.replace("\t", "    ")), code,
                                      maxLineLength=95))
            story.append(Spacer(1, 6))
        elif isinstance(block, md.Rule):
            story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    if not story:
        story.append(Spacer(1, 1))
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm,
                            topMargin=2 * cm, bottomMargin=2 * cm,
                            title=latin1(title) if title else "", author="", creator="")
    doc.build(story)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
Cell = str | int | float | bool | None


class SheetSpec(BaseModel):
    """One worksheet: a header row and the data rows."""

    name: str = Field(description="Worksheet name (at most 31 characters).")
    columns: list[str] = Field(description="Header row: one name per column.")
    rows: list[list[Cell]] = Field(
        default_factory=list,
        description="Data rows; each cell is text, a number, a boolean or null.",
    )


class SheetError(ValueError):
    """A sheet that cannot be written; the message is a fixed text."""


_SHEET_FORBIDDEN = re.compile(r"[\[\]:*?/\\]")


def _sheet_name(raw: str, taken: set[str]) -> str:
    name = _SHEET_FORBIDDEN.sub("_", xml_text(raw)).strip().strip("'")[:31] or "Sheet"
    candidate, n = name, 2
    while candidate.lower() in taken:
        suffix = f" ({n})"
        candidate = name[: 31 - len(suffix)] + suffix
        n += 1
    taken.add(candidate.lower())
    return candidate


def _cell_value(value: Cell) -> Cell:
    if isinstance(value, float) and not math.isfinite(value):
        raise SheetError("A cell holds a number that is not finite.")
    if isinstance(value, str):
        value = xml_text(value)
        if len(value) > MAX_CELL_CHARS:
            raise SheetError(f"A cell holds more than {MAX_CELL_CHARS} characters.")
    return value


def _width(value: Cell) -> int:
    if value is None:
        return 0
    text = str(value)
    return max((len(line) for line in text.split("\n")), default=0)


def build_xlsx(sheets: list[SheetSpec]) -> bytes:
    """An ``.xlsx`` of the sheets (CPU work: call it in a thread).

    ``SheetError`` for a sheet over a cap or a value that cannot be stored.
    """
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    if not sheets:
        raise SheetError("At least one sheet is needed.")
    if len(sheets) > MAX_SHEETS:
        raise SheetError(f"At most {MAX_SHEETS} sheets.")
    cells = 0
    for sheet in sheets:
        if len(sheet.columns) > MAX_COLUMNS or any(len(r) > MAX_COLUMNS for r in sheet.rows):
            raise SheetError(f"At most {MAX_COLUMNS} columns per sheet.")
        if len(sheet.rows) > MAX_ROWS:
            raise SheetError(f"At most {MAX_ROWS} rows per sheet.")
        cells += len(sheet.columns) + sum(len(r) for r in sheet.rows)
    if cells > MAX_CELLS:
        raise SheetError(f"At most {MAX_CELLS} cells per workbook.")

    book = Workbook(write_only=True)
    bold = Font(bold=True)
    taken: set[str] = set()
    for sheet in sheets:
        ws = book.create_sheet(title=_sheet_name(sheet.name, taken))
        header = [_cell_value(c) for c in sheet.columns]
        rows = [[_cell_value(c) for c in row] for row in sheet.rows]
        ncols = max([len(header), *(len(r) for r in rows)] or [0])
        for col in range(ncols):
            widest = max(
                [_width(header[col]) if col < len(header) else 0]
                + [_width(r[col]) for r in rows if col < len(r)]
            )
            ws.column_dimensions[get_column_letter(col + 1)].width = min(
                MAX_COLUMN_WIDTH, max(8, widest + 2))
        if header:
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = f"A1:{get_column_letter(max(1, ncols))}{len(rows) + 1}"
            head_cells = []
            for value in header:
                cell = WriteOnlyCell(ws, value=value)
                cell.data_type = "s"  # a header is a name, never a formula
                cell.font = bold
                head_cells.append(cell)
            ws.append(head_cells)
        for row in rows:
            out: list[Any] = []
            for value in row:
                if isinstance(value, str) and value.startswith("="):
                    # Stored as text: openpyxl would write it as a formula.
                    cell = WriteOnlyCell(ws, value=value)
                    cell.data_type = "s"
                    out.append(cell)
                else:
                    out.append(value)
            ws.append(out)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------
_DOC_HINT = (
    " Use it when the user asks for a document, report or file. After it "
    "returns, link the file in your answer with its download_url, e.g. "
    "[name](download_url)."
)


async def _store(ctx: ChatRunContext, name: str, media_type: str,
                 build: Callable[[], bytes]) -> dict[str, Any]:
    if ctx.created >= MAX_FILES_PER_RUN:
        return ERR_TOO_MANY
    ctx.created += 1  # counted before the await: parallel calls see it
    try:
        data = await asyncio.to_thread(build)
    except SheetError as exc:
        ctx.created -= 1
        return _error("invalid_argument", str(exc))
    except DashboardError as exc:
        ctx.created -= 1
        return _error(exc.code, str(exc))
    except Exception as exc:  # noqa: BLE001 - the model gets a fixed text
        ctx.created -= 1
        logger.warning("webchat: building %s failed (%s)", media_type, type(exc).__name__)
        return ERR_FAILED
    if len(data) > MAX_FILE_BYTES:
        ctx.created -= 1
        return ERR_TOO_LARGE
    try:
        info = await ctx.save(name, media_type, data)
    except Exception as exc:  # noqa: BLE001
        ctx.created -= 1
        logger.warning("webchat: storing a generated file failed (%s)", type(exc).__name__)
        return ERR_FAILED
    ctx.files.append(info)
    return info


def _title(title: Any) -> str | None:
    if not isinstance(title, str):
        return None
    line = " ".join(xml_text(title).split())
    return line[:MAX_TITLE_CHARS] or None


async def create_word_document(filename: str, markdown: str,
                               title: str | None = None) -> dict[str, Any]:
    """Create a Word document (.docx) from markdown: headings (#..####),
    paragraphs with **bold**, *italic* and `code`, bullet and numbered
    lists, pipe tables, fenced code blocks; a horizontal rule (---) starts a
    new page."""
    ctx = current_chat_run.get()
    if ctx is None:
        return ERR_NO_CONTEXT
    name = clean_filename(filename, ".docx")
    if name is None:
        return ERR_NAME
    if not isinstance(markdown, str):
        return ERR_FAILED
    if len(markdown) > MAX_MARKDOWN_CHARS:
        return ERR_MARKDOWN_TOO_LARGE
    head = _title(title)
    return await _store(ctx, name, DOCX, lambda: build_docx(markdown, head))


async def create_pdf_document(filename: str, markdown: str,
                              title: str | None = None) -> dict[str, Any]:
    """Create a PDF document (A4) from markdown: headings (#..####),
    paragraphs with **bold**, *italic* and `code`, bullet and numbered
    lists, pipe tables, fenced code blocks, horizontal rules. Only Latin-1
    characters can be shown; others appear as '?'."""
    ctx = current_chat_run.get()
    if ctx is None:
        return ERR_NO_CONTEXT
    name = clean_filename(filename, ".pdf")
    if name is None:
        return ERR_NAME
    if not isinstance(markdown, str):
        return ERR_FAILED
    if len(markdown) > MAX_MARKDOWN_CHARS:
        return ERR_MARKDOWN_TOO_LARGE
    head = _title(title)
    return await _store(ctx, name, PDF, lambda: build_pdf(markdown, head))


async def create_excel_workbook(filename: str, sheets: list[SheetSpec]) -> dict[str, Any]:
    """Create an Excel workbook (.xlsx): one worksheet per entry of
    `sheets`, each with a bold, frozen header row (`columns`) and its data
    `rows`. Formulas are not supported: a text starting with '=' is stored
    as text."""
    ctx = current_chat_run.get()
    if ctx is None:
        return ERR_NO_CONTEXT
    name = clean_filename(filename, ".xlsx")
    if name is None:
        return ERR_NAME
    return await _store(ctx, name, XLSX, lambda: build_xlsx(sheets))


_DASHBOARD_HINT = (
    " After it returns, the dashboard opens beside the chat by itself: say in "
    "your answer what it shows; do not repeat its data as a table."
)
_DASHBOARD_SUFFIX = re.compile(r"(\.dashboard)?\.json\s*$", re.IGNORECASE)


async def create_dashboard(filename: str, title: str, widgets: list[dict[str, Any]],
                           description: str | None = None) -> dict[str, Any]:
    """Create a dashboard that opens in the chat's viewer: charts, KPI tiles
    and tables on one page. Use it when the user asks for a dashboard,
    charts, graphs or KPIs. The data must come from this conversation
    (uploaded files, tool results, figures the user gave): never invent or
    estimate numbers. At most 12 widgets. Widget shapes: chart {type:
    bar|line|area|pie|scatter, title, data: {columns: [names], rows: [[cells]]},
    x: column, y: [columns] (1-8 series; pie and scatter exactly 1, scatter
    needs a numeric x), stacked (bar/area), unit, format:
    number|percent|currency, size: 1|2, note}; kpi {type: kpi, title, value
    (number or short text), unit, delta (number), delta_label, trend:
    [numbers], size}; table {type: table, title, data, size}. Cells are
    numbers, texts or null; y columns hold numbers or null only; one measure
    per axis (never two units on one chart); dates as ISO strings
    (YYYY-MM-DD)."""
    ctx = current_chat_run.get()
    if ctx is None:
        return ERR_NO_CONTEXT
    raw_name = _DASHBOARD_SUFFIX.sub("", filename) if isinstance(filename, str) else filename
    name = clean_filename(raw_name, DASHBOARD_EXTENSION)
    if name is None:
        return ERR_NAME
    raw: dict[str, Any] = {"title": title, "widgets": widgets}
    if description is not None:
        raw["description"] = description
    checked: dict[str, Any] = {}

    def build() -> bytes:
        spec = validate_spec(raw)
        checked.update(title=spec.title, widgets=len(spec.widgets))
        return build_dashboard(spec)

    info = await _store(ctx, name, DASHBOARD_MEDIA_TYPE, build)
    if "error" in info:
        return info
    return {**info, **checked}


def docgen_toolset() -> FunctionToolset:
    """The four document tools, for one run's ``toolsets=``."""
    toolset: FunctionToolset = FunctionToolset()
    for func in (create_word_document, create_pdf_document, create_excel_workbook,
                 create_dashboard):
        hint = _DASHBOARD_HINT if func is create_dashboard else _DOC_HINT
        toolset.add_function(
            func, takes_ctx=False, name=func.__name__,
            description=" ".join((func.__doc__ or "").split()) + hint,
        )
    return toolset
