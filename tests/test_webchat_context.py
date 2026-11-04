"""The context of a web chat conversation (``agents/webchat/context.py``,
design addendum 2026-10-31 (e)): ``GET /conversations/{id}/context`` and
``POST /conversations/{id}/context/attachments/{file_id}``.

Turns are real: a pydantic-ai ``FunctionModel`` in place of the
orchestrator records the history every request carried, so an excluded
upload is checked where it matters, in what the next turn sends.

Run:  .venv/bin/python -m pytest tests/test_webchat_context.py -q
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, update

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    BinaryContent,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel  # noqa: E402
from pydantic_ai.usage import RequestUsage  # noqa: E402

from agents import auth  # noqa: E402
from agents.auth import current_jwt, current_principal  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    ChatConversation,
    ChatFile,
    ChatMessage,
    ChatUserAgent,
    ChatUserSkill,
    InaQuery,
    ODataService,
    SessionLocal,
    SkillConfig,
    init_db,
    set_orchestrator_instructions,
)
from agents.registry import RunCounter  # noqa: E402
from agents.validation_errors import install_validation_handler  # noqa: E402
from agents.webchat import context, my_routes, routes, run, store  # noqa: E402

ALICE = "alice@example.com"
BOB = "bob@example.com"
API = "/chat/api"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"PNG-CONTENT" + b"\0" * 32
ORCH_TEXT = "You route requests to specialists."
BAD_BODY = {"detail": 'The body must be exactly {"excluded": true} or {"excluded": false}'}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
class FakeValidator:
    """``has_scope`` as ``XsuaaValidator`` answers it."""

    xsappname = "app!t1"

    def has_scope(self, payload, scope):
        scopes = payload.get("scope") or []
        return f"{self.xsappname}.{scope}" in scopes or scope in scopes


ROLES = {
    "user": {"user_name": "u", "scope": ["app!t1.user"]},
    "developer": {"user_name": "d", "scope": ["app!t1.user", "app!t1.developer"]},
    "admin": {"user_name": "a", "scope": ["app!t1.user", "app!t1.admin"]},
}


def app() -> Any:
    """The chat routers with the principal of ``x-test-user`` and, when a
    validator is installed, the claims of the role in ``x-test-role``."""
    inner = FastAPI()
    inner.include_router(routes.router)
    inner.include_router(my_routes.router)
    install_validation_handler(inner)

    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        headers = dict(scope["headers"])
        user = headers.get(b"x-test-user", ALICE.encode()).decode()
        role = headers.get(b"x-test-role", b"user").decode()
        p = current_principal.set(user)
        j = current_jwt.set(f"jwt-of-{user}")
        c = auth.current_claims.set(ROLES[role])
        try:
            await inner(scope, receive, send)
        finally:
            auth.current_claims.reset(c)
            current_principal.reset(p)
            current_jwt.reset(j)

    return asgi


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app()), base_url="http://test") as c:
        yield c


async def wipe() -> None:
    async with SessionLocal() as s:
        for model in (ChatFile, ChatMessage, ChatConversation, ChatUserAgent, ChatUserSkill,
                      AgentConfig, SkillConfig, ODataService, InaQuery):
            await s.execute(delete(model))
        await s.commit()


@pytest.fixture(autouse=True)
async def _clean(monkeypatch):
    saved = (auth._validator, auth._validator_checked)
    auth._validator, auth._validator_checked = None, True
    monkeypatch.setenv("AICORE_AVAILABLE_MODELS", "gpt-5,model-b")
    monkeypatch.delenv(context.CONTEXT_WINDOWS_ENV, raising=False)
    monkeypatch.setattr(run, "catalog", run.ModelCatalog())
    monkeypatch.setattr(run, "HEARTBEAT_QUIET_SECONDS", 3600.0)
    await init_db()
    await wipe()
    async with SessionLocal() as s:
        await set_orchestrator_instructions(s, ORCH_TEXT)
    yield
    await run.cancel_all_chat_runs()
    run._turns.clear()
    await wipe()
    auth._validator, auth._validator_checked = saved


@pytest.fixture
def roles():
    auth._validator, auth._validator_checked = FakeValidator(), True
    # `_clean` restores the validator.


def as_role(role: str) -> dict[str, dict[str, str]]:
    return {"headers": {"x-test-role": role}}


class Recorder:
    """The default model: answers ``answer`` and keeps the messages every
    request carried (the history the turn sent)."""

    def __init__(self) -> None:
        self.seen: list[list[Any]] = []

    async def stream(self, messages, info):
        self.seen.append(list(messages))
        yield "answer"


def use_registry(monkeypatch, real_agents_and_mcp, model: Recorder) -> SimpleNamespace:
    orchestrator = Agent(FunctionModel(stream_function=model.stream, model_name="gpt-5"))
    build = SimpleNamespace(specialists={}, in_flight=RunCounter())
    fake = SimpleNamespace(orchestrator=orchestrator, loaded=True, build=build)
    monkeypatch.setattr(run, "registry", fake)
    return fake


def frames(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def form(text: str = "Hello", files=()) -> dict[str, Any]:
    return {"files": [("text", (None, text))] + [("files", f) for f in files]}


async def conversation(client, **body) -> str:
    r = await client.post(f"{API}/conversations", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def turn(client, cid: str, text: str = "Hello", files=()) -> list[tuple[str, dict]]:
    r = await client.post(f"{API}/conversations/{cid}/messages", **form(text, files))
    assert r.status_code == 200, r.text
    events = frames(r.text)
    assert events[-1][1]["status"] == "done", events
    return events


async def admin_agent(name: str, *, tier: str = "basic", peers: list[str] | None = None,
                      skills: list[str] | None = None, chat: bool = True) -> None:
    async with SessionLocal() as s:
        s.add(AgentConfig(name=name, description=f"The {name} agent.",
                          instructions=f"ADMIN INSTRUCTIONS OF {name}",
                          mcp_url="https://mcp.example/mcp", enabled=1,
                          expose_chat=1 if chat else 0, tier=tier,
                          peers_json=json.dumps(peers or []),
                          skills_json=json.dumps(skills or [])))
        await s.commit()


async def admin_skill(name: str, content: str) -> None:
    async with SessionLocal() as s:
        s.add(SkillConfig(name=name, description="d", content=content))
        await s.commit()


async def get_context(client, cid: str, role: str = "user") -> dict[str, Any]:
    r = await client.get(f"{API}/conversations/{cid}/context", **as_role(role))
    assert r.status_code == 200, r.text
    return r.json()


async def set_excluded(client, cid: str, fid: str, excluded: Any) -> Any:
    return await client.post(f"{API}/conversations/{cid}/context/attachments/{fid}",
                             json={"excluded": excluded})


def prompt_items(messages: list[Any]) -> list[Any]:
    return [item for m in messages if isinstance(m, ModelRequest)
            for p in m.parts if isinstance(p, UserPromptPart)
            for item in (p.content if isinstance(p.content, list) else [p.content])]


# ---------------------------------------------------------------------------
# Pure parts
# ---------------------------------------------------------------------------
def test_context_windows_table_and_env_override(monkeypatch):
    assert context.context_windows("gpt-4o-mini") == 128_000
    assert context.context_windows("gpt-5.6-terra") == 400_000
    assert context.context_windows("anthropic--claude-4.5-sonnet") == 200_000
    assert context.context_windows("gemini-2.5-pro") == 1_000_000
    assert context.context_windows("mistral-large") is None
    assert context.context_windows(None) is None
    monkeypatch.setenv(context.CONTEXT_WINDOWS_ENV, json.dumps(
        {"mistral": 32000, "gpt-5.6-terra": 272000, "gpt-5": 300000, "bad": "x"}))
    assert context.context_windows("mistral-large") == 32_000  # by prefix
    assert context.context_windows("gpt-5.6-terra") == 272_000  # exact id wins
    assert context.context_windows("gpt-5-mini") == 300_000  # prefix beats the table
    assert context.context_windows("bad-model") is None  # a non-integer is ignored
    monkeypatch.setenv(context.CONTEXT_WINDOWS_ENV, "not json")
    assert context.context_windows("gpt-4o") == 128_000


def two_page_pdf() -> bytes:
    return (b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
            b"2 0 obj << /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >> endobj\n"
            b"3 0 obj << /Type /Page /Parent 2 0 R >> endobj\n"
            b"4 0 obj << /Type /Page /Parent 2 0 R >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF")


def test_estimate_counts_text_by_four_and_binaries_by_kind():
    messages = [
        ModelRequest(parts=[UserPromptPart(content=[
            BinaryContent(data=PNG_BYTES, media_type="image/png", identifier="f1"),
            BinaryContent(data=two_page_pdf(), media_type="application/pdf", identifier="f2"),
            BinaryContent(data=b"%PDF-1.4 nothing" + b"\0" * 150_000,
                          media_type="application/pdf", identifier="f3"),
            "x" * 40,
        ])]),
        ModelResponse(parts=[TextPart(content="y" * 20)]),
    ]
    chars, binary = context.history_figures(messages)
    assert chars == 60
    # image 1,000; two pages 3,000; no page count, 150 KB -> 2 x 1,500.
    assert binary == 1_000 + 3_000 + 3_000
    assert context.estimate_tokens(messages, "i" * 30, ["s" * 10]) == 25 + 7_000
    assert context.estimate_tokens([], "", []) == 0


def test_measured_tokens_reads_the_last_response():
    stored = ModelMessagesTypeAdapter.dump_json([
        ModelRequest(parts=[UserPromptPart(content="a")]),
        ModelResponse(parts=[TextPart(content="b")], usage=RequestUsage(input_tokens=11)),
        ModelRequest(parts=[UserPromptPart(content="c")]),
        ModelResponse(parts=[TextPart(content="d")], usage=RequestUsage(input_tokens=42)),
    ]).decode()
    assert context.measured_tokens(stored) == 42
    assert context.measured_tokens(None) is None
    assert context.measured_tokens("not json") is None
    no_usage = ModelMessagesTypeAdapter.dump_json(
        [ModelResponse(parts=[TextPart(content="d")])]).decode()
    assert context.measured_tokens(no_usage) is None


# ---------------------------------------------------------------------------
# GET /context
# ---------------------------------------------------------------------------
async def test_the_context_of_a_personal_agent(client, monkeypatch, real_agents_and_mcp):
    model = Recorder()
    use_registry(monkeypatch, real_agents_and_mcp, model)
    await admin_skill("abap-review", "ADMIN SKILL BODY")
    r = await client.post(f"{API}/my/skills", json={"name": "Tone", "content": "OWN SKILL BODY"})
    assert r.status_code == 201
    r = await client.post(f"{API}/my/agents", json={
        "name": "Writer", "instructions": "You write reports.",
        "skills": [{"scope": "admin", "name": "abap-review"},
                   {"scope": "mine", "name": "Tone"}]})
    assert r.status_code == 201, r.text
    ref = r.json()["ref"]
    cid = await conversation(client, agent_ref=ref)
    events = await turn(client, cid, "Read this", files=[("p.png", PNG_BYTES, "image/png")])
    file_id = events[0][1]["files"][0]["id"]

    out = await get_context(client, cid)
    assert out["agent"] == {"ref": ref, "name": "Writer", "kind": "mine", "tier": "basic"}
    assert out["access"] == "basic"
    assert out["model"] == {"id": "gpt-5", "context_window": 400_000}
    assert out["instructions"] == {
        "visible": True, "text": "You write reports.", "chars": 18,
        "skills": [
            {"scope": "admin", "name": "abap-review", "chars": 16, "visible": False,
             "text": None},
            {"scope": "mine", "name": "Tone", "chars": 14, "visible": True,
             "text": "OWN SKILL BODY"},
        ]}
    history = out["history"]
    assert (history["turns"], history["messages"]) == (1, 2)
    assert history["attachments"] == [{"file_id": file_id, "name": "p.png",
                                       "media_type": "image/png", "size": len(PNG_BYTES),
                                       "excluded": False}]
    # "Read this" + "answer"; the image is 1,000; instructions and skills too.
    assert history["chars"] == len("Read this") + len("answer")
    assert history["estimated_tokens"] == -(-(15 + 18 + 16 + 14) // 4) + 1_000
    async with SessionLocal() as s:
        stored = (await store.history_json(s, cid))[-1]
    assert history["measured_tokens"] == context.measured_tokens(stored)
    assert history["measured_tokens"] > 0
    assert out["tools"] == [
        {"kind": "document", "name": "create_word_document"},
        {"kind": "document", "name": "create_pdf_document"},
        {"kind": "document", "name": "create_excel_workbook"},
        {"kind": "document", "name": "create_dashboard"},
    ]
    assert out["usage"]["turns"] == 1 and out["usage"]["requests"] == 1
    assert set(out["usage"]) == {"input_tokens", "output_tokens", "total_tokens",
                                 "requests", "turns"}


async def test_admin_text_is_shown_to_an_admin_only(client, roles):
    await admin_agent("btp-admin", skills=["abap-review"])
    await admin_skill("abap-review", "ADMIN SKILL BODY")
    orch = await conversation(client)
    spec = await conversation(client, agent_ref="admin:btp-admin")
    for role, visible in (("user", False), ("developer", False), ("admin", True)):
        out = await get_context(client, orch, role)
        assert out["agent"] == {"ref": None, "name": "Orchestrator", "kind": "orchestrator",
                                "tier": "basic"}
        assert out["instructions"]["visible"] is visible, role
        assert out["instructions"]["chars"] == len(ORCH_TEXT)
        assert out["instructions"]["text"] == (ORCH_TEXT if visible else None)
        out = await get_context(client, spec, role)
        assert out["agent"]["kind"] == "admin" and out["agent"]["name"] == "btp-admin"
        assert out["instructions"]["text"] == (
            "ADMIN INSTRUCTIONS OF btp-admin" if visible else None)
        # Admin skills: name and length only, whoever asks.
        assert out["instructions"]["skills"] == [
            {"scope": "admin", "name": "abap-review", "chars": 16, "visible": False,
             "text": None}]


async def test_without_a_validator_the_text_is_shown(client):
    cid = await conversation(client)
    assert (await get_context(client, cid))["instructions"]["text"] == ORCH_TEXT


async def test_tools_name_the_delegation_targets_the_access_allows(client, roles):
    await admin_agent("btp-admin", peers=["abap-dev", "hidden", "missing"])
    await admin_agent("abap-dev", tier="advanced")
    await admin_agent("hidden", chat=False)
    basic = await conversation(client)
    r = await client.post(f"{API}/conversations", json={"access": "advanced"},
                          **as_role("developer"))
    advanced = r.json()["id"]

    def delegates(out):
        return [(t["name"], t["agent"]) for t in out["tools"] if t["kind"] == "delegate"]

    assert delegates(await get_context(client, basic)) == [("delegate_btp_admin", "btp-admin")]
    assert delegates(await get_context(client, advanced, "developer")) == [
        ("delegate_abap_dev", "abap-dev"), ("delegate_btp_admin", "btp-admin")]
    # An admin agent: its peers (enabled, not approving), not gated by tier.
    spec = await conversation(client, agent_ref="admin:btp-admin")
    assert delegates(await get_context(client, spec)) == [
        ("delegate_abap_dev", "abap-dev"), ("delegate_hidden", "hidden")]


async def test_an_admin_agent_lists_its_connectors_and_the_data_catalogue(client):
    # Run straight (`admin:<name>`), an admin agent has its own servers: one
    # `connector` entry per label (built-in canonical URL or MCP host; never a
    # path, query, credential or destination name), plus the data catalogue's
    # two tools once one of its OData services exists and is enabled.
    odata = {"url": "builtin:odata", "auth_mode": "destination",
             "oauth": {"services": ["sales-orders", "gone"]}}
    jira = {"url": "builtin:jira", "auth_mode": "destination",
            "oauth": {"destination": "JIRA_SECRET_DEST", "project": "P"}}
    async with SessionLocal() as s:
        s.add(AgentConfig(name="sap-data", description="d", instructions="x",
                          mcp_url="https://mcp.example.com/sse/path?token=abc",
                          auth_mode="jwt", enabled=1, expose_chat=1,
                          extra_servers_json=json.dumps([odata, jira])))
        s.add(ODataService(name="sales-orders", title="Sales orders", purpose="p",
                           destination="S4_ODATA_USER", user_context=1, odata_version="v2",
                           service_path="/sap/opu/odata/sap/API_SALES_ORDER_SRV",
                           definition_json="{}", enabled=1))
        await s.commit()
    cid = await conversation(client, agent_ref="admin:sap-data")

    def of(out, kind):
        return [t for t in out["tools"] if t["kind"] == kind]

    out = await get_context(client, cid)
    assert of(out, "connector") == [
        {"kind": "connector", "name": "mcp.example.com", "agent": "sap-data"},
        {"kind": "connector", "name": "builtin:odata", "agent": "sap-data"},
        {"kind": "connector", "name": "builtin:jira", "agent": "sap-data"},
    ]
    assert of(out, "data_catalog") == [
        {"kind": "data_catalog", "name": "find_data_source", "agent": "sap-data"},
        {"kind": "data_catalog", "name": "list_data_sources", "agent": "sap-data"},
    ]
    text = json.dumps(out["tools"])
    for secret in ("token=abc", "/sse/path", "JIRA_SECRET_DEST", "S4_ODATA_USER"):
        assert secret not in text, secret
    # No enabled service behind the entry: the catalogue tools are not attached.
    async with SessionLocal() as s:
        await s.execute(update(ODataService).values(enabled=0))
        await s.commit()
    out = await get_context(client, cid)
    assert of(out, "data_catalog") == [] and len(of(out, "connector")) == 3
    # The orchestrator lists no connector of its own.
    orch = await conversation(client)
    assert of(await get_context(client, orch), "connector") == []


async def test_an_admin_agent_with_an_ina_query_lists_the_data_catalogue(client):
    # The registry scopes the catalogue tools to `builtin:ina` queries too
    # (`allowed_sources_of`), so an InA-only agent lists them once one of its
    # queries is registered and enabled.
    ina = {"url": "builtin:ina", "auth_mode": "destination",
           "oauth": {"queries": ["sales-by-month"]}}
    async with SessionLocal() as s:
        s.add(AgentConfig(name="analyst", description="d", instructions="x",
                          mcp_url=ina["url"], auth_mode="destination",
                          oauth_json=json.dumps(ina["oauth"]), enabled=1, expose_chat=1))
        s.add(InaQuery(name="sales-by-month", title="Sales", purpose="p",
                       destination="SIA_INA_SECRET_DEST", user_context=0,
                       query="ZQ_SALES_01", enabled=1))
        await s.commit()
    cid = await conversation(client, agent_ref="admin:analyst")

    def of(out, kind):
        return [t for t in out["tools"] if t["kind"] == kind]

    out = await get_context(client, cid)
    assert of(out, "connector") == [
        {"kind": "connector", "name": "builtin:ina", "agent": "analyst"}]
    assert [t["name"] for t in of(out, "data_catalog")] == [
        "find_data_source", "list_data_sources"]
    assert "SIA_INA_SECRET_DEST" not in json.dumps(out["tools"])
    async with SessionLocal() as s:
        await s.execute(update(InaQuery).values(enabled=0))
        await s.commit()
    assert of(await get_context(client, cid), "data_catalog") == []


async def test_a_deep_personal_agent_lists_its_deep_tools_and_consults(client):
    await admin_agent("btp-admin")
    r = await client.post(f"{API}/my/agents", json={
        "name": "Deep", "instructions": "x", "deep": True, "consults": ["btp-admin"]})
    assert r.status_code == 201, r.text
    cid = await conversation(client, agent_ref=r.json()["ref"], access="advanced")
    out = await get_context(client, cid)
    assert out["agent"]["tier"] == "advanced"
    deep = [t["name"] for t in out["tools"] if t["kind"] == "deep"]
    assert {"write_todos", "read_todos", "ls", "read_file", "write_file", "edit_file",
            "task"} == set(deep)
    assert [t for t in out["tools"] if t["kind"] == "delegate"] == [
        {"kind": "delegate", "name": "delegate_btp_admin", "agent": "btp-admin"}]


async def test_context_of_another_users_conversation_is_404(client):
    cid = await conversation(client)
    for method, path, kw in (
        ("GET", f"/conversations/{cid}/context", {}),
        ("POST", f"/conversations/{cid}/context/attachments/x", {"json": {"excluded": True}}),
    ):
        r = await client.request(method, API + path, headers={"x-test-user": BOB}, **kw)
        assert r.status_code == 404, (method, path)


# ---------------------------------------------------------------------------
# Excluding an attachment
# ---------------------------------------------------------------------------
async def test_exclude_and_include_round_trip(client, monkeypatch, real_agents_and_mcp):
    model = Recorder()
    use_registry(monkeypatch, real_agents_and_mcp, model)
    cid = await conversation(client)
    events = await turn(client, cid, "Look", files=[("p.png", PNG_BYTES, "image/png"),
                                                     ("n.csv", b"a,b\n1,2\n", "text/csv")])
    png, csv = (f["id"] for f in events[0][1]["files"])
    before = (await get_context(client, cid))["history"]

    r = await set_excluded(client, cid, png, True)
    assert r.status_code == 200 and r.json() == {"file_id": png, "excluded": True}
    assert (await set_excluded(client, cid, png, True)).json() == {"file_id": png,
                                                                    "excluded": True}
    r = await set_excluded(client, cid, csv, True)
    assert r.json() == {"file_id": csv, "excluded": True}
    after = (await get_context(client, cid))["history"]
    assert [a["excluded"] for a in after["attachments"]] == [True, True]
    assert after["estimated_tokens"] < before["estimated_tokens"] - 900
    assert (await store_status(cid)) == "idle"

    await turn(client, cid, "Again")
    items = prompt_items(model.seen[-1])
    assert not any(isinstance(i, BinaryContent) for i in items)
    assert '[attachment "p.png" removed from the context]' in items
    assert '[attachment "n.csv" removed from the context]' in items
    assert not any(isinstance(i, str) and "a,b" in i for i in items)

    assert (await set_excluded(client, cid, png, False)).json() == {"file_id": png,
                                                                     "excluded": False}
    assert (await set_excluded(client, cid, csv, False)).status_code == 200
    assert [a["excluded"] for a in
            (await get_context(client, cid))["history"]["attachments"]] == [False, False]
    await turn(client, cid, "Third")
    items = prompt_items(model.seen[-1])
    binaries = [i for i in items if isinstance(i, BinaryContent)]
    assert len(binaries) == 1 and binaries[0].data == PNG_BYTES
    assert binaries[0].identifier == png
    assert any(isinstance(i, str) and i.startswith('<attachment name="n.csv"') and "a,b" in i
               for i in items)
    assert not any(isinstance(i, str) and "removed from the context" in i for i in items)
    # The file stays downloadable throughout.
    assert (await client.get(f"{API}/files/{png}")).content == PNG_BYTES


async def store_status(cid: str) -> str:
    async with SessionLocal() as s:
        return (await s.get(ChatConversation, cid)).status


async def test_exclude_is_refused_while_a_turn_runs(client, monkeypatch, real_agents_and_mcp):
    use_registry(monkeypatch, real_agents_and_mcp, Recorder())
    cid = await conversation(client)
    events = await turn(client, cid, files=[("p.png", PNG_BYTES, "image/png")])
    png = events[0][1]["files"][0]["id"]
    async with SessionLocal() as s:
        await s.execute(update(ChatConversation).where(ChatConversation.id == cid)
                        .values(status="running"))
        await s.commit()
    r = await set_excluded(client, cid, png, True)
    assert r.status_code == 409 and r.json() == {"detail": "a run is in progress"}
    assert await store_status(cid) == "running"  # the turn's lock is not released
    async with SessionLocal() as s:
        await s.execute(update(ChatConversation).where(ChatConversation.id == cid)
                        .values(status="idle"))
        await s.commit()
    assert not (await get_context(client, cid))["history"]["attachments"][0]["excluded"]


async def test_exclude_is_404_for_a_file_that_is_not_an_upload_here(
        client, monkeypatch, real_agents_and_mcp):
    use_registry(monkeypatch, real_agents_and_mcp, Recorder())
    cid = await conversation(client)
    other = await conversation(client)
    events = await turn(client, other, files=[("p.png", PNG_BYTES, "image/png")])
    foreign = events[0][1]["files"][0]["id"]
    await turn(client, cid)
    async with SessionLocal() as s:
        generated = await store.add_generated_file(
            s, conversation_id=cid, message_id=None, principal=ALICE, name="a.docx",
            media_type="application/octet-stream", data=b"PK")
    for fid in (generated["file_id"], foreign, "no-such-file"):
        r = await set_excluded(client, cid, fid, True)
        assert r.status_code == 404 and r.json() == {"detail": "No such file"}, fid
    assert await store_status(cid) == "idle"


async def test_exclude_takes_exactly_one_boolean(client):
    cid = await conversation(client)
    path = f"{API}/conversations/{cid}/context/attachments/x"
    for kw in ({"json": {"excluded": "true"}}, {"json": {"excluded": True, "x": 1}},
               {"json": []}, {"json": {}}, {"content": b"not json"},
               {"json": {"excluded": 1}}):
        r = await client.post(path, **kw)
        assert r.status_code == 422 and r.json() == BAD_BODY, kw


def test_tool_returns_count_as_text():
    messages = [ModelRequest(parts=[ToolReturnPart(tool_name="t", content="z" * 8,
                                                   tool_call_id="c")])]
    assert context.history_figures(messages) == (8, 0)
