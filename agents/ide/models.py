"""SQLAlchemy models for IDE sessions.

All tables hang off ``IdeSession`` with ``ON DELETE CASCADE``. SQLite only
enforces that with ``PRAGMA foreign_keys=ON``, which this app does not set,
so ``agents.ide.store`` also deletes children explicitly.

Ownership lives on ``IdeSession.owner`` only; child rows are reached through
an owned session, never directly by id.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from agents.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    # Python-side default next to the server default: microsecond precision
    # keeps rows created in the same second in insertion order on SQLite.
    return datetime.now(timezone.utc)


def _session_fk() -> ForeignKey:
    return ForeignKey("ide_sessions.id", ondelete="CASCADE")


class IdeSession(Base):
    __tablename__ = "ide_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    target: Mapped[str] = mapped_column(String(64), nullable=False)
    stage: Mapped[str] = mapped_column(String(16), nullable=False, default="chat")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="idle"
    )  # idle|running
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    requests_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    todos_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )

    def meta(self) -> dict:
        """Metadata only -- the admin overview never sees content."""
        return {
            "id": self.id,
            "owner": self.owner,
            "title": self.title,
            "target": self.target,
            "stage": self.stage,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class IdeMessage(Base):
    __tablename__ = "ide_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        String(36), _session_fk(), nullable=False, index=True
    )
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    role: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # user|assistant|system
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    activity_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )


class IdeArtifact(Base):
    __tablename__ = "ide_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        String(36), _session_fk(), nullable=False, index=True
    )
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # design|plan|review|note
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )


class IdeWorkspaceFile(Base):
    __tablename__ = "ide_workspace_files"
    __table_args__ = (
        UniqueConstraint("session_id", "path", name="uq_ide_workspace_file_path"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        String(36), _session_fk(), nullable=False, index=True
    )
    path: Mapped[str] = mapped_column(String(200), nullable=False)
    object_type: Mapped[str | None] = mapped_column(String(8), nullable=True)
    object_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    origin_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(8), nullable=False)  # read|modified|new
    lint_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )


class IdeConventions(Base):
    """Per-target conventions, maintained by an admin."""

    __tablename__ = "ide_conventions"

    target: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    # BTP destination of the target's ARC-1 (read-only) server.
    destination: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    namespace: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    package: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    atc_variant: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    clean_core_level: Mapped[str] = mapped_column(
        String(1), nullable=False, default="A"
    )
    free_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )


IDE_CHILD_MODELS = (IdeMessage, IdeArtifact, IdeWorkspaceFile)
