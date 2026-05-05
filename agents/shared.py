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
from pydantic_ai.mcp import MCPServerStreamableHTTP
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

from agents.auth import current_jwt

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
def create_mcp_server(
    name: str,
    base_url: str,
    auth_mode: str = "jwt",
    tool_prefix: str | None = None,
) -> MCPServerStreamableHTTP:
    """Create an MCP server connection.

    auth_mode:
      - "jwt" (default): JWT forwarding on CF; OAuth2 authorization_code
        with browser redirect locally. Use for BTP-hosted MCP servers.
      - "none": no authentication. Use for public MCP servers.

    tool_prefix: when set, all tools from this server are exposed as
    `{tool_prefix}_{tool_name}`. Use to disambiguate when a single agent
    binds multiple MCP servers that share tool names.
    """
    base_url = base_url.rstrip("/")
    # Accept URLs both with and without the `/mcp` suffix. The configured
    # URL is normalized to include exactly one `/mcp` at the end.
    mcp_url = base_url if base_url.endswith("/mcp") else f"{base_url}/mcp"

    auth: httpx.Auth | None
    if auth_mode == "none":
        auth = None
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

    return MCPServerStreamableHTTP(
        url=mcp_url,
        tool_prefix=tool_prefix,
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


def _build_openai_model(name: str) -> SAPAICoreModel:
    from gen_ai_hub.proxy import get_proxy_client
    from gen_ai_hub.proxy.native.openai import AsyncOpenAI

    proxy_client = get_proxy_client("gen-ai-hub")
    sap_openai_client = AsyncOpenAI(proxy_client=proxy_client)

    return SAPAICoreModel(
        name,
        provider=OpenAIProvider(openai_client=sap_openai_client),
        profile=OpenAIModelProfile(
            openai_supports_strict_tool_definition=False,
        ),
    )


def _build_bedrock_model(name: str):
    """Wrap a SAP AI Core Bedrock-hosted Claude deployment as a pydantic-ai model."""
    from gen_ai_hub.proxy.native.amazon.clients import Session
    from pydantic_ai.models.bedrock import BedrockConverseModel
    from pydantic_ai.providers.bedrock import BedrockProvider

    session = Session()
    bedrock_client = session.client(model_name=name)
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
