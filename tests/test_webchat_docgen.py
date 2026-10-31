"""The web chat's document tools and markdown subset (``agents/webchat/docgen.py``,
``agents/webchat/md.py``).

Each generator's file is opened again: the ``.docx`` with this app's own
reader (``uploads.extract_docx``; python-docx is not a dependency, it would
bring lxml) plus a well-formedness check of every XML part, the PDF with
reportlab's reader-free check of its header and trailer, the workbook with
openpyxl.

Run:  .venv/bin/python -m pytest tests/test_webchat_docgen.py -q
"""

from __future__ import annotations

import io
import os
import sys
import time
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)

from openpyxl import load_workbook  # noqa: E402

from agents.webchat import docgen, md, uploads  # noqa: E402
from agents.webchat.docgen import SheetSpec  # noqa: E402

SAMPLE = """# Report **Q3**

Intro with *italic*, **bold** and `code`, a [link](https://example.test).

## Items
- first
- second
  - nested
1. one
2. two

| Name | Value |
|------|------:|
| a    | 1     |
| b \\| c | 2 |

```python
print("<hello> & bye")
```

---

The end \x01 with a control character.
"""


# ---------------------------------------------------------------------------
# Markdown subset
# ---------------------------------------------------------------------------
def test_the_markdown_subset_is_parsed_into_blocks():
    blocks = md.parse(SAMPLE)
    kinds = [type(b).__name__ for b in blocks]
    assert kinds == ["Heading", "Paragraph", "Heading", "ListBlock", "ListBlock", "Table",
                     "CodeBlock", "Rule", "Paragraph"]
    head = blocks[0]
    assert head.level == 1 and head.spans[1] == md.Span("Q3", bold=True)
    para = blocks[1].spans
    assert md.Span("italic", italic=True) in para and md.Span("code", code=True) in para
    assert "link (https://example.test)" in md.plain(para)
    bullets, numbers = blocks[3], blocks[4]
    assert not bullets.ordered and [i.level for i in bullets.items] == [0, 0, 1]
    assert numbers.ordered and md.plain(numbers.items[1].spans) == "two"
    table = blocks[5]
    assert [md.plain(c) for c in table.header] == ["Name", "Value"]
    assert [md.plain(c) for c in table.rows[1]] == ["b | c", "2"]
    assert blocks[6].language == "python" and "<hello>" in blocks[6].text


def test_snake_case_and_unclosed_markers_stay_text():
    spans = md.parse_inline("a snake_case_name and *unclosed and **also")
    assert md.plain(spans) == "a snake_case_name and *unclosed and **also"
    assert all(not s.bold and not s.italic for s in spans)


def test_raw_html_is_text():
    (para,) = md.parse("<script>alert(1)</script>")
    assert md.plain(para.spans) == "<script>alert(1)</script>"


def test_pathological_markdown_is_linear_enough():
    started = time.monotonic()
    for text in ("*a " * 60_000, "**" + "a" * 190_000, "- " * 90_000 + "x",
                 "[" * 150_000, "|" * 150_000 + "\n|---|"):
        md.parse(text)
    assert time.monotonic() - started < 5


# ---------------------------------------------------------------------------
# File names
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("report", "report.docx"),
    ("Report.DOCX", "Report.docx"),
    ("../../etc/passwd", "passwd.docx"),
    ("C:\\temp\\x.pdf", "x.docx"),
    ("a\x00b\u202ec<>:|?*.docx", "abc______.docx"),
    ("  ..hidden.. ", "hidden.docx"),
    ("x" * 300, "x" * 95 + ".docx"),
])
def test_file_names_are_cleaned(raw, expected):
    assert docgen.clean_filename(raw, ".docx") == expected


@pytest.mark.parametrize("raw", ["", "  ", "...", "/", 5, None, "\x00\x01"])
def test_an_unusable_name_is_none(raw):
    assert docgen.clean_filename(raw, ".pdf") is None


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------
def test_word_reopens_and_holds_the_text():
    data = docgen.build_docx(SAMPLE, title="Quarterly \x02report")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.testzip() is None
        names = set(zf.namelist())
        assert {"[Content_Types].xml", "word/document.xml", "word/styles.xml"} <= names
        for name in names:
            ElementTree.fromstring(zf.read(name))  # every part is well-formed XML
        document = zf.read("word/document.xml").decode()
    assert 'w:val="Heading1"' in document and "<w:b/>" in document
    assert '<w:br w:type="page"/>' in document and "<w:tbl>" in document
    text = uploads.extract_docx(data)
    assert text.startswith("# Quarterly report\n# Report Q3")
    assert "Name | Value" in text and "b | c | 2" in text
    assert 'print("<hello> & bye")' in text
    assert "\x01" not in text and "The end  with a control character." in text


def test_pdf_is_a_pdf_and_replaces_what_latin1_cannot_draw():
    data = docgen.build_pdf(SAMPLE + "\nZürich € 漢字", title="T")
    assert data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")
    assert docgen.latin1("Zürich € 漢字") == "Zürich ? ??"


def test_an_empty_document_still_builds():
    assert docgen.build_pdf("").startswith(b"%PDF-")
    assert zipfile.ZipFile(io.BytesIO(docgen.build_docx(""))).testzip() is None


def test_workbook_has_a_bold_frozen_filtered_header_and_formulas_as_text():
    data = docgen.build_xlsx([
        SheetSpec(name="Sales: Q3/Q4", columns=["Region", "Amount", "Ok", "Note"],
                  rows=[["=SUM(A1:A9)", 12.5, True, None], ["North", 3, False, "+1"]]),
        SheetSpec(name="Sales: Q3/Q4", columns=["x"], rows=[]),
    ])
    book = load_workbook(io.BytesIO(data))
    assert book.sheetnames == ["Sales_ Q3_Q4", "Sales_ Q3_Q4 (2)"]
    ws = book["Sales_ Q3_Q4"]
    assert ws["A1"].value == "Region" and ws["A1"].font.b
    assert ws.freeze_panes == "A2" and ws.auto_filter.ref == "A1:D3"
    assert ws["A2"].value == "=SUM(A1:A9)" and ws["A2"].data_type == "s"
    assert ws["B2"].value == 12.5 and ws["C2"].value is True and ws["D2"].value is None
    assert ws.column_dimensions["A"].width == len("=SUM(A1:A9)") + 2


@pytest.mark.parametrize("sheets,message", [
    ([], "At least one sheet"),
    ([SheetSpec(name="s", columns=["a"])] * (docgen.MAX_SHEETS + 1), "sheets"),
    ([SheetSpec(name="s", columns=["a"] * (docgen.MAX_COLUMNS + 1))], "columns"),
    ([SheetSpec(name="s", columns=["a"], rows=[[1]] * (docgen.MAX_ROWS + 1))], "rows"),
    ([SheetSpec(name="s", columns=["a"], rows=[[float("nan")]])], "not finite"),
])
def test_workbook_caps(sheets, message):
    with pytest.raises(docgen.SheetError, match=message):
        docgen.build_xlsx(sheets)


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------
class Saved:
    def __init__(self) -> None:
        self.files: list[tuple[str, str, bytes]] = []

    async def __call__(self, name: str, media_type: str, data: bytes) -> dict[str, Any]:
        self.files.append((name, media_type, data))
        n = len(self.files)
        return {"file_id": f"f{n}", "name": name, "media_type": media_type,
                "size": len(data), "download_url": f"/chat/api/files/f{n}"}


@pytest.fixture
def run_ctx():
    saved = Saved()
    ctx = docgen.ChatRunContext(conversation_id="c", message_id="m", principal="p",
                                save=saved)
    token = docgen.current_chat_run.set(ctx)
    yield ctx, saved
    docgen.current_chat_run.reset(token)


async def test_the_tools_store_through_the_run_context(run_ctx):
    ctx, saved = run_ctx
    out = await docgen.create_word_document("../x/notes", "# Hi", title="T")
    assert out == {"file_id": "f1", "name": "notes.docx", "media_type": docgen.DOCX,
                   "size": len(saved.files[0][2]), "download_url": "/chat/api/files/f1"}
    out = await docgen.create_pdf_document("notes", "text")
    assert out["name"] == "notes.pdf" and saved.files[1][2].startswith(b"%PDF-")
    out = await docgen.create_excel_workbook(
        "data.csv", [SheetSpec(name="a", columns=["x"], rows=[["=1"]])])
    assert out["name"] == "data.xlsx" and ctx.created == 3


async def test_the_tools_answer_errors_instead_of_raising(run_ctx, monkeypatch):
    ctx, saved = run_ctx
    assert (await docgen.create_word_document("", "x"))["error"]["code"] == "invalid_name"
    big = "x" * (docgen.MAX_MARKDOWN_CHARS + 1)
    assert (await docgen.create_pdf_document("a", big))["error"]["code"] == "too_large"
    out = await docgen.create_excel_workbook("a", [])
    assert out["error"]["code"] == "invalid_argument"
    monkeypatch.setattr(docgen, "MAX_FILE_BYTES", 10)
    assert (await docgen.create_word_document("a", "# x"))["error"]["code"] == "too_large"
    monkeypatch.setattr(docgen, "build_pdf", lambda *a: 1 / 0)
    out = await docgen.create_pdf_document("a", "x")
    assert out["error"]["code"] == "invalid_argument" and "division" not in str(out)
    assert saved.files == [] and ctx.created == 0


async def test_at_most_ten_files_per_run(run_ctx):
    ctx, saved = run_ctx
    for _ in range(docgen.MAX_FILES_PER_RUN):
        assert "file_id" in await docgen.create_word_document("a", "x")
    out = await docgen.create_word_document("a", "x")
    assert out["error"]["code"] == "too_many_files"
    assert len(saved.files) == docgen.MAX_FILES_PER_RUN


async def test_without_a_web_chat_run_nothing_is_created():
    out = await docgen.create_word_document("a", "x")
    assert out["error"]["code"] == "invalid_argument"


def test_the_toolset_names_the_three_tools():
    toolset = docgen.docgen_toolset()
    assert sorted(toolset.tools) == sorted(docgen.TOOL_NAMES)
    assert "download_url" in toolset.tools["create_word_document"].description
