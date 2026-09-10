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
    GET    /admin/api/config               — public base URL for OAuth links
    POST   /admin/api/import               — bulk upsert from JSON
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, HttpUrl, ValidationError, field_validator, model_validator

from agents.auth import current_base_url, current_principal, require_admin
from agents.chat_app import dynamic_chat_app
from agents.builtins import BUILTIN_URLS, is_builtin_url
from agents.jira_tools import BUILTIN_JIRA_URL
from agents.sapnotedetail_tools import BUILTIN_SAPNOTEDETAIL_URL
from agents.db import (
    AUTH_MODE_JWT,
    AUTH_MODE_NONE,
    AUTH_MODE_APP_ONLY,
    AUTH_MODE_DESTINATION,
    AUTH_MODE_OAUTH2,
    AUTH_MODE_SESSION,
    BUILTIN_PUBLIC_KEYS,
    KEEP,
    OAUTH_CONFIG_MODES,
    VALID_AUTH_MODES,
    SessionLocal,
    delete_agent,
    delete_skill,
    delete_workflow,
    get_active_model_name,
    get_agent,
    get_agent_by_slug,
    get_orchestrator_instructions,
    get_skill,
    get_skill_by_name,
    get_workflow,
    get_workflow_by_name,
    get_workflow_parts,
    get_workflow_run,
    list_agents,
    list_item_runs,
    list_skills,
    list_step_runs,
    list_workflow_runs,
    list_workflows,
    normalize_skills_json,
    prepare_servers,
    rename_skill_references,
    set_active_model_name,
    set_orchestrator_instructions,
    upsert_agent,
    upsert_skill,
    upsert_workflow,
)
from agents.registry import registry
from agents.shared import available_models, default_model_name
from agents.workflow_runner import RunRefused as WorkflowRunRefused
from agents.workflow_runner import start_workflow_run

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
    # client_credentials only. `mailbox` names the target, since an app-only
    # token identifies no user. `allow_send` is a capability switch kept
    # separate from the token's permissions on purpose -- see
    # agents/outlook_tools.py on why sending is opt-in.
    mailbox: str = Field(default="", max_length=320)
    allow_send: bool = False
    # How far back a mail listing may reach: "90m", "5h", "2d", "1w", or a bare
    # number of hours. A ceiling, not a default the agent can widen.
    lookback: str = Field(default="", max_length=16)
    # destination only. `destination` names the BTP destination holding the
    # target's URL and credential -- there is nothing else to store, which is
    # the point of this mode. `allow_comment` is a capability switch kept
    # separate from what that credential permits, for the same reason
    # `allow_send` is: a credential that can write must not thereby hand every
    # agent the ability to write.
    destination: str = Field(default="", max_length=256)
    project: str = Field(default="", max_length=64)
    # Multi-value, comma-separated. Wider than `project` because several
    # statuses ("Open, In Progress, In Analysis") do not fit in 64.
    status: str = Field(default="", max_length=256)
    allow_comment: bool = False
    # The REST prefix under the destination's URL. Blank means Jira's own
    # `/rest/api/2`; a proxy that already contributes part of that path needs
    # the remainder here instead. See agents/jira_tools.normalize_api_base.
    api_base: str = Field(default="", max_length=64)
    # Multi-value, comma-separated, and ANDed -- "carries all of these
    # labels", not "any of them". See agents/jira_tools.build_jql for why
    # this is the opposite of how `status` combines.
    labels: str = Field(default="", max_length=256)
    # builtin:sapnotes only. The CVSS floor; 9.0 is what SAP calls HotNews.
    # A string, not a float, because every other field here is one and the
    # storage cleaner stringifies anyway.
    min_score: str = Field(default="", max_length=8)
    # builtin:outlook only. Multi-value, comma-separated: the fixed audience
    # for mail the agent originates. Deliberately not a tool argument -- see
    # OutlookClient.recipients.
    recipients: str = Field(default="", max_length=512)

    @field_validator("min_score")
    @classmethod
    def _validate_min_score(cls, v: str) -> str:
        # Validated here so a typo is a 422 naming the field rather than a
        # registry rebuild failure with no hint where it came from.
        text = (v or "").strip()
        if not text:
            return ""
        try:
            score = float(text)
        except ValueError:
            raise ValueError("min_score must be a number between 0 and 10") from None
        if not 0.0 <= score <= 10.0:
            raise ValueError("min_score must be between 0 and 10")
        return text

    @field_validator("status", "labels", "recipients", mode="before")
    @classmethod
    def _csv_list_is_a_string(cls, v: Any) -> Any:
        """Accept a JSON list for a multi-value field, store it as CSV.

        Both admin UIs post a plain string, but an API client or an exported
        bundle can legitimately carry a list. Without this, the list is a 422
        on a field the caller filled in correctly.
        """
        if isinstance(v, (list, tuple)):
            return ", ".join(str(x).strip() for x in v if str(x).strip())
        return v

    @field_validator("status", "labels", "recipients")
    @classmethod
    def _validate_csv_list(cls, v: str) -> str:
        # A bounded number of values: the cap is not about Jira's limits but
        # about a paste accident becoming a query nobody can read in a run
        # record. 20 is far above any real pin.
        from agents.jira_tools import normalize_csv_list

        if len(normalize_csv_list(v)) > 20:
            raise ValueError("at most 20 comma-separated values")
        return (v or "").strip()

    @field_validator("api_base")
    @classmethod
    def _validate_api_base(cls, v: str) -> str:
        # Same reason as `lookback` below: a typo should be a 422 naming the
        # field, not a 403 from a proxy that saw a doubled prefix mid-run.
        from agents.jira_tools import normalize_api_base

        normalize_api_base(v)
        return (v or "").strip()

    @field_validator("lookback")
    @classmethod
    def _validate_lookback(cls, v: str) -> str:
        # Parsed here so a typo is a 422 naming the field, rather than a Graph
        # 400 surfacing mid-run with no hint where it came from.
        from agents.lookback import parse_lookback

        parse_lookback(v)
        return (v or "").strip()

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
            "mailbox": self.mailbox.strip(),
            "lookback": self.lookback.strip(),
            "destination": self.destination.strip(),
            "project": self.project.strip(),
            "status": self.status.strip(),
            "api_base": self.api_base.strip(),
            "labels": self.labels.strip(),
            "min_score": self.min_score.strip(),
            "recipients": self.recipients.strip(),
        }
        config = {k: v for k, v in fields.items() if v}
        if self.allow_send:
            config["allow_send"] = True
        if self.allow_comment:
            config["allow_comment"] = True
        return config


class SessionPayload(BaseModel):
    """A browser session cookie for a `session`-mode server."""

    cookie: str = Field(min_length=1, max_length=16384)
    # 12h matches the upstream project's cache TTL. Bounded because an
    # over-long TTL makes the credentials panel claim a session is healthy
    # long after SAP dropped it, which is worse than showing nothing.
    expires_in_hours: int = Field(default=12, ge=1, le=48)
    # Defaults to the caller. Set it when connecting a service identity that
    # is not you -- the same value the "Use my principal" button fills in.
    principal: str = Field(default="", max_length=255)

    @field_validator("cookie")
    @classmethod
    def _cookie_is_not_blank(cls, v: str) -> str:
        text = (v or "").strip()
        if not text:
            raise ValueError("cookie must not be blank")
        return text


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
        # Before the per-mode rules, because the oauth2 branch below returns
        # early for DCR.
        if (
            str(self.url or "").strip().rstrip("/").lower() == BUILTIN_JIRA_URL
            and self.auth_mode != AUTH_MODE_DESTINATION
        ):
            # Caught here rather than at reload: jira_toolset has no other way
            # to reach Jira, so a server saved with any other mode builds fine,
            # then raises during the rebuild. The registry logs that and drops
            # the whole agent, which still looks configured in the UI but no
            # longer exists in chat.
            raise ValueError(
                f"{BUILTIN_JIRA_URL} requires auth_mode=destination: it holds "
                "no credential of its own and reaches Jira only through the "
                "BTP destination named in oauth.destination"
            )
        is_note_detail = (
            str(self.url or "").strip().rstrip("/").lower() == BUILTIN_SAPNOTEDETAIL_URL
        )
        if is_note_detail and self.auth_mode != AUTH_MODE_SESSION:
            # Caught here rather than at reload for the same reason the Jira
            # rule is: the toolset has no other way to authenticate, so a
            # server saved under another mode builds fine and then fails
            # mid-run, with the agent still looking configured in the UI.
            raise ValueError(
                f"{BUILTIN_SAPNOTEDETAIL_URL} requires auth_mode=session: it "
                "authenticates with a browser session cookie refreshed by a "
                "human, and holds no credential of its own"
            )
        if self.auth_mode == AUTH_MODE_SESSION and not is_note_detail:
            raise ValueError(
                "auth_mode=session is only supported for "
                f"{BUILTIN_SAPNOTEDETAIL_URL}; no other server reads a "
                "browser session cookie"
            )
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
        elif self.auth_mode == AUTH_MODE_APP_ONLY:
            cfg = self.oauth.to_config() if self.oauth else {}
            if cfg.get("dcr"):
                raise ValueError(
                    "client_credentials cannot use DCR: dynamic registration "
                    "produces a client with no admin-consented application "
                    "permissions, so its tokens can reach nothing"
                )
            if not cfg.get("client_id"):
                raise ValueError("client_credentials server requires oauth.client_id")
            if not (cfg.get("token_url") or cfg.get("uaa_url")):
                raise ValueError(
                    "client_credentials server requires oauth.token_url or "
                    "oauth.uaa_url (there is no authorize_url: no browser is involved)"
                )
            if is_builtin_url(self.url) and not cfg.get("mailbox"):
                raise ValueError(
                    f"{self.url} with auth_mode=client_credentials requires "
                    "oauth.mailbox: an app-only token identifies no user, so the "
                    "target mailbox has to be named"
                )
        elif self.auth_mode == AUTH_MODE_DESTINATION:
            cfg = self.oauth.to_config() if self.oauth else {}
            if not is_builtin_url(self.url):
                # Nothing reads the destination for a real MCP URL: the
                # transport falls through to JWT forwarding, so the user's
                # XSUAA token would go to that host while the UI reported the
                # server as connected by configuration.
                raise ValueError(
                    "auth_mode=destination is only supported for built-in "
                    f"toolsets ({', '.join(sorted(BUILTIN_URLS))}); an MCP "
                    "server over HTTP cannot be reached through a destination, "
                    "so use auth_mode=jwt, oauth2 or none for this URL"
                )
            if cfg.get("dcr"):
                raise ValueError(
                    "a destination server cannot use DCR: the destination "
                    "already holds the target's credential, so there is nothing "
                    "to register"
                )
            if not cfg.get("destination"):
                raise ValueError(
                    "destination server requires oauth.destination: the name of "
                    "the BTP destination holding the target's URL and credential"
                )
            if cfg.get("client_id") or cfg.get("client_secret"):
                raise ValueError(
                    "a destination server stores no credential of its own; "
                    "remove oauth.client_id and oauth.client_secret and keep the "
                    "secret in the destination, where it can be rotated without "
                    "touching this app"
                )
        elif self.auth_mode == AUTH_MODE_NONE and is_builtin_url(self.url):
            # The one credential-free config block. A public built-in reaches
            # a source that needs no token, so the only thing worth storing is
            # how to narrow it -- and the whitelist is what keeps 'none' from
            # becoming a place to stash settings on any server. Mirrors
            # BUILTIN_PUBLIC_KEYS in agents/db.py, which drops the rest on
            # the way to storage; refusing here is what tells the caller why.
            cfg = self.oauth.to_config() if self.oauth else {}
            stray = sorted(set(cfg) - set(BUILTIN_PUBLIC_KEYS))
            if stray:
                raise ValueError(
                    f"a public built-in on auth_mode=none stores no credential; "
                    f"remove oauth.{', oauth.'.join(stray)} "
                    f"(allowed: {', '.join(BUILTIN_PUBLIC_KEYS)})"
                )
        elif self.oauth is not None and self.oauth.to_config():
            raise ValueError(
                "oauth config is only valid when auth_mode=oauth2, app_only "
                "or destination"
            )
        return self

    @model_validator(mode="after")
    def _validate_url(self) -> "McpServerPayload":
        v = self.url.strip().rstrip("/")
        # Built-in toolsets are served in-process, so there is no host to reach
        # and none of the transport rules below apply. The set is closed: an
        # unknown builtin: value is a typo, not an extension point.
        if v.lower().startswith("builtin"):
            if not is_builtin_url(v):
                raise ValueError(
                    f"unknown built-in toolset {v!r}; known: {', '.join(sorted(BUILTIN_URLS))}"
                )
            self.url = v.lower()
            return self
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
    # None (key absent) means "the writer does not carry this field, keep
    # whatever is stored"; an explicit [] / "" means "clear it". See the
    # _Keep docstring in agents/db.py for why the distinction is needed.
    peers: list[str] | None = None
    model_name: str | None = Field(default=None, max_length=128)
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

    @field_validator("peers")
    @classmethod
    def _clean_peers(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        seen: set[str] = set()
        out: list[str] = []
        for p in v:
            p = str(p).strip()
            if p and p not in seen:
                seen.add(p)
                out.append(p)
        return out

    @field_validator("model_name")
    @classmethod
    def _clean_model_name(cls, v: str | None) -> str | None:
        # Deliberately no allowlist check here: see _unknown_model_note.
        if v is None:
            return None
        return v.strip()

    @field_validator("api_slug", mode="before")
    @classmethod
    def _slug_null_is_blank(cls, v: Any) -> Any:
        """Accept an explicit ``null`` slug as "no slug".

        ``AgentConfig.to_export`` emits ``"api_slug": null`` for every agent
        without one (the normal case -- a slug is only needed by expose_api),
        so without this an exported bundle re-imported verbatim 422s on the
        type, not on anything the operator can fix.
        """
        return "" if v is None else v

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
            if s.auth_mode in OAUTH_CONFIG_MODES and s.oauth is not None:
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
    # Bounded like the workflow's own timeout below: 0 would make every run of
    # this step fail instantly at asyncio.wait_for, and a step outliving the
    # whole run's budget can only ever be killed by the run timeout.
    step_timeout_seconds: int = Field(default=600, ge=10, le=1800)


class WorkflowPayload(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = ""
    api_slug: str = ""
    run_as_principal: str = ""
    # Bounds mirror AgentPayload.run_timeout_seconds, except for the ceiling:
    # the BTP scheduler's async timeout defaults to 30 minutes, so a run
    # allowed to exceed 1800s would be reported failed by the scheduler while
    # it was still working. 0 (or any value below the floor) would make every
    # run fail instantly at asyncio.wait_for.
    run_timeout_seconds: int = Field(default=1800, ge=60, le=1800)
    skip_seen_items: bool = True
    # Each parallel item invokes agents against the same target systems and
    # the same model quota; an unbounded value is a self-inflicted overload.
    max_parallel_items: int = Field(default=1, ge=1, le=20)
    on_unknown_branch: str = "fail"
    enabled: bool = True
    branches: list[WorkflowBranchPayload] = Field(default_factory=list)
    steps: list[WorkflowStepPayload] = Field(default_factory=list)

    @field_validator("api_slug", mode="before")
    @classmethod
    def _slug_null_is_blank(cls, v: Any) -> Any:
        """Accept an explicit ``null`` slug as "no slug".

        Same reason as AgentPayload's: ``Workflow.to_export`` emits
        ``"api_slug": null`` for every workflow without one, so without this
        an exported bundle re-imported verbatim 422s on the type.
        """
        return "" if v is None else v


class ImportPayload(BaseModel):
    orchestrator_instructions: str | None = None
    skills: list[SkillPayload] = Field(default_factory=list)
    agents: list[AgentPayload] = Field(default_factory=list)
    workflows: list[WorkflowPayload] = Field(default_factory=list)
    # if true, delete agents/skills/workflows not in the import
    replace: bool = False


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
def _or_keep(value: Any) -> Any:
    """Translate an AgentPayload "not sent" (None) into upsert_agent's KEEP."""
    return KEEP if value is None else value


def _unknown_model_note(agent_name: str, model_name: str | None) -> str | None:
    """Warn -- never reject -- when an override names an unavailable model.

    ``available_models()`` is not authoritative: it is an env override, else
    a live AI Core query, else a small static fallback. During an AI Core
    outage it shrinks to a handful of names, so rejecting on it would make an
    already-configured override unsaveable; and on import it would fail a
    whole bundle over one landscape-specific name -- the same property that
    keeps ``run_as_principal`` out of exports entirely. An override that
    cannot be loaded already degrades safely at build time in
    ``registry._model_for``, which falls back to the active model and logs.
    So this records the mismatch instead of blocking the write.
    """
    if not model_name:
        return None
    allowed = available_models()
    if allowed and model_name in allowed:
        return None
    note = (
        f"Agent {agent_name!r}: model {model_name!r} is not among this "
        f"landscape's available models; the agent will fall back to the "
        f"active model until that deployment exists."
    )
    logger.warning("%s", note)
    return note


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
                peers=_or_keep(payload.peers),
                enabled=payload.enabled,
                expose_chat=payload.expose_chat,
                expose_api=payload.expose_api,
                api_slug=payload.api_slug,
                run_as_principal=payload.run_as_principal,
                run_prompt=payload.run_prompt,
                run_timeout_seconds=payload.run_timeout_seconds,
                model_name=_or_keep(payload.model_name),
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        _unknown_model_note(payload.name, payload.model_name)
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
        # None means the client carries no such field (the UI5 admin form
        # does not), so the stored value stays; "" / [] still clear it.
        if payload.model_name is not None:
            row.model_name = payload.model_name.strip() or None
        if payload.peers is not None:
            row.peers_json = json.dumps(payload.peers) if payload.peers else None
        await session.commit()
        await session.refresh(row)
        _unknown_model_note(payload.name, payload.model_name)
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


@router.get("/api/config", dependencies=[Depends(require_admin)])
async def api_config() -> dict[str, Any]:
    """Public host the UI5 admin needs for absolute OAuth sign-in links.

    `/oauth/login` and the OAuth callback live on the approuter host, outside
    the UI5 app's path. A relative link to them breaks the moment the app is
    served from a Work Zone site, so the app builds absolute URLs from this.

    Reports the same `current_base_url` the OAuth callback reads to build
    `redirect_uri` (`agents/oauth2.py`, `agents/oauth_routes.py`) — set once
    per request by `JWTBindingMiddleware` from `PUBLIC_BASE_URL` /
    `A2A_PUBLIC_URL` / the forwarded host headers, in that order — so the
    sign-in link and the callback can never disagree about the host.
    """
    return {"public_base_url": (current_base_url.get() or "").rstrip("/")}


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

    from agents.oauth2 import normalize_mcp_url, token_status

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
        needs_token = auth_mode in (AUTH_MODE_OAUTH2, AUTH_MODE_SESSION)
        has_token = False
        login_url = ""
        # "none" both for a server that needs no token and for one nobody has
        # signed into yet — the UI tells those apart by needs_token.
        state = "none"
        expires_at: str | None = None
        if needs_token:
            server_key = normalize_mcp_url(url)
            login_url = (
                f"/oauth/login?agent={quote(agent_name)}"
                f"&server={quote(server_key, safe='')}"
            )
            if who:
                try:
                    state, expiry = await token_status(who, server_key)
                    expires_at = expiry.isoformat() if expiry else None
                    # Both states the live connection can use without an
                    # interactive sign-in — the same rule has_usable_token
                    # applies, kept in one place by deriving it here.
                    has_token = state in ("valid", "refreshable")
                except Exception:  # noqa: BLE001 — status display must not 500
                    logger.warning(
                        "Could not read token status for %s on %s",
                        who, server_key, exc_info=True,
                    )
        # An app-only or destination-backed server needs no user token, and
        # reporting has_token=False for it would render as "not connected"
        # forever with no way to fix it. It is connected by configuration, not
        # by anyone signing in.
        no_user_token = auth_mode in (AUTH_MODE_APP_ONLY, AUTH_MODE_DESTINATION)
        out.append({
            "url": url,
            "auth_mode": auth_mode,
            "needs_token": needs_token,
            "has_token": has_token or no_user_token,
            "login_url": login_url,
            "no_user_token": no_user_token,
            "token_state": "valid" if no_user_token else state,
            "expires_at": expires_at,
        })
    return out


@router.post("/api/sessions/{server_key:path}", dependencies=[Depends(require_admin)])
async def api_store_session(server_key: str, request: Request) -> dict[str, Any]:
    """Store a browser session cookie for a `session`-mode server.

    The response never echoes the cookie: it is a live credential, and an
    admin API that reflects one back has widened where it can leak to.

    The body is parsed by hand rather than declared as a ``SessionPayload``
    parameter: FastAPI's default 422 handler serializes
    ``pydantic.ValidationError.errors()`` verbatim, and that includes an
    ``input`` key holding the raw field value on a core-validation failure
    (e.g. a cookie over ``max_length`` -- entirely plausible for a real
    concatenated SAP session header). A ``SessionPayload`` parameter would
    reflect the cookie straight back in the error body, which is exactly the
    leak this docstring promises never happens. Reported errors carry only
    ``loc``/``msg``/``type``, never ``input``.
    """
    from agents.oauth2 import store_session_cookie
    from agents.sapnotedetail_tools import BUILTIN_SAPNOTEDETAIL_URL

    try:
        body = await request.json()
    except Exception as e:  # noqa: BLE001 - malformed JSON, not our business
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="body must be JSON",
        ) from e
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="body must be a JSON object",
        )
    try:
        payload = SessionPayload(**body)
    except ValidationError as e:
        # loc/msg/type only -- never `input`, which pydantic populates with
        # the raw field value (the cookie itself, on a length failure).
        detail = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in e.errors()
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=detail,
        ) from e

    key = server_key.strip().lower()
    if key != BUILTIN_SAPNOTEDETAIL_URL:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{server_key!r} does not use a browser session",
        )

    who = (payload.principal or "").strip() or (current_principal.get() or "")
    if not who:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no principal: pass one, or call this with a user token",
        )

    expires_at = await store_session_cookie(
        who, key, payload.cookie, payload.expires_in_hours
    )
    return {"server_key": key, "principal": who, "expires_at": expires_at.isoformat()}


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


@router.get("/api/runs/{run_id}/report.md", dependencies=[Depends(require_admin)])
async def api_get_run_markdown(run_id: str) -> PlainTextResponse:
    """The run's report as a downloadable .md file.

    Runs recorded before reports became markdown have no body_md; there is
    nothing to serve for those, so they 404 rather than returning an empty file.
    """
    from agents.db import get_job_run

    async with SessionLocal() as session:
        row = await get_job_run(session, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Run not found")
        body = (row.report or {}).get("body_md")
        if not isinstance(body, str):
            raise HTTPException(status_code=404, detail="Run has no markdown report")
        return PlainTextResponse(
            body,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="run-{run_id}.md"'},
        )


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
                # Flush, not commit: upsert_workflow's own by-name lookup
                # below needs to see the rename within this transaction (it
                # is how it identifies "this is the same row" rather than
                # creating a second one), but nothing may become durable
                # until validate_workflow_parts, called inside
                # upsert_workflow, has actually accepted the save. If it
                # rejects the new branches/steps, the except block below
                # rolls this back -- otherwise a rejected save would still
                # rename the workflow out from under its unchanged, invalid
                # steps, and the operator would never know.
                existing.name = payload.name
                await session.flush()
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
            await session.rollback()
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
        # A workflow's branches and steps are the definition; exporting the
        # scalar row alone would produce a bundle that imports as an empty
        # workflow, which validate_workflow_parts rejects anyway.
        workflows = []
        for w in await list_workflows(session):
            branches, steps = await get_workflow_parts(session, w.id)
            workflows.append(w.to_export(branches, steps))
        return {
            "version": 1,
            "orchestrator_instructions": orch,
            "skills": [s.to_export() for s in skills],
            "agents": [r.to_export() for r in rows],
            "workflows": workflows,
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
        warnings: list[str] = []
        for agent in payload.agents:
            try:
                # run_as_principal is deliberately not carried by exports
                # (it is a landscape-specific service identity), and is
                # therefore omitted here so upsert_agent preserves whatever
                # this landscape already has rather than wiping it.
                # peers/model_name use the same KEEP semantics via _or_keep:
                # a bundle exported before those fields existed carries
                # neither key, and must not wipe what is configured here.
                await upsert_agent(
                    session,
                    name=agent.name,
                    description=agent.description,
                    instructions=agent.instructions,
                    mcp_servers=agent.to_servers_list(),
                    skills=agent.skills,
                    peers=_or_keep(agent.peers),
                    enabled=agent.enabled,
                    expose_chat=agent.expose_chat,
                    expose_api=agent.expose_api,
                    api_slug=agent.api_slug,
                    run_prompt=agent.run_prompt,
                    run_timeout_seconds=agent.run_timeout_seconds,
                    model_name=_or_keep(agent.model_name),
                )
            except ValueError as e:
                raise HTTPException(
                    status_code=422, detail=f"Agent '{agent.name}': {e}"
                ) from e
            note = _unknown_model_note(agent.name, agent.model_name)
            if note:
                warnings.append(note)
            imported_names.add(agent.name)

        # Workflows last: validate_workflow_parts resolves every step's agent
        # by name against the enabled agents in this session, so the agents
        # above must already be in place.
        imported_workflow_names = set()
        for workflow in payload.workflows:
            try:
                # run_as_principal omitted for the same reason as the agents
                # above: exports do not carry it, and passing the payload's
                # default "" would wipe this landscape's service identity.
                await upsert_workflow(
                    session,
                    name=workflow.name,
                    description=workflow.description,
                    api_slug=workflow.api_slug,
                    run_timeout_seconds=workflow.run_timeout_seconds,
                    skip_seen_items=workflow.skip_seen_items,
                    max_parallel_items=workflow.max_parallel_items,
                    on_unknown_branch=workflow.on_unknown_branch,
                    enabled=workflow.enabled,
                    branches=[b.model_dump() for b in workflow.branches],
                    steps=[s.model_dump() for s in workflow.steps],
                )
            except ValueError as e:
                raise HTTPException(
                    status_code=422, detail=f"Workflow '{workflow.name}': {e}"
                ) from e
            imported_workflow_names.add(workflow.name)

        removed = 0
        removed_skills = 0
        removed_workflows = 0
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
            # Same rule for workflows: only a bundle that actually carries a
            # workflows section may remove the ones it does not name.
            if payload.workflows:
                for wrow in await list_workflows(session):
                    if wrow.name not in imported_workflow_names:
                        # delete_workflow, not session.delete: the branch and
                        # step rows are not ORM-related to the workflow and
                        # would otherwise be orphaned.
                        await delete_workflow(session, wrow.id)
                        removed_workflows += 1
            await session.commit()

    return {
        "status": "imported",
        "imported": len(payload.agents),
        "imported_skills": len(payload.skills),
        "imported_workflows": len(payload.workflows),
        "removed": removed,
        "removed_skills": removed_skills,
        "removed_workflows": removed_workflows,
        # Model overrides this landscape cannot currently serve are imported
        # rather than rejected, so the operator is told about them here.
        "warnings": warnings,
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
                    peers=_or_keep(payload.peers),
                    enabled=payload.enabled,
                    expose_chat=payload.expose_chat,
                    expose_api=payload.expose_api,
                    api_slug=payload.api_slug,
                    run_as_principal=payload.run_as_principal,
                    run_prompt=payload.run_prompt,
                    run_timeout_seconds=payload.run_timeout_seconds,
                    model_name=_or_keep(payload.model_name),
                )
            except ValueError as e:
                logger.warning("Skipping invalid seed entry %r: %s", entry.get("name"), e)
                continue
            count += 1

        # Workflows last: every step names an agent that must already exist
        # and be enabled, or validate_workflow_parts rejects the definition.
        # Unlike the import path, the seed file IS landscape-local, so it may
        # carry run_as_principal -- same distinction the agents above make.
        workflow_count = 0
        for entry in data.get("workflows", []):
            try:
                wpayload = WorkflowPayload.model_validate(entry)
            except Exception as e:
                logger.warning("Skipping invalid seed workflow %r: %s", entry, e)
                continue
            try:
                await upsert_workflow(
                    session,
                    name=wpayload.name,
                    description=wpayload.description,
                    api_slug=wpayload.api_slug,
                    run_as_principal=wpayload.run_as_principal,
                    run_timeout_seconds=wpayload.run_timeout_seconds,
                    skip_seen_items=wpayload.skip_seen_items,
                    max_parallel_items=wpayload.max_parallel_items,
                    on_unknown_branch=wpayload.on_unknown_branch,
                    enabled=wpayload.enabled,
                    branches=[b.model_dump() for b in wpayload.branches],
                    steps=[s.model_dump() for s in wpayload.steps],
                )
            except ValueError as e:
                logger.warning("Skipping invalid seed workflow %r: %s",
                               entry.get("name"), e)
                continue
            workflow_count += 1
        logger.info("Seeded %d skills, %d agents and %d workflows from %s",
                    skill_count, count, workflow_count, seed_path)
