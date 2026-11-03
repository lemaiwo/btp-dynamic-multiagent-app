"""Build the data catalogue graph from the registrations, incrementally.

``import_odata_service`` turns one ``odata_services`` row into nodes and
edges, under the row's kind as ``source_kind`` (``odata``, or ``generic`` for
a ZAGENT_DATA registration): the ``source``, an ``entity`` per entity set, a
``field`` per field (``readable`` = selectable, ``writable`` = writable on an
entity set that can create or update, ``is_key`` for a key), a ``function``
per ENABLED operation (``readable`` for a read, ``writable`` for a write, by
the one write rule of ``agents.odata.models`` for the service's kind) and
``navigation`` edges inside the source.

``import_ina_query`` does the same for one ``ina_queries`` row (addendum
(k), part 2): the ``source``, one ``query`` node (the InA object name; its
label is the registration's title) and a ``dimension`` and ``measure`` per
member ``Describe`` stored (``unit`` on a measure); everything readable,
nothing writable (InA only reads). A query not described yet is the source
and the query alone. Variables are no nodes.

``same_object`` edges join nodes of different sources by NAME, never by
data (``_link_same_objects``): entity sets that share an entity type name,
and across kinds the names SAP gives one repository object (an InA query
``2C<view>``, a ``generic`` table or view ``<view>``, an ``odata`` entity
type ``<view>`` / ``<view>Type``).

Edges an admin drew (``curated_at`` set) are never deleted by an import, and
an import does not add a ``same_object`` edge beside a drawn ``same_object``
of the same pair (one link per kind per pair; a drawn ``aggregates`` leaves
the imported ``same_object`` in place). A node that disappears takes its
curated edges with it.

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
catalogue service or InA query are kept (``rebuild``): the graph is what
agents can reach, not a copy of everything an admin ever imported. The source node's
``updated_at`` is stamped by every import: it is the source's "last import".

Nothing here raises for a broken stored definition: the service or query is
skipped with one WARNING naming it, and its old nodes stay until it is
repaired.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

# The kind constants live with the tables; imported here too, where the
# admin routes read them (`importer.ODATA`).
from agents.data_catalog.models import (
    CATALOGUE_KINDS,
    GENERIC,
    INA,
    LABEL_MAX,
    NAME_MAX,
    NOT_FOR_MAX,
    ODATA,
    PURPOSE_MAX,
    UNIT_MAX,
    DataCatalogEdge,
    DataCatalogNode,
    DataCatalogVector,
    utcnow,
)
from agents.data_catalog.scope import listed_names

logger = logging.getLogger(__name__)

# Node kinds an import links with ``navigation`` / ``same_object`` edges.
_LINKED_KINDS = ("entity", "query")
_IMPORTED_EDGE_KINDS = ("navigation", "same_object")
# The InA prefix of a query generated from a CDS view: ``2C<view>``.
_CDS_QUERY_PREFIX = "2C"
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


def service_kind(row: Any) -> str:
    """``generic`` for a ZAGENT_DATA registration, ``odata`` for every other
    catalogue row (also one without the column): the ``source_kind`` of its
    nodes and the kind `operation_is_write` decides its writes by, which is
    exactly ``"generic"`` or not."""
    return GENERIC if getattr(row, "kind", None) == GENERIC else ODATA


def plan_service(service: Any) -> tuple[list[_Planned], list[tuple[str, str, str]]]:
    """The nodes a service definition makes, in parent-first order, and its
    navigation edges as ``(from entity, to entity, label)``."""
    from agents.odata.models import ServiceDefinition

    definition = ServiceDefinition.model_validate(service.definition)
    # The service's kind decides which calls are writes (a GET function import
    # of a `generic` service is a read: `operation_is_write`).
    kind = service_kind(service)
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


def _ina_attrs(
    label: Any, *, purpose: Any = "", not_for: Any = "", unit: Any = None, type_name: Any = None
) -> dict[str, Any]:
    """The columns of an InA node: readable, never writable (InA only reads)."""
    return {
        "label": _cut(label, LABEL_MAX),
        "purpose": _cut(purpose, PURPOSE_MAX),
        "not_for": _cut(not_for, NOT_FOR_MAX),
        "readable": 1,
        "writable": 0,
        "is_key": 0,
        "type_name": _cut(type_name, 200) or None,
        "unit": _cut(unit, UNIT_MAX) or None,
        "synonyms_json": None,
        "examples_json": None,
    }


def plan_ina_query(row: Any) -> list[_Planned]:
    """The nodes an InA query registration makes, in parent-first order.

    The query node carries the InA object name as name and ``type_name``
    (the cross-kind join reads it) and the registration's title as label;
    its purpose and "not for" stay empty, so the search shows the source's
    (one place for the admin to write them). A stored description that does
    not validate raises (``ValueError`` / ``ValidationError``).

    A dimension's ``key_field`` is not read: in InA it names the member's
    key attribute (how a member is identified on an axis), not the identity
    of a result row, so no InA node is ``is_key`` and a query's search
    candidate answers no ``keys``.
    """
    from agents.ina.models import InaDefinition

    raw = row.definition_json
    definition = (
        InaDefinition() if raw is None else InaDefinition.model_validate(json.loads(raw))
    )
    query_name = str(row.query)[:NAME_MAX]
    planned = [
        _Planned(
            "source",
            row.name,
            None,
            _ina_attrs(row.title, purpose=row.purpose, not_for=row.not_for),
        ),
        _Planned(
            "query", query_name, ("source", row.name), _ina_attrs(row.title, type_name=row.query)
        ),
    ]
    query_ref = ("query", query_name)
    for dim in definition.dimensions:
        planned.append(_Planned("dimension", dim.name, query_ref, _ina_attrs(dim.label)))
    for measure in definition.measures:
        attrs = _ina_attrs(measure.label, unit=measure.unit)
        planned.append(_Planned("measure", measure.name, query_ref, attrs))
    for p in planned:
        p.name = p.name[:NAME_MAX]
    return planned


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
    """Bring the nodes and edges of one catalogue service up to date, under
    the service's kind (`service_kind`).

    Does not commit: the caller owns the transaction. A definition that does
    not validate leaves the stored nodes alone (one WARNING).
    """
    try:
        planned, navigations = plan_service(service_row)
    except (ValidationError, TypeError, ValueError):
        logger.warning(
            "data catalogue: OData service %s has a definition that does not validate; "
            "its nodes are left as they are",
            service_row.name,
        )
        return ImportResult()
    source = (service_kind(service_row), service_row.name)
    result, kept = await _import_planned(db, source, planned)
    await _replace_imported_edges(db, source, kept, navigations)
    await db.flush()
    return result


async def import_ina_query(db: AsyncSession, query_row: Any) -> ImportResult:
    """Bring the nodes and edges of one InA query up to date, as
    `import_odata_service` does for a catalogue service (same keep, update
    and delete rules; no navigations). Does not commit."""
    try:
        planned = plan_ina_query(query_row)
    except (ValidationError, TypeError, ValueError):
        logger.warning(
            "data catalogue: InA query %s has a stored description that does not validate; "
            "its nodes are left as they are",
            query_row.name,
        )
        return ImportResult()
    source = (INA, query_row.name)
    result, kept = await _import_planned(db, source, planned)
    await _replace_imported_edges(db, source, kept, [])
    await db.flush()
    return result


async def _import_planned(
    db: AsyncSession, source: tuple[str, str], planned: list[_Planned]
) -> tuple[ImportResult, dict[tuple[str, str], DataCatalogNode]]:
    """Match ``planned`` against the stored nodes of ``source``: create the
    new ones, update the changed ones (never an admin's hints, ``readable``
    never wider than the plan), delete the ones the plan no longer has.
    Answers the counts and the kept nodes by ``(kind, name)``."""
    source_kind, source_name = source
    result = ImportResult()
    existing = await _source_nodes(db, source_kind, source_name)
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
                source_kind=source_kind,
                source_name=source_name,
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
    return result, kept


async def _replace_imported_edges(
    db: AsyncSession,
    source: tuple[str, str],
    kept: dict[tuple[str, str], DataCatalogNode],
    navigations: list[tuple[str, str, str]],
) -> None:
    """Drop the imported ``navigation`` and ``same_object`` edges of the
    source's entities and queries and make them again; a curated edge stays
    (``curated_at`` set: an admin drew it)."""
    linked = [n for (kind, _), n in kept.items() if kind in _LINKED_KINDS]
    ids = [n.id for n in linked]
    for chunk in batched(ids):
        await db.execute(
            delete(DataCatalogEdge).where(
                DataCatalogEdge.kind.in_(_IMPORTED_EDGE_KINDS),
                DataCatalogEdge.curated_at.is_(None),
                or_(DataCatalogEdge.from_id.in_(chunk), DataCatalogEdge.to_id.in_(chunk)),
            )
        )
    for src, target, label in navigations:
        a, b = kept.get(("entity", src)), kept.get(("entity", target))
        if a is not None and b is not None:
            db.add(DataCatalogEdge(from_id=a.id, to_id=b.id, kind="navigation", label=label))
    await _link_same_objects(db, source, linked, await _curated_pairs(db, ids))


async def _curated_pairs(db: AsyncSession, ids: list[int]) -> set[frozenset[int]]:
    """The node pairs an admin linked as ``same_object``, of the edges that
    touch ``ids``: one link per kind per pair, so a drawn ``aggregates`` does
    not keep the import's ``same_object`` out."""
    pairs: set[frozenset[int]] = set()
    for chunk in batched(ids):
        rows = await db.execute(
            select(DataCatalogEdge.from_id, DataCatalogEdge.to_id).where(
                DataCatalogEdge.curated_at.is_not(None),
                DataCatalogEdge.kind == "same_object",
                or_(DataCatalogEdge.from_id.in_(chunk), DataCatalogEdge.to_id.in_(chunk)),
            )
        )
        pairs.update(frozenset(row) for row in rows.all())
    return pairs


def _cds_view_of(node: DataCatalogNode) -> str:
    """``2CZC_ONCALL`` -> ``ZC_ONCALL`` for an InA query generated from a CDS
    view; ``""`` for any other node."""
    name = node.name.upper() if node.source_kind == INA and node.kind == "query" else ""
    if name.startswith(_CDS_QUERY_PREFIX) and len(name) > len(_CDS_QUERY_PREFIX):
        return name[len(_CDS_QUERY_PREFIX) :]
    return ""


def _upper_type_local(node: DataCatalogNode) -> str:
    return _type_local_name(node.type_name or "").upper()


def _join_names(node: DataCatalogNode) -> set[str]:
    """The upper-cased names a node may share with a node of another kind
    (the index of the cross-kind pass; `_same_by_name` decides)."""
    if node.kind == "query":
        view = _cds_view_of(node)
        return {view} if view else set()
    if node.kind != "entity":
        return set()
    if node.source_kind == GENERIC:
        return {node.name.upper()}
    if node.source_kind == ODATA:
        local = _upper_type_local(node)
        if not local:
            return set()
        if local.endswith("TYPE") and len(local) > len("TYPE"):
            return {local, local[: -len("TYPE")]}
        return {local}
    return set()


def _same_by_name(a: DataCatalogNode, b: DataCatalogNode) -> str:
    """The name under which ``a`` and ``b``, of two different source kinds,
    are one SAP object, or ``""`` (addendum (k), "Cross-kind links" 1):

    * an InA query ``2C<view>`` and a ``generic`` entity ``<view>`` (a CDS
      view on SAP's allow-list);
    * an InA query ``2C<view>`` and an ``odata`` entity whose entity type's
      local name is ``<view>`` or ``<view>Type``;
    * a ``generic`` entity (a table or view name) and an ``odata`` entity
      whose entity type's local name is exactly that name.

    The two InA rules compare upper-cased: InA names a CDS query in upper
    case, while the OData type of a CDS entity keeps its camel case
    (``ZC_OnCallType``). The generic-to-OData rule compares exactly, as the
    entity type pass does: a table ``PRODUCT`` says nothing about a type
    ``Product`` of another service, and an imported edge is not one an admin
    can remove."""
    ends = {a.source_kind: a, b.source_kind: b}
    if len(ends) != 2:
        return ""
    query, generic, odata = ends.get(INA), ends.get(GENERIC), ends.get(ODATA)
    view = _cds_view_of(query) if query is not None else ""
    if generic is not None and generic.kind != "entity":
        return ""
    if odata is not None and odata.kind != "entity":
        return ""
    if view and generic is not None:
        return view if generic.name.upper() == view else ""
    if view and odata is not None:
        return view if _upper_type_local(odata) in (view, view + "TYPE") else ""
    if generic is not None and odata is not None:
        local = _type_local_name(odata.type_name or "")
        return generic.name if generic.name and local == generic.name else ""
    return ""


async def _link_same_objects(
    db: AsyncSession,
    source: tuple[str, str],
    mine: list[DataCatalogNode],
    taken: set[frozenset[int]],
) -> None:
    """``same_object`` edges from this source's entities and queries to the
    nodes of OTHER sources that are the same object: entities with the same
    entity type name (local part, exactly), then the cross-kind names of
    `_same_by_name`. One ``same_object`` per pair: a pair in ``taken`` (an
    admin's ``same_object``, or one this pass made) gets none."""
    by_type: dict[str, list[DataCatalogNode]] = {}
    by_name: dict[str, list[DataCatalogNode]] = {}
    for node in mine:
        local = _type_local_name(node.type_name or "") if node.kind == "entity" else ""
        if local:
            by_type.setdefault(local, []).append(node)
        for name in _join_names(node):
            by_name.setdefault(name, []).append(node)
    if not by_type and not by_name:
        return
    others = await db.execute(
        select(DataCatalogNode)
        .where(
            DataCatalogNode.kind.in_(_LINKED_KINDS),
            or_(
                DataCatalogNode.source_kind != source[0],
                DataCatalogNode.source_name != source[1],
            ),
        )
        .order_by(DataCatalogNode.id)
    )
    for other in others.scalars():
        found: list[tuple[DataCatalogNode, str]] = []
        if other.kind == "entity":
            local = _type_local_name(other.type_name or "")
            found.extend((node, node.type_name or "") for node in by_type.get(local, []))
        for name in _join_names(other):
            for node in by_name.get(name, []):
                shared = _same_by_name(node, other)
                if shared:
                    found.append((node, shared))
        for node, label in found:
            pair = frozenset((node.id, other.id))
            if pair in taken:
                continue
            taken.add(pair)
            db.add(
                DataCatalogEdge(
                    from_id=node.id,
                    to_id=other.id,
                    kind="same_object",
                    label=_cut(label, LABEL_MAX),
                )
            )


async def _names_enabled_agents_attach(
    db: AsyncSession, entries: Callable[[list[dict[str, Any]]], list[dict[str, Any]]], key: str
) -> set[str]:
    """The names listed under ``key`` in the ``entries`` blocks of every
    ENABLED agent (`listed_names`, the scope's own reading)."""
    from agents.db import list_agents

    names: set[str] = set()
    for agent in await list_agents(db):
        if agent.enabled:
            names |= listed_names(entries(agent.mcp_servers), key)
    return names


async def wanted_odata_services(db: AsyncSession) -> dict[str, Any]:
    """``{name: row}`` of the enabled catalogue services (either kind) that
    an ENABLED agent attaches: the only ones the graph holds."""
    from agents.db import list_odata_services, odata_entries

    names = await _names_enabled_agents_attach(db, odata_entries, "services")
    if not names:
        return {}
    return {
        row.name: row
        for row in await list_odata_services(db)
        if row.name in names and row.enabled
    }


async def wanted_ina_queries(db: AsyncSession) -> dict[str, Any]:
    """``{name: row}`` of the enabled InA queries that an ENABLED agent names
    in a ``builtin:ina`` entry: the only ones the graph holds."""
    from agents.db import ina_entries, list_ina_queries

    names = await _names_enabled_agents_attach(db, ina_entries, "queries")
    if not names:
        return {}
    return {
        row.name: row
        for row in await list_ina_queries(db)
        if row.name in names and row.enabled
    }


async def _has_nodes(db: AsyncSession, source_kind: str, source_name: str) -> bool:
    row = await db.execute(
        select(DataCatalogNode.id)
        .where(
            DataCatalogNode.source_kind == source_kind,
            DataCatalogNode.source_name == source_name,
        )
        .limit(1)
    )
    return row.first() is not None


async def _follow_kind(db: AsyncSession, service_row: Any) -> int:
    """Move the nodes of a catalogue service stored under the OTHER catalogue
    kind to the service's kind, in place (one conditional ``UPDATE`` of
    ``source_kind``): a kind change of the registration is no reason to
    lose what an admin wrote. Ids stay, so hints, a narrowed ``readable``,
    vectors and curated edges stay; the import that follows brings the rest
    up to date. Should both kinds hold nodes of that name, the service's own
    kind is kept and the other dropped. Answers the nodes dropped."""
    kind = service_kind(service_row)
    name = service_row.name
    removed = 0
    for other in CATALOGUE_KINDS:
        if other == kind or not await _has_nodes(db, other, name):
            continue
        if await _has_nodes(db, kind, name):
            removed += await remove_source(db, other, name)
            continue
        await db.execute(
            update(DataCatalogNode)
            .where(DataCatalogNode.source_kind == other, DataCatalogNode.source_name == name)
            .values(source_kind=kind)
        )
    return removed


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
    ``{imported, removed, vectors}``. Commits.

    A catalogue source is named ``("odata", name)`` or ``("generic", name)``
    alike (`CATALOGUE_KINDS`): the row's own kind decides, and nodes of that
    name stored under the other kind (a service whose kind changed) are
    moved to it in place first (`_follow_kind`), so the import that follows
    keeps their ids, the admin's hints, vectors and curated edges. What goes
    is dropped before anything is imported, so a stale node is never linked.
    """
    from agents.data_catalog.embeddings import refresh_vectors

    total = ImportResult()
    scope = source
    if source is None:
        services = await wanted_odata_services(db)
        queries = await wanted_ina_queries(db)
        for name in sorted(services):
            total.removed += await _follow_kind(db, services[name])
        for kind, name in sorted(await stored_sources(db)):
            if kind in CATALOGUE_KINDS:
                stale = name not in services or service_kind(services[name]) != kind
            else:
                stale = kind == INA and name not in queries
            if stale:
                total.removed += await remove_source(db, kind, name)
        for name in sorted(services):
            total.add(await import_odata_service(db, services[name]))
        for name in sorted(queries):
            total.add(await import_ina_query(db, queries[name]))
    elif source[0] in CATALOGUE_KINDS:
        row = (await wanted_odata_services(db)).get(source[1])
        if row is None:
            for kind in CATALOGUE_KINDS:
                total.removed += await remove_source(db, kind, source[1])
        else:
            total.removed += await _follow_kind(db, row)
            total.add(await import_odata_service(db, row))
            scope = (service_kind(row), row.name)
    elif source[0] == INA:
        row = (await wanted_ina_queries(db)).get(source[1])
        if row is not None:
            total.add(await import_ina_query(db, row))
        else:
            total.removed += await remove_source(db, *source)
    await db.commit()
    vectors = 0
    if embed:
        vectors = await refresh_vectors(db, scope=scope)
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
