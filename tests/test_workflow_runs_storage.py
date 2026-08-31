"""Workflow run / item run / step run records.

Run:  python tests/test_workflow_runs_storage.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = ROOT / "tests" / "_test_workflow_runs_storage.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB}"
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ["MCP_URL_ALLOWLIST"] = ""

from agents.db import (  # noqa: E402
    SessionLocal,
    active_workflow_run,
    create_item_run,
    create_step_run,
    create_workflow_run,
    finish_item_run,
    finish_step_run,
    finish_workflow_run,
    get_workflow,
    get_workflow_run,
    init_db,
    item_succeeded_before,
    list_item_runs,
    list_step_runs,
    list_workflow_runs,
    sweep_stale_workflow_runs,
    upsert_agent,
    upsert_workflow,
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


async def main() -> None:
    await init_db()
    servers = [{"url": "https://x.example.com/mcp", "auth_mode": "none"}]

    async with SessionLocal() as s:
        await upsert_agent(s, name="reader", description="r", instructions="r",
                           mcp_servers=servers)
        wf = await upsert_workflow(
            s, name="wf", description="d", enabled=True,
            steps=[{"branch_key": None, "position": 1, "agent_name": "reader",
                    "instructions": "go", "fan_out": True,
                    "step_timeout_seconds": 600}],
        )
        wf_id = wf.id

    print("\n== run lifecycle ==")
    async with SessionLocal() as s:
        wf = await get_workflow(s, wf_id)
        run = await create_workflow_run(s, workflow=wf, trigger="manual",
                                        created_by="me@example.com")
        run_id = run.id
        check("run starts running", run.status == "running", run.status)
        check("run knows its workflow", run.workflow_name == "wf")
        check("active run found", (await active_workflow_run(s, wf_id)) is not None)

    print("\n== item and step runs ==")
    async with SessionLocal() as s:
        item = await create_item_run(s, run_id=run_id, item_key="msg-1",
                                     title="ME21N dump", branches=["abap"])
        item_id = item.id
        step = await create_step_run(s, run_id=run_id, item_run_id=item_id,
                                     branch_key="abap", position=1,
                                     agent_name="reader")
        await finish_step_run(s, step.id, status="success", output="analysis")
        await finish_item_run(s, item_id, status="success")

        items = await list_item_runs(s, run_id)
        steps = await list_step_runs(s, run_id)
        check("item recorded", len(items) == 1 and items[0].item_key == "msg-1")
        check("selected branches recorded",
              items[0].to_dict()["branches"] == ["abap"],
              str(items[0].to_dict()["branches"]))
        check("step recorded", len(steps) == 1 and steps[0].output == "analysis")
        check("step tagged with its branch", steps[0].branch_key == "abap")
        step_dict = steps[0].to_dict()
        check("step dict carries its run id",
              step_dict["workflow_run_id"] == run_id, str(step_dict))
        check("step dict carries timing",
              step_dict["started_at"] is not None and step_dict["finished_at"] is not None,
              str(step_dict))

    print("\n== finishing the run ==")
    async with SessionLocal() as s:
        await finish_workflow_run(
            s, run_id, status="partial", summary="2 items: 1 ok, 1 failed",
            counts={"items_total": 2, "items_succeeded": 1,
                    "items_failed": 1, "items_skipped": 0},
        )
        row = await get_workflow_run(s, run_id)
        check("status recorded", row.status == "partial", row.status)
        check("counts recorded", row.items_total == 2 and row.items_failed == 1)
        check("finished_at set", row.finished_at is not None)
        check("no longer active", (await active_workflow_run(s, wf_id)) is None)
        check("listed", [r.id for r in await list_workflow_runs(s)] == [run_id])

    print("\n== repeat-run safety lookup ==")
    async with SessionLocal() as s:
        check("succeeded item is seen",
              await item_succeeded_before(s, workflow_id=wf_id, item_key="msg-1"))
        check("unknown item is not seen",
              not await item_succeeded_before(s, workflow_id=wf_id, item_key="msg-9"))

    print("\n== repeat-run safety is scoped per workflow ==")
    async with SessionLocal() as s:
        wf_other = await upsert_workflow(
            s, name="wf-other", description="d", enabled=True,
            steps=[{"branch_key": None, "position": 1, "agent_name": "reader",
                    "instructions": "go", "fan_out": True,
                    "step_timeout_seconds": 600}],
        )
        wf_other_id = wf_other.id
        check("same item_key under a different workflow is not seen",
              not await item_succeeded_before(
                  s, workflow_id=wf_other_id, item_key="msg-1"))

    print("\n== a failed item does not count as seen ==")
    async with SessionLocal() as s:
        wf = await get_workflow(s, wf_id)
        run2 = await create_workflow_run(s, workflow=wf, trigger="manual")
        it = await create_item_run(s, run_id=run2.id, item_key="msg-2",
                                   title="t", branches=[])
        await finish_item_run(s, it.id, status="failed", error="boom")
        await finish_workflow_run(s, run2.id, status="partial", summary="x")
        check("failed item is not seen",
              not await item_succeeded_before(s, workflow_id=wf_id, item_key="msg-2"))

    print("\n== stale sweep ==")
    async with SessionLocal() as s:
        wf = await get_workflow(s, wf_id)
        stuck = await create_workflow_run(s, workflow=wf, trigger="schedule")
        stuck_item = await create_item_run(s, run_id=stuck.id, item_key="msg-3",
                                           title="t", branches=[])
        stuck_step = await create_step_run(s, run_id=stuck.id, item_run_id=stuck_item.id,
                                           branch_key=None, position=1,
                                           agent_name="reader")
        swept = await sweep_stale_workflow_runs(s, all_running=True)
        check("stale run swept", swept == 1, str(swept))
        row = await get_workflow_run(s, stuck.id)
        check("swept run is interrupted", row.status == "interrupted", row.status)
        items_after = await list_item_runs(s, stuck.id)
        steps_after = await list_step_runs(s, stuck.id)
        check("swept item run is interrupted too",
              len(items_after) == 1 and items_after[0].status == "interrupted",
              str([i.status for i in items_after]))
        check("swept step run is interrupted too",
              len(steps_after) == 1 and steps_after[0].status == "interrupted",
              str([s_.status for s_ in steps_after]))

    print("\n== defensive errors instead of silent corruption ==")
    async with SessionLocal() as s:
        try:
            await create_item_run(s, run_id="no-such-run", item_key="x",
                                  title="t", branches=[])
            check("create_item_run rejects an unknown run_id", False,
                  "no exception raised")
        except ValueError:
            check("create_item_run rejects an unknown run_id", True)

        try:
            await finish_workflow_run(s, run_id, status="partial",
                                      counts={"items_bogus": 1})
            check("finish_workflow_run rejects an unknown counts key", False,
                  "no exception raised")
        except ValueError:
            check("finish_workflow_run rejects an unknown counts key", True)

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
