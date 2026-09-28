"""XsuaaValidator audience handling (review finding H1).

The old code retried with ``verify_aud=False`` on ``InvalidAudienceError``,
which is the exception PyJWT raises for a token whose ``aud`` names ANOTHER
app -- so any token signed by the tenant key was accepted. A token with no
``aud`` at all raises ``MissingRequiredClaimError`` instead. The fixed rule:

- ``aud`` contains our client id           -> accepted
- ``aud`` present but does not contain it  -> 401, always
- no ``aud``, ``client_id`` is ours        -> accepted
- no ``aud``, scope under our xsappname    -> accepted
- no ``aud``, foreign client, no scope     -> 401

Signing uses a throwaway RSA key injected as the binding's ``verificationkey``;
the JWKS client is stubbed to fail so the fallback path is what signs.

Run:  pytest tests/test_auth_validator.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from agents.auth import XsuaaValidator  # noqa: E402

CLIENT_ID = "sb-pydantic-agent-dev!t123"
XSAPPNAME = "pydantic-agent-dev!t123"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
_PUBLIC_PEM = _KEY.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()


class _NoJwks:
    """Stands in for PyJWKClient: no network, always fails the lookup."""

    def get_signing_key_from_jwt(self, token):  # noqa: ARG002
        raise RuntimeError("jwks unavailable in tests")


@pytest.fixture
def validator() -> XsuaaValidator:
    v = XsuaaValidator({
        "clientid": CLIENT_ID,
        "xsappname": XSAPPNAME,
        "url": "https://uaa.invalid",
        "verificationkey": _PUBLIC_PEM,
    })
    v.jwks_client = _NoJwks()  # type: ignore[assignment]
    return v


def _token(**claims) -> str:
    payload = {
        "sub": "user-1",
        "user_uuid": "user-1",
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
        **claims,
    }
    return jwt.encode(payload, _PRIVATE_PEM, algorithm="RS256", headers={"kid": "k1"})


def test_correct_audience_is_accepted(validator):
    payload = validator.validate(_token(aud=[CLIENT_ID, "openid"], scope=[f"{XSAPPNAME}.user"]))
    assert payload["sub"] == "user-1"


def test_wrong_audience_is_rejected(validator):
    # Same tenant key, another app's client id in aud: the exact token the
    # old fallback let through.
    tok = _token(aud=["sb-other-app!t123"], client_id="sb-other-app!t123",
                 scope=["other-app!t123.admin"])
    with pytest.raises(HTTPException) as ei:
        validator.validate(tok)
    assert ei.value.status_code == 401


def test_wrong_audience_with_our_client_id_claim_is_still_rejected(validator):
    # A mismatching aud is always a 401; the client_id fallback applies only
    # when aud is absent altogether.
    tok = _token(aud=["sb-other-app!t123"], client_id=CLIENT_ID)
    with pytest.raises(HTTPException) as ei:
        validator.validate(tok)
    assert ei.value.status_code == 401


def test_missing_audience_with_matching_client_id_is_accepted(validator):
    payload = validator.validate(_token(client_id=CLIENT_ID))
    assert payload["client_id"] == CLIENT_ID


def test_missing_audience_with_matching_cid_is_accepted(validator):
    payload = validator.validate(_token(cid=CLIENT_ID))
    assert payload["cid"] == CLIENT_ID


def test_missing_audience_with_our_scope_is_accepted(validator):
    payload = validator.validate(
        _token(client_id="sb-other-app!t123", scope=[f"{XSAPPNAME}.a2a"])
    )
    assert validator.has_scope(payload, "a2a")


def test_missing_audience_with_foreign_client_id_is_rejected(validator):
    tok = _token(client_id="sb-other-app!t123", scope=["other-app!t123.admin", "openid"])
    with pytest.raises(HTTPException) as ei:
        validator.validate(tok)
    assert ei.value.status_code == 401
    assert "not issued for this application" in str(ei.value.detail)


def test_missing_audience_and_no_client_claims_is_rejected(validator):
    with pytest.raises(HTTPException) as ei:
        validator.validate(_token())
    assert ei.value.status_code == 401


def test_expired_token_is_rejected_even_with_correct_audience(validator):
    tok = _token(aud=[CLIENT_ID], exp=int(time.time()) - 10)
    with pytest.raises(HTTPException) as ei:
        validator.validate(tok)
    assert ei.value.status_code == 401


def test_bad_signature_is_rejected(validator):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    tok = jwt.encode(
        {"sub": "x", "aud": [CLIENT_ID], "exp": int(time.time()) + 60},
        other_pem, algorithm="RS256",
    )
    with pytest.raises(HTTPException) as ei:
        validator.validate(tok)
    assert ei.value.status_code == 401


def test_unverifiable_without_fallback_key_is_401():
    v = XsuaaValidator({"clientid": CLIENT_ID, "xsappname": XSAPPNAME, "url": "https://uaa.invalid"})
    v.jwks_client = _NoJwks()  # type: ignore[assignment]
    with pytest.raises(HTTPException) as ei:
        v.validate(_token(aud=[CLIENT_ID]))
    assert ei.value.status_code == 401
