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
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402

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
# Middleware: bind JWT to contextvar for MCP forwarding
# ---------------------------------------------------------------------------
class JWTBindingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        auth = request.headers.get("authorization") or request.headers.get("Authorization")
        token: str | None = None
        if auth:
            parts = auth.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                token = parts[1].strip()

        marker = current_jwt.set(token)
        try:
            return await call_next(request)
        finally:
            current_jwt.reset(marker)


# ---------------------------------------------------------------------------
# FastAPI app assembly
# ---------------------------------------------------------------------------
app = FastAPI(title="SAP BTP Multi-Agent", lifespan=lifespan)
app.add_middleware(JWTBindingMiddleware)
app.include_router(admin_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# Mount the dynamic chat UI at /chat and redirect / → /chat
app.mount("/chat", dynamic_chat_app)


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/chat")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 7932))
    print(f"Starting SAP BTP Management app on http://127.0.0.1:{port}")
    print(f"  Chat:  http://127.0.0.1:{port}/chat")
    print(f"  Admin: http://127.0.0.1:{port}/admin")
    uvicorn.run(app, host="0.0.0.0", port=port)
