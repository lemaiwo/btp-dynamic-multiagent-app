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
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
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
    # change|diagnose. server_default: existing 1a rows become "change".
    session_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="change", server_default="change"
    )
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
            "type": self.session_type,
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
    # Only non-production targets accept diagnose sessions and trace approvals.
    non_production: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )


class IdeFinding(Base):
    """Something a diagnose run found: metadata, plus the detail text when
    the target is ``non_production``."""

    __tablename__ = "ide_findings"
    __table_args__ = (
        UniqueConstraint("session_id", "kind", "ref_id", name="uq_ide_finding_ref"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        String(36), _session_fk(), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # dump|trace|gateway_error|auth_check|odata_call
    ref_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    program: Mapped[str | None] = mapped_column(String(40), nullable=True)
    include: Mapped[str | None] = mapped_column(String(40), nullable=True)
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occurred_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The text of a detail read (a dump, a gateway error), as ARC-1 sent it.
    # Written only by a run on a ``non_production`` target; a masked run
    # stores metadata only and clears it (``store.upsert_findings``).
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )


class IdeApproval(Base):
    """A pending/decided request to arm or cancel a trace."""

    __tablename__ = "ide_approvals"
    # The expiry sweep filters on status and created_at.
    __table_args__ = (Index("ix_ide_approvals_status_created", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        String(36), _session_fk(), nullable=False, index=True
    )
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class IdeAuditLog(Base):
    """Trace arming audit trail. Deliberately not a session child: it must
    survive a session purge (own retention)."""

    __tablename__ = "ide_audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), index=True
    )
    principal: Mapped[str] = mapped_column(String(255), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    target: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # trace_arm|trace_cancel|trace_deny|trace_failed
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(100), nullable=True)


IDE_CHILD_MODELS = (IdeMessage, IdeArtifact, IdeWorkspaceFile, IdeFinding, IdeApproval)
