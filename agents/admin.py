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
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from agents.auth import require_admin
from agents.chat_app import dynamic_chat_app
from agents.db import (
    AUTH_MODE_JWT,
    AUTH_MODE_NONE,
    AUTH_MODE_OAUTH2,
    VALID_AUTH_MODES,
    SessionLocal,
    delete_agent,
    get_active_model_name,
    get_agent,
    get_orchestrator_instructions,
    list_agents,
    prepare_servers,
    set_active_model_name,
    set_orchestrator_instructions,
    upsert_agent,
)
from agents.registry import registry
from agents.shared import available_models, default_model_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class OAuthClientPayload(BaseModel):
    """OAuth2 client config for an ``auth_mode="oauth2"`` server.

    Two shapes:
    - ``dcr=True`` — auto-discover the authorization server and register a
      client dynamically; no manual credentials required.
    - manual — provide either ``uaa_url`` (XSUAA — authorize/token endpoints
      derived) or explicit ``authorize_url`` + ``token_url``, plus
      ``client_id`` / ``client_secret``. ``client_secret`` may be left blank
      on edit to keep the stored value.
    """

    dcr: bool = False
    client_id: str = Field(default="", max_length=512)
    client_secret: str = Field(default="", max_length=2048)
    uaa_url: str = Field(default="", max_length=512)
    authorize_url: str = Field(default="", max_length=512)
    token_url: str = Field(default="", max_length=512)
    scope: str = Field(default="", max_length=512)
    # Read-only flag echoed back by the API; ignored on input.
    has_client_secret: bool = False

    def to_config(self) -> dict[str, Any]:
        if self.dcr:
            out: dict[str, Any] = {"dcr": True}
            if self.scope.strip():
                out["scope"] = self.scope.strip()
            return out
        fields = {
            "client_id": self.client_id.strip(),
            "client_secret": self.client_secret.strip(),
            "uaa_url": self.uaa_url.strip(),
            "authorize_url": self.authorize_url.strip(),
            "token_url": self.token_url.strip(),
            "scope": self.scope.strip(),
        }
        return {k: v for k, v in fields.items() if v}


class McpServerPayload(BaseModel):
    url: str = Field(min_length=1)
    auth_mode: str = Field(default=AUTH_MODE_JWT)
    oauth: OAuthClientPayload | None = None

    @field_validator("auth_mode")
    @classmethod
    def _validate_auth_mode(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in VALID_AUTH_MODES:
            raise ValueError(
                f"auth_mode must be one of {sorted(VALID_AUTH_MODES)}"
            )
        return v

    @model_validator(mode="after")
    def _validate_oauth(self) -> "McpServerPayload":
        if self.auth_mode == AUTH_MODE_OAUTH2:
            cfg = self.oauth.to_config() if self.oauth else {}
            if cfg.get("dcr"):
                return self  # auto-discovery: no manual credentials needed
            if not cfg.get("client_id"):
                raise ValueError(
                    "oauth2 server requires oauth.client_id (or enable oauth.dcr "
                    "to auto-discover and register)"
                )
            if not (cfg.get("uaa_url") or (cfg.get("authorize_url") and cfg.get("token_url"))):
                raise ValueError(
                    "oauth2 server requires oauth.uaa_url or both "
                    "oauth.authorize_url and oauth.token_url"
                )
            # client_secret may be blank here (preserved from storage on edit);
            # the DB layer enforces that a secret ultimately exists.
        elif self.oauth is not None and self.oauth.to_config():
            raise ValueError("oauth config is only valid when auth_mode=oauth2")
        return self

    @model_validator(mode="after")
    def _validate_url(self) -> "McpServerPayload":
        v = self.url.strip().rstrip("/")
        public = self.auth_mode == AUTH_MODE_NONE
        # Public servers may use http; authenticated servers must use https
        # so forwarded JWTs are not exposed on the wire.
        if public:
            if not (v.startswith("http://") or v.startswith("https://")):
                raise ValueError("url must be http:// or https://")
        else:
            if not v.startswith("https://"):
                raise ValueError("url must use https:// (set auth_mode=none for public servers)")
        try:
            HttpUrl(v)
        except Exception as e:
            raise ValueError(f"invalid URL: {e}") from e
        # Host allow-list applies to authenticated (JWT-forwarding) servers
        # only. Public servers are unrestricted by design.
        if not public:
            allowlist = os.environ.get("MCP_URL_ALLOWLIST", "").strip()
            if allowlist:
                allowed = [a.strip() for a in allowlist.split(",") if a.strip()]
                if not any(v.startswith(a.rstrip("/")) for a in allowed):
                    raise ValueError(
                        f"url is not in MCP_URL_ALLOWLIST ({allowlist})"
                    )
            else:
                host = v.split("/", 3)[2]
                if not (
                    host.endswith(".hana.ondemand.com")
                    or host.endswith(".cfapps.sap.hana.ondemand.com")
                ):
                    raise ValueError(
                        "url must be a BTP-hosted URL (*.hana.ondemand.com). "
                        "Set MCP_URL_ALLOWLIST to override, or set auth_mode=none "
                        "for public MCP servers."
                    )
        self.url = v
        return self


class AgentPayload(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_\- ]+$")
    description: str = Field(min_length=1, max_length=2000)
    instructions: str = Field(min_length=1)
    mcp_servers: list[McpServerPayload] = Field(default_factory=list)
    enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_single_url(cls, data: Any) -> Any:
        """Accept legacy {mcp_url, auth_mode} singletons by converting to
        a single-entry mcp_servers list.
        """
        if not isinstance(data, dict):
            return data
        if data.get("mcp_servers"):
            return data
        legacy_url = data.get("mcp_url")
        if legacy_url:
            data = dict(data)
            data["mcp_servers"] = [
                {
                    "url": legacy_url,
                    "auth_mode": data.get("auth_mode") or AUTH_MODE_JWT,
                }
            ]
        return data

    @model_validator(mode="after")
    def _require_server(self) -> "AgentPayload":
        if not self.mcp_servers:
            raise ValueError("at least one mcp_servers entry is required")
        # Reject duplicates within a single agent
        urls = [s.url for s in self.mcp_servers]
        if len(set(urls)) != len(urls):
            raise ValueError("mcp_servers contains duplicate urls")
        return self

    def to_servers_list(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for s in self.mcp_servers:
            entry: dict[str, Any] = {"url": s.url, "auth_mode": s.auth_mode}
            if s.auth_mode == AUTH_MODE_OAUTH2 and s.oauth is not None:
                entry["oauth"] = s.oauth.to_config()
            out.append(entry)
        return out


class OrchestratorPayload(BaseModel):
    instructions: str = Field(min_length=1)


class ModelPayload(BaseModel):
    model_name: str = Field(min_length=1, max_length=128)

    @field_validator("model_name")
    @classmethod
    def _validate(cls, v: str) -> str:
        v = v.strip()
        allowed = available_models()
        if allowed and v not in allowed:
            raise ValueError(f"model_name must be one of {allowed}")
        return v


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
        try:
            row = await upsert_agent(
                session,
                name=payload.name,
                description=payload.description,
                instructions=payload.instructions,
                mcp_servers=payload.to_servers_list(),
                enabled=payload.enabled,
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
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
        try:
            primary, extras, primary_oauth_json = prepare_servers(
                payload.to_servers_list(), row
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        row.name = payload.name
        row.description = payload.description
        row.instructions = payload.instructions
        row.mcp_url = primary["url"]
        row.auth_mode = primary["auth_mode"]
        row.extra_servers_json = json.dumps(extras) if extras else None
        row.oauth_json = primary_oauth_json
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
# Active LLM model
# ---------------------------------------------------------------------------
@router.get("/api/model", dependencies=[Depends(require_admin)])
async def api_get_model() -> dict[str, Any]:
    async with SessionLocal() as session:
        active = await get_active_model_name(session)
    return {
        "model_name": active or default_model_name(),
        "available": available_models(),
        "default": default_model_name(),
    }


@router.put("/api/model", dependencies=[Depends(require_admin)])
async def api_update_model(payload: ModelPayload) -> dict[str, Any]:
    async with SessionLocal() as session:
        await set_active_model_name(session, payload.model_name)
    build = await registry.reload()
    dynamic_chat_app.refresh()
    return {
        "model_name": payload.model_name,
        "agents": len(build.configs),
        "enabled": len(build.specialists),
    }


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
            try:
                await upsert_agent(
                    session,
                    name=agent.name,
                    description=agent.description,
                    instructions=agent.instructions,
                    mcp_servers=agent.to_servers_list(),
                    enabled=agent.enabled,
                )
            except ValueError as e:
                raise HTTPException(
                    status_code=422, detail=f"Agent '{agent.name}': {e}"
                ) from e
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
                mcp_servers=payload.to_servers_list(),
                enabled=payload.enabled,
            )
            count += 1
        logger.info("Seeded %d agents from %s", count, seed_path)
