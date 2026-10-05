"""The OData catalogue in export and import bundles.

A bundle is how a landscape's configuration moves to the next one, so the
catalogue travels with the agents that attach it: ``odata_services`` in the
export, upserted by name on import *before* the agents, in the import's one
transaction. What is tested here: the round trip is byte-stable, a bundle
without the section behaves exactly as before, nothing is enabled that the
bundle does not enable, a refused entry never repeats its values, and an
import that changes whose identity a used service carries says so.
"""

from __future__ import annotations

import copy
import json
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
for _v in (
    "DESTINATION_CLIENT_ID",
    "DESTINATION_CLIENT_SECRET",
    "DESTINATION_URI",
    "DESTINATION_TOKEN_URL",
    "DESTINATION_UAA_URL",
    "CONNECTIVITY_CLIENT_ID",
    "CONNECTIVITY_CLIENT_SECRET",
    "CONNECTIVITY_TOKEN_URL",
    "CONNECTIVITY_PROXY_HOST",
    "CONNECTIVITY_PROXY_PORT",
    "CONNECTIVITY_PP_MODE",
):
    os.environ.pop(_v, None)

import app as app_module  # noqa: E402
from agents import admin  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    ODataAuditLog,
    ODataService,
    SessionLocal,
    SkillConfig,
    init_db,
    list_agents,
    list_odata_services,
)
from tests.odata_helpers import service_payload  # noqa: E402

EXPORT = "/admin/api/export"
IMPORT = "/admin/api/import"
SERVICES = "/admin/api/odata/services"
AGENTS = "/admin/api/agents"
# Never part of a refusal: what a refused entry carried.
SECRET = "s3cr3t-value"


def svc(name: str = "purchase-requisitions", **patch: Any) -> dict[str, Any]:
    """One bundle entry: what ``to_export()`` gives for a valid service."""
    return service_payload(name=name, **patch)


def agent(name: str = "pr-agent", services: list[str] | None = None, **patch: Any):
    body: dict[str, Any] = {
        "name": name,
        "description": "d",
        "instructions": "i",
        "mcp_servers": [
            {
                "url": "builtin:odata",
                "auth_mode": "destination",
                "oauth": {
                    "services": services or ["purchase-requisitions"],
                    "allow_write": True,
                },
            }
        ],
    }
    body.update(patch)
    return body


async def catalogue() -> dict[str, dict[str, Any]]:
    async with SessionLocal() as s:
        return {r.name: r.to_export() for r in await list_odata_services(s)}


async def agent_names() -> list[str]:
    async with SessionLocal() as s:
        return sorted(r.name for r in await list_agents(s))


async def wipe() -> None:
    async with SessionLocal() as s:
        for model in (AgentConfig, ODataService, ODataAuditLog, SkillConfig):
            await s.execute(delete(model))
        await s.commit()


@pytest.fixture(autouse=True)
async def _clean_tables():
    await init_db()
    await wipe()
    yield


@pytest.fixture
async def client():
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def create(client: AsyncClient, *services: dict[str, Any]) -> None:
    for body in services:
        r = await client.post(SERVICES, json=body)
        assert r.status_code == 201, r.text


def detail(response: Any) -> str:
    assert response.status_code == 422, response.text
    value = response.json()["detail"]
    assert isinstance(value, str), "the import's own refusal, not FastAPI's input echo"
    return value


# --- export -------------------------------------------------------------------


async def test_export_carries_the_full_service_payloads(client):
    await create(
        client,
        svc("stock", enabled=False, metadata_fetched_at="2026-10-05T08:30:00.123456+02:00"),
        svc(),
    )
    exported = (await client.get(EXPORT)).json()
    async with SessionLocal() as s:
        rows = await list_odata_services(s)
        assert exported["odata_services"] == [r.to_export() for r in rows]
    assert [e["name"] for e in exported["odata_services"]] == ["purchase-requisitions", "stock"]
    stock = exported["odata_services"][1]
    # Exactly the payload: no id, no row timestamps, nothing about its users.
    assert set(stock) == set(svc())
    assert stock["enabled"] is False
    # The one timestamp it carries is in UTC with a Z, whatever was sent.
    assert stock["metadata_fetched_at"] == "2026-10-05T06:30:00.123456Z"
    assert exported["odata_services"][0]["metadata_fetched_at"] is None
    # What was there before is unchanged.
    assert exported["version"] == 1
    assert set(exported) == {
        "version",
        "orchestrator_instructions",
        "skills",
        "agents",
        "workflows",
        "odata_services",
    }


async def test_export_of_an_empty_catalogue_is_an_empty_list(client):
    assert (await client.get(EXPORT)).json()["odata_services"] == []


# --- round trip ---------------------------------------------------------------


async def test_export_wipe_import_export_is_byte_stable(client):
    await create(
        client,
        svc(metadata_fetched_at="2026-10-05T08:30:00.123456+02:00"),
        svc("stock", enabled=False, user_context=True, metadata_fetched_at="2026-10-05T06:30:00Z"),
        svc("plain"),
    )
    assert (await client.post(AGENTS, json=agent(services=["stock", "plain"]))).status_code == 201
    first = (await client.get(EXPORT)).json()

    await wipe()
    r = await client.post(IMPORT, json=first)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported_odata_services"] == 3
    assert (body["created_odata_services"], body["updated_odata_services"]) == (3, 0)
    assert body["imported"] == 1

    second = (await client.get(EXPORT)).json()
    assert json.dumps(second, sort_keys=True) == json.dumps(first, sort_keys=True)
    # ... and importing onto itself changes nothing either.
    r = await client.post(IMPORT, json=second)
    assert r.status_code == 200, r.text
    # "updated" counts the services whose stored form changed: none here.
    assert (r.json()["created_odata_services"], r.json()["updated_odata_services"]) == (0, 0)
    assert r.json()["odata_identity_changes"] == []
    third = (await client.get(EXPORT)).json()
    assert json.dumps(third, sort_keys=True) == json.dumps(first, sort_keys=True)


async def test_a_bundle_carries_the_service_its_agent_attaches(client):
    """Order matters: the agent's existence check must see the bundle's
    service, so the catalogue goes in first."""
    bundle = {"agents": [agent()], "odata_services": [svc()]}
    r = await client.post(IMPORT, json=bundle)
    assert r.status_code == 200, r.text
    assert await agent_names() == ["pr-agent"]
    assert list(await catalogue()) == ["purchase-requisitions"]


async def test_an_agent_naming_a_service_nobody_has_refuses_the_whole_bundle(client):
    bundle = {
        "agents": [agent("good"), agent("bad", services=["purchase-requisitions", "nope"])],
        "odata_services": [svc()],
    }
    assert detail(await client.post(IMPORT, json=bundle)) == (
        "Agent 'bad': unknown OData service 'nope'"
    )
    assert await agent_names() == [] and await catalogue() == {}


async def test_a_service_created_first_in_a_transaction_is_rolled_back_with_it():
    """The import's first write may be a new service, inserted in a
    SAVEPOINT. On SQLite a savepoint opened before any other write was the
    outermost one and its release committed the row for good."""
    from agents.db import create_odata_service, validate_odata_service

    async with SessionLocal() as s:
        await create_odata_service(s, validate_odata_service(svc()), commit=False)
        await create_odata_service(s, validate_odata_service(svc("stock")), commit=False)
        await s.rollback()
    assert await catalogue() == {}
    async with SessionLocal() as s:
        await create_odata_service(s, validate_odata_service(svc()), commit=False)
        await s.commit()
    assert list(await catalogue()) == ["purchase-requisitions"]


# --- bundles without the section ----------------------------------------------


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("section", ["absent", "empty", "null"])
async def test_a_bundle_without_services_leaves_the_catalogue_alone(client, replace, section):
    """Every bundle exported before the catalogue existed, and the export of
    a landscape that has none: neither may wipe a catalogue, replace or not."""
    await create(client, svc(), svc("stock"))
    before = await catalogue()
    bundle: dict[str, Any] = {"agents": [agent()], "replace": replace}
    if section == "empty":
        bundle["odata_services"] = []
    elif section == "null":
        bundle["odata_services"] = None
    r = await client.post(IMPORT, json=bundle)
    assert r.status_code == 200, r.text
    assert await catalogue() == before
    body = r.json()
    assert body["imported_odata_services"] == 0 and body["removed_odata_services"] == 0
    assert body["odata_identity_changes"] == []
    # The answer an older client reads is still there.
    assert body["status"] == "imported" and body["imported"] == 1
    for key in ("imported_skills", "imported_workflows", "removed", "removed_skills", "warnings"):
        assert key in body


# --- upsert -------------------------------------------------------------------


async def test_import_creates_and_updates_by_name(client):
    await create(client, svc(), svc("stock"))
    bundle = {
        "odata_services": [
            svc(title="Requisitions, renamed", not_for="Not for purchase orders"),
            svc("new-one"),
        ]
    }
    r = await client.post(IMPORT, json=bundle)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported_odata_services"] == 2
    assert (body["created_odata_services"], body["updated_odata_services"]) == (1, 1)
    assert body["removed_odata_services"] == 0
    stored = await catalogue()
    assert sorted(stored) == ["new-one", "purchase-requisitions", "stock"]
    assert stored["purchase-requisitions"] == bundle["odata_services"][0]
    assert stored["stock"] == svc("stock"), "a service the bundle does not name is untouched"


async def test_an_import_makes_only_the_services_it_changed_stale_for_an_open_tab(client):
    """An open tab holds the ``updated_at`` it loaded. The import restamps
    the service it really changed (that tab must reload, 409) and leaves an
    identical one alone (that tab's save goes through)."""
    await create(client, svc(), svc("stock"))
    loaded = {s["name"]: s["updated_at"] for s in (await client.get(SERVICES)).json()}
    bundle = {"odata_services": [svc(title="Changed by the import"), svc("stock")]}
    r = await client.post(IMPORT, json=bundle)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported_odata_services"] == 2
    assert (body["created_odata_services"], body["updated_odata_services"]) == (0, 1)
    after = {s["name"]: s["updated_at"] for s in (await client.get(SERVICES)).json()}
    assert after["stock"] == loaded["stock"]
    assert after["purchase-requisitions"] != loaded["purchase-requisitions"]

    changed = await client.put(
        f"{SERVICES}/purchase-requisitions",
        json={**svc(title="From the tab"), "expected_updated_at": loaded["purchase-requisitions"]},
    )
    assert changed.status_code == 409, changed.text
    assert (await catalogue())["purchase-requisitions"]["title"] == "Changed by the import"
    untouched = await client.put(
        f"{SERVICES}/stock",
        json={**svc("stock", title="From the tab"), "expected_updated_at": loaded["stock"]},
    )
    assert untouched.status_code == 200, untouched.text


async def test_an_identity_change_alone_is_a_change(client):
    await create(client, svc())
    r = await client.post(IMPORT, json={"odata_services": [svc(user_context=True)]})
    assert r.status_code == 200, r.text
    assert r.json()["updated_odata_services"] == 1
    assert (await catalogue())["purchase-requisitions"]["user_context"] is True


async def test_import_takes_the_switches_exactly_as_the_bundle_says(client):
    """Nothing is enabled that the bundle does not enable -- neither the
    service nor an operation -- and nothing it enables is switched off."""
    await create(client, svc("was-on"), svc("was-off", enabled=False))
    read_only = svc("was-on", enabled=False)
    definition = svc()["definition"]
    definition["entity_sets"][0]["operations"] = ["list", "get", "update"]
    definition["entity_sets"][0]["fields"][1]["writable"] = True
    definition["operations"] = [
        {"name": "Release", "kind": "function_import", "http_method": "POST", "enabled": False}
    ]
    writing = svc("was-off", enabled=True, definition=definition)
    r = await client.post(IMPORT, json={"odata_services": [read_only, writing, svc("fresh")]})
    assert r.status_code == 200, r.text
    stored = await catalogue()
    assert stored["was-on"]["enabled"] is False
    assert stored["was-off"]["enabled"] is True
    assert stored["was-off"]["definition"] == writing["definition"]
    assert stored["was-off"]["definition"]["operations"][0]["enabled"] is False
    assert stored["fresh"]["definition"]["entity_sets"][0]["operations"] == ["list", "get"]


async def test_an_entry_must_say_whether_it_is_enabled(client):
    """The payload model defaults ``enabled`` to on. For an import that
    default would switch on a service the bundle never said to switch on
    (and re-enable one an admin disabled here), so the key is required."""
    await create(client, svc(enabled=False))
    entry = svc()
    del entry["enabled"]
    assert detail(await client.post(IMPORT, json={"odata_services": [entry]})) == (
        "OData service 'purchase-requisitions': enabled: Field required"
    )
    assert (await catalogue())["purchase-requisitions"]["enabled"] is False


# --- refusals -----------------------------------------------------------------


async def test_an_invalid_service_is_one_line_and_nothing_is_imported(client):
    await create(client, svc("stock"))
    before = await catalogue()
    bad = svc("broken")
    bad["service_path"] = f"https://user:{SECRET}@s4.internal/sap/opu"
    bundle = {
        "skills": [{"name": "a-skill", "description": "d", "content": "c"}],
        "agents": [agent(services=["good"])],
        "odata_services": [svc("good"), bad, svc("stock", title="Changed")],
    }
    r = await client.post(IMPORT, json=bundle)
    message = detail(r)
    assert message.startswith("OData service 'broken': service_path: ")
    assert message.count("\n") == 0, "one error line"
    assert SECRET not in r.text
    assert await catalogue() == before and await agent_names() == []
    async with SessionLocal() as s:
        assert (await s.execute(SkillConfig.__table__.select())).all() == []


@pytest.mark.parametrize(
    "patch, field",
    [
        ({"enabled": "true"}, "enabled"),
        ({"user_context": 1}, "user_context"),
        ({"title": f"two\nlines {SECRET}"}, "title"),
        ({"destination": f"https://{SECRET}/x"}, "destination"),
        ({"definition": None}, "definition"),
        ({"unknown_key": SECRET}, "unknown_key"),
        ({f"https://{SECRET}/pasted-as-a-key": 1}, "<unknown field>"),
        ({"name": "Not A Slug"}, "name"),
    ],
)
async def test_a_refused_entry_names_the_field_never_the_value(client, patch, field):
    entry = {**svc(), **patch}
    r = await client.post(IMPORT, json={"odata_services": [entry]})
    message = detail(r)
    label = "#1" if "name" in patch else "'purchase-requisitions'"
    assert message.startswith(f"OData service {label}: {field}: "), message
    assert SECRET not in r.text and "Not A Slug" not in r.text
    assert await catalogue() == {}


@pytest.mark.parametrize(
    "section, message",
    [
        (f"not a list {SECRET}", "odata_services: expected a list of services"),
        ({"name": SECRET}, "odata_services: expected a list of services"),
        ([f"not an object {SECRET}"], "OData service #1: service: expected an object"),
        ([None], "OData service #1: service: expected an object"),
    ],
)
async def test_a_section_of_the_wrong_shape_is_refused_without_an_echo(client, section, message):
    r = await client.post(IMPORT, json={"agents": [], "odata_services": section})
    assert detail(r) == message
    assert SECRET not in r.text


async def test_a_name_listed_twice_is_refused(client):
    """The second entry would silently overwrite the first."""
    bundle = {"odata_services": [svc(), svc("stock"), svc(title="Another")]}
    assert detail(await client.post(IMPORT, json=bundle)) == (
        "OData service 'purchase-requisitions': listed more than once"
    )
    assert await catalogue() == {}


async def test_the_bundle_size_is_bounded(client):
    entries = [svc(f"s{i}") for i in range(admin.MAX_IMPORT_ODATA_SERVICES + 1)]
    message = detail(await client.post(IMPORT, json={"odata_services": entries}))
    assert message == (
        f"odata_services: more than {admin.MAX_IMPORT_ODATA_SERVICES} services in one bundle"
    )
    assert await catalogue() == {}


# --- replace ------------------------------------------------------------------


async def test_replace_with_a_section_removes_the_services_it_does_not_name(client):
    await create(client, svc(), svc("stock"), svc("old"))
    r = await client.post(
        IMPORT, json={"agents": [agent()], "odata_services": [svc()], "replace": True}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["removed_odata_services"] == 2
    # A curated definition has no undo: the answer says which ones went.
    assert body["removed_odata_service_names"] == ["old", "stock"]
    assert body["warnings"] == ["OData service(s) removed by replace: 'old', 'stock'"]
    assert list(await catalogue()) == ["purchase-requisitions"]


async def test_without_replace_nothing_is_removed(client):
    await create(client, svc(), svc("stock"))
    r = await client.post(IMPORT, json={"odata_services": [svc()]})
    assert r.status_code == 200 and r.json()["removed_odata_services"] == 0
    assert r.json()["removed_odata_service_names"] == [] and r.json()["warnings"] == []
    assert sorted(await catalogue()) == ["purchase-requisitions", "stock"]


async def test_replace_refuses_to_remove_a_service_an_agent_still_uses(client):
    """Unlike a skill, a service is not detached: the agent would be left
    with an entry that names nothing. The whole import is rolled back."""
    await create(client, svc(), svc("stock"))
    assert (await client.post(AGENTS, json=agent("keeper", services=["stock"]))).status_code == 201
    before = await catalogue()
    bundle = {
        "agents": [agent("keeper", services=["stock"]), agent("newcomer")],
        "odata_services": [svc(title="Changed")],
        "replace": True,
    }
    assert detail(await client.post(IMPORT, json=bundle)) == (
        "OData service 'stock' cannot be removed by replace: it is used by agent(s) 'keeper'"
    )
    assert await catalogue() == before and await agent_names() == ["keeper"]


async def test_replace_removes_a_service_whose_only_user_is_removed_too(client):
    await create(client, svc(), svc("stock"))
    assert (await client.post(AGENTS, json=agent("leaver", services=["stock"]))).status_code == 201
    bundle = {"agents": [agent("stayer")], "odata_services": [svc()], "replace": True}
    r = await client.post(IMPORT, json=bundle)
    assert r.status_code == 200, r.text
    assert (r.json()["removed"], r.json()["removed_odata_services"]) == (1, 1)
    assert await agent_names() == ["stayer"]
    assert list(await catalogue()) == ["purchase-requisitions"]


@pytest.mark.parametrize("fault", ["invalid", "no_enabled", "listed_twice"])
async def test_replace_does_not_call_a_refused_entry_absent(client, fault):
    """The bundle names the service, so replace must not also report it as
    one it would remove: one fault, one line."""
    await create(client, svc(), svc("stock"))
    assert (await client.post(AGENTS, json=agent("keeper", services=["stock"]))).status_code == 201
    before = await catalogue()
    stock = svc("stock")
    entries = [svc(), stock]
    if fault == "invalid":
        stock["title"] = ""
        expected = "OData service 'stock': title: "
    elif fault == "no_enabled":
        del stock["enabled"]
        expected = "OData service 'stock': enabled: Field required"
    else:
        entries = [stock, svc(), svc("stock", title="Again")]
        expected = "OData service 'stock': listed more than once"
    bundle = {
        "agents": [agent("keeper", services=["stock"])],
        "odata_services": entries,
        "replace": True,
    }
    message = detail(await client.post(IMPORT, json=bundle))
    assert message.startswith(expected) and message.count("\n") == 0, message
    assert "cannot be removed" not in message
    assert await catalogue() == before


async def test_a_failing_workflow_rolls_back_the_catalogue_and_the_agents(client):
    """The last section to be written: by then the services and the agents
    of the bundle are flushed, and none of it may stay."""
    await create(client, svc("stock"))
    assert (await client.post(AGENTS, json=agent("keeper", services=["stock"]))).status_code == 201
    before = await catalogue()
    bundle = {
        "agents": [agent("newcomer")],
        "odata_services": [svc(), svc("stock", title="Changed", destination="S4_ODATA_TECH")],
        "workflows": [
            {
                "name": "broken",
                "api_slug": "broken",
                "steps": [{"position": 1, "agent_name": "nobody-by-that-name"}],
            }
        ],
    }
    message = detail(await client.post(IMPORT, json=bundle))
    assert message.startswith("Workflow 'broken': ") and message.count("\n") == 0
    assert await catalogue() == before and await agent_names() == ["keeper"]


# --- cost ---------------------------------------------------------------------


async def test_the_section_size_is_bounded(client, monkeypatch):
    """Up to MAX_IMPORT_ODATA_SERVICES entries of up to 2 MB each would be
    validated inside one request; the section as a whole has a ceiling, and
    the refusal is a fixed text."""
    one = len(json.dumps(svc(), ensure_ascii=False))
    monkeypatch.setattr(admin, "MAX_IMPORT_ODATA_CHARS", one * 2 + 10)
    calls: list[str] = []
    real = admin.validate_odata_service
    monkeypatch.setattr(
        admin, "validate_odata_service", lambda d: (calls.append(d["name"]), real(d))[1]
    )
    r = await client.post(IMPORT, json={"odata_services": [svc(), svc("stock")]})
    assert r.status_code == 200, r.text
    calls.clear()
    bundle = {"odata_services": [svc(), svc("stock"), svc("one-too-many", title=SECRET)]}
    r = await client.post(IMPORT, json=bundle)
    assert detail(r) == "odata_services: the section is too large for one import"
    assert SECRET not in r.text
    assert calls == [], "refused before anything is validated"
    assert sorted(await catalogue()) == ["purchase-requisitions", "stock"]
    assert admin.MAX_IMPORT_ODATA_CHARS  # the patched one; the real one is checked below


def test_the_real_section_ceiling():
    from agents.odata.models import MAX_DEFINITION_BYTES

    assert admin.MAX_IMPORT_ODATA_CHARS == 4 * MAX_DEFINITION_BYTES


async def test_each_entry_is_validated_once_and_off_the_event_loop(client, monkeypatch):
    import threading

    from agents import db as agents_db

    await create(client, svc("stock"))
    loop_thread = threading.get_ident()
    seen: list[tuple[str, bool]] = []
    real = admin.validate_odata_service

    def validate(data):
        seen.append((data["name"], threading.get_ident() != loop_thread))
        return real(data)

    monkeypatch.setattr(admin, "validate_odata_service", validate)
    # The definition models are not run a second time on the loop when the
    # row is written, and the stored JSON is not parsed to compare identity.
    on_loop: list[str] = []
    real_validate = agents_db._ODataServiceDefinition.model_validate

    def definition_validate(value, *a, **kw):
        if threading.get_ident() == loop_thread:
            on_loop.append("definition")
        return real_validate(value, *a, **kw)

    monkeypatch.setattr(agents_db._ODataServiceDefinition, "model_validate", definition_validate)
    monkeypatch.setattr(
        agents_db.ODataService,
        "to_export",
        lambda self: on_loop.append("to_export") or {},
    )
    r = await client.post(
        IMPORT, json={"odata_services": [svc(), svc("stock", destination="S4_ODATA_TECH")]}
    )
    assert r.status_code == 200, r.text
    assert seen == [("purchase-requisitions", True), ("stock", True)]
    assert on_loop == []
    monkeypatch.undo()
    stored = await catalogue()
    assert stored["stock"] == svc("stock", destination="S4_ODATA_TECH")
    assert stored["purchase-requisitions"] == svc()


def test_the_import_lock_is_an_exclusive_row_lock_on_postgres_only():
    """Compile-only: SQLite has no row locks, so no suite here would notice
    the clause missing where it matters."""
    from sqlalchemy.dialects import postgresql, sqlite

    from agents.db import _list_odata_services_query

    locked = _list_odata_services_query(lock=True)
    text = str(locked.compile(dialect=postgresql.dialect()))
    assert text.rstrip().endswith("FOR UPDATE") and "ORDER BY odata_services.name" in text
    assert "FOR" not in str(locked.compile(dialect=sqlite.dialect()))
    assert "FOR" not in str(_list_odata_services_query(lock=False).compile(
        dialect=postgresql.dialect()
    ))


# --- identity changes ---------------------------------------------------------


async def test_changing_the_identity_of_a_used_service_is_reported(client):
    """Allowed -- importing is the admin's own act -- but an agent that ran
    as the signed-in user and now runs as a technical user (or reaches
    another system) must not change hands unseen."""
    await create(client, svc(), svc("stock", user_context=True), svc("unused"), svc("same"))
    for name, services in (("buyer", ["purchase-requisitions", "same"]), ("auditor", ["stock"])):
        assert (await client.post(AGENTS, json=agent(name, services=services))).status_code == 201
    assert (
        await client.post(AGENTS, json=agent("reader", services=["stock"], enabled=False))
    ).status_code == 201
    bundle = {
        "odata_services": [
            svc(destination="S4_ODATA_TECH"),
            svc("stock", user_context=False, destination="S4_ODATA_TECH"),
            svc("unused", destination="S4_ODATA_TECH"),
            svc("same", title="Only the title"),
        ]
    }
    r = await client.post(IMPORT, json=bundle)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["odata_identity_changes"] == [
        {"service": "purchase-requisitions", "changed": ["destination"], "agents": ["buyer"]},
        {
            "service": "stock",
            "changed": ["destination", "user_context"],
            "agents": ["auditor", "reader"],
        },
    ]
    assert body["warnings"] == [
        "OData service 'purchase-requisitions': destination changed; used by agent(s) 'buyer'",
        "OData service 'stock': destination, user_context changed; "
        "used by agent(s) 'auditor', 'reader'",
    ]
    # Field names and agent names only: no destination name in the answer.
    assert "S4_ODATA" not in r.text
    stored = await catalogue()
    assert stored["stock"]["user_context"] is False
    assert stored["stock"]["destination"] == "S4_ODATA_TECH"


async def test_an_identity_change_counts_the_agents_the_same_bundle_attaches(client):
    await create(client, svc())
    bundle = {
        "agents": [agent("newcomer")],
        "odata_services": [svc(user_context=True)],
    }
    r = await client.post(IMPORT, json=bundle)
    assert r.status_code == 200, r.text
    assert r.json()["odata_identity_changes"] == [
        {"service": "purchase-requisitions", "changed": ["user_context"], "agents": ["newcomer"]}
    ]


# --- reload -------------------------------------------------------------------


async def test_import_does_not_reload_the_registry(client, monkeypatch):
    """The import route has never rebuilt the registry itself (the admin UIs
    call reload after it); carrying the catalogue does not change that, so a
    catalogue change takes effect at the same reload as the agents'."""
    calls: list[str] = []

    async def reload():
        calls.append("reload")

    monkeypatch.setattr(admin.registry, "reload", reload)
    monkeypatch.setattr(admin.dynamic_chat_app, "refresh", lambda: calls.append("refresh"))
    r = await client.post(IMPORT, json={"agents": [agent()], "odata_services": [svc()]})
    assert r.status_code == 200, r.text
    assert calls == []


def test_older_bundles_still_validate():
    payload = admin.ImportPayload.model_validate({"agents": [copy.deepcopy(agent())]})
    assert payload.odata_services is None and payload.replace is False
