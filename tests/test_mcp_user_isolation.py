"""One user's MCP tool calls must never carry another user's credentials.

The registry builds each MCP server once and every user's agent runs share it.
pydantic-ai reuses an open MCP session while any run holds it, and the mcp
transport sends each request from a task spawned in the task group of whoever
opened that session. ``PerUserOAuth2Auth`` (and ``JWTForwardAuth``) read the
caller from a contextvar, so while Alice's run kept the session open, Bob's tool
calls went out with Alice's token -- and ARC-1's principal propagation ran them
in SAP as Alice.

This drives two overlapping ``Agent.run`` calls against a real local MCP server
that echoes back the Authorization header it received.

Run:  python tests/test_mcp_user_isolation.py
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = ROOT / "tests" / "_test_mcp_user_isolation.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB}"
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
# A developer .env may set this. Set it EMPTY rather than popping it: app.py
# calls load_dotenv(), which fills in vars that are absent but never overrides
# ones already present. Empty means "no allowlist", i.e. the default rule.
os.environ["MCP_URL_ALLOWLIST"] = ""

import uvicorn  # noqa: E402
from mcp.server.fastmcp import Context, FastMCP  # noqa: E402
from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402

from agents.auth import current_principal  # noqa: E402
from agents.db import SessionLocal, init_db, upsert_user_token  # noqa: E402
from agents.oauth2 import normalize_mcp_url  # noqa: E402
from agents.shared import create_mcp_server  # noqa: E402

FAILED = 0
PASSED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILED, PASSED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}   {detail}")


OAUTH = {
    "client_id": "sb-arc1!t1",
    "client_secret": "super-secret",
    "uaa_url": "https://tenant.authentication.eu20.hana.ondemand.com",
    "scope": "openid",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _echo_server(port: int) -> uvicorn.Server:
    mcp = FastMCP("echo", host="127.0.0.1", port=port, log_level="WARNING")

    @mcp.tool()
    async def whoami(ctx: Context) -> str:
        """Return the Authorization header this request arrived with."""
        return ctx.request_context.request.headers.get("authorization", "<none>")

    config = uvicorn.Config(
        mcp.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning"
    )
    return uvicorn.Server(config)


def _tool_return(messages) -> str | None:
    for message in messages:
        for part in getattr(message, "parts", []):
            if isinstance(part, ToolReturnPart):
                return str(part.content)
    return None


def _model(after_tool: asyncio.Event | None = None, hold: asyncio.Event | None = None):
    """Call ``whoami`` once, then answer with what it returned.

    ``after_tool`` is set once the tool result is in; ``hold`` keeps the run
    (and so its MCP session) open until someone else sets it.
    """

    async def respond(messages, info: AgentInfo) -> ModelResponse:
        seen = _tool_return(messages)
        if seen is None:
            return ModelResponse(parts=[ToolCallPart(tool_name="whoami", args={})])
        if after_tool is not None:
            after_tool.set()
        if hold is not None:
            await hold.wait()
        return ModelResponse(parts=[TextPart(seen)])

    return FunctionModel(respond)


async def _run_as(user: str, agent: Agent) -> str:
    current_principal.set(user)
    result = await agent.run("who am I?")
    return result.output


async def main() -> None:
    await init_db()

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    server_key = normalize_mcp_url(base_url)

    async with SessionLocal() as s:
        for user in ("alice", "bob"):
            await upsert_user_token(
                s,
                user_id=user,
                server_key=server_key,
                access_token=f"token-of-{user}",
                refresh_token=None,
            )

    http = _echo_server(port)
    serve_task = asyncio.create_task(http.serve())
    while not http.started:
        await asyncio.sleep(0.05)

    try:
        # One server object, shared by every run -- exactly what the registry
        # builds for an agent with an oauth2 MCP server such as ARC-1.
        mcp_server = create_mcp_server("arc1", base_url, "oauth2", oauth=OAUTH)

        print("\n== one run at a time ==")
        agent = Agent(_model(), toolsets=[mcp_server])
        alone_alice = await asyncio.wait_for(_run_as("alice", agent), 30)
        alone_bob = await asyncio.wait_for(_run_as("bob", agent), 30)
        check("alice's call carries alice's token", alone_alice == "Bearer token-of-alice", alone_alice)
        check("bob's call carries bob's token", alone_bob == "Bearer token-of-bob", alone_bob)

        print("\n== bob calls while alice's run still holds its session ==")
        alice_called = asyncio.Event()
        release_alice = asyncio.Event()
        alice_agent = Agent(_model(alice_called, release_alice), toolsets=[mcp_server])
        bob_agent = Agent(_model(), toolsets=[mcp_server])

        alice_task = asyncio.create_task(_run_as("alice", alice_agent))
        try:
            await asyncio.wait_for(alice_called.wait(), 30)
            overlap_bob = await asyncio.wait_for(_run_as("bob", bob_agent), 30)
        finally:
            release_alice.set()
        overlap_alice = await asyncio.wait_for(alice_task, 30)

        check(
            "bob's call carries bob's token, not alice's",
            overlap_bob == "Bearer token-of-bob",
            overlap_bob,
        )
        check(
            "alice's call carries alice's token",
            overlap_alice == "Bearer token-of-alice",
            overlap_alice,
        )
    finally:
        http.should_exit = True
        await serve_task

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    try:
        await SessionLocal().bind.dispose()  # type: ignore[attr-defined]
    except Exception:
        pass
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
