"""``builtin:hana`` at the admin gate and in storage: the storage cleaner,
the gate's 422 texts (field and rule, never the value), one entry per agent,
the canonical URL, the refusal of other auth modes and of the hana keys on
another entry, and the gate's block equal to storage's; the factory table,
the entry in a real registry build, the denial in ABAP Assistant sessions and
the credential health entries (one per system).

Run:  python -m pytest tests/test_hanadb_registration.py
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
from pydantic import ValidationError  # noqa: E402

from agents.odata import admin_routes as _admin_routes  # noqa: E402

# The real seam, taken before `tests/conftest.py` stubs it for each test
# ("nothing is running yet"); the import reload test puts it back.
REAL_LIVE_REGISTRY = _admin_routes._live_registry

URL = "builtin:hana"
SECRET = "Zx9secret"
BASE = {"systems": [{"name": "prd", "destination": "HANA_PRD"}]}
FULL = {"systems": [{"name": "prd", "destination": "HANA_PRD"},
                    {"name": "sysdb", "destination": "HANA.SYS-DB"}],
        "allow_select": True}
ONE_ENTRY = "an agent may have at most one builtin:hana entry"
MODE_TEXT = "builtin:hana requires auth_mode=destination"
NOTES = {"url": "builtin:sapnotes", "auth_mode": "none"}
GIT = {"url": "builtin:git", "auth_mode": "destination",
       "oauth": {"destination": "GITHUB", "provider": "github", "repositories": ["acme/web"]}}
BITBUCKET = {"url": "builtin:bitbucket", "auth_mode": "destination",
             "oauth": {"destination": "BB", "workspace": "acme-ws",
                       "repositories": ["svc-a"]}}


def _server(block: Any, url: str = URL, mode: str = "destination") -> dict[str, Any]:
    return {"url": url, "auth_mode": mode, "oauth": block}


def _copy(block: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(block))


# --- storage ------------------------------------------------------------------

@pytest.mark.parametrize("block", [BASE, FULL])
def test_storage_keeps_exactly_the_checked_block(block):
    from agents.db import _clean_oauth

    assert _clean_oauth(_copy(block), "destination", None, url="Builtin:HANA/") == block


def test_storage_stores_allow_select_only_as_true_and_no_echoed_key():
    from agents.db import _clean_oauth

    stored = _clean_oauth({**_copy(BASE), "allow_select": False,
                           "has_client_secret": False, "user_context": False},
                          "destination", None, url=URL)
    assert stored == BASE and "allow_select" not in stored
    stored = _clean_oauth({**_copy(BASE), "allow_select": True}, "destination", None, url=URL)
    assert stored["allow_select"] is True


@pytest.mark.parametrize("change, field", [
    ({"user_context": True}, "oauth.user_context"),
    ({"allow_select": "true"}, "oauth.allow_select"),
    ({"allow_select": 1}, "oauth.allow_select"),
    ({"destination": SECRET}, "oauth.destination"),
    ({"systems": []}, "oauth.systems"),
    ({"systems": [{"name": SECRET, "destination": "D"}]}, "oauth.systems[0].name"),
    ({"systems": [{"name": "prd", "destination": f" {SECRET}"}]},
     "oauth.systems[0].destination"),
    ({"client_secret": SECRET}, "oauth: a builtin:hana entry holds only"),
    ({SECRET: 1}, "oauth: a builtin:hana entry holds only"),
])
def test_storage_refuses_by_itself_and_names_no_value(change, field):
    from agents.db import _clean_oauth

    with pytest.raises(ValueError) as info:
        _clean_oauth({**_copy(BASE), **change}, "destination", None, url=URL)
    assert str(info.value).startswith(field) and SECRET not in str(info.value)


@pytest.mark.parametrize("mode", ["app_only", "oauth2", "jwt", "none"])
def test_storage_refuses_every_other_auth_mode(mode):
    from agents.db import prepare_servers

    with pytest.raises(ValueError, match="requires auth_mode=destination"):
        prepare_servers([_server(_copy(BASE), mode=mode)], None)


@pytest.mark.parametrize("oauth", [None, [], "x"])
def test_storage_refuses_an_entry_without_a_config_object(oauth):
    from agents.db import prepare_servers

    with pytest.raises(ValueError, match="builtin:hana entry needs a config object"):
        prepare_servers([_server(oauth)], None)
    with pytest.raises(ValueError, match="builtin:hana entry needs a config object"):
        prepare_servers([{"url": URL, "auth_mode": "destination"}], None)


@pytest.mark.parametrize("spelling", [URL, "builtin:hana/", "Builtin:HANA/", " BUILTIN:HANA "])
def test_storage_stores_the_url_in_the_canonical_spelling(spelling):
    from agents.db import prepare_servers

    primary, _, oauth_json = prepare_servers([_server(_copy(BASE), spelling)], None)
    assert primary["url"] == URL and json.loads(oauth_json) == BASE


TWICE = [(URL, URL), (URL, "builtin:hana/"), ("Builtin:HANA", URL)]


def _two(first: str, second: str) -> list[dict[str, Any]]:
    other = {"systems": [{"name": "zx9", "destination": "ZX9_OTHER"}], "allow_select": True}
    return [_server(_copy(BASE), first), _server(other, second)]


@pytest.mark.parametrize("first, second", TWICE)
def test_storage_refuses_a_second_hana_entry_in_any_spelling(first, second):
    from agents.db import prepare_servers

    with pytest.raises(ValueError) as refused:
        prepare_servers(_two(first, second), None)
    assert str(refused.value).startswith(ONE_ENTRY) and "zx9" not in str(refused.value)
    with pytest.raises(ValueError, match="^" + ONE_ENTRY):
        prepare_servers([_two(first, second)[0], NOTES, _two(first, second)[1]], None)


@pytest.mark.parametrize("first, second", TWICE)
def test_the_gate_refuses_a_second_hana_entry_in_any_spelling(first, second):
    from agents.admin import AgentPayload

    with pytest.raises(ValidationError) as refused:
        AgentPayload.model_validate({"name": "hana-two", "description": "d",
                                     "instructions": "i", "mcp_servers": _two(first, second)})
    text = json.dumps(refused.value.errors(include_input=False, include_context=False,
                                           include_url=False))
    assert ONE_ENTRY in text and "zx9" not in text.replace(ONE_ENTRY, "")


def test_the_single_entry_message_is_the_one_of_the_config_module():
    from agents.db import HANA_SINGLE_ENTRY_MESSAGE
    from agents.hana.config import HANA_SINGLE_ENTRY_MESSAGE as config_message

    assert HANA_SINGLE_ENTRY_MESSAGE is config_message
    assert HANA_SINGLE_ENTRY_MESSAGE.startswith(ONE_ENTRY)


def test_one_hana_entry_next_to_a_git_and_a_bitbucket_entry_is_stored():
    from agents.db import prepare_servers

    _, extras, _ = prepare_servers([NOTES, _server(_copy(FULL)), _copy(GIT),
                                    _copy(BITBUCKET)], None)
    assert [e["url"] for e in extras] == [URL, "builtin:git", "builtin:bitbucket"]
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
    assert _payload(_copy(block)).oauth.to_config() == block


@pytest.mark.parametrize("block", [BASE, FULL])
def test_the_gates_block_round_trips_through_storage(block):
    from agents.admin import AgentPayload
    from agents.db import prepare_servers

    payload = AgentPayload.model_validate({
        "name": "hana-reader", "description": "d", "instructions": "i",
        # Advanced: a chat-exposed agent that may run SELECT must be (task 20b).
        "tier": "advanced",
        "mcp_servers": [NOTES, _server(_copy(block), "Builtin:HANA/"), _copy(GIT)]})
    servers = payload.to_servers_list()
    sent = next(s for s in servers if s["url"] == URL)
    assert sent["oauth"] == block
    _, extras, _ = prepare_servers(servers, None)
    assert next(e for e in extras if e["url"] == URL)["oauth"] == block


def test_the_gate_stores_allow_select_false_as_absent():
    assert _payload({**_copy(BASE), "allow_select": False}).oauth.to_config() == BASE
    assert _payload({**_copy(BASE), "allow_select": None}).oauth.to_config() == BASE


@pytest.mark.parametrize("change, said", [
    ({"systems": None}, "oauth.systems: required, a list of 1 to 10"),
    ({"systems": []}, "oauth.systems: required, a list of 1 to 10"),
    ({"systems": [SECRET]}, "oauth.systems[0]: each system is an object"),
    ({"systems": [{"name": SECRET, "destination": "D"}]}, "oauth.systems[0].name: required"),
    ({"systems": [{"name": "prd", "destination": f"{SECRET} "}]},
     "oauth.systems[0].destination: required, a destination name"),
    ({"systems": [{"name": "prd", "destination": "D", "host": SECRET}]},
     "oauth.systems[0]: a system holds only name, destination"),
    ({"systems": [{"name": "prd", "destination": "D"}, {"name": "prd", "destination": "E"}]},
     "oauth.systems[1].name: duplicate"),
    ({"allow_select": "true"}, "oauth.allow_select: must be the JSON boolean"),
    ({"allow_select": 1}, "oauth.allow_select: must be the JSON boolean"),
    ({"user_context": True}, "oauth.user_context: builtin:hana has no signed-in user"),
    ({"destination": SECRET}, "oauth.destination: a builtin:hana entry has no destination"),
    ({SECRET: "x"}, "oauth: a builtin:hana entry holds only"),
    ({"client_secret": SECRET}, "oauth: a builtin:hana entry holds only"),
    ({"workspace": "acme"}, "oauth: a builtin:hana entry holds only"),
    ({"provider": "github"}, "oauth: a builtin:hana entry holds only"),
])
def test_the_gate_refuses_and_names_the_field_never_the_value(change, said):
    assert said in _refusal({**_copy(BASE), **change})


@pytest.mark.parametrize("oauth", [None, [], "x"])
def test_the_gate_refuses_an_entry_without_a_config_object(oauth):
    assert "builtin:hana entry needs a config object" in _refusal(oauth)


def test_the_gate_refuses_an_entry_with_no_oauth_key():
    from agents.admin import McpServerPayload

    with pytest.raises(ValidationError) as info:
        McpServerPayload.model_validate({"url": URL, "auth_mode": "destination"})
    assert "builtin:hana entry needs a config object" in str(info.value)


@pytest.mark.parametrize("mode", ["app_only", "oauth2", "jwt", "none", "session"])
def test_the_gate_refuses_every_other_auth_mode(mode):
    text = _refusal(_copy(BASE), mode)
    assert MODE_TEXT in text and "each database only through the BTP destination" in text


@pytest.mark.parametrize("url, block", [
    ("builtin:jira", {"destination": "J", "project": "P", "allow_select": True}),
    ("builtin:jira", {"destination": "J", "project": "P",
                      "systems": [{"name": "prd", "destination": "D"}]}),
    ("builtin:slack", {"destination": "S", "allow_select": True}),
])
def test_the_hana_keys_belong_to_a_hana_entry_only(url, block):
    assert "belong to a builtin:hana entry only" in _refusal(block, url=url)


@pytest.mark.parametrize("empty", [None, []])
def test_an_empty_systems_list_on_another_entry_passes_like_services(empty):
    """``systems`` is read by truthiness on a non-hana entry, as ``services``
    and ``queries`` are: ``null`` and ``[]`` both mean "not set"."""
    block = {"destination": "S", "systems": empty}
    assert "systems" not in _payload(block, url="builtin:slack").oauth.to_config()
    assert _payload({"destination": "S", "services": empty},
                    url="builtin:slack").oauth.services == []


def test_a_system_with_a_null_extra_key_passes_and_is_stored_without_it():
    """A client that serialises an unset field as ``null`` is not refused by
    the gate (which hands ``systems`` on as received); storage keeps only the
    two keys of a system."""
    from agents.db import _clean_oauth

    block = {"systems": [{"name": "prd", "destination": "HANA_PRD", "host": None}]}
    handed_on = _payload(_copy(block)).oauth.to_config()
    assert _clean_oauth(handed_on, "destination", None, url=URL) == BASE
    assert _clean_oauth(_copy(block), "destination", None, url=URL) == BASE


def test_a_git_entry_with_a_hana_key_is_refused_by_its_own_reading():
    assert "builtin:git entry holds only" in _refusal(
        {**GIT["oauth"], "allow_select": True}, url="builtin:git")


def test_an_update_that_leaves_allow_select_out_stores_none_over_a_row_that_had_it():
    """No fallback to the row being replaced: the block holds no secret, so a
    switch an edit leaves out is gone (as for Bitbucket and Git)."""
    from types import SimpleNamespace

    from agents.db import prepare_servers

    existing = SimpleNamespace(mcp_servers=[_server(_copy(FULL))])
    primary, _, oauth_json = prepare_servers([_server(_copy(BASE))], existing)
    assert primary["url"] == URL
    stored = json.loads(oauth_json)
    assert stored == BASE and "allow_select" not in stored


# --- factory ------------------------------------------------------------------

TOOLS = {"list_systems", "list_checks", "run_check"}


def test_hana_is_a_known_builtin():
    from agents.builtins import BUILTIN_URLS, is_builtin_url

    assert URL in BUILTIN_URLS and is_builtin_url(" BUILTIN:Hana ")


def test_the_docstring_table_lists_the_builtin():
    import agents.builtins as builtins

    assert "builtin:hana        --        --        app (tcp)     --       --" in builtins.__doc__


def test_the_factory_builds_without_a_binding_and_registers_run_select_only_when_allowed():
    from agents.builtins import build_builtin_toolset

    assert set(build_builtin_toolset(URL, _copy(BASE), "destination").tools) == TOOLS
    assert set(build_builtin_toolset(URL, _copy(FULL), "destination").tools) == TOOLS | {
        "run_select"}


def test_the_factory_refuses_a_bad_block_and_names_the_server_without_the_value():
    from agents.builtins import build_builtin_toolset

    with pytest.raises(ValueError, match="^oauth.user_context: builtin:hana"):
        build_builtin_toolset(URL, {**_copy(BASE), "user_context": True}, "destination")
    for mode in ("app_only", "oauth2", "jwt", "none"):
        with pytest.raises(ValueError, match=f"^{URL} requires auth_mode=destination"):
            build_builtin_toolset(URL, _copy(BASE), mode)
    with pytest.raises(ValueError, match=r"^oauth.systems\[0\].name:") as info:
        build_builtin_toolset(URL, {"systems": [{"name": SECRET, "destination": "D"}]},
                              "destination")
    assert SECRET not in str(info.value)
    with pytest.raises(ValueError, match="builtin:hana entry needs a config object"):
        build_builtin_toolset(URL, None, "destination")


def test_the_factory_is_handed_its_entry_and_nothing_of_the_context(monkeypatch):
    import agents.builtins as builtins

    seen: dict[str, Any] = {}

    def fake(oauth, **kw):
        seen.update(kw, oauth=oauth)
        return object()

    monkeypatch.setitem(builtins._FACTORIES, URL, fake)
    builtins.build_builtin_toolset("Builtin:HANA", BASE, "destination",
                                   context={"agent_name": "dba", "odata_services": {}})
    assert seen == {"server_key": URL, "auth_mode": "destination", "oauth": BASE}


def test_importing_the_builtins_does_not_import_the_driver():
    import subprocess

    code = ("import sys; import agents.builtins, agents.registry; "
            "print('hdbcli' in sys.modules)")
    env = {**os.environ, "DATABASE_URL": "sqlite+aiosqlite:///:memory:"}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "False"


# --- a real registry build ----------------------------------------------------

async def _wipe() -> None:
    from sqlalchemy import delete

    from agents.db import AgentConfig, SessionLocal, SkillConfig

    async with SessionLocal() as session:
        await session.execute(delete(AgentConfig))
        await session.execute(delete(SkillConfig))
        await session.commit()


async def _build(agents: dict[str, list[dict[str, Any]]], raw: dict[str, Any] | None = None):
    from pydantic_ai.models.test import TestModel
    from sqlalchemy import select

    from agents import registry as registry_module
    from agents.db import AgentConfig, SessionLocal, init_db, upsert_agent

    await init_db()
    await _wipe()
    async with SessionLocal() as s:
        for name, servers in agents.items():
            # Advanced: a chat-exposed agent that may run SELECT must be
            # (task 20b, tests/test_hanadb_reach.py).
            await upsert_agent(s, name=name, description="d", instructions="i",
                               mcp_servers=servers, tier="advanced")
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


@pytest.mark.usefixtures("real_agents_and_mcp")
async def test_a_registry_build_gives_the_agent_the_tools_its_entry_allows():
    build = await _build({"reader": [_server(_copy(BASE))],
                          "selector": [_server(_copy(FULL))]})
    assert await _tool_names(build.specialists["reader"]) == TOOLS
    assert await _tool_names(build.specialists["selector"]) == TOOLS | {"run_select"}


@pytest.mark.usefixtures("real_agents_and_mcp")
async def test_with_a_second_server_the_tools_carry_the_hana_prefix():
    build = await _build({"dba": [_server(_copy(BASE)), NOTES]})
    names = await _tool_names(build.specialists["dba"])
    assert {"hana_" + n for n in TOOLS} <= names
    assert not (TOOLS | {"run_select"}) & names


@pytest.mark.usefixtures("real_agents_and_mcp")
async def test_a_row_written_by_hand_with_two_hana_entries_gets_no_hana_tools(caplog):
    caplog.set_level(logging.WARNING)
    build = await _build({"dba": [NOTES]},
                         raw={"dba": [_server(_copy(BASE)), _server(_copy(FULL))]})
    names = await _tool_names(build.specialists["dba"])
    assert names and not any(n.startswith("hana_") for n in names)
    assert caplog.text.count("more than one builtin:hana entry") == 1
    assert "HANA_PRD" not in caplog.text and "sysdb" not in caplog.text


# --- credential health --------------------------------------------------------

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


async def test_credential_health_lists_one_entry_per_system(monkeypatch, caplog):
    import agents.admin as admin
    import agents.destination as dest_mod
    from agents.destination import DestinationError, DestinationServiceConfig

    rows = [_Row("dba", [_server(_copy(FULL))]),
            _Row("off", [_server(_copy(BASE))], enabled=False)]
    monkeypatch.setattr(admin, "SessionLocal", lambda: _NoSession())

    async def fake_list_agents(session):
        return rows

    monkeypatch.setattr(admin, "list_agents", fake_list_agents)
    unbound = await admin._destination_health()
    assert [(e["system"], e["destination"], e["state"]) for e in unbound] == [
        ("prd", "HANA_PRD", "unbound"), ("sysdb", "HANA.SYS-DB", "unbound")]

    monkeypatch.setattr(
        dest_mod, "config_from_environment",
        lambda env: DestinationServiceConfig("id", "placeholder", "https://uaa/oauth/token",
                                             "https://api"),
    )
    asked: list[str] = []
    from tests import hanadb_helpers as hh

    class FakeProps(dict):
        auth_type = "BasicAuthentication"

    class FakeResolverCls:
        def __init__(self, name, config, **kw):
            self.name = name

        async def resolve(self, **kw):  # a TCP destination has no URL to resolve
            raise AssertionError("resolve() called for a builtin:hana destination")

        async def resolve_properties(self, **kw):
            asked.append(self.name)
            if self.name == "HANA.SYS-DB":
                raise DestinationError(f"500 from https://{SECRET}.example.com/destinations")
            return FakeProps(hh.http_props())

    monkeypatch.setattr(dest_mod, "DestinationResolver", FakeResolverCls)
    entries = await admin._destination_health()
    assert asked == ["HANA_PRD", "HANA.SYS-DB"]
    prd, sysdb = entries
    assert prd == {"agent": "dba", "server_key": URL, "system": "prd",
                   "destination": "HANA_PRD", "user_context": False,
                   "state": "resolvable", "auth_type": "BasicAuthentication", "error": None}
    assert sysdb["system"] == "sysdb" and sysdb["state"] == "error"
    assert SECRET not in json.dumps(entries)
    # The log names the system and the code, never the resolver's text.
    assert "system 'sysdb'" in caplog.text and SECRET not in caplog.text


async def test_credential_health_of_a_refused_block_is_one_fixed_error(monkeypatch):
    import agents.admin as admin

    rows = [_Row("dba", [_server({"systems": [{"name": SECRET, "destination": "D"}]})])]
    monkeypatch.setattr(admin, "SessionLocal", lambda: _NoSession())

    async def fake_list_agents(session):
        return rows

    monkeypatch.setattr(admin, "list_agents", fake_list_agents)
    (entry,) = await admin._destination_health()
    assert entry["state"] == "error" and entry["system"] == "" and entry["destination"] == ""
    assert entry["error"].startswith("oauth.systems[0].name:")
    assert SECRET not in json.dumps(entry)


# --- a bundle import that changes the entry reloads -----------------------------

@pytest.mark.usefixtures("real_agents_and_mcp")
async def test_an_import_that_drops_allow_select_reloads(monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from pydantic_ai.models.test import TestModel

    import agents.registry as registry_module
    import app as app_module
    from agents.chat_app import dynamic_chat_app
    from agents.db import init_db
    from agents.odata import admin_routes

    await init_db()
    await _wipe()
    monkeypatch.setattr(registry_module, "get_model", lambda *a, **k: TestModel())
    monkeypatch.setattr(admin_routes, "_live_registry", REAL_LIVE_REGISTRY)
    registry = registry_module.registry
    monkeypatch.setattr(registry, "_build", None)
    async with AsyncClient(transport=ASGITransport(app=app_module.app),
                           base_url="http://test") as client:
        r = await client.post("/admin/api/agents", json={
            "name": "dba", "description": "d", "instructions": "i",
            "mcp_servers": [_server(_copy(FULL))], "expose_chat": False})
        assert r.status_code == 201, r.text
        await registry.reload()
        assert "run_select" in await _tool_names(registry.build.specialists["dba"])

        reloads: list[int] = []
        real_reload = registry.reload

        async def reload():
            reloads.append(1)
            return await real_reload()

        monkeypatch.setattr(registry, "reload", reload)
        monkeypatch.setattr(dynamic_chat_app, "refresh", lambda: None)

        r = await client.get("/admin/api/export")
        agents = r.json()["agents"]
        (server,) = [s for s in agents[0]["mcp_servers"] if s.get("url") == URL]
        server["oauth"].pop("allow_select", None)
        r = await client.post("/admin/api/import", json={"agents": agents})
        assert r.status_code == 200, r.text
        assert r.json()["reloaded"] is True and reloads == [1]
        assert await _tool_names(registry.build.specialists["dba"]) == TOOLS

        # Unchanged entry: no reload.
        agents[0]["description"] = "another text"
        r = await client.post("/admin/api/import", json={"agents": agents})
        assert r.status_code == 200 and r.json()["reloaded"] is False and reloads == [1]


@pytest.mark.parametrize("override", [
    {"ProxyType": "Internet"},
    {"URL": "https://vhost-db:30015"},
    {"Password": None},
    {"User": None},
    {"hana.sslTrustStore": "x"},
    {"Type": "RFC"},
])
async def test_credential_health_reads_the_destination_as_the_toolset_does(
        monkeypatch, caplog, override):
    import agents.admin as admin
    import agents.destination as dest_mod
    from agents.destination import DestinationServiceConfig
    from agents.hana.errors import MESSAGES
    from tests import hanadb_helpers as hh

    rows = [_Row("dba", [_server({"systems": [{"name": "prd", "destination": "HANA_PRD"}]})])]
    monkeypatch.setattr(admin, "SessionLocal", lambda: _NoSession())

    async def fake_list_agents(session):
        return rows

    monkeypatch.setattr(admin, "list_agents", fake_list_agents)
    monkeypatch.setattr(
        dest_mod, "config_from_environment",
        lambda env: DestinationServiceConfig("id", "placeholder", "https://uaa/oauth/token",
                                             "https://api"),
    )

    class FakeResolverCls:
        def __init__(self, name, config, **kw):
            pass

        async def resolve_properties(self, **kw):
            return dest_mod.DestinationProperties(hh.http_props(**override), expires_at=0)

    monkeypatch.setattr(dest_mod, "DestinationResolver", FakeResolverCls)
    (entry,) = await admin._destination_health()
    assert entry["state"] == "error"
    assert entry["error"] == f"{MESSAGES['destination_error']} (destination_error)"
    text = json.dumps(entry) + caplog.text
    for secret in (hh.HOST, hh.USER, hh.PASSWORD, "vhost-db"):
        assert secret not in text


def test_credential_health_docstring_no_longer_calls_it_a_tcp_destination():
    import agents.admin as admin

    assert "TCP destination (no URL" not in (admin._hana_destination_health.__doc__ or "")
