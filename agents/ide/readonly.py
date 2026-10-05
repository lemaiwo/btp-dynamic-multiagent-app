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

The policy is chosen by the bound scope's ``session_type`` (:data:`POLICIES`):
``change`` sessions keep :data:`READONLY_POLICY`; ``diagnose`` sessions use
:data:`DIAGNOSE_POLICY`, which adds the ``SAPDiagnose`` data actions (dumps,
traces, ...) with the ``user``/``traceUser`` filters refused (kept from
phase 1c: a run looks at the developer's own reproduction, not at other
users' activity; opening them up is a separate decision).
``trace_start``/``trace_cancel`` pass the policy in ``diagnose`` only to be
recognised as approval actions: the guard never forwards them to ARC-1
(:func:`needs_approval`). It turns such a call into a *proposal*
(``agents.ide.approvals.request``: a pending approval plus an
``approval_required`` event) and answers the model with
:data:`PROPOSAL_STORED_PREFIX` text -- stored, nothing armed, waiting for the
developer. The run goes on and ends normally (spike variant B); only the
approval route ever sends the action to ARC-1. This is the whole proposal
mechanism: there is no separate tool, so nothing can be proposed outside a
diagnose run on the session target's server -- by the top-level agent, a
peer it delegates to or a deep sub-agent alike. A proposal that cannot be
stored is answered with :data:`PROPOSAL_REFUSED_PREFIX` and a stable code.
An unknown session type refuses everything.

A session that is not a ``change`` session also needs its run bound on
``agents.ide.diagnose.current_diagnose``; without it every call is refused
and no tool is offered. The run says which wrapped server is the session
target's ARC-1 server. Only that one gets the diagnose policy; any other
server of the agent stays under the ``change`` policy, whatever its tools
are called. A diagnose run exists only for a ``non_production`` target
(``agents.ide.runner`` refuses to start one otherwise), so results pass as
ARC-1 sent them.

What a ``SAPDiagnose`` data action of the target's server returned is then
handed to ``agents.ide.findings.collect``, which notes the findings (with
detail text) on the run.

A ``ModelRetry`` raised by the wrapped call (an MCP ``isError`` result) is
an ARC-1 error object, not data, and propagates unchanged in both cases.

Unbound (chat, A2A, scheduled jobs, workflows) both methods pass straight
through, so nothing outside the IDE changes. ``WrapperToolset`` forwards
``for_run``/``__aenter__``/``__aexit__`` to the wrapped toolset, so a
wrapped ``PerRunMCPServer`` still opens its own MCP session per run
(``tests/test_ide_readonly.py::test_for_run_still_gives_per_run_copy``).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.toolsets import WrapperToolset
from pydantic_ai.toolsets.abstract import ToolsetTool

from agents.auth import current_principal
from agents.db import SessionLocal
from agents.deep import WorkspaceScope, current_workspace
from agents.ide import diagnose, findings

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

# The phase 1a SAPDiagnose actions: checks on source, no runtime data. Shared
# by both policies.
READONLY_POLICY_SAPDIAGNOSE_ACTIONS = _set(
    "syntax", "unittest", "atc", "atc_variants", "cds_testcases",
    "object_state", "quickfix", "apply_quickfix", "cds_sql", "system_messages",
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
        ArgRule("action", allow=READONLY_POLICY_SAPDIAGNOSE_ACTIONS, required=True),
    ),
    "SAPTransport": (
        ArgRule(
            "action",
            allow=_set("list", "get", "diff", "check", "history", "layers", "targets"),
            required=True,
        ),
    ),
}

# Diagnose sessions (plan 1c, table 1.4). Data actions return runtime data
# of the target system. ``set_sql_trace_state`` is in no
# set: refused in every session type.
DIAGNOSE_DATA_ACTIONS = _set(
    "dumps", "traces", "gateway_errors", "odata_perf", "authorization_trace",
    "sql_trace_state", "sql_trace_directory", "trace_requests",
)
# Allowed by the diagnose policy only so they can be recognised; executed by
# the approval route alone, never by the guard (decision D1).
APPROVAL_ACTIONS = _set("trace_start", "trace_cancel")

# ``ArgRule(arg, allow=_NONE)``: any present value is refused.
_NONE: frozenset[str] = frozenset()

DIAGNOSE_POLICY: dict[str, tuple[ArgRule, ...]] = {
    **READONLY_POLICY,
    "SAPDiagnose": (
        ArgRule(
            "action",
            allow=READONLY_POLICY_SAPDIAGNOSE_ACTIONS | DIAGNOSE_DATA_ACTIONS | APPROVAL_ACTIONS,
            required=True,
        ),
        ArgRule("user", allow=_NONE),
        ArgRule("traceUser", allow=_NONE),
    ),
}

# Session types (``WorkspaceScope.session_type``).
CHANGE = "change"
DIAGNOSE = "diagnose"

POLICIES: dict[str, dict[str, tuple[ArgRule, ...]]] = {
    CHANGE: READONLY_POLICY,
    DIAGNOSE: DIAGNOSE_POLICY,
}

_MAX_VALUE_CHARS = 200


def _short(value: Any) -> str:
    """``repr`` of a model-controlled value, cut to 200 characters."""
    text = repr(value)
    if len(text) > _MAX_VALUE_CHARS:
        text = text[:_MAX_VALUE_CHARS] + "..."
    return text


def _policy(policy: Any) -> dict[str, tuple[ArgRule, ...]] | None:
    """The rules of session type ``policy``; ``None`` for anything unknown."""
    return POLICIES.get(policy) if isinstance(policy, str) else None


def policy_name(tool_name: str, policy: str = CHANGE) -> str | None:
    """The entry of ``policy`` that ``tool_name`` maps to, or ``None``.

    Exact name, or a registry/MCP prefix plus ``_`` (``arc1_SAPRead``).
    An unknown policy maps nothing.
    """
    rules = _policy(policy)
    if rules is None or not isinstance(tool_name, str):
        return None
    for name in rules:
        if tool_name == name or tool_name.endswith("_" + name):
            return name
    return None


def _action(args: Any) -> str | None:
    """The normalised ``action`` argument, or ``None`` if absent or not a string."""
    raw = args.get("action") if isinstance(args, dict) else None
    return raw.strip().lower() if isinstance(raw, str) else None


def _odata_perf_url_refusal(args: dict[str, Any]) -> str | None:
    """``odata_perf`` takes a path on the ABAP system, never a URL elsewhere.

    Besides ``://`` a leading ``//`` (scheme-relative) and any backslash
    (``/\\host`` is read as ``//host`` by URL parsers) are refused too, and
    so is any space or control character: parsers strip tabs and newlines,
    which turns ``/<tab>/host`` into ``//host``.
    """
    url = args.get("url")
    if (
        not isinstance(url, str)
        or not url.startswith("/")
        or url.startswith("//")
        or "://" in url
        or "\\" in url
        or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url)
    ):
        return (
            f"SAPDiagnose odata_perf with url={_short(url)} is not allowed "
            "(a path starting with '/' on the ABAP system only)"
        )
    return None


GATEWAY_ERRORLOG_PREFIX = "/sap/bc/adt/gw/errorlog/"


def _detail_url_refusal(args: dict[str, Any]) -> str | None:
    """``detailUrl`` is the link of one Gateway error: a path on the ADT
    error log of the session's system, never a URL the model (or a tool
    result it read) made up. Only ``gateway_errors`` takes one.

    Same reading as for ``odata_perf``: no scheme, no ``//``, no backslash,
    no space or control character -- and no ``..``, which would leave the
    error log again.
    """
    if "detailUrl" not in args or args["detailUrl"] is None:
        return None
    url = args["detailUrl"]
    if (
        _action(args) != "gateway_errors"
        or not isinstance(url, str)
        or not url.startswith(GATEWAY_ERRORLOG_PREFIX)
        or len(url) == len(GATEWAY_ERRORLOG_PREFIX)
        or "://" in url
        or "//" in url
        or ".." in url
        or "\\" in url
        # No percent-encoding (``%2e%2e`` is a traversal once decoded), no
        # query and no fragment: the path names the error, nothing else.
        or any(ch in url for ch in "%?#")
        or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url)
    ):
        return (
            f"SAPDiagnose with detailUrl={_short(url)} is not allowed (only "
            f"gateway_errors, with a path under {GATEWAY_ERRORLOG_PREFIX})"
        )
    return None


def check_call(
    tool_name: str, args: dict[str, Any] | None, policy: str = CHANGE
) -> str | None:
    """Why this call is refused in the read-only IDE, or ``None`` if allowed.

    ``policy`` is the bound session type; an unknown one refuses every call.
    ``None`` for an approval action means "passes the policy", not "run it":
    the caller must also ask :func:`needs_approval`.
    """
    rules = _policy(policy)
    if rules is None:
        return f"session type {_short(policy)} has no read-only policy"
    name = policy_name(tool_name, policy)
    if name is None:
        return f"tool {_short(tool_name)} is not available (read-only access only)"
    args = args if isinstance(args, dict) else {}
    # Constrained keys are matched exactly; a case variant (``User``,
    # ``TRACEUSER``, ``Type``) is refused outright, so it can neither dodge a
    # rule nor ride along next to the checked spelling if the server reads
    # argument names loosely.
    constrained = {rule.arg.lower(): rule.arg for rule in rules[name]}
    if name == "SAPDiagnose":
        constrained["url"] = "url"
        constrained["detailurl"] = "detailUrl"
    for key in args:
        if isinstance(key, str) and key not in constrained.values():
            if key.lower() in constrained:
                return (
                    f"{name} with argument {_short(key)} is not allowed "
                    f"(use {constrained[key.lower()]!r}; read-only access only)"
                )
    for rule in rules[name]:
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
    if name == "SAPDiagnose":
        reason = _detail_url_refusal(args)
        if reason is not None:
            return reason
        if _action(args) == "odata_perf":
            return _odata_perf_url_refusal(args)
    return None


def needs_approval(tool_name: str, args: dict[str, Any] | None, policy: str) -> bool:
    """True for ``trace_start``/``trace_cancel`` in a diagnose session.

    Such a call must never be forwarded by the guard; only the approval
    route executes it, with arguments built server-side (decision D1).
    """
    return (
        policy == DIAGNOSE
        and policy_name(tool_name, policy) == "SAPDiagnose"
        and _action(args) in APPROVAL_ACTIONS
    )


def is_diagnose_data(tool_name: str, args: dict[str, Any] | None) -> bool:
    """True for a ``SAPDiagnose`` data action, whose result yields findings."""
    return (
        policy_name(tool_name, DIAGNOSE) == "SAPDiagnose"
        and _action(args) in DIAGNOSE_DATA_ACTIONS
    )


NO_RUN_REASON = "no diagnose run is bound to this session"

# How the guard answers a ``trace_start``/``trace_cancel`` call. The seeded
# diagnose agent is told exactly this: the call is stored as a proposal.
PROPOSAL_STORED_PREFIX = "Proposal stored"
# ``"<prefix> (<code>): <why>"``; ``agents.ide.runner`` puts the code on the
# tool event, so the client can tell ``too_many_pending`` from a bad argument.
PROPOSAL_REFUSED_PREFIX = "Trace proposal not stored"
_PROPOSAL_CODE_RE = re.compile(
    re.escape(PROPOSAL_REFUSED_PREFIX) + r" \(([a-z0-9_]{1,64})\)"
)


def proposal_refusal_code(text: Any) -> str | None:
    """The code of a refused proposal in a tool output, else ``None``."""
    # At the start only: a tool result that merely quotes the phrase (a
    # dump, a source line) is not a refusal.
    match = _PROPOSAL_CODE_RE.match(text) if isinstance(text, str) else None
    return match.group(1) if match else None


def _proposal_refused(code: str, message: str) -> str:
    return f"{PROPOSAL_REFUSED_PREFIX} ({code}): {message}"


async def propose(
    run: diagnose.DiagnoseRun,
    tool_args: dict[str, Any],
    tool_call_id: str | None,
    retry: int = 0,
) -> str:
    """Store the agent's ``trace_start``/``trace_cancel`` call as a proposal
    and return what the model is told. Nothing reaches ARC-1.

    Arguments the model can fix (a wrong enum, a bad id) are a
    ``ModelRetry`` -- once: ``retry`` is the tool's retry count
    (``RunContext.retry``), and a model that repeats the bad value gets the
    same reason as text, because exhausting the tool's retries fails the
    whole run. Everything else that keeps the proposal from being stored is
    returned as text with its code from the start: calling again would not
    help.
    """
    # Imported here: ``approvals`` imports this module.
    from agents.ide import approvals

    action = _action(tool_args) or ""
    args = {k: v for k, v in tool_args.items() if k != "action"}
    try:
        async with SessionLocal() as db:
            row = await approvals.request(db, run, action, args, tool_call_id)
            aid, params = row.id, approvals.approval_json(row)["params"]
    except approvals.ApprovalError as exc:
        logger.info(
            "[ide] %s proposal in session %s not stored: %s",
            action, run.session_id, exc.code,
        )
        text = _proposal_refused(exc.code, exc.message)
        if exc.code == "invalid_request":
            if not retry:
                raise ModelRetry(text) from exc
            return (
                f"{text}. Nothing was armed or cancelled. Call {action} again "
                "only with corrected arguments; otherwise tell the developer."
            )
        return (
            f"{text}. Nothing was armed or cancelled. Do not call {action} "
            "again in this run; tell the developer."
        )
    except Exception:  # noqa: BLE001 -- the run goes on; details stay in the log
        logger.error(
            "[ide] %s proposal in session %s could not be stored",
            action, run.session_id, exc_info=True,
        )
        return (
            _proposal_refused("proposal_failed", "The proposal could not be stored")
            + f". Nothing was armed or cancelled. Do not call {action} again "
            "in this run; tell the developer."
        )
    return (
        f"{PROPOSAL_STORED_PREFIX} (approval id {aid}): {action} "
        f"{json.dumps(params, sort_keys=True)}. Nothing was armed or cancelled: "
        "the proposal is waiting for the developer's decision in the IDE. "
        "Tell the developer what is waiting for approval and what to "
        f"reproduce once it is approved, then end your answer. Do not call "
        f"{action} again and do not wait for the decision."
    )


@dataclass
class ReadOnlyGuard(WrapperToolset[Any]):
    """Enforces the bound session type's policy while an IDE session is bound."""

    def _bound(self, scope: WorkspaceScope) -> tuple[Any, diagnose.DiagnoseRun | None]:
        """``(policy for this server, the session's diagnose run)``.

        A change session has no run. In a diagnose session a server that is
        not the target's ARC-1 server gets the change policy. An unknown
        session type is passed on as it is, so ``check_call`` refuses it.
        """
        policy = scope.session_type
        if policy == CHANGE:
            return policy, None
        run = diagnose.run_for(scope.session_id)
        if (
            policy == DIAGNOSE
            and run is not None
            # Looked up on the module at call time: the tests' seam.
            and not diagnose.is_target_server(self.wrapped, run)
        ):
            policy = CHANGE
        return policy, run

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        tools = await self.wrapped.get_tools(ctx)
        scope = current_workspace.get()
        if scope is None:
            return tools
        policy, run = self._bound(scope)
        if scope.session_type != CHANGE and run is None:
            return {}
        return {
            name: t for name, t in tools.items()
            if policy_name(name, policy) is not None
        }

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[Any],
        tool: ToolsetTool[Any],
    ) -> Any:
        scope = current_workspace.get()
        run = None
        if scope is not None:
            policy, run = self._bound(scope)
            reason = check_call(name, tool_args, policy)
            if reason is None and scope.session_type != CHANGE and run is None:
                # No run bound for this session: nobody checked its target.
                reason = NO_RUN_REASON
            if reason is not None:
                logger.warning(
                    "[ide] read-only guard refused %s for %s in session %s: %s",
                    _short(name), current_principal.get() or "?",
                    scope.session_id, reason,
                )
                raise ModelRetry(f"{REFUSED_PREFIX}: {reason}")
            if needs_approval(name, tool_args, policy):
                # Never forwarded to ARC-1 (decision D1): stored as a
                # proposal instead. ``policy`` is DIAGNOSE here, so ``run``
                # is this session's run and this is the target's server.
                logger.info(
                    "[ide] read-only guard held %s for approval in session %s",
                    _action(tool_args), scope.session_id,
                )
                if run is None:  # unreachable; never fall through to ARC-1
                    raise ModelRetry(f"{REFUSED_PREFIX}: {NO_RUN_REASON}")
                retry = getattr(ctx, "retry", 0)
                return await propose(
                    run, tool_args, getattr(ctx, "tool_call_id", None),
                    retry=retry if isinstance(retry, int) else 1,
                )
        # A ModelRetry from the wrapped call (an ARC-1 error object) is not
        # caught: it is not data.
        result = await self.wrapped.call_tool(name, tool_args, ctx, tool)
        if run is None:
            return result
        if policy == DIAGNOSE and is_diagnose_data(name, tool_args):
            # Never raises; the result reaches the model unchanged.
            findings.collect(run, name, tool_args, result)
        return result
