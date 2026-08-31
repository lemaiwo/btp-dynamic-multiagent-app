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
from agents.db import SessionLocal, get_agent_by_slug, get_workflow_by_slug
from agents.job_runner import RunRefused, start_run
from agents.workflow_runner import RunRefused as WorkflowRunRefused
from agents.workflow_runner import start_workflow_run

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
