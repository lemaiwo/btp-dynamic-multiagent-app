"""Dynamic agent registry.

Builds the orchestrator agent and all specialist sub-agents from the
database on startup and on reload. Holds the current set of MCP servers
so they can be closed when the registry is rebuilt.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from pydantic_ai import Agent, RunContext

from agents.db import (
    AgentConfig,
    SessionLocal,
    get_active_model_name,
    get_orchestrator_instructions,
    list_agents,
)
from agents.shared import create_mcp_server, default_model_name, get_model

logger = logging.getLogger(__name__)

# How many times a tool may return a retryable error (pydantic-ai ModelRetry)
# before the agent gives up. MCP tools like SAP's SAPQuery surface query/syntax
# errors this way so the model can self-correct; the default of 1 is too low to
# recover, so allow a few attempts. Tunable via env.
_TOOL_RETRIES = int(os.environ.get("AGENT_TOOL_RETRIES", "3"))

# Hard ceiling on a single specialist run so a stuck MCP/model call surfaces as
# a logged error instead of an indefinitely "running" chat. Tunable via env.
_SPECIALIST_TIMEOUT = float(os.environ.get("SPECIALIST_TIMEOUT_SECONDS", "180"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_TOOL_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")


def _sanitize_tool_name(name: str) -> str:
    slug = _TOOL_NAME_RE.sub("_", name.strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return f"delegate_{slug}" if slug else "delegate_agent"


# Tool prefixes must stay short: prefixed tool names (`{prefix}_{tool}`) have
# to fit OpenAI/Azure's 64-char function-name limit. The first DNS label,
# truncated, gives a compact yet recognizable prefix.
_MAX_PREFIX_LEN = 12


def _compute_tool_prefixes(urls: list[str]) -> list[str]:
    """Derive one short tool prefix per URL, disambiguating collisions by
    appending an index. Used when an agent binds multiple MCP servers so their
    tool names don't clash. Kept short so `{prefix}_{tool}` stays within the
    64-char function-name limit (the full hostname would blow past it)."""
    slugs = []
    for u in urls:
        host = (urlparse(u).hostname or "mcp").lower()
        first_label = host.split(".")[0]
        slug = re.sub(r"[^a-z0-9]+", "_", first_label).strip("_") or "mcp"
        slug = slug[:_MAX_PREFIX_LEN].strip("_") or "mcp"
        slugs.append(slug)
    counts: dict[str, int] = {s: slugs.count(s) for s in set(slugs)}
    seen: dict[str, int] = {}
    result: list[str] = []
    for s in slugs:
        if counts[s] > 1:
            n = seen.get(s, 0)
            seen[s] = n + 1
            result.append(f"{s}_{n}")
        else:
            result.append(s)
    return result


def _format_error(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_format_error(e) for e in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


async def _authorization_prompt(agent_name: str, exc: BaseException) -> str | None:
    """If the failure was 'needs OAuth2 authorization', return a chat message
    with a sign-in link; otherwise None so normal error handling proceeds.

    The link points at this app's ``/oauth/login`` endpoint (short + stable),
    which builds the real authorize URL at click time — so the long PKCE/state
    URL never has to survive being relayed through the orchestrator LLM.
    """
    from urllib.parse import quote

    from agents.auth import current_base_url, current_principal
    from agents.oauth2 import find_oauth_required

    required = find_oauth_required(exc)
    if required is None:
        return None

    user_id = current_principal.get()
    base_url = current_base_url.get()
    if not user_id or not base_url:
        return (
            f"**{agent_name}** needs you to sign in, but the sign-in link could "
            "not be built (missing user identity or app URL). Open the app "
            "through its approuter URL and try again."
        )
    link = f"{base_url.rstrip('/')}/oauth/login?agent={quote(agent_name)}"
    return (
        f"🔐 **{agent_name}** needs you to sign in first.\n\n"
        f"**[Click here to sign in to {agent_name}]({link})**\n\n"
        "After signing in, send your request again."
    )


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
        active_model = await get_active_model_name(session)
        configs = [r.to_dict() for r in rows]
        enabled_rows = [r for r in rows if r.enabled]

    model_name = active_model or default_model_name()
    try:
        model = get_model(model_name)
    except Exception as e:
        fallback = default_model_name()
        logger.warning(
            "Active model %r could not be loaded (%s); falling back to %r. "
            "Pick a valid model in /admin to clear this.",
            model_name, e, fallback,
        )
        if fallback != model_name:
            try:
                model = get_model(fallback)
                model_name = fallback
            except Exception:
                logger.exception("Fallback model %r also failed", fallback)
                raise
        else:
            raise

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

    # A specialist may return a sign-in link (when its MCP server needs the
    # user to authorize). The model must pass that through untouched, or the
    # user never sees the link.
    instructions += (
        "\n\nIMPORTANT — sign-in links: if a specialist's response contains a "
        "sign-in or authorization link (a Markdown link), relay that response "
        "to the user verbatim, including the full link. Do not summarize, "
        "rephrase, shorten, or omit the link."
    )

    orchestrator = Agent(model, instructions=instructions, retries=_TOOL_RETRIES)

    # Build each specialist and register a delegation tool on the orchestrator
    for row in enabled_rows:
        servers = []
        specs = row.mcp_servers
        prefixes = (
            _compute_tool_prefixes([s["url"] for s in specs])
            if len(specs) > 1
            else [None] * len(specs)
        )
        for idx, (spec, prefix) in enumerate(zip(specs, prefixes)):
            server_name = row.name if idx == 0 else f"{row.name}-{idx}"
            try:
                servers.append(
                    create_mcp_server(
                        server_name,
                        spec["url"],
                        spec["auth_mode"],
                        tool_prefix=prefix,
                        oauth=spec.get("oauth"),
                    )
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
            model,
            instructions=row.instructions,
            toolsets=servers,
            retries=_TOOL_RETRIES,
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
        logger.info("[delegate] %s START | query=%.160s", row.name, query.replace("\n", " "))
        try:
            result = await asyncio.wait_for(
                specialist.run(query, usage=ctx.usage), timeout=_SPECIALIST_TIMEOUT
            )
            out = "" if result.output is None else str(result.output)
            logger.info(
                "[delegate] %s DONE | output=%d chars | %.300s",
                row.name, len(out), out.replace("\n", " "),
            )
            if not out.strip():
                return (
                    f"The {row.name} specialist completed but returned no text. "
                    "Please rephrase or try again."
                )
            return out
        except asyncio.TimeoutError:
            logger.error(
                "[delegate] %s TIMEOUT after %.0fs", row.name, _SPECIALIST_TIMEOUT
            )
            return (
                f"The {row.name} specialist timed out after {int(_SPECIALIST_TIMEOUT)}s. "
                "The underlying system may be slow or the query too large; try a "
                "narrower request."
            )
        except BaseException as e:  # noqa: BLE001
            auth_msg = await _authorization_prompt(row.name, e)
            if auth_msg is not None:
                logger.info("[delegate] %s -> authorization required", row.name)
                return auth_msg
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
