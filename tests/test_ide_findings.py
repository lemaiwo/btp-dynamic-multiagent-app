"""Findings from diagnose tool results (plan 1c Task 8, override 2026-10-03).

``agents.ide.findings.extract`` turns one ``SAPDiagnose`` data result into
finding metadata. The guard calls it on what the model was given -- ARC-1's
own text (a diagnose run exists only for a ``non_production`` target) -- and
collects the detail text, which the runner stores with the finding. The
metadata itself never carries free text or personal data.

Run:  python -m pytest tests/test_ide_findings.py -q
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    ModelMessage,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import (  # noqa: E402
    AgentInfo,
    DeltaToolCall,
    FunctionModel,
)
from pydantic_ai.toolsets import FunctionToolset  # noqa: E402

from agents.db import SessionLocal, init_db  # noqa: E402
from agents.deep import (  # noqa: E402
    DeepConfig,
    DeepState,
    WorkspaceScope,
    current_workspace,
    deep_toolset,
)
from agents.ide import diagnose, findings, runner  # noqa: E402
from agents.ide.diagnose import DiagnoseRun, current_diagnose  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeApproval,
    IdeArtifact,
    IdeAuditLog,
    IdeConventions,
    IdeFinding,
    IdeMessage,
    IdeSession,
    IdeWorkspaceFile,
)
from agents.ide.readonly import ReadOnlyGuard  # noqa: E402
from agents.ide.stages import SUBAGENTS_ALLOWED, Stage, StageGateError  # noqa: E402
from agents.ide.store import (  # noqa: E402
    create_session,
    get_finding,
    list_findings,
    upsert_conventions,
    upsert_findings,
)
from agents.registry import BuildResult, registry  # noqa: E402

pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")

FIXTURES = ROOT / "tests" / "fixtures" / "ide_diagnose"
SENTINELS = [
    "DEVUSER01",
    "jane.doe@example.com",
    "BE71 0961 2345 6769",
    "123456789012",
    "RAWSENTINEL-0123456789-ABCDEF",
]
KEYS = {"kind", "ref_id", "title", "program", "include", "line", "occurred_at"}
DEST = "arc1-abap-readonly"
SID = "s-findings"
DUMP_ID = "20261003101530DEVHOST_DEMO_00"
T = "SAPDiagnose"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def assert_clean(text: str, where: str = "") -> None:
    for s in SENTINELS:
        assert s.lower() not in text.lower(), f"{s} survived {where}"


def meta(kind, ref_id, title, program=None, include=None, line=None, occurred_at=None):
    return {"kind": kind, "ref_id": ref_id, "title": title, "program": program,
            "include": include, "line": line, "occurred_at": occurred_at}


# --- one test per shape ----------------------------------------------------------

DUMP_1 = meta(
    "dump", DUMP_ID, "COMPUTE_INT_ZERODIVIDE",
    "ZCL_DEMO_CALC=================CP", "ZCL_DEMO_CALC=================CM001",
    12, "2026-10-03T10:15:30Z",
)

EXPECTED = {
    "dumps_list.json": ({"action": "dumps"}, [
        DUMP_1,
        meta("dump", "20261003111045DEVHOST_DEMO_00", "MESSAGE_TYPE_X",
             "ZDEMO_REPORT", "ZDEMO_REPORT", 48, "2026-10-03T11:10:45Z"),
        meta("dump", "20261003120000DEVHOST_DEMO_00", "OBJECTS_OBJREF_NOT_ASSIGNED",
             "ZDEMO_REPORT", "ZDEMO_REPORT", 77, "2026-10-03T12:00:00Z"),
    ]),
    "dump_detail.json": ({"action": "dumps", "id": DUMP_ID}, [DUMP_1]),
    "dump_detail_st22.json": ({"action": "dumps", "id": DUMP_ID}, [DUMP_1]),
    "traces_list.json": ({"action": "traces"}, [
        meta("trace", "ATRA_20261003_101500_0001", "ZDEMO_REPORT",
             occurred_at="2026-10-03T10:15:00Z"),
    ]),
    "gateway_errors_list.json": ({"action": "gateway_errors"}, [
        meta("gateway_error", "/sap/bc/adt/gw/errorlog/0A1B2C3D4E5F",
             "Frontend Error ZDEMO_SRV", occurred_at="2026-10-03T10:20:00Z"),
        meta("gateway_error", "/sap/bc/adt/gw/errorlog/0A1B2C3D4E60",
             "Backend Error ZDEMO_SRV", occurred_at="2026-10-03T10:21:00Z"),
    ]),
    "gateway_error_detail.json": ({"action": "gateway_errors", "id": "0A1B2C3D4E5F"}, [
        meta("gateway_error", "/sap/bc/adt/gw/errorlog/0A1B2C3D4E5F",
             "Frontend Error ZDEMO_SRV", "ZCL_DEMO_DPC_EXT==============CP",
             "ZCL_DEMO_DPC_EXT==============CM002", 31, "2026-10-03T10:20:00Z"),
    ]),
    "authorization_trace.json": ({"action": "authorization_trace"}, [
        meta("auth_check", "S_TCODE:ZDEMO_REPORT:2026-10-03T10:16:00Z", "S_TCODE",
             "ZDEMO_REPORT", occurred_at="2026-10-03T10:16:00Z"),
        meta("auth_check", "Z_PARTNER:ZDEMO_REPORT:2026-10-03T10:16:01Z", "Z_PARTNER",
             "ZDEMO_REPORT", occurred_at="2026-10-03T10:16:01Z"),
    ]),
}


@pytest.mark.parametrize("fixture", sorted(EXPECTED))
def test_shape_yields_metadata(fixture):
    """Metadata is built from identifiers, numbers and timestamps only."""
    args, expected = EXPECTED[fixture]
    got = findings.extract(T, args, load(fixture))
    assert got == expected
    assert all(set(f) == KEYS for f in got)
    assert_clean(json.dumps(got), f"in {fixture} metadata")


def test_odata_perf():
    args = {"action": "odata_perf", "url": "/sap/opu/odata/sap/ZDEMO_SRV/Partners"}
    raw = findings.extract(T, args, load("odata_perf.json"))
    assert raw == [meta(
        "odata_call", "/sap/opu/odata/sap/ZDEMO_SRV/Partners('123456789012')",
        "OData timing", occurred_at="2026-10-03T10:22:00Z",
    )]
    assert "?" not in raw[0]["ref_id"] and "filter" not in raw[0]["ref_id"]


def test_auth_rows_that_passed_are_no_finding():
    rows = {"rows": [
        {"authObject": "S_TCODE", "rc": 0, "program": "ZP", "timestamp": "2026-10-03T10:00:00Z"},
        {"authObject": "S_DEVELOP", "rc": 4, "program": "ZP", "timestamp": "2026-10-03T10:00:01Z"},
        {"authObject": "S_NO_RC", "program": "ZP"},
        {"authObject": "S_BOOL", "rc": True, "program": "ZP"},
    ]}
    got = findings.extract(T, {"action": "authorization_trace"}, json.dumps(rows))
    assert [f["title"] for f in got] == ["S_DEVELOP"]


def test_prefixed_tool_name_is_recognised():
    got = findings.extract("arc1_SAPDiagnose", {"action": "dumps"}, load("dumps_list.json"))
    assert len(got) == 3


def test_titles_never_carry_free_text():
    """Short texts, messages and descriptions are prose about the failure
    (and about people); a title is built from identifiers only."""
    for fixture in ("dumps_list.json", "traces_list.json", "gateway_errors_list.json"):
        args = EXPECTED[fixture][0]
        for f in findings.extract(T, args, load(fixture)):  # raw text in
            assert "Division" not in f["title"] and "Partner" not in f["title"]
            assert "Measured" not in f["title"] and " for " not in f["title"]
            assert_clean(f["title"], f"in a title of {fixture}")
    # A trace whose object name is not an identifier gets a neutral title.
    payload = json.dumps({"traces": [{"id": "A1", "objectName": "run for Jane Doe (x)"}]})
    [got] = findings.extract(T, {"action": "traces"}, payload)
    assert got["title"] == "ABAP trace"


@pytest.mark.parametrize("args,text", [
    ({"action": "dumps"}, load("unknown_shape.json")),
    ({"action": "dumps"}, load("not_json.txt")),
    ({"action": "dumps"}, load("arc1_error.json")),
    ({"action": "sql_trace_state"}, json.dumps({"active": True})),
    ({"action": "trace_requests"}, load("trace_requests.json")),
    ({"action": "traces", "id": "A1", "analysis": "hitlist"}, load("trace_hitlist.json")),
    ({"action": "syntax"}, load("dumps_list.json")),
    ({"action": "dumps"}, ""),
    ({"action": "dumps"}, "[]"),
    ({"action": "dumps"}, json.dumps({"dumps": "not a list"})),
    ({"action": "dumps"}, json.dumps({"dumps": [1, None, "x", {"no": "id"}]})),
    ({}, load("dumps_list.json")),
])
def test_unknown_shape_yields_nothing(args, text):
    assert findings.extract(T, args, text) == []


def test_other_tools_yield_nothing():
    assert findings.extract("SAPRead", {"action": "dumps"}, load("dumps_list.json")) == []


@pytest.mark.parametrize("tool,args,text", [
    (None, None, None),
    (T, "dumps", 12),
    (T, {"action": ["dumps"]}, b"\xff"),
    (T, {"action": "dumps"}, "[" * 100_000),
    (T, {"action": "gateway_errors"}, json.dumps({"errors": [{"id": 1, "detailUrl": 2}]})),
    (T, {"action": "authorization_trace"}, json.dumps({"rows": [{"rc": "x"}, {"rc": 4}]})),
    (T, {"action": "odata_perf"}, json.dumps({"requests": [{"url": 5}, {"url": "no slash"}]})),
])
def test_bad_input_never_raises(tool, args, text):
    assert findings.extract(tool, args, text) == []
    assert findings.extract(tool, args, text, with_detail=True) == []


def test_argument_of_the_wrong_type_is_not_used():
    """The payload's own id identifies the dump; an ``id`` argument that is
    not a string is never turned into one."""
    args = {"action": "dumps", "id": {"a": 1}}
    assert findings.extract(T, args, load("dump_detail.json")) == [DUMP_1]
    data = json.loads(load("dump_detail.json"))
    del data["id"]
    assert findings.extract(T, args, json.dumps(data)) == []


def test_values_that_are_not_metadata_are_dropped():
    """A field of the wrong type or form is left out; an identity that is
    not an identifier means no finding at all."""
    payload = json.dumps({"dumps": [
        {"id": "D1", "runtimeError": "Jane Doe wrote this", "program": ["P"],
         "include": "has space", "line": "12", "timestamp": "yesterday at noon"},
        {"id": "an id with spaces", "runtimeError": "X"},
        {"id": "D" * 300, "runtimeError": "X"},
    ]})
    got = findings.extract(T, {"action": "dumps"}, payload)
    assert got == [meta("dump", "D1", "Runtime error")]


def test_a_failure_is_logged_without_values(monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("DEVUSER01 jane.doe@example.com")

    monkeypatch.setitem(findings._EXTRACTORS, "dumps_list", boom)
    with caplog.at_level(logging.DEBUG, logger="agents.ide.findings"):
        assert findings.extract(T, {"action": "dumps"}, load("dumps_list.json")) == []
    assert "RuntimeError" in caplog.text
    assert_clean(caplog.text, "in the log")


def test_one_call_is_capped():
    payload = json.dumps({"dumps": [
        {"id": f"D{i}", "runtimeError": "X"} for i in range(findings.MAX_PER_CALL + 50)
    ]})
    assert len(findings.extract(T, {"action": "dumps"}, payload)) == findings.MAX_PER_CALL


# --- detail: only when asked for -----------------------------------------------


def test_detail_only_with_detail():
    args = {"action": "dumps", "id": DUMP_ID}
    text = load("dump_detail.json")
    [plain] = findings.extract(T, args, text)
    assert "detail" not in plain
    [full] = findings.extract(T, args, text, with_detail=True)
    assert {k: v for k, v in full.items() if k != "detail"} == DUMP_1
    assert full["detail"] == json.loads(text)["formattedText"]
    # A list never carries detail text, whatever the flag.
    for f in findings.extract(T, {"action": "dumps"}, load("dumps_list.json"),
                              with_detail=True):
        assert "detail" not in f


def test_dump_detail_without_formatted_text_uses_chapters():
    data = json.loads(load("dump_detail.json"))
    del data["formattedText"]
    [got] = findings.extract(T, {"action": "dumps", "id": DUMP_ID}, json.dumps(data),
                             with_detail=True)
    assert got["detail"].startswith("## Short text\nDivision by zero")
    assert "## Chosen variables" in got["detail"]


def test_gateway_detail_text_and_size_cap():
    args = {"action": "gateway_errors", "id": "0A1B2C3D4E5F"}
    [got] = findings.extract(T, args, load("gateway_error_detail.json"), with_detail=True)
    assert "Partner 123456789012 not found" in got["detail"]
    big = json.loads(load("dump_detail.json"))
    big["formattedText"] = "x" * (findings.MAX_DETAIL + 10)
    [cut] = findings.extract(T, {"action": "dumps", "id": DUMP_ID}, json.dumps(big),
                             with_detail=True)
    assert len(cut["detail"]) == findings.MAX_DETAIL


# --- merge -----------------------------------------------------------------------


def test_merge_keeps_what_a_later_list_does_not_know():
    into: list[dict] = []
    findings.merge(into, [{**DUMP_1, "detail": "the text"}])
    findings.merge(into, [meta("dump", DUMP_ID, "COMPUTE_INT_ZERODIVIDE")])
    assert into == [{**DUMP_1, "detail": "the text"}]
    findings.merge(into, [meta("dump", DUMP_ID, "NEW_TITLE", line=13)])
    assert (into[0]["title"], into[0]["line"]) == ("NEW_TITLE", 13)
    findings.merge(into, [meta("trace", DUMP_ID, "t")])
    assert len(into) == 2


def test_merge_is_capped_per_run():
    into: list[dict] = []
    findings.merge(into, [meta("dump", f"D{i}", "X") for i in range(findings.MAX_PER_RUN + 20)])
    assert len(into) == findings.MAX_PER_RUN
    # A known finding is still updated when the run is full.
    findings.merge(into, [meta("dump", "D0", "Y")])
    assert into[0]["title"] == "Y" and len(into) == findings.MAX_PER_RUN


# --- store: the detail column -----------------------------------------------------


@pytest.fixture
async def clean_db():
    await init_db()
    async with SessionLocal() as db:
        for model in (IdeFinding, IdeApproval, IdeAuditLog, IdeWorkspaceFile,
                      IdeArtifact, IdeMessage, IdeSession, IdeConventions):
            await db.execute(model.__table__.delete())
        await db.commit()
    saved = registry._build
    yield
    registry._build = saved


async def _session(session_type: str = "diagnose") -> str:
    async with SessionLocal() as db:
        s = await create_session(db, owner="alice", title="t", target="T1",
                                 session_type=session_type)
        return s.id


async def test_detail_column_exists_after_init(clean_db):
    async with SessionLocal() as db:
        cols = {row[1] for row in
                (await db.execute(__import__("sqlalchemy").text(
                    "PRAGMA table_info(ide_findings)"))).all()}
    assert "detail" in cols


async def test_store_detail_upsert_and_read(clean_db):
    sid = await _session()
    async with SessionLocal() as db:
        [row] = await upsert_findings(db, sid, [{**DUMP_1, "detail": "the dump text"}])
        assert row.detail == "the dump text"
        # A later item without detail (a list result) keeps the stored text.
        [row] = await upsert_findings(db, sid, [dict(DUMP_1)])
        assert row.detail == "the dump text"
        [row] = await upsert_findings(db, sid, [{**DUMP_1, "detail": 12}])
        assert row.detail == "the dump text"
        assert (await get_finding(db, sid, row.id)).detail == "the dump text"
        assert (await list_findings(db, sid))[0].detail == "the dump text"


def test_finding_out_is_the_contract():
    from datetime import datetime, timezone

    row = IdeFinding(id="f1", session_id="s", **DUMP_1, detail="raw text",
                     created_at=datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc))
    out = findings.finding_out(row)
    assert out == {"id": "f1", **DUMP_1, "created_at": "2026-10-03T10:00:00+00:00"}


# --- guard: collects into the bound run -------------------------------------------


def arc1_toolset(calls: list):
    ts = FunctionToolset()

    @ts.tool_plain
    def SAPDiagnose(action: str, id: str = "") -> str:  # noqa: N802, A002
        """Diagnose."""
        calls.append(action)
        if action == "atc":
            return load("dumps_list.json")  # a source check, not diagnose data
        if action == "sql_trace_state":
            return load("unknown_shape.json")
        if action == "gateway_errors":
            return load("gateway_errors_list.json")
        return load("dump_detail.json" if id else "dumps_list.json")

    return ts


@pytest.fixture
def target_servers(monkeypatch):
    only: list = []

    def _is_target(toolset, run) -> bool:
        return not only or any(toolset is t for t in only)

    monkeypatch.setattr(diagnose, "is_target_server", _is_target)
    return only


class bound:
    def __init__(self, session_type="diagnose"):
        self.scope = WorkspaceScope(
            session_id=SID, state=DeepState(run_id=SID), session_type=session_type
        )
        self.run = DiagnoseRun(
            session_id=SID, owner="alice", target="T1", run_id="r-1",
            destination=DEST,
        )

    def __enter__(self):
        self._w = current_workspace.set(self.scope)
        self._d = current_diagnose.set(self.run)
        return self

    def __exit__(self, *exc):
        current_diagnose.reset(self._d)
        current_workspace.reset(self._w)


def scripted(turns: list):
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        n = sum(1 for m in messages if isinstance(m, ModelResponse))
        turn = turns[min(n, len(turns) - 1)]
        if isinstance(turn, str):
            return ModelResponse(parts=[TextPart(turn)])
        return ModelResponse(parts=[ToolCallPart(t, a) for t, a in turn])

    return FunctionModel(fn)


LIST = (T, {"action": "dumps"})
DETAIL = (T, {"action": "dumps", "id": DUMP_ID})


async def test_guard_collects_detail(target_servers):
    calls: list = []
    with bound() as b:
        await Agent().run("go", model=scripted([[DETAIL], [LIST], "done"]),
                          toolsets=[ReadOnlyGuard(arc1_toolset(calls))])
    assert len(b.run.findings) == 3
    first = b.run.findings[0]
    assert {k: v for k, v in first.items() if k != "detail"} == DUMP_1
    # The list that came after did not wipe the text of the detail read.
    assert "User: DEVUSER01" in first["detail"]


async def test_guard_collects_nothing_from_other_calls(target_servers):
    """Source checks, unknown shapes, another server and change sessions."""
    calls: list = []
    with bound() as b:
        await Agent().run(
            "go",
            model=scripted([[(T, {"action": "atc"})],
                            [(T, {"action": "sql_trace_state"})], "done"]),
            toolsets=[ReadOnlyGuard(arc1_toolset(calls))])
    assert calls == ["atc", "sql_trace_state"] and b.run.findings == []

    other = arc1_toolset(calls)
    target_servers.append(arc1_toolset([]))  # the target is some other toolset
    with bound() as b:
        await Agent().run("go", model=scripted([[LIST], "done"]),
                          toolsets=[ReadOnlyGuard(other)])
    assert b.run.findings == []  # refused under the change policy


async def test_extraction_failure_does_not_break_the_call(target_servers, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("DEVUSER01")

    monkeypatch.setattr(findings, "extract", boom)
    seen: list[str] = []

    def fn(messages, info):
        back = [p for m in messages for p in m.parts
                if isinstance(p, (ToolReturnPart, RetryPromptPart))]
        if not back:
            return ModelResponse(parts=[ToolCallPart(*LIST)])
        seen.append(type(back[-1]).__name__)
        return ModelResponse(parts=[TextPart("done")])

    with bound() as b:
        result = await Agent().run("go", model=FunctionModel(fn),
                                   toolsets=[ReadOnlyGuard(arc1_toolset([]))])
    assert result.output == "done" and seen == ["ToolReturnPart"]
    assert b.run.findings == []


def _child_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    back = [p for m in messages for p in m.parts
            if isinstance(p, (ToolReturnPart, RetryPromptPart))]
    if not back:
        return ModelResponse(parts=[ToolCallPart(*DETAIL)])
    return ModelResponse(parts=[TextPart("read it")])


async def _child_stream(messages: list[ModelMessage], info: AgentInfo):
    part = _child_fn(messages, info).parts[0]
    if isinstance(part, ToolCallPart):
        yield {0: DeltaToolCall(name=part.tool_name, json_args=part.args_as_json_str())}
    else:
        yield part.content


async def test_delegate_findings_land_in_the_same_run(target_servers):
    from agents.registry import _attach_delegation_tool, _sanitize_tool_name

    specialist = Agent(
        FunctionModel(_child_fn, stream_function=_child_stream),
        toolsets=[ReadOnlyGuard(arc1_toolset([]))],
    )
    row = SimpleNamespace(name="abap-diag", description="d", mcp_servers_json="[]")
    tool_name = _sanitize_tool_name(row.name)

    def parent_fn(messages, info):
        if not [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]:
            return ModelResponse(parts=[ToolCallPart(tool_name, {"query": "go"})])
        return ModelResponse(parts=[TextPart("done")])

    parent = Agent(FunctionModel(parent_fn))
    _attach_delegation_tool(parent, specialist, row)  # type: ignore[arg-type]
    with bound() as b:
        await parent.run("hi")
    assert [f["ref_id"] for f in b.run.findings] == [DUMP_ID]
    assert "User: DEVUSER01" in b.run.findings[0]["detail"]


async def test_deep_subagent_findings_land_in_the_same_run(target_servers):
    def fn(messages, info):
        if "task" not in {t.name for t in info.function_tools}:  # the sub-agent
            return _child_fn(messages, info)
        if not [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]:
            return ModelResponse(parts=[ToolCallPart("task", {"description": "dump"})])
        return ModelResponse(parts=[TextPart("done")])

    model = FunctionModel(fn)
    servers = [ReadOnlyGuard(arc1_toolset([]))]
    cfg = DeepConfig(enabled=True, subagent_max_depth=1, subagent_instructions="")
    ts = deep_toolset(cfg, parent_toolsets=servers, model=model, agent_name="p")
    with bound() as b:
        result = await Agent().run("go", model=model, toolsets=[*servers, ts])
    assert result.output == "done"
    assert [f["ref_id"] for f in b.run.findings] == [DUMP_ID]
    assert "User: DEVUSER01" in b.run.findings[0]["detail"]


async def test_calls_from_other_tasks_land_in_the_same_run(target_servers):
    """Contexts are copied per task; the run object in them is the same."""
    guard = ReadOnlyGuard(arc1_toolset([]))

    async def one(turns):
        await Agent().run("go", model=scripted(turns), toolsets=[guard])

    with bound() as b:
        await asyncio.gather(
            asyncio.create_task(one([[LIST], "a"])),
            asyncio.create_task(one([[(T, {"action": "gateway_errors"})], "b"])),
        )
    assert {f["kind"] for f in b.run.findings} == {"dump", "gateway_error"}
    assert len(b.run.findings) == 5


def test_investigate_allows_subagents():
    assert SUBAGENTS_ALLOWED[Stage.investigate] is True


# --- runner: stored and emitted -----------------------------------------------------


class _Specialist:
    def __init__(self, turns: list, toolsets: list, fail: bool = False):
        self.turns, self.toolsets, self.fail = turns, toolsets, fail

    def _turn(self, messages):
        n = sum(1 for m in messages if isinstance(m, ModelResponse))
        if self.fail and n == len(self.turns) - 1:
            raise RuntimeError("model is gone")
        return self.turns[min(n, len(self.turns) - 1)]

    async def stream(self, messages, info):
        turn = self._turn(messages)
        if isinstance(turn, str):
            yield turn
            return
        yield {i: DeltaToolCall(name=t, json_args=json.dumps(a), tool_call_id=f"c{i}-{t}")
               for i, (t, a) in enumerate(turn)}

    async def fn(self, messages, info):
        turn = self._turn(messages)
        if isinstance(turn, str):
            return ModelResponse(parts=[TextPart(turn)])
        return ModelResponse(parts=[ToolCallPart(t, a) for t, a in turn])

    async def run(self, prompt, **kwargs):
        model = FunctionModel(self.fn, stream_function=self.stream)
        return await Agent().run(prompt, model=model, toolsets=self.toolsets, **kwargs)


TURNS = [[LIST], [DETAIL], "The dump is a division by zero."]


def _install(turns: list = TURNS, fail: bool = False) -> list:
    calls: list = []
    spec = _Specialist(turns, [ReadOnlyGuard(arc1_toolset(calls))], fail)
    registry._build = BuildResult(
        orchestrator=None,
        specialists={"abap-orchestrator": spec, "abap-diagnostics": spec},
        mcp_clients=[], configs=[],
    )
    return calls


class Events(list):
    def __call__(self, kind: str, data: dict) -> None:
        self.append((kind, data))

    def of(self, kind: str) -> list[dict]:
        return [d for k, d in self if k == kind]


async def _rows(sid: str) -> list[IdeFinding]:
    async with SessionLocal() as db:
        return await list_findings(db, sid)


async def test_run_persists_findings_and_emits_events(clean_db, target_servers):
    """Raw run on a non_production target: rows, events, detail text."""
    async with SessionLocal() as db:
        await upsert_conventions(db, "T1", destination=DEST,
                                 actor="test-admin", non_production=True)
    _install()
    sid = await _session()
    ev = Events()
    await runner.run_stage(sid, "alice", "why the dump?", emit=ev)

    assert ev.of("error") == [] and ev[-1][0] == "done"
    rows = await _rows(sid)
    assert {r.ref_id for r in rows} == {f["ref_id"] for f in EXPECTED["dumps_list.json"][1]}
    by_ref = {r.ref_id: r for r in rows}
    first = by_ref[DUMP_ID]
    assert (first.kind, first.title, first.line) == ("dump", "COMPUTE_INT_ZERODIVIDE", 12)
    assert "User: DEVUSER01" in first.detail
    assert all(r.detail is None for r in rows if r.ref_id != DUMP_ID)

    emitted = ev.of("finding")
    assert len(emitted) == 3
    assert {e["id"] for e in emitted} == {r.id for r in rows}
    for e in emitted:
        assert set(e) == KEYS | {"id", "created_at"}  # the §1.2 Finding, no detail
    kinds = [k for k, _ in ev]
    assert kinds.index("finding") < kinds.index("done")

    # A second run over the same dumps updates the rows, it adds none.
    ev2 = Events()
    await runner.run_stage(sid, "alice", "again", emit=ev2)
    again = await _rows(sid)
    assert {r.id for r in again} == {r.id for r in rows}
    assert {e["id"] for e in ev2.of("finding")} == {r.id for r in rows}
    assert current_diagnose.get() is None


@pytest.mark.parametrize("conventions", ["flag_off", "no_row"])
async def test_run_is_refused_without_the_flag(clean_db, target_servers, conventions):
    if conventions == "flag_off":
        async with SessionLocal() as db:
            await upsert_conventions(db, "T1", destination=DEST,
                                     actor="test-admin", non_production=False)
    calls = _install()
    sid = await _session()
    ev = Events()
    with pytest.raises(StageGateError) as e:
        await runner.run_stage(sid, "alice", "why the dump?", emit=ev)
    assert e.value.code == "target_not_non_production"
    assert list(ev) == [] and calls == [] and await _rows(sid) == []


async def test_failed_run_still_stores_what_was_read(clean_db, target_servers):
    async with SessionLocal() as db:
        await upsert_conventions(db, "T1", destination=DEST,
                                 actor="test-admin", non_production=True)
    _install(fail=True)
    sid = await _session()
    ev = Events()
    await runner.run_stage(sid, "alice", "why?", emit=ev)
    assert [e["code"] for e in ev.of("error")] == ["run_failed"]
    assert len(await _rows(sid)) == 3 and len(ev.of("finding")) == 3
    assert ev[-1][0] == "done"


async def test_storing_findings_failing_does_not_break_the_run(
    clean_db, target_servers, monkeypatch
):
    async def broken(*a, **k):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(runner, "upsert_findings", broken)
    async with SessionLocal() as db:
        await upsert_conventions(db, "T1", destination=DEST,
                                 actor="test-admin", non_production=True)
    _install()
    sid = await _session()
    ev = Events()
    await runner.run_stage(sid, "alice", "why?", emit=ev)
    assert ev.of("finding") == []
    assert ev.of("error") == []
    assert ev[-1] == ("done", {**ev[-1][1], "status": "idle"})


async def test_change_session_has_no_findings(clean_db, target_servers):
    _install([[(T, {"action": "atc"})], "ok"])
    sid = await _session("change")
    ev = Events()
    await runner.run_stage(sid, "alice", "check", emit=ev)
    assert ev.of("finding") == [] and await _rows(sid) == []
