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
import reprlib
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
from agents.shared import (
    create_mcp_server,
    default_model_name,
    get_model,
    owned_http_client,
)

logger = logging.getLogger(__name__)

# Grace period before the previous build's MCP httpx clients are closed on
# reload, so chat turns that started before the reload can finish on the old
# orchestrator instead of failing mid-run. Tunable via env.
_OLD_CLIENT_CLOSE_DELAY = float(os.environ.get("RELOAD_CLIENT_CLOSE_DELAY_SECONDS", "300"))

# How many times a tool may return a retryable error (pydantic-ai ModelRetry)
# before the agent gives up. MCP tools like SAP's SAPQuery surface query/syntax
# errors this way so the model can self-correct; the default of 1 is too low to
# recover, so allow a few attempts. Tunable via env.
_TOOL_RETRIES = int(os.environ.get("AGENT_TOOL_RETRIES", "3"))

# Hard ceiling on a single specialist run so a stuck MCP/model call surfaces as
# a logged error instead of an indefinitely "running" chat. Tunable via env.
_SPECIALIST_TIMEOUT = float(os.environ.get("SPECIALIST_TIMEOUT_SECONDS", "180"))

# When a specialist's MCP server needs the user to sign in (auth_mode="oauth2"),
# show a sign-in link and wait this long for the user to authorize in the popup
# before giving up — then resume automatically. Polls the token store meanwhile.
_AUTH_WAIT_SECONDS = float(os.environ.get("MCP_AUTH_WAIT_SECONDS", "180"))
_AUTH_POLL_SECONDS = float(os.environ.get("MCP_AUTH_POLL_SECONDS", "1.5"))
# Cap auth→retry rounds so a server that keeps demanding auth can't loop forever.
_MAX_AUTH_ROUNDS = int(os.environ.get("MCP_AUTH_MAX_ROUNDS", "2"))


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


# Bounded repr for non-str tool payloads: limits string/container size so a
# multi-MB MCP result is never fully materialized just to be truncated away.
_PREVIEW_REPR = reprlib.Repr()
_PREVIEW_REPR.maxstring = 600
_PREVIEW_REPR.maxother = 600


def _short_tool_output(result) -> str:
    """A compact, single-blob preview of a specialist tool's return value.

    Shown (collapsed) inside the tool card in the chat UI, so keep it bounded —
    MCP tools can return large payloads we don't want to stream in full.
    """
    try:
        content = getattr(result, "content", None)
        if content is None and hasattr(result, "model_response_str"):
            content = result.model_response_str()
        text = content if isinstance(content, str) else _PREVIEW_REPR.repr(content)
    except Exception:  # noqa: BLE001 — preview must never break a run
        text = ""
    text = text.strip()
    return text[:600] + "…" if len(text) > 600 else text


def _make_progress_handler(agent_name: str):
    """Build a pydantic-ai ``event_stream_handler`` that surfaces each tool call
    a specialist makes while it runs.

    A specialist often takes several model+MCP iterations to answer. Those
    iterations happen inside an opaque delegation tool, so without this they are
    completely silent — both in the logs and to anyone watching the chat. We log
    each step for operators and report it via :mod:`agents.progress` so the chat
    endpoint can render it live as a native tool card (a pulsing "Running" badge
    that flips to a green "Completed" / red "Error"; see ``agents/chat_app.py``).

    Tool starts that never see a matching result (the run was cancelled mid-call
    — e.g. a timeout) are closed in the ``finally`` block so the UI never leaves
    a card stuck "Running".
    """
    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        RetryPromptPart,
    )

    from agents.progress import report_tool_end, report_tool_start

    async def handler(_ctx, events) -> None:
        step = 0
        open_calls: dict[str, str] = {}  # tool_call_id -> tool_name still running
        try:
            async for event in events:
                if isinstance(event, FunctionToolCallEvent):
                    part = event.part
                    step += 1
                    open_calls[part.tool_call_id] = part.tool_name
                    try:
                        args = part.args_as_dict()
                    except Exception:  # noqa: BLE001
                        args = None
                    logger.info(
                        "[delegate] %s working | step %d -> %s",
                        agent_name, step, part.tool_name,
                    )
                    report_tool_start(agent_name, part.tool_call_id, part.tool_name, args)
                elif isinstance(event, FunctionToolResultEvent):
                    result = event.result
                    open_calls.pop(result.tool_call_id, None)
                    failed = isinstance(result, RetryPromptPart)
                    logger.info(
                        "[delegate] %s working | %s %s",
                        agent_name,
                        getattr(result, "tool_name", "?"),
                        "failed" if failed else "returned",
                    )
                    report_tool_end(
                        agent_name,
                        result.tool_call_id,
                        ok=not failed,
                        output=_short_tool_output(result),
                    )
        finally:
            # The run ended (or was cancelled) with calls still in flight; close
            # their cards so they don't hang on the pulsing "Running" state.
            for call_id in open_calls:
                report_tool_end(
                    agent_name, call_id, ok=False, output="(interrupted)"
                )

    return handler


def _format_error(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_format_error(e) for e in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


def _build_login_link(agent_name: str, server_key: str | None = None) -> str | None:
    """The app's short, stable ``/oauth/login`` link for an agent (it builds the
    real authorize URL at click time). None if the app base URL is unknown.

    ``server_key`` pins the link to the specific MCP server that needs sign-in,
    so an agent with several oauth2 servers authorizes the right one (and the
    token lands under the key the wait loop polls)."""
    from urllib.parse import quote

    from agents.auth import current_base_url

    base_url = current_base_url.get()
    if not base_url:
        return None
    link = f"{base_url.rstrip('/')}/oauth/login?agent={quote(agent_name)}"
    if server_key:
        link += f"&server={quote(server_key, safe='')}"
    return link


def _signin_message(agent_name: str, link: str) -> str:
    """Chat bubble shown when a specialist needs sign-in; the run keeps going
    and resumes by itself once the user authorizes (see ``_wait_for_token``)."""
    return (
        f"🔐 **{agent_name}** needs you to sign in to continue.\n\n"
        f"[**Sign in to {agent_name}**]({link})\n\n"
        "A sign-in window will open. I'll keep working and continue "
        "automatically as soon as you're done — no need to message me again."
    )


def _signin_timeout_message(agent_name: str) -> str:
    return (
        f"I didn't detect a completed sign-in for **{agent_name}** in time. "
        "Use the sign-in link above, then send your request again."
    )


async def _wait_for_token(user_id: str, server_key: str) -> bool:
    """Poll the token store until the user has a valid token for ``server_key``
    (they finished signing in) or the wait window elapses.

    ``asyncio.sleep`` is a cancellation point, so a client disconnect unwinds
    this promptly rather than holding the request open."""
    from agents.oauth2 import has_valid_token

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _AUTH_WAIT_SECONDS
    while loop.time() < deadline:
        if await has_valid_token(user_id, server_key):
            return True
        await asyncio.sleep(_AUTH_POLL_SECONDS)
    return await has_valid_token(user_id, server_key)


def _oauth2_server_keys(row) -> list[str]:
    """Normalized server keys of an agent's oauth2 MCP servers (the keys tokens
    are stored under). Empty when the row carries no server list (e.g. tests)."""
    from agents.db import AUTH_MODE_OAUTH2
    from agents.oauth2 import normalize_mcp_url

    keys: list[str] = []
    for spec in getattr(row, "mcp_servers", None) or []:
        if spec.get("auth_mode") == AUTH_MODE_OAUTH2 and spec.get("url"):
            keys.append(normalize_mcp_url(str(spec["url"])))
    return keys


async def _await_signin(agent_name: str, server_key: str, user_id: str) -> bool:
    """Show the sign-in link for ``server_key`` and wait for the user to
    authorize in the popup. Returns True once a token appears, False on timeout
    (or if no link could be built). Shared by the pre-check and the in-run path."""
    from agents.progress import report_message, report_note

    link = _build_login_link(agent_name, server_key)
    if not link:
        return False
    report_message(agent_name, _signin_message(agent_name, link))
    report_note(agent_name, "Waiting for you to sign in…")
    if await _wait_for_token(user_id, server_key):
        report_note(agent_name, f"Signed in — resuming {agent_name}…")
        return True
    return False


async def _authorization_prompt(agent_name: str, exc: BaseException) -> str | None:
    """Static fallback for when auto-continue can't run (no app URL / identity):
    return a chat message with a sign-in link the user can use, then retry
    manually; otherwise None so normal error handling proceeds."""
    from agents.oauth2 import find_oauth_required

    required = find_oauth_required(exc)
    if required is None:
        return None
    link = _build_login_link(agent_name, required.server_key)
    if not link:
        return (
            f"**{agent_name}** needs you to sign in, but the sign-in link could "
            "not be built (missing user identity or app URL). Open the app "
            "through its approuter URL and try again."
        )
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

        # With exactly one specialist there is no routing decision to make —
        # always forward. Deliberating (or trying to answer directly) just adds
        # latency and the occasional refusal, so make delegation mandatory.
        if len(enabled_rows) == 1:
            only = enabled_rows[0]
            instructions += (
                f"\n\nThere is currently only ONE specialist available: "
                f"**{only.name}**. Forward every request that needs a specialist "
                f"straight to {only.name} — do not deliberate about which one to "
                "pick and do not try to answer such requests yourself."
            )

        # Keep the user informed while specialists run. The chat UI streams the
        # orchestrator's text immediately and shows a spinner on each delegation
        # tool call, so a one-line heads-up before delegating is what makes it
        # visible that an agent is working (specialist iterations themselves run
        # inside an opaque tool call and aren't streamed individually).
        instructions += (
            "\n\nBefore you call a specialist, tell the user in one short line "
            "what you're about to do (e.g. \"Checking with the "
            f"{enabled_rows[0].name} specialist…\"). When a request needs several "
            "specialists, narrate each step before the corresponding call so the "
            "user can follow your progress instead of staring at a silent screen."
        )
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
        from agents.auth import current_principal
        from agents.oauth2 import find_oauth_required, has_usable_token
        from agents.progress import (
            current_progress,
            report_delegation_end,
            report_delegation_start,
        )

        logger.info("[delegate] %s START | query=%.160s", row.name, query.replace("\n", " "))
        # Phase hint for the live "working…" heartbeat while the specialist spins
        # up (MCP connect + first model call) before any tool card appears. The
        # paired end (in `finally`) lets the chat endpoint tell when *every*
        # specialist is done and the orchestrator is composing the reply.
        report_delegation_start(row.name, f"Consulting the {row.name} specialist…")
        try:
            # Fast path: if we already know an oauth2 MCP server has no usable
            # credential for this user, prompt for sign-in *immediately* instead
            # of paying the full model-call + MCP-connect + OAuth-discovery cost
            # just to find out. Interactive runs only (A2A/no sink falls through
            # to the run, which raises and returns the static link).
            user_id = current_principal.get()
            if user_id and current_progress.get() is not None:
                for server_key in _oauth2_server_keys(row):
                    try:
                        # A refreshable token still works without an interactive
                        # sign-in (the run refreshes it silently), so only prompt
                        # when there's no usable credential at all.
                        if await has_usable_token(user_id, server_key):
                            continue
                        if not _build_login_link(row.name, server_key):
                            break  # no app URL → let the run raise → static fallback
                        logger.info("[delegate] %s -> sign-in needed (pre-check)", row.name)
                        signed_in = await _await_signin(row.name, server_key, user_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001
                        # Token lookup hiccup (e.g. transient DB error): don't
                        # crash the turn — defer to the run, which has graceful
                        # error handling and will re-prompt if auth is truly needed.
                        logger.warning(
                            "[delegate] %s pre-check failed; deferring to run",
                            row.name, exc_info=True,
                        )
                        break
                    if not signed_in:
                        return _signin_timeout_message(row.name)

            # Run the specialist; if its MCP server still needs the user to sign
            # in (e.g. token expired mid-run), show a sign-in link, wait for them
            # to authorize, then retry — so the user never has to message again.
            for attempt in range(_MAX_AUTH_ROUNDS + 1):
                try:
                    result = await asyncio.wait_for(
                        specialist.run(
                            query,
                            usage=ctx.usage,
                            event_stream_handler=_make_progress_handler(row.name),
                        ),
                        timeout=_SPECIALIST_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "[delegate] %s TIMEOUT after %.0fs", row.name, _SPECIALIST_TIMEOUT
                    )
                    return (
                        f"The {row.name} specialist timed out after "
                        f"{int(_SPECIALIST_TIMEOUT)}s. The underlying system may be "
                        "slow or the query too large; try a narrower request."
                    )
                except asyncio.CancelledError:
                    # The request was cancelled (e.g. the chat client
                    # disconnected). Let it unwind so the run actually stops,
                    # instead of being caught below and reported as a normal
                    # error (which would keep it running).
                    raise
                except BaseException as e:  # noqa: BLE001
                    required = find_oauth_required(e)
                    if required is None:
                        logger.exception("Specialist %s failed", row.name)
                        return f"Error from {row.name}: {_format_error(e)}"

                    # Sign-in needed. Auto-continue (show link, wait, retry) only
                    # makes sense with an interactive chat sink listening; for A2A
                    # / background runs there's no popup and no one to stream the
                    # link to, so return the sign-in link immediately instead of
                    # holding the request polling for a sign-in that can't happen.
                    user_id = current_principal.get()
                    interactive = current_progress.get() is not None
                    if not (
                        user_id
                        and interactive
                        and _build_login_link(row.name, required.server_key)
                    ):
                        return await _authorization_prompt(row.name, e) or (
                            f"Error from {row.name}: {_format_error(e)}"
                        )
                    if attempt >= _MAX_AUTH_ROUNDS:
                        return (
                            f"**{row.name}** still needs sign-in after several "
                            "attempts. Use the sign-in link above, then send your "
                            "request again."
                        )
                    logger.info("[delegate] %s -> awaiting user authorization", row.name)
                    if await _await_signin(row.name, required.server_key, user_id):
                        logger.info("[delegate] %s -> authorized; resuming", row.name)
                        continue  # retry the run, now with a token
                    logger.info("[delegate] %s -> sign-in not detected in time", row.name)
                    return _signin_timeout_message(row.name)

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

            # Defensive: loop exhausted without an explicit return.
            return (
                f"**{row.name}** could not complete sign-in. Use the sign-in link "
                "above, then send your request again."
            )
        finally:
            report_delegation_end(row.name)

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
        # Deferred-close tasks for retired MCP clients, held so they aren't GC'd.
        self._cleanup_tasks: set[asyncio.Task] = set()

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

            # Best-effort cleanup of the previous MCP clients, after a grace
            # period so in-flight chat turns still holding the old
            # orchestrator can finish instead of dying on a closed client.
            if old is not None:
                clients = [
                    c
                    for c in (owned_http_client(s) for s in old.mcp_clients)
                    if c is not None
                ]
                if clients:
                    task = asyncio.create_task(
                        self._close_later(clients, _OLD_CLIENT_CLOSE_DELAY)
                    )
                    self._cleanup_tasks.add(task)
                    task.add_done_callback(self._cleanup_tasks.discard)

            return new

    @staticmethod
    async def _close_later(clients: list, delay: float) -> None:
        await asyncio.sleep(delay)
        for client in clients:
            try:
                await client.aclose()
            except Exception:
                logger.debug("Failed to close old MCP client", exc_info=True)


registry = Registry()
