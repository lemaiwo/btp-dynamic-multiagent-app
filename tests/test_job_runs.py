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

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
