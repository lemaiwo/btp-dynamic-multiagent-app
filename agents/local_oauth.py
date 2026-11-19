"""Browser OAuth for an MCP server when the app runs locally (not on CF).

``create_mcp_server`` uses these when there is no JWT to forward: tokens and
the dynamic client registration are kept in a JSON file per server, and the
authorization code comes back to a one-shot HTTP server on ``CALLBACK_PORT``.
Moved out of ``agents/shared.py`` unchanged to keep that module under its
size limit.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

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


async def redirect_handler(auth_url: str) -> None:
    print("\nOpening browser for OAuth2 authentication...")
    print(f"If the browser doesn't open, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)


async def callback_handler() -> tuple[str, str | None]:
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

