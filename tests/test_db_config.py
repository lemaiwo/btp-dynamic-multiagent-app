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
