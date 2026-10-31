"""``builtin:git`` as a registered built-in: the factory table, the storage
cleaner, the admin gate (its 422 texts), one entry per agent, the refusal of
``user_context`` and of other auth modes, and the entry in a real registry
build.

Run:  python -m pytest tests/test_git_registration.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ.pop("MCP_URL_ALLOWLIST", None)
for _var in ("DESTINATION_CLIENT_ID", "DESTINATION_CLIENT_SECRET",
             "DESTINATION_URI", "DESTINATION_TOKEN_URL", "DESTINATION_UAA_URL"):
    os.environ.pop(_var, None)

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

URL = "builtin:git"
SECRET = "Zx9secret"
BASE = {"destination": "GITHUB", "provider": "github", "repositories": ["acme/web"]}
FULL = {"destination": "GITHUB", "provider": "github",
        "repositories": ["acme/web", "acme/api"], "base_branch": "develop",
        "branch_prefix": "bot/", "allow_push": True, "allow_pull_request": True}
READ_TOOLS = {"list_repositories", "list_files", "read_repository_file", "list_branches",
              "get_pull_request", "get_pipeline_status"}
WRITE_TOOLS = {"create_branch", "commit_files", "open_pull_request"}
ONE_ENTRY = "an agent may have at most one builtin:git entry"
NOTES = {"url": "builtin:sapnotes", "auth_mode": "none"}


def _server(block: dict[str, Any], url: str = URL) -> dict[str, Any]:
    return {"url": url, "auth_mode": "destination", "oauth": block}


# --- factory ------------------------------------------------------------------

def test_git_is_a_known_builtin():
    from agents.builtins import BUILTIN_URLS, is_builtin_url

    assert URL in BUILTIN_URLS and is_builtin_url(" BUILTIN:Git ")


def test_the_docstring_table_lists_the_builtin():
    import agents.builtins as builtins

    assert "builtin:git        --        --        app" in builtins.__doc__


def test_the_factory_refuses_a_bad_block_and_names_the_server_without_a_binding():
    from agents.builtins import build_builtin_toolset
    from agents.destination import DestinationError

    with pytest.raises(DestinationError, match=f"{URL}: no destination service binding"):
        build_builtin_toolset(URL, FULL, "destination")
    with pytest.raises(ValueError, match="^oauth.user_context:"):
        build_builtin_toolset(URL, {**BASE, "user_context": True}, "destination")
    for mode in ("app_only", "oauth2", "jwt", "none"):
        with pytest.raises(ValueError, match="requires auth_mode=destination"):
            build_builtin_toolset(URL, BASE, mode)
    with pytest.raises(ValueError, match="^oauth.repositories:") as info:
        build_builtin_toolset(URL, {**BASE, "repositories": [SECRET]}, "destination")
    assert SECRET not in str(info.value)


def test_the_factory_hands_the_agent_name_to_the_toolset(monkeypatch):
    import agents.builtins as builtins

    seen: dict[str, Any] = {}

    def fake(oauth, **kw):
        seen.update(kw)
        return object()

    monkeypatch.setitem(builtins._FACTORIES, URL, fake)
    builtins.build_builtin_toolset(URL, BASE, "destination", context={"agent_name": "cap-dev"})
    assert seen == {"server_key": URL, "auth_mode": "destination", "agent_name": "cap-dev"}


# --- storage ------------------------------------------------------------------

@pytest.mark.parametrize("block", [BASE, FULL])
def test_storage_keeps_exactly_the_checked_block(block):
    from agents.db import _clean_oauth

    assert _clean_oauth(dict(block), "destination", None, url="Builtin:Git/") == block


def test_storage_stores_no_default_switch_and_no_echoed_key():
    from agents.db import _clean_oauth

    stored = _clean_oauth({**BASE, "allow_push": False, "allow_pull_request": False,
                           "has_client_secret": False, "user_context": False},
                          "destination", None, url=URL)
    assert stored == BASE


@pytest.mark.parametrize("change, field", [
    ({"user_context": True}, "oauth.user_context"),
    ({"allow_push": "true"}, "oauth.allow_push"),
    ({"provider": "svn"}, "oauth.provider"),
    ({"repositories": [f"acme/{SECRET} "]}, "oauth.repositories"),
    ({"client_secret": SECRET}, "oauth: a builtin:git entry holds only"),
    ({SECRET: 1}, "oauth: a builtin:git entry holds only"),
])
def test_storage_refuses_by_itself_and_names_no_value(change, field):
    from agents.db import _clean_oauth

    with pytest.raises(ValueError) as info:
        _clean_oauth({**BASE, **change}, "destination", None, url=URL)
    assert str(info.value).startswith(field) and SECRET not in str(info.value)


@pytest.mark.parametrize("mode", ["app_only", "oauth2", "jwt", "none"])
def test_storage_refuses_every_other_auth_mode(mode):
    from agents.db import prepare_servers

    with pytest.raises(ValueError, match="requires auth_mode=destination"):
        prepare_servers([{"url": URL, "auth_mode": mode, "oauth": dict(BASE)}], None)


@pytest.mark.parametrize("spelling", [URL, "builtin:git/", "Builtin:Git", " BUILTIN:GIT "])
def test_storage_stores_the_url_in_the_canonical_spelling(spelling):
    from agents.db import prepare_servers

    primary, _, oauth_json = prepare_servers([_server(BASE, spelling)], None)
    assert primary["url"] == URL and json.loads(oauth_json) == BASE


TWICE = [(URL, URL), (URL, "builtin:git/"), ("Builtin:Git", URL)]


def _two(first: str, second: str) -> list[dict[str, Any]]:
    return [_server(dict(BASE), first),
            _server({**FULL, "repositories": ["zx9/other"]}, second)]


@pytest.mark.parametrize("first, second", TWICE)
def test_storage_refuses_a_second_git_entry_in_any_spelling(first, second):
    from agents.db import prepare_servers

    with pytest.raises(ValueError) as refused:
        prepare_servers(_two(first, second), None)
    assert str(refused.value).startswith(ONE_ENTRY) and "zx9" not in str(refused.value)
    with pytest.raises(ValueError, match="^" + ONE_ENTRY):
        prepare_servers([_two(first, second)[0], NOTES, _two(first, second)[1]], None)


@pytest.mark.parametrize("first, second", TWICE)
def test_the_gate_refuses_a_second_git_entry_in_any_spelling(first, second):
    from agents.admin import AgentPayload

    with pytest.raises(ValidationError) as refused:
        AgentPayload.model_validate({"name": "git-two", "description": "d",
                                     "instructions": "i", "mcp_servers": _two(first, second)})
    text = json.dumps(refused.value.errors(include_input=False, include_context=False,
                                           include_url=False))
    assert ONE_ENTRY in text and "zx9" not in text


def test_one_git_entry_next_to_a_bitbucket_entry_and_others_is_stored():
    from agents.db import prepare_servers

    bitbucket = {"url": "builtin:bitbucket", "auth_mode": "destination",
                 "oauth": {"destination": "BB", "workspace": "acme-ws",
                           "repositories": ["svc-a"]}}
    _, extras, _ = prepare_servers([NOTES, _server(FULL), bitbucket], None)
    assert [e["url"] for e in extras] == [URL, "builtin:bitbucket"]
    assert extras[0]["oauth"] == FULL


# --- the admin gate -----------------------------------------------------------

def _payload(oauth: Any, mode: str = "destination", url: str = URL):
    from agents.admin import McpServerPayload

    return McpServerPayload.model_validate({"url": url, "auth_mode": mode, "oauth": oauth})


def _refusal(oauth: Any, mode: str = "destination", url: str = URL) -> str:
    with pytest.raises(ValidationError) as info:
        _payload(oauth, mode, url)
    msgs = [e["msg"] for e in info.value.errors()]
    assert all(SECRET not in m for m in msgs)
    return " | ".join(msgs)


@pytest.mark.parametrize("block", [BASE, FULL])
def test_the_gate_accepts_an_entry_and_hands_storage_the_same_block(block):
    assert _payload(dict(block)).oauth.to_config() == block


@pytest.mark.parametrize("change, said", [
    ({"provider": None}, "oauth.provider: required, one of github, bitbucket, gitlab"),
    ({"provider": "GitHub"}, "oauth.provider: required, one of github, bitbucket, gitlab"),
    ({"destination": f" {SECRET}"}, "oauth.destination: required, a destination name"),
    ({"destination": None}, "oauth.destination: required, a destination name"),
    ({"repositories": []}, "oauth.repositories: required, a list of 1 to 50"),
    ({"repositories": [f"{SECRET}"]}, "oauth.repositories: each entry is one repository path"),
    ({"repositories": ["acme/web", "acme/web"]}, "oauth.repositories: a repository is listed"),
    ({"base_branch": "a..b"}, "oauth.base_branch: a branch name"),
    ({"base_branch": "agent/x"}, "oauth.base_branch: must not carry the branch prefix"),
    ({"branch_prefix": "agent"}, "oauth.branch_prefix: 1-40 letters"),
    ({"allow_push": "true"}, "oauth.allow_push: must be the JSON boolean"),
    ({"allow_pull_request": 1}, "oauth.allow_pull_request: must be the JSON boolean"),
    ({"user_context": True}, "oauth.user_context: builtin:git has no signed-in user"),
    ({SECRET: "x"}, "oauth: a builtin:git entry holds only"),
    ({"client_secret": SECRET}, "oauth: a builtin:git entry holds only"),
    ({"workspace": "acme"}, "oauth: a builtin:git entry holds only"),
])
def test_the_gate_refuses_and_names_the_field_never_the_value(change, said):
    assert said in _refusal({**BASE, **change})


@pytest.mark.parametrize("oauth", [None, [], "x"])
def test_the_gate_refuses_an_entry_without_a_config_object(oauth):
    assert "builtin:git entry needs a config object" in _refusal(oauth)


@pytest.mark.parametrize("mode", ["app_only", "oauth2", "jwt", "none", "session"])
def test_the_gate_refuses_every_other_auth_mode(mode):
    assert "builtin:git requires auth_mode=destination" in _refusal(dict(BASE), mode)


@pytest.mark.parametrize("url, block", [
    ("builtin:jira", {"destination": "J", "project": "P", "provider": "github"}),
    ("builtin:jira", {"destination": "J", "project": "P", "allow_push": True}),
    ("builtin:slack", {"destination": "S", "base_branch": "main"}),
    ("builtin:slack", {"destination": "S", "branch_prefix": "agent/"}),
])
def test_the_git_keys_belong_to_a_git_entry_only(url, block):
    assert "belong to a builtin:git entry only" in _refusal(block, url=url)


def test_a_bitbucket_entry_with_a_git_switch_is_refused_by_its_own_reading():
    assert "builtin:bitbucket entry holds only" in _refusal(
        {"destination": "B", "workspace": "acme", "allow_pull_request": True},
        url="builtin:bitbucket")


def test_repositories_still_belong_to_bitbucket_or_git_only():
    assert "belong to a builtin:bitbucket entry only" in _refusal(
        {"destination": "S", "repositories": ["a/b"]}, url="builtin:slack")


# --- the API ------------------------------------------------------------------

async def _wipe() -> None:
    from sqlalchemy import delete

    from agents.db import AgentConfig, SessionLocal, SkillConfig

    async with SessionLocal() as session:
        await session.execute(delete(AgentConfig))
        await session.execute(delete(SkillConfig))
        await session.commit()


@pytest.fixture
async def client():
    import app as app_module
    from agents.db import init_db

    await init_db()
    await _wipe()
    async with AsyncClient(transport=ASGITransport(app=app_module.app),
                           base_url="http://test") as c:
        yield c


def _agent(name: str, servers: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": name, "description": "d", "instructions": "i", "mcp_servers": servers}


async def _stored(name: str):
    from agents.db import SessionLocal, get_agent_by_name

    async with SessionLocal() as session:
        row = await get_agent_by_name(session, name)
        return row and (row.id, row.mcp_servers)


async def test_the_api_stores_the_entry_and_a_refused_save_answers_422_without_the_input(
    client, caplog
):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.INFO, logger="aiosqlite")
    r = await client.post("/admin/api/agents", json=_agent("git-dev", [_server(FULL)]))
    assert r.status_code in (200, 201), r.text
    agent_id, servers = await _stored("git-dev")
    assert servers == [_server(FULL)]
    listed = await client.get("/admin/api/agents")
    (answered,) = [a for a in listed.json() if a["name"] == "git-dev"]
    shown = answered["mcp_servers"][0]["oauth"]
    assert shown == {**FULL, "has_client_secret": False}

    for change, said in [({"provider": SECRET}, "oauth.provider"),
                         ({"allow_push": "true"}, "oauth.allow_push"),
                         ({"repositories": [SECRET]}, "oauth.repositories"),
                         ({SECRET: SECRET}, "holds only")]:
        for send in (client.post("/admin/api/agents",
                                 json=_agent("git-refused", [_server({**BASE, **change})])),
                     client.put(f"/admin/api/agents/{agent_id}",
                                json=_agent("git-dev", [_server({**FULL, **change})]))):
            r = await send
            assert r.status_code == 422, r.text
            assert said in r.text and SECRET not in r.text
            for item in r.json()["detail"]:
                assert set(item) == {"loc", "msg", "type"}, item
    r = await client.post("/admin/api/agents", json=_agent(
        "git-refused", [{"url": URL, "auth_mode": "app_only", "oauth": dict(BASE)}]))
    assert r.status_code == 422 and "requires auth_mode=destination" in r.text
    r = await client.post("/admin/api/agents", json=_agent("git-two", _two(URL, "builtin:git/")))
    assert r.status_code == 422 and ONE_ENTRY in r.text and "zx9" not in r.text
    assert SECRET not in caplog.text
    assert await _stored("git-refused") is None and await _stored("git-two") is None

    # An edit posts back what it read (the echoed key included): nothing changes.
    r = await client.put(f"/admin/api/agents/{agent_id}",
                         json=_agent("git-dev", [_server(shown)]))
    assert r.status_code == 200, r.text
    assert await _stored("git-dev") == (agent_id, [_server(FULL)])
    # An edit that drops the switches drops them.
    r = await client.put(f"/admin/api/agents/{agent_id}", json=_agent("git-dev", [_server(BASE)]))
    assert r.status_code == 200, r.text
    assert await _stored("git-dev") == (agent_id, [_server(BASE)])


@pytest.mark.parametrize("policy", ["change", "diagnose"])
def test_the_tools_are_denied_in_an_abap_assistant_session(policy):
    from agents.ide.readonly import POLICIES, READONLY_POLICY, check_call

    for tool in sorted(READ_TOOLS | WRITE_TOOLS):
        for name in (tool, "git_" + tool, "git_0_" + tool):
            assert name not in READONLY_POLICY
            assert all(name not in rules for rules in POLICIES.values())
            assert check_call(name, {"repository": "acme/web"}, policy) is not None


# --- a real registry build ----------------------------------------------------

@pytest.fixture
def binding(monkeypatch):
    monkeypatch.setenv("DESTINATION_CLIENT_ID", "cid")
    monkeypatch.setenv("DESTINATION_CLIENT_SECRET", "placeholder")
    monkeypatch.setenv("DESTINATION_URI", "https://destination.example.com")
    monkeypatch.setenv("DESTINATION_TOKEN_URL", "https://login.example.com/oauth/token")


async def _build(agents: dict[str, list[dict[str, Any]]], raw: dict[str, Any] | None = None):
    from pydantic_ai.models.test import TestModel
    from sqlalchemy import select

    from agents import registry as registry_module
    from agents.db import AgentConfig, SessionLocal, init_db, upsert_agent

    await init_db()
    await _wipe()
    async with SessionLocal() as s:
        for name, servers in agents.items():
            await upsert_agent(s, name=name, description="d", instructions="i",
                               mcp_servers=servers)
        await s.commit()
        for name, servers in (raw or {}).items():
            row = (await s.execute(
                select(AgentConfig).where(AgentConfig.name == name))).scalar_one()
            row.extra_servers_json = json.dumps(servers)
        await s.commit()
    real_get_model = registry_module.get_model
    registry_module.get_model = lambda *a, **k: TestModel()
    try:
        return await registry_module.build_orchestrator()
    finally:
        registry_module.get_model = real_get_model


async def _tool_names(agent) -> set[str]:
    from pydantic_ai import RunContext
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.usage import RunUsage

    from agents.ide.readonly import ReadOnlyGuard

    ctx = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    names: set[str] = set()
    for toolset in getattr(agent, "_user_toolsets", ()) or ():
        if isinstance(toolset, ReadOnlyGuard):
            names |= set(await toolset.get_tools(ctx))
    return names


@pytest.mark.usefixtures("real_agents_and_mcp", "binding")
async def test_a_registry_build_gives_the_agent_the_tools_its_entry_allows():
    build = await _build({
        "reader": [_server(BASE)],
        "pusher": [_server({**BASE, "allow_push": True})],
        "full": [_server(FULL)],
    })
    assert await _tool_names(build.specialists["reader"]) == READ_TOOLS
    assert await _tool_names(build.specialists["pusher"]) == READ_TOOLS | {
        "create_branch", "commit_files"}
    assert await _tool_names(build.specialists["full"]) == READ_TOOLS | WRITE_TOOLS


@pytest.mark.usefixtures("real_agents_and_mcp", "binding")
async def test_with_a_second_server_the_tools_carry_the_git_prefix():
    build = await _build({"dev": [_server(BASE), NOTES]})
    names = await _tool_names(build.specialists["dev"])
    assert {"git_" + n for n in READ_TOOLS} <= names
    assert not (READ_TOOLS | WRITE_TOOLS) & names


@pytest.mark.usefixtures("real_agents_and_mcp", "binding")
async def test_a_row_written_by_hand_with_two_git_entries_gets_no_git_tools(caplog):
    caplog.set_level(logging.WARNING)
    build = await _build({"dev": [NOTES]}, raw={"dev": [_server(BASE), _server(FULL)]})
    names = await _tool_names(build.specialists["dev"])
    assert not any(n.startswith("git_") for n in names)
    assert "more than one builtin:git entry" in caplog.text
    assert "acme" not in caplog.text
