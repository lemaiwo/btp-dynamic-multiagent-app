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
                          "oauth": {**FULL, "user_context": False}}])
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
            await upsert_agent(s, name=name, description="d", instructions="i",
                               mcp_servers=servers)
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
