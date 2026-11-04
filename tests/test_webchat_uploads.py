"""Uploads of the web chat (``agents/webchat/uploads.py``): media type by
content, refusals with fixed texts, text extraction of office and text files.

Run:  .venv/bin/python -m pytest tests/test_webchat_uploads.py -q
"""

from __future__ import annotations

import io
import os
import sys
import zipfile
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)

from openpyxl import Workbook  # noqa: E402

from agents.multipart_form import FormRefused  # noqa: E402
from agents.webchat import docgen, uploads  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 16


def pdf(pages: int) -> bytes:
    objects = b"".join(b"%d 0 obj << /Type /Page >> endobj\n" % (i + 3) for i in range(pages))
    return b"%PDF-1.7\n" + objects + b"%%EOF\n"


def xlsx(rows) -> bytes:
    book = Workbook()
    ws = book.active
    ws.title = "Budget"
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def docx(markdown: str = "# Title\n\nBody **text**\n\n| a | b |\n|---|---|\n| 1 | 2 |") -> bytes:
    return docgen.build_docx(markdown)


def zipped(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def refused(fn, *args) -> FormRefused:
    with pytest.raises(FormRefused) as caught:
        fn(*args)
    return caught.value


# ---------------------------------------------------------------------------
# Media type
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,declared,data,expected", [
    ("a.png", "image/png", PNG, uploads.PNG),
    ("photo", "", JPEG, uploads.JPEG),
    ("scan.bin", "application/octet-stream", pdf(1), uploads.PDF),
    ("x.jpg", "image/jpg", JPEG, uploads.JPEG),
    ("notes.txt", "text/plain", b"hi", uploads.TEXT),
    ("readme.md", "text/x-markdown", b"# hi", uploads.MARKDOWN),
    ("data.csv", "application/vnd.ms-excel", b"a,b", uploads.CSV),
    ("d.json", "application/json", b"{}", uploads.JSON),
    ("letter.docx", uploads.DOCX, docx(), uploads.DOCX),
    ("book.xlsx", "application/zip", xlsx([[1]]), uploads.XLSX),
])
def test_the_type_is_decided_by_content(name, declared, data, expected):
    assert uploads.classify(name, declared, data) == expected


def test_magic_bytes_win_over_the_extension():
    assert uploads.classify("picture.txt", "", PNG) == uploads.PNG


@pytest.mark.parametrize("name,declared,data,message", [
    ("a.png", "image/jpeg", PNG, uploads.MSG_TYPE_MISMATCH),
    ("a.pdf", "image/png", pdf(1), uploads.MSG_TYPE_MISMATCH),
    ("a.txt", "application/pdf", b"text", uploads.MSG_TYPE_MISMATCH),
    ("a.docx", "text/plain", docx(), uploads.MSG_TYPE_MISMATCH),
    ("a.gif", "image/gif", b"GIF89a....", uploads.MSG_TYPE_NOT_ALLOWED),
    ("a.exe", "", b"MZ\x90\x00", uploads.MSG_TYPE_NOT_ALLOWED),
    # A ZIP is an office file only with the extension AND the main part.
    ("a.zip", "application/zip", docx(), uploads.MSG_TYPE_NOT_ALLOWED),
    ("a.xlsx", "", docx(), uploads.MSG_TYPE_NOT_ALLOWED),
    ("a.docx", "", zipped({"other.xml": b"<x/>"}), uploads.MSG_TYPE_NOT_ALLOWED),
    ("a.docx", "", b"PK\x03\x04 not really a zip", uploads.MSG_TYPE_NOT_ALLOWED),
])
def test_a_file_of_the_wrong_type_is_refused(name, declared, data, message):
    out = refused(uploads.classify, name, declared, data)
    assert (out.status, out.detail) == (415, message)


def test_an_empty_file_is_refused():
    out = refused(uploads.classify, "a.txt", "", b"")
    assert (out.status, out.detail) == (422, uploads.MSG_EMPTY_FILE)


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------
def test_text_files_must_be_utf8():
    assert uploads.extract(uploads.CSV, "﻿naam;bedrag\nCafé;3\n".encode()) == \
        "naam;bedrag\nCafé;3\n"
    out = refused(uploads.extract, uploads.TEXT, "Café".encode("latin-1"))
    assert (out.status, out.detail) == (422, uploads.MSG_TEXT_FILE_NOT_UTF8)
    out = refused(uploads.extract, uploads.JSON, b'{"a": "\x00"}')
    assert out.detail == uploads.MSG_TEXT_FILE_NOT_UTF8


def test_binary_types_are_not_extracted():
    assert uploads.extract(uploads.PNG, PNG) is None


def test_long_text_is_cut_with_a_marker():
    text = uploads.extract(uploads.TEXT, b"x" * (uploads.MAX_TEXT_CHARS + 10))
    assert text.endswith(uploads.TRUNCATED)
    assert len(text) == uploads.MAX_TEXT_CHARS + len(uploads.TRUNCATED)


def test_docx_text_with_headings_and_tables():
    text = uploads.extract(uploads.DOCX, docx())
    assert text.splitlines()[:2] == ["# Title", "Body text"]
    assert "a | b" in text and "1 | 2" in text


def test_a_docx_with_an_entity_declaration_is_refused():
    evil = (b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x "boom">]>'
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            b"<w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body></w:document>")
    data = zipped({"word/document.xml": evil, "[Content_Types].xml": b"<Types/>"})
    out = refused(uploads.extract_docx, data)
    assert (out.status, out.detail) == (422, uploads.MSG_OFFICE_UNREADABLE)


def test_an_office_file_that_declares_too_much_is_refused(monkeypatch):
    monkeypatch.setattr(uploads, "MAX_UNCOMPRESSED_BYTES", 100)
    out = refused(uploads.extract_docx, docx("x" * 1000))
    assert out.detail == uploads.MSG_OFFICE_TOO_LARGE


def test_a_member_that_inflates_beyond_its_cap_is_refused(monkeypatch):
    monkeypatch.setattr(uploads, "MAX_PART_BYTES", 50)
    out = refused(uploads.extract_docx, docx("long " * 100))
    assert out.detail == uploads.MSG_OFFICE_TOO_LARGE


def test_xlsx_text_per_sheet_with_cached_values():
    data = xlsx([["Region", "Amount"], ["North", 12.0], ["South", 3.5], [None, None],
                 ["=1+1", "=SUM(B2:B3)"]])
    text = uploads.extract(uploads.XLSX, data)
    lines = text.splitlines()
    assert lines[:4] == ["## Budget", "Region | Amount", "North | 12", "South | 3.5"]
    # openpyxl stores formulas without a cached value: nothing is calculated
    # and the formula text is never evaluated.
    assert "2" not in lines[4:] and all("SUM" not in line for line in lines[4:])


def test_xlsx_rows_are_capped(monkeypatch):
    monkeypatch.setattr(uploads, "MAX_SHEET_ROWS", 3)
    text = uploads.extract(uploads.XLSX, xlsx([[i] for i in range(10)]))
    assert text.splitlines()[-1].startswith("[... more than 3 rows")


def test_a_broken_workbook_is_refused():
    data = zipped({"xl/workbook.xml": b"<not-a-workbook", "[Content_Types].xml": b"<x/>"})
    out = refused(uploads.extract_xlsx, data)
    assert (out.status, out.detail) == (422, uploads.MSG_OFFICE_UNREADABLE)


# ---------------------------------------------------------------------------
# PDF pages
# ---------------------------------------------------------------------------
async def test_pdf_pages_are_counted():
    assert (await uploads.check_upload("a.pdf", "", pdf(50))).media_type == uploads.PDF
    with pytest.raises(FormRefused) as caught:
        await uploads.check_upload("a.pdf", "", pdf(51))
    assert caught.value.detail == uploads.MSG_TOO_MANY_PAGES
    with pytest.raises(FormRefused) as caught:
        await uploads.check_upload("a.pdf", "", b"%PDF-1.7\n%%EOF")
    assert caught.value.detail == uploads.MSG_PAGES_UNKNOWN


async def test_upload_names_are_cleaned():
    up = await uploads.check_upload("a\x00‮.txt", "", b"hello")
    assert up.name == "a.txt" and up.text == "hello" and not up.binary


def test_count_pdf_pages_still_reads_object_streams():
    from agents.multipart_form import count_pdf_pages

    body = zlib.compress(b"1 0 obj << /Type /Page >> endobj 2 0 obj << /Type /Page >> endobj")
    data = (b"%%PDF-1.7\n9 0 obj << /Type /ObjStm /Length %d >>\nstream\n" % len(body)
            + body + b"\nendstream\nendobj\n")
    assert count_pdf_pages(data) == 2
