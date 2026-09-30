"""Tests for the built-in SMTP toolset (``builtin:smtp``).

The server and its credential come from a BTP destination of Type ``MAIL``;
``smtplib`` is replaced by a fake that records what it was asked to do, and
the destination by a fake resolver. No network, no real mail server.

Run:  python -m pytest tests/test_smtp_tools.py
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import sys
from email import message_from_bytes
from email.policy import default as default_policy
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

from agents import smtp_tools  # noqa: E402
from agents.destination import (  # noqa: E402
    DestinationResolver,
    DestinationServiceConfig,
)
from agents.smtp_tools import BUILTIN_SMTP_URL, smtp_toolset  # noqa: E402

PASSWORD = "sup3r-s3cret-pw"


def mail_destination(**overrides) -> dict:
    """A MAIL destination's properties, as the destination service returns them."""
    props = {
        "Name": "MAIL_RELAY",
        "Type": "MAIL",
        "Authentication": "BasicAuthentication",
        "ProxyType": "Internet",
        "mail.smtp.host": "smtp.example.com",
        "mail.smtp.port": "587",
        "mail.user": "apikey",
        "mail.password": PASSWORD,
        "mail.smtp.from": "sender@example.com",
        "mail.smtp.auth": "true",
        "mail.smtp.starttls.enable": "true",
        "mail.smtp.starttls.required": "true",
        "mail.smtp.ssl.enable": "false",
        "mail.transport.protocol": "smtp",
        # The two a relay setup guide tends to add. They must change nothing.
        "mail.smtp.ssl.trust": "*",
        "mail.smtp.ssl.checkserveridentity": "false",
    }
    props.update(overrides)
    return {k: v for k, v in props.items() if v is not None}


class FakeResolver:
    def __init__(self, props: dict | None = None, name: str = "MAIL_RELAY"):
        self.name = name
        self.props = props if props is not None else mail_destination()
        self.calls = 0

    async def resolve_properties(self, *, force: bool = False):
        self.calls += 1
        return dict(self.props)


class FakeSMTP:
    """Records the conversation; one instance per connection."""

    instances: list["FakeSMTP"] = []
    fail_login: Exception | None = None
    fail_connect: Exception | None = None

    def __init__(self, host="", port=0, *args, timeout=None, context=None, **kw):
        if FakeSMTP.fail_connect is not None:
            raise FakeSMTP.fail_connect
        self.host = host
        self.port = port
        self.timeout = timeout
        self.ssl_context = context
        self.starttls_context: ssl.SSLContext | None = None
        self.login_args: tuple | None = None
        self.sent: list[tuple[bytes, str, list[str]]] = []
        self.ops: list[str] = []
        self.closed = False
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed = True
        return False

    def ehlo(self, *a, **kw):
        self.ops.append("ehlo")
        return (250, b"ok")

    def has_extn(self, name):
        return name.lower() in ("starttls", "auth")

    def starttls(self, *a, context=None, **kw):
        self.ops.append("starttls")
        self.starttls_context = context
        return (220, b"ready")

    def login(self, user, password, **kw):
        self.ops.append("login")
        if FakeSMTP.fail_login is not None:
            raise FakeSMTP.fail_login
        self.login_args = (user, password)
        return (235, b"ok")

    def send_message(self, msg, from_addr=None, to_addrs=None, **kw):
        self.ops.append("send")
        self.sent.append((msg.as_bytes(), from_addr, list(to_addrs or [])))
        return {}

    def quit(self):
        self.ops.append("quit")
        self.closed = True


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail_login = None
    FakeSMTP.fail_connect = None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    return FakeSMTP


def _toolset(props=None, **oauth):
    cfg = {"destination": "MAIL_RELAY", "recipients": "team@example.com, ops@example.com",
           "allow_send": True}
    cfg.update(oauth)
    cfg = {k: v for k, v in cfg.items() if v is not None}
    resolver = FakeResolver(props)
    return smtp_toolset(cfg, resolver=resolver, auth_mode="destination"), resolver


async def _send(toolset, subject="Daily check -- 2026-09-29", body="All green.\n\n## Detail\n\n- one\n- two",
                status="ok"):
    return await toolset.tools["send_mail"].function(subject=subject, body=body, status=status)


def _parsed(conn: FakeSMTP):
    raw, _from, _to = conn.sent[0]
    return message_from_bytes(raw, policy=default_policy)


# --- sending ------------------------------------------------------------------

async def test_starttls_with_a_verified_context_even_when_the_destination_says_trust_all():
    toolset, _ = _toolset()
    await _send(toolset)
    (conn,) = FakeSMTP.instances
    assert type(conn) is FakeSMTP, "port 587 + starttls is plain SMTP upgraded, not SMTP_SSL"
    assert (conn.host, conn.port) == ("smtp.example.com", 587)
    assert conn.ops.index("starttls") < conn.ops.index("login") < conn.ops.index("send")
    ctx = conn.starttls_context
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert conn.timeout and 10 <= conn.timeout <= 60
    assert conn.closed


async def test_implicit_ssl_uses_smtp_ssl_with_a_verified_context():
    toolset, _ = _toolset(mail_destination(**{
        "mail.smtp.port": "465", "mail.smtp.ssl.enable": "true",
        "mail.smtp.starttls.enable": "false", "mail.smtp.starttls.required": "false",
    }))
    await _send(toolset)
    (conn,) = FakeSMTP.instances
    assert type(conn) is FakeSMTPSSL
    assert conn.port == 465
    assert "starttls" not in conn.ops
    assert conn.ssl_context.check_hostname is True
    assert conn.ssl_context.verify_mode == ssl.CERT_REQUIRED


async def test_logs_in_with_the_destination_user_and_password():
    toolset, _ = _toolset()
    await _send(toolset)
    assert FakeSMTP.instances[0].login_args == ("apikey", PASSWORD)


async def test_from_comes_from_the_destination():
    toolset, _ = _toolset()
    await _send(toolset)
    conn = FakeSMTP.instances[0]
    assert conn.sent[0][1] == "sender@example.com"
    assert _parsed(conn)["From"] == "sender@example.com"


async def test_from_in_the_config_overrides_the_destination():
    toolset, _ = _toolset(**{"from": "reports@example.com"})
    await _send(toolset)
    conn = FakeSMTP.instances[0]
    assert conn.sent[0][1] == "reports@example.com"
    assert _parsed(conn)["From"] == "reports@example.com"


async def test_to_is_the_configured_recipients_only():
    toolset, _ = _toolset()
    out = await _send(toolset)
    conn = FakeSMTP.instances[0]
    assert conn.sent[0][2] == ["team@example.com", "ops@example.com"]
    msg = _parsed(conn)
    assert msg["To"] == "team@example.com, ops@example.com"
    assert msg["Cc"] is None and msg["Bcc"] is None
    assert out == {"sent": True, "recipients": ["team@example.com", "ops@example.com"],
                   "subject": "Daily check -- 2026-09-29"}


def test_send_mail_takes_no_recipient_argument():
    import inspect

    toolset, _ = _toolset()
    params = inspect.signature(toolset.tools["send_mail"].function).parameters
    assert list(params) == ["subject", "body", "status"]


async def test_multipart_alternative_with_text_and_html():
    toolset, _ = _toolset()
    await _send(toolset)
    msg = _parsed(FakeSMTP.instances[0])
    assert msg.get_content_type() == "multipart/alternative"
    parts = [p.get_content_type() for p in msg.iter_parts()]
    assert parts == ["text/plain", "text/html"]
    text = msg.get_body(("plain",)).get_content()
    html = msg.get_body(("html",)).get_content()
    assert "All green." in text
    assert "<table" in html and "All green." in html
    assert msg["Subject"] == "Daily check -- 2026-09-29"


async def test_html_is_the_report_renderer_with_subline_and_status_tint():
    from agents.mail_render import render_report_html
    from agents.outlook_tools import REPORT_FOOTER

    body = "All green.\n\n## Detail\n\n- one"
    toolset, _ = _toolset()
    await _send(toolset, body=body, status="attention")
    html = _parsed(FakeSMTP.instances[0]).get_body(("html",)).get_content()
    expected = render_report_html(body, title="Daily check", subline="2026-09-29",
                                  status="attention", footer=REPORT_FOOTER)
    assert html.strip() == expected.strip()
    ok = render_report_html(body, title="Daily check", subline="2026-09-29",
                            status="ok", footer=REPORT_FOOTER)
    assert html != ok, "the status tint reaches the html"


async def test_header_injection_in_the_subject_is_flattened():
    toolset, _ = _toolset()
    await _send(toolset, subject="Hi\r\nBcc: evil@example.org")
    conn = FakeSMTP.instances[0]
    assert conn.sent[0][2] == ["team@example.com", "ops@example.com"]
    assert _parsed(conn)["Bcc"] is None


# --- registration -------------------------------------------------------------

def test_send_mail_absent_without_allow_send_true():
    off, _ = _toolset(allow_send=None)
    assert "send_mail" not in off.tools
    as_string, _ = _toolset(allow_send="true")
    assert "send_mail" not in as_string.tools
    on, _ = _toolset()
    assert "send_mail" in on.tools


def test_allow_send_without_recipients_is_refused_at_build():
    with pytest.raises(ValueError, match="recipients"):
        _toolset(recipients=None)
    with pytest.raises(ValueError, match="recipients"):
        _toolset(recipients=" , ")


def test_without_allow_send_and_recipients_no_send_tool():
    toolset, _ = _toolset(recipients=None, allow_send=None)
    assert "send_mail" not in toolset.tools


def test_only_destination_mode_is_supported():
    with pytest.raises(ValueError, match="destination"):
        smtp_toolset({"destination": "D", "recipients": "a@example.com"},
                     resolver=FakeResolver(), auth_mode="oauth2")


def test_a_destination_name_is_required():
    with pytest.raises(ValueError, match="requires a 'destination'"):
        smtp_toolset({"recipients": "a@example.com", "allow_send": True}, auth_mode="destination")


def test_a_bad_recipient_is_refused_at_build():
    with pytest.raises(ValueError, match="recipient"):
        _toolset(recipients="team@example.com, not-an-address")


def test_build_without_a_binding_names_the_server(monkeypatch):
    from agents.builtins import build_builtin_toolset
    from agents.destination import DestinationError

    # Another module's tests may have left a binding in the environment.
    for var in ("VCAP_SERVICES", "DESTINATION_CLIENT_ID", "DESTINATION_CLIENT_SECRET",
                "DESTINATION_URI", "DESTINATION_TOKEN_URL", "DESTINATION_UAA_URL"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(DestinationError, match="builtin:smtp: no destination service binding"):
        build_builtin_toolset("builtin:smtp", {"destination": "D", "recipients": "a@example.com",
                                                "allow_send": True}, "destination")


# --- refusals and errors ------------------------------------------------------

async def test_on_premise_is_rejected():
    toolset, _ = _toolset(mail_destination(ProxyType="OnPremise"))
    with pytest.raises(RuntimeError, match="Cloud Connector"):
        await _send(toolset)
    assert FakeSMTP.instances == []


async def test_a_non_mail_destination_is_rejected():
    toolset, _ = _toolset(mail_destination(Type="HTTP"))
    with pytest.raises(RuntimeError, match="MAIL"):
        await _send(toolset)


async def test_a_password_is_never_sent_unencrypted():
    toolset, _ = _toolset(mail_destination(**{
        "mail.smtp.starttls.enable": "false", "mail.smtp.starttls.required": "false",
    }))
    with pytest.raises(RuntimeError, match="unencrypted"):
        await _send(toolset)
    assert all("login" not in c.ops for c in FakeSMTP.instances)


async def test_auth_error_is_a_clean_runtime_error_without_the_password(caplog):
    FakeSMTP.fail_login = smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials")
    toolset, _ = _toolset()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RuntimeError) as info:
            await _send(toolset)
    assert "authentication" in str(info.value).lower()
    assert PASSWORD not in str(info.value)
    assert PASSWORD not in repr(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert PASSWORD not in caplog.text


async def test_connection_error_is_a_clean_runtime_error():
    FakeSMTP.fail_connect = ConnectionRefusedError(61, "Connection refused")
    toolset, _ = _toolset()
    with pytest.raises(RuntimeError, match="smtp.example.com:587") as info:
        await _send(toolset)
    assert PASSWORD not in str(info.value)


async def test_tls_failure_is_a_clean_runtime_error():
    FakeSMTP.fail_connect = ssl.SSLCertVerificationError("certificate verify failed")
    toolset, _ = _toolset()
    with pytest.raises(RuntimeError, match="(?i)certificate|tls") as info:
        await _send(toolset)
    assert PASSWORD not in str(info.value)


async def test_the_tool_result_carries_no_secret():
    toolset, _ = _toolset()
    out = await _send(toolset)
    assert PASSWORD not in json.dumps(out) and "apikey" not in json.dumps(out)


# --- destination properties ---------------------------------------------------

CONFIG = DestinationServiceConfig(
    client_id="svc-client", client_secret="svc-secret",
    token_url="https://uaa.example/oauth/token", api_url="https://destination.example",
)


class MailService:
    def __init__(self):
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "svc-token", "expires_in": 3600})
        # A MAIL destination: no URL, and BasicAuthentication hands back a
        # Basic token built from the same password.
        return httpx.Response(200, json={
            "destinationConfiguration": mail_destination(),
            "authTokens": [{"type": "Basic", "value": "YXBpa2V5OnN1cDNy",
                            "http_header": {"key": "Authorization", "value": "Basic YXBpa2V5OnN1cDNy"}}],
        })

    def resolver(self) -> DestinationResolver:
        return DestinationResolver("MAIL_RELAY", CONFIG, transport=httpx.MockTransport(self.handler))

    def destination_calls(self):
        return [c for c in self.calls if "/destinations/" in c.url.path]


async def test_resolve_properties_returns_the_raw_configuration_and_caches(caplog):
    svc = MailService()
    resolver = svc.resolver()
    with caplog.at_level(logging.DEBUG):
        props = await resolver.resolve_properties()
        again = await resolver.resolve_properties()
    assert props["mail.smtp.host"] == "smtp.example.com"
    assert props["mail.password"] == PASSWORD
    assert props["Type"] == "MAIL"
    assert len(svc.destination_calls()) == 1, "cached like the URL resolution"
    assert again["mail.user"] == "apikey"
    assert PASSWORD not in repr(props) and PASSWORD not in str(props)
    assert PASSWORD not in caplog.text
    await resolver.resolve_properties(force=True)
    assert len(svc.destination_calls()) == 2


async def test_resolve_properties_leaves_url_resolution_alone():
    svc = MailService()
    resolver = svc.resolver()
    await resolver.resolve_properties()
    from agents.destination import DestinationError

    # A MAIL destination has no URL; the URL API still says so.
    with pytest.raises(DestinationError, match="no URL"):
        await resolver.resolve()


async def test_resolve_properties_is_invalidated_with_the_rest():
    svc = MailService()
    resolver = svc.resolver()
    await resolver.resolve_properties()
    resolver.invalidate()
    await resolver.resolve_properties()
    assert len(svc.destination_calls()) == 2


# --- admin validation and storage ---------------------------------------------

def _payload(auth_mode="destination", **oauth):
    from agents.admin import McpServerPayload

    return McpServerPayload(url="builtin:smtp", auth_mode=auth_mode, oauth=oauth or None)


def test_smtp_is_a_known_builtin():
    from agents.builtins import BUILTIN_URLS, is_builtin_url

    assert BUILTIN_SMTP_URL == "builtin:smtp"
    assert "builtin:smtp" in BUILTIN_URLS
    assert is_builtin_url("BUILTIN:SMTP")


def test_admin_accepts_smtp_on_a_destination():
    p = _payload(destination="MAIL_RELAY", recipients="team@example.com", allow_send=True,
                 **{"from": "reports@example.com"})
    assert p.oauth.to_config() == {
        "destination": "MAIL_RELAY", "recipients": "team@example.com",
        "from": "reports@example.com", "allow_send": True,
    }
    _payload(destination="MAIL_RELAY")


@pytest.mark.parametrize("mode", ["jwt", "none", "oauth2", "app_only", "session"])
def test_admin_rejects_smtp_on_any_other_mode(mode):
    with pytest.raises(ValidationError, match="builtin:smtp requires auth_mode=destination"):
        _payload(auth_mode=mode)


@pytest.mark.parametrize("oauth,message", [
    ({"destination": "MAIL_RELAY", "allow_send": True}, "requires oauth.recipients"),
    ({"destination": "MAIL_RELAY", "recipients": "nope"}, "recipient"),
    ({"destination": "MAIL_RELAY", "from": "not an address"}, "oauth.from"),
    ({"destination": "MAIL_RELAY", "user_context": True}, "no signed-in user"),
    ({"recipients": "a@example.com"}, "requires oauth.destination"),
])
def test_admin_refuses_smtp_misconfiguration(oauth, message):
    with pytest.raises(ValidationError, match=message):
        _payload(**oauth)


def test_storage_keeps_smtp_keys_and_drops_the_rest():
    from agents.db import _clean_oauth

    out = _clean_oauth({"destination": " MAIL_RELAY ", "recipients": ["a@example.com", "b@example.com"],
                        "from": "reports@example.com", "allow_send": True, "user_context": True,
                        "client_secret": "x", "mailbox": "m@example.com"},
                       "destination", None, url="builtin:smtp")
    assert out == {"destination": "MAIL_RELAY", "recipients": "a@example.com, b@example.com",
                   "from": "reports@example.com", "allow_send": True}
    off = _clean_oauth({"destination": "D", "allow_send": "true"}, "destination", None,
                       url="builtin:smtp")
    assert off == {"destination": "D", "allow_send": False}


# --- credential health ----------------------------------------------------------

async def test_destination_health_resolves_a_mail_destination_by_its_properties(monkeypatch):
    import agents.admin as admin
    import agents.destination as dest_mod

    class _Row:
        name = "Mailer"
        enabled = True
        mcp_servers = [{"url": "builtin:smtp", "auth_mode": "destination",
                        "oauth": {"destination": "MAIL_RELAY"}}]

    class _NoSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def fake_list_agents(session):
        return [_Row()]

    monkeypatch.setattr(admin, "SessionLocal", lambda: _NoSession())
    monkeypatch.setattr(admin, "list_agents", fake_list_agents)
    monkeypatch.setattr(dest_mod, "config_from_environment", lambda env: CONFIG)
    svc = MailService()
    real = dest_mod.DestinationResolver
    monkeypatch.setattr(
        dest_mod, "DestinationResolver",
        lambda name, config, **kw: real(name, config, transport=httpx.MockTransport(svc.handler), **kw),
    )
    (entry,) = await admin._destination_health()
    assert entry["state"] == "resolvable", entry
    assert entry["auth_type"] == "BasicAuthentication"
    assert PASSWORD not in json.dumps(entry)


# --- mail theme -------------------------------------------------------------------

THEME = {"band": "#102030", "accent": "#e0a010", "link": "#123abc",
         "logo_url": "https://example.com/logo.png", "org_name": "Example Org",
         "footer": "Sent by the example platform"}


async def test_send_mail_html_carries_the_configured_theme():
    toolset, _ = _toolset(theme=THEME)
    await _send(toolset, body="All green.\n\nSee [the run](https://example.com/run).")
    msg = _parsed(FakeSMTP.instances[0])
    html = msg.get_body(("html",)).get_content()
    assert "background-color:#102030" in html
    assert "#e0a010" in html
    assert "color:#123abc" in html
    assert '<img src="https://example.com/logo.png"' in html
    assert "Sent by the example platform" in html
    assert "#1f3348" not in html
    # The plain-text part is untouched by the theme.
    assert "#102030" not in msg.get_body(("plain",)).get_content()


def test_a_bad_theme_is_refused_at_build():
    with pytest.raises(ValueError, match="band"):
        _toolset(theme={"band": "blue"})


def test_admin_accepts_a_theme_on_smtp():
    p = _payload(destination="MAIL_RELAY", recipients="team@example.com", allow_send=True,
                 theme=THEME)
    assert p.oauth.to_config()["theme"] == THEME


@pytest.mark.parametrize("theme,message", [
    ({"band": "blue"}, "band"),
    ({"logo_url": "http://example.com/logo.png"}, "https"),
    ({"bandd": "#102030"}, "unknown"),
])
def test_admin_refuses_a_bad_theme(theme, message):
    with pytest.raises(ValidationError, match=message):
        _payload(destination="MAIL_RELAY", theme=theme)


def test_admin_refuses_a_theme_on_a_server_that_sends_no_mail():
    from agents.admin import McpServerPayload

    with pytest.raises(ValidationError, match="theme"):
        McpServerPayload(url="builtin:jira", auth_mode="destination",
                         oauth={"destination": "JIRA", "project": "ABC",
                                "theme": {"band": "#102030"}})


def test_admin_api_answers_422_on_a_bad_theme():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agents.admin import McpServerPayload

    app = FastAPI()

    def probe(server) -> dict:
        return {"ok": True}

    # This module uses postponed annotations; resolve the body model here.
    probe.__annotations__["server"] = McpServerPayload
    app.post("/probe")(probe)

    client = TestClient(app)
    base = {"url": "builtin:smtp", "auth_mode": "destination",
            "oauth": {"destination": "MAIL_RELAY"}}
    assert client.post("/probe", json=base).status_code == 200
    bad = {**base, "oauth": {"destination": "MAIL_RELAY", "theme": {"link": "javascript:x"}}}
    r = client.post("/probe", json=bad)
    assert r.status_code == 422
    assert "link" in r.text


def test_storage_keeps_the_theme_through_save_load_build():
    from agents.db import _clean_oauth

    stored = _clean_oauth({"destination": "MAIL_RELAY", "recipients": "a@example.com",
                           "allow_send": True, "theme": THEME},
                          "destination", None, url="builtin:smtp")
    assert stored["theme"] == THEME
    # What the registry hands the factory is the stored block.
    toolset = smtp_toolset(json.loads(json.dumps(stored)), resolver=FakeResolver(None),
                           auth_mode="destination")
    assert "send_mail" in toolset.tools


def test_storage_drops_an_empty_theme_and_refuses_a_bad_one():
    from agents.db import _clean_oauth

    out = _clean_oauth({"destination": "D", "theme": {}}, "destination", None,
                       url="builtin:smtp")
    assert "theme" not in out
    with pytest.raises(ValueError, match="band"):
        _clean_oauth({"destination": "D", "theme": {"band": "x"}}, "destination", None,
                     url="builtin:smtp")
