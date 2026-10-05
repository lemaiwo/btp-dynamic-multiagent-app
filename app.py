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
    InvalidToken,
    authenticate,
    current_base_url,
    current_claims,
    current_jwt,
    current_principal,
    get_validator,
    public_base_url,
)
from agents.chat_app import dynamic_chat_app  # noqa: E402
from agents.db import (  # noqa: E402
    SessionLocal,
    init_db,
    sweep_stale_runs,
    sweep_stale_workflow_runs,
)
from agents.ide import approvals as ide_approvals  # noqa: E402
from agents.ide import runner as ide_runner  # noqa: E402
from agents.ide.review_routes import router as ide_review_router  # noqa: E402
from agents.ide.routes import install_validation_handler  # noqa: E402
from agents.ide.routes import router as ide_router  # noqa: E402
from agents.ide.seed import ensure_ide_seed  # noqa: E402
from agents.ide.store import (  # noqa: E402
    APPROVAL_TTL_MIN,
    IDE_AUDIT_RETENTION_DAYS,
    IDE_DIAGNOSE_RETENTION_DAYS,
    IDE_SESSION_RETENTION_DAYS,
    expire_pending_approvals,
    purge_audit_older_than,
    purge_sessions_older_than,
    reset_running_ide_sessions,
)
from agents.job_runner import cancel_all_runs  # noqa: E402
from agents.oauth2 import refresh_scheduled_tokens  # noqa: E402
from agents.oauth_routes import router as oauth_router  # noqa: E402
from agents.registry import registry  # noqa: E402
from agents.workflow_runner import cancel_all_workflow_runs  # noqa: E402

SEED_FILE = Path(__file__).resolve().parent / "agents.seed.json"
IDE_SEED_FILE = Path(__file__).resolve().parent / "agents" / "ide" / "seed.ide.json"


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


async def _purge_ide_sessions() -> int:
    """One retention pass; returns the number of sessions deleted.

    Change sessions age from their last activity, diagnose sessions from
    creation (a hard data limit), the audit log has its own clock, stale
    pending approvals are expired and approvals left without an outcome are
    closed. Each pass has its own 0-disables switch,
    its own DB session and its own error handling, so one failing pass cannot
    skip the others.
    """
    purged = 0

    async def _pass(label: str, enabled: bool, fn) -> int:
        if not enabled:
            return 0
        try:
            async with SessionLocal() as session:
                n = await fn(session)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("IDE retention pass '%s' failed", label, exc_info=True)
            return 0
        if n:
            logger.info("IDE retention: %s: %d", label, n)
        return n

    purged += await _pass(
        "change sessions purged",
        IDE_SESSION_RETENTION_DAYS >= 1,
        lambda s: purge_sessions_older_than(s, IDE_SESSION_RETENTION_DAYS),
    )
    purged += await _pass(
        "diagnose sessions purged",
        IDE_DIAGNOSE_RETENTION_DAYS >= 1,
        lambda s: purge_sessions_older_than(
            s, IDE_DIAGNOSE_RETENTION_DAYS, session_type="diagnose", by="created_at"
        ),
    )
    await _pass(
        "audit rows purged",
        IDE_AUDIT_RETENTION_DAYS >= 1,
        lambda s: purge_audit_older_than(s, IDE_AUDIT_RETENTION_DAYS),
    )
    await _pass(
        "approvals expired",
        APPROVAL_TTL_MIN >= 1,
        lambda s: expire_pending_approvals(s, older_than_min=APPROVAL_TTL_MIN),
    )
    # Approved, but the ARC-1 call never reported back (a crash or redeploy
    # mid-call): closed as failed/interrupted with a trace_failed audit row.
    # Always on -- it only touches rows older than the arm timeout.
    await _pass(
        "interrupted approvals closed",
        True,
        ide_approvals.sweep_interrupted,
    )
    return purged


async def _ide_purge_loop(interval: float) -> None:
    """Purge old IDE sessions every ``interval`` seconds (the startup pass is
    run by the lifespan itself)."""
    while True:
        await asyncio.sleep(interval)
        try:
            n = await _purge_ide_sessions()
            if n:
                logger.info("Purged %d old IDE session(s)", n)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("IDE session purge failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail closed before anything is served: with AUTH_REQUIRED on (the
    # default on CF) and no XSUAA binding this raises AuthConfigurationError
    # and the process never comes up, instead of answering every route to
    # anyone. See the agents.auth module docstring.
    get_validator()
    await init_db()
    # A process that has just started owns no run, so every row still marked
    # `running` is a ghost from the previous process (crash, or a CF redeploy
    # mid-run) — sweep them all, not just the ones past their timeout.
    async with SessionLocal() as session:
        swept = await sweep_stale_runs(session, all_running=True)
        swept_wf = await sweep_stale_workflow_runs(session, all_running=True)
        # Only IDE rows past the ghost age: a fresh one may be another
        # instance's live run (its heartbeat keeps it fresh).
        ide_reset = await reset_running_ide_sessions(
            session, min_age_s=ide_runner.ghost_age()
        )
    if ide_reset:
        logger.info("Reset %d ghost IDE session(s) to idle", ide_reset)
    if IDE_DIAGNOSE_RETENTION_DAYS < 1:
        # Allowed, but nobody should find out by accident: diagnose sessions
        # store raw dump and trace text of their (non-production) target.
        logger.warning(
            "IDE_DIAGNOSE_RETENTION_DAYS is %d: diagnose sessions are never "
            "purged, so the raw dump and trace text they store is kept until "
            "each session is deleted by hand",
            IDE_DIAGNOSE_RETENTION_DAYS,
        )
    try:
        purged = await _purge_ide_sessions()
        if purged:
            logger.info("Purged %d old IDE session(s) at startup", purged)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        # Housekeeping must never keep the app from starting.
        logger.warning("IDE startup purge failed", exc_info=True)
    if swept:
        logger.info("Marked %d ghost job run(s) as interrupted", swept)
    if swept_wf:
        logger.info("Swept %d stale workflow run(s) at startup", swept_wf)
    await seed_from_file_if_empty(SEED_FILE)
    await ensure_ide_seed(IDE_SEED_FILE)
    await registry.reload()
    dynamic_chat_app.refresh()
    heartbeat: asyncio.Task | None = None
    if DB_HEARTBEAT_SECONDS > 0:
        heartbeat = asyncio.create_task(_db_heartbeat(DB_HEARTBEAT_SECONDS))
    keepwarm: asyncio.Task | None = None
    if TOKEN_KEEPWARM_SECONDS > 0:
        keepwarm = asyncio.create_task(_token_keepwarm(TOKEN_KEEPWARM_SECONDS))
    ide_purge = asyncio.create_task(_ide_purge_loop(86400))
    logger.info("Application startup complete")
    yield
    # Shutdown: cancel in-flight runs so each finalizes as `interrupted`
    # rather than being killed mid-await and leaving its row `running`.
    for task in (heartbeat, keepwarm, ide_purge):
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    await cancel_all_runs()
    await cancel_all_workflow_runs()
    await ide_runner.cancel_all()
    # An approved trace whose ARC-1 call is in flight runs in its own task,
    # not in a run: let it record its outcome (armed / failed) rather than
    # leave the approval `approved` with nothing behind it. Each call is
    # bounded by ARM_TIMEOUT_S, so this wait is too.
    try:
        await asyncio.wait_for(
            ide_approvals.drain(), ide_approvals.ARM_TIMEOUT_S + 5
        )
    except TimeoutError:
        logger.warning(
            "IDE approval call(s) still running at shutdown; the next start "
            "closes them as interrupted"
        )
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

_STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_warned_no_public_url = False


def _origin_of(url: str) -> str:
    """``scheme://host[:port]`` of a URL, lowercased, for origin comparison."""
    parts = url.strip().lower().split("/")
    if len(parts) >= 3 and parts[1] == "":
        return f"{parts[0]}//{parts[2]}"
    return url.strip().lower()


class JWTBindingMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        wanted = (
            b"authorization", b"x-forwarded-proto", b"x-forwarded-host", b"host",
            b"origin", b"sec-fetch-site",
        )
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
        method = scope.get("method", "GET")
        # /.well-known/agent-card.json is anonymously readable (Joule
        # and other A2A clients fetch it before authenticating). Admin,
        # A2A JSON-RPC and the chat UI still require a forwarded JWT.
        is_public = (
            path == "/healthz"
            or path.startswith("/.well-known/")
            or path.startswith("/static/")
        )
        is_admin = path == "/admin" or path.startswith("/admin/")
        # /admin is left to require_admin for the *missing* token case so its
        # own 401 is what the UI sees; a token that is present but invalid is
        # rejected here like everywhere else.
        needs_jwt = ON_CF and not is_public and not is_admin
        if needs_jwt and not token:
            logger.warning(
                "Rejecting %s %s: no JWT — did you hit the approuter URL?",
                method, path,
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

        # Forwarded scheme/host as the approuter reports them.
        fwd_origin: str | None = None
        host = hdrs.get(b"x-forwarded-host") or hdrs.get(b"host")
        if host:
            proto_h = hdrs.get(b"x-forwarded-proto")
            scheme = (
                proto_h.decode("latin-1").split(",")[0].strip()
                if proto_h
                else scope.get("scheme", "https")
            )
            host_str = host.decode("latin-1").split(",")[0].strip()
            fwd_origin = f"{scheme}://{host_str}"

        # Public base URL (scheme://host) used to build the OAuth2
        # redirect_uri (PUBLIC_BASE_URL / A2A_PUBLIC_URL — the same resolver
        # scheduled runs use). Locally it falls back to the forwarded headers.
        # On CF it does not: the value ends up as a registered redirect_uri
        # and, for DCR servers, re-registers the shared client whenever it
        # changes, so a client-controlled header must not feed it. mta.yaml
        # wires PUBLIC_BASE_URL from the approuter route, so on CF it is only
        # empty when a deployment overrides that with nothing.
        base_url = public_base_url() or ""
        if not base_url:
            if ON_CF:
                global _warned_no_public_url
                if not _warned_no_public_url:
                    _warned_no_public_url = True
                    logger.warning(
                        "PUBLIC_BASE_URL/A2A_PUBLIC_URL is not set; refusing to "
                        "derive the public URL from X-Forwarded-Host on Cloud "
                        "Foundry. Per-user OAuth2 sign-in will not work until "
                        "it is configured."
                    )
            elif fwd_origin:
                base_url = fwd_origin

        # CSRF guard for the admin API. The approuter routes run with
        # csrfProtection off, so a state-changing admin request that the
        # browser attributes to another site is refused here. Browsers send
        # Origin on every cross-site POST/PUT/PATCH/DELETE; server-to-server
        # callers (the job scheduler on /api/*/run) send none and are not
        # affected, and /admin is the only prefix checked.
        if is_admin and method in _STATE_CHANGING:
            fetch_site = (hdrs.get(b"sec-fetch-site") or b"").decode("latin-1").strip().lower()
            origin_h = hdrs.get(b"origin")
            if fetch_site not in ("same-origin", "none") and origin_h:
                origin = _origin_of(origin_h.decode("latin-1"))
                allowed = {
                    _origin_of(u) for u in (base_url, public_base_url() or "", fwd_origin or "") if u
                }
                if origin not in allowed:
                    logger.warning(
                        "Rejecting %s %s: Origin %r is not this app (%s)",
                        method, path, origin, ", ".join(sorted(allowed)) or "-",
                    )
                    await _send_json(
                        send, 403,
                        {"detail": "Cross-site request refused: Origin does not match this app."},
                    )
                    return

        # Validate the token once; the dependencies reuse the claims.
        try:
            claims, principal = await authenticate(token)
        except InvalidToken as e:
            if not is_public:
                logger.warning("Rejecting %s %s: %s", method, path, e)
                await _send_json(send, 401, {"detail": f"Invalid bearer token: {e}"})
                return
            # Anonymous paths (agent card, static assets) serve without an
            # identity; a stale token there is dropped, not fatal.
            claims, principal = None, None
        if ON_CF and not is_public and token and principal is None:
            # Validated (or unverifiable in an open deployment) but carries no
            # usable identity: nothing downstream could key a user on it.
            logger.warning("Rejecting %s %s: token yields no principal", method, path)
            await _send_json(send, 401, {"detail": "Bearer token carries no user identity."})
            return

        if token:
            logger.info("JWT bound for %s %s", method, path)

        marker = current_jwt.set(token)
        marker_principal = current_principal.set(principal)
        marker_claims = current_claims.set(claims)
        marker_base = current_base_url.set(base_url)
        try:
            await self.app(scope, receive, send)
        finally:
            current_jwt.reset(marker)
            current_principal.reset(marker_principal)
            current_claims.reset(marker_claims)
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
# ABAP IDE API (/ide/api/...), developer scope; before the chat mount.
# The two routers share no path, so their order does not matter.
app.include_router(ide_review_router)
app.include_router(ide_router)
# A refused IDE request is a 422 that never echoes the input (a lone
# surrogate in it made FastAPI's own 422 a 500).
install_validation_handler(app)


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
