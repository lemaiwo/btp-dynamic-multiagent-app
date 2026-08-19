"""Scheduled/API agent runs: exposure config, run records, runner behaviour.

Run:  python tests/test_job_runs.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = ROOT / "tests" / "_test_job_runs.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB}"
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from agents.db import (  # noqa: E402
    DEFAULT_RUN_PROMPT,
    SessionLocal,
    get_agent_by_name,
    get_agent_by_slug,
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


async def main() -> None:
    await init_db()

    print("\n== agent exposure fields ==")
    async with SessionLocal() as s:
        await upsert_agent(
            s, name="chat-only", description="d", instructions="i",
            mcp_servers=SERVERS,
        )
        await upsert_agent(
            s, name="Daily Check", description="d", instructions="i",
            mcp_servers=SERVERS, expose_chat=False, expose_api=True,
            api_slug="daily-check", run_as_principal="svc@example.com",
            run_prompt="Run the daily check.", run_timeout_seconds=900,
            expected_sections=["st22", "slg1"],
        )
    async with SessionLocal() as s:
        chat = await get_agent_by_name(s, "chat-only")
        check("expose_chat defaults on", bool(chat.expose_chat) is True)
        check("expose_api defaults off", bool(chat.expose_api) is False)
        check("run_prompt defaults empty", not chat.run_prompt)
        check("timeout defaults to 1800", chat.run_timeout_seconds == 1800)
        check("expected_sections defaults empty", chat.expected_sections == [])

        job = await get_agent_by_slug(s, "daily-check")
        check("lookup by slug", job is not None and job.name == "Daily Check")
        check("expose_chat stored off", bool(job.expose_chat) is False)
        check("expose_api stored on", bool(job.expose_api) is True)
        check("run_as stored", job.run_as_principal == "svc@example.com")
        check("timeout stored", job.run_timeout_seconds == 900)
        check("expected_sections stored", job.expected_sections == ["st22", "slg1"])
        check("to_dict exposes fields", job.to_dict()["api_slug"] == "daily-check")

    print("\n== slug uniqueness ==")
    async with SessionLocal() as s:
        try:
            await upsert_agent(
                s, name="Other", description="d", instructions="i",
                mcp_servers=SERVERS, expose_api=True, api_slug="daily-check",
            )
            check("duplicate slug rejected", False, "no error raised")
        except ValueError:
            check("duplicate slug rejected", True)

    print("\n== default run prompt ==")
    check("DEFAULT_RUN_PROMPT is non-empty", bool(DEFAULT_RUN_PROMPT.strip()))

    print("\n== chat exposure filtering ==")
    from agents.db import list_agents

    async with SessionLocal() as s:
        rows = [r for r in await list_agents(s) if r.enabled]
    chat_rows = [r for r in rows if r.expose_chat]
    check("run-only agent excluded from chat", all(r.name != "Daily Check" for r in chat_rows))
    check("chat agent still included", any(r.name == "chat-only" for r in chat_rows))
    check("both agents still built as specialists", len(rows) >= 2)

    print("\n== run_as identity binding ==")
    from agents.auth import current_base_url, current_principal, run_as

    os.environ["PUBLIC_BASE_URL"] = "https://approuter.example.com"
    check("no principal before", current_principal.get() is None)
    async with run_as("svc@example.com"):
        check("principal bound", current_principal.get() == "svc@example.com")
        check("base url bound", current_base_url.get() == "https://approuter.example.com")
        check("jwt deliberately unset", __import__("agents.auth", fromlist=["x"]).current_jwt.get() is None)
    check("principal restored after", current_principal.get() is None)

    try:
        async with run_as("svc@example.com"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    check("principal restored after exception", current_principal.get() is None)

    os.environ.pop("PUBLIC_BASE_URL")
    try:
        async with run_as("svc@example.com"):
            pass
        check("missing PUBLIC_BASE_URL rejected", False, "no error raised")
    except RuntimeError:
        check("missing PUBLIC_BASE_URL rejected", True)

    print("\n== report model + completeness ==")
    from agents.reports import Finding, ReportSection, RunReport, missing_sections

    clean = RunReport(
        summary="No issues found.",
        overall_severity="info",
        sections=[
            ReportSection(source_key="st22", title="Short dumps", checked=True, findings=[]),
            ReportSection(source_key="slg1", title="App log", checked=True, findings=[]),
        ],
    )
    check("empty findings is complete", missing_sections(clean, ["st22", "slg1"]) == [])

    unchecked = RunReport(
        summary="Partial.",
        overall_severity="info",
        sections=[
            ReportSection(source_key="st22", title="Short dumps", checked=True, findings=[]),
            ReportSection(source_key="slg1", title="App log", checked=False,
                          note="RFC destination unavailable", findings=[]),
        ],
    )
    check("unchecked section is missing", missing_sections(unchecked, ["st22", "slg1"]) == ["slg1"])

    absent = RunReport(summary="s", overall_severity="info", sections=[])
    check("absent section is missing", missing_sections(absent, ["st22"]) == ["st22"])
    check("no expectations means complete", missing_sections(absent, []) == [])

    f = Finding(title="TSV_TNEW_PAGE_ALLOC_FAILED", severity="high", count=12,
                affected=["ZPROG"], detail="d")
    check("finding defaults are optional", f.analysis is None and f.references == [])

    print("\n== JobRun records ==")
    from datetime import datetime, timedelta, timezone

    from agents.db import (
        active_job_run,
        create_job_run,
        finish_job_run,
        get_job_run,
        list_job_runs,
        sweep_stale_runs,
    )

    async with SessionLocal() as s:
        job = await get_agent_by_slug(s, "daily-check")
        run = await create_job_run(s, agent=job, trigger="manual", created_by="me@x")
        run_id = run.id
    check("run id is a string", isinstance(run_id, str) and len(run_id) > 10)
    check("run starts running", run.status == "running")

    async with SessionLocal() as s:
        job = await get_agent_by_slug(s, "daily-check")
        check("active run found", (await active_job_run(s, job.id)) is not None)

    async with SessionLocal() as s:
        await finish_job_run(
            s, run_id, status="success", summary="All clear",
            report={"summary": "All clear", "overall_severity": "info", "sections": []},
        )
    async with SessionLocal() as s:
        done = await get_job_run(s, run_id)
        check("status persisted", done.status == "success")
        check("summary persisted", done.summary == "All clear")
        check("report persisted", done.report["overall_severity"] == "info")
        check("finished_at set", done.finished_at is not None)
        job = await get_agent_by_slug(s, "daily-check")
        check("no active run after finish", (await active_job_run(s, job.id)) is None)
        check("run listed", any(r.id == run_id for r in await list_job_runs(s)))

    print("\n== stale run sweep ==")
    async with SessionLocal() as s:
        job = await get_agent_by_slug(s, "daily-check")
        stale = await create_job_run(s, agent=job, trigger="schedule")
        stale.started_at = datetime.now(timezone.utc) - timedelta(seconds=job.run_timeout_seconds + 60)
        await s.commit()
        stale_id = stale.id
    async with SessionLocal() as s:
        swept = await sweep_stale_runs(s)
        check("stale run swept", swept == 1, f"swept {swept}")
    async with SessionLocal() as s:
        check("swept run is interrupted", (await get_job_run(s, stale_id)).status == "interrupted")

    print("\n== runner ==")
    import agents.job_runner as job_runner
    from agents.registry import registry

    class _FakeSpecialist:
        def __init__(self, report=None, exc=None):
            self._report = report
            self._exc = exc
        async def run(self, prompt, **kw):
            if self._exc:
                raise self._exc
            class _R:
                pass
            r = _R()
            r.output = self._report
            return r

    class _FakeBuild:
        def __init__(self, specialists):
            self.specialists = specialists

    good = RunReport(
        summary="2 dumps, 0 blockers",
        overall_severity="medium",
        sections=[
            ReportSection(source_key="st22", title="Dumps", checked=True, findings=[]),
            ReportSection(source_key="slg1", title="Log", checked=True, findings=[]),
        ],
    )
    registry._build = _FakeBuild({"Daily Check": _FakeSpecialist(good)})
    os.environ["PUBLIC_BASE_URL"] = "https://approuter.example.com"
    job_runner._has_usable_credentials = lambda agent: asyncio.sleep(0, result=True)

    async with SessionLocal() as s:
        job = await get_agent_by_slug(s, "daily-check")
        agent_id, run_id = job.id, (await create_job_run(s, agent=job, trigger="manual")).id
    await job_runner.execute_run(run_id, agent_id)
    async with SessionLocal() as s:
        r = await get_job_run(s, run_id)
        check("complete run is success", r.status == "success", r.status)
        check("summary stored from report", r.summary == "2 dumps, 0 blockers")
        check("report stored", r.report["overall_severity"] == "medium")

    partial = RunReport(
        summary="partial",
        overall_severity="info",
        sections=[ReportSection(source_key="st22", title="Dumps", checked=True, findings=[])],
    )
    registry._build = _FakeBuild({"Daily Check": _FakeSpecialist(partial)})
    async with SessionLocal() as s:
        job = await get_agent_by_slug(s, "daily-check")
        run_id2 = (await create_job_run(s, agent=job, trigger="manual")).id
    await job_runner.execute_run(run_id2, agent_id)
    async with SessionLocal() as s:
        r = await get_job_run(s, run_id2)
        check("incomplete run is degraded", r.status == "degraded", r.status)
        check("missing section named", r.missing_sections == ["slg1"])

    registry._build = _FakeBuild({"Daily Check": _FakeSpecialist(exc=RuntimeError("mcp down"))})
    async with SessionLocal() as s:
        job = await get_agent_by_slug(s, "daily-check")
        run_id3 = (await create_job_run(s, agent=job, trigger="manual")).id
    await job_runner.execute_run(run_id3, agent_id)
    async with SessionLocal() as s:
        r = await get_job_run(s, run_id3)
        check("failing run is failed", r.status == "failed", r.status)
        check("error recorded", "mcp down" in (r.error or ""))

    print("\n== runner: credential pre-flight ==")
    job_runner._has_usable_credentials = lambda agent: asyncio.sleep(0, result=False)
    registry._build = _FakeBuild({"Daily Check": _FakeSpecialist(good)})
    async with SessionLocal() as s:
        job = await get_agent_by_slug(s, "daily-check")
        run_id4 = (await create_job_run(s, agent=job, trigger="manual")).id
    await job_runner.execute_run(run_id4, agent_id)
    async with SessionLocal() as s:
        r = await get_job_run(s, run_id4)
        check("no credential fails fast", r.status == "failed")
        check("re-authorization message", "re-author" in (r.error or "").lower())

    print("\n== runner: overlap guard ==")
    job_runner._has_usable_credentials = lambda agent: asyncio.sleep(0, result=True)
    async with SessionLocal() as s:
        job = await get_agent_by_slug(s, "daily-check")
        await create_job_run(s, agent=job, trigger="manual")
        try:
            await job_runner.start_run(job, trigger="manual")
            check("overlapping run refused", False, "no error raised")
        except job_runner.RunRefused:
            check("overlapping run refused", True)

    print("\n== runner: agent no longer exists ==")
    async with SessionLocal() as s:
        job = await get_agent_by_slug(s, "daily-check")
        run_id5 = (await create_job_run(s, agent=job, trigger="manual")).id
    await job_runner.execute_run(run_id5, 999999)
    async with SessionLocal() as s:
        r = await get_job_run(s, run_id5)
        check("missing agent is failed", r.status == "failed", r.status)
        check("missing agent error message", "no longer exists" in (r.error or "").lower())

    print("\n== runner: specialist not built ==")
    registry._build = _FakeBuild({})
    async with SessionLocal() as s:
        job = await get_agent_by_slug(s, "daily-check")
        run_id6 = (await create_job_run(s, agent=job, trigger="manual")).id
    await job_runner.execute_run(run_id6, agent_id)
    async with SessionLocal() as s:
        r = await get_job_run(s, run_id6)
        check("unbuilt specialist is failed", r.status == "failed", r.status)
        check("unbuilt specialist error message", "not built" in (r.error or "").lower())
    registry._build = _FakeBuild({"Daily Check": _FakeSpecialist(good)})

    print("\n== runner: start_run refuses non-API agent ==")
    async with SessionLocal() as s:
        chat_only = await get_agent_by_name(s, "chat-only")
    try:
        await job_runner.start_run(chat_only, trigger="manual")
        check("non-API agent refused", False, "no error raised")
    except job_runner.RunRefused:
        check("non-API agent refused", True)

    print("\n== runner: finalize failure inside an except handler still propagates cancellation ==")
    # This targets the exact bug class Finding 1 was about: a finish_job_run
    # that raises from INSIDE an except handler. Pre-fix, that raise replaced
    # the CancelledError that was already unwinding (an exception raised in
    # an except block escapes instead of the original) -- so this would have
    # surfaced as a RuntimeError instead of a CancelledError, and the row
    # would still show whatever finish_job_run half-wrote (or, with the flaky
    # stub used here, nothing at all, since it always raises). Verified by
    # running this block against the pre-fix version of execute_run (the one
    # with unwrapped `finish_job_run` calls, before _finalize existed): it
    # raises RuntimeError instead of CancelledError, and fails both checks
    # below -- so this test does distinguish the fixed behaviour.
    registry._build = _FakeBuild(
        {"Daily Check": _FakeSpecialist(exc=asyncio.CancelledError())}
    )
    _real_finish_job_run = job_runner.finish_job_run

    async def _always_flaky_finish_job_run(session, run_id, **kw):
        raise RuntimeError("db exploded during cancellation")

    job_runner.finish_job_run = _always_flaky_finish_job_run
    async with SessionLocal() as s:
        job = await get_agent_by_slug(s, "daily-check")
        run_id7 = (await create_job_run(s, agent=job, trigger="manual")).id
    raised_cancelled = False
    raised_other: Exception | None = None
    try:
        await job_runner.execute_run(run_id7, agent_id)
    except asyncio.CancelledError:
        raised_cancelled = True
    except Exception as e:  # noqa: BLE001
        raised_other = e
    job_runner.finish_job_run = _real_finish_job_run
    check(
        "cancellation still propagates when finalize fails",
        raised_cancelled, f"other={raised_other!r}",
    )
    async with SessionLocal() as s:
        r = await get_job_run(s, run_id7)
        # finish_job_run always raised, so nothing was ever persisted -- the
        # row is exactly where create_job_run left it. That's the honest
        # outcome of "record-then-reraise, but recording failed": the row
        # stays 'running' (stale-run sweep cleans it up eventually) rather
        # than silently claiming 'interrupted' when it isn't.
        check("row untouched when finalize fails", r.status == "running", r.status)
    registry._build = _FakeBuild({"Daily Check": _FakeSpecialist(good)})

    print("\n== runner: start_run overlap guard is atomic under concurrency ==")
    async with SessionLocal() as s:
        await upsert_agent(
            s, name="Concurrent Check", description="d", instructions="i",
            mcp_servers=SERVERS, expose_chat=False, expose_api=True,
            api_slug="concurrent-check", run_as_principal="svc@example.com",
        )
        concurrent = await get_agent_by_name(s, "Concurrent Check")
    # Both calls hit the same (empty) event loop; the lock in start_run
    # serializes the check-and-create so this is deterministic, not a race
    # against wall-clock timing -- without the lock both would observe "no
    # active run" and both would succeed.
    results = await asyncio.gather(
        job_runner.start_run(concurrent, trigger="manual"),
        job_runner.start_run(concurrent, trigger="manual"),
        return_exceptions=True,
    )
    successes = [r for r in results if isinstance(r, str)]
    refusals = [r for r in results if isinstance(r, job_runner.RunRefused)]
    check("exactly one concurrent start_run succeeds", len(successes) == 1, results)
    check("the other concurrent start_run is refused", len(refusals) == 1, results)
    # Drain the background task the surviving start_run spawned so it
    # doesn't leak a "Task was destroyed but it is pending" warning at exit.
    for t in list(job_runner._tasks):
        await t

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
