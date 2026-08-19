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


shared.get_model = lambda name=None: _FakeModel()  # type: ignore[assignment]


class _FakeMCP:
    def __init__(self, name: str, base_url: str, **kwargs):
        self.name = name
        self.base_url = base_url
        self.kwargs = kwargs


shared.create_mcp_server = lambda name, base_url, *a, **k: _FakeMCP(  # type: ignore[assignment]
    name, base_url, **k
)

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

        # --- Root serves the chat mount --------------------------------------
        print("\n== / chat mount ==")
        r = await client.get("/", follow_redirects=False)
        check("root serves chat", r.status_code == 200 and r.text == "fake-chat",
              f"got {r.status_code} {r.text[:50]}")

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
        check("1 seeded agent", len(seeded) == 1, f"got {len(seeded)}: {[a['name'] for a in seeded]}")
        names = {a["name"] for a in seeded}
        check(
            "contains ABAP Development Agent",
            "ABAP Development Agent" in names,
            f"got {names}",
        )

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

        # --- Skills CRUD ----------------------------------------------------
        print("\n== Skills CRUD ==")
        r = await client.get("/admin/api/skills")
        check("empty skills list", r.status_code == 200 and r.json() == [])

        skill_payload = {
            "name": "cf-troubleshooting",
            "description": "How to diagnose failing Cloud Foundry apps.",
            "content": "1. Check recent logs.\n2. Check crash events.\n3. Check quotas.",
        }
        r = await client.post("/admin/api/skills", json=skill_payload)
        check("201 skill created", r.status_code == 201, f"got {r.status_code}: {r.text}")
        created_skill = r.json()
        check("skill name echo", created_skill["name"] == "cf-troubleshooting")
        check("skill content echo", created_skill["content"] == skill_payload["content"])
        skill_id = created_skill["id"]

        r = await client.post("/admin/api/skills", json={
            "name": "quota-checks",
            "description": "How to verify entitlements and quotas.",
            "content": "Check the subaccount entitlements before assigning quota.",
        })
        check("second skill created", r.status_code == 201)

        r = await client.get("/admin/api/skills")
        check("2 skills listed", len(r.json()) == 2, f"got {len(r.json())}")

        r = await client.get(f"/admin/api/skills/{skill_id}")
        check("get one skill", r.status_code == 200 and r.json()["id"] == skill_id)

        print("\n== Attach skills to an agent ==")
        upd_skills = dict(payload, skills=["cf-troubleshooting"])
        r = await client.put(f"/admin/api/agents/{new_id}", json=upd_skills)
        check("attach skill 200", r.status_code == 200, f"got {r.status_code}: {r.text}")
        check("agent echoes skills", r.json()["skills"] == ["cf-troubleshooting"], f"got {r.json().get('skills')}")

        r = await client.get(f"/admin/api/agents/{new_id}")
        check("skills persisted", r.json()["skills"] == ["cf-troubleshooting"])

        print("\n== Skill validation ==")
        r = await client.put(
            f"/admin/api/agents/{new_id}", json=dict(payload, skills=["no-such-skill"])
        )
        check("422 unknown skill on update", r.status_code == 422, f"got {r.status_code}")
        r = await client.post(
            "/admin/api/agents",
            json=dict(payload, name="skillbad", skills=["no-such-skill"]),
        )
        check("422 unknown skill on create", r.status_code == 422, f"got {r.status_code}")
        # The failed update must not have wiped the stored skills
        r = await client.get(f"/admin/api/agents/{new_id}")
        check("skills unchanged after 422", r.json()["skills"] == ["cf-troubleshooting"])

        print("\n== Rename skill follows references ==")
        r = await client.put(f"/admin/api/skills/{skill_id}", json={
            "name": "cf-diagnostics",
            "description": "How to diagnose failing Cloud Foundry apps.",
            "content": "1. Check recent logs.",
        })
        check("skill renamed", r.status_code == 200 and r.json()["name"] == "cf-diagnostics")
        r = await client.get(f"/admin/api/agents/{new_id}")
        check("agent reference renamed", r.json()["skills"] == ["cf-diagnostics"], f"got {r.json()['skills']}")

        print("\n== Reload with skills attached ==")
        r = await client.post("/admin/api/reload")
        check("reload with skills 200", r.status_code == 200, f"got {r.status_code}: {r.text}")

        print("\n== Delete skill detaches it ==")
        r = await client.delete(f"/admin/api/skills/{skill_id}")
        check("204 skill deleted", r.status_code == 204, f"got {r.status_code}")
        r = await client.get(f"/admin/api/agents/{new_id}")
        check("skill detached from agent", r.json()["skills"] == [], f"got {r.json()['skills']}")
        r = await client.get("/admin/api/skills")
        check("1 skill remains", len(r.json()) == 1)

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
        check("agents count >= 2", data.get("agents", 0) >= 2, f"got {data}")

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
        check("has agents", len(exported.get("agents", [])) >= 2)
        check(
            "has skills",
            [s["name"] for s in exported.get("skills", [])] == ["quota-checks"],
            f"got {exported.get('skills')}",
        )
        check(
            "agent export has skills key",
            all("skills" in a for a in exported.get("agents", [])),
        )

        # --- Import (merge) -------------------------------------------------
        print("\n== POST /admin/api/import (merge) ==")
        imp = {
            "orchestrator_instructions": "Imported instructions.",
            "skills": [
                {
                    "name": "imported-skill",
                    "description": "Imported skill.",
                    "content": "Follow the imported procedure.",
                }
            ],
            "agents": [
                {
                    "name": "imported1",
                    "description": "Imported agent 1.",
                    "instructions": "Imported 1.",
                    "mcp_url": "https://imp1.cfapps.eu20-001.hana.ondemand.com",
                    "skills": ["imported-skill"],
                    "enabled": True,
                }
            ],
            "replace": False,
        }
        r = await client.post("/admin/api/import", json=imp)
        check("200", r.status_code == 200, f"got {r.status_code}: {r.text}")
        check("imported 1", r.json().get("imported") == 1)
        check("imported 1 skill", r.json().get("imported_skills") == 1)
        check("removed 0", r.json().get("removed") == 0)

        r = await client.get("/admin/api/agents")
        all_names = {a["name"] for a in r.json()}
        check("imported1 present", "imported1" in all_names)
        check("testagent still present (merge)", "testagent" in all_names)
        imported1 = next(a for a in r.json() if a["name"] == "imported1")
        check("imported1 has skill", imported1["skills"] == ["imported-skill"])

        # --- Import (replace) ----------------------------------------------
        print("\n== POST /admin/api/import (replace) ==")
        imp2 = {
            "skills": [
                {
                    "name": "only-skill",
                    "description": "The only remaining skill.",
                    "content": "You are the only skill.",
                }
            ],
            "agents": [
                {
                    "name": "only",
                    "description": "The only remaining agent.",
                    "instructions": "You are alone.",
                    "mcp_url": "https://only.cfapps.eu20-001.hana.ondemand.com",
                    "skills": ["only-skill"],
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
        check(
            "removed 2 skills",
            r.json().get("removed_skills") == 2,
            f"got {r.json()}",
        )
        r = await client.get("/admin/api/agents")
        remaining = {a["name"] for a in r.json()}
        check("only 'only' remains", remaining == {"only"}, f"got {remaining}")
        r = await client.get("/admin/api/skills")
        remaining_skills = {s["name"] for s in r.json()}
        check("only 'only-skill' remains", remaining_skills == {"only-skill"}, f"got {remaining_skills}")

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

        # --- Chat exposure filtering (behavioural) --------------------------
        # Task 1 added AgentConfig.expose_chat; this checks the orchestrator
        # actually honours it (not just that the DB stores it): a chat-visible
        # agent must get a delegation tool, a run-only one must not, while both
        # are still built as specialists (the scheduled/API runner needs them).
        print("\n== Chat exposure filtering (orchestrator tools) ==")
        from agents.db import SessionLocal as _SessionLocal
        from agents.db import upsert_agent
        from agents.registry import _sanitize_tool_name, registry

        expose_url = "https://expose-test.cfapps.eu20-001.hana.ondemand.com"
        async with _SessionLocal() as s:
            await upsert_agent(
                s, name="chat-visible", description="d", instructions="i",
                mcp_servers=[{"url": expose_url, "auth_mode": "none"}],
                enabled=True, expose_chat=True,
            )
            await upsert_agent(
                s, name="run-only", description="d", instructions="i",
                mcp_servers=[{"url": expose_url, "auth_mode": "none"}],
                enabled=True, expose_chat=False,
            )

        r = await client.post("/admin/api/reload")
        check(
            "reload after exposure agents 200",
            r.status_code == 200,
            f"got {r.status_code}: {r.text}",
        )

        build = registry.build
        check(
            "both agents built as specialists",
            "chat-visible" in build.specialists and "run-only" in build.specialists,
            f"got {list(build.specialists)}",
        )

        # Inspect the orchestrator's registered tools directly (same private
        # attribute `_attach_delegation_tool` in agents/registry.py relies on
        # implicitly via Agent.tool()) rather than trying to run the model.
        tool_names = set(build.orchestrator._function_toolset.tools.keys())
        check(
            "chat-visible has a delegation tool",
            _sanitize_tool_name("chat-visible") in tool_names,
            f"got {tool_names}",
        )
        check(
            "run-only has NO delegation tool",
            _sanitize_tool_name("run-only") not in tool_names,
            f"got {tool_names}",
        )

        # --- Run endpoints ----------------------------------------------------
        print("\n== run endpoints ==")
        r = await client.post("/admin/api/agents", json={
            "name": "Run Agent", "description": "d", "instructions": "i",
            "mcp_url": "https://x.example.com/mcp", "auth_mode": "none",
            "expose_api": True, "api_slug": "run-agent",
            "run_as_principal": "svc@example.com",
        })
        check("create api-exposed agent", r.status_code == 201, r.text)
        run_agent = r.json()
        agent_id = run_agent["id"]
        check("expose_api stored on create", run_agent["expose_api"] is True, run_agent)
        check("api_slug stored on create", run_agent["api_slug"] == "run-agent", run_agent)

        r = await client.get("/admin/api/runs")
        check("run list endpoint", r.status_code == 200 and isinstance(r.json(), list))

        r = await client.post("/api/agents/does-not-exist/run")
        check("unknown slug -> 404", r.status_code == 404, r.text)

        r = await client.post("/admin/api/agents/999999/run")
        check("unknown agent id -> 404", r.status_code == 404, r.text)

        r = await client.post("/admin/api/agents", json={
            "name": "Chat Only", "description": "d", "instructions": "i",
            "mcp_url": "https://y.example.com/mcp", "auth_mode": "none",
        })
        chat_id = r.json()["id"]
        r = await client.post(f"/admin/api/agents/{chat_id}/run")
        check("non-API agent rejected", r.status_code == 409, r.text)

        # --- Update parity: exposure fields must survive a PUT, not just a
        # create. Task 7's highest-risk spot: wiring the new AgentPayload
        # fields into api_create_agent but forgetting api_update_agent would
        # silently un-expose a scheduled agent on its next edit.
        print("\n== PUT preserves exposure fields ==")
        r = await client.put(f"/admin/api/agents/{agent_id}", json={
            "name": "Run Agent", "description": "Updated d", "instructions": "i",
            "mcp_url": "https://x.example.com/mcp", "auth_mode": "none",
            "expose_api": True, "api_slug": "run-agent",
            "run_as_principal": "svc@example.com",
        })
        check("update 200", r.status_code == 200, r.text)
        updated = r.json()
        check("description updated", updated["description"] == "Updated d")
        check("expose_api survives update", updated["expose_api"] is True, updated)
        check("api_slug survives update", updated["api_slug"] == "run-agent", updated)
        check(
            "run_as_principal survives update",
            updated["run_as_principal"] == "svc@example.com",
            updated,
        )

        # --- Successful triggers: exercise the 202 contract end-to-end ------
        # Build the specialist so execute_run's background task can look it
        # up (it does not itself crash the test either way -- execute_run
        # never raises -- but this keeps the trigger meaningful).
        r = await client.post("/admin/api/reload")
        check("reload before triggering 200", r.status_code == 200, r.text)

        print("\n== POST /api/agents/{slug}/run (scheduler-facing) ==")
        import agents.job_runner as job_runner

        r = await client.post("/api/agents/run-agent/run")
        check("scheduled run accepted", r.status_code == 202, r.text)
        sched_run_id = r.json().get("run_id")
        check("run_id returned", bool(sched_run_id), r.text)

        # Drain the background task the run spawned so it doesn't leak a
        # "Task was destroyed but it is pending" warning at process exit
        # (same pattern as tests/test_job_runs.py).
        for t in list(job_runner._tasks):
            await t

        r = await client.get(f"/admin/api/runs/{sched_run_id}")
        check("run detail 200", r.status_code == 200, r.text)
        detail = r.json()
        check("run detail has report key", "report" in detail, detail)
        check("run detail id matches", detail["id"] == sched_run_id, detail)

        r = await client.get("/admin/api/runs/does-not-exist")
        check("unknown run id -> 404", r.status_code == 404, r.text)

        print("\n== POST /admin/api/agents/{id}/run (run-now) ==")
        r = await client.post(f"/admin/api/agents/{agent_id}/run")
        check("run-now accepted", r.status_code == 202, r.text)
        manual_run_id = r.json().get("run_id")
        check("run_id returned", bool(manual_run_id), r.text)

        for t in list(job_runner._tasks):
            await t

        r = await client.get("/admin/api/runs", params={"agent_id": agent_id})
        check(
            "run list filtered by agent_id",
            r.status_code == 200 and all(run["agent_id"] == agent_id for run in r.json()),
            r.text,
        )
        check(
            "both runs present in filtered list",
            {sched_run_id, manual_run_id} <= {run["id"] for run in r.json()},
            r.text,
        )

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
