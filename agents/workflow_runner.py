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
from agents.registry import _MAX_DELEGATION_DEPTH, registry

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

    # min_length: an empty id is worse than a missing one. item_succeeded_before
    # matches on it, so once one run records an item keyed "", every later item
    # keyed "" is skipped forever while skip_seen_items is on.
    id: str = Field(
        min_length=1,
        description="A stable identifier for this item, e.g. a message id.",
    )
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


class _PreflightError(_WorkflowError):
    """A preflight refusal, tagged with the step agent it should be blamed on.

    Preflight checks agents, not steps, and runs before anything executes — so
    without this the run records a message and no step run at all, and a
    reader (the run detail flow diagram, most visibly) cannot see where the run
    stopped. `step_agent` is the agent some step actually names: for a peer
    that fails its credential check, that is the agent which would have
    consulted it, since the peer has no step of its own.
    """

    def __init__(self, message: str, *, step_agent: str | None = None):
        super().__init__(message)
        self.step_agent = step_agent


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
    # (principal, agent name) -> row: everything whose credentials have to hold
    # for this run. The pair, not the agent alone, because a peer consulted from
    # a step runs inside *that step's* bound identity — run_as wraps the whole
    # step, delegation included — so the same peer reached from two steps with
    # different principals is two different questions.
    to_check: dict[tuple[str, str], object] = {}
    roots: dict[tuple[str, str], str] = {}
    async with SessionLocal() as session:
        for name in names:
            row = await get_agent_by_name(session, name)
            if row is None or not row.enabled:
                raise _PreflightError(
                    f"Agent {name!r} does not exist or is disabled. Enable it "
                    "(or point this step at a different agent) in /admin and "
                    "reload.",
                    step_agent=name,
                )
            if registry.build.specialists.get(name) is None:
                raise _PreflightError(
                    f"Agent {name!r} is not built (disabled, or no usable MCP "
                    "servers). Fix it in /admin and reload.",
                    step_agent=name,
                )
            principal = effective_principal(row, workflow)
            if not principal:
                raise _PreflightError(
                    f"No run-as principal is configured for agent {name!r}, and "
                    f"workflow {workflow.name!r} sets no fallback. A scheduled "
                    "run has no interactive user to borrow an identity from; "
                    "set one in /admin.",
                    step_agent=name,
                )
            resolved[name] = row
            to_check[(principal, name)] = row
            # Every entry remembers which step-level agent put it there, so a
            # failure deep in the peer graph can still be blamed on a real step.
            roots[(principal, name)] = name

        # Peers are reachable from a step's agent, and a peer with no usable
        # credential does NOT fail the delegation: registry._delegate returns
        # the sign-in prompt as its *answer* when there is no interactive sink
        # (correct for A2A), the parent model reads it as content, and the step
        # is recorded `success`. With skip_seen_items on, that wrong outcome is
        # then permanent. So the peer graph is checked here instead.
        #
        # Bounded by the same _MAX_DELEGATION_DEPTH that _delegate enforces at
        # run time, and cycle-guarded via to_check: mutual peers (A lists B, B
        # lists A) are a legitimate configuration, not an error.
        queue = [(name, effective_principal(resolved[name], workflow), 0)
                 for name in names]
        while queue:
            name, principal, depth = queue.pop(0)
            if depth >= _MAX_DELEGATION_DEPTH:
                continue
            for peer in to_check[(principal, name)].peers:
                key = (principal, peer)
                if peer == name or key in to_check:
                    continue
                peer_row = await get_agent_by_name(session, peer)
                if (peer_row is None or not peer_row.enabled
                        or registry.build.specialists.get(peer) is None):
                    # The registry skips an unresolvable peer with a warning
                    # rather than wiring it, so it is not reachable at all —
                    # nothing to check, and nothing to fail the run over.
                    continue
                to_check[key] = peer_row
                roots[key] = roots[(principal, name)]
                queue.append((peer, principal, depth + 1))

    for (principal, name), row in to_check.items():
        if not await job_runner._has_usable_credentials(row, principal):
            via = "" if name in resolved else (
                " That agent is reached as a peer from one of this workflow's "
                "steps."
            )
            raise _PreflightError(
                f"The service account {principal!r} has no usable credential "
                f"for agent {name!r}'s MCP servers. It must be re-authorized "
                f"interactively before scheduled runs can work.{via}",
                step_agent=roots.get((principal, name), name),
            )
    return resolved


def _execution_order(steps, branches) -> list:
    """Steps in the order a run reaches them.

    Not simply "main line first": the main-line steps after the fan-out are the
    join, and they run once per item *after* that item's branches. Sorting them
    with the rest of the main line would blame a join step for a block that a
    branch step would have hit first.
    """
    branch_position = {b.key: b.position for b in branches}
    main_line = sorted(
        [s for s in steps if s.branch_key is None], key=lambda s: s.position
    )
    fan_index = next((i for i, s in enumerate(main_line) if s.fan_out), None)
    if fan_index is None:
        return main_line + sorted(
            [s for s in steps if s.branch_key is not None],
            key=lambda s: (branch_position.get(s.branch_key, 10**6), s.position),
        )
    branch_steps = sorted(
        [s for s in steps if s.branch_key is not None],
        key=lambda s: (branch_position.get(s.branch_key, 10**6), s.position),
    )
    return main_line[:fan_index + 1] + branch_steps + main_line[fan_index + 1:]


async def _record_blocked_step(run_id: str, steps, branches, error) -> None:
    """Mark the step a preflight refusal stopped the run at.

    Preflight fails before anything executes, so without this the run carries a
    message and not one step run — and the run detail's flow diagram has no way
    to show where it stopped. Only the *first* step that would have used the
    offending agent is marked: the ones after it were never reached, and
    colouring them too would claim failures that never happened.

    Best-effort, like _finalize: this is reporting detail, and losing it must
    not stop the run from being recorded as failed.
    """
    agent_name = getattr(error, "step_agent", None)
    if not agent_name:
        return
    blocked = next(
        (s for s in _execution_order(steps, branches) if s.agent_name == agent_name),
        None,
    )
    if blocked is None:
        # The agent is reachable only as a peer of a peer, with no step of its
        # own to blame. The run-level error still explains it.
        return
    try:
        async with SessionLocal() as session:
            step_run = await create_step_run(
                session, run_id=run_id, item_run_id=None,
                branch_key=blocked.branch_key, position=blocked.position,
                agent_name=blocked.agent_name,
            )
        async with SessionLocal() as session:
            await finish_step_run(
                session, step_run.id, status="failed", error=str(error)
            )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Could not record the blocked step for workflow run %s", run_id
        )


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
    # Accumulated by _run_items as items finish, and shared by reference so the
    # timeout and cancellation handlers below — which run after _run_items has
    # been torn down — can still report what the run actually did. Without it an
    # interrupted run records 0/0/0/0 on a run that demonstrably processed items.
    # Keys are exactly finish_workflow_run's allow-list; it raises on any other.
    counts = {"items_total": 0, "items_succeeded": 0,
              "items_failed": 0, "items_skipped": 0}
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
            await _record_blocked_step(run_id, steps, branches, e)
            await _finalize(run_id, status="failed", error=str(e))
            return

        await asyncio.wait_for(
            _run_main_line(run_id, workflow, branches, steps, agent_rows, counts),
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
        await _finalize(run_id, status="failed", error=f"Run exceeded {budget}.",
                        counts=counts)
    except asyncio.CancelledError:
        await _finalize(run_id, status="interrupted",
                        error="Run was cancelled (app shutting down).",
                        counts=counts)
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Workflow run %s failed", run_id)
        await _finalize(run_id, status="failed", error=f"{type(e).__name__}: {e}")


async def _run_main_line(run_id, workflow, branches, steps, agent_rows,
                         counts) -> None:
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
                     agent_rows, fan_step, list(items or []), counts)


async def _run_items(run_id, workflow, branches, branch_steps, after,
                     agent_rows, fan_step, items, counts) -> None:
    """Run every discovered item through the post-fan-out steps."""
    counts["items_total"] = len(items)
    semaphore = asyncio.Semaphore(max(1, workflow.max_parallel_items))
    lock = asyncio.Lock()

    async def process(item: WorkItem) -> None:
        async with semaphore:
            # item_run is created inside this try, not before it: creating the
            # row (create_item_run, in either the skip or the normal path) can
            # itself raise, and that must fail only this item — same as a step
            # failure — never escape process() and abort sibling items via
            # asyncio.gather below, which has no return_exceptions=True.
            item_run = None
            # Set the moment the row reaches a terminal status, so a
            # cancellation landing between that write and the count increment
            # below cannot rewrite a finished item as `interrupted`.
            finished = False
            try:
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
                        finished = True
                        async with lock:
                            counts["items_skipped"] += 1
                        return

                async with SessionLocal() as session:
                    item_run = await create_item_run(
                        session, run_id=run_id, item_key=item.id,
                        title=item.title, branches=item.branches,
                    )
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
                # Inside the try, not after it: a DB failure while recording the
                # success would otherwise escape process(), and the gather below
                # (deliberately without return_exceptions) propagates it *without*
                # cancelling the sibling item tasks — the run would finalize
                # `failed`, releasing the overlap lock, while orphaned tasks kept
                # invoking agents and writing rows against a finished run.
                async with SessionLocal() as session:
                    await finish_item_run(session, item_run.id, status="success")
                finished = True
                async with lock:
                    counts["items_succeeded"] += 1
            except asyncio.CancelledError:
                # SIGTERM, or the run_timeout_seconds wait_for expiring. Without
                # this the in-flight item's row stays `running` forever — and on
                # a timeout nothing ever sweeps it, because the process lives on.
                if item_run is not None and not finished:
                    async with SessionLocal() as session:
                        await finish_item_run(
                            session, item_run.id, status="interrupted",
                            error="Cancelled (app shutting down).",
                        )
                raise
            except _WorkflowError as e:
                if item_run is not None:
                    async with SessionLocal() as session:
                        await finish_item_run(session, item_run.id, status="failed",
                                              error=str(e))
                async with lock:
                    counts["items_failed"] += 1
                return
            except Exception as e:  # noqa: BLE001
                # Anything else unexpected here — most plausibly create_item_run
                # or item_succeeded_before hitting a DB error — is a failure of
                # this item, not of the run. item_run may still be None (the
                # row itself never got created), so there is nothing to mark.
                logger.exception(
                    "Workflow run %s item %s failed outside step handling",
                    run_id, item.id,
                )
                if item_run is not None:
                    async with SessionLocal() as session:
                        await finish_item_run(session, item_run.id, status="failed",
                                              error=f"{type(e).__name__}: {e}")
                async with lock:
                    counts["items_failed"] += 1
                return

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
