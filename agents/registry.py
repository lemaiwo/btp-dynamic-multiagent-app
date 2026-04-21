"""Dynamic agent registry.

Builds the orchestrator agent and all specialist sub-agents from the
database on startup and on reload. Holds the current set of MCP servers
so they can be closed when the registry is rebuilt.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from pydantic_ai import Agent, RunContext

from agents.db import (
    AgentConfig,
    SessionLocal,
    get_orchestrator_instructions,
    list_agents,
)
from agents.shared import create_mcp_server, get_model

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_TOOL_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")


def _sanitize_tool_name(name: str) -> str:
    slug = _TOOL_NAME_RE.sub("_", name.strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return f"delegate_{slug}" if slug else "delegate_agent"


def _format_error(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_format_error(e) for e in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Build result
# ---------------------------------------------------------------------------
@dataclass
class BuildResult:
    orchestrator: Agent
    specialists: dict[str, Agent]
    mcp_clients: list  # httpx.AsyncClient owned by MCP servers, for cleanup
    configs: list[dict]  # snapshot of AgentConfig.to_dict()


async def build_orchestrator() -> BuildResult:
    """Build a fresh orchestrator + specialists from the current DB state."""
    async with SessionLocal() as session:
        rows = await list_agents(session)
        orch_instructions = await get_orchestrator_instructions(session)
        configs = [r.to_dict() for r in rows]
        enabled_rows = [r for r in rows if r.enabled]

    specialists: dict[str, Agent] = {}
    mcp_clients: list = []

    # Build the orchestrator instructions, listing enabled specialists
    specialist_lines = [
        f"- **{r.name}**: {r.description.strip()}" for r in enabled_rows
    ]
    instructions = orch_instructions.strip()
    if specialist_lines:
        instructions += "\n\nAvailable specialists:\n" + "\n".join(specialist_lines)
    else:
        instructions += (
            "\n\nNo specialists are currently configured. Inform the user "
            "that an administrator must add agents in /admin before you can "
            "help with BTP-specific tasks."
        )

    orchestrator = Agent(get_model(), instructions=instructions)

    # Build each specialist and register a delegation tool on the orchestrator
    for row in enabled_rows:
        servers = []
        for idx, spec in enumerate(row.mcp_servers):
            server_name = row.name if idx == 0 else f"{row.name}-{idx}"
            try:
                servers.append(
                    create_mcp_server(server_name, spec["url"], spec["auth_mode"])
                )
            except Exception:
                logger.exception(
                    "Failed to create MCP server %s for agent %s",
                    spec.get("url"),
                    row.name,
                )
        if not servers:
            logger.warning("Agent %s has no usable MCP servers; skipping", row.name)
            continue

        mcp_clients.extend(servers)

        specialist = Agent(
            get_model(),
            instructions=row.instructions,
            toolsets=servers,
        )
        specialists[row.name] = specialist

        _attach_delegation_tool(orchestrator, specialist, row)

    return BuildResult(
        orchestrator=orchestrator,
        specialists=specialists,
        mcp_clients=mcp_clients,
        configs=configs,
    )


def _attach_delegation_tool(
    orchestrator: Agent, specialist: Agent, row: AgentConfig
) -> None:
    """Register a per-specialist delegation tool on the orchestrator."""
    tool_name = _sanitize_tool_name(row.name)
    description = (
        f"Delegate to the '{row.name}' specialist. {row.description.strip()}"
    )

    async def _delegate(ctx: RunContext, query: str) -> str:
        try:
            result = await specialist.run(query, usage=ctx.usage)
            return str(result.output)
        except BaseException as e:  # noqa: BLE001
            logger.exception("Specialist %s failed", row.name)
            return f"Error from {row.name}: {_format_error(e)}"

    _delegate.__name__ = tool_name
    _delegate.__doc__ = description
    orchestrator.tool(name=tool_name, description=description)(_delegate)


# ---------------------------------------------------------------------------
# Registry singleton
# ---------------------------------------------------------------------------
class Registry:
    """Holds the current orchestrator and provides atomic reload."""

    def __init__(self) -> None:
        self._build: BuildResult | None = None
        self._lock = asyncio.Lock()

    @property
    def orchestrator(self) -> Agent:
        if self._build is None:
            raise RuntimeError("Registry not initialized; call reload() first")
        return self._build.orchestrator

    @property
    def build(self) -> BuildResult:
        if self._build is None:
            raise RuntimeError("Registry not initialized; call reload() first")
        return self._build

    async def reload(self) -> BuildResult:
        """Rebuild the orchestrator from the current database state."""
        async with self._lock:
            logger.info("Reloading agent registry...")
            old = self._build
            new = await build_orchestrator()
            self._build = new
            logger.info(
                "Registry reloaded: %d enabled / %d total specialists",
                len(new.specialists),
                len(new.configs),
            )

            # Best-effort cleanup of the previous MCP clients
            if old is not None:
                for server in old.mcp_clients:
                    try:
                        client = getattr(server, "_http_client", None) or getattr(
                            server, "http_client", None
                        )
                        if client is not None:
                            await client.aclose()
                    except Exception:
                        logger.debug("Failed to close old MCP client", exc_info=True)

            return new


registry = Registry()
