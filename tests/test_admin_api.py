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
# A developer .env may set this. Set it EMPTY rather than popping it: app.py
# calls load_dotenv(), which fills in vars that are absent but never overrides
# ones already present. Empty means "no allowlist", i.e. the default rule.
os.environ["MCP_URL_ALLOWLIST"] = ""
# Pin the model list: available_models() would otherwise try to reach AI Core.
os.environ["AICORE_AVAILABLE_MODELS"] = "gpt-4o,gpt-4o-mini"

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
        check("expected_sections gone from agent payload",
              "expected_sections" not in created,
              str(sorted(created.keys())))
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

        # --- UI round-trip: all six exposure fields must survive a
        # load-then-save cycle using the exact payload shape
        # templates/admin.html's saveAgent() builds from editAgent()'s
        # loaded values. AgentPayload has defaults and PUT is whole-object
        # semantics, so if the form ever omits one of these six fields,
        # saving *any* agent through the UI silently resets it (expose_api
        # -> False, expose_chat -> True, api_slug/run_as_principal/
        # run_prompt wiped) with no error surfaced anywhere -- e.g.
        # un-exposing a scheduled agent on its next edit.
        print("\n== UI round-trip preserves all six exposure fields ==")
        r = await client.post("/admin/api/agents", json={
            "name": "UI Roundtrip Agent", "description": "d", "instructions": "i",
            "mcp_servers": [{"url": "https://z.example.com/mcp", "auth_mode": "none"}],
            "expose_chat": False,
            "expose_api": True,
            "api_slug": "ui-roundtrip",
            "run_as_principal": "svc-roundtrip@example.com",
            "run_prompt": "Perform your configured check now.",
            "run_timeout_seconds": 900,
        })
        check("roundtrip agent created", r.status_code == 201, r.text)
        rt_id = r.json()["id"]

        # editAgent(id) -> GET
        r = await client.get(f"/admin/api/agents/{rt_id}")
        check("roundtrip agent loaded", r.status_code == 200, r.text)
        loaded = r.json()

        # saveAgent() -> PUT, built from the loaded values exactly as the
        # form's read/populate + collect logic does.
        ui_payload = {
            "name": loaded["name"],
            "description": loaded["description"],
            "instructions": loaded["instructions"],
            "mcp_servers": loaded["mcp_servers"],
            "skills": loaded["skills"],
            "enabled": loaded["enabled"],
            "expose_chat": loaded["expose_chat"],
            "expose_api": loaded["expose_api"],
            "api_slug": loaded["api_slug"] or "",
            "run_as_principal": loaded["run_as_principal"] or "",
            "run_prompt": loaded["run_prompt"] or "",
            "run_timeout_seconds": loaded["run_timeout_seconds"],
        }
        r = await client.put(f"/admin/api/agents/{rt_id}", json=ui_payload)
        check("roundtrip save 200", r.status_code == 200, r.text)
        saved = r.json()
        for field, expected in [
            ("expose_chat", False),
            ("expose_api", True),
            ("api_slug", "ui-roundtrip"),
            ("run_as_principal", "svc-roundtrip@example.com"),
            ("run_prompt", "Perform your configured check now."),
            ("run_timeout_seconds", 900),
        ]:
            check(
                f"roundtrip preserves {field}",
                saved.get(field) == expected,
                f"got {saved.get(field)!r}",
            )

        r = await client.delete(f"/admin/api/agents/{rt_id}")
        check("roundtrip agent cleanup", r.status_code == 204, r.text)

        # --- Export -> import round-trip ------------------------------------
        # Import-with-replace is the documented dev->prod promotion path, so
        # an export that carries the exposure fields but an import that drops
        # them silently un-schedules every API agent on first promotion.
        # run_as_principal is the deliberate exception: it is a
        # landscape-specific service identity, so it must NOT travel in the
        # export -- and an import must not wipe the one already configured.
        print("\n== export -> import round-trip preserves exposure ==")
        r = await client.post("/admin/api/agents", json={
            "name": "Export Roundtrip", "description": "d", "instructions": "i",
            "mcp_servers": [{"url": "https://ex.example.com/mcp", "auth_mode": "none"}],
            "expose_chat": False,
            "expose_api": True,
            "api_slug": "export-roundtrip",
            "run_as_principal": "svc-export@example.com",
            "run_prompt": "Run the nightly check.",
            "run_timeout_seconds": 1200,
        })
        check("export roundtrip agent created", r.status_code == 201, r.text)
        er_id = r.json()["id"]

        # --- multi-line run_prompt survives the round trip --------------------
        # The admin form's Run prompt is a <textarea> precisely so a prompt can
        # carry fenced mermaid syntax templates the model must copy verbatim.
        # That is worthless if a newline does not survive PUT -> DB -> GET.
        print("\n== multi-line run_prompt round trip ==")
        MULTILINE = (
            "Line one.\n\n"
            "```mermaid\n"
            "pie title T\n"
            '    "a" : 1\n'
            "```\n\n"
            "Line after fence."
        )
        r = await client.put(f"/admin/api/agents/{er_id}", json={
            "name": "Export Roundtrip",
            "description": "d",
            "instructions": "i",
            "mcp_servers": [{"url": "https://ex.example.com/mcp", "auth_mode": "none"}],
            "expose_api": True,
            "api_slug": "export-roundtrip",
            "run_prompt": MULTILINE,
        })
        check("multi-line prompt accepted", r.status_code == 200, r.text[:200])
        r = await client.get(f"/admin/api/agents/{er_id}")
        got = r.json().get("run_prompt") or ""
        check("newlines preserved exactly", got == MULTILINE, repr(got[:80]))
        check("fence survives", "```mermaid" in got)
        check("text after the fence survives", got.rstrip().endswith("Line after fence."))

        # Restore the prompt the export assertions below expect.
        r = await client.put(f"/admin/api/agents/{er_id}", json={
            "name": "Export Roundtrip",
            "description": "d",
            "instructions": "i",
            "mcp_servers": [{"url": "https://ex.example.com/mcp", "auth_mode": "none"}],
            "expose_chat": False,
            "expose_api": True,
            "api_slug": "export-roundtrip",
            "run_as_principal": "svc-export@example.com",
            "run_prompt": "Run the nightly check.",
            "run_timeout_seconds": 1200,
        })
        check("prompt restored for export checks", r.status_code == 200, r.text[:200])

        r = await client.get("/admin/api/export")
        exported = next(
            (a for a in r.json()["agents"] if a["name"] == "Export Roundtrip"), None
        )
        check("agent present in export", exported is not None)
        for field, expected in [
            ("expose_chat", False),
            ("expose_api", True),
            ("api_slug", "export-roundtrip"),
            ("run_prompt", "Run the nightly check."),
            ("run_timeout_seconds", 1200),
        ]:
            check(
                f"export carries {field}",
                (exported or {}).get(field) == expected,
                f"got {(exported or {}).get(field)!r}",
            )
        check(
            "export omits run_as_principal (landscape-specific)",
            "run_as_principal" not in (exported or {}),
            f"got {exported!r}",
        )

        # Drift the target landscape: everything exposure-related reset,
        # except the principal, which this landscape owns.
        r = await client.put(f"/admin/api/agents/{er_id}", json={
            "name": "Export Roundtrip", "description": "d", "instructions": "i",
            "mcp_servers": [{"url": "https://ex.example.com/mcp", "auth_mode": "none"}],
            "expose_chat": True,
            "expose_api": False,
            "api_slug": "",
            "run_as_principal": "svc-export@example.com",
            "run_prompt": "",
            "run_timeout_seconds": 1800,
        })
        check("exposure reset before import", r.status_code == 200, r.text)

        r = await client.post(
            "/admin/api/import", json={"agents": [exported], "replace": False}
        )
        check("import of export succeeds", r.status_code == 200, r.text)
        r = await client.get(f"/admin/api/agents/{er_id}")
        reimported = r.json()
        for field, expected in [
            ("expose_chat", False),
            ("expose_api", True),
            ("api_slug", "export-roundtrip"),
            ("run_prompt", "Run the nightly check."),
            ("run_timeout_seconds", 1200),
        ]:
            check(
                f"import restores {field}",
                reimported.get(field) == expected,
                f"got {reimported.get(field)!r}",
            )
        check(
            "import preserves existing run_as_principal",
            reimported.get("run_as_principal") == "svc-export@example.com",
            f"got {reimported.get('run_as_principal')!r}",
        )

        r = await client.delete(f"/admin/api/agents/{er_id}")
        check("export roundtrip cleanup", r.status_code == 204, r.text)

        # --- Workflows survive the export -> import round trip ---------------
        # A workflow is promoted between landscapes the same way an agent is,
        # so the bundle has to carry its branches and its per-branch steps --
        # otherwise the target landscape gets a workflow that either does not
        # exist or exists with no definition. run_as_principal is the same
        # deliberate exception as for agents.
        print("\n== workflows survive the export -> import round trip ==")
        for wf_agent, host in (("WF Reader", "wfreader"), ("WF Abap", "wfabap"),
                               ("WF Drafter", "wfdrafter")):
            r = await client.post("/admin/api/agents", json={
                "name": wf_agent, "description": "d", "instructions": "i",
                "mcp_servers": [{"url": f"https://{host}.example.com/mcp",
                                 "auth_mode": "none"}],
            })
            check(f"{wf_agent} created for the workflow", r.status_code == 201,
                  r.text[:200])

        WF_DEF = {
            "name": "Export Workflow",
            "description": "Triage and draft.",
            "api_slug": "export-workflow",
            "run_as_principal": "svc-wf@example.com",
            "run_timeout_seconds": 1200,
            "skip_seen_items": False,
            "max_parallel_items": 3,
            "on_unknown_branch": "skip",
            "enabled": True,
            "branches": [
                {"key": "abap", "description": "ABAP dumps", "position": 1},
                {"key": "fiori", "description": "UI issues", "position": 2},
            ],
            "steps": [
                {"branch_key": None, "position": 1, "agent_name": "WF Reader",
                 "instructions": "triage", "fan_out": True,
                 "step_timeout_seconds": 300},
                {"branch_key": None, "position": 2, "agent_name": "WF Drafter",
                 "instructions": "draft", "fan_out": False,
                 "step_timeout_seconds": 400},
                # A multi-step branch: order inside a branch is exactly what a
                # flattened export loses.
                {"branch_key": "abap", "position": 1, "agent_name": "WF Abap",
                 "instructions": "read the dump", "fan_out": False,
                 "step_timeout_seconds": 500},
                {"branch_key": "abap", "position": 2, "agent_name": "WF Drafter",
                 "instructions": "summarize the dump", "fan_out": False,
                 "step_timeout_seconds": 600},
                {"branch_key": "fiori", "position": 1, "agent_name": "WF Reader",
                 "instructions": "check the UI", "fan_out": False,
                 "step_timeout_seconds": 700},
            ],
        }
        r = await client.post("/admin/api/workflows", json=WF_DEF)
        check("export workflow created", r.status_code == 201, r.text[:300])
        wf_id = r.json()["id"]

        r = await client.get("/admin/api/export")
        check("export 200", r.status_code == 200, r.text[:200])
        bundle = r.json()
        wf_exported = next(
            (w for w in bundle.get("workflows", []) if w["name"] == "Export Workflow"),
            None,
        )
        check("workflow present in the export", wf_exported is not None,
              str(bundle.get("workflows"))[:300])
        check("export carries the branches",
              [b["key"] for b in (wf_exported or {}).get("branches", [])]
              == ["abap", "fiori"], str((wf_exported or {}).get("branches")))
        check("export carries every step",
              len((wf_exported or {}).get("steps", [])) == 5,
              str((wf_exported or {}).get("steps")))
        check("workflow export omits run_as_principal (landscape-specific)",
              "run_as_principal" not in (wf_exported or {}), str(wf_exported))

        # Drift the target landscape: the definition is gutted down to a single
        # main-line step, so a no-op import cannot produce a false pass.
        # run_as_principal stays -- this landscape owns it.
        r = await client.put(f"/admin/api/workflows/{wf_id}", json={
            "name": "Export Workflow",
            "description": "gutted",
            "api_slug": "",
            "run_as_principal": "svc-wf@example.com",
            "run_timeout_seconds": 1800,
            "skip_seen_items": True,
            "max_parallel_items": 1,
            "on_unknown_branch": "fail",
            "enabled": False,
            "branches": [],
            "steps": [
                {"branch_key": None, "position": 1, "agent_name": "WF Reader",
                 "instructions": "nothing", "fan_out": False,
                 "step_timeout_seconds": 600},
            ],
        })
        check("workflow gutted before import", r.status_code == 200, r.text[:300])

        # The whole export body, posted verbatim -- the promotion an operator
        # actually performs, not a hand-built subset of it.
        r = await client.post("/admin/api/import", json=bundle)
        check("import of the export bundle succeeds", r.status_code == 200,
              r.text[:300])
        check("import reports the workflows it carried",
              r.json().get("imported_workflows", 0) >= 1, r.text[:200])

        r = await client.get(f"/admin/api/workflows/{wf_id}")
        restored = r.json()
        for field in ("description", "api_slug", "run_timeout_seconds",
                      "skip_seen_items", "max_parallel_items",
                      "on_unknown_branch", "enabled"):
            check(f"import restores {field}",
                  restored.get(field) == WF_DEF[field],
                  f"got {restored.get(field)!r}, want {WF_DEF[field]!r}")
        check("import preserves this landscape's run_as_principal",
              restored.get("run_as_principal") == "svc-wf@example.com",
              f"got {restored.get('run_as_principal')!r}")
        check("import restores the branches",
              [(b["key"], b["description"], b["position"])
               for b in restored.get("branches", [])]
              == [("abap", "ABAP dumps", 1), ("fiori", "UI issues", 2)],
              str(restored.get("branches")))
        want_steps = sorted(
            (str(s["branch_key"]), s["position"], s["agent_name"],
             s["instructions"], s["fan_out"], s["step_timeout_seconds"])
            for s in WF_DEF["steps"]
        )
        got_steps = sorted(
            (str(s["branch_key"]), s["position"], s["agent_name"],
             s["instructions"], s["fan_out"], s["step_timeout_seconds"])
            for s in restored.get("steps", [])
        )
        check("import restores every step, branch and position intact",
              got_steps == want_steps, f"got {got_steps}\nwant {want_steps}")

        r = await client.delete(f"/admin/api/workflows/{wf_id}")
        check("workflow roundtrip cleanup", r.status_code in (200, 204), r.text)

        # --- Writers that do not carry peers/model_name must not wipe them --
        # The UI5 admin builds its PUT body from an explicit field list that
        # has neither key, and any bundle exported before those fields existed
        # carries neither either. With plain pydantic defaults ([] and "")
        # both arrive as "clear it", so one save from /ui5admin -- or one
        # import of an old bundle -- silently deletes a configured peer list
        # and model override, and reports success. Absent must mean "keep".
        print("\n== absent peers/model_name are kept, explicit blanks clear ==")
        BASE = {
            "name": "Keep Fields Agent", "description": "d", "instructions": "i",
            "mcp_servers": [{"url": "https://keep.example.com/mcp", "auth_mode": "none"}],
        }
        r = await client.post("/admin/api/agents", json={
            **BASE, "peers": ["Export Roundtrip"], "model_name": "gpt-4o-mini",
        })
        check("keep-fields agent created", r.status_code == 201, r.text)
        kf_id = r.json()["id"]
        check("created with peers", r.json()["peers"] == ["Export Roundtrip"], r.text)
        check("created with model_name", r.json()["model_name"] == "gpt-4o-mini", r.text)

        # 1. A PUT that omits both keys -- the UI5 admin's body shape.
        r = await client.put(f"/admin/api/agents/{kf_id}", json=dict(BASE))
        check("PUT without the new fields succeeds", r.status_code == 200, r.text)
        saved = r.json()
        check("omitted peers are kept",
              saved["peers"] == ["Export Roundtrip"], f"got {saved['peers']!r}")
        check("omitted model_name is kept",
              saved["model_name"] == "gpt-4o-mini", f"got {saved['model_name']!r}")
        # Re-read: prove it is the stored row, not just the echoed response.
        r = await client.get(f"/admin/api/agents/{kf_id}")
        check("omitted fields still stored after re-read",
              r.json()["peers"] == ["Export Roundtrip"]
              and r.json()["model_name"] == "gpt-4o-mini", r.text)

        # 2. The HTML admin always sends both, so clearing must still work.
        r = await client.put(f"/admin/api/agents/{kf_id}", json={
            **BASE, "peers": [], "model_name": "",
        })
        check("PUT with explicit blanks succeeds", r.status_code == 200, r.text)
        saved = r.json()
        check("explicit empty list clears peers",
              saved["peers"] == [], f"got {saved['peers']!r}")
        check("explicit empty string clears model_name",
              saved["model_name"] == "", f"got {saved['model_name']!r}")
        r = await client.get(f"/admin/api/agents/{kf_id}")
        check("cleared fields stay cleared after re-read",
              r.json()["peers"] == [] and r.json()["model_name"] == "", r.text)

        # 3. An import bundle without the keys -- a pre-branch export -- keeps
        # whatever this landscape already has.
        r = await client.put(f"/admin/api/agents/{kf_id}", json={
            **BASE, "peers": ["Export Roundtrip"], "model_name": "gpt-4o-mini",
        })
        check("keep-fields agent re-armed", r.status_code == 200, r.text)
        r = await client.post("/admin/api/import", json={"agents": [{
            "name": "Keep Fields Agent", "description": "d", "instructions": "i",
            "mcp_servers": [{"url": "https://keep.example.com/mcp", "auth_mode": "none"}],
            "enabled": True, "expose_chat": True, "expose_api": False,
            "api_slug": "", "run_prompt": "", "run_timeout_seconds": 1800,
        }], "replace": False})
        check("legacy bundle imports", r.status_code == 200, r.text)
        r = await client.get(f"/admin/api/agents/{kf_id}")
        legacy = r.json()
        check("import without peers keeps them",
              legacy["peers"] == ["Export Roundtrip"], f"got {legacy['peers']!r}")
        check("import without model_name keeps it",
              legacy["model_name"] == "gpt-4o-mini", f"got {legacy['model_name']!r}")

        # 5. A model this landscape does not offer is saved, not rejected:
        # available_models() is an env override / a live AI Core query / a
        # static fallback, so it goes stale exactly when an operator most
        # needs to save, and one landscape-specific name must not 422 a whole
        # bundle. registry._model_for already degrades safely at build time.
        print("\n== an unavailable model override is accepted, not 422 ==")
        r = await client.put(f"/admin/api/agents/{kf_id}", json={
            **BASE, "model_name": "gpt-5-not-deployed-here",
        })
        check("unavailable model saved", r.status_code == 200, r.text)
        check("unavailable model stored",
              r.json().get("model_name") == "gpt-5-not-deployed-here", r.text)
        r = await client.post("/admin/api/import", json={"agents": [{
            **BASE, "model_name": "gpt-5-not-deployed-here",
            "api_slug": None, "peers": [],
        }], "replace": False})
        check("unavailable model imported", r.status_code == 200, r.text)
        check("import reports the unavailable model",
              any("gpt-5-not-deployed-here" in w for w in r.json().get("warnings", [])),
              r.text)

        r = await client.delete(f"/admin/api/agents/{kf_id}")
        check("keep-fields cleanup", r.status_code == 204, r.text)

        # --- 4. Export -> import posted VERBATIM ---------------------------
        # "Export config" then "Import config" in the admin UI posts the
        # export body unchanged. to_export() emits api_slug: null for every
        # agent without a slug (the normal case), and AgentPayload.api_slug is
        # typed str -- in pydantic v2 a default only applies to an ABSENT key,
        # so an explicit null 422s the whole bundle.
        print("\n== verbatim export -> import round trip ==")
        r = await client.post("/admin/api/agents", json={
            "name": "No Slug Agent", "description": "d", "instructions": "i",
            "mcp_servers": [{"url": "https://noslug.example.com/mcp", "auth_mode": "none"}],
        })
        check("slugless agent created", r.status_code == 201, r.text)
        ns_id = r.json()["id"]
        check("slugless agent really has no slug",
              r.json().get("api_slug", "missing") is None, r.text)

        r = await client.get("/admin/api/export")
        check("export for verbatim import 200", r.status_code == 200, r.text)
        bundle = r.json()
        check("export carries a null api_slug",
              any(a.get("api_slug") is None for a in bundle["agents"]),
              str([a.get("api_slug") for a in bundle["agents"]]))
        r = await client.post("/admin/api/import", json=bundle)
        check("verbatim export imports", r.status_code == 200, r.text[:400])
        r = await client.get(f"/admin/api/agents/{ns_id}")
        check("slugless agent survived the round trip",
              r.status_code == 200 and r.json().get("api_slug", "missing") is None,
              r.text)
        r = await client.delete(f"/admin/api/agents/{ns_id}")
        check("slugless cleanup", r.status_code == 204, r.text)


        # --- /admin/api/runs limit is capped --------------------------------
        print("\n== GET /admin/api/runs limit bounds ==")
        r = await client.get("/admin/api/runs", params={"limit": 100000})
        check("oversized limit rejected", r.status_code == 422, r.text)
        r = await client.get("/admin/api/runs", params={"limit": 0})
        check("zero limit rejected", r.status_code == 422, r.text)
        r = await client.get("/admin/api/runs", params={"limit": 10})
        check("in-range limit accepted", r.status_code == 200, r.text)

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

        # start_run creates the run row (the overlap lock) and returns before
        # the background task does any work, so this second POST -- issued
        # before draining the first's task -- lands on a run that is already
        # `running` and must be refused.
        r2 = await client.post("/api/agents/run-agent/run")
        check("second concurrent scheduled run is 409", r2.status_code == 409, r2.text)

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
        check("missing_sections gone from run payload",
              "missing_sections" not in r.json(),
              str(sorted(r.json().keys())))

        r = await client.get("/admin/api/runs/does-not-exist")
        check("unknown run id -> 404", r.status_code == 404, r.text)

        print("\n== GET /admin/api/runs/{id}/report.md ==")
        from agents.db import finish_job_run, SessionLocal as _SL

        async with _SL() as s:
            await finish_job_run(s, sched_run_id, status="success",
                                 summary="one line",
                                 report={"summary": "one line",
                                         "body_md": "# What's new\n\n| a |\n| --- |\n"})
        r = await client.get(f"/admin/api/runs/{sched_run_id}/report.md")
        check("200", r.status_code == 200, f"got {r.status_code}: {r.text[:200]}")
        check("markdown content type",
              r.headers["content-type"].startswith("text/markdown"),
              r.headers.get("content-type"))
        check("attachment filename",
              f'filename="run-{sched_run_id}.md"' in r.headers.get("content-disposition", ""),
              r.headers.get("content-disposition"))
        check("body is the markdown verbatim", r.text.startswith("# What's new"))

        async with _SL() as s:
            await finish_job_run(s, sched_run_id, status="success", summary="old",
                                 report={"summary": "old", "sections": []})
        r = await client.get(f"/admin/api/runs/{sched_run_id}/report.md")
        check("legacy run 404s", r.status_code == 404, f"got {r.status_code}")

        r = await client.get("/admin/api/runs/does-not-exist/report.md")
        check("unknown run 404s", r.status_code == 404)

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

        # --- whoami: the caller's own principal ----------------------------
        # run_as_principal must hold the opaque XSUAA principal (user_uuid/sub),
        # not an email — an admin cannot know or type that, so the UI has to
        # offer it. Without this the field is unusable by construction.
        print("\n== whoami ==")
        r = await client.get("/admin/api/whoami")
        # Note: an unmatched /admin/api/* path falls through to the chat app
        # mounted at "/", which answers 200 with HTML — so assert on the body
        # type, not just the status, or a missing route looks like a pass.
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
        check("whoami returns JSON", body is not None, r.text[:120])
        check("whoami exposes principal", isinstance(body, dict) and "principal" in body, r.text[:120])
        check("whoami exposes a display label", isinstance(body, dict) and "label" in body, r.text[:120])

        # --- Config (public base URL for OAuth sign-in links) ---------------
        # Same body-type guard as whoami above: an unmatched /admin/api/*
        # path falls through to the chat app mounted at "/", which answers
        # 200 with HTML, so asserting on status alone would pass against a
        # route that doesn't exist.
        r = await client.get("/admin/api/config")
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
        check("config returns JSON", body is not None, r.text[:120])
        check("config exposes public_base_url", isinstance(body, dict) and "public_base_url" in body, r.text[:120])
        check(
            "public_base_url is a string",
            isinstance(body, dict) and isinstance(body.get("public_base_url"), str),
            r.text[:120],
        )

        os.environ["PUBLIC_BASE_URL"] = "https://example.test"
        r = await client.get("/admin/api/config")
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
        check(
            "config prefers PUBLIC_BASE_URL",
            isinstance(body, dict) and body.get("public_base_url") == "https://example.test",
            r.text[:120],
        )
        os.environ.pop("PUBLIC_BASE_URL", None)

        os.environ["A2A_PUBLIC_URL"] = "https://a2a.example.test"
        r = await client.get("/admin/api/config")
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
        check(
            "config falls back to A2A_PUBLIC_URL",
            isinstance(body, dict) and body.get("public_base_url") == "https://a2a.example.test",
            r.text[:120],
        )
        os.environ.pop("A2A_PUBLIC_URL", None)

        # --- credential status per MCP server ------------------------------
        # Shows whether a given principal actually holds a token for each of
        # the agent's oauth2 servers, so a misconfigured run-as is visible in
        # the form instead of surfacing as a failed run hours later.
        print("\n== credential status ==")
        r = await client.post("/admin/api/agents", json={
            "name": "Cred Agent", "description": "d", "instructions": "i",
            "mcp_url": "https://cred.example.com/mcp", "auth_mode": "none",
            "expose_api": True, "api_slug": "cred-agent",
        })
        check("create agent for credential check", r.status_code == 201, r.text)
        cred_id = r.json()["id"]

        r = await client.get(f"/admin/api/agents/{cred_id}/credentials")
        is_json = r.headers.get("content-type", "").startswith("application/json")
        check("credentials returns JSON", is_json, r.text[:120])
        servers = r.json() if is_json else []
        check("credentials lists every server", len(servers) == 1, r.text)
        check(
            "auth_mode none needs no token",
            servers[0]["auth_mode"] == "none" and servers[0]["needs_token"] is False,
            r.text,
        )

        r = await client.get("/admin/api/agents/999999/credentials")
        check("credentials 404s on unknown agent", r.status_code == 404, r.text)

        # An oauth2 server with no stored token for the principal reports False.
        r = await client.put(f"/admin/api/agents/{cred_id}", json={
            "name": "Cred Agent", "description": "d", "instructions": "i",
            "mcp_servers": [{
                "url": "https://cred.cfapps.eu20-001.hana.ondemand.com/mcp",
                "auth_mode": "oauth2",
                "oauth": {"dcr": True},
            }],
            "expose_api": True, "api_slug": "cred-agent",
        })
        check("switch server to oauth2", r.status_code == 200, r.text)
        r = await client.get(
            f"/admin/api/agents/{cred_id}/credentials", params={"principal": "nobody"}
        )
        srv = r.json()[0]
        check("oauth2 server needs a token", srv["needs_token"] is True, r.text)
        check("unknown principal has no token", srv["has_token"] is False, r.text)
        check("credentials exposes a login link", bool(srv.get("login_url")), r.text)
        check(
            "login link targets this server",
            "server=" in srv.get("login_url", "") and "agent=" in srv.get("login_url", ""),
            r.text,
        )

        # --- token validity ------------------------------------------------
        # The UI shows not just whether a token exists but whether it still
        # works: valid, refreshable (expired access token + refresh token),
        # expired (no refresh token), or none.
        from datetime import datetime, timedelta, timezone

        from agents.db import SessionLocal, upsert_user_token
        from agents.oauth2 import normalize_mcp_url

        check("no token -> token_state none", srv.get("token_state") == "none", r.text)
        check("no token -> expires_at null", srv.get("expires_at") is None, r.text)

        cred_key = normalize_mcp_url("https://cred.cfapps.eu20-001.hana.ondemand.com/mcp")
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        async with SessionLocal() as s:
            await upsert_user_token(
                s, user_id="cred-valid", server_key=cred_key,
                access_token="a", refresh_token=None, expires_at=future,
            )
            await upsert_user_token(
                s, user_id="cred-refresh", server_key=cred_key,
                access_token="a", refresh_token="r", expires_at=past,
            )
            await upsert_user_token(
                s, user_id="cred-expired", server_key=cred_key,
                access_token="a", refresh_token=None, expires_at=past,
            )

        r = await client.get(
            f"/admin/api/agents/{cred_id}/credentials", params={"principal": "cred-valid"}
        )
        srv = r.json()[0]
        check("valid token -> token_state valid", srv.get("token_state") == "valid", r.text)
        check("valid token -> expires_at reported", bool(srv.get("expires_at")), r.text)
        check("valid token -> has_token True", srv["has_token"] is True, r.text)

        r = await client.get(
            f"/admin/api/agents/{cred_id}/credentials", params={"principal": "cred-refresh"}
        )
        srv = r.json()[0]
        check(
            "expired+refresh -> token_state refreshable",
            srv.get("token_state") == "refreshable", r.text,
        )
        check("expired+refresh -> has_token stays True", srv["has_token"] is True, r.text)

        r = await client.get(
            f"/admin/api/agents/{cred_id}/credentials", params={"principal": "cred-expired"}
        )
        srv = r.json()[0]
        check(
            "expired without refresh -> token_state expired",
            srv.get("token_state") == "expired", r.text,
        )
        check("expired without refresh -> has_token False", srv["has_token"] is False, r.text)

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


# --- pytest-collected payload rules ----------------------------------------
# The script harness above needs a running app; these are pure model checks
# and are cheaper to express as plain pytest cases.


def test_oauth_payload_rejects_an_out_of_range_min_score():
    import pytest
    from pydantic import ValidationError

    from agents.admin import OAuthClientPayload

    with pytest.raises(ValidationError, match="between 0 and 10"):
        OAuthClientPayload(min_score="42")


def test_oauth_payload_carries_min_score_into_config():
    from agents.admin import OAuthClientPayload

    assert OAuthClientPayload(min_score="9.0").to_config()["min_score"] == "9.0"
