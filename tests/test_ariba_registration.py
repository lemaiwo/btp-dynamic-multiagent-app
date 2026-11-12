# tests/test_ariba_registration.py
"""``builtin:ariba`` as a registered built-in: the factory table, storage,
the admin gate, the activity preview and the entry in a real registry build.

Run:  python -m pytest tests/test_ariba_registration.py
"""

from __future__ import annotations

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
from pydantic import ValidationError  # noqa: E402

URL = "builtin:ariba"
BASE = {"destination": "ARIBA_SOURCING"}
TOOLS = {"list_events", "get_event", "list_documents", "read_document"}
FILE_NAME = "Offer of Supplier Alpha.pdf"
SIGNER = "Signer Person Alpha"


# --- factory ------------------------------------------------------------------

def test_ariba_is_a_known_builtin():
    from agents.builtins import BUILTIN_URLS, is_builtin_url

    assert URL in BUILTIN_URLS and is_builtin_url("BUILTIN:Ariba")


def test_the_factory_names_the_server_without_a_binding_and_refuses_a_bad_block():
    from agents.builtins import build_builtin_toolset
    from agents.destination import DestinationError

    with pytest.raises(DestinationError, match=f"{URL}: no destination service binding"):
        build_builtin_toolset(URL, BASE, "destination")
    with pytest.raises(ValueError, match="^oauth.user_context:"):
        build_builtin_toolset(URL, {**BASE, "user_context": True}, "destination")
    with pytest.raises(ValueError, match="requires auth_mode=destination"):
        build_builtin_toolset(URL, BASE, "app_only")
    with pytest.raises(ValueError, match="requires a 'destination'"):
        build_builtin_toolset(URL, {}, "destination")


def test_the_docstring_table_lists_the_builtin():
    import agents.builtins as builtins

    assert "builtin:ariba" in builtins.__doc__


# --- storage ------------------------------------------------------------------

def test_storage_keeps_the_destination_only():
    from agents.db import _clean_oauth

    stored = _clean_oauth(
        {**BASE, "user_context": True, "client_secret": "s3", "spaces": "X",
         "allow_comment": True, "has_client_secret": False, "realm": "r"},
        "destination", None, url="Builtin:Ariba/")
    assert stored == {"destination": "ARIBA_SOURCING"}
    assert _clean_oauth({"destination": " ARIBA_SOURCING "}, "destination", None,
                        url=URL) == BASE
    with pytest.raises(ValueError, match="requires a destination name"):
        _clean_oauth({}, "destination", None, url=URL)


def test_the_key_table_names_exactly_the_destination():
    from agents.db import _ARIBA_DEST_KEYS, _DEST_KEYS_BY_URL, _DEST_USER_CONTEXT_URLS

    assert _DEST_KEYS_BY_URL[URL] == _ARIBA_DEST_KEYS == ("destination",)
    assert URL not in _DEST_USER_CONTEXT_URLS


def test_storage_stores_the_canonical_url_and_refuses_a_second_entry_or_another_mode():
    from agents.db import ARIBA_SINGLE_ENTRY_MESSAGE, prepare_servers

    primary, extras, _ = prepare_servers(
        [{"url": "Builtin:Ariba/", "auth_mode": "destination", "oauth": BASE}], None)
    assert primary["url"] == URL and not extras
    with pytest.raises(ValueError, match="at most one builtin:ariba entry"):
        prepare_servers([
            {"url": URL, "auth_mode": "destination", "oauth": BASE},
            {"url": "BUILTIN:ARIBA/", "auth_mode": "destination", "oauth": BASE},
        ], None)
    assert "builtin:ariba" in ARIBA_SINGLE_ENTRY_MESSAGE
    with pytest.raises(ValueError, match="requires auth_mode=destination"):
        prepare_servers([{"url": URL, "auth_mode": "none"}], None)


# --- the admin gate -----------------------------------------------------------

def _payload(**kw):
    from agents.admin import McpServerPayload

    return McpServerPayload(**{"url": URL, "auth_mode": "destination", **kw})


def test_the_gate_accepts_the_entry():
    assert _payload(oauth=BASE).oauth.to_config() == BASE


def test_the_gate_requires_destination_mode():
    for mode in ("app_only", "oauth2", "jwt", "none", "session"):
        with pytest.raises(ValidationError, match="requires auth_mode=destination"):
            _payload(auth_mode=mode, oauth={**BASE, "client_id": "x"} if mode != "none" else None)


def test_the_gate_refuses_user_context_and_a_bad_destination_name():
    with pytest.raises(ValidationError, match="no signed-in user to act as"):
        _payload(oauth={**BASE, "user_context": True})
    with pytest.raises(ValidationError, match="oauth.destination must be") as info:
        _payload(oauth={"destination": "bad name/with slash"})
    assert all("bad name" not in e["msg"] for e in info.value.errors())


def test_the_gate_refuses_another_builtins_pins():
    with pytest.raises(ValidationError, match="oauth.spaces belongs to"):
        _payload(oauth={**BASE, "spaces": "DOCS"})


def test_the_agent_gate_refuses_a_second_entry_in_any_spelling():
    from agents.admin import AgentPayload

    with pytest.raises(ValidationError, match="at most one builtin:ariba entry"):
        AgentPayload(name="buyer", description="d", instructions="i", mcp_servers=[
            {"url": URL, "auth_mode": "destination", "oauth": BASE},
            {"url": "Builtin:Ariba/", "auth_mode": "destination", "oauth": BASE},
        ])


# --- the ABAP Assistant and the activity preview --------------------------------

def test_the_tools_are_not_in_the_readonly_policy():
    from agents.ide.readonly import READONLY_POLICY

    assert not TOOLS & set(READONLY_POLICY)


def test_the_activity_preview_is_the_fixed_line():
    from pydantic_ai.messages import ToolReturnPart

    from agents.registry import _short_tool_output

    result = {"file_name": FILE_NAME, "format": "pdf", "pages": 3,
              "digital_signature": True,
              "signatures": [{"signer": SIGNER, "signed_at": None,
                              "covers_whole_file": True}], "note": "n"}
    for name in ("read_document", "ariba_read_document", "ariba_0_read_document"):
        part = ToolReturnPart(tool_name=name, content=result, tool_call_id="c1")
        assert _short_tool_output(part) == "read_document: pdf, 3 pages, signed"
    listed = ToolReturnPart(tool_name="list_documents", tool_call_id="c2", content={
        "documents": [{"document_id": "D1", "file_name": FILE_NAME}]})
    assert _short_tool_output(listed) == "list_documents: 1 documents"
    event = ToolReturnPart(tool_name="get_event", tool_call_id="c3", content={
        "title": "Tender", "suppliers": [{"name": "Supplier Alpha", "responded": True}]})
    assert _short_tool_output(event) == "get_event: 1 suppliers, 1 responded"
    refused = ToolReturnPart(tool_name="list_events", tool_call_id="c4", content={
        "error": {"code": "throttled", "message": "m"}})
    assert _short_tool_output(refused) == "error: throttled"


# --- a real registry build ----------------------------------------------------

@pytest.fixture
def binding(monkeypatch):
    """A destination service binding by name; nothing is called."""
    monkeypatch.setenv("DESTINATION_CLIENT_ID", "cid")
    monkeypatch.setenv("DESTINATION_CLIENT_SECRET", "placeholder")
    monkeypatch.setenv("DESTINATION_URI", "https://destination.example.com")
    monkeypatch.setenv("DESTINATION_TOKEN_URL", "https://login.example.com/oauth/token")


async def _build(agents: dict[str, list[dict[str, Any]]],
                 raw: dict[str, list[dict[str, Any]]] | None = None):
    """``raw``: extra servers written into a row past every gate."""
    import json

    from pydantic_ai.models.test import TestModel
    from sqlalchemy import delete, select

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
async def test_an_agent_with_the_entry_is_built_with_the_four_tools():
    from sqlalchemy import select

    from agents.db import AgentConfig, SessionLocal

    build = await _build({"buyer": [
        {"url": "Builtin:Ariba/", "auth_mode": "destination", "oauth": BASE}]})
    assert await _tool_names(build.specialists["buyer"]) == TOOLS
    async with SessionLocal() as s:
        row = (await s.execute(select(AgentConfig).where(AgentConfig.name == "buyer"))
               ).scalar_one()
    assert row.mcp_servers[0]["url"] == URL  # stored in the canonical spelling
    assert row.mcp_servers[0]["oauth"] == BASE


@pytest.mark.usefixtures("real_agents_and_mcp", "binding")
async def test_a_hand_written_row_with_two_entries_gets_no_ariba_tools(caplog):
    caplog.set_level(logging.WARNING)
    notes = {"url": "builtin:sapnotes", "auth_mode": "none"}
    two = [{"url": URL, "auth_mode": "destination", "oauth": BASE},
           {"url": URL, "auth_mode": "destination", "oauth": {"destination": "OTHER"}}]
    build = await _build({"buyer": [notes]}, raw={"buyer": two})
    names = await _tool_names(build.specialists["buyer"])
    assert names and not any(n.startswith("ariba_") or n in TOOLS for n in names)
    assert "more than one builtin:ariba entry" in caplog.text
