"""Admin API of the data catalogue graph: ``/admin/api/data-catalog/...``.

``agents/admin.py`` includes this router under ``/admin``. Every route
carries ``require_admin`` itself (as ``agents/odata/admin_routes.py`` does).
Bodies are read as raw JSON and validated here: a refusal is a 422 whose
``detail`` is ``<field>: <rule>``, never a value the client sent.

The embeddings of the catalogue have routes of their own,
``agents/data_catalog/embedding_routes.py`` (included next to this router).

Routes: ``GET /sources`` (catalogue services of either kind and InA
queries), ``GET /sources/{kind}/{name}`` (the OData shape for ``odata`` and
``generic``, the query with its dimensions and measures for ``ina``), ``PUT
/nodes/{id}/hints``, ``POST /search``, ``GET /graph`` (``kind`` is one node
kind or several, comma-separated), ``POST /rebuild``, ``POST /edges`` and
``DELETE /edges/{id}`` (the links an admin draws, addendum (k)).

A catalogue service is one ``odata_services`` row whatever its kind, and its
name is unique there: ``odata`` and ``generic`` in a path or a scope name the
same row (`agents.data_catalog.scope.node_sources`), as the rebuild does.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)
from sqlalchemy import and_, delete, or_, select

from agents.access import ADVANCED, tier_of
from agents.auth import require_admin
from agents.data_catalog import importer
from agents.data_catalog.embeddings import refresh_vectors
from agents.data_catalog.models import (
    LABEL_MAX,
    NODE_KINDS,
    NOT_FOR_MAX,
    PURPOSE_MAX,
    SOURCE_KINDS,
    DataCatalogEdge,
    DataCatalogNode,
    utcnow,
)
from agents.data_catalog.scope import allowed_sources_of, node_sources
from agents.data_catalog.search import (
    CANDIDATE_KINDS,
    QUESTION_MAX,
    find_sources_detail,
    scoped_nodes,
)
from agents.db import (
    SessionLocal,
    begin_exclusive_write,
    get_agent_by_name,
    get_ina_query,
    get_odata_service,
    has_row_locks,
    ina_query_referrers,
    list_ina_queries,
    list_odata_services,
    odata_service_referrers,
)
from agents.loc_fields import one_line_text, refusal_text
from agents.odata.admin_routes import _bounded_body

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data-catalog", tags=["data-catalog"])

BODY_BYTES = 64 * 1024
GRAPH_MAX_NODES = 2000
GRAPH_QUERY_MAX = 200
SYNONYMS_MAX = 50
EXAMPLES_MAX = 20
_MAX_ID = 2_147_483_647
_NAME_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
# ASCII digits only: ``str.isdigit`` is true for "²", which ``int`` refuses.
_ROW_ID_RE = re.compile(r"[0-9]{1,10}")
_AGENT_RE = re.compile(r"[^\x00-\x1f\x7f]{1,64}")
_SOURCE_NOT_FOUND = "Source not found"
_NODE_NOT_FOUND = "Node not found"
_EDGE_NOT_FOUND = "Link not found"
_EDGE_IMPORTED = "Only a link an admin drew can be removed; an import made this one"
_ALREADY_LINKED = "The two nodes are already linked"
_NOT_LINKABLE = "not an entity, query or function"
_NOT_A_NODE = "not a node of the catalogue"
_SAME_SOURCE = "to_id: same source as from_id"
# What an admin may link: the nodes an agent picks among (`CANDIDATE_KINDS`),
# by the two kinds of addendum (k); `navigation` stays the import's.
CURATED_EDGE_KINDS = ("same_object", "aggregates")
_AGENT_NOT_FOUND = "Agent not found"
_BODY_NOT_OBJECT = "body: expected a JSON object"
_NOT_READABLE = "readable: the catalogue does not let agents read this node"


def _one_line(value: str) -> str:
    # A hint is shown as one line in the admin's tables and the model's tool
    # answer; it is refused with invisible format characters too.
    return one_line_text(value, refuse_format=True).strip()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HintsBody(_Strict):
    label: Annotated[StrictStr, Field(max_length=LABEL_MAX)] | None = None
    purpose: Annotated[StrictStr, Field(max_length=PURPOSE_MAX)] | None = None
    not_for: Annotated[StrictStr, Field(max_length=NOT_FOR_MAX)] | None = None
    synonyms: (
        Annotated[list[Annotated[StrictStr, Field(min_length=1, max_length=100)]],
                  Field(max_length=SYNONYMS_MAX)]
        | None
    ) = None
    examples: (
        Annotated[list[Annotated[StrictStr, Field(min_length=1, max_length=300)]],
                  Field(max_length=EXAMPLES_MAX)]
        | None
    ) = None
    readable: StrictBool | None = None

    @field_validator("label", "purpose", "not_for")
    @classmethod
    def _text(cls, value: str | None) -> str | None:
        return None if value is None else _one_line(value)

    @field_validator("label")
    @classmethod
    def _label_stays(cls, value: str | None) -> str | None:
        # A curated label survives every re-import, so a blank one would leave
        # the node nameless for good. Purpose and "not for" may be cleared.
        if value is not None and not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("synonyms", "examples")
    @classmethod
    def _items(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [_one_line(v) for v in value]
        if any(not v for v in cleaned):
            raise ValueError("items must not be blank")
        return list(dict.fromkeys(cleaned))


class SearchBody(_Strict):
    question: Annotated[StrictStr, Field(min_length=1, max_length=QUESTION_MAX)]
    agent: Annotated[StrictStr, Field(min_length=1, max_length=64)] | None = None


class SourceRef(_Strict):
    kind: StrictStr
    name: Annotated[StrictStr, Field(min_length=1, max_length=128)]

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if value not in SOURCE_KINDS:
            raise ValueError("unknown source kind")
        return value


class RebuildBody(_Strict):
    source: SourceRef | None = None


_NodeId = Annotated[StrictInt, Field(ge=1, le=_MAX_ID)]


class EdgeBody(_Strict):
    from_id: _NodeId
    to_id: _NodeId
    kind: StrictStr

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if value not in CURATED_EDGE_KINDS:
            raise ValueError("must be same_object or aggregates")
        return value


def _refusal(exc: ValidationError) -> HTTPException:
    return HTTPException(status_code=422, detail=refusal_text(exc, "body"))


async def _json_body(
    request: Request, model: type[BaseModel], *, empty_ok: bool = False, limit: int = BODY_BYTES
) -> Any:
    raw = await _bounded_body(request, limit)
    if not raw.strip() and empty_ok:
        data: Any = {}
    else:
        try:
            data = json.loads(raw)
        # RecursionError: a body nested deeper than the parser goes.
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise HTTPException(status_code=422, detail=_BODY_NOT_OBJECT) from None
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail=_BODY_NOT_OBJECT)
    try:
        return model.model_validate(data), data
    except ValidationError as exc:
        raise _refusal(exc) from None


def _stamp(value: Any) -> str | None:
    if value is None:
        return None
    from datetime import timezone

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


# Kinds that count towards a source's hint completeness: the source itself and
# what an agent picks among (entities, functions, an InA source's query).
# Fields, dimensions and measures do not: 53 fields would drown 4 entities.
_COMPLETENESS_KINDS = frozenset({"source", "entity", "function", "query"})


def _completeness(nodes: list[DataCatalogNode]) -> float:
    """Share of the source node, its entities and functions that carry hints
    an admin wrote (``curated_at`` set). Labels and purposes the import took
    from the OData catalogue are not hints."""
    counted = [n for n in nodes if n.kind in _COMPLETENESS_KINDS]
    if not counted:
        return 0.0
    complete = sum(1 for n in counted if n.curated_at is not None)
    return round(complete / len(counted), 2)


async def _agent_scope(db: Any, name: str | None) -> tuple[set[tuple[str, str]] | None, Any]:
    if name is None:
        return None, None
    if not _AGENT_RE.fullmatch(name):
        raise HTTPException(status_code=404, detail=_AGENT_NOT_FOUND)
    row = await get_agent_by_name(db, name)
    if row is None:
        raise HTTPException(status_code=404, detail=_AGENT_NOT_FOUND)
    # As the search scopes it: a catalogue service under the kind its nodes
    # are stored as (a generic one is attached through ``builtin:odata``).
    return node_sources(allowed_sources_of(row)), row


def _summary(
    kind: str, row: Any, used_by: list[dict[str, Any]], mine: list[DataCatalogNode]
) -> dict[str, Any]:
    source_node = next((n for n in mine if n.kind == "source"), None)
    return {
        "kind": kind,
        "name": row.name,
        "title": row.title,
        "destination": row.destination,
        "user_context": bool(row.user_context),
        "enabled": bool(row.enabled),
        "used_by": [r["agent"] for r in used_by],
        "nodes": len(mine),
        "last_import": _stamp(source_node.updated_at) if source_node else None,
        "hint_completeness": _completeness(mine),
    }


@router.get("/sources", dependencies=[Depends(require_admin)])
async def api_sources() -> dict[str, Any]:
    """Every registration a source can come from: the catalogue services
    (under their own kind, ``odata`` or ``generic``) and the InA queries,
    each with its nodes (none before its first import)."""
    async with SessionLocal() as db:
        services = await list_odata_services(db)
        referrers = await odata_service_referrers(db)
        queries = await list_ina_queries(db)
        query_referrers = await ina_query_referrers(db)
        nodes = list(
            (await db.execute(select(DataCatalogNode).order_by(DataCatalogNode.id))).scalars()
        )
    by_source: dict[tuple[str, str], list[DataCatalogNode]] = {}
    for n in nodes:
        by_source.setdefault((n.source_kind, n.source_name), []).append(n)
    out = []
    for s in services:
        kind = importer.service_kind(s)
        mine = by_source.get((kind, s.name), [])
        out.append(_summary(kind, s, referrers.get(s.name, []), mine))
    for q in queries:
        mine = by_source.get((importer.INA, q.name), [])
        out.append(_summary(importer.INA, q, query_referrers.get(q.name, []), mine))
    return {"sources": out}


@router.get("/sources/{kind}/{name}", dependencies=[Depends(require_admin)])
async def api_source(kind: str, name: str) -> dict[str, Any]:
    """One source with its nodes and every edge that touches them. A
    registration that was never imported answers ``source: null``."""
    if kind not in SOURCE_KINDS or not _NAME_RE.fullmatch(name):
        raise HTTPException(status_code=404, detail=_SOURCE_NOT_FOUND)
    async with SessionLocal() as db:
        nodes = await scoped_nodes(db, {(kind, name)})
        if kind == importer.INA:
            row: Any = await get_ina_query(db, name)
        else:
            row = await get_odata_service(db, name)
        if not nodes and row is None:
            raise HTTPException(status_code=404, detail=_SOURCE_NOT_FOUND)
        if kind == importer.INA:
            referrers = await ina_query_referrers(db, name)
        else:
            referrers = await odata_service_referrers(db, name)
        ids = [n.id for n in nodes]
        edges: list[DataCatalogEdge] = []
        for chunk in importer.batched(ids):
            rows = await db.execute(
                select(DataCatalogEdge).where(
                    or_(DataCatalogEdge.from_id.in_(chunk), DataCatalogEdge.to_id.in_(chunk))
                )
            )
            edges.extend(rows.scalars())
        others = {e.to_id for e in edges} | {e.from_id for e in edges}
        others -= set(ids)
        other_nodes: dict[int, DataCatalogNode] = {}
        for chunk in importer.batched(sorted(others)):
            rows = await db.execute(select(DataCatalogNode).where(DataCatalogNode.id.in_(chunk)))
            other_nodes.update({n.id: n for n in rows.scalars()})
    nodes.sort(key=lambda n: n.id)
    source = next((n for n in nodes if n.kind == "source"), None)
    everything = {n.id: n for n in nodes} | other_nodes
    unique = sorted({e.id: e for e in edges}.values(), key=lambda e: e.id)
    answer: dict[str, Any] = {"source": source.to_dict() if source else None}
    if kind == importer.INA:
        answer["query"] = _query_out(nodes)
    else:
        answer["entities"] = [
            {"node": e.to_dict(), "fields": _children(nodes, e, "field")}
            for e in nodes
            if e.kind == "entity"
        ]
        answer["functions"] = [f.to_dict() for f in nodes if f.kind == "function"]
    answer["edges"] = [_edge_out(e, everything) for e in unique]
    answer["used_by"] = [r["agent"] for r in referrers.get(name, [])]
    return answer


def _children(
    nodes: list[DataCatalogNode], parent: DataCatalogNode, kind: str
) -> list[dict[str, Any]]:
    return [n.to_dict() for n in nodes if n.kind == kind and n.parent_id == parent.id]


def _query_out(nodes: list[DataCatalogNode]) -> dict[str, Any] | None:
    """An InA source's one query with its members; ``None`` before the
    first import."""
    query = next((n for n in nodes if n.kind == "query"), None)
    if query is None:
        return None
    return {
        "node": query.to_dict(),
        "dimensions": _children(nodes, query, "dimension"),
        "measures": _children(nodes, query, "measure"),
    }


def _edge_out(edge: DataCatalogEdge, nodes: dict[int, DataCatalogNode]) -> dict[str, Any]:
    def end(node_id: int) -> dict[str, Any] | None:
        node = nodes.get(node_id)
        if node is None:
            return None
        return {"id": node.id, "kind": node.kind, "source_kind": node.source_kind,
                "source_name": node.source_name, "name": node.name,
                "label": node.label or node.name}

    return {
        "id": edge.id,
        "from": edge.from_id,
        "to": edge.to_id,
        "kind": edge.kind,
        "label": edge.label or "",
        "curated": edge.curated_at is not None,
        "from_node": end(edge.from_id),
        "to_node": end(edge.to_id),
    }


def _row_id(raw: str, not_found: str = _NODE_NOT_FOUND) -> int:
    """The id of a node or an edge in a path; anything else is a 404."""
    if not _ROW_ID_RE.fullmatch(raw) or int(raw) > _MAX_ID:
        raise HTTPException(status_code=404, detail=not_found)
    return int(raw)


async def _planned(db: Any, node: DataCatalogNode) -> list[Any] | None:
    """What an import of ``node``'s registration would make now; ``None``
    when the registration is gone or its stored definition does not
    validate."""
    try:
        if node.source_kind in importer.CATALOGUE_KINDS:
            service = await get_odata_service(db, node.source_name)
            return None if service is None else importer.plan_service(service)[0]
        if node.source_kind == importer.INA:
            query = await get_ina_query(db, node.source_name)
            return None if query is None else importer.plan_ina_query(query)
    except (ValidationError, TypeError, ValueError):
        return None
    return None


async def _catalogue_readable(db: Any, node: DataCatalogNode) -> bool:
    """Whether the registration itself lets agents read ``node`` (what an
    import would set: the OData catalogue of an ``odata`` or ``generic``
    service, the description of an InA query): a hint may narrow
    ``readable`` and widen it back up to that, never further."""
    planned = await _planned(db, node)
    if planned is None:
        return False
    parent = await db.get(DataCatalogNode, node.parent_id) if node.parent_id else None
    parent_ref = (parent.kind, parent.name) if parent is not None else None
    for item in planned:
        if item.key == (node.kind, parent_ref, node.name):
            return bool(item.attrs["readable"])
    return False


@router.put("/nodes/{node_id}/hints", dependencies=[Depends(require_admin)])
async def api_put_hints(node_id: str, request: Request) -> dict[str, Any]:
    nid = _row_id(node_id)
    body, raw = await _json_body(request, HintsBody)
    async with SessionLocal() as db:
        node = await db.get(DataCatalogNode, nid)
        if node is None:
            raise HTTPException(status_code=404, detail=_NODE_NOT_FOUND)
        if body.readable is True and not node.readable:
            if not await _catalogue_readable(db, node):
                raise HTTPException(status_code=422, detail=_NOT_READABLE)
        for key in ("label", "purpose", "not_for"):
            if key in raw:
                setattr(node, key, getattr(body, key) or "")
        if "synonyms" in raw:
            node.synonyms_json = json.dumps(body.synonyms or [])
        if "examples" in raw:
            node.examples_json = json.dumps(body.examples or [])
        if "readable" in raw and body.readable is not None:
            node.readable = int(body.readable)
        now = utcnow()
        node.curated_at = now
        node.updated_at = now
        await db.commit()
        answer = node.to_dict()
        source = (node.source_kind, node.source_name)
        try:
            await refresh_vectors(db, scope=source)
            await db.commit()
        except Exception as exc:  # noqa: BLE001 - the hint is stored; vectors follow later
            await db.rollback()
            logger.warning("data catalogue: vectors after a hint failed (%s)", type(exc).__name__)
    return answer


@router.post("/search", dependencies=[Depends(require_admin)])
async def api_search(request: Request) -> dict[str, Any]:
    body, _ = await _json_body(request, SearchBody)
    async with SessionLocal() as db:
        allowed, row = await _agent_scope(db, body.agent)
        scope = "agent"
        scope_tier = tier_of(row) if row is not None else ADVANCED
        if allowed is None:
            allowed = await importer.stored_sources(db)
            scope = "all"
        candidates, mode = await find_sources_detail(
            db, body.question, allowed_sources=allowed, tier=ADVANCED, scope_tier=scope_tier
        )
    # ``mode``: whether vectors ranked it, so the admin sees what embeddings do.
    return {"candidates": candidates, "scope": scope, "mode": mode}


@router.get("/graph", dependencies=[Depends(require_admin)])
async def api_graph(
    agent: str | None = None, kind: str | None = None, q: str | None = None
) -> dict[str, Any]:
    # One kind or several, comma-separated (at most one per known kind), so a
    # filter that leaves out the fields is applied before the node cap.
    kinds = None if kind is None else kind.split(",")
    if kinds is not None and (
        len(kinds) > len(NODE_KINDS) or any(k not in NODE_KINDS for k in kinds)
    ):
        raise HTTPException(status_code=422, detail="kind: unknown node kind")
    if q is not None and len(q) > GRAPH_QUERY_MAX:
        raise HTTPException(
            status_code=422, detail=f"q: at most {GRAPH_QUERY_MAX} characters"
        )
    async with SessionLocal() as db:
        allowed, _ = await _agent_scope(db, agent)
        if allowed is None:
            nodes = list((await db.execute(select(DataCatalogNode))).scalars())
        else:
            nodes = await scoped_nodes(db, allowed)
        if kinds is not None:
            nodes = [n for n in nodes if n.kind in kinds]
        needle = (q or "").strip().casefold()
        if needle:
            nodes = [
                n for n in nodes
                if needle in n.name.casefold()
                or needle in (n.label or "").casefold()
                or any(needle in s.casefold() for s in n.synonyms)
            ]
        nodes.sort(key=lambda n: (n.source_kind, n.source_name, n.id))
        truncated = len(nodes) > GRAPH_MAX_NODES
        nodes = nodes[:GRAPH_MAX_NODES]
        ids = {n.id for n in nodes}
        stored: list[DataCatalogEdge] = []
        for chunk in importer.batched(sorted(ids)):
            rows = await db.execute(
                select(DataCatalogEdge).where(DataCatalogEdge.from_id.in_(chunk))
            )
            stored.extend(e for e in rows.scalars() if e.to_id in ids)
    edges = [
        {"from": n.parent_id, "to": n.id, "kind": "contains", "label": "", "curated": False}
        for n in nodes
        if n.parent_id in ids
    ] + [{"from": e.from_id, "to": e.to_id, "kind": e.kind, "label": e.label or "",
          "curated": e.curated_at is not None}
         for e in stored]
    return {
        "nodes": [
            {
                "id": n.id,
                "kind": n.kind,
                "source_kind": n.source_kind,
                "source_name": n.source_name,
                "name": n.name,
                "label": n.label or n.name,
                "group": f"{n.source_kind}:{n.source_name}",
            }
            for n in nodes
        ],
        "edges": edges,
        "truncated": truncated,
    }


@router.post("/rebuild", dependencies=[Depends(require_admin)])
async def api_rebuild(request: Request) -> dict[str, int]:
    body, _ = await _json_body(request, RebuildBody, empty_ok=True)
    source = (body.source.kind, body.source.name) if body.source is not None else None
    try:
        async with SessionLocal() as db:
            return await importer.rebuild(db, source)
    except Exception as exc:  # noqa: BLE001 - a fixed answer; the class goes to the log
        logger.error("data catalogue: rebuild failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Rebuild failed") from None


def _same_source(a: DataCatalogNode, b: DataCatalogNode) -> bool:
    """One registration: the same kind and name, or one catalogue service
    under either catalogue kind (its name is unique in ``odata_services``)."""
    return (a.source_kind, a.source_name) in node_sources({(b.source_kind, b.source_name)})


def _lock_nodes_stmt(ids: list[int]) -> Any:
    """The row lock `api_create_edge` takes on Postgres and HANA, in id order
    (two requests for one pair cannot wait for each other). The id column
    alone: HANA locks no row through a LOB column. A function of its own so
    a test can compile it; the SQLite suite never sends it."""
    return (
        select(DataCatalogNode.id)
        .where(DataCatalogNode.id.in_(sorted(ids)))
        .order_by(DataCatalogNode.id)
        .with_for_update()
    )


@router.post(
    "/edges", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin)]
)
async def api_create_edge(request: Request) -> dict[str, Any]:
    """Draw a link between two nodes of different sources (an entity, an
    InA query or a function at each end): ``same_object`` or ``aggregates``.
    One link per kind per pair: 409 when an edge of that kind joins the two
    in either direction, imported or drawn; an ``aggregates`` over an
    imported ``same_object`` is fine. The edge carries ``curated_at``, so an
    import keeps it, and a drawn ``same_object`` keeps the import from adding
    its own for the pair (`importer._curated_pairs`).

    Check and insert in one transaction, behind a lock on both nodes
    (`_lock_nodes_stmt`; ``BEGIN IMMEDIATE`` on SQLite), so two requests for
    one pair cannot both pass the "already linked" check. An import that
    runs at the same moment takes no such lock: it may add its own
    ``same_object`` beside a new one (the next import of either end drops it
    again), or delete a node the edge then points at (the routes skip an end
    that is gone).
    """
    body, _ = await _json_body(request, EdgeBody)
    async with SessionLocal() as db:
        await begin_exclusive_write(db)
        if has_row_locks(db):
            await db.execute(_lock_nodes_stmt([body.from_id, body.to_id]))
        ends: dict[str, DataCatalogNode] = {}
        for field_name, nid in (("from_id", body.from_id), ("to_id", body.to_id)):
            node = await db.get(DataCatalogNode, nid)
            if node is None:
                raise HTTPException(status_code=422, detail=f"{field_name}: {_NOT_A_NODE}")
            if node.kind not in CANDIDATE_KINDS:
                raise HTTPException(status_code=422, detail=f"{field_name}: {_NOT_LINKABLE}")
            ends[field_name] = node
        start, end = ends["from_id"], ends["to_id"]
        if _same_source(start, end):
            raise HTTPException(status_code=422, detail=_SAME_SOURCE)
        linked = await db.execute(
            select(DataCatalogEdge.id).where(
                DataCatalogEdge.kind == body.kind,
                or_(
                    and_(DataCatalogEdge.from_id == start.id, DataCatalogEdge.to_id == end.id),
                    and_(DataCatalogEdge.from_id == end.id, DataCatalogEdge.to_id == start.id),
                )
            )
        )
        if linked.first() is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_ALREADY_LINKED)
        edge = DataCatalogEdge(
            from_id=start.id, to_id=end.id, kind=body.kind, label="", curated_at=utcnow()
        )
        db.add(edge)
        await db.commit()
        return _edge_out(edge, {start.id: start, end.id: end})


@router.delete(
    "/edges/{edge_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
async def api_delete_edge(edge_id: str) -> Response:
    """Remove a link an admin drew. An imported edge answers 409: the next
    import would make it again, so removing it would only look done."""
    eid = _row_id(edge_id, _EDGE_NOT_FOUND)
    async with SessionLocal() as db:
        # Conditional on ``curated_at``, so the 409 never races a delete.
        result = await db.execute(
            delete(DataCatalogEdge).where(
                DataCatalogEdge.id == eid, DataCatalogEdge.curated_at.is_not(None)
            )
        )
        if result.rowcount == 0:
            exists = await db.get(DataCatalogEdge, eid)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT if exists else 404,
                detail=_EDGE_IMPORTED if exists else _EDGE_NOT_FOUND,
            )
        await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
