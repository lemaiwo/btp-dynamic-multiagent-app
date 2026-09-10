"""Workflow runner: preflight, ordering, prompts, fan-out, branches, failures.

Run:  python tests/test_workflow_runner.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

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


def only_step(steps):
    """The single step run a blocked preflight records, or a blank stand-in.

    Without this, one missing row raises IndexError and takes the rest of the
    file's checks down with it -- a missing row should fail its own checks and
    nothing else.
    """
    if steps:
        return steps[0]
    return SimpleNamespace(agent_name=None, position=None, branch_key=None,
                           status=None, error=None, item_run_id=None)


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


# main() replaces this with a constant stub (the fakes have no real MCP
# servers), so the one test that needs the genuine credential gate -- the
# workflow-principal fallback -- has to capture it before that happens.
REAL_HAS_CREDENTIALS = job_runner._has_usable_credentials


def stub_credentials(result: bool = True):
    return lambda agent, principal=None: asyncio.sleep(0, result=result)


class PrincipalAgent(FakeAgent):
    """Records the identity bound while it ran."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.principals: list[str | None] = []

    async def run(self, prompt, **kwargs):
        from agents.auth import current_principal  # noqa: PLC0415

        self.principals.append(current_principal.get())
        return await super().run(prompt, **kwargs)


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
    job_runner._has_usable_credentials = lambda agent, principal=None: asyncio.sleep(0, result=True)

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

    print("\n== a work item must carry a non-empty id ==")
    # An empty id is worse than a missing one: item_succeeded_before matches on
    # it, so once one run records an item keyed "", every later item keyed ""
    # is skipped forever while skip_seen_items is on. The reader is told to
    # emit an id it cannot supply blank.
    from pydantic import ValidationError  # noqa: PLC0415

    try:
        wr.WorkItem(id="", title="t", branches=[], text="body")
        check("an empty item id is rejected", False, "no ValidationError raised")
    except ValidationError:
        check("an empty item id is rejected", True)
    check("a real item id is still accepted",
          wr.WorkItem(id="m1", text="body").id == "m1")

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
    # A preflight failure blocks the run before any step executes, but leaving
    # no step run at all makes the flow diagram a uniform grey -- it cannot say
    # where the run stopped. One failed step run is recorded against the step
    # that would have used the offending agent, so the stopping point is
    # visible without overstating what ran.
    st = only_step(steps)
    check("the blocked step is recorded as failed", len(steps) == 1, str(len(steps)))
    check("it is the step that names the unbuilt agent",
          st.agent_name == "second" and st.position == 2,
          f"{st.agent_name} pos={st.position}")
    check("recorded as failed", st.status == "failed", str(st.status))
    check("carrying the preflight message",
          "second" in (st.error or ""), str(st.error))
    check("and nothing is attributed to the step that never got a turn",
          all(st.position != 1 for st in steps), str([st.position for st in steps]))

    print("\n== preflight refuses a stale credential ==")
    install_specialists({"first": first, "second": second})
    job_runner._has_usable_credentials = lambda agent, principal=None: asyncio.sleep(0, result=False)
    run_id = await run_workflow("linear")
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
        steps = await list_step_runs(s, run_id)
    check("run failed on credentials", row.status == "failed", row.status)
    check("error mentions authorization",
          "author" in (row.error or "").lower(), row.error)
    # Both agents fail the gate here; only the earliest blocked step is marked,
    # because that is where the run actually stopped.
    st = only_step(steps)
    check("only the first blocked step is marked", len(steps) == 1, str(len(steps)))
    check("and it is the earliest one in execution order",
          st.position == 1 and st.agent_name == "first",
          f"{st.agent_name} pos={st.position}")
    job_runner._has_usable_credentials = lambda agent, principal=None: asyncio.sleep(0, result=True)

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

    print("\n== a blocked branch step is attributed to its own branch ==")
    # The diagram locates a node by branch_key + position, not by agent name:
    # two branches routinely share one agent. Recording the blocked step
    # without its branch would light up the wrong node.
    fan = FakeAgent("fan", answers=["[]"])
    async with SessionLocal() as s:
        await upsert_agent(s, name="fan", description="f", instructions="f",
                           mcp_servers=SERVERS, run_as_principal="svc@example.com")
        await upsert_agent(s, name="brancher", description="b", instructions="b",
                           mcp_servers=SERVERS, run_as_principal="svc@example.com")
        await upsert_workflow(
            s, name="branch_blocked", description="d", enabled=True,
            branches=[{"key": "billing", "description": "b", "position": 1}],
            steps=[
                {"branch_key": None, "position": 1, "agent_name": "fan",
                 "instructions": "read", "fan_out": True,
                 "step_timeout_seconds": 60},
                {"branch_key": "billing", "position": 1, "agent_name": "brancher",
                 "instructions": "draft", "fan_out": False,
                 "step_timeout_seconds": 60},
            ],
        )
    install_specialists({"fan": fan})  # 'brancher' is missing
    run_id = await run_workflow("branch_blocked")
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
        steps = await list_step_runs(s, run_id)
    check("run failed", row.status == "failed", row.status)
    st = only_step(steps)
    check("one blocked step recorded", len(steps) == 1, str(len(steps)))
    check("it carries the branch it belongs to",
          st.branch_key == "billing" and st.position == 1,
          f"{st.branch_key} pos={st.position}")
    check("with no item, because none were discovered",
          st.item_run_id is None, str(st.item_run_id))

    print("\n== a blocked peer is attributed to the step that would consult it ==")
    # The peer itself has no step of its own, so the only honest place to mark
    # is the step whose agent would have delegated to it.
    consulter = FakeAgent("consulter", answers=["ok"])
    async with SessionLocal() as s:
        await upsert_agent(s, name="peerless", description="p", instructions="p",
                           mcp_servers=SERVERS, run_as_principal="svc@example.com")
        await upsert_agent(s, name="consulter", description="c", instructions="c",
                           mcp_servers=SERVERS, run_as_principal="svc@example.com",
                           peers=["peerless"])
        await upsert_workflow(
            s, name="peer_blocked", description="d", enabled=True, branches=[],
            steps=[
                {"branch_key": None, "position": 1, "agent_name": "consulter",
                 "instructions": "ask the peer", "fan_out": False,
                 "step_timeout_seconds": 60},
            ],
        )
    install_specialists({"consulter": consulter, "peerless": FakeAgent("peerless")})
    job_runner._has_usable_credentials = (
        lambda agent, principal=None: asyncio.sleep(0, result=agent.name != "peerless")
    )
    run_id = await run_workflow("peer_blocked")
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
        steps = await list_step_runs(s, run_id)
    check("run failed on the peer's credential", row.status == "failed", row.status)
    check("run error names the peer", "peerless" in (row.error or ""), row.error)
    st = only_step(steps)
    check("one step recorded", len(steps) == 1, str(len(steps)))
    check("it is the consulting step, not the peer",
          st.agent_name == "consulter", str(st.agent_name))
    check("its error still names the peer that is actually broken",
          "peerless" in (st.error or ""), str(st.error))
    job_runner._has_usable_credentials = lambda agent, principal=None: asyncio.sleep(0, result=True)

    print("\n== a step falls back to the workflow's run-as principal ==")
    # The REAL credential gate, deliberately: every other test in this file
    # stubs it to a constant, which is exactly how the gate reading the agent's
    # own principal -- never the workflow's fallback -- went unnoticed. An
    # agent with a blank principal must pass preflight on the workflow's, and
    # must actually bind it.
    job_runner._has_usable_credentials = REAL_HAS_CREDENTIALS
    fallback_agent = PrincipalAgent("fallback_agent", answers=["done"])
    async with SessionLocal() as s:
        await upsert_agent(s, name="fallback_agent", description="f",
                           instructions="f", mcp_servers=SERVERS,
                           run_as_principal="")
        await upsert_workflow(
            s, name="fallback_wf", description="d", enabled=True, branches=[],
            run_as_principal="svc-fallback@example.com",
            steps=[
                {"branch_key": None, "position": 1,
                 "agent_name": "fallback_agent", "instructions": "go",
                 "fan_out": False, "step_timeout_seconds": 60},
            ],
        )
    install_specialists({"fallback_agent": fallback_agent})
    run_id = await run_workflow("fallback_wf")
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
        steps = await list_step_runs(s, run_id)
    check("preflight accepts the workflow's fallback principal",
          row.status == "success", f"{row.status} / {row.error}")
    check("the step actually ran",
          len(steps) == 1 and steps[0].status == "success",
          str([(st.status, st.error) for st in steps]))
    check("the step bound the workflow's principal",
          fallback_agent.principals == ["svc-fallback@example.com"],
          str(fallback_agent.principals))
    job_runner._has_usable_credentials = stub_credentials(True)

    print("\n== preflight checks the peers a step can reach ==")
    # A peer with no usable credential does NOT fail the delegation at run
    # time: registry._delegate returns the sign-in prompt as its answer when
    # there is no interactive sink, the parent reads it as content, and the
    # step is recorded `success` -- permanently, once skip_seen_items sees it.
    # peer_root -> peer_mid -> peer_deep, with peer_mid listing peer_root back
    # so the walk has to survive a legitimate mutual-peer cycle.
    async with SessionLocal() as s:
        await upsert_agent(s, name="peer_deep", description="d",
                           instructions="d", mcp_servers=SERVERS,
                           run_as_principal="svc@example.com")
        await upsert_agent(s, name="peer_mid", description="m",
                           instructions="m", mcp_servers=SERVERS,
                           run_as_principal="svc@example.com",
                           peers=["peer_root", "peer_deep"])
        await upsert_agent(s, name="peer_root", description="r",
                           instructions="r", mcp_servers=SERVERS,
                           run_as_principal="svc@example.com",
                           peers=["peer_mid"])
        await upsert_workflow(
            s, name="peers_wf", description="d", enabled=True, branches=[],
            steps=[
                {"branch_key": None, "position": 1, "agent_name": "peer_root",
                 "instructions": "go", "fan_out": False,
                 "step_timeout_seconds": 60},
            ],
        )
    peer_root_agent = FakeAgent("peer_root", answers=["done"])
    install_specialists({"peer_root": peer_root_agent,
                         "peer_mid": FakeAgent("peer_mid"),
                         "peer_deep": FakeAgent("peer_deep")})
    run_id = await run_workflow("peers_wf")
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
    check("a healthy peer graph with a cycle still runs",
          row.status == "success", f"{row.status} / {row.error}")

    job_runner._has_usable_credentials = (
        lambda agent, principal=None:
            asyncio.sleep(0, result=agent.name != "peer_deep")
    )
    peer_root_agent.prompts.clear()
    run_id = await run_workflow("peers_wf")
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
        steps = await list_step_runs(s, run_id)
    check("a peer with no usable credential fails preflight",
          row.status == "failed", f"{row.status} / {row.error}")
    check("the error names the peer, not the step's agent",
          "peer_deep" in (row.error or ""), row.error)
    check("the error says it was reached as a peer",
          "peer" in (row.error or "").lower(), row.error)
    # Nothing executed, but the run still records *where* it was stopped, on
    # the step whose agent would have consulted the broken peer -- see
    # _record_blocked_step.
    blocked = only_step(steps)
    check("only the blocked step is recorded", len(steps) == 1, str(len(steps)))
    check("recorded as failed, with no output",
          blocked.status == "failed" and getattr(blocked, "output", None) is None,
          f"{blocked.status} / {getattr(blocked, 'output', None)!r}")
    check("no model call was made", peer_root_agent.prompts == [],
          str(peer_root_agent.prompts))
    job_runner._has_usable_credentials = stub_credentials(True)

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

    print("\n== branches ==")
    BRANCH_STEPS = [
        {"branch_key": None, "position": 1, "agent_name": "reader",
         "instructions": "triage", "fan_out": True, "step_timeout_seconds": 60},
        {"branch_key": None, "position": 2, "agent_name": "drafter",
         "instructions": "draft the reply", "fan_out": False,
         "step_timeout_seconds": 60},
        {"branch_key": "abap", "position": 1, "agent_name": "abap",
         "instructions": "analyze the dump", "fan_out": False,
         "step_timeout_seconds": 60},
        {"branch_key": "abap", "position": 2, "agent_name": "abap2",
         "instructions": "review the analysis", "fan_out": False,
         "step_timeout_seconds": 60},
        {"branch_key": "fiori", "position": 1, "agent_name": "fiori",
         "instructions": "analyze the ui", "fan_out": False,
         "step_timeout_seconds": 60},
    ]
    BRANCH_DEFS = [
        {"key": "abap", "description": "ABAP dumps and ST22", "position": 1},
        {"key": "fiori", "description": "UI5 and launchpad", "position": 2},
    ]
    await seed_agents(["abap2"])
    async with SessionLocal() as s:
        await upsert_workflow(
            s, name="branched", description="d", enabled=True,
            skip_seen_items=False, branches=BRANCH_DEFS, steps=BRANCH_STEPS,
        )

    def branch_agents(reader_items):
        return {
            "reader": FakeAgent("reader", answers=[reader_items]),
            "abap": FakeAgent("abap", answers=["abap says dump"]),
            "abap2": FakeAgent("abap2", answers=["abap2 confirms"]),
            "fiori": FakeAgent("fiori", answers=["fiori says layout"]),
            "drafter": FakeAgent("drafter", answers=["drafted"]),
        }

    print("\n-- the catalogue reaches the reader --")
    agents_map = branch_agents([wr.WorkItem(id="c1", title="t", branches=["abap"],
                                            text="body")])
    install_specialists(agents_map)
    await run_workflow("branched")
    reader_prompt = agents_map["reader"].prompts[0]
    check("catalogue lists every branch key",
          "abap:" in reader_prompt and "fiori:" in reader_prompt,
          reader_prompt[-300:])
    check("catalogue carries the descriptions",
          "ABAP dumps and ST22" in reader_prompt, reader_prompt[-300:])
    check("catalogue tells the reader to pick only what is needed",
          "only the branches" in reader_prompt.lower(), reader_prompt[-300:])

    print("\n-- one branch selected runs only that branch --")
    agents_map = branch_agents([wr.WorkItem(id="one", title="t", branches=["abap"],
                                            text="the mail body")])
    install_specialists(agents_map)
    run_id = await run_workflow("branched")
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
        steps = await list_step_runs(s, run_id)
    check("run succeeded", row.status == "success", f"{row.status} / {row.error}")
    check("the abap branch ran", agents_map["abap"].prompts != [])
    check("the fiori branch did NOT run", agents_map["fiori"].prompts == [],
          str(agents_map["fiori"].prompts))
    check("branch step 1 saw the item text",
          "the mail body" in agents_map["abap"].prompts[0],
          agents_map["abap"].prompts[0][:200])
    check("branch step 2 chained from step 1",
          "abap says dump" in agents_map["abap2"].prompts[0],
          agents_map["abap2"].prompts[0][:200])
    check("branch step runs are tagged with the branch",
          {st.branch_key for st in steps if st.agent_name in ("abap", "abap2")} == {"abap"},
          str([(st.agent_name, st.branch_key) for st in steps]))
    check("the join saw the branch tail",
          "abap2 confirms" in agents_map["drafter"].prompts[0],
          agents_map["drafter"].prompts[0][:250])

    print("\n-- two branches selected fork and join --")
    agents_map = branch_agents([wr.WorkItem(id="two", title="t",
                                            branches=["fiori", "abap"],
                                            text="mixed question")])
    install_specialists(agents_map)
    run_id = await run_workflow("branched")
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
    join_prompt = agents_map["drafter"].prompts[0]
    check("both branches ran",
          agents_map["abap"].prompts != [] and agents_map["fiori"].prompts != [])
    check("the join saw both branch outputs",
          "abap2 confirms" in join_prompt and "fiori says layout" in join_prompt,
          join_prompt[:300])
    check("one From block per branch", join_prompt.count("## From ") == 2,
          join_prompt[:300])
    check("branches run in declared position order, not item order",
          join_prompt.index("abap2 confirms") < join_prompt.index("fiori says layout"),
          join_prompt[:300])
    check("run succeeded", row.status == "success", f"{row.status} / {row.error}")

    print("\n-- no branches selected goes straight to the join --")
    agents_map = branch_agents([wr.WorkItem(id="none", title="t", branches=[],
                                            text="plain question")])
    install_specialists(agents_map)
    run_id = await run_workflow("branched")
    check("no branch ran",
          agents_map["abap"].prompts == [] and agents_map["fiori"].prompts == [])
    check("the join saw the item text",
          "plain question" in agents_map["drafter"].prompts[0],
          agents_map["drafter"].prompts[0][:200])

    print("\n-- a failing branch fails its item and skips the join --")
    agents_map = branch_agents([wr.WorkItem(id="boom", title="t",
                                            branches=["abap", "fiori"],
                                            text="body")])
    agents_map["abap"] = FakeAgent("abap", fail_with=RuntimeError("ARC-1 down"))
    install_specialists(agents_map)
    run_id = await run_workflow("branched")
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
        items = await list_item_runs(s, run_id)
    check("item failed", items[0].status == "failed", items[0].status)
    check("run failed", row.status == "failed", row.status)
    check("the later branch did not run", agents_map["fiori"].prompts == [],
          str(agents_map["fiori"].prompts))
    check("the join did not run", agents_map["drafter"].prompts == [],
          str(agents_map["drafter"].prompts))

    print("\n-- on_unknown_branch --")
    async with SessionLocal() as s:
        await upsert_workflow(
            s, name="branched", description="d", enabled=True,
            skip_seen_items=False, on_unknown_branch="fail",
            branches=BRANCH_DEFS, steps=BRANCH_STEPS,
        )
    agents_map = branch_agents([wr.WorkItem(id="ghost", title="t",
                                            branches=["nope"], text="body")])
    install_specialists(agents_map)
    run_id = await run_workflow("branched")
    async with SessionLocal() as s:
        items = await list_item_runs(s, run_id)
    check("unknown branch fails the item under 'fail'",
          items[0].status == "failed", items[0].status)
    check("error names the branch", "nope" in (items[0].error or ""), items[0].error)

    async with SessionLocal() as s:
        await upsert_workflow(
            s, name="branched", description="d", enabled=True,
            skip_seen_items=False, on_unknown_branch="skip",
            branches=BRANCH_DEFS, steps=BRANCH_STEPS,
        )
    agents_map = branch_agents([wr.WorkItem(id="ghost2", title="t",
                                            branches=["nope", "abap"], text="body")])
    install_specialists(agents_map)
    run_id = await run_workflow("branched")
    async with SessionLocal() as s:
        items = await list_item_runs(s, run_id)
    check("unknown branch is ignored under 'skip'",
          items[0].status == "success", f"{items[0].status} / {items[0].error}")
    check("the known branch still ran", agents_map["abap"].prompts != [])

    print("\n== registry reload keeps MCP clients alive during a run ==")

    class FakeClient:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    class FakeServer:
        def __init__(self, client):
            self._http_client = client

    client = FakeClient()
    old_build = BuildResult(orchestrator=None, specialists={},
                            mcp_clients=[FakeServer(client)], configs=[])
    registry._build = old_build
    new_build = BuildResult(orchestrator=None, specialists={}, mcp_clients=[],
                            configs=[])
    real_build_fn = registry_module.build_orchestrator
    registry_module.build_orchestrator = lambda: asyncio.sleep(0, result=new_build)
    try:
        async def never_ends():
            await asyncio.sleep(3600)

        sentinel = asyncio.create_task(never_ends())
        wr._tasks.add(sentinel)
        try:
            await registry.reload()
            check("client kept open while a workflow run is in flight",
                  not client.closed)
        finally:
            sentinel.cancel()
            wr._tasks.discard(sentinel)
            await asyncio.gather(sentinel, return_exceptions=True)

        registry._build = old_build
        await registry.reload()
        check("client closed once no workflow run is in flight", client.closed)
    finally:
        registry_module.build_orchestrator = real_build_fn

    print("\n== a DB failure recording success fails only its own item ==")
    # The success-path finish_item_run used to sit OUTSIDE process()'s try. A
    # failure there escaped process(), and asyncio.gather (deliberately without
    # return_exceptions) propagates immediately WITHOUT cancelling the sibling
    # item tasks -- so the run finalized `failed`, releasing the overlap lock,
    # while orphaned tasks kept invoking agents and writing rows against a
    # finished run.
    real_finish_item = wr.finish_item_run
    blown = {"done": False}

    async def flaky_finish_item(session, item_run_id, *, status, error=None):
        if status == "success" and not blown["done"]:
            blown["done"] = True
            raise RuntimeError("pool exhausted")
        return await real_finish_item(session, item_run_id, status=status,
                                      error=error)

    install_specialists({
        "reader": FakeAgent("reader", answers=[[
            wr.WorkItem(id="f1", title="a", branches=[], text="body one"),
            wr.WorkItem(id="f2", title="b", branches=[], text="body two"),
            wr.WorkItem(id="f3", title="c", branches=[], text="body three"),
        ]]),
        "drafter": FakeAgent("drafter"),
    })
    wr.finish_item_run = flaky_finish_item
    try:
        run_id = await run_workflow("fanout")
    finally:
        wr.finish_item_run = real_finish_item
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
        items = {i.item_key: i for i in await list_item_runs(s, run_id)}
    check("the run is partial, not failed", row.status == "partial",
          f"{row.status} / {row.error}")
    check("the item whose success write blew up is recorded failed",
          items["f1"].status == "failed", items["f1"].status)
    check("the sibling items still completed",
          items["f2"].status == "success" and items["f3"].status == "success",
          str({k: v.status for k, v in items.items()}))
    check("counts reflect the split",
          (row.items_total, row.items_succeeded, row.items_failed) == (3, 2, 1),
          f"{row.items_total}/{row.items_succeeded}/{row.items_failed}")

    print("\n== cancel_all_workflow_runs records interrupted ==")
    slow_reader = FakeAgent("reader", answers=[[]], delay=5)
    install_specialists({"reader": slow_reader, "drafter": FakeAgent("drafter")})
    async with SessionLocal() as s:
        wf = await get_workflow_by_name(s, "fanout")
    run_id = await wr.start_workflow_run(wf, trigger="manual")
    await asyncio.sleep(0.05)
    await wr.cancel_all_workflow_runs()
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
    check("cancelled run is interrupted", row.status == "interrupted", row.status)

    print("\n== cancelling mid-item records the item and the counts ==")
    # Cancelling once items are in flight is the case SIGTERM and a
    # run_timeout_seconds expiry both produce. The in-flight item's row must
    # not be left `running` (a timeout never self-heals -- the process lives
    # on, so no startup sweep ever clears it), and an interrupted run must
    # report the items it did finish rather than 0/0/0/0.
    class HangForOne(FakeAgent):
        async def run(self, prompt, **kwargs):
            self.prompts.append(prompt)
            if "body two" in prompt:
                await asyncio.sleep(5)
            return FakeResult("drafted")

    install_specialists({
        "reader": FakeAgent("reader", answers=[[
            wr.WorkItem(id="c-ok", title="a", branches=[], text="body one"),
            wr.WorkItem(id="c-hang", title="b", branches=[], text="body two"),
        ]]),
        "drafter": HangForOne("drafter"),
    })
    async with SessionLocal() as s:
        wf = await get_workflow_by_name(s, "fanout")
    run_id = await wr.start_workflow_run(wf, trigger="manual")
    # Long enough for the first item to finish and the second to be mid-step;
    # max_parallel_items is 1, so they cannot overlap.
    await asyncio.sleep(0.4)
    await wr.cancel_all_workflow_runs()
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
        items = {i.item_key: i for i in await list_item_runs(s, run_id)}
    check("cancelled mid-item run is interrupted", row.status == "interrupted",
          f"{row.status} / {row.error}")
    check("the finished item is still success",
          items["c-ok"].status == "success", items["c-ok"].status)
    check("the in-flight item is interrupted, not left running",
          items["c-hang"].status == "interrupted", items["c-hang"].status)
    check("counts report the items already finished",
          (row.items_total, row.items_succeeded, row.items_failed,
           row.items_skipped) == (2, 1, 0, 0),
          f"{row.items_total}/{row.items_succeeded}/{row.items_failed}/"
          f"{row.items_skipped}")

    print("\n== _finalize never lets a DB failure escape ==")
    # The run row IS the overlap lock, so an exception escaping the finalizer
    # would wedge the workflow forever AND surface only as an "exception was
    # never retrieved" warning at GC time.
    real_finish = wr.finish_workflow_run

    async def boom(*a, **kw):
        raise RuntimeError("pool exhausted")

    wr.finish_workflow_run = boom
    escaped = False
    try:
        await wr._finalize("no-such-run-id", status="failed", error="x")
    except Exception:
        escaped = True
    finally:
        wr.finish_workflow_run = real_finish
    check("_finalize swallows a DB failure", not escaped)

    print("\n== the scheduler endpoint ==")
    # No auth setup is needed: with VCAP_SERVICES popped there is no XSUAA
    # validator, and require_jobscheduler returns a local-dev payload. This is
    # the same reason tests/test_admin_api.py can call these routes directly.
    from httpx import ASGITransport, AsyncClient  # noqa: PLC0415

    import app as app_module  # noqa: PLC0415

    install_specialists({"reader": FakeAgent("reader", answers=[[]]),
                         "drafter": FakeAgent("drafter")})
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/workflows/fanout-slug/run")
        check("unknown slug is 404", r.status_code == 404, str(r.status_code))

    async with SessionLocal() as s:
        wf = await get_workflow_by_name(s, "fanout")
        await upsert_workflow(
            s, name="fanout", description="d", enabled=True, branches=[],
            api_slug="fanout-slug", skip_seen_items=False,
            steps=[
                {"branch_key": None, "position": 1, "agent_name": "reader",
                 "instructions": "triage", "fan_out": True,
                 "step_timeout_seconds": 60},
                {"branch_key": None, "position": 2, "agent_name": "drafter",
                 "instructions": "draft", "fan_out": False,
                 "step_timeout_seconds": 60},
            ],
        )
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/workflows/fanout-slug/run")
        check("accepted with 202", r.status_code == 202, str(r.status_code))
        check("returns a run id", "run_id" in r.json(), r.text)

        # start_workflow_run creates the run row (the overlap lock) and
        # returns before the background task does any work, so this second
        # POST -- issued before draining the first's task -- lands on a run
        # that is already `running` and must be refused.
        r2 = await c.post("/api/workflows/fanout-slug/run")
        check("second concurrent trigger is 409", r2.status_code == 409, str(r2.status_code))
    await asyncio.gather(*[t for t in wr._tasks if not t.done()],
                         return_exceptions=True)

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
