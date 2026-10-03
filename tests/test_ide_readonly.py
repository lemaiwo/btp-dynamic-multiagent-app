"""The ARC-1 read-only allowlist (``agents.ide.readonly``).

While an IDE session is bound (``agents.deep.current_workspace``), every MCP
server and built-in of a specialist is wrapped in ``ReadOnlyGuard``: tools
outside the policy are hidden and refused, and calls to allowed tools are
checked argument by argument. Unbound (chat, A2A, jobs, workflows) the guard
is a pass-through. The wrapper must also keep ``PerRunMCPServer``'s per-run
session copy, or overlapping runs share one user's MCP session.

Run:  python -m pytest tests/test_ide_readonly.py -q
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault(
    "DATABASE_URL", f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_ide_readonly.db'}"
)
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
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402
from pydantic_ai.models.test import TestModel  # noqa: E402
from pydantic_ai.toolsets import FunctionToolset  # noqa: E402

from agents.deep import (  # noqa: E402
    DeepConfig,
    DeepState,
    WorkspaceScope,
    current_workspace,
    deep_toolset,
)
from agents.ide.readonly import (  # noqa: E402
    APPROVAL_ACTIONS,
    DIAGNOSE_DATA_ACTIONS,
    DIAGNOSE_POLICY,
    POLICIES,
    READONLY_POLICY,
    READONLY_POLICY_SAPDIAGNOSE_ACTIONS,
    ReadOnlyGuard,
    check_call,
    is_diagnose_data,
    needs_approval,
    policy_name,
)

# Undo the import-time Agent / create_mcp_server stubs of other suites.
pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")


@pytest.fixture
def bound():
    scope = WorkspaceScope(session_id="s-1", state=DeepState(run_id="s-1"))
    token = current_workspace.set(scope)
    try:
        yield scope
    finally:
        current_workspace.reset(token)


# --- policy -------------------------------------------------------------------
@pytest.mark.parametrize("tool,args,ok", [
    ("SAPRead", {"type": "CLAS", "name": "ZCL_X"}, True),
    ("SAPRead", {"type": "TABLE_CONTENTS", "name": "MARA"}, False),
    ("SAPRead", {"type": "TABLE_QUERY", "name": "MARA"}, False),
    ("SAPRead", {"type": "table_contents", "name": "MARA"}, False),
    ("SAPSearch", {"query": "ZCL*"}, True),
    ("SAPSearch", {"query": "x", "searchType": "source_code"}, True),
    ("SAPSearch", {"searchType": "tadir_lookup"}, True),
    ("SAPSearch", {"searchType": "tadir_lookup", "source": "db"}, False),
    ("SAPSearch", {"query": "x", "source": "both"}, False),
    ("SAPSearch", {"query": "x", "searchType": "sql"}, False),
    ("SAPContext", {"action": "deps"}, True),
    ("SAPNavigate", {"action": "references"}, True),
    ("SAPLint", {"action": "lint"}, True),
    ("SAPLint", {"action": "lint_and_fix"}, True),
    ("SAPLint", {"action": "list_rules"}, True),
    ("SAPLint", {"action": "set_formatter_settings"}, False),
    ("SAPLint", {"action": "format"}, False),
    ("SAPLint", {}, False),
    ("SAPDiagnose", {"action": "atc"}, True),
    ("SAPDiagnose", {"action": "unittest"}, True),
    ("SAPDiagnose", {"action": "syntax"}, True),
    ("SAPDiagnose", {"action": "dumps"}, False),
    ("SAPDiagnose", {"action": "trace_start"}, False),
    ("SAPDiagnose", {"action": "set_sql_trace_state"}, False),
    ("SAPDiagnose", {"action": "something_new"}, False),
    ("SAPTransport", {"action": "list"}, True),
    ("SAPTransport", {"action": "diff"}, True),
    ("SAPTransport", {"action": "release"}, False),
    ("SAPTransport", {"action": "create"}, False),
    ("SAPWrite", {}, False),
    ("SAPActivate", {}, False),
    ("SAPManage", {}, False),
    ("SAPQuery", {}, False),
    ("SAPGit", {}, False),
    ("arc1_SAPRead", {"type": "CLAS"}, True),
    ("arc1_SAPWrite", {}, False),
    ("arc1_SAPRead", {"type": "TABLE_QUERY"}, False),
    ("SAPReadX", {}, False),
    ("Unknown", {}, False),
    ("send_mail", {}, False),
])
def test_policy(tool, args, ok):
    assert (check_call(tool, args) is None) is ok
    # The change policy is the default and is unchanged when named explicitly.
    assert (check_call(tool, args, "change") is None) is ok


@pytest.mark.parametrize("tool,args,ok", [
    # Non-string values never slip past a constrained argument.
    ("SAPRead", {"type": ["TABLE_QUERY"], "name": "MARA"}, False),
    ("SAPRead", {"type": ["CLAS"], "name": "ZCL_X"}, False),
    ("SAPRead", {"type": {"x": "TABLE_QUERY"}}, False),
    ("SAPRead", {"type": 1}, False),
    ("SAPSearch", {"query": "x", "source": ["db"]}, False),
    ("SAPSearch", {"query": "x", "searchType": ["object"]}, False),
    ("SAPDiagnose", {"action": ["atc", "dumps"]}, False),
    ("SAPTransport", {"action": True}, False),
    # SAPRead type is an allowlist: unknown types are refused.
    ("SAPRead", {"type": "DDLS", "name": "ZI_X"}, True),
    ("SAPRead", {"type": "VERSION_SOURCE"}, True),
    ("SAPRead", {"type": "NEW_DATA_TYPE"}, False),
    ("SAPRead", {"name": "ZCL_X"}, False),
    ("SAPRead", {"type": "CLAS", "action": "diff"}, True),
    ("SAPRead", {"type": "CLAS", "action": "write"}, False),
    # SAPSearch source is an allowlist; the server default (adt) is safe.
    ("SAPSearch", {"searchType": "tadir_lookup", "source": "adt"}, True),
    ("SAPSearch", {"searchType": "tadir_lookup", "source": "sql"}, False),
    # SAPContext / SAPNavigate actions pinned.
    ("SAPContext", {"name": "ZI_X"}, True),
    ("SAPContext", {"action": "impact", "name": "ZI_X"}, True),
    ("SAPContext", {"action": "usages", "name": "ZCL_X"}, True),
    ("SAPContext", {"action": "structure", "name": "MARA"}, True),
    ("SAPContext", {"action": "rewrite", "name": "ZCL_X"}, False),
    ("SAPNavigate", {"action": "definition"}, True),
    ("SAPNavigate", {"action": "hierarchy"}, True),
    ("SAPNavigate", {"action": "completion"}, True),
    ("SAPNavigate", {"action": "rename"}, False),
    ("SAPNavigate", {}, False),
])
def test_policy_tightened(tool, args, ok):
    assert (check_call(tool, args) is None) is ok
    assert (check_call(tool, args, "change") is None) is ok


def test_refusal_reason_truncates_model_values():
    reason = check_call("SAPRead", {"type": "X" * 5000})
    assert reason is not None and len(reason) < 400
    reason = check_call("Y" * 5000, {})
    assert reason is not None and len(reason) < 400


async def test_refusal_log_truncates_model_values(bound, caplog):
    from pydantic_ai import ModelRetry

    guard = ReadOnlyGuard(_arc1_toolset([]))
    with caplog.at_level("WARNING", logger="agents.ide.readonly"):
        with pytest.raises(ModelRetry):
            await guard.call_tool("Z" * 5000, {}, None, None)  # type: ignore[arg-type]
    assert caplog.records and all(len(r.getMessage()) < 800 for r in caplog.records)


def test_policy_tolerates_missing_args():
    assert check_call("SAPContext", None) is None
    assert check_call("SAPRead", None) is not None  # type is required
    assert check_call("SAPLint", None) is not None


def test_policy_names_match_the_contract():
    assert set(READONLY_POLICY) == {
        "SAPRead", "SAPSearch", "SAPContext", "SAPNavigate",
        "SAPLint", "SAPDiagnose", "SAPTransport",
    }


# --- diagnose policy (plan 1c, table 1.4) ---------------------------------------
@pytest.mark.parametrize("args,policy,ok", [
    ({"action": "dumps"}, "change", False),
    ({"action": "dumps"}, "diagnose", True),
    ({"action": "dumps", "user": "X"}, "diagnose", False),
    ({"action": "trace_requests", "traceUser": "X"}, "diagnose", False),
    ({"action": "traces", "id": "1", "analysis": "hitlist"}, "diagnose", True),
    ({"action": "odata_perf", "url": "/sap/opu/odata4/x"}, "diagnose", True),
    ({"action": "odata_perf", "url": "https://evil/x"}, "diagnose", False),
    ({"action": "set_sql_trace_state", "sqlOn": True}, "diagnose", False),
    ({"action": "atc"}, "diagnose", True),
    ({"action": "atc"}, "nonsense", False),
])
def test_diagnose_policy(args, policy, ok):
    assert (check_call("SAPDiagnose", args, policy) is None) is ok


@pytest.mark.parametrize("args,ok", [
    # Every data action is allowed in diagnose, refused in change.
    *[({"action": a}, True) for a in sorted(DIAGNOSE_DATA_ACTIONS - {"odata_perf"})],
    ({"action": "DUMPS"}, True),
    ({"action": "authorization_trace", "user": "DEVUSER01"}, False),
    ({"action": "authorization_trace", "user": ""}, False),
    ({"action": "dumps", "user": ["DEVUSER01"]}, False),
    ({"action": "dumps", "traceUser": 1}, False),
    ({"action": "dumps", "user": None}, True),  # absent, as in phase 1a
    # odata_perf url: a string path, no scheme, no other type.
    ({"action": "odata_perf"}, False),
    ({"action": "odata_perf", "url": ""}, False),
    ({"action": "odata_perf", "url": "sap/opu/x"}, False),
    ({"action": "odata_perf", "url": "//evil/x"}, False),
    ({"action": "odata_perf", "url": "/\\evil/x"}, False),
    ({"action": "odata_perf", "url": "/x?r=https://evil"}, False),
    # Whitespace and control characters: parsers strip them, "/\t/host" -> "//host".
    ({"action": "odata_perf", "url": "/\t/host"}, False),
    ({"action": "odata_perf", "url": "/\n/host"}, False),
    ({"action": "odata_perf", "url": "/\r/host"}, False),
    ({"action": "odata_perf", "url": "/sap/opu x"}, False),
    ({"action": "odata_perf", "url": "/sap/\x00x"}, False),
    ({"action": "odata_perf", "url": "/sap/\x7fx"}, False),
    ({"action": "odata_perf", "url": " /sap/opu/x"}, False),
    ({"action": "odata_perf", "url": "/sap/opu/x%20y?$top=1"}, True),
    # gateway_errors detailUrl: a path on the ADT error log of this system.
    ({"action": "gateway_errors"}, True),
    ({"action": "gateway_errors", "id": "0A1B", "errorType": "Frontend Error"}, True),
    ({"action": "gateway_errors", "detailUrl": "/sap/bc/adt/gw/errorlog/0A1B2C"}, True),
    # No query (it can name a user) and no percent-encoding (``%2e%2e``).
    ({"action": "gateway_errors", "detailUrl": "/sap/bc/adt/gw/errorlog/0A1B?x=1"}, False),
    ({"action": "gateway_errors",
      "detailUrl": "/sap/bc/adt/gw/errorlog/1?user=DEVUSER01"}, False),
    ({"action": "gateway_errors",
      "detailUrl": "/sap/bc/adt/gw/errorlog/%2e%2e/%2e%2e/oo/classes/zcl_x"}, False),
    ({"action": "gateway_errors", "detailUrl": "/sap/bc/adt/gw/errorlog/a%2fb"}, False),
    ({"action": "gateway_errors", "detailUrl": "/sap/bc/adt/gw/errorlog/a#b"}, False),
    ({"action": "gateway_errors", "detailUrl": "/sap/bc/adt/gw/errorlog/"}, False),
    ({"action": "gateway_errors", "detailUrl": "/sap/bc/adt/oo/classes/zcl_x"}, False),
    ({"action": "gateway_errors", "detailUrl": "/sap/opu/odata/sap/ZDEMO_SRV"}, False),
    ({"action": "gateway_errors",
      "detailUrl": "https://evil.example.com/sap/bc/adt/gw/errorlog/1"}, False),
    ({"action": "gateway_errors",
      "detailUrl": "//evil.example.com/sap/bc/adt/gw/errorlog/1"}, False),
    ({"action": "gateway_errors",
      "detailUrl": "/sap/bc/adt/gw/errorlog/../../oo/classes/zcl_x"}, False),
    ({"action": "gateway_errors", "detailUrl": "/sap/bc/adt/gw/errorlog//x"}, False),
    ({"action": "gateway_errors",
      "detailUrl": "/sap/bc/adt/gw/errorlog/1?r=https://evil"}, False),
    ({"action": "gateway_errors", "detailUrl": "/sap/bc/adt/gw/errorlog/a\\b"}, False),
    ({"action": "gateway_errors", "detailUrl": "/sap/bc/adt/gw/errorlog/a b"}, False),
    ({"action": "gateway_errors", "detailUrl": "/sap/bc/adt/gw/errorlog/a\tb"}, False),
    ({"action": "gateway_errors", "detailUrl": "/sap/bc/adt/gw/errorlog/a\x00"}, False),
    ({"action": "gateway_errors", "detailUrl": ["/sap/bc/adt/gw/errorlog/1"]}, False),
    ({"action": "gateway_errors", "detailUrl": ""}, False),
    ({"action": "gateway_errors", "detailurl": "/sap/bc/adt/gw/errorlog/1"}, False),
    ({"action": "gateway_errors", "DetailURL": "https://evil.example.com/"}, False),
    # Whatever the action: no other one takes a detailUrl.
    ({"action": "dumps", "detailUrl": "https://evil.example.com/"}, False),
    # Constrained keys are matched case-insensitively.
    ({"action": "dumps", "User": "DEVUSER01"}, False),
    ({"action": "dumps", "USER": "DEVUSER01"}, False),
    ({"action": "trace_requests", "TRACEUSER": "X"}, False),
    ({"action": "trace_requests", "traceuser": "X"}, False),
    ({"action": "trace_requests", "TraceUser": None}, False),
    ({"action": "atc", "Action": "dumps"}, False),
    ({"Action": "atc"}, False),
    ({"action": "odata_perf", "URL": "https://evil/x", "url": "/sap/x"}, False),
    ({"action": "odata_perf", "Url": "/sap/x"}, False),
    ({"action": "odata_perf", "url": ["/sap/opu/x"]}, False),
    ({"action": "odata_perf", "url": 1}, False),
    # Never callable from the agent through the policy text alone.
    ({"action": "set_sql_trace_state"}, False),
    ({"action": "something_new"}, False),
    ({"action": ["dumps"]}, False),
    ({}, False),
])
def test_diagnose_policy_arguments(args, ok):
    assert (check_call("SAPDiagnose", args, "diagnose") is None) is ok


@pytest.mark.parametrize("tool,args", [
    ("SAPRead", {"type": "CLAS", "Type": "TABLE_QUERY"}),
    ("SAPRead", {"TYPE": "CLAS"}),
    ("SAPSearch", {"query": "x", "Source": "db"}),
    ("SAPSearch", {"query": "x", "SEARCHTYPE": "object"}),
    ("SAPContext", {"ACTION": "deps"}),
    ("SAPTransport", {"action": "list", "Action": "release"}),
])
@pytest.mark.parametrize("policy", ["change", "diagnose"])
def test_case_variants_of_constrained_args_are_refused(tool, args, policy):
    assert check_call(tool, args, policy) is not None


def test_non_string_arg_keys_do_not_crash():
    assert check_call("SAPContext", {1: "x", "action": "deps"}) is None


@pytest.mark.parametrize("action", sorted(DIAGNOSE_DATA_ACTIONS | APPROVAL_ACTIONS))
def test_change_policy_refuses_every_diagnose_action(action):
    args = {"action": action, "url": "/sap/opu/x"}
    assert check_call("SAPDiagnose", args) is not None
    assert check_call("SAPDiagnose", args, "change") is not None


def test_diagnose_policy_shares_everything_else_with_change():
    assert set(DIAGNOSE_POLICY) == set(READONLY_POLICY)
    for name, rules in READONLY_POLICY.items():
        if name != "SAPDiagnose":
            assert DIAGNOSE_POLICY[name] == rules
    assert READONLY_POLICY_SAPDIAGNOSE_ACTIONS <= DIAGNOSE_POLICY["SAPDiagnose"][0].allow
    assert set(POLICIES) == {"change", "diagnose"}
    assert POLICIES["change"] is READONLY_POLICY


@pytest.mark.parametrize("tool,args", [
    ("SAPRead", {"type": "TABLE_CONTENTS", "name": "MARA"}),
    ("SAPWrite", {}),
    ("SAPQuery", {}),
    ("SAPSearch", {"query": "x", "source": "db"}),
    ("SAPTransport", {"action": "release"}),
])
def test_diagnose_policy_keeps_phase_1a_refusals(tool, args):
    assert check_call(tool, args, "diagnose") is not None


@pytest.mark.parametrize("policy", ["nonsense", "", "CHANGE ", None, 1])
def test_unknown_policy_refuses_everything(policy):
    assert check_call("SAPRead", {"type": "CLAS"}, policy) is not None  # type: ignore[arg-type]
    assert check_call("SAPContext", {}, policy) is not None  # type: ignore[arg-type]
    assert policy_name("SAPRead", policy) is None  # type: ignore[arg-type]


def test_trace_start_needs_approval_only_in_diagnose():
    for action in ("trace_start", "trace_cancel", "TRACE_START"):
        args = {"action": action}
        assert needs_approval("SAPDiagnose", args, "diagnose") is True
        assert needs_approval("arc1_SAPDiagnose", args, "diagnose") is True
        assert needs_approval("SAPDiagnose", args, "change") is False
        assert check_call("SAPDiagnose", args, "change") is not None
    assert needs_approval("SAPDiagnose", {"action": "dumps"}, "diagnose") is False
    assert needs_approval("SAPDiagnose", {"action": ["trace_start"]}, "diagnose") is False
    assert needs_approval("SAPRead", {"action": "trace_start"}, "diagnose") is False
    assert needs_approval("SAPDiagnose", {"action": "trace_start"}, "nonsense") is False
    assert APPROVAL_ACTIONS == {"trace_start", "trace_cancel"}


def test_is_diagnose_data():
    assert is_diagnose_data("SAPDiagnose", {"action": "dumps"})
    assert is_diagnose_data("arc1_SAPDiagnose", {"action": "Odata_Perf"})
    assert not is_diagnose_data("SAPDiagnose", {"action": "atc"})
    assert not is_diagnose_data("SAPDiagnose", {"action": "trace_start"})
    assert not is_diagnose_data("SAPDiagnose", {"action": ["dumps"]})
    assert not is_diagnose_data("SAPDiagnose", None)
    assert not is_diagnose_data("SAPRead", {"action": "dumps"})


# --- guard ----------------------------------------------------------------------
def _arc1_toolset(calls: list):
    ts = FunctionToolset()

    @ts.tool_plain
    def SAPRead(type: str, name: str = "") -> str:  # noqa: N802, A002
        """Read an object."""
        calls.append(("SAPRead", type, name))
        return f"source of {name}"

    @ts.tool_plain
    def SAPWrite(name: str = "") -> str:  # noqa: N802
        """Write an object."""
        calls.append(("SAPWrite", name))
        return "written"

    return ts


def _tool_names(info: AgentInfo) -> set[str]:
    return {t.name for t in info.function_tools}


async def test_guard_hides_write_tools_only_when_bound(bound):
    seen: dict[str, set[str]] = {}

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen["tools"] = _tool_names(info)
        return ModelResponse(parts=[TextPart("ok")])

    guard = ReadOnlyGuard(_arc1_toolset([]))
    agent = Agent()
    await agent.run("hi", model=FunctionModel(fn), toolsets=[guard])
    assert seen["tools"] == {"SAPRead"}

    current_workspace.set(None)
    await agent.run("hi", model=FunctionModel(fn), toolsets=[guard])
    assert seen["tools"] == {"SAPRead", "SAPWrite"}


async def test_guard_hides_prefixed_tools(bound):
    seen: dict[str, set[str]] = {}

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen["tools"] = _tool_names(info)
        return ModelResponse(parts=[TextPart("ok")])

    guard = ReadOnlyGuard(_arc1_toolset([]).prefixed("arc1"))
    await Agent().run("hi", model=FunctionModel(fn), toolsets=[guard])
    assert seen["tools"] == {"arc1_SAPRead"}


async def test_guard_refuses_call_with_bad_args_when_bound(bound, caplog):
    calls: list = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        retries = [
            p for m in messages for p in m.parts if isinstance(p, RetryPromptPart)
        ]
        if not retries:
            return ModelResponse(parts=[
                ToolCallPart("SAPRead", {"type": "TABLE_QUERY", "name": "MARA"})
            ])
        return ModelResponse(parts=[TextPart(str(retries[-1].content))])

    guard = ReadOnlyGuard(_arc1_toolset(calls))
    with caplog.at_level("WARNING", logger="agents.ide.readonly"):
        result = await Agent().run("hi", model=FunctionModel(fn), toolsets=[guard])
    assert "Refused in the read-only IDE" in result.output
    assert calls == []
    assert any("SAPRead" in r.getMessage() for r in caplog.records)


async def test_guard_refuses_hidden_tool_called_directly(bound):
    """Defence in depth: even a direct call_tool on a hidden tool is refused."""
    from pydantic_ai import ModelRetry

    calls: list = []
    guard = ReadOnlyGuard(_arc1_toolset(calls))
    with pytest.raises(ModelRetry, match="Refused in the read-only IDE"):
        await guard.call_tool("SAPWrite", {"name": "ZCL_X"}, None, None)  # type: ignore[arg-type]
    assert calls == []


async def test_guard_allows_good_call_when_bound(bound):
    calls: list = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("SAPRead", {"type": "CLAS", "name": "ZCL_X"})])
        return ModelResponse(parts=[TextPart(str(returns[-1].content))])

    guard = ReadOnlyGuard(_arc1_toolset(calls))
    result = await Agent().run("hi", model=FunctionModel(fn), toolsets=[guard])
    assert result.output == "source of ZCL_X"
    assert calls == [("SAPRead", "CLAS", "ZCL_X")]


async def test_guard_is_pass_through_when_unbound():
    calls: list = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("SAPWrite", {"name": "ZCL_X"})])
        return ModelResponse(parts=[TextPart(str(returns[-1].content))])

    assert current_workspace.get() is None
    guard = ReadOnlyGuard(_arc1_toolset(calls))
    result = await Agent().run("hi", model=FunctionModel(fn), toolsets=[guard])
    assert result.output == "written"
    assert calls == [("SAPWrite", "ZCL_X")]


def _is_subagent(info: AgentInfo) -> bool:
    return "task" not in _tool_names(info)


async def test_subagent_inherits_guard(bound):
    seen: dict[str, set[str]] = {}

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not _is_subagent(info):
            seen["parent"] = _tool_names(info)
            if not returns:
                return ModelResponse(parts=[ToolCallPart("task", {"description": "read"})])
            return ModelResponse(parts=[TextPart("done")])
        seen["child"] = _tool_names(info)
        return ModelResponse(parts=[TextPart("child done")])

    model = FunctionModel(fn)
    servers = [ReadOnlyGuard(_arc1_toolset([]))]
    cfg = DeepConfig(enabled=True, subagent_max_depth=1, subagent_instructions="")
    ts = deep_toolset(cfg, parent_toolsets=servers, model=model, agent_name="p")
    result = await Agent().run("go", model=model, toolsets=[*servers, ts])
    assert result.output == "done"
    assert "SAPRead" in seen["parent"] and "SAPWrite" not in seen["parent"]
    assert "SAPRead" in seen["child"] and "SAPWrite" not in seen["child"]


async def test_for_run_still_gives_per_run_copy():
    import agents.shared as shared

    server = shared.create_mcp_server("arc1", "https://arc1.example.com/mcp", "none")
    assert isinstance(server, shared.PerRunMCPServer)
    guard = ReadOnlyGuard(server)
    run1 = await guard.for_run(None)  # type: ignore[arg-type]
    run2 = await guard.for_run(None)  # type: ignore[arg-type]
    assert isinstance(run1, ReadOnlyGuard) and isinstance(run2, ReadOnlyGuard)
    assert run1 is not run2
    assert run1.wrapped is not server and run2.wrapped is not server
    assert run1.wrapped is not run2.wrapped


async def test_registry_wraps_every_server():
    """``build_orchestrator`` wraps MCP servers and built-ins alike, and keeps
    the raw servers for transport cleanup."""
    from agents import registry as registry_module
    from agents.db import SessionLocal, init_db, upsert_agent

    await init_db()
    async with SessionLocal() as s:
        await upsert_agent(
            s, name="ro-agent", description="d", instructions="i",
            mcp_servers=[
                {"url": "https://arc1.example.com/mcp", "auth_mode": "none"},
                {"url": "builtin:sapnotes", "auth_mode": "none"},
            ],
        )
        await s.commit()

    real_get_model = registry_module.get_model
    registry_module.get_model = lambda *a, **k: TestModel()
    try:
        build = await registry_module.build_orchestrator()
    finally:
        registry_module.get_model = real_get_model

    agent = build.specialists["ro-agent"]
    user_toolsets = list(getattr(agent, "_user_toolsets", ()) or ())
    assert len(user_toolsets) == 2
    assert all(isinstance(ts, ReadOnlyGuard) for ts in user_toolsets)
    assert not any(isinstance(c, ReadOnlyGuard) for c in build.mcp_clients)


# --- guard per session type -------------------------------------------------------
class _DiagnoseStub:
    """Minimal wrapped toolset: one ``SAPDiagnose`` tool, records calls."""

    def __init__(self, calls: list):
        self.calls = calls

    async def get_tools(self, ctx):
        return {"SAPDiagnose": object()}

    async def call_tool(self, name, tool_args, ctx, tool):
        self.calls.append((tool_args.get("action"), tool_args.get("user", "")))
        return f"ran {tool_args.get('action')}"


def _diagnose_toolset(calls: list):
    return _DiagnoseStub(calls)


@pytest.fixture
def target_server(monkeypatch):
    """The stub toolsets below stand for the session target's ARC-1 server
    (a stub has no destination to recognise it by)."""
    from agents.ide import diagnose

    monkeypatch.setattr(diagnose, "is_target_server", lambda toolset, run: True)


@contextmanager
def _scope(sid: str, session_type: str):
    """Bind an IDE scope as the runner does: a session that is not a change
    session also gets its diagnose run (raw here: these tests are about the
    policy; masking is covered in ``test_ide_masking_wiring.py``)."""
    from agents.ide.diagnose import DiagnoseRun, current_diagnose

    scope = WorkspaceScope(
        session_id=sid, state=DeepState(run_id=sid), session_type=session_type
    )
    run = None if session_type == "change" else DiagnoseRun(
        session_id=sid, owner="alice", target="T1", run_id="r", masking=False
    )
    token = current_workspace.set(scope)
    run_token = current_diagnose.set(run)
    try:
        yield scope
    finally:
        current_diagnose.reset(run_token)
        current_workspace.reset(token)


@pytest.mark.parametrize("session_type,ok", [
    ("diagnose", True), ("change", False), ("nonsense", False),
])
async def test_guard_uses_scope_session_type(session_type, ok, target_server):
    from pydantic_ai import ModelRetry

    calls: list = []
    guard = ReadOnlyGuard(_diagnose_toolset(calls))
    with _scope("s-2", session_type):
        tools = await guard.get_tools(None)  # type: ignore[arg-type]
        assert ("SAPDiagnose" in tools) is (session_type != "nonsense")
        tool = None
        if ok:
            out = await guard.call_tool("SAPDiagnose", {"action": "dumps"}, None, tool)  # type: ignore[arg-type]
            assert out == "ran dumps"
            assert calls == [("dumps", "")]
        else:
            with pytest.raises(ModelRetry, match="Refused in the read-only IDE"):
                await guard.call_tool("SAPDiagnose", {"action": "dumps"}, None, tool)  # type: ignore[arg-type]
            assert calls == []


def test_scope_session_type_defaults_to_change():
    assert WorkspaceScope(session_id="s", state=DeepState(run_id="s")).session_type == "change"


@pytest.mark.parametrize("action", ["trace_start", "trace_cancel"])
async def test_guard_never_forwards_approval_actions(action, target_server):
    """Diagnose: held as a proposal (stored or not, never forwarded; the
    flow is pinned in ``tests/test_ide_approval_flow.py``). Change: refused."""
    from pydantic_ai import ModelRetry

    from agents.ide.readonly import PROPOSAL_REFUSED_PREFIX, PROPOSAL_STORED_PREFIX

    calls: list = []
    guard = ReadOnlyGuard(_diagnose_toolset(calls))
    tool = None
    with _scope("s-3", "diagnose"):
        # ``s-3`` is no stored session, so nothing can be proposed for it.
        try:
            out = await guard.call_tool("SAPDiagnose", {"action": action}, None, tool)  # type: ignore[arg-type]
        except ModelRetry as exc:
            out = str(exc)
        assert out.startswith(PROPOSAL_REFUSED_PREFIX)
        assert not out.startswith(PROPOSAL_STORED_PREFIX)
    with _scope("s-3", "change"):
        with pytest.raises(ModelRetry, match="not allowed") as exc:
            await guard.call_tool("SAPDiagnose", {"action": action}, None, tool)  # type: ignore[arg-type]
        assert str(exc.value).startswith("Refused in the read-only IDE")
    assert calls == []


async def test_guard_refuses_user_filter_in_diagnose(target_server):
    from pydantic_ai import ModelRetry

    calls: list = []
    guard = ReadOnlyGuard(_diagnose_toolset(calls))
    tool = None
    with _scope("s-4", "diagnose"):
        with pytest.raises(ModelRetry, match="user="):
            await guard.call_tool(
                "SAPDiagnose", {"action": "dumps", "user": "DEVUSER01"}, None, tool
            )  # type: ignore[arg-type]
    assert calls == []


# --- the policy follows the scope into delegates and sub-agents -------------------
def _diagnose_function_toolset(calls: list):
    ts = FunctionToolset()

    @ts.tool_plain
    def SAPDiagnose(action: str) -> str:  # noqa: N802
        """Diagnose."""
        calls.append(action)
        return f"ran {action}"

    return ts


def _child_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Calls SAPDiagnose(dumps) once, then answers with what came back."""
    back = [
        p for m in messages for p in m.parts
        if isinstance(p, (ToolReturnPart, RetryPromptPart))
    ]
    if not back:
        return ModelResponse(parts=[ToolCallPart("SAPDiagnose", {"action": "dumps"})])
    return ModelResponse(parts=[TextPart(str(back[-1].content))])


async def _child_stream(messages: list[ModelMessage], info: AgentInfo):
    """``_child_fn`` as a stream: the delegation tool runs with an event handler."""
    from pydantic_ai.models.function import DeltaToolCall

    part = _child_fn(messages, info).parts[0]
    if isinstance(part, ToolCallPart):
        yield {0: DeltaToolCall(name=part.tool_name, json_args=part.args_as_json_str())}
    else:
        yield part.content


@pytest.mark.parametrize("session_type,ok", [("diagnose", True), ("change", False)])
async def test_delegate_sees_the_scope_policy(session_type, ok, target_server):
    """A specialist reached through the registry's delegation tool runs under
    the session type of the scope bound around the top-level run."""
    from types import SimpleNamespace

    from agents.registry import _attach_delegation_tool, _sanitize_tool_name

    calls: list = []
    specialist = Agent(
        FunctionModel(_child_fn, stream_function=_child_stream),
        toolsets=[ReadOnlyGuard(_diagnose_function_toolset(calls))],
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

    with _scope("s-5", session_type):
        result = await parent.run("hi")
    if ok:
        assert calls == ["dumps"]
        assert "ran dumps" in result.output
    else:
        assert calls == []
        assert "Refused in the read-only IDE" in result.output


@pytest.mark.parametrize("session_type,ok", [("diagnose", True), ("change", False)])
async def test_deep_subagent_sees_the_scope_policy(session_type, ok, target_server):
    calls: list = []
    seen: dict[str, str] = {}

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if _is_subagent(info):
            response = _child_fn(messages, info)
            if isinstance(response.parts[0], TextPart):
                seen["child"] = response.parts[0].content
            return response
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("task", {"description": "dumps"})])
        return ModelResponse(parts=[TextPart("done")])

    model = FunctionModel(fn)
    servers = [ReadOnlyGuard(_diagnose_function_toolset(calls))]
    cfg = DeepConfig(enabled=True, subagent_max_depth=1, subagent_instructions="")
    ts = deep_toolset(cfg, parent_toolsets=servers, model=model, agent_name="p")
    with _scope("s-6", session_type):
        result = await Agent().run("go", model=model, toolsets=[*servers, ts])
    assert result.output == "done"
    if ok:
        assert calls == ["dumps"]
        assert seen["child"] == "ran dumps"
    else:
        assert calls == []
        assert "Refused in the read-only IDE" in seen["child"]
