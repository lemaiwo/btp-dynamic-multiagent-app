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

    print("\n== fan-out produces one item run per work item ==")
    items_out = [
        wr.WorkItem(id="m1", title="dump", branches=[], text="body one"),
        wr.WorkItem(id="m2", title="ui", branches=[], text="body two"),
    ]
    reader = FakeAgent("reader", answers=[items_out])
    drafter = FakeAgent("drafter")
    install_specialists({"reader": reader, "drafter": drafter})
    async with SessionLocal() as s:
        await upsert_workflow(
            s, name="fanout", description="d", enabled=True, branches=[],
            skip_seen_items=False,
            steps=[
                {"branch_key": None, "position": 1, "agent_name": "reader",
                 "instructions": "triage", "fan_out": True,
                 "step_timeout_seconds": 60},
                {"branch_key": None, "position": 2, "agent_name": "drafter",
                 "instructions": "draft", "fan_out": False,
                 "step_timeout_seconds": 60},
            ],
        )
    run_id = await run_workflow("fanout")
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
        items = await list_item_runs(s, run_id)
        steps = await list_step_runs(s, run_id)
    check("run succeeded", row.status == "success", f"{row.status} / {row.error}")
    check("one item run per item", len(items) == 2, str(len(items)))
    check("item keys recorded",
          sorted(i.item_key for i in items) == ["m1", "m2"],
          str([i.item_key for i in items]))
    check("counts recorded",
          (row.items_total, row.items_succeeded, row.items_failed) == (2, 2, 0),
          f"{row.items_total}/{row.items_succeeded}/{row.items_failed}")
    check("fan-out step run has no item",
          any(st.item_run_id is None and st.agent_name == "reader" for st in steps))
    check("post-fan-out step ran once per item",
          sum(1 for st in steps if st.agent_name == "drafter") == 2,
          str([st.agent_name for st in steps]))
    check("the drafter saw the item text",
          any("body one" in p for p in drafter.prompts), str(drafter.prompts)[:200])

    print("\n== an empty item list is a successful, empty run ==")
    reader = FakeAgent("reader", answers=[[]])
    install_specialists({"reader": reader, "drafter": FakeAgent("drafter")})
    run_id = await run_workflow("fanout")
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
        items = await list_item_runs(s, run_id)
    check("empty fan-out succeeds", row.status == "success", f"{row.status} / {row.error}")
    check("zero items recorded", row.items_total == 0 and items == [], str(items))

    print("\n== one failing item does not stop the others ==")
    class PickyDrafter(FakeAgent):
        async def run(self, prompt, **kwargs):
            self.prompts.append(prompt)
            if "body two" in prompt:
                raise RuntimeError("drafting blew up")
            return FakeResult("drafted")

    reader = FakeAgent("reader", answers=[[
        wr.WorkItem(id="ok1", title="a", branches=[], text="body one"),
        wr.WorkItem(id="bad", title="b", branches=[], text="body two"),
        wr.WorkItem(id="ok2", title="c", branches=[], text="body three"),
    ]])
    install_specialists({"reader": reader, "drafter": PickyDrafter("drafter")})
    run_id = await run_workflow("fanout")
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
        items = await list_item_runs(s, run_id)
    by_key = {i.item_key: i for i in items}
    check("run is partial", row.status == "partial", row.status)
    check("good items succeeded",
          by_key["ok1"].status == "success" and by_key["ok2"].status == "success",
          str({k: v.status for k, v in by_key.items()}))
    check("bad item failed", by_key["bad"].status == "failed", by_key["bad"].status)
    check("failure recorded on the item",
          "blew up" in (by_key["bad"].error or ""), by_key["bad"].error)
    check("counts reflect the split",
          (row.items_succeeded, row.items_failed) == (2, 1),
          f"{row.items_succeeded}/{row.items_failed}")

    print("\n== a step timeout fails only its own item ==")

    class SlowForOne(FakeAgent):
        async def run(self, prompt, **kwargs):
            self.prompts.append(prompt)
            if "body two" in prompt:
                await asyncio.sleep(5)  # far beyond the 1s step timeout below
            return FakeResult("drafted")

    reader = FakeAgent("reader", answers=[[
        wr.WorkItem(id="t-ok", title="a", branches=[], text="body one"),
        wr.WorkItem(id="t-slow", title="b", branches=[], text="body two"),
    ]])
    install_specialists({"reader": reader, "drafter": SlowForOne("drafter")})
    async with SessionLocal() as s:
        await upsert_workflow(
            s, name="timeouts", description="d", enabled=True, branches=[],
            skip_seen_items=False,
            steps=[
                {"branch_key": None, "position": 1, "agent_name": "reader",
                 "instructions": "triage", "fan_out": True,
                 "step_timeout_seconds": 60},
                {"branch_key": None, "position": 2, "agent_name": "drafter",
                 "instructions": "draft", "fan_out": False,
                 "step_timeout_seconds": 1},
            ],
        )
    run_id = await run_workflow("timeouts")
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
        items = {i.item_key: i for i in await list_item_runs(s, run_id)}
        steps = await list_step_runs(s, run_id)
    check("the fast item succeeded", items["t-ok"].status == "success",
          items["t-ok"].status)
    check("the slow item failed", items["t-slow"].status == "failed",
          items["t-slow"].status)
    check("the error names the timeout",
          "timeout" in (items["t-slow"].error or "").lower(), items["t-slow"].error)
    check("the run is partial", row.status == "partial", row.status)
    check("the timed-out step run is recorded failed",
          any(st.status == "failed" and "timeout" in (st.error or "").lower()
              for st in steps),
          str([(st.agent_name, st.status) for st in steps]))

    print("\n== skip_seen_items skips an item completed before ==")
    async with SessionLocal() as s:
        await upsert_workflow(
            s, name="dedup", description="d", enabled=True, branches=[],
            skip_seen_items=True,
            steps=[
                {"branch_key": None, "position": 1, "agent_name": "reader",
                 "instructions": "triage", "fan_out": True,
                 "step_timeout_seconds": 60},
                {"branch_key": None, "position": 2, "agent_name": "drafter",
                 "instructions": "draft", "fan_out": False,
                 "step_timeout_seconds": 60},
            ],
        )
    same_items = [wr.WorkItem(id="dup1", title="a", branches=[], text="body")]
    install_specialists({"reader": FakeAgent("reader", answers=[list(same_items)]),
                         "drafter": FakeAgent("drafter")})
    await run_workflow("dedup")
    second_drafter = FakeAgent("drafter")
    install_specialists({"reader": FakeAgent("reader", answers=[list(same_items)]),
                         "drafter": second_drafter})
    run_id = await run_workflow("dedup")
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
        items = await list_item_runs(s, run_id)
    check("repeat item is skipped", items[0].status == "skipped", items[0].status)
    check("skipped item ran no steps", second_drafter.prompts == [],
          str(second_drafter.prompts))
    check("skip counted", row.items_skipped == 1, str(row.items_skipped))
    check("a run of only skips still succeeds", row.status == "success", row.status)

    print("\n== the same key under a different workflow is not skipped ==")
    other_drafter = FakeAgent("drafter")
    install_specialists({"reader": FakeAgent("reader", answers=[list(same_items)]),
                         "drafter": other_drafter})
    async with SessionLocal() as s:
        await upsert_workflow(
            s, name="dedup-other", description="d", enabled=True, branches=[],
            skip_seen_items=True,
            steps=[
                {"branch_key": None, "position": 1, "agent_name": "reader",
                 "instructions": "triage", "fan_out": True,
                 "step_timeout_seconds": 60},
                {"branch_key": None, "position": 2, "agent_name": "drafter",
                 "instructions": "draft", "fan_out": False,
                 "step_timeout_seconds": 60},
            ],
        )
    await run_workflow("dedup-other")
    check("dedup is per workflow", other_drafter.prompts != [],
          "the item was skipped, but it belongs to a different workflow")

    print("\n== a step before the fan-out runs once and reaches its prompt ==")
    await seed_agents(["scanner"])
    scanner = FakeAgent("scanner", answers=["scan output"])
    reader = FakeAgent("reader", answers=[[
        wr.WorkItem(id="p1", title="a", branches=[], text="body one"),
    ]])
    drafter = FakeAgent("drafter")
    install_specialists({"scanner": scanner, "reader": reader, "drafter": drafter})
    async with SessionLocal() as s:
        await upsert_workflow(
            s, name="prefan", description="d", enabled=True, branches=[],
            skip_seen_items=False,
            steps=[
                {"branch_key": None, "position": 1, "agent_name": "scanner",
                 "instructions": "scan", "fan_out": False,
                 "step_timeout_seconds": 60},
                {"branch_key": None, "position": 2, "agent_name": "reader",
                 "instructions": "triage", "fan_out": True,
                 "step_timeout_seconds": 60},
                {"branch_key": None, "position": 3, "agent_name": "drafter",
                 "instructions": "draft", "fan_out": False,
                 "step_timeout_seconds": 60},
            ],
        )
    run_id = await run_workflow("prefan")
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
        steps = await list_step_runs(s, run_id)
    check("run succeeded", row.status == "success", f"{row.status} / {row.error}")
    check("the pre-fan-out step ran exactly once, with no item",
          sum(1 for st in steps if st.agent_name == "scanner") == 1 and
          all(st.item_run_id is None for st in steps if st.agent_name == "scanner"),
          str([(st.agent_name, st.item_run_id) for st in steps]))
    check("the fan-out step's prompt carried the pre-fan-out step's output",
          any("scan output" in p for p in reader.prompts), str(reader.prompts))
    check("the post-fan-out step ran once for the one item",
          sum(1 for st in steps if st.agent_name == "drafter") == 1,
          str([(st.agent_name, st.item_run_id) for st in steps]))

    print("\n== a create_item_run failure fails only that item ==")
    original_create_item_run = wr.create_item_run

    async def flaky_create_item_run(session, *, run_id, item_key, title, branches):
        if item_key == "boom":
            raise ValueError("simulated create_item_run failure")
        return await original_create_item_run(
            session, run_id=run_id, item_key=item_key, title=title, branches=branches,
        )

    wr.create_item_run = flaky_create_item_run
    try:
        reader = FakeAgent("reader", answers=[[
            wr.WorkItem(id="ok3", title="a", branches=[], text="body one"),
            wr.WorkItem(id="boom", title="b", branches=[], text="body two"),
        ]])
        install_specialists({"reader": reader, "drafter": FakeAgent("drafter")})
        run_id = await run_workflow("fanout")
    finally:
        wr.create_item_run = original_create_item_run
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
        items = await list_item_runs(s, run_id)
    check("run is partial when create_item_run raises for one item",
          row.status == "partial", row.status)
    check("the item whose row could not be created recorded no row",
          all(i.item_key != "boom" for i in items), str(items))
    check("the other item still succeeded",
          any(i.item_key == "ok3" and i.status == "success" for i in items), str(items))
    check("counts reflect the split",
          (row.items_succeeded, row.items_failed) == (1, 1),
          f"{row.items_succeeded}/{row.items_failed}")

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
