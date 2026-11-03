"""The InA queries in export and import bundles (design addendum (k), part 2
point 4: "export/import of the bundle carry ``ina_queries`` like
``odata_services``").

The HTTP import takes ``ina_queries`` before the agents, in its one
transaction: an agent of the same bundle may name a query the bundle brings,
an agent naming an unknown query refuses the whole bundle, and ``replace``
removes a query the section does not name unless an agent still names it.
``scripts/import_bundle.py`` writes query by query, each in its own commit,
and reports a refused one as a ``FAIL`` line. The OData behaviour of the same
routes is ``tests/test_odata_bundles.py``.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import app as app_module  # noqa: E402
from agents import admin  # noqa: E402
from agents.chat_app import dynamic_chat_app  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    InaQuery,
    ODataService,
    SessionLocal,
    SkillConfig,
    init_db,
    list_agents,
    list_ina_queries,
)
from agents.odata import admin_routes as odata_routes  # noqa: E402
from tests.test_ina_client import definition  # noqa: E402

EXPORT = "/admin/api/export"
IMPORT = "/admin/api/import"
QUERIES = "/admin/api/ina/queries"
SALES = "sales-by-month"
STOCK = "stock-levels"
SECRET = "s3cr3t-value"


def qry(name: str = SALES, **patch: Any) -> dict[str, Any]:
    """One bundle entry: what ``to_export()`` gives for a described query."""
    data: dict[str, Any] = {
        "name": name,
        "title": f"Title {name}",
        "purpose": f"Purpose of {name}",
        "not_for": "",
        "destination": "SIA_INA",
        "user_context": False,
        "query": "ZQ_SALES_01",
        "package": "",
        "definition": definition().model_dump(mode="json"),
        "metadata_fetched_at": "2026-11-03T10:00:00Z",
        "enabled": True,
    }
    data.update(patch)
    return data


def agent(name: str = "analyst", queries: list[str] | None = None, **patch: Any) -> dict:
    body: dict[str, Any] = {
        "name": name,
        "description": "d",
        "instructions": "i",
        "mcp_servers": [
            {
                "url": "builtin:ina",
                "auth_mode": "destination",
                "oauth": {"queries": queries or [SALES]},
            }
        ],
    }
    body.update(patch)
    return body


async def stored() -> dict[str, dict[str, Any]]:
    async with SessionLocal() as s:
        return {r.name: r.to_export() for r in await list_ina_queries(s)}


async def agent_names() -> list[str]:
    async with SessionLocal() as s:
        return sorted(r.name for r in await list_agents(s))


@pytest.fixture(autouse=True)
async def _clean_tables():
    await init_db()
    async with SessionLocal() as s:
        for model in (AgentConfig, InaQuery, ODataService, SkillConfig):
            await s.execute(delete(model))
        await s.commit()
    yield


@pytest.fixture
async def client():
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def create(client: AsyncClient, *bodies: dict[str, Any]) -> None:
    for body in bodies:
        r = await client.post(QUERIES, json=body)
        assert r.status_code == 201, r.text


def detail(response: Any) -> str:
    assert response.status_code == 422, response.text
    value = response.json()["detail"]
    assert isinstance(value, str), "the import's own refusal, not FastAPI's input echo"
    return value


# --- export -------------------------------------------------------------------


async def test_export_carries_the_full_query_records(client):
    await create(client, qry(), qry(STOCK, definition=None, metadata_fetched_at=None))
    bundle = (await client.get(EXPORT)).json()
    assert [q["name"] for q in bundle["ina_queries"]] == [SALES, STOCK]
    assert bundle["ina_queries"][0] == qry()
    assert bundle["ina_queries"][1]["definition"] is None
    assert "id" not in bundle["ina_queries"][0] and "updated_at" not in bundle["ina_queries"][0]


async def test_export_of_no_queries_is_an_empty_list(client):
    assert (await client.get(EXPORT)).json()["ina_queries"] == []


async def test_export_wipe_import_export_is_byte_stable(client):
    await create(client, qry(), qry(STOCK, user_context=True, enabled=False, package="ZPKG"))
    r = await client.post(IMPORT, json={"agents": [agent(queries=[SALES, STOCK])]})
    assert r.status_code == 200, r.text
    first = (await client.get(EXPORT)).json()
    async with SessionLocal() as s:
        for model in (AgentConfig, InaQuery):
            await s.execute(delete(model))
        await s.commit()
    r = await client.post(IMPORT, json=first)
    assert r.status_code == 200, r.text
    second = (await client.get(EXPORT)).json()
    assert json.dumps(second["ina_queries"]) == json.dumps(first["ina_queries"])
    assert second["agents"] == first["agents"]


# --- import -------------------------------------------------------------------


async def test_import_creates_and_updates_by_name(client):
    await create(client, qry(), qry(STOCK))
    bundle = {"ina_queries": [qry(title="New title"), qry(STOCK), qry("cost-centres")]}
    r = await client.post(IMPORT, json=bundle)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported_ina_queries"] == 3
    assert body["created_ina_queries"] == 1
    assert body["updated_ina_queries"] == 1  # STOCK came unchanged
    assert body["removed_ina_queries"] == 0
    assert (await stored())[SALES]["title"] == "New title"
    assert sorted(await stored()) == ["cost-centres", SALES, STOCK]


async def test_an_import_restamps_only_the_queries_it_changed(client):
    await create(client, qry(), qry(STOCK))
    before = {q["name"]: q["updated_at"] for q in (await client.get(QUERIES)).json()}
    r = await client.post(IMPORT, json={"ina_queries": [qry(purpose="Changed"), qry(STOCK)]})
    assert r.status_code == 200, r.text
    after = {q["name"]: q["updated_at"] for q in (await client.get(QUERIES)).json()}
    assert after[STOCK] == before[STOCK]
    assert after[SALES] != before[SALES]


async def test_an_entry_without_a_definition_key_keeps_the_stored_one(client):
    await create(client, qry())
    entry = qry(title="No definition key")
    del entry["definition"], entry["metadata_fetched_at"]
    r = await client.post(IMPORT, json={"ina_queries": [entry]})
    assert r.status_code == 200, r.text
    assert (await stored())[SALES]["definition"] == qry()["definition"]
    assert (await stored())[SALES]["metadata_fetched_at"] == qry()["metadata_fetched_at"]


async def test_an_entry_must_say_whether_it_is_enabled(client):
    entry = qry()
    del entry["enabled"]
    r = await client.post(IMPORT, json={"ina_queries": [entry]})
    assert detail(r) == f"InA query '{SALES}': enabled: Field required"
    assert await stored() == {}


@pytest.mark.parametrize(
    "patch, field",
    [
        ({"destination": f"https://user:{SECRET}@evil.example"}, "destination"),
        ({"title": f"two\nlines {SECRET}"}, "title"),
        ({"user_context": SECRET}, "user_context"),
        ({"unknown": SECRET}, "unknown"),
        ({"definition": {"measures": [{"name": SECRET + "\n"}]}}, "definition"),
    ],
)
async def test_a_refused_entry_names_the_field_never_the_value(client, patch, field):
    r = await client.post(IMPORT, json={"ina_queries": [qry(**patch)], "agents": [agent()]})
    text = detail(r)
    assert text.startswith(f"InA query '{SALES}': ") and field in text
    assert SECRET not in r.text
    assert await stored() == {} and await agent_names() == []


async def test_an_entry_whose_name_is_no_name_is_called_by_its_position(client):
    r = await client.post(IMPORT, json={"ina_queries": [qry(), qry(name=f"Bad {SECRET}")]})
    assert detail(r).startswith("InA query #2: name: ") and SECRET not in r.text


@pytest.mark.parametrize(
    "section, message",
    [
        ({"name": SECRET}, "ina_queries: expected a list of queries"),
        (SECRET, "ina_queries: expected a list of queries"),
        ([SECRET], "InA query #1: ina_query: expected an object"),
    ],
)
async def test_a_section_of_the_wrong_shape_is_refused_without_an_echo(client, section, message):
    r = await client.post(IMPORT, json={"ina_queries": section})
    assert detail(r) == message and SECRET not in r.text


async def test_a_name_listed_twice_is_refused(client):
    r = await client.post(IMPORT, json={"ina_queries": [qry(), qry(title="Again")]})
    assert detail(r) == f"InA query '{SALES}': listed more than once"
    assert await stored() == {}


async def test_the_count_and_the_size_are_bounded(client, monkeypatch):
    monkeypatch.setattr(admin, "MAX_IMPORT_INA_QUERIES", 2)
    r = await client.post(IMPORT, json={"ina_queries": [qry(), qry(STOCK), qry("third")]})
    assert detail(r) == "ina_queries: more than 2 queries in one bundle"
    monkeypatch.setattr(admin, "MAX_IMPORT_INA_QUERIES", 200)
    monkeypatch.setattr(admin, "MAX_IMPORT_INA_CHARS", len(json.dumps(qry())) + 10)
    r = await client.post(IMPORT, json={"ina_queries": [qry(), qry(STOCK, title=SECRET)]})
    assert detail(r) == "ina_queries: the section is too large for one import"
    assert SECRET not in r.text and await stored() == {}


@pytest.mark.parametrize("section", [None, []])
async def test_a_bundle_without_queries_leaves_them_alone(client, section):
    await create(client, qry())
    body: dict[str, Any] = {"replace": True, "agents": []}
    if section is not None:
        body["ina_queries"] = section
    r = await client.post(IMPORT, json=body)
    assert r.status_code == 200, r.text
    assert sorted(await stored()) == [SALES]
    assert r.json()["imported_ina_queries"] == 0


async def test_an_agent_may_name_a_query_its_bundle_brings(client):
    r = await client.post(IMPORT, json={"ina_queries": [qry()], "agents": [agent()]})
    assert r.status_code == 200, r.text
    assert await agent_names() == ["analyst"]


async def test_an_agent_naming_an_unknown_query_refuses_the_whole_bundle(client):
    bundle = {"ina_queries": [qry()], "agents": [agent(queries=[SALES, "gone"])]}
    r = await client.post(IMPORT, json=bundle)
    assert detail(r) == "Agent 'analyst': unknown InA query 'gone'"
    assert await stored() == {} and await agent_names() == []


async def test_replace_removes_the_queries_its_section_does_not_name(client):
    await create(client, qry(), qry(STOCK))
    r = await client.post(IMPORT, json={"replace": True, "ina_queries": [qry()]})
    assert r.status_code == 200, r.text
    assert r.json()["removed_ina_queries"] == 1
    assert r.json()["removed_ina_query_names"] == [STOCK]
    assert f"InA query(s) removed by replace: '{STOCK}'" in r.json()["warnings"]
    assert sorted(await stored()) == [SALES]


async def test_without_replace_nothing_is_removed(client):
    await create(client, qry(), qry(STOCK))
    r = await client.post(IMPORT, json={"ina_queries": [qry()]})
    assert r.status_code == 200 and sorted(await stored()) == [SALES, STOCK]


async def test_replace_refuses_to_remove_a_query_an_agent_still_names(client):
    await create(client, qry(), qry(STOCK))
    bundle = {"replace": True, "ina_queries": [qry()], "agents": [agent(queries=[SALES, STOCK])]}
    r = await client.post(IMPORT, json=bundle)
    assert detail(r) == (
        f"InA query '{STOCK}' cannot be removed by replace: it is used by agent(s) 'analyst'"
    )
    assert sorted(await stored()) == [SALES, STOCK] and await agent_names() == []


async def test_replace_removes_a_query_whose_only_user_is_removed_too(client):
    await create(client, qry(), qry(STOCK))
    r = await client.post(IMPORT, json={"agents": [agent("old", queries=[STOCK])]})
    assert r.status_code == 200, r.text
    r = await client.post(IMPORT, json={"replace": True, "ina_queries": [qry()], "agents": []})
    assert r.status_code == 200, r.text
    assert sorted(await stored()) == [SALES] and await agent_names() == []


async def test_replace_does_not_call_a_refused_entry_absent(client):
    await create(client, qry(), qry(STOCK))
    r = await client.post(
        IMPORT, json={"replace": True, "ina_queries": [qry(), qry(STOCK, title="")]}
    )
    assert "cannot be removed" not in detail(r)
    assert sorted(await stored()) == [SALES, STOCK]


async def test_changing_the_identity_of_a_used_query_is_reported(client):
    await create(client, qry())
    assert (await client.post(IMPORT, json={"agents": [agent()]})).status_code == 200
    r = await client.post(
        IMPORT, json={"ina_queries": [qry(destination="OTHER", user_context=True)]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["ina_identity_changes"] == [
        {"query": SALES, "changed": ["destination", "user_context"], "agents": ["analyst"]}
    ]
    assert (
        f"InA query '{SALES}': destination, user_context changed; used by agent(s) 'analyst'"
        in r.json()["warnings"]
    )


async def test_an_import_takes_no_write_lock_it_cannot_take(client, caplog):
    """The rows are locked by the section's first read; an update in the
    same transaction must not ask for the SQLite write lock again."""
    await create(client, qry(), qry(STOCK))
    with caplog.at_level(logging.WARNING):
        r = await client.post(
            IMPORT, json={"ina_queries": [qry(title="A"), qry(STOCK, title="B")]}
        )
    assert r.status_code == 200 and r.json()["updated_ina_queries"] == 2
    assert "begin_exclusive_write" not in caplog.text


async def test_an_import_that_changes_a_query_in_use_reloads(client, monkeypatch):
    await create(client, qry())
    assert (await client.post(IMPORT, json={"agents": [agent()]})).status_code == 200

    class Live:
        reloads = 0

        def holds_odata_service(self, name: str) -> bool:
            return False

        def holds_ina_query(self, name: str) -> bool:
            return False

        async def reload(self) -> None:
            Live.reloads += 1

    monkeypatch.setattr(odata_routes, "_live_registry", lambda: Live())
    monkeypatch.setattr(dynamic_chat_app, "refresh", lambda: None)
    r = await client.post(IMPORT, json={"ina_queries": [qry(enabled=False)]})
    assert r.status_code == 200, r.text
    assert r.json()["reloaded"] is True and Live.reloads == 1
    # An unchanged query reloads nothing.
    r = await client.post(IMPORT, json={"ina_queries": [qry(enabled=False)]})
    assert r.json()["reloaded"] is False and Live.reloads == 1


# --- scripts/import_bundle.py ----------------------------------------------------


def _script():
    spec = importlib.util.spec_from_file_location(
        "import_bundle_script_ina", ROOT / "scripts" / "import_bundle.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_script_imports_queries_before_the_agents(capsys):
    script = _script()
    failed = await script.import_bundle({"ina_queries": [qry(), qry(STOCK)], "agents": [agent()]})
    assert failed == []
    assert sorted(await stored()) == [SALES, STOCK] and await agent_names() == ["analyst"]
    out = capsys.readouterr().out
    assert f"ina    {SALES}" in out and f"ina    {STOCK}" in out
    # Again, changed: updated by name, never duplicated.
    failed = await script.import_bundle({"ina_queries": [qry(title="New")]})
    assert failed == [] and (await stored())[SALES]["title"] == "New"


async def test_the_script_skips_a_refused_query_and_reports_it(capsys):
    script = _script()
    missing_enabled = qry(STOCK)
    del missing_enabled["enabled"]
    bundle = {
        "ina_queries": [qry(destination=f"https://{SECRET}@x"), missing_enabled, qry("ok-one")],
        "agents": [],
    }
    failed = await script.import_bundle(bundle)
    out = capsys.readouterr().out
    assert sorted(failed) == [SALES, STOCK]
    assert f"FAIL   ina query {SALES}: destination" in out
    assert f"FAIL   ina query {STOCK}: enabled: Field required" in out
    assert SECRET not in out
    assert sorted(await stored()) == ["ok-one"]


async def test_the_script_names_a_query_without_a_name_by_its_position(capsys):
    script = _script()
    failed = await script.import_bundle({"ina_queries": [qry(name=f"Bad {SECRET}")]})
    out = capsys.readouterr().out
    assert failed == ["#1"] and "FAIL   ina query #1: name: " in out and SECRET not in out
