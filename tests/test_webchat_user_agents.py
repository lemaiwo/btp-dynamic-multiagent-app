"""Who answers a web chat turn: the conversation's ``agent_ref``
(``agents/webchat/user_agents.py`` and ``run.resolve_runner``).

The models are pydantic-ai ``FunctionModel``s: the default model (the
orchestrator's) records the instructions and tools each request carried, an
admin specialist answers with its own model. A consulted specialist is wired
by the registry's real ``_attach_delegation_tool``, so the ``agent`` card,
usage and depth rules are the registry's.

Run:  .venv/bin/python -m pytest tests/test_webchat_user_agents.py -q
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
from sqlalchemy import delete, select

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.messages import ToolReturnPart  # noqa: E402
from pydantic_ai.models.function import DeltaToolCall, FunctionModel  # noqa: E402

from agents import auth  # noqa: E402
from agents.auth import current_jwt, current_principal  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    ChatConversation,
    ChatFile,
    ChatMessage,
    ChatUserAgent,
    ChatUserSkill,
    SessionLocal,
    SkillConfig,
    init_db,
)
from agents.registry import RunCounter  # noqa: E402
from agents.validation_errors import install_validation_handler  # noqa: E402
from agents.webchat import my_routes, routes, run, user_agents  # noqa: E402

ALICE = "alice@example.com"
API = "/chat/api"


def bound_app() -> Any:
    inner = FastAPI()
    inner.include_router(routes.router)
    inner.include_router(my_routes.router)
    install_validation_handler(inner)

    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        p = current_principal.set(ALICE)
        j = current_jwt.set("jwt-of-alice")
        try:
            await inner(scope, receive, send)
        finally:
            current_principal.reset(p)
            current_jwt.reset(j)

    return asgi


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=bound_app()),
                           base_url="http://test") as c:
        yield c


async def wipe() -> None:
    async with SessionLocal() as s:
        for model in (ChatFile, ChatMessage, ChatConversation, ChatUserAgent, ChatUserSkill,
                      AgentConfig, SkillConfig):
            await s.execute(delete(model))
        await s.commit()


@pytest.fixture(autouse=True)
async def _clean(monkeypatch):
    saved = (auth._validator, auth._validator_checked)
    auth._validator, auth._validator_checked = None, True
    monkeypatch.setenv("AICORE_AVAILABLE_MODELS", "model-a,model-b")
    monkeypatch.setattr(run, "catalog", run.ModelCatalog())
    monkeypatch.setattr(run, "HEARTBEAT_QUIET_SECONDS", 3600.0)
    await init_db()
    await wipe()
    yield
    await run.cancel_all_chat_runs()
    run._turns.clear()
    await wipe()
    auth._validator, auth._validator_checked = saved


def frames(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def form(text: str = "Hello") -> dict[str, Any]:
    return {"files": [("text", (None, text))]}


class Recorder:
    """The default model: records instructions and tool names per request;
    calls the first ``delegate_*`` tool once when ``consult`` is set, and
    the Word tool once when ``document`` is set."""

    def __init__(self, consult: bool = False, document: bool = False) -> None:
        self.consult = consult
        self.document = document
        self.instructions: list[str | None] = []
        self.tools: list[list[str]] = []
        self.histories: list[int] = []

    async def stream(self, messages, info):
        request = messages[-1]
        self.instructions.append(getattr(request, "instructions", None))
        self.tools.append(sorted(t.name for t in info.function_tools))
        self.histories.append(len(messages))
        returned = any(isinstance(p, ToolReturnPart) for p in request.parts)
        if not returned and self.consult:
            name = next(t for t in self.tools[-1] if t.startswith("delegate_"))
            yield {0: DeltaToolCall(name=name, json_args=json.dumps({"query": "systems?"}),
                                    tool_call_id="deleg-1")}
            return
        if not returned and self.document:
            yield {0: DeltaToolCall(name="create_word_document",
                                    json_args=json.dumps({"filename": "a", "markdown": "# A"}),
                                    tool_call_id="doc-1")}
            return
        yield "answer"


def specialist(answer: str = "3 systems", model_name: str = "spec-model") -> Agent:
    async def stream(messages, info):
        if any(isinstance(p, ToolReturnPart) for p in messages[-1].parts):
            yield answer
            return
        yield {0: DeltaToolCall(name="count", json_args="{}", tool_call_id="spec-call-1")}

    agent = Agent(FunctionModel(stream_function=stream, model_name=model_name))

    @agent.tool_plain
    def count() -> int:
        return 3

    return agent


def use_registry(monkeypatch, model: Recorder, specialists: dict[str, Agent]) -> SimpleNamespace:
    orchestrator = Agent(FunctionModel(stream_function=model.stream,
                                       model_name="orchestrator-model"))
    build = SimpleNamespace(specialists=specialists, in_flight=RunCounter())
    fake = SimpleNamespace(orchestrator=orchestrator, loaded=True, build=build)
    monkeypatch.setattr(run, "registry", fake)
    return fake


async def admin_agent(name: str, *, enabled: bool = True, chat: bool = True) -> None:
    async with SessionLocal() as s:
        s.add(AgentConfig(name=name, description=f"The {name} agent.", instructions="x",
                          mcp_url="https://mcp.example/mcp", enabled=1 if enabled else 0,
                          expose_chat=1 if chat else 0))
        await s.commit()


async def set_admin(name: str, **values) -> None:
    async with SessionLocal() as s:
        row = (await s.execute(select(AgentConfig).where(AgentConfig.name == name))).scalar_one()
        for key, value in values.items():
            setattr(row, key, value)
        await s.commit()


async def admin_skill(name: str, content: str) -> None:
    async with SessionLocal() as s:
        s.add(SkillConfig(name=name, description="d", content=content))
        await s.commit()


async def conversation(client, agent_ref: str | None) -> str:
    r = await client.post(f"{API}/conversations", json={"agent_ref": agent_ref})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def turn(client, cid: str, text: str = "Hello") -> list[tuple[str, dict]]:
    r = await client.post(f"{API}/conversations/{cid}/messages", **form(text))
    assert r.status_code == 200, r.text
    return frames(r.text)


# ---------------------------------------------------------------------------
# A personal agent
# ---------------------------------------------------------------------------
async def test_a_personal_agent_inlines_skills_and_consults_allowed_agents(
        client, monkeypatch, real_agents_and_mcp):
    model = Recorder(consult=True)
    fake = use_registry(monkeypatch, model, {"btp-admin": specialist(),
                                             "off": specialist(), "runonly": specialist()})
    for name in ("btp-admin", "off", "runonly"):
        await admin_agent(name)
    await admin_skill("abap-review", "ADMIN SKILL BODY")
    r = await client.post(f"{API}/my/skills", json={"name": "Tone", "content": "OWN SKILL BODY"})
    assert r.status_code == 201
    r = await client.post(f"{API}/my/skills", json={"name": "Gone", "content": "GONE BODY"})
    gone = r.json()["id"]
    r = await client.post(f"{API}/my/agents", json={
        "name": "Writer", "instructions": "You write reports.",
        "skills": [{"scope": "admin", "name": "abap-review"}, {"scope": "mine", "name": "Tone"},
                   {"scope": "mine", "name": "Gone"}],
        "consults": ["btp-admin", "off", "runonly"]})
    assert r.status_code == 201, r.text
    agent = r.json()
    # After the save: an admin agent switched off, one no longer chat-exposed,
    # and the own skill's row removed behind the cascade's back.
    await set_admin("off", enabled=0)
    await set_admin("runonly", expose_chat=0)
    async with SessionLocal() as s:
        await s.execute(delete(ChatUserSkill).where(ChatUserSkill.id == gone))
        await s.commit()

    cid = await conversation(client, agent["ref"])
    events = await turn(client, cid)
    names = [e for e, _ in events]
    assert names[0] == "start" and names[-1] == "done"
    instructions = model.instructions[0]
    assert instructions.startswith("You write reports.")
    assert "## Skill: abap-review\nADMIN SKILL BODY" in instructions
    assert "## Skill: Tone\nOWN SKILL BODY" in instructions
    assert "GONE BODY" not in instructions
    assert "create_word_document" in instructions.split("## Documents", 1)[1]
    tools = model.tools[0]
    assert "delegate_btp_admin" in tools
    assert "delegate_off" not in tools and "delegate_runonly" not in tools
    assert {"create_word_document", "create_pdf_document", "create_excel_workbook"} <= set(tools)

    message = [d for e, d in events if e == "message"]
    assert message == [{"text": "Skipped: 2 consult(s) / 1 skill(s) no longer available"}]
    agent_frames = [d for e, d in events if e == "agent"]
    assert [d["agent"] for d in agent_frames] == ["btp-admin", "btp-admin"]
    assert agent_frames[-1]["status"] == "done" and agent_frames[-1]["output"] == "3 systems"
    spec_tools = [d for e, d in events if e == "tool"]
    assert spec_tools and all(d["agent"] == "btp-admin" for d in spec_tools)
    usage = next(d for e, d in events if e == "usage")
    assert usage["model"] == "orchestrator-model"
    assert [a["agent"] for a in usage["agents"]] == ["btp-admin"]
    assert fake.build.in_flight.value == 0

    detail = (await client.get(f"{API}/conversations/{cid}")).json()
    assert (detail["agent_ref"], detail["agent_name"]) == (agent["ref"], "Writer")
    assistant = detail["messages"][1]
    assert assistant["status"] == "done"
    assert {"type": "note",
            "text": "Skipped: 2 consult(s) / 1 skill(s) no longer available"} in assistant["parts"]


async def test_own_tool_cards_carry_the_personal_agents_name_and_model(
        client, monkeypatch, real_agents_and_mcp):
    model = Recorder(document=True)
    use_registry(monkeypatch, model, {})
    picked = Recorder()

    def build(name):
        return FunctionModel(stream_function=picked.stream, model_name=name) if name else None

    monkeypatch.setattr(run.catalog, "build", build)
    r = await client.post(f"{API}/my/agents", json={"name": "Doc maker", "instructions": "Docs."})
    plain = r.json()
    r = await client.post(f"{API}/my/agents", json={"name": "Picky", "instructions": "x",
                                                    "model": "model-a"})
    picky = r.json()

    events = await turn(client, await conversation(client, plain["ref"]))
    tools = [d for e, d in events if e == "tool"]
    assert [d["agent"] for d in tools] == ["Doc maker", "Doc maker"]
    assert tools[0]["name"] == "create_word_document"
    assert not [d for e, d in events if e == "message"]  # nothing skipped, no note
    assert next(d for e, d in events if e == "usage")["model"] == "orchestrator-model"

    events = await turn(client, await conversation(client, picky["ref"]))
    assert next(d for e, d in events if e == "usage")["model"] == "model-a"
    assert picked.instructions and len(model.instructions) == 2  # the default ran twice only


async def test_switching_agents_keeps_the_history(client, monkeypatch, real_agents_and_mcp):
    model = Recorder()
    use_registry(monkeypatch, model, {})
    agent = (await client.post(f"{API}/my/agents",
                               json={"name": "Helper", "instructions": "Help."})).json()
    cid = await conversation(client, None)
    await turn(client, cid, "first")
    r = await client.patch(f"{API}/conversations/{cid}", json={"agent_ref": agent["ref"]})
    assert r.status_code == 200
    await turn(client, cid, "second")
    assert model.histories[1] > model.histories[0]
    assert model.instructions[1].startswith("Help.")


async def test_a_deleted_personal_agent_falls_back_to_the_orchestrator(
        client, monkeypatch, real_agents_and_mcp):
    model = Recorder()
    use_registry(monkeypatch, model, {})
    agent = (await client.post(f"{API}/my/agents",
                               json={"name": "Helper", "instructions": "PERSONAL"})).json()
    cid = await conversation(client, agent["ref"])
    assert (await client.delete(f"{API}/my/agents/{agent['id']}")).status_code == 204
    events = await turn(client, cid)
    assert events[-1][1]["status"] == "done"
    assert "PERSONAL" not in (model.instructions[0] or "")


# ---------------------------------------------------------------------------
# An admin agent straight
# ---------------------------------------------------------------------------
async def test_an_admin_agent_runs_straight_with_the_document_tools(
        client, monkeypatch, real_agents_and_mcp):
    model = Recorder()
    spec = specialist(answer="straight answer")
    fake = use_registry(monkeypatch, model, {"btp-admin": spec})
    await admin_agent("btp-admin")
    events = await turn(client, await conversation(client, "admin:btp-admin"))
    assert model.instructions == []  # the orchestrator's model never ran
    tools = [d for e, d in events if e == "tool"]
    assert [(d["agent"], d["name"], d["agent_id"]) for d in tools] == [
        ("btp-admin", "count", None), ("btp-admin", "count", None)]
    assert "".join(d["delta"] for e, d in events if e == "text") == "straight answer"
    assert next(d for e, d in events if e == "usage")["model"] == "spec-model"
    assert events[-1][1]["status"] == "done"
    assert fake.build.in_flight.value == 0


@pytest.mark.parametrize("change", ["disabled", "not_chat", "not_built"])
async def test_an_admin_agent_gone_fails_the_turn_as_agent_unavailable(
        client, monkeypatch, real_agents_and_mcp, change):
    model = Recorder()
    specialists = {"btp-admin": specialist()}
    use_registry(monkeypatch, model, specialists)
    await admin_agent("btp-admin")
    cid = await conversation(client, "admin:btp-admin")
    if change == "disabled":
        await set_admin("btp-admin", enabled=0)
    elif change == "not_chat":
        await set_admin("btp-admin", expose_chat=0)
    else:
        specialists.clear()
    events = await turn(client, cid)
    assert [e for e, _ in events] == ["start", "error", "title", "done"]
    assert events[1][1] == {"code": "agent_unavailable",
                            "message": run.MSG_AGENT_UNAVAILABLE}
    assert events[-1][1]["status"] == "failed"
    assert model.instructions == []
    detail = (await client.get(f"{API}/conversations/{cid}")).json()
    assistant = detail["messages"][1]
    assert assistant["status"] == "failed" and assistant["error"].startswith("agent_unavailable")
    assert detail["status"] == "idle"
    if change != "not_built":
        assert detail["agent_name"] == "btp-admin (deleted)"


# ---------------------------------------------------------------------------
# Units and the walls
# ---------------------------------------------------------------------------
def test_parse_ref():
    assert user_agents.parse_ref(None) == ("orchestrator", None)
    assert user_agents.parse_ref("admin:x") == ("admin", "x")
    assert user_agents.parse_ref("mine:1") == ("mine", "1")
    for bad in ("admin:", "mine:", "x:y", ""):
        with pytest.raises(ValueError):
            user_agents.parse_ref(bad)


def test_personal_agents_never_reach_the_registry_a2a_invoke_or_jobs():
    for module in ("registry.py", "a2a.py", "invoke.py", "job_runner.py",
                   "workflow_runner.py", "chat_app.py"):
        text = (ROOT / "agents" / module).read_text()
        assert "ChatUserAgent" not in text and "chat_user_agents" not in text, module
        assert "webchat" not in text or module == "chat_app.py", module



# ---------------------------------------------------------------------------
# Basic and advanced access (addendum (d))
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
ADVANCED_FORBIDDEN = {"detail": "Advanced access needs the developer role"}


def role_app() -> Any:
    """Like ``bound_app``, with XSUAA claims of the role in ``x-test-role``."""
    inner = FastAPI()
    inner.include_router(routes.router)
    inner.include_router(my_routes.router)
    install_validation_handler(inner)

    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        role = dict(scope["headers"]).get(b"x-test-role", b"user").decode()
        p = current_principal.set(ALICE)
        j = current_jwt.set("jwt-of-alice")
        c = auth.current_claims.set(ROLES[role])
        try:
            await inner(scope, receive, send)
        finally:
            auth.current_claims.reset(c)
            current_principal.reset(p)
            current_jwt.reset(j)

    return asgi


@pytest.fixture
async def roles():
    auth._validator, auth._validator_checked = FakeValidator(), True
    async with AsyncClient(transport=ASGITransport(app=role_app()),
                           base_url="http://test") as c:
        yield c
    # `_clean` restores the validator.


def as_role(role: str) -> dict[str, dict[str, str]]:
    return {"headers": {"x-test-role": role}}


async def test_me_tells_who_may_switch(roles):
    for role, developer in (("user", False), ("developer", True), ("admin", True)):
        me = (await roles.get(f"{API}/me", **as_role(role))).json()
        assert me["developer"] is developer, role
        assert me["access_default"] == "basic" and me["tiers"] == ["basic", "advanced"]


async def test_the_catalogue_lists_advanced_agents_only_for_an_advanced_developer(roles):
    await admin_agent("btp-admin")
    await admin_agent("abap-dev")
    await set_admin("abap-dev", tier="advanced")

    async def listed(role: str, query: str = "") -> dict[str, str]:
        r = await roles.get(f"{API}/catalog{query}", **as_role(role))
        assert r.status_code == 200
        return {a["name"]: a["tier"] for a in r.json()["agents"]}

    assert await listed("developer") == {"btp-admin": "basic"}
    assert await listed("developer", "?access=basic") == {"btp-admin": "basic"}
    assert await listed("user", "?access=advanced") == {"btp-admin": "basic"}
    both = {"btp-admin": "basic", "abap-dev": "advanced"}
    assert await listed("developer", "?access=advanced") == both
    assert await listed("admin", "?access=advanced") == both
    r = await roles.get(f"{API}/catalog?access=root", **as_role("admin"))
    assert r.status_code == 422


async def test_advanced_conversations_need_the_developer_or_admin_role(
        roles, monkeypatch, real_agents_and_mcp):
    await admin_agent("btp-admin")
    await admin_agent("abap-dev")
    await set_admin("abap-dev", tier="advanced")

    r = await roles.post(f"{API}/conversations", json={"access": "advanced"}, **as_role("user"))
    assert (r.status_code, r.json()) == (403, ADVANCED_FORBIDDEN)
    r = await roles.post(f"{API}/conversations", json={}, **as_role("user"))
    assert r.status_code == 201 and r.json()["access"] == "basic"
    basic = r.json()["id"]
    r = await roles.patch(f"{API}/conversations/{basic}", json={"access": "advanced"},
                          **as_role("user"))
    assert (r.status_code, r.json()) == (403, ADVANCED_FORBIDDEN)
    r = await roles.post(f"{API}/conversations", json={"access": "expert"}, **as_role("admin"))
    assert r.status_code == 422

    # The advanced admin agent does not exist for a basic conversation.
    for role in ("user", "developer"):
        r = await roles.post(f"{API}/conversations", json={"agent_ref": "admin:abap-dev"},
                             **as_role(role))
        assert (r.status_code, r.json()) == (422, {"detail": "Unknown agent"})
        r = await roles.patch(f"{API}/conversations/{basic}",
                              json={"agent_ref": "admin:abap-dev"}, **as_role(role))
        assert (r.status_code, r.json()) == (422, {"detail": "Unknown agent"})

    for role in ("developer", "admin"):
        r = await roles.post(f"{API}/conversations",
                             json={"access": "advanced", "agent_ref": "admin:abap-dev"},
                             **as_role(role))
        assert r.status_code == 201, (role, r.text)
        assert (r.json()["access"], r.json()["agent_ref"]) == ("advanced", "admin:abap-dev")
    advanced = r.json()["id"]
    listed = (await roles.get(f"{API}/conversations", **as_role("admin"))).json()
    accesses = {c["id"]: c["access"] for c in listed}
    assert len(accesses) == 3 and accesses[basic] == "basic"
    assert accesses[advanced] == "advanced"
    detail = (await roles.get(f"{API}/conversations/{advanced}", **as_role("admin"))).json()
    assert detail["access"] == "advanced"

    # Back to basic: the advanced agent cannot run there, the conversation
    # falls back to the orchestrator.
    r = await roles.patch(f"{API}/conversations/{advanced}", json={"access": "basic"},
                          **as_role("developer"))
    assert r.status_code == 200
    assert (r.json()["access"], r.json()["agent_ref"]) == ("basic", None)
    r = await roles.patch(f"{API}/conversations/{advanced}", json={"access": "advanced"},
                          **as_role("developer"))
    assert r.json()["access"] == "advanced"

    # The role is checked again at every turn.
    use_registry(monkeypatch, Recorder(), {})
    r = await roles.post(f"{API}/conversations/{advanced}/messages", **form(),
                         **as_role("user"))
    assert (r.status_code, r.json()) == (403, ADVANCED_FORBIDDEN)
    async with SessionLocal() as s:
        conv = await s.get(ChatConversation, advanced)
        assert conv.status == "idle"
    r = await roles.post(f"{API}/conversations/{advanced}/messages", **form(),
                         **as_role("admin"))
    assert r.status_code == 200


async def test_an_advanced_turn_binds_its_access_for_the_orchestrator(
        roles, monkeypatch, real_agents_and_mcp):
    """The turn's task sees the conversation's access, not the role."""
    from agents import access

    seen: list[str] = []

    async def stream(messages, info):
        seen.append(access.effective_tier())
        yield "ok"

    fake = use_registry(monkeypatch, Recorder(), {})
    fake.orchestrator = Agent(FunctionModel(stream_function=stream, model_name="o"))
    for value in ("basic", "advanced"):
        r = await roles.post(f"{API}/conversations", json={"access": value},
                             **as_role("developer"))
        r = await roles.post(f"{API}/conversations/{r.json()['id']}/messages", **form(),
                             **as_role("developer"))
        assert r.status_code == 200
    assert seen == ["basic", "advanced"]
    assert access.current_access.get() is None


async def test_deep_and_advanced_consults_only_for_a_developer_or_admin(roles):
    await admin_agent("btp-admin")
    await admin_agent("abap-dev")
    await set_admin("abap-dev", tier="advanced")
    body = {"name": "Deep one", "instructions": "x", "deep": True}

    r = await roles.post(f"{API}/my/agents", json=body, **as_role("user"))
    assert r.status_code == 422
    assert [d["loc"] for d in r.json()["detail"]] == [["body", "deep"]]
    r = await roles.post(f"{API}/my/agents", json={"name": "Consulter", "instructions": "x",
                                                   "consults": ["btp-admin", "abap-dev"]},
                         **as_role("user"))
    assert r.status_code == 422
    assert [d["loc"] for d in r.json()["detail"]] == [["body", "consults", 1]]
    r = await roles.post(f"{API}/my/agents", json={**body, "deep": "true"}, **as_role("admin"))
    assert r.status_code == 422  # strict boolean

    r = await roles.post(f"{API}/my/agents", json=body, **as_role("developer"))
    assert r.status_code == 201, r.text
    assert (r.json()["deep"], r.json()["tier"]) == (True, "advanced")
    deep_ref = r.json()["ref"]
    r = await roles.post(f"{API}/my/agents", json={"name": "Consulter", "instructions": "x",
                                                   "consults": ["abap-dev"]}, **as_role("admin"))
    assert r.status_code == 201 and r.json()["tier"] == "advanced"
    r = await roles.post(f"{API}/my/agents", json={"name": "Plain", "instructions": "x",
                                                   "consults": ["btp-admin"]}, **as_role("user"))
    assert (r.json()["deep"], r.json()["tier"]) == (False, "basic")
    listed = {a["name"]: a["tier"]
              for a in (await roles.get(f"{API}/my/agents", **as_role("user"))).json()}
    assert listed == {"Consulter": "advanced", "Deep one": "advanced", "Plain": "basic"}

    # An advanced personal agent does not exist for a basic conversation.
    r = await roles.post(f"{API}/conversations", json={"agent_ref": deep_ref},
                         **as_role("developer"))
    assert (r.status_code, r.json()) == (422, {"detail": "Unknown agent"})
    r = await roles.post(f"{API}/conversations",
                         json={"agent_ref": deep_ref, "access": "advanced"},
                         **as_role("developer"))
    assert r.status_code == 201

    # Export carries deep; an import by a plain user drops it and the
    # advanced consult, a developer's or an admin's keeps both.
    bundle = (await roles.get(f"{API}/my/export", **as_role("user"))).json()
    assert {a["name"]: a["deep"] for a in bundle["agents"]} == {
        "Consulter": False, "Deep one": True, "Plain": False}
    r = await roles.post(f"{API}/my/import?on_conflict=replace", json=bundle,
                         **as_role("user"))
    assert r.status_code == 200, r.text
    assert sorted((d["item"], d["field"], d["name"]) for d in r.json()["dropped"]) == [
        ("agent:Consulter", "consults", "abap-dev"), ("agent:Deep one", "deep", None)]
    listed = {a["name"]: (a["deep"], a["tier"], a["consults"])
              for a in (await roles.get(f"{API}/my/agents", **as_role("user"))).json()}
    assert listed["Deep one"] == (False, "basic", [])
    assert listed["Consulter"] == (False, "basic", [])
    for role in ("developer", "admin"):
        r = await roles.post(f"{API}/my/import?on_conflict=replace", json=bundle,
                             **as_role(role))
        assert r.status_code == 200 and r.json()["dropped"] == [], role


async def test_a_deep_personal_agent_runs_with_the_deep_tools(
        roles, monkeypatch, real_agents_and_mcp):
    model = Recorder()
    use_registry(monkeypatch, model, {"abap-dev": specialist(), "btp-admin": specialist()})
    await admin_agent("btp-admin")
    await admin_agent("abap-dev")
    await set_admin("abap-dev", tier="advanced")
    r = await roles.post(f"{API}/my/agents", json={
        "name": "Deep one", "instructions": "You plan.", "deep": True,
        "consults": ["btp-admin", "abap-dev"]}, **as_role("developer"))
    assert r.status_code == 201, r.text
    deep_ref = r.json()["ref"]
    r = await roles.post(f"{API}/my/agents", json={"name": "Flat", "instructions": "x",
                                                   "consults": ["btp-admin"]},
                         **as_role("developer"))
    flat = r.json()["ref"]

    cid = (await roles.post(f"{API}/conversations",
                            json={"access": "advanced", "agent_ref": deep_ref},
                            **as_role("developer"))).json()["id"]
    r = await roles.post(f"{API}/conversations/{cid}/messages", **form(),
                         **as_role("developer"))
    assert r.status_code == 200
    tools = set(model.tools[0])
    assert {"write_todos", "read_todos", "write_file", "read_file", "edit_file", "ls",
            "task"} <= tools
    assert {"delegate_btp_admin", "delegate_abap_dev", "create_word_document"} <= tools
    instructions = model.instructions[0]
    assert instructions.startswith("You plan.") and "write_todos" in instructions

    # A basic personal agent: no deep tools; in a basic conversation.
    model.tools.clear()
    model.instructions.clear()
    cid = (await roles.post(f"{API}/conversations", json={"agent_ref": flat},
                            **as_role("developer"))).json()["id"]
    r = await roles.post(f"{API}/conversations/{cid}/messages", **form(),
                         **as_role("developer"))
    assert r.status_code == 200
    tools = set(model.tools[0])
    assert "write_todos" not in tools and "task" not in tools
    assert "delegate_btp_admin" in tools


async def test_a_consult_gone_advanced_is_not_listed_in_a_basic_turn(
        roles, monkeypatch, real_agents_and_mcp):
    """An admin agent made advanced after the save: the personal agent is
    advanced now and refused in a basic conversation; the turn of an
    advanced conversation lists the consult."""
    model = Recorder()
    use_registry(monkeypatch, model, {"abap-dev": specialist()})
    await admin_agent("abap-dev")
    r = await roles.post(f"{API}/my/agents", json={"name": "C", "instructions": "x",
                                                   "consults": ["abap-dev"]},
                         **as_role("user"))
    ref = r.json()["ref"]
    cid = (await roles.post(f"{API}/conversations", json={"agent_ref": ref},
                            **as_role("user"))).json()["id"]
    await set_admin("abap-dev", tier="advanced")
    r = await roles.post(f"{API}/conversations/{cid}/messages", **form(), **as_role("user"))
    events = frames(r.text)
    assert ("error", {"code": "agent_unavailable",
                      "message": run.MSG_AGENT_UNAVAILABLE}) in events
    assert model.tools == []
