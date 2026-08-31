"""Workflow admin API: CRUD, validation surfacing, run-now, run views.

Run:  python tests/test_workflow_admin_api.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = ROOT / "tests" / "_test_workflow_admin.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB}"
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ["MCP_URL_ALLOWLIST"] = ""
os.environ["PUBLIC_BASE_URL"] = "https://app.example.com"

from httpx import ASGITransport, AsyncClient  # noqa: E402

import app as app_module  # noqa: E402
from agents.db import SessionLocal, init_db, upsert_agent  # noqa: E402

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


SERVERS = [{"url": "https://x.example.com/mcp", "auth_mode": "none"}]

GOOD = {
    "name": "mail-triage",
    "description": "triage the inbox",
    "api_slug": "mail-triage",
    "run_as_principal": "svc@example.com",
    "run_timeout_seconds": 1800,
    "skip_seen_items": True,
    "max_parallel_items": 1,
    "on_unknown_branch": "fail",
    "enabled": True,
    "branches": [{"key": "abap", "description": "ABAP", "position": 1}],
    "steps": [
        {"branch_key": None, "position": 1, "agent_name": "reader",
         "instructions": "triage", "fan_out": True, "step_timeout_seconds": 600},
        {"branch_key": None, "position": 2, "agent_name": "drafter",
         "instructions": "draft", "fan_out": False, "step_timeout_seconds": 600},
        {"branch_key": "abap", "position": 1, "agent_name": "abap",
         "instructions": "analyze", "fan_out": False, "step_timeout_seconds": 600},
    ],
}


async def main() -> None:
    await init_db()
    async with SessionLocal() as s:
        for n in ("reader", "abap", "drafter"):
            await upsert_agent(s, name=n, description=n, instructions=n,
                               mcp_servers=SERVERS, run_as_principal="svc@example.com")

    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        print("\n== create and read ==")
        r = await c.post("/admin/api/workflows", json=GOOD)
        check("created", r.status_code in (200, 201), f"{r.status_code} {r.text[:200]}")
        wf_id = r.json()["id"]

        r = await c.get("/admin/api/workflows")
        check("listed", any(w["name"] == "mail-triage" for w in r.json()), r.text[:200])

        r = await c.get(f"/admin/api/workflows/{wf_id}")
        body = r.json()
        check("detail carries branches", len(body["branches"]) == 1, r.text[:200])
        check("detail carries steps", len(body["steps"]) == 3, r.text[:200])

        print("\n== validation is surfaced as 4xx, not 500 ==")
        for label, mutate, expect in [
            ("two fan-out steps",
             lambda d: d["steps"][1].update({"fan_out": True}), "fan-out"),
            ("unknown agent",
             lambda d: d["steps"][0].update({"agent_name": "ghost"}), "ghost"),
            ("step in an undeclared branch",
             lambda d: d["steps"][2].update({"branch_key": "nope"}), "nope"),
            ("branch with no steps",
             lambda d: d["branches"].append(
                 {"key": "fiori", "description": "f", "position": 2}), "fiori"),
            ("non-contiguous positions",
             lambda d: d["steps"][1].update({"position": 5}), "contiguous"),
        ]:
            payload = {**GOOD, "name": "broken", "api_slug": "broken",
                       "branches": [dict(b) for b in GOOD["branches"]],
                       "steps": [dict(s) for s in GOOD["steps"]]}
            mutate(payload)
            r = await c.post("/admin/api/workflows", json=payload)
            check(f"{label} rejected with 4xx",
                  400 <= r.status_code < 500, f"{r.status_code} {r.text[:160]}")
            check(f"{label} explains why",
                  expect.lower() in r.text.lower(), r.text[:200])

        print("\n== duplicate slug ==")
        dup = {**GOOD, "name": "second"}
        r = await c.post("/admin/api/workflows", json=dup)
        check("duplicate slug rejected", 400 <= r.status_code < 500,
              f"{r.status_code} {r.text[:160]}")

        print("\n== update and delete ==")
        updated = {**GOOD, "description": "changed"}
        r = await c.put(f"/admin/api/workflows/{wf_id}", json=updated)
        check("updated", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
        check("change persisted", r.json()["description"] == "changed", r.text[:160])

        print("\n== run now ==")
        r = await c.post(f"/admin/api/workflows/{wf_id}/run")
        # The agents are not built in this suite, so the run starts and then
        # fails preflight — starting is what this asserts.
        check("run accepted", r.status_code in (200, 202),
              f"{r.status_code} {r.text[:200]}")
        run_id = r.json().get("run_id")
        check("run id returned", bool(run_id), r.text[:200])

        import agents.workflow_runner as wr  # noqa: PLC0415
        await asyncio.gather(*[t for t in wr._tasks if not t.done()],
                             return_exceptions=True)

        print("\n== run views ==")
        r = await c.get("/admin/api/workflow-runs")
        check("runs listed", any(x["id"] == run_id for x in r.json()), r.text[:200])
        r = await c.get(f"/admin/api/workflow-runs/{run_id}")
        body = r.json()
        check("detail has the run", body["run"]["id"] == run_id, r.text[:200])
        check("detail has items", "items" in body, r.text[:200])
        check("detail has steps", "steps" in body, r.text[:200])

        r = await c.delete(f"/admin/api/workflows/{wf_id}")
        check("deleted", r.status_code in (200, 204), str(r.status_code))

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
