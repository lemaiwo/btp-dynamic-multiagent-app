"""The viewer's file preview: ``GET /chat/api/files/{fid}/preview``.

Through the web chat routers on a bare app behind the same principal-binding
wrapper as ``tests/test_webchat_api.py``; file rows are inserted directly.

Run:  .venv/bin/python -m pytest tests/test_webchat_preview.py -q
"""

from __future__ import annotations

import io
import os
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from openpyxl import Workbook  # noqa: E402

from agents import auth  # noqa: E402
from agents.auth import current_jwt, current_principal  # noqa: E402
from agents.db import (  # noqa: E402
    ChatConversation,
    ChatFile,
    ChatMessage,
    SessionLocal,
    init_db,
)
from agents.validation_errors import install_validation_handler  # noqa: E402
from agents.webchat import docgen, preview, routes, store, uploads  # noqa: E402

ALICE = "alice@example.com"
BOB = "bob@example.com"
API = "/chat/api"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
def bound_app() -> Any:
    inner = FastAPI()
    inner.include_router(routes.router)
    install_validation_handler(inner)

    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        headers = dict(scope["headers"])
        user = headers.get(b"x-test-user", ALICE.encode()).decode()
        p = current_principal.set(user)
        j = current_jwt.set(f"jwt-of-{user}")
        try:
            await inner(scope, receive, send)
        finally:
            current_principal.reset(p)
            current_jwt.reset(j)

    return asgi


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=bound_app()),
                           base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def _clean():
    saved = (auth._validator, auth._validator_checked)
    auth._validator, auth._validator_checked = None, True
    await init_db()
    async with SessionLocal() as s:
        for model in (ChatFile, ChatMessage, ChatConversation):
            await s.execute(delete(model))
        await s.commit()
    yield
    auth._validator, auth._validator_checked = saved


async def add_file(name: str, media_type: str, data: bytes, owner: str = ALICE) -> str:
    async with SessionLocal() as db:
        conv = await store.create_conversation(db, owner, title=None, model=None)
        row = ChatFile(conversation_id=conv.id, principal=owner, kind="upload",
                       name=name, media_type=media_type, size=len(data), data=data)
        db.add(row)
        await db.commit()
        return row.id


async def get_preview(client, fid: str, user: str = ALICE):
    return await client.get(f"{API}/files/{fid}/preview", headers={"x-test-user": user})


def xlsx(fill) -> bytes:
    book = Workbook()
    fill(book)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def docx(body: str, numbering: str | None = None) -> bytes:
    """A minimal hand-made ``.docx``: the document part and, optionally,
    the numbering part."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml",
                    f'<w:document xmlns:w="{W_NS}"><w:body>{body}</w:body></w:document>')
        if numbering is not None:
            zf.writestr("word/numbering.xml",
                        f'<w:numbering xmlns:w="{W_NS}">{numbering}</w:numbering>')
    return buffer.getvalue()


def para(text: str, style: str | None = None, num: str | None = None) -> str:
    ppr = ""
    if style:
        ppr += f'<w:pStyle w:val="{style}"/>'
    if num:
        ppr += f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num}"/></w:numPr>'
    ppr = f"<w:pPr>{ppr}</w:pPr>" if ppr else ""
    run = f"<w:r><w:t>{text}</w:t></w:r>" if text else ""
    return f"<w:p>{ppr}{run}</w:p>"


def table(rows: list[list[str]]) -> str:
    return "<w:tbl>" + "".join(
        "<w:tr>" + "".join(f"<w:tc>{cell}</w:tc>" for cell in row) + "</w:tr>"
        for row in rows
    ) + "</w:tbl>"


NUMBERING = (
    '<w:abstractNum w:abstractNumId="10"><w:lvl w:ilvl="0">'
    '<w:numFmt w:val="decimal"/></w:lvl></w:abstractNum>'
    '<w:abstractNum w:abstractNumId="20"><w:lvl w:ilvl="0">'
    '<w:numFmt w:val="bullet"/></w:lvl></w:abstractNum>'
    '<w:num w:numId="1"><w:abstractNumId w:val="10"/></w:num>'
    '<w:num w:numId="2"><w:abstractNumId w:val="20"/></w:num>'
)


# ---------------------------------------------------------------------------
# Workbooks
# ---------------------------------------------------------------------------
async def test_a_workbook_is_sheets_with_iso_dates_and_text_formulas(client):
    def fill(book):
        ws = book.active
        ws.title = "Data"
        ws.append(["Name", "Amount", "When", "Ok", "Formula"])
        ws.append(["a", 1.5, date(2026, 10, 31), True, None])
        ws["E2"] = "=SUM(A1:A2)"
        ws["E2"].data_type = "s"  # stored as a string that starts with "="
        ws.append(["b", 2, datetime(2026, 10, 31, 12, 30), False, None])
        book.create_sheet("Empty")

    fid = await add_file("book.xlsx", uploads.XLSX, xlsx(fill))
    res = await get_preview(client, fid)
    assert res.status_code == 200
    assert res.headers["cache-control"] == "private, no-store"
    body = res.json()
    assert body["kind"] == "sheets" and body["name"] == "book.xlsx"
    assert body["truncated"] is False
    data, empty = body["sheets"]
    assert data["name"] == "Data"
    assert data["columns"] == ["A", "B", "C", "D", "E"]
    assert data["rows"][0] == ["Name", "Amount", "When", "Ok", "Formula"]
    assert data["rows"][1][:4] == ["a", 1.5, "2026-10-31", True]
    assert data["rows"][1][4] == "=SUM(A1:A2)"
    assert data["rows"][2][2] == "2026-10-31T12:30:00"
    assert data["row_count"] == 3 and data["truncated"] is False
    assert empty == {"name": "Empty", "columns": [], "rows": [], "row_count": 0,
                     "truncated": False}


async def test_a_formula_cell_shows_its_cached_value_never_the_formula(client):
    def fill(book):
        book.active["A1"] = 2
        book.active["A2"] = "=A1*2"  # saved by openpyxl without a cached value
        book.active["A3"] = 3

    fid = await add_file("f.xlsx", uploads.XLSX, xlsx(fill))
    sheet = (await get_preview(client, fid)).json()["sheets"][0]
    assert sheet["rows"] == [[2], [None], [3]] and sheet["row_count"] == 3


async def test_a_long_sheet_is_cut_at_200_rows(client):
    def fill(book):
        for i in range(250):
            book.active.append([i])

    fid = await add_file("long.xlsx", uploads.XLSX, xlsx(fill))
    body = (await get_preview(client, fid)).json()
    sheet = body["sheets"][0]
    assert len(sheet["rows"]) == preview.MAX_ROWS == 200
    assert sheet["rows"][-1] == [199]
    assert sheet["row_count"] == 250
    assert sheet["truncated"] is True and body["truncated"] is True


async def test_a_wide_sheet_is_cut_at_50_columns(client):
    def fill(book):
        book.active.append(list(range(60)))

    fid = await add_file("wide.xlsx", uploads.XLSX, xlsx(fill))
    sheet = (await get_preview(client, fid)).json()["sheets"][0]
    assert len(sheet["columns"]) == 50 and sheet["columns"][-1] == "AX"
    assert sheet["rows"] == [list(range(50))]
    assert sheet["truncated"] is True


async def test_at_most_ten_sheets(client):
    def fill(book):
        for i in range(11):
            book.create_sheet(f"S{i}")

    fid = await add_file("many.xlsx", uploads.XLSX, xlsx(fill))
    body = (await get_preview(client, fid)).json()
    assert len(body["sheets"]) == 10 and body["truncated"] is True


async def test_a_generated_workbook_previews(client):
    data = docgen.build_xlsx([docgen.SheetSpec(name="Report", columns=["k", "v"],
                                               rows=[["x", 1], ["=1+1", 2]])])
    fid = await add_file("report.xlsx", uploads.XLSX, data)
    sheet = (await get_preview(client, fid)).json()["sheets"][0]
    assert sheet["rows"] == [["k", "v"], ["x", 1], ["=1+1", 2]]


# ---------------------------------------------------------------------------
# Word documents
# ---------------------------------------------------------------------------
async def test_a_generated_docx_is_blocks(client):
    data = docgen.build_docx("# Head\n\nSome **bold** text.\n\n| a | b |\n|---|---|\n| 1 | 2 |",
                             title="The title")
    fid = await add_file("doc.docx", uploads.DOCX, data)
    res = await get_preview(client, fid)
    assert res.status_code == 200
    assert res.headers["cache-control"] == "private, no-store"
    body = res.json()
    assert body["kind"] == "document" and body["truncated"] is False
    assert body["blocks"] == [
        {"type": "heading", "level": 1, "text": "The title"},
        {"type": "heading", "level": 1, "text": "Head"},
        {"type": "paragraph", "text": "Some bold text."},
        {"type": "table", "rows": [["a", "b"], ["1", "2"]]},
    ]


async def test_heading_styles_and_empty_paragraphs(client):
    body = (para("T", "Title") + para("", None) + para("Two", "Heading2")
            + para("Six", "heading 6") + para("Plain", "Normal") + para("   "))
    fid = await add_file("h.docx", uploads.DOCX, docx(body))
    blocks = (await get_preview(client, fid)).json()["blocks"]
    assert blocks == [
        {"type": "heading", "level": 1, "text": "T"},
        {"type": "heading", "level": 2, "text": "Two"},
        {"type": "heading", "level": 6, "text": "Six"},
        {"type": "paragraph", "text": "Plain"},
    ]


async def test_numbered_and_bulleted_lists(client):
    body = (para("one", num="1") + para("two", num="1") + para("between")
            + para("dot", num="2") + para("dot2", num="2") + para("again", num="1"))
    fid = await add_file("l.docx", uploads.DOCX, docx(body, NUMBERING))
    blocks = (await get_preview(client, fid)).json()["blocks"]
    assert blocks == [
        {"type": "list", "ordered": True, "items": ["one", "two"]},
        {"type": "paragraph", "text": "between"},
        {"type": "list", "ordered": False, "items": ["dot", "dot2"]},
        {"type": "list", "ordered": True, "items": ["again"]},
    ]


async def test_a_list_without_numbering_part_is_unordered(client):
    fid = await add_file("l.docx", uploads.DOCX, docx(para("a", num="1") + para("b", num="1")))
    blocks = (await get_preview(client, fid)).json()["blocks"]
    assert blocks == [{"type": "list", "ordered": False, "items": ["a", "b"]}]


async def test_a_nested_table_is_flattened_into_the_cell(client):
    inner = table([[para("x"), para("y")]])
    body = table([[para("h1"), para("h2")], [para("top") + inner, para("plain")]])
    fid = await add_file("t.docx", uploads.DOCX, docx(body))
    blocks = (await get_preview(client, fid)).json()["blocks"]
    assert blocks == [{"type": "table", "rows": [["h1", "h2"], ["top\nx\ny", "plain"]]}]


async def test_a_big_table_is_cut_at_100_rows_and_20_columns(client):
    rows = [[para(f"{r}-{c}") for c in range(25)] for r in range(120)]
    fid = await add_file("t.docx", uploads.DOCX, docx(table(rows)))
    body = (await get_preview(client, fid)).json()
    rows_out = body["blocks"][0]["rows"]
    assert len(rows_out) == 100 and all(len(r) == 20 for r in rows_out)
    assert body["truncated"] is True


async def test_a_long_document_is_cut(client, monkeypatch):
    monkeypatch.setattr(preview, "MAX_BLOCKS", 5)
    fid = await add_file("p.docx", uploads.DOCX,
                         docx("".join(para(f"p{i}") for i in range(10))))
    body = (await get_preview(client, fid)).json()
    assert [b["text"] for b in body["blocks"]] == ["p0", "p1", "p2", "p3", "p4"]
    assert body["truncated"] is True

    monkeypatch.setattr(preview, "MAX_BLOCKS", 2000)
    long = "y" * 150_000
    fid = await add_file("c.docx", uploads.DOCX, docx(para(long) + para(long) + para("z")))
    body = (await get_preview(client, fid)).json()
    assert sum(len(b["text"]) for b in body["blocks"]) == preview.MAX_DOC_CHARS
    assert len(body["blocks"]) == 2 and body["truncated"] is True


# ---------------------------------------------------------------------------
# Text and inline
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("media_type,language,name", [
    (uploads.TEXT, "text", "a.txt"),
    (uploads.MARKDOWN, "markdown", "a.md"),
    (uploads.CSV, "csv", "a.csv"),
    (uploads.JSON, "json", "a.json"),
])
async def test_text_kinds(client, media_type, language, name):
    fid = await add_file(name, media_type, "﻿héllo\n=1+1".encode("utf-8"))
    body = (await get_preview(client, fid)).json()
    assert body == {"kind": "text", "name": name, "language": language,
                    "text": "héllo\n=1+1", "truncated": False}


async def test_a_long_text_is_cut(client):
    fid = await add_file("a.txt", uploads.TEXT, b"x" * 200_005)
    body = (await get_preview(client, fid)).json()
    assert len(body["text"]) == 200_000 and body["truncated"] is True


@pytest.mark.parametrize("media_type,data", [
    (uploads.PNG, b"\x89PNG\r\n\x1a\n" + b"\0" * 16),
    (uploads.JPEG, b"\xff\xd8\xff\xe0" + b"\0" * 16),
    (uploads.PDF, b"%PDF-1.4\n%%EOF\n"),
])
async def test_images_and_pdf_are_inline(client, media_type, data):
    fid = await add_file("f", media_type, data)
    res = await get_preview(client, fid)
    assert res.status_code == 200
    assert res.json() == {"kind": "inline", "name": "f", "media_type": media_type,
                          "url": f"/chat/api/files/{fid}?inline=1"}
    assert res.headers["cache-control"] == "private, no-store"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
async def test_another_users_file_and_an_unknown_id_are_404(client):
    fid = await add_file("a.txt", uploads.TEXT, b"secret", owner=BOB)
    for target in (fid, "no-such-id"):
        res = await get_preview(client, target)
        assert res.status_code == 404
        assert res.json() == {"detail": routes.FILE_NOT_FOUND}
    assert (await get_preview(client, fid, user=BOB)).status_code == 200


async def test_an_unknown_media_type_is_415(client):
    fid = await add_file("a.bin", "application/zip", b"PK\x03\x04")
    res = await get_preview(client, fid)
    assert res.status_code == 415
    assert res.json() == {"detail": "No preview for this file type"}
    assert res.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("media_type,data", [
    (uploads.XLSX, b"PK\x03\x04 not a zip at all"),
    (uploads.XLSX, docx(para("a word file, not a workbook"))),
    (uploads.DOCX, b"not a zip"),
    (uploads.DOCX, docx("<w:p><w:r>unclosed")),
    (uploads.TEXT, b"\xff\xfe\x00bad"),
])
async def test_broken_bytes_are_422(client, media_type, data):
    fid = await add_file("broken", media_type, data)
    res = await get_preview(client, fid)
    assert res.status_code == 422
    assert res.json() == {"detail": "The file could not be read"}
    assert res.headers["cache-control"] == "private, no-store"


def test_build_preview_is_pure_and_refuses_with_fixed_texts():
    with pytest.raises(preview.PreviewError) as caught:
        preview.build_preview("application/x-secret-type", "n", b"data", "id")
    assert (caught.value.status, caught.value.detail) == (415, preview.MSG_NO_PREVIEW)
    with pytest.raises(preview.PreviewError) as caught:
        preview.build_preview(uploads.DOCX, "n", b"\0" * 10, "id")
    assert (caught.value.status, caught.value.detail) == (422, preview.MSG_UNREADABLE)
