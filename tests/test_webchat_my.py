"""Personal agents and skills of the web chat, the bundle and the admin
catalogue: ``/chat/api/catalog`` and ``/chat/api/my/...``
(``agents/webchat/my_routes.py``, ``my_store.py``, ``my_models.py``).

Same harness as ``tests/test_webchat_api.py``: the routers on a bare app
behind an ASGI wrapper that binds the principal from a test header.

Run:  .venv/bin/python -m pytest tests/test_webchat_my.py -q
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
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

from agents import auth  # noqa: E402
from agents.auth import current_jwt, current_principal  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    ChatConversation,
    ChatUserAgent,
    ChatUserSkill,
    SessionLocal,
    SkillConfig,
    init_db,
)
from agents.validation_errors import install_validation_handler  # noqa: E402
from agents.webchat import my_models, my_routes, routes, run  # noqa: E402

ALICE = "alice@example.com"
BOB = "bob@example.com"
API = "/chat/api"
SECRET = "S3cr3t-value-xyz"


def bound_app() -> Any:
    inner = FastAPI()
    inner.include_router(routes.router)
    inner.include_router(my_routes.router)
    install_validation_handler(inner)

    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        user = dict(scope["headers"]).get(b"x-test-user", ALICE.encode()).decode()
        p = current_principal.set(user)
        j = current_jwt.set(f"jwt-of-{user}")
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
        for model in (ChatUserAgent, ChatUserSkill, ChatConversation, AgentConfig, SkillConfig):
            await s.execute(delete(model))
        await s.commit()


@pytest.fixture(autouse=True)
async def _clean(monkeypatch):
    saved = (auth._validator, auth._validator_checked)
    auth._validator, auth._validator_checked = None, True
    monkeypatch.setenv("AICORE_AVAILABLE_MODELS", "model-a,model-b")
    monkeypatch.setattr(run, "catalog", run.ModelCatalog())
    monkeypatch.setattr(run.catalog, "default_name", lambda: "default-model")
    await init_db()
    await wipe()
    yield
    await wipe()
    auth._validator, auth._validator_checked = saved


async def admin_agent(name: str, *, enabled: bool = True, chat: bool = True,
                      description: str = "Admin agent.", servers: list | None = None) -> None:
    async with SessionLocal() as s:
        s.add(AgentConfig(name=name, description=description, instructions="secret admin text",
                          mcp_url="https://mcp.example/mcp", enabled=1 if enabled else 0,
                          expose_chat=1 if chat else 0,
                          extra_servers_json=json.dumps(servers) if servers else None))
        await s.commit()


async def admin_skill(name: str, description: str = "Admin skill.") -> None:
    async with SessionLocal() as s:
        s.add(SkillConfig(name=name, description=description, content="admin skill body"))
        await s.commit()


def agent_body(name: str = "Helper", **extra) -> dict[str, Any]:
    return {"name": name, "instructions": "Be helpful.", **extra}


async def create_agent(client, user: str = ALICE, **body) -> dict[str, Any]:
    r = await client.post(f"{API}/my/agents", json=agent_body(**body),
                          headers={"x-test-user": user})
    assert r.status_code == 201, r.text
    return r.json()


async def create_skill(client, name: str = "Tone", user: str = ALICE, **extra) -> dict[str, Any]:
    r = await client.post(f"{API}/my/skills", json={"name": name, "content": "Be brief.", **extra},
                          headers={"x-test-user": user})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------
async def test_catalog_shows_chat_usable_admin_agents_and_all_skills(client):
    await admin_agent("btp-admin", description="Runs\x00 the " + "x" * 400)
    await admin_agent("off", enabled=False)
    await admin_agent("runonly", chat=False)
    await admin_agent("approver", servers=[{"url": "builtin:bitbucket", "auth_mode": "destination",
                                            "oauth": {"allow_approve": True}}])
    await admin_skill("abap-review", description="Reviews ABAP.\nSecond line")
    out = (await client.get(f"{API}/catalog")).json()
    assert [a["name"] for a in out["agents"]] == ["btp-admin"]
    agent = out["agents"][0]
    assert agent == {"ref": "admin:btp-admin", "name": "btp-admin",
                     "description": agent["description"], "tier": "basic", "source": "admin",
                     "readonly": True}
    assert len(agent["description"]) <= 300 and "\x00" not in agent["description"]
    assert out["skills"] == [{"name": "abap-review", "description": "Reviews ABAP. Second line",
                              "source": "admin", "readonly": True}]
    assert out["orchestrator"]["ref"] is None and out["orchestrator"]["name"] == "Orchestrator"
    assert "secret admin text" not in json.dumps(out)
    assert "admin skill body" not in json.dumps(out)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
async def test_agent_crud_and_shape(client):
    await admin_agent("btp-admin")
    await admin_skill("abap-review")
    await create_skill(client, "Tone")
    made = await create_agent(
        client, name="Writer", description="Writes\nthings", model="model-a",
        instructions="Write.\r\nWell\x07.\tOK",
        skills=[{"scope": "admin", "name": "abap-review"}, {"scope": "mine", "name": "tone"},
                {"scope": "mine", "name": "TONE"}],
        consults=["btp-admin", "btp-admin"])
    assert set(made) == {"id", "ref", "name", "description", "instructions", "skills", "consults",
                         "model", "deep", "tier", "created_at", "updated_at", "source",
                         "readonly"}
    assert made["ref"] == f"mine:{made['id']}"
    assert made["description"] == "Writes things"
    assert made["instructions"] == "Write.\nWell.\tOK"
    assert made["skills"] == [{"scope": "admin", "name": "abap-review"},
                              {"scope": "mine", "name": "Tone"}]
    assert made["consults"] == ["btp-admin"]
    assert (made["model"], made["source"], made["readonly"]) == ("model-a", "mine", False)

    listed = (await client.get(f"{API}/my/agents")).json()
    assert [a["id"] for a in listed] == [made["id"]]
    assert (await client.get(f"{API}/my/agents/{made['id']}")).json() == made

    r = await client.put(f"{API}/my/agents/{made['id']}", json=agent_body("Writer 2"))
    assert r.status_code == 200
    assert (r.json()["name"], r.json()["skills"], r.json()["model"]) == ("Writer 2", [], None)
    assert (await client.delete(f"{API}/my/agents/{made['id']}")).status_code == 204
    assert (await client.get(f"{API}/my/agents/{made['id']}")).status_code == 404


async def test_names_are_unique_per_user_case_insensitive(client):
    first = await create_agent(client, name="Helper")
    r = await client.post(f"{API}/my/agents", json=agent_body("HELPER"))
    assert r.status_code == 409 and r.json() == {"detail": "an agent of this name exists"}
    await create_agent(client, user=BOB, name="helper")  # another user's namespace
    other = await create_agent(client, name="Other")
    r = await client.put(f"{API}/my/agents/{other['id']}", json=agent_body("helper"))
    assert r.status_code == 409
    r = await client.put(f"{API}/my/agents/{first['id']}", json=agent_body("HELPER"))
    assert r.status_code == 200 and r.json()["name"] == "HELPER"
    await create_skill(client, "Tone")
    r = await client.post(f"{API}/my/skills", json={"name": "tone", "content": "x"})
    assert r.status_code == 409 and r.json() == {"detail": "a skill of this name exists"}


@pytest.mark.parametrize("body", [
    {"name": "", "instructions": "x"},
    {"name": "-dash first", "instructions": "x"},
    {"name": "a" * 65, "instructions": "x"},
    {"name": f"bad<{SECRET}>", "instructions": "x"},
    {"name": "ok", "instructions": " \n "},
    {"name": "ok", "instructions": "x" * 20_001},
    {"name": "ok", "instructions": "x", "description": "d" * 301},
    {"name": "ok", "instructions": "x", "skills": [{"scope": "mine", "name": f"s{i}"}
                                                    for i in range(21)]},
    {"name": "ok", "instructions": "x", "consults": [f"c{i}" for i in range(11)]},
    {"name": "ok", "instructions": "x", "mcp_url": SECRET},
    {"name": "ok", "instructions": 5},
    {"name": "ok", "instructions": "x", "skills": [{"scope": "global", "name": "a"}]},
])
async def test_agent_limits_are_422_without_the_input(client, body):
    r = await client.post(f"{API}/my/agents", json=body)
    assert r.status_code == 422, r.text
    assert SECRET not in r.text
    assert all({"loc", "msg", "type"} == set(d) for d in r.json()["detail"])


async def test_skill_limits(client):
    for body in ({"name": "s", "content": ""}, {"name": "s", "content": "x" * 50_001},
                 {"name": "s", "content": "x", "extra": SECRET}):
        r = await client.post(f"{API}/my/skills", json=body)
        assert r.status_code == 422 and SECRET not in r.text
    made = await create_skill(client, "Tone", description="One\nline", content="a\x00b")
    assert (made["description"], made["content"]) == ("One line", "ab")


async def test_references_must_be_usable_now(client):
    await admin_agent("off", enabled=False)
    await admin_agent("runonly", chat=False)
    for body, loc in (
        (agent_body(skills=[{"scope": "admin", "name": "nope"}]), ["body", "skills", 0, "name"]),
        (agent_body(skills=[{"scope": "mine", "name": "nope"}]), ["body", "skills", 0, "name"]),
        (agent_body(consults=["off"]), ["body", "consults", 0]),
        (agent_body(consults=["runonly"]), ["body", "consults", 0]),
        (agent_body(model=SECRET), ["body", "model"]),
    ):
        r = await client.post(f"{API}/my/agents", json=body)
        assert r.status_code == 422, r.text
        assert r.json()["detail"][0]["loc"] == loc and SECRET not in r.text


async def test_per_user_limits(client, monkeypatch):
    monkeypatch.setattr(my_models, "MAX_AGENTS", 2)
    monkeypatch.setattr(my_routes.m, "MAX_AGENTS", 2)
    await create_agent(client, name="a1")
    await create_agent(client, name="a2")
    r = await client.post(f"{API}/my/agents", json=agent_body("a3"))
    assert r.status_code == 422 and r.json() == {"detail": my_routes.AGENT_LIMIT}


async def test_owner_scoping_is_404(client):
    agent = await create_agent(client)
    skill = await create_skill(client)
    bob = {"x-test-user": BOB}
    assert (await client.get(f"{API}/my/agents", headers=bob)).json() == []
    assert (await client.get(f"{API}/my/skills", headers=bob)).json() == []
    for method, path, kw in (
        ("GET", f"/my/agents/{agent['id']}", {}),
        ("PUT", f"/my/agents/{agent['id']}", {"json": agent_body()}),
        ("DELETE", f"/my/agents/{agent['id']}", {}),
        ("GET", f"/my/skills/{skill['id']}", {}),
        ("PUT", f"/my/skills/{skill['id']}", {"json": {"name": "x", "content": "y"}}),
        ("DELETE", f"/my/skills/{skill['id']}", {}),
    ):
        r = await client.request(method, API + path, headers=bob, **kw)
        assert r.status_code == 404, (method, path)
    # Bob cannot point a conversation at Alice's agent, nor attach her skill.
    r = await client.post(f"{API}/conversations", json={"agent_ref": agent["ref"]}, headers=bob)
    assert r.status_code == 422 and r.json() == {"detail": "Unknown agent"}
    r = await client.post(f"{API}/my/agents", headers=bob,
                          json=agent_body(skills=[{"scope": "mine", "name": "Tone"}]))
    assert r.status_code == 422
    out = (await client.get(f"{API}/my/export", headers=bob,
                            params={"agents": agent["id"]})).json()
    assert out["agents"] == [] and out["skills"] == []


# ---------------------------------------------------------------------------
# Cascades and the conversation's agent_ref
# ---------------------------------------------------------------------------
async def test_deleting_an_agent_resets_conversations(client):
    await admin_agent("btp-admin")
    agent = await create_agent(client)
    r = await client.post(f"{API}/conversations", json={"agent_ref": agent["ref"]})
    assert r.status_code == 201
    conv = r.json()
    assert (conv["agent_ref"], conv["agent_name"]) == (agent["ref"], "Helper")
    other = (await client.post(f"{API}/conversations", json={})).json()
    r = await client.patch(f"{API}/conversations/{other['id']}",
                           json={"agent_ref": "admin:btp-admin"})
    assert (r.json()["agent_ref"], r.json()["agent_name"]) == ("admin:btp-admin", "btp-admin")
    for bad in ("admin:nope", "mine:nope", "elsewhere:x", SECRET):
        r = await client.patch(f"{API}/conversations/{other['id']}", json={"agent_ref": bad})
        assert r.status_code == 422 and r.json() == {"detail": "Unknown agent"}

    listed = {c["id"]: c for c in (await client.get(f"{API}/conversations")).json()}
    assert listed[conv["id"]]["agent_name"] == "Helper"
    assert (await client.delete(f"{API}/my/agents/{agent['id']}")).status_code == 204
    detail = (await client.get(f"{API}/conversations/{conv['id']}")).json()
    assert (detail["agent_ref"], detail["agent_name"]) == (None, "Orchestrator")

    # An admin agent switched off later: shown as deleted, the ref is kept.
    async with SessionLocal() as s:
        row = (await s.execute(select(AgentConfig))).scalar_one()
        row.expose_chat = 0
        await s.commit()
    detail = (await client.get(f"{API}/conversations/{other['id']}")).json()
    assert (detail["agent_ref"], detail["agent_name"]) == ("admin:btp-admin",
                                                          "btp-admin (deleted)")
    r = await client.patch(f"{API}/conversations/{other['id']}", json={"agent_ref": None})
    assert r.json()["agent_ref"] is None


async def test_deleting_or_renaming_a_skill_updates_the_agents(client):
    await admin_skill("abap-review")
    await create_skill(client, "Tone")
    keep = await create_skill(client, "Keep")
    agent = await create_agent(client, skills=[{"scope": "mine", "name": "Tone"},
                                               {"scope": "mine", "name": "Keep"},
                                               {"scope": "admin", "name": "abap-review"}])
    r = await client.put(f"{API}/my/skills/{keep['id']}",
                         json={"name": "Kept", "content": "c"})
    assert r.status_code == 200
    tone = next(s for s in (await client.get(f"{API}/my/skills")).json() if s["name"] == "Tone")
    assert (await client.delete(f"{API}/my/skills/{tone['id']}")).status_code == 204
    got = (await client.get(f"{API}/my/agents/{agent['id']}")).json()
    assert got["skills"] == [{"scope": "mine", "name": "Kept"},
                             {"scope": "admin", "name": "abap-review"}]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
async def test_export_bytes_and_selection(client):
    await admin_agent("btp-admin")
    await create_skill(client, "Tone", description="Short")
    await create_skill(client, "Other")
    agent = await create_agent(client, name="Writer", consults=["btp-admin"],
                               skills=[{"scope": "mine", "name": "Tone"}])
    await create_agent(client, name="Second")
    r = await client.get(f"{API}/my/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "private, no-store"
    disposition = r.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="webchat-bundle-')
    assert disposition.endswith('.json"')
    bundle = r.json()
    assert bundle["format"] == "webchat-bundle/1" and bundle["exported_at"].endswith("Z")
    assert [a["name"] for a in bundle["agents"]] == ["Second", "Writer"]
    assert bundle["agents"][1] == {"name": "Writer", "description": "",
                                   "instructions": "Be helpful.",
                                   "skills": [{"scope": "mine", "name": "Tone"}],
                                   "consults": ["btp-admin"], "model": None, "deep": False}
    assert [s["name"] for s in bundle["skills"]] == ["Other", "Tone"]
    assert "id" not in json.dumps(bundle["agents"]) and ALICE not in r.text

    # A selected agent takes the own skills it attaches with it.
    sel = (await client.get(f"{API}/my/export", params={"agents": agent["id"]})).json()
    assert [a["name"] for a in sel["agents"]] == ["Writer"]
    assert [s["name"] for s in sel["skills"]] == ["Tone"]
    r = await client.get(f"{API}/my/export", params={"agents": "x" * 37})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
def bundle(agents=(), skills=(), **extra) -> dict[str, Any]:
    return {"format": "webchat-bundle/1", "agents": list(agents), "skills": list(skills),
            **extra}


async def post_import(client, data: dict | bytes, rule: str | None = None, *,
                      as_json: bool = False, user: str = ALICE):
    raw = data if isinstance(data, bytes) else json.dumps(data).encode()
    headers = {"x-test-user": user}
    if as_json:
        params = {"on_conflict": rule} if rule else {}
        return await client.post(f"{API}/my/import", content=raw, params=params,
                                 headers={**headers, "content-type": "application/json"})
    form = {"on_conflict": (None, rule)} if rule else {}
    return await client.post(f"{API}/my/import", headers=headers,
                             files={**form, "file": ("b.json", raw, "application/json")})


async def test_import_creates_and_resolves_references(client):
    await admin_agent("btp-admin")
    await admin_skill("abap-review")
    data = bundle(
        agents=[{"name": "Writer", "description": "d", "instructions": "Write.",
                 "skills": [{"scope": "mine", "name": "tone"},
                            {"scope": "admin", "name": "abap-review"},
                            {"scope": "admin", "name": "elsewhere-skill"}],
                 "consults": ["btp-admin", "elsewhere-agent"], "model": "gpt-elsewhere"}],
        skills=[{"name": "Tone", "description": "", "content": "Be brief."}],
        unknown_top_level="ignored")
    r = await post_import(client, data)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["agents"] == {"created": 1, "replaced": 0, "skipped": 0, "renamed": []}
    assert out["skills"] == {"created": 1, "replaced": 0, "skipped": 0, "renamed": []}
    assert out["errors"] == []
    assert out["dropped"] == [
        {"item": "agent:Writer", "field": "skills", "name": "elsewhere-skill"},
        {"item": "agent:Writer", "field": "consults", "name": "elsewhere-agent"},
        {"item": "agent:Writer", "field": "model", "name": None},
    ]
    agent = (await client.get(f"{API}/my/agents")).json()[0]
    assert agent["skills"] == [{"scope": "mine", "name": "Tone"},
                               {"scope": "admin", "name": "abap-review"}]
    assert agent["consults"] == ["btp-admin"] and agent["model"] is None


async def test_import_as_json_body(client):
    r = await post_import(client, bundle(skills=[{"name": "Tone", "content": "x"}]),
                          as_json=True)
    assert r.status_code == 200 and r.json()["skills"]["created"] == 1


@pytest.mark.parametrize("rule", ["rename", "replace", "skip", None])
async def test_import_conflict_rules(client, rule):
    await create_skill(client, "Tone", content="mine")
    agent = await create_agent(client, name="Writer", instructions="old",
                               skills=[{"scope": "mine", "name": "Tone"}])
    data = bundle(
        agents=[{"name": "writer", "instructions": "new",
                 "skills": [{"scope": "mine", "name": "Tone"}]}],
        skills=[{"name": "TONE", "content": "theirs"}])
    out = (await post_import(client, data, rule)).json()
    agents = (await client.get(f"{API}/my/agents")).json()
    skills = (await client.get(f"{API}/my/skills")).json()
    if rule == "skip":
        assert out["agents"]["skipped"] == 1 and out["skills"]["skipped"] == 1
        assert [a["instructions"] for a in agents] == ["old"]
        assert [s["content"] for s in skills] == ["mine"]
    elif rule == "replace":
        assert out["agents"]["replaced"] == 1 and out["skills"]["replaced"] == 1
        assert [(a["id"], a["name"], a["instructions"]) for a in agents] == [
            (agent["id"], "writer", "new")]
        assert [(s["name"], s["content"]) for s in skills] == [("TONE", "theirs")]
    else:  # rename, the default
        assert out["agents"]["renamed"] == [{"from": "writer", "to": "writer (2)"}]
        assert out["skills"]["renamed"] == [{"from": "TONE", "to": "TONE (2)"}]
        assert out["agents"]["created"] == 1 and out["skills"]["created"] == 1
        by_name = {a["name"]: a for a in agents}
        # The imported agent follows the renamed skill of its own bundle.
        assert by_name["writer (2)"]["skills"] == [{"scope": "mine", "name": "TONE (2)"}]
        assert by_name["Writer"]["instructions"] == "old"
        # A renamed name is itself a valid name: it can be saved again.
        r = await client.put(f"{API}/my/agents/{by_name['writer (2)']['id']}",
                             json=agent_body("writer (2)"))
        assert r.status_code == 200


def test_rename_keeps_within_the_name_length():
    long = "a" * 64
    assert my_models.renamed(long, {long}) == "a" * 60 + " (2)"
    assert my_models.renamed("x", {"x", "x (2)"}) == "x (3)"


async def test_import_reports_item_errors_and_writes_the_rest(client):
    data = bundle(
        agents=[{"name": "Good", "instructions": "ok"},
                {"name": f"<b>{SECRET}</b>" + "y" * 80, "instructions": "ok"},
                {"name": "Bad keys", "instructions": "ok", SECRET: 1},
                {"name": "Empty", "instructions": ""},
                "not an object",
                {"name": "good", "instructions": "dup"}],
        skills=[{"name": "s", "content": 7}])
    r = await post_import(client, data)
    assert r.status_code == 200
    out = r.json()
    assert out["agents"]["created"] == 1
    assert [a["name"] for a in (await client.get(f"{API}/my/agents")).json()] == ["Good"]
    errors = out["errors"]
    assert {"item": "skill:s", "field": "content", "rule": "wrong type"} in errors
    assert {"item": "agent:Bad keys", "field": "<unknown field>", "rule": "unknown field"} in errors
    assert {"item": "agent:Empty", "field": "instructions",
            "rule": "too short or empty"} in errors
    assert {"item": "agent:#5", "field": "item", "rule": "not an object"} in errors
    assert {"item": "agent:good", "field": "name", "rule": "duplicate name in the bundle"} in errors
    long_item = next(e for e in errors if e["field"] == "name" and e["rule"] == "too long"
                     or e["rule"] == "not a valid name")
    # The item's own name is shown cut to 64 characters, nothing else of the file.
    assert len(long_item["item"]) <= len("agent:") + 64
    text = json.dumps(out)
    assert text.count(SECRET) <= 1  # only inside the item's own (cut) name
    assert SECRET not in json.dumps([e for e in errors if e is not long_item])


@pytest.mark.parametrize("raw, status, detail", [
    (b"{not json", 422, my_routes.IMPORT_NOT_JSON),
    (b"[]", 422, my_routes.IMPORT_NOT_JSON),
    (json.dumps({"format": "other/1"}).encode(), 422, my_routes.IMPORT_FORMAT),
    (json.dumps({"format": "webchat-bundle/1", "agents": {}}).encode(), 422,
     my_routes.IMPORT_FORMAT),
    (json.dumps(bundle(skills=[{"name": f"s{i}", "content": "x"} for i in range(101)])).encode(),
     422, my_routes.IMPORT_TOO_MANY),
    (b"[" * 200_000, 422, my_routes.IMPORT_NOT_JSON),
])
async def test_bundle_level_refusals(client, raw, status, detail):
    r = await post_import(client, raw)
    assert (r.status_code, r.json()) == (status, {"detail": detail})


async def test_bundle_caps_and_transport(client):
    big = json.dumps(bundle(skills=[{"name": "s", "content": "x" * (1024 * 1024)}])).encode()
    r = await post_import(client, big)
    assert r.status_code == 413
    r = await post_import(client, big, as_json=True)
    assert r.status_code == 413
    r = await client.post(f"{API}/my/import", content=b"x", headers={"content-type": "text/plain"})
    assert r.status_code == 415
    r = await client.post(f"{API}/my/import", files={"on_conflict": (None, "rename")})
    assert r.status_code == 422 and r.json() == {"detail": my_routes.IMPORT_NO_FILE}
    r = await post_import(client, bundle(), "merge")
    assert r.status_code == 422 and r.json() == {"detail": my_routes.IMPORT_CONFLICT_RULE}
    r = await post_import(client, bundle(), "merge", as_json=True)
    assert r.status_code == 422 and "merge" not in r.text


async def test_import_respects_the_per_user_limit(client, monkeypatch):
    await create_skill(client, "existing")
    monkeypatch.setattr(my_models, "MAX_SKILLS", 2)
    data = bundle(skills=[{"name": "a", "content": "x"}, {"name": "b", "content": "x"}])
    out = (await post_import(client, data)).json()
    assert out["skills"]["created"] == 1
    assert out["errors"] == [{"item": "skill:b", "field": "name", "rule": my_models.RULE_LIMIT}]


async def test_export_then_import_round_trip(client):
    await admin_agent("btp-admin")
    await create_skill(client, "Tone")
    await create_agent(client, name="Writer", consults=["btp-admin"],
                       skills=[{"scope": "mine", "name": "Tone"}], model="model-b")
    raw = (await client.get(f"{API}/my/export")).content
    out = (await post_import(client, raw, user=BOB)).json()
    assert out["errors"] == [] and out["dropped"] == []
    bob = (await client.get(f"{API}/my/agents", headers={"x-test-user": BOB})).json()
    assert [(a["name"], a["skills"], a["consults"], a["model"]) for a in bob] == [
        ("Writer", [{"scope": "mine", "name": "Tone"}], ["btp-admin"], "model-b")]
