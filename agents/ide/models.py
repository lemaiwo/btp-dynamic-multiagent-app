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


def iso_utc(value: datetime | None) -> str | None:
    """A stored timestamp as ISO 8601 with an explicit offset, for the API.

    Every stored value is UTC, but SQLite hands ``DateTime(timezone=True)``
    back naive; served without an offset, a browser reads it as local time.
    A naive value is therefore tagged UTC; an aware one keeps its offset."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


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
    # What the developer approved, per stage kind and per file:
    # {"design": 2, "plan": 1, "files": {"src/CLAS/zcl_x.clas.abap": 4}}.
    # NULL on sessions from before revisions; readers fall back to latest.
    pins_json: Mapped[str | None] = mapped_column(Text, nullable=True)
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
            "created_at": iso_utc(self.created_at),
            "updated_at": iso_utc(self.updated_at),
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
    # One row per version: ``store.add_artifact`` computes max + 1 and
    # retries when a concurrent writer took that number. Existing databases
    # get the index from ``init_db`` (guarded: duplicates only warn).
    __table_args__ = (
        Index("uq_ide_artifacts_version", "session_id", "kind", "version",
              unique=True),
    )

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
    # The session's pins when the document was submitted (a plan records
    # the design version it was written against), so a later re-pin cannot
    # silently change what an older document claims to build on.
    based_on_json: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    # Latest IdeFileRevision.revision of this path; 0 = none yet. The
    # server default lets the column be added NOT NULL to existing rows.
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # SAP version marker of origin_source (None = unknown).
    origin_version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # "sap" | "absent" | "unknown"; None = never checked (legacy rows, scratch).
    base_status: Mapped[str | None] = mapped_column(String(8), nullable=True)
    base_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
    # Written only by a diagnose run, which needs a ``non_production``
    # target; a later list read keeps it (``store.upsert_findings``).
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
    """Audit trail of trace arming and of changes to a target's
    ``non_production`` flag (``conventions_flag``) and of a flagged
    target's ``destination`` (``conventions_destination``). Deliberately not a
    session child: it must survive a session purge (own retention). A row
    that belongs to no session (``conventions_flag``) carries
    ``store.NO_SESSION`` as its ``session_id``."""

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
    )  # trace_arm|trace_cancel|trace_deny|trace_failed|conventions_flag|
    #    conventions_destination (store.AUDIT_ACTIONS)
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(100), nullable=True)


class IdeFileRevision(Base):
    """One proposed source of a workspace path, kept per run so a comment
    can point at the exact text it was written on."""

    __tablename__ = "ide_file_revisions"
    __table_args__ = (
        UniqueConstraint("session_id", "path", "revision", name="uq_ide_file_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        String(36), _session_fk(), nullable=False, index=True
    )
    path: Mapped[str] = mapped_column(String(200), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    proposed_source: Mapped[str] = mapped_column(Text, nullable=False, default="")
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    # None = not checked yet | "ok" | "errors" | "unavailable". Only a
    # recognised empty message list may become "ok" (fail safe).
    syntax_status: Mapped[str | None] = mapped_column(String(12), nullable=True)
    # [{"line": int|None, "message": str, "severity": "error"|"warning"}], <= 50
    syntax_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    syntax_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class IdeComment(Base):
    """A developer's review comment on a file revision or a document
    paragraph. Its text reaches the model only as quoted data."""

    __tablename__ = "ide_comments"
    # Approve and the request-changes run look up a session's comments by state.
    __table_args__ = (Index("ix_ide_comments_session_state", "session_id", "state"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        String(36), _session_fk(), nullable=False, index=True
    )
    anchor: Mapped[str] = mapped_column(String(8), nullable=False)  # file|document
    # anchor == "file"; lines are 1-based and inclusive.
    path: Mapped[str | None] = mapped_column(String(200), nullable=True)
    revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # anchor == "document"; paragraph is the 0-based block index.
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paragraph: Mapped[int | None] = mapped_column(Integer, nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # The text the developer selected, so the model sees what was meant even
    # when a block or line range is ambiguous: plain text on one line, cut to
    # store.MAX_QUOTE_CHARS. Added after 2.18.0 (db.init_db adds the column).
    quote: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # open | sent | addressed | dismissed
    state: Mapped[str] = mapped_column(
        String(10), nullable=False, default="open", server_default="open"
    )
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )


IDE_CHILD_MODELS = (
    IdeMessage,
    IdeArtifact,
    IdeWorkspaceFile,
    IdeFinding,
    IdeApproval,
    IdeFileRevision,
    IdeComment,
)
