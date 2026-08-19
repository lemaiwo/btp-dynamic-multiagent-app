# Scheduled Agent Runs — Increment 1 (Core) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an admin-configured agent be invoked over an API and produce a stored, structured, reviewable run report — with no external BTP services involved yet.

**Architecture:** Agents gain API-exposure fields. A `run_as` context manager binds a non-interactive identity the way `JWTBindingMiddleware` binds a request identity. A shared runner executes the agent with a typed `RunReport` output, applies a completeness post-condition, and writes a `JobRun` row. Two entry points reach that runner: a scope-protected endpoint for the future BTP scheduler, and an admin "Run now" button.

**Tech Stack:** FastAPI, SQLAlchemy async, pydantic-ai 1.72.0 (pinned), Postgres on CF / SQLite locally, vanilla-JS admin UI.

**Spec:** `docs/superpowers/specs/2026-08-19-scheduled-agent-runs-design.md`

## Global Constraints

- **Tests are hand-rolled scripts, not pytest.** Each `tests/test_*.py` defines `check(label, condition, detail)`, counts PASSED/FAILED, prints `==== N passed, M failed ====`, and calls `sys.exit(1 if FAILED else 0)`. Run with `python tests/test_x.py`. Never introduce pytest.
- **Booleans are stored as `Integer`** (see `AgentConfig.enabled`). Follow that convention for new columns.
- **New columns on existing tables MUST use `_ensure_column(conn, table, column, ddl_type)`** in `init_db` — `create_all` does not alter existing tables. New *tables* need no migration.
- **Do not add a DB-level UNIQUE constraint via `_ensure_column`** — it cannot express one portably here. Enforce `api_slug` uniqueness in the CRUD layer.
- **Dev mode:** when `get_validator()` returns `None` (no XSUAA binding), auth dependencies must allow the call. Mirror `require_admin`'s handling exactly.
- Python 3.14, `from __future__ import annotations` at the top of every module.
- Run the full suite before every commit: all `tests/test_*.py` must exit 0.

**Spec addendum discovered during planning:** the spec does not say what user prompt the runner passes to the agent. `Agent.run()` requires one. Task 1 adds a `run_prompt` column for it, defaulting to a fixed string when blank. Note this in the spec when the plan is done.

---

### Task 1: Agent API-exposure fields

**Files:**
- Modify: `agents/db.py` (`AgentConfig` model ~line 137-230, `init_db` ~line 407, `upsert_agent` ~line 590)
- Test: `tests/test_job_runs.py` (create)

**Interfaces:**
- Produces: `AgentConfig.expose_chat`, `.expose_api`, `.api_slug`, `.run_as_principal`, `.run_prompt`, `.run_timeout_seconds`, `.expected_sections` (list[str] property over `expected_sections_json`); `upsert_agent(..., expose_chat=True, expose_api=False, api_slug=None, run_as_principal=None, run_prompt=None, run_timeout_seconds=1800, expected_sections=None)`; `get_agent_by_slug(session, slug) -> AgentConfig | None`; `DEFAULT_RUN_PROMPT`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_job_runs.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe tests/test_job_runs.py`
Expected: FAIL — `ImportError: cannot import name 'DEFAULT_RUN_PROMPT'`

- [ ] **Step 3: Add the columns to `AgentConfig`**

In `agents/db.py`, after `skills_json` (~line 159) add:

```python
    # API exposure. expose_chat keeps the agent in the orchestrator's
    # delegation list; expose_api gives it a run endpoint. They are
    # independent: a run-only agent is expose_chat=0, expose_api=1.
    expose_chat: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    expose_api: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # URL segment for POST /api/agents/{api_slug}/run. Uniqueness is enforced
    # in upsert_agent, not by a DB constraint (see plan Global Constraints).
    api_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Identity API-triggered runs bind via run_as(). No interactive user
    # exists at 03:00, so this names the technical account whose stored
    # OAuth2 token the run uses.
    run_as_principal: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # User prompt handed to Agent.run(). The work itself is described by the
    # agent's instructions and attached skills; this just starts it.
    run_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1800, server_default="1800"
    )
    # JSON list of source_keys a complete report must contain.
    expected_sections_json: Mapped[str | None] = mapped_column(Text, nullable=True)
```

Add near `DEFAULT_ORCHESTRATOR_INSTRUCTIONS`:

```python
DEFAULT_RUN_PROMPT = (
    "Perform your configured check now and return the structured report."
)
```

Add the `expected_sections` property next to the `skills` property:

```python
    @property
    def expected_sections(self) -> list[str]:
        """source_keys a complete report must contain (may be empty)."""
        if not self.expected_sections_json:
            return []
        try:
            data = json.loads(self.expected_sections_json)
        except Exception:
            logger.warning("Malformed expected_sections_json on agent %s", self.name)
            return []
        if not isinstance(data, list):
            return []
        return [str(s) for s in data if isinstance(s, str) and s.strip()]
```

Extend `to_dict` with:

```python
            "expose_chat": bool(self.expose_chat),
            "expose_api": bool(self.expose_api),
            "api_slug": self.api_slug,
            "run_as_principal": self.run_as_principal,
            "run_prompt": self.run_prompt or "",
            "run_timeout_seconds": self.run_timeout_seconds,
            "expected_sections": self.expected_sections,
```

Extend `to_export` with the same keys except `run_as_principal` (an
environment-specific identity that must not travel between landscapes).

- [ ] **Step 4: Add the migrations**

In `init_db`, alongside the existing `_ensure_column` calls:

```python
        await _ensure_column(
            conn, "agent_configs", "expose_chat", "INTEGER NOT NULL DEFAULT 1"
        )
        await _ensure_column(
            conn, "agent_configs", "expose_api", "INTEGER NOT NULL DEFAULT 0"
        )
        await _ensure_column(conn, "agent_configs", "api_slug", "VARCHAR(64)")
        await _ensure_column(
            conn, "agent_configs", "run_as_principal", "VARCHAR(255)"
        )
        await _ensure_column(conn, "agent_configs", "run_prompt", "TEXT")
        await _ensure_column(
            conn,
            "agent_configs",
            "run_timeout_seconds",
            "INTEGER NOT NULL DEFAULT 1800",
        )
        await _ensure_column(
            conn, "agent_configs", "expected_sections_json", "TEXT"
        )
```

- [ ] **Step 5: Extend `upsert_agent` and add `get_agent_by_slug`**

Add parameters to `upsert_agent` after `enabled`:

```python
    expose_chat: bool = True,
    expose_api: bool = False,
    api_slug: str | None = None,
    run_as_principal: str | None = None,
    run_prompt: str | None = None,
    run_timeout_seconds: int = 1800,
    expected_sections: list[str] | None = None,
```

Before writing the row, validate the slug:

```python
    slug = (api_slug or "").strip() or None
    if slug:
        clash = await get_agent_by_slug(session, slug)
        if clash is not None and (existing is None or clash.id != existing.id):
            raise ValueError(f"api_slug {slug!r} is already used by agent {clash.name!r}")
    if expose_api and not slug:
        raise ValueError("expose_api requires an api_slug")
```

Set the fields on both the create and update branches (the update branch
starts at `existing.description = description`):

```python
    row.expose_chat = 1 if expose_chat else 0
    row.expose_api = 1 if expose_api else 0
    row.api_slug = slug
    row.run_as_principal = (run_as_principal or "").strip() or None
    row.run_prompt = (run_prompt or "").strip() or None
    row.run_timeout_seconds = int(run_timeout_seconds)
    row.expected_sections_json = (
        json.dumps(expected_sections) if expected_sections else None
    )
```

(Use whichever variable each branch already binds — `row` in the create
branch, `existing` in the update branch.)

Add next to `get_agent_by_name`:

```python
async def get_agent_by_slug(session: AsyncSession, slug: str) -> AgentConfig | None:
    result = await session.execute(
        select(AgentConfig).where(AgentConfig.api_slug == slug)
    )
    return result.scalar_one_or_none()
```

- [ ] **Step 6: Run the test**

Run: `.venv\Scripts\python.exe tests/test_job_runs.py`
Expected: PASS — 13 passed, 0 failed

- [ ] **Step 7: Run the full suite**

Run each of `tests/test_oauth2.py`, `test_admin_api.py`, `test_admin_ui.py`, `test_a2a.py`, `test_tool_prefixes.py`, `test_signin_dedup.py`, `test_chat_progress_ui.py`, `test_chat_heartbeat_ui.py`, `test_chat_oauth_autocontinue_ui.py`.
Expected: all exit 0. Existing agents get `expose_chat=1` from the column default, so nothing changes for them.

- [ ] **Step 8: Commit**

```bash
git add agents/db.py tests/test_job_runs.py
git commit -m "Add API-exposure fields to agent config"
```

---

### Task 2: Chat exposure filtering in the orchestrator

**Files:**
- Modify: `agents/registry.py` (`build_orchestrator` ~line 418-528)
- Test: `tests/test_job_runs.py`

**Interfaces:**
- Consumes: `AgentConfig.expose_chat` (Task 1).
- Produces: specialists dict contains every enabled agent; only `expose_chat` agents are listed in orchestrator instructions and given a delegation tool.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_job_runs.py` before the results print. This exercises the
selection logic directly rather than booting the model stack:

```python
    print("\n== chat exposure filtering ==")
    from agents.db import list_agents

    async with SessionLocal() as s:
        rows = [r for r in await list_agents(s) if r.enabled]
    chat_rows = [r for r in rows if r.expose_chat]
    check("run-only agent excluded from chat", all(r.name != "Daily Check" for r in chat_rows))
    check("chat agent still included", any(r.name == "chat-only" for r in chat_rows))
    check("both agents still built as specialists", len(rows) >= 2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe tests/test_job_runs.py`
Expected: FAIL — "run-only agent excluded from chat" (nothing filters yet, so `Daily Check` appears)

Note: this fails only if `expose_chat` is not yet honoured. If Task 1 already
stored `expose_chat=0` for `Daily Check`, the assertion passes trivially — in
that case skip to Step 3 and rely on Step 4's orchestrator assertions.

- [ ] **Step 3: Filter the instruction list**

In `build_orchestrator`, replace the `specialist_lines` construction:

```python
    chat_rows = [r for r in enabled_rows if r.expose_chat]
    specialist_lines = [
        f"- **{r.name}**: {r.description.strip()}" for r in chat_rows
    ]
```

Then replace every later use of `enabled_rows` that concerns *chat* routing
with `chat_rows` — specifically the single-specialist special case:

```python
        if len(chat_rows) == 1:
            only = chat_rows[0]
```

Leave the main build loop iterating over `enabled_rows` so run-only agents are
still built.

- [ ] **Step 4: Guard the delegation tool**

At the end of the build loop, replace:

```python
        _attach_delegation_tool(orchestrator, specialist, row)
```

with:

```python
        # Run-only agents are built (the runner needs them) but must not be
        # reachable from chat.
        if row.expose_chat:
            _attach_delegation_tool(orchestrator, specialist, row)
```

- [ ] **Step 5: Run the tests**

Run: `.venv\Scripts\python.exe tests/test_job_runs.py` then `tests/test_admin_api.py` and `tests/test_a2a.py`.
Expected: all PASS. `test_a2a.py` exercises orchestrator construction, so it is the real regression check here.

- [ ] **Step 6: Commit**

```bash
git add agents/registry.py tests/test_job_runs.py
git commit -m "Keep run-only agents out of chat delegation"
```

---

### Task 3: `run_as` non-interactive identity

**Files:**
- Modify: `agents/auth.py`
- Test: `tests/test_job_runs.py`

**Interfaces:**
- Produces: `run_as(principal: str) -> AsyncContextManager[None]` binding `current_principal` and `current_base_url`; `public_base_url() -> str | None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_job_runs.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe tests/test_job_runs.py`
Expected: FAIL — `ImportError: cannot import name 'run_as'`

- [ ] **Step 3: Implement `run_as`**

Add to `agents/auth.py` (imports: `os`, `from contextlib import asynccontextmanager`, `from typing import AsyncIterator`):

```python
def public_base_url() -> str | None:
    """The app's public (approuter) URL, for runs that have no request."""
    return (os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/") or None


@asynccontextmanager
async def run_as(principal: str) -> AsyncIterator[None]:
    """Bind a non-interactive identity for a scheduled or API-triggered run.

    Mirrors what JWTBindingMiddleware does per request, minus the JWT: there
    is no user token to forward, so the run can only reach MCP servers on
    auth_mode "oauth2" (per-user token store, keyed by this principal) or
    "none". auth_mode "jwt" servers will fail, by design.

    current_base_url must be set for PerUserOAuth2Auth to resolve its DCR
    client, and no request exists to derive it from — hence PUBLIC_BASE_URL.
    """
    base_url = public_base_url()
    if not base_url:
        raise RuntimeError(
            "PUBLIC_BASE_URL must be set for API-triggered runs; there is no "
            "request to derive the callback URL from."
        )
    marker_principal = current_principal.set(principal)
    marker_base = current_base_url.set(base_url)
    try:
        yield
    finally:
        current_principal.reset(marker_principal)
        current_base_url.reset(marker_base)
```

- [ ] **Step 4: Run the test**

Run: `.venv\Scripts\python.exe tests/test_job_runs.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agents/auth.py tests/test_job_runs.py
git commit -m "Add run_as context manager for non-interactive runs"
```

---

### Task 4: `RunReport` model

**Files:**
- Create: `agents/reports.py`
- Test: `tests/test_job_runs.py`

**Interfaces:**
- Produces: `Finding`, `ReportSection`, `RunReport`, `Severity`; `missing_sections(report: RunReport, expected: list[str]) -> list[str]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_job_runs.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe tests/test_job_runs.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.reports'`

- [ ] **Step 3: Create `agents/reports.py`**

```python
"""Structured report an API-triggered agent run produces.

The report is data, not prose: the run detail page, the notification summary
and any later trending all read this one structure. pydantic-ai validates it
as the agent's output_type and retries the model on a mismatch, so the shape
is enforced rather than hoped for.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "high", "medium", "low", "info"]


class Finding(BaseModel):
    title: str
    severity: Severity
    count: int = 1
    affected: list[str] = Field(default_factory=list)
    detail: str
    analysis: str | None = None
    recommendation: str | None = None
    references: list[str] = Field(default_factory=list)


class ReportSection(BaseModel):
    source_key: str
    title: str
    # Whether the source was actually queried. A checked source with zero
    # findings is a healthy result; an unchecked one is a gap in the run.
    checked: bool
    findings: list[Finding] = Field(default_factory=list)
    note: str | None = None


class RunReport(BaseModel):
    summary: str
    overall_severity: Severity
    sections: list[ReportSection] = Field(default_factory=list)


def missing_sections(report: RunReport, expected: list[str]) -> list[str]:
    """Expected source_keys the report did not actually check.

    A section with no findings is complete — most days nothing is wrong. Only
    a section that is absent, or present with checked=False, counts as missing.
    """
    checked = {s.source_key for s in report.sections if s.checked}
    return [key for key in expected if key not in checked]
```

- [ ] **Step 4: Run the test**

Run: `.venv\Scripts\python.exe tests/test_job_runs.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agents/reports.py tests/test_job_runs.py
git commit -m "Add structured RunReport model"
```

---

### Task 5: `JobRun` table and CRUD

**Files:**
- Modify: `agents/db.py`
- Test: `tests/test_job_runs.py`

**Interfaces:**
- Produces: `JobRun` model; `create_job_run(...) -> JobRun`, `finish_job_run(...)`, `get_job_run(session, run_id) -> JobRun | None`, `list_job_runs(session, limit=50, agent_id=None) -> list[JobRun]`, `active_job_run(session, agent_id) -> JobRun | None`, `sweep_stale_runs(session) -> int`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_job_runs.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe tests/test_job_runs.py`
Expected: FAIL — `ImportError: cannot import name 'create_job_run'`

- [ ] **Step 3: Add the `JobRun` model**

In `agents/db.py`, after `SkillConfig` (imports needed: `uuid`):

```python
class JobRun(Base):
    """One API-triggered execution of an agent.

    Created before the run starts so the row doubles as the overlap lock: a
    second trigger while one is `running` is refused rather than double-hitting
    the target system.
    """

    __tablename__ = "job_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)  # schedule|manual
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=1800)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    missing_sections_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # False when the notification could not be delivered; the run itself keeps
    # its real status so a delivery problem never loses a completed run.
    notified: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # BTP Job Scheduling callback coordinates (increment 3).
    scheduler_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scheduler_schedule_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scheduler_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scheduler_host: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def report(self) -> dict[str, Any] | None:
        if not self.report_json:
            return None
        try:
            return json.loads(self.report_json)
        except Exception:
            logger.warning("Malformed report_json on run %s", self.id)
            return None

    @property
    def missing_sections(self) -> list[str]:
        if not self.missing_sections_json:
            return []
        try:
            data = json.loads(self.missing_sections_json)
        except Exception:
            return []
        return [str(x) for x in data] if isinstance(data, list) else []

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "trigger": self.trigger,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "summary": self.summary,
            "error": self.error,
            "missing_sections": self.missing_sections,
            "notified": bool(self.notified),
            "created_by": self.created_by,
        }
```

- [ ] **Step 4: Add the CRUD helpers**

```python
ACTIVE_RUN_STATUS = "running"


async def create_job_run(
    session: AsyncSession,
    *,
    agent: AgentConfig,
    trigger: str,
    created_by: str | None = None,
    scheduler: dict[str, str] | None = None,
) -> JobRun:
    row = JobRun(
        id=str(uuid.uuid4()),
        agent_id=agent.id,
        agent_name=agent.name,
        trigger=trigger,
        status=ACTIVE_RUN_STATUS,
        started_at=datetime.now(timezone.utc),
        timeout_seconds=agent.run_timeout_seconds,
        created_by=created_by,
        scheduler_job_id=(scheduler or {}).get("job_id"),
        scheduler_schedule_id=(scheduler or {}).get("schedule_id"),
        scheduler_run_id=(scheduler or {}).get("run_id"),
        scheduler_host=(scheduler or {}).get("host"),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def finish_job_run(
    session: AsyncSession,
    run_id: str,
    *,
    status: str,
    summary: str | None = None,
    report: dict[str, Any] | None = None,
    error: str | None = None,
    missing: list[str] | None = None,
) -> None:
    row = await session.get(JobRun, run_id)
    if row is None:
        return
    row.status = status
    row.summary = summary
    row.report_json = json.dumps(report) if report is not None else None
    row.error = error
    row.missing_sections_json = json.dumps(missing) if missing else None
    row.finished_at = datetime.now(timezone.utc)
    await session.commit()


async def mark_run_notified(session: AsyncSession, run_id: str, ok: bool) -> None:
    row = await session.get(JobRun, run_id)
    if row is not None:
        row.notified = 1 if ok else 0
        await session.commit()


async def get_job_run(session: AsyncSession, run_id: str) -> JobRun | None:
    return await session.get(JobRun, run_id)


async def list_job_runs(
    session: AsyncSession, *, limit: int = 50, agent_id: int | None = None
) -> list[JobRun]:
    stmt = select(JobRun).order_by(JobRun.started_at.desc()).limit(limit)
    if agent_id is not None:
        stmt = stmt.where(JobRun.agent_id == agent_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def active_job_run(session: AsyncSession, agent_id: int) -> JobRun | None:
    result = await session.execute(
        select(JobRun).where(
            JobRun.agent_id == agent_id, JobRun.status == ACTIVE_RUN_STATUS
        )
    )
    return result.scalars().first()


async def sweep_stale_runs(session: AsyncSession) -> int:
    """Mark runs that outlived their timeout as interrupted.

    A crashed or redeployed run would otherwise stay `running` forever and
    wedge the overlap lock, making the agent permanently untriggerable.
    """
    result = await session.execute(
        select(JobRun).where(JobRun.status == ACTIVE_RUN_STATUS)
    )
    now = datetime.now(timezone.utc)
    swept = 0
    for row in result.scalars().all():
        started = row.started_at
        if started is None:
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if (now - started).total_seconds() > row.timeout_seconds:
            row.status = "interrupted"
            row.finished_at = now
            row.error = "Run did not finish within its timeout (app restart or crash)."
            swept += 1
    if swept:
        await session.commit()
    return swept
```

- [ ] **Step 5: Sweep on startup**

In `app.py`'s lifespan, after `init_db()`:

```python
    async with SessionLocal() as session:
        swept = await sweep_stale_runs(session)
    if swept:
        logger.info("Marked %d stale job run(s) as interrupted", swept)
```

- [ ] **Step 6: Run the tests**

Run: `.venv\Scripts\python.exe tests/test_job_runs.py` then `tests/test_admin_api.py`
Expected: both PASS

- [ ] **Step 7: Commit**

```bash
git add agents/db.py app.py tests/test_job_runs.py
git commit -m "Add JobRun records with overlap lock and stale sweep"
```

---

### Task 6: The runner

**Files:**
- Create: `agents/job_runner.py`
- Test: `tests/test_job_runs.py`

**Interfaces:**
- Consumes: `run_as` (Task 3), `RunReport`/`missing_sections` (Task 4), JobRun CRUD (Task 5).
- Produces: `execute_run(run_id: str, agent_id: int) -> None`; `start_run(agent, trigger, created_by=None, scheduler=None) -> str` (creates the row, spawns the task, returns the run id); `RunRefused` exception.

- [ ] **Step 1: Verify the pydantic-ai output_type override**

Run:

```bash
.venv\Scripts\python.exe -c "import inspect; from pydantic_ai import Agent; print('output_type' in inspect.signature(Agent.run).parameters)"
```

Expected: `True`. If it prints `False`, the runner must instead construct a
report-typed agent:
`Agent(model, instructions=..., toolsets=..., output_type=RunReport)` built from
the same `AgentConfig` — note the deviation in the commit message and continue.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_job_runs.py`:

```python
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv\Scripts\python.exe tests/test_job_runs.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.job_runner'`

- [ ] **Step 4: Create `agents/job_runner.py`**

```python
"""Executes an API-triggered agent run and records the result.

Shared by both entry points: the scope-protected endpoint the BTP scheduler
calls, and the admin "Run now" button. The only difference between them is the
trigger label and whether a scheduler callback fires afterwards.
"""

from __future__ import annotations

import asyncio
import logging

from agents.auth import run_as
from agents.db import (
    AgentConfig,
    SessionLocal,
    active_job_run,
    create_job_run,
    finish_job_run,
)
from agents.db import DEFAULT_RUN_PROMPT
from agents.registry import registry
from agents.reports import RunReport, missing_sections

logger = logging.getLogger(__name__)

# Background tasks are kept referenced; asyncio only holds weak references and
# would otherwise garbage-collect a run mid-flight.
_tasks: set[asyncio.Task] = set()


class RunRefused(Exception):
    """The run could not be started (already running, or not API-exposed)."""


async def _has_usable_credentials(agent: AgentConfig) -> bool:
    """True when every oauth2 server this agent binds has a usable token for
    its run-as principal. Patched in tests."""
    from agents.db import AUTH_MODE_OAUTH2
    from agents.oauth2 import has_usable_token, normalize_mcp_url

    principal = agent.run_as_principal
    if not principal:
        return False
    for spec in agent.mcp_servers:
        if spec.get("auth_mode") != AUTH_MODE_OAUTH2:
            continue
        if not await has_usable_token(principal, normalize_mcp_url(str(spec["url"]))):
            return False
    return True


async def start_run(
    agent: AgentConfig,
    *,
    trigger: str,
    created_by: str | None = None,
    scheduler: dict[str, str] | None = None,
) -> str:
    """Create the run record and launch it in the background. Returns run id."""
    if not agent.expose_api:
        raise RunRefused(f"Agent {agent.name!r} is not exposed over the API.")
    async with SessionLocal() as session:
        if await active_job_run(session, agent.id) is not None:
            raise RunRefused(
                f"A run of {agent.name!r} is already in progress."
            )
        run = await create_job_run(
            session, agent=agent, trigger=trigger,
            created_by=created_by, scheduler=scheduler,
        )
    task = asyncio.create_task(execute_run(run.id, agent.id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return run.id


async def execute_run(run_id: str, agent_id: int) -> None:
    """Run the agent and record the outcome. Never raises."""
    async with SessionLocal() as session:
        agent = await session.get(AgentConfig, agent_id)
    if agent is None:
        async with SessionLocal() as session:
            await finish_job_run(
                session, run_id, status="failed",
                error="Agent no longer exists.",
            )
        return

    try:
        if not await _has_usable_credentials(agent):
            async with SessionLocal() as session:
                await finish_job_run(
                    session, run_id, status="failed",
                    error=(
                        f"The service account {agent.run_as_principal!r} has no "
                        "usable credential for this agent's MCP servers. It must "
                        "be re-authorized interactively before scheduled runs can "
                        "work."
                    ),
                )
            return

        specialist = registry.current.specialists.get(agent.name)
        if specialist is None:
            async with SessionLocal() as session:
                await finish_job_run(
                    session, run_id, status="failed",
                    error=f"Agent {agent.name!r} is not built (disabled, or no usable MCP servers).",
                )
            return

        async with run_as(agent.run_as_principal):
            result = await asyncio.wait_for(
                specialist.run(
                    agent.run_prompt or DEFAULT_RUN_PROMPT,
                    output_type=RunReport,
                ),
                timeout=agent.run_timeout_seconds,
            )
        report: RunReport = result.output
        missing = missing_sections(report, agent.expected_sections)
        async with SessionLocal() as session:
            await finish_job_run(
                session, run_id,
                status="degraded" if missing else "success",
                summary=report.summary,
                report=report.model_dump(mode="json"),
                missing=missing,
                error=(
                    "Report did not check: " + ", ".join(missing) if missing else None
                ),
            )
    except asyncio.TimeoutError:
        async with SessionLocal() as session:
            await finish_job_run(
                session, run_id, status="failed",
                error=f"Run exceeded its {agent.run_timeout_seconds}s timeout.",
            )
    except asyncio.CancelledError:
        async with SessionLocal() as session:
            await finish_job_run(
                session, run_id, status="interrupted",
                error="Run was cancelled (app shutting down).",
            )
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Run %s of agent %s failed", run_id, agent.name)
        async with SessionLocal() as session:
            await finish_job_run(
                session, run_id, status="failed", error=f"{type(e).__name__}: {e}",
            )
```

- [ ] **Step 5: Run the test**

Run: `.venv\Scripts\python.exe tests/test_job_runs.py`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add agents/job_runner.py tests/test_job_runs.py
git commit -m "Add job runner with credential pre-flight and completeness check"
```

---

### Task 7: Run endpoints

**Files:**
- Create: `agents/api_runs.py`
- Modify: `agents/auth.py` (add `require_jobscheduler`), `agents/admin.py` (Run-now + run listing), `app.py` (include router)
- Test: `tests/test_admin_api.py`

**Interfaces:**
- Consumes: `start_run`, `RunRefused` (Task 6); JobRun CRUD (Task 5).
- Produces: `POST /api/agents/{slug}/run` → 202 `{"run_id": str}`; `POST /admin/api/agents/{id}/run` → 202 `{"run_id": str}`; `GET /admin/api/runs` → list; `GET /admin/api/runs/{run_id}` → detail with `report`.

- [ ] **Step 1: Write the failing test**

Append to the existing `main()` in `tests/test_admin_api.py`, following its
`client`/`check` conventions:

```python
    print("\n== run endpoints ==")
    r = await client.post("/admin/api/agents", json={
        "name": "Run Agent", "description": "d", "instructions": "i",
        "mcp_url": "https://x.example.com/mcp", "auth_mode": "none",
        "expose_api": True, "api_slug": "run-agent",
        "run_as_principal": "svc@example.com",
    })
    check("create api-exposed agent", r.status_code == 201, r.text)
    agent_id = r.json()["id"]

    r = await client.get("/admin/api/runs")
    check("run list endpoint", r.status_code == 200 and isinstance(r.json(), list))

    r = await client.post("/api/agents/does-not-exist/run")
    check("unknown slug -> 404", r.status_code == 404, r.text)

    r = await client.post("/admin/api/agents/999999/run")
    check("unknown agent id -> 404", r.status_code == 404, r.text)

    r = await client.post("/admin/api/agents", json={
        "name": "Chat Only", "description": "d", "instructions": "i",
        "mcp_url": "https://y.example.com/mcp", "auth_mode": "none",
    })
    chat_id = r.json()["id"]
    r = await client.post(f"/admin/api/agents/{chat_id}/run")
    check("non-API agent rejected", r.status_code == 409, r.text)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe tests/test_admin_api.py`
Expected: FAIL — "run list endpoint" (404, route does not exist)

- [ ] **Step 3: Add `require_jobscheduler`**

In `agents/auth.py`, after `require_admin`:

```python
async def require_jobscheduler(request: Request) -> dict[str, Any]:
    """Ensure the caller holds the `<xsappname>.JOBSCHEDULER` scope.

    Granted to the jobscheduler service instance via `grant-as-authority-to-apps`
    in xs-security.json, so only the scheduler can trigger runs.
    """
    validator = get_validator()
    token = _extract_token(request)

    if validator is None:
        return {"user_name": "local-dev", "scope": ["JOBSCHEDULER"]}

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token"
        )
    payload = validator.validate(token)
    if not validator.has_scope(payload, "JOBSCHEDULER"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Job scheduler scope required",
        )
    return payload
```

- [ ] **Step 4: Create `agents/api_runs.py`**

```python
"""Scheduler-facing run endpoint.

The BTP Job Scheduling Service calls this on a cron. Its synchronous timeout
is 15s, so this must follow the asynchronous contract: acknowledge with 202
immediately and report the outcome later via the Update Run Log callback
(added in increment 3). The scheduler reaches the app directly rather than
through the approuter, so the scope check is what protects this route; it is
deliberately absent from approuter/xs-app.json.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from agents.auth import require_jobscheduler
from agents.db import SessionLocal, get_agent_by_slug
from agents.job_runner import RunRefused, start_run

logger = logging.getLogger(__name__)

router = APIRouter(tags=["runs"])


@router.post(
    "/api/agents/{slug}/run",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_jobscheduler)],
)
async def api_run_agent(slug: str, request: Request) -> dict[str, str]:
    async with SessionLocal() as session:
        agent = await get_agent_by_slug(session, slug)
    if agent is None or not agent.enabled:
        raise HTTPException(status_code=404, detail=f"No agent for slug {slug!r}")

    h = request.headers
    scheduler = {
        "job_id": h.get("x-sap-job-id", ""),
        "schedule_id": h.get("x-sap-job-schedule-id", ""),
        "run_id": h.get("x-sap-job-run-id", ""),
        "host": h.get("x-sap-scheduler-host", ""),
    }
    try:
        run_id = await start_run(agent, trigger="schedule", scheduler=scheduler)
    except RunRefused as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    logger.info("Started scheduled run %s of agent %s", run_id, agent.name)
    return {"run_id": run_id}
```

Register it in `app.py` next to the other routers, **before** the
`app.mount("/", dynamic_chat_app)` catch-all:

```python
from agents.api_runs import router as runs_router
app.include_router(runs_router)
```

- [ ] **Step 5: Add the admin endpoints**

In `agents/admin.py`:

```python
@router.post(
    "/api/agents/{agent_id}/run",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_admin)],
)
async def api_run_now(agent_id: int) -> dict[str, str]:
    """Run-now button: same runner as the scheduler, no callback."""
    from agents.job_runner import RunRefused, start_run

    async with SessionLocal() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Agent not found")
    try:
        run_id = await start_run(
            row, trigger="manual", created_by=current_principal.get()
        )
    except RunRefused as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"run_id": run_id}


@router.get("/api/runs", dependencies=[Depends(require_admin)])
async def api_list_runs(agent_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
    from agents.db import list_job_runs

    async with SessionLocal() as session:
        rows = await list_job_runs(session, limit=limit, agent_id=agent_id)
        return [r.to_dict() for r in rows]


@router.get("/api/runs/{run_id}", dependencies=[Depends(require_admin)])
async def api_get_run(run_id: str) -> dict[str, Any]:
    from agents.db import get_job_run

    async with SessionLocal() as session:
        row = await get_job_run(session, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Run not found")
        data = row.to_dict()
        data["report"] = row.report
        return data
```

Import `current_principal` from `agents.auth` at the top of `admin.py` if not
already imported.

- [ ] **Step 6: Extend `AgentPayload`**

In `agents/admin.py`, add to `AgentPayload`:

```python
    expose_chat: bool = True
    expose_api: bool = False
    api_slug: str = Field(default="", max_length=64)
    run_as_principal: str = Field(default="", max_length=255)
    run_prompt: str = ""
    run_timeout_seconds: int = Field(default=1800, ge=60, le=86400)
    expected_sections: list[str] = Field(default_factory=list)
```

Pass them through in both `api_create_agent` and the update handler
(`api_update_agent`, ~line 366) to `upsert_agent`.

- [ ] **Step 7: Run the tests**

Run: `.venv\Scripts\python.exe tests/test_admin_api.py` then `tests/test_job_runs.py`
Expected: both PASS

- [ ] **Step 8: Commit**

```bash
git add agents/api_runs.py agents/auth.py agents/admin.py app.py tests/test_admin_api.py
git commit -m "Add run endpoints for scheduler and admin run-now"
```

---

### Task 8: Admin UI — exposure fields and Job Runs tab

**Files:**
- Modify: `templates/admin.html`
- Test: `tests/test_admin_ui.py`

**Interfaces:**
- Consumes: `/admin/api/runs`, `/admin/api/runs/{id}`, `/admin/api/agents/{id}/run` (Task 7).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_admin_ui.py`, matching its existing assertion style
(it asserts on served HTML/JS content):

```python
    print("\n== job runs UI ==")
    check("runs tab present", 'data-tab="runs"' in html)
    check("runs list container", 'id="runs-list"' in html)
    check("run-now button", "runAgentNow" in html)
    check("runs api used", "/admin/api/runs" in html)
    check("exposure field expose_api", 'id="agent-expose-api"' in html)
    check("exposure field api_slug", 'id="agent-api-slug"' in html)
    check("run-as field", 'id="agent-run-as"' in html)
    check("expected sections field", 'id="agent-expected-sections"' in html)
    check("endpoint URL hint shown", "/api/agents/" in html)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe tests/test_admin_ui.py`
Expected: FAIL — "runs tab present"

- [ ] **Step 3: Add the exposure fields to the agent form**

Follow the existing field markup in `templates/admin.html`. Add a fieldset:

```html
<fieldset class="exposure">
  <legend>Availability</legend>
  <label><input type="checkbox" id="agent-expose-chat" checked> Available in chat</label>
  <label><input type="checkbox" id="agent-expose-api"> Available via API (schedulable)</label>
  <label>API slug
    <input type="text" id="agent-api-slug" placeholder="daily-check">
  </label>
  <p class="hint" id="agent-endpoint-hint">Endpoint: <code>POST /api/agents/&lt;slug&gt;/run</code></p>
  <label>Run as (technical user)
    <input type="text" id="agent-run-as" placeholder="svc-account@company.com">
  </label>
  <label>Run prompt
    <input type="text" id="agent-run-prompt" placeholder="Perform your configured check now.">
  </label>
  <label>Run timeout (seconds)
    <input type="number" id="agent-run-timeout" value="1800" min="60" max="86400">
  </label>
  <label>Expected report sections (comma-separated)
    <input type="text" id="agent-expected-sections" placeholder="st22, slg1, sm21, sm37">
  </label>
</fieldset>
```

Wire them into the existing agent load/save JS: read/populate each field, and
send `expected_sections` as an array by splitting on commas and trimming.
Update `agent-endpoint-hint` from the slug input's value on change.

- [ ] **Step 4: Add the Job Runs tab**

Add a tab button `data-tab="runs"` alongside the existing tabs, a panel with
`<div id="runs-list"></div>` and `<div id="run-detail"></div>`, and JS:

```javascript
async function loadRuns() {
  const res = await fetch('/admin/api/runs');
  const runs = await res.json();
  const el = document.getElementById('runs-list');
  if (!runs.length) { el.innerHTML = '<p class="hint">No runs yet.</p>'; return; }
  el.innerHTML = '<table><thead><tr><th>Agent</th><th>Status</th><th>Started</th>'
    + '<th>Trigger</th><th>Summary</th></tr></thead><tbody>'
    + runs.map(r => `<tr class="run-row" data-id="${r.id}">
        <td>${esc(r.agent_name)}</td>
        <td><span class="status status-${r.status}">${r.status}</span></td>
        <td>${r.started_at ? new Date(r.started_at).toLocaleString() : ''}</td>
        <td>${esc(r.trigger)}</td>
        <td>${esc(r.summary || r.error || '')}</td>
      </tr>`).join('') + '</tbody></table>';
  el.querySelectorAll('.run-row').forEach(row =>
    row.addEventListener('click', () => showRun(row.dataset.id)));
}

async function showRun(id) {
  const res = await fetch(`/admin/api/runs/${id}`);
  const run = await res.json();
  const el = document.getElementById('run-detail');
  const sections = (run.report && run.report.sections) || [];
  el.innerHTML = `<h3>${esc(run.agent_name)} — ${esc(run.status)}</h3>`
    + (run.error ? `<p class="error">${esc(run.error)}</p>` : '')
    + (run.missing_sections.length
        ? `<p class="warn">Not checked: ${run.missing_sections.map(esc).join(', ')}</p>` : '')
    + sections.map(s => `<section>
        <h4>${esc(s.title)} ${s.checked ? '' : '(not checked)'}</h4>
        ${s.note ? `<p class="hint">${esc(s.note)}</p>` : ''}
        ${s.findings.length
          ? '<ul>' + s.findings.map(f => `<li>
              <strong>${esc(f.title)}</strong>
              <span class="sev sev-${f.severity}">${f.severity}</span> ×${f.count}
              <p>${esc(f.detail)}</p>
              ${f.recommendation ? `<p><em>${esc(f.recommendation)}</em></p>` : ''}
              ${f.references.length
                ? '<p>' + f.references.map(u =>
                    `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a>`).join(' ') + '</p>'
                : ''}
            </li>`).join('') + '</ul>'
          : '<p class="hint">No findings.</p>'}
      </section>`).join('');
}

async function runAgentNow(agentId) {
  const res = await fetch(`/admin/api/agents/${agentId}/run`, { method: 'POST' });
  if (!res.ok) { showError((await res.json()).detail); return; }
  switchTab('runs');
  await loadRuns();
}
```

Reuse the page's existing `esc()` HTML-escaping helper, error banner and
`switchTab` function rather than adding new ones. If no `esc()` exists, add one
— report content is model-generated and must never be injected raw.

- [ ] **Step 5: Add a Run now button to the agent row**

In the agent list rendering, for agents with `expose_api`, add:

```html
<button class="secondary" onclick="runAgentNow(${a.id})">Run now</button>
```

- [ ] **Step 6: Run the tests**

Run: `.venv\Scripts\python.exe tests/test_admin_ui.py` then the full suite.
Expected: all PASS

- [ ] **Step 7: Manual verification**

```bash
.venv\Scripts\python.exe app.py
```

Open `http://127.0.0.1:7932/admin`, create an agent with `expose_api` and a
slug, click **Run now**, confirm a run appears in the Job Runs tab and its
detail renders. The run will fail without a real MCP server — that is the
expected local result, and it verifies the record/UI path.

- [ ] **Step 8: Commit**

```bash
git add templates/admin.html tests/test_admin_ui.py
git commit -m "Add job runs overview and agent exposure fields to admin UI"
```

---

## Self-Review Notes

**Spec coverage.** Increment 1 items all map to tasks: exposure fields (1),
chat filtering (2), `run_as` (3), `RunReport` + post-condition (4), `JobRun`
+ overlap lock + sweep (5), runner + credential pre-flight (6), both endpoints
(7), admin UI (8).

Deliberately deferred to later increments, per the spec's sequencing:

- `agents/ans.py` and event emission → increment 2
- Update Run Log callback, `xs-security.json` scope, `mta.yaml` bindings,
  `PUBLIC_BASE_URL` deployment value → increment 3
- The public `/runs/{id}` page and its `xs-app.json` route → increment 2,
  since it exists to be linked from the notification. Increment 1 views runs
  through `/admin`.
- `JobRun` retention sweep (90 days) → increment 2, alongside notification
  bookkeeping. `sweep_stale_runs` in Task 5 handles *stuck* runs, which is a
  correctness issue, not retention.

**Spec addendum:** `run_prompt` (Task 1) is not in the spec. Add it to the
spec's agent-fields table when this plan is executed.
