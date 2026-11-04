"""Basic and advanced access (``agents/access.py``, addendum (d)).

- the effective tier: the web chat's contextvar first, else the developer
  scope in the bound claims (the scope rule of ``require_developer``);
- the orchestrator built by the real ``build_orchestrator`` from one basic
  and one advanced specialist lists the advanced delegation tool only in an
  advanced run, and the advanced specialist under its own heading;
- ``tier`` through the admin API (default, keep, refusal), export, import,
  ``scripts/import_bundle.py`` and the seed loader with the shipped seed.

Run:  .venv/bin/python -m pytest tests/test_access_tier.py -q
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ["MCP_URL_ALLOWLIST"] = ""
os.environ.setdefault("AICORE_AVAILABLE_MODELS", "gpt-4o")

from httpx import ASGITransport, AsyncClient  # noqa: E402
from pydantic_ai.messages import ModelResponse, TextPart  # noqa: E402
from pydantic_ai.models.function import FunctionModel  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from agents import access, auth  # noqa: E402
from agents.db import AgentConfig, SessionLocal, SkillConfig, init_db, upsert_agent  # noqa: E402

PREFIX = "tier-"
SERVER = [{"url": "https://tier.example.com/mcp", "auth_mode": "none"}]
SEED_FILE = ROOT / "agents.seed.json"
DEVELOPER_AGENTS = ("abap-developer-deep", "ui5-fiori-developer-deep", "cap-developer-deep")
DEVELOPER_SKILLS = ("ui5-freestyle-conventions", "fiori-elements-guidance", "cap-cds-modeling")


class FakeValidator:
    """``has_scope`` as ``XsuaaValidator`` answers it for ``xsappname``."""

    xsappname = "app!t1"

    def has_scope(self, payload: dict[str, Any], scope: str) -> bool:
        scopes = payload.get("scope") or []
        return f"{self.xsappname}.{scope}" in scopes or scope in scopes


@pytest.fixture
def validator():
    saved = (auth._validator, auth._validator_checked)
    auth._validator, auth._validator_checked = FakeValidator(), True
    yield
    auth._validator, auth._validator_checked = saved


@pytest.fixture
def open_mode():
    saved = (auth._validator, auth._validator_checked)
    auth._validator, auth._validator_checked = None, True
    yield
    auth._validator, auth._validator_checked = saved


def bind_claims(claims: dict[str, Any] | None):
    return auth.current_claims.set(claims)


DEV = {"user_name": "dev", "scope": ["app!t1.user", "app!t1.developer"]}
USER = {"user_name": "user", "scope": ["app!t1.user"]}
ADMIN = {"user_name": "admin", "scope": ["app!t1.user", "app!t1.admin"]}


# ---------------------------------------------------------------------------
# The effective tier
# ---------------------------------------------------------------------------
def test_tiers_and_the_allowed_table():
    assert access.TIERS == ("basic", "advanced")
    assert access.agent_allowed("basic", "basic")
    assert access.agent_allowed("basic", "advanced")
    assert access.agent_allowed("advanced", "advanced")
    assert not access.agent_allowed("advanced", "basic")
    # A row, and an unknown stored value fails closed.
    assert not access.agent_allowed(SimpleNamespace(tier="advanced"), "basic")
    assert not access.agent_allowed(SimpleNamespace(tier="expert"), "basic")
    assert access.agent_allowed(SimpleNamespace(tier=None), "basic")


def test_effective_tier_follows_the_developer_scope(validator):
    token = bind_claims(DEV)
    try:
        assert access.is_developer(DEV) and access.effective_tier() == "advanced"
    finally:
        auth.current_claims.reset(token)
    token = bind_claims(USER)
    try:
        assert not access.is_developer(USER) and access.effective_tier() == "basic"
    finally:
        auth.current_claims.reset(token)
    assert access.effective_tier() == "basic"  # no claims bound at all
    assert not access.is_developer(None)


def test_the_admin_scope_alone_grants_advanced_access(validator):
    """The admin role has full access: an admin-only token is advanced."""
    assert access.is_developer(ADMIN)
    token = bind_claims(ADMIN)
    try:
        assert access.caller_is_developer() and access.effective_tier() == "advanced"
    finally:
        auth.current_claims.reset(token)
    # A scope of another application's xsappname grants nothing.
    assert not access.is_developer({"scope": ["other!t2.admin", "other!t2.developer"]})


def test_the_bound_access_wins_over_the_claims(validator):
    claims = bind_claims(DEV)
    bound = access.current_access.set("basic")
    try:
        assert access.effective_tier() == "basic"
    finally:
        access.current_access.reset(bound)
        auth.current_claims.reset(claims)
    claims = bind_claims(USER)
    bound = access.current_access.set("advanced")
    try:
        assert access.effective_tier() == "advanced"
    finally:
        access.current_access.reset(bound)
        auth.current_claims.reset(claims)
    bound = access.current_access.set("weird")
    try:
        assert access.effective_tier() == "basic"
    finally:
        access.current_access.reset(bound)


def test_without_a_validator_everyone_is_a_developer(open_mode):
    assert access.is_developer(None) and access.effective_tier() == "advanced"


def test_a_validator_that_cannot_be_set_up_admits_nobody(monkeypatch):
    def broken():
        raise auth.AuthConfigurationError("no binding")

    monkeypatch.setattr(auth, "get_validator", broken)
    assert not access.is_developer(DEV)


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------
async def _wipe_agents() -> None:
    async with SessionLocal() as s:
        await s.execute(delete(AgentConfig))
        await s.execute(delete(SkillConfig))
        await s.commit()


class Recorder:
    def __init__(self) -> None:
        self.tools: list[list[str]] = []

    def respond(self, messages, info) -> ModelResponse:
        self.tools.append(sorted(t.name for t in info.function_tools))
        return ModelResponse(parts=[TextPart("ok")])


async def test_the_advanced_delegation_tool_exists_only_in_an_advanced_run(
        monkeypatch, real_agents_and_mcp, validator):
    from agents import registry as registry_module

    await init_db()
    await _wipe_agents()
    monkeypatch.setattr(registry_module, "get_model",
                        lambda name: FunctionModel(lambda m, i: ModelResponse(parts=[])))
    try:
        async with SessionLocal() as s:
            await upsert_agent(s, name="basic-helper", description="Basic help.",
                               instructions="x", mcp_servers=SERVER)
            await upsert_agent(s, name="dev-helper", description="Developer help.",
                               instructions="x", mcp_servers=SERVER, tier="advanced")
        build = await registry_module.build_orchestrator()
        text = "\n".join(str(i) for i in build.orchestrator._instructions)
        assert "Available specialists:\n- **basic-helper**: Basic help." in text
        assert ("Developer specialists (advanced access only):\n"
                "- **dev-helper**: Developer help.") in text
        assert "**dev-helper**" not in text.split("Developer specialists")[0]

        async def tools_in(tier: str | None, claims: dict | None) -> list[str]:
            rec = Recorder()
            c = bind_claims(claims)
            b = access.current_access.set(tier)
            try:
                await build.orchestrator.run("hi", model=FunctionModel(rec.respond))
            finally:
                access.current_access.reset(b)
                auth.current_claims.reset(c)
            return rec.tools[0]

        assert await tools_in("basic", DEV) == ["delegate_basic_helper"]
        assert await tools_in("advanced", USER) == ["delegate_basic_helper",
                                                    "delegate_dev_helper"]
        # Outside the web chat: the claims decide (classic chat, A2A).
        assert await tools_in(None, USER) == ["delegate_basic_helper"]
        assert await tools_in(None, DEV) == ["delegate_basic_helper", "delegate_dev_helper"]
        assert await tools_in(None, ADMIN) == ["delegate_basic_helper",
                                               "delegate_dev_helper"]
    finally:
        await _wipe_agents()


async def test_peer_delegation_is_not_gated(monkeypatch, real_agents_and_mcp, validator):
    from agents import registry as registry_module

    await init_db()
    await _wipe_agents()
    monkeypatch.setattr(registry_module, "get_model",
                        lambda name: FunctionModel(lambda m, i: ModelResponse(parts=[])))
    try:
        async with SessionLocal() as s:
            await upsert_agent(s, name="dev-peer", description="d", instructions="x",
                               mcp_servers=SERVER, tier="advanced", expose_chat=False)
            await upsert_agent(s, name="caller", description="d", instructions="x",
                               mcp_servers=SERVER, peers=["dev-peer"])
        build = await registry_module.build_orchestrator()
        # Not run (the specialist would open its MCP server): the peer tool
        # is registered without a prepare, while the orchestrator's has one.
        tools = build.specialists["caller"]._function_toolset.tools
        assert tools["delegate_dev_peer"].prepare is None
        assert "delegate_dev_peer" not in build.orchestrator._function_toolset.tools
        assert build.orchestrator._function_toolset.tools["delegate_caller"].prepare
    finally:
        await _wipe_agents()


# ---------------------------------------------------------------------------
# Storage and the admin API
# ---------------------------------------------------------------------------
async def test_upsert_agent_defaults_keeps_and_refuses():
    await init_db()
    async with SessionLocal() as s:
        try:
            row = await upsert_agent(s, name=PREFIX + "store", description="d",
                                     instructions="x", mcp_servers=SERVER)
            assert row.tier == "basic"
            row = await upsert_agent(s, name=PREFIX + "store", description="d",
                                     instructions="x", mcp_servers=SERVER, tier="advanced")
            assert row.tier == "advanced"
            row = await upsert_agent(s, name=PREFIX + "store", description="e",
                                     instructions="x", mcp_servers=SERVER)
            assert row.tier == "advanced" and row.description == "e"
            with pytest.raises(ValueError, match="^tier must be basic or advanced$"):
                await upsert_agent(s, name=PREFIX + "store", description="d",
                                   instructions="x", mcp_servers=SERVER, tier="expert")
        finally:
            await s.rollback()
            await s.execute(delete(AgentConfig).where(AgentConfig.name.like(PREFIX + "%")))
            await s.commit()


def agent_body(name: str, **extra) -> dict:
    return {"name": PREFIX + name, "description": f"{name} d", "instructions": f"{name} i",
            "mcp_servers": SERVER, **extra}


@pytest.fixture
async def admin_client(open_mode):
    import app as app_module

    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app_module.app),
                           base_url="http://test") as c:
        yield c
        r = await c.get("/admin/api/agents")
        for a in r.json():
            if a["name"].startswith(PREFIX):
                await c.delete(f"/admin/api/agents/{a['id']}?force=true")


async def test_admin_payload_default_keep_and_refusal(admin_client):
    c = admin_client
    r = await c.post("/admin/api/agents", json=agent_body("api"))
    assert r.status_code == 201, r.text
    assert r.json()["tier"] == "basic"
    aid = r.json()["id"]
    r = await c.put(f"/admin/api/agents/{aid}", json=agent_body("api", tier="advanced"))
    assert r.status_code == 200 and r.json()["tier"] == "advanced"
    # Absent keeps; the list and the detail carry it.
    r = await c.put(f"/admin/api/agents/{aid}", json=agent_body("api", description="e"))
    assert r.status_code == 200 and r.json()["tier"] == "advanced"
    listed = {a["name"]: a for a in (await c.get("/admin/api/agents")).json()}
    assert listed[PREFIX + "api"]["tier"] == "advanced"
    assert (await c.get(f"/admin/api/agents/{aid}")).json()["tier"] == "advanced"
    # POST of an existing name without the key keeps it too.
    r = await c.post("/admin/api/agents", json=agent_body("api"))
    assert r.status_code == 201 and r.json()["tier"] == "advanced"
    for bad in ("expert", "Advanced", 1, ""):
        r = await c.put(f"/admin/api/agents/{aid}", json=agent_body("api", tier=bad))
        assert r.status_code == 422, bad
        detail = r.json()["detail"]
        assert [d["loc"] for d in detail] == [["body", "tier"]]
        assert "input" not in detail[0]


async def test_export_and_import_carry_tier(admin_client):
    c = admin_client
    r = await c.post("/admin/api/agents", json=agent_body("exp", tier="advanced"))
    assert r.status_code == 201, r.text
    bundle = (await c.get("/admin/api/export")).json()
    (exported,) = [a for a in bundle["agents"] if a["name"] == PREFIX + "exp"]
    assert exported["tier"] == "advanced"
    # Import: a new agent without the key is basic, an existing one keeps its
    # tier, an explicit value is stored.
    r = await c.post("/admin/api/import", json={"agents": [
        agent_body("imp-new"),
        agent_body("exp", description="again"),
        agent_body("imp-adv", tier="advanced"),
    ]})
    assert r.status_code == 200, r.text
    async with SessionLocal() as s:
        rows = {r.name: r.tier for r in (await s.execute(
            select(AgentConfig).where(AgentConfig.name.like(PREFIX + "%")))).scalars()}
    assert rows == {PREFIX + "exp": "advanced", PREFIX + "imp-new": "basic",
                    PREFIX + "imp-adv": "advanced"}
    r = await c.post("/admin/api/import", json={"agents": [agent_body("x", tier="root")]})
    assert r.status_code == 422


async def test_the_import_script_carries_tier(capsys):
    sys.path.insert(0, str(ROOT / "scripts"))
    import import_bundle as script

    await init_db()
    try:
        failed = await script.import_bundle({"agents": [
            agent_body("script-adv", tier="advanced"),
            agent_body("script-plain"),
            agent_body("script-bad", tier="root"),
        ]})
        assert failed == [PREFIX + "script-bad"]
        assert "tier must be basic or advanced" in capsys.readouterr().out
        async with SessionLocal() as s:
            rows = {r.name: r.tier for r in (await s.execute(
                select(AgentConfig).where(AgentConfig.name.like(PREFIX + "script%"))
            )).scalars()}
        assert rows == {PREFIX + "script-adv": "advanced", PREFIX + "script-plain": "basic"}
    finally:
        async with SessionLocal() as s:
            await s.execute(delete(AgentConfig).where(AgentConfig.name.like(PREFIX + "%")))
            await s.commit()


# ---------------------------------------------------------------------------
# The shipped seed
# ---------------------------------------------------------------------------
SEED = json.loads(SEED_FILE.read_text(encoding="utf-8"))


def _seed_agent(name: str) -> dict:
    (agent,) = [a for a in SEED["agents"] if a["name"] == name]
    return agent


def test_the_developer_agents_ship_disabled_advanced_and_deep():
    from agents.admin import AgentPayload, SkillPayload

    for name in DEVELOPER_AGENTS:
        agent = _seed_agent(name)
        payload = AgentPayload.model_validate(agent)
        assert payload.tier == "advanced" and payload.enabled is False
        assert payload.expose_chat is True
        assert payload.deep is not None and payload.deep.enabled
        assert payload.deep.planning and payload.deep.scratchpad and payload.deep.subagents
        lines = agent["instructions"].splitlines()
        assert 20 <= len(lines) <= 40, (name, len(lines))
        low = agent["instructions"].lower()
        for word in ("write_todos", "scratchpad", "test", "verify", "task"):
            assert word in low, (name, word)
    for name in DEVELOPER_SKILLS:
        (skill,) = [s for s in SEED["skills"] if s["name"] == name]
        SkillPayload.model_validate(skill)
        assert 30 <= len(skill["content"].splitlines()) <= 60, name
    abap = _seed_agent("abap-developer-deep")
    assert abap["mcp_servers"] == [{
        "url": "https://arc1.example/SID/100/mcp", "auth_mode": "destination",
        "oauth": {"destination": "ARC1_DEV", "user_context": True}}]
    assert "skills" not in abap
    assert _seed_agent("ui5-fiori-developer-deep")["skills"] == [
        "ui5-freestyle-conventions", "fiori-elements-guidance"]
    assert _seed_agent("cap-developer-deep")["skills"] == ["cap-cds-modeling"]
    for name in ("ui5-fiori-developer-deep", "cap-developer-deep"):
        (server,) = _seed_agent(name)["mcp_servers"]
        assert server["auth_mode"] == "jwt" and "replace-me" in server["url"]


def test_the_developer_texts_are_generic():
    from tests.test_ide_seed_content import EMAIL, forbidden_tokens

    texts = [_seed_agent(n)["instructions"] + _seed_agent(n)["description"]
             for n in DEVELOPER_AGENTS]
    texts += [s["content"] + s["description"] for s in SEED["skills"]
              if s["name"] in DEVELOPER_SKILLS]
    raw = "\n".join(texts)
    for token in forbidden_tokens():
        assert token not in raw.lower(), token
    assert not EMAIL.findall(raw)


async def test_the_seed_loader_accepts_the_developer_agents(caplog):
    from agents.admin import seed_from_file_if_empty
    from agents.db import get_orchestrator_instructions, set_orchestrator_instructions

    await init_db()
    await _wipe_agents()
    async with SessionLocal() as s:
        before = await get_orchestrator_instructions(s)
    try:
        caplog.set_level("INFO")
        await seed_from_file_if_empty(SEED_FILE)
        assert "Skipping invalid seed" not in caplog.text
        async with SessionLocal() as s:
            rows = {r.name: r for r in (await s.execute(select(AgentConfig))).scalars()}
            skills = set((await s.execute(select(SkillConfig.name))).scalars())
        assert set(DEVELOPER_SKILLS) <= skills
        for name in DEVELOPER_AGENTS:
            row = rows[name]
            assert (row.tier, row.enabled, row.expose_chat) == ("advanced", 0, 1)
            assert row.deep.enabled and row.deep.subagents
        assert rows["ABAP Development Agent"].tier == "basic"
        assert rows["ui5-fiori-developer-deep"].skills == [
            "ui5-freestyle-conventions", "fiori-elements-guidance"]
        assert rows["abap-developer-deep"].mcp_servers == [{
            "url": "https://arc1.example/SID/100/mcp", "auth_mode": "destination",
            "oauth": {"destination": "ARC1_DEV", "user_context": True}}]
    finally:
        await _wipe_agents()
        async with SessionLocal() as s:
            await set_orchestrator_instructions(s, before)
