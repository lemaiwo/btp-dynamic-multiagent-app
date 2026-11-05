"""The background reconciliation run (``agents/reconcile/run.py``).

A stub stands in for the agent's ARC-1 MCP server, as in
``tests/test_reconcile_arc1.py``; this one answers ``SAPQuery`` from a table
in memory (by the keys of the ``IN`` list), asserts on every call that it runs
as the signed-in caller (``current_principal``), and can hold a call until a
test releases it. The CSV, spec and code table are synthetic.

Run:  .venv/bin/python -m pytest tests/test_reconcile_run.py -q
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()

from sqlalchemy import delete, select  # noqa: E402

from agents.auth import current_principal  # noqa: E402
from agents.db import (  # noqa: E402
    ChatConversation,
    ChatFile,
    ChatMessage,
    ReconcileRun,
    SessionLocal,
    init_db,
)
from agents.oauth2 import OAuthAuthorizationRequired  # noqa: E402
from agents.reconcile import run, runs_store  # noqa: E402
from agents.reconcile.arc1_reader import Arc1ReadError  # noqa: E402
from agents.reconcile.engine import Comparer, iter_csv  # noqa: E402
from agents.reconcile.report import REPORT_MEDIA_TYPE  # noqa: E402
from agents.reconcile.spec import ReconcileSpec  # noqa: E402
from agents.reconcile.xref_reader import build_lookups  # noqa: E402
from agents.webchat import store  # noqa: E402
from tests.reconcile_helpers import xref_book  # noqa: E402

ALICE = "alice@example.com"
BOB = "bob@example.com"
ROWS = 1_050
QUERY_SCHEMA = {
    "type": "object",
    "properties": {"sql": {"type": "string"}, "maxRows": {"type": "number"}},
    "required": ["sql"],
}
_IN_LIST = re.compile(r"IN \((.*)\)\Z")
_LITERAL = re.compile(r"'((?:[^']|'')*)'")


def spec() -> ReconcileSpec:
    return ReconcileSpec.model_validate({
        "table": "ZTAB",
        "wave": "W1",
        "key": {"csv_column": "Object", "ecc_field": "OBJNR", "length": 10},
        "columns": {
            "Kind": {"kind": "direct", "source": "KIND"},
            "Code": {"kind": "value_map", "sources": ["CODE"], "lookups": [
                {"sheet": "Codes", "header_row": 1, "keys": [{"letter": "B"}],
                 "value": {"letter": "C"}},
            ]},
            "Flag": {"kind": "constant", "value": "X"},
        },
    })


def plain_spec() -> ReconcileSpec:
    """No value_map: no code table is needed."""
    return ReconcileSpec.model_validate({
        "table": "ZTAB", "wave": "W1",
        "key": {"csv_column": "Object", "ecc_field": "OBJNR", "length": 10},
        "columns": {"Kind": {"kind": "direct", "source": "KIND"}},
    })


def csv_bytes(rows: int = ROWS, header: str = "Object,Kind,Code,Flag") -> bytes:
    lines = [header]
    for i in range(1, rows + 1):
        kind = "K" if i % 7 else "WRONG"
        lines.append(f"{i},{kind},A,X")
    return ("\n".join(lines) + "\n").encode()


def ecc_table(rows: int = ROWS) -> dict[str, dict[str, str]]:
    """Source rows by padded key; every 100th key is missing in the source."""
    return {
        f"{i:010d}": {"OBJNR": f"{i:010d}", "KIND": "K", "CODE": "01"}
        for i in range(1, rows + 1) if i % 100
    }


class StubRun:
    def __init__(self, server: StubServer) -> None:
        self.server = server

    async def __aenter__(self) -> StubRun:
        self.server.check_caller()
        self.server.sessions += 1
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.server.closed += 1

    async def list_tools(self) -> list[Any]:
        self.server.check_caller()
        return [SimpleNamespace(name="SAPQuery", inputSchema=QUERY_SCHEMA)]

    async def direct_call_tool(self, name: str, args: dict[str, Any]) -> Any:
        server = self.server
        server.check_caller()
        assert name == "SAPQuery"
        sql = args["sql"]
        if sql.startswith("SELECT FIELDNAME FROM DD03L"):
            server.dd03l_calls += 1
            return json.dumps({"columns": ["FIELDNAME"],
                               "rows": [[f] for f in ("OBJNR", "KIND", "CODE")]})
        server.calls.append(dict(args))
        n = len(server.calls)
        if server.on_batch is not None:
            await server.on_batch(n)
        if server.gate is not None:
            await server.gate.wait()
        if n in server.fail_at:
            raise server.fail_at[n]
        keys = [k.replace("''", "'") for k in _LITERAL.findall(_IN_LIST.search(sql).group(1))]
        fields = sql[len("SELECT "):sql.index(" FROM ")].split(", ")
        rows = [[server.table[k][f] for f in fields] for k in keys if k in server.table]
        return json.dumps({"columns": fields, "rows": rows})


class StubServer:
    def __init__(self, table: dict[str, dict[str, str]] | None = None,
                 principal: str = ALICE) -> None:
        self.table = ecc_table() if table is None else table
        self.principal = principal
        self.calls: list[dict[str, Any]] = []
        self.dd03l_calls = 0
        self.callers: list[str | None] = []
        self.sessions = 0
        self.closed = 0
        self.gate: asyncio.Event | None = None
        self.on_batch: Any = None
        self.fail_at: dict[int, BaseException] = {}

    def check_caller(self) -> None:
        who = current_principal.get()
        self.callers.append(who)
        assert who == self.principal, "ARC-1 was called as somebody else"

    async def for_run(self, ctx: Any) -> StubRun:
        assert ctx is None
        self.check_caller()
        return StubRun(self)


async def wipe() -> None:
    async with SessionLocal() as s:
        for model in (ReconcileRun, ChatFile, ChatMessage, ChatConversation):
            await s.execute(delete(model))
        await s.commit()


@pytest.fixture(autouse=True)
async def _clean():
    await init_db()
    await wipe()
    run.reset()
    token = current_principal.set(ALICE)
    yield
    current_principal.reset(token)
    await run.cancel_all()
    run.reset()
    await wipe()


async def conversation(principal: str = ALICE) -> tuple[str, str]:
    async with SessionLocal() as s:
        conv = await store.create_conversation(s, principal, title="Recon", model=None)
        turn = await store.start_turn(s, conv, "reconcile", [])
        return conv.id, turn.assistant_message_id


async def start(cid: str, mid: str, server: StubServer, **over: Any) -> dict[str, Any]:
    args: dict[str, Any] = dict(
        conversation_id=cid, principal=ALICE, message_id=mid, spec=spec(),
        spec_file_id="f-spec", csv_data=csv_bytes(), xref_data=xref_book(),
        server=server, target="", batch_size=200, mismatch_cap=50_000,
    )
    args.update(over)
    return await run.start(**args)


async def wait(cid: str) -> None:
    task = run.task_of(cid)
    if task is not None:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)


async def row_of(run_id: str) -> ReconcileRun | None:
    async with SessionLocal() as s:
        return (await s.execute(
            select(ReconcileRun).where(ReconcileRun.id == run_id)
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()


async def files_of(cid: str) -> list[ChatFile]:
    async with SessionLocal() as s:
        return list((await s.execute(
            select(ChatFile).where(ChatFile.conversation_id == cid)
        )).scalars().all())


def engine_counts(data: bytes, table: dict[str, dict[str, str]]) -> dict[str, int]:
    """What the engine says for the same input, compared outside the run."""
    sp = spec()
    comparer = Comparer(sp, build_lookups(xref_book(), sp))
    _, rows = iter_csv(data)
    for row_no, row in rows:
        key = comparer.key_of(row, row_no)
        if key is not None:
            comparer.compare(key, row_no, row, table.get(key))
    result = comparer.result(malformed_rows=rows.malformed)
    return {
        "rows_total": result.rows_total, "rows_compared": result.rows_compared,
        "mismatches": result.mismatch_count, "missing_in_ecc": result.missing_count,
        "invalid_keys": result.invalid_keys, "duplicate_keys": result.duplicate_keys,
        "malformed_rows": result.malformed_rows,
    }


# --- a whole run --------------------------------------------------------------


async def test_a_run_reads_in_batches_and_stores_its_report():
    cid, mid = await conversation()
    server = StubServer()
    answer = await start(cid, mid, server)
    assert set(answer) == {"run_id", "rows_total", "batches"}
    assert answer["rows_total"] == ROWS and answer["batches"] == 6
    await wait(cid)

    got = await row_of(answer["run_id"])
    assert got.status == "done" and got.error_code is None
    assert got.rows_done == got.rows_total == ROWS
    counts = json.loads(got.counts_json)
    expected = engine_counts(csv_bytes(), server.table)
    assert {k: counts[k] for k in expected} == expected
    assert counts["mismatches"] > 0 and counts["missing_in_ecc"] == 10

    assert [c["maxRows"] for c in server.calls] == [200, 200, 200, 200, 200, 50]
    assert server.dd03l_calls == 1  # the preflight of start, not again in the task
    files = await files_of(cid)
    assert len(files) == 1
    f = files[0]
    assert (f.kind, f.media_type, f.message_id, f.principal) == (
        "generated", REPORT_MEDIA_TYPE, mid, ALICE)
    assert f.id == got.report_file_id
    assert f.name.startswith("reconcile-ZTAB-W1-") and f.name.endswith(".xlsx")
    assert f.data[:2] == b"PK"
    assert server.closed == server.sessions  # every ARC-1 session it opened is closed


async def test_the_background_task_keeps_acting_as_the_caller():
    cid, mid = await conversation()
    server = StubServer()
    token = current_principal.set(ALICE)
    try:
        await start(cid, mid, server)
    finally:
        current_principal.reset(token)
    # The caller's binding is gone here; the task captured it at start.
    current_principal.set(BOB)
    await wait(cid)
    assert server.callers and set(server.callers) == {ALICE}
    current_principal.set(ALICE)


async def test_start_refuses_a_principal_that_is_not_the_signed_in_caller():
    cid, mid = await conversation()
    with pytest.raises(run.RunError) as info:
        await start(cid, mid, StubServer(), principal=BOB)
    assert info.value.code == "not_signed_in"
    current_principal.set(None)
    with pytest.raises(run.RunError) as info:
        await start(cid, mid, StubServer())
    assert info.value.code == "not_signed_in"


async def test_a_spec_without_value_map_needs_no_code_table():
    cid, mid = await conversation()
    answer = await start(cid, mid, StubServer(), spec=plain_spec(), xref_data=None,
                         csv_data=csv_bytes(header="Object,Kind,Code,Flag"))
    await wait(cid)
    assert (await row_of(answer["run_id"])).status == "done"


async def test_a_value_map_without_a_code_table_is_refused():
    cid, mid = await conversation()
    with pytest.raises(run.RunError) as info:
        await start(cid, mid, StubServer(), xref_data=None)
    assert info.value.code == "xref_required"


# --- refusals before the row exists -------------------------------------------


async def test_a_csv_without_a_spec_column_is_refused_before_anything_runs():
    cid, mid = await conversation()
    server = StubServer()
    with pytest.raises(run.RunError) as info:
        await start(cid, mid, server, csv_data=csv_bytes(header="Object,Kind,Flag,Other"))
    assert info.value.code == "csv_invalid"
    assert info.value.csv_code == "column_missing"
    assert info.value.columns == ["Code"]
    assert server.sessions == 0
    async with SessionLocal() as s:
        assert await runs_store.latest_run(s, cid, ALICE) is None


@pytest.mark.parametrize(("data", "csv_code"), [
    (b"", "no_header"),
    (b"Object,Object\n1,2\n", "duplicate_header"),
    (b"Object,Kind,Code,Flag\n1,\xff,A,X\n", "not_utf8"),
])
async def test_an_unreadable_csv_is_refused_with_its_code(data: bytes, csv_code: str):
    cid, mid = await conversation()
    with pytest.raises(run.RunError) as info:
        await start(cid, mid, StubServer(), csv_data=data)
    assert (info.value.code, info.value.csv_code) == ("csv_invalid", csv_code)


async def test_a_csv_without_data_rows_is_refused():
    cid, mid = await conversation()
    with pytest.raises(run.RunError) as info:
        await start(cid, mid, StubServer(), csv_data=b"Object,Kind,Code,Flag\n")
    assert info.value.code == "csv_empty"


async def test_a_preflight_error_is_answered_in_the_call_and_creates_no_row():
    cid, mid = await conversation()
    server = StubServer()

    async def no_tools() -> list[Any]:
        return []

    real_for_run = server.for_run

    async def for_run(ctx: Any) -> StubRun:
        r = await real_for_run(ctx)
        r.list_tools = no_tools  # type: ignore[method-assign]
        return r

    server.for_run = for_run  # type: ignore[method-assign]
    with pytest.raises(Arc1ReadError) as info:
        await start(cid, mid, server)
    assert info.value.code == "query_tool_missing"
    async with SessionLocal() as s:
        assert await runs_store.latest_run(s, cid, ALICE) is None
    assert run.task_of(cid) is None


# --- one run per conversation --------------------------------------------------


async def test_a_second_start_while_the_first_runs_is_refused():
    cid, mid = await conversation()
    server = StubServer()
    server.gate = asyncio.Event()
    await start(cid, mid, server)
    with pytest.raises(run.RunError) as info:
        await start(cid, mid, StubServer())
    assert info.value.code == "already_running"
    other, other_mid = await conversation()
    second = await start(other, other_mid, StubServer())
    server.gate.set()
    await wait(cid)
    await wait(other)
    assert (await row_of(second["run_id"])).status == "done"


async def test_a_live_run_of_another_instance_is_refused():
    cid, mid = await conversation()
    async with SessionLocal() as s:
        await runs_store.create_run(
            s, conversation_id=cid, principal=ALICE, message_id=mid, table_name="ZTAB",
            wave="W1", spec_file_id=None, rows_total=5)
    server = StubServer()
    with pytest.raises(run.RunError) as info:
        await start(cid, mid, server)
    assert info.value.code == "already_running"
    assert server.sessions == 0


async def test_create_run_answering_none_is_already_running(monkeypatch):
    cid, mid = await conversation()

    async def none(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(runs_store, "create_run", none)
    with pytest.raises(run.RunError) as info:
        await start(cid, mid, StubServer())
    assert info.value.code == "already_running"
    assert run.task_of(cid) is None


# --- failures in the background -------------------------------------------------


async def test_sign_in_needed_mid_run_fails_the_row_without_a_report():
    cid, mid = await conversation()
    server = StubServer()
    server.fail_at = {3: OAuthAuthorizationRequired("https://arc1.example/mcp")}
    answer = await start(cid, mid, server)
    await wait(cid)
    got = await row_of(answer["run_id"])
    assert (got.status, got.error_code, got.report_file_id) == ("failed", "sign_in_needed", None)
    assert got.rows_done == 400
    assert await files_of(cid) == []


async def test_an_unexpected_exception_is_failed_internal_with_the_class_only(
    monkeypatch, caplog,
):
    cid, mid = await conversation()

    def boom(*a: Any, **k: Any) -> bytes:
        raise KeyError("SECRET-CELL-VALUE")

    monkeypatch.setattr(run, "build_report", boom)
    with caplog.at_level(logging.WARNING):
        answer = await start(cid, mid, StubServer())
        await wait(cid)
    got = await row_of(answer["run_id"])
    assert (got.status, got.error_code) == ("failed", "internal")
    assert "KeyError" in caplog.text
    assert "SECRET-CELL-VALUE" not in caplog.text
    assert await files_of(cid) == []


async def test_a_read_that_outlasts_its_timeout_fails_the_run(monkeypatch):
    monkeypatch.setattr(run, "READ_TIMEOUT_SECONDS", 0.1)
    cid, mid = await conversation()
    server = StubServer()
    server.gate = asyncio.Event()  # never released
    answer = await start(cid, mid, server)
    await wait(cid)
    got = await row_of(answer["run_id"])
    assert (got.status, got.error_code) == ("failed", "arc1_timeout")


async def test_a_deleted_conversation_stops_the_run_and_stores_nothing(caplog):
    cid, mid = await conversation()
    server = StubServer()

    async def delete_after_first(n: int) -> None:
        if n == 2:
            async with SessionLocal() as s:
                assert await store.delete_conversation(s, cid, ALICE)

    server.on_batch = delete_after_first
    with caplog.at_level(logging.ERROR):
        answer = await start(cid, mid, server)
        await wait(cid)
    assert await row_of(answer["run_id"]) is None
    assert await files_of(cid) == []
    assert len(server.calls) == 2  # stopped at the heartbeat after batch 2
    assert run.task_of(cid) is None
    assert "Traceback" not in caplog.text


async def test_a_row_lost_at_the_last_heartbeat_stores_nothing(monkeypatch):
    cid, mid = await conversation()

    async def closed_at_the_end(db: Any, run_id: str, rows_done: int) -> bool:
        return rows_done < ROWS  # the heartbeat with every row done finds it closed

    monkeypatch.setattr(runs_store, "heartbeat", closed_at_the_end)
    answer = await start(cid, mid, StubServer())
    await wait(cid)
    assert (await row_of(answer["run_id"])).status == "running"  # never finished by a loser
    assert await files_of(cid) == []


async def test_a_lost_finish_removes_the_stored_report(monkeypatch):
    cid, mid = await conversation()

    async def lost(*a: Any, **k: Any) -> bool:
        return False

    monkeypatch.setattr(runs_store, "finish_run", lost)
    await start(cid, mid, StubServer())
    await wait(cid)
    assert await files_of(cid) == []


# --- heartbeat, progress, cancel -------------------------------------------------


async def test_the_heartbeat_ticks_while_a_read_waits_on_arc1(monkeypatch):
    monkeypatch.setattr(run, "HEARTBEAT_SECONDS", 0.05)
    beats: list[int] = []
    real = runs_store.heartbeat

    async def counted(db: Any, run_id: str, rows_done: int) -> bool:
        beats.append(rows_done)
        return await real(db, run_id, rows_done)

    monkeypatch.setattr(runs_store, "heartbeat", counted)
    cid, mid = await conversation()
    server = StubServer()
    server.gate = asyncio.Event()
    await start(cid, mid, server)
    await asyncio.sleep(0.3)
    assert len(beats) >= 3  # no batch finished, the ticker kept the row fresh
    server.gate.set()
    await wait(cid)


def test_the_timings_stay_well_inside_the_stale_age():
    assert run.HEARTBEAT_SECONDS * 3 <= runs_store.STALE_SECONDS
    assert run.READ_TIMEOUT_SECONDS < runs_store.STALE_SECONDS


async def test_progress_while_running_and_cancel_all_interrupts():
    cid, mid = await conversation()
    server = StubServer()
    server.gate = asyncio.Event()

    async def second_waits(n: int) -> None:
        if n == 1:
            server.gate.set()
        else:
            server.gate.clear()

    server.on_batch = second_waits
    answer = await start(cid, mid, server)
    for _ in range(100):
        if len(server.calls) >= 2:
            break
        await asyncio.sleep(0.01)
    assert run.progress(cid) == {"rows_done": 200, "rows_total": ROWS}
    assert run.progress("no-such-conversation") is None
    await run.cancel_all()
    got = await row_of(answer["run_id"])
    assert (got.status, got.error_code) == ("interrupted", "interrupted")
    assert run.progress(cid) is None
    assert await files_of(cid) == []


async def test_batch_size_and_cap_are_checked():
    cid, mid = await conversation()
    for bad in (0, 10_001, True, "200"):
        with pytest.raises(ValueError):
            await start(cid, mid, StubServer(), batch_size=bad)
    with pytest.raises(ValueError):
        await start(cid, mid, StubServer(), mismatch_cap=0)


# --- per-column counters --------------------------------------------------------


def test_column_counters_are_flattened_plain_counters_and_read_back():
    from agents.reconcile.engine import ColumnStats

    stats = {"A.b": ColumnStats(kind="direct", checked=5, matched=3, mismatched=2),
             "Free": ColumnStats(kind="unresolved", not_checked=5)}
    flat = run.column_counts(stats)
    assert flat["col.A.b.checked"] == 5 and flat["col.A.b.mismatched"] == 2
    assert flat["col.Free.not_checked"] == 5
    assert flat["col.A.b.kind.direct"] == 1
    assert all(type(v) is int for v in flat.values())
    runs_store._counters(flat)  # runs_store's validator accepts them
    totals, columns = run.split_counts({"rows_total": 5, **flat, "col.x": 1,
                                                  "col.Bad.checked": "3"})
    assert totals == {"rows_total": 5}
    assert columns == [
        {"column": "A.b", "kind": "direct", "checked": 5, "matched": 3, "mismatched": 2,
         "not_checked": 0},
        {"column": "Free", "kind": "unresolved", "checked": 0, "matched": 0, "mismatched": 0,
         "not_checked": 5},
    ]


async def test_a_finished_run_stores_per_column_counters():
    cid, mid = await conversation()
    server = StubServer()
    answer = await start(cid, mid, server)
    await wait(cid)
    counts = json.loads((await row_of(answer["run_id"])).counts_json)
    sp = spec()
    for column in sp.columns:
        for metric in ("checked", "matched", "mismatched", "not_checked"):
            assert isinstance(counts[f"col.{column}.{metric}"], int)
    per_column = sum(counts[f"col.{c}.mismatched"] for c in sp.columns)
    assert per_column == counts["mismatches"]
