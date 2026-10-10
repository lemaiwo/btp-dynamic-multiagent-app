"""Storage is one gate with the admin payload for server URLs and write switches.

`scripts/import_bundle.py` and direct `upsert_agent` callers reach
`prepare_servers` without `McpServerPayload`. Whatever the payload refuses
about where a credential is sent (an http:// endpoint, userinfo, a fragment,
an unknown `builtin:` that would otherwise be built as a remote MCP server)
storage must refuse as well, and a write switch is stored only for the JSON
boolean ``true``: the string "false" must not open a write tool.

Also the Postgres TLS context: without a usable CA from the binding,
certificate verification stays on (system trust store); turning it off takes
the explicit ``PG_SSL_INSECURE=1``.
"""

from __future__ import annotations

import datetime
import logging
import os
import ssl
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ.setdefault("AICORE_AVAILABLE_MODELS", "gpt-4o")

from agents.admin import McpServerPayload  # noqa: E402
from agents.db import _build_ssl_context, _clean_oauth, prepare_servers  # noqa: E402

HOST = "https://mcp.cfapps.eu10.hana.ondemand.com"
SECRET = "s3cr3t-pw"


@pytest.fixture(autouse=True)
def _no_allowlist(monkeypatch):
    # The host allow-list is the payload's alone (it depends on the
    # environment); every URL below is on a BTP host, so it never decides.
    monkeypatch.setenv("MCP_URL_ALLOWLIST", "")


def _one(url: str, mode: str = "jwt", oauth: dict | None = None):
    entry: dict = {"url": url, "auth_mode": mode}
    if oauth is not None:
        entry["oauth"] = oauth
    return prepare_servers([entry], None)


# --- (a) server URLs --------------------------------------------------------

REFUSED_URLS = [
    ("http://mcp.cfapps.eu10.hana.ondemand.com/mcp", "jwt", None),
    (f"https://admin:{SECRET}@mcp.cfapps.eu10.hana.ondemand.com/mcp", "jwt", None),
    (f"https://{SECRET}@mcp.cfapps.eu10.hana.ondemand.com/mcp", "jwt", None),
    (f"https://mcp.cfapps.eu10.hana.ondemand.com/mcp#{SECRET}", "jwt", None),
    ("https:///mcp", "jwt", None),
    ("https://mcp.cfapps.eu10.hana.ondemand.com:99999/mcp", "jwt", None),
    ("ftp://mcp.cfapps.eu10.hana.ondemand.com/mcp", "jwt", None),
    ("mcp.cfapps.eu10.hana.ondemand.com/mcp", "jwt", None),
    ("http://mcp.cfapps.eu10.hana.ondemand.com/mcp", "destination", {"destination": "D"}),
    (f"http://u:{SECRET}@public.example.org/mcp", "none", None),
    (f"https://public.example.org/mcp#{SECRET}", "none", None),
    ("builtin:nope", "jwt", None),
    ("builtin:gmailx", "oauth2", {"dcr": True}),
    (f"BUILTIN:{SECRET}", "none", None),
]


@pytest.mark.parametrize(("url", "mode", "oauth"), REFUSED_URLS)
def test_storage_refuses_a_url_the_gate_refuses(url, mode, oauth):
    with pytest.raises(ValueError) as exc:
        _one(url, mode, oauth)
    text = str(exc.value)
    assert "url" in text
    assert SECRET not in text
    assert url not in text


@pytest.mark.parametrize(("url", "mode", "oauth"), REFUSED_URLS)
def test_the_gate_refuses_the_same_urls(url, mode, oauth):
    """Parity: what storage refuses here the payload refuses too, so the
    storage rule never turns away a save the admin UI accepts."""
    body: dict = {"url": url, "auth_mode": mode}
    if oauth is not None:
        body["oauth"] = oauth
    with pytest.raises(ValidationError):
        McpServerPayload(**body)


ACCEPTED_URLS = [
    (f"{HOST}/mcp", "jwt", None),
    (f"{HOST}:8443/mcp", "jwt", None),
    ("http://localhost:8000/mcp", "none", None),
    ("http://public.example.org/mcp", "none", None),
    (f"{HOST}/mcp", "destination", {"destination": "D"}),
    ("builtin:sapnotes", "none", None),
]


@pytest.mark.parametrize(("url", "mode", "oauth"), ACCEPTED_URLS)
def test_storage_and_gate_accept_the_same_urls(url, mode, oauth):
    body: dict = {"url": url, "auth_mode": mode}
    if oauth is not None:
        body["oauth"] = oauth
    McpServerPayload(**body)
    primary, _, _ = _one(url, mode, oauth)
    assert primary["auth_mode"] == mode


def test_a_known_builtin_in_another_spelling_is_stored_canonical():
    """The payload stores a built-in lower-cased without a trailing slash;
    storage does the same, so the registry knows it as a built-in instead of
    building it as a remote MCP server."""
    primary, _, _ = _one("Builtin:SapNotes/", "none")
    assert primary["url"] == "builtin:sapnotes"


def test_a_row_stored_in_an_old_spelling_keeps_its_secret_on_save():
    import json

    class _Existing:
        mcp_servers = [{
            "url": "Builtin:Outlook/",
            "auth_mode": "app_only",
            "oauth": {"client_id": "cid", "client_secret": "kept",
                      "token_url": "https://auth.example.org/token",
                      "mailbox": "box@example.org"},
        }]

    _, _, oauth_json = prepare_servers(
        [{"url": "Builtin:Outlook/", "auth_mode": "app_only",
          "oauth": {"client_id": "cid", "client_secret": "",
                    "token_url": "https://auth.example.org/token",
                    "mailbox": "box@example.org"}}],
        _Existing(),
    )
    assert json.loads(oauth_json)["client_secret"] == "kept"


# --- (a) authorization-server endpoints ---------------------------------------

OAUTH2_BASE = {
    "client_id": "cid",
    "client_secret": "csecret",
    "authorize_url": "https://auth.example.org/authorize",
    "token_url": "https://auth.example.org/token",
}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("token_url", "http://auth.example.org/token"),
        ("authorize_url", "http://auth.example.org/authorize"),
        ("uaa_url", "http://tenant.authentication.eu10.hana.ondemand.com"),
        ("token_url", f"https://u:{SECRET}@auth.example.org/token"),
        ("token_url", f"https://auth.example.org/token#{SECRET}"),
    ],
)
@pytest.mark.parametrize("mode", ["oauth2", "app_only"])
def test_storage_refuses_an_insecure_authorization_endpoint(mode, key, value):
    if mode == "app_only" and key == "authorize_url":
        pytest.skip("client_credentials stores no authorize_url")
    oauth = dict(OAUTH2_BASE)
    if mode == "app_only":
        oauth.pop("authorize_url")
        oauth["mailbox"] = "box@example.org"
    oauth[key] = value
    with pytest.raises(ValueError) as exc:
        _one(f"{HOST}/mcp", mode, oauth)
    text = str(exc.value)
    assert f"oauth.{key}" in text
    assert SECRET not in text and value not in text
    # Parity with the payload.
    with pytest.raises(ValidationError):
        McpServerPayload(url=f"{HOST}/mcp", auth_mode=mode, oauth=oauth)


def test_https_authorization_endpoints_are_stored():
    _, _, oauth_json = _one(f"{HOST}/mcp", "oauth2", dict(OAUTH2_BASE))
    assert "https://auth.example.org/token" in (oauth_json or "")


# --- (b) write switches ------------------------------------------------------


@pytest.mark.parametrize("value", ["false", "0", "no", 1, "true", ["x"], {"a": 1}])
def test_jira_allow_comment_is_stored_only_for_true(value):
    cleaned = _clean_oauth(
        {"destination": "JIRA", "allow_comment": value}, "destination", None, url="builtin:jira"
    )
    assert cleaned["allow_comment"] is False


def test_jira_allow_comment_true_is_stored():
    cleaned = _clean_oauth(
        {"destination": "JIRA", "allow_comment": True}, "destination", None, url="builtin:jira"
    )
    assert cleaned["allow_comment"] is True


@pytest.mark.parametrize("url", ["builtin:outlook", "builtin:teams"])
@pytest.mark.parametrize("value", ["false", "0", 1, "true"])
def test_oauth2_allow_send_is_stored_only_for_true(url, value):
    oauth = dict(OAUTH2_BASE, allow_send=value, team="t-1")
    cleaned = _clean_oauth(oauth, "oauth2", None, url=url)
    assert cleaned["allow_send"] is False


@pytest.mark.parametrize("url", ["builtin:outlook", "builtin:teams"])
def test_oauth2_allow_send_true_is_stored(url):
    oauth = dict(OAUTH2_BASE, allow_send=True, team="t-1")
    assert _clean_oauth(oauth, "oauth2", None, url=url)["allow_send"] is True


@pytest.mark.parametrize("value", ["false", "0", 1, "true"])
def test_client_credentials_allow_send_is_stored_only_for_true(value):
    oauth = {
        "client_id": "cid",
        "client_secret": "csecret",
        "token_url": "https://auth.example.org/token",
        "mailbox": "box@example.org",
        "allow_send": value,
    }
    cleaned = _clean_oauth(oauth, "app_only", None, url="builtin:outlook")
    assert cleaned["allow_send"] is False


# --- A4: Postgres TLS -------------------------------------------------------


def _self_signed_pem() -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-pg-ca")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def test_pg_tls_without_ca_verifies_by_default(monkeypatch, caplog):
    monkeypatch.delenv("PG_SSL_INSECURE", raising=False)
    with caplog.at_level(logging.WARNING, logger="agents.db"):
        ctx = _build_ssl_context(None)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert any("no CA" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_pg_tls_with_an_unloadable_ca_still_verifies(monkeypatch, caplog):
    monkeypatch.delenv("PG_SSL_INSECURE", raising=False)
    bogus = "-----BEGIN CERTIFICATE-----\nnot a cert\n-----END CERTIFICATE-----\n"
    with caplog.at_level(logging.WARNING, logger="agents.db"):
        ctx = _build_ssl_context(bogus)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    # The PEM is never logged.
    assert "not a cert" not in caplog.text


def test_pg_tls_insecure_is_an_explicit_opt_out(monkeypatch, caplog):
    monkeypatch.setenv("PG_SSL_INSECURE", "1")
    with caplog.at_level(logging.WARNING, logger="agents.db"):
        ctx = _build_ssl_context(None)
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False
    assert any(
        "PG_SSL_INSECURE" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    )


@pytest.mark.parametrize("value", ["0", "", "true", "yes"])
def test_pg_tls_insecure_needs_exactly_1(monkeypatch, value):
    monkeypatch.setenv("PG_SSL_INSECURE", value)
    ctx = _build_ssl_context(None)
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_pg_tls_with_a_ca_verifies_against_it(monkeypatch, caplog):
    monkeypatch.delenv("PG_SSL_INSECURE", raising=False)
    with caplog.at_level(logging.WARNING, logger="agents.db"):
        ctx = _build_ssl_context(_self_signed_pem())
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    subjects = [dict(x[0] for x in c["subject"]) for c in ctx.get_ca_certs()]
    assert {"commonName": "test-pg-ca"} in subjects
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
