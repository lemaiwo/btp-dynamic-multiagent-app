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

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

# Import after load_dotenv so SAP AI Core & XSUAA env vars are available.
from fastapi import FastAPI  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from sqlalchemy import text  # noqa: E402

from agents.a2a import router as a2a_router  # noqa: E402
from agents.admin import router as admin_router, seed_from_file_if_empty  # noqa: E402
from agents.api_runs import router as runs_router  # noqa: E402
from agents.auth import (  # noqa: E402
    current_base_url,
    current_jwt,
    current_principal,
    principal_from_token,
    public_base_url,
)
from agents.chat_app import dynamic_chat_app  # noqa: E402
from agents.db import (  # noqa: E402
    SessionLocal,
    init_db,
    sweep_stale_runs,
    sweep_stale_workflow_runs,
)
from agents.job_runner import cancel_all_runs  # noqa: E402
from agents.oauth2 import refresh_scheduled_tokens  # noqa: E402
from agents.oauth_routes import router as oauth_router  # noqa: E402
from agents.registry import registry  # noqa: E402
from agents.workflow_runner import cancel_all_workflow_runs  # noqa: E402

SEED_FILE = Path(__file__).resolve().parent / "agents.seed.json"


# ---------------------------------------------------------------------------
# Lifespan: init DB, seed if empty, build initial registry
# ---------------------------------------------------------------------------
DB_HEARTBEAT_SECONDS = float(os.environ.get("DB_HEARTBEAT_SECONDS", "60"))


async def _db_heartbeat(interval: float) -> None:
    """Touch Postgres on a timer so no user pays for the pool going cold.

    `/healthz` answers from memory and the admin UI is idle most of the day,
    so without this the pool sits untouched for hours. Measured on ACC, the
    first DB-backed request after such a gap cost 8s (10 minutes idle) to 18s
    (overnight) while a concurrent request that opened no session answered in
    20ms. A `SELECT 1` a minute keeps a connection established, so that cost
    lands here instead of on whoever opens the admin UI next.

    QueuePool hands out connections FIFO, so successive beats rotate through
    the pool rather than refreshing one connection and letting the rest die.

    Never raises: a failed beat is logged and the next one retries. The pool
    is self-healing via `pool_pre_ping` regardless, so a heartbeat that cannot
    reach the database is a warning, not a reason to take the app down.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            async with SessionLocal() as session:
                await session.execute(text("SELECT 1"))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("Database heartbeat failed", exc_info=True)


TOKEN_KEEPWARM_SECONDS = float(os.environ.get("TOKEN_KEEPWARM_SECONDS", "21600"))


async def _token_keepwarm(interval: float) -> None:
    """Refresh the tokens scheduled runs need, before a run needs them.

    OAuth refresh is otherwise lazy: it happens only when an outbound MCP call
    finds an expired access token. Between nightly runs nothing calls anything,
    so the stored token simply ages -- and the first thing to notice is the
    03:12 run failing with nobody watching.

    Six hours by default: comfortably inside any sane access-token lifetime,
    and frequent enough that a refresh-rotating authorization server keeps
    re-issuing the refresh token. Set TOKEN_KEEPWARM_SECONDS=0 to disable.

    This cannot rescue a refresh token with a fixed absolute lifetime -- see
    refresh_scheduled_tokens. GET /admin/api/credential-health is what surfaces
    that case, and the admin UI raises it to the operator.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            counts = await refresh_scheduled_tokens()
            if counts["failed"]:
                logger.warning("Token keep-warm: %s", counts)
            elif counts["checked"]:
                logger.info("Token keep-warm: %s", counts)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("Token keep-warm pass failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # A process that has just started owns no run, so every row still marked
    # `running` is a ghost from the previous process (crash, or a CF redeploy
    # mid-run) — sweep them all, not just the ones past their timeout.
    async with SessionLocal() as session:
        swept = await sweep_stale_runs(session, all_running=True)
        swept_wf = await sweep_stale_workflow_runs(session, all_running=True)
    if swept:
        logger.info("Marked %d ghost job run(s) as interrupted", swept)
    if swept_wf:
        logger.info("Swept %d stale workflow run(s) at startup", swept_wf)
    await seed_from_file_if_empty(SEED_FILE)
    await registry.reload()
    dynamic_chat_app.refresh()
    heartbeat: asyncio.Task | None = None
    if DB_HEARTBEAT_SECONDS > 0:
        heartbeat = asyncio.create_task(_db_heartbeat(DB_HEARTBEAT_SECONDS))
    keepwarm: asyncio.Task | None = None
    if TOKEN_KEEPWARM_SECONDS > 0:
        keepwarm = asyncio.create_task(_token_keepwarm(TOKEN_KEEPWARM_SECONDS))
    logger.info("Application startup complete")
    yield
    # Shutdown: cancel in-flight runs so each finalizes as `interrupted`
    # rather than being killed mid-await and leaving its row `running`.
    for task in (heartbeat, keepwarm):
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    await cancel_all_runs()
    await cancel_all_workflow_runs()
    logger.info("Application shutdown complete")


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

        wanted = (b"authorization", b"x-forwarded-proto", b"x-forwarded-host", b"host")
        hdrs: dict[bytes, bytes] = {}
        for key, value in scope.get("headers", []):
            lk = key.lower()
            if lk in wanted and lk not in hdrs:
                hdrs[lk] = value

        token: str | None = None
        auth_header = hdrs.get(b"authorization")
        if auth_header:
            parts = auth_header.decode("latin-1").split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                token = parts[1].strip()

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

        # Public base URL (scheme://host) as seen by the approuter, used to
        # build the OAuth2 redirect_uri. An explicit override wins so the
        # redirect_uri exactly matches what is registered with the target.
        # Optional explicit public base URL for OAuth2 redirect_uri
        # construction (PUBLIC_BASE_URL / A2A_PUBLIC_URL — the same resolver
        # scheduled runs use), falling back to the forwarded headers.
        base_url = public_base_url() or ""
        if not base_url:
            host = hdrs.get(b"x-forwarded-host") or hdrs.get(b"host")
            if host:
                proto_h = hdrs.get(b"x-forwarded-proto")
                scheme = (
                    proto_h.decode("latin-1").split(",")[0].strip()
                    if proto_h
                    else scope.get("scheme", "https")
                )
                host_str = host.decode("latin-1").split(",")[0].strip()
                base_url = f"{scheme}://{host_str}"

        principal = principal_from_token(token)

        marker = current_jwt.set(token)
        marker_principal = current_principal.set(principal)
        marker_base = current_base_url.set(base_url)
        try:
            await self.app(scope, receive, send)
        finally:
            current_jwt.reset(marker)
            current_principal.reset(marker_principal)
            current_base_url.reset(marker_base)


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
# OAuth2 per-user authorization callback (auth_mode="oauth2"). Registered
# before the chat mount so /oauth/callback resolves here.
app.include_router(oauth_router)
# Scheduler-facing run endpoint (POST /api/agents/{slug}/run). Registered
# before the chat mount so it resolves here rather than falling through to
# the catch-all.
app.include_router(runs_router)


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
