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
VALID_AUTH_MODES = frozenset({AUTH_MODE_JWT, AUTH_MODE_NONE, AUTH_MODE_OAUTH2})

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
        String(16), nullable=False, default=AUTH_MODE_JWT, server_default=AUTH_MODE_JWT
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
    # JSON list of source_keys a complete report must contain.
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
    def expected_sections(self) -> list[str]:
        """source_keys a complete report must contain (may be empty)."""
        if not self.expected_sections_json:
            return []
        try:
            data = json.loads(self.expected_sections_json)
        except Exception:
            logger.warning("Malformed expected_sections_json on agent %s", self.name)
            return []
        if not isinstance(data, list):
            return []
        return [str(s) for s in data if isinstance(s, str) and s.strip()]

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
            "enabled": bool(self.enabled),
            "expose_chat": bool(self.expose_chat),
            "expose_api": bool(self.expose_api),
            "api_slug": self.api_slug,
            "run_as_principal": self.run_as_principal,
            "run_prompt": self.run_prompt or "",
            "run_timeout_seconds": self.run_timeout_seconds,
            "expected_sections": self.expected_sections,
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
            "enabled": bool(self.enabled),
            "expose_chat": bool(self.expose_chat),
            "expose_api": bool(self.expose_api),
            "api_slug": self.api_slug,
            "run_prompt": self.run_prompt or "",
            "run_timeout_seconds": self.run_timeout_seconds,
            "expected_sections": self.expected_sections,
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
    "Perform your configured check now and return the structured report."
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

    Returns None for non-oauth2 modes.
    """
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
    run_as_principal: str | None = None,
    run_prompt: str | None = None,
    run_timeout_seconds: int = 1800,
    expected_sections: list[str] | None = None,
) -> AgentConfig:
    existing = await get_agent_by_name(session, name)
    primary, extras, primary_oauth_json = prepare_servers(mcp_servers, existing)
    extras_json = json.dumps(extras) if extras else None
    skills_json = await normalize_skills_json(session, skills)

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
        row.run_as_principal = (run_as_principal or "").strip() or None
        row.run_prompt = (run_prompt or "").strip() or None
        row.run_timeout_seconds = int(run_timeout_seconds)
        row.expected_sections_json = (
            json.dumps(expected_sections) if expected_sections else None
        )
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
        existing.run_as_principal = (run_as_principal or "").strip() or None
        existing.run_prompt = (run_prompt or "").strip() or None
        existing.run_timeout_seconds = int(run_timeout_seconds)
        existing.expected_sections_json = (
            json.dumps(expected_sections) if expected_sections else None
        )
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
