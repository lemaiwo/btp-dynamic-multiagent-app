"""Storage-normalization rules for server config blocks."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402,F401

from agents.db import _clean_oauth  # noqa: E402


def test_none_mode_builtin_keeps_public_config():
    cleaned = _clean_oauth(
        {"min_score": "9.0", "lookback": "45d"}, "none", None, url="builtin:sapnotes"
    )
    assert cleaned == {"min_score": "9.0", "lookback": "45d"}


def test_none_mode_builtin_drops_credential_keys():
    """Same promise as destination mode: nothing secret lands in the DB."""
    cleaned = _clean_oauth(
        {"min_score": "9.0", "client_secret": "hunter2", "api_key": "abc"},
        "none",
        None,
        url="builtin:sapnotes",
    )
    assert cleaned == {"min_score": "9.0"}


def test_none_mode_real_mcp_url_still_carries_no_config():
    """Only built-ins get a config block on 'none'; a real MCP server has
    nothing to configure and should not gain a place to hide settings."""
    assert _clean_oauth({"min_score": "9.0"}, "none", None, url="https://x.example/mcp") is None


def test_none_mode_without_a_url_carries_no_config():
    assert _clean_oauth({"min_score": "9.0"}, "none", None) is None


def test_jwt_mode_carries_no_config():
    assert _clean_oauth({"min_score": "9.0"}, "jwt", None, url="builtin:sapnotes") is None


def test_client_credentials_stores_recipients():
    cleaned = _clean_oauth(
        {"client_id": "a", "client_secret": "b", "token_url": "https://t.example",
         "mailbox": "agent@example.com", "recipients": "team@example.com, basis@example.com"},
        "app_only",
        None,
        url="builtin:outlook",
    )
    assert cleaned["recipients"] == "team@example.com, basis@example.com"
    assert cleaned["allow_send"] is False


def test_session_mode_carries_no_config_block():
    """The cookie is a credential and lives in mcp_oauth_tokens, not here."""
    assert _clean_oauth({"cookie": "x"}, "session", None, url="builtin:sapnotedetail") is None


OAUTH2_BASE = {"client_id": "cid", "client_secret": "sec", "uaa_url": "https://uaa.example"}


def test_oauth2_outlook_keeps_send_settings():
    """The UI5 dialog sends lookback/recipients/allow_send for an oauth2 Outlook
    server, and outlook_tools honours them; storage must not drop them."""
    cleaned = _clean_oauth(
        {**OAUTH2_BASE, "lookback": "2d", "recipients": ["a@x.example", " b@x.example "],
         "allow_send": True},
        "oauth2", None, url="builtin:outlook",
    )
    assert cleaned["lookback"] == "2d"
    assert cleaned["recipients"] == "a@x.example, b@x.example"
    assert cleaned["allow_send"] is True
    assert cleaned["client_secret"] == "sec"


def test_oauth2_outlook_allow_send_defaults_off_and_blanks_are_dropped():
    cleaned = _clean_oauth(
        {**OAUTH2_BASE, "lookback": "", "recipients": ""}, "oauth2", None,
        url="builtin:outlook",
    )
    assert cleaned["allow_send"] is False
    assert "lookback" not in cleaned and "recipients" not in cleaned


def test_oauth2_non_outlook_server_still_drops_send_settings():
    cleaned = _clean_oauth(
        {**OAUTH2_BASE, "lookback": "2d", "recipients": "a@x.example", "allow_send": True},
        "oauth2", None, url="https://mcp.example.hana.ondemand.com/mcp",
    )
    assert "lookback" not in cleaned and "recipients" not in cleaned
    assert "allow_send" not in cleaned


def test_oauth2_gmail_keeps_allow_send():
    """builtin:gmail gains a send tool only with allow_send, so storage must
    keep the switch on an oauth2 Gmail server -- and only as a real true."""
    on = _clean_oauth({**OAUTH2_BASE, "allow_send": True}, "oauth2", None,
                      url="builtin:gmail")
    assert on["allow_send"] is True
    assert on["client_secret"] == "sec"
    off = _clean_oauth({**OAUTH2_BASE}, "oauth2", None, url="builtin:gmail")
    assert off["allow_send"] is False
    stringy = _clean_oauth({**OAUTH2_BASE, "allow_send": "true"}, "oauth2", None,
                           url="builtin:gmail")
    assert stringy["allow_send"] is False
    assert "recipients" not in _clean_oauth(
        {**OAUTH2_BASE, "recipients": "a@x.example"}, "oauth2", None, url="builtin:gmail"
    )
