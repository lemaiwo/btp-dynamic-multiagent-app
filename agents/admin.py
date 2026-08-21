"""Admin API + UI for dynamic agent configuration.

Endpoints (all require `<xsappname>.admin` XSUAA scope):

    GET    /admin                          — HTML admin UI
    GET    /admin/api/agents               — list agents
    POST   /admin/api/agents               — create or upsert an agent
    GET    /admin/api/agents/{id}          — fetch one agent
    PUT    /admin/api/agents/{id}          — update an agent
    DELETE /admin/api/agents/{id}          — delete an agent
    GET    /admin/api/skills               — list skills
    POST   /admin/api/skills               — create or upsert a skill
    GET    /admin/api/skills/{id}          — fetch one skill
    PUT    /admin/api/skills/{id}          — update a skill
    DELETE /admin/api/skills/{id}          — delete a skill (detaches it)
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

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from agents.auth import current_principal, require_admin
from agents.chat_app import dynamic_chat_app
from agents.db import (
    AUTH_MODE_JWT,
    AUTH_MODE_NONE,
    AUTH_MODE_OAUTH2,
    VALID_AUTH_MODES,
    SessionLocal,
    delete_agent,
    delete_skill,
    get_active_model_name,
    get_agent,
    get_agent_by_slug,
    get_orchestrator_instructions,
    get_skill,
    get_skill_by_name,
    list_agents,
    list_skills,
    normalize_skills_json,
    prepare_servers,
    rename_skill_references,
    set_active_model_name,
    set_orchestrator_instructions,
    upsert_agent,
    upsert_skill,
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


class SkillPayload(BaseModel):
    """A reusable skill agents can be equipped with.

    ``description`` tells the specialist *when* to use the skill (it goes
    into the system prompt); ``content`` is the full instruction body the
    specialist loads on demand via its ``load_skill`` tool.
    """

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_\- ]+$")
    description: str = Field(min_length=1, max_length=2000)
    content: str = Field(min_length=1)

    @field_validator("name", "description")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class AgentPayload(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_\- ]+$")
    description: str = Field(min_length=1, max_length=2000)
    instructions: str = Field(min_length=1)
    mcp_servers: list[McpServerPayload] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    enabled: bool = True
    expose_chat: bool = True
    expose_api: bool = False
    api_slug: str = Field(default="", max_length=64)
    run_as_principal: str = Field(default="", max_length=255)
    run_prompt: str = ""
    run_timeout_seconds: int = Field(default=1800, ge=60, le=86400)

    @field_validator("skills")
    @classmethod
    def _clean_skills(cls, v: list[str]) -> list[str]:
        cleaned: list[str] = []
        for s in v:
            s = str(s).strip()
            if s and s not in cleaned:
                cleaned.append(s)
        return cleaned

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
    skills: list[SkillPayload] = Field(default_factory=list)
    agents: list[AgentPayload] = Field(default_factory=list)
    replace: bool = False  # if true, delete agents/skills not in the import


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
                skills=payload.skills,
                enabled=payload.enabled,
                expose_chat=payload.expose_chat,
                expose_api=payload.expose_api,
                api_slug=payload.api_slug,
                run_as_principal=payload.run_as_principal,
                run_prompt=payload.run_prompt,
                run_timeout_seconds=payload.run_timeout_seconds,
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
            skills_json = await normalize_skills_json(session, payload.skills)

            slug = payload.api_slug.strip() or None
            if slug:
                clash = await get_agent_by_slug(session, slug)
                if clash is not None and clash.id != agent_id:
                    raise ValueError(
                        f"api_slug {slug!r} is already used by agent {clash.name!r}"
                    )
            if payload.expose_api and not slug:
                raise ValueError("expose_api requires an api_slug")
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        row.name = payload.name
        row.description = payload.description
        row.instructions = payload.instructions
        row.mcp_url = primary["url"]
        row.auth_mode = primary["auth_mode"]
        row.extra_servers_json = json.dumps(extras) if extras else None
        row.oauth_json = primary_oauth_json
        row.skills_json = skills_json
        row.enabled = 1 if payload.enabled else 0
        row.expose_chat = 1 if payload.expose_chat else 0
        row.expose_api = 1 if payload.expose_api else 0
        row.api_slug = slug
        row.run_as_principal = payload.run_as_principal.strip() or None
        row.run_prompt = payload.run_prompt.strip() or None
        row.run_timeout_seconds = payload.run_timeout_seconds
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
# Identity helpers for run-as configuration
# ---------------------------------------------------------------------------
@router.get("/api/whoami")
async def api_whoami(payload: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """The caller's own principal, so the admin UI can offer it for run-as.

    ``run_as_principal`` stores the opaque XSUAA principal (``user_uuid`` /
    ``sub``), which nobody can recall or type. Without the UI handing it over,
    the field is unusable: an admin naturally types an email address, which
    matches no row in the token store, and every run then fails claiming the
    service account needs re-authorization.
    """
    label = payload.get("email") or payload.get("user_name") or ""
    return {"principal": current_principal.get() or "", "label": str(label)}


@router.get("/api/agents/{agent_id}/credentials", dependencies=[Depends(require_admin)])
async def api_agent_credentials(
    agent_id: int, principal: str = Query(default="", max_length=255)
) -> list[dict[str, Any]]:
    """Per-MCP-server credential status for a principal, plus a sign-in link.

    Lets the agent form show whether the configured run-as identity actually
    holds a token for each oauth2 server *before* a run is triggered, instead
    of the mismatch surfacing hours later as a failed scheduled run.

    ``principal`` defaults to the agent's stored ``run_as_principal``.
    """
    from urllib.parse import quote

    from agents.oauth2 import has_usable_token, normalize_mcp_url

    async with SessionLocal() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        agent_name = row.name
        servers = row.mcp_servers
        stored_principal = row.run_as_principal

    who = (principal or "").strip() or (stored_principal or "")
    out: list[dict[str, Any]] = []
    for spec in servers:
        url = str(spec.get("url") or "")
        auth_mode = str(spec.get("auth_mode") or "")
        needs_token = auth_mode == AUTH_MODE_OAUTH2
        has_token = False
        login_url = ""
        if needs_token:
            server_key = normalize_mcp_url(url)
            login_url = (
                f"/oauth/login?agent={quote(agent_name)}"
                f"&server={quote(server_key, safe='')}"
            )
            if who:
                try:
                    has_token = await has_usable_token(who, server_key)
                except Exception:  # noqa: BLE001 — status display must not 500
                    logger.warning(
                        "Could not read token status for %s on %s",
                        who, server_key, exc_info=True,
                    )
        out.append({
            "url": url,
            "auth_mode": auth_mode,
            "needs_token": needs_token,
            "has_token": has_token,
            "login_url": login_url,
        })
    return out


# ---------------------------------------------------------------------------
# Runs (Run-now button + run listing)
# ---------------------------------------------------------------------------
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
async def api_list_runs(
    agent_id: int | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
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


# ---------------------------------------------------------------------------
# Skills CRUD
# ---------------------------------------------------------------------------
@router.get("/api/skills", dependencies=[Depends(require_admin)])
async def api_list_skills() -> list[dict[str, Any]]:
    async with SessionLocal() as session:
        rows = await list_skills(session)
        return [r.to_dict() for r in rows]


@router.post(
    "/api/skills",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def api_create_skill(payload: SkillPayload) -> dict[str, Any]:
    async with SessionLocal() as session:
        row = await upsert_skill(
            session,
            name=payload.name,
            description=payload.description,
            content=payload.content,
        )
        return row.to_dict()


@router.get("/api/skills/{skill_id}", dependencies=[Depends(require_admin)])
async def api_get_skill(skill_id: int) -> dict[str, Any]:
    async with SessionLocal() as session:
        row = await get_skill(session, skill_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Skill not found")
        return row.to_dict()


@router.put("/api/skills/{skill_id}", dependencies=[Depends(require_admin)])
async def api_update_skill(skill_id: int, payload: SkillPayload) -> dict[str, Any]:
    async with SessionLocal() as session:
        row = await get_skill(session, skill_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Skill not found")
        if row.name != payload.name:
            clash = await get_skill_by_name(session, payload.name)
            if clash and clash.id != skill_id:
                raise HTTPException(
                    status_code=409, detail=f"Skill name '{payload.name}' already exists"
                )
            # Keep agent references pointing at the renamed skill
            await rename_skill_references(session, row.name, payload.name)
        row.name = payload.name
        row.description = payload.description
        row.content = payload.content
        await session.commit()
        await session.refresh(row)
        return row.to_dict()


@router.delete(
    "/api/skills/{skill_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
async def api_delete_skill(skill_id: int) -> None:
    """Delete a skill and detach it from any agents that reference it."""
    async with SessionLocal() as session:
        ok = await delete_skill(session, skill_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Skill not found")


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
        skills = await list_skills(session)
        orch = await get_orchestrator_instructions(session)
        return {
            "version": 1,
            "orchestrator_instructions": orch,
            "skills": [s.to_export() for s in skills],
            "agents": [r.to_export() for r in rows],
        }


@router.post("/api/import", dependencies=[Depends(require_admin)])
async def api_import(payload: ImportPayload = Body(...)) -> dict[str, Any]:
    async with SessionLocal() as session:
        if payload.orchestrator_instructions:
            await set_orchestrator_instructions(session, payload.orchestrator_instructions)

        # Skills first, so imported agents can reference them.
        imported_skill_names = set()
        for skill in payload.skills:
            await upsert_skill(
                session,
                name=skill.name,
                description=skill.description,
                content=skill.content,
            )
            imported_skill_names.add(skill.name)

        imported_names = set()
        for agent in payload.agents:
            try:
                # run_as_principal is deliberately not carried by exports
                # (it is a landscape-specific service identity), and is
                # therefore omitted here so upsert_agent preserves whatever
                # this landscape already has rather than wiping it.
                await upsert_agent(
                    session,
                    name=agent.name,
                    description=agent.description,
                    instructions=agent.instructions,
                    mcp_servers=agent.to_servers_list(),
                    skills=agent.skills,
                    enabled=agent.enabled,
                    expose_chat=agent.expose_chat,
                    expose_api=agent.expose_api,
                    api_slug=agent.api_slug,
                    run_prompt=agent.run_prompt,
                    run_timeout_seconds=agent.run_timeout_seconds,
                )
            except ValueError as e:
                raise HTTPException(
                    status_code=422, detail=f"Agent '{agent.name}': {e}"
                ) from e
            imported_names.add(agent.name)

        removed = 0
        removed_skills = 0
        if payload.replace:
            existing = await list_agents(session)
            for row in existing:
                if row.name not in imported_names:
                    await session.delete(row)
                    removed += 1
            # Replace applies to skills only when the import carries a skills
            # section, so older exports (without one) don't wipe the library.
            if payload.skills:
                for srow in await list_skills(session):
                    if srow.name not in imported_skill_names:
                        await rename_skill_references(session, srow.name, None)
                        await session.delete(srow)
                        removed_skills += 1
            await session.commit()

    return {
        "status": "imported",
        "imported": len(payload.agents),
        "imported_skills": len(payload.skills),
        "removed": removed,
        "removed_skills": removed_skills,
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

        # Skills first, so seeded agents can reference them.
        skill_count = 0
        for entry in data.get("skills", []):
            try:
                skill = SkillPayload.model_validate(entry)
            except Exception as e:
                logger.warning("Skipping invalid seed skill %r: %s", entry, e)
                continue
            await upsert_skill(
                session,
                name=skill.name,
                description=skill.description,
                content=skill.content,
            )
            skill_count += 1

        count = 0
        for entry in data.get("agents", []):
            try:
                # Validate via pydantic model
                payload = AgentPayload.model_validate(entry)
            except Exception as e:
                logger.warning("Skipping invalid seed entry %r: %s", entry, e)
                continue
            try:
                await upsert_agent(
                    session,
                    name=payload.name,
                    description=payload.description,
                    instructions=payload.instructions,
                    mcp_servers=payload.to_servers_list(),
                    skills=payload.skills,
                    enabled=payload.enabled,
                    expose_chat=payload.expose_chat,
                    expose_api=payload.expose_api,
                    api_slug=payload.api_slug,
                    run_as_principal=payload.run_as_principal,
                    run_prompt=payload.run_prompt,
                    run_timeout_seconds=payload.run_timeout_seconds,
                )
            except ValueError as e:
                logger.warning("Skipping invalid seed entry %r: %s", entry.get("name"), e)
                continue
            count += 1
        logger.info("Seeded %d skills and %d agents from %s", skill_count, count, seed_path)
