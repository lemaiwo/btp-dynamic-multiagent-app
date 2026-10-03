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
from agents.ide.readonly import READONLY_POLICY, ReadOnlyGuard, check_call  # noqa: E402

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
