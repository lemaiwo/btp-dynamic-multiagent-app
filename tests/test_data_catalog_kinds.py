"""The data catalogue for every kind (addendum 2026-11-03 (k)).

A ``generic`` catalogue service imported under its own kind, an InA query as
one ``query`` node with its ``dimension`` and ``measure`` nodes, the
cross-kind ``same_object`` links by name, curated edges kept across
re-imports, the scope of a ``builtin:ina`` entry, an ``("odata", name)``
scope that reaches a generic service, and the search boost and keys over
dimensions and measures. The helpers and fixtures are those of
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
from sqlalchemy import delete

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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
    BILLING,
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
