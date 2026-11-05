"""The five tools of ``builtin:reconcile`` (``agents/reconcile/tools.py``).

Called through the toolset, as the agent calls them, with the conversation
bound the way the web chat binds it (``current_chat_run`` with a
``ChatRunContext`` whose ``save`` stores a generated file) and the caller in
``current_principal``. The files are ``upload`` rows of a SQLite test
conversation; a small stub stands in for the agent's ARC-1 MCP server. Every
workbook, CSV and spec is synthetic.

Run:  .venv/bin/python -m pytest tests/test_reconcile_tools.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import json
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

from agents import registry  # noqa: E402
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
from agents.reconcile.tools import MESSAGES, activity_summary, reconcile_toolset  # noqa: E402
from agents.webchat import store  # noqa: E402
from agents.webchat.docgen import ChatRunContext, current_chat_run  # noqa: E402
from agents.webchat.uploads import CSV, JSON, XLSX  # noqa: E402
from tests.reconcile_helpers import mapping_book, xref_book  # noqa: E402

ALICE = "alice@example.com"
BOB = "bob@example.com"
ARC1 = "https://arc1.example.test/mcp"
ROWS = 30
QUERY_SCHEMA = {
    "type": "object",
    "properties": {"sql": {"type": "string"}, "maxRows": {"type": "number"}},
    "required": ["sql"],
}
_IN_LIST = re.compile(r"IN \((.*)\)\Z")
_LITERAL = re.compile(r"'((?:[^']|'')*)'")
TOOLS = ("read_mapping", "read_xref_sheet", "save_spec", "start_reconciliation",
         "reconciliation_status")


def spec_dict(**over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
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
    }
    spec.update(over)
    return spec


def csv_bytes(rows: int = ROWS, header: str = "Object,Kind,Code,Flag") -> bytes:
    lines = [header]
    lines += [f"{i},{'K' if i % 7 else 'WRONG'},A,X" for i in range(1, rows + 1)]
    return ("\n".join(lines) + "\n").encode()


# --- the ARC-1 stand-in --------------------------------------------------------


class StubRun:
    def __init__(self, server: StubServer) -> None:
        self.server = server

    async def __aenter__(self) -> StubRun:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def list_tools(self) -> list[Any]:
        if self.server.list_error is not None:
            raise self.server.list_error
        return [SimpleNamespace(name="SAPQuery", inputSchema=QUERY_SCHEMA)]

    async def direct_call_tool(self, name: str, args: dict[str, Any]) -> Any:
        server = self.server
        server.callers.append(current_principal.get())
        sql = args["sql"]
        if sql.startswith("SELECT FIELDNAME FROM DD03L"):
            return json.dumps({"columns": ["FIELDNAME"],
                               "rows": [[f] for f in server.fields]})
        keys = [k.replace("''", "'") for k in _LITERAL.findall(_IN_LIST.search(sql).group(1))]
        fields = sql[len("SELECT "):sql.index(" FROM ")].split(", ")
        rows = [[server.table[k][f] for f in fields] for k in keys if k in server.table]
        return json.dumps({"columns": fields, "rows": rows})


class StubServer:
    def __init__(self) -> None:
        self.table = {f"{i:010d}": {"OBJNR": f"{i:010d}", "KIND": "K", "CODE": "01"}
                      for i in range(1, ROWS + 1)}
        self.fields = ("OBJNR", "KIND", "CODE")
        self.list_error: BaseException | None = None
        self.callers: list[str | None] = []

    async def for_run(self, ctx: Any) -> StubRun:
        return StubRun(self)


# --- fixtures ------------------------------------------------------------------


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


async def upload(cid: str, name: str, media_type: str, data: bytes,
                 principal: str = ALICE) -> str:
    async with SessionLocal() as s:
        row = ChatFile(conversation_id=cid, message_id=None, principal=principal,
                       kind="upload", name=name, media_type=media_type, size=len(data),
                       data=data)
        s.add(row)
        await s.commit()
        return row.id


def chat_context(cid: str, mid: str, principal: str = ALICE) -> ChatRunContext:
    async def save(name: str, media_type: str, data: bytes) -> dict[str, Any]:
        async with SessionLocal() as s:
            return await store.add_generated_file(
                s, conversation_id=cid, message_id=mid, principal=principal,
                name=name, media_type=media_type, data=data)
    return ChatRunContext(conversation_id=cid, message_id=mid, principal=principal, save=save)


@contextlib.contextmanager
def bound(ctx: ChatRunContext | None):
    token = current_chat_run.set(ctx)
    try:
        yield ctx
    finally:
        current_chat_run.reset(token)


def toolset(server: Any = None, *, arc1: str | None = ARC1) -> Any:
    block = {"arc1_server": arc1} if arc1 else None
    return reconcile_toolset(
        block, server_for=lambda url: server if url == ARC1 else None,
        agent_name="Reconciler")


async def call(ts: Any, name: str, **kwargs: Any) -> Any:
    return await ts.tools[name].function(**kwargs)


def error_of(answer: Any) -> dict[str, Any]:
    assert isinstance(answer, dict) and set(answer) >= {"error"}, answer
    err = answer["error"]
    assert isinstance(err["code"], str) and isinstance(err["message"], str)
    return err


async def wait(cid: str) -> None:
    task = run.task_of(cid)
    if task is not None:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)


class World:
    """A conversation with the three uploads bound as the current chat run."""

    def __init__(self) -> None:
        self.server = StubServer()
        self.ts = toolset(self.server)

    async def setup(self) -> World:
        self.cid, self.mid = await conversation()
        self.mapping = await upload(self.cid, "mapping.xlsx", XLSX, mapping_book())
        self.xref = await upload(self.cid, "codes.xlsx", XLSX, xref_book())
        self.csv = await upload(self.cid, "ztab.csv", CSV, csv_bytes())
        self.ctx = chat_context(self.cid, self.mid)
        return self


@pytest.fixture
async def world() -> Any:
    w = await World().setup()
    with bound(w.ctx):
        yield w


# --- the toolset ---------------------------------------------------------------


def test_the_toolset_holds_exactly_the_five_tools_with_docstrings():
    ts = toolset()
    assert set(ts.tools) == set(TOOLS)
    for name in TOOLS:
        doc = ts.tools[name].function.__doc__ or ""
        assert len(doc) > 80, name
    assert "data" in ts.tools["read_mapping"].function.__doc__
    assert "reconciliation_status" in ts.tools["start_reconciliation"].function.__doc__


def test_every_code_has_a_fixed_message():
    for code in ("no_chat", "file_not_found", "wrong_file_type", "invalid_spec",
                 "missing_rules", "unknown_columns", "lookup_failed", "csv_invalid",
                 "arc1_not_configured", "target_required", "target_not_supported",
                 "query_tool_missing", "field_not_in_table", "arc1_refused",
                 "arc1_failed", "already_running", "run_not_found", "internal"):
        assert isinstance(MESSAGES.get(code), str) and MESSAGES[code], code


@pytest.mark.parametrize("name, kwargs", [
    ("read_mapping", dict(file_id="x", sheet="Fields", filter_column=None,
                          filter_value=None, columns=[{"letter": "A"}])),
    ("read_xref_sheet", dict(file_id="x")),
    ("save_spec", dict(spec=spec_dict(), csv_file_id="x")),
    ("start_reconciliation", dict(spec_file_id="x", csv_file_id="y")),
    ("reconciliation_status", dict()),
])
async def test_every_tool_needs_the_chat(name, kwargs):
    with bound(None):
        err = error_of(await call(toolset(StubServer()), name, **kwargs))
    assert err["code"] == "no_chat"
    assert err["message"] == MESSAGES["no_chat"]


# --- file scoping --------------------------------------------------------------


async def test_a_file_of_another_conversation_principal_or_unknown_is_not_found(world):
    other_cid, _ = await conversation()
    elsewhere = await upload(other_cid, "theirs.xlsx", XLSX, mapping_book())
    bob_cid, _ = await conversation(BOB)
    bobs = await upload(bob_cid, "bob.xlsx", XLSX, mapping_book(), principal=BOB)
    # A row in THIS conversation that another principal owns (never by the
    # app, but the owner check must still hold).
    foreign = await upload(world.cid, "planted.xlsx", XLSX, mapping_book(), principal=BOB)
    for fid in (elsewhere, bobs, foreign, "no-such-id", ""):
        err = error_of(await call(world.ts, "read_xref_sheet", file_id=fid))
        assert err["code"] == "file_not_found", fid
        assert err["message"] == MESSAGES["file_not_found"]
        listed = err["uploads"]
        assert sorted(f["name"] for f in listed) == ["codes.xlsx", "mapping.xlsx", "ztab.csv"]
        for item in listed:
            assert set(item) == {"file_id", "name", "size"}
        assert {f["file_id"] for f in listed} == {world.mapping, world.xref, world.csv}


async def test_a_generated_file_is_not_an_input_workbook(world):
    stored = await world.ctx.save("made.xlsx", XLSX, xref_book())
    err = error_of(await call(world.ts, "read_xref_sheet", file_id=stored["file_id"]))
    assert err["code"] == "file_not_found"


async def test_a_csv_where_a_workbook_is_expected_is_the_wrong_type(world):
    err = error_of(await call(world.ts, "read_xref_sheet", file_id=world.csv))
    assert err["code"] == "wrong_file_type"
    err = error_of(await call(world.ts, "read_mapping", file_id=world.csv, sheet="Fields",
                              filter_column=None, filter_value=None,
                              columns=[{"letter": "A"}]))
    assert err["code"] == "wrong_file_type"
    err = error_of(await call(world.ts, "save_spec", spec=spec_dict(),
                              csv_file_id=world.mapping))
    assert err["code"] == "wrong_file_type"
    err = error_of(await call(world.ts, "save_spec", spec=spec_dict(),
                              csv_file_id=world.csv, xref_file_id=world.csv))
    assert err["code"] == "wrong_file_type"


async def test_a_file_over_the_chat_cap_is_refused(world, monkeypatch):
    from agents.reconcile import tool_support as tools_module
    monkeypatch.setattr(tools_module, "MAX_FILE_BYTES", 100)
    err = error_of(await call(world.ts, "read_xref_sheet", file_id=world.xref))
    assert err["code"] == "wrong_file_type"


# --- read_mapping / read_xref_sheet --------------------------------------------


async def test_read_mapping_answers_the_mapping_rows(world):
    answer = await call(
        world.ts, "read_mapping", file_id=world.mapping, sheet="Fields",
        filter_column={"label": "Target table"}, filter_value="ZTAB",
        columns=[{"label": "Target field"}, {"label": "Rule type W1 go live"}])
    assert answer["sheet"] == "Fields" and answer["header_row"] == 4
    assert [r["values"] for r in answer["rows"]] == [
        ["OBJNR", "Direct"], ["KIND", "Value map"], ["NOTE", "Not needed"]]
    assert answer["truncated"] is False


async def test_read_mapping_passes_a_refusal_on_with_its_code(world):
    err = error_of(await call(
        world.ts, "read_mapping", file_id=world.mapping, sheet="Nope",
        filter_column=None, filter_value=None, columns=[{"letter": "A"}]))
    assert err["code"] == "sheet_not_found"
    err = error_of(await call(
        world.ts, "read_mapping", file_id=world.mapping, sheet="Fields",
        filter_column=None, filter_value=None, columns="A"))
    assert err["code"] == "invalid_arguments"


async def test_read_xref_sheet_lists_sheets_then_reads_a_window(world):
    answer = await call(world.ts, "read_xref_sheet", file_id=world.xref)
    assert "Codes" in [s["name"] for s in answer["sheets"]]
    grid = await call(world.ts, "read_xref_sheet", file_id=world.xref, sheet="codes",
                      first_row=1, max_rows=3, max_columns=3)
    assert grid["sheet"] == "Codes"
    assert grid["rows"][0]["cells"] == ["Note", "Source value", "Target value"]


# --- save_spec -----------------------------------------------------------------


async def test_save_spec_stores_the_spec_and_answers_what_it_holds(world):
    answer = await call(world.ts, "save_spec", spec=spec_dict(), csv_file_id=world.csv,
                        xref_file_id=world.xref)
    assert answer["name"] == "ZTAB-W1.reconcile-spec.json"
    assert answer["download_url"] == f"/chat/api/files/{answer['file_id']}"
    assert answer["columns_by_kind"] == {"direct": ["Kind"], "value_map": ["Code"],
                                         "constant": ["Flag"]}
    assert answer["rows_in_csv"] == ROWS
    [lookup] = answer["lookups"]
    assert lookup["sheet"] == "Codes" and lookup["header_row"] == 1
    assert lookup["entries"] >= 1 and isinstance(lookup["duplicates"], int)
    async with SessionLocal() as s:
        row = await store.get_file(s, answer["file_id"], ALICE)
    assert (row.kind, row.media_type, row.conversation_id, row.message_id) == (
        "generated", JSON, world.cid, world.mid)
    assert json.loads(row.data)["table"] == "ZTAB"


async def test_save_spec_refuses_an_invalid_spec_without_echoing_input(world):
    secret = "SECRETCELLVALUE"
    bad = spec_dict(wave=f"{secret}!!", table=f"{secret}x")
    bad["columns"] = {f"C{i}": {"kind": f"{secret}{i}"} for i in range(30)}
    err = error_of(await call(world.ts, "save_spec", spec=bad, csv_file_id=world.csv))
    assert err["code"] == "invalid_spec"
    assert 1 <= len(err["errors"]) <= 20
    for item in err["errors"]:
        assert set(item) == {"loc", "msg"}
    assert secret not in json.dumps(err)
    err = error_of(await call(world.ts, "save_spec", spec="not an object",
                              csv_file_id=world.csv))
    assert err["code"] == "invalid_spec"


async def test_save_spec_checks_the_csv_header_against_the_rules(world):
    extra = await upload(world.cid, "extra.csv", CSV,
                         csv_bytes(header="Object,Kind,Code,Flag,More"))
    err = error_of(await call(world.ts, "save_spec", spec=spec_dict(), csv_file_id=extra,
                              xref_file_id=world.xref))
    assert err["code"] == "missing_rules" and err["missing_rules"] == ["More"]

    short = await upload(world.cid, "short.csv", CSV, csv_bytes(header="Object,Kind,Flag,Other"))
    err = error_of(await call(world.ts, "save_spec", spec=spec_dict(), csv_file_id=short,
                              xref_file_id=world.xref))
    assert err["code"] == "unknown_columns" and err["unknown_columns"] == ["Code"]

    nokey = await upload(world.cid, "nokey.csv", CSV, csv_bytes(header="Id,Kind,Code,Flag"))
    err = error_of(await call(world.ts, "save_spec", spec=spec_dict(), csv_file_id=nokey,
                              xref_file_id=world.xref))
    assert err["code"] == "unknown_columns" and "Object" in err["unknown_columns"]

    broken = await upload(world.cid, "broken.csv", CSV, b"\xff\xfe\x00bad")
    err = error_of(await call(world.ts, "save_spec", spec=spec_dict(), csv_file_id=broken))
    assert err["code"] == "csv_invalid"


async def test_save_spec_reports_a_code_table_it_cannot_build(world):
    spec = spec_dict()
    spec["columns"]["Code"]["lookups"][0]["sheet"] = "Missing"
    err = error_of(await call(world.ts, "save_spec", spec=spec, csv_file_id=world.csv,
                              xref_file_id=world.xref))
    assert err["code"] == "lookup_failed"
    assert "hint" in err
    err = error_of(await call(world.ts, "save_spec", spec=spec_dict(), csv_file_id=world.csv))
    assert err["code"] == "xref_required"


# --- start_reconciliation / reconciliation_status ------------------------------


@pytest.mark.parametrize("arc1, found", [(None, True), (ARC1, False)])
async def test_start_needs_the_agents_arc1_server(world, arc1, found):
    ts = reconcile_toolset({"arc1_server": arc1} if arc1 else None,
                           server_for=lambda url: world.server if found else None)
    err = error_of(await call(ts, "start_reconciliation", spec_file_id="x",
                              csv_file_id=world.csv))
    assert err["code"] == "arc1_not_configured"


async def test_a_run_starts_as_the_chat_principal_and_reports_its_status(world):
    saved = await call(world.ts, "save_spec", spec=spec_dict(), csv_file_id=world.csv,
                       xref_file_id=world.xref)
    answer = await call(world.ts, "start_reconciliation", spec_file_id=saved["file_id"],
                        csv_file_id=world.csv, xref_file_id=world.xref)
    assert answer["rows_total"] == ROWS and answer["batches"] == 1
    assert answer["batch_size"] == 200
    assert "reconciliation_status" in answer["note"]
    await wait(world.cid)
    async with SessionLocal() as s:
        row = (await s.execute(select(ReconcileRun).where(
            ReconcileRun.id == answer["run_id"]))).scalar_one()
    assert (row.principal, row.message_id, row.spec_file_id) == (
        ALICE, world.mid, saved["file_id"])
    assert set(world.server.callers) == {ALICE}

    status = await call(world.ts, "reconciliation_status")
    assert status["run_id"] == answer["run_id"] and status["status"] == "done"
    assert (status["table"], status["wave"]) == ("ZTAB", "W1")
    assert status["rows_done"] == status["rows_total"] == ROWS
    assert status["counts"]["mismatches"] > 0
    report = status["report"]
    assert set(report) == {"file_id", "name", "download_url"}
    assert report["name"].endswith(".xlsx")
    assert "error" not in status
    assert (await call(world.ts, "reconciliation_status",
                       run_id=answer["run_id"]))["run_id"] == answer["run_id"]


async def test_a_spec_upload_may_start_a_run(world):
    spec_id = await upload(world.cid, "my.reconcile-spec.json", JSON,
                           json.dumps(spec_dict()).encode())
    answer = await call(world.ts, "start_reconciliation", spec_file_id=spec_id,
                        csv_file_id=world.csv, xref_file_id=world.xref)
    assert "run_id" in answer
    await wait(world.cid)


async def test_start_refuses_a_spec_that_is_not_json_or_not_a_spec(world):
    for name, media, data in (("s.json", JSON, b"{not json"),
                              ("s.json", JSON, json.dumps({"table": 1}).encode()),
                              ("s.csv", CSV, csv_bytes())):
        fid = await upload(world.cid, name, media, data)
        err = error_of(await call(world.ts, "start_reconciliation", spec_file_id=fid,
                                  csv_file_id=world.csv, xref_file_id=world.xref))
        assert err["code"] in {"invalid_spec", "wrong_file_type"}, name


async def test_start_passes_run_and_reader_refusals_on(world):
    spec_id = (await call(world.ts, "save_spec", spec=spec_dict(), csv_file_id=world.csv,
                          xref_file_id=world.xref))["file_id"]
    short = await upload(world.cid, "short.csv", CSV, csv_bytes(header="Object,Kind,Flag,X"))
    err = error_of(await call(world.ts, "start_reconciliation", spec_file_id=spec_id,
                              csv_file_id=short, xref_file_id=world.xref))
    assert err["code"] == "csv_invalid" and err["columns"] == ["Code"]

    world.server.fields = ("OBJNR", "CODE")
    err = error_of(await call(world.ts, "start_reconciliation", spec_file_id=spec_id,
                              csv_file_id=world.csv, xref_file_id=world.xref))
    assert err["code"] == "field_not_in_table" and err["fields"] == ["KIND"]

    world.server.fields = ("OBJNR", "KIND", "CODE")
    err = error_of(await call(world.ts, "start_reconciliation", spec_file_id=spec_id,
                              csv_file_id=world.csv))
    assert err["code"] == "xref_required"


async def test_start_refuses_when_the_chat_principal_is_not_the_caller(world):
    spec_id = (await call(world.ts, "save_spec", spec=spec_dict(), csv_file_id=world.csv,
                          xref_file_id=world.xref))["file_id"]
    current_principal.set(BOB)
    err = error_of(await call(world.ts, "start_reconciliation", spec_file_id=spec_id,
                              csv_file_id=world.csv, xref_file_id=world.xref))
    assert err["code"] == "not_signed_in"
    current_principal.set(ALICE)


async def test_a_sign_in_at_the_preflight_raises_the_oauth_error(world):
    spec_id = (await call(world.ts, "save_spec", spec=spec_dict(), csv_file_id=world.csv,
                          xref_file_id=world.xref))["file_id"]
    world.server.list_error = OAuthAuthorizationRequired(ARC1)
    with pytest.raises(OAuthAuthorizationRequired) as info:
        await call(world.ts, "start_reconciliation", spec_file_id=spec_id,
                   csv_file_id=world.csv, xref_file_id=world.xref)
    assert info.value.server_key == ARC1


async def test_a_second_start_while_running_is_already_running(world):
    spec_id = (await call(world.ts, "save_spec", spec=spec_dict(), csv_file_id=world.csv,
                          xref_file_id=world.xref))["file_id"]
    gate = asyncio.Event()
    original = StubRun.direct_call_tool

    async def held(self, name, args):
        if not args["sql"].startswith("SELECT FIELDNAME"):
            await gate.wait()
        return await original(self, name, args)

    StubRun.direct_call_tool = held
    try:
        first = await call(world.ts, "start_reconciliation", spec_file_id=spec_id,
                           csv_file_id=world.csv, xref_file_id=world.xref)
        err = error_of(await call(world.ts, "start_reconciliation", spec_file_id=spec_id,
                                  csv_file_id=world.csv, xref_file_id=world.xref))
        assert err["code"] == "already_running"
        status = await call(world.ts, "reconciliation_status")
        assert status["run_id"] == first["run_id"] and status["status"] == "running"
        assert "report" not in status and "error" not in status
    finally:
        gate.set()
        StubRun.direct_call_tool = original
    await wait(world.cid)


async def test_status_never_shows_another_conversations_run(world):
    other_cid, other_mid = await conversation()
    async with SessionLocal() as s:
        theirs = await runs_store.create_run(
            s, conversation_id=other_cid, principal=ALICE, message_id=other_mid,
            table_name="ZTAB", wave="W1", spec_file_id=None, rows_total=5)
    err = error_of(await call(world.ts, "reconciliation_status", run_id=theirs))
    assert err["code"] == "run_not_found"
    err = error_of(await call(world.ts, "reconciliation_status"))
    assert err["code"] == "run_not_found"
    err = error_of(await call(world.ts, "reconciliation_status", run_id=12))
    assert err["code"] == "run_not_found"


@pytest.mark.parametrize("outcome, code", [("failed", "arc1_timeout"),
                                           ("interrupted", "interrupted"),
                                           ("failed", "something_new")])
async def test_status_says_when_a_run_failed_or_was_interrupted(world, outcome, code):
    async with SessionLocal() as s:
        rid = await runs_store.create_run(
            s, conversation_id=world.cid, principal=ALICE, message_id=world.mid,
            table_name="ZTAB", wave="W1", spec_file_id=None, rows_total=5)
        assert await runs_store.finish_run(s, rid, status=outcome, error_code=code,
                                           counts={"rows_total": 5})
    status = await call(world.ts, "reconciliation_status")
    assert status["status"] == outcome
    assert status["error"]["code"] == code
    assert isinstance(status["error"]["message"], str) and status["error"]["message"]
    assert "report" not in status


# --- activity ------------------------------------------------------------------


def test_activity_summary_keeps_counts_and_codes_only():
    rows = {"sheet": "Fields", "rows": [{"row": 5, "values": ["SECRET"]}] * 2}
    assert activity_summary("read_mapping", rows) == "read_mapping: 2 rows"
    assert activity_summary("reconcile_read_mapping", rows) == "read_mapping: 2 rows"
    assert activity_summary("reconcile_0_read_xref_sheet",
                            {"sheets": [{"name": "SECRET"}]}) == "read_xref_sheet: 1 sheets"
    assert activity_summary("read_xref_sheet", {"rows": [{}] * 3}) == "read_xref_sheet: 3 rows"
    assert activity_summary("save_spec", {"columns_by_kind": {"direct": ["A", "B"]},
                                          "lookups": [{}]}) == "save_spec: 2 columns, 1 lookups"
    assert activity_summary("start_reconciliation", {"rows_total": 9, "batches": 1}) == (
        "start_reconciliation: 9 rows, 1 batches")
    assert activity_summary("reconciliation_status", {"status": "running", "rows_done": 3,
                                                      "rows_total": 9}) == (
        "reconciliation_status: running 3/9")
    assert activity_summary("save_spec", {"error": {"code": "missing_rules"}}) == (
        "error: missing_rules")
    assert activity_summary("save_spec", {"error": {"code": "SECRET VALUE"}}) == "error"
    assert activity_summary("read_table", rows) is None
    assert activity_summary("other_read_mapping_x", rows) is None


def test_the_registry_preview_uses_the_summary():
    result = SimpleNamespace(tool_name="reconcile_read_mapping",
                             content={"rows": [{"row": 1, "values": ["SECRETCELL"]}]})
    preview = registry._short_tool_output(result)
    assert preview == "read_mapping: 1 rows"


async def test_status_of_a_finished_run_lists_columns_by_mismatches(world):
    counts = {"rows_total": 5, "mismatches": 4,
              "col.Few.checked": 5, "col.Few.matched": 4, "col.Few.mismatched": 1,
              "col.Few.not_checked": 0, "col.Few.kind.direct": 1,
              "col.Many.checked": 5, "col.Many.matched": 2, "col.Many.mismatched": 3,
              "col.Many.not_checked": 0, "col.Many.kind.value_map": 1,
              "col.Open.checked": 0, "col.Open.matched": 0, "col.Open.mismatched": 0,
              "col.Open.not_checked": 5, "col.Open.kind.unresolved": 1}
    async with SessionLocal() as s:
        rid = await runs_store.create_run(
            s, conversation_id=world.cid, principal=ALICE, message_id=world.mid,
            table_name="ZTAB", wave="W1", spec_file_id=None, rows_total=5)
        assert await runs_store.finish_run(s, rid, status="done", counts=counts)
    status = await call(world.ts, "reconciliation_status")
    assert status["counts"] == {"rows_total": 5, "mismatches": 4}
    assert [c["column"] for c in status["columns"]] == ["Many", "Few", "Open"]
    assert status["columns"][0] == {"column": "Many", "kind": "value_map", "checked": 5,
                                    "matched": 2, "mismatched": 3, "not_checked": 0}


async def test_status_of_a_failed_run_has_no_column_list(world):
    async with SessionLocal() as s:
        rid = await runs_store.create_run(
            s, conversation_id=world.cid, principal=ALICE, message_id=world.mid,
            table_name="ZTAB", wave="W1", spec_file_id=None, rows_total=5)
        assert await runs_store.finish_run(
            s, rid, status="failed", error_code="arc1_timeout",
            counts={"rows_total": 5, "col.A.checked": 1, "col.A.kind.direct": 1})
    status = await call(world.ts, "reconciliation_status")
    assert status["counts"] == {"rows_total": 5}
    assert "columns" not in status
