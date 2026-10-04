"""Real-stream e2e backend: the real app with a scripted model and a fake ARC-1.

TEST-ONLY. Nothing under ``agents/`` or ``app.py`` imports this module, and
the MTA build leaves ``tests/`` out of the archive
(``tests/test_ide_stream_server.py`` checks both).

Why it exists: the UI's OPA journeys run against ``FakeBackend``, which
mirrors the API by hand. This server runs the *real* backend -- runner, SSE
framing, stages, comments, pins, the run-end SAP base check and syntax dry
run, findings and trace approvals -- so the Playwright spec
``ui5-ide/e2e/real-stream.spec.ts`` (task E2) drives the UI against what the
product really emits, with no mocked routes. Only two things are replaced:

- **the model**: the IDE agents (``IDE_ORCHESTRATOR_AGENT``,
  ``IDE_DIAGNOSE_AGENT``) become one pydantic-ai ``Agent`` on a
  ``FunctionModel`` with a ``stream_function``. Its script
  (:func:`turns_for`) is deterministic and stage-aware: it reads the run's
  stage from ``agents.ide.session_tools.current_ide_run`` and the review
  comment ids from the prompt;
- **SAP**: ``arc1.get_arc1_client`` returns a client over :class:`FakeSap`,
  an in-memory system with one existing class, and the agent's ARC-1 server
  is a ``SAPDiagnose`` function toolset behind the real ``ReadOnlyGuard``.
  ``diagnose.is_target_server`` answers True, so that toolset counts as the
  session target's server.

Local open access as in local development (no XSUAA binding). Requests
without a bearer token get an unsigned placeholder token for the constant
``local-dev`` principal (:class:`DevTokenMiddleware`), because arming an
approved trace requires a user JWT to be bound, exactly as on Cloud Foundry.

Start (from the repo root or from ``ui5-ide/``)::

    .venv/bin/python tests/e2e/ide_stream_server.py
    # -> http://127.0.0.1:7933/ide/api/... ; health: /healthz

Every start deletes and recreates ``_e2e_stream.db`` in the repo root; a
clean shutdown (SIGINT/SIGTERM) deletes it again. The process ignores the
developer's ``.env`` and any ``DATABASE_URL`` in the environment.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, MutableMapping

ROOT = Path(__file__).resolve().parents[2]
HOST = "127.0.0.1"
PORT = 7933
DB_FILE = ROOT / "_e2e_stream.db"
TARGET = "DEMO"
# Never contacted: get_arc1_client is replaced. Set so that code paths that
# read the target's URL find one, as in local development.
ARC1_URL = "http://127.0.0.1:9/mcp"
PRINCIPAL = "local-dev"


def configure_env(environ: MutableMapping[str, str] = os.environ) -> None:
    """Point the app at a throw-away SQLite file and unreachable placeholders.

    Overrides rather than defaults: a developer's shell ``DATABASE_URL``
    (Postgres, say) must never receive e2e writes.
    """
    environ.update({
        "DATABASE_URL": f"sqlite+aiosqlite:///{DB_FILE}",
        "MCP_URL_ALLOWLIST": "",
        "PUBLIC_BASE_URL": f"http://{HOST}:{PORT}",
        # Start-up builds the registry, which needs AI Core settings to
        # construct a client; nothing here calls it (as in playwright.config.ts).
        "AICORE_CLIENT_ID": "e2e",
        "AICORE_CLIENT_SECRET": "e2e",
        "AICORE_AUTH_URL": "http://127.0.0.1:9",
        "AICORE_BASE_URL": "http://127.0.0.1:9/v2",
        "AICORE_RESOURCE_GROUP": "default",
        f"IDE_ARC1_URL_{TARGET}": ARC1_URL,
        # The scripted agents replace the IDE agents; no seeded rows needed.
        "IDE_SEED": "false",
        "IDE_ORCHESTRATOR_AGENT": "abap-orchestrator",
        "IDE_DIAGNOSE_AGENT": "abap-diagnostics",
    })
    for name in ("VCAP_SERVICES", "VCAP_APPLICATION", "AUTH_REQUIRED",
                 "IDE_SESSION_REQUEST_CAP"):
        environ.pop(name, None)


if __name__ == "__main__":
    # Before anything imports ``agents.db``, which reads DATABASE_URL at import.
    sys.path.insert(0, str(ROOT))
    configure_env()
    import dotenv

    # app.py calls load_dotenv(); a developer's .env must not add an XSUAA
    # binding, IDE settings or anything else to this process.
    dotenv.load_dotenv = lambda *args, **kwargs: False

import json  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

import jwt  # noqa: E402
from pydantic_ai import Agent, ModelRetry  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import (  # noqa: E402
    AgentInfo,
    DeltaToolCall,
    FunctionModel,
)
from pydantic_ai.toolsets import FunctionToolset  # noqa: E402

from agents.db import SessionLocal  # noqa: E402
from agents.deep import DeepConfig, deep_toolset  # noqa: E402
from agents.ide import arc1, diagnose, paths, readonly, runner, store  # noqa: E402
from agents.ide.readonly import ReadOnlyGuard  # noqa: E402
from agents.ide.session_tools import (  # noqa: E402
    IdeRunContext,
    current_ide_run,
    ide_session_toolset,
)
from agents.registry import BuildResult, registry  # noqa: E402

# --- the fake SAP system --------------------------------------------------------

EXISTING = "ZCL_DEMO_EXISTING"
NEW = "ZCL_DEMO_NEW"
EXISTING_PATH = paths.path_for("CLAS", EXISTING)
NEW_PATH = paths.path_for("CLAS", NEW)
VERSION_MARKER = "rev-1"
# The token that makes the fake's syntax dry run report one error.
SYNTAX_TOKEN = "SYNTAXERROR"
SYNTAX_MESSAGE = 'Statement "SYNTAXERROR" is unknown.'

EXISTING_SOURCE = (
    "CLASS zcl_demo_existing DEFINITION PUBLIC FINAL CREATE PUBLIC.\n"
    "  PUBLIC SECTION.\n"
    "    METHODS get_total\n"
    "      RETURNING VALUE(result) TYPE i.\n"
    "ENDCLASS.\n"
    "\n"
    "CLASS zcl_demo_existing IMPLEMENTATION.\n"
    "  METHOD get_total.\n"
    "    result = 1.\n"
    "  ENDMETHOD.\n"
    "ENDCLASS.\n"
)
EXISTING_CHANGED = EXISTING_SOURCE.replace(
    "    result = 1.\n",
    "    \" Counts the open items instead of a constant.\n"
    "    result = lines( mt_items ).\n",
).replace(
    "  PUBLIC SECTION.\n",
    "  PUBLIC SECTION.\n"
    "    DATA mt_items TYPE string_table READ-ONLY.\n",
)
SYNTAX_ERROR_LINE = "    SYNTAXERROR."
NEW_SOURCE = (
    "CLASS zcl_demo_new DEFINITION PUBLIC FINAL CREATE PUBLIC.\n"
    "  PUBLIC SECTION.\n"
    "    METHODS run.\n"
    "ENDCLASS.\n"
    "\n"
    "CLASS zcl_demo_new IMPLEMENTATION.\n"
    "  METHOD run.\n"
    f"{SYNTAX_ERROR_LINE}\n"
    "  ENDMETHOD.\n"
    "ENDCLASS.\n"
)
NEW_SOURCE_FIXED = NEW_SOURCE.replace(
    f"{SYNTAX_ERROR_LINE}\n", "    DATA(total) = NEW zcl_demo_existing( )->get_total( ).\n"
)

DUMP_ID = "DUMP-20261003-0001"
DUMP_ERROR = "COMPUTE_INT_ZERODIVIDE"
_POOL = EXISTING.ljust(30, "=")
DUMPS_LIST = {"dumps": [{
    "id": DUMP_ID,
    "runtimeError": DUMP_ERROR,
    "program": f"{_POOL}CP",
    "include": f"{_POOL}CM001",
    "line": 3,
    "timestamp": "2026-10-03T09:15:00Z",
}]}
DUMP_DETAIL = {
    **DUMPS_LIST["dumps"][0],
    "formattedText": (
        f"Runtime error {DUMP_ERROR}\n"
        "Division by zero in method GET_TOTAL of class ZCL_DEMO_EXISTING.\n"
    ),
}
TRACE_REQUEST_ID = "REQ-E2E-0001"
TRACE_PARAMS = {
    "processType": "http",
    "objectType": "url",
    "maxExecutions": 2,
    "expiresHours": 4,
    "sqlTrace": True,
    "aggregate": False,
    "description": "Trace the demo report",
}


class FakeSap:
    """The in-memory ABAP system behind every ARC-1 call of the e2e.

    One class exists (:data:`EXISTING`, version :data:`VERSION_MARKER`);
    every other object answers 404 with an object-shaped "does not exist",
    which ``arc1.is_not_found`` recognises (new objects show "new in SAP").
    The syntax dry run reports one error on the line holding
    :data:`SYNTAX_TOKEN`; dumps returns one runtime error. Anything else is a
    404 that is *not* object-shaped.
    """

    def __init__(self) -> None:
        # (tool, args) of every direct client call (routes, session tools,
        # run-end checks); the agent's own toolset calls go to ``agent_calls``.
        self.client_calls: list[tuple[str, dict]] = []
        self.agent_calls: list[tuple[str, dict]] = []
        self.armed: list[dict] = []
        self.cancelled: list[str] = []

    def answer(self, tool: str, args: dict[str, Any]) -> str:
        if tool == "SAPRead":
            return self._read(args)
        if tool == "SAPDiagnose":
            action = args.get("action")
            if action == "syntax":
                return json.dumps(_syntax_items(args.get("source")))
            if action == "dumps":
                if args.get("id") in (None, ""):
                    return json.dumps(DUMPS_LIST)
                if args.get("id") == DUMP_ID:
                    return json.dumps(DUMP_DETAIL)
                raise arc1.Arc1Error(404, f"Dump {args.get('id')} does not exist")
        if tool == "SAPLint" and args.get("action") == "lint":
            return "[]"
        raise arc1.Arc1Error(404, f"{tool} {args.get('action') or ''} is not in the e2e fake")

    def _read(self, args: dict[str, Any]) -> str:
        name = str(args.get("name") or "").upper()
        if args.get("type") == "VERSIONS":
            if name == EXISTING:
                return json.dumps({"revisions": [{"id": VERSION_MARKER}]})
            raise arc1.Arc1Error(404, f"Object {name} does not exist")
        if args.get("type") == "CLAS" and name == EXISTING and not args.get("include"):
            return EXISTING_SOURCE
        raise arc1.Arc1Error(404, f"Object {name} does not exist")

    def client(self, target: str, destination: str = "",
               policy: str = readonly.CHANGE) -> FakeArc1Client:
        """Stands in for ``arc1.get_arc1_client``."""
        return FakeArc1Client(self, target, destination, policy)


def _syntax_items(source: Any) -> list[dict[str, Any]]:
    if not isinstance(source, str):
        return []
    return [
        {"line": n, "message": SYNTAX_MESSAGE, "severity": "error"}
        for n, line in enumerate(source.splitlines(), start=1)
        if SYNTAX_TOKEN in line
    ]


class FakeArc1Client:
    """``arc1.Arc1Client``'s interface over :class:`FakeSap`.

    Keeps the real client's gates: the read-only policy before every call,
    trace actions only through :meth:`arm_trace`/:meth:`cancel_trace`, and
    those only with a user JWT bound (``arc1.require_user_context``).
    """

    def __init__(self, sap: FakeSap, target: str, destination: str, policy: str):
        self.sap = sap
        self.target = target
        self.destination = destination
        self.policy = policy

    async def call(self, tool: str, args: dict) -> str:
        reason = readonly.check_call(tool, args, self.policy)
        if reason is None and readonly.needs_approval(tool, args, self.policy):
            reason = "this action runs only through an approval"
        if reason is not None:
            raise arc1.Arc1Refused(reason)
        self.sap.client_calls.append((tool, dict(args)))
        return self.sap.answer(tool, dict(args))

    async def arm_trace(self, params: dict) -> dict[str, str]:
        from agents.ide.approvals import ApprovalError, normalize_request

        try:
            clean = normalize_request("trace_start", params)
        except ApprovalError as exc:
            raise arc1.Arc1Refused(f"invalid trace parameters: {exc.message}") from exc
        if self.policy != readonly.DIAGNOSE:
            raise arc1.Arc1Refused("only an approved trace action of a diagnose session")
        arc1.require_user_context(self.destination)
        self.sap.armed.append(dict(clean))
        expires = datetime.now(timezone.utc) + timedelta(hours=clean["expiresHours"])
        return {"trace_request_id": TRACE_REQUEST_ID,
                "expires_at": expires.isoformat(timespec="seconds")}

    async def cancel_trace(self, request_id: str) -> dict[str, str]:
        if self.policy != readonly.DIAGNOSE:
            raise arc1.Arc1Refused("only an approved trace action of a diagnose session")
        arc1.require_user_context(self.destination)
        self.sap.cancelled.append(request_id)
        return {"trace_request_id": request_id}


def fake_sapdiagnose_toolset(sap: FakeSap) -> FunctionToolset:
    """The agent's ARC-1 server: ``SAPDiagnose`` over :class:`FakeSap`.

    Wrapped in the real ``ReadOnlyGuard`` by :func:`build_agent`, so the
    session policy, finding collection and the trace proposal (never
    forwarded here) are the product's own.
    """
    toolset: FunctionToolset = FunctionToolset(id="e2e-arc1")

    async def SAPDiagnose(  # noqa: N802 -- the ARC-1 tool name
        action: str,
        id: str | None = None,  # noqa: A002 -- ARC-1 argument name
        type: str | None = None,  # noqa: A002 -- ARC-1 argument name
        name: str | None = None,
        source: str | None = None,
        processType: str | None = None,  # noqa: N803
        objectType: str | None = None,  # noqa: N803
        maxExecutions: int | None = None,  # noqa: N803
        expiresHours: int | None = None,  # noqa: N803
        sqlTrace: bool | None = None,  # noqa: N803
        aggregate: bool | None = None,
        description: str | None = None,
    ) -> str:
        """Diagnose ABAP: syntax dry run, runtime errors (dumps), traces."""
        args = {key: value for key, value in {
            "action": action, "id": id, "type": type, "name": name,
            "source": source, "processType": processType,
            "objectType": objectType, "maxExecutions": maxExecutions,
            "expiresHours": expiresHours, "sqlTrace": sqlTrace,
            "aggregate": aggregate, "description": description,
        }.items() if value is not None}
        sap.agent_calls.append(("SAPDiagnose", args))
        try:
            return sap.answer("SAPDiagnose", args)
        except arc1.Arc1Error as exc:
            # What the real MCP server does with a tool error: a retry prompt.
            raise ModelRetry(str(exc.detail)) from None

    toolset.add_function(SAPDiagnose, takes_ctx=False)
    return toolset


# --- the script -------------------------------------------------------------------

CHAT_ANSWER = (
    "ZCL_DEMO_EXISTING computes a total for the demo report. Its only method, "
    "GET_TOTAL, returns a constant today, so the report always shows 1. "
    "Approve the chat when you want a design for counting the open items."
)
DESIGN_V1_MARK = "Version 1 of the design."
DESIGN_V2_MARK = "Version 2 of the design: the review comments are addressed."
COMMENT_ANSWER = "Done: the design now names the package and the new class."


def _design(mark: str) -> str:
    return (
        "# Design: count the open items\n\n"
        f"{mark}\n\n"
        "## Goal\n\nGET_TOTAL of ZCL_DEMO_EXISTING returns the number of open "
        "items instead of a constant.\n\n"
        "## Objects\n\n- CLAS ZCL_DEMO_EXISTING (changed)\n"
        "- CLAS ZCL_DEMO_NEW (new, package $TMP)\n\n"
        "## Tests\n\nABAP Unit for GET_TOTAL with zero, one and many items.\n"
    )


DESIGN_V1 = _design(DESIGN_V1_MARK)
DESIGN_V2 = _design(DESIGN_V2_MARK)
PLAN_V1 = (
    "# Plan\n\n"
    f"1. Change CLAS {EXISTING} at `{EXISTING_PATH}`: count the open items.\n"
    f"2. Create CLAS {NEW} at `{NEW_PATH}`: run the total.\n"
    "3. ABAP Unit for GET_TOTAL.\n"
)
NOTE_V1 = (
    f"Changed {EXISTING}: GET_TOTAL counts the open items.\n\n"
    f"New {NEW}: calls GET_TOTAL. Check the syntax result of both files."
)
NOTE_V2 = f"{NOTE_V1}\n\nRevised: {NEW} no longer has the syntax error."
REVIEW_V1 = (
    "# Review\n\n| Severity | File | Finding |\n|---|---|---|\n"
    f"| info | {EXISTING_PATH} | Counting is correct. |\n\n"
    "Verdict: ready to transport after the syntax error is fixed."
)
REPORT_V1 = (
    "# Diagnosis\n\n"
    f"The dump {DUMP_ID} is a {DUMP_ERROR} in GET_TOTAL of {EXISTING}. "
    "A trace of the next two executions was proposed to confirm the input."
)


@dataclass(frozen=True)
class Turn:
    """One model response: text streamed in deltas, then tool calls."""

    text: str
    calls: tuple[tuple[str, dict], ...] = ()


def _submit(kind: str, content: str) -> tuple[str, dict]:
    return ("submit_document", {"kind": kind, "content": content})


_DOC = {"design": (DESIGN_V1, DESIGN_V2), "plan": (PLAN_V1, PLAN_V1),
        "review": (REVIEW_V1, REVIEW_V1)}
_COMMENT_ID = re.compile(r'<comment id="([^"<>]{1,64})"')


def comment_ids(prompt: str) -> list[str]:
    """The ids of the review comments the runner rendered into the prompt."""
    return list(dict.fromkeys(_COMMENT_ID.findall(prompt or "")))


def turns_for(run: IdeRunContext | None, prompt: str) -> list[Turn]:
    """The whole script of one run. Deterministic: same context, same turns.

    The last turn is the final answer (no tool calls). A run that has a
    response for every turn keeps getting the last one.
    """
    if run is None:
        return [Turn("No IDE session is bound to this run.")]
    stage = run.stage
    if run.session_type == "diagnose":
        if run.report:
            return [
                Turn("Writing the diagnosis report.", (_submit("report", REPORT_V1),)),
                Turn("Submitted the report: one dump, one proposed trace."),
            ]
        return [
            Turn("Reading the recent runtime errors of the system.",
                 (("SAPDiagnose", {"action": "dumps"}),)),
            Turn(f"Opening dump {DUMP_ID}.",
                 (("SAPDiagnose", {"action": "dumps", "id": DUMP_ID}),)),
            Turn("Proposing a trace of the next executions.",
                 (("SAPDiagnose", {"action": "trace_start", **TRACE_PARAMS}),)),
            Turn(f"The dump is a {DUMP_ERROR} in GET_TOTAL of {EXISTING}. "
                 "I proposed a trace; approve it to confirm the input values."),
        ]
    if run.request_changes:
        ids = comment_ids(prompt)
        turns = []
        if ids:
            turns.append(Turn(
                "Working through the review comments.",
                (("resolve_comments", {"items": [
                    {"id": cid, "answer": COMMENT_ANSWER} for cid in ids]}),),
            ))
        if stage == "propose":
            turns.append(Turn(f"Fixing {NEW}.",
                              (("write_file", {"path": NEW_PATH,
                                               "content": NEW_SOURCE_FIXED}),)))
            turns.append(Turn("Updating the change summary.", (_submit("note", NOTE_V2),)))
        elif stage in _DOC:
            turns.append(Turn(f"Submitting the revised {stage}.",
                              (_submit(stage, _DOC[stage][1]),)))
        turns.append(Turn(f"Revised the {stage} and answered every review comment."))
        return turns
    if stage == "chat":
        return [Turn(CHAT_ANSWER)]
    if stage == "design":
        return [
            Turn("Drafting the design for counting the open items.",
                 (_submit("design", DESIGN_V1),)),
            Turn("Submitted design version 1: change ZCL_DEMO_EXISTING and add "
                 "ZCL_DEMO_NEW."),
        ]
    if stage == "plan":
        todos = [{"content": "Change ZCL_DEMO_EXISTING", "status": "in_progress"},
                 {"content": "Create ZCL_DEMO_NEW", "status": "pending"}]
        return [
            Turn("Planning the tasks.", (("write_todos", {"todos": todos}),)),
            Turn("Writing the plan.", (_submit("plan", PLAN_V1),)),
            Turn("Submitted plan version 1 with three tasks."),
        ]
    if stage == "propose":
        return [
            Turn(f"Opening {EXISTING} from SAP.",
                 (("open_object", {"type": "CLAS", "name": EXISTING}),)),
            Turn("Writing the proposals.",
                 (("write_file", {"path": EXISTING_PATH, "content": EXISTING_CHANGED}),
                  ("write_file", {"path": NEW_PATH, "content": NEW_SOURCE}))),
            Turn("Summarising the change.", (_submit("note", NOTE_V1),)),
            Turn(f"Proposed {EXISTING_PATH} and {NEW_PATH}."),
        ]
    if stage == "review":
        return [
            Turn("Reviewing the proposals.", (_submit("review", REVIEW_V1),)),
            Turn("Submitted the review: one finding, the syntax error."),
        ]
    return [Turn("This session is done.")]


def _deltas(text: str, parts: int = 6) -> list[str]:
    """``text`` in at least five pieces (word boundaries), joined unchanged."""
    words = re.findall(r"\S+\s*|\s+", text)
    size = max(1, -(-len(words) // parts))
    chunks = ["".join(words[i:i + size]) for i in range(0, len(words), size)]
    while len(chunks) < 5 and any(len(c) > 1 for c in chunks):
        longest = max(range(len(chunks)), key=lambda i: len(chunks[i]))
        piece = chunks[longest]
        cut = len(piece) // 2
        chunks[longest:longest + 1] = [piece[:cut], piece[cut:]]
    return chunks


@dataclass
class Script:
    """The scripted model: ``fn`` for plain runs, ``stream`` for the runner
    (which always streams). Records every turn it played, for tests."""

    played: list[tuple[str, int]] = field(default_factory=list)
    _calls: int = 0

    def _turn(self, messages: list[ModelMessage]) -> tuple[Turn, bool]:
        run = current_ide_run.get()
        prompt = "\n".join(
            str(part.content)
            for message in messages if isinstance(message, ModelRequest)
            for part in message.parts if isinstance(part, UserPromptPart)
        )
        turns = turns_for(run, prompt)
        n = sum(1 for m in messages if isinstance(m, ModelResponse))
        turn = turns[min(n, len(turns) - 1)]
        self.played.append((run.stage if run else "-", n))
        return turn, n >= len(turns) - 1

    def _call_id(self) -> str:
        self._calls += 1
        return f"e2e-call-{self._calls}"

    async def fn(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        turn, _ = self._turn(messages)
        parts: list[Any] = [TextPart(turn.text)]
        parts += [ToolCallPart(tool, args, tool_call_id=self._call_id())
                  for tool, args in turn.calls]
        return ModelResponse(parts=parts)

    async def stream(self, messages: list[ModelMessage], info: AgentInfo):
        turn, _ = self._turn(messages)
        for delta in _deltas(turn.text):
            yield delta
        if turn.calls:
            yield {
                i: DeltaToolCall(name=tool, json_args=json.dumps(args),
                                 tool_call_id=self._call_id())
                for i, (tool, args) in enumerate(turn.calls)
            }

    def model(self) -> FunctionModel:
        return FunctionModel(self.fn, stream_function=self.stream,
                             model_name="e2e-script")


def build_script() -> Script:
    """A fresh scripted model (see :func:`turns_for`)."""
    return Script()


def build_agent(sap: FakeSap, script: Script, name: str) -> Agent:
    """One IDE agent on the script, with the toolsets a seeded one would
    have in an IDE run: its ARC-1 server behind ``ReadOnlyGuard``, the
    session tools and a deep toolset (plan + scratchpad, no sub-agents)."""
    model = script.model()
    guarded = ReadOnlyGuard(fake_sapdiagnose_toolset(sap))
    deep = deep_toolset(
        DeepConfig(enabled=True, planning=True, scratchpad=True, subagents=False),
        parent_toolsets=[guarded], model=model, agent_name=name,
    )
    return Agent(model, name=name, toolsets=[guarded, ide_session_toolset(), deep])


def install_agents(build: BuildResult, sap: FakeSap) -> None:
    """Put the scripted agent in place of both IDE agents of ``build``."""
    script = build_script()
    for name in {runner.orchestrator_name(), runner.diagnose_agent_name()}:
        build.specialists[name] = build_agent(sap, script, name)


def install(sap: FakeSap, patch: Callable[[Any, str, Any], None] = setattr) -> None:
    """Patch the app's ARC-1 seams and keep the scripted agents across
    registry reloads (``POST /ide/api/admin/seed/refresh``, the admin's
    reload). ``patch`` is ``setattr`` in the server, ``monkeypatch.setattr``
    in tests."""
    patch(arc1, "get_arc1_client", sap.client)
    patch(diagnose, "is_target_server", lambda toolset, run: True)
    original = registry.reload

    async def reload() -> BuildResult:
        build = await original()
        install_agents(build, sap)
        return build

    patch(registry, "reload", reload)


async def seed_conventions() -> None:
    """The one target of the e2e: ``DEMO``, non-production, no destination."""
    async with SessionLocal() as db:
        await store.upsert_conventions(
            db, TARGET, actor="e2e-setup", label="Demo system", non_production=True,
        )


# --- the server -----------------------------------------------------------------

# Unsigned (alg "none"): in local development the app reads claims without
# verifying them. Carries no secret; names the same principal as no token.
DEV_TOKEN = jwt.encode(
    {"sub": PRINCIPAL, "user_name": PRINCIPAL},
    key=None, algorithm="none",
)


class DevTokenMiddleware:
    """Adds ``Authorization: Bearer <DEV_TOKEN>`` to HTTP requests that have
    none, standing in for the approuter's forwarded user JWT."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not any(
            key.lower() == b"authorization" for key, _ in scope.get("headers") or ()
        ):
            scope = dict(scope)
            scope["headers"] = [*scope.get("headers", ()),
                                (b"authorization", f"Bearer {DEV_TOKEN}".encode())]
        await self.app(scope, receive, send)


def _remove_db() -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(f"{DB_FILE}{suffix}").unlink(missing_ok=True)


def main() -> None:
    from contextlib import asynccontextmanager

    import uvicorn

    from app import app  # after configure_env: agents.db reads DATABASE_URL

    _remove_db()
    sap = FakeSap()
    install(sap)
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(asgi_app):
        # The app's own start-up (init_db, registry.reload -> scripted
        # agents) first, then the e2e target.
        async with original(asgi_app) as state:
            await seed_conventions()
            yield state
        # Here, not after uvicorn.run: uvicorn re-raises SIGTERM/SIGINT once
        # it has shut down, which ends the process before a ``finally``.
        from agents.db import engine

        await engine.dispose()
        _remove_db()

    app.router.lifespan_context = lifespan
    port = int(os.environ.get("E2E_STREAM_PORT", PORT))
    print(f"Real-stream e2e backend on http://{HOST}:{port}/ide/api (target {TARGET})")
    try:
        uvicorn.run(DevTokenMiddleware(app), host=HOST, port=port, log_level="info")
    finally:
        _remove_db()  # a start-up failure (port in use, say)


if __name__ == "__main__":
    main()
