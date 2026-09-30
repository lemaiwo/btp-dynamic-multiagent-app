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

        print("\n== numeric fields are bounded ==")
        # Unbounded, `run_timeout_seconds: 0` makes every run of this workflow
        # fail instantly at asyncio.wait_for -- a save that reads as accepted
        # producing a workflow that can never run. The ceiling matters too: the
        # BTP scheduler's async timeout defaults to 30 minutes, so a run
        # allowed past 1800s is reported failed while it is still working.
        for label, field, value in [
            ("zero run timeout", "run_timeout_seconds", 0),
            ("run timeout past the scheduler's 30-minute limit",
             "run_timeout_seconds", 3600),
            ("zero parallelism", "max_parallel_items", 0),
            ("unbounded parallelism", "max_parallel_items", 500),
        ]:
            payload = {**GOOD, "name": "bounded", "api_slug": "bounded",
                       field: value,
                       "branches": [dict(b) for b in GOOD["branches"]],
                       "steps": [dict(s) for s in GOOD["steps"]]}
            r = await c.post("/admin/api/workflows", json=payload)
            check(f"{label} rejected", 400 <= r.status_code < 500,
                  f"{r.status_code} {r.text[:160]}")
        for label, value in [("zero step timeout", 0), ("huge step timeout", 99999)]:
            payload = {**GOOD, "name": "bounded", "api_slug": "bounded",
                       "branches": [dict(b) for b in GOOD["branches"]],
                       "steps": [dict(s) for s in GOOD["steps"]]}
            payload["steps"][0]["step_timeout_seconds"] = value
            r = await c.post("/admin/api/workflows", json=payload)
            check(f"{label} rejected", 400 <= r.status_code < 500,
                  f"{r.status_code} {r.text[:160]}")

        print("\n== duplicate slug ==")
        dup = {**GOOD, "name": "second"}
        r = await c.post("/admin/api/workflows", json=dup)
        check("duplicate slug rejected", 400 <= r.status_code < 500,
              f"{r.status_code} {r.text[:160]}")

        print("\n== a rejected rename must not stick ==")
        # Renaming and breaking the steps in the same PUT must not leave the
        # rename applied: the whole save is one unit, and a 400 here has to
        # mean nothing changed, not "renamed, but steps still broken."
        renamed_and_broken = {**GOOD, "name": "mail-triage-renamed",
                               "branches": [dict(b) for b in GOOD["branches"]],
                               "steps": [dict(s) for s in GOOD["steps"]]}
        renamed_and_broken["steps"][1]["fan_out"] = True  # two fan-out steps
        r = await c.put(f"/admin/api/workflows/{wf_id}", json=renamed_and_broken)
        check("rename + invalid steps rejected with 4xx",
              400 <= r.status_code < 500, f"{r.status_code} {r.text[:200]}")
        r = await c.get(f"/admin/api/workflows/{wf_id}")
        check("name was NOT renamed by the rejected save",
              r.json()["name"] == "mail-triage", r.text[:200])

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

        # --- step kinds ---
        print("\n== step kinds round-trip through the API ==")
        KINDS = {
            **GOOD, "name": "kinds", "api_slug": "kinds", "branches": [],
            "steps": [
                {"branch_key": None, "position": 1, "agent_name": "reader",
                 "instructions": "read", "fan_out": False, "step_timeout_seconds": 600},
                {"branch_key": None, "position": 2, "kind": "condition", "agent_name": "",
                 "instructions": "", "fan_out": False, "step_timeout_seconds": 60,
                 "config": {"rules": [{"when": {"source": "text", "op": "contains",
                                                "value": "nothing"},
                                       "then": {"action": "stop", "output": "Done."}}],
                            "else": {"action": "continue"}}},
                {"branch_key": None, "position": 3, "kind": "transform",
                 "config": {"template": "{{text}}", "truncate": 500}},
                {"branch_key": None, "position": 4, "kind": "python",
                 "config": {"code": "output = text.upper()", "timeout_seconds": 5}},
                {"branch_key": None, "position": 5, "kind": "http",
                 "config": {"destination": "jira", "method": "POST",
                            "path": "/rest/api/2/issue/{{item.id}}/comment",
                            "body": "{\"body\": \"{{text}}\"}"}},
                {"branch_key": None, "position": 6, "agent_name": "drafter",
                 "instructions": "draft", "fan_out": False, "step_timeout_seconds": 600},
            ],
        }
        r = await c.post("/admin/api/workflows", json=KINDS)
        check("created with mixed kinds", r.status_code in (200, 201),
              f"{r.status_code} {r.text[:300]}")
        kinds_id = r.json()["id"]
        body = r.json()
        kinds = [s["kind"] for s in body["steps"]]
        check("every step reports its kind",
              kinds == ["agent", "condition", "transform", "python", "http", "agent"], str(kinds))
        check("agent steps carry an empty config and non-agent steps an empty agent name",
              body["steps"][0]["config"] == {} and body["steps"][2]["agent_name"] == "",
              str(body["steps"][:3])[:300])
        check("configs are normalised and returned",
              body["steps"][1]["config"]["else"]["action"] == "continue"
              and body["steps"][4]["config"]["method"] == "POST"
              and body["steps"][4]["config"]["expect_status"][0] == 200,
              str(body["steps"][4])[:300])
        r = await c.get(f"/admin/api/workflows/{kinds_id}")
        check("GET returns the same kinds and configs",
              [s["kind"] for s in r.json()["steps"]] == kinds
              and r.json()["steps"][3]["config"]["code"] == "output = text.upper()",
              r.text[:300])

        print("\n== invalid kind configs are 4xx naming the step ==")
        for label, mutate, expect in [
            ("unknown kind", lambda d: d["steps"][2].update({"kind": "shell"}), "Step 3"),
            ("python that does not compile",
             lambda d: d["steps"][3].update({"config": {"code": "def ("}}), "Step 4 (python)"),
            ("http without a destination",
             lambda d: d["steps"][4].update({"config": {"path": "/x"}}), "Step 5 (http)"),
            ("http path escaping the destination",
             lambda d: d["steps"][4]["config"].update({"path": "https://evil.example.com/"}),
             "Step 5 (http)"),
            ("a deterministic fan-out step",
             lambda d: d["steps"][2].update({"fan_out": True}), "fan-out step must be an agent"),
            ("an agent step with no agent",
             lambda d: d["steps"][0].update({"agent_name": ""}), "Step 1"),
        ]:
            import copy  # noqa: PLC0415
            payload = copy.deepcopy(KINDS)
            payload.update({"name": "broken-kinds", "api_slug": "broken-kinds"})
            mutate(payload)
            r = await c.post("/admin/api/workflows", json=payload)
            check(f"{label} rejected with 4xx", 400 <= r.status_code < 500,
                  f"{r.status_code} {r.text[:160]}")
            check(f"{label} names the step", expect.lower() in r.text.lower(), r.text[:200])

        print("\n== export carries kinds and import accepts them ==")
        r = await c.get("/admin/api/export")
        check("export ok", r.status_code == 200, str(r.status_code))
        bundle = r.json()
        exported = next((w for w in bundle["workflows"] if w["name"] == "kinds"), None)
        check("exported workflow lists step kinds",
              exported is not None
              and [s["kind"] for s in exported["steps"]] == kinds
              and exported["steps"][4]["config"]["destination"] == "jira",
              str(exported)[:300])
        r = await c.delete(f"/admin/api/workflows/{kinds_id}")
        check("deleted before re-import", r.status_code in (200, 204), str(r.status_code))
        r = await c.post("/admin/api/import", json={"workflows": [exported]})
        check("import of the exported workflow succeeds", r.status_code == 200,
              f"{r.status_code} {r.text[:300]}")
        r = await c.get("/admin/api/workflows")
        imported = next((w for w in r.json() if w["name"] == "kinds"), None)
        check("imported workflow exists", imported is not None, r.text[:200])
        if imported is not None:
            r = await c.get(f"/admin/api/workflows/{imported['id']}")
            check("imported steps keep their kinds and configs",
                  [s["kind"] for s in r.json()["steps"]] == kinds
                  and r.json()["steps"][1]["config"]["rules"][0]["then"]["output"] == "Done.",
                  r.text[:300])
            broken = copy.deepcopy(exported)
            broken["steps"][3]["config"]["code"] = "def ("
            r = await c.post("/admin/api/import", json={"workflows": [broken]})
            check("import with a broken python step is 422 naming the step",
                  r.status_code == 422 and "step 4 (python)" in r.text.lower(),
                  f"{r.status_code} {r.text[:200]}")
            await c.delete(f"/admin/api/workflows/{imported['id']}")

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
