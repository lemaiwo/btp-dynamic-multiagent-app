"""The tables of the data catalogue graph (HANA schema generation 8).

Three tables, portable across SQLite, Postgres and HANA:

* ``data_catalog_nodes``: one row per source, entity, field, function (and,
  later, InA query, dimension and measure). ``source_kind`` / ``source_name``
  name the record the node came from (an ``odata_services.name`` today).
  ``label``, ``purpose``, ``not_for``, ``synonyms_json`` and
  ``examples_json`` are the hints; once an admin edits them (``curated_at``
  set) an import never overwrites them. ``type_name`` is an entity's
  entity type (the join of ``same_object`` edges) or a field's EDM type.
  ``is_key`` marks a key field: the
  search answers an entity's keys without reading the (possibly megabytes
  large) service definition.
* ``data_catalog_edges``: ``navigation``, ``association``, ``foreign_key``,
  ``same_object``, ``aggregates`` between two nodes.
* ``data_catalog_vectors``: one embedding per node and model, float32 bytes
  (cosine is computed in Python over the small scoped candidate set; no HANA
  ``REAL_VECTOR`` in v1). ``text_hash`` is the SHA-256 of the text that was
  embedded: an unchanged text is never sent to the model again.

No foreign keys: the importer deletes children, edges and vectors itself, in
an order every database accepts. ``Text`` columns are never compared,
ordered or grouped in SQL (HANA's ``NCLOB``).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Index, Integer, LargeBinary, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from agents.db import Base

NODE_KINDS = ("source", "entity", "field", "query", "dimension", "measure", "function")
SOURCE_KINDS = ("odata", "ina", "generic")
EDGE_KINDS = ("navigation", "association", "foreign_key", "same_object", "aggregates")

LABEL_MAX = 200
PURPOSE_MAX = 1000
NOT_FOR_MAX = 500
NAME_MAX = 128


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_list(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


class DataCatalogNode(Base):
    __tablename__ = "data_catalog_nodes"
    __table_args__ = (
        Index("ix_data_catalog_nodes_source", "source_kind", "source_name"),
        Index("ix_data_catalog_nodes_parent", "parent_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    label: Mapped[str] = mapped_column(
        String(LABEL_MAX), nullable=False, default="", server_default=""
    )
    purpose: Mapped[str] = mapped_column(
        String(PURPOSE_MAX), nullable=False, default="", server_default=""
    )
    not_for: Mapped[str] = mapped_column(
        String(NOT_FOR_MAX), nullable=False, default="", server_default=""
    )
    synonyms_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    examples_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The entity type of an entity (``same_object`` edges join on its local
    # name across sources), the EDM type of a field.
    type_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    readable: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    writable: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_key: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    curated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def synonyms(self) -> list[str]:
        return _json_list(self.synonyms_json)

    @property
    def examples(self) -> list[str]:
        return _json_list(self.examples_json)

    def to_dict(self) -> dict[str, Any]:
        def stamp(value: datetime | None) -> str | None:
            if value is None:
                return None
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc).isoformat()

        return {
            "id": self.id,
            "kind": self.kind,
            "source_kind": self.source_kind,
            "source_name": self.source_name,
            "name": self.name,
            "label": self.label or "",
            "purpose": self.purpose or "",
            "not_for": self.not_for or "",
            "synonyms": self.synonyms,
            "examples": self.examples,
            "unit": self.unit,
            "type_name": self.type_name,
            "readable": bool(self.readable),
            "writable": bool(self.writable),
            "is_key": bool(self.is_key),
            "parent_id": self.parent_id,
            "updated_at": stamp(self.updated_at),
            "curated_at": stamp(self.curated_at),
        }


class DataCatalogEdge(Base):
    __tablename__ = "data_catalog_edges"
    __table_args__ = (
        Index("ix_data_catalog_edges_from", "from_id"),
        Index("ix_data_catalog_edges_to", "to_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_id: Mapped[int] = mapped_column(Integer, nullable=False)
    to_id: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    label: Mapped[str] = mapped_column(
        String(LABEL_MAX), nullable=False, default="", server_default=""
    )


class DataCatalogVector(Base):
    __tablename__ = "data_catalog_vectors"

    node_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    model: Mapped[str] = mapped_column(String(128), primary_key=True)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    vector: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
