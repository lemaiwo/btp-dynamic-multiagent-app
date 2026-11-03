"""The ``same_object`` pass of a catalogue import: which nodes of different
sources are one SAP object, by NAME, never by data.

``link_same_objects`` runs at the end of every import of a source
(`importer._replace_imported_edges`), after the source's imported
``navigation`` and ``same_object`` edges were dropped, and links the
source's entities and queries to the nodes of every OTHER source:

1. entities whose entity types share a local name, exactly (in any two
   sources, whatever their kinds);
2. across kinds, the names SAP gives one repository object (addendum (k),
   "Cross-kind links" 1; `_same_by_name`).

A ``generic`` entity is the object its entity TYPE names, never its set:
``ZCL_AGENT_DATA_MODEL`` serves an allow-listed table or view ``<OBJ>`` as
the set ``<OBJ>Set`` of the type ``ZAGENT_DATA_SRV.<OBJ>`` (the recorded
``$metadata`` of ``ZAGENT_DATA_SRV``, ``SCARRSet`` / ``SCARR``). A generic
set without a type names no object and joins nothing across kinds.

One ``same_object`` per pair: a pair an admin linked as ``same_object``
(`_curated_pairs`), or one this pass already made, gets none. Nothing here
commits; the importer owns the transaction.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.data_catalog.models import (
    GENERIC,
    INA,
    LABEL_MAX,
    ODATA,
    DataCatalogEdge,
    DataCatalogNode,
    batched,
    cut,
)

# Node kinds an import links with ``navigation`` / ``same_object`` edges.
LINKED_KINDS = ("entity", "query")
# The InA prefix of a query generated from a CDS view: ``2C<view>``.
_CDS_QUERY_PREFIX = "2C"


def _type_local_name(entity_type: str) -> str:
    """``API_X.A_PurchaseOrderType`` -> ``A_PurchaseOrderType``."""
    return entity_type.rsplit(".", 1)[-1] if entity_type else ""


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


def _generic_object(node: DataCatalogNode) -> str:
    """The table or view a ``generic`` entity reads: its entity type's local
    name, as SAP wrote it (``ZAGENT_DATA_SRV.SCARR`` -> ``SCARR``; the set
    is ``SCARRSet``); ``""`` for a set without a type."""
    return _type_local_name(node.type_name or "")


def _join_names(node: DataCatalogNode) -> set[str]:
    """The upper-cased names a node may share with a node of another kind
    (the index of the cross-kind pass; `_same_by_name` decides)."""
    if node.kind == "query":
        view = _cds_view_of(node)
        return {view} if view else set()
    if node.kind != "entity":
        return set()
    if node.source_kind == GENERIC:
        obj = _generic_object(node)
        return {obj.upper()} if obj else set()
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

    * (a) an InA query ``2C<view>`` and a ``generic`` entity whose type
      names ``<view>`` (a CDS view on SAP's allow-list, served as the set
      ``<view>Set``);
    * an InA query ``2C<view>`` and an ``odata`` entity whose entity type's
      local name is ``<view>`` or ``<view>Type``;
    * (b) a ``generic`` entity and an ``odata`` entity whose entity type's
      local name is exactly the generic entity's object (its type's local
      name).

    The two InA rules compare upper-cased: InA names a CDS query in upper
    case, while the OData type of a CDS entity keeps its camel case
    (``ZC_OnCallType``). Rule (b) compares exactly, as the entity type pass
    does: a table ``PRODUCT`` says nothing about a type ``Product`` of
    another service, and an imported edge is not one an admin can remove.
    Under SAP's naming rule (b) is what the entity type pass already joins;
    it is kept so the rule does not depend on that pass."""
    ends = {a.source_kind: a, b.source_kind: b}
    if len(ends) != 2:
        return ""
    query, generic, odata = ends.get(INA), ends.get(GENERIC), ends.get(ODATA)
    view = _cds_view_of(query) if query is not None else ""
    if generic is not None and generic.kind != "entity":
        return ""
    if odata is not None and odata.kind != "entity":
        return ""
    obj = _generic_object(generic) if generic is not None else ""
    if view and generic is not None:
        return view if obj.upper() == view else ""
    if view and odata is not None:
        return view if _upper_type_local(odata) in (view, view + "TYPE") else ""
    if generic is not None and odata is not None:
        local = _type_local_name(odata.type_name or "")
        return obj if obj and local == obj else ""
    return ""


async def link_same_objects(
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
            DataCatalogNode.kind.in_(LINKED_KINDS),
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
                    label=cut(label, LABEL_MAX),
                )
            )
