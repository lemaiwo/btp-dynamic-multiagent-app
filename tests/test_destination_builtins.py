"""Every built-in through a BTP destination, app-level and as the signed-in user.

For each built-in in ``auth_mode="destination"`` a tool call must reach the
destination's URL carrying the destination's ``Authorization`` header, for
both values of ``user_context``; the per-built-in rules the app-only path
already enforces (mailbox, read-only Teams) must hold; storage must keep the
destination keys; admin validation must 422 the same mistakes; and the
credential-health endpoint must report destination servers.

No network: the destination is a fake resolver and the API a MockTransport.

Run:  python -m pytest tests/test_destination_builtins.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
for _var in ("DESTINATION_CLIENT_ID", "DESTINATION_CLIENT_SECRET",
             "DESTINATION_URI", "DESTINATION_TOKEN_URL", "DESTINATION_UAA_URL"):
    os.environ.pop(_var, None)

import httpx  # noqa: E402
import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from agents.auth import current_jwt, current_principal  # noqa: E402
from agents.destination import Destination, DestinationError  # noqa: E402
from agents.destination_auth import (  # noqa: E402
    DestinationUserRequired,
    destination_http_client,
)

FIXTURES = ROOT / "tests" / "fixtures"


class FakeResolver:
    def __init__(self, url: str, headers: dict[str, str] | None = None, name: str = "DEST"):
        self.name = name
        self.url = url
        self.headers = headers if headers is not None else {"Authorization": "Bearer dest-token"}
        self.calls: list[tuple[str | None, str | None]] = []

    async def resolve(self, *, force: bool = False, user_token=None, principal=None) -> Destination:
        self.calls.append((user_token, principal))
        headers = dict(self.headers)
        if user_token and "Authorization" in headers:
            headers["Authorization"] = f"Bearer user-token-of-{principal}"
        auth_type = "OAuth2UserTokenExchange" if user_token else "OAuth2ClientCredentials"
        return Destination(url=self.url, headers=headers, expires_at=time.monotonic() + 60,
                           auth_type=auth_type, per_user=bool(user_token))

    def invalidate(self, principal: str | None = None) -> None:
        pass


class Recorder:
    def __init__(self, respond):
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _http(resolver, recorder, *, user_context: bool, server_key: str, expected_hosts=()):
    return destination_http_client(
        resolver, user_context=user_context, expected_hosts=expected_hosts,
        server_key=server_key, transport=recorder.transport(),
    )


@pytest.fixture
def signed_in():
    """A bound user, as the chat request middleware would leave it."""
    j = current_jwt.set("jwt-ann")
    p = current_principal.set("ann")
    yield "ann"
    current_jwt.reset(j)
    current_principal.reset(p)


def _expected_auth(user_context: bool) -> str:
    return "Bearer user-token-of-ann" if user_context else "Bearer dest-token"


# --- outlook ----------------------------------------------------------------

@pytest.mark.parametrize("user_context", [False, True])
async def test_outlook_through_a_destination(user_context, signed_in):
    from agents.outlook_tools import outlook_toolset

    rec = Recorder(lambda r: httpx.Response(200, json={"value": []}))
    resolver = FakeResolver("https://graph.microsoft.com")
    oauth = {"destination": "GRAPH", "user_context": user_context, "mailbox": "svc@example.com"}
    toolset = outlook_toolset(
        oauth, auth_mode="destination",
        http=_http(resolver, rec, user_context=user_context, server_key="builtin:outlook",
                   expected_hosts=("graph.microsoft.com",)),
    )
    await toolset.tools["list_pending"].function("inbox")
    (sent,) = rec.requests
    root = "/v1.0/me" if user_context else "/v1.0/users/svc@example.com"
    assert str(sent.url).startswith(f"https://graph.microsoft.com{root}/mailFolders/inbox/messages")
    assert sent.headers["Authorization"] == _expected_auth(user_context)
    assert resolver.calls == [("jwt-ann", "ann")] if user_context else [(None, None)]


def test_outlook_app_level_destination_requires_a_mailbox():
    from agents.outlook_tools import outlook_toolset

    with pytest.raises(ValueError, match="requires a 'mailbox'"):
        outlook_toolset({"destination": "GRAPH"}, auth_mode="destination",
                        http=httpx.AsyncClient())
    # As the signed-in user the mailbox is not needed (and is ignored: /me).
    outlook_toolset({"destination": "GRAPH", "user_context": True}, auth_mode="destination",
                    http=httpx.AsyncClient())


async def test_outlook_user_context_without_a_user_is_a_clear_error():
    from agents.outlook_tools import outlook_toolset

    rec = Recorder(lambda r: httpx.Response(200, json={"value": []}))
    toolset = outlook_toolset(
        {"destination": "GRAPH", "user_context": True}, auth_mode="destination",
        http=_http(FakeResolver("https://graph.microsoft.com"), rec,
                   user_context=True, server_key="builtin:outlook"),
    )
    assert current_jwt.get() is None
    with pytest.raises(DestinationUserRequired, match="builtin:outlook"):
        await toolset.tools["list_pending"].function("inbox")
    assert rec.requests == []


async def test_outlook_destination_may_be_a_proxy_with_a_prefix(signed_in):
    from agents.outlook_tools import outlook_toolset

    rec = Recorder(lambda r: httpx.Response(200, json={"value": []}))
    toolset = outlook_toolset(
        {"destination": "GRAPH", "user_context": True}, auth_mode="destination",
        http=_http(FakeResolver("https://apim.example/graph"), rec, user_context=True,
                   server_key="builtin:outlook", expected_hosts=("graph.microsoft.com",)),
    )
    await toolset.tools["list_pending"].function("inbox")
    assert str(rec.requests[0].url).startswith("https://apim.example/graph/v1.0/me/mailFolders/")


# --- gmail ------------------------------------------------------------------

@pytest.mark.parametrize("user_context", [False, True])
async def test_gmail_through_a_destination(user_context, signed_in):
    from agents.gmail_tools import gmail_toolset

    rec = Recorder(lambda r: httpx.Response(200, json={"labels": [{"name": "agent", "id": "L1"}]}))
    resolver = FakeResolver("https://gmail.googleapis.com")
    oauth = {"destination": "GMAIL", "user_context": user_context, "mailbox": "svc@example.com"}
    toolset = gmail_toolset(
        oauth, auth_mode="destination",
        http=_http(resolver, rec, user_context=user_context, server_key="builtin:gmail",
                   expected_hosts=("gmail.googleapis.com",)),
    )
    labels = await toolset.tools["list_labels"].function()
    assert labels == {"agent": "L1"}
    (sent,) = rec.requests
    # httpx re-encodes the rewritten path, where "@" is a legal character.
    user = "me" if user_context else "svc@example.com"
    assert sent.url.path == f"/gmail/v1/users/{user}/labels"
    assert sent.url.host == "gmail.googleapis.com"
    assert sent.headers["Authorization"] == _expected_auth(user_context)


def test_gmail_app_level_destination_requires_a_mailbox_and_app_only_stays_refused():
    from agents.gmail_tools import gmail_toolset

    with pytest.raises(ValueError, match="requires a 'mailbox'"):
        gmail_toolset({"destination": "GMAIL"}, auth_mode="destination", http=httpx.AsyncClient())
    with pytest.raises(ValueError, match="domain-wide delegation"):
        gmail_toolset({}, auth_mode="app_only", http=httpx.AsyncClient())


# --- teams ------------------------------------------------------------------

@pytest.mark.parametrize("user_context", [False, True])
async def test_teams_through_a_destination(user_context, signed_in):
    from agents.teams_tools import teams_toolset

    rec = Recorder(lambda r: httpx.Response(200, json={"value": [
        {"id": "19:c1", "displayName": "General", "description": ""},
    ]}))
    resolver = FakeResolver("https://graph.microsoft.com")
    toolset = teams_toolset(
        {"destination": "GRAPH", "user_context": user_context, "team": "team-1"},
        auth_mode="destination",
        http=_http(resolver, rec, user_context=user_context, server_key="builtin:teams"),
    )
    channels = await toolset.tools["list_channels"].function()
    assert channels[0]["name"] == "General"
    (sent,) = rec.requests
    assert str(sent.url).startswith("https://graph.microsoft.com/v1.0/teams/team-1/channels")
    assert sent.headers["Authorization"] == _expected_auth(user_context)


def test_teams_posts_through_a_destination_only_as_the_user():
    from agents.teams_tools import teams_toolset

    with pytest.raises(ValueError, match="without user context"):
        teams_toolset({"destination": "GRAPH", "team": "t", "allow_send": True},
                      auth_mode="destination", http=httpx.AsyncClient())
    posting = teams_toolset(
        {"destination": "GRAPH", "team": "t", "allow_send": True, "user_context": True},
        auth_mode="destination", http=httpx.AsyncClient(),
    )
    assert {"post_message", "reply_to_message"} <= set(posting.tools)
    read_only = teams_toolset({"destination": "GRAPH", "team": "t"},
                              auth_mode="destination", http=httpx.AsyncClient())
    assert set(read_only.tools) == {"list_channels", "list_messages", "get_thread"}


# --- sapnotes ---------------------------------------------------------------

async def test_sapnotes_through_a_destination_uses_its_url_and_headers():
    from agents.sapnotes_tools import sapnotes_toolset

    sample = json.loads((FIXTURES / "nvd_sap_sample.json").read_text())
    rec = Recorder(lambda r: httpx.Response(200, json=sample))
    resolver = FakeResolver("https://nvd-proxy.example/nvd", headers={"apiKey": "from-destination"})
    toolset = sapnotes_toolset(
        {"destination": "NVD", "min_score": "0"}, auth_mode="destination",
        http=_http(resolver, rec, user_context=False, server_key="builtin:sapnotes",
                   expected_hosts=("services.nvd.nist.gov",)),
    )
    out = await toolset.tools["list_critical_notes"].function()
    assert out["count"] >= 1
    sent = rec.requests[0]
    assert str(sent.url).startswith("https://nvd-proxy.example/nvd/rest/json/cves/2.0?")
    assert "sourceIdentifier=cna%40sap.com" in str(sent.url)
    assert sent.headers["apiKey"] == "from-destination"
    assert "Authorization" not in sent.headers


def test_sapnotes_default_mode_still_calls_nvd_directly():
    from agents.sapnotes_tools import NVD_URL, SapNotesClient, sapnotes_toolset

    toolset = sapnotes_toolset({}, auth_mode="none", http=httpx.AsyncClient())
    assert SapNotesClient(httpx.AsyncClient())._url == NVD_URL
    assert "list_critical_notes" in toolset.tools


# --- sapnotedetail ----------------------------------------------------------

async def test_sapnotedetail_through_a_destination_uses_the_destination_cookie():
    from agents.sapnotedetail_tools import sapnotedetail_toolset

    detail = json.loads((FIXTURES / "sapnote_detail.json").read_text())
    rec = Recorder(lambda r: httpx.Response(
        200, json=detail, headers={"content-type": "application/json"}))
    resolver = FakeResolver("https://me.sap.com", headers={"Cookie": "SAP_SESSIONID=abc"})
    toolset = sapnotedetail_toolset(
        {"destination": "MESAP"}, auth_mode="destination", resolver=resolver,
        http=_http(resolver, rec, user_context=False, server_key="builtin:sapnotedetail",
                   expected_hosts=("me.sap.com",)),
    )
    out = await toolset.tools["get_note_details"].function(["3771065"])
    assert out["count"] == 1 and out["notes"][0]["status"] == "ok"
    (sent,) = rec.requests
    assert str(sent.url).startswith("https://me.sap.com/backend/raw/sapnotes/Detail?q=3771065")
    assert sent.headers["Cookie"] == "SAP_SESSIONID=abc"


async def test_sapnotedetail_destination_without_a_cookie_reads_as_an_expired_session():
    from agents.sapnotedetail_tools import sapnotedetail_toolset

    rec = Recorder(lambda r: httpx.Response(200, json={}))
    resolver = FakeResolver("https://me.sap.com", headers={})
    toolset = sapnotedetail_toolset(
        {"destination": "MESAP"}, auth_mode="destination", resolver=resolver,
        http=_http(resolver, rec, user_context=False, server_key="builtin:sapnotedetail"),
    )
    out = await toolset.tools["get_note_details"].function(["1"])
    assert out["session_expired"] is True
    assert rec.requests == [], "nothing was sent without a cookie"


# --- the factory needs a binding ---------------------------------------------

@pytest.mark.parametrize("url,oauth", [
    ("builtin:gmail", {"destination": "D", "mailbox": "a@b"}),
    ("builtin:outlook", {"destination": "D", "mailbox": "a@b"}),
    ("builtin:teams", {"destination": "D", "team": "t"}),
    ("builtin:sapnotes", {"destination": "D"}),
    ("builtin:sapnotedetail", {"destination": "D"}),
])
def test_build_without_a_destination_binding_names_the_server(url, oauth):
    from agents.builtins import build_builtin_toolset

    with pytest.raises(DestinationError, match=f"{url}: no destination service binding"):
        build_builtin_toolset(url, oauth, "destination")


@pytest.mark.parametrize("url", ["builtin:gmail", "builtin:outlook", "builtin:teams",
                                 "builtin:sapnotes", "builtin:sapnotedetail"])
def test_build_without_a_destination_name_is_refused(url):
    from agents.builtins import build_builtin_toolset

    with pytest.raises(ValueError, match="requires a 'destination'"):
        build_builtin_toolset(url, {"mailbox": "a@b", "team": "t"}, "destination")


# --- storage ----------------------------------------------------------------

def test_clean_oauth_keeps_each_builtins_destination_keys():
    from agents.db import _clean_oauth

    everything = {
        "destination": " GRAPH ", "user_context": True, "mailbox": "svc@example.com",
        "lookback": "2d", "recipients": ["a@x", "b@x"], "team": "t1", "channels": "General",
        "min_score": "9.0", "allow_send": True, "allow_comment": True,
        "client_id": "cid", "client_secret": "hunter2", "project": "ABC",
    }
    gmail = _clean_oauth(everything, "destination", None, url="builtin:gmail")
    assert gmail == {"destination": "GRAPH", "mailbox": "svc@example.com", "user_context": True}
    outlook = _clean_oauth(everything, "destination", None, url="builtin:outlook")
    assert outlook == {
        "destination": "GRAPH", "mailbox": "svc@example.com", "lookback": "2d",
        "recipients": "a@x, b@x", "user_context": True, "allow_send": True,
    }
    teams = _clean_oauth(everything, "destination", None, url="builtin:teams")
    assert teams == {
        "destination": "GRAPH", "team": "t1", "channels": "General", "lookback": "2d",
        "user_context": True, "allow_send": True,
    }
    notes = _clean_oauth(everything, "destination", None, url="builtin:sapnotes")
    assert notes == {"destination": "GRAPH", "min_score": "9.0", "lookback": "2d"}
    detail = _clean_oauth(everything, "destination", None, url="builtin:sapnotedetail")
    assert detail == {"destination": "GRAPH"}
    for block in (gmail, outlook, teams, notes, detail):
        assert "client_secret" not in block and "client_id" not in block


def test_clean_oauth_stores_the_switches_as_real_booleans():
    from agents.db import _clean_oauth

    out = _clean_oauth({"destination": "D", "user_context": "false", "allow_send": "false"},
                       "destination", None, url="builtin:outlook")
    assert "user_context" not in out
    assert out["allow_send"] is False
    with pytest.raises(ValueError, match="destination name"):
        _clean_oauth({"mailbox": "a@b"}, "destination", None, url="builtin:outlook")


def test_jira_and_slack_storage_is_unchanged():
    from agents.db import _clean_oauth

    jira = _clean_oauth({"destination": "J", "project": "ABC", "user_context": True},
                        "destination", None, url="builtin:jira")
    assert jira == {"destination": "J", "project": "ABC", "allow_comment": False}
    slack = _clean_oauth({"destination": "S", "channels": "general"},
                         "destination", None, url="builtin:slack")
    assert slack == {"destination": "S", "channels": "general", "allow_send": False}


# --- admin validation -------------------------------------------------------

def _payload(url: str, **oauth):
    from agents.admin import McpServerPayload

    return McpServerPayload(url=url, auth_mode="destination", oauth=oauth)


def test_admin_accepts_every_builtin_on_a_destination():
    assert _payload("builtin:gmail", destination="D", mailbox="a@b").auth_mode == "destination"
    assert _payload("builtin:gmail", destination="D", user_context=True).oauth.to_config() == {
        "destination": "D", "user_context": True}
    _payload("builtin:outlook", destination="D", mailbox="a@b", allow_send=True)
    _payload("builtin:outlook", destination="D", user_context=True)
    _payload("builtin:teams", destination="D", team="t")
    _payload("builtin:teams", destination="D", team="t", user_context=True, allow_send=True)
    _payload("builtin:sapnotes", destination="D", min_score="9.0")
    _payload("builtin:sapnotedetail", destination="D")


@pytest.mark.parametrize("url,oauth,message", [
    ("builtin:outlook", {"destination": "D"}, "requires oauth.mailbox"),
    ("builtin:gmail", {"destination": "D"}, "requires oauth.mailbox"),
    ("builtin:teams", {"destination": "D"}, "requires oauth.team"),
    ("builtin:teams", {"destination": "D", "team": "t", "allow_send": True},
     "without oauth.user_context"),
    ("builtin:outlook", {"destination": "bad name!", "mailbox": "a@b"}, "destination name"),
    ("builtin:outlook", {"destination": "x" * 201, "mailbox": "a@b"}, "destination name"),
    ("builtin:sapnotes", {"destination": "D", "user_context": True}, "no signed-in user"),
    ("builtin:sapnotedetail", {"destination": "D", "user_context": True}, "no signed-in user"),
    ("builtin:outlook", {"destination": "D", "mailbox": "a@b", "client_id": "c"},
     "stores no credential"),
    ("builtin:gmail", {"mailbox": "a@b"}, "requires oauth.destination"),
])
def test_admin_refuses_destination_misconfiguration(url, oauth, message):
    with pytest.raises(ValidationError, match=message):
        _payload(url, **oauth)


def test_admin_still_refuses_destination_on_a_remote_url_and_keeps_old_rules():
    with pytest.raises(ValidationError, match="only supported for built-in"):
        _payload("https://x.hana.ondemand.com/mcp", destination="D")
    with pytest.raises(ValidationError, match="requires auth_mode=session"):
        from agents.admin import McpServerPayload

        McpServerPayload(url="builtin:sapnotedetail", auth_mode="none")


def test_user_context_field_is_a_boolean():
    from agents.admin import OAuthClientPayload

    on = OAuthClientPayload(destination="D", user_context=True).to_config()
    assert on["user_context"] is True
    assert "user_context" not in OAuthClientPayload(destination="D").to_config()
    with pytest.raises(ValidationError):
        OAuthClientPayload(destination="D", user_context="maybe")


# --- credential health ------------------------------------------------------

class _Row:
    def __init__(self, name, servers, enabled=True):
        self.name = name
        self.mcp_servers = servers
        self.enabled = enabled


class _NoSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def test_destination_health_without_a_binding_reports_unbound(monkeypatch):
    import agents.admin as admin

    rows = [
        _Row("Mail", [
            {"url": "builtin:outlook", "auth_mode": "destination",
             "oauth": {"destination": "GRAPH", "user_context": True}},
            {"url": "https://x.hana.ondemand.com/mcp", "auth_mode": "jwt"},
        ]),
        _Row("Off", [{"url": "builtin:jira", "auth_mode": "destination",
                      "oauth": {"destination": "J"}}], enabled=False),
        _Row("Broken", [{"url": "builtin:jira", "auth_mode": "destination", "oauth": {}}]),
    ]
    monkeypatch.setattr(admin, "SessionLocal", lambda: _NoSession())

    async def fake_list_agents(session):
        return rows

    monkeypatch.setattr(admin, "list_agents", fake_list_agents)
    out = await admin._destination_health()
    assert [e["agent"] for e in out] == ["Mail", "Broken"], \
        "disabled agents and jwt servers are skipped"
    mail, broken = out
    assert mail["state"] == "unbound" and mail["user_context"] is True
    assert mail["destination"] == "GRAPH" and "binding" in mail["error"]
    assert broken["state"] == "error" and "no destination name" in broken["error"]


async def test_destination_health_resolves_with_the_app_token_and_warns_on_mismatch(monkeypatch):
    import agents.admin as admin
    import agents.destination as dest_mod
    from agents.destination import DestinationServiceConfig

    rows = [_Row("Mail", [
        {"url": "builtin:outlook", "auth_mode": "destination",
         "oauth": {"destination": "GRAPH", "user_context": True}},
        {"url": "builtin:jira", "auth_mode": "destination", "oauth": {"destination": "GONE"}},
    ])]
    monkeypatch.setattr(admin, "SessionLocal", lambda: _NoSession())

    async def fake_list_agents(session):
        return rows

    monkeypatch.setattr(admin, "list_agents", fake_list_agents)
    monkeypatch.setattr(
        dest_mod, "config_from_environment",
        lambda env: DestinationServiceConfig("id", "secret", "https://uaa/oauth/token", "https://api"),
    )

    class FakeResolverCls:
        def __init__(self, name, config, **kw):
            self.name = name
            self.kw = kw

        async def resolve(self, **kw):
            assert "user_token" not in kw, "health resolves with the app token only"
            if self.name == "GONE":
                raise DestinationError("destination 'GONE' does not exist; secret=never")
            return Destination(
                url="https://graph.microsoft.com",
                headers={"Authorization": "Bearer s3cr3t"},
                expires_at=time.monotonic() + 60,
                auth_type="OAuth2ClientCredentials",
            )

    monkeypatch.setattr(dest_mod, "DestinationResolver", FakeResolverCls)
    out = await admin._destination_health()
    graph, gone = out
    assert graph["state"] == "resolvable" and graph["auth_type"] == "OAuth2ClientCredentials"
    assert "app-level type" in graph["warning"]
    assert gone["state"] == "error" and "does not exist" in gone["error"]
    text = json.dumps(out)
    assert "s3cr3t" not in text and "Authorization" not in text
