"""Admin API of the data catalogue graph: ``/admin/api/data-catalog/...``.

``agents/admin.py`` includes this router under ``/admin``. Every route
carries ``require_admin`` itself (as ``agents/odata/admin_routes.py`` does).
Bodies are read as raw JSON and validated here: a refusal is a 422 whose
``detail`` is ``<field>: <rule>``, never a value the client sent.

Routes: ``GET /sources``, ``GET /sources/{kind}/{name}``, ``PUT
/nodes/{id}/hints``, ``POST /search``, ``GET /graph``, ``POST /rebuild``.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    ValidationError,
    field_validator,
)
from sqlalchemy import or_, select

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
from agents.data_catalog.scope import allowed_sources_of
from agents.data_catalog.search import QUESTION_MAX, find_sources, scoped_nodes
from agents.db import (
    SessionLocal,
    get_agent_by_name,
    get_odata_service,
    list_odata_services,
    odata_service_referrers,
)
from agents.odata.admin_routes import _bounded_body
from agents.odata.models import loc_path

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data-catalog", tags=["data-catalog"])

BODY_BYTES = 64 * 1024
GRAPH_MAX_NODES = 2000
GRAPH_QUERY_MAX = 200
SYNONYMS_MAX = 50
EXAMPLES_MAX = 20
_MAX_ID = 2_147_483_647
_NAME_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_AGENT_RE = re.compile(r"[^\x00-\x1f\x7f]{1,64}")
_SOURCE_NOT_FOUND = "Source not found"
_NODE_NOT_FOUND = "Node not found"
_AGENT_NOT_FOUND = "Agent not found"
_BODY_NOT_OBJECT = "body: expected a JSON object"
_NOT_READABLE = "readable: the catalogue does not let agents read this node"


def _one_line(value: str) -> str:
    if any(unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") for ch in value):
        raise ValueError("must be one line of text without control characters")
    return value.strip()


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


def _refusal(exc: ValidationError) -> HTTPException:
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    lines = [f"{loc_path(e['loc'], 'body')}: {e['msg']}" for e in errors[:20]]
    return HTTPException(status_code=422, detail="; ".join(lines))


async def _json_body(request: Request, model: type[BaseModel], *, empty_ok: bool = False) -> Any:
    raw = await _bounded_body(request, BODY_BYTES)
    if not raw.strip() and empty_ok:
        data: Any = {}
    else:
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
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


def _completeness(nodes: list[DataCatalogNode]) -> float:
    """Share of the nodes an agent sees that carry a label and, for a source,
    an entity or a function, a purpose."""
    counted = [
        n for n in nodes if n.kind != "field" or n.readable
    ]
    if not counted:
        return 0.0
    complete = sum(
        1 for n in counted if (n.label or "").strip() and (n.kind == "field" or n.purpose)
    )
    return round(complete / len(counted), 2)


async def _agent_scope(db: Any, name: str | None) -> tuple[set[tuple[str, str]] | None, Any]:
    if name is None:
        return None, None
    if not _AGENT_RE.fullmatch(name):
        raise HTTPException(status_code=404, detail=_AGENT_NOT_FOUND)
    row = await get_agent_by_name(db, name)
    if row is None:
        raise HTTPException(status_code=404, detail=_AGENT_NOT_FOUND)
    return allowed_sources_of(row), row


@router.get("/sources", dependencies=[Depends(require_admin)])
async def api_sources() -> dict[str, Any]:
    async with SessionLocal() as db:
        services = await list_odata_services(db)
        referrers = await odata_service_referrers(db)
        nodes = list(
            (await db.execute(select(DataCatalogNode).order_by(DataCatalogNode.id))).scalars()
        )
    by_source: dict[tuple[str, str], list[DataCatalogNode]] = {}
    for n in nodes:
        by_source.setdefault((n.source_kind, n.source_name), []).append(n)
    out = []
    for s in services:
        mine = by_source.get(("odata", s.name), [])
        source_node = next((n for n in mine if n.kind == "source"), None)
        out.append(
            {
                "kind": "odata",
                "name": s.name,
                "title": s.title,
                "destination": s.destination,
                "user_context": bool(s.user_context),
                "enabled": bool(s.enabled),
                "used_by": [r["agent"] for r in referrers.get(s.name, [])],
                "nodes": len(mine),
                "last_import": _stamp(source_node.updated_at) if source_node else None,
                "hint_completeness": _completeness(mine),
            }
        )
    return {"sources": out}


@router.get("/sources/{kind}/{name}", dependencies=[Depends(require_admin)])
async def api_source(kind: str, name: str) -> dict[str, Any]:
    if kind not in SOURCE_KINDS or not _NAME_RE.fullmatch(name):
        raise HTTPException(status_code=404, detail=_SOURCE_NOT_FOUND)
    async with SessionLocal() as db:
        nodes = await scoped_nodes(db, {(kind, name)})
        service = await get_odata_service(db, name) if kind == "odata" else None
        if not nodes and service is None:
            raise HTTPException(status_code=404, detail=_SOURCE_NOT_FOUND)
        referrers = await odata_service_referrers(db, name) if kind == "odata" else {}
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
    entities = [
        {
            "node": e.to_dict(),
            "fields": [f.to_dict() for f in nodes if f.kind == "field" and f.parent_id == e.id],
        }
        for e in nodes
        if e.kind == "entity"
    ]
    everything = {n.id: n for n in nodes} | other_nodes
    unique = {e.id: e for e in edges}.values()
    return {
        "source": source.to_dict() if source else None,
        "entities": entities,
        "functions": [f.to_dict() for f in nodes if f.kind == "function"],
        "edges": [_edge_out(e, everything) for e in unique],
        "used_by": [r["agent"] for r in referrers.get(name, [])],
    }


def _edge_out(edge: DataCatalogEdge, nodes: dict[int, DataCatalogNode]) -> dict[str, Any]:
    def end(node_id: int) -> dict[str, Any] | None:
        node = nodes.get(node_id)
        if node is None:
            return None
        return {"id": node.id, "source_name": node.source_name, "name": node.name,
                "label": node.label or node.name}

    return {
        "from": edge.from_id,
        "to": edge.to_id,
        "kind": edge.kind,
        "label": edge.label or "",
        "from_node": end(edge.from_id),
        "to_node": end(edge.to_id),
    }


def _node_id(raw: str) -> int:
    if not raw.isdigit() or len(raw) > 10 or int(raw) > _MAX_ID:
        raise HTTPException(status_code=404, detail=_NODE_NOT_FOUND)
    return int(raw)


async def _catalogue_readable(db: Any, node: DataCatalogNode) -> bool:
    """Whether the OData catalogue itself lets agents read ``node`` (what an
    import would set): a hint may narrow ``readable``, never widen it."""
    if node.source_kind != importer.ODATA:
        return bool(node.readable)
    service = await get_odata_service(db, node.source_name)
    if service is None:
        return False
    try:
        planned, _ = importer.plan_service(service)
    except (ValidationError, TypeError, ValueError):
        return False
    parent = await db.get(DataCatalogNode, node.parent_id) if node.parent_id else None
    parent_ref = (parent.kind, parent.name) if parent is not None else None
    for item in planned:
        if item.key == (node.kind, parent_ref, node.name):
            return bool(item.attrs["readable"])
    return False


@router.put("/nodes/{node_id}/hints", dependencies=[Depends(require_admin)])
async def api_put_hints(node_id: str, request: Request) -> dict[str, Any]:
    nid = _node_id(node_id)
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
        candidates = await find_sources(
            db, body.question, allowed_sources=allowed, tier=ADVANCED, scope_tier=scope_tier
        )
    return {"candidates": candidates, "scope": scope}


@router.get("/graph", dependencies=[Depends(require_admin)])
async def api_graph(
    agent: str | None = None, kind: str | None = None, q: str | None = None
) -> dict[str, Any]:
    if kind is not None and kind not in NODE_KINDS:
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
        if kind is not None:
            nodes = [n for n in nodes if n.kind == kind]
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
        {"from": n.parent_id, "to": n.id, "kind": "contains", "label": ""}
        for n in nodes
        if n.parent_id in ids
    ] + [{"from": e.from_id, "to": e.to_id, "kind": e.kind, "label": e.label or ""}
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
