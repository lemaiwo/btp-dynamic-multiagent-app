"""Agent-to-agent peer delegation: storage, build wiring, and loop guards.

Run:  python tests/test_peer_delegation.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = ROOT / "tests" / "_test_peer_delegation.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB}"
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
# A developer .env may set this. Set it EMPTY rather than popping it: app.py
# calls load_dotenv(), which fills in vars that are absent but never overrides
# ones already present. Empty means "no allowlist", i.e. the default rule.
os.environ["MCP_URL_ALLOWLIST"] = ""
os.environ["AICORE_AVAILABLE_MODELS"] = "gpt-4o"
os.environ["AICORE_MODEL"] = "gpt-4o"

from pydantic_ai.models.test import TestModel  # noqa: E402

import agents.registry as registry_module  # noqa: E402
from agents.db import (  # noqa: E402
    SessionLocal,
    get_agent_by_name,
    init_db,
    upsert_agent,
)

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


def servers(host: str) -> list[dict[str, str]]:
    return [{"url": f"https://{host}.example.com/mcp", "auth_mode": "none"}]


def tool_names(agent) -> list[str]:
    """The function tools registered on a pydantic-ai Agent.

    `_function_toolset` is private, but it is the only handle on the registered
    tool set; there is no public accessor. If a pydantic-ai upgrade moves it,
    this helper is the single place to fix.
    """
    toolset = getattr(agent, "_function_toolset", None)
    return sorted(getattr(toolset, "tools", {}).keys())


async def build_with_test_model():
    """Build the registry with a fake model so no AI Core credentials are needed."""
    real = registry_module.get_model
    registry_module.get_model = lambda name=None: TestModel()
    try:
        return await registry_module.build_orchestrator()
    finally:
        registry_module.get_model = real


async def main() -> None:
    await init_db()

    print("\n== peer storage ==")
    async with SessionLocal() as s:
        await upsert_agent(
            s, name="abap", description="ABAP specialist", instructions="abap",
            mcp_servers=servers("abap"),
        )
        await upsert_agent(
            s, name="fiori", description="Fiori specialist", instructions="fiori",
            mcp_servers=servers("fiori"), peers=["abap"],
        )
        fiori = await get_agent_by_name(s, "fiori")
        abap = await get_agent_by_name(s, "abap")
        check("peers stored", fiori.peers == ["abap"], str(fiori.peers))
        check("no peers is an empty list", abap.peers == [], str(abap.peers))
        check("to_dict carries peers", fiori.to_dict()["peers"] == ["abap"])
        check("to_export carries peers", fiori.to_export()["peers"] == ["abap"])

    print("\n== peer list is cleaned ==")
    async with SessionLocal() as s:
        await upsert_agent(
            s, name="fiori", description="Fiori specialist", instructions="fiori",
            mcp_servers=servers("fiori"), peers=["abap", "  ", "abap", ""],
        )
        fiori = await get_agent_by_name(s, "fiori")
        check("blanks dropped and duplicates collapsed",
              fiori.peers == ["abap"], str(fiori.peers))

    print("\n== malformed json degrades quietly ==")
    async with SessionLocal() as s:
        row = await get_agent_by_name(s, "fiori")
        row.peers_json = "{not json"
        await s.commit()
        row = await get_agent_by_name(s, "fiori")
        check("malformed peers_json yields []", row.peers == [], str(row.peers))
        row.peers_json = '{"a": 1}'
        await s.commit()
        row = await get_agent_by_name(s, "fiori")
        check("non-list peers_json yields []", row.peers == [], str(row.peers))
        # Restore for the build tests in later tasks.
        row.peers_json = '["abap"]'
        await s.commit()

    # The storage test above proves upsert_agent()/AgentConfig.peers work in
    # isolation. It would still pass even if the *admin API* silently dropped
    # peers on the way through export/import (that bug shipped for model_name
    # in Task 1 and had to be fixed in a follow-up round). Drive the real HTTP
    # endpoints to rule that out here.
    await test_export_import_via_http()

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


async def test_export_import_via_http() -> None:
    """peers must survive a POST /admin/api/export -> POST /admin/api/import
    round-trip. A no-op import (agent already present, nothing to restore)
    would pass even with the bug, so the agent is deleted before re-import.
    """
    print("\n== peers survive export/import via HTTP API ==")

    # Build the registry with a fake model before serving requests, same as
    # the app's own lifespan does, so importing agents.a2a (pulled in by
    # app.py) never reaches out to SAP AI Core.
    real_get_model = registry_module.get_model
    registry_module.get_model = lambda name=None: TestModel()
    try:
        from httpx import ASGITransport, AsyncClient
        from starlette.types import Message

        import app as app_module

        transport = ASGITransport(app=app_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            state: dict[str, list[Message]] = {"messages": []}
            received: list[Message] = [{"type": "lifespan.startup"}]

            async def receive() -> Message:
                if received:
                    return received.pop(0)
                await asyncio.sleep(3600)
                return {"type": "lifespan.shutdown"}

            async def send(msg: Message) -> None:
                state["messages"].append(msg)

            lifespan_task = asyncio.create_task(
                app_module.app({"type": "lifespan"}, receive, send)
            )
            for _ in range(50):
                if any(
                    m["type"] == "lifespan.startup.complete" for m in state["messages"]
                ):
                    break
                await asyncio.sleep(0.05)
            else:
                raise RuntimeError(f"Lifespan did not complete: {state['messages']}")

            # 1. Create an agent with peers set.
            create_resp = await client.post(
                "/admin/api/agents",
                json={
                    "name": "peer_roundtrip",
                    "description": "Peer export/import roundtrip test",
                    "instructions": "Do nothing",
                    "mcp_servers": [
                        {"url": "https://peer-rt.example.com/mcp", "auth_mode": "none"}
                    ],
                    "peers": ["abap"],
                },
            )
            check("agent created with peers", create_resp.status_code == 201,
                  f"got {create_resp.status_code}: {create_resp.text}")
            created_agent = create_resp.json()
            check("created agent has peers",
                  created_agent.get("peers") == ["abap"],
                  f"got {created_agent.get('peers')}")

            # 2. Export the bundle, keep it.
            export_resp = await client.get("/admin/api/export")
            check("export returns 200", export_resp.status_code == 200)
            export_data = export_resp.json()
            test_agent_export = None
            for agent in export_data.get("agents", []):
                if agent.get("name") == "peer_roundtrip":
                    test_agent_export = agent
                    break
            check("test agent in export", test_agent_export is not None)
            check("export carries peers",
                  (test_agent_export or {}).get("peers") == ["abap"],
                  f"got {(test_agent_export or {}).get('peers')}")

            # 3. Delete the agent so a no-op import cannot produce a false pass.
            agent_id = created_agent["id"]
            delete_resp = await client.delete(f"/admin/api/agents/{agent_id}")
            check("agent deleted", delete_resp.status_code == 204)
            get_resp = await client.get(f"/admin/api/agents/{agent_id}")
            check("deleted agent not found", get_resp.status_code == 404)

            # 4. Re-import via POST /admin/api/import — exercises api_import.
            if test_agent_export:
                test_agent_export["api_slug"] = test_agent_export.get("api_slug") or ""
            import_resp = await client.post(
                "/admin/api/import",
                json={
                    "version": 1,
                    "orchestrator_instructions": export_data.get("orchestrator_instructions"),
                    "skills": export_data.get("skills", []),
                    "agents": [test_agent_export],
                },
            )
            check("import succeeds", import_resp.status_code == 200,
                  f"got {import_resp.status_code}: {import_resp.text}")

            # 5. Read it back and confirm peers survived.
            agents_resp = await client.get("/admin/api/agents")
            check("agents list returns 200", agents_resp.status_code == 200)
            reimported_agent = None
            for agent in agents_resp.json():
                if agent.get("name") == "peer_roundtrip":
                    reimported_agent = agent
                    break
            check("reimported agent found", reimported_agent is not None)
            check("peers survived api_import",
                  (reimported_agent or {}).get("peers") == ["abap"],
                  f"expected ['abap'], got {(reimported_agent or {}).get('peers')}")

            # Don't bother with a graceful ASGI shutdown handshake: the
            # process exits via sys.exit() right after this test returns,
            # which tears the lifespan task down with it.
            lifespan_task.cancel()
    finally:
        registry_module.get_model = real_get_model


if __name__ == "__main__":
    asyncio.run(main())
