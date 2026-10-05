"""Save-time rules that need no database: URL host checks, tool names, slugs.

The MCP URL decides where every chat user's XSUAA token is sent for a `jwt`
server, so the host check has to be decided on the parsed hostname, by DNS
label, never on the raw string.
"""

from __future__ import annotations

import os
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
os.environ["MCP_URL_ALLOWLIST"] = ""
os.environ.setdefault("AICORE_AVAILABLE_MODELS", "gpt-4o")

from agents.admin import AgentPayload, McpServerPayload, WorkflowPayload  # noqa: E402
from agents.db import delegation_tool_name, validate_api_slug  # noqa: E402


def _server(url: str, mode: str = "jwt", oauth: dict | None = None) -> McpServerPayload:
    return McpServerPayload(url=url, auth_mode=mode, oauth=oauth)


# --- default rule: *.hana.ondemand.com by DNS label -------------------------
@pytest.mark.parametrize("url", [
    "https://evil.com#.hana.ondemand.com",            # fragment fooled endswith
    "https://allowed.hana.ondemand.com.evil.com/",    # suffix is not a label
    "https://allowed.hana.ondemand.com@evil.com/",    # userinfo: host is evil.com
    "https://xhana.ondemand.com",                     # not a label boundary
    "https://example.com",
    "https://",
    "https://x.hana.ondemand.com:abc/",
    "http://x.hana.ondemand.com",                     # jwt over http
])
def test_default_rule_rejects_lookalike_hosts(url):
    with pytest.raises(ValidationError):
        _server(url)


@pytest.mark.parametrize("url", [
    "https://foo.cfapps.eu20-001.hana.ondemand.com",
    "https://foo.cfapps.eu20-001.hana.ondemand.com/mcp/",
    "https://hana.ondemand.com",
    "https://FOO.HANA.ondemand.com/mcp",
])
def test_default_rule_accepts_btp_hosts(url):
    assert _server(url).url == url.rstrip("/")


def test_public_servers_may_use_http_but_never_userinfo():
    assert _server("http://example.com/mcp", "none").url == "http://example.com/mcp"
    with pytest.raises(ValidationError):
        _server("http://a@example.com/mcp", "none")
    with pytest.raises(ValidationError):
        _server("http://example.com/mcp#x", "none")


# --- MCP_URL_ALLOWLIST: scheme, host, port, path segment ---------------------
ALLOWLIST = "https://allowed.hana.ondemand.com/mcp, .corp.example, other.example:8443"


@pytest.mark.parametrize("url", [
    "https://allowed.hana.ondemand.com/mcp",
    "https://allowed.hana.ondemand.com/mcp/v1",
    "https://ALLOWED.hana.ondemand.com/mcp",
    "https://a.b.corp.example/anything",
    "https://corp.example",
    "https://other.example:8443/",
])
def test_allowlist_accepts_matching_entries(monkeypatch, url):
    monkeypatch.setenv("MCP_URL_ALLOWLIST", ALLOWLIST)
    _server(url)


@pytest.mark.parametrize("url", [
    "https://allowed.hana.ondemand.com/mcp-evil",     # not a path segment
    "https://allowed.hana.ondemand.com/other",
    "https://allowed.hana.ondemand.com.evil.com/mcp", # raw prefix used to pass
    "https://allowed.hana.ondemand.com@evil.com/mcp", # raw prefix used to pass
    "https://allowed.hana.ondemand.com:8443/mcp",     # port differs
    "https://evil.com#https://allowed.hana.ondemand.com/mcp",
    "https://other.example/",                         # port differs
    "https://notcorp.example/",                       # label suffix only
])
def test_allowlist_rejects_lookalikes(monkeypatch, url):
    monkeypatch.setenv("MCP_URL_ALLOWLIST", ALLOWLIST)
    # Userinfo and fragments are refused before the allowlist is consulted;
    # everything else is refused by it.
    with pytest.raises(ValidationError, match="MCP_URL_ALLOWLIST|credentials|fragment"):
        _server(url)


# --- oauth block endpoints -----------------------------------------------------
BTP = "https://x.cfapps.eu20.hana.ondemand.com/mcp"


def test_oauth_endpoints_must_be_https_without_userinfo_or_fragment():
    base = {"client_id": "a", "client_secret": "b"}
    with pytest.raises(ValidationError, match="oauth.token_url"):
        _server(BTP, "oauth2", {**base, "authorize_url": "https://a/x", "token_url": "http://t/x"})
    with pytest.raises(ValidationError, match="oauth.uaa_url"):
        _server(BTP, "oauth2", {**base, "uaa_url": "https://u@evil.example/x"})
    with pytest.raises(ValidationError, match="oauth.authorize_url"):
        _server(BTP, "oauth2", {**base, "authorize_url": "https://a/x#f", "token_url": "https://t/x"})
    with pytest.raises(ValidationError, match="oauth.token_url"):
        _server("builtin:outlook", "app_only",
                {**base, "token_url": "http://t/x", "mailbox": "m@x"})
    ok = _server(BTP, "oauth2", {**base, "uaa_url": "https://x.authentication.eu10.hana.ondemand.com"})
    assert ok.oauth is not None and ok.oauth.uaa_url.startswith("https://")


# --- delegation tool names -------------------------------------------------------
def test_delegation_tool_name_collapses_case_space_and_punctuation():
    assert delegation_tool_name("Foo Bar") == "delegate_foo_bar"
    assert delegation_tool_name(" Foo Bar ") == "delegate_foo_bar"
    assert delegation_tool_name("foo_bar") == "delegate_foo_bar"
    assert delegation_tool_name("FOO-BAR") == "delegate_foo_bar"
    assert delegation_tool_name("   ") == "delegate_agent"


def test_delegation_tool_name_fits_the_64_char_function_limit():
    long_name = "a" * 64
    tool = delegation_tool_name(long_name)
    assert len(tool) == 64
    assert tool.startswith("delegate_")
    # Deterministic: two names that differ only past the cut collide, which
    # is exactly what the save-time collision check has to see.
    assert delegation_tool_name("a" * 60 + "b" * 4) == tool
    # The registry's own helper is the same mapping.
    from agents.registry import _sanitize_tool_name

    assert _sanitize_tool_name(long_name) == tool


def test_agent_payload_strips_the_name():
    p = AgentPayload(
        name="  Foo Bar ", description="d", instructions="i",
        mcp_servers=[{"url": "https://x.example.com/mcp", "auth_mode": "none"}],
    )
    assert p.name == "Foo Bar"


# --- api_slug ----------------------------------------------------------------------
def test_api_slug_charset():
    assert validate_api_slug(" mail-triage ") == "mail-triage"
    assert validate_api_slug("") is None
    assert validate_api_slug(None) is None
    for bad in ("a/b", "-x", "Upper", "a b", "a" * 65):
        with pytest.raises(ValueError, match="api_slug"):
            validate_api_slug(bad)


def test_payloads_reject_a_malformed_slug():
    servers = [{"url": "https://x.example.com/mcp", "auth_mode": "none"}]
    with pytest.raises(ValidationError, match="api_slug"):
        AgentPayload(name="a", description="d", instructions="i",
                     mcp_servers=servers, api_slug="a/b")
    with pytest.raises(ValidationError, match="api_slug"):
        WorkflowPayload(name="w", api_slug="Bad Slug")
    assert AgentPayload(name="a", description="d", instructions="i",
                        mcp_servers=servers, api_slug=None).api_slug == ""
