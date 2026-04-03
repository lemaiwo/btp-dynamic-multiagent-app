"""Shared infrastructure for SAP BTP management agents.

Provides OAuth2 authentication, MCP server factory, and SAP AI Core model
setup used by all specialist agents.
"""

import asyncio
import copy
import json
import logging
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
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

logger = logging.getLogger(__name__)

ON_CF = "VCAP_APPLICATION" in os.environ

CALLBACK_PORT = 3000
CALLBACK_URL = f"http://localhost:{CALLBACK_PORT}/callback"


# ---------------------------------------------------------------------------
# Persistent token storage (file-based, one file per MCP server)
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
# OAuth2 callback handling (shared across all MCP connections)
# ---------------------------------------------------------------------------
_callback_future: asyncio.Future | None = None
_callback_loop: asyncio.AbstractEventLoop | None = None


class _CallbackHandler(BaseHTTPRequestHandler):
    """Tiny HTTP handler that captures the OAuth2 authorization code callback."""

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
        pass  # suppress noisy HTTP logs


async def _redirect_handler(auth_url: str) -> None:
    """Open the user's browser to the OAuth2 authorization page."""
    print(f"\nOpening browser for OAuth2 authentication...")
    print(f"If the browser doesn't open, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)


async def _callback_handler() -> tuple[str, str | None]:
    """Start a temporary local server and wait for the OAuth2 callback."""
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
# Client credentials auth for Cloud Foundry (no browser available)
# ---------------------------------------------------------------------------
class ClientCredentialsAuth(httpx.Auth):
    """OAuth2 client_credentials flow for server-to-server auth on CF.

    Discovers the OAuth server metadata, dynamically registers a client,
    and obtains tokens via client_credentials grant — no browser needed.
    """

    def __init__(self, server_url: str, client_name: str):
        self._server_url = server_url.rstrip("/")
        self._client_name = client_name
        self._access_token: str | None = None
        self._token_expiry: float = 0
        self._client_id: str | None = None
        self._client_secret: str | None = None
        self._token_endpoint: str | None = None
        self._lock = asyncio.Lock()

    async def _discover_and_register(self) -> None:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            resp = await client.get(
                f"{self._server_url}/.well-known/oauth-authorization-server"
            )
            resp.raise_for_status()
            metadata = resp.json()
            logger.debug("OAuth metadata: %s", metadata)

            # The MCP server's /oauth/token only proxies authorization_code.
            # For client_credentials we must talk to XSUAA directly.
            # The metadata 'issuer' points to the XSUAA base URL.
            issuer = metadata.get("issuer", "").rstrip("/")
            if issuer:
                self._token_endpoint = f"{issuer}/oauth/token"
            else:
                self._token_endpoint = metadata["token_endpoint"]

            logger.info("Using token endpoint: %s", self._token_endpoint)

            resp = await client.post(
                metadata["registration_endpoint"],
                json={
                    "client_name": self._client_name,
                    "grant_types": ["client_credentials"],
                    "response_types": [],
                    "token_endpoint_auth_method": "client_secret_post",
                },
            )
            resp.raise_for_status()
            reg = resp.json()
            self._client_id = reg["client_id"]
            self._client_secret = reg["client_secret"]
            logger.info("Registered OAuth client: %s", self._client_id)

    async def _ensure_token(self) -> str:
        async with self._lock:
            if self._access_token and time.time() < self._token_expiry:
                return self._access_token

            if not self._token_endpoint:
                await self._discover_and_register()

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    self._token_endpoint,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            self._access_token = data["access_token"]
            self._token_expiry = time.time() + data.get("expires_in", 3600) - 60
            return self._access_token

    async def async_auth_flow(self, request):
        token = await self._ensure_token()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request

        if response.status_code == 401:
            self._access_token = None
            token = await self._ensure_token()
            request.headers["Authorization"] = f"Bearer {token}"
            yield request


# ---------------------------------------------------------------------------
# MCP server factory
# ---------------------------------------------------------------------------
def create_mcp_server(name: str, base_url: str) -> MCPServerStreamableHTTP:
    """Create an MCP server connection with OAuth2 authentication.

    On Cloud Foundry: uses client_credentials grant (no browser needed).
    Locally: uses authorization_code grant with browser redirect.
    """
    if ON_CF:
        auth = ClientCredentialsAuth(
            server_url=base_url,
            client_name=f"SAP BTP Agent - {name}",
        )
    else:
        auth = OAuthClientProvider(
            server_url=base_url,
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
        url=f"{base_url}/mcp",
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
        """Remove non-standard JSON Schema fields that SAP AI Core rejects."""
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


_model: SAPAICoreModel | None = None


def get_model() -> SAPAICoreModel:
    """Return a shared SAP AI Core model instance (created once)."""
    global _model
    if _model is None:
        from gen_ai_hub.proxy import get_proxy_client
        from gen_ai_hub.proxy.native.openai import AsyncOpenAI

        proxy_client = get_proxy_client("gen-ai-hub")
        sap_openai_client = AsyncOpenAI(proxy_client=proxy_client)

        _model = SAPAICoreModel(
            "gpt-4o",
            provider=OpenAIProvider(openai_client=sap_openai_client),
            profile=OpenAIModelProfile(
                openai_supports_strict_tool_definition=False,
            ),
        )
    return _model
