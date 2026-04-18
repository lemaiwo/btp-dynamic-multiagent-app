"""SAP BTP Management — Dynamic Multi-Agent Application.

Combines:
- A FastAPI admin UI (XSUAA-secured) for CRUD on agent configurations,
  import/export, and registry reload.
- A pydantic-ai chat web UI, served from a dynamic wrapper that is
  refreshed whenever the agent registry is reloaded.
- JWT-forwarding middleware that binds the incoming user token to a
  contextvar, so MCP servers receive the user's identity on each call.

Start with:
    python app.py
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

# Import after load_dotenv so SAP AI Core & XSUAA env vars are available.
from fastapi import FastAPI  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from agents.a2a import router as a2a_router  # noqa: E402
from agents.admin import router as admin_router, seed_from_file_if_empty  # noqa: E402
from agents.auth import current_jwt  # noqa: E402
from agents.chat_app import dynamic_chat_app  # noqa: E402
from agents.db import init_db  # noqa: E402
from agents.registry import registry  # noqa: E402

SEED_FILE = Path(__file__).resolve().parent / "agents.seed.json"


# ---------------------------------------------------------------------------
# Lifespan: init DB, seed if empty, build initial registry
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await seed_from_file_if_empty(SEED_FILE)
    await registry.reload()
    dynamic_chat_app.refresh()
    logger.info("Application startup complete")
    yield


# ---------------------------------------------------------------------------
# Middleware: bind JWT to contextvar for MCP forwarding.
#
# This is a pure-ASGI middleware (not Starlette's BaseHTTPMiddleware) because
# BaseHTTPMiddleware runs the endpoint in a separate task whose context is
# captured at call_next() time — for streaming responses (as pydantic-ai's
# chat uses) the middleware's `finally` can reset the contextvar before the
# body has finished streaming, making the bound JWT invisible to downstream
# MCP calls made during streaming.
# ---------------------------------------------------------------------------
ON_CF = "VCAP_APPLICATION" in os.environ


class JWTBindingMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        token: str | None = None
        for key, value in scope.get("headers", []):
            if key.lower() == b"authorization":
                parts = value.decode("latin-1").split(None, 1)
                if len(parts) == 2 and parts[0].lower() == "bearer":
                    token = parts[1].strip()
                break

        # On Cloud Foundry, any API request other than /healthz must come
        # through the approuter (which injects the user JWT). If there is no
        # token on a request that needs it, fail fast with a clear message
        # instead of letting the chat silently lose MCP authentication.
        path = scope.get("path", "")
        # /.well-known/agent-card.json is anonymously readable (Joule
        # and other A2A clients fetch it before authenticating). Admin,
        # A2A JSON-RPC and the chat UI still require a forwarded JWT.
        is_public = (
            path == "/healthz"
            or path.startswith("/.well-known/")
            or path.startswith("/static/")
        )
        needs_jwt = ON_CF and not is_public and not path.startswith("/admin")
        if needs_jwt and not token:
            logger.warning(
                "Rejecting %s %s: no JWT — did you hit the approuter URL?",
                scope.get("method"), path,
            )
            await _send_json(
                send,
                401,
                {
                    "detail": "Missing bearer token. This app must be accessed "
                    "through its approuter URL so the user JWT is forwarded."
                },
            )
            return

        if token:
            logger.info("JWT bound for %s %s", scope.get("method"), path)
        marker = current_jwt.set(token)
        try:
            await self.app(scope, receive, send)
        finally:
            current_jwt.reset(marker)


async def _send_json(send, status_code: int, body: dict) -> None:
    import json as _json
    payload = _json.dumps(body).encode()
    await send({
        "type": "http.response.start",
        "status": status_code,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": payload})


# ---------------------------------------------------------------------------
# FastAPI app assembly
# ---------------------------------------------------------------------------
app = FastAPI(title="SAP BTP Multi-Agent", lifespan=lifespan)
app.add_middleware(JWTBindingMiddleware)
app.include_router(admin_router)
# A2A (Agent-to-Agent) protocol — exposes the orchestrator to SAP Joule
# and other A2A-capable clients. Must be included before the catch-all
# chat mount so /.well-known/agent-card.json and /a2a resolve here.
app.include_router(a2a_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# Serve branding assets (logo, favicon) referenced by templates/chat.html.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# Mount the dynamic chat UI at /. The pydantic-ai chat UI uses absolute
# paths for its API (e.g. /api/configure), so it must be served from the
# root. Admin routes are registered above with prefix /admin and take
# precedence over the chat mount.
app.mount("/", dynamic_chat_app)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 7932))
    print(f"Starting SAP BTP Management app on http://127.0.0.1:{port}")
    print(f"  Chat:  http://127.0.0.1:{port}/")
    print(f"  Admin: http://127.0.0.1:{port}/admin")
    uvicorn.run(app, host="0.0.0.0", port=port)
