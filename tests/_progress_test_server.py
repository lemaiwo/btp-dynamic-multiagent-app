"""Hermetic chat server for the live-progress Playwright tests.

Serves the *real* pydantic-ai chat UI and our custom ``/api/chat`` route
(``agents.chat_app.DynamicChatApp``), but backs it with deterministic agents
instead of SAP AI Core + MCP. This lets a real browser exercise the
streaming/progress path with zero external dependencies.

Two modes (selected by env var):

Default — *cards demo* (``test_chat_progress_ui.py``):
1. Orchestrator delegates to the ``demo`` specialist via ``delegate_demo``.
2. The specialist calls two tools (``step_one``/``step_two``); each call fires
   the progress handler, which renders a native tool card (Running -> Completed).
3. Orchestrator returns the final answer text.

``HEARTBEAT_DEMO=1`` — *heartbeat demo* (``test_chat_heartbeat_ui.py``):
The delegate tool sleeps ``HB_SLEEP`` seconds with no specialist tool calls,
creating a blank gap so the live "working…" heartbeat (an animated reasoning
block) must appear before the final answer.

``OAUTH_DEMO=1`` — *auto-continue demo* (``test_chat_oauth_autocontinue_ui.py``):
Wires the *real* delegation tool (``registry._attach_delegation_tool``) around a
specialist whose MCP tool raises ``OAuthAuthorizationRequired`` until a token
exists. A ``GET /test/authorize`` endpoint stores a token (simulating the popup
callback). Proves the chat shows a sign-in link, waits, then resumes by itself
once the token appears — no second user message. Uses a file-backed DB so the
token written by the endpoint is visible to the polling chat request.

``TestModel`` is used (not ``FunctionModel``) because the chat path streams
responses and ``TestModel`` supports streaming out of the box while still
calling every tool and emitting a deterministic final answer.

Run directly:  python tests/_progress_test_server.py [port]
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Local, dependency-free environment (no CF/XSUAA/AICORE/Postgres).
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from pydantic_ai import Agent, RunContext  # noqa: E402
from pydantic_ai.models.test import TestModel  # noqa: E402

from agents.chat_app import CHAT_HTML, dynamic_chat_app  # noqa: E402
from agents.registry import BuildResult, _make_progress_handler, registry  # noqa: E402

FINAL_ANSWER = "Done. The demo specialist reported 42."
HEARTBEAT_ANSWER = "Done after the slow step. The answer is 42."


def _build_cards_orchestrator():
    # Specialist calls both of its tools, then returns deterministic text.
    specialist = Agent(TestModel(custom_output_text="specialist result: 42"))

    @specialist.tool_plain
    def step_one() -> str:
        return "step one complete"

    @specialist.tool_plain
    def step_two() -> str:
        return "step two complete"

    orchestrator = Agent(TestModel(custom_output_text=FINAL_ANSWER))

    # PROGRESS_DISABLED is a test-only switch used by the UI test's negative
    # control to prove the assertions fail when progress is NOT forwarded.
    forward_progress = os.environ.get("PROGRESS_DISABLED") != "1"

    @orchestrator.tool
    async def delegate_demo(ctx: RunContext, query: str) -> str:
        handler = _make_progress_handler("demo") if forward_progress else None
        result = await specialist.run(
            query, usage=ctx.usage, event_stream_handler=handler
        )
        return str(result.output)

    return orchestrator, {"demo": specialist}


def _build_heartbeat_orchestrator():
    # A specialist with no tools: it just "thinks" (we simulate latency in the
    # delegate tool) and answers, so there are no tool cards to fill the gap —
    # only the heartbeat can.
    specialist = Agent(TestModel(custom_output_text="slow specialist result"))
    sleep_s = float(os.environ.get("HB_SLEEP", "4.0"))

    orchestrator = Agent(TestModel(custom_output_text=HEARTBEAT_ANSWER))

    @orchestrator.tool
    async def delegate_slow(ctx: RunContext, query: str) -> str:
        # Blank gap: no progress reports, just latency.
        await asyncio.sleep(sleep_s)
        result = await specialist.run(query, usage=ctx.usage)
        return str(result.output)

    return orchestrator, {"slow": specialist}


_OAUTH_USER = "testuser"
_OAUTH_SERVER_KEY = "testkey"


def _build_oauth_orchestrator():
    """Real delegation tool around a specialist that needs sign-in until a token
    exists, so the auto-continue (poll + retry) path is exercised end to end."""
    from types import SimpleNamespace

    from agents.auth import current_principal
    from agents.oauth2 import OAuthAuthorizationRequired, has_valid_token
    from agents.registry import _attach_delegation_tool

    specialist = Agent(TestModel(custom_output_text="ARC-1 reports: 42 open incidents"))

    @specialist.tool_plain
    async def query_arc1() -> str:
        uid = current_principal.get() or "anon"
        if not await has_valid_token(uid, _OAUTH_SERVER_KEY):
            raise OAuthAuthorizationRequired(_OAUTH_SERVER_KEY, reason="no-token")
        return "ARC-1 data: 42 open incidents"

    orchestrator = Agent(
        TestModel(custom_output_text="ARC-1 reports 42 open incidents.")
    )
    row = SimpleNamespace(name="arc1", description="ARC-1 incident specialist")
    _attach_delegation_tool(orchestrator, specialist, row)
    return orchestrator, {"arc1": specialist}


def build_app():
    if os.environ.get("OAUTH_DEMO") == "1":
        orchestrator, specialists = _build_oauth_orchestrator()
    elif os.environ.get("HEARTBEAT_DEMO") == "1":
        orchestrator, specialists = _build_heartbeat_orchestrator()
    else:
        orchestrator, specialists = _build_cards_orchestrator()

    # Install our scripted build and wire the chat app without touching AI Core
    # (refresh() would call get_model(); we set _app directly instead).
    registry._build = BuildResult(
        orchestrator=orchestrator,
        specialists=specialists,
        mcp_clients=[],
        configs=[],
    )
    dynamic_chat_app._extra_models = []
    html_source = CHAT_HTML if CHAT_HTML.is_file() else None
    dynamic_chat_app._app = orchestrator.to_web(html_source=html_source)

    if os.environ.get("OAUTH_DEMO") == "1":
        return _oauth_wrapper(dynamic_chat_app)
    return dynamic_chat_app


def _oauth_wrapper(inner):
    """ASGI wrapper for the OAuth demo: initializes the DB (lifespan), binds a
    fake principal + base URL per request so the real auth path can build links
    and poll, and serves GET /test/authorize to simulate the popup callback
    storing the user's token."""
    from agents.auth import current_base_url, current_principal
    from agents.db import SessionLocal, init_db, upsert_user_token

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await init_db()
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        if scope["path"] == "/test/authorize":
            async with SessionLocal() as s:
                await upsert_user_token(
                    s,
                    user_id=_OAUTH_USER,
                    server_key=_OAUTH_SERVER_KEY,
                    access_token="tok-123",
                    refresh_token=None,
                )
            body = b'{"ok": true}'
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        # Bind identity + app URL so _delegate can build a sign-in link and poll.
        current_principal.set(_OAUTH_USER)
        host, prt = scope.get("server") or ("127.0.0.1", 0)
        current_base_url.set(f"http://{host}:{prt}")
        await inner(scope, receive, send)

    return app


if __name__ == "__main__":
    import uvicorn

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7949
    uvicorn.run(build_app(), host="127.0.0.1", port=port, log_level="warning")
