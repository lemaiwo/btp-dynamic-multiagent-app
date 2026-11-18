"""The row-character budget of ``run_query`` (``row_chars_budget``) and the
export that passes it: a wide result stops while it is fetched, before it is
held, copied or written to a workbook.

Final review finding 1 of the ``builtin:hana`` admin SQL change: an export was
bounded by rows and LOB characters, not by plain cell size.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from agents.hana import connection, export, reading, tools
from agents.hana.connection import HanaSystem, run_query
from agents.hana.errors import MESSAGES, HanaError
from agents.webchat import docgen
from tests import hanadb_helpers as h

OAUTH = {"systems": [{"name": "erp_tenant", "destination": "hana-dest-one"}]}
OAUTH_SELECT = {**OAUTH, "allow_select": True}


def _system() -> HanaSystem:
    return HanaSystem("erp_tenant", "hana-test", resolver=h.FakeResolver(),
                      tokens=h.FakeTokens())


def _fetches(db: h.FakeDbapi) -> list[int]:
    return [c[1] for c in db.calls if c[0] == "fetchmany"]


def _ends_clean(db: h.FakeDbapi) -> None:
    steps = db.steps()
    assert steps[-3:] == ["rollback", "cursor_close", "close"]
    assert "commit" not in steps


# ---- run_query --------------------------------------------------------------------------


async def test_without_a_budget_one_fetch_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    db = h.install(monkeypatch, h.FakeDbapi(rows=[(i, "x" * 100) for i in range(1200)]))
    _, rows, truncated = await run_query(_system(), "SELECT A, B FROM SYS.M_X", [], 1000)
    assert _fetches(db) == [1001]
    assert len(rows) == 1000 and truncated is True


async def test_a_budget_fetches_in_chunks_and_keeps_the_result(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reading, "ROW_FETCH_CHUNK", 2)
    rows_in = [(i, "ab", b"xyz") for i in range(7)]
    db = h.install(monkeypatch, h.FakeDbapi(rows=rows_in))
    db.description = (("A",), ("B",), ("C",))
    _, rows, truncated = await run_query(_system(), "SELECT A, B, C FROM SYS.M_X", [], 5,
                                         row_chars_budget=1000)
    assert _fetches(db) == [2, 2, 2]  # 6 = max_rows + 1, then no further fetch
    assert rows == rows_in[:5] and truncated is True
    _ends_clean(db)


async def test_a_budget_stops_at_a_short_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reading, "ROW_FETCH_CHUNK", 2)
    db = h.install(monkeypatch, h.FakeDbapi(rows=[(1, "a"), (2, "b"), (3, "c")]))
    _, rows, truncated = await run_query(_system(), "SELECT A, B FROM SYS.M_X", [], 10,
                                         row_chars_budget=1000)
    assert _fetches(db) == [2, 2]
    assert len(rows) == 3 and truncated is False


async def test_the_default_chunk_is_500(monkeypatch: pytest.MonkeyPatch) -> None:
    assert reading.ROW_FETCH_CHUNK == connection.ROW_FETCH_CHUNK == 500
    db = h.install(monkeypatch, h.FakeDbapi(rows=[(1, "a")] * 1200))
    _, rows, _ = await run_query(_system(), "SELECT A, B FROM SYS.M_X", [], 1000,
                                 row_chars_budget=10**9)
    assert _fetches(db) == [500, 500, 1]
    assert len(rows) == 1000


async def test_over_the_budget_stops_the_fetch_and_is_result_too_large(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(reading, "ROW_FETCH_CHUNK", 2)
    secret = "row-value-4711"
    db = h.install(monkeypatch, h.FakeDbapi(rows=[(i, secret * 3) for i in range(20)]))
    with pytest.raises(HanaError) as info:
        # 42 characters a row: the budget of 100 passes in the second chunk.
        await run_query(_system(), "SELECT A, B FROM SYS.M_X", [], 15,
                        row_chars_budget=100)
    assert info.value.code == "result_too_large"
    assert _fetches(db) == [2, 2]  # nothing fetched after the budget was passed
    _ends_clean(db)
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert record.exc_info is None


async def test_bytes_count_and_other_values_do_not(monkeypatch: pytest.MonkeyPatch) -> None:
    h.install(monkeypatch, h.FakeDbapi(rows=[(10**30, b"12345")]))
    with pytest.raises(HanaError):
        await run_query(_system(), "SELECT A, B FROM SYS.M_X", [], 5, row_chars_budget=4)
    h.install(monkeypatch, h.FakeDbapi(rows=[(10**30, b"1234")]))
    _, rows, _ = await run_query(_system(), "SELECT A, B FROM SYS.M_X", [], 5,
                                 row_chars_budget=4)
    assert rows == [(10**30, b"1234")]


# ---- the export -------------------------------------------------------------------------


class _Saved:
    def __init__(self) -> None:
        self.files: list[tuple[str, str, bytes]] = []

    async def __call__(self, name: str, media_type: str, data: bytes) -> dict[str, Any]:
        self.files.append((name, media_type, data))
        return {"file_id": "f1", "name": name, "media_type": media_type,
                "size": len(data), "download_url": "/chat/api/files/f1"}


@pytest.fixture
def saved():
    keep = _Saved()
    ctx = docgen.ChatRunContext(conversation_id="c", message_id="m", principal="p",
                                save=keep)
    token = docgen.current_chat_run.set(ctx)
    yield keep
    docgen.current_chat_run.reset(token)


def _factory(name: str, destination: str) -> HanaSystem:
    return HanaSystem(name, destination, resolver=h.FakeResolver(), tokens=h.FakeTokens())


def _toolset(oauth: dict):
    return tools.hana_toolset(oauth, auth_mode="destination", system_factory=_factory)


async def _export(**kw: Any) -> dict[str, Any]:
    return await _toolset(OAUTH_SELECT).tools["export_result"].function(**kw)


def test_the_export_budget() -> None:
    assert export.EXPORT_ROW_CHARS_BUDGET == 20_000_000


async def test_an_export_over_the_budget_is_export_too_large_and_stores_nothing(
        monkeypatch: pytest.MonkeyPatch, saved: _Saved) -> None:
    monkeypatch.setattr(export, "EXPORT_ROW_CHARS_BUDGET", 1000)
    monkeypatch.setattr(reading, "ROW_FETCH_CHUNK", 10)
    db = h.install(monkeypatch, h.FakeDbapi(rows=[(i, "w" * 60) for i in range(100)]))
    answer = await _export(system="erp_tenant", sql="SELECT A, B FROM SYS.M_ALERTS",
                           filename="wide")
    assert answer == {"error": {"code": "export_too_large",
                                "message": MESSAGES["export_too_large"],
                                "hint": "select fewer columns or rows"}}
    assert saved.files == []
    # 61 characters a row: 1,000 is passed in the second chunk of 10.
    assert _fetches(db) == [10, 10]
    _ends_clean(db)


async def test_an_export_under_the_budget_is_unchanged(
        monkeypatch: pytest.MonkeyPatch, saved: _Saved) -> None:
    db = h.install(monkeypatch, h.FakeDbapi(rows=[(i, "r") for i in range(3)]))
    answer = await _export(system="erp_tenant", sql="SELECT A, B FROM SYS.M_ALERTS",
                           filename="small")
    assert answer["row_count"] == 3 and answer["truncated"] is False
    assert len(saved.files) == 1
    assert _fetches(db) == [500]  # one chunk, three rows: short, so done


async def test_the_export_passes_the_budget_and_run_check_and_run_select_do_not(
        monkeypatch: pytest.MonkeyPatch, saved: _Saved) -> None:
    seen: list[dict[str, Any]] = []

    async def recorder(system, sql, binds, max_rows, **kw):
        seen.append(kw)
        return ["A", "B"], [(1, 2)], False

    monkeypatch.setattr(tools, "run_query", recorder)
    toolset = _toolset(OAUTH_SELECT)
    await toolset.tools["export_result"].function(
        system="erp_tenant", sql="SELECT A, B FROM SYS.M_ALERTS", filename="t")
    assert seen[-1]["row_chars_budget"] == export.EXPORT_ROW_CHARS_BUDGET
    seen.clear()
    await toolset.tools["run_select"].function(system="erp_tenant",
                                               sql="SELECT A, B FROM SYS.M_ALERTS")
    await toolset.tools["run_check"].function(system="erp_tenant", check="alerts")
    assert len(seen) >= 2 and all("row_chars_budget" not in kw for kw in seen)


# ---- final review finding 2: the refusal text of the guard ----------------------------


def test_select_refused_does_not_claim_monitoring_views_only() -> None:
    # An entry with allow_select_all reads every schema: the text must hold there too.
    assert MESSAGES["select_refused"] == "The statement is not a read the guard allows."


# ---- fix re-review Minor A: the column cap before the read ----------------------------


def _wide(count: int) -> tuple:
    return tuple((f"C{i}",) for i in range(count))


async def test_max_columns_refuses_before_any_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    db = h.install(monkeypatch, h.FakeDbapi(rows=[(1, 2, 3)]))
    db.description = _wide(3)
    with pytest.raises(HanaError) as info:
        await run_query(_system(), "SELECT * FROM SYS.M_X", [], 5, max_columns=2)
    assert info.value.code == "result_too_large"
    assert _fetches(db) == []  # nothing fetched: the description alone decided
    _ends_clean(db)
    # The login worked: the call is recorded as any call past connect.
    state = connection._logins[connection._accounts["hana-test"]]
    assert state.confirmed is not None


async def test_max_columns_at_the_cap_reads_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    db = h.install(monkeypatch, h.FakeDbapi(rows=[(1, 2, 3)]))
    db.description = _wide(3)
    columns, rows, _ = await run_query(_system(), "SELECT * FROM SYS.M_X", [], 5,
                                       max_columns=3)
    assert columns == ["C0", "C1", "C2"] and rows == [(1, 2, 3)]
    assert _fetches(db) == [6]


@pytest.mark.parametrize("bad", [-1, True, 1.5, "100"])
async def test_max_columns_must_be_a_whole_number(monkeypatch: pytest.MonkeyPatch,
                                                  bad: Any) -> None:
    db = h.install(monkeypatch, h.FakeDbapi(rows=[(1, 2)]))
    with pytest.raises(HanaError) as info:
        await run_query(_system(), "SELECT A, B FROM SYS.M_X", [], 5, max_columns=bad)
    assert info.value.code == "invalid_argument"
    assert db.calls == []


async def test_an_export_of_101_columns_is_refused_before_it_reads(
        monkeypatch: pytest.MonkeyPatch, saved: _Saved) -> None:
    db = h.install(monkeypatch, h.FakeDbapi(rows=[tuple(range(101))] * 3))
    db.description = _wide(101)
    answer = await _export(system="erp_tenant", sql="SELECT * FROM SYS.M_ALERTS",
                           filename="wide")
    assert answer == {"error": {"code": "export_too_large",
                                "message": MESSAGES["export_too_large"],
                                "hint": "select fewer columns or rows"}}
    assert saved.files == []
    assert _fetches(db) == []
    _ends_clean(db)


async def test_the_export_passes_max_columns_and_run_check_and_run_select_do_not(
        monkeypatch: pytest.MonkeyPatch, saved: _Saved) -> None:
    seen: list[dict[str, Any]] = []

    async def recorder(system, sql, binds, max_rows, **kw):
        seen.append(kw)
        return ["A", "B"], [(1, 2)], False

    monkeypatch.setattr(tools, "run_query", recorder)
    toolset = _toolset(OAUTH_SELECT)
    await toolset.tools["export_result"].function(
        system="erp_tenant", sql="SELECT A, B FROM SYS.M_ALERTS", filename="t")
    assert seen[-1]["max_columns"] == docgen.MAX_COLUMNS == 100
    seen.clear()
    await toolset.tools["run_select"].function(system="erp_tenant",
                                               sql="SELECT A, B FROM SYS.M_ALERTS")
    await toolset.tools["run_check"].function(system="erp_tenant", check="alerts")
    assert len(seen) >= 2 and all("max_columns" not in kw for kw in seen)


# ---- fix re-review Minor B: the export's LOB budget -----------------------------------


async def test_an_export_over_the_lob_budget_is_export_too_large_and_stores_nothing(
        monkeypatch: pytest.MonkeyPatch, saved: _Saved) -> None:
    monkeypatch.setattr(export, "EXPORT_LOB_BUDGET", 50)
    db = h.install(monkeypatch, h.FakeDbapi())
    db.rows = [(i, h.FakeLob("t" * 30, db=db)) for i in range(5)]
    answer = await _export(system="erp_tenant", sql="SELECT A, B FROM SYS.M_ALERTS",
                           filename="lobs")
    assert answer["error"]["code"] == "export_too_large"
    assert answer["error"]["code"] != "result_too_large"
    assert saved.files == []
    # 30 + 30 passes 50: the second value stops the reading, no third read.
    assert db.steps().count("lob_read") == 2
    _ends_clean(db)


# ---- Nit C: the message names the read budget -----------------------------------------


def test_export_too_large_names_the_text_budget() -> None:
    assert "too much text" in MESSAGES["export_too_large"]
