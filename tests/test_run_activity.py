"""Live activity of API-triggered runs, and the request limit every run gets.

Run:  python tests/test_run_activity.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = ROOT / "tests" / "_test_run_activity.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB}"
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ["MCP_URL_ALLOWLIST"] = ""
os.environ["PUBLIC_BASE_URL"] = "https://approuter.example.com"

from agents.db import (  # noqa: E402
    SessionLocal,
    create_job_run,
    get_agent_by_slug,
    get_job_run,
    init_db,
    upsert_agent,
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


SERVERS = [{"url": "https://arc1.example.com/mcp", "auth_mode": "none"}]


def recorder_tests() -> None:
    from agents.progress import ProgressUpdate
    from agents.run_activity import MAX_EVENTS, RunActivity

    print("\n== recorder ==")
    a = RunActivity()
    a.record(ProgressUpdate(agent="deep", kind="tool_start", tool_call_id="c1",
                            tool_name="arc1_SAPRead", args={"type": "CLAS", "name": "ZCL_X"}))
    check("tool start is one running event",
          len(a.events) == 1 and a.events[0]["status"] == "running", str(a.events))
    check("arguments previewed", "name=ZCL_X" in a.events[0]["detail"], a.events[0]["detail"])
    a.record(ProgressUpdate(agent="deep", kind="tool_end", tool_call_id="c1",
                            ok=True, output="METHOD x.\n  ENDMETHOD."))
    check("tool end completes the same event",
          len(a.events) == 1 and a.events[0]["status"] == "ok", str(a.events))
    check("output previewed on one line", a.events[0]["output"] == "METHOD x. ENDMETHOD.")

    a.record(ProgressUpdate(agent="deep", kind="tool_start", tool_call_id="c2",
                            tool_name="write_todos",
                            args={"todos": [{"content": "Find the mail", "status": "completed"},
                                            {"content": "Repair", "status": "in_progress"}]}))
    check("write_todos replaces the plan",
          [t["status"] for t in a.plan] == ["completed", "in_progress"], str(a.plan))

    a.record(ProgressUpdate(agent="deep", kind="tool_start", tool_call_id="c3",
                            tool_name="arc1_SAPActivate", args={}))
    a.close_open_calls()
    check("an unfinished call ends interrupted",
          a.events[-1]["status"] == "error" and a.events[-1]["output"] == "(interrupted)")

    a.record(ProgressUpdate(agent="deep", kind="tool_end", tool_call_id="unknown", ok=True))
    check("an unmatched end adds nothing", len(a.events) == 3, str(len(a.events)))

    b = RunActivity()
    for i in range(MAX_EVENTS + 7):
        b.record(ProgressUpdate(agent="x", kind="tool_start", tool_call_id=f"k{i}",
                                tool_name="t"))
    check("event list is capped", len(b.events) == MAX_EVENTS, str(len(b.events)))
    check("dropped events are counted", b.to_dict()["dropped"] == 7)
    check("oldest dropped first", b.events[0]["id"] == "k7", b.events[0]["id"])


def sink_tests() -> None:
    from agents.progress import current_progress, is_interactive
    from agents.run_activity import live_activity, recording

    print("\n== sink ==")
    check("no sink is not interactive", is_interactive() is False)
    token = current_progress.set(lambda update: None)
    try:
        check("a chat sink is interactive", is_interactive() is True)
    finally:
        current_progress.reset(token)
    with recording("run-1") as activity:
        check("a recording sink is not interactive", is_interactive() is False)
        check("live while recording", live_activity("run-1") is not None)
        check("installed sink is the recorder", current_progress.get() is activity)
    check("gone after recording", live_activity("run-1") is None)
    check("sink restored", current_progress.get() is None)


async def runner_tests() -> None:
    import agents.job_runner as job_runner
    from agents.progress import report_tool_end, report_tool_start
    from agents.registry import registry
    from agents.reports import RunReport
    from agents.run_activity import live_activity
    from agents.shared import AGENT_REQUEST_LIMIT

    print("\n== runner ==")
    await init_db()
    async with SessionLocal() as s:
        await upsert_agent(
            s, name="Deep Check", description="d", instructions="i",
            mcp_servers=SERVERS, expose_chat=False, expose_api=True,
            api_slug="deep-check", run_as_principal="svc@example.com",
            run_prompt="Run it.", run_timeout_seconds=900,
        )

    seen_kwargs: dict = {}
    mid_run: dict = {}
    report = RunReport(summary="1 defect repaired", body_md="# Done\n")

    class _Specialist:
        def __init__(self, exc=None):
            self._exc = exc

        async def run(self, prompt, **kw):
            seen_kwargs.update(kw)
            report_tool_start("Deep Check", "t1", "arc1_SAPRead", {"name": "ZCL_X"})
            report_tool_end("Deep Check", "t1", ok=True, output="source")
            report_tool_start("Deep Check", "t2", "write_todos",
                              {"todos": [{"content": "Repair", "status": "in_progress"}]})
            report_tool_end("Deep Check", "t2", ok=True, output="ok")
            mid_run["activity"] = live_activity(current_run["id"])
            if self._exc:
                report_tool_start("Deep Check", "t3", "arc1_SAPWrite", {})
                raise self._exc

            class _R:
                output = report
            return _R()

    class _Build:
        def __init__(self, specialist):
            self.specialists = {"Deep Check": specialist}

    current_run: dict = {}
    job_runner._has_usable_credentials = lambda agent, principal=None: asyncio.sleep(0, result=True)
    registry._build = _Build(_Specialist())
    async with SessionLocal() as s:
        job = await get_agent_by_slug(s, "deep-check")
        agent_id = job.id
        current_run["id"] = (await create_job_run(s, agent=job, trigger="manual")).id
    await job_runner.execute_run(current_run["id"], agent_id)

    live = mid_run.get("activity") or {}
    check("activity readable while the run is live",
          [e["tool"] for e in live.get("events", [])] == ["arc1_SAPRead", "write_todos"],
          str(live))
    check("plan readable while the run is live",
          live.get("plan") == [{"content": "Repair", "status": "in_progress"}], str(live))
    check("run started with the app's request limit",
          getattr(seen_kwargs.get("usage_limits"), "request_limit", None) == AGENT_REQUEST_LIMIT,
          str(seen_kwargs.get("usage_limits")))
    check("request limit is above the library default of 50", AGENT_REQUEST_LIMIT > 50)
    check("root run reports its own tool calls",
          callable(seen_kwargs.get("event_stream_handler")))
    async with SessionLocal() as s:
        row = await get_job_run(s, current_run["id"])
        check("run succeeded", row.status == "success", row.status)
        stored = row.activity or {}
        check("activity stored on the row", len(stored.get("events", [])) == 2, str(stored))
        check("stored calls are completed",
              all(e["status"] == "ok" for e in stored.get("events", [])))
    check("live activity released", live_activity(current_run["id"]) is None)

    print("\n== runner: failure keeps the activity ==")
    registry._build = _Build(_Specialist(exc=RuntimeError("SAP down")))
    async with SessionLocal() as s:
        job = await get_agent_by_slug(s, "deep-check")
        current_run["id"] = (await create_job_run(s, agent=job, trigger="manual")).id
    await job_runner.execute_run(current_run["id"], agent_id)
    async with SessionLocal() as s:
        row = await get_job_run(s, current_run["id"])
        check("failed run is failed", row.status == "failed", row.status)
        events = (row.activity or {}).get("events", [])
        check("failed run's activity stored", len(events) == 3, str(events))
        check("the call that never returned is interrupted",
              events and events[-1]["status"] == "error"
              and events[-1]["output"] == "(interrupted)", str(events[-1:]))

    print("\n== admin API ==")
    from agents.admin import api_get_run

    data = await api_get_run(current_run["id"])
    check("run detail carries the stored activity",
          len((data.get("activity") or {}).get("events", [])) == 3)


async def main() -> None:
    recorder_tests()
    sink_tests()
    await runner_tests()
    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
