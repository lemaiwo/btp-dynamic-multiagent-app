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
import re
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)

from agents.auth import current_base_url, current_principal, require_admin
from agents.chat_app import dynamic_chat_app
from agents.builtins import BUILTIN_URLS, is_builtin_url
from agents.jira_tools import BUILTIN_JIRA_URL
from agents.mail_render import MailTheme
from agents.odata import BUILTIN_ODATA_URL
from agents.odata.models import SERVICE_NAME_RE
from agents.outlook_tools import BUILTIN_OUTLOOK_URL
from agents.sapnotedetail_tools import BUILTIN_SAPNOTEDETAIL_URL
from agents.slack_tools import BUILTIN_SLACK_URL
from agents.smtp_tools import BUILTIN_SMTP_URL, is_address
from agents.teams_tools import BUILTIN_TEAMS_URL
from agents.db import (
    AUTH_MODE_JWT,
    AUTH_MODE_NONE,
    AUTH_MODE_APP_ONLY,
    AUTH_MODE_DESTINATION,
    AUTH_MODE_OAUTH2,
    AUTH_MODE_SESSION,
    BUILTIN_PUBLIC_KEYS,
    KEEP,
    MAX_ODATA_ENTRY_SERVICES,
    OAUTH_CONFIG_MODES,
    ODATA_ENTRY_KEYS,
    ODATA_SINGLE_ENTRY_MESSAGE,
    VALID_AUTH_MODES,
    SessionLocal,
    agent_referrers,
    agent_where_used,
    check_delegation_name_collision,
    delete_agent,
    delete_skill,
    delete_workflow,
    check_odata_services,
    create_odata_service,
    delete_odata_service,
    describe_referrers,
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
    list_odata_services,
    list_skills,
    list_step_runs,
    list_workflow_runs,
    list_workflows,
    normalize_skills_json,
    odata_entries,
    odata_service_referrers,
    prepare_servers,
    prepared_server_list,
    rename_agent_references,
    rename_skill_references,
    set_active_model_name,
    set_orchestrator_instructions,
    update_odata_service,
    upsert_agent,
    upsert_skill,
    upsert_workflow,
    validate_api_slug,
    validate_odata_service,
)
from agents.registry import registry
from agents.shared import available_models, default_model_name
from agents.workflow_runner import RunRefused as WorkflowRunRefused
from agents.workflow_runner import start_workflow_run
# --- deep agents ---
from agents.deep import DeepConfig, dump_deep_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL rules
# ---------------------------------------------------------------------------
_BTP_HOST_SUFFIX = "hana.ondemand.com"


def _split_endpoint_url(
    value: str, *, field: str = "url", allow_http: bool = False
) -> SplitResult:
    """Parse a configured endpoint and reject the shapes that fool a host check.

    Everything here is decided by ``urlsplit``, the same parser httpx uses to
    pick the host it connects to, so what is checked is what is dialled.
    Userinfo (``allowed.host@evil.com``), a fragment (``evil.com#.allowed``)
    and an empty host are refused outright: none has a legitimate use in a
    server URL, and each one made a suffix check pass for the wrong host.
    """
    v = (value or "").strip()
    try:
        parts = urlsplit(v)
    except ValueError as e:
        raise ValueError(f"{field}: invalid URL: {e}") from e
    schemes = ("https", "http") if allow_http else ("https",)
    if parts.scheme not in schemes:
        if allow_http:
            raise ValueError(f"{field} must be http:// or https://")
        raise ValueError(f"{field} must use https://")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise ValueError(f"{field} must not carry credentials (user@host)")
    if "#" in v:
        raise ValueError(f"{field} must not carry a #fragment")
    if not parts.hostname:
        raise ValueError(f"{field} must name a host")
    try:
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError as e:
        raise ValueError(f"{field}: invalid port") from e
    return parts


def _host_matches(hostname: str, pattern: str) -> bool:
    """Exact hostname match, or a DNS-label suffix match for ``.suffix``."""
    hostname = hostname.lower()
    pattern = pattern.lower()
    if pattern.startswith("."):
        suffix = pattern.lstrip(".")
        return hostname == suffix or hostname.endswith("." + suffix)
    return hostname == pattern


def _effective_port(parts: SplitResult) -> int | None:
    if parts.port is not None:
        return parts.port
    return {"https": 443, "http": 80}.get(parts.scheme)


def _allowlist_permits(parts: SplitResult, allowlist: str) -> bool:
    """Whether MCP_URL_ALLOWLIST admits this URL.

    Each comma-separated entry is a URL (``https://host[:port][/path]``) or a
    bare host; a host starting with a dot admits its subdomains. Scheme,
    hostname and port must match exactly, and the entry's path must be a
    prefix of the URL's path on a segment boundary -- ``/mcp`` admits
    ``/mcp/v1`` but not ``/mcp-evil``, and ``allowed.example`` never admits
    ``allowed.example.evil.com``.
    """
    for raw in allowlist.split(","):
        entry = raw.strip().rstrip("/")
        if not entry:
            continue
        if "://" not in entry:
            entry = "https://" + entry
        try:
            ep = urlsplit(entry)
            ep.port  # noqa: B018
        except ValueError:
            continue
        if ep.scheme != parts.scheme or not ep.hostname:
            continue
        if not _host_matches(parts.hostname or "", ep.hostname):
            continue
        if _effective_port(ep) != _effective_port(parts):
            continue
        entry_path = ep.path.rstrip("/")
        url_path = parts.path.rstrip("/")
        if entry_path and not (
            url_path == entry_path or url_path.startswith(entry_path + "/")
        ):
            continue
        return True
    return False


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
    # builtin:teams only. `team` is the one team's id the toolset may touch;
    # `channels` (comma-separated names or ids) narrows it further. Both are
    # pinned here rather than tool arguments -- see agents/teams_tools.py.
    team: str = Field(default="", max_length=128)
    channels: str = Field(default="", max_length=512)
    # destination only. On, the destination is resolved with the signed-in
    # user's JWT (X-user-token) and the tools act as that user; off, the
    # destination's own app-level credential is used. See
    # agents/destination_auth.py.
    user_context: bool = False
    # builtin:smtp only. The sender address, overriding the MAIL destination's
    # mail.smtp.from. `from` on the wire and in storage; a Python keyword, so
    # the attribute is named `sender`.
    sender: str = Field(default="", max_length=320, alias="from")
    # builtin:smtp and builtin:outlook only. The look of originated mail:
    # colours, font, logo, org name, footer. See agents/mail_render.MailTheme.
    theme: dict[str, Any] | None = None
    # builtin:odata only, and all its entry holds: the catalogue services the
    # agent may use and whether it may change data through them. Strict,
    # because pydantic would read the string "true" or the number 1 as True
    # and so open writes. See `_validate_odata_entry`.
    services: list[str] = Field(default_factory=list)
    allow_write: StrictBool = False

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("services", "allow_write", mode="before")
    @classmethod
    def _null_is_absent(cls, v: Any, info: Any) -> Any:
        """A client that serialises an unset field as ``null`` means "not
        set": no services, no writes. It can only ever close, never open."""
        if v is None:
            return [] if info.field_name == "services" else False
        return v

    @field_validator("theme")
    @classmethod
    def _validate_theme(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        # Validated here so a bad colour or a non-https logo is a 422 naming
        # the key, not a registry rebuild that drops the agent.
        if v is None:
            return None
        return MailTheme.from_config(v).to_config() or None

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

    @field_validator("status", "labels", "recipients", "channels", mode="before")
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

    @field_validator("status", "labels", "recipients", "channels")
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
            "team": self.team.strip(),
            "channels": self.channels.strip(),
            "from": self.sender.strip(),
        }
        config = {k: v for k, v in fields.items() if v}
        if self.theme:
            config["theme"] = dict(self.theme)
        if self.allow_send:
            config["allow_send"] = True
        if self.allow_comment:
            config["allow_comment"] = True
        if self.user_context:
            config["user_context"] = True
        if self.services:
            config["services"] = list(self.services)
        if self.allow_write:
            config["allow_write"] = True
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

    def _validate_teams(self) -> None:
        """Rules only builtin:teams has, checked before the per-mode ones.

        Each is caught here rather than at reload, where the registry would log
        the failure and drop the agent while the UI still showed it configured.
        """
        if self.auth_mode not in (AUTH_MODE_OAUTH2, AUTH_MODE_APP_ONLY, AUTH_MODE_DESTINATION):
            raise ValueError(
                f"{BUILTIN_TEAMS_URL} requires auth_mode=oauth2 (as the signed-in "
                "user), app_only (read-only, as the application) or destination"
            )
        cfg = self.oauth.to_config() if self.oauth else {}
        if cfg.get("dcr"):
            raise ValueError(
                f"{BUILTIN_TEAMS_URL} cannot use DCR: Microsoft Entra ID does not "
                "offer dynamic client registration, and a DCR config has nowhere "
                "to keep the pinned oauth.team"
            )
        if not cfg.get("team"):
            raise ValueError(
                f"{BUILTIN_TEAMS_URL} requires oauth.team: the id of the one team "
                "this agent may read, pinned here rather than chosen by the agent"
            )
        if self.auth_mode == AUTH_MODE_APP_ONLY and cfg.get("allow_send"):
            raise ValueError(
                f"{BUILTIN_TEAMS_URL} cannot post under auth_mode=app_only: Graph "
                "does not let an application send channel messages. Use oauth2 "
                "to post as the signed-in user, or turn allow_send off"
            )
        if (
            self.auth_mode == AUTH_MODE_DESTINATION
            and cfg.get("allow_send")
            and not cfg.get("user_context")
        ):
            # The same Graph rule through a destination: an app-level
            # destination credential is an application token.
            raise ValueError(
                f"{BUILTIN_TEAMS_URL} cannot post through a destination without "
                "oauth.user_context: the destination's app-level credential is an "
                "application token, and Graph does not let an application send "
                "channel messages. Turn user_context on, or turn allow_send off"
            )

    @staticmethod
    def _validate_oauth_urls(cfg: dict[str, Any]) -> None:
        """The authorization server's endpoints get the same structural rules
        as the MCP URL: https only, no userinfo, no fragment. A client secret
        and every user's authorization code travel to these hosts."""
        for key in ("uaa_url", "authorize_url", "token_url"):
            if cfg.get(key):
                _split_endpoint_url(str(cfg[key]), field=f"oauth.{key}")

    @model_validator(mode="before")
    @classmethod
    def _validate_raw_odata_entry(cls, data: Any) -> Any:
        """The ``builtin:odata`` block as the client sent it.

        Checked before the block becomes an `OAuthClientPayload`, which
        ignores keys it does not know and fills every other field with a
        default: afterwards a stray key, or ``user_context: false``, can no
        longer be told from an entry that was sent correctly.
        """
        if isinstance(data, dict) and _server_key(data.get("url")) == BUILTIN_ODATA_URL:
            oauth = data.get("oauth")
            if isinstance(oauth, OAuthClientPayload):
                oauth = {k: getattr(oauth, k) for k in oauth.model_fields_set}
            if isinstance(oauth, dict):
                _validate_odata_entry(oauth)
        return data

    @model_validator(mode="after")
    def _validate_oauth(self) -> "McpServerPayload":
        if _server_key(self.url) == BUILTIN_ODATA_URL:
            # Its own rules, and none of the generic destination ones below:
            # this entry names no destination (see `_validate_odata_entry`).
            if self.auth_mode != AUTH_MODE_DESTINATION:
                raise ValueError(
                    f"{BUILTIN_ODATA_URL} requires auth_mode=destination: every "
                    "catalogue service is reached through the BTP destination it "
                    "names, and the entry holds no credential of its own"
                )
            _validate_odata_entry(self.oauth.to_config() if self.oauth else {})
            return self
        if self.oauth is not None and (self.oauth.services or self.oauth.allow_write):
            raise ValueError(
                f"oauth.services and oauth.allow_write belong to a {BUILTIN_ODATA_URL} "
                "entry only; no other server reads them"
            )
        if self.oauth is not None and self.oauth.theme:
            _validate_mail_theme(self.url, self.oauth.theme)
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
        if is_note_detail and self.auth_mode not in (AUTH_MODE_SESSION, AUTH_MODE_DESTINATION):
            # Caught here rather than at reload for the same reason the Jira
            # rule is: the toolset has no other way to authenticate, so a
            # server saved under another mode builds fine and then fails
            # mid-run, with the agent still looking configured in the UI.
            raise ValueError(
                f"{BUILTIN_SAPNOTEDETAIL_URL} requires auth_mode=session (a browser "
                "session cookie stored here) or destination (the cookie held as "
                "URL.headers.Cookie in a BTP destination); it holds no credential "
                "of its own"
            )
        if str(self.url or "").strip().rstrip("/").lower() == BUILTIN_TEAMS_URL:
            self._validate_teams()
        if (
            str(self.url or "").strip().rstrip("/").lower() == BUILTIN_SLACK_URL
            and self.auth_mode != AUTH_MODE_DESTINATION
        ):
            # Same reason as the Jira rule above. Slack has no
            # client-credentials grant, so the bot token lives in a destination.
            raise ValueError(
                f"{BUILTIN_SLACK_URL} requires auth_mode=destination: the Slack "
                "bot token lives in the BTP destination named in "
                "oauth.destination, as URL.headers.Authorization"
            )
        if (
            str(self.url or "").strip().rstrip("/").lower() == BUILTIN_SMTP_URL
            and self.auth_mode != AUTH_MODE_DESTINATION
        ):
            # Same reason as the Jira rule above: the SMTP host and its
            # credential live only in a BTP destination of Type MAIL.
            raise ValueError(
                f"{BUILTIN_SMTP_URL} requires auth_mode=destination: the SMTP "
                "server and its credential live in the BTP MAIL destination "
                "named in oauth.destination"
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
            self._validate_oauth_urls(cfg)
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
            self._validate_oauth_urls(cfg)
            if not cfg.get("client_id"):
                raise ValueError("client_credentials server requires oauth.client_id")
            if not (cfg.get("token_url") or cfg.get("uaa_url")):
                raise ValueError(
                    "client_credentials server requires oauth.token_url or "
                    "oauth.uaa_url (there is no authorize_url: no browser is involved)"
                )
            if (
                is_builtin_url(self.url)
                and str(self.url).strip().rstrip("/").lower() != BUILTIN_TEAMS_URL
                and not cfg.get("mailbox")
            ):
                raise ValueError(
                    f"{self.url} with auth_mode=client_credentials requires "
                    "oauth.mailbox: an app-only token identifies no user, so the "
                    "target mailbox has to be named"
                )
        elif self.auth_mode == AUTH_MODE_DESTINATION:
            cfg = self.oauth.to_config() if self.oauth else {}
            # A remote MCP URL is allowed too: `create_mcp_server` sends it
            # through `DestinationAuth`, so the destination names the host and
            # the credential, and the configured URL contributes only its
            # path. That is why `_validate_url` skips the host allow-list for
            # this mode.
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
            # Name shape, user context and the per-built-in pins; see
            # `# --- destinations ---` at the end of this module.
            _validate_destination_config(self.url, cfg)
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
        try:
            parts = _split_endpoint_url(v, allow_http=public)
        except ValueError as e:
            if not public and "https://" in str(e):
                raise ValueError(
                    "url must use https:// (set auth_mode=none for public servers)"
                ) from None
            raise
        try:
            HttpUrl(v)
        except Exception as e:
            raise ValueError(f"invalid URL: {e}") from e
        # Host allow-list applies to authenticated (JWT-forwarding) servers
        # only. Public servers are unrestricted by design. The decision is
        # made on the parsed hostname by DNS label, never on the raw string:
        # `https://evil.com#.hana.ondemand.com` and
        # `https://allowed.hana.ondemand.com.evil.com` both used to pass, and
        # a jwt server sends every chat user's XSUAA token to that host.
        # A destination server sends nothing to this host: DestinationAuth
        # rewrites every request onto the host the BTP destination names and
        # refuses any other, so the allow-list guards nothing here. https is
        # still required above.
        if not public and self.auth_mode != AUTH_MODE_DESTINATION:
            allowlist = os.environ.get("MCP_URL_ALLOWLIST", "").strip()
            if allowlist:
                if not _allowlist_permits(parts, allowlist):
                    raise ValueError(
                        f"url is not in MCP_URL_ALLOWLIST ({allowlist})"
                    )
            elif not _host_matches(parts.hostname or "", "." + _BTP_HOST_SUFFIX):
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
    # --- deep agents --- None (key absent) keeps the stored config, like
    # peers/model_name; a sent object replaces it (defaults clear the column).
    deep: DeepConfig | None = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        # " Foo Bar" and "Foo Bar" are one delegation tool to the registry
        # but two rows to the unique constraint; strip before either sees it.
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v

    @field_validator("api_slug")
    @classmethod
    def _validate_slug(cls, v: str) -> str:
        # A 422 naming the field, rather than an unreachable run endpoint.
        return validate_api_slug(v) or ""

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
        # Before the duplicate rule, so two OData entries get the message
        # that says what to do instead.
        if sum(1 for u in urls if _server_key(u) == BUILTIN_ODATA_URL) > 1:
            raise ValueError(ODATA_SINGLE_ENTRY_MESSAGE)
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
    # Blank is legal for a non-agent kind; validate_workflow_parts requires a
    # real, enabled agent when kind is "agent" and says which step is wrong.
    agent_name: str = Field(default="", max_length=64)
    instructions: str = ""
    fan_out: bool = False
    # Bounded like the workflow's own timeout below: 0 would make every run of
    # this step fail instantly at asyncio.wait_for, and a step outliving the
    # whole run's budget can only ever be killed by the run timeout.
    step_timeout_seconds: int = Field(default=600, ge=10, le=1800)
    # --- step kinds ---
    # "agent" or one of agents.step_kinds.DETERMINISTIC_KINDS. Checked by
    # validate_workflow_parts rather than a Literal here, so the 400/422 names
    # the step position instead of a pydantic location path.
    kind: str = Field(default="agent", max_length=16)
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("agent_name", "kind", mode="before")
    @classmethod
    def _null_is_blank(cls, v: Any) -> Any:
        return "" if v is None else v

    @field_validator("config", mode="before")
    @classmethod
    def _null_config_is_empty(cls, v: Any) -> Any:
        return {} if v is None else v


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

    @field_validator("api_slug")
    @classmethod
    def _validate_slug(cls, v: str) -> str:
        return validate_api_slug(v) or ""


class ImportPayload(BaseModel):
    orchestrator_instructions: str | None = None
    skills: list[SkillPayload] = Field(default_factory=list)
    agents: list[AgentPayload] = Field(default_factory=list)
    workflows: list[WorkflowPayload] = Field(default_factory=list)
    # The OData catalogue, one `ODataService.to_export()` per service.
    # Deliberately untyped: a declared `list[ODataServicePayload]` would be
    # validated by FastAPI, whose 422 echoes each refused `input`, and a
    # catalogue field (a path, a destination name) is where a URL with a
    # credential in it gets pasted by mistake. `_import_odata_services`
    # validates every entry and reports field names and rules only.
    # None / absent / [] all mean "this bundle carries no catalogue".
    odata_services: Any = None
    # if true, delete agents/skills/workflows/OData services not in the import
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


# --- deep agents ---
def _deep_json_or_keep(deep: DeepConfig | None) -> Any:
    """upsert_agent's ``deep_json`` for a payload: KEEP when the client sent
    no ``deep`` key; otherwise the JSON to store (None when it is all
    defaults, so an explicit reset clears the column)."""
    return KEEP if deep is None else dump_deep_config(deep)


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
                deep_json=_deep_json_or_keep(payload.deep),
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
        old_name = row.name
        renamed = old_name != payload.name
        if renamed:
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
            # `upsert_agent` does this for every other writer; this route
            # writes the row itself. Same session as the commit below, and
            # on the prepared entries: the ones the row is given further
            # down, not a second reading of the payload.
            await check_odata_services(
                session, prepared_server_list(primary, extras, primary_oauth_json)
            )
            skills_json = await normalize_skills_json(session, payload.skills)

            slug = validate_api_slug(payload.api_slug)
            if slug:
                clash = await get_agent_by_slug(session, slug)
                if clash is not None and clash.id != agent_id:
                    raise ValueError(
                        f"api_slug {slug!r} is already used by agent {clash.name!r}"
                    )
            if payload.expose_api and not slug:
                raise ValueError("expose_api requires an api_slug")
            if payload.enabled:
                await check_delegation_name_collision(
                    session, payload.name, exclude_id=agent_id
                )
            if row.enabled and not payload.enabled:
                # Disabling drops the agent from the build: peer tools vanish
                # with a log line and workflow steps fail at trigger time.
                # Refuse while anything enabled still depends on it.
                peers, wfs = await agent_referrers(session, old_name)
                if peers or wfs:
                    raise ValueError(
                        f"cannot disable agent {old_name!r}: it is "
                        f"{describe_referrers(peers, wfs)}. Remove those "
                        "references first."
                    )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        if renamed:
            # Keep peer lists and workflow steps pointing at the renamed agent
            await rename_agent_references(session, old_name, payload.name)
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
        # None means the client carries no such field (an older client or
        # bundle), so the stored value stays; "" / [] still clear it.
        if payload.model_name is not None:
            row.model_name = payload.model_name.strip() or None
        if payload.peers is not None:
            row.peers_json = json.dumps(payload.peers) if payload.peers else None
        # --- deep agents ---
        if payload.deep is not None:
            row.deep_json = dump_deep_config(payload.deep)
        await session.commit()
        await session.refresh(row)
        _unknown_model_note(payload.name, payload.model_name)
        return row.to_dict()


@router.delete(
    "/api/agents/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
async def api_delete_agent(
    agent_id: int,
    force: bool = Query(
        default=False,
        description="Also strip the agent from other agents' peer lists. A "
        "workflow step naming it still blocks the delete.",
    ),
) -> None:
    """Delete an agent nothing depends on.

    409 while an enabled agent lists it as a peer or an enabled workflow step
    names it, because deleting anyway would break those quietly (the registry
    logs and drops the peer tool; the workflow fails its preflight at
    trigger time). ``?force=true`` removes the peer references in the same
    transaction; a workflow step is never edited behind the operator's back,
    so it has to be changed first.
    """
    async with SessionLocal() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        peers, wfs = await agent_referrers(session, row.name)
        if wfs or (peers and not force):
            hint = (
                " Edit those workflow steps first."
                if wfs
                else " Repeat with ?force=true to remove the peer references."
            )
            raise HTTPException(
                status_code=409,
                detail=f"Agent {row.name!r} is still referenced: "
                f"{describe_referrers(peers, wfs)}.{hint}",
            )
        # Strip stale peer entries (including those on disabled agents) in
        # the same transaction as the delete; delete_agent commits.
        await rename_agent_references(session, row.name, None)
        await delete_agent(session, agent_id)


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


@router.get("/api/credential-health", dependencies=[Depends(require_admin)])
async def api_credential_health() -> dict[str, Any]:
    """Whether the credentials scheduled runs depend on are still usable.

    Scheduled runs act as an agent's ``run_as_principal`` using a token that
    principal authorized once by hand. Refresh is lazy, so a token can lapse
    quietly between runs and the failure only shows up as a job that produced
    nothing at 03:00. The admin UI reads this to say so out loud instead.

    ``valid`` and ``refreshable`` are both healthy: the second only means the
    access token has expired and the stored refresh token will renew it on the
    next call, which is the normal steady state between runs.
    """
    from agents.oauth2 import scheduled_credentials, token_status_many

    entries = await scheduled_credentials()
    by_principal: dict[str, list[str]] = {}
    for e in entries:
        by_principal.setdefault(e["principal"], []).append(e["server_key"])

    statuses: dict[str, dict[str, tuple[str, Any]]] = {}
    for principal, keys in by_principal.items():
        try:
            statuses[principal] = await token_status_many(principal, keys)
        except Exception:  # noqa: BLE001 — a health check must not 500
            logger.warning(
                "Could not read token status for principal %s", principal, exc_info=True
            )
            statuses[principal] = {}

    problems: list[dict[str, Any]] = []
    for e in entries:
        state, expiry = statuses.get(e["principal"], {}).get(
            e["server_key"], ("unknown", None)
        )
        if state in ("valid", "refreshable"):
            continue
        problems.append({
            "agent": e["agent"],
            "server_key": e["server_key"],
            "principal": e["principal"],
            "token_state": state,
            "expires_at": expiry.isoformat() if expiry else None,
        })

    # Destination-mode servers hold no user token, so they are not among the
    # entries above; they are reported alongside, resolved with the app's
    # token, so a deleted or misconfigured destination shows up here rather
    # than as a failed run. See `_destination_health`.
    try:
        destinations = await _destination_health()
    except Exception:  # noqa: BLE001 — a health check must not 500
        logger.warning("Could not check destination health", exc_info=True)
        destinations = []

    return {
        "checked": len(entries),
        "healthy": len(entries) - len(problems),
        "problems": problems,
        "destinations": destinations,
    }


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

    from agents.oauth2 import normalize_mcp_url, token_status_many

    async with SessionLocal() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        agent_name = row.name
        servers = row.mcp_servers
        stored_principal = row.run_as_principal

    who = (principal or "").strip() or (stored_principal or "")

    # One query for every server's token rather than one per server: the old
    # per-server `token_status` made opening an agent cost N sequential round
    # trips, which showed up as ~0.8-3.8s on this endpoint even with a warm
    # pool. A failure here is a database failure, which would have taken every
    # server's lookup down individually anyway, so the panel degrades to
    # "no token known" as a whole instead of per row.
    statuses: dict[str, tuple[str, Any]] = {}
    if who:
        keys = [
            normalize_mcp_url(str(spec.get("url") or ""))
            for spec in servers
            if str(spec.get("auth_mode") or "")
            in (AUTH_MODE_OAUTH2, AUTH_MODE_SESSION)
        ]
        try:
            statuses = await token_status_many(who, keys)
        except Exception:  # noqa: BLE001 — status display must not 500
            logger.warning(
                "Could not read token status for %s on %d server(s)",
                who, len(keys), exc_info=True,
            )

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
            # Session mode has no authorization-code flow to send anyone
            # through: the cookie is written by POST /admin/api/sessions and
            # refreshed by scripts/sap_session.py, not by signing in here.
            # Advertising a login link for it would send an operator into a
            # flow that does not exist for this server.
            if auth_mode == AUTH_MODE_OAUTH2:
                login_url = (
                    f"/oauth/login?agent={quote(agent_name)}"
                    f"&server={quote(server_key, safe='')}"
                )
            if who and server_key in statuses:
                state, expiry = statuses[server_key]
                expires_at = expiry.isoformat() if expiry else None
                # Both states the live connection can use without an
                # interactive sign-in — the same rule has_usable_token
                # applies, kept in one place by deriving it here.
                has_token = state in ("valid", "refreshable")
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
        # A live run's activity is only in memory; a finished one's is stored.
        from agents.run_activity import live_activity

        data["activity"] = live_activity(run_id) or row.activity
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
        # The catalogue travels with the agents that attach it. A service
        # holds a destination NAME, never a credential, so nothing is
        # redacted; `to_export` carries no id and no row timestamp, and
        # `metadata_fetched_at` in UTC with a `Z`, which is also what an
        # import stores, so export -> import -> export is byte-stable.
        services = await list_odata_services(session)
        return {
            "version": 1,
            "orchestrator_instructions": orch,
            "skills": [s.to_export() for s in skills],
            "agents": [r.to_export() for r in rows],
            "workflows": workflows,
            "odata_services": [s.to_export() for s in services],
        }


def _missing_secret_errors(agent: AgentPayload, existing: Any) -> list[str]:
    """Servers whose secret is neither in the bundle nor already stored.

    ``to_export`` redacts ``client_secret``, so a bundle promoted to another
    landscape carries none. Re-importing onto the same landscape is fine:
    prepare_servers keeps the stored secret for a server with the same URL on
    the same agent. Anywhere else the DB layer would refuse with a message
    that names neither the agent nor the server; this names both.
    """
    stored: dict[str, dict[str, Any]] = {}
    if existing is not None:
        for s in existing.mcp_servers:
            if isinstance(s.get("oauth"), dict):
                stored[s["url"]] = s["oauth"]
    errors: list[str] = []
    for server in agent.mcp_servers:
        if server.auth_mode not in (AUTH_MODE_OAUTH2, AUTH_MODE_APP_ONLY):
            continue
        cfg = server.oauth.to_config() if server.oauth else {}
        if cfg.get("dcr") or cfg.get("client_secret"):
            continue
        if (stored.get(server.url) or {}).get("client_secret"):
            continue
        errors.append(
            f"Agent '{agent.name}': server {server.url} ({server.auth_mode}) has "
            "no oauth.client_secret. Exports redact secrets, so add it to the "
            "bundle or save this server once in the admin UI before importing."
        )
    return errors


# One bundle may carry this many catalogue services. Each may be a definition
# of up to MAX_DEFINITION_BYTES that is validated in the request, so the
# count is bounded like the size of a single one is.
MAX_IMPORT_ODATA_SERVICES = 200
# What decides whose identity a call carries and which system it reaches.
_ODATA_SERVICE_IDENTITY_FIELDS = ("destination", "user_context")


class _ODataImport:
    """What `_import_odata_services` did, for the replace step and the answer."""

    def __init__(self) -> None:
        self.carried = False  # the bundle has a non-empty catalogue section
        self.names: set[str] = set()
        self.existing: dict[str, Any] = {}  # name -> row, as before the import
        self.created = 0
        self.updated = 0
        # name -> the identity fields an update changed
        self.identity: dict[str, list[str]] = {}


async def _import_odata_services(
    session: Any, section: Any, errors: list[str]
) -> _ODataImport:
    """Upsert a bundle's catalogue services by name, without committing.

    Runs before the agents of the same bundle: `upsert_agent` refuses a
    service name the catalogue lacks, and it reads this session, so a service
    carried by the bundle counts. Each entry goes through
    `validate_odata_service` as a whole -- an entry is stored as the bundle
    has it or not at all -- and a refusal is one line that names the service
    and the field, never a value: the name is repeated only when it has the
    form of a service name, otherwise the entry is called by its position.

    ``enabled`` must be present. The payload model defaults it to on, which
    for an import would switch on a service the bundle never said to switch
    on, or re-enable one an admin disabled on this landscape. Entity set
    operations and operation switches default to off and need no such rule.

    The catalogue rows are locked first (all of them: a replace may delete
    any, and the catalogue is small), before anything else of this
    transaction takes a lock: see `agents.db.list_odata_services`.
    """
    result = _ODataImport()
    if section is None:
        return result
    if not isinstance(section, list):
        errors.append("odata_services: expected a list of services")
        return result
    if len(section) > MAX_IMPORT_ODATA_SERVICES:
        errors.append(
            f"odata_services: more than {MAX_IMPORT_ODATA_SERVICES} services in one bundle"
        )
        return result
    if not section:
        return result
    result.carried = True
    result.existing = {r.name: r for r in await list_odata_services(session, lock=True)}
    for position, entry in enumerate(section, start=1):
        name = entry.get("name") if isinstance(entry, dict) else None
        if isinstance(name, str) and re.fullmatch(SERVICE_NAME_RE, name):
            label = f"OData service '{name}'"
        else:
            label = f"OData service #{position}"
        try:
            data = validate_odata_service(entry)
        except ValueError as e:
            errors.append(f"{label}: {e}")
            continue
        if "enabled" not in entry:
            errors.append(f"{label}: enabled: Field required")
            continue
        name = data["name"]
        if name in result.names:
            # The later entry would silently overwrite the earlier one.
            errors.append(f"{label}: listed more than once")
            continue
        result.names.add(name)
        row = result.existing.get(name)
        if row is None:
            try:
                await create_odata_service(session, data, commit=False)
            except ValueError as e:  # created by someone else since the read
                errors.append(f"{label}: {e}")
                continue
            result.created += 1
            continue
        changed = [
            field
            for field in _ODATA_SERVICE_IDENTITY_FIELDS
            if row.to_export()[field] != data[field]
        ]
        # By name, so the name cannot differ: a rename is impossible here.
        await update_odata_service(session, row, data, commit=False)
        result.updated += 1
        if changed:
            result.identity[name] = changed
    return result


@router.post("/api/import", dependencies=[Depends(require_admin)])
async def api_import(payload: ImportPayload = Body(...)) -> dict[str, Any]:
    """Import a bundle as one transaction.

    Every write below flushes rather than commits; the single commit at the
    end happens only when nothing was rejected, so a 422 leaves the database
    exactly as it was -- no half-imported agents and no skipped ``replace``
    deletions. Errors are collected across the whole bundle rather than
    stopping at the first, so the operator fixes them in one round.

    The registry is not rebuilt here (it never was: the admin UIs call
    reload after an import), so an imported catalogue change takes effect at
    the same reload as the agents of its bundle.
    """
    errors: list[str] = []
    warnings: list[str] = []
    removed = removed_skills = removed_workflows = removed_services = 0
    identity_changes: list[dict[str, Any]] = []
    async with SessionLocal() as session:
        try:
            existing_agents = {r.name: r for r in await list_agents(session)}
            imported_names = {a.name for a in payload.agents}
            # Agents a replace import removes: they neither count as
            # delegation-tool collisions nor as referrers of what remains.
            doomed = set(existing_agents) - imported_names if payload.replace else set()

            if payload.orchestrator_instructions:
                await set_orchestrator_instructions(
                    session, payload.orchestrator_instructions, commit=False
                )

            # Skills first, so imported agents can reference them.
            imported_skill_names = set()
            for skill in payload.skills:
                await upsert_skill(
                    session,
                    name=skill.name,
                    description=skill.description,
                    content=skill.content,
                    commit=False,
                )
                imported_skill_names.add(skill.name)

            # The catalogue before the agents, for the same reason.
            odata = await _import_odata_services(
                session, payload.odata_services, errors
            )

            for agent in payload.agents:
                secret_errors = _missing_secret_errors(agent, existing_agents.get(agent.name))
                if secret_errors:
                    errors.extend(secret_errors)
                    continue
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
                        commit=False,
                        ignore_collisions_with=doomed,
                        deep_json=_deep_json_or_keep(agent.deep),
                    )
                except ValueError as e:
                    errors.append(f"Agent '{agent.name}': {e}")
                    continue
                note = _unknown_model_note(agent.name, agent.model_name)
                if note:
                    warnings.append(note)

            # Workflows last: validate_workflow_parts resolves every step's
            # agent by name against the enabled agents in this session, so
            # the agents above must already be in place.
            imported_workflow_names = set()
            for workflow in payload.workflows:
                try:
                    # run_as_principal omitted for the same reason as the
                    # agents above: exports do not carry it, and passing the
                    # payload's default "" would wipe this landscape's
                    # service identity.
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
                        commit=False,
                    )
                except ValueError as e:
                    errors.append(f"Workflow '{workflow.name}': {e}")
                    continue
                imported_workflow_names.add(workflow.name)

            if payload.replace:
                # Workflows go first so a removed workflow's steps no longer
                # count as referrers of an agent removed below. Only a bundle
                # that carries a workflows section may remove the ones it
                # does not name.
                if payload.workflows:
                    for wrow in await list_workflows(session):
                        if wrow.name not in imported_workflow_names:
                            # delete_workflow, not session.delete: the branch
                            # and step rows are not ORM-related to the
                            # workflow and would otherwise be orphaned.
                            await delete_workflow(session, wrow.id, commit=False)
                            removed_workflows += 1
                for name in sorted(doomed):
                    peers, wfs = await agent_referrers(
                        session, name, exclude_agent_names=doomed
                    )
                    if peers or wfs:
                        errors.append(
                            f"Agent '{name}' cannot be removed by replace: it is "
                            f"{describe_referrers(peers, wfs)}"
                        )
                        continue
                    await session.delete(existing_agents[name])
                    removed += 1
                # Replace applies to skills only when the import carries a
                # skills section, so older exports (without one) don't wipe
                # the library.
                if payload.skills:
                    for srow in await list_skills(session):
                        if srow.name not in imported_skill_names:
                            await rename_skill_references(session, srow.name, None)
                            await session.delete(srow)
                            removed_skills += 1

            if odata.carried and (payload.replace or odata.identity):
                # Who attaches what once this import is in: the agents it
                # wrote count, the ones it removes do not.
                await session.flush()
                used_by = {
                    name: [r["agent"] for r in users if r["agent"] not in doomed]
                    for name, users in (await odata_service_referrers(session)).items()
                }
                # Replace applies to the catalogue only when the bundle
                # carries one, like skills. Unlike a skill, a service is not
                # detached from its agents: one that lost its only service
                # could not run, so a service still in use is an error.
                if payload.replace:
                    for name, srow in sorted(odata.existing.items()):
                        if name in odata.names:
                            continue
                        if used_by.get(name):
                            agents = ", ".join(f"'{a}'" for a in used_by[name])
                            errors.append(
                                f"OData service '{name}' cannot be removed by replace: "
                                f"it is used by agent(s) {agents}"
                            )
                            continue
                        await delete_odata_service(session, srow, commit=False)
                        removed_services += 1
                # Allowed, as an admin's own act, but never unseen: an agent
                # whose service now runs as another identity or reaches
                # another system. Field and agent names only.
                for name in sorted(odata.identity):
                    if not used_by.get(name):
                        continue
                    changed = odata.identity[name]
                    identity_changes.append(
                        {"service": name, "changed": changed, "agents": used_by[name]}
                    )
                    agents = ", ".join(f"'{a}'" for a in used_by[name])
                    warnings.append(
                        f"OData service '{name}': {', '.join(changed)} changed; "
                        f"used by agent(s) {agents}"
                    )

            if errors:
                await session.rollback()
                raise HTTPException(status_code=422, detail="\n".join(errors))
            await session.commit()
        except HTTPException:
            raise
        except Exception:
            await session.rollback()
            raise

    return {
        "status": "imported",
        "imported": len(payload.agents),
        "imported_skills": len(payload.skills),
        "imported_workflows": len(payload.workflows),
        "imported_odata_services": odata.created + odata.updated,
        "created_odata_services": odata.created,
        "updated_odata_services": odata.updated,
        "removed": removed,
        "removed_skills": removed_skills,
        "removed_workflows": removed_workflows,
        "removed_odata_services": removed_services,
        # Catalogue services in use whose destination or identity
        # (`user_context`) this import changed: [{service, changed, agents}].
        "odata_identity_changes": identity_changes,
        # Model overrides this landscape cannot currently serve are imported
        # rather than rejected, so the operator is told about them here; so
        # is every entry of `odata_identity_changes`, as a line of text.
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

        # One transaction, committed only once an agent has actually seeded.
        # Committing the orchestrator instructions (or skills) alone would
        # leave a half-seeded database that still counts as empty by the
        # rule above, so the next start would seed on top of it -- and if
        # every agent is rejected there is nothing worth keeping anyway.
        if "orchestrator_instructions" in data and data["orchestrator_instructions"]:
            await set_orchestrator_instructions(
                session, data["orchestrator_instructions"], commit=False
            )

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
                commit=False,
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
                    commit=False,
                    deep_json=_deep_json_or_keep(payload.deep),
                )
            except ValueError as e:
                logger.warning("Skipping invalid seed entry %r: %s", entry.get("name"), e)
                continue
            count += 1

        if count == 0:
            await session.rollback()
            logger.warning(
                "No agent from %s could be seeded; nothing was written, so the "
                "seed is retried on the next start", seed_path,
            )
            return

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
                    commit=False,
                )
            except ValueError as e:
                logger.warning("Skipping invalid seed workflow %r: %s",
                               entry.get("name"), e)
                continue
            workflow_count += 1
        await session.commit()
        logger.info("Seeded %d skills, %d agents and %d workflows from %s",
                    skill_count, count, workflow_count, seed_path)


# --- where used ---
@router.get("/api/agents/{agent_id}/where-used", dependencies=[Depends(require_admin)])
async def api_agent_where_used(agent_id: int) -> dict[str, Any]:
    """Who refers to this agent: peers that list it and workflows that run it.

    Read-only and for display, so unlike the delete/disable guard it includes
    disabled referrers, each flagged with ``enabled``; see ``agent_where_used``.
    """
    async with SessionLocal() as session:
        result = await agent_where_used(session, agent_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        return result


# ---------------------------------------------------------------------------
# --- destinations ---
# ---------------------------------------------------------------------------
# What a BTP destination may be called. The service itself allows a little
# more, but this covers every real name and keeps the value out of any
# position where a stray character could be read as a path.
_DESTINATION_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")

# Built-ins whose destination may act as the signed-in user. Mirrors
# `_DEST_USER_CONTEXT_URLS` in agents/db.py.
_DESTINATION_USER_CONTEXT_URLS = frozenset({
    "builtin:gmail", "builtin:outlook", "builtin:teams",
})
# Built-ins that read a mailbox: without user context the destination's
# credential names no user, so the target mailbox has to be named.
_DESTINATION_MAILBOX_URLS = frozenset({"builtin:gmail", "builtin:outlook"})


def _validate_destination_config(url: str, cfg: dict[str, Any]) -> None:
    """The per-built-in rules of ``auth_mode=destination``, at save time.

    Each mirrors what the toolset factory refuses at build time, so the
    mistake is a 422 naming the field rather than an agent that vanishes at
    the next reload. The Jira and Slack rules predate this and live in the
    branch that calls it; the Teams team-id rule is in `_validate_teams`.
    """
    key = str(url or "").strip().rstrip("/").lower()
    name = str(cfg.get("destination") or "")
    if not _DESTINATION_NAME_RE.match(name):
        raise ValueError(
            "oauth.destination must be a destination name of 1-200 letters, "
            "digits, '_', '.' or '-'"
        )
    user_context = cfg.get("user_context") is True
    if not is_builtin_url(key):
        # A remote MCP server: name and user context are all it has. With
        # user context the user's JWT goes to the destination service only.
        return
    if user_context and key not in _DESTINATION_USER_CONTEXT_URLS:
        raise ValueError(
            f"{key} has no signed-in user to act as; turn oauth.user_context off "
            "(only builtin:gmail, builtin:outlook and builtin:teams act as a user)"
        )
    if key == BUILTIN_SMTP_URL:
        _validate_smtp_config(cfg)
    if key in _DESTINATION_MAILBOX_URLS and not user_context and not cfg.get("mailbox"):
        raise ValueError(
            f"{key} with auth_mode=destination requires oauth.mailbox unless "
            "oauth.user_context is on: the destination's app-level credential "
            "identifies no user, so the target mailbox has to be named"
        )


def _server_key(url: Any) -> str:
    """A server URL the way storage compares it: trimmed, no trailing slash,
    lower case (``agents.db.odata_entries`` and ``_clean_destination``)."""
    return str(url or "").strip().rstrip("/").lower()


# Every key an `OAuthClientPayload` knows, by its wire name. A closed set, so
# a refusal may name one of these; a key outside it is the client's own text
# and is not repeated.
_OAUTH_FIELD_NAMES = frozenset(
    field.alias or name for name, field in OAuthClientPayload.model_fields.items()
) | frozenset(OAuthClientPayload.model_fields)
# Added to every block by the API's own answers and exports (`_redact_servers`
# in agents/db.py); accepted back when false, never stored.
_ODATA_ECHOED_KEY = "has_client_secret"
_ODATA_IDENTITY_KEYS = frozenset({"destination", "user_context"})
_ODATA_CREDENTIAL_KEYS = frozenset({
    "client_id", "client_secret", "uaa_url", "authorize_url", "token_url", "scope", "dcr",
})


def _validate_odata_entry(cfg: dict[str, Any]) -> None:
    """The block of a ``builtin:odata`` entry: ``{services, allow_write?}``.

    This is the gate for which agent may use which OData service and whether
    it may write, so it allows by exact key and refuses the rest -- including
    a key sent with its default value: ``user_context: false`` reads as a
    choice of identity, and there is none to make here. The destination and
    the identity (signed-in user or technical user) belong to the catalogue
    service, so that no agent can run a service as someone else by editing
    its own entry.

    Messages name the field, never the value: a service name is repeated
    only when it has the form of one, a key only when it is a known field.
    Storage (`agents.db._clean_odata_entry`) keeps the same two keys.
    """
    keys = {str(k) for k in cfg}
    if cfg.get(_ODATA_ECHOED_KEY) is False:
        keys.discard(_ODATA_ECHOED_KEY)
    stray = keys - set(ODATA_ENTRY_KEYS)
    identity = sorted(stray & _ODATA_IDENTITY_KEYS)
    if identity:
        raise ValueError(
            f"{BUILTIN_ODATA_URL} takes no oauth.{', oauth.'.join(identity)}: the "
            "destination and identity belong to the catalogue service; duplicate "
            "the service to run it as another identity"
        )
    credential = sorted(stray & _ODATA_CREDENTIAL_KEYS)
    if credential:
        raise ValueError(
            f"{BUILTIN_ODATA_URL} stores no credential of its own; remove "
            f"oauth.{', oauth.'.join(credential)} (the credential lives in the "
            "destination of each catalogue service)"
        )
    if stray:
        known = sorted(stray & _OAUTH_FIELD_NAMES)
        named = f"oauth.{', oauth.'.join(known)}" if known else "the unknown keys"
        raise ValueError(
            f"a {BUILTIN_ODATA_URL} entry holds only oauth.services and "
            f"oauth.allow_write; remove {named}"
        )
    if cfg.get("allow_write") is not None and not isinstance(cfg["allow_write"], bool):
        raise ValueError(
            "oauth.allow_write must be the JSON boolean true or false; a string "
            "or a number does not open writes"
        )
    services = cfg.get("services")
    if services is None or services == []:
        raise ValueError(
            f"{BUILTIN_ODATA_URL} requires oauth.services: the names of the "
            "catalogue services this agent may use (at least one)"
        )
    if not isinstance(services, list):
        raise ValueError("oauth.services must be a list of catalogue service names")
    if len(services) > MAX_ODATA_ENTRY_SERVICES:
        raise ValueError(
            f"oauth.services lists more than {MAX_ODATA_ENTRY_SERVICES} services"
        )
    seen: set[str] = set()
    for name in services:
        if not isinstance(name, str) or not re.fullmatch(SERVICE_NAME_RE, name):
            raise ValueError(
                "oauth.services: invalid service name (lower-case letters, digits "
                "and '-', at most 64 characters)"
            )
        if name in seen:
            raise ValueError(f"oauth.services: duplicate service '{name}'")
        seen.add(name)


# Built-ins that originate mail through agents/mail_render and so read a
# `theme`. Mirrors `_MAIL_THEME_URLS` in agents/db.py.
_MAIL_THEME_URLS = frozenset({BUILTIN_SMTP_URL, BUILTIN_OUTLOOK_URL})


def _validate_mail_theme(url: str, theme: Any) -> None:
    """``oauth.theme`` only where mail is originated, and only valid values."""
    key = str(url or "").strip().rstrip("/").lower()
    if key not in _MAIL_THEME_URLS:
        raise ValueError(
            f"oauth.theme is only supported for {', '.join(sorted(_MAIL_THEME_URLS))}; "
            f"{key} sends no report mail"
        )
    MailTheme.from_config(theme)


def _validate_smtp_config(cfg: dict[str, Any]) -> None:
    """``builtin:smtp``'s pins, mirroring what ``smtp_toolset`` refuses."""
    from agents.jira_tools import normalize_csv_list

    recipients = normalize_csv_list(cfg.get("recipients"))
    bad = [r for r in recipients if not is_address(r)]
    if bad:
        raise ValueError(
            f"oauth.recipients: not a valid recipient address: {', '.join(bad)}"
        )
    if cfg.get("allow_send") is True and not recipients:
        raise ValueError(
            f"{BUILTIN_SMTP_URL} with allow_send requires oauth.recipients: the "
            "audience of originated mail is fixed here, never chosen by the agent"
        )
    sender = str(cfg.get("from") or "").strip()
    if sender and not is_address(sender):
        raise ValueError("oauth.from must be a single email address")


async def _destination_health() -> list[dict[str, Any]]:
    """One entry per destination-mode server of an enabled agent.

    Each destination is resolved once with the app's own token -- never a
    user's, there is none on this request worth borrowing -- and reported as
    ``resolvable``, ``error`` (with the service's message; no header or token
    ever reaches the response) or ``unbound`` when this app has no
    destination service binding at all. A destination meant to act as the
    signed-in user whose ``Authentication`` is an app-level type gets a
    warning, because that mismatch otherwise surfaces only as every user
    reading the same mailbox.

    A ``builtin:odata`` entry names no destination, so it is reported as one
    entry per attached service, with that service's destination and identity
    from the catalogue (``service`` and ``service_enabled`` are the extra
    keys). A name the catalogue does not have is a ``missing`` entry with
    ``service_enabled`` false; see `_odata_destination_health`.
    """
    from agents.destination import (
        MISSING_BINDING_MESSAGE,
        USER_PROPAGATING_AUTH_TYPES,
        DestinationError,
        DestinationResolver,
        config_from_environment,
    )

    async with SessionLocal() as session:
        rows = await list_agents(session)
        catalogue: dict[str, Any] = {}
        if any(r.enabled and odata_entries(r.mcp_servers) for r in rows):
            # One read for every entry of every agent.
            catalogue = {s.name: s for s in await list_odata_services(session)}
    config = config_from_environment(os.environ)
    resolvers: dict[str, DestinationResolver] = {}
    out: list[dict[str, Any]] = []
    for row in rows:
        if not row.enabled:
            continue
        for srv in row.mcp_servers:
            if str(srv.get("auth_mode") or "") != AUTH_MODE_DESTINATION:
                continue
            oauth = srv.get("oauth") if isinstance(srv.get("oauth"), dict) else {}
            if _server_key(srv.get("url")) == BUILTIN_ODATA_URL:
                out.extend(
                    await _odata_destination_health(
                        row.name, oauth, catalogue, config, resolvers
                    )
                )
                continue
            name = str(oauth.get("destination") or "").strip()
            user_context = oauth.get("user_context") is True
            entry: dict[str, Any] = {
                "agent": row.name,
                "server_key": str(srv.get("url") or ""),
                "destination": name,
                "user_context": user_context,
                "state": "error",
                "auth_type": "",
                "error": None,
            }
            if not name:
                entry["error"] = "no destination name configured"
            elif config is None:
                entry["state"] = "unbound"
                entry["error"] = MISSING_BINDING_MESSAGE
            else:
                resolver = resolvers.get(name)
                if resolver is None:
                    # A public target (NVD) legitimately hands back no
                    # credential; that is not what this check is for.
                    resolver = DestinationResolver(name, config, require_credential=False)
                    resolvers[name] = resolver
                try:
                    if str(srv.get("url") or "").strip().rstrip("/").lower() == BUILTIN_SMTP_URL:
                        # A MAIL destination has no URL; its properties are
                        # the resolution. Only the auth type is reported.
                        props = await resolver.resolve_properties()
                        entry["auth_type"] = props.auth_type
                    else:
                        resolved = await resolver.resolve()
                        entry["auth_type"] = resolved.auth_type
                    entry["state"] = "resolvable"
                except DestinationError as e:
                    entry["error"] = str(e)[:400]
                except Exception as e:  # noqa: BLE001 - a health check must not 500
                    entry["error"] = f"{type(e).__name__}: {e}"[:400]
            auth_type = str(entry["auth_type"] or "")
            if user_context and auth_type and auth_type not in USER_PROPAGATING_AUTH_TYPES:
                entry["warning"] = (
                    f"configured to act as the signed-in user, but the destination's "
                    f"Authentication is {auth_type}, an app-level type; every user "
                    f"would share one credential. Use OAuth2UserTokenExchange, "
                    f"OAuth2JWTBearer or OAuth2SAMLBearerAssertion"
                )
            out.append(entry)
    return out


async def _odata_destination_health(
    agent: str,
    oauth: dict[str, Any],
    catalogue: dict[str, Any],
    config: Any,
    resolvers: dict[str, Any],
) -> list[dict[str, Any]]:
    """The `_destination_health` entries of one ``builtin:odata`` entry: one
    per service it lists.

    ``{agent, server_key, service, service_enabled, destination,
    user_context, state, auth_type, error}`` plus ``warning`` on an identity
    mismatch. ``state`` is ``resolvable``, ``error``, ``unbound`` or
    ``missing``: a listed name the catalogue no longer has (the save gate
    refuses it, but a row can predate a delete), reported rather than left
    out because the agent silently works without that service.
    ``service_enabled`` is false for a disabled catalogue service -- its
    destination is still checked, but the agent cannot use it until it is
    switched on -- and for one that is missing.

    Resolved as the application, like every other entry -- never with a
    user's token. A destination of a user-propagating type hands the
    application no token (``PrincipalPropagation`` has no credential at all
    without a user), so for a service that runs as the signed-in user its
    properties are read instead, and it counts as resolvable when they name
    such a type. That makes ``resolvable`` weaker for these types: ANY failed
    app-level resolve is forgiven once the properties name one, so a
    transient destination-service failure on the resolve is not visible
    here, and nothing says the destination works for a user. The answer
    carries the service and destination names, the identity and the auth
    type: no host, no header.
    """
    from agents.destination import (
        MISSING_BINDING_MESSAGE,
        USER_PROPAGATING_AUTH_TYPES,
        DestinationError,
        DestinationResolver,
    )

    services = oauth.get("services")
    out: list[dict[str, Any]] = []
    for name in services if isinstance(services, list) else []:
        entry: dict[str, Any] = {
            "agent": agent,
            "server_key": BUILTIN_ODATA_URL,
            "service": "",
            "service_enabled": False,
            "destination": "",
            "user_context": False,
            "state": "error",
            "auth_type": "",
            "error": None,
        }
        out.append(entry)
        if not isinstance(name, str) or not re.fullmatch(SERVICE_NAME_RE, name):
            # A row written around the save gate; the value is not repeated.
            entry["error"] = "invalid service name"
            continue
        entry["service"] = name
        service = catalogue.get(name)
        if service is None:
            entry["state"] = "missing"
            entry["error"] = "unknown OData service"
            continue
        entry["service_enabled"] = bool(service.enabled)
        destination = str(service.destination or "").strip()
        user_context = bool(service.user_context)
        entry["destination"] = destination
        entry["user_context"] = user_context
        if not destination:
            entry["error"] = "no destination name configured"
            continue
        if config is None:
            entry["state"] = "unbound"
            entry["error"] = MISSING_BINDING_MESSAGE
            continue
        resolver = resolvers.get(destination)
        if resolver is None:
            resolver = DestinationResolver(destination, config, require_credential=False)
            resolvers[destination] = resolver
        try:
            try:
                entry["auth_type"] = (await resolver.resolve()).auth_type
            except DestinationError:
                if not user_context:
                    raise
                try:
                    fallback = (await resolver.resolve_properties()).auth_type
                except Exception:  # noqa: BLE001 - the first error is the answer
                    fallback = ""
                if fallback not in USER_PROPAGATING_AUTH_TYPES:
                    raise
                entry["auth_type"] = fallback
            entry["state"] = "resolvable"
        except DestinationError as e:
            entry["error"] = str(e)[:400]
        except Exception as e:  # noqa: BLE001 - a health check must not 500
            entry["error"] = f"{type(e).__name__}: {e}"[:400]
        auth_type = str(entry["auth_type"] or "")
        if user_context and auth_type and auth_type not in USER_PROPAGATING_AUTH_TYPES:
            entry["warning"] = (
                f"the service runs as the signed-in user, but the destination's "
                f"Authentication is {auth_type}, an app-level type; every user "
                f"would share one credential. Use PrincipalPropagation (on-premise), "
                f"OAuth2UserTokenExchange, OAuth2JWTBearer or OAuth2SAMLBearerAssertion"
            )
    return out


# ---------------------------------------------------------------------------
# OData catalogue (/admin/api/odata/...)
#
# A late import: the routes live in their own module, which needs nothing
# from this one. Each of them carries `require_admin` itself.
# ---------------------------------------------------------------------------
from agents.odata.admin_routes import router as _odata_router  # noqa: E402

router.include_router(_odata_router)
