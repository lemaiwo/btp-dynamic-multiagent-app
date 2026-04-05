"""Admin API + UI for dynamic agent configuration.

Endpoints (all require `<xsappname>.admin` XSUAA scope):

    GET    /admin                          — HTML admin UI
    GET    /admin/api/agents               — list agents
    POST   /admin/api/agents               — create or upsert an agent
    GET    /admin/api/agents/{id}          — fetch one agent
    PUT    /admin/api/agents/{id}          — update an agent
    DELETE /admin/api/agents/{id}          — delete an agent
    GET    /admin/api/orchestrator         — fetch orchestrator instructions
    PUT    /admin/api/orchestrator         — update orchestrator instructions
    POST   /admin/api/reload               — rebuild the orchestrator in-memory
    POST   /admin/api/restart              — reload + trigger CF app restart
    GET    /admin/api/export               — dump full config as JSON
    POST   /admin/api/import               — bulk upsert from JSON
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, HttpUrl, field_validator

from agents.auth import require_admin
from agents.chat_app import dynamic_chat_app
from agents.db import (
    SessionLocal,
    delete_agent,
    get_agent,
    get_orchestrator_instructions,
    list_agents,
    set_orchestrator_instructions,
    upsert_agent,
)
from agents.registry import registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class AgentPayload(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_\- ]+$")
    description: str = Field(min_length=1, max_length=2000)
    instructions: str = Field(min_length=1)
    mcp_url: str = Field(min_length=1)
    enabled: bool = True

    @field_validator("mcp_url")
    @classmethod
    def _validate_mcp_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        # Enforce HTTPS-only BTP MCP URLs
        if not v.startswith("https://"):
            raise ValueError("mcp_url must use https://")
        # Validate it parses as a URL
        try:
            HttpUrl(v)
        except Exception as e:
            raise ValueError(f"invalid URL: {e}") from e
        # Require BTP-hosted MCP (cfapps domain). The allow-list can be
        # tightened further via the MCP_URL_ALLOWLIST env var (comma-sep).
        allowlist = os.environ.get("MCP_URL_ALLOWLIST", "").strip()
        if allowlist:
            allowed = [a.strip() for a in allowlist.split(",") if a.strip()]
            if not any(v.startswith(a.rstrip("/")) for a in allowed):
                raise ValueError(
                    f"mcp_url is not in MCP_URL_ALLOWLIST ({allowlist})"
                )
        else:
            # Default: only allow BTP Cloud Foundry domains
            host = v.split("/", 3)[2]
            if not (host.endswith(".hana.ondemand.com") or host.endswith(".cfapps.sap.hana.ondemand.com")):
                raise ValueError(
                    "mcp_url must be a BTP-hosted URL (*.hana.ondemand.com). "
                    "Set MCP_URL_ALLOWLIST to override."
                )
        return v


class OrchestratorPayload(BaseModel):
    instructions: str = Field(min_length=1)


class ImportPayload(BaseModel):
    orchestrator_instructions: str | None = None
    agents: list[AgentPayload] = Field(default_factory=list)
    replace: bool = False  # if true, delete agents not in the import


# ---------------------------------------------------------------------------
# Router setup
# ---------------------------------------------------------------------------
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def admin_ui(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "admin.html")


# ---------------------------------------------------------------------------
# Agents CRUD
# ---------------------------------------------------------------------------
@router.get("/api/agents", dependencies=[Depends(require_admin)])
async def api_list_agents() -> list[dict[str, Any]]:
    async with SessionLocal() as session:
        rows = await list_agents(session)
        return [r.to_dict() for r in rows]


@router.post(
    "/api/agents",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def api_create_agent(payload: AgentPayload) -> dict[str, Any]:
    async with SessionLocal() as session:
        row = await upsert_agent(
            session,
            name=payload.name,
            description=payload.description,
            instructions=payload.instructions,
            mcp_url=payload.mcp_url,
            enabled=payload.enabled,
        )
        return row.to_dict()


@router.get("/api/agents/{agent_id}", dependencies=[Depends(require_admin)])
async def api_get_agent(agent_id: int) -> dict[str, Any]:
    async with SessionLocal() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        return row.to_dict()


@router.put("/api/agents/{agent_id}", dependencies=[Depends(require_admin)])
async def api_update_agent(agent_id: int, payload: AgentPayload) -> dict[str, Any]:
    async with SessionLocal() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        if row.name != payload.name:
            # Check uniqueness of new name
            from agents.db import get_agent_by_name

            clash = await get_agent_by_name(session, payload.name)
            if clash and clash.id != agent_id:
                raise HTTPException(
                    status_code=409, detail=f"Agent name '{payload.name}' already exists"
                )
        row.name = payload.name
        row.description = payload.description
        row.instructions = payload.instructions
        row.mcp_url = payload.mcp_url
        row.enabled = 1 if payload.enabled else 0
        await session.commit()
        await session.refresh(row)
        return row.to_dict()


@router.delete(
    "/api/agents/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
async def api_delete_agent(agent_id: int) -> None:
    async with SessionLocal() as session:
        ok = await delete_agent(session, agent_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Agent not found")


# ---------------------------------------------------------------------------
# Orchestrator instructions
# ---------------------------------------------------------------------------
@router.get("/api/orchestrator", dependencies=[Depends(require_admin)])
async def api_get_orchestrator() -> dict[str, str]:
    async with SessionLocal() as session:
        return {"instructions": await get_orchestrator_instructions(session)}


@router.put("/api/orchestrator", dependencies=[Depends(require_admin)])
async def api_update_orchestrator(payload: OrchestratorPayload) -> dict[str, str]:
    async with SessionLocal() as session:
        await set_orchestrator_instructions(session, payload.instructions)
        return {"instructions": payload.instructions}


# ---------------------------------------------------------------------------
# Reload & restart
# ---------------------------------------------------------------------------
@router.post("/api/reload", dependencies=[Depends(require_admin)])
async def api_reload() -> dict[str, Any]:
    """Rebuild the orchestrator from the database without restarting."""
    build = await registry.reload()
    dynamic_chat_app.refresh()
    return {
        "status": "reloaded",
        "agents": len(build.configs),
        "enabled": len(build.specialists),
    }


@router.post("/api/restart", dependencies=[Depends(require_admin)])
async def api_restart() -> JSONResponse:
    """Reload in-memory and trigger a Cloud Foundry app restart.

    The CF API restart is a best-effort operation — it requires the
    `CF_API_URL`, `CF_USERNAME`, and `CF_PASSWORD` env vars (or a bound
    user-provided service `cf-api`) and the configured user to have the
    SpaceDeveloper role on the app's space. If CF restart is not
    configured, the in-memory reload alone is sufficient for newly added
    agents to take effect.
    """
    build = await registry.reload()
    dynamic_chat_app.refresh()

    from agents.cf_api import restart_self

    cf_result = await restart_self()
    return JSONResponse(
        {
            "status": "reloaded",
            "agents": len(build.configs),
            "enabled": len(build.specialists),
            "cf_restart": cf_result,
        }
    )


# ---------------------------------------------------------------------------
# Export / import
# ---------------------------------------------------------------------------
@router.get("/api/export", dependencies=[Depends(require_admin)])
async def api_export() -> dict[str, Any]:
    async with SessionLocal() as session:
        rows = await list_agents(session)
        orch = await get_orchestrator_instructions(session)
        return {
            "version": 1,
            "orchestrator_instructions": orch,
            "agents": [r.to_export() for r in rows],
        }


@router.post("/api/import", dependencies=[Depends(require_admin)])
async def api_import(payload: ImportPayload = Body(...)) -> dict[str, Any]:
    async with SessionLocal() as session:
        if payload.orchestrator_instructions:
            await set_orchestrator_instructions(session, payload.orchestrator_instructions)

        imported_names = set()
        for agent in payload.agents:
            await upsert_agent(
                session,
                name=agent.name,
                description=agent.description,
                instructions=agent.instructions,
                mcp_url=agent.mcp_url,
                enabled=agent.enabled,
            )
            imported_names.add(agent.name)

        removed = 0
        if payload.replace:
            existing = await list_agents(session)
            for row in existing:
                if row.name not in imported_names:
                    await session.delete(row)
                    removed += 1
            await session.commit()

    return {
        "status": "imported",
        "imported": len(payload.agents),
        "removed": removed,
    }


# ---------------------------------------------------------------------------
# Seed helper (called on startup)
# ---------------------------------------------------------------------------
async def seed_from_file_if_empty(seed_path: Path) -> None:
    """Seed the DB from a JSON file if no agents exist yet."""
    async with SessionLocal() as session:
        existing = await list_agents(session)
        if existing:
            return
        if not seed_path.exists():
            logger.info("No seed file at %s; starting with empty registry", seed_path)
            return

        try:
            data = json.loads(seed_path.read_text())
        except Exception:
            logger.exception("Failed to read seed file %s", seed_path)
            return

        if "orchestrator_instructions" in data and data["orchestrator_instructions"]:
            await set_orchestrator_instructions(session, data["orchestrator_instructions"])

        count = 0
        for entry in data.get("agents", []):
            try:
                # Validate via pydantic model
                payload = AgentPayload.model_validate(entry)
            except Exception as e:
                logger.warning("Skipping invalid seed entry %r: %s", entry, e)
                continue
            await upsert_agent(
                session,
                name=payload.name,
                description=payload.description,
                instructions=payload.instructions,
                mcp_url=payload.mcp_url,
                enabled=payload.enabled,
            )
            count += 1
        logger.info("Seeded %d agents from %s", count, seed_path)
