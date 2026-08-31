"""Workflow runner: preflight, ordering, prompts, fan-out, branches, failures.

Run:  python tests/test_workflow_runner.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = ROOT / "tests" / "_test_workflow_runner.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB}"
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ["MCP_URL_ALLOWLIST"] = ""
# run_as() refuses to bind an identity without a public base URL.
os.environ["PUBLIC_BASE_URL"] = "https://app.example.com"

from agents import job_runner  # noqa: E402
import agents.registry as registry_module  # noqa: E402
import agents.workflow_runner as wr  # noqa: E402
from agents.db import (  # noqa: E402
    SessionLocal,
    get_workflow,
    get_workflow_by_name,
    get_workflow_run,
    init_db,
    list_item_runs,
    list_step_runs,
    upsert_agent,
    upsert_workflow,
)
from agents.registry import BuildResult, registry  # noqa: E402

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


class FakeResult:
    def __init__(self, output):
        self.output = output


class FakeAgent:
    """Stands in for a built specialist.

    Records the prompt it was given and returns a scripted answer, so the
    engine's ordering and prompt assembly can be asserted without a model.
    """

    def __init__(self, name, answers=None, fail_with=None, delay=0.0):
        self.name = name
        self.answers = list(answers or [])
        self.fail_with = fail_with
        self.delay = delay
        self.prompts: list[str] = []

    async def run(self, prompt, **kwargs):
        self.prompts.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_with is not None:
            raise self.fail_with
        answer = self.answers.pop(0) if self.answers else f"{self.name}-out"
        return FakeResult(answer)


CALL_ORDER: list[str] = []


class OrderedAgent(FakeAgent):
    async def run(self, prompt, **kwargs):
        CALL_ORDER.append(self.name)
        return await super().run(prompt, **kwargs)


def install_specialists(mapping):
    """Point the registry at fake specialists, bypassing model construction."""
    registry._build = BuildResult(
        orchestrator=None, specialists=dict(mapping), mcp_clients=[], configs=[]
    )


async def seed_agents(names):
    async with SessionLocal() as s:
        for n in names:
            await upsert_agent(s, name=n, description=n, instructions=n,
                               mcp_servers=SERVERS, run_as_principal="svc@example.com")


async def run_workflow(name) -> str:
    async with SessionLocal() as s:
        wf = await get_workflow_by_name(s, name)
    run_id = await wr.start_workflow_run(wf, trigger="manual")
    # start_workflow_run detaches the task; wait for it to finish.
    await asyncio.gather(*[t for t in wr._tasks if not t.done()],
                         return_exceptions=True)
    return run_id


async def main() -> None:
    await init_db()
    # Credentials are checked by job_runner; the fake agents have no real MCP
    # servers, so approve them and test the refusal path explicitly below.
    job_runner._has_usable_credentials = lambda agent: asyncio.sleep(0, result=True)

    await seed_agents(["reader", "abap", "fiori", "drafter"])

    print("\n== prompt assembly ==")
    p = wr.build_prompt("do the thing", [])
    check("no From block when there is no source",
          p.startswith("## Your task") and "## From" not in p, p[:80])
    p = wr.build_prompt("draft it", [("abap", "found a dump"), ("fiori", "ui is fine")])
    check("one From block per source",
          p.count("## From ") == 2 and "## From abap" in p and "## From fiori" in p,
          p[:160])
    check("task comes last", p.rindex("## Your task") > p.rindex("## From "), p[:160])

    print("\n== a linear workflow runs its steps in order ==")
    CALL_ORDER.clear()
    # 'first' is deliberately slower than 'second' — order must come from the
    # declaration, never from which agent happens to finish sooner.
    first = OrderedAgent("first", answers=["step one output"], delay=0.05)
    second = OrderedAgent("second", answers=["step two output"])
    await seed_agents(["first", "second"])
    install_specialists({"first": first, "second": second})
    async with SessionLocal() as s:
        await upsert_workflow(
            s, name="linear", description="d", enabled=True, branches=[],
            steps=[
                {"branch_key": None, "position": 1, "agent_name": "first",
                 "instructions": "start", "fan_out": False,
                 "step_timeout_seconds": 60},
                {"branch_key": None, "position": 2, "agent_name": "second",
                 "instructions": "finish", "fan_out": False,
                 "step_timeout_seconds": 60},
            ],
        )
    run_id = await run_workflow("linear")

    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
        steps = await list_step_runs(s, run_id)
        items = await list_item_runs(s, run_id)
    check("declared order followed", CALL_ORDER == ["first", "second"], str(CALL_ORDER))
    check("run succeeded", row.status == "success", f"{row.status} / {row.error}")
    check("a step run per step", len(steps) == 2, str(len(steps)))
    check("no item runs without a fan-out step", items == [], str(items))
    check("step runs carry no item", all(st.item_run_id is None for st in steps))
    check("output recorded", steps[0].output == "step one output", str(steps[0].output))
    check("step 2 saw step 1's output",
          "step one output" in second.prompts[0], second.prompts[0][:200])
    check("step 2 saw its own instructions",
          "finish" in second.prompts[0], second.prompts[0][:200])
    check("step 1 had no From block", "## From" not in first.prompts[0],
          first.prompts[0][:120])

    print("\n== a step failure is recorded on the step run ==")
    failing = FakeAgent("failing", fail_with=RuntimeError("boom"))
    install_specialists({"first": failing, "second": second})
    run_id = await run_workflow("linear")
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
        steps = await list_step_runs(s, run_id)
    check("run failed", row.status == "failed", row.status)
    check("run error names the step and the exception",
          "first" in (row.error or "") and "boom" in (row.error or ""), row.error)
    check("one step run recorded (second never ran)", len(steps) == 1, str(len(steps)))
    check("step run recorded as failed", steps[0].status == "failed", steps[0].status)
    check("step run error names the exception",
          "boom" in (steps[0].error or ""), steps[0].error)

    print("\n== a step timeout is recorded on the step run ==")
    # The smallest step_timeout_seconds upsert_workflow accepts is 1 (an
    # Integer column; 0 is treated as "unset" and falls back to 600), so the
    # delay below just needs to comfortably outlast one second.
    slow_timeout = FakeAgent("slow_timeout", answers=["late"], delay=1.3)
    install_specialists({"first": slow_timeout, "second": second})
    async with SessionLocal() as s:
        await upsert_workflow(
            s, name="linear", description="d", enabled=True, branches=[],
            steps=[
                {"branch_key": None, "position": 1, "agent_name": "first",
                 "instructions": "start", "fan_out": False,
                 "step_timeout_seconds": 1},
                {"branch_key": None, "position": 2, "agent_name": "second",
                 "instructions": "finish", "fan_out": False,
                 "step_timeout_seconds": 60},
            ],
        )
    run_id = await run_workflow("linear")
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
        steps = await list_step_runs(s, run_id)
    check("run failed on timeout", row.status == "failed", row.status)
    check("run error mentions the timeout",
          "timeout" in (row.error or "").lower(), row.error)
    check("one step run recorded (second never ran)", len(steps) == 1, str(len(steps)))
    check("step run recorded as failed", steps[0].status == "failed", steps[0].status)
    check("step run error mentions the timeout",
          "timeout" in (steps[0].error or "").lower(), steps[0].error)

    print("\n== preflight refuses a disabled agent ==")
    async with SessionLocal() as s:
        await upsert_agent(s, name="third", description="t", instructions="t",
                           mcp_servers=SERVERS, run_as_principal="svc@example.com")
        await upsert_workflow(
            s, name="disabled_agent_wf", description="d", enabled=True, branches=[],
            steps=[
                {"branch_key": None, "position": 1, "agent_name": "third",
                 "instructions": "start", "fan_out": False,
                 "step_timeout_seconds": 60},
            ],
        )
        # Disable the agent only after the workflow references it — save-time
        # validation only runs when the workflow itself is (re)saved, so a
        # workflow can end up pointing at an agent that has since been
        # disabled without ever being re-saved itself.
        await upsert_agent(s, name="third", description="t", instructions="t",
                           mcp_servers=SERVERS, run_as_principal="svc@example.com",
                           enabled=False)
    install_specialists({"third": FakeAgent("third")})
    run_id = await run_workflow("disabled_agent_wf")
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
    check("run failed on disabled agent", row.status == "failed", row.status)
    check("error names the disabled agent", "third" in (row.error or ""), row.error)

    print("\n== preflight refuses an unbuilt agent ==")
    install_specialists({"first": first})  # 'second' is missing
    run_id = await run_workflow("linear")
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
        steps = await list_step_runs(s, run_id)
    check("run failed", row.status == "failed", row.status)
    check("error names the missing agent", "second" in (row.error or ""), row.error)
    check("nothing ran", steps == [], str(steps))

    print("\n== preflight refuses a stale credential ==")
    install_specialists({"first": first, "second": second})
    job_runner._has_usable_credentials = lambda agent: asyncio.sleep(0, result=False)
    run_id = await run_workflow("linear")
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
    check("run failed on credentials", row.status == "failed", row.status)
    check("error mentions authorization",
          "author" in (row.error or "").lower(), row.error)
    job_runner._has_usable_credentials = lambda agent: asyncio.sleep(0, result=True)

    print("\n== preflight refuses a step with no principal at all ==")
    async with SessionLocal() as s:
        await upsert_agent(s, name="second", description="s", instructions="s",
                           mcp_servers=SERVERS, run_as_principal="")
        await upsert_workflow(
            s, name="linear", description="d", enabled=True, branches=[],
            run_as_principal=None,
            steps=[
                {"branch_key": None, "position": 1, "agent_name": "first",
                 "instructions": "start", "fan_out": False,
                 "step_timeout_seconds": 60},
                {"branch_key": None, "position": 2, "agent_name": "second",
                 "instructions": "finish", "fan_out": False,
                 "step_timeout_seconds": 60},
            ],
        )
    run_id = await run_workflow("linear")
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
    check("run failed without a principal", row.status == "failed", row.status)
    check("error names the principal-less agent",
          "second" in (row.error or "") and "principal" in (row.error or "").lower(),
          row.error)

    print("\n== start_workflow_run refuses a disabled workflow ==")
    async with SessionLocal() as s:
        await upsert_workflow(
            s, name="disabled_wf_direct", description="d", enabled=False,
            branches=[], steps=[],
        )
        disabled_wf = await get_workflow_by_name(s, "disabled_wf_direct")
    refused_disabled = False
    try:
        await wr.start_workflow_run(disabled_wf, trigger="manual")
    except wr.RunRefused:
        refused_disabled = True
    check("disabled workflow refused", refused_disabled)

    print("\n== overlap is refused ==")
    async with SessionLocal() as s:
        await upsert_agent(s, name="second", description="s", instructions="s",
                           mcp_servers=SERVERS, run_as_principal="svc@example.com")
        wf = await get_workflow_by_name(s, "linear")
    slow = OrderedAgent("first", answers=["x"], delay=0.3)
    install_specialists({"first": slow, "second": second})
    first_id = await wr.start_workflow_run(wf, trigger="manual")
    refused = False
    try:
        await wr.start_workflow_run(wf, trigger="manual")
    except wr.RunRefused:
        refused = True
    check("second concurrent trigger refused", refused)
    await asyncio.gather(*[t for t in wr._tasks if not t.done()],
                         return_exceptions=True)
    check("first run still completed", first_id is not None)

    print("\n== overlap is refused under genuine concurrency ==")
    # The test above awaits the first call before issuing the second, so the
    # DB row alone (created by the first call) explains the refusal — the
    # lock's actual job, closing the check-and-create race between two calls
    # that observe "no active run" before either has created one, is never
    # exercised. Fire both through asyncio.gather so they genuinely race for
    # _lock_for's asyncio.Lock.
    async with SessionLocal() as s:
        wf = await get_workflow_by_name(s, "linear")
    install_specialists({
        "first": OrderedAgent("first", answers=["x"], delay=0.3),
        "second": second,
    })
    results = await asyncio.gather(
        wr.start_workflow_run(wf, trigger="manual"),
        wr.start_workflow_run(wf, trigger="manual"),
        return_exceptions=True,
    )
    concurrent_succeeded = [r for r in results if isinstance(r, str)]
    concurrent_refused = [r for r in results if isinstance(r, wr.RunRefused)]
    check("exactly one concurrent start succeeded",
          len(concurrent_succeeded) == 1, str(results))
    check("exactly one concurrent start was refused",
          len(concurrent_refused) == 1, str(results))
    await asyncio.gather(*[t for t in wr._tasks if not t.done()],
                         return_exceptions=True)

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
