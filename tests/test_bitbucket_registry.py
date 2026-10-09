"""A registry build fails closed on an agent row with two ``builtin:bitbucket``
entries.

The admin gate and storage refuse a second entry, but a row written directly
in the database passes neither, and the second toolset could carry wider
switches (``allow_approve``) than the one that was reviewed. Such a row gets
NO Bitbucket toolset, not the first one: which of the two was meant is not
for the build to guess.
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

import pytest  # noqa: E402

URL = "builtin:bitbucket"
BASE = {"destination": "BITBUCKET_zq7_DEST", "workspace": "acme-zq7-ws"}
WIDE = {**BASE, "allow_comment": True, "allow_approve": True}
NOTES = {"url": "builtin:sapnotes", "auth_mode": "none"}
READ_TOOLS = {"list_pull_requests", "get_pull_request", "get_diff", "get_file"}
WRITE_TOOLS = {"add_inline_comment", "submit_review", "complete_approval"}
RULE = "more than one builtin:bitbucket entry"


def _server(block: dict[str, Any], url: str = URL) -> dict[str, Any]:
    return {"url": url, "auth_mode": "destination", "oauth": block}


async def _build(agents: dict[str, list[dict[str, Any]]],
                 extra: dict[str, list[dict[str, Any]]]):
    """A registry build of ``agents`` (name -> servers) after ``extra`` (name ->
    further servers) was written into the row directly, past gate and storage."""
    from pydantic_ai.models.test import TestModel
    from sqlalchemy import delete, select

    from agents import registry as registry_module
    from agents.db import AgentConfig, SessionLocal, SkillConfig, init_db, upsert_agent

    await init_db()
    async with SessionLocal() as s:
        await s.execute(delete(AgentConfig))
        await s.execute(delete(SkillConfig))
        for name, servers in agents.items():
            # Not exposed to chat: an entry that approves may not be (final
            # review M3, tests/test_bitbucket_reach.py).
            await upsert_agent(s, name=name, description="d", instructions="i",
                               mcp_servers=servers, expose_chat=False)
        await s.commit()
        for name, servers in extra.items():
            row = (await s.execute(
                select(AgentConfig).where(AgentConfig.name == name))).scalar_one()
            row.extra_servers_json = json.dumps(
                json.loads(row.extra_servers_json or "[]") + servers)
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


@pytest.fixture
def binding(monkeypatch):
    """A destination service binding by name; nothing is called."""
    monkeypatch.setenv("DESTINATION_CLIENT_ID", "cid")
    monkeypatch.setenv("DESTINATION_CLIENT_SECRET", "placeholder")
    monkeypatch.setenv("DESTINATION_URI", "https://destination.example.com")
    monkeypatch.setenv("DESTINATION_TOKEN_URL", "https://login.example.com/oauth/token")


def _bitbucket(names: set[str]) -> set[str]:
    return {n for n in names if n.endswith(tuple(READ_TOOLS | WRITE_TOOLS))}


@pytest.mark.usefixtures("real_agents_and_mcp", "binding")
@pytest.mark.parametrize("second", [URL, "Builtin:Bitbucket", " builtin:bitbucket ",
                                    "builtin:bitbucket/", "BUILTIN:BITBUCKET//"])
async def test_a_row_with_two_entries_gets_no_bitbucket_toolset_and_keeps_the_rest(
        second, caplog):
    caplog.set_level(logging.DEBUG)
    build = await _build(
        {"reviewer": [_server(BASE), NOTES], "other": [_server(BASE)]},
        extra={"reviewer": [_server(WIDE, second)]})
    names = await _tool_names(build.specialists["reviewer"])
    # Not the first entry and not the wider second one: none.
    assert _bitbucket(names) == set()
    assert any(n.startswith("sapnotes_") for n in names)
    # An agent with its one entry is built as before.
    assert await _tool_names(build.specialists["other"]) == READ_TOOLS
    told = [r for r in caplog.records if RULE in r.getMessage()]
    assert len(told) == 1 and told[0].levelno == logging.WARNING
    assert "reviewer" in told[0].getMessage()
    # The agent and the rule only: no URL as it was typed, no config value.
    # (The app's own lines: at DEBUG the test's database driver logs its rows.)
    said = "\n".join(caplog.handler.format(r) for r in caplog.records
                     if r.name.startswith("agents"))
    assert "zq7" not in said and "allow_approve" not in said
    if second.strip() != URL:        # (the rule itself names the built-in)
        assert second not in said
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.usefixtures("real_agents_and_mcp", "binding")
async def test_a_row_with_nothing_but_two_entries_is_not_built_at_all(caplog):
    caplog.set_level(logging.WARNING)
    build = await _build({"reviewer": [_server(BASE)], "other": [_server(WIDE)]},
                         extra={"reviewer": [_server(WIDE)]})
    assert "reviewer" not in build.specialists
    assert await _tool_names(build.specialists["other"]) == READ_TOOLS | WRITE_TOOLS
    assert len([r for r in caplog.records if RULE in r.getMessage()]) == 1


@pytest.mark.usefixtures("real_agents_and_mcp", "binding")
async def test_one_entry_next_to_other_servers_is_built_as_before(caplog):
    caplog.set_level(logging.WARNING)
    build = await _build({"reviewer": [_server(WIDE), NOTES]}, extra={})
    names = await _tool_names(build.specialists["reviewer"])
    assert _bitbucket(names) == {"bitbucket_" + n for n in READ_TOOLS | WRITE_TOOLS}
    assert not [r for r in caplog.records if RULE in r.getMessage()]


@pytest.mark.usefixtures("real_agents_and_mcp", "binding")
@pytest.mark.parametrize("spelling", ["Builtin:Bitbucket", " builtin:bitbucket ",
                                      "\tBUILTIN:BITBUCKET\n"])
async def test_one_entry_in_another_spelling_gets_the_prefix_of_its_name(spelling):
    """A row written by hand: the fixed-form activity line goes by the tool's
    name, so the prefix must be the built-in's, however the URL is spelled."""
    from agents.bitbucket_tools import activity_summary

    build = await _build({"reviewer": [NOTES]}, extra={"reviewer": [_server(WIDE, spelling)]})
    names = _bitbucket(await _tool_names(build.specialists["reviewer"]))
    assert names == {"bitbucket_" + n for n in READ_TOOLS | WRITE_TOOLS}
    assert all(activity_summary(n, "x") is not None for n in names)
