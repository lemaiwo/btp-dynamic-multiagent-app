"""``builtin:bitbucket`` as a registered built-in: the factory table, storage,
and the entry in a real registry build.

The entry decides whether an agent may comment on and approve pull requests,
so three things are pinned here on top of "it is accepted": storage refuses by
itself (it is reached without the admin payload by an import script and by a
direct ``upsert_agent``), a refusal never repeats what was typed, and the row
holds exactly the block that was checked, never a repaired or converted one.

Run:  python -m pytest tests/test_bitbucket_registration.py
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
import time
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

URL = "builtin:bitbucket"
BASE = {"destination": "BITBUCKET", "workspace": "acme-ws"}
FULL = {**BASE, "repositories": ["svc-a", "svc-b"], "branch": "main",
        "allow_comment": True, "allow_approve": True, "require_green_builds": False}
# Never part of an answer or a log line: what a refused entry carried.
SECRET = "S3cr3t Zx9!tok"

READ_TOOLS = {"list_pull_requests", "get_pull_request", "get_diff", "get_file"}
WRITE_TOOLS = {"add_inline_comment", "submit_review", "complete_approval"}


# --- factory ------------------------------------------------------------------

def test_bitbucket_is_a_known_builtin():
    from agents.builtins import BUILTIN_URLS, is_builtin_url

    assert URL in BUILTIN_URLS and is_builtin_url("BUILTIN:Bitbucket")


def test_the_factory_names_the_server_without_a_binding_and_refuses_a_bad_block():
    from agents.builtins import build_builtin_toolset
    from agents.destination import DestinationError

    with pytest.raises(DestinationError, match=f"{URL}: no destination service binding"):
        build_builtin_toolset(URL, BASE, "destination")
    with pytest.raises(ValueError, match="^oauth.workspace:"):
        build_builtin_toolset(URL, {"destination": "BITBUCKET"}, "destination")
    with pytest.raises(ValueError, match="^oauth.user_context:"):
        build_builtin_toolset(URL, {**BASE, "user_context": True}, "destination")
    with pytest.raises(ValueError, match="requires auth_mode=destination"):
        build_builtin_toolset(URL, BASE, "app_only")


def test_the_docstring_table_lists_the_builtin():
    import agents.builtins as builtins

    assert "builtin:bitbucket" in builtins.__doc__


# --- storage ------------------------------------------------------------------

@pytest.mark.parametrize("block", [BASE, FULL])
def test_storage_keeps_exactly_the_checked_block(block):
    from agents.db import _clean_oauth

    stored = _clean_oauth(copy.deepcopy(block), "destination", None, url=URL)
    assert stored == block and list(stored) == [k for k in (
        "destination", "workspace", "repositories", "branch", "allow_comment",
        "allow_approve", "require_green_builds") if k in block]


@pytest.mark.parametrize("extra", [{"allow_comment": False}, {"allow_approve": False},
                                   {"require_green_builds": True}, {"user_context": False},
                                   {"has_client_secret": False}])
def test_storage_stores_no_default_and_no_echoed_key(extra):
    from agents.db import _clean_oauth

    assert _clean_oauth({**BASE, **extra}, "destination", None, url=URL) == BASE


@pytest.mark.parametrize("change, field", [
    ({"workspace": "acme/" + SECRET}, "oauth.workspace"),
    ({"workspace": " acme-ws"}, "oauth.workspace"),
    ({"repositories": ["svc-a", SECRET]}, "oauth.repositories"),
    ({"branch": 'main" OR "' + SECRET}, "oauth.branch"),
    ({"allow_comment": "true"}, "oauth.allow_comment"),
    ({"allow_approve": "true", "allow_comment": True}, "oauth.allow_approve"),
    ({"allow_approve": True}, "oauth.allow_approve"),
    ({"user_context": True}, "oauth.user_context"),
    ({"client_secret": SECRET}, "oauth"),
    ({"project": "ABC"}, "oauth"),
    ({SECRET: True}, "oauth"),
])
def test_storage_refuses_by_itself_and_names_no_value(change, field):
    """The last gate, reached without the payload by a script or a direct
    ``upsert_agent``: nothing is repaired, dropped or converted."""
    from agents.db import _clean_oauth

    with pytest.raises(ValueError) as refused:
        _clean_oauth({**BASE, **change}, "destination", None, url=URL)
    text = str(refused.value)
    assert text.startswith(field + ":") and SECRET not in text and "Zx9" not in text


@pytest.mark.parametrize("block", [None, "", [], "destination=BITBUCKET"])
def test_storage_refuses_an_entry_without_a_config_object(block):
    from agents.db import _clean_oauth

    with pytest.raises(ValueError, match="^oauth:"):
        _clean_oauth(block, "destination", None, url=URL)


def test_storage_never_fills_a_block_from_the_row_it_replaces():
    """The stored block of the same URL is no fallback: an edit that drops the
    switches drops them, and one that sends nothing is refused."""
    from agents.db import _clean_oauth

    assert _clean_oauth(dict(BASE), "destination", dict(FULL), url=URL) == BASE
    with pytest.raises(ValueError, match="^oauth:"):
        _clean_oauth(None, "destination", dict(FULL), url=URL)


@pytest.mark.parametrize("mode", ["jwt", "none", "oauth2", "app_only", "session"])
def test_storage_refuses_every_other_auth_mode(mode):
    from agents.db import _clean_oauth

    with pytest.raises(ValueError, match="^builtin:bitbucket requires auth_mode=destination"):
        _clean_oauth(dict(FULL), mode, None, url=URL)


@pytest.mark.parametrize("spelling", [
    "builtin:bitbucket/", "Builtin:Bitbucket", " BUILTIN:BITBUCKET// "])
def test_storage_stores_the_url_in_the_spelling_the_registry_knows(spelling):
    from agents.builtins import is_builtin_url
    from agents.db import prepare_servers

    primary, extras, _ = prepare_servers(
        [{"url": "https://mcp.example.com/mcp", "auth_mode": "jwt"},
         {"url": spelling, "auth_mode": "destination", "oauth": FULL}], None)
    (entry,) = extras
    assert entry == {"url": URL, "auth_mode": "destination", "oauth": FULL}
    assert is_builtin_url(entry["url"])


def test_a_respelled_entry_cannot_slip_past_the_cleaner_as_a_jira_shaped_block():
    """Without the canonical spelling a trailing slash would send the block to
    the generic destination cleaner, which stores ``allow_comment`` by ``bool()``."""
    from agents.db import prepare_servers

    with pytest.raises(ValueError, match="^oauth.allow_comment:"):
        prepare_servers([{"url": "builtin:bitbucket/", "auth_mode": "destination",
                          "oauth": {**BASE, "allow_comment": "yes"}}], None)


async def test_a_direct_upsert_stores_the_checked_block_or_nothing():
    """`upsert_agent` without the admin payload, as an import script calls it."""
    from agents.db import SessionLocal, init_db, list_agents, upsert_agent

    await init_db()
    await _wipe()
    async with SessionLocal() as s:
        for change in ({"allow_comment": "true"}, {"user_context": True},
                       {"workspace": "Acme " + SECRET}, {"client_secret": SECRET}):
            with pytest.raises(ValueError) as refused:
                await upsert_agent(
                    s, name="reviewer", description="d", instructions="i",
                    mcp_servers=[{"url": URL, "auth_mode": "destination",
                                  "oauth": {**BASE, **change}}])
            assert str(refused.value).startswith("oauth") and SECRET not in str(refused.value)
        await s.rollback()
        assert await list_agents(s) == []
        await upsert_agent(
            s, name="reviewer", description="d", instructions="i",
            mcp_servers=[{"url": "Builtin:Bitbucket/", "auth_mode": "destination",
                          "oauth": {**FULL, "user_context": False}}],
            expose_chat=False)          # an approving entry: never chat (M3)
        await s.commit()
        (row,) = await list_agents(s)
        assert row.mcp_servers == [{"url": URL, "auth_mode": "destination", "oauth": FULL}]


# --- the entry in a real registry build ---------------------------------------

async def _wipe() -> None:
    from sqlalchemy import delete

    from agents.db import AgentConfig, SessionLocal, SkillConfig

    async with SessionLocal() as session:
        await session.execute(delete(AgentConfig))
        await session.execute(delete(SkillConfig))
        await session.commit()


async def _build(agents: dict[str, list[dict[str, Any]]], raw: dict[str, Any] | None = None):
    """A registry build of ``agents`` (name -> servers). ``raw`` replaces the
    stored block of an agent's Bitbucket entry after the save, as a row
    written by an older version or by hand would hold it."""
    from pydantic_ai.models.test import TestModel
    from sqlalchemy import select

    from agents import registry as registry_module
    from agents.db import AgentConfig, SessionLocal, init_db, upsert_agent

    await init_db()
    await _wipe()
    async with SessionLocal() as s:
        for name, servers in agents.items():
            # Not exposed to chat: an entry that approves may not be (final
            # review M3, tests/test_bitbucket_reach.py).
            await upsert_agent(s, name=name, description="d", instructions="i",
                               mcp_servers=servers, expose_chat=False)
        await s.commit()
        for name, block in (raw or {}).items():
            row = (await s.execute(
                select(AgentConfig).where(AgentConfig.name == name))).scalar_one()
            assert row.auth_mode == "destination" and row.mcp_url == URL
            row.oauth_json = json.dumps(block)
        await s.commit()
    real_get_model = registry_module.get_model
    registry_module.get_model = lambda *a, **k: TestModel()
    try:
        return await registry_module.build_orchestrator()
    finally:
        registry_module.get_model = real_get_model


async def _tool_names(agent) -> set[str]:
    """The tools of the agent's servers, as a run lists them."""
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


def _server(block: dict[str, Any]) -> dict[str, Any]:
    return {"url": URL, "auth_mode": "destination", "oauth": block}


NOTES = {"url": "builtin:sapnotes", "auth_mode": "none"}


@pytest.fixture
def binding(monkeypatch):
    """A destination service binding by name: without one a destination
    entry is refused at build, whatever its block holds. Nothing is called."""
    monkeypatch.setenv("DESTINATION_CLIENT_ID", "cid")
    monkeypatch.setenv("DESTINATION_CLIENT_SECRET", "placeholder")
    monkeypatch.setenv("DESTINATION_URI", "https://destination.example.com")
    monkeypatch.setenv("DESTINATION_TOKEN_URL", "https://login.example.com/oauth/token")


@pytest.mark.usefixtures("real_agents_and_mcp", "binding")
async def test_a_registry_build_gives_the_agent_the_tools_its_entry_allows():
    build = await _build({
        "reader": [_server(BASE)],
        "commenter": [_server({**BASE, "allow_comment": True})],
        "approver": [_server(FULL)],
    })
    assert await _tool_names(build.specialists["reader"]) == READ_TOOLS
    assert await _tool_names(build.specialists["commenter"]) == READ_TOOLS | {
        "add_inline_comment", "submit_review"}
    assert await _tool_names(build.specialists["approver"]) == READ_TOOLS | WRITE_TOOLS


@pytest.mark.usefixtures("real_agents_and_mcp", "binding")
async def test_with_a_second_server_the_tools_carry_the_bitbucket_prefix():
    build = await _build({"reviewer": [_server(BASE), NOTES]})
    names = await _tool_names(build.specialists["reviewer"])
    assert {"bitbucket_" + n for n in READ_TOOLS} <= names
    assert not (READ_TOOLS | WRITE_TOOLS) & names
    assert not any(n.endswith(tuple(WRITE_TOOLS)) for n in names)
    assert any(n.startswith("sapnotes_") for n in names)


@pytest.mark.usefixtures("real_agents_and_mcp", "binding")
@pytest.mark.parametrize("change", [
    {"workspace": "Acme " + SECRET},
    {"allow_comment": "true"},
    {"allow_comment": True, "allow_approve": "true"},
    {"user_context": True},
    {SECRET: True},
])
async def test_a_stored_block_that_is_refused_costs_only_that_server(change, caplog):
    """A row no gate has seen (written by hand or by an older version) gives
    no tool at all: not the read tools, and never a write tool by ``bool()``."""
    caplog.set_level(logging.INFO)
    bad = {**BASE, **change}
    build = await _build(
        {"reviewer": [_server(BASE), NOTES], "broken": [_server(BASE)],
         "other": [_server(BASE)]},
        raw={"reviewer": bad, "broken": bad})
    # The agent keeps its other server and has no Bitbucket tool ...
    names = await _tool_names(build.specialists["reviewer"])
    assert names and not any(
        n.endswith(tuple(READ_TOOLS | WRITE_TOOLS)) for n in names)
    # ... an agent with nothing else is left out, and the others are built.
    assert "broken" not in build.specialists
    assert await _tool_names(build.specialists["other"]) == READ_TOOLS
    # The refusal is logged with its traceback: field and rule, never a value.
    said = "\n".join(caplog.handler.format(r) for r in caplog.records
                     if r.name.startswith("agents."))
    assert said.count("Failed to create MCP server builtin:bitbucket") == 2
    assert SECRET not in said and "Zx9" not in said


# --- admin gate ---------------------------------------------------------------

def _payload(mode: str = "destination", url: str = URL, **oauth):
    from agents.admin import McpServerPayload

    return McpServerPayload.model_validate({"url": url, "auth_mode": mode, "oauth": oauth})


def _refusal(block: dict, mode: str = "destination", url: str = URL) -> str:
    with pytest.raises(ValidationError) as refused:
        _payload(mode, url, **block)
    # As the 422 handler answers: loc, msg and type, never the input.
    return json.dumps(refused.value.errors(include_input=False, include_context=False,
                                           include_url=False))


@pytest.mark.parametrize("block", [BASE, FULL])
def test_the_gate_accepts_an_entry_and_hands_storage_the_same_block(block):
    from agents.db import _clean_oauth

    config = _payload(**block).oauth.to_config()
    assert config == block
    assert _clean_oauth(config, "destination", None, url=URL) == block


def test_the_gate_hands_on_require_green_builds_only_when_it_is_false():
    assert "require_green_builds" not in _payload(
        **BASE, require_green_builds=True).oauth.to_config()
    config = _payload(**BASE, require_green_builds=False).oauth.to_config()
    assert config["require_green_builds"] is False


@pytest.mark.parametrize("change, said", [
    ({"workspace": ""}, "oauth.workspace"),
    ({"workspace": "Acme " + SECRET}, "oauth.workspace"),
    ({"workspace": 7}, "oauth.workspace"),
    ({"repositories": []}, "oauth.repositories"),
    ({"repositories": ["svc-a", "../" + SECRET]}, "oauth.repositories"),
    ({"repositories": "svc-a"}, "oauth.repositories"),
    ({"branch": "a..b"}, "oauth.branch"),
    ({"allow_comment": "true"}, "oauth.allow_comment"),       # the lax bool of the payload
    ({"allow_comment": 1}, "oauth.allow_comment"),
    ({"allow_approve": True}, "oauth.allow_approve"),          # without allow_comment
    ({"allow_approve": True, "allow_comment": False}, "oauth.allow_approve"),
    ({"allow_approve": "true", "allow_comment": True}, "allow_approve"),
    ({"allow_approve": 1, "allow_comment": True}, "allow_approve"),
    ({"require_green_builds": "false"}, "require_green_builds"),
    ({"require_green_builds": 0}, "require_green_builds"),
    ({"user_context": True}, "oauth.user_context"),
    ({"project": "ABC"}, "holds only"),                        # another built-in's key
    ({"client_id": "cid", "client_secret": SECRET}, "holds only"),
    ({"allow_send": True}, "holds only"),
    ({"dcr": True}, "holds only"),
    ({SECRET: True}, "holds only"),                            # a key the payload would ignore
    ({"destination": ""}, "oauth.destination"),
    ({"destination": " BITBUCKET"}, "oauth.destination"),      # not repaired by a trim
])
def test_the_gate_refuses_and_names_the_field_never_the_value(change, said):
    text = _refusal({**BASE, **change})
    assert said in text, text
    assert SECRET not in text and "Zx9" not in text


@pytest.mark.parametrize("oauth", [None, "destination=BITBUCKET", []])
def test_the_gate_refuses_an_entry_without_a_config_object(oauth):
    from agents.admin import McpServerPayload

    with pytest.raises(ValidationError) as refused:
        McpServerPayload.model_validate(
            {"url": URL, "auth_mode": "destination", "oauth": oauth})
    assert "needs a config object" in str(refused.value.errors(include_input=False))
    with pytest.raises(ValidationError):
        McpServerPayload.model_validate({"url": URL, "auth_mode": "destination"})


@pytest.mark.parametrize("mode", ["jwt", "none", "oauth2", "app_only", "session"])
def test_the_gate_refuses_every_other_auth_mode(mode):
    assert "builtin:bitbucket requires auth_mode=destination" in _refusal(dict(BASE), mode)


def test_the_gate_refuses_an_entry_that_names_no_auth_mode():
    from agents.admin import McpServerPayload

    with pytest.raises(ValidationError, match="requires auth_mode=destination"):
        McpServerPayload.model_validate({"url": URL, "oauth": dict(BASE)})


@pytest.mark.parametrize("url, mode, block", [
    ("builtin:jira", "destination", {"destination": "JIRA", "workspace": "acme-ws"}),
    ("builtin:jira", "destination", {"destination": "JIRA", "allow_approve": True}),
    ("builtin:slack", "destination", {"destination": "SLACK", "repositories": ["svc-a"]}),
    ("builtin:slack", "destination", {"destination": "SLACK", "repositories": []}),
    ("builtin:jira", "destination", {"destination": "JIRA", "branch": "main"}),
    ("builtin:jira", "destination", {"destination": "JIRA", "require_green_builds": False}),
    ("builtin:jira", "destination", {"destination": "JIRA", "require_green_builds": True}),
    ("https://mcp.example.com/mcp", "destination", {"destination": "D", "allow_approve": True}),
])
def test_the_bitbucket_keys_belong_to_a_bitbucket_entry_only(url, mode, block):
    assert "belong to a builtin:bitbucket entry only" in _refusal(block, mode, url)


def test_the_other_builtins_are_accepted_as_before():
    assert _payload(url="builtin:jira", destination="JIRA", project="ABC",
                    allow_comment=True).oauth.to_config() == {
        "destination": "JIRA", "project": "ABC", "allow_comment": True}
    # A client that serialises every field as null still saves an Outlook entry.
    _payload("oauth2", "builtin:outlook", client_id="cid", uaa_url="https://uaa.example.com",
             workspace=None, repositories=None, branch=None, allow_approve=None,
             require_green_builds=None)


@pytest.mark.parametrize("spelling", ["builtin:bitbucket/", "Builtin:Bitbucket",
                                      " BUILTIN:BITBUCKET// "])
def test_the_gate_reads_a_respelled_url_as_the_builtin(spelling):
    assert "oauth.allow_comment" in _refusal({**BASE, "allow_comment": "true"}, url=spelling)


def test_the_gate_checks_a_block_that_arrives_as_a_payload_object():
    """A caller that builds the models itself: only the fields it set are the
    block, and they are held to the same rules."""
    from agents.admin import McpServerPayload, OAuthClientPayload

    ok = McpServerPayload(url=URL, auth_mode="destination", oauth=OAuthClientPayload(**FULL))
    assert ok.oauth.to_config() == FULL
    with pytest.raises(ValidationError, match="holds only"):
        McpServerPayload(url=URL, auth_mode="destination",
                         oauth=OAuthClientPayload(**BASE, project="ABC"))


# --- the API, credential health, the ABAP Assistant ---------------------------

@pytest.fixture
async def client():
    import app as app_module
    from agents.db import init_db

    await init_db()
    await _wipe()
    async with AsyncClient(transport=ASGITransport(app=app_module.app),
                           base_url="http://test") as c:
        yield c


def _agent(name: str, oauth: dict[str, Any], mode: str = "destination") -> dict[str, Any]:
    # Not exposed to chat: an agent whose entry approves may not be (final
    # review M3; the refusals of that rule are in tests/test_bitbucket_reach.py).
    return {"name": name, "description": "d", "instructions": "i", "expose_chat": False,
            "mcp_servers": [{"url": URL, "auth_mode": mode, "oauth": oauth}]}


async def _stored(name: str):
    from agents.db import SessionLocal, get_agent_by_name

    async with SessionLocal() as session:
        row = await get_agent_by_name(session, name)
        return row and (row.id, row.mcp_servers)


async def test_the_api_stores_the_entry_and_a_refused_save_answers_422_without_the_input(
    client, caplog
):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.INFO, logger="aiosqlite")  # prints bound parameters at DEBUG
    r = await client.post("/admin/api/agents", json=_agent("bb-reviewer", FULL))
    assert r.status_code in (200, 201), r.text
    server = {"url": URL, "auth_mode": "destination", "oauth": FULL}
    agent_id, servers = await _stored("bb-reviewer")
    assert servers == [server]
    listed = await client.get("/admin/api/agents")
    (answered,) = [a for a in listed.json() if a["name"] == "bb-reviewer"]
    shown = answered["mcp_servers"][0]["oauth"]
    assert shown == {**FULL, "has_client_secret": False}

    # A refused save: 422, the field and the rule, nothing of what was typed.
    for change, said in [({"workspace": "Acme " + SECRET}, "oauth.workspace"),
                         ({"allow_comment": "true"}, "oauth.allow_comment"),
                         ({"allow_approve": "true"}, "oauth.allow_approve"),
                         ({SECRET: SECRET}, "holds only"),
                         ({"client_secret": SECRET}, "holds only")]:
        for send in (client.post("/admin/api/agents",
                                 json=_agent("bb-refused", {**BASE, **change})),
                     client.put(f"/admin/api/agents/{agent_id}",
                                json=_agent("bb-reviewer", {**FULL, **change}))):
            r = await send
            assert r.status_code == 422, r.text
            assert said in r.text and SECRET not in r.text and "Zx9" not in r.text
            for item in r.json()["detail"]:
                assert set(item) == {"loc", "msg", "type"}, item
    r = await client.post("/admin/api/agents", json=_agent("bb-refused", dict(BASE), "app_only"))
    assert r.status_code == 422 and "requires auth_mode=destination" in r.text
    assert SECRET not in caplog.text
    assert await _stored("bb-refused") is None
    assert await _stored("bb-reviewer") == (agent_id, [server])

    # An edit posts back what it read (the echoed key included): nothing changes.
    r = await client.put(f"/admin/api/agents/{agent_id}", json=_agent("bb-reviewer", shown))
    assert r.status_code == 200, r.text
    assert await _stored("bb-reviewer") == (agent_id, [server])

    # An edit that drops the switches drops them: the stored block is no fallback.
    r = await client.put(f"/admin/api/agents/{agent_id}", json=_agent("bb-reviewer", BASE))
    assert r.status_code == 200, r.text
    assert await _stored("bb-reviewer") == (
        agent_id, [{"url": URL, "auth_mode": "destination", "oauth": BASE}])


class _Row:
    def __init__(self, name, servers, enabled=True):
        self.name = name
        self.mcp_servers = servers
        self.enabled = enabled


class _NoSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def test_credential_health_reports_the_bitbucket_destination(monkeypatch):
    import agents.admin as admin
    import agents.destination as dest_mod
    from agents.destination import Destination, DestinationServiceConfig

    rows = [_Row("Reviewer", [{"url": URL, "auth_mode": "destination", "oauth": FULL}])]
    monkeypatch.setattr(admin, "SessionLocal", lambda: _NoSession())

    async def fake_list_agents(session):
        return rows

    monkeypatch.setattr(admin, "list_agents", fake_list_agents)
    (unbound,) = await admin._destination_health()
    assert unbound["state"] == "unbound" and unbound["destination"] == "BITBUCKET"

    monkeypatch.setattr(
        dest_mod, "config_from_environment",
        lambda env: DestinationServiceConfig("id", "placeholder", "https://uaa/oauth/token",
                                             "https://api"),
    )

    class FakeResolverCls:
        def __init__(self, name, config, **kw):
            self.name = name

        async def resolve(self, **kw):
            # Application-level: the technical user of the destination, never
            # the signed-in admin's token.
            assert "user_token" not in kw
            return Destination(url="https://api.bitbucket.example.com",
                               headers={"Authorization": "Bearer t0k3n-value"},
                               expires_at=time.monotonic() + 60,
                               auth_type="BasicAuthentication")

    monkeypatch.setattr(dest_mod, "DestinationResolver", FakeResolverCls)
    (entry,) = await admin._destination_health()
    assert entry["agent"] == "Reviewer" and entry["server_key"] == URL
    assert entry["state"] == "resolvable" and entry["auth_type"] == "BasicAuthentication"
    assert entry["destination"] == "BITBUCKET" and entry["user_context"] is False
    text = json.dumps(entry)
    assert "t0k3n-value" not in text and "acme-ws" not in text and "svc-a" not in text


@pytest.mark.parametrize("policy", ["change", "diagnose"])
def test_the_tools_are_denied_in_an_abap_assistant_session(policy):
    from agents.ide.readonly import POLICIES, READONLY_POLICY, check_call

    assert len(READ_TOOLS | WRITE_TOOLS) == 7
    for tool in sorted(READ_TOOLS | WRITE_TOOLS):
        for name in (tool, "bitbucket_" + tool, "bitbucket_0_" + tool):
            assert name not in READONLY_POLICY
            assert all(name not in rules for rules in POLICIES.values())
            assert check_call(name, {"repository": "svc-a", "id": 7}, policy) is not None


# --- one entry per agent ------------------------------------------------------

ONE_ENTRY = "an agent may have at most one builtin:bitbucket entry"
# Every pair of spellings the two gates read as the built-in, the same one
# twice included.
TWICE = [(URL, URL), (URL, "builtin:bitbucket/"), ("Builtin:Bitbucket", URL),
         (" BUILTIN:BITBUCKET// ", "builtin:bitbucket/")]


def _two(first: str, second: str) -> list[dict[str, Any]]:
    """A reading entry, and a second one that would add the approval."""
    return [{"url": first, "auth_mode": "destination", "oauth": dict(BASE)},
            {"url": second, "auth_mode": "destination",
             "oauth": {**FULL, "workspace": "other-" + "zx9"}}]


@pytest.mark.parametrize("first, second", TWICE)
def test_the_gate_refuses_a_second_bitbucket_entry_in_any_spelling(first, second):
    from agents.admin import AgentPayload

    with pytest.raises(ValidationError) as refused:
        AgentPayload.model_validate({"name": "bb-two", "description": "d",
                                     "instructions": "i",
                                     "mcp_servers": _two(first, second)})
    text = json.dumps(refused.value.errors(include_input=False, include_context=False,
                                           include_url=False))
    assert ONE_ENTRY in text and "zx9" not in text


@pytest.mark.parametrize("first, second", TWICE)
def test_storage_refuses_a_second_bitbucket_entry_in_any_spelling(first, second):
    from agents.db import prepare_servers

    with pytest.raises(ValueError) as refused:
        prepare_servers(_two(first, second), None)
    assert str(refused.value).startswith(ONE_ENTRY) and "zx9" not in str(refused.value)
    # Also when another server stands between them.
    with pytest.raises(ValueError, match="^" + ONE_ENTRY):
        prepare_servers([_two(first, second)[0], NOTES, _two(first, second)[1]], None)


def test_one_bitbucket_entry_next_to_other_servers_is_stored():
    from agents.db import prepare_servers

    primary, extras, _ = prepare_servers(
        [NOTES, _server(FULL), {"url": "builtin:jira", "auth_mode": "destination",
                                "oauth": {"destination": "JIRA"}}], None)
    assert [e["url"] for e in extras] == [URL, "builtin:jira"]


async def test_a_direct_upsert_refuses_a_second_bitbucket_entry():
    from agents.db import SessionLocal, init_db, list_agents, upsert_agent

    await init_db()
    await _wipe()
    async with SessionLocal() as s:
        with pytest.raises(ValueError, match="^" + ONE_ENTRY):
            await upsert_agent(s, name="bb-two", description="d", instructions="i",
                               mcp_servers=_two(URL, "builtin:bitbucket/"))
        await s.rollback()
        assert await list_agents(s) == []


async def test_the_api_refuses_a_second_bitbucket_entry_with_422(client):
    body = {"name": "bb-two", "description": "d", "instructions": "i"}
    for first, second in TWICE:
        r = await client.post("/admin/api/agents",
                              json={**body, "mcp_servers": _two(first, second)})
        assert r.status_code == 422, r.text
        assert ONE_ENTRY in r.text and "zx9" not in r.text
    assert await _stored("bb-two") is None
    # An agent that has one cannot be edited into two.
    r = await client.post("/admin/api/agents", json=_agent("bb-two", BASE))
    assert r.status_code in (200, 201), r.text
    agent_id, servers = await _stored("bb-two")
    r = await client.put(f"/admin/api/agents/{agent_id}",
                         json={**body, "mcp_servers": _two(URL, "builtin:bitbucket/")})
    assert r.status_code == 422 and ONE_ENTRY in r.text and "zx9" not in r.text
    assert await _stored("bb-two") == (agent_id, servers)


async def test_a_refusal_by_storage_during_a_save_is_a_422_with_fixed_text_never_a_500(
    client, monkeypatch, caplog
):
    """The payload gate switched off, so that storage is the one that refuses:
    the route answers its fixed text as a 422, for a create and for an edit."""
    import agents.admin as admin

    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.INFO, logger="aiosqlite")
    r = await client.post("/admin/api/agents", json=_agent("bb-kept", FULL))
    assert r.status_code in (200, 201), r.text
    agent_id, servers = await _stored("bb-kept")

    monkeypatch.setattr(admin, "check_bitbucket_entry", lambda cfg: None)
    for change, said in [({"workspace": "Acme " + SECRET}, "oauth.workspace:"),
                         ({"branch": "a.." + SECRET}, "oauth.branch:"),
                         ({"repositories": ["svc-a", "svc-a"]}, "oauth.repositories:"),
                         ({"allow_approve": True, "allow_comment": False},
                          "oauth.allow_approve:"),
                         ({"user_context": True}, "user_context")]:
        for send in (client.post("/admin/api/agents",
                                 json=_agent("bb-refused", {**BASE, **change})),
                     client.put(f"/admin/api/agents/{agent_id}",
                                json=_agent("bb-kept", {**BASE, **change}))):
            r = await send
            assert r.status_code == 422, (r.status_code, r.text)
            assert said in r.text and SECRET not in r.text and "Zx9" not in r.text
    assert SECRET not in caplog.text
    assert await _stored("bb-refused") is None
    assert await _stored("bb-kept") == (agent_id, servers)


# --- the two routes that reach storage without the admin form -----------------

def _past_the_form() -> list[dict[str, Any]]:
    """The two entries a bundle or a seed file may carry and no form sends:
    a switch as a string, and two entries in different spellings. Each agent
    carries ``SECRET`` in its free text, the second one a typed workspace."""
    return [
        {**_agent("bb-string-switch", {**BASE, "allow_comment": "true"}),
         "description": SECRET},
        {"name": "bb-two-spellings", "description": SECRET, "instructions": "i",
         "mcp_servers": _two(URL, "Builtin:Bitbucket/")},
    ]


async def _agent_names() -> list[str]:
    from agents.db import SessionLocal, list_agents

    async with SessionLocal() as session:
        return sorted(row.name for row in await list_agents(session))


@pytest.mark.parametrize("which, said", [(0, "oauth.allow_comment"), (1, ONE_ENTRY)])
async def test_an_import_bundle_is_held_to_the_entry_gate(client, caplog, which, said):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.INFO, logger="aiosqlite")
    refused = _past_the_form()[which]
    # An agent that is there: a refused bundle with `replace` must not remove it.
    r = await client.post("/admin/api/agents", json=_agent("bb-kept", FULL))
    assert r.status_code in (200, 201), r.text
    kept = await _stored("bb-kept")
    for bundle in ({"agents": [refused]},
                   # A good agent in the same bundle is not stored either.
                   {"agents": [_agent("bb-good", BASE), refused]},
                   {"agents": [refused], "replace": True}):
        r = await client.post("/admin/api/import", json=bundle)
        assert r.status_code == 422, r.text
        assert said in r.text
        assert SECRET not in r.text and "zx9" not in r.text and "Zx9" not in r.text
        assert '"input"' not in r.text and '"ctx"' not in r.text
        assert await _agent_names() == ["bb-kept"]
        assert await _stored("bb-kept") == kept
    assert SECRET not in caplog.text and "zx9" not in caplog.text


async def test_a_seed_file_is_held_to_the_entry_gate_and_logs_no_value(
    client, caplog, tmp_path
):
    from agents.admin import seed_from_file_if_empty

    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.INFO, logger="aiosqlite")
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"agents": _past_the_form()}))
    await seed_from_file_if_empty(seed)
    assert await _agent_names() == []
    skipped = [r.getMessage() for r in caplog.records
               if "Skipping invalid seed" in r.getMessage()]
    assert len(skipped) == 2, skipped
    # Which entry and where, never what was typed.
    assert "'bb-string-switch'" in skipped[0] and "'bb-two-spellings'" in skipped[1]
    # The switch is refused at its server, the pair by the agent as a whole.
    assert skipped[0].endswith("1 validation error(s): mcp_servers.0: value_error")
    assert skipped[1].endswith("1 validation error(s): <entry>: value_error")
    assert SECRET not in caplog.text and "zx9" not in caplog.text
    assert "other-" not in caplog.text and "acme-ws" not in "\n".join(skipped)

    # Next to a good entry only the good one is stored.
    seed.write_text(json.dumps({"agents": [*_past_the_form(), _agent("bb-good", BASE)]}))
    caplog.clear()
    await seed_from_file_if_empty(seed)
    assert await _agent_names() == ["bb-good"]
    assert await _stored("bb-string-switch") is None
    assert await _stored("bb-two-spellings") is None
    assert (await _stored("bb-good"))[1] == [
        {"url": URL, "auth_mode": "destination", "oauth": BASE}]
    assert SECRET not in caplog.text and "zx9" not in caplog.text
