"""The data catalogue graph (addendum 2026-11-01 (j), part 1).

Import of OData catalogue services into nodes and edges, the admin's hints
(inherited, preserved across re-imports, never widening what the catalogue
lets agents read), the scope filter that runs before any scoring, the
lexical fallback and the vector path, the tier rule, the incremental
rebuild, every admin route, the agent tools and their attachment in the
registry. No network: embedding calls are patched or switched off
(``DATA_CATALOG_EMBEDDINGS=0`` from ``tests/conftest.py``).

The other kinds of addendum (k) (``generic``, InA, cross-kind links, curated
edges) are in ``tests/test_data_catalog_kinds.py``, which reuses the helpers
and fixtures here (``_clean`` empties ``ina_queries`` for it).
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, insert, select

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ["DATA_CATALOG_EMBEDDINGS"] = "0"

from pydantic_ai.messages import ModelResponse, TextPart  # noqa: E402
from pydantic_ai.models.function import FunctionModel  # noqa: E402
from pydantic_ai.models.test import TestModel  # noqa: E402

import agents.registry as registry_module  # noqa: E402
import app as app_module  # noqa: E402
from agents import auth  # noqa: E402
from agents.access import current_access  # noqa: E402
from agents.data_catalog import embeddings, importer, search  # noqa: E402
from agents.data_catalog import routes as dc_routes  # noqa: E402
from agents.data_catalog.models import (  # noqa: E402
    DataCatalogEdge,
    DataCatalogNode,
    DataCatalogVector,
)
from agents.data_catalog.scope import allowed_sources_of  # noqa: E402
from agents.data_catalog.tool import NO_MATCH_HINT, data_catalog_toolset  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    InaQuery,
    ODataService,
    SessionLocal,
    create_odata_service,
    get_odata_service,
    init_db,
    update_odata_service,
    validate_odata_service,
)

pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")

API = "/admin/api/data-catalog"
SECRET = "secret-token-value"

ORDERS: dict[str, Any] = {
    "name": "sales-orders",
    "title": "Sales orders",
    "purpose": "Read sales orders and their items",
    "not_for": "Purchase orders",
    "destination": "S4_ODATA_USER",
    "user_context": True,
    "odata_version": "v2",
    "service_path": "/sap/opu/odata/sap/API_SALES_ORDER_SRV",
    "definition": {
        "entity_sets": [
            {
                "name": "A_SalesOrder",
                "title": "Sales order",
                "entity_type": "API_SALES_ORDER_SRV.A_SalesOrderType",
                "keys": [{"name": "SalesOrder"}],
                "operations": ["list", "get", "update"],
                "fields": [
                    {"name": "SalesOrder", "label": "Order", "selectable": True,
                     "filterable": True},
                    {"name": "SoldToParty", "label": "Customer", "selectable": True},
                    {"name": "DeliveryBlock", "label": "Delivery block", "selectable": True,
                     "writable": True},
                    {"name": "InternalNote", "label": "Internal note"},
                ],
                "navigations": [
                    {"name": "to_Item", "target": "A_SalesOrderItem", "collection": True}
                ],
            },
            {
                "name": "A_SalesOrderItem",
                "title": "Sales order item",
                "description": "One row per order item with material and quantity",
                "keys": [{"name": "SalesOrder"}, {"name": "SalesOrderItem"}],
                "operations": ["list"],
                "fields": [
                    {"name": "SalesOrder", "selectable": True},
                    {"name": "SalesOrderItem", "selectable": True},
                    {"name": "Material", "label": "Material", "selectable": True},
                ],
            },
        ],
        "operations": [
            {"name": "ReleaseOrder", "kind": "function_import", "http_method": "POST",
             "enabled": True, "title": "Release a sales order"},
            {"name": "HiddenOp", "kind": "function_import", "http_method": "GET"},
        ],
    },
}

BILLING: dict[str, Any] = {
    "name": "billing",
    "title": "Billing documents",
    "purpose": "Invoices and credit memos",
    "destination": "S4_ODATA_USER",
    "user_context": True,
    "odata_version": "v2",
    "service_path": "/sap/opu/odata/sap/API_BILLING_DOCUMENT_SRV",
    "definition": {
        "entity_sets": [
            {
                "name": "A_BillingDocument",
                "title": "Invoice",
                "entity_type": "OTHER.A_SalesOrderType",
                "keys": [{"name": "BillingDocument"}],
                "operations": ["list"],
                "fields": [
                    {"name": "BillingDocument", "label": "Invoice number", "selectable": True},
                    {"name": "InvoiceAmount", "label": "Invoice amount", "selectable": True},
                ],
            }
        ]
    },
}


def svc(base: dict[str, Any], **patch: Any) -> dict[str, Any]:
    data = copy.deepcopy(base)
    data.update(patch)
    return data


def odata_server(*names: str) -> dict[str, Any]:
    return {"url": "builtin:odata", "auth_mode": "destination", "oauth": {"services": list(names)}}


async def add_service(data: dict[str, Any]) -> None:
    async with SessionLocal() as s:
        await create_odata_service(s, validate_odata_service(copy.deepcopy(data)))
        await s.commit()


async def replace_service(data: dict[str, Any]) -> None:
    async with SessionLocal() as s:
        row = await get_odata_service(s, data["name"])
        await update_odata_service(s, row, validate_odata_service(copy.deepcopy(data)))
        await s.commit()


async def add_agent(
    name: str, *servers: dict, enabled: int = 1, tier: str = "basic"
) -> None:
    primary, extras = servers[0], list(servers[1:])
    async with SessionLocal() as s:
        s.add(
            AgentConfig(
                name=name,
                description="d",
                instructions="You help.",
                mcp_url=primary["url"],
                auth_mode=primary["auth_mode"],
                oauth_json=json.dumps(primary["oauth"]) if primary.get("oauth") else None,
                extra_servers_json=json.dumps(extras) if extras else None,
                enabled=enabled,
                tier=tier,
            )
        )
        await s.commit()


async def rebuild(source: tuple[str, str] | None = None) -> dict[str, int]:
    async with SessionLocal() as db:
        return await importer.rebuild(db, source)


async def nodes_of(source_name: str | None = None) -> list[DataCatalogNode]:
    async with SessionLocal() as db:
        q = select(DataCatalogNode).order_by(DataCatalogNode.id)
        if source_name:
            q = q.where(DataCatalogNode.source_name == source_name)
        return list((await db.execute(q)).scalars())


async def node(source_name: str, kind: str, name: str) -> DataCatalogNode:
    found = [n for n in await nodes_of(source_name) if n.kind == kind and n.name == name]
    assert len(found) == 1, (source_name, kind, name)
    return found[0]


async def edges() -> list[DataCatalogEdge]:
    async with SessionLocal() as db:
        return list((await db.execute(select(DataCatalogEdge))).scalars())


@pytest.fixture(autouse=True)
async def _clean(monkeypatch):
    await init_db()
    async with SessionLocal() as s:
        for model in (DataCatalogEdge, DataCatalogVector, DataCatalogNode, ODataService,
                      InaQuery, AgentConfig):
            await s.execute(delete(model))
        await s.commit()
    embeddings.reset_caches()
    monkeypatch.setattr(registry_module, "get_model", lambda *a, **k: TestModel())
    yield
    embeddings.reset_caches()


@pytest.fixture
async def two_services():
    await add_service(ORDERS)
    await add_service(BILLING)
    await add_agent("sales", odata_server("sales-orders"))
    await add_agent("finance", odata_server("billing"))


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app_module.app),
                           base_url="http://test") as c:
        yield c


# --- import -----------------------------------------------------------------------


async def test_a_service_becomes_source_entity_field_and_function_nodes(two_services):
    counts = await rebuild()
    assert counts["removed"] == 0 and counts["vectors"] == 0
    source = await node("sales-orders", "source", "sales-orders")
    assert (source.label, source.purpose, source.not_for) == (
        "Sales orders", "Read sales orders and their items", "Purchase orders")
    assert source.parent_id is None and source.writable and source.readable
    order = await node("sales-orders", "entity", "A_SalesOrder")
    assert order.parent_id == source.id and order.label == "Sales order"
    assert (order.readable, order.writable) == (1, 1)
    item = await node("sales-orders", "entity", "A_SalesOrderItem")
    assert (item.readable, item.writable) == (1, 0)
    assert item.purpose.startswith("One row per order item")
    fields = {n.name: n for n in await nodes_of("sales-orders")
              if n.kind == "field" and n.parent_id == order.id}
    assert set(fields) == {"SalesOrder", "SoldToParty", "DeliveryBlock", "InternalNote"}
    assert fields["SalesOrder"].is_key and not fields["SoldToParty"].is_key
    assert fields["SoldToParty"].label == "Customer"  # the SAP label
    assert fields["InternalNote"].readable == 0  # not selectable
    assert fields["DeliveryBlock"].writable == 1 and fields["SoldToParty"].writable == 0
    functions = [n for n in await nodes_of("sales-orders") if n.kind == "function"]
    assert [f.name for f in functions] == ["ReleaseOrder"]  # the disabled one is absent
    assert (functions[0].readable, functions[0].writable) == (0, 1)
    total = await rebuild()
    assert total["imported"] == 0  # a second rebuild changes nothing


async def test_navigation_and_same_object_edges(two_services):
    await rebuild()
    order = await node("sales-orders", "entity", "A_SalesOrder")
    item = await node("sales-orders", "entity", "A_SalesOrderItem")
    invoice = await node("billing", "entity", "A_BillingDocument")
    kinds = {(e.kind, frozenset((e.from_id, e.to_id))) for e in await edges()}
    assert ("navigation", frozenset((order.id, item.id))) in kinds
    assert ("same_object", frozenset((order.id, invoice.id))) in kinds
    assert len(await edges()) == 2
    await rebuild(("odata", "billing"))  # re-import of one side keeps one edge
    assert len([e for e in await edges() if e.kind == "same_object"]) == 1


async def test_an_edge_is_not_curated_unless_an_admin_drew_it(two_services):
    """``curated_at`` (generation 9) marks a link an admin drew; the edges an
    import makes, and a row written without it, leave it NULL."""
    assert DataCatalogEdge.__table__.c.curated_at.nullable is True
    await rebuild()
    assert [e.curated_at for e in await edges()] == [None, None]
    order = await node("sales-orders", "entity", "A_SalesOrder")
    invoice = await node("billing", "entity", "A_BillingDocument")
    async with SessionLocal() as db:
        db.add(DataCatalogEdge(from_id=order.id, to_id=invoice.id, kind="aggregates"))
        await db.commit()
    assert {e.kind: e.curated_at for e in await edges()}["aggregates"] is None


async def test_only_sources_of_enabled_agents_and_enabled_services_are_imported():
    await add_service(ORDERS)
    await add_service(BILLING)
    await add_agent("off", odata_server("billing"), enabled=0)
    await add_agent("sales", odata_server("sales-orders"))
    await rebuild()
    assert {n.source_name for n in await nodes_of()} == {"sales-orders"}
    await replace_service(svc(ORDERS, enabled=False))
    counts = await rebuild()
    assert counts["removed"] > 0 and await nodes_of() == []


async def test_a_definition_that_no_longer_validates_leaves_the_nodes(two_services, caplog):
    await rebuild()
    before = len(await nodes_of("sales-orders"))
    async with SessionLocal() as s:
        row = await get_odata_service(s, "sales-orders")
        row.definition_json = json.dumps({"entity_sets": [{"name": 1}]})
        await s.commit()
    await rebuild()
    assert len(await nodes_of("sales-orders")) == before
    assert "does not validate" in caplog.text and SECRET not in caplog.text


# --- incremental rebuild and hints ---------------------------------------------------


async def test_unchanged_nodes_keep_their_row_and_stamp_removed_nodes_go(two_services):
    await rebuild()
    order = await node("sales-orders", "entity", "A_SalesOrder")
    material = await node("sales-orders", "field", "Material")
    async with SessionLocal() as db:
        db.add(DataCatalogVector(node_id=material.id, model="m", text_hash="h",
                                 vector=embeddings.pack([1.0])))
        db.add(DataCatalogEdge(from_id=material.id, to_id=order.id, kind="association"))
        await db.commit()
    changed = svc(ORDERS)
    item_fields = changed["definition"]["entity_sets"][1]["fields"]
    changed["definition"]["entity_sets"][1]["fields"] = item_fields[:2]
    changed["definition"]["entity_sets"][0]["title"] = "Sales order header"
    await replace_service(changed)
    counts = await rebuild(("odata", "sales-orders"))
    assert counts == {"imported": 1, "removed": 1, "vectors": 0}
    after = await node("sales-orders", "entity", "A_SalesOrder")
    assert after.id == order.id and after.label == "Sales order header"
    assert not [n for n in await nodes_of() if n.name == "Material"]
    async with SessionLocal() as db:
        assert (await db.execute(select(func.count()).select_from(DataCatalogVector))
                ).scalar() == 0
    assert all(material.id not in (e.from_id, e.to_id) for e in await edges())
    sold_to = await node("sales-orders", "field", "SoldToParty")
    stamp = sold_to.updated_at
    await rebuild()
    assert (await node("sales-orders", "field", "SoldToParty")).updated_at == stamp


async def test_hints_survive_a_reimport_and_uncurated_labels_follow_sap(client, two_services):
    await rebuild()
    order = await node("sales-orders", "entity", "A_SalesOrder")
    r = await client.put(f"{API}/nodes/{order.id}/hints", json={
        "label": "Customer order", "purpose": "Header data of customer orders",
        "synonyms": ["SO", "order", "SO"], "examples": ["Which orders are blocked?"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["synonyms"] == ["SO", "order"] and body["curated_at"]
    changed = svc(ORDERS)
    changed["definition"]["entity_sets"][0]["title"] = "SAP title changed"
    changed["definition"]["entity_sets"][0]["fields"][1]["label"] = "Sold-to party"
    await replace_service(changed)
    await rebuild()
    again = await node("sales-orders", "entity", "A_SalesOrder")
    assert (again.label, again.purpose, again.synonyms) == (
        "Customer order", "Header data of customer orders", ["SO", "order"])
    assert again.examples == ["Which orders are blocked?"]
    assert (await node("sales-orders", "field", "SoldToParty")).label == "Sold-to party"


async def test_hints_inherit_from_the_source(two_services):
    await rebuild()
    async with SessionLocal() as db:
        found = await search.find_sources(
            db, "sales order", allowed_sources={("odata", "sales-orders")}, tier="basic")
    order = next(c for c in found if c["name"] == "A_SalesOrder")
    assert order["purpose"] == "Read sales orders and their items"  # the source's
    assert order["not_for"] == "Purchase orders"
    item = next(c for c in found if c["name"] == "A_SalesOrderItem")
    assert item["purpose"].startswith("One row per order item")  # its own


async def test_readable_can_be_narrowed_never_widened(client, two_services):
    await rebuild()
    hidden = await node("sales-orders", "field", "InternalNote")
    r = await client.put(f"{API}/nodes/{hidden.id}/hints", json={"readable": True})
    assert r.status_code == 422 and r.json()["detail"].startswith("readable:")
    shown = await node("sales-orders", "field", "SoldToParty")
    r = await client.put(f"{API}/nodes/{shown.id}/hints", json={"readable": False})
    assert r.status_code == 200 and r.json()["readable"] is False
    await rebuild()
    assert (await node("sales-orders", "field", "SoldToParty")).readable == 0
    r = await client.put(f"{API}/nodes/{shown.id}/hints", json={"readable": True})
    assert r.status_code == 200 and r.json()["readable"] is True


# --- search: scope, ranking, vectors, tier --------------------------------------------


async def test_the_scope_is_applied_before_scoring_and_nothing_leaks(two_services, monkeypatch):
    await rebuild()
    scored: list[str] = []
    real = search._lexical

    def spy(tokens, n, fields, source):
        scored.append(n.source_name)
        return real(tokens, n, fields, source)

    monkeypatch.setattr(search, "_lexical", spy)
    async with SessionLocal() as db:
        found = await search.find_sources(
            db, "invoice amount billing document sales order",
            allowed_sources={("odata", "sales-orders")}, tier="basic", k=1)
    assert set(scored) == {"sales-orders"}  # billing was never scored
    assert len(found) == 1 and found[0]["source_name"] == "sales-orders"
    text = json.dumps(found)
    assert "billing" not in text and "Invoice" not in text  # the same_object edge is dropped


async def test_unscoped_and_empty_scope(two_services):
    await rebuild()
    async with SessionLocal() as db:
        assert await search.find_sources(db, "invoice", allowed_sources=set(),
                                         tier="advanced") == []
        found = await search.find_sources(
            db, "invoice amount", allowed_sources=await importer.stored_sources(db),
            tier="advanced")
    assert found[0]["name"] == "A_BillingDocument"
    assert found[0]["keys"] == ["BillingDocument"]
    assert found[0]["relations"] == [
        {"kind": "same_object",
         "to": {"source_name": "sales-orders", "name": "A_SalesOrder", "label": "Sales order"}}]


async def test_lexical_fallback_ranks_by_words_without_a_model(two_services):
    await rebuild()
    async with SessionLocal() as db:
        found = await search.find_sources(
            db, "Which materials are on the order items?",
            allowed_sources={("odata", "sales-orders"), ("odata", "billing")}, tier="basic")
        nothing = await search.find_sources(
            db, "weather forecast tomorrow",
            allowed_sources={("odata", "sales-orders")}, tier="basic")
    assert found[0]["name"] == "A_SalesOrderItem"
    assert found[0]["keys"] == ["SalesOrder", "SalesOrderItem"]
    assert 0 < found[0]["score"] <= 1
    assert nothing == []


async def test_a_hidden_field_adds_nothing_to_the_score(two_services):
    await rebuild()
    async with SessionLocal() as db:
        found = await search.find_sources(
            db, "internal note", allowed_sources={("odata", "sales-orders")}, tier="basic")
    assert found == []


def _fake_vector(text: str) -> list[float]:
    """Three dimensions: billing-ish, order-ish, anything else."""
    low = text.lower()
    return [1.0 if "invoice" in low or "bill" in low else 0.0,
            1.0 if "order" in low else 0.0, 0.1]


async def test_vectors_are_made_once_and_rank_by_cosine(two_services, monkeypatch):
    calls: list[list[str]] = []

    async def model():
        return "emb-model"

    async def create(model_name, texts):
        calls.append(list(texts))
        return [_fake_vector(t) for t in texts]

    monkeypatch.setattr(embeddings, "resolve_model", model)
    monkeypatch.setattr(search, "resolve_model", model)
    monkeypatch.setattr(embeddings, "_create", create)
    counts = await rebuild()
    assert counts["vectors"] == 6  # 2 sources, 3 entities and 1 function; no field
    calls.clear()
    assert (await rebuild())["vectors"] == 0 and calls == []  # unchanged text: no call
    async with SessionLocal() as db:
        found = await search.find_sources(
            db, "what did we bill", allowed_sources=await importer.stored_sources(db),
            tier="basic")
    assert found[0]["name"] == "A_BillingDocument" and found[0]["score"] > 0.9


async def test_a_failed_embedding_call_means_no_vectors(two_services, monkeypatch, caplog):
    async def model():
        return "emb-model"

    async def boom(model_name, texts):
        raise RuntimeError(f"https://user:{SECRET}@aicore.example/v2?token={SECRET}")

    monkeypatch.setattr(embeddings, "resolve_model", model)
    monkeypatch.setattr(search, "resolve_model", model)
    monkeypatch.setattr(embeddings, "_create", boom)
    assert (await rebuild())["vectors"] == 0
    async with SessionLocal() as db:
        found = await search.find_sources(
            db, "invoice", allowed_sources={("odata", "billing")}, tier="basic")
    assert found and found[0]["name"] == "A_BillingDocument"  # lexical
    assert SECRET not in caplog.text and "RuntimeError" in caplog.text


async def test_model_resolution(monkeypatch):
    monkeypatch.setenv("DATA_CATALOG_EMBEDDINGS", "1")
    monkeypatch.setenv("AICORE_EMBEDDING_MODEL", "my-embedder")
    assert await embeddings.resolve_model() == "my-embedder"
    monkeypatch.delenv("AICORE_EMBEDDING_MODEL")
    import agents.shared as shared

    monkeypatch.setattr(shared, "_discover_deployed_models",
                        lambda: ["gpt-4o", "text-embedding-3-small"])
    assert await embeddings.resolve_model() == "text-embedding-3-small"
    embeddings.reset_caches()
    monkeypatch.setattr(shared, "_discover_deployed_models",
                        lambda: ["text-embedding-3-small", "text-embedding-3-large"])
    assert await embeddings.resolve_model() == "text-embedding-3-large"
    embeddings.reset_caches()
    monkeypatch.setattr(shared, "_discover_deployed_models", lambda: ["gpt-4o"])
    assert await embeddings.resolve_model() is None
    monkeypatch.setenv("DATA_CATALOG_EMBEDDINGS", "0")
    monkeypatch.setenv("AICORE_EMBEDDING_MODEL", "my-embedder")
    assert await embeddings.resolve_model() is None
    assert await embeddings.embed_texts(["x"]) is None


async def test_an_advanced_scope_is_empty_in_a_basic_turn(two_services):
    await rebuild()
    allowed = {("odata", "sales-orders")}
    async with SessionLocal() as db:
        assert await search.find_sources(db, "sales order", allowed_sources=allowed,
                                         tier="basic", scope_tier="advanced") == []
        assert await search.find_sources(db, "sales order", allowed_sources=allowed,
                                         tier="advanced", scope_tier="advanced")
        assert await search.list_sources(db, allowed_sources=allowed, tier="basic",
                                         scope_tier="advanced") == []


def test_the_scope_of_an_agent_is_its_own_odata_entries():
    row = AgentConfig(name="a", description="d", instructions="i", mcp_url="builtin:odata",
                      auth_mode="destination",
                      oauth_json=json.dumps({"services": ["x", "y", 3]}),
                      extra_servers_json=json.dumps([
                          {"url": "builtin:sapnotes", "auth_mode": "none"},
                          {"url": "Builtin:OData/", "auth_mode": "destination",
                           "oauth": {"services": ["z"]}}]))
    assert allowed_sources_of(row) == {("odata", "x"), ("odata", "y"), ("odata", "z")}
    assert allowed_sources_of(odata_server("q")) == {("odata", "q")}
    assert allowed_sources_of({"url": "builtin:sapnotes"}) == set()


# --- the agent tools ----------------------------------------------------------------


def _tools(toolset) -> dict[str, Any]:
    return {name: tool.function for name, tool in toolset.tools.items()}


async def test_find_data_source_answers_candidates_or_a_hint(two_services):
    await rebuild()
    tools = _tools(data_catalog_toolset(sources=frozenset({("odata", "sales-orders")}),
                                        scope_tier="basic"))
    assert set(tools) == {"find_data_source", "list_data_sources"}
    answer = await tools["find_data_source"]("Which sales orders have a delivery block?")
    assert answer["candidates"][0]["name"] == "A_SalesOrder"
    assert len(answer["candidates"]) <= 5
    assert set(answer["candidates"][0]) == {
        "source_kind", "source_name", "kind", "name", "label", "purpose", "not_for",
        "keys", "relations", "score"}
    assert await tools["find_data_source"]("invoice amount") == {
        "candidates": [], "hint": NO_MATCH_HINT}
    assert (await tools["find_data_source"]("   "))["candidates"] == []
    listed = await tools["list_data_sources"]()
    assert listed == {"sources": [{"source_kind": "odata", "name": "sales-orders",
                                   "label": "Sales orders",
                                   "purpose": "Read sales orders and their items",
                                   "not_for": "Purchase orders"}]}


async def test_the_tool_of_an_advanced_agent_finds_nothing_in_a_basic_turn(two_services):
    await rebuild()
    tools = _tools(data_catalog_toolset(sources=frozenset({("odata", "sales-orders")}),
                                        scope_tier="advanced"))
    token = current_access.set("basic")
    try:
        assert (await tools["find_data_source"]("sales order"))["candidates"] == []
        assert (await tools["list_data_sources"]())["sources"] == []
    finally:
        current_access.reset(token)
    token = current_access.set("advanced")
    try:
        assert (await tools["find_data_source"]("sales order"))["candidates"]
    finally:
        current_access.reset(token)


# --- registry -----------------------------------------------------------------------


async def seen_by_model(agent) -> tuple[set[str], str]:
    seen: dict[str, Any] = {}

    def answer(messages, info):
        seen["tools"] = {t.name for t in info.function_tools}
        seen["instructions"] = info.instructions or ""
        return ModelResponse(parts=[TextPart("ok")])

    await agent.run("hello", model=FunctionModel(answer))
    return seen["tools"], seen["instructions"]


async def test_the_tool_is_attached_only_to_agents_with_a_source(two_services):
    await add_agent("notes", {"url": "builtin:sapnotes", "auth_mode": "none"})
    await add_agent("ghost", odata_server("no-such-service"),
                    {"url": "builtin:sapnotes", "auth_mode": "none"})
    build = await registry_module.build_orchestrator()
    tools, instructions = await seen_by_model(build.specialists["sales"])
    assert {"find_data_source", "list_data_sources"} <= tools
    assert "## OData services" in instructions and "sales-orders" in instructions
    for name in ("notes", "ghost"):
        tools, _ = await seen_by_model(build.specialists[name])
        assert "find_data_source" not in tools, name


async def test_above_ten_sources_the_index_is_replaced_by_the_tool(monkeypatch):
    names = [f"svc-{i:02d}" for i in range(11)]
    for name in names:
        await add_service(svc(BILLING, name=name, title=f"Title {name}"))
    await add_agent("many", odata_server(*names))
    await add_agent("ten", odata_server(*names[:10]))
    build = await registry_module.build_orchestrator()
    tools, instructions = await seen_by_model(build.specialists["many"])
    assert "find_data_source" in tools
    assert "## OData services" not in instructions and "Title svc-00" not in instructions
    assert "You can reach 11 SAP data sources" in instructions
    assert "`find_data_source`" in instructions
    assert "run_query" not in instructions  # no analytical query: no line about one
    _, instructions = await seen_by_model(build.specialists["ten"])
    assert "## OData services" in instructions and "Title svc-09" in instructions


# --- admin routes -------------------------------------------------------------------


async def test_sources_list_and_detail(client, two_services):
    await add_service(svc(BILLING, name="unused"))
    await rebuild()
    r = await client.get(f"{API}/sources")
    assert r.status_code == 200
    by_name = {s["name"]: s for s in r.json()["sources"]}
    assert set(by_name) == {"sales-orders", "billing", "unused"}
    sales = by_name["sales-orders"]
    assert {k: sales[k] for k in ("kind", "title", "destination", "user_context", "enabled",
                                  "used_by")} == {
        "kind": "odata", "title": "Sales orders", "destination": "S4_ODATA_USER",
        "user_context": True, "enabled": True, "used_by": ["sales"]}
    assert sales["nodes"] == 11 and sales["last_import"]
    assert sales["hint_completeness"] == 0, "imported labels are no admin's hints"
    assert by_name["unused"]["nodes"] == 0 and by_name["unused"]["last_import"] is None

    r = await client.get(f"{API}/sources/odata/sales-orders")
    assert r.status_code == 200
    body = r.json()
    assert body["source"]["name"] == "sales-orders" and body["used_by"] == ["sales"]
    assert [e["node"]["name"] for e in body["entities"]] == ["A_SalesOrder",
                                                            "A_SalesOrderItem"]
    assert [f["name"] for f in body["entities"][1]["fields"]] == [
        "SalesOrder", "SalesOrderItem", "Material"]
    assert [f["name"] for f in body["functions"]] == ["ReleaseOrder"]
    assert {e["kind"] for e in body["edges"]} == {"navigation", "same_object"}
    for path in ("sources/odata/nothing", "sources/bogus/sales-orders",
                 "sources/odata/a%20b"):
        r = await client.get(f"{API}/{path}")
        assert r.status_code == 404, path
    r = await client.get(f"{API}/sources/odata/unused")
    assert r.status_code == 200 and r.json()["source"] is None


async def test_hint_completeness_counts_what_an_admin_curated(client, two_services):
    # Source, entities and functions count; fields do not (they would drown the
    # entities); a node counts once an admin saved hints for it (curated_at).
    await rebuild()

    async def completeness() -> float:
        r = await client.get(f"{API}/sources")
        assert r.status_code == 200
        return {s["name"]: s for s in r.json()["sources"]}["sales-orders"]["hint_completeness"]

    assert await completeness() == 0
    field = await node("sales-orders", "field", "SoldToParty")
    r = await client.put(f"{API}/nodes/{field.id}/hints", json={"synonyms": ["buyer"]})
    assert r.status_code == 200, r.text
    assert await completeness() == 0, "a field adds nothing"
    order = await node("sales-orders", "entity", "A_SalesOrder")
    r = await client.put(f"{API}/nodes/{order.id}/hints", json={"purpose": "Order heads"})
    assert r.status_code == 200, r.text
    # source, A_SalesOrder, A_SalesOrderItem, ReleaseOrder: 1 of 4
    assert await completeness() == 0.25
    for kind, name in (("source", "sales-orders"), ("entity", "A_SalesOrderItem")):
        target = await node("sales-orders", kind, name)
        r = await client.put(f"{API}/nodes/{target.id}/hints", json={"synonyms": ["x"]})
        assert r.status_code == 200, r.text
    assert await completeness() == 0.75
    billing = {s["name"]: s for s in (await client.get(f"{API}/sources")).json()["sources"]}
    assert billing["billing"]["hint_completeness"] == 0


@pytest.mark.parametrize("body, field", [
    ({"label": "x" * 201}, "label"),
    ({"label": ""}, "label"),
    ({"label": "   "}, "label"),
    ({"purpose": 5}, "purpose"),
    ({"synonyms": ["ok", 7]}, "synonyms.1"),
    ({"synonyms": "not a list"}, "synonyms"),
    ({"examples": [""]}, "examples.0"),
    ({"readable": "true"}, "readable"),
    ({"label": f"two\nlines {SECRET}"}, "label"),
    ({"owner": SECRET}, "owner"),
    ({f"{SECRET} key!": 1}, "<unknown field>"),
])
async def test_hints_refusals_name_the_field_never_the_value(client, two_services, body, field):
    await rebuild()
    order = await node("sales-orders", "entity", "A_SalesOrder")
    r = await client.put(f"{API}/nodes/{order.id}/hints", json=body)
    assert r.status_code == 422, r.text
    assert r.json()["detail"].startswith(f"{field}:")
    assert SECRET not in r.text and "x" * 50 not in r.text
    assert (await node("sales-orders", "entity", "A_SalesOrder")).curated_at is None


async def test_hints_body_and_node_errors(client, two_services):
    await rebuild()
    order = await node("sales-orders", "entity", "A_SalesOrder")
    for raw in (b"[1]", b"not json", b'"x"'):
        r = await client.put(f"{API}/nodes/{order.id}/hints", content=raw,
                             headers={"content-type": "application/json"})
        assert r.status_code == 422 and r.json()["detail"] == "body: expected a JSON object"
    for nid in ("999999", "abc", "99999999999", "-1", "\u00b2"):
        r = await client.put(f"{API}/nodes/{nid}/hints", json={"label": "x"})
        assert r.status_code == 404, nid


async def test_search_route_scoped_unscoped_and_unknown_agent(client, two_services):
    await rebuild()
    r = await client.post(f"{API}/search", json={"question": "invoice amount"})
    assert r.status_code == 200 and r.json()["scope"] == "all"
    assert r.json()["candidates"][0]["source_name"] == "billing"
    r = await client.post(f"{API}/search",
                          json={"question": "invoice amount", "agent": "sales"})
    assert r.json() == {"candidates": [], "scope": "agent", "mode": "lexical"}
    r = await client.post(f"{API}/search", json={"question": "order", "agent": "nobody"})
    assert r.status_code == 404 and r.json()["detail"] == "Agent not found"
    for body in ({"question": ""}, {"question": "x" * 501}, {"question": 3},
                 {"question": "q", "extra": SECRET}, {}):
        r = await client.post(f"{API}/search", json=body)
        assert r.status_code == 422, body
        assert SECRET not in r.text


async def test_graph_route_filters_and_the_cap(client, two_services, monkeypatch):
    await rebuild()
    r = await client.get(f"{API}/graph")
    body = r.json()
    assert r.status_code == 200 and body["truncated"] is False
    assert {n["group"] for n in body["nodes"]} == {"odata:sales-orders", "odata:billing"}
    ids = {n["id"] for n in body["nodes"]}
    assert all(e["from"] in ids and e["to"] in ids for e in body["edges"])
    assert {e["kind"] for e in body["edges"]} == {"contains", "navigation", "same_object"}
    r = await client.get(f"{API}/graph", params={"agent": "finance"})
    assert {n["source_name"] for n in r.json()["nodes"]} == {"billing"}
    r = await client.get(f"{API}/graph", params={"kind": "entity"})
    assert {n["kind"] for n in r.json()["nodes"]} == {"entity"}
    # Several kinds, comma-separated: the graph page's default (every kind
    # but field) is applied before the cap, not after it.
    r = await client.get(f"{API}/graph", params={"kind": "source,entity,function"})
    assert {n["kind"] for n in r.json()["nodes"]} == {"source", "entity", "function"}
    for bad in ("entity,bogus", "entity,", ",", "entity,entity,entity,entity,entity,entity,"
                "entity,entity"):
        r = await client.get(f"{API}/graph", params={"kind": bad})
        assert r.status_code == 422, bad
        assert r.json()["detail"] == "kind: unknown node kind" and "bogus" not in r.text
    r = await client.get(f"{API}/graph", params={"q": "material"})
    assert [n["name"] for n in r.json()["nodes"]] == ["Material"]
    assert (await client.get(f"{API}/graph", params={"kind": "bogus"})).status_code == 422
    assert (await client.get(f"{API}/graph", params={"q": "x" * 201})).status_code == 422
    assert (await client.get(f"{API}/graph", params={"agent": "nobody"})).status_code == 404
    # The cap: 2,000 nodes at most, `truncated` says so.
    async with SessionLocal() as db:
        await db.execute(insert(DataCatalogNode), [
            {"kind": "field", "source_kind": "odata", "source_name": "zz-bulk",
             "name": f"F{i}", "readable": 1} for i in range(2001)])
        await db.commit()
    r = await client.get(f"{API}/graph")
    assert len(r.json()["nodes"]) == dc_routes.GRAPH_MAX_NODES == 2000
    assert r.json()["truncated"] is True
    r = await client.get(f"{API}/graph", params={"kind": "source,entity,function"})
    assert r.json()["truncated"] is False
    assert {n["kind"] for n in r.json()["nodes"]} == {"source", "entity", "function"}


async def test_rebuild_route(client, two_services):
    r = await client.post(f"{API}/rebuild")
    assert r.status_code == 200 and r.json()["imported"] > 0
    assert r.json() == {"imported": r.json()["imported"], "removed": 0, "vectors": 0}
    r = await client.post(f"{API}/rebuild", json={"source": {"kind": "odata",
                                                             "name": "billing"}})
    assert r.json() == {"imported": 0, "removed": 0, "vectors": 0}
    for body in ({"source": {"kind": "bogus", "name": "x"}}, {"source": "billing"},
                 {"everything": True}):
        r = await client.post(f"{API}/rebuild", json=body)
        assert r.status_code == 422, body


async def test_a_catalogue_save_and_an_agent_save_rebuild_the_graph(client):
    await add_service(ORDERS)
    r = await client.post("/admin/api/agents", json={
        "name": "sales", "description": "d", "instructions": "i",
        "mcp_servers": [odata_server("sales-orders")]})
    assert r.status_code in (200, 201), r.text
    assert await nodes_of("sales-orders")  # the agent save imported it
    changed = svc(ORDERS)
    changed["definition"]["entity_sets"][1]["title"] = "Order line"
    r = await client.get("/admin/api/odata/services/sales-orders")
    changed["expected_updated_at"] = r.json()["updated_at"]
    r = await client.put("/admin/api/odata/services/sales-orders", json=changed)
    assert r.status_code == 200, r.text
    assert (await node("sales-orders", "entity", "A_SalesOrderItem")).label == "Order line"


async def test_a_failed_rebuild_never_fails_the_save(client, monkeypatch, caplog):
    async def broken(*a, **k):
        raise RuntimeError(f"postgres://u:{SECRET}@db/x")

    monkeypatch.setattr(importer, "rebuild", broken)
    await add_service(ORDERS)
    r = await client.post("/admin/api/agents", json={
        "name": "sales", "description": "d", "instructions": "i",
        "mcp_servers": [odata_server("sales-orders")]})
    assert r.status_code in (200, 201)
    assert "RuntimeError" in caplog.text and SECRET not in caplog.text


class _StubValidator:
    xsappname = "app"

    def validate(self, token: str) -> dict[str, Any]:
        claims = {"usr": {"user_name": "u", "scope": ["user"]},
                  "dev": {"user_name": "d", "scope": ["developer"]}}
        if token not in claims:
            raise HTTPException(status_code=401, detail="Invalid token")
        return claims[token]

    def has_scope(self, payload: dict[str, Any], scope: str) -> bool:
        return scope in (payload.get("scope") or [])


async def test_every_route_requires_the_admin_scope(monkeypatch):
    from agents.auth import require_admin

    for route in dc_routes.router.routes:
        assert require_admin in [d.call for d in route.dependant.dependencies], route.path
    monkeypatch.setattr(auth, "get_validator", lambda: _StubValidator())
    token = auth.current_claims.set(None)
    try:
        bare = FastAPI()
        bare.include_router(dc_routes.router, prefix="/admin")
        calls = [(m, route.path.replace("{kind}", "odata").replace("{name}", "x")
                  .replace("{node_id}", "1").replace("{edge_id}", "1"))
                 for route in dc_routes.router.routes for m in route.methods - {"HEAD"}]
        assert len(calls) == 8
        async with AsyncClient(transport=ASGITransport(app=bare), base_url="http://t") as c:
            for method, path in calls:
                kwargs = {} if method == "GET" else {"json": {"question": "x"}}
                r = await c.request(method, f"/admin{path}", **kwargs)
                assert r.status_code == 401, (method, path)
                for bearer in ("usr", "dev"):
                    r = await c.request(method, f"/admin{path}",
                                        headers={"Authorization": f"Bearer {bearer}"}, **kwargs)
                    assert r.status_code == 403, (method, path, bearer)
    finally:
        auth.current_claims.reset(token)


def test_the_app_serves_the_routes_under_admin_api_data_catalog():
    paths = app_module.app.openapi()["paths"]
    served = {p for p in paths if "/data-catalog/" in p}
    assert served == {
        f"{API}/sources", f"{API}/sources/{{kind}}/{{name}}",
        f"{API}/nodes/{{node_id}}/hints", f"{API}/search", f"{API}/graph", f"{API}/rebuild",
        f"{API}/edges", f"{API}/edges/{{edge_id}}",
        # The embeddings' own router (tests/test_data_catalog_embeddings.py).
        f"{API}/embeddings", f"{API}/embeddings/run", f"{API}/embeddings/deploy"}
