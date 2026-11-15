"""Throw-away keys, certificates and tokens for the tests of the self-hosted
stack (``agents/oidc.py``, ``agents/saml_bearer.py``,
``agents/local_destinations.py``). Everything is generated in the test."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ISSUER = "https://idp.example.test/realms/agents"
AUDIENCE = "agent-app"


def rsa_key(bits: int = 2048) -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def key_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def cert_pem(key: rsa.RSAPrivateKey, common_name: str = "agent-local-saml-idp") -> bytes:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def token(key: rsa.RSAPrivateKey, **claims) -> str:
    """An RS256 token of ``ISSUER`` for ``AUDIENCE``; ``claims`` override, a
    claim given as ``None`` is left out."""
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-1",
        "email": "dev@example.test",
        "exp": int(time.time()) + 600,
        "iat": int(time.time()),
        "realm_access": {"roles": ["user"]},
    }
    payload.update(claims)
    payload = {name: value for name, value in payload.items() if value is not None}
    return jwt.encode(payload, key, algorithm="RS256")


class StubKeys:
    """Stands in for ``PyJWKClient``: hands out one public key."""

    def __init__(self, key: rsa.RSAPrivateKey) -> None:
        self._public = key.public_key()

    def get_signing_key_from_jwt(self, _token: str):
        return SimpleNamespace(key=self._public)


def validator(key: rsa.RSAPrivateKey, **kwargs):
    from agents.oidc import OidcValidator

    made = OidcValidator(ISSUER, AUDIENCE, jwks_url="https://idp.example.test/certs", **kwargs)
    made._jwks_client = StubKeys(key)  # noqa: SLF001 - the seam of these tests
    return made
