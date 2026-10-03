"""The ARC-1 read-only allowlist, enforced in code for IDE sessions.

The IDE proposes changes in a workspace; it never writes to the ABAP system.
Prompting the agent to stay read-only is not enough, so ``build_orchestrator``
wraps every MCP server and built-in of every specialist in
:class:`ReadOnlyGuard`. While an IDE session is bound
(``agents.deep.current_workspace``) the guard:

- hides every tool whose unprefixed name is not in :data:`READONLY_POLICY`
  (default deny: ``SAPWrite``, ``SAPActivate``, ``SAPManage``, ``SAPGit``,
  ``SAPQuery``, any built-in and any tool nobody has reviewed yet);
- checks the arguments of each call to an allowed tool against allowlists
  (object types, actions, search sources), so data preview, SQL, traces,
  dumps, formatter settings, transport mutations, any value it does not know
  and any non-string value are refused.

Unbound (chat, A2A, scheduled jobs, workflows) both methods pass straight
through, so nothing outside the IDE changes. ``WrapperToolset`` forwards
``for_run``/``__aenter__``/``__aexit__`` to the wrapped toolset, so a
wrapped ``PerRunMCPServer`` still opens its own MCP session per run
(``tests/test_ide_readonly.py::test_for_run_still_gives_per_run_copy``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.toolsets import WrapperToolset
from pydantic_ai.toolsets.abstract import ToolsetTool

from agents.auth import current_principal
from agents.deep import current_workspace

logger = logging.getLogger(__name__)

# Every refusal text starts with this; ``agents.ide.runner`` tags tool events
# that carry it with the code ``readonly_refused``.
REFUSED_PREFIX = "Refused in the read-only IDE"


@dataclass(frozen=True)
class ArgRule:
    """One argument constraint of a tool: an allowlist of string values.

    A present value must be a string in ``allow`` (case-insensitive); any
    other value -- a list, a dict, a number, an unknown string -- is refused.
    ``required``: a missing argument is refused too. Leave it ``False`` only
    when the server's own default for the argument is in ``allow``.
    """

    arg: str
    allow: frozenset[str]
    required: bool = False


def _set(*values: str) -> frozenset[str]:
    return frozenset(v.lower() for v in values)


# Every SAPRead type in the ARC-1 schema except data preview (TABLE_CONTENTS,
# TABLE_QUERY). A type ARC-1 adds later stays refused until reviewed here.
SAPREAD_TYPES = _set(
    "PROG", "CLAS", "INTF", "FUNC", "FUGR", "INCL", "DDLS", "DCLS", "DDLX",
    "BDEF", "SRVD", "SRVB", "SKTD", "KTD", "TABL", "VIEW", "DOMA", "DTEL",
    "TRAN", "TTYP", "DEVC", "SOBJ", "SYSTEM", "COMPONENTS", "MSAG",
    "MESSAGES", "TEXT_ELEMENTS", "VARIANTS", "BSP", "BSP_DEPLOY", "API_STATE",
    "INACTIVE_OBJECTS", "AUTH", "FEATURE_TOGGLE", "FTG2", "ENHO", "VERSIONS",
    "VERSION_SOURCE", "DESD", "DTSC", "CSNM", "EVTB", "EVTO", "COTA", "DSFD",
    "DTDC", "UIAD",
)

# Table 1.5 of the IDE plan, as allowlists, checked against the live ARC-1
# tool schemas. Server defaults relied on for optional arguments:
# SAPSearch searchType=object, source=adt; SAPContext action=deps.
READONLY_POLICY: dict[str, tuple[ArgRule, ...]] = {
    "SAPRead": (
        ArgRule("type", allow=SAPREAD_TYPES, required=True),
        ArgRule("action", allow=_set("diff")),
    ),
    "SAPSearch": (
        ArgRule("searchType", allow=_set("object", "source_code", "tadir_lookup")),
        ArgRule("source", allow=_set("adt")),
    ),
    "SAPContext": (
        ArgRule("action", allow=_set("impact", "deps", "usages", "structure")),
    ),
    "SAPNavigate": (
        ArgRule(
            "action",
            allow=_set("definition", "references", "completion", "hierarchy"),
            required=True,
        ),
    ),
    "SAPLint": (
        ArgRule("action", allow=_set("lint", "lint_and_fix", "list_rules"), required=True),
    ),
    "SAPDiagnose": (
        ArgRule(
            "action",
            allow=_set(
                "syntax", "unittest", "atc", "atc_variants", "cds_testcases",
                "object_state", "quickfix", "apply_quickfix", "cds_sql",
                "system_messages",
            ),
            required=True,
        ),
    ),
    "SAPTransport": (
        ArgRule(
            "action",
            allow=_set("list", "get", "diff", "check", "history", "layers", "targets"),
            required=True,
        ),
    ),
}

_MAX_VALUE_CHARS = 200


def _short(value: Any) -> str:
    """``repr`` of a model-controlled value, cut to 200 characters."""
    text = repr(value)
    if len(text) > _MAX_VALUE_CHARS:
        text = text[:_MAX_VALUE_CHARS] + "..."
    return text


def policy_name(tool_name: str) -> str | None:
    """The policy entry ``tool_name`` maps to, or ``None``.

    Exact name, or a registry/MCP prefix plus ``_`` (``arc1_SAPRead``).
    """
    for name in READONLY_POLICY:
        if tool_name == name or tool_name.endswith("_" + name):
            return name
    return None


def check_call(tool_name: str, args: dict[str, Any] | None) -> str | None:
    """Why this call is refused in the read-only IDE, or ``None`` if allowed."""
    name = policy_name(tool_name)
    if name is None:
        return f"tool {_short(tool_name)} is not available (read-only access only)"
    args = args if isinstance(args, dict) else {}
    for rule in READONLY_POLICY[name]:
        if rule.arg not in args or args[rule.arg] is None:
            if rule.required:
                return f"{name} needs an explicit {rule.arg!r}"
            continue
        raw = args[rule.arg]
        if not isinstance(raw, str) or raw.strip().lower() not in rule.allow:
            return (
                f"{name} with {rule.arg}={_short(raw)} is not allowed "
                "(read-only access only)"
            )
    return None


@dataclass
class ReadOnlyGuard(WrapperToolset[Any]):
    """Enforces :data:`READONLY_POLICY` while an IDE session is bound."""

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        tools = await self.wrapped.get_tools(ctx)
        if current_workspace.get() is None:
            return tools
        return {name: t for name, t in tools.items() if policy_name(name) is not None}

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[Any],
        tool: ToolsetTool[Any],
    ) -> Any:
        scope = current_workspace.get()
        if scope is not None:
            reason = check_call(name, tool_args)
            if reason is not None:
                logger.warning(
                    "[ide] read-only guard refused %s for %s in session %s: %s",
                    _short(name), current_principal.get() or "?",
                    scope.session_id, reason,
                )
                raise ModelRetry(f"{REFUSED_PREFIX}: {reason}")
        return await self.wrapped.call_tool(name, tool_args, ctx, tool)
