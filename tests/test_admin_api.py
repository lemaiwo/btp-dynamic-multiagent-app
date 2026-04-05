"""End-to-end HTTP tests for the admin API.

Stubs the heavy/external pieces so the app can boot without an SAP AI
Core or MCP server, then exercises every /admin/api/* endpoint against
the real FastAPI app using httpx.AsyncClient with an ASGI transport.

Run:  python tests/test_admin_api.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Make the project importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Use a throwaway SQLite DB for the test run
TEST_DB = ROOT / "tests" / "_test_registry.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB}"
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

# Stub SAP AI Core model + MCP server factory before importing the app
import agents.shared as shared  # noqa: E402


class _FakeModel:
    """Minimal stand-in used only to be handed to Agent()."""

    model_name = "fake"

    def __repr__(self) -> str:
        return "FakeModel()"


shared.get_model = lambda: _FakeModel()  # type: ignore[assignment]


class _FakeMCP:
    def __init__(self, name: str, base_url: str):
        self.name = name
        self.base_url = base_url


shared.create_mcp_server = lambda name, base_url: _FakeMCP(name, base_url)  # type: ignore[assignment]

# Patch pydantic_ai.Agent so it accepts our fake model + fake toolsets
# without touching a real LLM or MCP process. We keep the Agent.tool
# decorator behaviour intact (the registry uses it) but stub .run and
# .to_web so nothing external is needed.
import pydantic_ai  # noqa: E402

_orig_agent_init = pydantic_ai.Agent.__init__


def _patched_init(self, model=None, **kwargs):  # type: ignore[no-untyped-def]
    # Drop toolsets (they may reference our fake MCP)
    kwargs.pop("toolsets", None)
    _orig_agent_init(self, model="test", **kwargs)


pydantic_ai.Agent.__init__ = _patched_init  # type: ignore[method-assign]


def _fake_to_web(self):  # type: ignore[no-untyped-def]
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

# Now we can safely import the real app
from httpx import ASGITransport, AsyncClient  # noqa: E402

import app as app_module  # noqa: E402


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------
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


async def run_tests() -> None:
    # We must use the lifespan so init_db + seed runs.
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Trigger lifespan manually via the transport's app — httpx ASGI
        # transport does not run lifespan automatically, so invoke it:
        from contextlib import asynccontextmanager

        from starlette.types import Message

        # --- Lifespan startup ----------------------------------------------
        state = {"messages": []}
        received: list[Message] = [{"type": "lifespan.startup"}]

        async def receive() -> Message:
            if received:
                return received.pop(0)
            # Block forever — lifespan won't read again until shutdown
            await asyncio.sleep(3600)
            return {"type": "lifespan.shutdown"}

        async def send(msg: Message) -> None:
            state["messages"].append(msg)

        lifespan_task = asyncio.create_task(
            app_module.app({"type": "lifespan"}, receive, send)
        )
        # Wait until startup completes
        for _ in range(50):
            if any(m["type"] == "lifespan.startup.complete" for m in state["messages"]):
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError(f"Lifespan did not complete: {state['messages']}")

        print("\n== Lifespan startup ==")
        check("startup.complete emitted", True)

        # --- /healthz -------------------------------------------------------
        print("\n== /healthz ==")
        r = await client.get("/healthz")
        check("status 200", r.status_code == 200, f"got {r.status_code}")
        check("body {status: ok}", r.json() == {"status": "ok"})

        # --- Root redirect --------------------------------------------------
        print("\n== / redirect ==")
        r = await client.get("/", follow_redirects=False)
        check("redirects", r.status_code in (302, 307), f"got {r.status_code}")
        check("to /chat", r.headers.get("location") == "/chat")

        # --- Chat mount -----------------------------------------------------
        print("\n== /chat ==")
        r = await client.get("/chat/", follow_redirects=True)
        check(
            "chat mount responds",
            r.status_code == 200 and r.text == "fake-chat",
            f"got {r.status_code} {r.text[:50]}",
        )

        # --- Admin UI -------------------------------------------------------
        print("\n== GET /admin ==")
        r = await client.get("/admin")
        check("admin ui html", r.status_code == 200 and "Agent Administration" in r.text)

        # --- List seeded agents --------------------------------------------
        print("\n== GET /admin/api/agents (seeded) ==")
        r = await client.get("/admin/api/agents")
        check("200", r.status_code == 200)
        seeded = r.json()
        check("3 seeded agents", len(seeded) == 3, f"got {len(seeded)}: {[a['name'] for a in seeded]}")
        names = {a["name"] for a in seeded}
        check("contains cloudfoundry", "cloudfoundry" in names)
        check("contains btp", "btp" in names)
        check("contains auditlog", "auditlog" in names)

        auditlog = next(a for a in seeded if a["name"] == "auditlog")
        check("auditlog disabled", auditlog["enabled"] is False)

        # --- Create a new agent --------------------------------------------
        print("\n== POST /admin/api/agents ==")
        payload = {
            "name": "testagent",
            "description": "A test specialist.",
            "instructions": "You are a test specialist.",
            "mcp_url": "https://foo-mcp.cfapps.eu20-001.hana.ondemand.com",
            "enabled": True,
        }
        r = await client.post("/admin/api/agents", json=payload)
        check("201 created", r.status_code == 201, f"got {r.status_code}: {r.text}")
        created = r.json()
        check("returned id", "id" in created)
        check("name echo", created["name"] == "testagent")
        new_id = created["id"]

        # --- Validation: bad URL -------------------------------------------
        print("\n== POST validation: non-HTTPS ==")
        bad = dict(payload, name="badhttp", mcp_url="http://insecure.hana.ondemand.com")
        r = await client.post("/admin/api/agents", json=bad)
        check("422 rejects http://", r.status_code == 422, f"got {r.status_code}")

        print("\n== POST validation: non-BTP host ==")
        bad = dict(payload, name="badhost", mcp_url="https://example.com")
        r = await client.post("/admin/api/agents", json=bad)
        check("422 rejects non-BTP host", r.status_code == 422, f"got {r.status_code}")

        # --- GET one --------------------------------------------------------
        print("\n== GET /admin/api/agents/{id} ==")
        r = await client.get(f"/admin/api/agents/{new_id}")
        check("200", r.status_code == 200)
        check("name matches", r.json()["name"] == "testagent")

        # --- Update ---------------------------------------------------------
        print("\n== PUT /admin/api/agents/{id} ==")
        upd = dict(payload, description="Updated description.")
        r = await client.put(f"/admin/api/agents/{new_id}", json=upd)
        check("200", r.status_code == 200, f"got {r.status_code}: {r.text}")
        check("description updated", r.json()["description"] == "Updated description.")

        # --- Orchestrator instructions -------------------------------------
        print("\n== GET/PUT /admin/api/orchestrator ==")
        r = await client.get("/admin/api/orchestrator")
        check("200", r.status_code == 200)
        check("has instructions", "instructions" in r.json())
        r = await client.put(
            "/admin/api/orchestrator", json={"instructions": "Be concise."}
        )
        check("update 200", r.status_code == 200)
        check("updated", r.json()["instructions"] == "Be concise.")

        # --- Reload ---------------------------------------------------------
        print("\n== POST /admin/api/reload ==")
        r = await client.post("/admin/api/reload")
        check("200", r.status_code == 200, f"got {r.status_code}: {r.text}")
        data = r.json()
        check("status reloaded", data.get("status") == "reloaded")
        check("agents count >= 3", data.get("agents", 0) >= 3, f"got {data}")

        # --- Restart (CF not configured → ok=false) ------------------------
        print("\n== POST /admin/api/restart ==")
        r = await client.post("/admin/api/restart")
        check("200", r.status_code == 200, f"got {r.status_code}: {r.text}")
        data = r.json()
        check("cf_restart.ok false", data.get("cf_restart", {}).get("ok") is False)

        # --- Export ---------------------------------------------------------
        print("\n== GET /admin/api/export ==")
        r = await client.get("/admin/api/export")
        check("200", r.status_code == 200)
        exported = r.json()
        check("version 1", exported.get("version") == 1)
        check("has agents", len(exported.get("agents", [])) >= 4)

        # --- Import (merge) -------------------------------------------------
        print("\n== POST /admin/api/import (merge) ==")
        imp = {
            "orchestrator_instructions": "Imported instructions.",
            "agents": [
                {
                    "name": "imported1",
                    "description": "Imported agent 1.",
                    "instructions": "Imported 1.",
                    "mcp_url": "https://imp1.cfapps.eu20-001.hana.ondemand.com",
                    "enabled": True,
                }
            ],
            "replace": False,
        }
        r = await client.post("/admin/api/import", json=imp)
        check("200", r.status_code == 200, f"got {r.status_code}: {r.text}")
        check("imported 1", r.json().get("imported") == 1)
        check("removed 0", r.json().get("removed") == 0)

        r = await client.get("/admin/api/agents")
        all_names = {a["name"] for a in r.json()}
        check("imported1 present", "imported1" in all_names)
        check("testagent still present (merge)", "testagent" in all_names)

        # --- Import (replace) ----------------------------------------------
        print("\n== POST /admin/api/import (replace) ==")
        imp2 = {
            "agents": [
                {
                    "name": "only",
                    "description": "The only remaining agent.",
                    "instructions": "You are alone.",
                    "mcp_url": "https://only.cfapps.eu20-001.hana.ondemand.com",
                    "enabled": True,
                }
            ],
            "replace": True,
        }
        r = await client.post("/admin/api/import", json=imp2)
        check("200", r.status_code == 200, f"got {r.status_code}: {r.text}")
        check(
            "removed > 0",
            r.json().get("removed", 0) > 0,
            f"got {r.json()}",
        )
        r = await client.get("/admin/api/agents")
        remaining = {a["name"] for a in r.json()}
        check("only 'only' remains", remaining == {"only"}, f"got {remaining}")

        # --- Delete ---------------------------------------------------------
        print("\n== DELETE /admin/api/agents/{id} ==")
        r = await client.get("/admin/api/agents")
        only_id = r.json()[0]["id"]
        r = await client.delete(f"/admin/api/agents/{only_id}")
        check("204", r.status_code == 204, f"got {r.status_code}")
        r = await client.get("/admin/api/agents")
        check("empty after delete", r.json() == [])

        # --- 404 on missing ------------------------------------------------
        print("\n== GET missing id -> 404 ==")
        r = await client.get("/admin/api/agents/999999")
        check("404", r.status_code == 404)

        # --- Lifespan shutdown ---------------------------------------------
        received.append({"type": "lifespan.shutdown"})
        try:
            await asyncio.wait_for(lifespan_task, timeout=5)
        except asyncio.TimeoutError:
            lifespan_task.cancel()

    print(f"\n=== {PASSED} passed, {FAILED} failed ===")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run_tests())
