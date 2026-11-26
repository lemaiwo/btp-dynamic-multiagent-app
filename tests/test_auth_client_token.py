"""``agents.auth.is_client_token``: a validated token that names an OAuth
client and no user (a client-credentials token of a binding or service key).

The invoke API runs such a caller under the agent's run-as identity; every
other caller keeps its own identity. A token that carries any user claim is
never a client token, whatever else it says.

Run:  .venv/bin/python -m pytest tests/test_auth_client_token.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.auth import is_client_token  # noqa: E402

CLIENT = "sb-app!t1"

# What XSUAA issues for grant_type=client_credentials: the client in sub,
# cid and client_id, no user claim at all.
CLIENT_TOKEN = {
    "sub": CLIENT, "cid": CLIENT, "client_id": CLIENT,
    "grant_type": "client_credentials", "scope": ["uaa.resource"],
}
USER_TOKEN = {
    "sub": "u-1", "user_uuid": "u-1", "user_name": "alice", "email": "a@example.com",
    "origin": "idp", "cid": CLIENT, "client_id": CLIENT,
    "grant_type": "authorization_code", "scope": ["openid"],
}


def without(claims: dict, *names: str) -> dict:
    return {k: v for k, v in claims.items() if k not in names}


@pytest.mark.parametrize("claims", [
    CLIENT_TOKEN,
    without(CLIENT_TOKEN, "grant_type"),             # sub == cid
    without(CLIENT_TOKEN, "grant_type", "cid"),      # sub == client_id
    without(CLIENT_TOKEN, "cid", "client_id"),       # the grant type says so
])
def test_a_client_credentials_token_is_a_client_token(claims):
    assert is_client_token(claims) is True


@pytest.mark.parametrize("claims", [
    USER_TOKEN,
    # jwt-bearer: a user token exchanged by an app, still a user.
    USER_TOKEN | {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer"},
    # Any one user claim makes it a user's token, even with sub == cid or the
    # client-credentials grant type.
    CLIENT_TOKEN | {"user_name": "alice"},
    CLIENT_TOKEN | {"user_uuid": "u-1"},
    CLIENT_TOKEN | {"email": "a@example.com"},
    # No user claim but nothing says "client" either: not decided as one.
    {"sub": "x", "cid": CLIENT, "scope": []},
    {"scope": ["uaa.resource"]},
    # A blank subject never equals a blank client id.
    {"sub": "", "cid": "", "client_id": ""},
])
def test_anything_else_is_not(claims):
    assert is_client_token(claims) is False


@pytest.mark.parametrize("claims", [None, {}, "not-a-dict", ["sub"]])
def test_no_claims_is_not_a_client_token(claims):
    assert is_client_token(claims) is False


# ---------------------------------------------------------------------------
# run_as: the base URL a run started inside a request may fall back to
# ---------------------------------------------------------------------------
async def test_run_as_falls_back_to_the_given_base_url(monkeypatch):
    from agents.auth import current_base_url, current_principal, run_as

    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("A2A_PUBLIC_URL", raising=False)
    async with run_as("svc", base_url="http://local.test/"):
        assert (current_principal.get(), current_base_url.get()) == ("svc", "http://local.test")
    assert current_principal.get() is None


async def test_run_as_prefers_the_configured_public_url(monkeypatch):
    from agents.auth import current_base_url, run_as

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://hub.example.test")
    async with run_as("svc", base_url="http://forwarded.test"):
        assert current_base_url.get() == "https://hub.example.test"


@pytest.mark.parametrize("base_url", [None, "", "  "])
async def test_run_as_without_any_base_url_still_refuses(monkeypatch, base_url):
    from agents.auth import run_as

    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("A2A_PUBLIC_URL", raising=False)
    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
        async with run_as("svc", base_url=base_url):
            pass
