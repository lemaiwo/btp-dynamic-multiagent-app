"""The data catalogue for every kind (addendum 2026-11-03 (k)).

A ``generic`` catalogue service imported under its own kind, an InA query as
one ``query`` node with its ``dimension`` and ``measure`` nodes, the
cross-kind ``same_object`` links by name, curated edges kept across
re-imports, the scope of a ``builtin:ina`` entry, an ``("odata", name)``
scope that reaches a generic service, and the search boost and keys over
dimensions and measures; the admin routes for every kind (the sources list
and the detail per kind, the links an admin draws and removes, hints on
query, dimension and measure nodes). The helpers and fixtures are those of
``tests/test_data_catalog.py``: ``_clean`` (autouse) and ``two_services``
are imported so that pytest finds them here by name.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402
from agents.data_catalog import search  # noqa: E402
from agents.data_catalog.models import DataCatalogEdge, DataCatalogNode, utcnow  # noqa: E402
from agents.data_catalog.scope import allowed_sources_of  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    InaQuery,
    SessionLocal,
    create_ina_query,
    get_ina_query,
    update_ina_query,
)
from tests.test_data_catalog import (  # noqa: E402
    API,
    BILLING,
    SECRET,
    _clean,  # noqa: F401
    add_agent,
    add_service,
    edges,
    node,
    nodes_of,
    odata_server,
    rebuild,
    replace_service,
    svc,
    two_services,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")


GENERIC_DATA: dict[str, Any] = {
    "name": "agent-data",
    "kind": "generic",
    "title": "SAP allow-listed data",
    "purpose": "Tables, views and function modules SAP lets agents read",
    "destination": "AGENT_DATA_A4H",
    "odata_version": "v2",
    "service_path": "/sap/opu/odata/sap/ZAGENT_DATA_SRV",
    "definition": {
        "entity_sets": [
            {"name": "ZC_ONCALL", "title": "On-call duty rows",
             "entity_type": "ZAGENT_DATA_SRV.ZC_ONCALLType",
             "keys": [{"name": "EMPLOYEE"}], "operations": ["list"],
             "fields": [{"name": "EMPLOYEE", "label": "Employee", "selectable": True}]},
            {"name": "SCARR", "title": "Airline",
             "entity_type": "ZAGENT_DATA_SRV.SCARRType",
             "keys": [{"name": "CARRID"}], "operations": ["list"],
             "fields": [{"name": "CARRID", "label": "Airline code", "selectable": True}]},
        ],
        # `changes_data` left at its default (true): on a generic service a
        # GET function import is a read all the same (`operation_is_write`).
        "operations": [
            {"name": "MONTH_NAMES_GET", "kind": "function_import", "http_method": "GET",
             "enabled": True, "title": "Month names"},
        ],
    },
}

# An OData service whose entity types are named after SAP objects: `SCARR`
# (the generic table) and `ZC_OnCallType` (the CDS view behind the InA query,
# in the camel case an OData type keeps).
FLIGHTS: dict[str, Any] = svc(
    BILLING,
    name="flights",
    title="Flight data",
    purpose="Airlines and their connections",
    service_path="/sap/opu/odata/sap/ZFLIGHT_SRV",
    definition={
        "entity_sets": [
            {"name": "Carriers", "title": "Carrier", "entity_type": "ZFLIGHT_SRV.SCARR",
             "keys": [{"name": "CarrierId"}], "operations": ["list"],
             "fields": [{"name": "CarrierId", "label": "Carrier", "selectable": True}]},
            {"name": "OnCall", "title": "Standby roster",
             "entity_type": "ZFLIGHT_SRV.ZC_OnCallType",
             "keys": [{"name": "Employee"}], "operations": ["list"],
             "fields": [{"name": "Employee", "label": "Employee", "selectable": True}]},
        ]
    },
)

ONCALL_QUERY: dict[str, Any] = {
    "name": "oncall",
    "title": "On-call analysis",
    "purpose": "Analyse on-call duty per team",
    "not_for": "Payroll",
    "destination": "SIA_INA",
    "user_context": True,
    "query": "2CZC_ONCALL",
    "definition": {
        "dimensions": [
            {"name": "0CALMONTH", "label": "Calendar month"},
            {"name": "ZTEAM", "label": "Team", "key_field": "ZTEAM_KEY"},
        ],
        "measures": [
            {"name": "ZSTANDBY_H", "label": "Standby hours", "unit": "H"},
            {"name": "ZCALLOUTS", "label": "Callouts"},
        ],
        "variables": [{"name": "!V_YEAR", "label": "Year", "mandatory": True}],
    },
}


def ina_server(*names: str) -> dict[str, Any]:
    return {"url": "builtin:ina", "auth_mode": "destination", "oauth": {"queries": list(names)}}


async def add_query(data: dict[str, Any]) -> None:
    async with SessionLocal() as s:
        await create_ina_query(s, copy.deepcopy(data))


@pytest.fixture
async def ina_query():
    await add_query(ONCALL_QUERY)
    await add_agent("standby", ina_server("oncall"))


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app_module.app),
                           base_url="http://test") as c:
        yield c


@pytest.fixture
async def generic_service():
    await add_service(GENERIC_DATA)
    await add_agent("data", odata_server("agent-data"))


async def edge_between(a: DataCatalogNode, b: DataCatalogNode) -> list[DataCatalogEdge]:
    return [e for e in await edges() if {e.from_id, e.to_id} == {a.id, b.id}]


async def test_a_generic_service_is_imported_as_its_own_kind(generic_service):
    await rebuild()
    nodes = await nodes_of("agent-data")
    assert nodes and {n.source_kind for n in nodes} == {"generic"}
    month = await node("agent-data", "function", "MONTH_NAMES_GET")
    assert (month.readable, month.writable) == (1, 0)  # a GET function import reads
    source = await node("agent-data", "source", "agent-data")
    assert (source.readable, source.writable) == (1, 0)
    assert (await rebuild())["imported"] == 0


async def test_the_catalogue_handle_of_a_generic_service_rebuilds_it(generic_service):
    """The catalogue routes rebuild ``("odata", name)`` whatever the kind;
    a service whose kind changed leaves no nodes of its old kind."""
    counts = await rebuild(("odata", "agent-data"))
    assert counts["imported"] > 0
    assert {n.source_kind for n in await nodes_of("agent-data")} == {"generic"}
    await replace_service(svc(GENERIC_DATA, kind="odata"))
    await rebuild(("odata", "agent-data"))
    assert {n.source_kind for n in await nodes_of("agent-data")} == {"odata"}
    month = await node("agent-data", "function", "MONTH_NAMES_GET")
    assert (month.readable, month.writable) == (0, 1)  # on kind odata it is a write
    await replace_service(svc(GENERIC_DATA))
    await rebuild()  # the full rebuild drops the old kind as well
    assert {n.source_kind for n in await nodes_of("agent-data")} == {"generic"}


async def test_an_ina_query_becomes_a_query_with_dimensions_and_measures(ina_query):
    await rebuild()
    source = await node("oncall", "source", "oncall")
    assert source.source_kind == "ina" and source.parent_id is None
    assert (source.label, source.purpose, source.not_for) == (
        "On-call analysis", "Analyse on-call duty per team", "Payroll")
    assert (source.readable, source.writable) == (1, 0)
    query = await node("oncall", "query", "2CZC_ONCALL")
    assert query.parent_id == source.id and query.label == "On-call analysis"
    assert query.type_name == "2CZC_ONCALL" and (query.readable, query.writable) == (1, 0)
    children = [n for n in await nodes_of("oncall") if n.parent_id == query.id]
    assert {(n.kind, n.name) for n in children} == {
        ("dimension", "0CALMONTH"), ("dimension", "ZTEAM"),
        ("measure", "ZSTANDBY_H"), ("measure", "ZCALLOUTS")}
    assert all(n.readable == 1 and n.writable == 0 and n.is_key == 0 for n in children)
    by_name = {n.name: n for n in children}
    assert by_name["ZSTANDBY_H"].unit == "H" and by_name["ZCALLOUTS"].unit is None
    assert by_name["0CALMONTH"].label == "Calendar month" and by_name["ZTEAM"].unit is None
    assert len(await nodes_of("oncall")) == 6  # the variable is no node
    assert (await rebuild())["imported"] == 0


async def test_an_ina_query_follows_its_description_and_keeps_hints(ina_query):
    await rebuild()
    measure = await node("oncall", "measure", "ZSTANDBY_H")
    async with SessionLocal() as db:  # what PUT .../hints stores
        row = await db.get(DataCatalogNode, measure.id)
        row.label, row.curated_at = "Hours on standby", utcnow()
        await db.commit()
    changed = copy.deepcopy(ONCALL_QUERY)
    changed["definition"]["measures"] = [
        {"name": "ZSTANDBY_H", "label": "Standby (h)", "unit": "HUR"}]
    async with SessionLocal() as s:
        await update_ina_query(s, "oncall", changed)
    counts = await rebuild(("ina", "oncall"))
    assert counts["removed"] == 1  # ZCALLOUTS
    again = await node("oncall", "measure", "ZSTANDBY_H")
    assert again.id == measure.id and again.label == "Hours on standby"  # the admin's
    assert again.unit == "HUR"  # SAP's


async def test_an_undescribed_or_broken_ina_query(ina_query, caplog):
    bare = svc(ONCALL_QUERY, name="bare", query="2CZC_BARE")
    del bare["definition"]
    await add_query(bare)
    await add_agent("bare-agent", ina_server("bare"))
    await rebuild()
    assert {(n.kind, n.name) for n in await nodes_of("bare")} == {
        ("source", "bare"), ("query", "2CZC_BARE")}
    before = len(await nodes_of("oncall"))
    async with SessionLocal() as s:
        row = await get_ina_query(s, "oncall")
        row.definition_json = json.dumps({"dimensions": [{"name": 1}]})
        await s.commit()
    await rebuild()
    assert len(await nodes_of("oncall")) == before
    assert "does not validate" in caplog.text


async def test_an_ina_query_of_a_disabled_agent_or_a_disabled_query_is_not_imported():
    await add_query(ONCALL_QUERY)
    await add_agent("off", ina_server("oncall"), enabled=0)
    await rebuild()
    assert await nodes_of() == []
    await add_agent("on", ina_server("oncall"))
    await rebuild()
    assert await nodes_of("oncall")
    async with SessionLocal() as s:
        row = await get_ina_query(s, "oncall")
        row.enabled = 0
        await s.commit()
    assert (await rebuild())["removed"] == 6 and await nodes_of() == []


@pytest.mark.usefixtures("two_services")
async def test_rebuild_of_one_ina_source_and_a_full_rebuild_drop_what_is_gone(ina_query):
    counts = await rebuild(("ina", "oncall"))
    assert counts["imported"] == 6
    assert {n.source_name for n in await nodes_of()} == {"oncall"}  # that source only
    async with SessionLocal() as s:
        await s.execute(delete(AgentConfig).where(AgentConfig.name == "standby"))
        await s.commit()
    assert (await rebuild(("ina", "oncall")))["removed"] == 6
    await add_agent("standby", ina_server("oncall"))
    await rebuild()
    assert {n.source_name for n in await nodes_of()} == {"sales-orders", "billing", "oncall"}
    async with SessionLocal() as s:
        await s.execute(delete(InaQuery))
        await s.commit()
    assert (await rebuild())["removed"] == 6
    assert "oncall" not in {n.source_name for n in await nodes_of()}


async def test_same_object_links_across_kinds_by_name(ina_query, generic_service):
    await add_service(FLIGHTS)
    await add_agent("flights", odata_server("flights"))
    await rebuild()
    query = await node("oncall", "query", "2CZC_ONCALL")
    view = await node("agent-data", "entity", "ZC_ONCALL")
    table = await node("agent-data", "entity", "SCARR")
    carriers = await node("flights", "entity", "Carriers")
    roster = await node("flights", "entity", "OnCall")
    assert [e.kind for e in await edge_between(query, view)] == ["same_object"]  # 2C<view>
    assert [e.kind for e in await edge_between(query, roster)] == ["same_object"]  # <view>Type
    assert [e.kind for e in await edge_between(table, carriers)] == ["same_object"]
    assert await edge_between(view, roster) == []  # generic to odata: the name exactly
    assert await edge_between(table, roster) == []
    assert len(await edges()) == 3
    for source in (("ina", "oncall"), ("odata", "agent-data"), ("odata", "flights")):
        await rebuild(source)  # a re-import of either end keeps one edge per pair
        assert len(await edges()) == 3


@pytest.mark.usefixtures("two_services")
async def test_curated_edges_survive_a_reimport_and_are_not_doubled(ina_query, generic_service):
    await rebuild()
    query = await node("oncall", "query", "2CZC_ONCALL")
    view = await node("agent-data", "entity", "ZC_ONCALL")
    order = await node("sales-orders", "entity", "A_SalesOrder")
    async with SessionLocal() as db:
        db.add(DataCatalogEdge(from_id=view.id, to_id=query.id, kind="same_object",
                               curated_at=utcnow()))
        db.add(DataCatalogEdge(from_id=query.id, to_id=order.id, kind="aggregates",
                               curated_at=utcnow()))
        await db.commit()
    assert len(await edge_between(query, view)) == 2  # the imported one and the admin's
    for source in (("ina", "oncall"), ("odata", "agent-data"), ("odata", "sales-orders"),
                   None):
        await rebuild(source)
        pair = await edge_between(query, view)
        assert len(pair) == 1 and pair[0].curated_at is not None
        assert [e.kind for e in await edge_between(query, order)] == ["aggregates"]
    async with SessionLocal() as s:  # a node that disappears takes its curated edges
        await s.execute(delete(InaQuery))
        await s.commit()
    await rebuild()
    assert all(e.curated_at is None for e in await edges())


def test_the_scope_of_an_agent_holds_its_ina_queries():
    row = AgentConfig(name="a", description="d", instructions="i", mcp_url="builtin:ina",
                      auth_mode="destination",
                      oauth_json=json.dumps({"queries": ["q1", 2, ""]}),
                      extra_servers_json=json.dumps([odata_server("s1")]))
    assert allowed_sources_of(row) == {("ina", "q1"), ("odata", "s1")}
    assert allowed_sources_of(ina_server("q2")) == {("ina", "q2")}


async def test_an_odata_scope_reaches_a_generic_service(generic_service, ina_query):
    """The registry and ``allowed_sources_of`` name a catalogue service
    ``("odata", name)`` whatever its kind; the search finds its nodes."""
    await rebuild()
    async with SessionLocal() as db:
        found = await search.find_sources(
            db, "airline", allowed_sources={("odata", "agent-data")}, tier="basic")
        listed = await search.list_sources(
            db, allowed_sources={("odata", "agent-data")}, tier="basic")
        other = await search.find_sources(
            db, "airline", allowed_sources={("odata", "oncall")}, tier="basic")
    assert found[0]["source_kind"] == "generic" and found[0]["name"] == "SCARR"
    assert found[0]["keys"] == ["CARRID"]
    assert [s["name"] for s in listed] == ["agent-data"]
    assert other == []  # an InA query of that name is not an OData service


@pytest.mark.usefixtures("two_services")
async def test_the_boost_counts_dimensions_and_measures(ina_query):
    await rebuild()
    allowed = {("odata", "sales-orders"), ("odata", "billing"), ("ina", "oncall")}
    async with SessionLocal() as db:
        by_measure = await search.find_sources(
            db, "standby hours", allowed_sources=allowed, tier="basic")
        by_dimension = await search.find_sources(
            db, "per calendar month", allowed_sources=allowed, tier="basic")
    for found in (by_measure, by_dimension):
        assert found[0]["kind"] == "query" and found[0]["name"] == "2CZC_ONCALL"
        assert found[0]["source_kind"] == "ina" and found[0]["keys"] == []
        assert found[0]["purpose"] == "Analyse on-call duty per team"  # the source's
        assert found[0]["not_for"] == "Payroll"


async def test_the_keys_of_a_candidate_come_from_any_child_kind(ina_query):
    await rebuild()
    team = await node("oncall", "dimension", "ZTEAM")
    async with SessionLocal() as db:
        row = await db.get(DataCatalogNode, team.id)
        row.is_key = 1
        await db.commit()
        found = await search.find_sources(
            db, "team", allowed_sources={("ina", "oncall")}, tier="basic")
    assert found[0]["keys"] == ["ZTEAM"]


# --- admin routes: sources of every kind, detail per kind, links ---------------------


@pytest.mark.usefixtures("two_services")
async def test_sources_lists_every_kind(client, ina_query, generic_service):
    spare = svc(ONCALL_QUERY, name="spare", title="Spare query", query="2CZC_SPARE")
    del spare["definition"]
    await add_query(spare)  # registered, attached by nobody: never imported
    await rebuild()
    r = await client.get(f"{API}/sources")
    assert r.status_code == 200
    by_key = {(s["kind"], s["name"]): s for s in r.json()["sources"]}
    assert set(by_key) == {("odata", "sales-orders"), ("odata", "billing"),
                           ("generic", "agent-data"), ("ina", "oncall"), ("ina", "spare")}
    generic = by_key[("generic", "agent-data")]
    assert generic["nodes"] == len(await nodes_of("agent-data")) == 6
    assert generic["last_import"] and generic["used_by"] == ["data"]
    oncall = by_key[("ina", "oncall")]
    assert {k: oncall[k] for k in ("title", "destination", "user_context", "enabled",
                                   "used_by", "nodes", "hint_completeness")} == {
        "title": "On-call analysis", "destination": "SIA_INA", "user_context": True,
        "enabled": True, "used_by": ["standby"], "nodes": 6, "hint_completeness": 0}
    assert oncall["last_import"]
    assert (by_key[("ina", "spare")]["nodes"], by_key[("ina", "spare")]["last_import"],
            by_key[("ina", "spare")]["used_by"]) == (0, None, [])

    async def completeness() -> float:
        listed = (await client.get(f"{API}/sources")).json()["sources"]
        return {(s["kind"], s["name"]): s for s in listed}[("ina", "oncall")][
            "hint_completeness"]

    measure = await node("oncall", "measure", "ZSTANDBY_H")
    r = await client.put(f"{API}/nodes/{measure.id}/hints", json={"synonyms": ["standby"]})
    assert r.status_code == 200, r.text
    assert await completeness() == 0, "a dimension or a measure adds nothing"
    query = await node("oncall", "query", "2CZC_ONCALL")
    r = await client.put(f"{API}/nodes/{query.id}/hints", json={"purpose": "Duty per team"})
    assert r.status_code == 200, r.text
    assert await completeness() == 0.5  # the source and the query count


async def test_the_detail_of_an_ina_source(client, ina_query, generic_service):
    await rebuild()
    r = await client.get(f"{API}/sources/ina/oncall")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"source", "query", "edges", "used_by"}
    assert body["source"]["name"] == "oncall" and body["source"]["source_kind"] == "ina"
    assert body["used_by"] == ["standby"]
    assert body["query"]["node"]["name"] == "2CZC_ONCALL"
    assert [d["name"] for d in body["query"]["dimensions"]] == ["0CALMONTH", "ZTEAM"]
    assert [(m["name"], m["unit"]) for m in body["query"]["measures"]] == [
        ("ZSTANDBY_H", "H"), ("ZCALLOUTS", None)]
    [edge] = body["edges"]  # the imported link to the generic view
    assert (edge["kind"], edge["curated"]) == ("same_object", False) and edge["id"]
    assert {edge["from_node"]["name"], edge["to_node"]["name"]} == {"2CZC_ONCALL",
                                                                    "ZC_ONCALL"}
    assert {edge["from_node"]["source_kind"], edge["to_node"]["source_kind"]} == {
        "ina", "generic"}

    bare = svc(ONCALL_QUERY, name="bare", query="2CZC_BARE")
    del bare["definition"]
    await add_query(bare)
    r = await client.get(f"{API}/sources/ina/bare")  # registered, never imported
    assert r.status_code == 200 and r.json() == {
        "source": None, "query": None, "edges": [], "used_by": []}
    for path in ("sources/ina/nothing", "sources/ina/a%20b"):
        assert (await client.get(f"{API}/{path}")).status_code == 404, path


async def test_the_detail_of_a_generic_source_has_the_odata_shape(client, generic_service):
    await rebuild()
    r = await client.get(f"{API}/sources/generic/agent-data")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"source", "entities", "functions", "edges", "used_by"}
    assert body["source"]["source_kind"] == "generic" and body["used_by"] == ["data"]
    assert [e["node"]["name"] for e in body["entities"]] == ["ZC_ONCALL", "SCARR"]
    assert [f["name"] for f in body["entities"][1]["fields"]] == ["CARRID"]
    assert [f["name"] for f in body["functions"]] == ["MONTH_NAMES_GET"]
    # The catalogue's own handle names the same row, as the rebuild route does.
    again = (await client.get(f"{API}/sources/odata/agent-data")).json()
    assert again["source"]["id"] == body["source"]["id"]


async def test_an_agent_scope_reaches_its_generic_service(client, generic_service):
    await rebuild()
    r = await client.get(f"{API}/graph", params={"agent": "data"})
    assert r.status_code == 200
    assert {n["group"] for n in r.json()["nodes"]} == {"generic:agent-data"}
    r = await client.post(f"{API}/search", json={"question": "airline", "agent": "data"})
    assert r.json()["scope"] == "agent"
    assert r.json()["candidates"][0]["name"] == "SCARR"


@pytest.mark.usefixtures("two_services")
async def test_an_admin_draws_a_link_and_removes_it(client, ina_query, generic_service):
    await rebuild()
    query = await node("oncall", "query", "2CZC_ONCALL")
    order = await node("sales-orders", "entity", "A_SalesOrder")
    view = await node("agent-data", "entity", "ZC_ONCALL")
    [imported] = await edge_between(query, view)
    before = len(await edges())
    r = await client.post(f"{API}/edges",
                          json={"from_id": query.id, "to_id": order.id, "kind": "aggregates"})
    assert r.status_code == 201, r.text
    drawn = r.json()
    assert {k: drawn[k] for k in ("from", "to", "kind", "label", "curated")} == {
        "from": query.id, "to": order.id, "kind": "aggregates", "label": "", "curated": True}
    assert drawn["from_node"]["name"] == "2CZC_ONCALL"
    assert drawn["to_node"]["source_name"] == "sales-orders"
    [stored] = await edge_between(query, order)
    assert stored.id == drawn["id"] and stored.curated_at is not None

    detail = (await client.get(f"{API}/sources/ina/oncall")).json()
    assert {(e["kind"], e["curated"]) for e in detail["edges"]} == {
        ("same_object", False), ("aggregates", True)}
    graph = (await client.get(f"{API}/graph")).json()
    assert all(isinstance(e["curated"], bool) for e in graph["edges"])
    assert [e["curated"] for e in graph["edges"]
            if (e["from"], e["to"]) == (query.id, order.id)] == [True]
    assert [e["curated"] for e in graph["edges"]
            if {e["from"], e["to"]} == {query.id, view.id}] == [False]
    assert not any(e["curated"] for e in graph["edges"] if e["kind"] == "contains")

    # One link per kind per pair, in either direction, imported or drawn.
    for body in ({"from_id": order.id, "to_id": query.id, "kind": "aggregates"},
                 {"from_id": view.id, "to_id": query.id, "kind": "same_object"}):
        r = await client.post(f"{API}/edges", json=body)
        assert r.status_code == 409, body
    assert len(await edges()) == before + 1
    r = await client.post(f"{API}/edges",  # over the imported same_object
                          json={"from_id": view.id, "to_id": query.id, "kind": "aggregates"})
    assert r.status_code == 201, r.text
    assert sorted(e.kind for e in await edge_between(query, view)) == [
        "aggregates", "same_object"]
    r = await client.post(f"{API}/edges",
                          json={"from_id": query.id, "to_id": order.id, "kind": "same_object"})
    assert r.status_code == 201, r.text
    assert len(await edges()) == before + 3

    r = await client.delete(f"{API}/edges/{imported.id}")
    assert r.status_code == 409 and imported.id in {e.id for e in await edges()}
    r = await client.delete(f"{API}/edges/{drawn['id']}")
    assert r.status_code == 204
    assert [e.kind for e in await edge_between(query, order)] == ["same_object"]
    assert (await client.delete(f"{API}/edges/{drawn['id']}")).status_code == 404
    for eid in ("abc", "99999999999", "-1", "0x1", "\u00b2"):
        assert (await client.delete(f"{API}/edges/{eid}")).status_code == 404, eid


@pytest.mark.usefixtures("two_services")
async def test_a_function_can_be_linked(client, generic_service):
    await rebuild()
    month = await node("agent-data", "function", "MONTH_NAMES_GET")
    release = await node("sales-orders", "function", "ReleaseOrder")
    r = await client.post(f"{API}/edges",
                          json={"from_id": month.id, "to_id": release.id, "kind": "same_object"})
    assert r.status_code == 201, r.text


@pytest.mark.usefixtures("two_services")
async def test_link_refusals_name_the_field_never_the_value(client, ina_query):
    await rebuild()
    query = await node("oncall", "query", "2CZC_ONCALL")
    order = await node("sales-orders", "entity", "A_SalesOrder")
    item = await node("sales-orders", "entity", "A_SalesOrderItem")
    field = await node("sales-orders", "field", "SoldToParty")
    source = await node("billing", "source", "billing")
    dimension = await node("oncall", "dimension", "ZTEAM")
    cases = [
        ({"from_id": order.id, "to_id": item.id, "kind": "aggregates"}, 422,
         "to_id: same source as from_id"),
        ({"from_id": order.id, "to_id": order.id, "kind": "aggregates"}, 422,
         "to_id: same source as from_id"),
        ({"from_id": field.id, "to_id": query.id, "kind": "aggregates"}, 422,
         "from_id: not an entity, query or function"),
        ({"from_id": query.id, "to_id": source.id, "kind": "aggregates"}, 422,
         "to_id: not an entity, query or function"),
        ({"from_id": dimension.id, "to_id": order.id, "kind": "aggregates"}, 422,
         "from_id: not an entity, query or function"),
        ({"from_id": 999999, "to_id": order.id, "kind": "aggregates"}, 422,
         "from_id: not a node of the catalogue"),
        ({"from_id": query.id, "to_id": 999999, "kind": "aggregates"}, 422,
         "to_id: not a node of the catalogue"),
        ({"from_id": query.id, "to_id": order.id, "kind": "navigation"}, 422, "kind:"),
        ({"from_id": query.id, "to_id": order.id, "kind": SECRET}, 422, "kind:"),
        ({"from_id": query.id, "to_id": order.id, "kind": 1}, 422, "kind:"),
        ({"from_id": str(query.id), "to_id": order.id, "kind": "aggregates"}, 422, "from_id:"),
        ({"from_id": True, "to_id": order.id, "kind": "aggregates"}, 422, "from_id:"),
        ({"from_id": 0, "to_id": order.id, "kind": "aggregates"}, 422, "from_id:"),
        ({"from_id": query.id, "kind": "aggregates"}, 422, "to_id:"),
        ({"from_id": query.id, "to_id": order.id, "kind": "aggregates", "label": SECRET},
         422, "label:"),
    ]
    for body, status, detail in cases:
        r = await client.post(f"{API}/edges", json=body)
        assert r.status_code == status, (body, r.text)
        assert r.json()["detail"].startswith(detail), (body, r.text)
        assert SECRET not in r.text and "999999" not in r.text
    for raw in (b"[1]", b"not json", b'"x"'):
        r = await client.post(f"{API}/edges", content=raw,
                              headers={"content-type": "application/json"})
        assert r.status_code == 422 and r.json()["detail"] == "body: expected a JSON object"
    assert all(e.curated_at is None for e in await edges())


@pytest.mark.usefixtures("two_services")
async def test_a_drawn_link_is_named_in_the_relations_like_an_imported_one(
    client, ina_query
):
    await rebuild()
    query = await node("oncall", "query", "2CZC_ONCALL")
    order = await node("sales-orders", "entity", "A_SalesOrder")
    r = await client.post(f"{API}/edges",
                          json={"from_id": query.id, "to_id": order.id, "kind": "aggregates"})
    assert r.status_code == 201, r.text
    allowed = {("ina", "oncall"), ("odata", "sales-orders")}
    async with SessionLocal() as db:
        by_query = await search.find_sources(
            db, "standby hours", allowed_sources=allowed, tier="basic")
        narrow = await search.find_sources(
            db, "standby hours", allowed_sources={("ina", "oncall")}, tier="basic")
    assert by_query[0]["name"] == "2CZC_ONCALL"
    assert {"kind": "aggregates", "to": {"source_name": "sales-orders", "name": "A_SalesOrder",
                                         "label": order.label}} in by_query[0]["relations"]
    assert narrow[0]["relations"] == []  # the other end is outside that scope


async def test_hints_on_query_dimension_and_measure_nodes(client, ina_query):
    await rebuild()
    query = await node("oncall", "query", "2CZC_ONCALL")
    team = await node("oncall", "dimension", "ZTEAM")
    hours = await node("oncall", "measure", "ZSTANDBY_H")
    r = await client.put(f"{API}/nodes/{query.id}/hints", json={"label": "On-call duty"})
    assert r.status_code == 200 and r.json()["label"] == "On-call duty"
    r = await client.put(f"{API}/nodes/{team.id}/hints", json={"synonyms": ["crew"]})
    assert r.status_code == 200 and r.json()["synonyms"] == ["crew"]
    r = await client.put(f"{API}/nodes/{hours.id}/hints", json={"readable": False})
    assert r.status_code == 200 and r.json()["readable"] is False
    await rebuild()
    assert (await node("oncall", "measure", "ZSTANDBY_H")).readable == 0  # narrowed, kept
    assert (await node("oncall", "query", "2CZC_ONCALL")).label == "On-call duty"
    async with SessionLocal() as db:
        found = await search.find_sources(
            db, "standby hours", allowed_sources={("ina", "oncall")}, tier="basic")
        by_crew = await search.find_sources(
            db, "crew", allowed_sources={("ina", "oncall")}, tier="basic")
    assert found == []  # a measure that is not readable boosts nothing
    assert by_crew[0]["name"] == "2CZC_ONCALL"
    # Back up to what the description allows: an InA member is readable.
    r = await client.put(f"{API}/nodes/{hours.id}/hints", json={"readable": True})
    assert r.status_code == 200 and r.json()["readable"] is True
    r = await client.put(f"{API}/nodes/{team.id}/hints", json={"readable": False})
    assert r.status_code == 200
    async with SessionLocal() as s:  # the registration is gone, its nodes not yet
        await s.execute(delete(InaQuery))
        await s.commit()
    r = await client.put(f"{API}/nodes/{team.id}/hints", json={"readable": True})
    assert r.status_code == 422 and r.json()["detail"].startswith("readable:")


async def test_a_generic_node_is_widened_back_up_to_the_definition_only(client):
    hidden = svc(GENERIC_DATA)
    hidden["definition"]["entity_sets"][1]["fields"].append(
        {"name": "INTERNAL", "label": "Internal", "selectable": False})
    await add_service(hidden)
    await add_agent("data", odata_server("agent-data"))
    await rebuild()
    carrid = await node("agent-data", "field", "CARRID")
    internal = await node("agent-data", "field", "INTERNAL")
    assert carrid.source_kind == "generic" and internal.readable == 0
    r = await client.put(f"{API}/nodes/{carrid.id}/hints", json={"readable": False})
    assert r.status_code == 200
    r = await client.put(f"{API}/nodes/{carrid.id}/hints", json={"readable": True})
    assert r.status_code == 200 and r.json()["readable"] is True
    r = await client.put(f"{API}/nodes/{internal.id}/hints", json={"readable": True})
    assert r.status_code == 422 and r.json()["detail"].startswith("readable:")


def test_the_node_lock_compiles_for_hana_and_postgres():
    """The suite runs on SQLite, where `api_create_edge` takes no row lock:
    the statement of the other two databases is checked by compiling it."""
    from sqlalchemy.dialects import postgresql

    from agents.data_catalog import routes
    from tests.hana_sql import hana_sql, refusals

    stmt = routes._lock_nodes_stmt([7, 3])
    assert refusals(stmt) == []
    sql = hana_sql(stmt)
    assert sql.startswith("SELECT data_catalog_nodes.id FROM data_catalog_nodes WHERE")
    assert sql.endswith("ORDER BY data_catalog_nodes.id FOR UPDATE")
    pg = " ".join(str(stmt.compile(dialect=postgresql.dialect())).split())
    assert pg.endswith("ORDER BY data_catalog_nodes.id FOR UPDATE")
