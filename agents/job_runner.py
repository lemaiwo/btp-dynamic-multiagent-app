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
    sweep_stale_runs,
)
from agents.db import DEFAULT_RUN_PROMPT, get_agent_by_name
from agents.registry import _MAX_DELEGATION_DEPTH, _make_progress_handler, registry
from agents.reports import RunReport
from agents.run_activity import final_activity, recording
from agents.shared import run_usage_limits

logger = logging.getLogger(__name__)

# Background tasks are kept referenced; asyncio only holds weak references and
# would otherwise garbage-collect a run mid-flight.
_tasks: set[asyncio.Task] = set()

# Serializes the check-and-create in start_run per agent, so two concurrent
# triggers (a double-clicked "Run now", a scheduler retry) can't both observe
# "no active run" and both start one, double-hitting the target system. Same
# pattern as _refresh_locks in agents/oauth2.py. This only closes the
# in-process race — the app runs a single CF instance, so that's the
# realistic one; a cross-instance race (multiple app instances) would need a
# DB-level constraint and is deliberately deferred to a later increment.
_start_locks: dict[int, asyncio.Lock] = {}

# Seconds to wait before the 2nd and 3rd attempt to record a run's outcome.
# One DB hiccup at the end of a run must not leave the row `running` (it is
# the overlap lock); three attempts within a few seconds ride out a dropped
# connection or a pool momentarily exhausted. Patched to zeros in tests.
FINALIZE_RETRY_DELAYS: tuple[float, ...] = (0.5, 1.0)


def _lock_for(agent_id: int) -> asyncio.Lock:
    lock = _start_locks.get(agent_id)
    if lock is None:
        lock = asyncio.Lock()
        _start_locks[agent_id] = lock
    return lock


class RunRefused(Exception):
    """The run could not be started (already running, or not API-exposed)."""


async def cancel_all_runs() -> None:
    """Cancel every in-flight run and wait for it to finalize.

    Called from the lifespan shutdown. Without this, SIGTERM tears the event
    loop down under the running tasks, the CancelledError -> `interrupted`
    path in execute_run is never reliably reached, and the rows stay
    `running` until the next startup sweep clears them.
    """
    tasks = [t for t in _tasks if not t.done()]
    if not tasks:
        return
    logger.info("Cancelling %d in-flight job run(s) for shutdown", len(tasks))
    for task in tasks:
        task.cancel()
    # return_exceptions: each task re-raises CancelledError after recording
    # itself as interrupted, and that must not abort the shutdown sequence.
    await asyncio.gather(*tasks, return_exceptions=True)


async def _has_usable_credentials(
    agent: AgentConfig, principal: str | None = None
) -> bool:
    """True when every oauth2 server this agent binds has a usable token for
    `principal` — by default the agent's own run-as principal.

    The identity is a parameter because it is not always the agent's own: a
    workflow step binds the workflow's fallback principal when the agent has
    none (Workflow.run_as_principal), and the whole step — delegated peers
    included — runs under that one identity. Reading agent.run_as_principal
    unconditionally would fail every such agent at preflight and send the
    operator to re-authorize an account that was never the one in question.
    Patched in tests."""
    from agents.db import AUTH_MODE_OAUTH2, AUTH_MODE_SESSION
    from agents.oauth2 import has_usable_token, normalize_mcp_url

    principal = (principal or agent.run_as_principal or "").strip() or None
    if not principal:
        return False
    for spec in agent.mcp_servers:
        # session (a browser cookie) needs the same "someone signed in and it
        # has not expired" check as oauth2 -- both are per-principal
        # credentials nobody but a human can renew.
        if spec.get("auth_mode") not in (AUTH_MODE_OAUTH2, AUTH_MODE_SESSION):
            continue
        if not await has_usable_token(principal, normalize_mcp_url(str(spec["url"]))):
            return False
    return True


class CredentialBlocker:
    """The first agent in a run's reachable graph with no usable credential.

    ``root`` is the agent the run (or the workflow step) actually names; when
    ``via_peer`` is set, ``agent_name`` is a peer reached from it and has no
    step or run of its own to be blamed on.
    """

    __slots__ = ("principal", "agent_name", "root", "via_peer")

    def __init__(self, principal: str, agent_name: str, root: str, via_peer: bool):
        self.principal = principal
        self.agent_name = agent_name
        self.root = root
        self.via_peer = via_peer

    def message(self, *, scope: str = "the agent this run names") -> str:
        via = f" That agent is reached as a peer from {scope}." if self.via_peer else ""
        return (
            f"The service account {self.principal!r} has no usable credential "
            f"for agent {self.agent_name!r}'s MCP servers. It must be "
            f"re-authorized interactively before scheduled runs can work.{via}"
        )


async def find_credential_blocker(
    session, roots: dict[tuple[str, str], AgentConfig]
) -> CredentialBlocker | None:
    """Walk the peer graph from ``roots`` and check every credential on it.

    ``roots`` maps (principal, agent name) to the agent row; a run binds one
    identity per root and a delegated peer runs inside it, so the same peer
    reached under two principals is two different checks.

    Why peers matter: a peer with no usable credential does NOT fail the
    delegation. registry._delegate returns the sign-in prompt as its *answer*
    when there is no interactive sink (correct for A2A), the parent model
    reads it as content, and the run is recorded `success`. So the graph is
    checked here, before any model call, by both the job and workflow runners.

    Bounded by the same _MAX_DELEGATION_DEPTH that _delegate enforces at run
    time, and cycle-guarded: mutual peers (A lists B, B lists A) are a
    legitimate configuration, not an error. An unresolvable peer (missing,
    disabled, or not built) is skipped: the registry never wires it, so it is
    not reachable and there is nothing to check.
    """
    to_check: dict[tuple[str, str], AgentConfig] = dict(roots)
    blame: dict[tuple[str, str], str] = {key: key[1] for key in roots}
    queue = [(key, 0) for key in roots]
    while queue:
        (principal, name), depth = queue.pop(0)
        if depth >= _MAX_DELEGATION_DEPTH:
            continue
        for peer in to_check[(principal, name)].peers:
            key = (principal, peer)
            if peer == name or key in to_check:
                continue
            peer_row = await get_agent_by_name(session, peer)
            if (peer_row is None or not peer_row.enabled
                    or registry.build.specialists.get(peer) is None):
                continue
            to_check[key] = peer_row
            blame[key] = blame[(principal, name)]
            queue.append((key, depth + 1))

    for (principal, name), row in to_check.items():
        if not await _has_usable_credentials(row, principal):
            return CredentialBlocker(
                principal, name, blame[(principal, name)],
                via_peer=(principal, name) not in roots,
            )
    return None


# Phrases registry._delegate returns *as the tool answer* when a specialist
# needs sign-in and there is no interactive sink to wait on. In a scheduled
# run nobody can click the link, so a report built on one of these is a
# failure, not a result. Matched case-insensitively against the report.
SIGNIN_PROMPT_MARKERS = (
    "needs you to sign in",
    "needs sign-in",
    "could not complete sign-in",
    "/oauth/login?agent=",
)


def signin_prompt_in(report: RunReport) -> str | None:
    """The first sign-in marker found in a report, or None."""
    text = f"{report.summary}\n{report.body_md}".lower()
    for marker in SIGNIN_PROMPT_MARKERS:
        if marker.lower() in text:
            return marker
    return None


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
    # Hold the per-agent lock across the check-and-create so two concurrent
    # callers can't both see "no active run" and both start one.
    async with _lock_for(agent.id):
        await _sweep_stale()
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


async def _sweep_stale() -> None:
    """Close `running` rows older than their timeout before an overlap check.

    Without this the age-based sweep had no caller: a row left `running` by a
    finalize that never landed answered every later trigger 409 until the
    next app restart. Sweeping here hands that row's lock back to the next
    trigger instead. Never blocks the start: a failure is logged and the
    overlap check runs on whatever is stored.
    """
    try:
        async with SessionLocal() as session:
            swept = await sweep_stale_runs(session)
        if swept:
            logger.warning("Marked %d stale job run(s) interrupted", swept)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Stale job run sweep failed (%s); starting anyway",
                       type(exc).__name__)


async def _finalize(run_id: str, **kwargs) -> None:
    """Best-effort finish_job_run, retried: never raises (but cancellation).

    execute_run runs detached with nobody awaiting it, so if recording the
    outcome itself fails (DB hiccup, pool exhaustion, engine tearing down at
    shutdown), the row would otherwise stay 'running' — which wedges the
    overlap lock — AND the exception would surface only as an "exception was
    never retrieved" warning at GC time. So the write is tried
    ``len(FINALIZE_RETRY_DELAYS) + 1`` times (it is idempotent: one commit
    that sets the final columns), then logged and swallowed; the stale sweep
    at the next start or restart closes what is still left. A cancel during
    the backoff (shutdown) propagates and ends the retries.
    """
    if "activity" not in kwargs:
        try:
            kwargs["activity"] = final_activity(run_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Run %s: activity not stored (%s)",
                           run_id, type(exc).__name__)
            kwargs["activity"] = None
    delays = tuple(FINALIZE_RETRY_DELAYS)
    for attempt in range(len(delays) + 1):
        if attempt:
            await asyncio.sleep(delays[attempt - 1])
        try:
            async with SessionLocal() as session:
                await finish_job_run(session, run_id, **kwargs)
            return
        except Exception as exc:  # noqa: BLE001
            if attempt < len(delays):
                logger.warning(
                    "Finalizing run %s failed (%s), attempt %d; retrying",
                    run_id, type(exc).__name__, attempt + 1,
                )
                continue
            logger.exception(
                "Failed to finalize run %s (status=%s) after %d attempts; its "
                "row may be stuck 'running' until the next stale-run sweep.",
                run_id, kwargs.get("status"), attempt + 1,
            )


async def execute_run(run_id: str, agent_id: int) -> None:
    """Run the agent, recording what it does as it goes (agents.run_activity),
    and record the outcome. See _execute_run."""
    with recording(run_id):
        await _execute_run(run_id, agent_id)


async def _execute_run(run_id: str, agent_id: int) -> None:
    """Run the agent and record the outcome.

    Never raises — the row this writes to IS the overlap lock (see
    active_job_run), so any escaping exception would leave it stuck
    'running' forever. The one exception is asyncio.CancelledError: it is
    recorded as 'interrupted' and then re-raised so app shutdown still
    unwinds normally.
    """
    agent: AgentConfig | None = None
    try:
        async with SessionLocal() as session:
            agent = await session.get(AgentConfig, agent_id)
        if agent is None:
            await _finalize(run_id, status="failed", error="Agent no longer exists.")
            return

        # Distinguish "never configured" from "configured but stale": both
        # stop the run, but they need opposite fixes, and pointing an
        # operator at re-authorization when no service account exists yet is
        # a dead end.
        if not agent.run_as_principal:
            await _finalize(
                run_id, status="failed",
                error=(
                    "No run-as principal is configured for this agent. Set "
                    "'Run as (technical user)' in the admin UI to a service "
                    "account that has authorized this agent's MCP servers; a "
                    "scheduled run has no interactive user to borrow an "
                    "identity from."
                ),
            )
            return

        # The agent's own servers AND every peer it can delegate to: a peer
        # whose token was revoked answers with a sign-in prompt the model then
        # writes into the report, and the run would be recorded `success`.
        principal = agent.run_as_principal.strip()
        async with SessionLocal() as session:
            blocker = await find_credential_blocker(
                session, {(principal, agent.name): agent}
            )
        if blocker is not None:
            await _finalize(run_id, status="failed", error=blocker.message())
            return

        specialist = registry.build.specialists.get(agent.name)
        if specialist is None:
            await _finalize(
                run_id, status="failed",
                error=f"Agent {agent.name!r} is not built (disabled, or no usable MCP servers).",
            )
            return

        async with run_as(agent.run_as_principal):
            result = await asyncio.wait_for(
                specialist.run(
                    agent.run_prompt or DEFAULT_RUN_PROMPT,
                    output_type=RunReport,
                    usage_limits=run_usage_limits(),
                    # The agent's own tool calls; delegations and deep
                    # sub-agents report theirs through the same sink.
                    event_stream_handler=_make_progress_handler(agent.name),
                ),
                timeout=agent.run_timeout_seconds,
            )
        report: RunReport = result.output
        marker = signin_prompt_in(report)
        if marker is not None:
            # A token died mid-run (or a peer needed sign-in that preflight
            # could not see). The report is kept so an operator can read what
            # happened, but the run is a failure: nobody could click the link.
            await _finalize(
                run_id,
                status="failed",
                summary=report.summary,
                report=report.model_dump(mode="json"),
                error=(
                    "The run ended with a sign-in prompt instead of a result "
                    f"(the report contains {marker!r}). The service account "
                    f"{agent.run_as_principal!r} must re-authorize the agent "
                    "(or one of its peers) interactively before scheduled runs "
                    "can work."
                ),
            )
            return
        await _finalize(
            run_id,
            status="success",
            summary=report.summary,
            report=report.model_dump(mode="json"),
        )
    except asyncio.TimeoutError:
        # `agent` can still be None here (e.g. the initial session.get itself
        # timed out on a pool acquire — asyncpg raises this same
        # TimeoutError), so don't dereference it unguarded: that would raise
        # AttributeError *inside* this handler, escape execute_run, and leave
        # the row stuck 'running' — the exact bug this whole except chain
        # exists to prevent.
        timeout_desc = (
            f"its {agent.run_timeout_seconds}s timeout" if agent is not None
            else "its timeout"
        )
        await _finalize(
            run_id, status="failed",
            error=f"Run exceeded {timeout_desc}.",
        )
    except asyncio.CancelledError:
        await _finalize(
            run_id, status="interrupted",
            error="Run was cancelled (app shutting down).",
        )
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception(
            "Run %s of agent %s failed",
            run_id, agent.name if agent is not None else agent_id,
        )
        await _finalize(run_id, status="failed", error=f"{type(e).__name__}: {e}")
