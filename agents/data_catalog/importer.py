"""Build the data catalogue graph from the OData catalogue, incrementally.

``import_odata_service`` turns one ``odata_services`` row into nodes and
edges: the ``source``, an ``entity`` per entity set, a ``field`` per field
(``readable`` = selectable, ``writable`` = writable on an entity set that can
create or update, ``is_key`` for a key), a ``function`` per ENABLED operation
(``readable`` for a read, ``writable`` for a write, by the one write rule of
``agents.odata.models``), ``navigation`` edges inside the source and
``same_object`` edges between entity sets of different sources that share an
entity type name.

Incremental, because the admin's hints are worth more than the import:

* a node is matched by (kind, parent, name); an unchanged one keeps its row,
  its ``updated_at`` and its vector;
* once ``curated_at`` is set, ``label``, ``purpose``, ``not_for``,
  ``synonyms`` and ``examples`` are the admin's and are never overwritten,
  and ``readable`` stays what the admin left it but never wider than the
  catalogue (stored AND imported): a hint cannot open a field the OData
  catalogue keeps hidden;
* a node that is no longer in the definition is deleted with its edges and
  vectors.

Only sources attached to at least one ENABLED agent through an enabled
catalogue service are kept (``rebuild``): the graph is what agents can reach,
not a copy of everything an admin ever imported. The source node's
``updated_at`` is stamped by every import: it is the source's "last import".

Nothing here raises for a broken stored definition: the service is skipped
with one WARNING naming it, and its old nodes stay until it is repaired.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.data_catalog.models import (
    LABEL_MAX,
    NAME_MAX,
    NOT_FOR_MAX,
    PURPOSE_MAX,
    DataCatalogEdge,
    DataCatalogNode,
    DataCatalogVector,
    utcnow,
)

logger = logging.getLogger(__name__)

ODATA = "odata"
# Statement parameter lists are cut into batches of this size: SQLite allows
# 999 bound parameters per statement, and very long IN lists are slow
# everywhere.
BATCH = 500
_WRITE_OPS = ("create", "update", "delete")
_HINT_FIELDS = ("label", "purpose", "not_for", "synonyms_json", "examples_json")


def batched(values: list[Any], size: int = BATCH) -> list[list[Any]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


@dataclass
class _Planned:
    kind: str
    name: str
    parent: tuple[str, str] | None  # (kind, name) of the parent node
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, tuple[str, str] | None, str]:
        return (self.kind, self.parent, self.name)


def _cut(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _type_local_name(entity_type: str) -> str:
    """``API_X.A_PurchaseOrderType`` -> ``A_PurchaseOrderType``."""
    return entity_type.rsplit(".", 1)[-1] if entity_type else ""


def plan_service(service: Any) -> tuple[list[_Planned], list[tuple[str, str, str]]]:
    """The nodes a service definition makes, in parent-first order, and its
    navigation edges as ``(from entity, to entity, label)``."""
    from agents.odata.models import ServiceDefinition

    definition = ServiceDefinition.model_validate(service.definition)
    # The service's kind decides which calls are writes (a GET function import
    # of a `generic` service is a read: `operation_is_write`).
    kind = getattr(service, "kind", None) or "odata"
    readable_sets = [e for e in definition.entity_sets if {"list", "get"} & set(e.operations)]
    enabled_ops = [o for o in definition.operations if o.enabled]
    source = _Planned(
        "source",
        service.name,
        None,
        {
            "label": _cut(service.title, LABEL_MAX),
            "purpose": _cut(service.purpose, PURPOSE_MAX),
            "not_for": _cut(service.not_for, NOT_FOR_MAX),
            "readable": int(
                bool(readable_sets) or any(not o.is_write(kind) for o in enabled_ops)
            ),
            "writable": int(definition.has_write(kind)),
            "is_key": 0,
            "type_name": None,
            "synonyms_json": None,
            "examples_json": None,
        },
    )
    planned = [source]
    navigations: list[tuple[str, str, str]] = []
    source_ref = ("source", service.name)
    for es in definition.entity_sets:
        can_write_fields = bool({"create", "update"} & set(es.operations))
        examples = [_cut(ex.description, 200) for ex in es.examples][:20]
        planned.append(
            _Planned(
                "entity",
                es.name,
                source_ref,
                {
                    "label": _cut(es.title, LABEL_MAX),
                    "purpose": _cut(es.description, PURPOSE_MAX),
                    "not_for": "",
                    "readable": int(bool({"list", "get"} & set(es.operations))),
                    "writable": int(any(op in _WRITE_OPS for op in es.operations)),
                    "is_key": 0,
                    "type_name": _cut(es.entity_type, 200) or None,
                    "synonyms_json": None,
                    "examples_json": json.dumps(examples) if examples else None,
                },
            )
        )
        keys = {k.name for k in es.keys}
        for fd in es.fields:
            planned.append(
                _Planned(
                    "field",
                    fd.name,
                    ("entity", es.name),
                    {
                        "label": _cut(fd.label, LABEL_MAX),
                        "purpose": _cut(fd.hint, PURPOSE_MAX),
                        "not_for": "",
                        "readable": int(fd.selectable),
                        "writable": int(fd.writable and can_write_fields),
                        "is_key": int(fd.name in keys),
                        "type_name": _cut(fd.type, 200) or None,
                        "synonyms_json": None,
                        "examples_json": None,
                    },
                )
            )
        for nav in es.navigations:
            navigations.append((es.name, nav.target, _cut(nav.description or nav.name, LABEL_MAX)))
    for op in enabled_ops:
        planned.append(
            _Planned(
                "function",
                op.name,
                source_ref,
                {
                    "label": _cut(op.title, LABEL_MAX),
                    "purpose": _cut(op.description, PURPOSE_MAX),
                    "not_for": "",
                    "readable": int(not op.is_write(kind)),
                    "writable": int(op.is_write(kind)),
                    "is_key": 0,
                    "type_name": None,
                    "synonyms_json": None,
                    "examples_json": None,
                },
            )
        )
    for p in planned:
        p.name = p.name[:NAME_MAX]
    return planned, navigations


@dataclass
class ImportResult:
    imported: int = 0  # nodes created or changed
    removed: int = 0  # nodes deleted
    changed_ids: list[int] = field(default_factory=list)

    def add(self, other: ImportResult) -> None:
        self.imported += other.imported
        self.removed += other.removed
        self.changed_ids.extend(other.changed_ids)


async def _source_nodes(
    db: AsyncSession, source_kind: str, source_name: str
) -> list[DataCatalogNode]:
    rows = await db.execute(
        select(DataCatalogNode)
        .where(
            DataCatalogNode.source_kind == source_kind,
            DataCatalogNode.source_name == source_name,
        )
        .order_by(DataCatalogNode.id)
    )
    return list(rows.scalars())


async def delete_nodes(db: AsyncSession, ids: list[int]) -> None:
    """Nodes with their edges (either end) and vectors, in batches."""
    for chunk in batched(ids):
        await db.execute(
            delete(DataCatalogEdge).where(
                or_(DataCatalogEdge.from_id.in_(chunk), DataCatalogEdge.to_id.in_(chunk))
            )
        )
        await db.execute(delete(DataCatalogVector).where(DataCatalogVector.node_id.in_(chunk)))
        await db.execute(delete(DataCatalogNode).where(DataCatalogNode.id.in_(chunk)))


async def remove_source(db: AsyncSession, source_kind: str, source_name: str) -> int:
    nodes = await _source_nodes(db, source_kind, source_name)
    await delete_nodes(db, [n.id for n in nodes])
    return len(nodes)


async def import_odata_service(db: AsyncSession, service_row: Any) -> ImportResult:
    """Bring the nodes and edges of one catalogue service up to date.

    Does not commit: the caller owns the transaction. A definition that does
    not validate leaves the stored nodes alone (one WARNING).
    """
    result = ImportResult()
    try:
        planned, navigations = plan_service(service_row)
    except (ValidationError, TypeError, ValueError):
        logger.warning(
            "data catalogue: OData service %s has a definition that does not validate; "
            "its nodes are left as they are",
            service_row.name,
        )
        return result
    existing = await _source_nodes(db, ODATA, service_row.name)
    by_id = {n.id: n for n in existing}

    def ref_of(node: DataCatalogNode) -> tuple[str, str] | None:
        parent = by_id.get(node.parent_id) if node.parent_id is not None else None
        return (parent.kind, parent.name) if parent is not None else None

    by_key = {(n.kind, ref_of(n), n.name): n for n in existing}
    now = utcnow()
    kept: dict[tuple[str, str], DataCatalogNode] = {}
    seen_ids: set[int] = set()
    for item in planned:
        node = by_key.get(item.key)
        parent_id = kept[item.parent].id if item.parent is not None else None
        attrs = dict(item.attrs, parent_id=parent_id)
        if node is None:
            node = DataCatalogNode(
                kind=item.kind,
                source_kind=ODATA,
                source_name=service_row.name,
                name=item.name,
                updated_at=now,
                **attrs,
            )
            db.add(node)
            await db.flush()
            result.imported += 1
            result.changed_ids.append(node.id)
        else:
            if node.curated_at is not None:
                for hint in _HINT_FIELDS:
                    attrs.pop(hint, None)
                attrs["readable"] = int(bool(node.readable) and bool(attrs["readable"]))
            changed = any(getattr(node, k) != v for k, v in attrs.items())
            if changed:
                for k, v in attrs.items():
                    setattr(node, k, v)
                node.updated_at = now
                result.imported += 1
                result.changed_ids.append(node.id)
            elif item.kind == "source":
                node.updated_at = now  # the source's "last import"
        seen_ids.add(node.id)
        kept[(item.kind, item.name)] = node
    gone = [n.id for n in existing if n.id not in seen_ids]
    await delete_nodes(db, gone)
    result.removed += len(gone)

    entity_ids = [n.id for (kind, _), n in kept.items() if kind == "entity"]
    for chunk in batched(entity_ids):
        await db.execute(
            delete(DataCatalogEdge).where(
                DataCatalogEdge.kind.in_(("navigation", "same_object")),
                or_(DataCatalogEdge.from_id.in_(chunk), DataCatalogEdge.to_id.in_(chunk)),
            )
        )
    for src, target, label in navigations:
        a, b = kept.get(("entity", src)), kept.get(("entity", target))
        if a is not None and b is not None:
            db.add(DataCatalogEdge(from_id=a.id, to_id=b.id, kind="navigation", label=label))
    await _link_same_objects(db, service_row.name, [kept[k] for k in kept if k[0] == "entity"])
    await db.flush()
    return result


async def _link_same_objects(
    db: AsyncSession, source_name: str, entities: list[DataCatalogNode]
) -> None:
    """``same_object`` edges from this source's entities to the entities of
    OTHER sources with the same entity type name (local part)."""
    mine = {}
    for node in entities:
        local = _type_local_name(node.type_name or "")
        if local:
            mine.setdefault(local, []).append(node)
    if not mine:
        return
    others = await db.execute(
        select(DataCatalogNode).where(
            DataCatalogNode.kind == "entity",
            DataCatalogNode.type_name.is_not(None),
            DataCatalogNode.source_name != source_name,
        )
    )
    for other in others.scalars():
        for node in mine.get(_type_local_name(other.type_name or ""), []):
            db.add(
                DataCatalogEdge(
                    from_id=node.id, to_id=other.id, kind="same_object", label=node.type_name
                )
            )


async def wanted_odata_services(db: AsyncSession) -> dict[str, Any]:
    """``{name: row}`` of the enabled catalogue services that an ENABLED
    agent attaches: the only ones the graph holds."""
    from agents.db import list_agents, list_odata_services, odata_entries

    names: set[str] = set()
    for agent in await list_agents(db):
        if not agent.enabled:
            continue
        for block in odata_entries(agent.mcp_servers):
            services = block.get("services")
            if isinstance(services, list):
                names.update(n for n in services if isinstance(n, str))
    if not names:
        return {}
    return {
        row.name: row
        for row in await list_odata_services(db)
        if row.name in names and row.enabled
    }


async def stored_sources(db: AsyncSession) -> set[tuple[str, str]]:
    rows = await db.execute(
        select(DataCatalogNode.source_kind, DataCatalogNode.source_name)
        .where(DataCatalogNode.kind == "source")
        .distinct()
    )
    return {(k, n) for k, n in rows.all()}


async def rebuild(
    db: AsyncSession, source: tuple[str, str] | None = None, *, embed: bool = True
) -> dict[str, int]:
    """Import the wanted sources (all, or the one named) and drop the rest:
    ``{imported, removed, vectors}``. Commits."""
    from agents.data_catalog.embeddings import refresh_vectors

    total = ImportResult()
    wanted = await wanted_odata_services(db)
    if source is None:
        for name in sorted(wanted):
            total.add(await import_odata_service(db, wanted[name]))
        for kind, name in sorted(await stored_sources(db)):
            if kind == ODATA and name not in wanted:
                total.removed += await remove_source(db, kind, name)
    elif source[0] == ODATA:
        if source[1] in wanted:
            total.add(await import_odata_service(db, wanted[source[1]]))
        else:
            total.removed += await remove_source(db, *source)
    await db.commit()
    vectors = 0
    if embed:
        vectors = await refresh_vectors(db, scope=source)
        await db.commit()
    return {"imported": total.imported, "removed": total.removed, "vectors": vectors}


async def rebuild_safely(
    sources: list[tuple[str, str]] | None = None, *, what: str = "change"
) -> dict[str, int] | None:
    """``rebuild`` in its own session after a COMMITTED change, never raising.

    Called after a catalogue save, an agent save and at startup: the change
    is stored, so a failed import is logged (the exception class only, its
    text can carry a database URL) and the save answers as it would have.
    ``None`` names every source.
    """
    from agents.db import SessionLocal

    totals = {"imported": 0, "removed": 0, "vectors": 0}
    try:
        async with SessionLocal() as db:
            for source in sources if sources is not None else [None]:
                counts = await rebuild(db, source)
                for k in totals:
                    totals[k] += counts[k]
    except Exception as exc:  # noqa: BLE001 - see the docstring
        logger.error("data catalogue: rebuild after %s failed (%s)", what, type(exc).__name__)
        return None
    if any(totals.values()):
        logger.info(
            "data catalogue: rebuilt after %s: %d imported, %d removed, %d vectors",
            what, totals["imported"], totals["removed"], totals["vectors"],
        )
    return totals
