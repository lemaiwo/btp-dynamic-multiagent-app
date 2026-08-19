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

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
