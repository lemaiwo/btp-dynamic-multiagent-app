"""``export_result`` of the ``builtin:hana`` toolset: a check's or a guarded
SELECT's full result as one Excel file of the web chat conversation.

The registry fixture and the fake driver are those of
``tests/test_hanadb_tools.py``; the conversation is a ``ChatRunContext`` with
an in-memory ``save``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import logging
from decimal import Decimal
from typing import Any

import pytest
from openpyxl import load_workbook

from agents.hana import tools
from agents.hana.checks import base
from agents.hana.checks.base import Check, Statement, register
from agents.hana.connection import LOB_CUT_MARKER, HanaSystem
from agents.hana.errors import ERROR_CODES, MESSAGES
from agents.hana.export import EXPORT_ROW_CHARS_BUDGET
from agents.webchat import docgen
from tests import hanadb_helpers as h

SECRETS = (h.MARKER, h.HOST, h.USER, h.PASSWORD, h.TOKEN, "hana-dest-one", "hana-dest-two")
STATEMENT = "SELECT * FROM SAPABAP1.KNA1 WHERE KUNNR = 'customer4711' AND X = 99"
SQL = "SELECT A, B FROM SYS.M_ALERTS"

OAUTH = {"systems": [{"name": "erp_tenant", "destination": "hana-dest-one"}]}
OAUTH_SELECT = {**OAUTH, "allow_select": True}
OAUTH_ALL = {**OAUTH_SELECT, "allow_select_all": True}


@pytest.fixture(autouse=True)
def registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "_REGISTRY", {})
    register(
        Check(name="alerts", category="monitoring", purpose="Current alerts.",
              source="bin/alerts.js",
              statements=(Statement("", "SELECT A, B FROM SYS.M_ALERTS", ()),)),
        Check(name="longRunning", category="performance", purpose="Long statements.",
              source="bin/longRunning.js",
              statements=(Statement(
                  "", "SELECT A, STATEMENT_STRING FROM SYS.M_X "
                      "WHERE (? IS NULL OR SCHEMA_NAME = ?) AND H < ?",
                  ("schema", "schema", "hours")),),
              args=frozenset({"top", "schema", "hours"}),
              masked_columns=frozenset({"STATEMENT_STRING"})),
        Check(name="topped", category="performance", purpose="A capped check.",
              source="bin/topped.js",
              statements=(Statement("", "SELECT TOP ? A, B FROM SYS.M_Y", ("top_plus_one",)),),
              args=frozenset({"top"}), max_rows_cap=50),
        Check(name="backups", category="backup", purpose="Backup catalog.",
              source="bin/backups.js",
              statements=(Statement("catalog", "SELECT A FROM SYS.M_BACKUP_CATALOG", ()),
                          Statement("size", "SELECT B FROM SYS.M_BACKUP_SIZE", ()))),
    )


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> h.FakeDbapi:
    return h.install(monkeypatch)


class Saved:
    def __init__(self) -> None:
        self.files: list[tuple[str, str, bytes]] = []

    async def __call__(self, name: str, media_type: str, data: bytes) -> dict[str, Any]:
        self.files.append((name, media_type, data))
        n = len(self.files)
        return {"file_id": f"f{n}", "name": name, "media_type": media_type,
                "size": len(data), "download_url": f"/chat/api/files/f{n}"}


@pytest.fixture
def chat():
    saved = Saved()
    ctx = docgen.ChatRunContext(conversation_id="c", message_id="m", principal="p",
                                save=saved)
    token = docgen.current_chat_run.set(ctx)
    yield ctx, saved
    docgen.current_chat_run.reset(token)


def _factory(name: str, destination: str) -> HanaSystem:
    return HanaSystem(name, destination, resolver=h.FakeResolver(), tokens=h.FakeTokens())


def build(oauth: dict | None = None):
    return tools.hana_toolset(oauth or OAUTH, auth_mode="destination", system_factory=_factory)


async def export(toolset=None, /, **kw: Any) -> dict[str, Any]:
    return await (toolset or build()).tools["export_result"].function(**kw)


def sheets(data: bytes) -> dict[str, list[tuple]]:
    book = load_workbook(io.BytesIO(data))
    return {ws.title: list(ws.iter_rows(values_only=True)) for ws in book.worksheets}


def _executed(db: h.FakeDbapi) -> list[tuple]:
    return [c for c in db.calls if c[0] == "execute"]


def _clean(answer: Any, *extra: str) -> None:
    text = json.dumps(answer)
    for secret in SECRETS + extra:
        assert secret not in text


def _refusal(answer: dict, code: str, **extra: Any) -> None:
    assert answer["error"]["code"] == code
    assert answer["error"]["message"] == MESSAGES[code]
    for key, value in extra.items():
        assert answer["error"][key] == value


# ---- registration and errors ----------------------------------------------------------


def test_export_is_registered_always_and_its_codes_are_closed() -> None:
    assert "export_result" in build().tools
    assert "export_result" in build(OAUTH_SELECT).tools
    assert "export_result" in tools.TOOL_NAMES
    for code in ("no_chat", "export_too_large", "export_failed", "export_busy"):
        assert code in ERROR_CODES
    description = build().tools["export_result"].description
    assert "20" in description and "masked" in description


def test_offset_docs_point_at_export() -> None:
    for toolset, name in ((build(), "run_check"), (build(OAUTH_SELECT), "run_select")):
        assert "export_result" in toolset.tools[name].function.__doc__


async def test_export_outside_a_chat_is_no_chat(db: h.FakeDbapi) -> None:
    answer = await export(system="erp_tenant", check="alerts", filename="x")
    _refusal(answer, "no_chat")
    assert _executed(db) == []


async def test_filename_refused(db: h.FakeDbapi, chat) -> None:
    for bad in ("", "  ", None, 7, "../..."):
        answer = await export(system="erp_tenant", check="alerts", filename=bad)
        _refusal(answer, "invalid_argument", argument="filename")
    assert _executed(db) == []


async def test_export_needs_exactly_one_of_check_and_sql(db: h.FakeDbapi, chat) -> None:
    for kw in ({}, {"check": "alerts", "sql": SQL}):
        answer = await export(build(OAUTH_SELECT), system="erp_tenant", filename="x", **kw)
        _refusal(answer, "invalid_argument", argument="check",
                 rule="give exactly one of check and sql")
    assert _executed(db) == []


async def test_unknown_system_and_check(db: h.FakeDbapi, chat) -> None:
    _refusal(await export(system="nope", check="alerts", filename="x"), "unknown_system")
    _refusal(await export(system="erp_tenant", check="nope", filename="x"), "unknown_check")
    answer = await export(system="erp_tenant", check="alerts", filename="x", schema="SYS")
    _refusal(answer, "invalid_argument", argument="schema")
    for top in (0, 10_001, True, "5"):
        answer = await export(system="erp_tenant", check="alerts", filename="x", top=top)
        _refusal(answer, "invalid_argument", argument="top")
    assert _executed(db) == []


# ---- a check ----------------------------------------------------------------------------


async def test_export_check_writes_one_xlsx_and_answers_a_preview(db: h.FakeDbapi,
                                                                  chat) -> None:
    ctx, saved = chat
    db.description = (("A",), ("STATEMENT_STRING",))
    db.rows = [(i, f"SELECT {i} FROM DUMMY") for i in range(30)]
    answer = await export(system="erp_tenant", check="longRunning", filename="long")
    assert len(saved.files) == 1
    name, media_type, data = saved.files[0]
    assert (name, media_type) == ("long.xlsx", docgen.XLSX)
    assert set(answer) == {"system", "check", "file_id", "filename", "download_url",
                           "row_count", "truncated", "columns", "preview"}
    assert answer["system"] == "erp_tenant" and answer["check"] == "longRunning"
    assert answer["file_id"] == "f1" and answer["filename"] == "long.xlsx"
    assert answer["download_url"] == "/chat/api/files/f1"
    assert answer["row_count"] == 30 and answer["truncated"] is False
    assert answer["columns"] == ["A", "STATEMENT_STRING"]
    assert len(answer["preview"]) == tools.EXPORT_PREVIEW_ROWS == 20
    assert answer["preview"][0] == [0, "SELECT ? FROM DUMMY"]
    book = sheets(data)
    assert list(book) == ["longRunning"]
    rows = book["longRunning"]
    assert rows[0] == ("A", "STATEMENT_STRING")
    assert len(rows) == 31
    assert rows[30] == (29, "SELECT ? FROM DUMMY")
    assert ctx.files == [{"file_id": "f1", "name": "long.xlsx", "media_type": docgen.XLSX,
                          "size": len(data), "download_url": "/chat/api/files/f1"}]


async def test_export_masks_the_checks_masked_columns(db: h.FakeDbapi, chat) -> None:
    _, saved = chat
    db.description = (("A",), ("STATEMENT_STRING",))
    db.rows = [(1, STATEMENT), (2, None)]
    answer = await export(system="erp_tenant", check="longRunning", filename="long")
    _clean(answer, "customer4711")
    assert answer["preview"][1] == [2, None]
    text = json.dumps(sheets(saved.files[0][2]), default=str)
    assert "customer4711" not in text and "99" not in text
    assert "SAPABAP1.KNA1" in text


async def test_export_binds_export_top(monkeypatch: pytest.MonkeyPatch, chat) -> None:
    seen: list[tuple] = []

    async def recorder(system, sql, binds, max_rows, **kw):
        seen.append((sql, tuple(binds), max_rows, kw))
        return ["A", "B"], [(1, 2)], False

    monkeypatch.setattr(tools, "run_query", recorder)
    answer = await export(system="erp_tenant", check="topped", filename="t")
    assert answer["row_count"] == 1
    assert seen == [("SELECT TOP ? A, B FROM SYS.M_Y", (10_001,), 10_000,
                     {"lob_chars": 4096, "lob_budget": 5_000_000,
                      "row_chars_budget": 20_000_000, "max_columns": 100})]
    seen.clear()
    await export(system="erp_tenant", check="topped", filename="t", top=200)
    assert seen[0][1:3] == ((201,), 200)
    seen.clear()
    await export(system="erp_tenant", check="longRunning", filename="t", schema="SYS")
    assert seen[0][1:] == (("SYS", "SYS", 24), 10_000,
                           {"lob_chars": tools.EXPORT_LOB_CHARS,
                            "lob_budget": tools.EXPORT_LOB_BUDGET,
                            "row_chars_budget": EXPORT_ROW_CHARS_BUDGET,
                            "max_columns": 100})


async def test_export_composite_check_writes_one_sheet_per_part(db: h.FakeDbapi,
                                                                chat) -> None:
    _, saved = chat
    db.description = (("A",),)
    db.rows = [(1,), (2,)]
    answer = await export(system="erp_tenant", check="backups", filename="b")
    assert set(answer) == {"system", "check", "file_id", "filename", "download_url", "parts"}
    assert [p["label"] for p in answer["parts"]] == ["catalog", "size"]
    for part in answer["parts"]:
        assert part == {"label": part["label"], "columns": ["A"], "row_count": 2,
                        "truncated": False, "preview": [[1], [2]]}
    book = sheets(saved.files[0][2])
    assert list(book) == ["catalog", "size"]
    assert book["size"] == [("A",), (1,), (2,)]


async def test_export_names_cut_columns(db: h.FakeDbapi, chat) -> None:
    _, saved = chat
    db.description = (("A",), ("TEXT",))
    db.rows = [(1, h.FakeLob("x" * 5000, db=db)), (2, "short")]
    answer = await export(system="erp_tenant", check="alerts", filename="c")
    assert answer["truncated_columns"] == ["TEXT"]
    rows = sheets(saved.files[0][2])["alerts"]
    assert rows[1][1].endswith(LOB_CUT_MARKER)
    assert rows[1][1].startswith("x" * 4096)


# ---- a SELECT ---------------------------------------------------------------------------


async def test_export_sql_needs_allow_select(db: h.FakeDbapi, chat) -> None:
    answer = await export(system="erp_tenant", sql=SQL, filename="x")
    _refusal(answer, "invalid_argument", argument="sql",
             rule="free SQL is not allowed for this agent")
    assert _executed(db) == []


async def test_export_sql_uses_the_guard_and_the_switch(monkeypatch: pytest.MonkeyPatch,
                                                        db: h.FakeDbapi, chat) -> None:
    _, saved = chat
    seen: list[tuple] = []
    real = tools.check_select

    def spy(sql, **kw):
        seen.append((sql, kw))
        return real(sql, **kw)

    monkeypatch.setattr(tools, "check_select", spy)
    db.rows = [(1, STATEMENT)]
    answer = await export(build(OAUTH_SELECT), system="erp_tenant", sql=SQL, filename="s")
    assert seen == [(SQL, {"any_schema": False})]
    assert set(answer) == {"system", "file_id", "filename", "download_url", "row_count",
                           "truncated", "columns", "preview"}
    assert answer["preview"] == [[1, STATEMENT]]  # a SELECT is unmasked
    assert _executed(db)[0][1:] == (SQL,)
    assert list(sheets(saved.files[0][2])) == ["Result"]

    seen.clear()
    other = "SELECT A FROM SAPABAP1.KNA1"
    answer = await export(build(OAUTH_ALL), system="erp_tenant", sql=other, filename="s")
    assert seen == [(other, {"any_schema": True})]
    assert "error" not in answer

    db.calls.clear()
    answer = await export(build(OAUTH_SELECT), system="erp_tenant", sql=other, filename="s")
    _refusal(answer, "select_refused")
    assert answer["error"]["rule"]
    assert _executed(db) == []
    _clean(answer, "KNA1")


async def test_export_sql_refuses_check_arguments(db: h.FakeDbapi, chat) -> None:
    answer = await export(build(OAUTH_SELECT), system="erp_tenant", sql=SQL, filename="x",
                          schema="SYS")
    _refusal(answer, "invalid_argument", argument="schema")
    assert _executed(db) == []


# ---- caps -------------------------------------------------------------------------------


async def test_export_truncated_and_caps(db: h.FakeDbapi, chat,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, saved = chat
    db.rows = [(i, "r") for i in range(10_001)]
    answer = await export(system="erp_tenant", check="alerts", filename="big")
    assert answer["row_count"] == 10_000 and answer["truncated"] is True
    assert len(sheets(saved.files[0][2])["alerts"]) == 10_001

    db.description = tuple((f"C{i}",) for i in range(101))
    db.rows = [tuple(range(101))]
    _refusal(await export(system="erp_tenant", check="alerts", filename="wide"),
             "export_too_large")

    monkeypatch.setattr(docgen, "MAX_CELLS", 50)
    db.description = (("A",), ("B",))
    db.rows = [(i, i) for i in range(40)]
    answer = await export(system="erp_tenant", check="alerts", filename="cells")
    assert answer["row_count"] == 24 and answer["truncated"] is True
    assert len(sheets(saved.files[-1][2])["alerts"]) == 25
    monkeypatch.setattr(docgen, "MAX_CELLS", 500_000)

    monkeypatch.setattr(docgen, "MAX_FILE_BYTES", 100)
    stored = len(saved.files)
    _refusal(await export(system="erp_tenant", check="alerts", filename="bytes"),
             "export_too_large")
    assert len(saved.files) == stored
    monkeypatch.setattr(docgen, "MAX_FILE_BYTES", 10 * 1024 * 1024)

    ctx.created = docgen.MAX_FILES_PER_RUN
    _refusal(await export(system="erp_tenant", check="alerts", filename="many"),
             "export_failed")
    assert len(saved.files) == stored


async def test_a_failing_save_is_export_failed(db: h.FakeDbapi,
                                               caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    async def broken(name: str, media_type: str, data: bytes) -> dict[str, Any]:
        raise RuntimeError(h.MARKER)

    ctx = docgen.ChatRunContext(conversation_id="c", message_id="m", principal="p",
                                save=broken)
    token = docgen.current_chat_run.set(ctx)
    try:
        db.rows = [(1, "secret-row-value")]
        answer = await export(system="erp_tenant", check="alerts", filename="x")
    finally:
        docgen.current_chat_run.reset(token)
    _refusal(answer, "export_failed")
    for record in caplog.records:
        assert h.MARKER not in record.getMessage()
        assert "secret-row-value" not in record.getMessage()


async def test_a_preview_over_the_cap_is_dropped_not_the_file(db: h.FakeDbapi,
                                                              chat) -> None:
    _, saved = chat
    db.rows = [(i, "y" * 4000) for i in range(30)]
    answer = await export(system="erp_tenant", check="alerts", filename="wide")
    assert answer["preview"] == [] and answer["preview_omitted"] is True
    assert answer["row_count"] == 30
    assert len(saved.files) == 1


async def test_export_cells(db: h.FakeDbapi, chat) -> None:
    _, saved = chat
    db.description = tuple((c,) for c in "ABCDEFGH")
    db.rows = [("=HYPERLINK(\"http://x\")", b"\x00\x01\x02", dt.datetime(2026, 11, 10, 8, 30),
                dt.date(2026, 11, 10), Decimal("1.25"), Decimal("12345678901234567890.1"),
                7, 2.5)]
    answer = await export(system="erp_tenant", check="alerts", filename="cells")
    assert answer["preview"][0][:4] == ["=HYPERLINK(\"http://x\")", "<3 bytes>",
                                        "2026-11-10T08:30:00", "2026-11-10"]
    book = load_workbook(io.BytesIO(saved.files[0][2]))
    ws = book["alerts"]
    assert ws["A2"].data_type == "s" and ws["A2"].value == "=HYPERLINK(\"http://x\")"
    assert [c.value for c in ws[2]][1:] == ["<3 bytes>", "2026-11-10T08:30:00",
                                            "2026-11-10", 1.25,
                                            "12345678901234567890.1", 7, 2.5]


def test_excel_value() -> None:
    assert tools._excel_value(Decimal("1.25")) == 1.25
    assert tools._excel_value(Decimal("0.1")) == 0.1
    assert tools._excel_value(Decimal("12345678901234567890.1")) == "12345678901234567890.1"
    assert tools._excel_value(Decimal("NaN")) == "NaN"
    assert tools._excel_value(Decimal("Infinity")) == "Infinity"
    assert tools._excel_value(float("inf")) == "inf"
    assert tools._excel_value(3) == 3 and tools._excel_value(True) is True
    assert tools._excel_value(None) is None


# ---- one at a time ----------------------------------------------------------------------


async def test_one_export_at_a_time_per_toolset(monkeypatch: pytest.MonkeyPatch,
                                                chat) -> None:
    gate = asyncio.Event()
    started = asyncio.Event()

    async def slow(system, sql, binds, max_rows, **kw):
        started.set()
        await gate.wait()
        return ["A"], [(1,)], False

    monkeypatch.setattr(tools, "run_query", slow)
    toolset = build()
    first = asyncio.create_task(export(toolset, system="erp_tenant", check="alerts",
                                       filename="one"))
    await asyncio.wait_for(started.wait(), 5)
    second = await export(toolset, system="erp_tenant", check="alerts", filename="two")
    _refusal(second, "export_busy")
    other = asyncio.create_task(export(build(), system="erp_tenant", check="alerts",
                                       filename="other"))
    await asyncio.sleep(0)
    gate.set()
    assert "error" not in await first
    assert "error" not in await other
    assert "error" not in await export(toolset, system="erp_tenant", check="alerts",
                                       filename="three")


# ---- nothing leaks ----------------------------------------------------------------------


async def test_no_value_or_sql_in_errors_or_logs(db: h.FakeDbapi, chat,
                                                 caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    secret_sql = "SELECT A FROM SAPABAP1.SECRET_TABLE_4711"
    answers = [
        await export(build(OAUTH_SELECT), system="erp_tenant", sql=secret_sql, filename="x"),
        await export(system="erp_tenant", sql=secret_sql, filename="x"),
        await export(system="system_4711", check="alerts", filename="x"),
        await export(system="erp_tenant", check="check_4711", filename="x"),
        await export(system="erp_tenant", check="alerts", filename="x", top="4711"),
    ]
    db.fail_at["execute"] = h.driver_error(258, "insufficient privilege")
    answers.append(await export(system="erp_tenant", check="alerts", filename="x"))
    del db.fail_at["execute"]
    db.description = tuple((f"C{i}",) for i in range(101))
    db.rows = [tuple(f"row-4711-{i}" for i in range(101))]
    answers.append(await export(system="erp_tenant", check="alerts", filename="x"))
    for answer in answers:
        assert "error" in answer
        assert answer["error"]["code"] in ERROR_CODES
        _clean(answer, "4711", "SECRET_TABLE")
    for record in caplog.records:
        message = record.getMessage()
        for secret in SECRETS + ("4711", "SECRET_TABLE"):
            assert secret not in message
        assert record.exc_info is None


# ---- deferred minors of the export review ------------------------------------------------


async def test_an_answer_too_large_without_previews_still_answers_its_file(
        db: h.FakeDbapi, chat) -> None:
    """Columns and ``truncated_columns`` alone can pass the cap once the file is
    stored: the call then answers the file with counts only, never
    ``result_too_large`` (the model would narrow a query whose export exists)."""
    _, saved = chat
    register(Check(name="wide", category="monitoring", purpose="Three wide parts.",
                   source="bin/wide.js",
                   statements=tuple(Statement(f"part{i}", f"SELECT * FROM SYS.M_W{i}", ())
                                    for i in range(3))))
    names = [f"C{i:03d}".ljust(tools.MAX_COLUMN_NAME_CHARS, "x") for i in range(100)]
    db.description = tuple((n,) for n in names)
    db.rows = [tuple(f"v{LOB_CUT_MARKER}" for _ in names)] * 2
    answer = await export(system="erp_tenant", check="wide", filename="wide")
    assert "error" not in answer
    assert len(saved.files) == 1
    assert set(answer) == {"system", "check", "file_id", "filename", "download_url",
                           "row_count", "truncated", "column_count", "preview_omitted"}
    assert answer["check"] == "wide" and answer["file_id"] == "f1"
    assert answer["row_count"] == 6 and answer["truncated"] is False
    assert answer["column_count"] == 300 and answer["preview_omitted"] is True
    assert tools.activity_summary("export_result", answer) == "export_result: 6 rows"
    assert list(sheets(saved.files[0][2])) == ["part0", "part1", "part2"]


async def test_non_finite_numbers_are_exported_as_text(db: h.FakeDbapi, chat) -> None:
    _, saved = chat
    db.description = (("A",), ("B",), ("C",), ("D",))
    db.rows = [(float("nan"), float("inf"), float("-inf"), 1.5)]
    answer = await export(system="erp_tenant", check="alerts", filename="nan")
    assert "error" not in answer
    assert answer["preview"] == [["nan", "inf", "-inf", 1.5]]
    ws = load_workbook(io.BytesIO(saved.files[0][2]))["alerts"]
    assert [c.value for c in ws[2]] == ["nan", "inf", "-inf", 1.5]
    assert [c.data_type for c in ws[2]][:3] == ["s", "s", "s"]
    for value, text in ((float("nan"), "nan"), (float("inf"), "inf"),
                        (float("-inf"), "-inf")):
        assert tools._excel_value(value) == text == tools.json_value(value)


async def test_the_export_lock_is_released_after_a_failure_and_a_cancel(
        monkeypatch: pytest.MonkeyPatch, db: h.FakeDbapi, chat) -> None:
    toolset = build()
    db.fail_at["execute"] = h.driver_error(258, "insufficient privilege")
    _refusal(await export(toolset, system="erp_tenant", check="alerts", filename="x"),
             "not_authorized")
    del db.fail_at["execute"]
    assert "error" not in await export(toolset, system="erp_tenant", check="alerts",
                                       filename="after_failure")

    started = asyncio.Event()

    async def hangs(system, sql, binds, max_rows, **kw):
        started.set()
        await asyncio.Event().wait()

    real = tools.run_query
    monkeypatch.setattr(tools, "run_query", hangs)
    task = asyncio.create_task(export(toolset, system="erp_tenant", check="alerts",
                                      filename="cancelled"))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(tools, "run_query", real)
    assert "error" not in await export(toolset, system="erp_tenant", check="alerts",
                                       filename="after_cancel")


# ---- one long cell never fails the export (task F4) ------------------------------------


async def test_a_cell_over_the_workbook_limit_is_cut_and_named(
        monkeypatch: pytest.MonkeyPatch, db: h.FakeDbapi, chat) -> None:
    from agents.hana import export as export_module
    _, saved = chat
    monkeypatch.setattr(export_module, "EXPORT_LOB_CHARS", 50_000)
    db.description = (("A",), ("TEXT",))
    db.rows = [(1, "x" * 40_000), (2, "short")]
    answer = await export(build(OAUTH_SELECT), system="erp_tenant", sql=SQL, filename="long")
    assert "error" not in answer
    assert answer["truncated_columns"] == ["TEXT"]
    rows = sheets(saved.files[0][2])["Result"]
    assert len(rows[1][1]) == docgen.MAX_CELL_CHARS
    assert rows[1][1].endswith(LOB_CUT_MARKER)
    assert rows[1][1].startswith("x" * 30_000)
    assert rows[2][1] == "short"


async def test_a_cell_at_the_workbook_limit_is_unchanged(
        monkeypatch: pytest.MonkeyPatch, db: h.FakeDbapi, chat) -> None:
    from agents.hana import export as export_module
    _, saved = chat
    monkeypatch.setattr(export_module, "EXPORT_LOB_CHARS", 50_000)
    db.description = (("A",), ("TEXT",))
    db.rows = [(1, "x" * docgen.MAX_CELL_CHARS)]
    answer = await export(build(OAUTH_SELECT), system="erp_tenant", sql=SQL, filename="edge")
    assert "truncated_columns" not in answer
    assert sheets(saved.files[0][2])["Result"][1][1] == "x" * docgen.MAX_CELL_CHARS


async def test_a_store_error_is_logged_by_code_and_is_export_failed(
        monkeypatch: pytest.MonkeyPatch, db: h.FakeDbapi, chat,
        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    async def refusing(ctx, name, media_type, build):  # noqa: ARG001
        return {"error": {"code": "invalid_argument",
                          "message": "A cell holds a number that is not finite."}}

    monkeypatch.setattr(docgen, "store_file", refusing)
    db.rows = [(1, "secret-row-value")]
    answer = await export(system="erp_tenant", check="alerts", filename="x")
    _refusal(answer, "export_failed")
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "invalid_argument" in warnings[0] and "store" in warnings[0]
    assert "A cell holds a number that is not finite." in warnings[0]
    assert "secret-row-value" not in warnings[0]
