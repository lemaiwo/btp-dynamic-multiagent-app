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

        specialist = registry.build.specialists.get(agent.name)
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
