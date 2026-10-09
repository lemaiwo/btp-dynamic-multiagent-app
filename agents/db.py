"""Database layer for dynamic agent configuration.

SQLAlchemy async on one of three databases. On SAP BTP the connection is
resolved from VCAP_SERVICES: a `postgresql-db` binding (asyncpg), or an SAP
HANA Cloud HDI container (`hana`, plan `hdi-shared`; `hana+aiohdbcli`),
whichever is bound -- `DB_KIND` decides when both are. Locally it falls back
to the DATABASE_URL environment variable (or a SQLite file for quick
experiments).

The models are the schema on all three. Postgres and SQLite get it from
`create_all` plus the additive `_ensure_*` steps of `init_db`; an HDI
container accepts no DDL from the app's user, so there `init_db` deploys the
same models as design-time artifacts instead (agents/hana_hdi.py).
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    exists,
    false,
    func,
    insert,
    literal,
    select,
    text,
    true,
    update,
)
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from agents import hana_hdi
from agents.odata import BUILTIN_ODATA_URL

# The catalogue's save-time gate lives with its models; re-exported because
# every other `validate_*` of a stored definition is found in this module.
from agents.odata.models import SERVICE_NAME_RE as _ODATA_SERVICE_NAME_RE
from agents.odata.models import WRITE_OPS as _ODATA_WRITE_OPS
from agents.odata.models import ServiceDefinition as _ODataServiceDefinition
from agents.odata.models import operation_is_write as _odata_operation_is_write
from agents.odata.models import validate_odata_service  # noqa: F401  (re-export)

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
# A browser session cookie a human obtained and pasted in. Distinct from
# oauth2 because there is no authorize endpoint and nothing to refresh with:
# only a person with a browser can renew it, which is exactly what the
# credentials panel needs to be able to say.
AUTH_MODE_SESSION = "session"
VALID_AUTH_MODES = frozenset({
    AUTH_MODE_JWT, AUTH_MODE_NONE, AUTH_MODE_OAUTH2, AUTH_MODE_APP_ONLY,
    AUTH_MODE_DESTINATION, AUTH_MODE_SESSION,
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


class DatabaseConfigError(RuntimeError):
    """The environment does not say which database to use, or names one that
    cannot be used. Raised while this module is imported, so the app does not
    start; the message never repeats a credential or a URL."""


# VCAP_SERVICES labels per database kind. ``hana`` is the label of SAP HANA
# Cloud's schema and HDI container service (plan ``hdi-shared`` is the one
# this app can use: see agents/hana_hdi.py).
_POSTGRES_LABELS = ("postgresql-db", "postgresql", "hyperscaler-option-postgresql")
_HANA_LABELS = ("hana",)
DB_KINDS = ("postgres", "hana")
HANA_DRIVER = "hana+aiohdbcli"
# A HANA connection is always encrypted and its certificate always
# validated. There is deliberately no switch for anything less (compare
# PG_SSL_INSECURE above, which this does not copy).
_HANA_TLS = {"encrypt": "true", "sslValidateCertificate": "true"}


@dataclass(frozen=True)
class DatabaseTarget:
    """Which database this process uses. ``url`` holds the password, so a
    ``repr`` names only the kind."""

    url: str = field(repr=False)
    kind: str  # "postgres", "hana" or "sqlite"; "other" for an unknown scheme
    ssl_ca: str | None = field(default=None, repr=False)
    # SAP HANA only: the design-time user that deploys the tables.
    hdi: hana_hdi.HdiCredentials | None = field(default=None, repr=False)


def _on_cloud_foundry() -> bool:
    """A deployed app: the platform sets VCAP_APPLICATION for every one.

    Decides one thing: locally an environment that names no usable database
    ends on a SQLite file, which is a convenience; on Cloud Foundry the same
    fallback would run the app on a file the next restart deletes, with
    nobody told, so there it is a :class:`DatabaseConfigError` instead.
    """
    return bool(os.environ.get("VCAP_APPLICATION", "").strip())


def _is_hdi_container(instance: Any) -> bool:
    credentials = instance.get("credentials") if isinstance(instance, dict) else None
    return isinstance(instance, dict) and (
        instance.get("plan") == "hdi-shared"
        or (isinstance(credentials, dict) and bool(credentials.get("hdi_user")))
    )


def _bound_databases(services: Any) -> dict[str, dict[str, Any]]:
    """``{kind: credentials}`` of the database services bound to the app.

    The first instance of a label, except for ``hana``: that label also
    covers plain schemas, and of several bindings the HDI container (plan
    ``hdi-shared``, or credentials with an ``hdi_user``) is the one the app
    can create its tables in. A binding whose ``credentials`` is not an
    object is skipped locally and refused on Cloud Foundry.
    """
    bound: dict[str, dict[str, Any]] = {}
    if not isinstance(services, dict):
        return bound
    for kind, labels in (("postgres", _POSTGRES_LABELS), ("hana", _HANA_LABELS)):
        for label in labels:
            instances = services.get(label)
            if not isinstance(instances, list) or not instances:
                continue
            chosen = instances[0]
            if kind == "hana":
                chosen = next((i for i in instances if _is_hdi_container(i)), chosen)
            credentials = chosen.get("credentials") if isinstance(chosen, dict) else None
            if isinstance(credentials, dict):
                bound[kind] = credentials
                break
            if _on_cloud_foundry():
                raise DatabaseConfigError(
                    f"The credentials of the bound {label} service are not an object"
                )
    return bound


def _choose_kind(bound: dict[str, dict[str, Any]]) -> str | None:
    """Which of the bound databases to use; None when none is bound.

    One kind bound: that one. Both bound: only what ``DB_KIND`` names; with
    no ``DB_KIND`` the start is refused, because guessing would let a
    landscape that switches from one to the other run on the wrong one (and
    on HANA, deploy a schema) without anyone having said so. ``DB_KIND``
    naming a kind that is not bound is refused as well, never answered with
    the other one.
    """
    wanted = os.environ.get("DB_KIND", "").strip().lower()
    if not bound:
        return None
    if wanted and wanted not in DB_KINDS:
        raise DatabaseConfigError(
            "DB_KIND must be 'postgres' or 'hana' (it selects one of the bound "
            "database services)"
        )
    if wanted:
        if wanted not in bound:
            raise DatabaseConfigError(
                f"DB_KIND names '{wanted}', but no such database service is bound"
            )
        return wanted
    if len(bound) > 1:
        raise DatabaseConfigError(
            "Both a PostgreSQL and an SAP HANA service are bound. Set DB_KIND to "
            "'postgres' or 'hana' to say which one this app uses, or unbind the other."
        )
    return next(iter(bound))


def _postgres_target(creds: dict[str, Any]) -> DatabaseTarget:
    # BTP PG credentials expose hostname, port, username, password, dbname, sslcert
    host = creds.get("hostname") or creds.get("host")
    port = creds.get("port", 5432)
    user = creds.get("username")
    password = creds.get("password")
    dbname = creds.get("dbname") or creds.get("database")
    # BTP exposes the server CA under one of these keys
    ssl_ca = (
        creds.get("sslrootcert")
        or creds.get("sslcert")
        or creds.get("ca")
        or creds.get("cert")
    )
    sslmode = "require"
    return DatabaseTarget(
        f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{dbname}"
        f"?ssl={sslmode}",
        "postgres",
        ssl_ca=ssl_ca,
    )


def _hana_target(creds: dict[str, Any]) -> DatabaseTarget:
    """An ``hdi-shared`` binding: the runtime user for the engine, the
    design-time user for :func:`init_db`."""
    missing = [k for k in ("host", "port", "user", "password", "schema") if not creds.get(k)]
    if missing:
        raise DatabaseConfigError(
            "The SAP HANA binding has no " + ", ".join(missing)
        )
    url = URL.create(
        HANA_DRIVER,
        username=str(creds["user"]),
        password=str(creds["password"]),
        host=str(creds["host"]),
        port=int(creds["port"]),
        query={**_HANA_TLS, "currentSchema": str(creds["schema"])},
    )
    try:
        hdi = hana_hdi.credentials_from_binding(creds)
    except hana_hdi.HdiError:
        # Not an HDI container (plan ``schema``, say). init_db refuses it
        # with the reason; rows of an existing schema could still be read.
        hdi = None
    certificate = creds.get("certificate")
    return DatabaseTarget(
        url.render_as_string(hide_password=False),
        "hana",
        ssl_ca=str(certificate) if certificate else None,
        hdi=hdi,
    )


def _hana_target_from_url(raw: str) -> DatabaseTarget:
    """A ``hana://`` ``DATABASE_URL`` (local runs, tests).

    Any driver spelling becomes the async one, and the two TLS options are
    set whatever the URL said. The design-time user, which a URL has no
    place for, comes from ``HANA_HDI_USER`` / ``HANA_HDI_PASSWORD``; the
    container schema is the URL's ``currentSchema``.
    """
    try:
        url = make_url(raw)
    except Exception:  # noqa: BLE001 -- the text would repeat the URL
        raise DatabaseConfigError("DATABASE_URL is not a valid SAP HANA URL") from None
    if not url.host:
        raise DatabaseConfigError("The SAP HANA DATABASE_URL names no host")
    # The driver reads its options whatever their case, so every spelling of
    # a TLS option goes before the fixed values are set: `ENCRYPT=false`
    # must not survive next to `encrypt=true`. sslHostNameInCertificate goes
    # too: `*` would accept a valid certificate of any other host.
    fixed = {key.lower(): value for key, value in _HANA_TLS.items()}
    weaker = False
    kept: dict[str, Any] = {}
    for key, value in url.query.items():
        name = key.lower()
        if name in fixed:
            weaker = weaker or str(value).lower() != fixed[name]
        elif name == "sslhostnameincertificate":
            weaker = True
        else:
            kept[key] = value
    if weaker:
        logger.warning(
            "DATABASE_URL asked for an unencrypted or unvalidated SAP HANA "
            "connection; ignored, the connection is encrypted and validated"
        )
    url = url.set(drivername=HANA_DRIVER, query={**kept, **_HANA_TLS})
    hdi = None
    user = os.environ.get("HANA_HDI_USER", "").strip()
    password = os.environ.get("HANA_HDI_PASSWORD", "")
    schema = url.query.get("currentSchema")
    if user and password and url.host and isinstance(schema, str) and schema:
        hdi = hana_hdi.HdiCredentials(
            host=url.host, port=int(url.port or 443), schema=schema,
            user=user, password=password,
        )
    return DatabaseTarget(url.render_as_string(hide_password=False), "hana", hdi=hdi)


def _target_from_url(url: str, *, on_cf: bool = False) -> DatabaseTarget:
    """A database named by a URL (``DATABASE_URL``, or the variable a script
    was pointed at): the driver and TLS rules of each kind applied."""
    if url.startswith(("hana://", "hana+")):
        return _hana_target_from_url(url)
    # Normalize common variants to async driver
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql"):
        return DatabaseTarget(url, "postgres")
    if not url.startswith("sqlite"):
        return DatabaseTarget(url, "other")
    if on_cf:
        raise DatabaseConfigError(
            "DATABASE_URL names a SQLite file, which a deployed app loses at "
            "every restart; bind a PostgreSQL or SAP HANA service"
        )
    return DatabaseTarget(url, "sqlite")


def bound_target(kind: str) -> DatabaseTarget:
    """The bound database service of ``kind`` (``postgres`` | ``hana``),
    whatever ``DB_KIND`` says: for a script that works on both at once
    (scripts/copy_registry_config.py). ``DatabaseConfigError`` when no such
    service is bound."""
    if kind not in DB_KINDS:
        raise DatabaseConfigError("A database kind is 'postgres' or 'hana'")
    try:
        services = json.loads(os.environ.get("VCAP_SERVICES") or "{}")
    except ValueError:
        raise DatabaseConfigError("VCAP_SERVICES is not valid JSON") from None
    credentials = _bound_databases(services).get(kind)
    if credentials is None:
        raise DatabaseConfigError(f"No {kind} database service is bound")
    return _hana_target(credentials) if kind == "hana" else _postgres_target(credentials)


def _resolve_database() -> DatabaseTarget:
    """The database from VCAP_SERVICES or the environment.

    A bound service wins over ``DATABASE_URL``; which of two bound kinds is
    :func:`_choose_kind`. Locally ``DATABASE_URL`` names PostgreSQL, SAP
    HANA (``hana://`` or ``hana+aiohdbcli://``) or SQLite, and without it a
    SQLite file is used.
    """
    on_cf = _on_cloud_foundry()
    vcap = os.environ.get("VCAP_SERVICES")
    if vcap:
        try:
            services = json.loads(vcap)
        except Exception:
            if on_cf:
                raise DatabaseConfigError("VCAP_SERVICES is not valid JSON") from None
            logger.exception("Failed to parse VCAP_SERVICES for a database binding")
            services = None
        bound = _bound_databases(services)
        kind = _choose_kind(bound)
        if kind == "hana":
            return _hana_target(bound[kind])
        if kind == "postgres":
            try:
                return _postgres_target(bound[kind])
            except Exception:
                if on_cf:
                    raise DatabaseConfigError(
                        "The bound PostgreSQL service could not be read"
                    ) from None
                logger.exception("Failed to parse VCAP_SERVICES for postgres")

    url = os.environ.get("DATABASE_URL")
    if url:
        return _target_from_url(url, on_cf=on_cf)

    if on_cf:
        raise DatabaseConfigError(
            "On Cloud Foundry no database service is bound (postgresql-db, or "
            "hana with plan hdi-shared) and no DATABASE_URL is set"
        )
    # Local dev fallback
    logger.warning("No DATABASE_URL or VCAP database binding; using local SQLite")
    return DatabaseTarget("sqlite+aiosqlite:///./agents_registry.db", "sqlite")


def _resolve_database_url() -> str:
    """Build an async SQLAlchemy URL from VCAP_SERVICES or env."""
    return _resolve_database().url


def _engine_settings(target: DatabaseTarget) -> tuple[str, dict[str, Any]]:
    """``(url, connect_args)`` for ``create_async_engine``."""
    url = target.url
    connect_args: dict[str, Any] = {}
    if url.startswith("postgresql+asyncpg") and "ssl=" in url:
        # asyncpg expects ssl via connect_args, not URL; strip & pass through
        url, _, _query = url.partition("?")
        connect_args["ssl"] = _build_ssl_context(target.ssl_ca)
    elif target.kind == "hana" and target.ssl_ca:
        # The binding's CA as the trust store, in memory and out of the URL.
        # Encryption and validation themselves are in the URL (_HANA_TLS).
        connect_args["sslTrustStore"] = target.ssl_ca
    return url, connect_args


_target = _resolve_database()
# The HDI design-time user, on SAP HANA; what init_db deploys the tables with.
_hana_hdi: hana_hdi.HdiCredentials | None = _target.hdi
DATABASE_URL, _connect_args = _engine_settings(_target)

# Nothing touches Postgres between admin clicks, and CF's health check hits
# /healthz, which answers from memory — so a pooled connection can sit idle
# for hours. Measured on ACC: the first DB-backed request after an idle gap
# cost 8s after ~10 minutes and 18s overnight, while a request issued 20ms
# later that opened no session answered in 20ms.
#
# pool_pre_ping issues a `SELECT 1` on checkout, so a connection the network
# has already dropped is replaced there and then instead of stalling whichever
# request happened to draw it. pool_recycle retires a connection before the
# path in front of Postgres times it out, so replacement happens on our
# schedule rather than mid-request.
#
# Neither keeps the pool *warm* — they only make going cold cheap to recover
# from. `_db_heartbeat` in app.py is what stops it going cold at all.
engine = create_async_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    future=True,
    pool_pre_ping=True,
    pool_recycle=1800,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


# ---------------------------------------------------------------------------
# ORM models
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


class AgentConfig(Base):
    __tablename__ = "agent_configs"
    __table_args__ = (
        UniqueConstraint("name", name="uq_agent_configs_name"),
        # Partial: many agents have no slug, and NULLs must not collide. The
        # check in upsert_agent stays as the friendly 422; this closes the
        # check-then-write race that could otherwise yield two rows for one
        # slug and a MultipleResultsFound 500 on the scheduler endpoint.
        Index(
            "uq_agent_configs_api_slug", "api_slug", unique=True,
            postgresql_where=text("api_slug IS NOT NULL"),
            sqlite_where=text("api_slug IS NOT NULL"),
        ),
    )

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
    # URL segment for POST /api/agents/{api_slug}/run. Uniqueness is checked
    # in upsert_agent (for the readable error) and enforced by the partial
    # unique index in __table_args__ (for the race).
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
    # --- deep agents ---
    # JSON-encoded agents.deep.DeepConfig: planning tool, per-run scratchpad
    # and ephemeral sub-agents, all opt-in. Null means the defaults (off).
    deep_json: Mapped[str | None] = mapped_column(Text, nullable=True)

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

    # --- deep agents ---
    @property
    def deep(self):
        """The parsed deep-agent config (``agents.deep.DeepConfig``); the
        defaults, i.e. disabled, when the column is null or malformed."""
        from agents.deep import parse_deep_config

        return parse_deep_config(self.deep_json, agent_name=self.name)

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
            "deep": self.deep.model_dump(),
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
            "deep": self.deep.model_dump(),
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



def _odata_stamp(value: datetime | None) -> str | None:
    """A stored timestamp the way ``ODataServicePayload`` dumps one.

    SQLite hands a timezone-aware column back naive, Postgres hands it back
    aware; both hold UTC (`_odata_utc` writes it). Formatting it here the way
    pydantic does keeps ``to_export()`` equal to a payload dump on either
    database, so an export imports unchanged.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    text_value = value.astimezone(timezone.utc).isoformat()
    return text_value.replace("+00:00", "Z")


def _odata_utc(value: Any) -> datetime | None:
    """``metadata_fetched_at`` of a validated payload as an aware UTC datetime.

    A value without an offset is taken as UTC: the app writes this stamp
    itself (the moment `$metadata` was read), always in UTC.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ODataService(Base):
    """One catalogue service of ``builtin:odata``.

    What an admin curated from a service's ``$metadata``: the destination it
    is reached through, whose identity a call carries (``user_context``) and
    the entity sets, fields and operations that exist for agents
    (``definition_json``, an ``agents.odata.models.ServiceDefinition``).
    Agents attach a service by ``name``, like skills, so the name is the
    stable handle and is never changed after creation.
    """

    __tablename__ = "odata_services"
    __table_args__ = (UniqueConstraint("name", name="uq_odata_services_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Widths follow the limits in agents/odata/models.py. SQLite ignores
    # them and Postgres does not (see AUTH_MODE_MAX_LENGTH above), so a
    # limit raised there must be raised here too; tests/test_odata_db.py
    # holds the two together.
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    purpose: Mapped[str] = mapped_column(String(200), nullable=False)
    not_for: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", server_default=""
    )
    destination: Mapped[str] = mapped_column(String(200), nullable=False)
    # 1 = every call runs as the signed-in user (principal propagation);
    # 0 = as the destination's technical user. Integer 0/1 like expose_chat.
    user_context: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    odata_version: Mapped[str] = mapped_column(String(2), nullable=False)
    service_path: Mapped[str] = mapped_column(String(512), nullable=False)
    definition_json: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    enabled: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # The version the admin API compares with ``expected_updated_at``. The
    # default stamps a new row; no ``onupdate``: `_next_odata_stamp` owns the
    # column on update (`update_odata_service`), and a second source would
    # be whole seconds on SQLite and the transaction's start on Postgres.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def _stored_definition(self) -> dict[str, Any] | None:
        """The stored definition, or None when it is not a JSON object."""
        try:
            value = json.loads(self.definition_json or "")
        except (TypeError, ValueError):
            logger.warning("Malformed definition_json on OData service %s", self.name)
            return None
        return value if isinstance(value, dict) else None

    @property
    def definition(self) -> dict[str, Any]:
        """The definition as a plain dict; ``{}`` when the stored text is
        unreadable, so a broken row still lists and can be repaired."""
        return self._stored_definition() or {}

    def _counts_and_write(self) -> tuple[dict[str, int], bool]:
        """Entity set and operation counts, and whether anything can change
        data in SAP (same rule as ``ServiceDefinition.has_write``).

        Read from the dict rather than through the models: the service list
        asks this for every row, and re-validating up to 2 MB per row would
        make the list as slow as the largest definition. An unreadable
        definition counts as writing: this flag labels a service in the
        admin list, and "read-only" must never be the answer to "unknown".
        """
        stored = self._stored_definition()
        if stored is None:
            return {"entity_sets": 0, "operations": 0}, True
        sets = [e for e in stored.get("entity_sets") or [] if isinstance(e, dict)]
        ops = [o for o in stored.get("operations") or [] if isinstance(o, dict)]
        has_write = any(
            op in _ODATA_WRITE_OPS for e in sets for op in e.get("operations") or []
        ) or any(
            # The models' own rule: a missing flag and every POST are writes.
            bool(o.get("enabled"))
            and _odata_operation_is_write(o.get("changes_data"), o.get("http_method"))
            for o in ops
        )
        return {"entity_sets": len(sets), "operations": len(ops)}, has_write

    def to_export(self) -> dict[str, Any]:
        """Exactly an ``ODataServicePayload`` dump: portable, no ids, and no
        timestamp except when the metadata was read."""
        return {
            "name": self.name,
            "title": self.title,
            "purpose": self.purpose,
            "not_for": self.not_for or "",
            "destination": self.destination,
            "user_context": bool(self.user_context),
            "odata_version": self.odata_version,
            "service_path": self.service_path,
            "enabled": bool(self.enabled),
            "definition": self.definition,
            "metadata_fetched_at": _odata_stamp(self.metadata_fetched_at),
        }

    def to_summary(self, used_by: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """``to_dict`` without the definition, for the service list."""
        counts, has_write = self._counts_and_write()
        data = self.to_export()
        del data["definition"]
        data.update(
            id=self.id,
            created_at=self.created_at.isoformat() if self.created_at else None,
            updated_at=self.updated_at.isoformat() if self.updated_at else None,
            counts=counts,
            has_write=has_write,
            used_by=list(used_by or []),
        )
        return data

    def to_dict(self, used_by: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """``to_summary`` plus the definition and ``uncallable_operations``:
        the ENABLED operations no agent can ever call, as ``[{name,
        reason}]`` (``agents.odata.calls``). A warning for the admin UI, not
        a refusal -- the save stands. Not in the summary: it validates every
        operation, which the service list must not do per row."""
        # Imported here: the dialects pull in the OData client.
        from agents.odata.calls import uncallable_operations

        data = self.to_summary(used_by)
        data["definition"] = self.definition
        data["uncallable_operations"] = uncallable_operations(
            self.definition, self.odata_version
        )
        return data


class ODataAuditLog(Base):
    """One row per create, update or delete an agent sent through
    ``builtin:odata``: who, which service and entity, and how it ended.

    Written in two steps by ``agents.odata.audit.StoredWriteRecorder``: the
    row is inserted with outcome ``intent`` before anything is sent, and
    finalised exactly once afterwards (a conditional UPDATE on ``outcome =
    'intent'``): by ``result``, or by ``abandon``, which closes the intent
    of a write that was never sent as ``refused`` / ``token``. Those two
    statements are the only ones that update a row, and only
    ``purge_odata_audit`` deletes rows, by age. A row that stays ``intent`` is explained in the
    docstring of ``agents.odata.audit``.

    Two identities, on purpose: ``sent_as`` is whose credential SAP saw
    (derived from the token actually sent, or ``technical:<destination>``),
    ``run_principal`` is the principal of the run -- in a job the run-as
    user, which can differ from the owner of the token the run carries.

    ``key_json`` / ``created_key_json`` hold the key VALUES of the entity
    touched (an audit must name it) and ``body_fields_json`` the *names* of
    the fields sent, never their values. So the table can hold personal
    data (key values, principals): it is read by admins only and purged by
    age. Never stored: a body value, a token, a cookie, an ETag. No foreign
    key to ``odata_services`` -- the log outlives the service it names.
    """

    __tablename__ = "odata_audit_log"
    __table_args__ = (
        UniqueConstraint("call_id", name="uq_odata_audit_log_call_id"),
        # What an operator asks: one service, one sender, or "what is still
        # open" (outcome = intent), each newest first.
        Index("ix_odata_audit_log_service_created", "service", "created_at"),
        Index("ix_odata_audit_log_sent_as_created", "sent_as", "created_at"),
        Index("ix_odata_audit_log_outcome_created", "outcome", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # UTC. `created_at` is when the intent was recorded (before the request),
    # `finished_at` when the result was; NULL while the row is an intent.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    call_id: Mapped[str] = mapped_column(String(32), nullable=False)  # uuid4 hex
    agent: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # "technical:<destination>", the principal of the token sent, or
    # "token:<sha256>" when that token's owner could not be named.
    sent_as: Mapped[str] = mapped_column(String(255), nullable=False)
    run_principal: Mapped[str | None] = mapped_column(String(255), nullable=True)
    token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)  # sha256 hex
    service: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)  # create|update|delete
    key_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_key_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_fields_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # NULL while the row is an intent (nothing is known yet -- it must not
    # read as "nothing left"); then "token": no modifying request left the
    # app, or "write": one did.
    phase: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # intent | ok | refused | sap_error | unknown | cancelled
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        """The row for the admin audit route (key values included)."""

        def stamp(value: datetime | None) -> str | None:
            if value is None:
                return None
            # SQLite hands a timestamp back without its zone; it was UTC.
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc).isoformat()

        def loaded(text_value: str | None) -> Any:
            if text_value is None:
                return None
            try:
                return json.loads(text_value)
            except ValueError:
                return None

        return {
            "id": self.id,
            "call_id": self.call_id,
            "created_at": stamp(self.created_at),
            "finished_at": stamp(self.finished_at),
            "agent": self.agent,
            "run_id": self.run_id,
            "service": self.service,
            "target": self.target,
            "operation": self.operation,
            "key": loaded(self.key_json),
            "created_key": loaded(self.created_key_json),
            "fields": loaded(self.body_fields_json) or [],
            "phase": self.phase,
            "outcome": self.outcome,
            "status": self.http_status,
            "sent_as": self.sent_as,
            "run_principal": self.run_principal,
            "token_digest": self.token_digest,
        }


class Workflow(Base):
    """A declared, ordered sequence of agents run as one background job.

    The definition is the authority: the engine runs exactly these steps in
    exactly this order. No model gets to reorder or skip them — the only
    per-item decision is which branches to enter, and that is data the
    fan-out step emits.
    """

    __tablename__ = "workflows"
    __table_args__ = (
        UniqueConstraint("name", name="uq_workflows_name"),
        Index(
            "uq_workflows_api_slug", "api_slug", unique=True,
            postgresql_where=text("api_slug IS NOT NULL"),
            sqlite_where=text("api_slug IS NOT NULL"),
        ),
    )

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

    def to_export(self, branches: list[Any], steps: list[Any]) -> dict[str, Any]:
        """The landscape-portable definition, branches and steps included.

        ``run_as_principal`` is deliberately absent, for the same reason
        AgentConfig.to_export omits its own: it is a landscape-specific
        service identity, not part of the definition being promoted. The
        import path passes nothing for it, so the target landscape keeps
        whatever it already has -- see the _Keep docstring.
        """
        return {
            "name": self.name,
            "description": self.description,
            "api_slug": self.api_slug,
            "run_timeout_seconds": self.run_timeout_seconds,
            "skip_seen_items": bool(self.skip_seen_items),
            "max_parallel_items": self.max_parallel_items,
            "on_unknown_branch": self.on_unknown_branch,
            "enabled": bool(self.enabled),
            "branches": [b.to_dict() for b in branches],
            "steps": [s.to_dict() for s in steps],
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
    # --- step kinds ---
    # "agent" (the default) or one of agents.step_kinds.DETERMINISTIC_KINDS.
    # A non-agent step keeps agent_name = "" and carries its settings in
    # config_json, validated by agents.step_kinds at save time.
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="agent", server_default="agent"
    )
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def config(self) -> dict[str, Any]:
        from agents.step_kinds import config_from_json  # noqa: PLC0415

        return config_from_json(self.config_json)

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch_key": self.branch_key,
            "position": self.position,
            "agent_name": self.agent_name,
            "instructions": self.instructions,
            "fan_out": bool(self.fan_out),
            "step_timeout_seconds": self.step_timeout_seconds,
            "kind": self.kind or "agent",
            "config": self.config,
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
    # Indexed for agents/notifications.py, which filters and orders on it
    # at every poll (ix_workflow_runs_finished_at).
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
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
            "workflow_run_id": self.workflow_run_id,
            "item_run_id": self.item_run_id,
            "branch_key": self.branch_key,
            "position": self.position,
            "agent_name": self.agent_name,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
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
    # Indexed for agents/notifications.py, which filters and orders on it
    # at every poll (ix_job_runs_finished_at).
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=1800)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What the run did, step by step (agents.run_activity). Held in memory
    # while the run is live and written here once, when it ends.
    activity_json: Mapped[str | None] = mapped_column(Text, nullable=True)
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

    @property
    def activity(self) -> dict[str, Any] | None:
        if not self.activity_json:
            return None
        try:
            return json.loads(self.activity_json)
        except Exception:
            logger.warning("Malformed activity_json on run %s", self.id)
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


class AdminNotificationState(Base):
    """Up to when an admin has read the finished-run notifications.

    The notifications themselves are not stored: ``agents/notifications.py``
    derives them from ``job_runs`` and ``workflow_runs``. One row per admin,
    keyed by the principal of the validated token; ``seen_at`` only moves
    forward (a conditional UPDATE there).
    """

    __tablename__ = "admin_notification_state"

    principal: Mapped[str] = mapped_column(String(255), primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


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
    import agents.ide.models  # noqa: F401  (registers the IDE tables on Base)

    if engine.dialect.name == "hana":
        await _deploy_hana_schema()
    else:
        await _create_and_migrate()

    async with SessionLocal() as session:
        existing = await session.get(OrchestratorConfig, 1)
        if existing is None:
            session.add(
                OrchestratorConfig(id=1, instructions=DEFAULT_ORCHESTRATOR_INSTRUCTIONS)
            )
            await session.commit()


async def _deploy_hana_schema() -> None:
    """The schema step of :func:`init_db` on SAP HANA.

    The runtime user cannot run DDL in an HDI container, so neither
    ``create_all`` nor the ``_ensure_*`` chain of :func:`_create_and_migrate`
    can run. The tables are generated from the same models as design-time
    artifacts and deployed by the design-time user (agents/hana_hdi.py); HDI
    then migrates each table from the difference to what it deployed last
    time, which is what the ``_ensure_column`` calls do by hand elsewhere.

    The data steps of the other path are not run: both repair rows written
    by app versions that never ran on HANA (proposals from before file
    revisions, and proposals edited by 2.18.0 after a rollback), and
    ``_resync_ide_revisions`` compares two ``Text`` columns in SQL, which
    HANA cannot do.
    """
    if _hana_hdi is None:
        raise DatabaseConfigError(
            "SAP HANA needs the design-time user of an HDI container to create "
            "the tables: bind a service of plan hdi-shared (binding fields "
            "hdi_user / hdi_password), or set HANA_HDI_USER and HANA_HDI_PASSWORD "
            "next to a hana DATABASE_URL that names currentSchema"
        )
    await hana_hdi.deploy(_hana_hdi, Base.metadata)


async def _create_and_migrate() -> None:
    """The schema step of :func:`init_db` on Postgres and SQLite."""
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
        # --- step kinds ---
        await _ensure_column(
            conn, "workflow_steps", "kind", "VARCHAR(16) NOT NULL DEFAULT 'agent'"
        )
        await _ensure_column(conn, "workflow_steps", "config_json", "TEXT")
        await _ensure_column(
            conn, "orchestrator_config", "model_name", "VARCHAR(128)"
        )
        # --- deep agents ---
        await _ensure_column(conn, "agent_configs", "deep_json", "TEXT")
        await _ensure_column(conn, "job_runs", "activity_json", "TEXT")
        # --- IDE phase 1c: diagnose sessions ---
        await _ensure_column(
            conn, "ide_sessions", "session_type", "VARCHAR(16) NOT NULL DEFAULT 'change'"
        )
        await _ensure_column(
            conn, "ide_conventions", "non_production", "BOOLEAN NOT NULL DEFAULT FALSE"
        )
        await _ensure_column(conn, "ide_findings", "detail", "TEXT")
        # --- IDE redesign: file revisions, comments, pins, base check ---
        # Additive only. ide_file_revisions / ide_comments (and the
        # ix_ide_comments_session_state index) are new tables, so create_all
        # above creates them; existing rows get revision 0 and NULLs.
        tstz = (
            "TIMESTAMP WITH TIME ZONE"
            if conn.dialect.name == "postgresql"
            else "TIMESTAMP"
        )
        await _ensure_column(conn, "ide_sessions", "pins_json", "TEXT")
        await _ensure_column(conn, "ide_artifacts", "based_on_json", "TEXT")
        await _ensure_column(
            conn, "ide_workspace_files", "revision", "INTEGER NOT NULL DEFAULT 0"
        )
        await _ensure_column(
            conn, "ide_workspace_files", "origin_version", "VARCHAR(255)"
        )
        await _ensure_column(conn, "ide_workspace_files", "base_status", "VARCHAR(8)")
        await _ensure_column(conn, "ide_workspace_files", "base_checked_at", tstz)
        # ide_comments already exists where the redesign ran before quotes.
        await _ensure_column(conn, "ide_comments", "quote", "VARCHAR(200)")
        await _backfill_ide_revisions(conn)
        await _resync_ide_revisions(conn)
        # create_all only creates indexes together with a new table; an
        # existing deployment needs them added here.
        await _ensure_index(conn, "uq_agent_configs_api_slug", "agent_configs", "api_slug")
        await _ensure_index(conn, "uq_workflows_api_slug", "workflows", "api_slug")
        await _ensure_unique_index(
            conn, "uq_ide_artifacts_version", "ide_artifacts",
            ("session_id", "kind", "version"),
        )
        await _ensure_plain_index(conn, "ix_job_runs_finished_at", "job_runs", "finished_at")
        await _ensure_plain_index(
            conn, "ix_workflow_runs_finished_at", "workflow_runs", "finished_at"
        )


async def _backfill_ide_revisions(conn) -> None:
    """Give every pre-redesign proposal its revision 1, once.

    A workspace file saved before revisions existed holds a proposal with
    ``revision = 0``: nothing could comment on, pin or approve it. Each such
    row gets ``IdeFileRevision(1, its proposal, run_id NULL)``. The claim is
    a conditional UPDATE (``revision = 0`` -> 1) and the revision is
    inserted only by the transaction whose UPDATE matched, so two instances
    starting together cannot both insert; an already existing revision row
    is kept. A second start finds no ``revision = 0`` proposal: a no-op.
    """
    from agents.ide.models import IdeFileRevision, IdeWorkspaceFile

    files = IdeWorkspaceFile.__table__
    revisions = IdeFileRevision.__table__
    pending = (
        await conn.execute(
            select(files.c.id, files.c.session_id, files.c.path).where(
                files.c.proposed_source.is_not(None), files.c.revision == 0
            )
            # One order for every instance: two starting together then lock
            # the rows they both claim in the same sequence (no deadlock).
            .order_by(files.c.id)
        )
    ).all()
    for fid, sid, path in pending:
        claimed = await conn.execute(
            update(files)
            .where(files.c.id == fid, files.c.revision == 0)
            .values(revision=1)
        )
        if claimed.rowcount != 1:
            continue  # another instance did it
        exists = (
            await conn.execute(
                select(revisions.c.id).where(
                    revisions.c.session_id == sid,
                    revisions.c.path == path,
                    revisions.c.revision == 1,
                )
            )
        ).first()
        if exists is not None:
            continue
        await conn.execute(
            insert(revisions).from_select(
                ["id", "session_id", "path", "revision", "proposed_source",
                 "created_at"],
                select(
                    literal(str(uuid.uuid4())),
                    files.c.session_id,
                    files.c.path,
                    literal(1),
                    files.c.proposed_source,
                    literal(datetime.now(timezone.utc), DateTime(timezone=True)),
                ).where(files.c.id == fid),
            )
        )


async def _resync_ide_revisions(conn) -> None:
    """Re-align proposals that 2.18.0 code edited after a rollback.

    2.18.0 rewrites ``proposed_source`` (and creates or deletes file rows)
    without knowing ``revision`` or ``ide_file_revisions``. After rolling
    forward, a file's ``revision`` can then name a revision whose text is
    not the proposal, or lie below revisions that exist for its path (a file
    deleted and written again). Comments, pins and approve would point at
    the wrong text, and the next save (``revision + 1``) would collide.

    Runs after :func:`_backfill_ide_revisions` (no proposal is at revision 0
    any more). For every proposal whose revision row is missing, holds other
    text, or is not the path's latest:

    * the path's latest revision holds the proposal -> the file points at it;
    * otherwise a NEW revision ``max(latest, revision) + 1`` with the
      proposal's text is added (``run_id`` NULL, like the backfill).

    Additive: no revision row is changed or removed. The file update is
    conditional on the revision that was read, and the insert copies the
    text from the row inside the same transaction, so two instances starting
    together cannot both add one. A consistent database is a no-op.
    """
    from agents.ide.models import IdeFileRevision, IdeWorkspaceFile

    files = IdeWorkspaceFile.__table__
    revisions = IdeFileRevision.__table__
    same_path = (revisions.c.session_id == files.c.session_id) & (
        revisions.c.path == files.c.path
    )
    matching = exists().where(
        same_path,
        revisions.c.revision == files.c.revision,
        revisions.c.proposed_source == files.c.proposed_source,
    )
    newer = exists().where(same_path, revisions.c.revision > files.c.revision)
    pending = (
        await conn.execute(
            select(files.c.id, files.c.session_id, files.c.path, files.c.revision)
            .where(files.c.proposed_source.is_not(None), ~matching | newer)
            .order_by(files.c.id)
        )
    ).all()
    for fid, sid, path, seen in pending:
        latest = (
            await conn.execute(
                select(revisions.c.revision, revisions.c.proposed_source)
                .where(revisions.c.session_id == sid, revisions.c.path == path)
                .order_by(revisions.c.revision.desc())
                .limit(1)
            )
        ).first()
        proposal = (
            await conn.execute(
                select(files.c.proposed_source).where(files.c.id == fid)
            )
        ).scalar_one_or_none()
        if proposal is None:
            continue
        if latest is not None and latest[1] == proposal:
            await conn.execute(
                update(files)
                .where(files.c.id == fid, files.c.revision == seen)
                .values(revision=latest[0])
            )
            continue
        number = max(latest[0] if latest is not None else 0, seen or 0) + 1
        claimed = await conn.execute(
            update(files)
            .where(files.c.id == fid, files.c.revision == seen)
            .values(revision=number)
        )
        if claimed.rowcount != 1:
            continue  # another instance did it
        await conn.execute(
            insert(revisions).from_select(
                ["id", "session_id", "path", "revision", "proposed_source",
                 "created_at"],
                select(
                    literal(str(uuid.uuid4())),
                    files.c.session_id,
                    files.c.path,
                    literal(number),
                    files.c.proposed_source,
                    literal(datetime.now(timezone.utc), DateTime(timezone=True)),
                ).where(files.c.id == fid),
            )
        )
        logger.warning(
            "IDE file %s: proposal changed outside revisions; added revision %d",
            fid, number,
        )


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


async def _ensure_index(conn, name: str, table: str, column: str) -> None:
    """Idempotently add the partial unique index on a nullable slug column.

    ``CREATE UNIQUE INDEX IF NOT EXISTS ... WHERE col IS NOT NULL`` is the
    same statement on SQLite and Postgres. A pre-existing duplicate makes the
    statement fail; that is logged rather than raised, because refusing to
    start would take the whole app down over two rows an operator can fix in
    the admin UI (get_agent_by_slug tolerates the duplicate meanwhile). On
    Postgres the attempt runs in a SAVEPOINT, as in
    :func:`_ensure_unique_index`: a failed statement would otherwise abort
    the whole migration transaction.
    """
    ddl = text(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} ({column}) "
        f"WHERE {column} IS NOT NULL"
    )
    try:
        if conn.dialect.name == "postgresql":
            async with conn.begin_nested():
                await conn.execute(ddl)
        else:
            await conn.execute(ddl)
    except Exception:
        logger.warning(
            "Could not create index %s on %s(%s); duplicate slugs may exist. "
            "Fix them in the admin UI and restart.",
            name, table, column, exc_info=True,
        )


async def _ensure_plain_index(conn, name: str, table: str, column: str) -> None:
    """Idempotently add a non-unique index on one column.

    For an index a model gained after its table shipped (``index=True``
    names it ``ix_<table>_<column>``; keep the name given here equal to
    that, or a new database and a migrated one end up with two indexes).
    The index only makes reads faster, so a failure is logged as a WARNING,
    never raised. On Postgres the attempt runs in a SAVEPOINT, as in
    :func:`_ensure_unique_index`. Not ``CONCURRENTLY`` (that cannot run in
    this transaction): on Postgres the first start after the upgrade blocks
    writes to the table while the index is built.
    """
    ddl = text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({column})")
    try:
        if conn.dialect.name == "postgresql":
            async with conn.begin_nested():
                await conn.execute(ddl)
        else:
            await conn.execute(ddl)
    except Exception:
        logger.warning(
            "Could not create index %s on %s(%s); reads on it stay unindexed.",
            name, table, column, exc_info=True,
        )


async def _ensure_unique_index(
    conn, name: str, table: str, columns: tuple[str, ...]
) -> None:
    """Idempotently add a unique index over ``columns``.

    A database written by an older version may hold duplicates; then the
    index cannot be built. That is logged as a WARNING, never raised: the
    app still starts, and the writer's retry keeps new rows unique. On
    Postgres the attempt runs in a SAVEPOINT, because a failed statement
    would otherwise abort the whole migration transaction.
    """
    ddl = text(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} ({', '.join(columns)})"
    )
    try:
        if conn.dialect.name == "postgresql":
            async with conn.begin_nested():
                await conn.execute(ddl)
        else:
            await conn.execute(ddl)
    except Exception:
        logger.warning(
            "Could not create unique index %s on %s(%s); duplicate rows may "
            "exist. New rows stay unique; remove the duplicates and restart "
            "to add the index.",
            name, table, ", ".join(columns), exc_info=True,
        )


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
    # first(), not scalar_one_or_none(): a database from before the partial
    # unique index may still hold two rows for one slug, and the scheduler
    # endpoint must not 500 on that. The lowest id wins, deterministically.
    result = await session.execute(
        select(AgentConfig).where(AgentConfig.api_slug == slug).order_by(AgentConfig.id)
    )
    rows = list(result.scalars().all())
    if len(rows) > 1:
        logger.warning(
            "api_slug %r is used by %d agents (%s); using %r. Give the others "
            "a different slug.",
            slug, len(rows), ", ".join(r.name for r in rows), rows[0].name,
        )
    return rows[0] if rows else None


# What POST /api/agents/{api_slug}/run accepts as a path segment. Lower-case
# so a slug is typed the same way it is stored; no `/`, so it cannot be a
# segment nobody can route to.
API_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def validate_api_slug(slug: str | None) -> str | None:
    """Return the trimmed slug (None when blank); ValueError when malformed."""
    text_ = (slug or "").strip()
    if not text_:
        return None
    if not API_SLUG_RE.match(text_):
        raise ValueError(
            "api_slug is invalid: use lower-case letters, digits and "
            "hyphens, starting with a letter or digit (max 64 characters)"
        )
    return text_


# `delegate_` plus the slug has to fit OpenAI/Azure's 64-character function
# name limit; agent names may be 64 characters themselves.
_DELEGATION_TOOL_PREFIX = "delegate_"
_DELEGATION_SLUG_MAX = 64 - len(_DELEGATION_TOOL_PREFIX)
_TOOL_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")


def delegation_tool_name(name: str) -> str:
    """The delegation tool an agent of this name registers on its callers.

    Lives here rather than in the registry because the save path has to know
    it: two enabled agents whose names sanitize to the same tool ("Foo Bar"
    and "foo_bar") make pydantic-ai refuse the duplicate tool at build time,
    which freezes the registry on the last good build. Truncation is
    deterministic so the collision check sees exactly the name the build
    will use.
    """
    slug = _TOOL_NAME_RE.sub("_", name.strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    slug = slug[:_DELEGATION_SLUG_MAX].rstrip("_")
    return f"{_DELEGATION_TOOL_PREFIX}{slug}" if slug else f"{_DELEGATION_TOOL_PREFIX}agent"


async def check_delegation_name_collision(
    session: AsyncSession,
    name: str,
    *,
    exclude_id: int | None = None,
    ignore_names: set[str] | None = None,
) -> None:
    """ValueError when another enabled agent maps to the same delegation tool.

    ``exclude_id`` is the row being saved; ``ignore_names`` are agents about to
    be removed in the same transaction (a replace import), which cannot
    collide with anything once it commits.
    """
    tool = delegation_tool_name(name)
    for r in await list_agents(session):
        if r.id == exclude_id or not r.enabled or r.name == name:
            continue
        if ignore_names and r.name in ignore_names:
            continue
        if delegation_tool_name(r.name) == tool:
            raise ValueError(
                f"name {name!r} would register the same delegation tool "
                f"({tool}) as the enabled agent {r.name!r}; pick a name that "
                "differs in more than case, spacing or punctuation"
            )


_OAUTH_KEYS = ("client_id", "client_secret", "uaa_url", "authorize_url", "token_url", "scope")
# client_credentials has no browser leg, so no authorize_url. It gains a target
# (`mailbox`) because an app-only token names no user, `recipients` because mail
# the agent originates has no incoming message to reply to and so no audience of
# its own, and `allow_send`, which is deliberately separate from the token's
# permissions: holding Mail.Send must not be enough to give an agent a send tool.
_CC_KEYS = ("client_id", "client_secret", "uaa_url", "token_url", "scope", "mailbox",
            "lookback", "recipients", "team", "channels")
# builtin:teams on oauth2 keeps its pinned scope and window next to the client
# credentials. Scoped to that one URL so every other oauth2 server stores
# exactly what it stored before.
_TEAMS_OAUTH2_KEYS = ("team", "channels", "lookback")
# builtin:outlook under oauth2 (as the signed-in user) carries the same send
# settings as its app_only shape: `recipients` pins the audience of mail the
# agent originates and `lookback` bounds the listing window. outlook_tools
# reads them from the oauth block whatever the mode, so dropping them here
# silently took the send tool away from every oauth2 Outlook agent.
_OUTLOOK_OAUTH2_KEYS = ("lookback", "recipients")


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
_DEST_KEYS = ("destination", "project", "status", "lookback", "api_base", "labels")
# builtin:slack on a destination keeps a channel pin instead of Jira's filters,
# and a posting switch. Scoped to that URL so a Jira server stores exactly
# what it stored before.
_SLACK_DEST_KEYS = ("destination", "channels", "lookback")


# A public built-in stores no credential either -- NVD needs none. These are
# filtering knobs only, and the whitelist is what keeps this from becoming a
# general-purpose place to stash settings on any 'none' server. Public rather
# than private because `agents.admin` refuses the same set at the payload
# boundary, and the two lists drifting apart would mean an admin could save a
# key that is then silently dropped on the way to storage.
BUILTIN_PUBLIC_KEYS = ("min_score", "lookback")


def _clean_builtin_public(oauth: Any) -> dict[str, Any] | None:
    """Normalize the config block of a built-in on ``auth_mode='none'``.

    Returns None when nothing survives, so an unconfigured server stores no
    block at all rather than an empty dict.
    """
    src = oauth if isinstance(oauth, dict) else {}
    cleaned: dict[str, Any] = {}
    for k in BUILTIN_PUBLIC_KEYS:
        v = src.get(k)
        if v is not None and str(v).strip() != "":
            cleaned[k] = str(v).strip()
    return cleaned or None


def _clean_destination(oauth: Any, url: str | None = None) -> dict[str, Any]:
    """Normalize a ``destination`` oauth block for storage.

    Credential keys are dropped rather than rejected here: an admin editing a
    server that used to be oauth2 will still be POSTing a client_id, and
    silently not storing it is what keeps this mode's promise that nothing
    secret lands in the database. The payload validator refuses them earlier,
    with a message explaining why.
    """
    src = oauth if isinstance(oauth, dict) else {}
    cleaned: dict[str, Any] = {}
    builtin = str(url or "").strip().rstrip("/").lower()
    if builtin == BUILTIN_ODATA_URL:
        # The one destination-mode block without a destination name.
        return _clean_odata_entry(src)
    if builtin in _DEST_KEYS_BY_URL:
        # The built-ins that gained destination mode later keep their own
        # pinned keys plus the user-context switch; see `# --- destinations ---`.
        return _clean_builtin_destination(src, builtin)
    if builtin and not builtin.startswith("builtin:"):
        # A remote MCP server through a destination: the destination holds
        # URL and credential, so only its name and the user-context switch
        # are stored. ``is True``: the string "false" must not decide whose
        # token a request carries.
        name = str(src.get("destination") or "").strip()
        if not name:
            raise ValueError("destination server requires a destination name")
        remote: dict[str, Any] = {"destination": name}
        if src.get("user_context") is True:
            remote["user_context"] = True
        return remote
    slack = builtin == "builtin:slack"
    for k in _SLACK_DEST_KEYS if slack else _DEST_KEYS:
        v = src.get(k)
        if isinstance(v, (list, tuple)):
            # `status` and `labels` are multi-value and stored comma-separated,
            # which is also what both admin UIs post. A JSON list is a
            # legitimate thing for an API client or an imported bundle to send,
            # and str() on it would store "['a', 'b']" -- which then parses back
            # as the two values "['a'" and "'b']" and quietly matches nothing.
            v = ", ".join(str(x).strip() for x in v if str(x).strip())
        if v is not None and str(v).strip() != "":
            cleaned[k] = str(v).strip()
    if not cleaned.get("destination"):
        raise ValueError("destination server requires a destination name")
    if slack:
        cleaned["allow_send"] = src.get("allow_send") is True
    else:
        cleaned["allow_comment"] = bool(src.get("allow_comment"))
    return cleaned


# Built-ins that originate report mail and so keep a `theme` block (see
# agents/mail_render.MailTheme) next to their other keys, whatever the mode.
_MAIL_THEME_URLS = frozenset({"builtin:smtp", "builtin:outlook"})


# builtin:sharepoint: the one workbook (`site`, `library`, `path`) and the
# `views` of it an agent may name, next to the destination name or the client
# credentials. Nothing else is stored for it, whatever the block carried.
_SHAREPOINT_URL = "builtin:sharepoint"
_SHAREPOINT_PIN_KEYS = ("site", "library", "path")
_SHAREPOINT_CREDENTIAL_KEYS = ("client_id", "client_secret", "uaa_url", "token_url", "scope")


def _clean_sharepoint_entry(
    oauth: Any, mode: str, fallback: dict[str, Any] | None
) -> dict[str, Any]:
    """Normalize the block of a ``builtin:sharepoint`` entry for storage.

    The last gate before the row, whoever the caller is: the pins and the
    views are checked here again (``agents.sharepoint_views``, the same
    reading the admin gate and the toolset use), unknown view keys are a
    ValueError rather than dropped, and only this entry's own keys are kept
    -- no ``user_context`` (there is no per-user mode; a true one is
    refused), no ``allow_send`` (there is nothing to send), no key of
    another built-in.

    **What is stored is what was checked.** The three pins are taken from
    the block as they are, never through the generic cleaners (which trim
    and ``str()`` every value): ``check_pins`` refuses a pin that is not in
    its exact form and returns nothing, so a pin repaired here would be
    stored in a form no gate has seen, and a number or a list would become
    its ``str()``. The check runs on the very dict that is returned. The
    views are stored as ``clean_views`` returns them, never the posted ones.
    """
    from agents.sharepoint_views import check_pins, clean_views

    src = oauth if isinstance(oauth, dict) else {}
    if mode not in (AUTH_MODE_DESTINATION, AUTH_MODE_APP_ONLY):
        raise ValueError(f"{_SHAREPOINT_URL} requires auth_mode=destination or app_only")
    if src.get("user_context") is True:
        # Refused, not dropped: a script or a direct `upsert_agent` reaches
        # this without the admin payload, and an entry that asked for "as
        # the signed-in user" must not become an application entry without
        # a word. `is True` as `user_context_of` reads it; anything else is
        # "not set" and is not stored.
        raise ValueError(
            f"oauth.user_context: {_SHAREPOINT_URL} has no signed-in user to act "
            "as; it reads the pinned workbook as the application"
        )
    cleaned: dict[str, Any] = {}
    if mode == AUTH_MODE_DESTINATION:
        # The name is no pin: it gets the rule every destination entry has
        # (a non-empty string, stored trimmed). Credential keys are dropped.
        name = src.get("destination")
        name = name.strip() if isinstance(name, str) else ""
        if not name:
            raise ValueError("destination server requires a destination name")
        cleaned["destination"] = name
    else:
        # The existing client-credentials rules (required keys, the stored
        # secret kept when the edit sends a blank one); of what they return
        # only the credential itself is kept: no mailbox, no `allow_send`.
        credential = _clean_client_credentials(src, fallback)
        cleaned.update(
            (k, credential[k]) for k in _SHAREPOINT_CREDENTIAL_KEYS if k in credential
        )
    cleaned.update((k, src.get(k)) for k in _SHAREPOINT_PIN_KEYS)
    check_pins(cleaned)
    cleaned["views"] = clean_views(src.get("views"))
    return cleaned


def _clean_oauth(
    oauth: Any, mode: str, fallback: dict[str, Any] | None, url: str | None = None
) -> dict[str, Any] | None:
    """`_clean_oauth_block`, plus the mail ``theme`` for the mail built-ins.

    The theme is validated and stored as only the keys that differ from the
    default; an empty or all-default theme stores nothing. A bad theme is a
    ValueError, like every other malformed block.
    """
    builtin = str(url or "").strip().rstrip("/").lower()
    if builtin == _SHAREPOINT_URL:
        # Its own cleaner, and none of the generic ones below: they trim and
        # stringify what they keep, and a pin is stored only as it was checked.
        return _clean_sharepoint_entry(oauth, mode, fallback)
    cleaned = _clean_oauth_block(oauth, mode, fallback, url=url)
    if cleaned is not None and builtin in _MAIL_THEME_URLS and isinstance(oauth, dict):
        from agents.mail_render import MailTheme

        theme = MailTheme.from_config(oauth.get("theme")).to_config()
        if theme:
            cleaned["theme"] = theme
    return cleaned


def _clean_oauth_block(
    oauth: Any, mode: str, fallback: dict[str, Any] | None, url: str | None = None
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

    A built-in on ``none`` takes a fourth shape: ``{min_score?, lookback?}``.
    Public data source, no credential, filtering knobs only.

    Returns None for modes that carry no oauth block.
    """
    if mode == AUTH_MODE_NONE:
        from agents.builtins import is_builtin_url

        return _clean_builtin_public(oauth) if is_builtin_url(url) else None
    if mode == AUTH_MODE_DESTINATION:
        return _clean_destination(oauth, url)
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
    builtin = str(url or "").strip().rstrip("/").lower()
    extra_keys = {
        "builtin:teams": _TEAMS_OAUTH2_KEYS,
        "builtin:outlook": _OUTLOOK_OAUTH2_KEYS,
    }.get(builtin)
    if extra_keys:
        for k in extra_keys:
            v = src.get(k)
            if isinstance(v, (list, tuple)):
                v = ", ".join(str(x).strip() for x in v if str(x).strip())
            if v is not None and str(v).strip() != "":
                cleaned[k] = str(v).strip()
        cleaned["allow_send"] = bool(src.get("allow_send"))
    elif builtin == "builtin:gmail":
        # Gmail replies to the thread it was given, so it pins no audience;
        # the switch is all it keeps. Only a real true opens the send tool.
        cleaned["allow_send"] = src.get("allow_send") is True
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
    odata_seen = False
    for s in mcp_servers:
        url = (s.get("url") or "").strip()
        mode = (s.get("auth_mode") or AUTH_MODE_JWT).strip().lower()
        if not url:
            raise ValueError("MCP server url is required")
        if mode not in VALID_AUTH_MODES:
            raise ValueError(f"invalid auth_mode {mode!r}")
        if url.rstrip("/").lower() == BUILTIN_ODATA_URL:
            # Stored under exactly this spelling. `odata_entries` (who uses a
            # service) forgives case and a trailing slash, the registry's
            # built-in lookup does not: any other spelling would be an entry
            # that blocks the delete of a service it can never call.
            url = BUILTIN_ODATA_URL
            if mode != AUTH_MODE_DESTINATION:
                raise ValueError(_ODATA_MODE_MESSAGE)
            if odata_seen:
                raise ValueError(ODATA_SINGLE_ENTRY_MESSAGE)
            odata_seen = True
        elif url.rstrip("/").lower() == _SHAREPOINT_URL:
            # Stored under exactly this spelling, for the reason above:
            # `_clean_oauth` forgives case and a trailing slash, the
            # registry's built-in lookup forgives no slash, and an entry it
            # does not know as a built-in is built as a remote MCP server
            # with this entry's destination. Before the lookup of the
            # stored secret below, which is by URL.
            url = _SHAREPOINT_URL
        oauth = _clean_oauth(s.get("oauth"), mode, prev_oauth_by_url.get(url), url=url)
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


def prepared_server_list(
    primary: dict[str, Any], extras: list[dict[str, Any]], primary_oauth_json: str | None
) -> list[dict[str, Any]]:
    """`prepare_servers` output as one server list, primary first: the list
    the row will hold (``AgentConfig.mcp_servers`` after the write).

    For checks that must judge what is stored rather than what was sent
    (`check_odata_services`): a second reading of the raw input would be a
    second normaliser of URL spelling and block shape, free to drift from
    the one that decides what is written.
    """
    first = dict(primary)
    if primary_oauth_json:
        first["oauth"] = json.loads(primary_oauth_json)
    return [first, *extras]


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
    commit: bool = True,
    ignore_collisions_with: set[str] | None = None,
    # --- deep agents --- JSON from agents.deep.dump_deep_config; None clears,
    # KEEP (not sent) preserves what is stored, like model_name/peers.
    deep_json: str | None | _Keep = KEEP,
) -> AgentConfig:
    """Create or update the agent named ``name``.

    ``commit=False`` flushes instead, so a caller writing a whole bundle can
    keep every row in one transaction and commit (or roll back) once.
    ``ignore_collisions_with`` names agents that same caller is about to
    delete, so they do not count as delegation-tool collisions.
    """
    name = name.strip()
    existing = await get_agent_by_name(session, name)
    primary, extras, primary_oauth_json = prepare_servers(mcp_servers, existing)
    # Here rather than in the admin routes: scripts and seeds write agents
    # through this function too, and none of them may store a service name
    # the catalogue does not have. On the prepared list, i.e. on exactly the
    # entries the row is about to hold.
    await check_odata_services(
        session, prepared_server_list(primary, extras, primary_oauth_json)
    )
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

    slug = validate_api_slug(api_slug)
    if slug:
        clash = await get_agent_by_slug(session, slug)
        if clash is not None and (existing is None or clash.id != existing.id):
            raise ValueError(f"api_slug {slug!r} is already used by agent {clash.name!r}")
    if expose_api and not slug:
        raise ValueError("expose_api requires an api_slug")
    if enabled:
        await check_delegation_name_collision(
            session, name,
            exclude_id=existing.id if existing is not None else None,
            ignore_names=ignore_collisions_with,
        )

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
        # --- deep agents ---
        row.deep_json = None if isinstance(deep_json, _Keep) else deep_json
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
        # --- deep agents ---
        if not isinstance(deep_json, _Keep):
            existing.deep_json = deep_json
        row = existing
    if commit:
        await session.commit()
    else:
        await session.flush()
    await session.refresh(row)
    return row


async def delete_agent(session: AsyncSession, agent_id: int) -> bool:
    row = await session.get(AgentConfig, agent_id)
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True


async def agent_referrers(
    session: AsyncSession,
    name: str,
    *,
    exclude_agent_names: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Who depends on the agent called ``name``.

    Returns ``(peer_agents, workflows)``: the enabled agents listing it as a
    peer, and the enabled workflows with a step that names it. Both are what
    breaks when the agent is renamed, disabled or deleted: the registry drops
    the peer tool with only a log line, and the workflow fails its preflight
    at trigger time. ``exclude_agent_names`` are agents leaving in the same
    transaction (a replace import), whose peer lists no longer matter.
    """
    peers: list[str] = []
    for r in await list_agents(session):
        if r.name == name or not r.enabled:
            continue
        if exclude_agent_names and r.name in exclude_agent_names:
            continue
        if name in r.peers:
            peers.append(r.name)
    result = await session.execute(
        select(Workflow.name)
        .join(WorkflowStep, WorkflowStep.workflow_id == Workflow.id)
        # --- step kinds --- only agent steps name an agent.
        .where(WorkflowStep.agent_name == name, WorkflowStep.kind == "agent",
               Workflow.enabled == 1)
        .distinct()
        .order_by(Workflow.name)
    )
    workflows = [n for (n,) in result.all()]
    return peers, workflows


def describe_referrers(peers: list[str], workflows: list[str]) -> str:
    parts = []
    if peers:
        parts.append("peer of agent(s) " + ", ".join(repr(p) for p in peers))
    if workflows:
        parts.append("named by a step of workflow(s) " + ", ".join(repr(w) for w in workflows))
    return "; ".join(parts)


async def rename_agent_references(
    session: AsyncSession, old_name: str, new_name: str | None
) -> None:
    """Update every peer list and workflow step after an agent rename.

    ``new_name=None`` strips the agent from peer lists instead (the forced
    delete); workflow steps are left alone in that case, because a step with
    no agent is a workflow that cannot run, and the caller refuses the delete
    while such a step exists. Caller commits, like rename_skill_references.
    """
    result = await session.execute(
        select(AgentConfig).where(AgentConfig.peers_json.is_not(None))
    )
    for agent in result.scalars().all():
        peers = agent.peers
        if old_name not in peers:
            continue
        mapped = [new_name if p == old_name else p for p in peers]
        deduped = list(dict.fromkeys(p for p in mapped if p and p != agent.name))
        agent.peers_json = json.dumps(deduped) if deduped else None
    if new_name is not None:
        steps = await session.execute(
            select(WorkflowStep).where(WorkflowStep.agent_name == old_name)
        )
        for step in steps.scalars().all():
            step.agent_name = new_name


# --- where used ---
async def agent_where_used(
    session: AsyncSession, agent_id: int
) -> dict[str, Any] | None:
    """Everything that refers to one agent, for display rather than for a guard.

    The sibling of ``agent_referrers`` with the opposite audience: that one
    answers "may this be removed?" and so deliberately sees only *enabled*
    referrers, because a disabled peer or workflow breaks nothing. An
    operator reading a where-used list wants the disabled ones too -- a
    workflow switched off last week still names this agent and will need it
    the day it is switched back on -- so each entry carries its ``enabled``
    flag instead of being filtered on it. Returns ``None`` for an unknown id.

    Workflow steps are matched on ``agent_name`` only; a step's ``steps``
    list is ordered main line first, then branch by key, each by position.
    """
    row = await session.get(AgentConfig, agent_id)
    if row is None:
        return None
    peers = [
        {"id": r.id, "name": r.name, "enabled": bool(r.enabled)}
        for r in await list_agents(session)
        if r.id != row.id and row.name in r.peers
    ]
    result = await session.execute(
        select(Workflow, WorkflowStep.position, WorkflowStep.branch_key)
        .join(WorkflowStep, WorkflowStep.workflow_id == Workflow.id)
        # --- step kinds --- only agent steps name an agent; a deterministic
        # step's agent_name is "" and must not count as a use.
        .where(WorkflowStep.agent_name == row.name, WorkflowStep.kind == "agent")
        .order_by(Workflow.name, Workflow.id)
    )
    by_id: dict[int, dict[str, Any]] = {}
    for wf, position, branch_key in result.all():
        entry = by_id.setdefault(wf.id, {
            "id": wf.id,
            "name": wf.name,
            "api_slug": wf.api_slug,
            "enabled": bool(wf.enabled),
            "steps": [],
        })
        entry["steps"].append({"position": position, "branch_key": branch_key})
    workflows = list(by_id.values())
    for entry in workflows:
        entry["steps"].sort(
            key=lambda s: (s["branch_key"] is not None, s["branch_key"] or "", s["position"])
        )
    return {
        "agent": {"id": row.id, "name": row.name},
        "peers": peers,
        "workflows": workflows,
    }


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
    session: AsyncSession, *, name: str, description: str, content: str,
    commit: bool = True,
) -> SkillConfig:
    row = await get_skill_by_name(session, name)
    if row is None:
        row = SkillConfig(name=name, description=description, content=content)
        session.add(row)
    else:
        row.description = description
        row.content = content
    if commit:
        await session.commit()
    else:
        await session.flush()
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



# --- OData services ---
def odata_service_columns(data: dict[str, Any]) -> dict[str, Any]:
    """Column values for a service from ``validate_odata_service`` output.

    The definition is stored as ``ServiceDefinition.model_dump_json()``, the
    exact text ``MAX_DEFINITION_BYTES`` was measured on, so a definition that
    passed the cap is never larger in the table than the gate allowed.

    Public, and pure, for a caller that writes many services in one request
    (the bundle import): building the JSON runs the definition models once
    more, which for a large definition is work to do off the event loop and
    then hand to `create_odata_service` / `update_odata_service` as
    ``columns``.
    """
    definition = _ODataServiceDefinition.model_validate(data.get("definition") or {})
    return {
        "title": data["title"],
        "purpose": data["purpose"],
        "not_for": data.get("not_for") or "",
        "destination": data["destination"],
        "user_context": 1 if data.get("user_context") is True else 0,
        "odata_version": data["odata_version"],
        "service_path": data["service_path"],
        "definition_json": definition.model_dump_json(),
        "metadata_fetched_at": _odata_utc(data.get("metadata_fetched_at")),
        "enabled": 0 if data.get("enabled") is False else 1,
    }


async def list_odata_services(
    session: AsyncSession, *, lock: bool = False
) -> list[ODataService]:
    """Every catalogue service, in name order.

    ``lock`` takes an exclusive row lock on each (Postgres; SQLite has none
    and serialises writers), held until the caller's transaction ends. For a
    writer of many services at once (the bundle import): an agent save that
    attaches one of them waits (`existing_odata_service_names`), exactly as
    it does for the delete route. The name order is the lock order, so two
    such writers cannot wait for each other.
    """
    result = await session.execute(_list_odata_services_query(lock=lock))
    return list(result.scalars().all())


def _list_odata_services_query(*, lock: bool) -> Any:
    """The statement of `list_odata_services`; a function of its own so a
    test can compile it for Postgres (``FOR UPDATE``)."""
    query = select(ODataService).order_by(ODataService.name)
    if lock:
        query = query.with_for_update()
    return query


async def get_odata_service(session: AsyncSession, name: str) -> ODataService | None:
    result = await session.execute(select(ODataService).where(ODataService.name == name))
    return result.scalar_one_or_none()


async def existing_odata_service_names(
    session: AsyncSession, names: list[str], *, lock: bool = False
) -> set[str]:
    """Which of ``names`` are catalogue services right now (enabled or not).

    ``lock`` takes a shared row lock on the services found, held until the
    caller's transaction ends: an agent write that attaches them cannot
    commit a name whose delete has already been issued (the select waits for
    that delete and then no longer finds the row). A delete issued later
    waits for the agent write; whether it then still goes through is decided
    by the delete's own referrer check. Postgres only; SQLite has no row
    locks, ignores the clause and serialises writers anyway.
    """
    if not names:
        return set()
    query = _existing_odata_names_query(names, lock=lock)
    return set((await session.execute(query)).scalars().all())


def _existing_odata_names_query(names: list[str], *, lock: bool) -> Any:
    """The statement of `existing_odata_service_names`; a function of its own
    so a test can compile it for Postgres (``FOR SHARE``), which no
    SQLite-backed suite would otherwise see."""
    query = select(ODataService.name).where(ODataService.name.in_(sorted(set(names))))
    if lock:
        query = query.with_for_update(read=True)
    return query


async def check_odata_services(session: AsyncSession, servers: list[dict[str, Any]]) -> None:
    """Refuse a server list that attaches a service the catalogue lacks.

    ``ValueError("unknown OData service 'x'")``, every missing name in the
    order listed. Called by every write of an agent's servers
    (`upsert_agent`, and the admin update route beside its own
    `prepare_servers`), in the session that then writes the row and with the
    found rows locked, so the answer holds until that write commits. Both
    hand over `prepared_server_list(...)`, the entries as they will be
    stored, so this check and storage cannot disagree on what counts as a
    ``builtin:odata`` entry.

    A dangling name is not harmless: the agent would be attached, with
    whatever ``allow_write`` its entry holds, to the next service somebody
    creates under that name, without anyone saving the agent again. A
    disabled service exists and may be attached: an admin prepares the agent
    first and switches the service on later; the run time leaves it out
    until then.

    A name is repeated in the message only when it has the form of a service
    name; anything else is "invalid service name", without the value.
    """
    names: list[str] = []
    for block in odata_entries(servers):
        services = block.get("services")
        for name in services if isinstance(services, list) else []:
            if not isinstance(name, str) or not re.fullmatch(_ODATA_SERVICE_NAME_RE, name):
                raise ValueError("invalid service name")
            if name not in names:
                names.append(name)
    if not names:
        return
    found = await existing_odata_service_names(session, names, lock=True)
    missing = [n for n in names if n not in found]
    if missing:
        raise ValueError("unknown OData service " + ", ".join(f"'{n}'" for n in missing))


async def _begin_before_savepoint(session: AsyncSession) -> None:
    """Make sure a real transaction is open before a SAVEPOINT on SQLite.

    The sqlite3 driver opens its transaction only at the first INSERT,
    UPDATE or DELETE. A SAVEPOINT issued before that is the outermost one,
    and releasing it COMMITS: the row of a caller with ``commit=False``
    would be stored at once and survive that caller's rollback (a refused
    import kept the services it had created first). An explicit BEGIN makes
    the savepoint a nested one, as it always is on Postgres, where this does
    nothing.
    """
    if session.get_bind().dialect.name != "sqlite":
        return
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    if not raw.driver_connection.in_transaction:
        await connection.exec_driver_sql("BEGIN")


async def begin_exclusive_write(session: AsyncSession) -> None:
    """Make this transaction a writer from its first statement, on SQLite.

    For a route that reads a row, decides on what it read and then writes
    it (the catalogue update: compare ``updated_at``, then replace). On
    Postgres the read itself takes the row lock (``FOR UPDATE``) and this
    does nothing. SQLite has no row locks and takes its write lock only at
    the first INSERT, UPDATE or DELETE, so two such requests would both read
    the old row and both write. ``BEGIN IMMEDIATE`` takes the write lock up
    front: the second request waits here and then reads what the first one
    committed. That wait is the sqlite3 driver's busy timeout (5 s by
    default), after which the request fails with "database is locked";
    SQLite is the local and test database only, so nothing here tunes it.

    Call it before the first statement of the session. In a transaction
    that is already open at the driver it can take no lock, and whatever
    that transaction read before is not protected: it logs a WARNING and
    does nothing, because that is a mistake in the caller.
    """
    if session.get_bind().dialect.name != "sqlite":
        return
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    if raw.driver_connection.in_transaction:
        logger.warning(
            "begin_exclusive_write: the transaction is already open, no write lock "
            "taken; call it before the session's first statement"
        )
        return
    await connection.exec_driver_sql("BEGIN IMMEDIATE")


def has_row_locks(session: AsyncSession) -> bool:
    """Whether ``FOR UPDATE`` / ``FOR SHARE`` lock a row on this database.

    True on Postgres and on SAP HANA. SQLite has no row locks: it drops the
    clause and serialises writers instead, so a caller that needs the lock
    there takes the write lock up front (:func:`begin_exclusive_write`).
    """
    return session.get_bind().dialect.name != "sqlite"


def compares_lobs(session: AsyncSession) -> bool:
    """Whether a ``Text`` column can stand in a comparison on this database.

    False on SAP HANA only: ``Text`` is ``NCLOB`` there, and HANA refuses
    ``=`` on a LOB, against a value as much as against another column (as it
    refuses ``ORDER BY``, ``GROUP BY`` and ``DISTINCT`` on one; ``IS NULL``
    and ``length()`` are fine). See :func:`text_unchanged`.
    """
    return session.get_bind().dialect.name != "hana"


class ComparedText(TypeDecorator):
    """The type of the value :func:`text_unchanged` compares a ``Text``
    column with, on the databases that can compare one.

    It is ``Text`` in every respect (same SQL, same bind handling); what it
    adds is a name. ``tests/hana_sql.py`` checks every statement the suite
    executes against what SAP HANA refuses, and a comparison on a ``Text``
    column is refused there -- except this one, which :func:`text_unchanged`
    never builds on HANA. The type is how that one comparison is told from
    a hand-written ``column == value``, which stays an error.
    """

    impl = Text
    cache_ok = True


async def text_unchanged(
    session: AsyncSession, column: Any, seen: str | None, *row: Any
) -> Any:
    """The WHERE term "``column`` still holds ``seen``" of a conditional UPDATE.

    For a read-modify-write of a ``Text`` column (the session pins, a seed
    text): the UPDATE applies only while the column holds what was read, so
    two writers cannot overwrite each other. ``row`` are the conditions that
    name the one row; the caller's UPDATE repeats them.

    Postgres and SQLite compare in the UPDATE itself: this returns
    ``column == seen`` and sends nothing. SAP HANA cannot compare an
    ``NCLOB``, so there the row is locked (``FOR UPDATE``), the column is
    read again and the comparison is made here; the answer is a constant
    true or false term. The lock lasts until the caller's transaction ends,
    so the UPDATE that follows writes the row that was compared: the same
    guarantee, bought with a lock instead of a predicate. A row that is gone
    compares as changed.
    """
    if seen is None:
        return column.is_(None)
    if compares_lobs(session):
        return column == literal(seen, ComparedText())
    current = (
        await session.execute(
            select(column)
            .where(*row)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    return true() if [tuple(r) for r in current] == [(seen,)] else false()


def _odata_now() -> datetime:
    """The clock `update_odata_service` stamps with; a function so a test
    can stop it."""
    return datetime.now(timezone.utc)


def _next_odata_stamp(previous: datetime | None) -> datetime:
    """The ``updated_at`` of an update: now, and always later than ``previous``.

    ``updated_at`` is the version a client hands back as
    ``expected_updated_at``, so two updates must never leave the same value.
    The column default cannot promise that: SQLite's ``CURRENT_TIMESTAMP``
    counts in seconds, and Postgres' ``now()`` is the start of the
    transaction, which for a writer that waited for the row lock lies
    *before* the value the winner stored. So the stamp is set here, from the
    row read under the lock: the clock when it is ahead, otherwise the
    stored value plus one microsecond (the resolution of both column types).
    """
    now = _odata_now()
    stored = _odata_utc(previous)
    if stored is None or now > stored:
        return now
    return stored + timedelta(microseconds=1)


async def create_odata_service(
    session: AsyncSession,
    data: dict[str, Any],
    *,
    commit: bool = True,
    columns: dict[str, Any] | None = None,
) -> ODataService:
    """Store a new catalogue service; ``data`` is `validate_odata_service` output.

    ``columns`` is `odata_service_columns(data)` when the caller already
    built it (see there); never anything else.

    A taken name is a ``ValueError``. The lookup gives the readable answer;
    the unique constraint closes the check-then-write race, and the insert
    runs in a SAVEPOINT so losing that race costs a caller with
    ``commit=False`` (an import) only this row, not its whole transaction.
    """
    name = data["name"]
    taken = f"Service name {name!r} already exists"
    if await get_odata_service(session, name) is not None:
        raise ValueError(taken)
    row = ODataService(name=name, **(columns or odata_service_columns(data)))
    await _begin_before_savepoint(session)
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        raise ValueError(taken) from None
    if commit:
        await session.commit()
    await session.refresh(row)
    return row


def odata_service_unchanged(row: ODataService, columns: dict[str, Any]) -> bool:
    """Whether writing ``columns`` (`odata_service_columns` output) to ``row``
    would change nothing.

    Stored form against stored form: the definition as the text the models
    serialise (what the row holds), the flags as 0/1, the metadata stamp as
    an instant (SQLite hands it back naive). For a bulk writer (the bundle
    import) that must not restamp, and so make stale for every open admin
    tab, a service it does not change. A row whose definition is unreadable
    never equals a validated one, so it is rewritten.
    """
    for column, value in columns.items():
        stored = getattr(row, column)
        if column == "metadata_fetched_at":
            stored, value = _odata_utc(stored), _odata_utc(value)
        if stored != value:
            return False
    return True


async def update_odata_service(
    session: AsyncSession,
    row: ODataService,
    data: dict[str, Any],
    *,
    commit: bool = True,
    columns: dict[str, Any] | None = None,
) -> ODataService:
    """Replace everything but the name with ``data`` (`validate_odata_service` output).

    ``columns`` as in `create_odata_service`.

    The name is refused rather than ignored: agents attach a service by
    name, so a silent rename would detach every one of them.

    ``updated_at`` moves forward on every call, also when no field changed
    (`_next_odata_stamp`): it is the version the admin API compares against
    ``expected_updated_at``. That is only race-free when ``row`` was read
    under a lock the caller still holds (the update route, the bundle
    import); the function itself takes none.
    """
    if data["name"] != row.name:
        raise ValueError("name cannot be changed; duplicate the service instead")
    for column, value in (columns or odata_service_columns(data)).items():
        setattr(row, column, value)
    row.updated_at = _next_odata_stamp(row.updated_at)
    if commit:
        await session.commit()
    else:
        await session.flush()
    await session.refresh(row)
    return row


async def delete_odata_service(
    session: AsyncSession, row: ODataService, *, commit: bool = True
) -> bool:
    """Delete the service. Whether agents still use it is the caller's
    question (`odata_service_referrers`); nothing is detached here."""
    await session.delete(row)
    if commit:
        await session.commit()
    else:
        await session.flush()
    return True


def odata_entries(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ``builtin:odata`` config blocks of a server list, in order.

    An entry without a block, or with one that is not an object, has no
    services and is left out.
    """
    blocks: list[dict[str, Any]] = []
    for server in servers or []:
        if not isinstance(server, dict):
            continue
        url = str(server.get("url") or "").strip().rstrip("/").lower()
        if url == BUILTIN_ODATA_URL and isinstance(server.get("oauth"), dict):
            blocks.append(server["oauth"])
    return blocks


async def odata_service_referrers(
    session: AsyncSession, name: str | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Which agents attach which catalogue service.

    ``{service name: [{agent_id, agent, enabled, expose_api, api_slug,
    allow_write}]}``, agents in name order, enabled and disabled alike: a
    disabled agent still breaks when its service is deleted and it is turned
    back on. One pass over the agents whatever the number of services;
    ``name`` narrows the answer to one service (absent when nobody uses it).
    An agent listing a service on two entries appears once, with
    ``allow_write`` if either entry allows it. Only a real ``true`` counts.
    """
    found: dict[str, dict[int, dict[str, Any]]] = {}
    for agent in await list_agents(session):
        for block in odata_entries(agent.mcp_servers):
            services = block.get("services")
            if not isinstance(services, (list, tuple)):
                continue
            allow_write = block.get("allow_write") is True
            for service in services:
                if not isinstance(service, str) or (name is not None and service != name):
                    continue
                entry = found.setdefault(service, {}).setdefault(agent.id, {
                    "agent_id": agent.id,
                    "agent": agent.name,
                    "enabled": bool(agent.enabled),
                    "expose_api": bool(agent.expose_api),
                    "api_slug": agent.api_slug,
                    "allow_write": False,
                })
                entry["allow_write"] = entry["allow_write"] or allow_write
    return {service: list(by_agent.values()) for service, by_agent in found.items()}


# The longest retention the purge computes with (100 years): a larger
# value would take the cutoff below year 1 and raise OverflowError.
ODATA_AUDIT_MAX_RETENTION_DAYS = 36_500
ODATA_AUDIT_OUTCOMES = ("intent", "ok", "refused", "sap_error", "unknown", "cancelled")


async def purge_odata_audit(session: AsyncSession, days: int) -> int:
    """Delete audit rows older than ``days``; returns how many.

    ``days < 1`` deletes nothing: 0 is how retention is switched off, and a
    misread setting must not empty the log. A value beyond
    ``ODATA_AUDIT_MAX_RETENTION_DAYS`` is treated as that bound. Rows are
    aged by ``created_at`` whatever their outcome: an ``intent`` row that
    never got a result is purged like any other.

    This function COMMITS the session it is given. Call it with a session
    of its own (``app._purge_ide_sessions`` does), never with one that
    carries a caller's open transaction.
    """
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        return 0
    days = min(days, ODATA_AUDIT_MAX_RETENTION_DAYS)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await session.execute(
        delete(ODataAuditLog)
        .where(ODataAuditLog.created_at < cutoff)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return int(result.rowcount or 0)


async def list_odata_audit(
    session: AsyncSession,
    *,
    service: str | None = None,
    sent_as: str | None = None,
    outcome: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    before_id: int | None = None,
    limit: int = 100,
) -> list[ODataAuditLog]:
    """Audit rows, newest first, at most ``limit``. Read-only.

    ``service``, ``sent_as`` and ``outcome`` are exact matches; ``since`` /
    ``until`` bound ``created_at`` (at or after / at or before);
    ``before_id`` keeps rows with a smaller id. "Newest first" is by id,
    the order in which the intents were recorded, so that ``before_id`` =
    the last id of a page continues exactly where that page ended.
    """
    query = select(ODataAuditLog)
    if service is not None:
        query = query.where(ODataAuditLog.service == service)
    if sent_as is not None:
        query = query.where(ODataAuditLog.sent_as == sent_as)
    if outcome is not None:
        query = query.where(ODataAuditLog.outcome == outcome)
    if since is not None:
        query = query.where(ODataAuditLog.created_at >= since)
    if until is not None:
        query = query.where(ODataAuditLog.created_at <= until)
    if before_id is not None:
        query = query.where(ODataAuditLog.id < before_id)
    query = query.order_by(ODataAuditLog.id.desc()).limit(limit)
    return list((await session.execute(query)).scalars().all())


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

    # Local import: step_kinds imports nothing from this module, but keeping
    # the dependency here means loading the models never pulls in httpx.
    from agents.step_kinds import STEP_KINDS, validate_step_config  # noqa: PLC0415

    for s in steps:
        # --- step kinds ---
        kind = str(s.get("kind") or "agent").strip()
        if kind not in STEP_KINDS:
            raise ValueError(
                f"Step {s.get('position')} has unknown kind {kind!r}; expected one "
                f"of {', '.join(STEP_KINDS)}."
            )
        agent = str(s.get("agent_name") or "").strip()
        if kind == "agent":
            if agent not in known_agents:
                raise ValueError(
                    f"Step {s.get('position')} names agent {agent!r}, which does not "
                    "exist or is disabled."
                )
        else:
            if s.get("fan_out"):
                raise ValueError(
                    f"The fan-out step must be an agent step; step "
                    f"{s.get('position')} is a {kind} step."
                )
            try:
                validate_step_config(kind, s.get("config"))
            except ValueError as e:
                raise ValueError(
                    f"Step {s.get('position')} ({kind}) has an invalid config: {e}"
                ) from None
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
    # first(): same pre-index duplicate tolerance as get_agent_by_slug.
    result = await session.execute(
        select(Workflow).where(Workflow.api_slug == slug).order_by(Workflow.id)
    )
    rows = list(result.scalars().all())
    if len(rows) > 1:
        logger.warning(
            "api_slug %r is used by %d workflows (%s); using %r.",
            slug, len(rows), ", ".join(r.name for r in rows), rows[0].name,
        )
    return rows[0] if rows else None


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
    # KEEP, not None: an imported bundle carries no run_as_principal (it is a
    # landscape-specific service identity, excluded from to_export), and
    # defaulting to None would silently un-configure every workflow on the
    # first import. See the _Keep docstring.
    run_as_principal: str | None | _Keep = KEEP,
    run_timeout_seconds: int = 1800,
    skip_seen_items: bool = True,
    max_parallel_items: int = 1,
    on_unknown_branch: str = "fail",
    enabled: bool = True,
    branches: list[dict[str, Any]] | None = None,
    steps: list[dict[str, Any]] | None = None,
    commit: bool = True,
) -> Workflow:
    """Create or replace a workflow and its parts.

    Branches and steps are replaced wholesale rather than diffed: a definition
    is small and edited as a unit, so reconciliation would add bugs and buy
    nothing. ``commit=False`` flushes instead, for callers writing a bundle
    in one transaction.
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
    slug = validate_api_slug(api_slug)
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
    if not isinstance(run_as_principal, _Keep):
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
    # --- step kinds ---
    from agents.step_kinds import config_to_json  # noqa: PLC0415

    for s in steps:
        bk = s.get("branch_key")
        kind = str(s.get("kind") or "agent").strip()
        session.add(WorkflowStep(
            workflow_id=row.id,
            branch_key=str(bk).strip() if bk is not None else None,
            position=int(s["position"]),
            agent_name=str(s.get("agent_name") or "").strip() if kind == "agent" else "",
            instructions=str(s.get("instructions") or ""),
            fan_out=1 if s.get("fan_out") else 0,
            step_timeout_seconds=int(s.get("step_timeout_seconds") or 600),
            kind=kind,
            config_json=config_to_json(kind, s.get("config")),
        ))
    if commit:
        await session.commit()
    else:
        await session.flush()
    await session.refresh(row)
    return row


async def delete_workflow(
    session: AsyncSession, workflow_id: int, *, commit: bool = True
) -> bool:
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
    if commit:
        await session.commit()
    else:
        await session.flush()
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


# The only WorkflowRun columns `finish_workflow_run`'s `counts` may set. An
# allow-list, not a blind setattr: a typo'd key would otherwise create a
# plain Python attribute SQLAlchemy silently drops at flush, and a key that
# collides with a real column (e.g. "workflow_id") would silently overwrite
# it if int-convertible.
_WORKFLOW_RUN_COUNT_FIELDS = frozenset(
    {"items_total", "items_succeeded", "items_failed", "items_skipped"}
)


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
    unknown = set(counts or {}) - _WORKFLOW_RUN_COUNT_FIELDS
    if unknown:
        raise ValueError(f"Unknown workflow run count field(s): {sorted(unknown)}")
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
    if parent is None:
        raise ValueError(f"No workflow run with id {run_id!r}")
    row = WorkflowItemRun(
        id=str(uuid.uuid4()),
        workflow_run_id=run_id,
        workflow_id=parent.workflow_id,
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

    ``all_running=True`` sweeps every running row (startup: a fresh process
    owns none of them). Otherwise only runs older than their workflow's
    ``run_timeout_seconds`` are swept -- a young row may be one of our own --
    mirroring sweep_stale_runs. A run whose workflow no longer exists gets
    the 1800s ceiling every workflow is bounded by.
    """
    result = await session.execute(
        select(WorkflowRun).where(WorkflowRun.status == ACTIVE_RUN_STATUS)
    )
    now = datetime.now(timezone.utc)
    swept: list[WorkflowRun] = []
    timeouts: dict[int, int] = {}
    for row in result.scalars().all():
        if not all_running:
            started = row.started_at
            if started is None:
                continue
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if row.workflow_id not in timeouts:
                wf = await session.get(Workflow, row.workflow_id)
                timeouts[row.workflow_id] = (
                    int(wf.run_timeout_seconds) if wf is not None else 1800
                )
            if (now - started).total_seconds() <= timeouts[row.workflow_id]:
                continue
        row.status = "interrupted"
        row.error = (
            "Run was still marked running at startup; marked interrupted."
            if all_running
            else "Run did not finish within its timeout (app restart or crash)."
        )
        row.finished_at = now
        swept.append(row)
    if not swept:
        return 0
    run_ids = [r.id for r in swept]
    items = await session.execute(
        select(WorkflowItemRun).where(
            WorkflowItemRun.status == ACTIVE_RUN_STATUS,
            WorkflowItemRun.workflow_run_id.in_(run_ids),
        )
    )
    for item in items.scalars().all():
        item.status = "interrupted"
        item.finished_at = now
    steps = await session.execute(
        select(WorkflowStepRun).where(
            WorkflowStepRun.status == ACTIVE_RUN_STATUS,
            WorkflowStepRun.workflow_run_id.in_(run_ids),
        )
    )
    for step in steps.scalars().all():
        step.status = "interrupted"
        step.finished_at = now
    await session.commit()
    return len(swept)


async def get_orchestrator_instructions(session: AsyncSession) -> str:
    row = await session.get(OrchestratorConfig, 1)
    return row.instructions if row else DEFAULT_ORCHESTRATOR_INSTRUCTIONS


async def set_orchestrator_instructions(
    session: AsyncSession, instructions: str, *, commit: bool = True
) -> None:
    row = await session.get(OrchestratorConfig, 1)
    if row is None:
        row = OrchestratorConfig(id=1, instructions=instructions)
        session.add(row)
    else:
        row.instructions = instructions
    if commit:
        await session.commit()
    else:
        await session.flush()


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


async def get_user_tokens(
    session: AsyncSession, user_id: str, server_keys: list[str]
) -> dict[str, McpOAuthToken]:
    """Every stored token this principal holds for `server_keys`, keyed by key.

    The one-at-a-time `get_user_token` turned the admin credentials panel into
    N sequential round trips for an agent with N servers. Keys absent from the
    result simply have no token stored.
    """
    if not server_keys:
        return {}
    result = await session.execute(
        select(McpOAuthToken).where(
            McpOAuthToken.user_id == user_id,
            McpOAuthToken.server_key.in_(server_keys),
        )
    )
    return {row.server_key: row for row in result.scalars().all()}


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
    activity: dict[str, Any] | None = None,
) -> None:
    row = await session.get(JobRun, run_id)
    if row is None:
        return
    row.status = status
    row.summary = summary
    row.report_json = json.dumps(report) if report is not None else None
    row.error = error
    if activity is not None:
        row.activity_json = json.dumps(activity)
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


# --- destinations ---------------------------------------------------------
# Every built-in can run through a BTP destination. The block stores the
# destination's name, the `user_context` switch (resolve the destination with
# the signed-in user's JWT and act as that user, versus the destination's own
# app-level credential) and the same pinned keys the built-in carries on its
# other modes. Jira and Slack keep their original tuples above; these cover
# the built-ins that gained the mode later. Mirrored by `cleanOAuth` in
# ui5-admin/webapp/model/oauthConfig.ts.
_GMAIL_DEST_KEYS = ("destination", "mailbox")
_OUTLOOK_DEST_KEYS = ("destination", "mailbox", "lookback", "recipients")
_TEAMS_DEST_KEYS = ("destination", "team", "channels", "lookback")
_SAPNOTES_DEST_KEYS = ("destination", "min_score", "lookback")
_SAPNOTEDETAIL_DEST_KEYS = ("destination",)
# builtin:smtp: the fixed audience and an optional sender overriding the MAIL
# destination's mail.smtp.from. The SMTP credential stays in the destination.
_SMTP_DEST_KEYS = ("destination", "recipients", "from")

_DEST_KEYS_BY_URL: dict[str, tuple[str, ...]] = {
    "builtin:gmail": _GMAIL_DEST_KEYS,
    "builtin:outlook": _OUTLOOK_DEST_KEYS,
    "builtin:teams": _TEAMS_DEST_KEYS,
    "builtin:sapnotes": _SAPNOTES_DEST_KEYS,
    "builtin:sapnotedetail": _SAPNOTEDETAIL_DEST_KEYS,
    "builtin:smtp": _SMTP_DEST_KEYS,
}
# builtin:odata is not in the map above on purpose: its entry stores no
# destination. Which destination a call goes through, and whether as the
# signed-in user, belongs to each catalogue service (`ODataService`), so an
# agent cannot pick another identity for a service by editing its own entry.
# The entry names the services the agent may use and whether it may write.
# Public because `agents.admin` refuses every other key at the payload
# boundary (the reason `BUILTIN_PUBLIC_KEYS` is public).
ODATA_ENTRY_KEYS = ("services", "allow_write")
MAX_ODATA_ENTRY_SERVICES = 50
ODATA_SINGLE_ENTRY_MESSAGE = (
    "an agent may have at most one builtin:odata entry; list every service "
    "in that entry's oauth.services"
)
_ODATA_MODE_MESSAGE = (
    "builtin:odata requires auth_mode=destination: every catalogue service "
    "is reached through the BTP destination it names"
)
# Built-ins whose destination may act as the signed-in user. NVD has no user
# to act as and the me.sap.com cookie is one shared session, so the switch is
# dropped for those rather than stored as a promise nothing keeps.
_DEST_USER_CONTEXT_URLS = frozenset({"builtin:gmail", "builtin:outlook", "builtin:teams"})
# Built-ins with a posting/sending capability switch in destination mode.
_DEST_ALLOW_SEND_URLS = frozenset({"builtin:outlook", "builtin:teams", "builtin:smtp"})


def _clean_builtin_destination(src: dict[str, Any], builtin: str) -> dict[str, Any]:
    """Normalize a ``destination`` block for a built-in in ``_DEST_KEYS_BY_URL``.

    Same promises as `_clean_destination`: credential keys are dropped, so
    nothing secret lands in the database, and ``user_context`` /
    ``allow_send`` are stored as real booleans and only when true -- the
    string "false" must neither switch whose token a request carries nor
    open a send tool.
    """
    cleaned: dict[str, Any] = {}
    for k in _DEST_KEYS_BY_URL[builtin]:
        v = src.get(k)
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x).strip() for x in v if str(x).strip())
        if v is not None and str(v).strip() != "":
            cleaned[k] = str(v).strip()
    if not cleaned.get("destination"):
        raise ValueError("destination server requires a destination name")
    if builtin in _DEST_USER_CONTEXT_URLS and src.get("user_context") is True:
        cleaned["user_context"] = True
    if builtin in _DEST_ALLOW_SEND_URLS:
        cleaned["allow_send"] = src.get("allow_send") is True
    return cleaned


def _clean_odata_entry(src: dict[str, Any]) -> dict[str, Any]:
    """Normalize the block of a ``builtin:odata`` entry for storage.

    Exactly ``{"services": [...]}`` plus ``"allow_write": True``; every other
    key is dropped, ``destination`` and ``user_context`` included. The last
    gate before the row, whoever the caller is, so it denies by default:

    - ``services`` must be a list of service names (the catalogue's slug
      form). Anything else is refused rather than skipped: an entry whose
      list was silently shortened would be an agent with less than its admin
      configured, and a non-string value has no meaning here. Duplicates are
      dropped, first occurrence wins.
    - ``allow_write`` is kept only for the JSON boolean ``true``. The string
      "true" or the number 1 must not open writes; stored without the key,
      the entry is read-only.

    Whether the names exist in the catalogue is `check_odata_services`'
    question (it needs the session). The message never repeats a refused
    value.
    """
    raw = src.get("services")
    if not isinstance(raw, list) or not raw:
        raise ValueError("builtin:odata requires at least one service")
    services: list[str] = []
    for name in raw:
        if not isinstance(name, str) or not re.fullmatch(_ODATA_SERVICE_NAME_RE, name):
            raise ValueError("builtin:odata services: invalid service name")
        if name not in services:
            services.append(name)
    if len(services) > MAX_ODATA_ENTRY_SERVICES:
        raise ValueError(
            f"builtin:odata allows at most {MAX_ODATA_ENTRY_SERVICES} services per entry"
        )
    cleaned: dict[str, Any] = {"services": services}
    if src.get("allow_write") is True:
        cleaned["allow_write"] = True
    return cleaned
