"""Per-agent model override: storage, export, and registry resolution.

Run:  python tests/test_agent_model.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = ROOT / "tests" / "_test_agent_model.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB}"
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
# A developer .env may set this. Set it EMPTY rather than popping it: app.py
# calls load_dotenv(), which fills in vars that are absent but never overrides
# ones already present. Empty means "no allowlist", i.e. the default rule.
os.environ["MCP_URL_ALLOWLIST"] = ""
# available_models() would otherwise query AI Core over the network.
os.environ["AICORE_AVAILABLE_MODELS"] = "gpt-4o,gpt-4o-mini"
os.environ["AICORE_MODEL"] = "gpt-4o"

from pydantic_ai.models.test import TestModel  # noqa: E402

# Stub SAP AI Core model + MCP server factory BEFORE importing anything else
import agents.shared as shared  # noqa: E402


# Module-level fake_get_model that caches instances
_fake_model_cache: dict = {}

def _module_fake_get_model(name: str | None = None):
    """Fake get_model that caches TestModel instances per name."""
    if name == "no-such-model":
        raise RuntimeError("deployment not found")
    if name not in _fake_model_cache:
        _fake_model_cache[name] = TestModel()
    return _fake_model_cache[name]

shared.get_model = _module_fake_get_model  # type: ignore[assignment]


class _FakeMCP:
    def __init__(self, name: str, base_url: str, **kwargs):
        self.name = name
        self.base_url = base_url
        self.kwargs = kwargs


shared.create_mcp_server = lambda name, base_url, *a, **k: _FakeMCP(  # type: ignore[assignment]
    name, base_url, **k
)

# Patch pydantic_ai.Agent so it accepts our fake model + fake toolsets
import pydantic_ai  # noqa: E402

_orig_agent_init = pydantic_ai.Agent.__init__


def _patched_init(self, model=None, **kwargs):  # type: ignore[no-untyped-def]
    # Drop toolsets (they may reference our fake MCP). Pass the given model
    # through unchanged — registry.py always passes a real (fake) TestModel
    # instance built via the patched get_model, and the registry-resolution
    # checks below depend on `is` identity against those exact instances.
    # Only fall back to the string "test" when no model was given at all.
    kwargs.pop("toolsets", None)
    _orig_agent_init(self, model=(model if model is not None else "test"), **kwargs)


pydantic_ai.Agent.__init__ = _patched_init  # type: ignore[method-assign]


def _fake_to_web(self, *args, **kwargs):  # type: ignore[no-untyped-def]
    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"fake-chat"})

    return app


pydantic_ai.Agent.to_web = _fake_to_web  # type: ignore[method-assign]

# Now import the app and other modules
from httpx import ASGITransport, AsyncClient  # noqa: E402
import app as app_module  # noqa: E402

import agents.registry as registry_module  # noqa: E402
from agents.db import (  # noqa: E402
    SessionLocal,
    get_agent_by_name,
    init_db,
    upsert_agent,
    list_agents,
)
import json  # noqa: E402

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


async def main() -> None:
    # This function is the original test (storage + registry)
    await init_db()

    print("\n== storage ==")
    async with SessionLocal() as s:
        await upsert_agent(
            s, name="small", description="cheap reader", instructions="read",
            mcp_servers=servers("small"), model_name="gpt-4o-mini",
        )
        await upsert_agent(
            s, name="plain", description="no override", instructions="work",
            mcp_servers=servers("plain"),
        )
        await upsert_agent(
            s, name="broken", description="bad model", instructions="work",
            mcp_servers=servers("broken"), model_name="no-such-model",
        )
        small = await get_agent_by_name(s, "small")
        plain = await get_agent_by_name(s, "plain")
        check("override stored", small.model_name == "gpt-4o-mini", small.model_name)
        check("absent override is None", plain.model_name is None, plain.model_name)
        check("to_dict carries it", small.to_dict()["model_name"] == "gpt-4o-mini")
        check("to_dict blank when unset", plain.to_dict()["model_name"] == "")
        check("to_export carries it", small.to_export()["model_name"] == "gpt-4o-mini")

    print("\n== blank is normalized to None ==")
    async with SessionLocal() as s:
        await upsert_agent(
            s, name="small", description="cheap reader", instructions="read",
            mcp_servers=servers("small"), model_name="   ",
        )
        blanked = await get_agent_by_name(s, "small")
        check("whitespace override becomes None", blanked.model_name is None,
              repr(blanked.model_name))
        # Put it back for the build tests below.
        await upsert_agent(
            s, name="small", description="cheap reader", instructions="read",
            mcp_servers=servers("small"), model_name="gpt-4o-mini",
        )

    print("\n== registry resolution ==")
    asked: list[str | None] = []

    # Clear the cache before starting fresh test
    _fake_model_cache.clear()

    # Wrap the registry_module.get_model to track calls
    real_registry_get_model = registry_module.get_model

    def tracking_get_model(name: str | None = None):
        asked.append(name)
        return real_registry_get_model(name)

    registry_module.get_model = tracking_get_model  # type: ignore[assignment]
    try:
        build = await registry_module.build_orchestrator()
    finally:
        registry_module.get_model = real_registry_get_model  # type: ignore[assignment]

    default_model = _fake_model_cache.get("gpt-4o")
    check("override model was requested", "gpt-4o-mini" in asked, str(asked))

    # Debug: show what's in the cache and what models the specialists have
    small_model = build.specialists["small"].model
    cached_mini = _fake_model_cache.get("gpt-4o-mini")
    check("overriding agent got its own model",
          small_model is cached_mini,
          f"got {small_model!r}, expected {cached_mini!r}, cache keys: {list(_fake_model_cache.keys())}")
    check("plain agent got the global model",
          build.specialists["plain"].model is default_model)
    check("orchestrator keeps the global model",
          build.orchestrator.model is default_model)

    print("\n== unresolvable override falls back ==")
    check("agent with a bad model is still built",
          "broken" in build.specialists, str(sorted(build.specialists)))
    check("bad override falls back to the global model",
          build.specialists["broken"].model is default_model)

    # Run HTTP API test after database tests complete
    await test_export_import_via_http()

    # Print final results
    await end_tests()


async def test_export_import_via_http() -> None:
    """Test that api_import passes model_name through (HTTP API level test)."""
    print("\n== export/import round-trip (via HTTP API) ==")
    # Use the real HTTP endpoints to test that api_import passes model_name through
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Trigger lifespan startup
        from starlette.types import Message

        state = {"messages": []}
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
            if any(m["type"] == "lifespan.startup.complete" for m in state["messages"]):
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError(f"Lifespan did not complete: {state['messages']}")

        # 1. Create an agent with model_name via POST /admin/api/agents
        create_resp = await client.post(
            "/admin/api/agents",
            json={
                "name": "roundtrip_test",
                "description": "Test export/import roundtrip",
                "instructions": "Do nothing",
                "mcp_servers": [{"url": "https://test.hana.ondemand.com/mcp", "auth_mode": "none"}],
                "model_name": "gpt-4o-mini",
            },
        )
        check("agent created with model_name", create_resp.status_code == 201)
        created_agent = create_resp.json()
        check("created agent has model_name",
              created_agent.get("model_name") == "gpt-4o-mini",
              f"got {created_agent.get('model_name')}")

        # 2. Export via GET /admin/api/export
        export_resp = await client.get("/admin/api/export")
        check("export returns 200", export_resp.status_code == 200)
        export_data = export_resp.json()
        check("export has agents", len(export_data.get("agents", [])) > 0)

        # Find our test agent in the export
        test_agent_export = None
        for agent in export_data.get("agents", []):
            if agent.get("name") == "roundtrip_test":
                test_agent_export = agent
                break
        check("test agent in export", test_agent_export is not None)
        check("export carries model_name",
              test_agent_export.get("model_name") == "gpt-4o-mini" if test_agent_export else False,
              f"got {test_agent_export.get('model_name') if test_agent_export else 'N/A'}")

        # 3. Delete the agent so re-import doesn't just update an existing row
        agent_id = created_agent["id"]
        delete_resp = await client.delete(f"/admin/api/agents/{agent_id}")
        check("agent deleted", delete_resp.status_code == 204)

        # Verify it's gone
        get_resp = await client.get(f"/admin/api/agents/{agent_id}")
        check("deleted agent not found", get_resp.status_code == 404)

        # 4. Re-import via POST /admin/api/import (this exercises api_import code path)
        # Clean up the export to ensure api_slug is an empty string, not null
        if test_agent_export:
            test_agent_export["api_slug"] = test_agent_export.get("api_slug") or ""
        import_resp = await client.post(
            "/admin/api/import",
            json={
                "version": 1,
                "orchestrator_instructions": export_data.get("orchestrator_instructions"),
                "skills": export_data.get("skills", []),
                "agents": [test_agent_export],  # Re-import only our test agent
            },
        )
        check("import succeeds", import_resp.status_code == 200,
              f"got {import_resp.status_code}: {import_resp.text}")

        # 5. Read the agent back and verify model_name survived
        agents_resp = await client.get("/admin/api/agents")
        check("agents list returns 200", agents_resp.status_code == 200)
        agents_list = agents_resp.json()
        reimported_agent = None
        for agent in agents_list:
            if agent.get("name") == "roundtrip_test":
                reimported_agent = agent
                break

        check("reimported agent found", reimported_agent is not None)
        check("model_name survived api_import",
              reimported_agent.get("model_name") == "gpt-4o-mini" if reimported_agent else False,
              f"expected 'gpt-4o-mini' after import, got {reimported_agent.get('model_name') if reimported_agent else 'N/A'}")


async def end_tests() -> None:
    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
