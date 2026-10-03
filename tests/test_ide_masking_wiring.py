"""Where masking is wired in (plan 1c Task 6, decision override 2026-10-03).

One switch, ``agents.ide.diagnose.masking_required``: a diagnose session on a
target flagged ``non_production`` is RAW (results reach the model as ARC-1
sent them, activity is stored as produced, the user's text is stored
verbatim); any other target -- flag off, no conventions row, conventions that
cannot be loaded -- is MASKED: every tool result passes the masker before
the model sees it, the stored activity carries no tool output and the user's
text is masked before it is stored.

Both cases are tested at each of the three places the switch is used: the
guard around an agent's toolsets (top-level agent, delegate, deep
sub-agent), ``Arc1Client`` (direct routes) and the runner (what is stored).

Run:  python -m pytest tests/test_ide_masking_wiring.py -q
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
(ROOT / "tests" / "_test_ide_masking_wiring.db").unlink(missing_ok=True)
os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_ide_masking_wiring.db'}",
)
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from pydantic_ai import Agent, ModelRetry  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    ModelMessage,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import (  # noqa: E402
    AgentInfo,
    DeltaToolCall,
    FunctionModel,
)
from pydantic_ai.toolsets import FunctionToolset  # noqa: E402
from sqlalchemy import select  # noqa: E402

from agents.auth import current_jwt  # noqa: E402
from agents.db import Base, SessionLocal, init_db  # noqa: E402
from agents.deep import (  # noqa: E402
    DeepConfig,
    DeepState,
    WorkspaceScope,
    current_workspace,
    deep_toolset,
)
from agents.ide import arc1, diagnose, routes, runner  # noqa: E402
from agents.ide.diagnose import (  # noqa: E402
    STRIPPED_OUTPUT,
    DiagnoseRun,
    current_diagnose,
    masking_required,
    strip_activity,
)
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
from agents.ide.stages import StageGateError  # noqa: E402
from agents.ide.store import create_session, upsert_conventions  # noqa: E402
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
NAME = "abap-orchestrator"
DEST = "arc1-abap-readonly"
SID = "s-wiring"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def assert_clean(text: str, where: str = "") -> None:
    for s in SENTINELS:
        assert s.lower() not in text.lower(), f"{s} survived {where}"


# --- harness -------------------------------------------------------------------


def arc1_toolset(calls: list, prefix: str = ""):
    """Stands in for one MCP server: SAPDiagnose and SAPRead, optionally
    behind a registry tool prefix."""
    ts = FunctionToolset()

    @ts.tool_plain
    def SAPDiagnose(action: str, id: str = "") -> str:  # noqa: N802, A002
        """Diagnose."""
        calls.append((prefix, "SAPDiagnose", action))
        if action == "atc":
            return json.dumps({"findings": [], "lastChangedBy": "DEVUSER01",
                               "note": "mail jane.doe@example.com"})
        if action == "sql_trace_state":
            return load("unknown_shape.json")
        return load("dump_detail.json" if id else "dumps_list.json")

    @ts.tool_plain
    def SAPRead(type: str, name: str = "") -> str:  # noqa: N802, A002
        """Read."""
        calls.append((prefix, "SAPRead", type))
        return json.dumps({
            "source": "REPORT zdemo.\n* contact jane.doe@example.com",
            "author": "DEVUSER01",
            "versions": [{"changedBy": "devuser01", "text": "by DEVUSER01"}],
        })

    return ts.prefixed(prefix) if prefix else ts


@pytest.fixture
def target_servers(monkeypatch):
    """Which wrapped toolsets count as the session target's ARC-1 server.
    A ``FunctionToolset`` has no destination to recognise, so the tests name
    them: everything by default, or only the ones added to the list."""
    only: list = []

    def _is_target(toolset, run) -> bool:
        return not only or any(toolset is t for t in only)

    monkeypatch.setattr(diagnose, "is_target_server", _is_target)
    return only


class bound:
    """Bind an IDE scope and (optionally) a diagnose run, as the runner does."""

    def __init__(self, session_type="diagnose", masking: bool | None = True,
                 run_sid: str = SID):
        self.scope = WorkspaceScope(
            session_id=SID, state=DeepState(run_id=SID), session_type=session_type
        )
        self.run = None if masking is None else DiagnoseRun(
            session_id=run_sid, owner="alice", target="T1", run_id="r-1",
            destination=DEST, masking=masking,
        )

    def __enter__(self):
        self._w = current_workspace.set(self.scope)
        self._d = current_diagnose.set(self.run)
        return self

    def __exit__(self, *exc):
        current_diagnose.reset(self._d)
        current_workspace.reset(self._w)


def scripted(calls_then_text: list):
    """A model that plays ``calls_then_text`` turn by turn and records every
    tool return / retry prompt it is shown."""
    seen: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen[:] = [
            str(p.content) if isinstance(p, ToolReturnPart) else p.model_response()
            for m in messages for p in m.parts
            if isinstance(p, (ToolReturnPart, RetryPromptPart))
        ]
        n = sum(1 for m in messages if isinstance(m, ModelResponse))
        turn = calls_then_text[min(n, len(calls_then_text) - 1)]
        if isinstance(turn, str):
            return ModelResponse(parts=[TextPart(turn)])
        return ModelResponse(parts=[ToolCallPart(t, a) for t, a in turn])

    return FunctionModel(fn), seen


async def run_agent(turns: list, toolsets: list) -> list[str]:
    model, seen = scripted(turns)
    await Agent().run("go", model=model, toolsets=toolsets)
    return seen


DETAIL = ("SAPDiagnose", {"action": "dumps", "id": "20261003101530DEVHOST_DEMO_00"})


# --- the switch ------------------------------------------------------------------


@pytest.mark.parametrize("conventions,required", [
    (None, True),                                    # not loaded / no row
    (SimpleNamespace(non_production=True), False),   # the only raw case
    (SimpleNamespace(non_production=False), True),
    (SimpleNamespace(non_production=None), True),    # unknown flag
    (SimpleNamespace(non_production="true"), True),  # not a bool: unknown
    (SimpleNamespace(non_production=1), True),
    (SimpleNamespace(), True),                       # no such attribute
    ({"non_production": True}, True),                # not a conventions row
])
def test_masking_required_fails_safe(conventions, required):
    assert masking_required(conventions) is required


def test_masking_required_on_real_rows():
    assert masking_required(IdeConventions(target="P", non_production=False)) is True
    assert masking_required(IdeConventions(target="Q", non_production=True)) is False
    assert masking_required(IdeConventions(target="R")) is True  # flag not set yet


def test_diagnose_run_masks_by_default():
    run = DiagnoseRun(session_id="s", owner="o", target="T", run_id="r")
    assert run.masking is True


# --- guard: masked ---------------------------------------------------------------


async def test_guard_masks_dump_result_before_model_sees_it(target_servers):
    calls: list = []
    with bound(masking=True):
        seen = await run_agent([[DETAIL], "done"], [ReadOnlyGuard(arc1_toolset(calls))])
    assert calls == [("", "SAPDiagnose", "dumps")]
    assert len(seen) == 1
    assert_clean(seen[0], "in what the model saw")
    assert "USER_1" in seen[0]
    assert json.loads(seen[0])["runtimeError"] == "COMPUTE_INT_ZERODIVIDE"


async def test_guard_passes_raw_result_for_a_non_production_target(target_servers):
    calls: list = []
    with bound(masking=False):
        seen = await run_agent([[DETAIL], "done"], [ReadOnlyGuard(arc1_toolset(calls))])
    assert seen == [load("dump_detail.json")]


@pytest.mark.parametrize("run_sid", [None, "another-session"])
async def test_guard_refuses_diagnose_session_without_its_run_binding(
    target_servers, run_sid
):
    """No run bound (or another session's): the guard cannot know whether to
    mask, so nothing is forwarded and no tool is offered."""
    calls: list = []

    class _Stub:
        async def get_tools(self, ctx):
            return {"SAPDiagnose": object(), "SAPRead": object()}

        async def call_tool(self, name, tool_args, ctx, tool):
            calls.append(name)
            return load("dump_detail.json")

    guard = ReadOnlyGuard(_Stub())  # type: ignore[arg-type]
    with (bound(masking=None) if run_sid is None
          else bound(masking=False, run_sid=run_sid)):
        assert await guard.get_tools(None) == {}  # type: ignore[arg-type]
        for name, args in (DETAIL, ("SAPRead", {"type": "CLAS", "name": "ZCL_X"})):
            with pytest.raises(ModelRetry, match="Refused in the read-only IDE"):
                await guard.call_tool(name, args, None, None)  # type: ignore[arg-type]
    assert calls == []


async def test_guard_masks_every_tool_result_not_only_diagnose_data(target_servers):
    calls: list = []
    turns = [
        [("SAPRead", {"type": "CLAS", "name": "ZCL_X"})],
        [("SAPDiagnose", {"action": "atc"})],
        "done",
    ]
    with bound(masking=True):
        seen = await run_agent(turns, [ReadOnlyGuard(arc1_toolset(calls))])
    assert len(calls) == 2 and len(seen) == 2
    for text in seen:
        assert_clean(text, "in a non-data result")
    assert "REPORT zdemo." in seen[0]  # source stays readable
    assert "USER_1" in seen[0] and "[EMAIL]" in seen[0]

    calls.clear()
    with bound(masking=False):
        raw = await run_agent(turns, [ReadOnlyGuard(arc1_toolset(calls))])
    assert "DEVUSER01" in raw[0] and "jane.doe@example.com" in raw[1]


async def test_guard_unknown_shape_is_metadata_only(target_servers):
    calls: list = []
    with bound(masking=True):
        seen = await run_agent(
            [[("SAPDiagnose", {"action": "sql_trace_state"})], "done"],
            [ReadOnlyGuard(arc1_toolset(calls))],
        )
    assert json.loads(seen[0]) == {
        "masked": True, "shape": "unknown",
        "chars": len(load("unknown_shape.json")), "keys": 1,
    }


@pytest.mark.parametrize("masking", [True, False])
async def test_arc1_error_propagates_unchanged(target_servers, masking):
    """An ARC-1 error object is not data: the model gets it as it came."""
    calls: list = []
    with bound(masking=masking):
        seen = await run_agent(
            [[("SAPDiagnose", {"action": "dumps"})], "done"],
            [ReadOnlyGuard(_raising(calls))],
        )
    assert calls == ["dumps"]
    assert load("arc1_error.json") in seen[0]


def _raising(calls: list):
    ts = FunctionToolset()

    @ts.tool_plain
    def SAPDiagnose(action: str) -> str:  # noqa: N802
        """Diagnose."""
        calls.append(action)
        raise ModelRetry(load("arc1_error.json"))

    return ts


# --- guard: only the session target's ARC-1 server gets diagnose rights ----------


@pytest.mark.parametrize("masking", [True, False])
async def test_other_server_gets_no_diagnose_rights_and_no_bypass(
    target_servers, masking
):
    calls: list = []
    target, other = arc1_toolset(calls), arc1_toolset(calls, "other")
    target_servers.append(target)
    turns = [
        [("other_SAPDiagnose", {"action": "dumps"})],          # refused
        [("other_SAPRead", {"type": "CLAS", "name": "ZCL_X"})],  # change policy
        [("SAPDiagnose", {"action": "dumps"})],                # the target: allowed
        "done",
    ]
    with bound(masking=masking):
        seen = await run_agent(turns, [ReadOnlyGuard(target), ReadOnlyGuard(other)])
    assert calls == [("other", "SAPRead", "CLAS"), ("", "SAPDiagnose", "dumps")]
    assert "Refused in the read-only IDE" in seen[0]
    if masking:
        for text in seen[1:]:
            assert_clean(text, "from a server")
    else:
        assert "DEVUSER01" in seen[1] and seen[2] == load("dumps_list.json")


def _server(url: str, mode: str, oauth: dict | None = None, prefix: str | None = None):
    from agents.shared import create_mcp_server

    return create_mcp_server("s", url, mode, tool_prefix=prefix, oauth=oauth)


def test_is_target_server_by_destination(monkeypatch):
    monkeypatch.setenv("DESTINATION_URI", "https://destination.example.test")
    monkeypatch.setenv("DESTINATION_TOKEN_URL", "https://auth.example.test/oauth/token")
    monkeypatch.setenv("DESTINATION_CLIENT_ID", "id")
    monkeypatch.setenv("DESTINATION_CLIENT_SECRET", "not-a-secret")
    run = DiagnoseRun(session_id="s", owner="o", target="T1", run_id="r",
                      destination=DEST)
    mine = _server("https://x.example.test/mcp", "destination",
                   {"destination": DEST, "user_context": True}, prefix="arc1")
    theirs = _server("https://x.example.test/mcp", "destination",
                     {"destination": "some-other-destination"})
    plain = _server("https://x.example.test/mcp", "none")
    assert diagnose.is_target_server(mine, run) is True
    assert diagnose.is_target_server(ReadOnlyGuard(mine), run) is True  # unwraps
    assert diagnose.is_target_server(theirs, run) is False
    assert diagnose.is_target_server(plain, run) is False
    assert diagnose.is_target_server(FunctionToolset(), run) is False


def test_is_target_server_by_url_without_destination(monkeypatch):
    run = DiagnoseRun(session_id="s", owner="o", target="t-1", run_id="r")
    mine = _server("http://localhost:3000", "none")
    other = _server("http://localhost:3001/mcp", "none")
    monkeypatch.delenv("IDE_ARC1_URL_T_1", raising=False)
    assert diagnose.is_target_server(mine, run) is False  # nothing configured
    monkeypatch.setenv("IDE_ARC1_URL_T_1", "http://localhost:3000/mcp")
    assert diagnose.is_target_server(mine, run) is True
    assert diagnose.is_target_server(other, run) is False


# --- guard: delegates and deep sub-agents ---------------------------------------


def _child_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Reads one dump, then answers with exactly what came back."""
    back = [p for m in messages for p in m.parts
            if isinstance(p, (ToolReturnPart, RetryPromptPart))]
    if not back:
        return ModelResponse(parts=[ToolCallPart(*DETAIL)])
    return ModelResponse(parts=[TextPart(str(back[-1].content))])


async def _child_stream(messages: list[ModelMessage], info: AgentInfo):
    part = _child_fn(messages, info).parts[0]
    if isinstance(part, ToolCallPart):
        yield {0: DeltaToolCall(name=part.tool_name, json_args=part.args_as_json_str())}
    else:
        yield part.content


@pytest.mark.parametrize("masking", [True, False])
async def test_delegate_gets_the_same_treatment(target_servers, masking):
    from agents.registry import _attach_delegation_tool, _sanitize_tool_name

    calls: list = []
    specialist = Agent(
        FunctionModel(_child_fn, stream_function=_child_stream),
        toolsets=[ReadOnlyGuard(arc1_toolset(calls))],
    )
    row = SimpleNamespace(name="abap-diag", description="d", mcp_servers_json="[]")
    tool_name = _sanitize_tool_name(row.name)

    def parent_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart(tool_name, {"query": "go"})])
        return ModelResponse(parts=[TextPart(str(returns[-1].content))])

    parent = Agent(FunctionModel(parent_fn))
    _attach_delegation_tool(parent, specialist, row)  # type: ignore[arg-type]
    with bound(masking=masking):
        result = await parent.run("hi")
    assert calls == [("", "SAPDiagnose", "dumps")]
    if masking:
        assert_clean(result.output, "through a delegate")
        assert "USER_1" in result.output
    else:
        assert "DEVUSER01" in result.output and "jane.doe@example.com" in result.output


@pytest.mark.parametrize("masking", [True, False])
async def test_deep_subagent_gets_the_same_treatment(target_servers, masking):
    calls: list = []
    seen: dict[str, str] = {}

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if "task" not in {t.name for t in info.function_tools}:  # the sub-agent
            response = _child_fn(messages, info)
            if isinstance(response.parts[0], TextPart):
                seen["child"] = response.parts[0].content
            return response
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("task", {"description": "dump"})])
        seen["parent"] = str(returns[-1].content)
        return ModelResponse(parts=[TextPart("done")])

    model = FunctionModel(fn)
    servers = [ReadOnlyGuard(arc1_toolset(calls))]
    cfg = DeepConfig(enabled=True, subagent_max_depth=1, subagent_instructions="")
    ts = deep_toolset(cfg, parent_toolsets=servers, model=model, agent_name="p")
    with bound(masking=masking):
        result = await Agent().run("go", model=model, toolsets=[*servers, ts])
    assert result.output == "done"
    assert calls == [("", "SAPDiagnose", "dumps")]
    if masking:
        assert_clean(seen["child"], "in the sub-agent")
        assert_clean(seen["parent"], "in the parent")
        assert "USER_1" in seen["child"]
    else:
        assert "DEVUSER01" in seen["child"]


async def test_change_session_guard_is_untouched(target_servers):
    """A change session never masks and needs no diagnose run."""
    calls: list = []
    with bound(session_type="change", masking=None):
        seen = await run_agent(
            [[("SAPRead", {"type": "CLAS", "name": "ZCL_X"})], [DETAIL], "done"],
            [ReadOnlyGuard(arc1_toolset(calls))],
        )
    assert calls == [("", "SAPRead", "CLAS")]
    assert "DEVUSER01" in seen[0]
    assert "Refused in the read-only IDE" in seen[1]


# --- strip_activity ----------------------------------------------------------------


def test_strip_activity_drops_every_tool_output():
    activity = {
        "events": [
            {"kind": "tool", "tool": "SAPDiagnose", "detail": "action=dumps",
             "status": "ok", "output": "DEVUSER01", "id": "1"},
            {"kind": "tool", "tool": "read_file", "status": "error",
             "output": "DEVUSER01", "code": "readonly_refused", "id": "2"},
            {"kind": "note", "detail": "thinking"},
        ],
        "plan": [{"content": "look", "status": "pending"}],
        "dropped": 0,
    }
    out = strip_activity(activity)
    assert [e.get("output") for e in out["events"]] == [
        STRIPPED_OUTPUT, STRIPPED_OUTPUT, None]
    assert out["events"][1]["code"] == "readonly_refused"
    assert out["events"][0]["detail"] == "action=dumps"
    assert out["plan"] == activity["plan"] and out["dropped"] == 0
    assert activity["events"][0]["output"] == "DEVUSER01"  # the input is not changed
    assert STRIPPED_OUTPUT == "(not stored: diagnose data)"


# --- Arc1Client (direct routes) ------------------------------------------------------


class _FakeRun:
    def __init__(self, result="ok"):
        self.result, self.calls = result, []

    async def direct_call_tool(self, name, args):
        self.calls.append((name, args))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.fixture
def mcp(monkeypatch):
    run = _FakeRun()

    class _Server:
        async def for_run(self, ctx):
            return run

    monkeypatch.setattr(arc1.shared, "create_mcp_server", lambda *a, **kw: _Server())
    arc1._SERVERS.clear()
    token = current_jwt.set("user.jwt.token")
    yield run
    current_jwt.reset(token)
    arc1._SERVERS.clear()


async def test_arc1_client_masks_direct_diagnose_reads(mcp):
    mcp.result = load("dumps_list.json")
    client = arc1.Arc1Client("T1", DEST, policy="diagnose")  # masking defaults on
    out = await client.call("SAPDiagnose", {"action": "dumps"})
    assert_clean(out, "in a direct read")
    assert "USER_1" in out and len(json.loads(out)["dumps"]) >= 2
    # A fresh pseudonym map per request.
    mcp.result = load("dump_detail.json")
    assert "USER_1" in await client.call("SAPDiagnose", {"action": "dumps", "id": "x"})
    # ... and every other result of a diagnose session is masked too.
    mcp.result = "* by jane.doe@example.com"
    assert await client.call("SAPRead", {"type": "PROG", "name": "ZX"}) == "* by [EMAIL]"


async def test_arc1_client_is_raw_for_a_non_production_target(mcp):
    mcp.result = load("dumps_list.json")
    client = arc1.Arc1Client("T1", DEST, policy="diagnose", masking=False)
    assert await client.call("SAPDiagnose", {"action": "dumps"}) == load("dumps_list.json")


async def test_arc1_client_change_policy_refuses_dumps(mcp):
    for client in (arc1.Arc1Client("T1", DEST), arc1.get_arc1_client("T1", DEST)):
        assert client.policy == "change"
        with pytest.raises(arc1.Arc1Refused) as exc:
            await client.call("SAPDiagnose", {"action": "dumps"})
        assert exc.value.status_code == 403
    assert mcp.calls == []


async def test_arc1_client_change_policy_never_masks(mcp):
    mcp.result = "* by jane.doe@example.com"
    client = arc1.Arc1Client("T1", DEST)
    assert await client.call("SAPRead", {"type": "PROG", "name": "ZX"}) == mcp.result


@pytest.mark.parametrize("masking", [True, False])
async def test_arc1_client_never_runs_approval_actions(mcp, masking):
    client = arc1.Arc1Client("T1", DEST, policy="diagnose", masking=masking)
    for action in ("trace_start", "trace_cancel"):
        with pytest.raises(arc1.Arc1Refused):
            await client.call("SAPDiagnose", {"action": action})
    with pytest.raises(arc1.Arc1Refused):
        await client.call("SAPDiagnose", {"action": "dumps", "user": "DEVUSER01"})
    assert mcp.calls == []


@pytest.mark.parametrize("masking", [True, False])
async def test_arc1_client_error_payload_is_an_error_not_data(mcp, masking):
    client = arc1.Arc1Client("T1", DEST, policy="diagnose", masking=masking)
    for result in (load("arc1_error.json"), ModelRetry(load("arc1_error.json"))):
        mcp.result = result
        with pytest.raises(arc1.Arc1Error) as exc:
            await client.call("SAPDiagnose", {"action": "dumps"})
        assert exc.value.status_code == 502
        assert exc.value.code == "sap_authentication_failed"


# --- routes: the client follows the session ----------------------------------------


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


@pytest.mark.parametrize("flag,masking", [(True, False), (False, True)])
async def test_client_for_passes_policy_and_switch(clean_db, monkeypatch, flag, masking):
    got: list = []

    def factory(target, destination="", **kwargs):
        got.append((target, destination, kwargs))
        return object()

    monkeypatch.setattr(arc1, "get_arc1_client", factory)
    async with SessionLocal() as db:
        await upsert_conventions(db, "T1", destination=DEST, non_production=flag)
        await routes._client_for(db, "T1", "diagnose")
        await routes._client_for(db, "T1")
    assert got == [
        ("T1", DEST, {"policy": "diagnose", "masking": masking}),
        ("T1", DEST, {"policy": "change", "masking": masking}),
    ]


# --- runner: what is stored -----------------------------------------------------------


class _Specialist:
    def __init__(self, turns: list, toolsets: list):
        self.turns, self.toolsets = turns, toolsets
        self.seen: list[str] = []
        self.prompts: list[str] = []

    async def _turn(self, messages):
        self.seen[:] = [str(p.content) for m in messages for p in m.parts
                        if isinstance(p, ToolReturnPart)]
        self.prompts[:] = [str(p.content) for m in messages for p in m.parts
                           if isinstance(p, UserPromptPart)]
        n = sum(1 for m in messages if isinstance(m, ModelResponse))
        return self.turns[min(n, len(self.turns) - 1)]

    async def fn(self, messages, info):
        turn = await self._turn(messages)
        if isinstance(turn, str):
            return ModelResponse(parts=[TextPart(turn)])
        return ModelResponse(parts=[ToolCallPart(t, a) for t, a in turn])

    async def stream(self, messages, info):
        turn = await self._turn(messages)
        if isinstance(turn, str):
            yield turn
            return
        yield {i: DeltaToolCall(name=t, json_args=json.dumps(a), tool_call_id=f"c{i}-{t}")
               for i, (t, a) in enumerate(turn)}

    async def run(self, prompt, **kwargs):
        model = FunctionModel(self.fn, stream_function=self.stream)
        ts = deep_toolset(DeepConfig(enabled=True, subagent_max_depth=1),
                          parent_toolsets=[], model=model, agent_name=NAME)
        return await Agent().run(prompt, model=model,
                                 toolsets=[*self.toolsets, ts], **kwargs)


TURNS = [
    [("SAPDiagnose", {"action": "dumps"})],
    [DETAIL],
    [("write_file", {"path": "notes/dump.md", "content": "see the dump"})],
    "The dump is a division by zero.",
]
USER_TEXT = "why did it dump for user DEVUSER01? mail jane.doe@example.com"


def _install(calls: list) -> _Specialist:
    spec = _Specialist(TURNS, [ReadOnlyGuard(arc1_toolset(calls))])
    # A diagnose run's top-level agent is ``abap-diagnostics``; the change
    # session of ``test_change_session_unaffected`` runs the orchestrator.
    registry._build = BuildResult(
        orchestrator=None, specialists={NAME: spec, "abap-diagnostics": spec},
        mcp_clients=[], configs=[],
    )
    return spec


async def _diagnose_session(session_type: str = "diagnose") -> str:
    async with SessionLocal() as db:
        s = await create_session(db, owner="alice", title="t", target="T1",
                                 session_type=session_type)
        return s.id


class Events(list):
    def __call__(self, kind: str, data: dict) -> None:
        self.append((kind, data))

    def of(self, kind: str) -> list[dict]:
        return [d for k, d in self if k == kind]


async def _everything_stored() -> str:
    """Every column of every row of every ``ide_*`` table, as one text."""
    parts: list[str] = []
    tables = [t for name, t in Base.metadata.tables.items() if name.startswith("ide_")]
    assert {t.name for t in tables} >= {
        "ide_sessions", "ide_messages", "ide_artifacts", "ide_workspace_files",
        "ide_findings", "ide_approvals", "ide_audit_log", "ide_conventions"}
    async with SessionLocal() as db:
        for table in tables:
            for row in (await db.execute(select(table))).all():
                parts.extend(str(value) for value in row)
    return "\n".join(parts)


async def _messages(sid: str) -> list[IdeMessage]:
    async with SessionLocal() as db:
        return list((await db.execute(
            select(IdeMessage).where(IdeMessage.session_id == sid)
            .order_by(IdeMessage.created_at))).scalars())


@pytest.mark.parametrize("conventions", ["flag_off", "no_row", "load_fails"])
async def test_diagnose_run_is_refused_without_the_flag(
    clean_db, target_servers, monkeypatch, conventions
):
    """A target that is not (known to be) non-production: no run, nothing
    read, nothing stored -- not even the user's text."""
    async with SessionLocal() as db:
        if conventions != "no_row":
            await upsert_conventions(db, "T1", destination=DEST,
                                     non_production=conventions == "load_fails")
    if conventions == "load_fails":
        async def broken(db, target):
            raise RuntimeError("database is gone")

        # The gate (stages) still reads the row; the runner's own read fails.
        monkeypatch.setattr(runner, "get_conventions", broken)
    calls: list = []
    spec = _install(calls)
    sid = await _diagnose_session()
    ev = Events()
    with pytest.raises(StageGateError) as e:
        await runner.run_stage(sid, "alice", USER_TEXT, feedback=None, emit=ev)
    assert e.value.code == "target_not_non_production"
    assert list(ev) == [] and calls == [] and spec.prompts == []
    assert await _messages(sid) == []
    assert_clean(await _everything_stored(), "in the database")
    async with SessionLocal() as db:
        row = await db.get(IdeSession, sid)
        assert (row.status, row.run_id) == ("idle", None)


_FORCED_TEXT = "why did it dump?"


async def _forced_masked_run(sid: str, text: str, ev, *, known: bool = True) -> None:
    """Run what is behind ``runner._start`` as a *masked* run.

    A diagnose run on a target that requires masking is refused at the start
    (see above), so no caller reaches this any more. The pipeline behind the
    start still fails towards masking -- ``_Start.masked`` defaults to true --
    and the test below keeps that covered: the start is taken while the
    target is flagged, then run as if it had said "masked" (``known=False``:
    as if the conventions could not be read either).
    """
    start = await runner._start(sid, "alice", text, None)
    start = dataclasses.replace(start, masked=True, conventions_known=known)
    await runner._execute(start, ev)


@pytest.mark.parametrize("known", [True, False])
async def test_masked_run_persists_no_sentinel(clean_db, target_servers, known):
    """Behind the start, a masked run is masked everywhere."""
    async with SessionLocal() as db:
        await upsert_conventions(db, "T1", destination=DEST, non_production=True)
    calls: list = []
    spec = _install(calls)
    sid = await _diagnose_session()
    ev = Events()
    await _forced_masked_run(sid, _FORCED_TEXT, ev, known=known)

    # Unknown conventions are a note of their own; the run still ends.
    assert [e["code"] for e in ev.of("error")] == (
        [] if known else ["conventions_unavailable"]
    )
    assert ev[-1][0] == "done"
    assert [c[2] for c in calls] == ["dumps", "dumps"]
    # What the model saw, what went over the stream, what is stored.
    assert len(spec.seen) == 3
    assert_clean("\n".join(spec.seen), "in what the model saw")
    assert_clean("\n".join(spec.prompts), "in the prompt")
    assert_clean(json.dumps(list(ev)), "in the stream")
    assert_clean(await _everything_stored(), "in the database")

    user, assistant = await _messages(sid)
    events = json.loads(assistant.activity_json)["events"]
    tools = [e for e in events if e["kind"] == "tool"]
    assert len(tools) == 3
    assert all(e["output"] == STRIPPED_OUTPUT for e in tools)
    # The live stream carried the masked output.
    live = [t for t in ev.of("tool") if t["tool"] == "SAPDiagnose" and t["status"] == "ok"]
    assert len(live) == 2 and all("USER_1" in t["output"] for t in live)
    # Diagnose never persists the workspace (D2).
    async with SessionLocal() as db:
        assert (await db.execute(select(IdeWorkspaceFile))).all() == []
    assert current_diagnose.get() is None and current_workspace.get() is None


async def test_non_production_diagnose_run_is_raw_and_stored(clean_db, target_servers):
    async with SessionLocal() as db:
        await upsert_conventions(db, "T1", destination=DEST, non_production=True)
    calls: list = []
    spec = _install(calls)
    sid = await _diagnose_session()
    ev = Events()
    await runner.run_stage(sid, "alice", USER_TEXT, feedback=None, emit=ev)

    assert ev.of("error") == []
    assert spec.seen[:2] == [load("dumps_list.json"), load("dump_detail.json")]
    assert USER_TEXT in "\n".join(spec.prompts)
    user, assistant = await _messages(sid)
    assert user.content == USER_TEXT
    events = json.loads(assistant.activity_json)["events"]
    outputs = [e["output"] for e in events
               if e["kind"] == "tool" and e["tool"] == "SAPDiagnose"]
    assert len(outputs) == 2
    assert all("DEVUSER01" in o and STRIPPED_OUTPUT not in o for o in outputs)
    # Still no persisted scratchpad (unchanged by the override).
    async with SessionLocal() as db:
        assert (await db.execute(select(IdeWorkspaceFile))).all() == []
    assert current_diagnose.get() is None


@pytest.mark.parametrize("flag", [True, False])
async def test_change_session_unaffected(clean_db, target_servers, flag):
    async with SessionLocal() as db:
        await upsert_conventions(db, "T1", destination=DEST, non_production=flag)
    calls: list = []
    spec = _install(calls)
    spec.turns = [
        [("SAPRead", {"type": "CLAS", "name": "ZCL_X"})],
        [("SAPDiagnose", {"action": "dumps"})],  # refused in a change session
        "ok",
    ]
    sid = await _diagnose_session("change")
    ev = Events()
    await runner.run_stage(sid, "alice", USER_TEXT, feedback=None, emit=ev)

    assert calls == [("", "SAPRead", "CLAS")]
    assert "DEVUSER01" in spec.seen[0]  # as phase 1a: not masked
    user, assistant = await _messages(sid)
    assert user.content == USER_TEXT
    events = [e for e in json.loads(assistant.activity_json)["events"]
              if e["kind"] == "tool"]
    assert "DEVUSER01" in events[0]["output"]
    assert events[1]["code"] == "readonly_refused"
    assert "Refused in the read-only IDE" in events[1]["output"]
