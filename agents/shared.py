"""Shared infrastructure for SAP BTP management agents.

Provides OAuth2 authentication, MCP server factory, and SAP AI Core model
setup. On Cloud Foundry the MCP servers are authenticated by forwarding the
user's JWT (read from a contextvar set by request middleware). Locally,
an interactive authorization_code flow with browser redirect is used.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from openai import omit as OMIT
from pydantic import AnyUrl
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.mcp import MCPServerStreamableHTTP
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

from agents.auth import current_jwt
from agents.errors import describe_exception, mask_text

logger = logging.getLogger(__name__)

ON_CF = "VCAP_APPLICATION" in os.environ

CALLBACK_PORT = int(os.environ.get("CALLBACK_PORT", "3000"))
CALLBACK_URL = f"http://localhost:{CALLBACK_PORT}/callback"


# ---------------------------------------------------------------------------
# Persistent token storage (file-based, one file per MCP server) — local only
# ---------------------------------------------------------------------------
class FileTokenStorage(TokenStorage):
    """Persists OAuth2 client registration and tokens to a local JSON file."""

    def __init__(self, path: Path):
        self.path = path
        self._data: dict = {}
        if self.path.exists():
            self._data = json.loads(self.path.read_text())

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._data, indent=2))

    async def get_tokens(self) -> OAuthToken | None:
        if "tokens" in self._data:
            return OAuthToken(**self._data["tokens"])
        return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._data["tokens"] = tokens.model_dump(mode="json")
        self._save()

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        if "client_info" in self._data:
            return OAuthClientInformationFull(**self._data["client_info"])
        return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._data["client_info"] = client_info.model_dump(mode="json")
        self._save()


# ---------------------------------------------------------------------------
# OAuth2 callback handling (local dev only)
# ---------------------------------------------------------------------------
_callback_future: asyncio.Future | None = None
_callback_loop: asyncio.AbstractEventLoop | None = None


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _callback_future, _callback_loop

        if self.path.startswith("/callback"):
            params = parse_qs(urlparse(self.path).query)
            code = params.get("code", [None])[0]
            state = params.get("state", [None])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body>"
                b"<h1>Authorization successful!</h1>"
                b"<p>You can close this tab and return to the chat.</p>"
                b"</body></html>"
            )

            if _callback_loop and _callback_future and not _callback_future.done():
                _callback_loop.call_soon_threadsafe(
                    _callback_future.set_result, (code, state)
                )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


async def _redirect_handler(auth_url: str) -> None:
    print("\nOpening browser for OAuth2 authentication...")
    print(f"If the browser doesn't open, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)


async def _callback_handler() -> tuple[str, str | None]:
    global _callback_future, _callback_loop
    _callback_loop = asyncio.get_running_loop()
    _callback_future = _callback_loop.create_future()

    server = HTTPServer(("localhost", CALLBACK_PORT), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        code, state = await _callback_future
        return code, state
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# JWT-forwarding auth (production on CF)
# ---------------------------------------------------------------------------
class JWTForwardAuth(httpx.Auth):
    """Forwards the current request's bound JWT as the MCP bearer token.

    Reads `agents.auth.current_jwt` which is populated by FastAPI middleware
    on each incoming request. Because contextvars propagate into async tasks,
    the bound token is visible to any httpx call made during request handling.
    """

    requires_request_body = False

    async def async_auth_flow(self, request):
        token = current_jwt.get()
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
            logger.debug("JWTForwardAuth: forwarding JWT to %s", request.url)
        else:
            logger.warning("JWTForwardAuth: no JWT bound for %s", request.url)
        yield request


# ---------------------------------------------------------------------------
# MCP server factory
# ---------------------------------------------------------------------------
# How many times a single MCP tool may be retried within one run before the
# failure is handed back to the model as an ordinary result. MCP toolsets do
# NOT inherit Agent(retries=...) — they carry their own max_retries, which
# defaults to 1 — so this has to be passed explicitly or SAPQuery gets a single
# attempt to self-correct an invalid query.
MCP_TOOL_RETRIES = int(os.environ.get("AGENT_TOOL_RETRIES", "3"))

# Model requests one agent run may make. pydantic-ai's own default is 50,
# which a deep agent spends before it finishes: its sub-agents run on the
# parent's usage (deep.py passes usage=ctx.usage), so every request they make
# counts against the same budget. Every run site passes run_usage_limits()
# so the ceiling is one setting rather than a default buried in the library.
AGENT_REQUEST_LIMIT = int(os.environ.get("AGENT_REQUEST_LIMIT", "200"))


def run_usage_limits():
    """The UsageLimits every agent run in this app is started with.

    Inside an IDE session (``agents.deep.current_workspace`` bound) the limit
    is also capped by what the session may still spend. Delegations and deep
    sub-agents pass ``usage=ctx.usage`` and call this too, so the check runs
    against the whole run tree's cumulative requests.
    """
    from pydantic_ai.usage import UsageLimits

    # Deferred: agents.deep imports this module.
    from agents.deep import current_workspace

    limit = AGENT_REQUEST_LIMIT
    scope = current_workspace.get()
    if scope is not None and scope.request_limit is not None:
        limit = min(limit, max(0, scope.request_limit))
    return UsageLimits(request_limit=limit)


async def _resilient_tool_call(ctx, call_tool, name: str, args, metadata=None):
    """Keep one failing MCP tool from killing the whole agent run.

    pydantic-ai raises ``UnexpectedModelBehavior`` out of ``Agent.run()`` once a
    tool exhausts its retries. For an unattended run that turns "one source was
    unreachable" into "no report at all", throwing away every source that did
    work. Returning the failure as an ordinary tool result instead lets the
    model record that source as unchecked and still produce its report.

    Genuine ``ModelRetry`` errors are re-raised while attempts remain, so
    pydantic-ai's own retry loop still runs and the model can fix, say, invalid
    SQL. Only the final attempt degrades to text. Transport and protocol errors
    degrade immediately — no amount of rewriting the arguments fixes a closed
    connection, so spending the retry budget on it only delays the report.
    """
    try:
        return await call_tool(name, args, metadata)
    except asyncio.CancelledError:
        raise
    except ModelRetry as e:
        if ctx.retry < ctx.max_retries:
            raise
        logger.warning(
            "MCP tool %s exhausted its %d retries; degrading to a reported failure",
            name, ctx.max_retries,
        )
        return (
            f"Tool {name!r} failed after {ctx.retry + 1} attempts: {mask_text(e.message)} "
            f"Treat this source as unavailable, record it as not checked with "
            f"this reason, and continue with the other sources."
        )
    except Exception as e:  # noqa: BLE001 — a broken server must not end the run
        # A sign-in requirement is not a broken server: the delegation tool
        # in the registry turns it into a sign-in link (or a wait for the
        # user), so it must propagate rather than be reported as a failure.
        from agents.oauth2 import OAuthAuthorizationRequired

        if isinstance(e, OAuthAuthorizationRequired):
            raise
        logger.warning("MCP tool %s failed: %s", name, e, exc_info=True)
        return (
            # The text goes to the model, the tool card and the stored run
            # activity: an httpx error embeds the URL as sent, credentials in
            # its query included, so it is masked (agents/errors.py).
            f"Tool {name!r} failed: {describe_exception(e)} "
            f"Treat this source as unavailable, record it as not checked with "
            f"this reason, and continue with the other sources."
        )


class PerRunMCPServer(MCPServerStreamableHTTP):
    """An MCP server connection that opens a session of its own for every run.

    The registry builds each server once and every user's runs share it. A plain
    ``MCPServerStreamableHTTP`` reuses an open session while any run holds it,
    and the mcp transport sends each request from a task spawned by whoever
    opened that session -- so the auth reads *that* run's ``current_principal``
    / ``current_jwt``. While Alice's run held the session, Bob's tool calls went
    out with Alice's token, and ARC-1 ran them in SAP as Alice.

    ``for_run`` gives each run a fresh copy, entered in the run's own context.
    The copies share this object's httpx client and connection pool: the auth
    resolves the caller per request, so the client itself holds nobody's
    identity.
    """

    async def for_run(self, ctx):
        run = copy.copy(self)
        run.__post_init__()  # its own session: lock, running count, caches
        return run


def mcp_endpoint_url(base_url: str) -> str:
    """The MCP endpoint for a configured base URL.

    A bare host gets ``/mcp`` appended: that is where MCP servers conventionally
    listen and admins routinely configure only the host. A URL that already
    carries a path is taken as final -- servers publishing a versioned or nested
    endpoint must not have ``/mcp`` bolted on. Google's Gmail MCP server lives at
    ``/mcp/v1``; appending would produce ``/mcp/v1/mcp``, which 404s.

    ``normalize_mcp_url`` in agents.oauth2 delegates here so the endpoint and the
    token-storage key can never disagree. They used to be separate copies of the
    same rule; drift would store tokens under a key the live connection never
    looks up, and every request would silently reauthenticate.
    """
    base_url = base_url.rstrip("/")
    return base_url if urlparse(base_url).path else f"{base_url}/mcp"


def safe_server_key(url: str) -> str:
    """``url`` as ``scheme://host[:port]/path`` for log and error texts.

    The configured URL of a destination-mode server is not host-checked and
    may carry userinfo, a query (``?token=``) or a fragment; error messages
    (``DestinationUserRequired`` names its server) must never echo them."""
    parts = urlparse(url or "")
    if not parts.scheme or not parts.hostname:
        return (url or "").split("?", 1)[0].split("#", 1)[0]
    host = parts.hostname
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port else host
    return f"{parts.scheme}://{netloc}{parts.path}"


def _destination_mcp_server(
    mcp_url: str,
    oauth: dict,
    *,
    tool_prefix: str | None,
    max_retries: int,
) -> "PerRunMCPServer":
    """An MCP server reached through a BTP destination.

    The destination names the host and supplies the credential; the
    configured URL contributes only its path. Every request is built against
    ``PLACEHOLDER_BASE`` and ``DestinationAuth`` rewrites it onto the
    destination per request -- as the signed-in user when
    ``user_context`` is true (their JWT goes to the destination service as
    ``X-user-token``; no JWT bound raises ``DestinationUserRequired`` rather
    than falling back to an app credential).

    ``expected_hosts`` stays empty on purpose: the credential may go only to
    the host the destination names, not to the configured URL's host (which
    the admin validator does not host-check in this mode). Redirects are not
    followed, so a 3xx cannot carry the credential elsewhere either. The
    client holds nobody's identity -- the auth reads the caller per request --
    so ``PerRunMCPServer`` keeps runs of different users apart as before.
    """
    from agents.destination_auth import (
        PLACEHOLDER_BASE,
        DestinationAuth,
        resolver_for,
        user_context_of,
    )

    server_key = safe_server_key(mcp_url)
    resolver = resolver_for(oauth, server_key)
    path = urlparse(mcp_url).path or "/mcp"
    return PerRunMCPServer(
        url=f"{PLACEHOLDER_BASE}{path}",
        tool_prefix=tool_prefix,
        max_retries=max_retries,
        process_tool_call=_resilient_tool_call,
        http_client=httpx.AsyncClient(
            auth=DestinationAuth(
                resolver,
                user_context=user_context_of(oauth),
                expected_hosts=(),
                server_key=server_key,
            ),
            follow_redirects=False,
            timeout=httpx.Timeout(30.0),
        ),
    )


def create_mcp_server(
    name: str,
    base_url: str,
    auth_mode: str = "jwt",
    tool_prefix: str | None = None,
    oauth: dict | None = None,
    max_retries: int = MCP_TOOL_RETRIES,
) -> MCPServerStreamableHTTP:
    """Create an MCP server connection.

    auth_mode:
      - "jwt" (default): JWT forwarding on CF; OAuth2 authorization_code
        with browser redirect locally. Use for BTP-hosted MCP servers that
        trust this app's XSUAA.
      - "oauth2": per-user OAuth2 authorization_code against the server's own
        authorization server (e.g. a separate XSUAA). `oauth` carries the
        client config. Each user authorizes once; tokens are stored per user.
      - "app_only": app-only OAuth2 (client_credentials grant). The agent
        authenticates as
        itself, so there is nobody to sign in and nothing stored per user —
        which is what makes unattended, scheduled runs work. The access it
        gets is whatever was granted to the registration, not to a person.
      - "none": no authentication. Use for public MCP servers.
      - "destination": a BTP destination holds the server's URL and
        credential (`oauth` = `{destination, user_context?}`); see
        `_destination_mcp_server`.

    tool_prefix: when set, all tools from this server are exposed as
    `{tool_prefix}_{tool_name}`. Use to disambiguate when a single agent
    binds multiple MCP servers that share tool names.
    """
    mcp_url = mcp_endpoint_url(base_url)

    auth: httpx.Auth | None
    if auth_mode == "none":
        auth = None
    elif auth_mode == "oauth2":
        from agents.oauth2 import PerUserOAuth2Auth

        if not oauth:
            raise ValueError(f"oauth2 server {name!r} is missing its oauth config")
        auth = PerUserOAuth2Auth(server_key=mcp_url, spec_oauth=oauth)
    elif auth_mode == "app_only":
        from agents.client_credentials import ClientCredentialsAuth, config_from_oauth

        if not oauth:
            raise ValueError(
                f"client_credentials server {name!r} is missing its oauth config"
            )
        auth = ClientCredentialsAuth(
            server_key=mcp_url, config=config_from_oauth(oauth)
        )
    elif auth_mode == "destination":
        return _destination_mcp_server(
            mcp_url, oauth or {}, tool_prefix=tool_prefix, max_retries=max_retries
        )
    elif ON_CF:
        auth = JWTForwardAuth()
    else:
        # OAuthClientProvider discovers endpoints from the server root, not /mcp.
        oauth_base = mcp_url[: -len("/mcp")]
        auth = OAuthClientProvider(
            server_url=oauth_base,
            client_metadata=OAuthClientMetadata(
                client_name=f"SAP BTP Agent - {name}",
                redirect_uris=[AnyUrl(CALLBACK_URL)],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
            ),
            storage=FileTokenStorage(Path(f".tokens-{name}.json")),
            redirect_handler=_redirect_handler,
            callback_handler=_callback_handler,
        )

    return PerRunMCPServer(
        url=mcp_url,
        tool_prefix=tool_prefix,
        max_retries=max_retries,
        process_tool_call=_resilient_tool_call,
        http_client=httpx.AsyncClient(
            auth=auth,
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
        ),
    )


# ---------------------------------------------------------------------------
# SAP AI Core LLM model
# ---------------------------------------------------------------------------
class SAPAICoreModel(OpenAIChatModel):
    """OpenAI-compatible model adapted for SAP AI Core compatibility.

    Strips stream_options and cleans MCP tool schemas that contain
    non-standard fields SAP AI Core rejects ($schema, typeless props, etc).
    """

    def _get_stream_options(self, model_settings: OpenAIChatModelSettings):
        return OMIT

    def _get_tools(self, model_request_parameters):
        tools = super()._get_tools(model_request_parameters)
        return [self._clean_tool(t) for t in tools]

    @staticmethod
    def _clean_tool(tool: dict) -> dict:
        tool = copy.deepcopy(tool)
        params = tool.get("function", {}).get("parameters", {})
        SAPAICoreModel._clean_schema(params)
        return tool

    @staticmethod
    def _clean_schema(schema: dict) -> None:
        schema.pop("$schema", None)
        for prop in schema.get("properties", {}).values():
            if "type" not in prop:
                prop["type"] = "string"
            if prop.get("additionalProperties") == {}:
                del prop["additionalProperties"]
            SAPAICoreModel._clean_schema(prop)


DEFAULT_AVAILABLE_MODELS = (
    "gpt-4o,gpt-4o-mini,gpt-35-turbo,"
    "anthropic--claude-4.6-opus,anthropic--claude-4-sonnet,"
    "anthropic--claude-3.7-sonnet"
)


def _discover_deployed_models() -> list[str]:
    """Query SAP AI Core for the model names that are actually deployed.

    Returns an empty list if discovery fails (no credentials, network error,
    etc.) so the caller can fall back to the static default list.
    """
    try:
        from gen_ai_hub.proxy import get_proxy_client

        client = get_proxy_client("gen-ai-hub")
        names = {d.model_name for d in client.deployments if d.model_name}
        return sorted(names)
    except Exception:
        logger.warning("Could not discover deployed models from AI Core", exc_info=True)
        return []


def available_models() -> list[str]:
    """Models offered in the admin UI and chat dropdown.

    Resolution order:
      1. `AICORE_AVAILABLE_MODELS` env var (explicit override, comma-separated)
      2. Live query of SAP AI Core for deployed models
      3. `DEFAULT_AVAILABLE_MODELS` as a last-resort fallback
    """
    raw = os.environ.get("AICORE_AVAILABLE_MODELS")
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()]
    discovered = _discover_deployed_models()
    if discovered:
        return discovered
    return [m.strip() for m in DEFAULT_AVAILABLE_MODELS.split(",") if m.strip()]


def default_model_name() -> str:
    return os.environ.get("AICORE_MODEL", "gpt-4o")


_models: dict[str, Any] = {}


def _is_anthropic(name: str) -> bool:
    return name.startswith("anthropic") or "claude" in name.lower()


# Retries of one model request by the openai SDK (429, 408, 409, 5xx and
# connection errors). The SDK's own default is 2, about 1.5 s of backoff in
# total, which a rate limit counted per minute outlasts: a deep agent's
# parallel sub-agents share one deployment and then fail the whole run. The
# backoff doubles from 0.5 s and is capped at 8 s, so 8 retries wait about
# 40 s (less jitter), or as long as a Retry-After of at most 60 s asks.
AICORE_MAX_RETRIES_DEFAULT = 8


def _max_retries() -> int:
    """``AICORE_MAX_RETRIES``, read when a model is built; 0 means no retries.

    A value that is not a non-negative integer is the default, with a warning
    that names the variable but not its value: a typo must not keep a model
    from being built (the same rule as ``agents.ide.store.env_int``, which
    this module cannot import).
    """
    raw = (os.environ.get("AICORE_MAX_RETRIES") or "").strip()
    if not raw:
        return AICORE_MAX_RETRIES_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        logger.warning(
            "AICORE_MAX_RETRIES is not a non-negative integer; using the default %d",
            AICORE_MAX_RETRIES_DEFAULT,
        )
        return AICORE_MAX_RETRIES_DEFAULT
    return value


def _build_openai_model(name: str) -> SAPAICoreModel:
    from gen_ai_hub.proxy import get_proxy_client
    from gen_ai_hub.proxy.native.openai import AsyncOpenAI

    proxy_client = get_proxy_client("gen-ai-hub")
    sap_openai_client = AsyncOpenAI(
        proxy_client=proxy_client, max_retries=_max_retries()
    )

    return SAPAICoreModel(
        name,
        provider=OpenAIProvider(openai_client=sap_openai_client),
        profile=OpenAIModelProfile(
            openai_supports_strict_tool_definition=False,
        ),
    )


def _build_bedrock_model(name: str):
    """Wrap a SAP AI Core Bedrock-hosted Claude deployment as a pydantic-ai model.

    Bedrock calls are synchronous (boto3) and run in a worker thread, so asyncio
    cancellation — when a chat client disconnects or a specialist run hits its
    timeout — cannot interrupt an in-flight call; it would keep running in the
    background. Bound each call with explicit connect/read timeouts so a stuck
    LLM call dies within a known window (≤ read_timeout) instead of lingering.

    Retries are kept modest (``max_attempts`` is *total* attempts incl. the
    first, so 3 = 2 retries) so transient Bedrock throttling / 5xx recover
    instead of failing the whole turn, while the read timeout plus the 180s
    specialist timeout still bound the worst case. All tunable via env.
    """
    from botocore.config import Config
    from gen_ai_hub.proxy.native.amazon.clients import Session
    from pydantic_ai.models.bedrock import BedrockConverseModel
    from pydantic_ai.providers.bedrock import BedrockProvider

    session = Session()
    bedrock_client = session.client(
        model_name=name,
        config=Config(
            connect_timeout=float(os.environ.get("BEDROCK_CONNECT_TIMEOUT", "10")),
            read_timeout=float(os.environ.get("BEDROCK_READ_TIMEOUT", "120")),
            retries={
                "max_attempts": int(os.environ.get("BEDROCK_MAX_ATTEMPTS", "3")),
                "mode": "standard",
            },
        ),
    )
    return BedrockConverseModel(
        name, provider=BedrockProvider(bedrock_client=bedrock_client)
    )


def get_model(name: str | None = None):
    """Return a Pydantic AI model instance for the given deployment name.

    Names containing "claude" or starting with "anthropic" are routed to
    SAP AI Core's Bedrock proxy; everything else uses the OpenAI-compatible
    proxy. Instances are cached per name for the process lifetime.
    """
    name = (name or default_model_name()).strip()
    cached = _models.get(name)
    if cached is not None:
        return cached
    model = _build_bedrock_model(name) if _is_anthropic(name) else _build_openai_model(name)
    _models[name] = model
    return model
