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
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection string resolution
# ---------------------------------------------------------------------------
def _resolve_database_url() -> str:
    """Build an async SQLAlchemy URL from VCAP_SERVICES or env."""
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
    _connect_args["ssl"] = True

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
            "instructions": self.instructions,
            "mcp_url": self.mcp_url,
            "enabled": bool(self.enabled),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def to_export(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "instructions": self.instructions,
            "mcp_url": self.mcp_url,
            "enabled": bool(self.enabled),
        }


class OrchestratorConfig(Base):
    __tablename__ = "orchestrator_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


DEFAULT_ORCHESTRATOR_INSTRUCTIONS = (
    "You are an SAP BTP platform management orchestrator. "
    "You coordinate between specialized agents to help users manage their SAP BTP "
    "landscape. Delegate each task to the most appropriate specialist based on "
    "their description. You may combine results from multiple agents to give "
    "comprehensive answers. When a request spans multiple domains, call the "
    "relevant specialists one at a time and synthesize their responses."
)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
async def init_db() -> None:
    """Create tables and ensure an orchestrator config row exists."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as session:
        existing = await session.get(OrchestratorConfig, 1)
        if existing is None:
            session.add(
                OrchestratorConfig(id=1, instructions=DEFAULT_ORCHESTRATOR_INSTRUCTIONS)
            )
            await session.commit()


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


async def upsert_agent(
    session: AsyncSession,
    *,
    name: str,
    description: str,
    instructions: str,
    mcp_url: str,
    enabled: bool = True,
) -> AgentConfig:
    existing = await get_agent_by_name(session, name)
    if existing is None:
        row = AgentConfig(
            name=name,
            description=description,
            instructions=instructions,
            mcp_url=mcp_url,
            enabled=1 if enabled else 0,
        )
        session.add(row)
    else:
        existing.description = description
        existing.instructions = instructions
        existing.mcp_url = mcp_url
        existing.enabled = 1 if enabled else 0
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
