"""A small registry configuration with planted secrets, for the tests of
``scripts/copy_registry_config.py`` (SQLite, Postgres and SAP HANA use the
same rows). The three ``MARKER`` values stand for a client secret, a
registered client's secret and a user's token: they must arrive in the
target and appear nowhere else."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from agents.db import (
    AgentConfig,
    McpOAuthClient,
    McpOAuthState,
    McpOAuthToken,
    OrchestratorConfig,
    SkillConfig,
    Workflow,
    WorkflowBranch,
    WorkflowStep,
    create_job_run,
    create_odata_service,
    validate_odata_service,
)
from agents.ide.models import IdeConventions
from tests.odata_helpers import service_payload

MARKER_AGENT = "MARKER-agent-client-secret-7f3a"
MARKER_CLIENT = "MARKER-registered-client-secret-91bc"
MARKER_TOKEN = "MARKER-user-access-token-55de"
MARKERS = (MARKER_AGENT, MARKER_CLIENT, MARKER_TOKEN)
# A stamp with microseconds, not "now": it must survive to the microsecond.
STAMP = datetime(2026, 3, 4, 5, 6, 7, 123457, tzinfo=timezone.utc)
SERVER = "https://mcp.example.invalid/mcp"


async def seed_source(sessions: Any) -> None:
    """Fill a database with one of everything that is copied, plus history
    that is not."""
    async with sessions() as s:
        s.add(SkillConfig(name="triage-skill", description="d", content="c" * 6000))
        s.add(AgentConfig(
            name="reader", description="reads", instructions="i" * 6000,
            mcp_url=SERVER, auth_mode="oauth2", enabled=1, api_slug="reader",
            oauth_json=json.dumps({"client_id": "cid", "client_secret": MARKER_AGENT}),
            extra_servers_json=json.dumps([{"url": "builtin:odata"}]),
            skills_json=json.dumps(["triage-skill"]),
            created_at=STAMP, updated_at=STAMP))
        s.add(AgentConfig(name="seeded", description="from the source",
                          instructions="source text", mcp_url=SERVER, enabled=1))
        s.add(OrchestratorConfig(id=1, instructions="source orchestrator"))
        workflow = Workflow(name="mail-triage", description="triage", enabled=1,
                            api_slug="mail-triage")
        s.add(workflow)
        await s.flush()
        s.add(WorkflowBranch(workflow_id=workflow.id, key="abap", description="ABAP",
                             position=1))
        for branch, position, agent in ((None, 1, "reader"), (None, 2, "seeded"),
                                        ("abap", 1, "reader")):
            s.add(WorkflowStep(workflow_id=workflow.id, branch_key=branch,
                               position=position, agent_name=agent, instructions="do"))
        s.add(IdeConventions(target="DEV", label="Development", non_production=True))
        s.add(McpOAuthClient(
            server_key=SERVER, authorize_url="https://idp.example.invalid/authorize",
            token_url="https://idp.example.invalid/token", client_id="registered",
            client_secret=MARKER_CLIENT, redirect_uri="https://app.example.invalid/cb"))
        s.add(McpOAuthToken(user_id="alice@example.com", server_key=SERVER,
                            access_token=MARKER_TOKEN, refresh_token="r",
                            expires_at=STAMP))
        # History and flow state: never copied.
        s.add(McpOAuthState(state="st", user_id="alice@example.com", server_key=SERVER,
                            code_verifier="v", redirect_uri="https://x.invalid",
                            expires_at=STAMP))
        await s.commit()
        await create_odata_service(s, validate_odata_service(service_payload(name="stock")))
        agent = (await s.execute(
            select(AgentConfig).where(AgentConfig.name == "reader"))).scalar_one()
        await create_job_run(s, agent=agent, trigger="api")


async def seed_target(sessions: Any) -> None:
    """What a freshly started app holds: its own orchestrator row, a seeded
    agent of the same name as one in the source, and rows of its own that
    shift every integer id."""
    async with sessions() as s:
        s.add(OrchestratorConfig(id=1, instructions="target default"))
        for n in range(3):
            s.add(AgentConfig(name=f"local-{n}", description="local", instructions="i",
                              mcp_url=SERVER, enabled=1))
            s.add(Workflow(name=f"local-wf-{n}", description="local", enabled=1))
        s.add(AgentConfig(name="seeded", description="seeded by the app",
                          instructions="target text", mcp_url=SERVER, enabled=1))
        await s.flush()
        local = (await s.execute(Workflow.__table__.select())).first()
        s.add(WorkflowStep(workflow_id=local.id, branch_key=None, position=1,
                           agent_name="local-0", instructions="local"))
        await s.commit()
