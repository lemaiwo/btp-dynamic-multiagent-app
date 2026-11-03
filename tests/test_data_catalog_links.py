"""The links of the data catalogue across kinds (addendum 2026-11-03 (k)).

A catalogue service whose kind changes (``odata`` <-> ``generic``) keeps its
nodes in place, so the admin's hints, a narrowed ``readable``, vectors and
curated edges survive; the cross-kind ``same_object`` rules do not join
names that only look alike (rule (b) is exact, ``2C<view>`` needs the view
name whole); a generic entity is the object its entity TYPE names (SAP's
``<OBJ>Set`` of ``ZAGENT_DATA_SRV.<OBJ>``), never its set name; a relation
to a source outside the caller's scope is dropped;
one link per kind per pair: a curated ``same_object`` keeps the import from
adding its own beside it, a curated ``aggregates`` does not. The helpers and fixtures are those of
``tests/test_data_catalog.py``: ``_clean`` (autouse) is imported so that
pytest finds it here by name.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.data_catalog import embeddings, search  # noqa: E402
from agents.data_catalog.models import (  # noqa: E402
    DataCatalogEdge,
    DataCatalogNode,
    DataCatalogVector,
    utcnow,
)
from agents.db import SessionLocal, create_ina_query  # noqa: E402
from agents.odata.metadata import parse_metadata  # noqa: E402
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
)

pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")

# The recorded `$metadata` of A4H's ZAGENT_DATA_SRV.
RECORDED = ROOT / "tests" / "fixtures" / "odata" / "zagent_data_metadata.xml"


def entity_set(name: str, entity_type: str, key: str = "ID") -> dict[str, Any]:
    return {"name": name, "title": name, "entity_type": entity_type, "keys": [{"name": key}],
            "operations": ["list"],
            "fields": [{"name": key, "label": f"{name} key", "selectable": True}]}


def generic(name: str, *sets: dict[str, Any]) -> dict[str, Any]:
    return svc(BILLING, name=name, kind="generic", title=f"Generic {name}",
               service_path="/sap/opu/odata/sap/ZAGENT_DATA_SRV",
               definition={"entity_sets": list(sets)})


def odata(name: str, *sets: dict[str, Any]) -> dict[str, Any]:
    return svc(BILLING, name=name, title=f"OData {name}", definition={"entity_sets": list(sets)})


def ina_server(*names: str) -> dict[str, Any]:
    return {"url": "builtin:ina", "auth_mode": "destination", "oauth": {"queries": list(names)}}


async def add_query(name: str, query: str) -> None:
    async with SessionLocal() as s:
        await create_ina_query(s, {
            "name": name, "title": f"Query {name}", "purpose": "Analyse on-call duty",
            "destination": "SIA_INA", "query": query,
            "definition": {"measures": [{"name": "ZSTANDBY_H", "label": "Standby hours"}]},
        })
    await add_agent(f"ina-{name}", ina_server(name))


async def attach(data: dict[str, Any]) -> None:
    await add_service(data)
    await add_agent(f"agent-{data['name']}", odata_server(data["name"]))


async def pair(a: DataCatalogNode, b: DataCatalogNode) -> list[DataCatalogEdge]:
    return [e for e in await edges() if {e.from_id, e.to_id} == {a.id, b.id}]


# Named as ZCL_AGENT_DATA_MODEL names them: `<OBJ>Set` of `ZAGENT_DATA_SRV.<OBJ>`.
DATA = generic(
    "agent-data",
    entity_set("SCARRSet", "ZAGENT_DATA_SRV.SCARR", "CARRID"),
    entity_set("ZC_ONCALLSet", "ZAGENT_DATA_SRV.ZC_ONCALL"),
)


async def test_a_kind_flip_keeps_hints_vectors_and_curated_edges():
    await attach(DATA)
    await add_query("oncall", "2CZC_ONCALL")
    await rebuild()
    carrier = await node("agent-data", "entity", "SCARRSet")
    key = await node("agent-data", "field", "CARRID")
    query = await node("oncall", "query", "2CZC_ONCALL")
    async with SessionLocal() as db:
        row = await db.get(DataCatalogNode, carrier.id)
        row.label, row.curated_at = "Airline master", utcnow()
        row = await db.get(DataCatalogNode, key.id)
        row.readable, row.curated_at = 0, utcnow()  # the admin narrowed it
        db.add(DataCatalogVector(node_id=carrier.id, model="m", text_hash="h",
                                 vector=embeddings.pack([1.0])))
        db.add(DataCatalogEdge(from_id=carrier.id, to_id=query.id, kind="aggregates",
                               curated_at=utcnow()))
        await db.commit()

    async def unchanged(kind: str) -> None:
        after = await node("agent-data", "entity", "SCARRSet")
        assert (after.id, after.source_kind, after.label) == (carrier.id, kind, "Airline master")
        field = await node("agent-data", "field", "CARRID")
        assert (field.id, field.readable) == (key.id, 0)
        assert {n.source_kind for n in await nodes_of("agent-data")} == {kind}
        async with SessionLocal() as db:
            assert await db.get(DataCatalogVector, (carrier.id, "m")) is not None
        assert [e.kind for e in await pair(after, query) if e.curated_at] == ["aggregates"]

    await replace_service(svc(DATA, kind="odata"))
    counts = await rebuild(("odata", "agent-data"))
    assert counts["removed"] == 0
    await unchanged("odata")
    await replace_service(copy.deepcopy(DATA))
    counts = await rebuild()  # the full rebuild follows the flip back
    assert counts["removed"] == 0
    await unchanged("generic")
    await replace_service(svc(DATA, kind="odata"))
    await rebuild(("generic", "agent-data"))  # either handle names the row
    await unchanged("odata")


async def test_names_that_only_look_alike_are_not_joined():
    # The generic sets as SAP names them: the object is the type's local name.
    await attach(generic("tables", entity_set("PRODUCTSet", "G.PRODUCT"),
                         entity_set("SCARRSet", "G.SCARR"), entity_set("FOOBARSet", "G.FOOBAR")))
    await attach(odata("api", entity_set("Products", "API.Product"),
                       entity_set("Carriers", "API.SCARRType"),
                       entity_set("Foos", "API.FooType")))
    await add_query("foo", "2CFOO")
    await rebuild()
    product = await node("tables", "entity", "PRODUCTSet")
    carrier = await node("tables", "entity", "SCARRSet")
    foobar = await node("tables", "entity", "FOOBARSet")
    query = await node("foo", "query", "2CFOO")
    assert await pair(product, await node("api", "entity", "Products")) == []  # case: (b) exact
    assert await pair(carrier, await node("api", "entity", "Carriers")) == []  # SCARR vs SCARRType
    assert await pair(query, foobar) == []  # 2CFOO is FOO, not FOOBAR
    foos = await node("api", "entity", "Foos")
    assert [e.kind for e in await pair(query, foos)] == ["same_object"]  # (a): FooType
    assert len(await edges()) == 1


async def test_an_ina_query_joins_the_generic_set_of_its_view():
    """Rule (a) on SAP's generic naming: the InA query ``2CZC_ONCALL`` is the
    CDS view ``ZC_ONCALL``, which the generic service serves as the set
    ``ZC_ONCALLSet`` of the type ``ZAGENT_DATA_SRV.ZC_ONCALL``. The set's own
    name is no object name, and a set without a type names no object."""
    await attach(generic("agent-data", entity_set("ZC_ONCALLSet", "ZAGENT_DATA_SRV.ZC_ONCALL"),
                         entity_set("ZC_ROSTERSet", "")))
    await add_query("oncall", "2CZC_ONCALL")
    await add_query("by-set-name", "2CZC_ONCALLSET")
    await add_query("untyped", "2CZC_ROSTERSET")
    await rebuild()
    view = await node("agent-data", "entity", "ZC_ONCALLSet")
    [edge] = await pair(view, await node("oncall", "query", "2CZC_ONCALL"))
    assert (edge.kind, edge.label, edge.curated_at) == ("same_object", "ZC_ONCALL", None)
    assert await pair(view, await node("by-set-name", "query", "2CZC_ONCALLSET")) == []
    roster = await node("agent-data", "entity", "ZC_ROSTERSet")
    assert await pair(roster, await node("untyped", "query", "2CZC_ROSTERSET")) == []
    assert len(await edges()) == 1


async def test_a_generic_set_joins_the_odata_type_of_its_object():
    """Rule (b) on the recorded naming: the generic set ``SCARRSet`` of the
    type ``ZAGENT_DATA_SRV.SCARR`` and an odata entity whose type's local
    name is ``SCARR`` are one object; an odata type named like the SET is
    not, nor one of another case (rule (b) is exact)."""
    recorded = {e.name: e.entity_type
                for e in parse_metadata(RECORDED.read_bytes(), "v2").entity_sets}
    assert recorded["SCARRSet"] == "ZAGENT_DATA_SRV.SCARR"
    await attach(generic("agent-data", entity_set("SCARRSet", recorded["SCARRSet"], "CARRID")))
    await attach(odata("flights", entity_set("Carriers", "ZFLIGHT_SRV.SCARR"),
                       entity_set("CarrierSets", "ZFLIGHT_SRV.SCARRSet"),
                       entity_set("Airlines", "ZFLIGHT_SRV.Scarr")))
    await rebuild()
    table = await node("agent-data", "entity", "SCARRSet")
    [edge] = await pair(table, await node("flights", "entity", "Carriers"))
    assert (edge.kind, edge.curated_at) == ("same_object", None)
    assert await pair(table, await node("flights", "entity", "CarrierSets")) == []
    assert await pair(table, await node("flights", "entity", "Airlines")) == []
    assert len(await edges()) == 1


async def test_a_relation_to_a_source_outside_the_scope_is_dropped():
    await attach(DATA)
    await attach(odata("flights", entity_set("Carriers", "ZFLIGHT_SRV.SCARR"),
                       entity_set("Roster", "ZFLIGHT_SRV.ZC_OnCallType")))
    await add_query("oncall", "2CZC_ONCALL")
    await rebuild()
    assert len(await edges()) == 3  # SCARR, and the query to both on-call entities

    async def relations(allowed: set[tuple[str, str]], question: str) -> dict[str, list]:
        async with SessionLocal() as db:
            found = await search.find_sources(db, question, allowed_sources=allowed,
                                              tier="basic", k=10)
        return {c["name"]: c["relations"] for c in found}

    alone = await relations({("odata", "flights")}, "carriers roster")
    assert alone == {"Carriers": [], "Roster": []}
    query_alone = await relations({("ina", "oncall")}, "standby hours")
    assert query_alone == {"oncall": []}  # the registration's slug, never the InA name
    together = await relations({("odata", "flights"), ("odata", "agent-data"),
                                ("ina", "oncall")}, "carriers roster")
    assert {r["to"]["name"] for r in together["Roster"]} == {"oncall"}
    assert {r["to"]["source_name"] for r in together["Carriers"]} == {"agent-data"}


async def test_a_curated_aggregates_edge_leaves_the_imported_same_object():
    await attach(DATA)
    await add_query("oncall", "2CZC_ONCALL")
    await rebuild()
    view = await node("agent-data", "entity", "ZC_ONCALLSet")
    query = await node("oncall", "query", "2CZC_ONCALL")
    assert [e.kind for e in await pair(view, query)] == ["same_object"]
    async with SessionLocal() as db:
        db.add(DataCatalogEdge(from_id=query.id, to_id=view.id, kind="aggregates",
                               curated_at=utcnow()))
        await db.commit()
    for source in (("ina", "oncall"), ("odata", "agent-data"), None):
        await rebuild(source)
        assert sorted((e.kind, e.curated_at is not None) for e in await pair(view, query)) == [
            ("aggregates", True), ("same_object", False)]


async def test_a_curated_same_object_edge_keeps_out_the_imported_one():
    await attach(DATA)
    await add_query("oncall", "2CZC_ONCALL")
    await rebuild()
    view = await node("agent-data", "entity", "ZC_ONCALLSet")
    query = await node("oncall", "query", "2CZC_ONCALL")
    async with SessionLocal() as db:
        db.add(DataCatalogEdge(from_id=query.id, to_id=view.id, kind="same_object",
                               curated_at=utcnow()))
        await db.commit()
    for source in (("ina", "oncall"), ("odata", "agent-data"), None):
        await rebuild(source)
        assert [(e.kind, e.curated_at is not None) for e in await pair(view, query)] == [
            ("same_object", True)]
