# Workflow Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run a declared, ordered sequence of agents as one background job that fans out over the work items its first step discovers and branches each item into the specialists that item actually needs.

**Architecture:** Six new tables — three for the definition (`workflows`, `workflow_branches`, `workflow_steps`) and three for the record (`workflow_runs`, `workflow_item_runs`, `workflow_step_runs`). A new `agents/workflow_runner.py` mirrors `agents/job_runner.py` exactly: detached task set, per-entity start lock, a never-raising `_finalize`, and a shutdown cancel hook. Steps hand plain text to each other; the only structure the engine understands is the `WorkItem` the fan-out step returns.

**Tech Stack:** Python 3.13, SQLAlchemy async, FastAPI, pydantic-ai, vanilla-JS admin template.

**Spec:** `docs/superpowers/specs/2026-08-31-agent-workflows-design.md` (components 3–5, increment 3).

**Depends on:** `docs/superpowers/plans/2026-08-31-per-agent-model-and-peer-delegation.md` Task 1 (`AgentConfig.model_name`), which the email reader needs to run on a cheaper model. Peer delegation (Tasks 2–5 of that plan) is **not** a dependency.

## Global Constraints

- **Tests are standalone scripts, not pytest.** Every suite is `python tests/test_x.py`: module-level env setup, a `check(label, condition, detail)` helper, `async def main()`, `asyncio.run(main())`, and `sys.exit(1 if FAILED else 0)`. Copy the shape from `tests/test_job_runs.py`. Do not add pytest.
- **Env must be set before importing `agents.db`.** Each suite sets `DATABASE_URL` to its own SQLite file, pops `VCAP_SERVICES` and `VCAP_APPLICATION`, sets `MCP_URL_ALLOWLIST=""` (empty, not popped), and sets `PUBLIC_BASE_URL` — `agents.auth.run_as` raises without it.
- **`workflow_runner` must reach `job_runner._has_usable_credentials` through the module**, i.e. `from agents import job_runner` then `job_runner._has_usable_credentials(...)`. A `from agents.job_runner import _has_usable_credentials` binds the function at import time and makes it unpatchable, which silently breaks every credential test.
- **`execute_workflow_run` must never raise.** The run row is the overlap lock; an escaping exception leaves it `running` forever. `asyncio.CancelledError` is the sole exception: record `interrupted`, then re-raise so shutdown unwinds. This is the same discipline `agents/job_runner.py:execute_run` documents.
- **New tables come from `create_all`; new columns on existing tables use `_ensure_column`.**
- **Branches and steps are replaced wholesale on save**, not diffed. A workflow definition is small and edited as a unit; diffing would add reconciliation bugs for no benefit.
- **Status vocabulary matches `JobRun`:** `running`, `success`, `failed`, `interrupted`; plus `skipped` on item runs and `partial` on workflow runs.
- **Sequential by default.** `max_parallel_items` defaults to 1 and branches never run concurrently — concurrent work multiplies load on ARC-1 and the model quota.

---

### Task 1: Workflow definition storage and validation

**Files:**
- Modify: `agents/db.py` — three models, CRUD helpers, validation, `init_db`
- Test: `tests/test_workflow_config.py` (create)

**Interfaces:**
- Produces:
  - `Workflow`, `WorkflowBranch`, `WorkflowStep` models, each with `to_dict()`
  - `list_workflows(session) -> list[Workflow]`
  - `get_workflow(session, workflow_id)`, `get_workflow_by_name(session, name)`, `get_workflow_by_slug(session, slug)`
  - `get_workflow_parts(session, workflow_id) -> tuple[list[WorkflowBranch], list[WorkflowStep]]`
  - `validate_workflow_parts(branches, steps, known_agents, *, enabled) -> None` (raises `ValueError`)
  - `upsert_workflow(session, *, name, description, api_slug, run_as_principal, run_timeout_seconds, skip_seen_items, max_parallel_items, on_unknown_branch, enabled, branches, steps) -> Workflow`
  - `delete_workflow(session, workflow_id) -> bool`

`branches` is a list of `{"key", "description", "position"}`; `steps` is a list of
`{"branch_key", "position", "agent_name", "instructions", "fan_out", "step_timeout_seconds"}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_workflow_config.py`:

```python
"""Workflow definition storage and save-time validation.

Run:  python tests/test_workflow_config.py
"""

from __future__ import annotations

import asyncio
import os
import sys
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
    delete_workflow,
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe tests/test_workflow_config.py`
Expected: FAIL — `ImportError: cannot import name 'upsert_workflow' from 'agents.db'`.

- [ ] **Step 3: Add the models**

In `agents/db.py`, add after the `SkillConfig` class:

```python
class Workflow(Base):
    """A declared, ordered sequence of agents run as one background job.

    The definition is the authority: the engine runs exactly these steps in
    exactly this order. No model gets to reorder or skip them — the only
    per-item decision is which branches to enter, and that is data the
    fan-out step emits.
    """

    __tablename__ = "workflows"
    __table_args__ = (UniqueConstraint("name", name="uq_workflows_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    api_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Fallback identity for steps whose agent has no run_as_principal of its
    # own. A scheduled run has no interactive user to borrow one from.
    run_as_principal: Mapped[str | None] = mapped_column(String(255), nullable=True)
    run_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1800, server_default="1800"
    )
    # Repeat-run safety: an item already completed by an earlier run is
    # skipped, so a retry after a crash resumes rather than re-drafting.
    skip_seen_items: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    max_parallel_items: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    on_unknown_branch: Mapped[str] = mapped_column(
        String(16), nullable=False, default="fail", server_default="fail"
    )
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "api_slug": self.api_slug,
            "run_as_principal": self.run_as_principal,
            "run_timeout_seconds": self.run_timeout_seconds,
            "skip_seen_items": bool(self.skip_seen_items),
            "max_parallel_items": self.max_parallel_items,
            "on_unknown_branch": self.on_unknown_branch,
            "enabled": bool(self.enabled),
        }


class WorkflowBranch(Base):
    """A named sub-sequence of steps an item may or may not enter.

    ``key`` is what the fan-out step emits to select this branch;
    ``description`` is shown to it in the branch catalogue so it chooses from
    a list it can see rather than guessing label strings.
    """

    __tablename__ = "workflow_branches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "description": self.description,
            "position": self.position,
        }


class WorkflowStep(Base):
    """One agent invocation in a workflow.

    ``branch_key`` null means the main line; otherwise the step belongs to
    that branch. Every step names exactly one agent — branch selection is the
    item's, not the step's.
    """

    __tablename__ = "workflow_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    branch_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    fan_out: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    step_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=600, server_default="600"
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch_key": self.branch_key,
            "position": self.position,
            "agent_name": self.agent_name,
            "instructions": self.instructions,
            "fan_out": bool(self.fan_out),
            "step_timeout_seconds": self.step_timeout_seconds,
        }
```

- [ ] **Step 4: Add the validation helper**

In `agents/db.py`, add above the workflow CRUD helpers:

```python
VALID_ON_UNKNOWN_BRANCH = ("fail", "skip")


def validate_workflow_parts(
    branches: list[dict[str, Any]],
    steps: list[dict[str, Any]],
    known_agents: set[str],
    *,
    enabled: bool,
) -> None:
    """Reject a workflow definition that cannot run.

    Raises ValueError with a message naming the offending value. This runs at
    save time on purpose: a workflow that cannot run must say so while someone
    is looking at it, not at 03:00 when the scheduler fires.
    """
    if enabled and not steps:
        raise ValueError("An enabled workflow must have at least one step; this has no steps.")

    fan_out_steps = [s for s in steps if s.get("fan_out")]
    if len(fan_out_steps) > 1:
        raise ValueError(
            f"A workflow may have at most one fan-out step; found {len(fan_out_steps)}."
        )

    keys: list[str] = []
    for b in branches:
        key = str(b.get("key") or "").strip()
        if not key:
            raise ValueError("A branch key must not be empty.")
        if key in keys:
            raise ValueError(f"Duplicate branch key {key!r}.")
        keys.append(key)

    if branches and not fan_out_steps:
        raise ValueError(
            "A workflow that declares branches must have a fan-out step: "
            "branches are entered per work item, and without a fan-out step "
            "there are no items."
        )

    for s in steps:
        agent = str(s.get("agent_name") or "").strip()
        if agent not in known_agents:
            raise ValueError(
                f"Step {s.get('position')} names agent {agent!r}, which does not "
                "exist or is disabled."
            )
        bk = s.get("branch_key")
        if bk is not None and str(bk).strip() not in keys:
            raise ValueError(
                f"Step {s.get('position')} belongs to branch {str(bk)!r}, "
                "which is not declared on this workflow."
            )

    used_branches = {str(s["branch_key"]).strip() for s in steps
                     if s.get("branch_key") is not None}
    for key in keys:
        if key not in used_branches:
            raise ValueError(f"Branch {key!r} has no steps.")

    def _check_positions(label: str, group: list[dict[str, Any]]) -> None:
        positions = [int(s.get("position") or 0) for s in group]
        if len(set(positions)) != len(positions):
            raise ValueError(f"Duplicate step positions in {label}: {sorted(positions)}.")
        if sorted(positions) != list(range(1, len(positions) + 1)):
            raise ValueError(
                f"Step positions in {label} must be contiguous from 1; got "
                f"{sorted(positions)}."
            )

    _check_positions("the main line", [s for s in steps if s.get("branch_key") is None])
    for key in keys:
        _check_positions(
            f"branch {key!r}",
            [s for s in steps if str(s.get("branch_key") or "").strip() == key],
        )
```

- [ ] **Step 5: Add the CRUD helpers**

In `agents/db.py`, add after the skills CRUD section:

```python
# ---------------------------------------------------------------------------
# Workflow CRUD
# ---------------------------------------------------------------------------
async def list_workflows(session: AsyncSession) -> list[Workflow]:
    result = await session.execute(select(Workflow).order_by(Workflow.name))
    return list(result.scalars().all())


async def get_workflow(session: AsyncSession, workflow_id: int) -> Workflow | None:
    return await session.get(Workflow, workflow_id)


async def get_workflow_by_name(session: AsyncSession, name: str) -> Workflow | None:
    result = await session.execute(select(Workflow).where(Workflow.name == name))
    return result.scalar_one_or_none()


async def get_workflow_by_slug(session: AsyncSession, slug: str) -> Workflow | None:
    result = await session.execute(select(Workflow).where(Workflow.api_slug == slug))
    return result.scalar_one_or_none()


async def get_workflow_parts(
    session: AsyncSession, workflow_id: int
) -> tuple[list[WorkflowBranch], list[WorkflowStep]]:
    """Branches in position order, and steps in (branch, position) order."""
    b = await session.execute(
        select(WorkflowBranch)
        .where(WorkflowBranch.workflow_id == workflow_id)
        .order_by(WorkflowBranch.position, WorkflowBranch.id)
    )
    s = await session.execute(
        select(WorkflowStep)
        .where(WorkflowStep.workflow_id == workflow_id)
        .order_by(WorkflowStep.branch_key, WorkflowStep.position, WorkflowStep.id)
    )
    return list(b.scalars().all()), list(s.scalars().all())


async def upsert_workflow(
    session: AsyncSession,
    *,
    name: str,
    description: str = "",
    api_slug: str | None = None,
    run_as_principal: str | None = None,
    run_timeout_seconds: int = 1800,
    skip_seen_items: bool = True,
    max_parallel_items: int = 1,
    on_unknown_branch: str = "fail",
    enabled: bool = True,
    branches: list[dict[str, Any]] | None = None,
    steps: list[dict[str, Any]] | None = None,
) -> Workflow:
    """Create or replace a workflow and its parts.

    Branches and steps are replaced wholesale rather than diffed: a definition
    is small and edited as a unit, so reconciliation would add bugs and buy
    nothing.
    """
    branches = branches or []
    steps = steps or []

    if on_unknown_branch not in VALID_ON_UNKNOWN_BRANCH:
        raise ValueError(
            f"on_unknown_branch must be one of {VALID_ON_UNKNOWN_BRANCH}, "
            f"got {on_unknown_branch!r}"
        )

    known_agents = {
        r.name for r in await list_agents(session) if r.enabled
    }
    validate_workflow_parts(branches, steps, known_agents, enabled=enabled)

    existing = await get_workflow_by_name(session, name)
    slug = (api_slug or "").strip() or None
    if slug:
        clash = await get_workflow_by_slug(session, slug)
        if clash is not None and (existing is None or clash.id != existing.id):
            raise ValueError(
                f"api_slug {slug!r} is already used by workflow {clash.name!r}"
            )

    if existing is None:
        row = Workflow(name=name)
        session.add(row)
    else:
        row = existing
    row.description = description
    row.api_slug = slug
    row.run_as_principal = (run_as_principal or "").strip() or None
    row.run_timeout_seconds = int(run_timeout_seconds)
    row.skip_seen_items = 1 if skip_seen_items else 0
    row.max_parallel_items = max(1, int(max_parallel_items))
    row.on_unknown_branch = on_unknown_branch
    row.enabled = 1 if enabled else 0
    await session.commit()
    await session.refresh(row)

    await session.execute(
        delete(WorkflowBranch).where(WorkflowBranch.workflow_id == row.id)
    )
    await session.execute(
        delete(WorkflowStep).where(WorkflowStep.workflow_id == row.id)
    )
    for b in branches:
        session.add(WorkflowBranch(
            workflow_id=row.id,
            key=str(b["key"]).strip(),
            description=str(b.get("description") or ""),
            position=int(b.get("position") or 1),
        ))
    for s in steps:
        bk = s.get("branch_key")
        session.add(WorkflowStep(
            workflow_id=row.id,
            branch_key=str(bk).strip() if bk is not None else None,
            position=int(s["position"]),
            agent_name=str(s["agent_name"]).strip(),
            instructions=str(s.get("instructions") or ""),
            fan_out=1 if s.get("fan_out") else 0,
            step_timeout_seconds=int(s.get("step_timeout_seconds") or 600),
        ))
    await session.commit()
    await session.refresh(row)
    return row


async def delete_workflow(session: AsyncSession, workflow_id: int) -> bool:
    row = await session.get(Workflow, workflow_id)
    if row is None:
        return False
    await session.execute(
        delete(WorkflowBranch).where(WorkflowBranch.workflow_id == workflow_id)
    )
    await session.execute(
        delete(WorkflowStep).where(WorkflowStep.workflow_id == workflow_id)
    )
    await session.delete(row)
    await session.commit()
    return True
```

Add `delete` to the SQLAlchemy import block at the top of `agents/db.py`:

```python
from sqlalchemy import delete
```

(or add `delete` to the existing parenthesised `from sqlalchemy import (...)` list).

- [ ] **Step 6: Run the test to verify it passes**

Run: `./.venv/Scripts/python.exe tests/test_workflow_config.py`
Expected: PASS, `0 failed`. `create_all` in `init_db` creates the three new
tables with no `_ensure_column` calls needed.

- [ ] **Step 7: Commit**

```bash
git add agents/db.py tests/test_workflow_config.py
git commit -m "feat: workflow definition storage and save-time validation"
```

---

### Task 2: Workflow run records

**Files:**
- Modify: `agents/db.py` — three run models and their CRUD
- Test: `tests/test_workflow_runs_storage.py` (create)

**Interfaces:**
- Produces:
  - `WorkflowRun`, `WorkflowItemRun`, `WorkflowStepRun` models with `to_dict()`
  - `create_workflow_run(session, *, workflow, trigger, created_by=None, scheduler=None) -> WorkflowRun`
  - `finish_workflow_run(session, run_id, *, status, summary=None, error=None, counts=None) -> None`
  - `create_item_run(session, *, run_id, item_key, title, branches) -> WorkflowItemRun`
  - `finish_item_run(session, item_run_id, *, status, error=None) -> None`
  - `create_step_run(session, *, run_id, item_run_id, branch_key, position, agent_name) -> WorkflowStepRun`
  - `finish_step_run(session, step_run_id, *, status, output=None, error=None) -> None`
  - `get_workflow_run(session, run_id)`, `list_workflow_runs(session, *, limit=50, workflow_id=None)`
  - `list_item_runs(session, run_id)`, `list_step_runs(session, run_id)`
  - `active_workflow_run(session, workflow_id) -> WorkflowRun | None`
  - `item_succeeded_before(session, *, workflow_id, item_key) -> bool`
  - `sweep_stale_workflow_runs(session, *, all_running=False) -> int`

- [ ] **Step 1: Write the failing test**

Create `tests/test_workflow_runs_storage.py`:

```python
"""Workflow run / item run / step run records.

Run:  python tests/test_workflow_runs_storage.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = ROOT / "tests" / "_test_workflow_runs_storage.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB}"
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ["MCP_URL_ALLOWLIST"] = ""

from agents.db import (  # noqa: E402
    SessionLocal,
    active_workflow_run,
    create_item_run,
    create_step_run,
    create_workflow_run,
    finish_item_run,
    finish_step_run,
    finish_workflow_run,
    get_workflow_run,
    init_db,
    item_succeeded_before,
    list_item_runs,
    list_step_runs,
    list_workflow_runs,
    sweep_stale_workflow_runs,
    upsert_agent,
    upsert_workflow,
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


async def main() -> None:
    await init_db()
    servers = [{"url": "https://x.example.com/mcp", "auth_mode": "none"}]

    async with SessionLocal() as s:
        await upsert_agent(s, name="reader", description="r", instructions="r",
                           mcp_servers=servers)
        wf = await upsert_workflow(
            s, name="wf", description="d", enabled=True,
            steps=[{"branch_key": None, "position": 1, "agent_name": "reader",
                    "instructions": "go", "fan_out": True,
                    "step_timeout_seconds": 600}],
        )
        wf_id = wf.id

    print("\n== run lifecycle ==")
    async with SessionLocal() as s:
        wf = await get_workflow_run.__globals__["get_workflow"](s, wf_id)
        run = await create_workflow_run(s, workflow=wf, trigger="manual",
                                        created_by="me@example.com")
        run_id = run.id
        check("run starts running", run.status == "running", run.status)
        check("run knows its workflow", run.workflow_name == "wf")
        check("active run found", (await active_workflow_run(s, wf_id)) is not None)

    print("\n== item and step runs ==")
    async with SessionLocal() as s:
        item = await create_item_run(s, run_id=run_id, item_key="msg-1",
                                     title="ME21N dump", branches=["abap"])
        item_id = item.id
        step = await create_step_run(s, run_id=run_id, item_run_id=item_id,
                                     branch_key="abap", position=1,
                                     agent_name="reader")
        await finish_step_run(s, step.id, status="success", output="analysis")
        await finish_item_run(s, item_id, status="success")

        items = await list_item_runs(s, run_id)
        steps = await list_step_runs(s, run_id)
        check("item recorded", len(items) == 1 and items[0].item_key == "msg-1")
        check("selected branches recorded",
              items[0].to_dict()["branches"] == ["abap"],
              str(items[0].to_dict()["branches"]))
        check("step recorded", len(steps) == 1 and steps[0].output == "analysis")
        check("step tagged with its branch", steps[0].branch_key == "abap")

    print("\n== finishing the run ==")
    async with SessionLocal() as s:
        await finish_workflow_run(
            s, run_id, status="partial", summary="2 items: 1 ok, 1 failed",
            counts={"items_total": 2, "items_succeeded": 1,
                    "items_failed": 1, "items_skipped": 0},
        )
        row = await get_workflow_run(s, run_id)
        check("status recorded", row.status == "partial", row.status)
        check("counts recorded", row.items_total == 2 and row.items_failed == 1)
        check("finished_at set", row.finished_at is not None)
        check("no longer active", (await active_workflow_run(s, wf_id)) is None)
        check("listed", [r.id for r in await list_workflow_runs(s)] == [run_id])

    print("\n== repeat-run safety lookup ==")
    async with SessionLocal() as s:
        check("succeeded item is seen",
              await item_succeeded_before(s, workflow_id=wf_id, item_key="msg-1"))
        check("unknown item is not seen",
              not await item_succeeded_before(s, workflow_id=wf_id, item_key="msg-9"))

    print("\n== a failed item does not count as seen ==")
    async with SessionLocal() as s:
        wf = await get_workflow_run.__globals__["get_workflow"](s, wf_id)
        run2 = await create_workflow_run(s, workflow=wf, trigger="manual")
        it = await create_item_run(s, run_id=run2.id, item_key="msg-2",
                                   title="t", branches=[])
        await finish_item_run(s, it.id, status="failed", error="boom")
        await finish_workflow_run(s, run2.id, status="partial", summary="x")
        check("failed item is not seen",
              not await item_succeeded_before(s, workflow_id=wf_id, item_key="msg-2"))

    print("\n== stale sweep ==")
    async with SessionLocal() as s:
        wf = await get_workflow_run.__globals__["get_workflow"](s, wf_id)
        stuck = await create_workflow_run(s, workflow=wf, trigger="schedule")
        swept = await sweep_stale_workflow_runs(s, all_running=True)
        check("stale run swept", swept == 1, str(swept))
        row = await get_workflow_run(s, stuck.id)
        check("swept run is interrupted", row.status == "interrupted", row.status)

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
```

Note: the `get_workflow_run.__globals__["get_workflow"]` idiom fetches
`get_workflow` without adding another import line; import it directly instead if
you prefer — the behaviour under test is unaffected.

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe tests/test_workflow_runs_storage.py`
Expected: FAIL — `ImportError: cannot import name 'create_workflow_run'`.

- [ ] **Step 3: Add the run models**

In `agents/db.py`, add after the `WorkflowStep` class:

```python
class WorkflowRun(Base):
    """One execution of a workflow.

    Created before the run starts so the row doubles as the overlap lock, the
    same way JobRun does: a second trigger while one is running is refused
    rather than double-hitting the target systems.
    """

    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    workflow_name: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    items_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_succeeded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scheduler_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scheduler_schedule_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scheduler_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scheduler_host: Mapped[str | None] = mapped_column(Text, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "workflow_name": self.workflow_name,
            "trigger": self.trigger,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "items_total": self.items_total,
            "items_succeeded": self.items_succeeded,
            "items_failed": self.items_failed,
            "items_skipped": self.items_skipped,
            "summary": self.summary,
            "error": self.error,
            "created_by": self.created_by,
        }


class WorkflowItemRun(Base):
    """One work item flowing through a workflow run."""

    __tablename__ = "workflow_item_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    workflow_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # The fan-out step's stable key for this item (e.g. a Gmail message id).
    # Repeat-run safety looks items up by (workflow_id, item_key).
    item_key: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Which branches the reader selected. Stored so the routing decision is
    # auditable and correctable rather than buried in a model's reasoning.
    branches_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def branches(self) -> list[str]:
        if not self.branches_json:
            return []
        try:
            data = json.loads(self.branches_json)
        except Exception:
            logger.warning("Malformed branches_json on item run %s", self.id)
            return []
        return [str(b) for b in data] if isinstance(data, list) else []

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workflow_run_id": self.workflow_run_id,
            "item_key": self.item_key,
            "title": self.title,
            "branches": self.branches,
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class WorkflowStepRun(Base):
    """One agent invocation inside a workflow run."""

    __tablename__ = "workflow_step_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # Null for steps that run before the fan-out, i.e. once per run.
    item_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    branch_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "item_run_id": self.item_run_id,
            "branch_key": self.branch_key,
            "position": self.position,
            "agent_name": self.agent_name,
            "status": self.status,
            "output": self.output,
            "error": self.error,
        }
```

- [ ] **Step 4: Add the run CRUD**

In `agents/db.py`, add after the workflow CRUD section:

```python
# ---------------------------------------------------------------------------
# Workflow run records
# ---------------------------------------------------------------------------
async def create_workflow_run(
    session: AsyncSession,
    *,
    workflow: Workflow,
    trigger: str,
    created_by: str | None = None,
    scheduler: dict[str, str] | None = None,
) -> WorkflowRun:
    row = WorkflowRun(
        id=str(uuid.uuid4()),
        workflow_id=workflow.id,
        workflow_name=workflow.name,
        trigger=trigger,
        status=ACTIVE_RUN_STATUS,
        started_at=datetime.now(timezone.utc),
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


async def finish_workflow_run(
    session: AsyncSession,
    run_id: str,
    *,
    status: str,
    summary: str | None = None,
    error: str | None = None,
    counts: dict[str, int] | None = None,
) -> None:
    row = await session.get(WorkflowRun, run_id)
    if row is None:
        return
    row.status = status
    row.summary = summary
    row.error = error
    for key, value in (counts or {}).items():
        setattr(row, key, int(value))
    row.finished_at = datetime.now(timezone.utc)
    await session.commit()


async def create_item_run(
    session: AsyncSession,
    *,
    run_id: str,
    item_key: str,
    title: str,
    branches: list[str],
) -> WorkflowItemRun:
    parent = await session.get(WorkflowRun, run_id)
    row = WorkflowItemRun(
        id=str(uuid.uuid4()),
        workflow_run_id=run_id,
        workflow_id=parent.workflow_id if parent is not None else 0,
        item_key=item_key[:255],
        title=title,
        branches_json=json.dumps(branches) if branches else None,
        status=ACTIVE_RUN_STATUS,
        started_at=datetime.now(timezone.utc),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def finish_item_run(
    session: AsyncSession, item_run_id: str, *, status: str, error: str | None = None
) -> None:
    row = await session.get(WorkflowItemRun, item_run_id)
    if row is None:
        return
    row.status = status
    row.error = error
    row.finished_at = datetime.now(timezone.utc)
    await session.commit()


async def create_step_run(
    session: AsyncSession,
    *,
    run_id: str,
    item_run_id: str | None,
    branch_key: str | None,
    position: int,
    agent_name: str,
) -> WorkflowStepRun:
    row = WorkflowStepRun(
        id=str(uuid.uuid4()),
        workflow_run_id=run_id,
        item_run_id=item_run_id,
        branch_key=branch_key,
        position=position,
        agent_name=agent_name,
        status=ACTIVE_RUN_STATUS,
        started_at=datetime.now(timezone.utc),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def finish_step_run(
    session: AsyncSession,
    step_run_id: str,
    *,
    status: str,
    output: str | None = None,
    error: str | None = None,
) -> None:
    row = await session.get(WorkflowStepRun, step_run_id)
    if row is None:
        return
    row.status = status
    row.output = output
    row.error = error
    row.finished_at = datetime.now(timezone.utc)
    await session.commit()


async def get_workflow_run(session: AsyncSession, run_id: str) -> WorkflowRun | None:
    return await session.get(WorkflowRun, run_id)


async def list_workflow_runs(
    session: AsyncSession, *, limit: int = 50, workflow_id: int | None = None
) -> list[WorkflowRun]:
    stmt = select(WorkflowRun).order_by(WorkflowRun.started_at.desc()).limit(limit)
    if workflow_id is not None:
        stmt = stmt.where(WorkflowRun.workflow_id == workflow_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_item_runs(session: AsyncSession, run_id: str) -> list[WorkflowItemRun]:
    result = await session.execute(
        select(WorkflowItemRun)
        .where(WorkflowItemRun.workflow_run_id == run_id)
        .order_by(WorkflowItemRun.started_at, WorkflowItemRun.id)
    )
    return list(result.scalars().all())


async def list_step_runs(session: AsyncSession, run_id: str) -> list[WorkflowStepRun]:
    result = await session.execute(
        select(WorkflowStepRun)
        .where(WorkflowStepRun.workflow_run_id == run_id)
        .order_by(WorkflowStepRun.started_at, WorkflowStepRun.id)
    )
    return list(result.scalars().all())


async def active_workflow_run(
    session: AsyncSession, workflow_id: int
) -> WorkflowRun | None:
    result = await session.execute(
        select(WorkflowRun).where(
            WorkflowRun.workflow_id == workflow_id,
            WorkflowRun.status == ACTIVE_RUN_STATUS,
        )
    )
    return result.scalars().first()


async def item_succeeded_before(
    session: AsyncSession, *, workflow_id: int, item_key: str
) -> bool:
    """Has this workflow already completed this item successfully?

    Per workflow, not global: the same email may legitimately be processed by
    two different workflows.
    """
    result = await session.execute(
        select(WorkflowItemRun.id).where(
            WorkflowItemRun.workflow_id == workflow_id,
            WorkflowItemRun.item_key == item_key,
            WorkflowItemRun.status == "success",
        ).limit(1)
    )
    return result.scalars().first() is not None


async def sweep_stale_workflow_runs(
    session: AsyncSession, *, all_running: bool = False
) -> int:
    """Mark leftover `running` rows interrupted. Returns how many were swept.

    A run row is the overlap lock, so a row left `running` by a hard crash
    would wedge the workflow until someone noticed.
    """
    result = await session.execute(
        select(WorkflowRun).where(WorkflowRun.status == ACTIVE_RUN_STATUS)
    )
    rows = list(result.scalars().all())
    if not all_running:
        return len(rows)
    for row in rows:
        row.status = "interrupted"
        row.error = "Run was still marked running at startup; marked interrupted."
        row.finished_at = datetime.now(timezone.utc)
    items = await session.execute(
        select(WorkflowItemRun).where(WorkflowItemRun.status == ACTIVE_RUN_STATUS)
    )
    for item in items.scalars().all():
        item.status = "interrupted"
        item.finished_at = datetime.now(timezone.utc)
    steps = await session.execute(
        select(WorkflowStepRun).where(WorkflowStepRun.status == ACTIVE_RUN_STATUS)
    )
    for step in steps.scalars().all():
        step.status = "interrupted"
        step.finished_at = datetime.now(timezone.utc)
    await session.commit()
    return len(rows)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `./.venv/Scripts/python.exe tests/test_workflow_runs_storage.py`
Expected: PASS, `0 failed`.

- [ ] **Step 6: Commit**

```bash
git add agents/db.py tests/test_workflow_runs_storage.py
git commit -m "feat: workflow run, item run and step run records"
```

---

### Task 3: The runner — preflight and a linear main line

**Files:**
- Create: `agents/workflow_runner.py`
- Test: `tests/test_workflow_runner.py` (create)

**Interfaces:**
- Consumes: `registry.build.specialists`, `agents.auth.run_as`, `job_runner._has_usable_credentials` (through the module, never imported by name).
- Produces:
  - `WorkItem` pydantic model (`id`, `title`, `branches`, `text`)
  - `RunRefused` exception
  - `_tasks: set[asyncio.Task]`
  - `build_prompt(instructions, sources) -> str`
  - `effective_principal(agent_row, workflow) -> str | None`
  - `start_workflow_run(workflow, *, trigger, created_by=None, scheduler=None) -> str`
  - `execute_workflow_run(run_id, workflow_id) -> None`
  - `cancel_all_workflow_runs() -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_workflow_runner.py`:

```python
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

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe tests/test_workflow_runner.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.workflow_runner'`.

- [ ] **Step 3: Write the runner**

Create `agents/workflow_runner.py`:

```python
"""Executes a workflow: an ordered sequence of agents run as one background job.

Deliberately mirrors agents/job_runner.py — detached task set, per-entity start
lock, a never-raising finalizer, and a shutdown cancel hook. That module's
docstrings explain why each of those exists; the reasons apply identically here.

The difference is shape. A job run is one agent; a workflow run is a declared
main line, a fan-out step that discovers work items, and per-item branches the
fan-out step selects. The definition is the authority: no model reorders or
skips a step.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from pydantic import BaseModel, Field

from agents import job_runner
from agents.auth import run_as
from agents.db import (
    SessionLocal,
    Workflow,
    active_workflow_run,
    create_item_run,
    create_step_run,
    create_workflow_run,
    finish_item_run,
    finish_step_run,
    finish_workflow_run,
    get_agent_by_name,
    get_workflow,
    get_workflow_parts,
    item_succeeded_before,
)
from agents.registry import registry

logger = logging.getLogger(__name__)

# Background tasks are kept referenced; asyncio only holds weak references and
# would otherwise garbage-collect a run mid-flight.
_tasks: set[asyncio.Task] = set()

# Serializes check-and-create per workflow, so two concurrent triggers can't
# both observe "no active run" and both start one. Same pattern as
# job_runner._start_locks; closes the in-process race only.
_start_locks: dict[int, asyncio.Lock] = {}


class RunRefused(Exception):
    """The run could not be started (already running, or workflow disabled)."""


class WorkItem(BaseModel):
    """One unit of work discovered by the fan-out step.

    This is the only structure the engine understands. `id` gives the item
    identity that reporting and repeat-run safety need; `branches` names the
    branches this item actually requires. Everything else goes in `text`,
    unparsed, and is handed to the next agent as-is.
    """

    id: str = Field(description="A stable identifier for this item, e.g. a message id.")
    title: str = Field(default="", description="A short human-readable label.")
    branches: list[str] = Field(
        default_factory=list,
        description="Keys of the branches this item needs. Name only what it needs.",
    )
    text: str = Field(description="Everything the next agent should read about this item.")


def build_prompt(instructions: str, sources: list[tuple[str, str]]) -> str:
    """Assemble a step's prompt from its predecessors' outputs.

    Plain text by design: the handoff is steered by writing each agent's
    instructions and each step's task text, not by configuration. Only the
    immediate predecessors appear — at a join that is one block per branch —
    so a long chain over many items cannot grow the prompt without bound.
    """
    parts = [f"## From {name}\n{(text or '').strip()}" for name, text in sources]
    parts.append(f"## Your task\n{instructions.strip()}")
    return "\n\n".join(parts)


def branch_catalogue(branches) -> str:
    """The block appended to the fan-out step's prompt.

    The reader chooses from a list it can see rather than guessing label
    strings, and the "only what is needed" rule sits next to the choices.
    """
    if not branches:
        return ""
    lines = "\n".join(
        f"- {b.key}: {(b.description or '').strip()}" for b in branches
    )
    return (
        "\n\n## Available branches\n" + lines +
        "\n\nFor each item, list in `branches` only the branches that item "
        "actually needs. An item that needs just one must name only that one."
    )


def effective_principal(agent_row, workflow: Workflow) -> str | None:
    """The identity a step binds: the agent's own, else the workflow's."""
    return (agent_row.run_as_principal or "").strip() or (
        (workflow.run_as_principal or "").strip() or None
    )


def _lock_for(workflow_id: int) -> asyncio.Lock:
    lock = _start_locks.get(workflow_id)
    if lock is None:
        lock = asyncio.Lock()
        _start_locks[workflow_id] = lock
    return lock


async def cancel_all_workflow_runs() -> None:
    """Cancel every in-flight workflow run and wait for it to finalize.

    Without this, SIGTERM tears the event loop down under the running tasks and
    their rows stay `running` until the next startup sweep clears them.
    """
    tasks = [t for t in _tasks if not t.done()]
    if not tasks:
        return
    logger.info("Cancelling %d in-flight workflow run(s) for shutdown", len(tasks))
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def start_workflow_run(
    workflow: Workflow,
    *,
    trigger: str,
    created_by: str | None = None,
    scheduler: dict[str, str] | None = None,
) -> str:
    """Create the run record and launch it in the background. Returns run id."""
    if not workflow.enabled:
        raise RunRefused(f"Workflow {workflow.name!r} is disabled.")
    async with _lock_for(workflow.id):
        async with SessionLocal() as session:
            if await active_workflow_run(session, workflow.id) is not None:
                raise RunRefused(
                    f"A run of {workflow.name!r} is already in progress."
                )
            run = await create_workflow_run(
                session, workflow=workflow, trigger=trigger,
                created_by=created_by, scheduler=scheduler,
            )
    task = asyncio.create_task(execute_workflow_run(run.id, workflow.id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return run.id


async def _finalize(run_id: str, **kwargs) -> None:
    """Best-effort finish_workflow_run: never raises.

    execute_workflow_run runs detached with nobody awaiting it, so a failure to
    record the outcome would otherwise leave the row 'running' forever — which
    wedges the overlap lock — and surface only as an "exception was never
    retrieved" warning at GC time.
    """
    try:
        async with SessionLocal() as session:
            await finish_workflow_run(session, run_id, **kwargs)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to finalize workflow run %s (status=%s); its row may be "
            "stuck 'running' until the next stale-run sweep.",
            run_id, kwargs.get("status"),
        )


class _WorkflowError(Exception):
    """Something stopped a run, an item, or a step.

    Carries the operator-facing message. Which scope it kills depends on who
    catches it: preflight failures reach execute_workflow_run and fail the run;
    a step failure inside an item is caught per item and fails only that item.
    """


def _snapshot(rows) -> list:
    """Detach ORM rows into plain namespaces before their session closes.

    A run outlives the session that loaded its definition, and touching a
    detached SQLAlchemy instance later is a latent DetachedInstanceError. The
    definition is small and never mutated mid-run, so copying it is both safe
    and simpler than keeping a session open for the whole run.
    """
    return [SimpleNamespace(**{
        c.name: getattr(row, c.name) for c in row.__table__.columns
    }) for row in rows]


async def _preflight(workflow: Workflow, steps) -> dict:
    """Check every agent the workflow can reach, before any model call.

    Failing here rather than at step three is the whole point: an operator
    reading the run record must see which agent is misconfigured and what to
    do about it.
    """
    resolved: dict = {}
    names = sorted({s.agent_name for s in steps})
    async with SessionLocal() as session:
        for name in names:
            row = await get_agent_by_name(session, name)
            if row is None or not row.enabled:
                raise _WorkflowError(
                    f"Agent {name!r} does not exist or is disabled."
                )
            if registry.build.specialists.get(name) is None:
                raise _WorkflowError(
                    f"Agent {name!r} is not built (disabled, or no usable MCP "
                    "servers). Fix it in /admin and reload."
                )
            if not effective_principal(row, workflow):
                raise _WorkflowError(
                    f"No run-as principal is configured for agent {name!r}, and "
                    f"workflow {workflow.name!r} sets no fallback. A scheduled "
                    "run has no interactive user to borrow an identity from; "
                    "set one in /admin."
                )
            resolved[name] = row
    for name, row in resolved.items():
        if not await job_runner._has_usable_credentials(row):
            raise _WorkflowError(
                f"The service account {effective_principal(row, workflow)!r} has "
                f"no usable credential for agent {name!r}'s MCP servers. It must "
                "be re-authorized interactively before scheduled runs can work."
            )
    return resolved


async def _run_step(
    *,
    run_id: str,
    item_run_id: str | None,
    step,
    agent_row,
    workflow: Workflow,
    prompt: str,
    output_type=None,
):
    """Run one step and record it. Returns the agent's output.

    Raises on failure, after recording the step run as failed — the caller
    decides whether that kills one item or the whole run.
    """
    specialist = registry.build.specialists[step.agent_name]
    async with SessionLocal() as session:
        step_run = await create_step_run(
            session, run_id=run_id, item_run_id=item_run_id,
            branch_key=step.branch_key, position=step.position,
            agent_name=step.agent_name,
        )
    try:
        kwargs = {"output_type": output_type} if output_type is not None else {}
        async with run_as(effective_principal(agent_row, workflow)):
            result = await asyncio.wait_for(
                specialist.run(prompt, **kwargs),
                timeout=step.step_timeout_seconds,
            )
    except asyncio.TimeoutError:
        message = (
            f"Step {step.position} ({step.agent_name}) exceeded its "
            f"{step.step_timeout_seconds}s timeout."
        )
        async with SessionLocal() as session:
            await finish_step_run(session, step_run.id, status="failed", error=message)
        raise _WorkflowError(message) from None
    except asyncio.CancelledError:
        async with SessionLocal() as session:
            await finish_step_run(
                session, step_run.id, status="interrupted",
                error="Cancelled (app shutting down).",
            )
        raise
    except Exception as e:  # noqa: BLE001
        message = f"Step {step.position} ({step.agent_name}) failed: {type(e).__name__}: {e}"
        logger.exception("Workflow run %s step %s failed", run_id, step.position)
        async with SessionLocal() as session:
            await finish_step_run(session, step_run.id, status="failed", error=message)
        raise _WorkflowError(message) from None

    output = result.output
    async with SessionLocal() as session:
        await finish_step_run(
            session, step_run.id, status="success",
            output=output if isinstance(output, str) else repr(output),
        )
    return output


async def execute_workflow_run(run_id: str, workflow_id: int) -> None:
    """Run the workflow and record the outcome.

    Never raises — the row this writes to IS the overlap lock, so any escaping
    exception would leave it stuck 'running' forever. CancelledError is the one
    exception: it is recorded as 'interrupted' and re-raised so app shutdown
    still unwinds normally.
    """
    workflow = None
    try:
        async with SessionLocal() as session:
            row = await get_workflow(session, workflow_id)
            if row is None:
                await _finalize(run_id, status="failed",
                                error="Workflow no longer exists.")
                return
            branches, steps = await get_workflow_parts(session, workflow_id)
            # Detach before the session closes; the run outlives it.
            workflow = _snapshot([row])[0]
            branches, steps = _snapshot(branches), _snapshot(steps)

        try:
            agent_rows = await _preflight(workflow, steps)
        except _WorkflowError as e:
            await _finalize(run_id, status="failed", error=str(e))
            return

        await asyncio.wait_for(
            _run_main_line(run_id, workflow, branches, steps, agent_rows),
            timeout=workflow.run_timeout_seconds,
        )
    except asyncio.TimeoutError:
        # `workflow` can still be None here — the initial load itself may have
        # timed out on a pool acquire, which asyncpg raises as this same
        # TimeoutError. Dereferencing it unguarded would raise AttributeError
        # *inside* this handler, escape execute_workflow_run, and leave the row
        # stuck 'running' — the exact failure this except chain exists to
        # prevent. job_runner.execute_run guards the identical case.
        budget = (
            f"its {workflow.run_timeout_seconds}s timeout"
            if workflow is not None else "its timeout"
        )
        await _finalize(run_id, status="failed", error=f"Run exceeded {budget}.")
    except asyncio.CancelledError:
        await _finalize(run_id, status="interrupted",
                        error="Run was cancelled (app shutting down).")
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Workflow run %s failed", run_id)
        await _finalize(run_id, status="failed", error=f"{type(e).__name__}: {e}")


async def _run_main_line(run_id, workflow, branches, steps, agent_rows) -> None:
    """Run the main line. Fan-out and branches arrive in later tasks."""
    main_line = sorted(
        [s for s in steps if s.branch_key is None], key=lambda s: s.position
    )
    previous: list[tuple[str, str]] = []
    for step in main_line:
        try:
            output = await _run_step(
                run_id=run_id, item_run_id=None, step=step,
                agent_row=agent_rows[step.agent_name], workflow=workflow,
                prompt=build_prompt(step.instructions, previous),
            )
        except _WorkflowError as e:
            await _finalize(run_id, status="failed", error=str(e))
            return
        previous = [(step.agent_name, output if isinstance(output, str) else str(output))]
    await _finalize(run_id, status="success", summary="Completed.")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `./.venv/Scripts/python.exe tests/test_workflow_runner.py`
Expected: PASS, `0 failed`.

- [ ] **Step 5: Commit**

```bash
git add agents/workflow_runner.py tests/test_workflow_runner.py
git commit -m "feat: workflow runner with preflight and a linear main line"
```

---

### Task 4: Fan-out, item runs, and repeat-run safety

**Files:**
- Modify: `agents/workflow_runner.py` — split the main line at the fan-out step
- Test: `tests/test_workflow_runner.py` (extend)

**Interfaces:**
- Consumes: `WorkItem`, `_run_step`, `build_prompt`, `branch_catalogue` (Task 3).
- Produces: `_run_items(...)`, and per-item `WorkflowItemRun` rows.

- [ ] **Step 1: Write the failing test**

In `tests/test_workflow_runner.py`, insert before the final
`print(f"\n==== {PASSED} passed...")`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe tests/test_workflow_runner.py`
Expected: FAIL — `one item run per item` (0 instead of 2), because the runner
still treats the fan-out step as an ordinary step.

- [ ] **Step 3: Split the main line at the fan-out step**

In `agents/workflow_runner.py`, replace `_run_main_line` with:

```python
async def _run_main_line(run_id, workflow, branches, steps, agent_rows) -> None:
    """Run the pre-fan-out steps once, then every item through the rest."""
    main_line = sorted(
        [s for s in steps if s.branch_key is None], key=lambda s: s.position
    )
    branch_steps: dict[str, list] = {}
    for s in steps:
        if s.branch_key is not None:
            branch_steps.setdefault(s.branch_key, []).append(s)
    for group in branch_steps.values():
        group.sort(key=lambda s: s.position)

    fan_index = next(
        (i for i, s in enumerate(main_line) if s.fan_out), None
    )
    before = main_line if fan_index is None else main_line[:fan_index]
    after = [] if fan_index is None else main_line[fan_index + 1:]

    previous: list[tuple[str, str]] = []
    for step in before:
        try:
            output = await _run_step(
                run_id=run_id, item_run_id=None, step=step,
                agent_row=agent_rows[step.agent_name], workflow=workflow,
                prompt=build_prompt(step.instructions, previous),
            )
        except _WorkflowError as e:
            await _finalize(run_id, status="failed", error=str(e))
            return
        previous = [(step.agent_name, str(output))]

    if fan_index is None:
        # No fan-out step: a linear main line that runs once, with no items.
        await _finalize(run_id, status="success", summary="Completed.")
        return

    fan_step = main_line[fan_index]
    try:
        items = await _run_step(
            run_id=run_id, item_run_id=None, step=fan_step,
            agent_row=agent_rows[fan_step.agent_name], workflow=workflow,
            prompt=build_prompt(fan_step.instructions, previous)
                   + branch_catalogue(branches),
            output_type=list[WorkItem],
        )
    except _WorkflowError as e:
        await _finalize(run_id, status="failed", error=str(e))
        return

    await _run_items(run_id, workflow, branches, branch_steps, after,
                     agent_rows, fan_step, list(items or []))


async def _run_items(run_id, workflow, branches, branch_steps, after,
                     agent_rows, fan_step, items) -> None:
    """Run every discovered item through the post-fan-out steps."""
    counts = {"items_total": len(items), "items_succeeded": 0,
              "items_failed": 0, "items_skipped": 0}
    semaphore = asyncio.Semaphore(max(1, workflow.max_parallel_items))
    lock = asyncio.Lock()

    async def process(item: WorkItem) -> None:
        async with semaphore:
            if workflow.skip_seen_items:
                async with SessionLocal() as session:
                    seen = await item_succeeded_before(
                        session, workflow_id=workflow.id, item_key=item.id
                    )
                if seen:
                    async with SessionLocal() as session:
                        item_run = await create_item_run(
                            session, run_id=run_id, item_key=item.id,
                            title=item.title, branches=item.branches,
                        )
                        await finish_item_run(session, item_run.id, status="skipped")
                    async with lock:
                        counts["items_skipped"] += 1
                    return

            async with SessionLocal() as session:
                item_run = await create_item_run(
                    session, run_id=run_id, item_key=item.id,
                    title=item.title, branches=item.branches,
                )
            try:
                sources = await _run_item_branches(
                    run_id, workflow, branches, branch_steps, agent_rows,
                    item, item_run.id, fan_step,
                )
                for step in after:
                    output = await _run_step(
                        run_id=run_id, item_run_id=item_run.id, step=step,
                        agent_row=agent_rows[step.agent_name], workflow=workflow,
                        prompt=build_prompt(step.instructions, sources),
                    )
                    sources = [(step.agent_name, str(output))]
            except _WorkflowError as e:
                async with SessionLocal() as session:
                    await finish_item_run(session, item_run.id, status="failed",
                                          error=str(e))
                async with lock:
                    counts["items_failed"] += 1
                return
            async with SessionLocal() as session:
                await finish_item_run(session, item_run.id, status="success")
            async with lock:
                counts["items_succeeded"] += 1

    await asyncio.gather(*(process(i) for i in items))

    if counts["items_failed"]:
        status = "partial" if counts["items_succeeded"] or counts["items_skipped"] else "failed"
    else:
        status = "success"
    summary = (
        f"{counts['items_total']} item(s): {counts['items_succeeded']} succeeded, "
        f"{counts['items_failed']} failed, {counts['items_skipped']} skipped."
    )
    await _finalize(run_id, status=status, summary=summary, counts=counts)


async def _run_item_branches(run_id, workflow, branches, branch_steps,
                             agent_rows, item, item_run_id,
                             fan_step) -> list[tuple[str, str]]:
    """Branches arrive in the next task; for now hand the item text straight on."""
    return [(fan_step.agent_name, item.text)]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `./.venv/Scripts/python.exe tests/test_workflow_runner.py`
Expected: PASS, `0 failed`.

- [ ] **Step 5: Commit**

```bash
git add agents/workflow_runner.py tests/test_workflow_runner.py
git commit -m "feat: workflow fan-out, item runs and repeat-run safety"
```

---

### Task 5: Branches — fork, catalogue, and join

**Files:**
- Modify: `agents/workflow_runner.py` — implement `_run_item_branches`
- Test: `tests/test_workflow_runner.py` (extend)

**Interfaces:**
- Consumes: `_run_step`, `build_prompt`, `branch_catalogue`, `_run_items` (Task 4).
- Produces: `_run_item_branches` returning one `(agent_name, output)` source per branch taken, which the join step's prompt renders as one `## From` block each.

- [ ] **Step 1: Write the failing test**

In `tests/test_workflow_runner.py`, insert before the final
`print(f"\n==== {PASSED} passed...")`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe tests/test_workflow_runner.py`
Expected: FAIL — `the abap branch ran` is false, because `_run_item_branches`
is still the pass-through stub.

- [ ] **Step 3: Implement branches**

In `agents/workflow_runner.py`, replace the `_run_item_branches` stub with:

```python
async def _run_item_branches(run_id, workflow, branches, branch_steps,
                             agent_rows, item, item_run_id,
                             fan_step) -> list[tuple[str, str]]:
    """Run the branches this item selected and return one source per branch.

    The branches taken are the ones the fan-out step selected, never every
    branch declared — that is the whole point of having a cheap model read the
    question first. Selected branches run sequentially in declared position
    order: concurrent branches would multiply load on the target systems and
    the model quota.

    Returns one (agent_name, output) pair per branch, which the join step's
    prompt renders as one "## From" block each. An item that selected nothing
    yields the item text itself, so the join still has something to read.
    """
    by_key = {b.key: b for b in branches}
    selected = []
    for key in item.branches:
        branch = by_key.get(key)
        if branch is None:
            if workflow.on_unknown_branch == "skip":
                logger.info(
                    "Workflow run %s item %s names unknown branch %r; skipping it",
                    run_id, item.id, key,
                )
                continue
            raise _WorkflowError(
                f"Item {item.id!r} selected branch {key!r}, which this workflow "
                f"does not declare. Declared branches: {sorted(by_key) or 'none'}."
            )
        if branch not in selected:
            selected.append(branch)
    # Declared order, not the order the reader happened to list them in, so two
    # runs of the same item produce the same sequence.
    selected.sort(key=lambda b: (b.position, b.id))

    if not selected:
        return [(fan_step.agent_name, item.text)]

    sources: list[tuple[str, str]] = []
    for branch in selected:
        chained: list[tuple[str, str]] = [(fan_step.agent_name, item.text)]
        last_agent, last_output = fan_step.agent_name, item.text
        for step in branch_steps.get(branch.key, []):
            output = await _run_step(
                run_id=run_id, item_run_id=item_run_id, step=step,
                agent_row=agent_rows[step.agent_name], workflow=workflow,
                prompt=build_prompt(step.instructions, chained),
            )
            last_agent, last_output = step.agent_name, str(output)
            chained = [(last_agent, last_output)]
        sources.append((last_agent, last_output))
    return sources
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `./.venv/Scripts/python.exe tests/test_workflow_runner.py`
Expected: PASS, `0 failed`.

- [ ] **Step 5: Commit**

```bash
git add agents/workflow_runner.py tests/test_workflow_runner.py
git commit -m "feat: workflow branches with fork, catalogue and join"
```

---

### Task 6: Trigger endpoint, shutdown, and the reload guard

**Files:**
- Modify: `agents/api_runs.py` — the workflow run endpoint
- Modify: `app.py` — lifespan sweep and shutdown cancel
- Modify: `agents/registry.py` — keep MCP clients alive for in-flight workflow runs
- Test: `tests/test_workflow_runner.py` (extend)

**Interfaces:**
- Consumes: `start_workflow_run`, `RunRefused`, `cancel_all_workflow_runs`, `sweep_stale_workflow_runs`, `get_workflow_by_slug`.
- Produces: `POST /api/workflows/{slug}/run` returning `202 {"run_id": ...}`.

- [ ] **Step 1: Write the failing test**

In `tests/test_workflow_runner.py`, insert before the final
`print(f"\n==== {PASSED} passed...")`:

```python
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
    await asyncio.gather(*[t for t in wr._tasks if not t.done()],
                         return_exceptions=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe tests/test_workflow_runner.py`
Expected: FAIL — `client kept open while a workflow run is in flight` is false,
and the endpoint returns 404 for a valid slug.

- [ ] **Step 3: Add the endpoint**

In `agents/api_runs.py`, add the import and the route:

```python
from agents.db import SessionLocal, get_agent_by_slug, get_workflow_by_slug
from agents.workflow_runner import RunRefused as WorkflowRunRefused
from agents.workflow_runner import start_workflow_run


@router.post(
    "/api/workflows/{slug}/run",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_jobscheduler)],
)
async def api_run_workflow(slug: str, request: Request) -> dict[str, str]:
    async with SessionLocal() as session:
        workflow = await get_workflow_by_slug(session, slug)
    if workflow is None or not workflow.enabled:
        raise HTTPException(status_code=404, detail=f"No workflow for slug {slug!r}")

    h = request.headers
    scheduler = {
        "job_id": h.get("x-sap-job-id", ""),
        "schedule_id": h.get("x-sap-job-schedule-id", ""),
        "run_id": h.get("x-sap-job-run-id", ""),
        "host": h.get("x-sap-scheduler-host", ""),
    }
    try:
        run_id = await start_workflow_run(
            workflow, trigger="schedule", scheduler=scheduler
        )
    except WorkflowRunRefused as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    logger.info("Started scheduled run %s of workflow %s", run_id, workflow.name)
    return {"run_id": run_id}
```

- [ ] **Step 4: Extend the reload guard**

In `agents/registry.py`, in `Registry.reload`, change the in-flight check so a
workflow run also protects the old build's clients:

```python
                # Deferred import: both runners import this module at load
                # time, so a top-level import here would be circular.
                from agents.job_runner import _tasks as in_flight_runs
                from agents.workflow_runner import _tasks as in_flight_workflows

                busy = len(in_flight_runs) + len(in_flight_workflows)
                if busy:
                    logger.info(
                        "Keeping %d MCP client(s) from the previous build open: "
                        "%d run(s) still in flight are using them.",
                        len(old.mcp_clients), busy,
                    )
                else:
```

Leave the `else:` body (the client-closing loop) unchanged.

- [ ] **Step 5: Wire the lifespan**

In `app.py`, extend the imports:

```python
from agents.db import SessionLocal, init_db, sweep_stale_runs, sweep_stale_workflow_runs
from agents.workflow_runner import cancel_all_workflow_runs
```

In the lifespan startup block, next to the existing
`swept = await sweep_stale_runs(session, all_running=True)`:

```python
        swept_wf = await sweep_stale_workflow_runs(session, all_running=True)
        if swept_wf:
            logger.info("Swept %d stale workflow run(s) at startup", swept_wf)
```

and in the shutdown block, next to `await cancel_all_runs()`:

```python
    await cancel_all_workflow_runs()
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `./.venv/Scripts/python.exe tests/test_workflow_runner.py`
Expected: PASS, `0 failed`.

- [ ] **Step 7: Run the suites this task touches**

Run:
```bash
./.venv/Scripts/python.exe tests/test_job_runs.py
./.venv/Scripts/python.exe tests/test_admin_api.py
```
Expected: both PASS. `test_job_runs.py` exercises `registry.reload()`'s
in-flight guard, which this task changed.

- [ ] **Step 8: Commit**

```bash
git add agents/api_runs.py agents/registry.py app.py tests/test_workflow_runner.py
git commit -m "feat: workflow trigger endpoint, shutdown cancel and reload guard"
```

---

### Task 7: Admin API and UI for workflows

**Files:**
- Modify: `agents/admin.py` — workflow CRUD routes, run-now, run listing
- Modify: `templates/admin.html` — Workflows panel, editor modal, runs view
- Test: `tests/test_workflow_admin_api.py` (create), `tests/test_admin_ui.py` (extend)

**Interfaces:**
- Produces:
  - `GET /admin/api/workflows`, `POST /admin/api/workflows`, `GET|PUT|DELETE /admin/api/workflows/{id}`
  - `POST /admin/api/workflows/{id}/run` → `{"run_id": ...}`
  - `GET /admin/api/workflow-runs`, `GET /admin/api/workflow-runs/{run_id}` (run plus its items and steps)

- [ ] **Step 1: Write the failing test**

Create `tests/test_workflow_admin_api.py`:

```python
"""Workflow admin API: CRUD, validation surfacing, run-now, run views.

Run:  python tests/test_workflow_admin_api.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = ROOT / "tests" / "_test_workflow_admin.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB}"
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ["MCP_URL_ALLOWLIST"] = ""
os.environ["PUBLIC_BASE_URL"] = "https://app.example.com"

from httpx import ASGITransport, AsyncClient  # noqa: E402

import app as app_module  # noqa: E402
from agents.db import SessionLocal, init_db, upsert_agent  # noqa: E402

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

GOOD = {
    "name": "mail-triage",
    "description": "triage the inbox",
    "api_slug": "mail-triage",
    "run_as_principal": "svc@example.com",
    "run_timeout_seconds": 1800,
    "skip_seen_items": True,
    "max_parallel_items": 1,
    "on_unknown_branch": "fail",
    "enabled": True,
    "branches": [{"key": "abap", "description": "ABAP", "position": 1}],
    "steps": [
        {"branch_key": None, "position": 1, "agent_name": "reader",
         "instructions": "triage", "fan_out": True, "step_timeout_seconds": 600},
        {"branch_key": None, "position": 2, "agent_name": "drafter",
         "instructions": "draft", "fan_out": False, "step_timeout_seconds": 600},
        {"branch_key": "abap", "position": 1, "agent_name": "abap",
         "instructions": "analyze", "fan_out": False, "step_timeout_seconds": 600},
    ],
}


async def main() -> None:
    await init_db()
    async with SessionLocal() as s:
        for n in ("reader", "abap", "drafter"):
            await upsert_agent(s, name=n, description=n, instructions=n,
                               mcp_servers=SERVERS, run_as_principal="svc@example.com")

    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        print("\n== create and read ==")
        r = await c.post("/admin/api/workflows", json=GOOD)
        check("created", r.status_code in (200, 201), f"{r.status_code} {r.text[:200]}")
        wf_id = r.json()["id"]

        r = await c.get("/admin/api/workflows")
        check("listed", any(w["name"] == "mail-triage" for w in r.json()), r.text[:200])

        r = await c.get(f"/admin/api/workflows/{wf_id}")
        body = r.json()
        check("detail carries branches", len(body["branches"]) == 1, r.text[:200])
        check("detail carries steps", len(body["steps"]) == 3, r.text[:200])

        print("\n== validation is surfaced as 4xx, not 500 ==")
        for label, mutate, expect in [
            ("two fan-out steps",
             lambda d: d["steps"][1].update({"fan_out": True}), "fan-out"),
            ("unknown agent",
             lambda d: d["steps"][0].update({"agent_name": "ghost"}), "ghost"),
            ("step in an undeclared branch",
             lambda d: d["steps"][2].update({"branch_key": "nope"}), "nope"),
            ("branch with no steps",
             lambda d: d["branches"].append(
                 {"key": "fiori", "description": "f", "position": 2}), "fiori"),
            ("non-contiguous positions",
             lambda d: d["steps"][1].update({"position": 5}), "contiguous"),
        ]:
            payload = {**GOOD, "name": "broken", "api_slug": "broken",
                       "branches": [dict(b) for b in GOOD["branches"]],
                       "steps": [dict(s) for s in GOOD["steps"]]}
            mutate(payload)
            r = await c.post("/admin/api/workflows", json=payload)
            check(f"{label} rejected with 4xx",
                  400 <= r.status_code < 500, f"{r.status_code} {r.text[:160]}")
            check(f"{label} explains why",
                  expect.lower() in r.text.lower(), r.text[:200])

        print("\n== duplicate slug ==")
        dup = {**GOOD, "name": "second"}
        r = await c.post("/admin/api/workflows", json=dup)
        check("duplicate slug rejected", 400 <= r.status_code < 500,
              f"{r.status_code} {r.text[:160]}")

        print("\n== update and delete ==")
        updated = {**GOOD, "description": "changed"}
        r = await c.put(f"/admin/api/workflows/{wf_id}", json=updated)
        check("updated", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
        check("change persisted", r.json()["description"] == "changed", r.text[:160])

        print("\n== run now ==")
        r = await c.post(f"/admin/api/workflows/{wf_id}/run")
        # The agents are not built in this suite, so the run starts and then
        # fails preflight — starting is what this asserts.
        check("run accepted", r.status_code in (200, 202),
              f"{r.status_code} {r.text[:200]}")
        run_id = r.json().get("run_id")
        check("run id returned", bool(run_id), r.text[:200])

        import agents.workflow_runner as wr  # noqa: PLC0415
        await asyncio.gather(*[t for t in wr._tasks if not t.done()],
                             return_exceptions=True)

        print("\n== run views ==")
        r = await c.get("/admin/api/workflow-runs")
        check("runs listed", any(x["id"] == run_id for x in r.json()), r.text[:200])
        r = await c.get(f"/admin/api/workflow-runs/{run_id}")
        body = r.json()
        check("detail has the run", body["run"]["id"] == run_id, r.text[:200])
        check("detail has items", "items" in body, r.text[:200])
        check("detail has steps", "steps" in body, r.text[:200])

        r = await c.delete(f"/admin/api/workflows/{wf_id}")
        check("deleted", r.status_code in (200, 204), str(r.status_code))

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe tests/test_workflow_admin_api.py`
Expected: FAIL — `created` gets 404, the routes do not exist.

- [ ] **Step 3: Add the payload models and routes**

In `agents/admin.py`, add the payload models next to the existing ones:

```python
class WorkflowBranchPayload(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    description: str = ""
    position: int = 1


class WorkflowStepPayload(BaseModel):
    branch_key: str | None = None
    position: int
    agent_name: str = Field(min_length=1, max_length=64)
    instructions: str = ""
    fan_out: bool = False
    step_timeout_seconds: int = 600


class WorkflowPayload(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = ""
    api_slug: str = ""
    run_as_principal: str = ""
    run_timeout_seconds: int = 1800
    skip_seen_items: bool = True
    max_parallel_items: int = 1
    on_unknown_branch: str = "fail"
    enabled: bool = True
    branches: list[WorkflowBranchPayload] = Field(default_factory=list)
    steps: list[WorkflowStepPayload] = Field(default_factory=list)
```

and the routes:

```python
# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------
async def _save_workflow(payload: WorkflowPayload, workflow_id: int | None):
    """Shared create/update body. ValueError from the DB layer is a 400.

    Every save-time rule lives in validate_workflow_parts, so surfacing its
    message verbatim is what tells an operator which step is wrong.
    """
    async with SessionLocal() as session:
        if workflow_id is not None:
            existing = await get_workflow(session, workflow_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="Workflow not found")
            if existing.name != payload.name:
                clash = await get_workflow_by_name(session, payload.name)
                if clash is not None:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Workflow name '{payload.name}' already exists",
                    )
                existing.name = payload.name
                await session.commit()
        try:
            row = await upsert_workflow(
                session,
                name=payload.name,
                description=payload.description,
                api_slug=payload.api_slug,
                run_as_principal=payload.run_as_principal,
                run_timeout_seconds=payload.run_timeout_seconds,
                skip_seen_items=payload.skip_seen_items,
                max_parallel_items=payload.max_parallel_items,
                on_unknown_branch=payload.on_unknown_branch,
                enabled=payload.enabled,
                branches=[b.model_dump() for b in payload.branches],
                steps=[s.model_dump() for s in payload.steps],
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        branches, steps = await get_workflow_parts(session, row.id)
        return {
            **row.to_dict(),
            "branches": [b.to_dict() for b in branches],
            "steps": [s.to_dict() for s in steps],
        }


@router.get("/api/workflows", dependencies=[Depends(require_admin)])
async def api_list_workflows() -> list[dict[str, Any]]:
    async with SessionLocal() as session:
        return [w.to_dict() for w in await list_workflows(session)]


@router.post("/api/workflows", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_admin)])
async def api_create_workflow(payload: WorkflowPayload) -> dict[str, Any]:
    return await _save_workflow(payload, None)


@router.get("/api/workflows/{workflow_id}", dependencies=[Depends(require_admin)])
async def api_get_workflow(workflow_id: int) -> dict[str, Any]:
    async with SessionLocal() as session:
        row = await get_workflow(session, workflow_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        branches, steps = await get_workflow_parts(session, workflow_id)
        return {
            **row.to_dict(),
            "branches": [b.to_dict() for b in branches],
            "steps": [s.to_dict() for s in steps],
        }


@router.put("/api/workflows/{workflow_id}", dependencies=[Depends(require_admin)])
async def api_update_workflow(workflow_id: int, payload: WorkflowPayload) -> dict[str, Any]:
    return await _save_workflow(payload, workflow_id)


@router.delete("/api/workflows/{workflow_id}",
               status_code=status.HTTP_204_NO_CONTENT,
               dependencies=[Depends(require_admin)])
async def api_delete_workflow(workflow_id: int) -> None:
    async with SessionLocal() as session:
        if not await delete_workflow(session, workflow_id):
            raise HTTPException(status_code=404, detail="Workflow not found")


@router.post("/api/workflows/{workflow_id}/run", dependencies=[Depends(require_admin)])
async def api_run_workflow_now(workflow_id: int) -> dict[str, str]:
    async with SessionLocal() as session:
        row = await get_workflow(session, workflow_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
    try:
        run_id = await start_workflow_run(row, trigger="manual")
    except WorkflowRunRefused as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"run_id": run_id}


@router.get("/api/workflow-runs", dependencies=[Depends(require_admin)])
async def api_list_workflow_runs(
    limit: int = Query(default=50, ge=1, le=200),
    workflow_id: int | None = Query(default=None),
) -> list[dict[str, Any]]:
    async with SessionLocal() as session:
        rows = await list_workflow_runs(session, limit=limit, workflow_id=workflow_id)
        return [r.to_dict() for r in rows]


@router.get("/api/workflow-runs/{run_id}", dependencies=[Depends(require_admin)])
async def api_get_workflow_run(run_id: str) -> dict[str, Any]:
    async with SessionLocal() as session:
        row = await get_workflow_run(session, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Run not found")
        items = await list_item_runs(session, run_id)
        steps = await list_step_runs(session, run_id)
        return {
            "run": row.to_dict(),
            "items": [i.to_dict() for i in items],
            "steps": [s.to_dict() for s in steps],
        }
```

Extend the `agents.db` import block in `agents/admin.py` with:
`delete_workflow`, `get_workflow`, `get_workflow_by_name`, `get_workflow_parts`,
`get_workflow_run`, `list_item_runs`, `list_step_runs`, `list_workflow_runs`,
`list_workflows`, `upsert_workflow`. Add at the top:

```python
from agents.workflow_runner import RunRefused as WorkflowRunRefused
from agents.workflow_runner import start_workflow_run
```

- [ ] **Step 4: Run the API test to verify it passes**

Run: `./.venv/Scripts/python.exe tests/test_workflow_admin_api.py`
Expected: PASS, `0 failed`.

- [ ] **Step 5: Commit the API**

```bash
git add agents/admin.py tests/test_workflow_admin_api.py
git commit -m "feat: admin API for workflows and workflow runs"
```

- [ ] **Step 6: Write the failing UI test**

In `tests/test_admin_ui.py`, add `"workflows-tbody"` and `"workflow-runs-tbody"`
to the tbody assertions near the existing `agents-tbody` / `skills-tbody`
checks:

```python
        check("workflows tbody present",
              find(coll, "tbody", id="workflows-tbody") is not None)
        check("workflow runs tbody present",
              find(coll, "tbody", id="workflow-runs-tbody") is not None)
```

and add these JS assertions alongside the other `js` checks:

```python
        check("workflow steps are collected on save", "collectWorkflowSteps()" in js)
        check("workflow branches are collected on save",
              "collectWorkflowBranches()" in js)
        check("run-now posts to the workflow run endpoint",
              "/workflows/${id}/run" in js or "/workflows/' + id + '/run" in js)
```

- [ ] **Step 7: Run the UI test to verify it fails**

Run: `./.venv/Scripts/python.exe tests/test_admin_ui.py`
Expected: FAIL — `workflows tbody present`.

- [ ] **Step 8: Add the Workflows panel**

In `templates/admin.html`, add a panel modelled on the existing Skills panel,
placed after it:

```html
        <section class="panel">
            <h2>Workflows</h2>
            <p class="hint">
                A workflow runs several agents in a declared order as one
                background job. Its fan-out step returns the work items; each
                item enters only the branches that step selected for it, and the
                remaining main-line steps then run as the join.
            </p>
            <button onclick="openWorkflowModal()">+ New workflow</button>
            <table>
                <thead><tr><th>Name</th><th>Steps</th><th>Slug</th><th></th></tr></thead>
                <tbody id="workflows-tbody"></tbody>
            </table>
        </section>

        <section class="panel">
            <h2>Workflow runs</h2>
            <table>
                <thead><tr><th>Workflow</th><th>Status</th><th>Items</th><th>Started</th><th></th></tr></thead>
                <tbody id="workflow-runs-tbody"></tbody>
            </table>
        </section>
```

Add the editor modal next to the existing `#skill-modal`, using the same
`modal-backdrop` / `modal` classes and the same click-outside-to-close pattern:

```html
<div id="workflow-modal" class="modal-backdrop" onclick="if(event.target===this)closeWorkflowModal()">
    <div class="modal">
        <h3 id="workflow-modal-title">New workflow</h3>
        <input type="hidden" id="workflow-id">
        <label>Name <input type="text" id="workflow-name" placeholder="mail-triage" required></label>
        <label>Description <textarea id="workflow-description" rows="2"></textarea></label>
        <label>API slug <span style="color:var(--muted);font-weight:normal">(the BTP scheduler posts to /api/workflows/&lt;slug&gt;/run)</span>
            <input type="text" id="workflow-api-slug" placeholder="mail-triage"></label>
        <label>Run as (technical user) <span style="color:var(--muted);font-weight:normal">(fallback for steps whose agent has none)</span>
            <input type="text" id="workflow-run-as" placeholder="svc@example.com"></label>
        <label>Run timeout (seconds) <input type="number" id="workflow-timeout" value="1800" min="60"></label>
        <label>Max items in parallel <span style="color:var(--muted);font-weight:normal">(1 keeps load on the target systems predictable)</span>
            <input type="number" id="workflow-max-parallel" value="1" min="1"></label>
        <label>If an item names an unknown branch
            <select id="workflow-on-unknown-branch">
                <option value="fail">Fail that item</option>
                <option value="skip">Ignore the unknown branch</option>
            </select>
        </label>
        <label><input type="checkbox" id="workflow-skip-seen" checked>
            Skip items this workflow already completed</label>
        <label><input type="checkbox" id="workflow-enabled" checked> Enabled</label>

        <label class="full">
            Branches <span style="color:var(--muted);font-weight:normal">(optional — the fan-out step picks per item from this list, so write descriptions it can choose by)</span>
            <div id="workflow-branches"></div>
            <button type="button" class="secondary small" onclick="addWorkflowBranchRow()">+ Add branch</button>
        </label>
        <label class="full">
            Steps <span style="color:var(--muted);font-weight:normal">(run in this order; exactly one may be the fan-out step)</span>
            <div id="workflow-steps"></div>
            <button type="button" class="secondary small" onclick="addWorkflowStepRow()">+ Add step</button>
        </label>

        <div class="modal-actions">
            <button class="secondary" onclick="closeWorkflowModal()">Cancel</button>
            <button onclick="saveWorkflow()">Save</button>
        </div>
    </div>
</div>
```

Match the surrounding file's actual class names and action-row markup if they
differ from `modal-actions` — copy them from `#skill-modal`.

- [ ] **Step 9: Add the Workflows JS**

Add functions mirroring the existing agent/skill handlers:
`loadWorkflows()`, `renderWorkflows()`, `openWorkflowModal()`,
`editWorkflow(id)`, `saveWorkflow()`, `deleteWorkflow(id)`,
`runWorkflowNow(id)`, `loadWorkflowRuns()`, `showWorkflowRun(runId)`,
`addWorkflowBranchRow()`, `addWorkflowStepRow()`, `collectWorkflowBranches()`,
`collectWorkflowSteps()`.

`collectWorkflowSteps()` must assign `position` per group — 1..n over the
main-line rows, and independently 1..n within each branch — because
`validate_workflow_parts` requires contiguous positions and will otherwise
reject the save with a message the user did not cause:

```javascript
function collectWorkflowSteps() {
    const rows = Array.from(document.querySelectorAll('#workflow-steps .wf-step-row'));
    const counters = {};
    return rows.map(row => {
        const branch = row.querySelector('.wf-step-branch').value || null;
        const groupKey = branch === null ? '__main__' : branch;
        counters[groupKey] = (counters[groupKey] || 0) + 1;
        return {
            branch_key: branch,
            position: counters[groupKey],
            agent_name: row.querySelector('.wf-step-agent').value,
            instructions: row.querySelector('.wf-step-instructions').value,
            fan_out: row.querySelector('.wf-step-fanout').checked,
            step_timeout_seconds: parseInt(row.querySelector('.wf-step-timeout').value, 10) || 600,
        };
    });
}

function collectWorkflowBranches() {
    return Array.from(document.querySelectorAll('#workflow-branches .wf-branch-row'))
        .map((row, i) => ({
            key: row.querySelector('.wf-branch-key').value.trim(),
            description: row.querySelector('.wf-branch-description').value,
            position: i + 1,
        }))
        .filter(b => b.key);
}

async function runWorkflowNow(id) {
    const res = await api(`/workflows/${id}/run`, {method: 'POST'});
    if (!res.ok) { toast((await res.json()).detail || 'Run failed to start', 'error'); return; }
    toast('Run started');
    loadWorkflowRuns();
}
```

The two row builders, which the collectors above read back:

```javascript
function addWorkflowBranchRow(branch) {
    branch = branch || {key: '', description: ''};
    const row = document.createElement('div');
    row.className = 'wf-branch-row';
    row.style.cssText = 'display:flex;gap:6px;margin-bottom:4px';
    row.innerHTML = `
        <input type="text" class="wf-branch-key" placeholder="abap" style="flex:0 0 140px"
               value="${escapeHtml(branch.key || '')}">
        <input type="text" class="wf-branch-description" style="flex:1"
               placeholder="When should an item take this branch?"
               value="${escapeHtml(branch.description || '')}">
        <button type="button" class="secondary small" onclick="this.parentNode.remove()">Remove</button>
    `;
    document.getElementById('workflow-branches').appendChild(row);
}

function addWorkflowStepRow(step) {
    step = step || {branch_key: null, agent_name: '', instructions: '',
                    fan_out: false, step_timeout_seconds: 600};
    // Branch options come from the rows currently in the branch editor, so a
    // branch added moments ago is selectable without saving first.
    const branchKeys = Array.from(
        document.querySelectorAll('#workflow-branches .wf-branch-key')
    ).map(i => i.value.trim()).filter(Boolean);
    const row = document.createElement('div');
    row.className = 'wf-step-row';
    row.style.cssText = 'display:flex;gap:6px;margin-bottom:4px;align-items:flex-start';
    row.innerHTML = `
        <select class="wf-step-branch" style="flex:0 0 130px">
            <option value="">Main line</option>
            ${branchKeys.map(k =>
                `<option value="${escapeHtml(k)}" ${k === step.branch_key ? 'selected' : ''}>${escapeHtml(k)}</option>`
            ).join('')}
        </select>
        <select class="wf-step-agent" style="flex:0 0 170px">
            ${allAgents.map(a =>
                `<option value="${escapeHtml(a.name)}" ${a.name === step.agent_name ? 'selected' : ''}>${escapeHtml(a.name)}</option>`
            ).join('')}
        </select>
        <textarea class="wf-step-instructions" rows="2" style="flex:1"
                  placeholder="What should this agent do with what it was handed?">${escapeHtml(step.instructions || '')}</textarea>
        <label style="font-weight:normal;white-space:nowrap">
            <input type="checkbox" class="wf-step-fanout" ${step.fan_out ? 'checked' : ''}> fan-out
        </label>
        <input type="number" class="wf-step-timeout" style="flex:0 0 80px"
               value="${step.step_timeout_seconds || 600}" min="10">
        <button type="button" class="secondary small" onclick="this.parentNode.remove()">Remove</button>
    `;
    document.getElementById('workflow-steps').appendChild(row);
}
```

The agent dropdown is populated from `allAgents` (`templates/admin.html:405`),
the same array the agent table renders from.

`editWorkflow(id)` fills the editor by calling `addWorkflowBranchRow(b)` for
each branch and `addWorkflowStepRow(s)` for each step, **branches first** — the
step rows read the branch editor to build their dropdowns.

Call `loadWorkflows()` and `loadWorkflowRuns()` from the page's existing
initial-load function, alongside `loadAgents()` and `loadSkills()`.

- [ ] **Step 10: Run the UI test to verify it passes**

Run: `./.venv/Scripts/python.exe tests/test_admin_ui.py`
Expected: PASS, `0 failed`. This suite runs `node --check` on the inline script,
so a JS syntax error fails here.

- [ ] **Step 11: Run the whole suite**

Run:
```bash
for t in tests/test_*.py; do echo "== $t"; ./.venv/Scripts/python.exe "$t" || echo "FAILED: $t"; done
npm test
```
Expected: every suite reports `0 failed`.

- [ ] **Step 12: Update the project docs**

In `CLAUDE.md`, add to **Key files**:

```
- `agents/workflow_runner.py` — runs a workflow: the declared main line, a
  fan-out step returning `WorkItem`s, and per-item branches the fan-out step
  selects. Mirrors `job_runner.py` (task set, start lock, never-raising
  `_finalize`, shutdown cancel). Steps hand plain text to each other; the join
  step sees one `## From` block per branch taken. See
  `docs/superpowers/specs/2026-08-31-agent-workflows-design.md`
```

and extend the `agents/db.py` bullet with the six new tables. In the
**Runtime flow** section, note that `POST /api/workflows/{slug}/run` is the
second scheduler entry point alongside `/api/agents/{slug}/run`.

- [ ] **Step 13: Commit**

```bash
git add templates/admin.html tests/test_admin_ui.py CLAUDE.md
git commit -m "feat: admin UI for workflows and workflow runs"
```

---

## Verification

The engine is complete when all of the following hold:

1. Every suite in `tests/` reports `0 failed`, and `npm test` passes.
2. In `/admin`, a workflow can be created with a fan-out reader, two branches,
   and a join drafter; **Run now** produces a run whose detail view shows the
   run → item → branch → step tree, with each item's selected branches visible.
3. An item selecting one branch leaves the other branch's agents uninvoked —
   confirmed by the absent step runs, not only by the log.
4. Re-running the same workflow immediately reports every item `skipped` and
   invokes no downstream agent.
5. `POST /api/workflows/{slug}/run` returns 202 within the scheduler's 15s
   synchronous budget, and a second call while the first runs returns 409.
6. Pressing **Reload** in `/admin` during a run does not kill it.
7. An existing deployment upgrades cleanly: `create_all` adds the six new
   tables and nothing about agents or job runs changes.
