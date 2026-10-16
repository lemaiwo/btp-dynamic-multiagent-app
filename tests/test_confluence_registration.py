# tests/test_confluence_registration.py
"""``builtin:confluence`` as a registered built-in: the factory table, storage,
the admin gate and the entry in a real registry build.

Run:  python -m pytest tests/test_confluence_registration.py
"""

from __future__ import annotations

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
from pydantic import ValidationError  # noqa: E402

URL = "builtin:confluence"
BASE = {"destination": "JIRA"}
FULL = {"destination": "JIRA", "spaces": "shERPa, DOCS", "api_base": "/rest/api"}
TOOLS = {"search_pages", "get_page", "list_children"}
SECRET = "Zq9!bad key"


# --- factory ------------------------------------------------------------------

def test_confluence_is_a_known_builtin():
    from agents.builtins import BUILTIN_URLS, is_builtin_url

    assert URL in BUILTIN_URLS and is_builtin_url("BUILTIN:Confluence")


def test_the_factory_names_the_server_without_a_binding_and_refuses_a_bad_block():
    from agents.builtins import build_builtin_toolset
    from agents.destination import DestinationError

    with pytest.raises(DestinationError, match=f"{URL}: no destination service binding"):
        build_builtin_toolset(URL, FULL, "destination")
    with pytest.raises(ValueError, match="^oauth.user_context:"):
        build_builtin_toolset(URL, {**BASE, "user_context": True}, "destination")
    with pytest.raises(ValueError, match="requires auth_mode=destination"):
        build_builtin_toolset(URL, BASE, "app_only")
    with pytest.raises(ValueError, match="^oauth.spaces:") as info:
        build_builtin_toolset(URL, {**BASE, "spaces": SECRET}, "destination")
    assert SECRET not in str(info.value)


def test_the_docstring_table_lists_the_builtin():
    import agents.builtins as builtins

    assert "builtin:confluence" in builtins.__doc__


# --- storage ------------------------------------------------------------------

def test_storage_keeps_the_three_keys_and_drops_the_rest():
    from agents.db import _clean_oauth

    stored = _clean_oauth(
        {**FULL, "user_context": True, "client_secret": "s3", "project": "ABC",
         "allow_comment": True, "has_client_secret": False},
        "destination", None, url="Builtin:Confluence/")
    assert stored == {"destination": "JIRA", "spaces": "shERPa, DOCS", "api_base": "/rest/api"}
    assert _clean_oauth({"destination": " JIRA "}, "destination", None, url=URL) == {
        "destination": "JIRA"}
    with pytest.raises(ValueError, match="requires a destination name"):
        _clean_oauth({"spaces": "shERPa"}, "destination", None, url=URL)


def test_a_spaces_list_is_stored_as_csv():
    from agents.db import _clean_oauth

    stored = _clean_oauth({"destination": "JIRA", "spaces": ["shERPa", " DOCS ", ""]},
                          "destination", None, url=URL)
    assert stored == {"destination": "JIRA", "spaces": "shERPa, DOCS"}


def test_the_key_table_names_exactly_the_three():
    from agents.db import _CONFLUENCE_DEST_KEYS, _DEST_KEYS_BY_URL, _DEST_USER_CONTEXT_URLS

    assert _DEST_KEYS_BY_URL[URL] == _CONFLUENCE_DEST_KEYS == ("destination", "spaces", "api_base")
    assert URL not in _DEST_USER_CONTEXT_URLS


# --- the admin gate -----------------------------------------------------------

def _payload(**kw):
    from agents.admin import McpServerPayload

    return McpServerPayload(**{"url": URL, "auth_mode": "destination", **kw})


def test_the_gate_accepts_the_entry_and_hands_storage_the_csv():
    server = _payload(oauth=FULL)
    assert server.oauth.to_config() == FULL
    listed = _payload(oauth={"destination": "JIRA", "spaces": ["shERPa", "DOCS"]})
    assert listed.oauth.to_config()["spaces"] == "shERPa, DOCS"
    assert _payload(oauth=BASE).oauth.to_config() == BASE  # no pin is allowed (a WARNING)


def test_the_gate_requires_destination_mode():
    for mode in ("app_only", "oauth2", "jwt", "none", "session"):
        with pytest.raises(ValidationError, match="requires auth_mode=destination"):
            _payload(auth_mode=mode, oauth={**BASE, "client_id": "x"} if mode != "none" else None)


def test_the_gate_refuses_user_context_and_a_bad_space_key_naming_the_field_only():
    with pytest.raises(ValidationError, match="no signed-in user to act as"):
        _payload(oauth={**BASE, "user_context": True})
    with pytest.raises(ValidationError, match="oauth.spaces must be") as info:
        _payload(oauth={**BASE, "spaces": f"DOCS, {SECRET}"})
    # What a client gets is `msg` (agents/validation_errors.py drops the input).
    assert all(SECRET not in e["msg"] for e in info.value.errors())
    with pytest.raises(ValidationError, match="at most 20"):
        _payload(oauth={**BASE, "spaces": ", ".join(f"S{i}" for i in range(21))})


def test_spaces_belongs_to_confluence_only():
    from agents.admin import McpServerPayload

    with pytest.raises(ValidationError, match=f"oauth.spaces belongs to a {URL} entry only"):
        McpServerPayload(url="builtin:jira", auth_mode="destination",
                         oauth={"destination": "J", "project": "P", "spaces": "X"})
    with pytest.raises(ValidationError, match="oauth.spaces belongs to"):
        McpServerPayload(url="https://mcp.example.hana.ondemand.com/mcp", auth_mode="destination",
                         oauth={"destination": "D", "spaces": "X"})


def test_the_gate_keeps_the_api_base_rule():
    with pytest.raises(ValidationError, match="api_base must be a path"):
        _payload(oauth={**BASE, "api_base": "https://x.example/wiki"})


# --- a real registry build ----------------------------------------------------

@pytest.fixture
def binding(monkeypatch):
    """A destination service binding by name; nothing is called."""
    monkeypatch.setenv("DESTINATION_CLIENT_ID", "cid")
    monkeypatch.setenv("DESTINATION_CLIENT_SECRET", "placeholder")
    monkeypatch.setenv("DESTINATION_URI", "https://destination.example.com")
    monkeypatch.setenv("DESTINATION_TOKEN_URL", "https://login.example.com/oauth/token")


async def _build(agents: dict[str, list[dict[str, Any]]]):
    from pydantic_ai.models.test import TestModel
    from sqlalchemy import delete

    from agents import registry as registry_module
    from agents.db import AgentConfig, SessionLocal, SkillConfig, init_db, upsert_agent

    await init_db()
    async with SessionLocal() as s:
        await s.execute(delete(AgentConfig))
        await s.execute(delete(SkillConfig))
        for name, servers in agents.items():
            await upsert_agent(s, name=name, description="d", instructions="i",
                               mcp_servers=servers)
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
async def test_an_agent_with_the_entry_is_built_with_the_three_tools():
    from sqlalchemy import select

    from agents.db import AgentConfig, SessionLocal

    build = await _build({"reviewer": [
        {"url": "Builtin:Confluence/", "auth_mode": "destination", "oauth": FULL}]})
    assert await _tool_names(build.specialists["reviewer"]) == TOOLS
    async with SessionLocal() as s:
        row = (await s.execute(select(AgentConfig).where(AgentConfig.name == "reviewer"))
               ).scalar_one()
    assert row.mcp_servers[0]["url"] == URL  # stored in the canonical spelling
