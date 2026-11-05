"""Uploads of the web chat: the multipart body, the media type, the text.

The body is read in memory under the chat's limits by the shared reader
(``agents/multipart_form.py``, also the invoke API's). Each file's media
type is decided by its content, never by what the browser declares:

* PNG, JPEG and PDF by their magic bytes; they go to the model as
  ``BinaryContent`` (as the invoke API does). A PDF over ``MAX_PDF_PAGES``
  is refused (the conservative count of ``count_pdf_pages``).
* ``.docx`` and ``.xlsx`` are ZIP archives (``PK\\x03\\x04``) with the
  right extension AND the right main part in the archive listing
  (``word/document.xml``, ``xl/workbook.xml``); their text is extracted.
* ``.txt``, ``.md``, ``.csv``, ``.json`` by extension, and only when the
  bytes are UTF-8; the text is passed on as it is.

A declared type that names something else is refused (a few aliases the
browsers send are accepted). Extracted text is cut at ``MAX_TEXT_CHARS``
with a marker, never silently.

The office formats are files somebody else wrote: the archive caps are
checked from the declared sizes before anything is inflated (the idea of
``agents/sharepoint_workbook.py``), the XML is parsed with defusedxml
(no entity declarations), a workbook is read through the SharePoint
reader's guarded archive, and every extraction runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import zipfile
import zlib
from dataclasses import dataclass
from typing import Any

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from agents.ide.store import env_int
from agents.multipart_form import (
    JPEG,
    PDF,
    PNG,
    FormRefused,
    FormSpec,
    clean_name,
    count_pdf_pages,
    read_multipart,
    sniff,
)

logger = logging.getLogger(__name__)


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TEXT = "text/plain"
MARKDOWN = "text/markdown"
CSV = "text/csv"
JSON = "application/json"

MEDIA_TYPES = (PNG, JPEG, PDF, TEXT, MARKDOWN, CSV, JSON, DOCX, XLSX)
BINARY_TYPES = frozenset({PNG, JPEG, PDF})
TEXT_EXTENSIONS = {".txt": TEXT, ".md": MARKDOWN, ".markdown": MARKDOWN,
                   ".csv": CSV, ".json": JSON}

MAX_FILE_BYTES = env_int("CHAT_MAX_FILE_BYTES", 10 * 1024 * 1024, minimum=1)
MAX_FILES = env_int("CHAT_MAX_FILES_PER_MESSAGE", 5, minimum=1)
TEXT_MAX_BYTES = 64 * 1024
MODEL_FIELD_BYTES = 256
# How much of an extracted upload goes into the prompt; the bytes stay in
# chat_files in full for tools that read them (builtin:reconcile).
MAX_TEXT_CHARS = env_int("CHAT_ATTACHMENT_MAX_CHARS", 200_000, minimum=1_000)
MAX_PDF_PAGES = 50
TRUNCATED = "\n\n[... cut here: the file holds more text than is passed on]"

# Archive caps of an office file, from the sizes it declares.
MAX_ARCHIVE_MEMBERS = 5_000
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_PART_BYTES = 16 * 1024 * 1024
# What a workbook extraction reads at most.
MAX_SHEET_ROWS = 10_000
MAX_SHEET_COLUMNS = 200

# Fixed texts: none names a file or a value.
MSG_NOT_MULTIPART = "The request must be multipart/form-data"
MSG_MALFORMED = "The multipart body could not be read"
MSG_DISCONNECTED = "The request body was not received completely"
MSG_UNEXPECTED_FIELD = "The form holds a field other than 'text', 'files' and 'model'"
MSG_TEXT_MISSING = "The field 'text' is required and must not be empty"
MSG_TEXT_TOO_LARGE = "The field 'text' is too large"
MSG_NOT_TEXT = "The field 'text' must be UTF-8 text"
MSG_BODY_TOO_LARGE = "The request body is too large"
MSG_FILE_TOO_LARGE = "A file is larger than the chat accepts"
MSG_TOO_MANY_FILES = "More files than the chat accepts in one message"
MSG_EMPTY_FILE = "A file is empty"
MSG_TYPE_NOT_ALLOWED = "A file is of a type the chat does not accept"
MSG_TYPE_MISMATCH = "A file's content does not match its declared media type"
MSG_TEXT_FILE_NOT_UTF8 = "A text file must be UTF-8"
MSG_TOO_MANY_PAGES = "A PDF has more pages than the chat accepts"
MSG_PAGES_UNKNOWN = "The page count of a PDF could not be determined"
MSG_OFFICE_UNREADABLE = "An office file could not be read"
MSG_OFFICE_TOO_LARGE = "An office file is larger than the chat reads"

_UNDECLARED = frozenset({"", "application/octet-stream"})
# What a browser may declare for each type it then turns out to be.
_DECLARED = {
    PNG: {PNG},
    JPEG: {JPEG, "image/jpg", "image/pjpeg"},
    PDF: {PDF, "application/x-pdf"},
    TEXT: {TEXT},
    MARKDOWN: {MARKDOWN, "text/x-markdown", TEXT},
    CSV: {CSV, "application/csv", "text/x-csv", "application/vnd.ms-excel", TEXT},
    JSON: {JSON, "text/json", TEXT},
    DOCX: {DOCX, "application/zip", "application/x-zip-compressed"},
    XLSX: {XLSX, "application/zip", "application/x-zip-compressed"},
}


@dataclass
class Upload:
    """One accepted file: ``text`` is the extracted text of an office or
    text file, ``None`` for a file that goes to the model as bytes."""

    name: str
    media_type: str
    data: bytes
    text: str | None = None

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def binary(self) -> bool:
        return self.media_type in BINARY_TYPES


@dataclass
class ChatForm:
    text: str
    model: str | None
    uploads: list[Upload]


def form_spec() -> FormSpec:
    return FormSpec(
        text_fields={"text": TEXT_MAX_BYTES, "model": MODEL_FIELD_BYTES},
        max_files=MAX_FILES,
        max_file_bytes=MAX_FILE_BYTES,
        msg_not_multipart=MSG_NOT_MULTIPART,
        msg_malformed=MSG_MALFORMED,
        msg_disconnected=MSG_DISCONNECTED,
        msg_unexpected_field=MSG_UNEXPECTED_FIELD,
        msg_body_too_large=MSG_BODY_TOO_LARGE,
        msg_file_too_large=MSG_FILE_TOO_LARGE,
        msg_text_too_large=MSG_TEXT_TOO_LARGE,
        msg_too_many_files=MSG_TOO_MANY_FILES,
    )


# ---------------------------------------------------------------------------
# Media type
# ---------------------------------------------------------------------------
def _extension(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def _zip_listing(data: bytes) -> list[zipfile.ZipInfo]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return zf.infolist()
    except (zipfile.BadZipFile, ValueError, OSError):
        raise FormRefused(415, MSG_TYPE_NOT_ALLOWED) from None


def classify(name: str, declared: str, data: bytes) -> str:
    """The media type of an upload, decided by its content; ``FormRefused``
    when it is none the chat accepts or the declared type disagrees."""
    if not data:
        raise FormRefused(422, MSG_EMPTY_FILE)
    ext = _extension(name)
    found = sniff(data)
    if found is None and data.startswith(b"PK\x03\x04"):
        members = {info.filename for info in _zip_listing(data)}
        if ext == ".docx" and "word/document.xml" in members:
            found = DOCX
        elif ext == ".xlsx" and "xl/workbook.xml" in members:
            found = XLSX
        else:
            raise FormRefused(415, MSG_TYPE_NOT_ALLOWED)
    if found is None:
        found = TEXT_EXTENSIONS.get(ext)
        if found is None:
            raise FormRefused(415, MSG_TYPE_NOT_ALLOWED)
    if declared not in _UNDECLARED and declared not in _DECLARED[found]:
        raise FormRefused(415, MSG_TYPE_MISMATCH)
    return found


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------
def cap_text(text: str) -> str:
    """``text`` cut at ``MAX_TEXT_CHARS`` with a marker."""
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return text[:MAX_TEXT_CHARS] + TRUNCATED


def decode_text(data: bytes) -> str:
    """UTF-8 (a byte order mark is dropped); ``FormRefused`` otherwise."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise FormRefused(422, MSG_TEXT_FILE_NOT_UTF8) from None
    if "\x00" in text:
        raise FormRefused(422, MSG_TEXT_FILE_NOT_UTF8)
    return text


def check_office_archive(data: bytes) -> None:
    """Archive caps from the declared sizes; nothing is inflated."""
    infos = _zip_listing(data)
    if len(infos) > MAX_ARCHIVE_MEMBERS or sum(i.file_size for i in infos) > MAX_UNCOMPRESSED_BYTES:
        raise FormRefused(422, MSG_OFFICE_TOO_LARGE)


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _paragraph_text(p: Any) -> str:
    # Runs only: the paragraph properties hold <w:tab> stops too.
    out: list[str] = []
    for run in p.iter(f"{_W}r"):
        for node in run:
            if node.tag == f"{_W}t" and node.text:
                out.append(node.text)
            elif node.tag == f"{_W}tab":
                out.append("\t")
            elif node.tag in (f"{_W}br", f"{_W}cr"):
                out.append("\n")
    return "".join(out)


def extract_docx(data: bytes) -> str:
    """The text of a ``.docx``: paragraphs, headings as ``#``, tables as
    ``a | b`` rows (CPU work on an untrusted file: call it in a thread)."""
    check_office_archive(data)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            info = zf.getinfo("word/document.xml")
            if info.file_size > MAX_PART_BYTES:
                raise FormRefused(422, MSG_OFFICE_TOO_LARGE)
            with zf.open(info) as part:
                # Read to the declared size and one byte more: a member that
                # inflates beyond what it declares is refused, not read on.
                xml = part.read(MAX_PART_BYTES + 1)
        if len(xml) > MAX_PART_BYTES:
            raise FormRefused(422, MSG_OFFICE_TOO_LARGE)
        root = SafeET.fromstring(xml, forbid_dtd=True)
    except FormRefused:
        raise
    except (KeyError, zipfile.BadZipFile, ValueError, OSError, SafeET.ParseError,
            DefusedXmlException, zlib.error):
        raise FormRefused(422, MSG_OFFICE_UNREADABLE) from None
    body = root.find(f"{_W}body")
    if body is None:
        return ""
    lines: list[str] = []
    size = 0
    for child in body:
        if child.tag == f"{_W}p":
            text = _paragraph_text(child)
            style = child.find(f"{_W}pPr/{_W}pStyle")
            value = style.get(f"{_W}val", "") if style is not None else ""
            if value.lower().startswith("heading") and value[-1:].isdigit() and text.strip():
                text = "#" * min(int(value[-1]), 6) + " " + text
            elif value.lower() == "title" and text.strip():
                text = "# " + text
            lines.append(text)
        elif child.tag == f"{_W}tbl":
            for row in child.iter(f"{_W}tr"):
                cells = [" ".join(_paragraph_text(p) for p in tc.iter(f"{_W}p")).strip()
                         for tc in row.iter(f"{_W}tc")]
                lines.append(" | ".join(cells))
            lines.append("")
        else:
            continue
        size += len(lines[-1]) + 1
        if size > MAX_TEXT_CHARS:
            break
    return "\n".join(lines).strip()


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return " ".join(str(value).split())


def extract_xlsx(data: bytes) -> str:
    """The text of an ``.xlsx``: per sheet a ``## <name>`` heading and its
    rows as ``a | b`` lines, cached values only, at most
    ``MAX_SHEET_ROWS`` x ``MAX_SHEET_COLUMNS`` per sheet (CPU work on an
    untrusted file: call it in a thread)."""
    from agents.sharepoint_workbook import Refused, check_archive, open_workbook

    try:
        check_archive(data)
        book = open_workbook(data)
    except Refused as refused:
        status_text = MSG_OFFICE_TOO_LARGE if refused.code == "too_large" else MSG_OFFICE_UNREADABLE
        raise FormRefused(422, status_text) from None
    lines: list[str] = []
    size = 0
    try:
        for ws in book.worksheets:
            lines.append(f"## {ws.title}")
            blank = 0
            for index, row in enumerate(ws.iter_rows(max_col=MAX_SHEET_COLUMNS,
                                                     values_only=True)):
                if index >= MAX_SHEET_ROWS:
                    lines.append(f"[... more than {MAX_SHEET_ROWS} rows; cut here]")
                    break
                cells = [_cell_text(v) for v in row]
                while cells and not cells[-1]:
                    cells.pop()
                if not cells:
                    blank += 1
                    if blank >= 50:  # the rest of a sheet with a stray far cell
                        break
                    continue
                blank = 0
                line = " | ".join(cells)
                lines.append(line)
                size += len(line) + 1
                if size > MAX_TEXT_CHARS:
                    break
            lines.append("")
            if size > MAX_TEXT_CHARS:
                break
    except FormRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - one code for whatever the parser says
        logger.info("webchat: workbook extraction failed (%s)", type(exc).__name__)
        raise FormRefused(422, MSG_OFFICE_UNREADABLE) from None
    finally:
        try:
            book.close()
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(lines).strip()


def extract(upload_type: str, data: bytes) -> str | None:
    """The text of an upload of ``upload_type`` (``None`` for the binary
    types), cut at ``MAX_TEXT_CHARS``."""
    if upload_type in BINARY_TYPES:
        return None
    if upload_type == DOCX:
        text = extract_docx(data)
    elif upload_type == XLSX:
        text = extract_xlsx(data)
    else:
        text = decode_text(data)
    return cap_text(text)


# ---------------------------------------------------------------------------
# The form
# ---------------------------------------------------------------------------
async def check_upload(filename: str, declared: str, data: bytes) -> Upload:
    """One file of the form, classified and (for text types) extracted."""
    name = clean_name(filename)
    media_type = classify(name, declared, data)
    if media_type == PDF:
        pages = await asyncio.to_thread(count_pdf_pages, data)
        if pages == 0:
            raise FormRefused(422, MSG_PAGES_UNKNOWN)
        if pages > MAX_PDF_PAGES:
            raise FormRefused(422, MSG_TOO_MANY_PAGES)
    text = await asyncio.to_thread(extract, media_type, data)
    return Upload(name=name, media_type=media_type, data=data, text=text)


async def read_chat_form(request: Any) -> ChatForm:
    """The ``text``, the optional ``model`` and the checked uploads."""
    parts = await read_multipart(request, form_spec(), label="webchat")
    fields: dict[str, str] = {}
    for part in parts:
        if part.filename is not None:
            continue
        if part.name in fields:
            raise FormRefused(422, MSG_MALFORMED)
        try:
            fields[part.name] = bytes(part.data).decode("utf-8")
        except UnicodeDecodeError:
            raise FormRefused(422, MSG_NOT_TEXT) from None
    text = fields.get("text", "")
    if not text.strip():
        raise FormRefused(422, MSG_TEXT_MISSING)
    model = (fields.get("model") or "").strip() or None
    uploads: list[Upload] = []
    for part in parts:
        if part.filename is None:
            continue
        data = bytes(part.data)
        part.data = bytearray()  # the body is never held twice
        uploads.append(await check_upload(part.filename, part.content_type, data))
    return ChatForm(text=text, model=model, uploads=uploads)
