"""require_developer: the `<xsappname>.developer` scope gates the ABAP IDE.

Run:  pytest tests/test_auth_developer.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException, Request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from agents import auth  # noqa: E402


class _StubValidator:
    """Maps bearer token -> claims; scopes are plain `developer`/`user` names."""

    def __init__(self, tokens):
        self.tokens = tokens

    def validate(self, token):
        from fastapi import HTTPException as H

        if token not in self.tokens:
            raise H(status_code=401, detail="Invalid token")
        return self.tokens[token]

    def has_scope(self, payload, scope):
        return scope in payload.get("scope", [])


def _req(token=None):
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    return Request({"type": "http", "headers": headers, "method": "GET", "path": "/ide/api/me"})


@pytest.fixture(autouse=True)
def _no_bound_claims():
    t = auth.current_claims.set(None)
    yield
    auth.current_claims.reset(t)


@pytest.fixture
def validator(monkeypatch):
    v = _StubValidator({
        "dev": {"user_name": "d", "scope": ["developer", "user"]},
        "usr": {"user_name": "u", "scope": ["user"]},
    })
    monkeypatch.setattr(auth, "get_validator", lambda: v)
    return v


def test_developer_scope_returns_claims(validator):
    assert auth.require_developer(_req("dev"))["user_name"] == "d"


def test_user_only_is_403(validator):
    with pytest.raises(HTTPException) as e:
        auth.require_developer(_req("usr"))
    assert e.value.status_code == 403
    assert e.value.detail == "Developer scope required"


def test_no_token_is_401(validator):
    with pytest.raises(HTTPException) as e:
        auth.require_developer(_req())
    assert e.value.status_code == 401


def test_invalid_token_is_401(validator):
    with pytest.raises(HTTPException) as e:
        auth.require_developer(_req("bogus"))
    assert e.value.status_code == 401


def test_middleware_bound_claims_are_checked_too(validator):
    t = auth.current_claims.set({"user_name": "u", "scope": ["user"]})
    try:
        with pytest.raises(HTTPException) as e:
            auth.require_developer(_req("dev"))
        assert e.value.status_code == 403
    finally:
        auth.current_claims.reset(t)


def test_dev_mode_without_validator(monkeypatch):
    monkeypatch.setattr(auth, "get_validator", lambda: None)
    out = auth.require_developer(_req())
    assert out["user_name"] == "local-dev"
    assert "developer" in out["scope"]
