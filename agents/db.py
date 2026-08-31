"""Database layer for dynamic agent configuration.

Uses PostgreSQL via SQLAlchemy async. On SAP BTP the connection URL is
resolved from the `postgresql-db` service in VCAP_SERVICES. Locally it
falls back to the DATABASE_URL environment variable (or an in-memory
SQLite fallback for quick experiments).
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Supported MCP auth modes
AUTH_MODE_JWT = "jwt"
AUTH_MODE_NONE = "none"
# Per-user OAuth2 authorization_code against the MCP server's *own* OAuth
# provider (e.g. a separate XSUAA). Each user authorizes once in the browser;
# the resulting access/refresh tokens are stored per (user, server) and
# forwarded on every MCP request. See agents/oauth2.py.
AUTH_MODE_OAUTH2 = "oauth2"
# App-only OAuth2 client_credentials: the agent authenticates as *itself*, not
# as anyone signed in, so it works against a mailbox or system nobody logs into
# and scheduled runs need no stored user token. The trade is that there is no
# user identity to scope access with -- the grant is whatever an admin
# consented to for the whole registration -- so the target must be named
# explicitly in config (`mailbox` for builtin:outlook). See
# agents/client_credentials.py.
AUTH_MODE_APP_ONLY = "app_only"
# Reached through a BTP destination: the destination holds the target's URL
# and credential, so nothing secret is stored here at all. 11 characters --
# see AUTH_MODE_MAX_LENGTH below, and what happened to "client_credentials".
AUTH_MODE_DESTINATION = "destination"
VALID_AUTH_MODES = frozenset({
    AUTH_MODE_JWT, AUTH_MODE_NONE, AUTH_MODE_OAUTH2, AUTH_MODE_APP_ONLY,
    AUTH_MODE_DESTINATION,
})
# Modes carrying an `oauth` config block.
OAUTH_CONFIG_MODES = frozenset({
    AUTH_MODE_OAUTH2, AUTH_MODE_APP_ONLY, AUTH_MODE_DESTINATION,
})

# Width of AgentConfig.auth_mode. Asserted rather than assumed: SQLite ignores
# VARCHAR limits, so a mode too long for the column passes every local test and
# every SQLite-backed suite, then fails on Postgres with
# StringDataRightTruncationError at the first insert. That is exactly how
# "client_credentials" (18 chars) reached a deployed environment. Failing at
# import makes the mistake impossible to ship.
AUTH_MODE_MAX_LENGTH = 16
_too_long = sorted(m for m in VALID_AUTH_MODES if len(m) > AUTH_MODE_MAX_LENGTH)
if _too_long:
    raise ValueError(
        f"auth_mode value(s) exceed the {AUTH_MODE_MAX_LENGTH}-char column: "
        f"{', '.join(_too_long)}. Shorten the value, or widen "
        f"AgentConfig.auth_mode and migrate existing rows."
    )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection string resolution
# ---------------------------------------------------------------------------
_vcap_ssl_ca: str | None = None  # populated when reading VCAP_SERVICES below


def _build_ssl_context(ca_pem: str | None) -> ssl.SSLContext:
    """SSL context for asyncpg.

    BTP managed postgres uses a self-signed CA chain that isn't in the
    system trust store. If the binding exposes the CA pem, load it.
    Otherwise (or if PG_SSL_INSECURE=1) skip verification — TLS is still
    on but the cert chain isn't validated.
    """
    ctx = ssl.create_default_context()
    if ca_pem:
        try:
            ctx.load_verify_locations(cadata=ca_pem)
            return ctx
        except Exception:
            logger.exception("Failed to load BTP postgres CA from VCAP; disabling verification")
    if os.environ.get("PG_SSL_INSECURE", "1") == "1":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _resolve_database_url() -> str:
    """Build an async SQLAlchemy URL from VCAP_SERVICES or env."""
    global _vcap_ssl_ca
    vcap = os.environ.get("VCAP_SERVICES")
    if vcap:
        try:
            services = json.loads(vcap)
            for key in ("postgresql-db", "postgresql", "hyperscaler-option-postgresql"):
                if key in services and services[key]:
                    creds = services[key][0]["credentials"]
                    # BTP PG credentials expose hostname, port, username, password, dbname, sslcert
                    host = creds.get("hostname") or creds.get("host")
                    port = creds.get("port", 5432)
                    user = creds.get("username")
                    password = creds.get("password")
                    dbname = creds.get("dbname") or creds.get("database")
                    # BTP exposes the server CA under one of these keys
                    _vcap_ssl_ca = (
                        creds.get("sslrootcert")
                        or creds.get("sslcert")
                        or creds.get("ca")
                        or creds.get("cert")
                    )
                    sslmode = "require"
                    return (
                        f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{dbname}"
                        f"?ssl={sslmode}"
                    )
        except Exception:
            logger.exception("Failed to parse VCAP_SERVICES for postgres")

    url = os.environ.get("DATABASE_URL")
    if url:
        # Normalize common variants to async driver
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://") and "+asyncpg" not in url:
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    # Local dev fallback
    logger.warning("No DATABASE_URL or VCAP postgres binding; using local SQLite")
    return "sqlite+aiosqlite:///./agents_registry.db"


DATABASE_URL = _resolve_database_url()

_connect_args: dict[str, Any] = {}
if DATABASE_URL.startswith("postgresql+asyncpg") and "ssl=" in DATABASE_URL:
    # asyncpg expects ssl via connect_args, not URL; strip & pass through
    base, _, query = DATABASE_URL.partition("?")
    DATABASE_URL = base
    _connect_args["ssl"] = _build_ssl_context(_vcap_ssl_ca)

engine = create_async_engine(DATABASE_URL, connect_args=_connect_args, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


# ---------------------------------------------------------------------------
# ORM models
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


class AgentConfig(Base):
    __tablename__ = "agent_configs"
    __table_args__ = (UniqueConstraint("name", name="uq_agent_configs_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    mcp_url: Mapped[str] = mapped_column(Text, nullable=False)
    auth_mode: Mapped[str] = mapped_column(
        String(AUTH_MODE_MAX_LENGTH), nullable=False,
        default=AUTH_MODE_JWT, server_default=AUTH_MODE_JWT,
    )
    # JSON-encoded list of additional MCP servers beyond the primary
    # (mcp_url/auth_mode). Each entry is {"url": str, "auth_mode": str,
    # optional "oauth": {...}}.
    extra_servers_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON-encoded OAuth2 client config for the PRIMARY server when its
    # auth_mode is "oauth2": {client_id, client_secret, uaa_url|authorize_url|
    # token_url, scope?}. Extras carry their own under each entry's "oauth".
    oauth_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON-encoded list of skill names (SkillConfig.name) attached to this
    # agent. Skills are referenced by name so exports stay portable.
    skills_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON-encoded list of agent names this agent may consult directly. Peers
    # become delegation tools on this agent, so a chain can run specialist to
    # specialist instead of routing every hop through the orchestrator.
    # Referenced by name, like skills, so exports stay portable.
    peers_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # API exposure. expose_chat keeps the agent in the orchestrator's
    # delegation list; expose_api gives it a run endpoint. They are
    # independent: a run-only agent is expose_chat=0, expose_api=1.
    expose_chat: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    expose_api: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # URL segment for POST /api/agents/{api_slug}/run. Uniqueness is enforced
    # in upsert_agent, not by a DB constraint (see plan Global Constraints).
    api_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Identity API-triggered runs bind via run_as(). No interactive user
    # exists at 03:00, so this names the technical account whose stored
    # OAuth2 token the run uses.
    run_as_principal: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # User prompt handed to Agent.run(). The work itself is described by the
    # agent's instructions and attached skills; this just starts it.
    run_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1800, server_default="1800"
    )
    # Overrides the globally active model for this agent. Null means "use the
    # global one" — which is what every agent did before workflows needed a
    # cheap reader and an expensive specialist in the same chain.
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Retained only so the schema is unchanged for existing deployments;
    # nothing reads or writes this column any more (see
    # docs/superpowers/specs/2026-08-21-generic-markdown-run-reports-design.md).
    expected_sections_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def mcp_servers(self) -> list[dict[str, Any]]:
        """Full list of MCP servers, primary first.

        Each entry is {"url", "auth_mode"} plus an optional "oauth" dict when
        auth_mode == "oauth2".
        """
        primary: dict[str, Any] = {"url": self.mcp_url, "auth_mode": self.auth_mode}
        if self.oauth_json:
            try:
                oauth = json.loads(self.oauth_json)
                if isinstance(oauth, dict):
                    primary["oauth"] = oauth
            except Exception:
                logger.warning("Malformed oauth_json on agent %s", self.name)
        out: list[dict[str, Any]] = [primary]
        if self.extra_servers_json:
            try:
                extras = json.loads(self.extra_servers_json)
            except Exception:
                logger.warning("Malformed extra_servers_json on agent %s", self.name)
                return out
            if isinstance(extras, list):
                for e in extras:
                    if isinstance(e, dict) and "url" in e:
                        entry: dict[str, Any] = {
                            "url": str(e["url"]),
                            "auth_mode": str(e.get("auth_mode") or AUTH_MODE_JWT),
                        }
                        if isinstance(e.get("oauth"), dict):
                            entry["oauth"] = e["oauth"]
                        out.append(entry)
        return out

    @property
    def skills(self) -> list[str]:
        """Names of the skills attached to this agent (may be empty)."""
        if not self.skills_json:
            return []
        try:
            data = json.loads(self.skills_json)
        except Exception:
            logger.warning("Malformed skills_json on agent %s", self.name)
            return []
        if not isinstance(data, list):
            return []
        return [str(s) for s in data if isinstance(s, str) and s.strip()]

    @property
    def peers(self) -> list[str]:
        """Names of the agents this agent may consult (may be empty)."""
        if not self.peers_json:
            return []
        try:
            data = json.loads(self.peers_json)
        except Exception:
            logger.warning("Malformed peers_json on agent %s", self.name)
            return []
        if not isinstance(data, list):
            return []
        return [str(p) for p in data if isinstance(p, str) and p.strip()]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "instructions": self.instructions,
            "mcp_url": self.mcp_url,
            "auth_mode": self.auth_mode,
            "mcp_servers": _redact_servers(self.mcp_servers),
            "skills": self.skills,
            "peers": self.peers,
            "enabled": bool(self.enabled),
            "expose_chat": bool(self.expose_chat),
            "expose_api": bool(self.expose_api),
            "api_slug": self.api_slug,
            "run_as_principal": self.run_as_principal,
            "run_prompt": self.run_prompt or "",
            "run_timeout_seconds": self.run_timeout_seconds,
            "model_name": self.model_name or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def to_export(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "instructions": self.instructions,
            "mcp_servers": _redact_servers(self.mcp_servers),
            "skills": self.skills,
            "peers": self.peers,
            "enabled": bool(self.enabled),
            "expose_chat": bool(self.expose_chat),
            "expose_api": bool(self.expose_api),
            "api_slug": self.api_slug,
            "run_prompt": self.run_prompt or "",
            "run_timeout_seconds": self.run_timeout_seconds,
            "model_name": self.model_name or "",
        }


# OAuth client_secret is never returned over the API or written to exports.
# The full secret stays in the DB and is read only by the registry when it
# builds the live MCP server connections.
OAUTH_SECRET_KEYS = ("client_secret",)


def _redact_servers(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy of the server list with OAuth secrets masked.

    Adds ``has_client_secret`` so the admin UI can show that a secret is
    stored without revealing it; the actual value is replaced by "".
    """
    out: list[dict[str, Any]] = []
    for s in servers:
        s = dict(s)
        oauth = s.get("oauth")
        if isinstance(oauth, dict):
            oauth = dict(oauth)
            oauth["has_client_secret"] = bool(oauth.get("client_secret"))
            for k in OAUTH_SECRET_KEYS:
                if k in oauth:
                    oauth[k] = ""
            s["oauth"] = oauth
        out.append(s)
    return out


class OrchestratorConfig(Base):
    __tablename__ = "orchestrator_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SkillConfig(Base):
    """A reusable skill: a named block of expert instructions.

    Skills are attached to agents by name (AgentConfig.skills_json). The
    registry lists each attached skill's name + description in the
    specialist's system prompt and exposes the full ``content`` through a
    ``load_skill`` tool, so the model only pulls in a skill's body when the
    task at hand actually matches it.
    """

    __tablename__ = "skill_configs"
    __table_args__ = (UniqueConstraint("name", name="uq_skill_configs_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "content": self.content,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def to_export(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "content": self.content,
        }


class Workflow(Base):
    """A declared, ordered sequence of agents run as one background job.

    The definition is the authority: the engine runs exactly these steps in
    exactly this order. No model gets to reorder or skip them — the only
    per-item decision is which branches to enter, and that is data the
    fan-out step emits.
    """

    __tablename__ = "workflows"
    __table_args__ = (UniqueConstraint("name", name="uq_workflows_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    api_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Fallback identity for steps whose agent has no run_as_principal of its
    # own. A scheduled run has no interactive user to borrow one from.
    run_as_principal: Mapped[str | None] = mapped_column(String(255), nullable=True)
    run_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1800, server_default="1800"
    )
    # Repeat-run safety: an item already completed by an earlier run is
    # skipped, so a retry after a crash resumes rather than re-drafting.
    skip_seen_items: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    max_parallel_items: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    on_unknown_branch: Mapped[str] = mapped_column(
        String(16), nullable=False, default="fail", server_default="fail"
    )
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "api_slug": self.api_slug,
            "run_as_principal": self.run_as_principal,
            "run_timeout_seconds": self.run_timeout_seconds,
            "skip_seen_items": bool(self.skip_seen_items),
            "max_parallel_items": self.max_parallel_items,
            "on_unknown_branch": self.on_unknown_branch,
            "enabled": bool(self.enabled),
        }


class WorkflowBranch(Base):
    """A named sub-sequence of steps an item may or may not enter.

    ``key`` is what the fan-out step emits to select this branch;
    ``description`` is shown to it in the branch catalogue so it chooses from
    a list it can see rather than guessing label strings.
    """

    __tablename__ = "workflow_branches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "description": self.description,
            "position": self.position,
        }


class WorkflowStep(Base):
    """One agent invocation in a workflow.

    ``branch_key`` null means the main line; otherwise the step belongs to
    that branch. Every step names exactly one agent — branch selection is the
    item's, not the step's.
    """

    __tablename__ = "workflow_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    branch_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    fan_out: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    step_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=600, server_default="600"
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch_key": self.branch_key,
            "position": self.position,
            "agent_name": self.agent_name,
            "instructions": self.instructions,
            "fan_out": bool(self.fan_out),
            "step_timeout_seconds": self.step_timeout_seconds,
        }


class WorkflowRun(Base):
    """One execution of a workflow.

    Created before the run starts so the row doubles as the overlap lock, the
    same way JobRun does: a second trigger while one is running is refused
    rather than double-hitting the target systems.
    """

    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    workflow_name: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    items_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_succeeded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scheduler_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scheduler_schedule_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scheduler_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scheduler_host: Mapped[str | None] = mapped_column(Text, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "workflow_name": self.workflow_name,
            "trigger": self.trigger,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "items_total": self.items_total,
            "items_succeeded": self.items_succeeded,
            "items_failed": self.items_failed,
            "items_skipped": self.items_skipped,
            "summary": self.summary,
            "error": self.error,
            "created_by": self.created_by,
        }


class WorkflowItemRun(Base):
    """One work item flowing through a workflow run."""

    __tablename__ = "workflow_item_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    workflow_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # The fan-out step's stable key for this item (e.g. a Gmail message id).
    # Repeat-run safety looks items up by (workflow_id, item_key).
    item_key: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Which branches the reader selected. Stored so the routing decision is
    # auditable and correctable rather than buried in a model's reasoning.
    branches_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def branches(self) -> list[str]:
        if not self.branches_json:
            return []
        try:
            data = json.loads(self.branches_json)
        except Exception:
            logger.warning("Malformed branches_json on item run %s", self.id)
            return []
        return [str(b) for b in data] if isinstance(data, list) else []

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workflow_run_id": self.workflow_run_id,
            "item_key": self.item_key,
            "title": self.title,
            "branches": self.branches,
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class WorkflowStepRun(Base):
    """One agent invocation inside a workflow run."""

    __tablename__ = "workflow_step_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # Null for steps that run before the fan-out, i.e. once per run.
    item_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    branch_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "item_run_id": self.item_run_id,
            "branch_key": self.branch_key,
            "position": self.position,
            "agent_name": self.agent_name,
            "status": self.status,
            "output": self.output,
            "error": self.error,
        }


class JobRun(Base):
    """One API-triggered execution of an agent.

    Created before the run starts so the row doubles as the overlap lock: a
    second trigger while one is `running` is refused rather than double-hitting
    the target system.
    """

    __tablename__ = "job_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)  # schedule|manual
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=1800)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    missing_sections_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # False when the notification could not be delivered; the run itself keeps
    # its real status so a delivery problem never loses a completed run.
    notified: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # BTP Job Scheduling callback coordinates (increment 3).
    scheduler_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scheduler_schedule_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scheduler_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scheduler_host: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def report(self) -> dict[str, Any] | None:
        if not self.report_json:
            return None
        try:
            return json.loads(self.report_json)
        except Exception:
            logger.warning("Malformed report_json on run %s", self.id)
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "trigger": self.trigger,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "summary": self.summary,
            "error": self.error,
            "notified": bool(self.notified),
            "created_by": self.created_by,
        }


class McpOAuthToken(Base):
    """Per-user OAuth2 tokens for an MCP server (auth_mode="oauth2").

    One row per (user, server_key). ``server_key`` is the normalized MCP URL
    (ending in /mcp). Tokens are refreshed in place when they expire.
    """

    __tablename__ = "mcp_oauth_tokens"
    __table_args__ = (
        UniqueConstraint("user_id", "server_key", name="uq_mcp_oauth_user_server"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    server_key: Mapped[str] = mapped_column(String(512), nullable=False)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_type: Mapped[str] = mapped_column(String(32), nullable=False, default="Bearer")
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class McpOAuthClient(Base):
    """A registered OAuth client for an MCP server (auth_mode="oauth2" + DCR).

    For servers configured to auto-discover, the app performs OAuth metadata
    discovery + Dynamic Client Registration once and caches the result here
    (one row per server_key) so every user reuses the same registered client.
    """

    __tablename__ = "mcp_oauth_clients"

    server_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    authorize_url: Mapped[str] = mapped_column(Text, nullable=False)
    token_url: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class McpOAuthState(Base):
    """Short-lived authorization_code flow state (CSRF state + PKCE verifier).

    Created when an authorization URL is generated and consumed once at the
    /oauth/callback. Rows past ``expires_at`` are ignored and swept.
    """

    __tablename__ = "mcp_oauth_states"

    state: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    server_key: Mapped[str] = mapped_column(String(512), nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(255), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


DEFAULT_ORCHESTRATOR_INSTRUCTIONS = (
    "You are an SAP BTP platform management orchestrator. "
    "You coordinate between specialized agents to help users manage their SAP BTP "
    "landscape. Delegate each task to the most appropriate specialist based on "
    "their description. You may combine results from multiple agents to give "
    "comprehensive answers. When a request spans multiple domains, call the "
    "relevant specialists one at a time and synthesize their responses."
)

DEFAULT_RUN_PROMPT = (
    "Perform your configured check now and return the report."
)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
async def init_db() -> None:
    """Create tables and ensure an orchestrator config row exists."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Lightweight migrations: SQLAlchemy create_all doesn't add columns
        # to existing tables.
        await _ensure_column(
            conn,
            "agent_configs",
            "auth_mode",
            f"VARCHAR(16) NOT NULL DEFAULT '{AUTH_MODE_JWT}'",
        )
        await _ensure_column(
            conn, "agent_configs", "extra_servers_json", "TEXT"
        )
        await _ensure_column(
            conn, "agent_configs", "oauth_json", "TEXT"
        )
        await _ensure_column(
            conn, "agent_configs", "skills_json", "TEXT"
        )
        await _ensure_column(
            conn, "agent_configs", "expose_chat", "INTEGER NOT NULL DEFAULT 1"
        )
        await _ensure_column(
            conn, "agent_configs", "expose_api", "INTEGER NOT NULL DEFAULT 0"
        )
        await _ensure_column(conn, "agent_configs", "api_slug", "VARCHAR(64)")
        await _ensure_column(
            conn, "agent_configs", "run_as_principal", "VARCHAR(255)"
        )
        await _ensure_column(conn, "agent_configs", "run_prompt", "TEXT")
        await _ensure_column(
            conn,
            "agent_configs",
            "run_timeout_seconds",
            "INTEGER NOT NULL DEFAULT 1800",
        )
        await _ensure_column(
            conn, "agent_configs", "expected_sections_json", "TEXT"
        )
        await _ensure_column(
            conn, "agent_configs", "model_name", "VARCHAR(128)"
        )
        await _ensure_column(conn, "agent_configs", "peers_json", "TEXT")
        await _ensure_column(
            conn, "orchestrator_config", "model_name", "VARCHAR(128)"
        )

    async with SessionLocal() as session:
        existing = await session.get(OrchestratorConfig, 1)
        if existing is None:
            session.add(
                OrchestratorConfig(id=1, instructions=DEFAULT_ORCHESTRATOR_INSTRUCTIONS)
            )
            await session.commit()


async def _ensure_column(conn, table: str, column: str, ddl_type: str) -> None:
    """Idempotently add a column to a table if it doesn't already exist."""
    dialect = conn.dialect.name
    if dialect == "postgresql":
        await conn.execute(
            text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl_type}")
        )
        return
    try:
        result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
        cols = {row[1] for row in result.fetchall()}
    except Exception:
        logger.debug("Could not introspect %s columns", table, exc_info=True)
        return
    if column not in cols:
        try:
            await conn.exec_driver_sql(
                f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"
            )
        except Exception:
            logger.exception("Failed to add %s column to %s", column, table)


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------
async def list_agents(session: AsyncSession) -> list[AgentConfig]:
    result = await session.execute(select(AgentConfig).order_by(AgentConfig.name))
    return list(result.scalars().all())


async def get_agent(session: AsyncSession, agent_id: int) -> AgentConfig | None:
    return await session.get(AgentConfig, agent_id)


async def get_agent_by_name(session: AsyncSession, name: str) -> AgentConfig | None:
    result = await session.execute(select(AgentConfig).where(AgentConfig.name == name))
    return result.scalar_one_or_none()


async def get_agent_by_slug(session: AsyncSession, slug: str) -> AgentConfig | None:
    result = await session.execute(
        select(AgentConfig).where(AgentConfig.api_slug == slug)
    )
    return result.scalar_one_or_none()


_OAUTH_KEYS = ("client_id", "client_secret", "uaa_url", "authorize_url", "token_url", "scope")
# client_credentials has no browser leg, so no authorize_url. It gains a target
# (`mailbox`) because an app-only token names no user, and `allow_send`, which
# is deliberately separate from the token's permissions: holding Mail.Send must
# not be enough to give an agent a send tool.
_CC_KEYS = ("client_id", "client_secret", "uaa_url", "token_url", "scope", "mailbox",
            "lookback")


def _clean_client_credentials(
    oauth: Any, fallback: dict[str, Any] | None
) -> dict[str, Any]:
    """Normalize a ``client_credentials`` oauth block for storage.

    Deliberately stricter than the oauth2 shape in one place: ``allow_send`` is
    stored as a real boolean and defaults to False. An app-only token's scope
    is whatever an admin consented to for the entire registration, so a tenant
    that granted Mail.Send would otherwise hand every agent bound to it the
    ability to send mail. The capability has to be turned on here, per server,
    on purpose.
    """
    src = oauth if isinstance(oauth, dict) else {}
    cleaned: dict[str, Any] = {}
    for k in _CC_KEYS:
        v = src.get(k)
        if v is not None and str(v).strip() != "":
            cleaned[k] = str(v).strip()
    if not cleaned.get("client_secret") and fallback and fallback.get("client_secret"):
        cleaned["client_secret"] = fallback["client_secret"]
    if not cleaned.get("client_id"):
        raise ValueError("client_credentials server requires a client_id")
    if not cleaned.get("client_secret"):
        raise ValueError("client_credentials server requires a client_secret")
    if not (cleaned.get("token_url") or cleaned.get("uaa_url")):
        raise ValueError("client_credentials server requires a token_url or uaa_url")
    cleaned["allow_send"] = bool(src.get("allow_send"))
    return cleaned


# A destination server stores no credential: the destination itself holds the
# target's URL and its secret. `destination` names it; the rest is filtering.
_DEST_KEYS = ("destination", "project", "status", "lookback", "api_base")


def _clean_destination(oauth: Any) -> dict[str, Any]:
    """Normalize a ``destination`` oauth block for storage.

    Credential keys are dropped rather than rejected here: an admin editing a
    server that used to be oauth2 will still be POSTing a client_id, and
    silently not storing it is what keeps this mode's promise that nothing
    secret lands in the database. The payload validator refuses them earlier,
    with a message explaining why.
    """
    src = oauth if isinstance(oauth, dict) else {}
    cleaned: dict[str, Any] = {}
    for k in _DEST_KEYS:
        v = src.get(k)
        if v is not None and str(v).strip() != "":
            cleaned[k] = str(v).strip()
    if not cleaned.get("destination"):
        raise ValueError("destination server requires a destination name")
    cleaned["allow_comment"] = bool(src.get("allow_comment"))
    return cleaned


def _clean_oauth(
    oauth: Any, mode: str, fallback: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Normalize an OAuth config dict for storage.

    Two shapes are accepted for oauth2 servers:

    - ``{"dcr": true, "scope"?}`` — auto-discover the authorization server and
      dynamically register a client at runtime; no manual credentials needed.
    - manual: ``{client_id, client_secret, uaa_url|authorize_url+token_url,
      scope?}``. When ``client_secret`` is blank but a ``fallback`` (the
      previously stored oauth for the same url) has one, the old secret is
      preserved so edits from the admin UI — which never receives the secret —
      don't wipe it.

    ``client_credentials`` takes a third shape: ``{client_id, client_secret,
    token_url|uaa_url, scope?, mailbox?, allow_send?}``. No authorize_url,
    because nobody visits a browser.

    Returns None for modes that carry no oauth block.
    """
    if mode == AUTH_MODE_DESTINATION:
        return _clean_destination(oauth)
    if mode == AUTH_MODE_APP_ONLY:
        return _clean_client_credentials(oauth, fallback)
    if mode != AUTH_MODE_OAUTH2:
        return None
    src = oauth if isinstance(oauth, dict) else {}
    if src.get("dcr"):
        cleaned_dcr: dict[str, Any] = {"dcr": True}
        if src.get("scope"):
            cleaned_dcr["scope"] = str(src["scope"]).strip()
        return cleaned_dcr
    cleaned: dict[str, Any] = {}
    for k in _OAUTH_KEYS:
        v = src.get(k)
        if v is not None and str(v).strip() != "":
            cleaned[k] = str(v).strip()
    if not cleaned.get("client_secret") and fallback and fallback.get("client_secret"):
        cleaned["client_secret"] = fallback["client_secret"]
    if not cleaned.get("client_id"):
        raise ValueError("oauth2 server requires a client_id")
    if not cleaned.get("client_secret"):
        raise ValueError("oauth2 server requires a client_secret")
    if not (cleaned.get("uaa_url") or (cleaned.get("authorize_url") and cleaned.get("token_url"))):
        raise ValueError(
            "oauth2 server requires either uaa_url or both authorize_url and token_url"
        )
    return cleaned


def prepare_servers(
    mcp_servers: list[dict[str, Any]], existing: AgentConfig | None
) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
    """Validate + normalize servers; return (primary, extras, primary_oauth_json).

    ``primary`` is {"url","auth_mode"}; extras entries additionally carry an
    embedded "oauth" dict when oauth2. Existing secrets are preserved per url.
    """
    if not mcp_servers:
        raise ValueError("at least one MCP server is required")
    prev_oauth_by_url: dict[str, dict[str, Any]] = {}
    if existing is not None:
        for s in existing.mcp_servers:
            if isinstance(s.get("oauth"), dict):
                prev_oauth_by_url[s["url"]] = s["oauth"]

    normalized: list[dict[str, Any]] = []
    for s in mcp_servers:
        url = (s.get("url") or "").strip()
        mode = (s.get("auth_mode") or AUTH_MODE_JWT).strip().lower()
        if not url:
            raise ValueError("MCP server url is required")
        if mode not in VALID_AUTH_MODES:
            raise ValueError(f"invalid auth_mode {mode!r}")
        oauth = _clean_oauth(s.get("oauth"), mode, prev_oauth_by_url.get(url))
        entry: dict[str, Any] = {"url": url, "auth_mode": mode}
        if oauth is not None:
            entry["oauth"] = oauth
        normalized.append(entry)

    primary = normalized[0]
    extras = normalized[1:]
    primary_oauth_json = (
        json.dumps(primary["oauth"]) if primary.get("oauth") else None
    )
    primary_clean = {"url": primary["url"], "auth_mode": primary["auth_mode"]}
    return primary_clean, extras, primary_oauth_json


async def normalize_skills_json(
    session: AsyncSession, skills: list[str] | None
) -> str | None:
    """Validate a list of skill names and return it JSON-encoded (or None).

    Names are trimmed and de-duplicated (order preserved). Referencing a
    skill that does not exist raises ValueError so the admin API can reject
    the request instead of silently storing a dangling reference.
    """
    cleaned: list[str] = []
    for s in skills or []:
        s = str(s).strip()
        if s and s not in cleaned:
            cleaned.append(s)
    if not cleaned:
        return None
    result = await session.execute(select(SkillConfig.name))
    known = {n for (n,) in result.all()}
    unknown = [s for s in cleaned if s not in known]
    if unknown:
        raise ValueError(f"unknown skill(s): {', '.join(unknown)}")
    return json.dumps(cleaned)


class _Keep:
    """Sentinel for upsert_agent: leave the stored value untouched.

    ``run_as_principal`` is a landscape-specific service identity and is
    deliberately absent from exports (see AgentConfig.to_export), so the
    import and seed paths pass nothing for it. Defaulting it to None instead
    would silently un-configure every API agent on the first import.

    ``model_name`` and ``peers`` need the same distinction for a different
    reason: a writer that predates them (an older exported bundle, or a
    client whose form has no field for them) sends no key at all. Without a
    sentinel their absence is indistinguishable from "clear it", so every
    such write would wipe a configured override or peer list. KEEP means
    "not sent"; an explicit ``""`` / ``[]`` still clears.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<KEEP>"


KEEP = _Keep()


async def upsert_agent(
    session: AsyncSession,
    *,
    name: str,
    description: str,
    instructions: str,
    mcp_servers: list[dict[str, Any]],
    skills: list[str] | None = None,
    enabled: bool = True,
    expose_chat: bool = True,
    expose_api: bool = False,
    api_slug: str | None = None,
    run_as_principal: str | None | _Keep = KEEP,
    run_prompt: str | None = None,
    run_timeout_seconds: int = 1800,
    model_name: str | None | _Keep = KEEP,
    peers: list[str] | None | _Keep = KEEP,
) -> AgentConfig:
    existing = await get_agent_by_name(session, name)
    primary, extras, primary_oauth_json = prepare_servers(mcp_servers, existing)
    extras_json = json.dumps(extras) if extras else None
    skills_json = await normalize_skills_json(session, skills)
    # Order-preserving de-duplication: the same peer listed twice would
    # otherwise register the same delegation tool twice on the same agent.
    # KEEP short-circuits it: nothing was sent, so nothing is written below.
    peers_json: str | None = None
    if not isinstance(peers, _Keep):
        seen: set[str] = set()
        cleaned_peers: list[str] = []
        for p in peers or []:
            p = str(p).strip()
            if p and p not in seen:
                seen.add(p)
                cleaned_peers.append(p)
        peers_json = json.dumps(cleaned_peers) if cleaned_peers else None

    slug = (api_slug or "").strip() or None
    if slug:
        clash = await get_agent_by_slug(session, slug)
        if clash is not None and (existing is None or clash.id != existing.id):
            raise ValueError(f"api_slug {slug!r} is already used by agent {clash.name!r}")
    if expose_api and not slug:
        raise ValueError("expose_api requires an api_slug")

    if existing is None:
        row = AgentConfig(
            name=name,
            description=description,
            instructions=instructions,
            mcp_url=primary["url"],
            auth_mode=primary["auth_mode"],
            extra_servers_json=extras_json,
            oauth_json=primary_oauth_json,
            skills_json=skills_json,
            enabled=1 if enabled else 0,
        )
        session.add(row)
        row.expose_chat = 1 if expose_chat else 0
        row.expose_api = 1 if expose_api else 0
        row.api_slug = slug
        row.run_as_principal = (
            None if isinstance(run_as_principal, _Keep)
            else (run_as_principal or "").strip() or None
        )
        row.run_prompt = (run_prompt or "").strip() or None
        row.run_timeout_seconds = int(run_timeout_seconds)
        # A brand-new row has nothing to keep, so KEEP simply means "unset".
        row.model_name = (
            None if isinstance(model_name, _Keep)
            else (model_name or "").strip() or None
        )
        row.peers_json = peers_json
    else:
        existing.description = description
        existing.instructions = instructions
        existing.mcp_url = primary["url"]
        existing.auth_mode = primary["auth_mode"]
        existing.extra_servers_json = extras_json
        existing.oauth_json = primary_oauth_json
        existing.skills_json = skills_json
        existing.enabled = 1 if enabled else 0
        existing.expose_chat = 1 if expose_chat else 0
        existing.expose_api = 1 if expose_api else 0
        existing.api_slug = slug
        # KEEP: the caller did not carry a principal (import / seed), so the
        # environment-specific identity already stored here is preserved.
        if not isinstance(run_as_principal, _Keep):
            existing.run_as_principal = (run_as_principal or "").strip() or None
        existing.run_prompt = (run_prompt or "").strip() or None
        existing.run_timeout_seconds = int(run_timeout_seconds)
        # KEEP: the caller carries no model override / peer list (an older
        # bundle, or a client without those fields), so what this landscape
        # already has is preserved instead of being silently wiped.
        if not isinstance(model_name, _Keep):
            existing.model_name = (model_name or "").strip() or None
        if not isinstance(peers, _Keep):
            existing.peers_json = peers_json
        row = existing
    await session.commit()
    await session.refresh(row)
    return row


async def delete_agent(session: AsyncSession, agent_id: int) -> bool:
    row = await session.get(AgentConfig, agent_id)
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True


# ---------------------------------------------------------------------------
# Skills CRUD
# ---------------------------------------------------------------------------
async def list_skills(session: AsyncSession) -> list[SkillConfig]:
    result = await session.execute(select(SkillConfig).order_by(SkillConfig.name))
    return list(result.scalars().all())


async def get_skill(session: AsyncSession, skill_id: int) -> SkillConfig | None:
    return await session.get(SkillConfig, skill_id)


async def get_skill_by_name(session: AsyncSession, name: str) -> SkillConfig | None:
    result = await session.execute(select(SkillConfig).where(SkillConfig.name == name))
    return result.scalar_one_or_none()


async def upsert_skill(
    session: AsyncSession, *, name: str, description: str, content: str
) -> SkillConfig:
    row = await get_skill_by_name(session, name)
    if row is None:
        row = SkillConfig(name=name, description=description, content=content)
        session.add(row)
    else:
        row.description = description
        row.content = content
    await session.commit()
    await session.refresh(row)
    return row


async def rename_skill_references(
    session: AsyncSession, old_name: str, new_name: str | None
) -> None:
    """Update every agent's skill list after a skill rename or delete.

    ``new_name=None`` detaches the skill instead. Caller commits (this runs
    inside the same transaction as the rename/delete itself).
    """
    result = await session.execute(
        select(AgentConfig).where(AgentConfig.skills_json.is_not(None))
    )
    for agent in result.scalars().all():
        skills = agent.skills
        if old_name not in skills:
            continue
        mapped = [new_name if s == old_name else s for s in skills]
        # Drop detached entries and de-dup in case new_name was already there
        deduped = list(dict.fromkeys(s for s in mapped if s))
        agent.skills_json = json.dumps(deduped) if deduped else None


async def delete_skill(session: AsyncSession, skill_id: int) -> bool:
    row = await session.get(SkillConfig, skill_id)
    if row is None:
        return False
    await rename_skill_references(session, row.name, None)
    await session.delete(row)
    await session.commit()
    return True


VALID_ON_UNKNOWN_BRANCH = ("fail", "skip")


def validate_workflow_parts(
    branches: list[dict[str, Any]],
    steps: list[dict[str, Any]],
    known_agents: set[str],
    *,
    enabled: bool,
) -> None:
    """Reject a workflow definition that cannot run.

    Raises ValueError with a message naming the offending value. This runs at
    save time on purpose: a workflow that cannot run must say so while someone
    is looking at it, not at 03:00 when the scheduler fires.
    """
    if enabled and not steps:
        raise ValueError("An enabled workflow must have at least one step; this has no steps.")

    fan_out_steps = [s for s in steps if s.get("fan_out")]
    if len(fan_out_steps) > 1:
        raise ValueError(
            f"A workflow may have at most one fan-out step; found {len(fan_out_steps)}."
        )
    for s in fan_out_steps:
        bk = s.get("branch_key")
        if bk is not None:
            raise ValueError(
                f"The fan-out step must be on the main line; step {s.get('position')} "
                f"in branch {str(bk)!r} cannot be the fan-out step."
            )

    keys: list[str] = []
    for b in branches:
        key = str(b.get("key") or "").strip()
        if not key:
            raise ValueError("A branch key must not be empty.")
        if key in keys:
            raise ValueError(f"Duplicate branch key {key!r}.")
        keys.append(key)

    if branches and not fan_out_steps:
        raise ValueError(
            "A workflow that declares branches must have a fan-out step: "
            "branches are entered per work item, and without a fan-out step "
            "there are no items."
        )

    for s in steps:
        agent = str(s.get("agent_name") or "").strip()
        if agent not in known_agents:
            raise ValueError(
                f"Step {s.get('position')} names agent {agent!r}, which does not "
                "exist or is disabled."
            )
        bk = s.get("branch_key")
        if bk is not None and str(bk).strip() not in keys:
            raise ValueError(
                f"Step {s.get('position')} belongs to branch {str(bk)!r}, "
                "which is not declared on this workflow."
            )

    used_branches = {str(s["branch_key"]).strip() for s in steps
                     if s.get("branch_key") is not None}
    for key in keys:
        if key not in used_branches:
            raise ValueError(f"Branch {key!r} has no steps.")

    def _check_positions(label: str, group: list[dict[str, Any]]) -> None:
        positions = [int(s.get("position") or 0) for s in group]
        if len(set(positions)) != len(positions):
            raise ValueError(f"Duplicate step positions in {label}: {sorted(positions)}.")
        if sorted(positions) != list(range(1, len(positions) + 1)):
            raise ValueError(
                f"Step positions in {label} must be contiguous from 1; got "
                f"{sorted(positions)}."
            )

    _check_positions("the main line", [s for s in steps if s.get("branch_key") is None])
    for key in keys:
        _check_positions(
            f"branch {key!r}",
            [s for s in steps if str(s.get("branch_key") or "").strip() == key],
        )


# ---------------------------------------------------------------------------
# Workflow CRUD
# ---------------------------------------------------------------------------
async def list_workflows(session: AsyncSession) -> list[Workflow]:
    result = await session.execute(select(Workflow).order_by(Workflow.name))
    return list(result.scalars().all())


async def get_workflow(session: AsyncSession, workflow_id: int) -> Workflow | None:
    return await session.get(Workflow, workflow_id)


async def get_workflow_by_name(session: AsyncSession, name: str) -> Workflow | None:
    result = await session.execute(select(Workflow).where(Workflow.name == name))
    return result.scalar_one_or_none()


async def get_workflow_by_slug(session: AsyncSession, slug: str) -> Workflow | None:
    result = await session.execute(select(Workflow).where(Workflow.api_slug == slug))
    return result.scalar_one_or_none()


async def get_workflow_parts(
    session: AsyncSession, workflow_id: int
) -> tuple[list[WorkflowBranch], list[WorkflowStep]]:
    """Branches in position order, and steps in (branch, position) order."""
    b = await session.execute(
        select(WorkflowBranch)
        .where(WorkflowBranch.workflow_id == workflow_id)
        .order_by(WorkflowBranch.position, WorkflowBranch.id)
    )
    s = await session.execute(
        select(WorkflowStep)
        .where(WorkflowStep.workflow_id == workflow_id)
        .order_by(WorkflowStep.branch_key, WorkflowStep.position, WorkflowStep.id)
    )
    return list(b.scalars().all()), list(s.scalars().all())


async def upsert_workflow(
    session: AsyncSession,
    *,
    name: str,
    description: str = "",
    api_slug: str | None = None,
    run_as_principal: str | None = None,
    run_timeout_seconds: int = 1800,
    skip_seen_items: bool = True,
    max_parallel_items: int = 1,
    on_unknown_branch: str = "fail",
    enabled: bool = True,
    branches: list[dict[str, Any]] | None = None,
    steps: list[dict[str, Any]] | None = None,
) -> Workflow:
    """Create or replace a workflow and its parts.

    Branches and steps are replaced wholesale rather than diffed: a definition
    is small and edited as a unit, so reconciliation would add bugs and buy
    nothing.
    """
    branches = branches or []
    steps = steps or []

    if on_unknown_branch not in VALID_ON_UNKNOWN_BRANCH:
        raise ValueError(
            f"on_unknown_branch must be one of {VALID_ON_UNKNOWN_BRANCH}, "
            f"got {on_unknown_branch!r}"
        )

    known_agents = {
        r.name for r in await list_agents(session) if r.enabled
    }
    validate_workflow_parts(branches, steps, known_agents, enabled=enabled)

    existing = await get_workflow_by_name(session, name)
    slug = (api_slug or "").strip() or None
    if slug:
        clash = await get_workflow_by_slug(session, slug)
        if clash is not None and (existing is None or clash.id != existing.id):
            raise ValueError(
                f"api_slug {slug!r} is already used by workflow {clash.name!r}"
            )

    if existing is None:
        row = Workflow(name=name)
        session.add(row)
    else:
        row = existing
    row.description = description
    row.api_slug = slug
    row.run_as_principal = (run_as_principal or "").strip() or None
    row.run_timeout_seconds = int(run_timeout_seconds)
    row.skip_seen_items = 1 if skip_seen_items else 0
    row.max_parallel_items = max(1, int(max_parallel_items))
    row.on_unknown_branch = on_unknown_branch
    row.enabled = 1 if enabled else 0
    # flush (not commit): a new row's id must be assigned before the branch/
    # step rows below can reference it, but the whole save must land in one
    # transaction so a crash mid-save can never pair the new scalar fields
    # with stale branches/steps.
    await session.flush()

    await session.execute(
        delete(WorkflowBranch).where(WorkflowBranch.workflow_id == row.id)
    )
    await session.execute(
        delete(WorkflowStep).where(WorkflowStep.workflow_id == row.id)
    )
    for b in branches:
        session.add(WorkflowBranch(
            workflow_id=row.id,
            key=str(b["key"]).strip(),
            description=str(b.get("description") or ""),
            position=int(b.get("position") or 1),
        ))
    for s in steps:
        bk = s.get("branch_key")
        session.add(WorkflowStep(
            workflow_id=row.id,
            branch_key=str(bk).strip() if bk is not None else None,
            position=int(s["position"]),
            agent_name=str(s["agent_name"]).strip(),
            instructions=str(s.get("instructions") or ""),
            fan_out=1 if s.get("fan_out") else 0,
            step_timeout_seconds=int(s.get("step_timeout_seconds") or 600),
        ))
    await session.commit()
    await session.refresh(row)
    return row


async def delete_workflow(session: AsyncSession, workflow_id: int) -> bool:
    row = await session.get(Workflow, workflow_id)
    if row is None:
        return False
    await session.execute(
        delete(WorkflowBranch).where(WorkflowBranch.workflow_id == workflow_id)
    )
    await session.execute(
        delete(WorkflowStep).where(WorkflowStep.workflow_id == workflow_id)
    )
    await session.delete(row)
    await session.commit()
    return True


# ---------------------------------------------------------------------------
# Workflow run records
# ---------------------------------------------------------------------------
async def create_workflow_run(
    session: AsyncSession,
    *,
    workflow: Workflow,
    trigger: str,
    created_by: str | None = None,
    scheduler: dict[str, str] | None = None,
) -> WorkflowRun:
    row = WorkflowRun(
        id=str(uuid.uuid4()),
        workflow_id=workflow.id,
        workflow_name=workflow.name,
        trigger=trigger,
        status=ACTIVE_RUN_STATUS,
        started_at=datetime.now(timezone.utc),
        created_by=created_by,
        scheduler_job_id=(scheduler or {}).get("job_id"),
        scheduler_schedule_id=(scheduler or {}).get("schedule_id"),
        scheduler_run_id=(scheduler or {}).get("run_id"),
        scheduler_host=(scheduler or {}).get("host"),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def finish_workflow_run(
    session: AsyncSession,
    run_id: str,
    *,
    status: str,
    summary: str | None = None,
    error: str | None = None,
    counts: dict[str, int] | None = None,
) -> None:
    row = await session.get(WorkflowRun, run_id)
    if row is None:
        return
    row.status = status
    row.summary = summary
    row.error = error
    for key, value in (counts or {}).items():
        setattr(row, key, int(value))
    row.finished_at = datetime.now(timezone.utc)
    await session.commit()


async def create_item_run(
    session: AsyncSession,
    *,
    run_id: str,
    item_key: str,
    title: str,
    branches: list[str],
) -> WorkflowItemRun:
    parent = await session.get(WorkflowRun, run_id)
    row = WorkflowItemRun(
        id=str(uuid.uuid4()),
        workflow_run_id=run_id,
        workflow_id=parent.workflow_id if parent is not None else 0,
        item_key=item_key[:255],
        title=title,
        branches_json=json.dumps(branches) if branches else None,
        status=ACTIVE_RUN_STATUS,
        started_at=datetime.now(timezone.utc),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def finish_item_run(
    session: AsyncSession, item_run_id: str, *, status: str, error: str | None = None
) -> None:
    row = await session.get(WorkflowItemRun, item_run_id)
    if row is None:
        return
    row.status = status
    row.error = error
    row.finished_at = datetime.now(timezone.utc)
    await session.commit()


async def create_step_run(
    session: AsyncSession,
    *,
    run_id: str,
    item_run_id: str | None,
    branch_key: str | None,
    position: int,
    agent_name: str,
) -> WorkflowStepRun:
    row = WorkflowStepRun(
        id=str(uuid.uuid4()),
        workflow_run_id=run_id,
        item_run_id=item_run_id,
        branch_key=branch_key,
        position=position,
        agent_name=agent_name,
        status=ACTIVE_RUN_STATUS,
        started_at=datetime.now(timezone.utc),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def finish_step_run(
    session: AsyncSession,
    step_run_id: str,
    *,
    status: str,
    output: str | None = None,
    error: str | None = None,
) -> None:
    row = await session.get(WorkflowStepRun, step_run_id)
    if row is None:
        return
    row.status = status
    row.output = output
    row.error = error
    row.finished_at = datetime.now(timezone.utc)
    await session.commit()


async def get_workflow_run(session: AsyncSession, run_id: str) -> WorkflowRun | None:
    return await session.get(WorkflowRun, run_id)


async def list_workflow_runs(
    session: AsyncSession, *, limit: int = 50, workflow_id: int | None = None
) -> list[WorkflowRun]:
    stmt = select(WorkflowRun).order_by(WorkflowRun.started_at.desc()).limit(limit)
    if workflow_id is not None:
        stmt = stmt.where(WorkflowRun.workflow_id == workflow_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_item_runs(session: AsyncSession, run_id: str) -> list[WorkflowItemRun]:
    result = await session.execute(
        select(WorkflowItemRun)
        .where(WorkflowItemRun.workflow_run_id == run_id)
        .order_by(WorkflowItemRun.started_at, WorkflowItemRun.id)
    )
    return list(result.scalars().all())


async def list_step_runs(session: AsyncSession, run_id: str) -> list[WorkflowStepRun]:
    result = await session.execute(
        select(WorkflowStepRun)
        .where(WorkflowStepRun.workflow_run_id == run_id)
        .order_by(WorkflowStepRun.started_at, WorkflowStepRun.id)
    )
    return list(result.scalars().all())


async def active_workflow_run(
    session: AsyncSession, workflow_id: int
) -> WorkflowRun | None:
    result = await session.execute(
        select(WorkflowRun).where(
            WorkflowRun.workflow_id == workflow_id,
            WorkflowRun.status == ACTIVE_RUN_STATUS,
        )
    )
    return result.scalars().first()


async def item_succeeded_before(
    session: AsyncSession, *, workflow_id: int, item_key: str
) -> bool:
    """Has this workflow already completed this item successfully?

    Per workflow, not global: the same email may legitimately be processed by
    two different workflows.
    """
    result = await session.execute(
        select(WorkflowItemRun.id).where(
            WorkflowItemRun.workflow_id == workflow_id,
            WorkflowItemRun.item_key == item_key,
            WorkflowItemRun.status == "success",
        ).limit(1)
    )
    return result.scalars().first() is not None


async def sweep_stale_workflow_runs(
    session: AsyncSession, *, all_running: bool = False
) -> int:
    """Mark leftover `running` rows interrupted. Returns how many were swept.

    A run row is the overlap lock, so a row left `running` by a hard crash
    would wedge the workflow until someone noticed.
    """
    result = await session.execute(
        select(WorkflowRun).where(WorkflowRun.status == ACTIVE_RUN_STATUS)
    )
    rows = list(result.scalars().all())
    if not all_running:
        return len(rows)
    for row in rows:
        row.status = "interrupted"
        row.error = "Run was still marked running at startup; marked interrupted."
        row.finished_at = datetime.now(timezone.utc)
    items = await session.execute(
        select(WorkflowItemRun).where(WorkflowItemRun.status == ACTIVE_RUN_STATUS)
    )
    for item in items.scalars().all():
        item.status = "interrupted"
        item.finished_at = datetime.now(timezone.utc)
    steps = await session.execute(
        select(WorkflowStepRun).where(WorkflowStepRun.status == ACTIVE_RUN_STATUS)
    )
    for step in steps.scalars().all():
        step.status = "interrupted"
        step.finished_at = datetime.now(timezone.utc)
    await session.commit()
    return len(rows)


async def get_orchestrator_instructions(session: AsyncSession) -> str:
    row = await session.get(OrchestratorConfig, 1)
    return row.instructions if row else DEFAULT_ORCHESTRATOR_INSTRUCTIONS


async def set_orchestrator_instructions(session: AsyncSession, instructions: str) -> None:
    row = await session.get(OrchestratorConfig, 1)
    if row is None:
        row = OrchestratorConfig(id=1, instructions=instructions)
        session.add(row)
    else:
        row.instructions = instructions
    await session.commit()


async def get_active_model_name(session: AsyncSession) -> str | None:
    row = await session.get(OrchestratorConfig, 1)
    return row.model_name if row else None


async def set_active_model_name(session: AsyncSession, model_name: str) -> None:
    row = await session.get(OrchestratorConfig, 1)
    if row is None:
        row = OrchestratorConfig(
            id=1,
            instructions=DEFAULT_ORCHESTRATOR_INSTRUCTIONS,
            model_name=model_name,
        )
        session.add(row)
    else:
        row.model_name = model_name
    await session.commit()


# ---------------------------------------------------------------------------
# Per-user OAuth2 token + flow-state storage (auth_mode="oauth2")
# ---------------------------------------------------------------------------
async def get_user_token(
    session: AsyncSession, user_id: str, server_key: str
) -> McpOAuthToken | None:
    result = await session.execute(
        select(McpOAuthToken).where(
            McpOAuthToken.user_id == user_id,
            McpOAuthToken.server_key == server_key,
        )
    )
    return result.scalar_one_or_none()


async def upsert_user_token(
    session: AsyncSession,
    *,
    user_id: str,
    server_key: str,
    access_token: str,
    refresh_token: str | None,
    token_type: str = "Bearer",
    scope: str | None = None,
    expires_at: datetime | None = None,
) -> McpOAuthToken:
    row = await get_user_token(session, user_id, server_key)
    if row is None:
        row = McpOAuthToken(user_id=user_id, server_key=server_key)
        session.add(row)
    row.access_token = access_token
    # A refresh response may omit refresh_token; keep the existing one then.
    if refresh_token:
        row.refresh_token = refresh_token
    row.token_type = token_type or "Bearer"
    row.scope = scope
    row.expires_at = expires_at
    await session.commit()
    await session.refresh(row)
    return row


async def delete_user_token(
    session: AsyncSession, user_id: str, server_key: str
) -> None:
    await session.execute(
        delete(McpOAuthToken).where(
            McpOAuthToken.user_id == user_id,
            McpOAuthToken.server_key == server_key,
        )
    )
    await session.commit()


async def get_oauth_client(
    session: AsyncSession, server_key: str
) -> McpOAuthClient | None:
    return await session.get(McpOAuthClient, server_key)


async def delete_oauth_client(session: AsyncSession, server_key: str) -> None:
    """Drop a registered client so the next use re-runs discovery + DCR.

    Needed when the target's authorization server stops honouring the client
    we registered (e.g. it signs stateless client_ids with a secret that
    rotates on restart) — the cached row is otherwise never invalidated.
    """
    await session.execute(
        delete(McpOAuthClient).where(McpOAuthClient.server_key == server_key)
    )
    await session.commit()


async def save_oauth_client(
    session: AsyncSession,
    *,
    server_key: str,
    authorize_url: str,
    token_url: str,
    client_id: str,
    client_secret: str | None,
    scope: str | None,
    redirect_uri: str,
) -> McpOAuthClient:
    row = await session.get(McpOAuthClient, server_key)
    if row is None:
        row = McpOAuthClient(server_key=server_key)
        session.add(row)
    row.authorize_url = authorize_url
    row.token_url = token_url
    row.client_id = client_id
    row.client_secret = client_secret
    row.scope = scope
    row.redirect_uri = redirect_uri
    await session.commit()
    await session.refresh(row)
    return row


async def save_oauth_state(
    session: AsyncSession,
    *,
    state: str,
    user_id: str,
    server_key: str,
    code_verifier: str,
    redirect_uri: str,
    expires_at: datetime,
) -> None:
    # Opportunistically sweep expired rows so the table can't grow unbounded.
    await session.execute(
        delete(McpOAuthState).where(McpOAuthState.expires_at < datetime.now(timezone.utc))
    )
    session.add(
        McpOAuthState(
            state=state,
            user_id=user_id,
            server_key=server_key,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
            expires_at=expires_at,
        )
    )
    await session.commit()


async def pop_oauth_state(session: AsyncSession, state: str) -> McpOAuthState | None:
    """Fetch and delete a flow state by its CSRF token. Returns None if missing
    or expired."""
    row = await session.get(McpOAuthState, state)
    if row is None:
        return None
    # Detach a plain snapshot before deleting so callers can read its fields.
    snapshot = McpOAuthState(
        state=row.state,
        user_id=row.user_id,
        server_key=row.server_key,
        code_verifier=row.code_verifier,
        redirect_uri=row.redirect_uri,
        expires_at=row.expires_at,
    )
    await session.delete(row)
    await session.commit()
    expires_at = snapshot.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at is not None and expires_at < datetime.now(timezone.utc):
        return None
    return snapshot


# ---------------------------------------------------------------------------
# Job runs
# ---------------------------------------------------------------------------
ACTIVE_RUN_STATUS = "running"


async def create_job_run(
    session: AsyncSession,
    *,
    agent: AgentConfig,
    trigger: str,
    created_by: str | None = None,
    scheduler: dict[str, str] | None = None,
) -> JobRun:
    row = JobRun(
        id=str(uuid.uuid4()),
        agent_id=agent.id,
        agent_name=agent.name,
        trigger=trigger,
        status=ACTIVE_RUN_STATUS,
        started_at=datetime.now(timezone.utc),
        timeout_seconds=agent.run_timeout_seconds,
        created_by=created_by,
        scheduler_job_id=(scheduler or {}).get("job_id"),
        scheduler_schedule_id=(scheduler or {}).get("schedule_id"),
        scheduler_run_id=(scheduler or {}).get("run_id"),
        scheduler_host=(scheduler or {}).get("host"),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def finish_job_run(
    session: AsyncSession,
    run_id: str,
    *,
    status: str,
    summary: str | None = None,
    report: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    row = await session.get(JobRun, run_id)
    if row is None:
        return
    row.status = status
    row.summary = summary
    row.report_json = json.dumps(report) if report is not None else None
    row.error = error
    row.finished_at = datetime.now(timezone.utc)
    await session.commit()


async def get_job_run(session: AsyncSession, run_id: str) -> JobRun | None:
    return await session.get(JobRun, run_id)


async def list_job_runs(
    session: AsyncSession, *, limit: int = 50, agent_id: int | None = None
) -> list[JobRun]:
    stmt = select(JobRun).order_by(JobRun.started_at.desc()).limit(limit)
    if agent_id is not None:
        stmt = stmt.where(JobRun.agent_id == agent_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def active_job_run(session: AsyncSession, agent_id: int) -> JobRun | None:
    result = await session.execute(
        select(JobRun).where(
            JobRun.agent_id == agent_id, JobRun.status == ACTIVE_RUN_STATUS
        )
    )
    return result.scalars().first()


async def sweep_stale_runs(session: AsyncSession, *, all_running: bool = False) -> int:
    """Mark runs that outlived their timeout as interrupted.

    A crashed or redeployed run would otherwise stay `running` forever and
    wedge the overlap lock, making the agent permanently untriggerable.

    With ``all_running=True`` every `running` row is swept regardless of age.
    That is the correct behaviour at startup: a freshly started process owns
    no in-flight run, so any row still marked running is by definition a
    ghost from the previous process — waiting out its (up to 24h) timeout
    would leave the agent untriggerable for that whole window. The age-based
    default is for sweeps taken while this process is live, where a young
    `running` row may well be one of our own.
    """
    result = await session.execute(
        select(JobRun).where(JobRun.status == ACTIVE_RUN_STATUS)
    )
    now = datetime.now(timezone.utc)
    swept = 0
    for row in result.scalars().all():
        if not all_running:
            started = row.started_at
            if started is None:
                continue
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if (now - started).total_seconds() <= row.timeout_seconds:
                continue
        row.status = "interrupted"
        row.finished_at = now
        row.error = (
            "Run was interrupted by an app restart."
            if all_running
            else "Run did not finish within its timeout (app restart or crash)."
        )
        swept += 1
    if swept:
        await session.commit()
    return swept
