"""Multipart bodies read in memory, under limits, for the routes that take files.

Shared by the invoke API (``agents/invoke.py``) and the web chat
(``agents/webchat/uploads.py``). Starlette's own form parser spools a file
over 1 MB to a temporary file on disk; both callers want the bytes in memory
only, and want a body refused the moment it passes a cap, not after it was
read. So the body is fed chunk by chunk to python-multipart's low-level
parser and collected here.

What differs per caller is a :class:`FormSpec`: which text fields exist and
how large each may be, how many files of what size, and the fixed texts of
the refusals (none of which names a file, a value or the caller).

Also here, because both callers need them: :func:`sniff` (PNG, JPEG, PDF by
their magic bytes), :func:`count_pdf_pages` (a conservative page count with
bounded work) and :func:`clean_name` (a file name fit to show).
"""

from __future__ import annotations

import logging
import re
import unicodedata
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from starlette.requests import ClientDisconnect

try:  # the package Starlette uses; a dependency of the admin UI already
    from python_multipart.exceptions import FormParserError
    from python_multipart.multipart import MultipartParser, parse_options_header
except ModuleNotFoundError:  # pragma: no cover - older package name
    from multipart.exceptions import FormParserError  # type: ignore[no-redef]
    from multipart.multipart import (  # type: ignore[no-redef]
        MultipartParser,
        parse_options_header,
    )

from agents.invoke_config import JPEG, PDF, PNG

logger = logging.getLogger(__name__)

# Room for the part headers and boundaries on top of the files and the texts.
FORM_OVERHEAD_BYTES = 64 * 1024
# Decompressed bytes a page count may inflate across all streams of one PDF,
# and the streams it looks at; a file over either cannot be counted.
PDF_INFLATE_BUDGET = 64 * 1024 * 1024
MAX_PDF_STREAMS = 5_000
NAME_CHARS = 120


class FormRefused(Exception):
    """A body that is refused: ``status`` and a fixed ``detail``."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class FormSpec:
    """What one route accepts, and the fixed texts of its refusals."""

    text_fields: Mapping[str, int]  # field name -> its cap in bytes
    max_files: int
    max_file_bytes: int
    msg_not_multipart: str
    msg_malformed: str
    msg_disconnected: str
    msg_unexpected_field: str
    msg_body_too_large: str
    msg_file_too_large: str
    msg_text_too_large: str
    msg_too_many_files: str
    file_fields: tuple[str, ...] = ("files", "files[]")

    @property
    def body_cap(self) -> int:
        return (self.max_files * self.max_file_bytes
                + sum(self.text_fields.values()) + FORM_OVERHEAD_BYTES)


@dataclass
class FormPart:
    name: str = ""
    filename: str | None = None
    content_type: str = ""
    data: bytearray = field(default_factory=bytearray)


class FormReader:
    """Collects the parts of one multipart body in memory, refusing as soon
    as a limit is passed (a file is never read past its cap)."""

    def __init__(self, spec: FormSpec) -> None:
        self.spec = spec
        self.parts: list[FormPart] = []
        self.files = 0
        self._part = FormPart()
        self._header_name = b""
        self._header_value = b""
        self._headers: dict[bytes, bytes] = {}
        # python-multipart's finalize() does not complain about a body that
        # stops before its closing boundary: the part in flight would just be
        # dropped (a file the run then never sees). So the end is tracked.
        self.in_part = False
        self.ended = False

    def callbacks(self) -> dict[str, Any]:
        return {
            "on_part_begin": self._begin,
            "on_part_data": self._data,
            "on_part_end": self._end,
            "on_header_field": self._header_field,
            "on_header_value": self._header_value_cb,
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_end": self._on_end,
        }

    def _begin(self) -> None:
        self._part = FormPart()
        self._headers = {}
        self.in_part = True

    def _on_end(self) -> None:
        self.ended = True

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_name += data[start:end]

    def _header_value_cb(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def _header_end(self) -> None:
        self._headers[self._header_name.lower()] = self._header_value
        self._header_name = b""
        self._header_value = b""

    def _headers_finished(self) -> None:
        spec = self.spec
        _, options = parse_options_header(self._headers.get(b"content-disposition", b""))
        name = options.get(b"name")
        if name is None:
            raise FormRefused(422, spec.msg_malformed)
        self._part.name = name.decode("utf-8", "replace")
        if b"filename" in options:
            if self._part.name not in spec.file_fields:
                raise FormRefused(422, spec.msg_unexpected_field)
            self.files += 1
            if self.files > spec.max_files:
                raise FormRefused(422, spec.msg_too_many_files)
            self._part.filename = options[b"filename"].decode("utf-8", "replace")
            ctype, _ = parse_options_header(self._headers.get(b"content-type", b""))
            self._part.content_type = ctype.decode("latin-1").strip().lower()
        elif self._part.name not in spec.text_fields:
            raise FormRefused(422, spec.msg_unexpected_field)

    def _data(self, data: bytes, start: int, end: int) -> None:
        part = self._part
        is_file = part.filename is not None
        cap = self.spec.max_file_bytes if is_file else self.spec.text_fields[part.name]
        if len(part.data) + (end - start) > cap:
            raise FormRefused(
                413, self.spec.msg_file_too_large if is_file else self.spec.msg_text_too_large
            )
        part.data.extend(data[start:end])

    def _end(self) -> None:
        self.parts.append(self._part)
        self.in_part = False


async def read_multipart(request: Any, spec: FormSpec, *, label: str) -> list[FormPart]:
    """Every part of the request's multipart body, in order.

    ``label`` names the caller in the one log line a malformed body gets
    (the exception class, never the body).
    """
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    if ctype.lower() != b"multipart/form-data" or b"boundary" not in params:
        raise FormRefused(415, spec.msg_not_multipart)
    body_cap = spec.body_cap
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > body_cap:
        raise FormRefused(413, spec.msg_body_too_large)
    reader = FormReader(spec)
    parser = MultipartParser(params[b"boundary"], reader.callbacks())
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > body_cap:
                raise FormRefused(413, spec.msg_body_too_large)
            parser.write(chunk)
        parser.finalize()
    except FormRefused:
        raise
    except ClientDisconnect:
        raise FormRefused(400, spec.msg_disconnected) from None
    except (FormParserError, ValueError) as exc:
        logger.info("%s: multipart body refused (%s)", label, type(exc).__name__)
        raise FormRefused(422, spec.msg_malformed) from None
    if reader.in_part or not reader.ended:
        raise FormRefused(422, spec.msg_malformed)
    return reader.parts


def clean_name(raw: str, limit: int = NAME_CHARS) -> str:
    """A file name fit to show: no control, format or separator characters,
    at most ``limit`` characters."""
    kept = "".join(
        ch for ch in raw
        if not unicodedata.category(ch).startswith("C")
        and unicodedata.category(ch) not in ("Zl", "Zp")
    ).strip()
    return kept[:limit] or "attachment"


def sniff(data: bytes) -> str | None:
    """The media type the magic bytes say, of the three that are known."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if data.startswith(b"\xff\xd8\xff"):
        return JPEG
    if data.startswith(b"%PDF-"):
        return PDF
    return None


_PAGE_RE = re.compile(rb"/Type\s*/Page(?![A-Za-z0-9])")
_STREAM_RE = re.compile(rb"(?<!end)stream\r?\n")
_OBJ_HEAD_RE = re.compile(rb"\d+\s+\d+\s+obj(?![A-Za-z0-9])")
_OBJSTM_RE = re.compile(rb"/Type\s*/ObjStm(?![A-Za-z0-9])")
# How far before `stream` the object's dictionary is looked for.
_DICT_LOOKBACK = 4096


def _is_object_stream(data: bytes, stream_at: int) -> bool:
    """Whether the dictionary of the stream starting at ``stream_at`` says
    ``/Type /ObjStm``. The dictionary is what follows the last ``N G obj``
    within ``_DICT_LOOKBACK`` bytes; a dictionary longer than that is read
    from the window's start, which can only make more streams count as
    object streams (more inflating, never a page missed)."""
    lo = max(0, stream_at - _DICT_LOOKBACK)
    head = lo
    for match in _OBJ_HEAD_RE.finditer(data, lo, stream_at):
        head = match.end()
    return _OBJSTM_RE.search(data, head, stream_at) is not None


def count_pdf_pages(data: bytes, *, budget: int = PDF_INFLATE_BUDGET,
                    max_streams: int = MAX_PDF_STREAMS) -> int:
    """A conservative page count: every ``/Type /Page`` object in the file
    and in its object streams (PDF 1.5+ may hold page dictionaries
    compressed inside a ``/Type /ObjStm`` stream). 0 means "cannot tell" and
    is refused.

    Why approximate, and in which direction: no PDF library is a dependency.
    A page tree leaf must carry ``/Type /Page``, so every page is counted;
    what can add to the count is an older revision of a page left by an
    incremental update, or such text inside a stream. So the count is at
    least the real one (a refusal errs towards refusing). Not seen: page
    objects in an object stream under another filter or encrypted; when
    nothing is found at all the caller refuses the file.

    A page dictionary can only be a plain object (seen by the scan of the
    whole file) or inside an object stream, so ONLY object streams are
    inflated; every other stream (a scanned receipt's images, content) is
    skipped without inflating. Bounded work on a file somebody else wrote
    (callers run this in a thread, but the regex and zlib hold the GIL): the
    streams are found in ONE forward scan (the next search starts after the
    previous ``endstream``), sliced through a ``memoryview`` (no copy), at
    most ``max_streams`` of them; every object-stream inflate is charged to
    ``budget``, and one that does not fit in what is left answers 0.
    """
    count = len(_PAGE_RE.findall(data))
    view = memoryview(data)
    streams = 0
    pos = 0
    while True:
        match = _STREAM_RE.search(data, pos)
        if match is None:
            break
        streams += 1
        if streams > max_streams:
            return 0
        start = match.end()
        end = data.find(b"endstream", start)
        if end < 0:
            break
        pos = end + len(b"endstream")
        if not _is_object_stream(data, match.start()):
            continue
        if budget <= 0:
            return 0
        inflater = zlib.decompressobj()
        try:
            chunk = inflater.decompress(view[start:end], budget)
        except zlib.error:
            continue  # not Flate (or damaged): nothing readable in it
        if inflater.unconsumed_tail:
            return 0  # did not fit in the budget: cannot tell
        budget -= len(chunk) + 1
        count += len(_PAGE_RE.findall(chunk))
    return count
