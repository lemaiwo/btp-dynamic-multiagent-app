"""Workflow definition storage and save-time validation.

Run:  python tests/test_workflow_config.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = ROOT / "tests" / "_test_workflow_config.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB}"
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ["MCP_URL_ALLOWLIST"] = ""

from agents.db import (  # noqa: E402
    SessionLocal,
    delete_agent,
    delete_workflow,
    get_agent_by_name,
    get_workflow_by_name,
    get_workflow_by_slug,
    get_workflow_parts,
    init_db,
    list_workflows,
    upsert_agent,
    upsert_workflow,
    validate_workflow_parts,
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


def rejects(label: str, branches, steps, *, known=("reader", "abap", "fiori", "drafter"),
            enabled=True, expect: str = "") -> None:
    try:
        validate_workflow_parts(branches, steps, set(known), enabled=enabled)
    except ValueError as e:
        check(label, expect.lower() in str(e).lower(), f"message was: {e}")
        return
    check(label, False, "no ValueError raised")


READER = {"branch_key": None, "position": 1, "agent_name": "reader",
          "instructions": "triage", "fan_out": True, "step_timeout_seconds": 600}
DRAFTER = {"branch_key": None, "position": 2, "agent_name": "drafter",
           "instructions": "draft", "fan_out": False, "step_timeout_seconds": 600}
ABAP_STEP = {"branch_key": "abap", "position": 1, "agent_name": "abap",
             "instructions": "analyze", "fan_out": False, "step_timeout_seconds": 600}
FIORI_STEP = {"branch_key": "fiori", "position": 1, "agent_name": "fiori",
              "instructions": "analyze", "fan_out": False, "step_timeout_seconds": 600}
ABAP_BRANCH = {"key": "abap", "description": "ABAP dumps", "position": 1}
FIORI_BRANCH = {"key": "fiori", "description": "UI5 issues", "position": 2}


async def main() -> None:
    await init_db()

    print("\n== the seed file carries workflows ==")
    # seed_from_file_if_empty only runs against an empty DB, so this has to be
    # the first thing this suite does. Unlike an export, the seed file IS
    # landscape-local, so it may carry run_as_principal -- the same
    # distinction the agents section makes.
    from agents.admin import seed_from_file_if_empty  # noqa: PLC0415

    seed = {
        "agents": [{
            "name": "seeded-reader", "description": "d", "instructions": "i",
            "mcp_servers": [{"url": "https://s.example.com/mcp",
                             "auth_mode": "none"}],
        }],
        "workflows": [{
            "name": "seeded-wf", "description": "from the seed file",
            "run_as_principal": "svc-seed@example.com",
            "branches": [{"key": "abap", "description": "ABAP", "position": 1}],
            "steps": [
                {"branch_key": None, "position": 1,
                 "agent_name": "seeded-reader", "instructions": "triage",
                 "fan_out": True, "step_timeout_seconds": 600},
                {"branch_key": "abap", "position": 1,
                 "agent_name": "seeded-reader", "instructions": "analyze",
                 "fan_out": False, "step_timeout_seconds": 600},
            ],
        }],
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(seed, f)
        seed_path = Path(f.name)
    try:
        await seed_from_file_if_empty(seed_path)
    finally:
        seed_path.unlink()
    async with SessionLocal() as s:
        seeded = await get_workflow_by_name(s, "seeded-wf")
        parts = await get_workflow_parts(s, seeded.id) if seeded else ([], [])
    check("the seed file's workflow was created", seeded is not None)
    check("its branches and steps came with it",
          len(parts[0]) == 1 and len(parts[1]) == 2,
          f"{len(parts[0])} branches, {len(parts[1])} steps")
    check("the seed file's run_as_principal is kept",
          seeded is not None and seeded.run_as_principal == "svc-seed@example.com",
          str(getattr(seeded, "run_as_principal", None)))
    # Leave the DB as the rest of this suite expects to find it.
    async with SessionLocal() as s:
        if seeded is not None:
            await delete_workflow(s, seeded.id)
        seeded_agent = await get_agent_by_name(s, "seeded-reader")
        if seeded_agent is not None:
            await delete_agent(s, seeded_agent.id)

    print("\n== validation rejects broken definitions ==")
    rejects("two fan-out steps",
            [], [READER, {**DRAFTER, "fan_out": True}], expect="one fan-out")
    rejects("branches without a fan-out step",
            [ABAP_BRANCH], [{**READER, "fan_out": False}, ABAP_STEP],
            expect="fan-out")
    rejects("unknown agent on the main line",
            [], [{**READER, "agent_name": "ghost"}], expect="ghost")
    rejects("unknown agent in a branch",
            [ABAP_BRANCH], [READER, {**ABAP_STEP, "agent_name": "ghost"}],
            expect="ghost")
    rejects("step naming an undeclared branch",
            [], [READER, ABAP_STEP], expect="abap")
    rejects("duplicate branch keys",
            [ABAP_BRANCH, {**ABAP_BRANCH, "position": 2}], [READER, ABAP_STEP],
            expect="duplicate")
    rejects("empty branch key",
            [{"key": "  ", "description": "d", "position": 1}], [READER],
            expect="empty")
    rejects("declared branch with no steps",
            [ABAP_BRANCH, FIORI_BRANCH], [READER, ABAP_STEP], expect="fiori")
    rejects("non-contiguous main-line positions",
            [], [READER, {**DRAFTER, "position": 3}], expect="contiguous")
    rejects("duplicate positions inside a branch",
            [ABAP_BRANCH],
            [READER, ABAP_STEP, {**ABAP_STEP, "agent_name": "abap"}],
            expect="duplicate")
    rejects("duplicate positions on the main line",
            [], [READER, {**DRAFTER, "position": 1}], expect="duplicate")
    rejects("non-contiguous positions inside a branch",
            [ABAP_BRANCH],
            [READER, ABAP_STEP, {**ABAP_STEP, "position": 3, "agent_name": "fiori"}],
            expect="contiguous")
    rejects("fan-out step nested in a branch",
            [ABAP_BRANCH],
            [{**READER, "fan_out": False}, {**ABAP_STEP, "fan_out": True}],
            expect="main line")
    rejects("enabling a workflow with no steps", [], [], expect="no steps")

    print("\n== validation accepts a good definition ==")
    ok = True
    try:
        validate_workflow_parts(
            [ABAP_BRANCH, FIORI_BRANCH],
            [READER, DRAFTER, ABAP_STEP, FIORI_STEP],
            {"reader", "abap", "fiori", "drafter"}, enabled=True,
        )
    except ValueError as e:
        ok = False
        print(f"    unexpected: {e}")
    check("full branching definition validates", ok)

    ok = True
    try:
        validate_workflow_parts([], [{**READER, "fan_out": False}],
                                {"reader"}, enabled=True)
    except ValueError as e:
        ok = False
        print(f"    unexpected: {e}")
    check("linear workflow with no fan-out and no branches validates", ok)

    ok = True
    try:
        validate_workflow_parts([], [], {"reader"}, enabled=False)
    except ValueError as e:
        ok = False
        print(f"    unexpected: {e}")
    check("a DISABLED workflow may have no steps", ok)

    print("\n== storage round-trip ==")
    servers = [{"url": "https://x.example.com/mcp", "auth_mode": "none"}]
    async with SessionLocal() as s:
        for n in ("reader", "abap", "fiori", "drafter"):
            await upsert_agent(s, name=n, description=n, instructions=n,
                               mcp_servers=servers)
        wf = await upsert_workflow(
            s, name="mail-triage", description="triage inbox",
            api_slug="mail-triage", run_as_principal="svc@example.com",
            run_timeout_seconds=1800, skip_seen_items=True, max_parallel_items=1,
            on_unknown_branch="fail", enabled=True,
            branches=[ABAP_BRANCH, FIORI_BRANCH],
            steps=[READER, DRAFTER, ABAP_STEP, FIORI_STEP],
        )
        check("workflow stored", wf.name == "mail-triage")
        branches, steps = await get_workflow_parts(s, wf.id)
        check("branches stored in position order",
              [b.key for b in branches] == ["abap", "fiori"],
              str([b.key for b in branches]))
        check("all steps stored", len(steps) == 4, str(len(steps)))
        main_line = [st for st in steps if st.branch_key is None]
        check("main line ordered", [st.agent_name for st in main_line] == ["reader", "drafter"],
              str([st.agent_name for st in main_line]))
        check("fan-out flag survives", main_line[0].fan_out == 1, str(main_line[0].fan_out))
        check("to_dict exposes the key fields",
              set(wf.to_dict()) >= {"id", "name", "api_slug", "enabled",
                                    "skip_seen_items", "on_unknown_branch"},
              str(sorted(wf.to_dict())))

    print("\n== lookups and replacement ==")
    async with SessionLocal() as s:
        check("lookup by slug", (await get_workflow_by_slug(s, "mail-triage")) is not None)
        check("lookup by name", (await get_workflow_by_name(s, "mail-triage")) is not None)
        check("listed", [w.name for w in await list_workflows(s)] == ["mail-triage"])
        wf = await get_workflow_by_name(s, "mail-triage")
        # Re-saving replaces branches and steps wholesale rather than diffing.
        await upsert_workflow(
            s, name="mail-triage", description="triage inbox",
            api_slug="mail-triage", run_as_principal="svc@example.com",
            run_timeout_seconds=1800, skip_seen_items=True, max_parallel_items=1,
            on_unknown_branch="skip", enabled=True,
            branches=[ABAP_BRANCH], steps=[READER, DRAFTER, ABAP_STEP],
        )
        branches, steps = await get_workflow_parts(s, wf.id)
        check("branches replaced, not appended", len(branches) == 1, str(len(branches)))
        check("steps replaced, not appended", len(steps) == 3, str(len(steps)))
        refreshed = await get_workflow_by_name(s, "mail-triage")
        check("scalar field updated", refreshed.on_unknown_branch == "skip")

    print("\n== duplicate slug is refused ==")
    async with SessionLocal() as s:
        try:
            await upsert_workflow(
                s, name="other", description="d", api_slug="mail-triage",
                run_as_principal=None, run_timeout_seconds=1800,
                skip_seen_items=True, max_parallel_items=1,
                on_unknown_branch="fail", enabled=True,
                branches=[], steps=[{**READER, "fan_out": False}],
            )
            check("duplicate api_slug rejected", False, "no ValueError")
        except ValueError as e:
            check("duplicate api_slug rejected", "slug" in str(e).lower(), str(e))

    print("\n== a disabled agent cannot be named by a step ==")
    async with SessionLocal() as s:
        await upsert_agent(s, name="retired", description="retired", instructions="retired",
                           mcp_servers=servers, enabled=False)
        try:
            await upsert_workflow(
                s, name="disabled-agent-wf", description="d", api_slug=None,
                run_as_principal=None, run_timeout_seconds=1800,
                skip_seen_items=True, max_parallel_items=1,
                on_unknown_branch="fail", enabled=True,
                branches=[], steps=[{**READER, "agent_name": "retired", "fan_out": False}],
            )
            check("disabled agent rejected", False, "no ValueError")
        except ValueError as e:
            check("disabled agent rejected", "retired" in str(e).lower(), str(e))

    print("\n== an invalid on_unknown_branch value is refused ==")
    async with SessionLocal() as s:
        try:
            await upsert_workflow(
                s, name="bad-on-unknown-branch", description="d", api_slug=None,
                run_as_principal=None, run_timeout_seconds=1800,
                skip_seen_items=True, max_parallel_items=1,
                on_unknown_branch="explode", enabled=True,
                branches=[], steps=[{**READER, "fan_out": False}],
            )
            check("invalid on_unknown_branch rejected", False, "no ValueError")
        except ValueError as e:
            check("invalid on_unknown_branch rejected",
                  "on_unknown_branch" in str(e).lower(), str(e))

    print("\n== delete removes the parts too ==")
    async with SessionLocal() as s:
        wf = await get_workflow_by_name(s, "mail-triage")
        wf_id = wf.id
        check("deleted", await delete_workflow(s, wf_id))
        branches, steps = await get_workflow_parts(s, wf_id)
        check("branches gone", branches == [], str(branches))
        check("steps gone", steps == [], str(steps))
        check("deleting again is False", not await delete_workflow(s, wf_id))

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
