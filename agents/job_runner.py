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

# Serializes the check-and-create in start_run per agent, so two concurrent
# triggers (a double-clicked "Run now", a scheduler retry) can't both observe
# "no active run" and both start one, double-hitting the target system. Same
# pattern as _refresh_locks in agents/oauth2.py. This only closes the
# in-process race — the app runs a single CF instance, so that's the
# realistic one; a cross-instance race (multiple app instances) would need a
# DB-level constraint and is deliberately deferred to a later increment.
_start_locks: dict[int, asyncio.Lock] = {}


def _lock_for(agent_id: int) -> asyncio.Lock:
    lock = _start_locks.get(agent_id)
    if lock is None:
        lock = asyncio.Lock()
        _start_locks[agent_id] = lock
    return lock


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
    # Hold the per-agent lock across the check-and-create so two concurrent
    # callers can't both see "no active run" and both start one.
    async with _lock_for(agent.id):
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


async def _finalize(run_id: str, **kwargs) -> None:
    """Best-effort finish_job_run: never raises.

    execute_run runs detached with nobody awaiting it, so if recording the
    outcome itself fails (DB hiccup, pool exhaustion, engine tearing down at
    shutdown), the row would otherwise stay 'running' forever — which wedges
    the overlap lock — AND the exception would surface only as an "exception
    was never retrieved" warning at GC time. Log and swallow instead.
    """
    try:
        async with SessionLocal() as session:
            await finish_job_run(session, run_id, **kwargs)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to finalize run %s (status=%s); its row may be stuck "
            "'running' until the next stale-run sweep.",
            run_id, kwargs.get("status"),
        )


async def execute_run(run_id: str, agent_id: int) -> None:
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

        if not await _has_usable_credentials(agent):
            await _finalize(
                run_id, status="failed",
                error=(
                    f"The service account {agent.run_as_principal!r} has no "
                    "usable credential for this agent's MCP servers. It must "
                    "be re-authorized interactively before scheduled runs can "
                    "work."
                ),
            )
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
                ),
                timeout=agent.run_timeout_seconds,
            )
        report: RunReport = result.output
        missing = missing_sections(report, agent.expected_sections)
        await _finalize(
            run_id,
            status="degraded" if missing else "success",
            summary=report.summary,
            report=report.model_dump(mode="json"),
            missing=missing,
            error=(
                "Report did not check: " + ", ".join(missing) if missing else None
            ),
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
