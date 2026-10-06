"""Workflow runner with deterministic step kinds mixed into agent steps.

Companion to tests/test_workflow_runner.py, with the same fakes: a main-line
condition that stops the run, a branch condition that skips the rest of its
branch for one item, a transform between two agent steps, and a python step
whose output feeds the next agent. Kept in its own file (own SQLite DB) so it
can run on its own.

Run:  python tests/test_workflow_step_kinds_runner.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ["MCP_URL_ALLOWLIST"] = ""
os.environ["PUBLIC_BASE_URL"] = "https://app.example.com"

from agents import job_runner  # noqa: E402
import agents.workflow_runner as wr  # noqa: E402
from agents.db import (  # noqa: E402
    SessionLocal,
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
    def __init__(self, name, answers=None):
        self.name = name
        self.answers = list(answers or [])
        self.prompts: list[str] = []

    async def run(self, prompt, **kwargs):
        self.prompts.append(prompt)
        answer = self.answers.pop(0) if self.answers else f"{self.name}-out"
        return FakeResult(answer)


def install_specialists(mapping):
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
    await asyncio.gather(*[t for t in wr._tasks if not t.done()],
                         return_exceptions=True)
    return run_id


async def load(run_id):
    async with SessionLocal() as s:
        row = await get_workflow_run(s, run_id)
        steps = await list_step_runs(s, run_id)
        items = await list_item_runs(s, run_id)
    return row, steps, items


def step(position, kind="agent", agent="", branch=None, config=None, **over):
    base = {"branch_key": branch, "position": position, "agent_name": agent,
            "instructions": f"do step {position}", "fan_out": False,
            "step_timeout_seconds": 60, "kind": kind, "config": config or {}}
    base.update(over)
    return base


async def main() -> None:
    await init_db()
    job_runner._has_usable_credentials = lambda agent, principal=None: asyncio.sleep(0, result=True)
    await seed_agents(["first", "second", "reader", "abap", "drafter"])

    print("\n== a transform between two agent steps ==")
    first = FakeAgent("first", answers=['{"issue": {"key": "ABC-1", "summary": "Login fails"}}'])
    second = FakeAgent("second", answers=["done"])
    install_specialists({"first": first, "second": second})
    async with SessionLocal() as s:
        await upsert_workflow(
            s, name="transform-line", description="d", enabled=True, branches=[],
            steps=[
                step(1, agent="first"),
                step(2, kind="transform", config={
                    "extract_json": "issue.summary",
                    "template": "Summary: {{text}} ({{json.issue.key}})",
                }),
                step(3, agent="second"),
            ],
        )
    run_id = await run_workflow("transform-line")
    row, steps, items = await load(run_id)
    check("run succeeded", row.status == "success", f"{row.status} / {row.error}")
    check("three step runs", len(steps) == 3, str(len(steps)))
    t = next((st for st in steps if st.position == 2), None)
    check("the transform's step run is labelled by its kind",
          t is not None and t.agent_name == "transform", getattr(t, "agent_name", None))
    check("the transform's output is recorded",
          t is not None and t.output == "Summary: Login fails (ABC-1)", getattr(t, "output", None))
    check("the next agent saw the transformed text under transform#2",
          "## From transform#2" in second.prompts[0]
          and "Summary: Login fails (ABC-1)" in second.prompts[0],
          second.prompts[0][:300])
    check("no agent step needed for a transform (preflight passed with 'first'/'second' only)",
          row.error is None, str(row.error))

    print("\n== a python step's output feeds the next agent ==")
    first = FakeAgent("first", answers=["3 4 5"])
    second = FakeAgent("second", answers=["ok"])
    install_specialists({"first": first, "second": second})
    async with SessionLocal() as s:
        await upsert_workflow(
            s, name="python-line", description="d", enabled=True, branches=[],
            steps=[
                step(1, agent="first"),
                step(2, kind="python", config={
                    "code": "nums = [int(x) for x in text.split()]\n"
                            "output = {'sum': sum(nums), 'max': max(nums)}",
                    "timeout_seconds": 10,
                }),
                step(3, agent="second"),
            ],
        )
    run_id = await run_workflow("python-line")
    row, steps, items = await load(run_id)
    check("run succeeded", row.status == "success", f"{row.status} / {row.error}")
    check("the agent after the python step saw its JSON output",
          '"sum": 12' in second.prompts[0] and "## From python#2" in second.prompts[0],
          second.prompts[0][:300])

    print("\n== a failing python step fails the run at that step ==")
    install_specialists({"first": FakeAgent("first", answers=["x"]), "second": FakeAgent("second")})
    async with SessionLocal() as s:
        await upsert_workflow(
            s, name="python-fails", description="d", enabled=True, branches=[],
            steps=[
                step(1, agent="first"),
                step(2, kind="python", config={"code": "output = 1 / 0"}),
                step(3, agent="second"),
            ],
        )
    run_id = await run_workflow("python-fails")
    row, steps, items = await load(run_id)
    check("run failed", row.status == "failed", row.status)
    check("run error names the step and the exception",
          "Step 2 (python)" in (row.error or "") and "ZeroDivisionError" in (row.error or ""),
          row.error)
    check("two step runs (the third never ran)", len(steps) == 2, str(len(steps)))
    check("the python step run is failed",
          any(st.position == 2 and st.status == "failed" for st in steps),
          str([(st.position, st.status) for st in steps]))

    print("\n== a main-line condition that stops ends the run successfully ==")
    first = FakeAgent("first", answers=["nothing new today"])
    second = FakeAgent("second", answers=["should not run"])
    install_specialists({"first": first, "second": second})
    async with SessionLocal() as s:
        await upsert_workflow(
            s, name="stop-main", description="d", enabled=True, branches=[],
            steps=[
                step(1, agent="first"),
                step(2, kind="condition", config={
                    "rules": [{"when": {"source": "text", "op": "contains", "value": "nothing new"},
                               "then": {"action": "stop", "output": "Nothing to do: {{text}}"}}],
                    "else": {"action": "continue"},
                }),
                step(3, agent="second"),
            ],
        )
    run_id = await run_workflow("stop-main")
    row, steps, items = await load(run_id)
    check("run succeeded", row.status == "success", f"{row.status} / {row.error}")
    check("the run's summary carries the condition's output",
          "Nothing to do: nothing new today" in (row.summary or ""), row.summary)
    check("the agent after the stop never ran", second.prompts == [], str(second.prompts))
    by_pos = {st.position: st for st in steps}
    check("the condition step run is success and says it stopped",
          by_pos.get(2) is not None and by_pos[2].status == "success"
          and "[stop: rule 1]" in (by_pos[2].output or ""),
          str({p: (s.status, s.output) for p, s in by_pos.items()}))
    check("the skipped step is recorded as skipped",
          by_pos.get(3) is not None and by_pos[3].status == "skipped"
          and by_pos[3].agent_name == "second",
          str({p: s.status for p, s in by_pos.items()}))

    print("\n-- the same condition lets a different text through --")
    first = FakeAgent("first", answers=["two new tickets"])
    second = FakeAgent("second", answers=["handled"])
    install_specialists({"first": first, "second": second})
    run_id = await run_workflow("stop-main")
    row, steps, items = await load(run_id)
    check("run succeeded", row.status == "success", f"{row.status} / {row.error}")
    check("the agent after the condition ran and saw the text passed through",
          len(second.prompts) == 1 and "two new tickets" in second.prompts[0]
          and "## From condition#2" in second.prompts[0],
          str(second.prompts)[:300])

    print("\n== a condition inside a branch skips the rest of that branch for one item ==")
    items_out = [
        wr.WorkItem(id="i-dump", title="dump", branches=["abap"], text="ST22 dump in ZPROG"),
        wr.WorkItem(id="i-quiet", title="quiet", branches=["abap"], text="just a question"),
    ]
    reader = FakeAgent("reader", answers=[items_out])
    abap = FakeAgent("abap", answers=["analysis of dump", "analysis: nothing"])
    drafter = FakeAgent("drafter", answers=["draft 1", "draft 2"])
    install_specialists({"reader": reader, "abap": abap, "drafter": drafter})
    async with SessionLocal() as s:
        await upsert_workflow(
            s, name="stop-branch", description="d", enabled=True, skip_seen_items=False,
            branches=[{"key": "abap", "description": "ABAP", "position": 1}],
            steps=[
                step(1, agent="reader", fan_out=True),
                step(2, agent="drafter"),
                step(1, branch="abap", kind="condition", config={
                    "rules": [{"when": {"source": "item", "field": "text", "op": "contains", "value": "dump"},
                               "then": {"action": "continue"}}],
                    "else": {"action": "stop", "output": "No dump in {{item.id}}; skipping analysis."},
                }),
                step(2, branch="abap", agent="abap"),
            ],
        )
    run_id = await run_workflow("stop-branch")
    row, steps, items = await load(run_id)
    check("run succeeded", row.status == "success", f"{row.status} / {row.error}")
    check("both items succeeded (a branch stop is not a failure)",
          (row.items_succeeded, row.items_failed) == (2, 0),
          f"{row.items_succeeded}/{row.items_failed}")
    check("the abap agent ran only for the item with a dump",
          len(abap.prompts) == 1 and "ST22 dump" in abap.prompts[0], str(abap.prompts)[:300])
    check("the join ran for both items", len(drafter.prompts) == 2, str(len(drafter.prompts)))
    quiet_join = next((p for p in drafter.prompts if "i-quiet" in p or "No dump" in p), "")
    check("the join saw the condition's stop output for the skipped branch",
          "No dump in i-quiet; skipping analysis." in quiet_join
          and "## From condition#1" in quiet_join, quiet_join[:300])
    quiet_item = next((i for i in items if i.item_key == "i-quiet"), None)
    quiet_steps = [st for st in steps if quiet_item and st.item_run_id == quiet_item.id]
    check("the skipped branch step is recorded as skipped for that item",
          any(st.branch_key == "abap" and st.position == 2 and st.status == "skipped"
              for st in quiet_steps),
          str([(st.branch_key, st.position, st.status) for st in quiet_steps]))

    print("\n== a condition on the join line stops the rest of the join for one item ==")
    items_out = [
        wr.WorkItem(id="j1", title="", branches=[], text="please reply"),
        wr.WorkItem(id="j2", title="", branches=[], text="FYI only"),
    ]
    reader = FakeAgent("reader", answers=[items_out])
    drafter = FakeAgent("drafter")
    install_specialists({"reader": reader, "drafter": drafter})
    async with SessionLocal() as s:
        await upsert_workflow(
            s, name="stop-join", description="d", enabled=True, skip_seen_items=False,
            branches=[],
            steps=[
                step(1, agent="reader", fan_out=True),
                step(2, kind="condition", config={
                    "rules": [{"when": {"source": "text", "op": "contains", "value": "FYI"},
                               "then": {"action": "stop"}}],
                }),
                step(3, agent="drafter"),
            ],
        )
    run_id = await run_workflow("stop-join")
    row, steps, items = await load(run_id)
    check("run succeeded", row.status == "success", f"{row.status} / {row.error}")
    check("both items succeeded", (row.items_succeeded, row.items_failed) == (2, 0),
          f"{row.items_succeeded}/{row.items_failed}")
    check("the drafter ran only for the item that was not stopped",
          len(drafter.prompts) == 1 and "please reply" in drafter.prompts[0],
          str(drafter.prompts)[:300])

    print("\n== an http step without a destination binding fails its step, not the process ==")
    install_specialists({"first": FakeAgent("first", answers=["x"])})
    async with SessionLocal() as s:
        await upsert_workflow(
            s, name="http-line", description="d", enabled=True, branches=[],
            steps=[
                step(1, agent="first"),
                step(2, kind="http", config={"destination": "ghost", "path": "/x"}),
            ],
        )
    run_id = await run_workflow("http-line")
    row, steps, items = await load(run_id)
    check("run failed", row.status == "failed", row.status)
    check("error names the http step and the destination",
          "Step 2 (http)" in (row.error or "") and "ghost" in (row.error or ""), row.error)

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
