"""The diagnose masking layer is gone; the non-production switch still gates.

Phase 1c masked every diagnose tool result for a target that was not flagged
``non_production``. Diagnose runs and reads were already refused for such a
target, so the masking only stood behind those refusals. It was removed
(parked on the local branch ``park/ide-masking``); what stays is the switch,
now named for what it answers: ``diagnose.is_non_production``.

These tests pin both halves: data of a flagged target reaches the model,
the stored activity and the finding detail as ARC-1 sent it, and a target
that lost its flag is still refused everywhere -- with no masked fallback
behind the refusal, the refusal is the whole protection.

Run:  python -m pytest tests/test_ide_no_masking.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import (  # noqa: E402
    AgentInfo,
    DeltaToolCall,
    FunctionModel,
)
from pydantic_ai.toolsets import FunctionToolset  # noqa: E402
from sqlalchemy import select  # noqa: E402

from agents.auth import current_jwt, require_developer  # noqa: E402
from agents.db import SessionLocal, init_db  # noqa: E402
from agents.deep import DeepState, WorkspaceScope, current_workspace  # noqa: E402
from agents.ide import arc1, diagnose, routes, runner  # noqa: E402
from agents.ide.approvals import normalize_request  # noqa: E402
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
from agents.ide.routes import router as ide_router  # noqa: E402
from agents.ide.store import (  # noqa: E402
    create_session,
    upsert_conventions,
    upsert_findings,
)
from agents.registry import BuildResult, registry  # noqa: E402

pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")

ALL_MODELS = (IdeFinding, IdeApproval, IdeAuditLog, IdeWorkspaceFile,
              IdeArtifact, IdeMessage, IdeSession, IdeConventions)
DEST = "arc1-abap-readonly"
DIAG = "abap-diagnostics"
CODE = "target_not_non_production"
HEADERS = {"x-test-user": "alice", "x-test-jwt": "user.jwt.token"}
DUMPS = {"dumps": [{"id": "D1", "user": "DEVUSER01"}]}


# --- fixtures ------------------------------------------------------------------


def _fake_developer(request: Request) -> dict:
    if request.headers.get("x-test-user") != "alice":
        raise HTTPException(status_code=403, detail="Developer scope required")
    return {"user_name": "alice", "scope": ["developer"]}


class _JwtMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        value = dict(scope.get("headers") or []).get(b"x-test-jwt")
        token = current_jwt.set(value.decode() if value else None)
        try:
            await self.app(scope, receive, send)
        finally:
            current_jwt.reset(token)


@pytest.fixture(autouse=True)
async def _clean_db(monkeypatch):
    monkeypatch.delenv("IDE_DIAGNOSE_AGENT", raising=False)
    await init_db()
    async with SessionLocal() as db:
        for model in ALL_MODELS:
            await db.execute(model.__table__.delete())
        await db.commit()
        await upsert_conventions(db, "T1", destination=DEST,
                                 actor="test-admin", non_production=True)
    saved = registry._build
    yield
    registry._build = saved


@pytest.fixture
def sent(monkeypatch):
    """Every ARC-1 client built and every call made; none reaches a network."""
    seen: list = []

    def factory(target, destination="", policy="change"):
        seen.append(("built", policy))

        class _Client:
            async def call(self, tool, args):
                seen.append((tool, dict(args)))
                if tool == "SAPRead" and args.get("type") not in (None, "VERSIONS"):
                    # A source read answers a source: a one-line JSON payload
                    # is an error envelope, not a base (basecheck.usable_source).
                    return "WRITE 2.\n"
                return json.dumps(DUMPS)

        return _Client()

    monkeypatch.setattr(arc1, "get_arc1_client", factory)
    return seen


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(ide_router)
    app.dependency_overrides[require_developer] = _fake_developer
    app.add_middleware(_JwtMiddleware)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


async def _session(session_type: str = "diagnose") -> str:
    async with SessionLocal() as db:
        row = await create_session(db, owner="alice", title="S", target="T1",
                                   session_type=session_type)
        return row.id


async def _lose_flag() -> None:
    async with SessionLocal() as db:
        await upsert_conventions(db, "T1", actor="test-admin", non_production=False)


# --- the module and the switch --------------------------------------------------


def test_masking_module_is_gone():
    assert importlib.util.find_spec("agents.ide.masking") is None


class _Row:
    def __init__(self, value):
        self.non_production = value


@pytest.mark.parametrize("row,expected", [
    (_Row(True), True),
    (_Row(False), False),
    (_Row(None), False),
    (_Row("true"), False),
    (_Row(1), False),
    ({}, False),
    ({"non_production": True}, False),
    (None, False),
])
def test_is_non_production_only_for_real_true(row, expected):
    assert diagnose.is_non_production(row) is expected


def test_old_switch_names_are_gone():
    for name in ("masking_required", "mask_plain", "strip_activity",
                 "STRIPPED_OUTPUT", "Masker"):
        assert not hasattr(diagnose, name), name
    assert "masking" not in DiagnoseRun.__dataclass_fields__
    assert "masker" not in DiagnoseRun.__dataclass_fields__


# --- the guard -----------------------------------------------------------------


class _FakeArc1:
    """Duck-typed wrapped toolset: the guard forwards ``call_tool`` to it."""

    async def get_tools(self, ctx):
        return {}

    async def call_tool(self, name, tool_args, ctx, tool):
        return json.dumps(DUMPS)


async def test_diagnose_tool_result_reaches_the_model_unchanged(monkeypatch):
    monkeypatch.setattr(diagnose, "is_target_server", lambda toolset, run: True)
    scope = WorkspaceScope(session_id="s1", state=DeepState(run_id="s1"),
                           session_type="diagnose")
    run = DiagnoseRun(session_id="s1", owner="alice", target="T1",
                      run_id="r1", destination=DEST)
    w, d = current_workspace.set(scope), current_diagnose.set(run)
    try:
        result = await ReadOnlyGuard(_FakeArc1()).call_tool(
            "SAPDiagnose", {"action": "dumps"}, None, None
        )
    finally:
        current_diagnose.reset(d)
        current_workspace.reset(w)
    assert "DEVUSER01" in diagnose.result_text(result)
    assert json.loads(diagnose.result_text(result)) == DUMPS


# --- the runner -----------------------------------------------------------------


class _Specialist:
    """One tool call, then a text answer; streamed, as the runner asks."""

    def __init__(self, toolsets):
        self.toolsets = toolsets

    @staticmethod
    def _answered(messages: list[ModelMessage]) -> bool:
        return any(isinstance(m, ModelResponse) for m in messages)

    def fn(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if self._answered(messages):
            return ModelResponse(parts=[TextPart("D1 by DEVUSER01")])
        return ModelResponse(parts=[ToolCallPart("SAPDiagnose", {"action": "dumps"})])

    async def stream(self, messages: list[ModelMessage], info: AgentInfo):
        if self._answered(messages):
            yield "D1 by DEVUSER01"
            return
        yield {0: DeltaToolCall(name="SAPDiagnose",
                                json_args=json.dumps({"action": "dumps"}),
                                tool_call_id="call-1")}

    async def run(self, prompt, **kwargs):
        model = FunctionModel(self.fn, stream_function=self.stream)
        return await Agent().run(prompt, model=model, toolsets=self.toolsets,
                                 **kwargs)


async def test_stored_activity_keeps_diagnose_output(monkeypatch):
    monkeypatch.setattr(diagnose, "is_target_server", lambda toolset, run: True)
    ts = FunctionToolset()

    @ts.tool_plain
    def SAPDiagnose(action: str) -> str:  # noqa: N802
        """Diagnose."""
        return json.dumps(DUMPS)

    registry._build = BuildResult(
        orchestrator=None,
        specialists={DIAG: _Specialist([ReadOnlyGuard(ts)])},
        mcp_clients=[], configs=[],
    )
    sid = await _session()
    events: list = []
    await runner.run_stage(sid, "alice", "why the dump?",
                           emit=lambda kind, data: events.append((kind, data)))
    assert [d for k, d in events if k == "error"] == []
    async with SessionLocal() as db:
        stored = (await db.execute(select(IdeMessage).where(
            IdeMessage.session_id == sid, IdeMessage.role == "assistant"
        ))).scalar_one()
    tool_events = [e for e in json.loads(stored.activity_json)["events"]
                   if e.get("kind") == "tool"]
    assert tool_events, stored.activity_json
    assert "DEVUSER01" in tool_events[0]["output"]
    assert "not stored" not in stored.activity_json


# --- the routes -----------------------------------------------------------------


async def test_session_json_has_target_non_production_not_masked(client):
    sid = await _session()
    r = await client.get(f"/ide/api/sessions/{sid}", headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "masked" not in body
    assert body["target_non_production"] is True
    await _lose_flag()
    r = await client.get("/ide/api/sessions", headers=HEADERS)
    assert [(s["id"], s["target_non_production"]) for s in r.json()] == [(sid, False)]


async def test_lost_flag_still_refuses_run_and_detail(client, sent):
    sid = await _session()
    async with SessionLocal() as db:
        [finding] = await upsert_findings(db, sid, [{
            "kind": "dump", "ref_id": "D1", "title": "COMPUTE_INT_ZERODIVIDE",
            "program": "ZDEMO_REPORT", "detail": "raised by DEVUSER01",
        }])
    await _lose_flag()
    r = await client.post(f"/ide/api/sessions/{sid}/messages",
                          json={"text": "why?"}, headers=HEADERS)
    assert r.status_code == 409 and r.json()["code"] == CODE, r.text
    r = await client.get(f"/ide/api/sessions/{sid}/findings/{finding.id}",
                         headers=HEADERS)
    assert r.status_code == 409 and r.json()["code"] == CODE, r.text
    assert "DEVUSER01" not in r.text
    assert sent == []


async def test_client_for_refuses_a_diagnose_read_after_the_flag_went(
    client, sent, monkeypatch
):
    """Defence in depth: the flag taken away between the route's check and
    the client being built is still a refusal, never a raw read."""
    sid = await _session()
    async with SessionLocal() as db:
        [finding] = await upsert_findings(db, sid, [{
            "kind": "dump", "ref_id": "D1", "title": "COMPUTE_INT_ZERODIVIDE",
        }])
    await _lose_flag()

    async def no_check(db, session):
        return None

    monkeypatch.setattr(routes, "_refuse_lost_flag", no_check)
    r = await client.get(
        f"/ide/api/sessions/{sid}/findings/{finding.id}?refresh=true",
        headers=HEADERS,
    )
    assert r.status_code == 409 and r.json()["code"] == CODE, r.text
    assert sent == []


async def test_change_client_is_built_without_the_flag(client, sent):
    """A change session never looks at the flag: the client is still built."""
    sid = await _session("change")
    async with SessionLocal() as db:
        db.add(IdeWorkspaceFile(
            session_id=sid, path="src/PROG/zdemo_report.prog.abap",
            object_type="PROG", object_name="ZDEMO_REPORT", state="read",
            origin_source="WRITE 1.",
        ))
        await db.commit()
    await _lose_flag()
    r = await client.post(f"/ide/api/sessions/{sid}/file/refresh",
                          params={"path": "src/PROG/zdemo_report.prog.abap"},
                          headers=HEADERS)
    assert r.status_code == 200, r.text
    assert sent[0] == ("built", "change")


# --- the trace approval description -------------------------------------------------


def test_trace_description_redacts_email():
    params = normalize_request("trace_start", {
        "processType": "http", "objectType": "url", "maxExecutions": 1,
        "expiresHours": 1, "description": "for jane.doe@example.com\n",
    })
    assert params["description"] == "for [EMAIL]"


def test_trace_description_keeps_numbers_and_names():
    """Only e-mail addresses are redacted now; the rest of masking is gone."""
    params = normalize_request("trace_start", {
        "processType": "http", "objectType": "url", "maxExecutions": 1,
        "expiresHours": 1, "description": "DEVUSER01 order 4711\tslow",
    })
    assert params["description"] == "DEVUSER01 order 4711 slow"
