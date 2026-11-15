"""OIDC token validation in place of XSUAA (``agents/oidc.py``) and its
wiring in ``agents/auth.py``."""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi import HTTPException

from agents import auth, oidc
from tests import local_stack_helpers as helpers


@pytest.fixture(scope="module")
def key():
    return helpers.rsa_key()


@pytest.fixture
def no_validator(monkeypatch):
    """``get_validator`` decides afresh, and is reset afterwards."""
    monkeypatch.setattr(auth, "_validator", None)
    monkeypatch.setattr(auth, "_validator_checked", False)
    auth.get_xsuaa_credentials.cache_clear()
    yield
    auth.get_xsuaa_credentials.cache_clear()


def test_valid_token_yields_claims_and_roles(key):
    validator = helpers.validator(key)
    claims = validator.validate(
        helpers.token(key, realm_access={"roles": ["admin", "developer"]}, scope="openid invoke")
    )
    assert claims["sub"] == "user-1"
    assert validator.has_scope(claims, "admin")
    assert validator.has_scope(claims, "developer")
    assert validator.has_scope(claims, "invoke")  # the scope claim counts too
    assert not validator.has_scope(claims, "a2a")


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "another-app"},
        {"aud": None},
        {"iss": "https://evil.example.test/realms/agents"},
        {"exp": int(time.time()) - 5},
        {"exp": None},
    ],
)
def test_wrong_audience_issuer_or_expiry_is_refused(key, claims):
    validator = helpers.validator(key)
    with pytest.raises(HTTPException) as refused:
        validator.validate(helpers.token(key, **claims))
    assert refused.value.status_code == 401


def test_token_signed_by_another_key_is_refused(key):
    validator = helpers.validator(key)
    with pytest.raises(HTTPException):
        validator.validate(helpers.token(helpers.rsa_key()))


def test_symmetric_and_unsigned_tokens_are_refused(key):
    validator = helpers.validator(key)
    payload = {"iss": helpers.ISSUER, "aud": helpers.AUDIENCE, "sub": "x",
               "exp": int(time.time()) + 60}
    for forged in (
        jwt.encode(payload, "a-shared-secret-of-sufficient-length-123", algorithm="HS256"),
        jwt.encode(payload, None, algorithm="none"),
    ):
        with pytest.raises(HTTPException):
            validator.validate(forged)


def test_refusal_does_not_quote_the_token_or_its_claims(key):
    validator = helpers.validator(key)
    with pytest.raises(HTTPException) as refused:
        validator.validate(helpers.token(key, aud="secret-audience-value"))
    assert "secret-audience-value" not in str(refused.value.detail)


def test_roles_claim_is_configurable(key):
    validator = helpers.validator(key, roles_claims="resource_access.agent-app.roles")
    claims = validator.validate(
        helpers.token(key, realm_access=None, **{"resource_access": {"agent-app": {"roles": ["admin"]}}})
    )
    assert validator.has_scope(claims, "admin")
    assert not validator.has_scope(claims, "user")


def test_audience_is_required():
    with pytest.raises(oidc.OidcConfigurationError):
        oidc.OidcValidator(helpers.ISSUER, "")


def test_get_validator_uses_oidc_without_xsuaa(monkeypatch, no_validator):
    monkeypatch.setenv("OIDC_ISSUER", helpers.ISSUER)
    monkeypatch.setenv("OIDC_AUDIENCE", helpers.AUDIENCE)
    monkeypatch.setenv("OIDC_JWKS_URL", "https://idp.example.test/certs")
    assert auth.oidc_enabled() and auth.auth_enforced()
    assert isinstance(auth.get_validator(), oidc.OidcValidator)


def test_oidc_without_audience_fails_the_start(monkeypatch, no_validator):
    monkeypatch.setenv("OIDC_ISSUER", helpers.ISSUER)
    monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
    with pytest.raises(auth.AuthConfigurationError):
        auth.get_validator()


def test_without_oidc_nothing_changes(monkeypatch, no_validator):
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    assert not auth.oidc_enabled()
    assert auth.get_validator() is None


async def test_authenticate_and_dependencies_with_oidc(monkeypatch, key, no_validator):
    monkeypatch.setattr(auth, "_validator", helpers.validator(key))
    monkeypatch.setattr(auth, "_validator_checked", True)
    claims, principal = await auth.authenticate(
        helpers.token(key, realm_access={"roles": ["admin"]})
    )
    assert principal == "user-1"
    marker = auth.current_claims.set(claims)
    try:
        assert auth.require_admin(None)["sub"] == "user-1"  # type: ignore[arg-type]
        with pytest.raises(HTTPException) as refused:
            auth.require_developer(None)  # type: ignore[arg-type]
        assert refused.value.status_code == 403
    finally:
        auth.current_claims.reset(marker)
    with pytest.raises(auth.InvalidToken):
        await auth.authenticate(helpers.token(key, aud="another-app"))
    assert await auth.authenticate(None) == (None, None)


def test_token_header_wins_over_authorization(monkeypatch):
    from starlette.requests import Request

    def request(headers: dict[str, str]) -> Request:
        raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        return Request({"type": "http", "headers": raw})

    both = {"Authorization": "Bearer from-authorization", "X-Forwarded-Access-Token": "from-proxy"}
    monkeypatch.delenv("AUTH_TOKEN_HEADER", raising=False)
    assert auth._extract_token(request(both)) == "from-authorization"
    monkeypatch.setenv("AUTH_TOKEN_HEADER", "X-Forwarded-Access-Token")
    assert auth._extract_token(request(both)) == "from-proxy"
    assert auth._extract_token(request({"Authorization": "Bearer api"})) == "api"
