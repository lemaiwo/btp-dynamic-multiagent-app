"""What a failed OAuth2 token request tells the user and the log.

The sign-in page (``/oauth/callback``) shows the ``ValueError`` text of
``complete_authorization`` to any ``user``-scope browser. A token endpoint's
error body can name the client, the zone or what was sent, and an httpx
error's text names the token URL as sent. So both the page and the log get
the status plus the OAuth ``error`` code (when it has the form of one), or
the exception class: never the body, ``error_description`` or the exception
text. The same reduction as ``agents/destination.py``.

Run:  .venv/bin/python -m pytest tests/test_oauth2_errors.py -q
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import agents.oauth2 as oauth2  # noqa: E402
from agents.db import SessionLocal, init_db, upsert_user_token  # noqa: E402

SECRET = "SECRET-VALUE"
TOKEN_URL = f"https://as.example.test/oauth/token?client_secret={SECRET}"
CONFIG = oauth2.Oauth2Config(
    authorize_url="https://as.example.test/oauth/authorize",
    token_url=TOKEN_URL,
    client_id="client-1",
)
SERVER_KEY = "https://mcp-errors.example.test/mcp"
USER = "oauth-errors-user"


class _Resp:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else repr(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not JSON")
        return self._body


def _flow_patches(monkeypatch, post_token) -> None:
    flow = SimpleNamespace(
        user_id=USER, server_key=SERVER_KEY,
        redirect_uri="https://approuter.example.test/oauth/callback",
        code_verifier="v",
    )

    async def pop_state(session, state):  # noqa: ARG001
        return flow

    async def find_config(server_key):  # noqa: ARG001
        return CONFIG

    async def find_spec(server_key):  # noqa: ARG001
        return {}

    monkeypatch.setattr(oauth2, "pop_oauth_state", pop_state)
    monkeypatch.setattr(oauth2, "find_oauth_config", find_config)
    monkeypatch.setattr(oauth2, "find_oauth_spec", find_spec)
    monkeypatch.setattr(oauth2, "_post_token", post_token)


async def _complete_error(monkeypatch, post_token) -> str:
    _flow_patches(monkeypatch, post_token)
    with pytest.raises(ValueError) as info:
        await oauth2.complete_authorization(code="c", state="s", principal=USER)
    return str(info.value)


async def test_exchange_refusal_shows_status_and_code_only(monkeypatch, caplog):
    async def post(config, data):  # noqa: ARG001
        return _Resp(400, {
            "error": "invalid_grant",
            "error_description": f"code was issued to client-1 in zone {SECRET}",
        })

    caplog.set_level("DEBUG", logger="agents.oauth2")
    text = await _complete_error(monkeypatch, post)
    assert "400" in text and "invalid_grant" in text
    assert SECRET not in text and "client-1" not in text
    assert SECRET not in caplog.text


async def test_exchange_refusal_without_json_shows_status_only(monkeypatch):
    async def post(config, data):  # noqa: ARG001
        return _Resp(502, f"<html>proxy error for {TOKEN_URL}</html>")

    text = await _complete_error(monkeypatch, post)
    assert "502" in text
    assert SECRET not in text and "<html>" not in text and "https://" not in text


async def test_exchange_error_code_not_of_oauth_form_is_dropped(monkeypatch):
    async def post(config, data):  # noqa: ARG001
        return _Resp(400, {"error": f"see {TOKEN_URL}"})

    text = await _complete_error(monkeypatch, post)
    assert "400" in text
    assert SECRET not in text and "https://" not in text


async def test_exchange_transport_error_shows_class_only(monkeypatch, caplog):
    async def post(config, data):  # noqa: ARG001
        raise httpx.ConnectError(f"connection refused for {TOKEN_URL}")

    caplog.set_level("DEBUG", logger="agents.oauth2")
    text = await _complete_error(monkeypatch, post)
    assert "ConnectError" in text
    assert SECRET not in text and "https://" not in text
    assert SECRET not in caplog.text


async def _expired_token() -> None:
    await init_db()
    async with SessionLocal() as s:
        await upsert_user_token(
            s, user_id=USER, server_key=SERVER_KEY, access_token="OLD",
            refresh_token="R-1",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        )


async def test_refresh_rejection_logs_status_and_code_only(monkeypatch, caplog):
    await _expired_token()

    async def post(config, data):  # noqa: ARG001
        return _Resp(400, {"error": "invalid_grant", "error_description": SECRET})

    monkeypatch.setattr(oauth2, "_post_token", post)
    caplog.set_level("DEBUG", logger="agents.oauth2")
    auth = oauth2.PerUserOAuth2Auth(SERVER_KEY, {})
    assert await auth._refresh(USER, "R-1", CONFIG) == (None, "Bearer")
    assert "400" in caplog.text and "invalid_grant" in caplog.text
    assert SECRET not in caplog.text


async def test_refresh_transport_error_logs_class_only(monkeypatch, caplog):
    await _expired_token()

    async def post(config, data):  # noqa: ARG001
        raise httpx.ConnectError(f"connection refused for {TOKEN_URL}")

    monkeypatch.setattr(oauth2, "_post_token", post)
    caplog.set_level("DEBUG", logger="agents.oauth2")
    auth = oauth2.PerUserOAuth2Auth(SERVER_KEY, {})
    assert await auth._refresh(USER, "R-1", CONFIG) == (None, "Bearer")
    assert "ConnectError" in caplog.text
    assert SECRET not in caplog.text


async def test_refresh_success_unchanged(monkeypatch):
    await _expired_token()

    async def post(config, data):  # noqa: ARG001
        assert data == {"grant_type": "refresh_token", "refresh_token": "R-1"}
        return _Resp(200, {"access_token": "NEW", "refresh_token": "R-2", "expires_in": 3600})

    monkeypatch.setattr(oauth2, "_post_token", post)
    auth = oauth2.PerUserOAuth2Auth(SERVER_KEY, {})
    assert await auth._refresh(USER, "R-1", CONFIG) == ("NEW", "Bearer")
