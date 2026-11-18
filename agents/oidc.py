"""OpenID Connect token validation for a deployment without XSUAA.

On Cloud Foundry the approuter signs the user in and XSUAA issues the JWT
(``agents.auth.XsuaaValidator``). A self-hosted deployment (Docker, an
on-premise host) has neither: a reverse proxy that speaks OIDC (oauth2-proxy
in ``deploy/local``) signs the user in at any OIDC identity provider
(Keycloak, SAP IAS, Entra ID) and hands the access token to this app, which
validates it here. ``OidcValidator`` has the two methods the rest of the app
uses of a validator, ``validate`` and ``has_scope``, so every
``require_*`` dependency, the tier check and the IDE work unchanged.

Settings (read once, by ``from_environment``):

``OIDC_ISSUER``      the issuer, compared with the token's ``iss`` exactly.
                     Setting it switches OIDC on (an XSUAA binding wins).
``OIDC_AUDIENCE``    required: the token's ``aud`` must hold it. There is no
                     switch to skip the audience check.
``OIDC_JWKS_URL``    where the signing keys are read. Default: the
                     ``jwks_uri`` of ``<issuer>/.well-known/openid-configuration``.
                     Set it when the app reaches the provider under another
                     host name than the browser does (a compose network).
``OIDC_ROLES_CLAIM`` comma-separated dotted paths of the claims that hold the
                     user's roles; default ``realm_access.roles,roles,groups``.
                     A role named like a scope of ``xs-security.json``
                     (``admin``, ``developer``, ``user``, ``a2a``, ``invoke``,
                     ``JOBSCHEDULER``) grants it. The ``scope`` / ``scp``
                     claim is read as well.

Only asymmetric signatures are accepted (``ALGORITHMS``): a symmetric one
would make the public key a signing key.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any, Mapping
from urllib.parse import urlparse

import jwt
from fastapi import HTTPException, status
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384")
DEFAULT_ROLES_CLAIMS = "realm_access.roles,roles,groups"
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


class OidcConfigurationError(RuntimeError):
    """OIDC is switched on but its settings cannot be used."""


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return bool(str(env.get("OIDC_ISSUER") or "").strip())


def _dig(payload: Mapping[str, Any], path: str) -> Any:
    node: Any = payload
    for part in path.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
    return node


def _names(value: Any) -> set[str]:
    """Role or scope names out of a claim: a list of strings, or one string
    with the names separated by blanks (the ``scope`` claim's form)."""
    if isinstance(value, str):
        return {part for part in value.split() if part}
    if isinstance(value, (list, tuple)):
        return {item for item in value if isinstance(item, str) and item}
    return set()


def _discover_jwks_url(issuer: str) -> str:
    url = f"{issuer}/.well-known/openid-configuration"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
            document = json.loads(response.read(1_000_000))
        jwks = str(document.get("jwks_uri") or "").strip()
    except Exception as exc:  # noqa: BLE001 - reported by class, below
        raise OidcConfigurationError(
            f"OIDC discovery failed ({type(exc).__name__}); set OIDC_JWKS_URL "
            f"when the issuer is not reachable from this app under its own name"
        ) from None
    if not jwks:
        raise OidcConfigurationError("OIDC discovery answered no jwks_uri")
    return jwks


class OidcValidator:
    """Validates access tokens of one OIDC issuer; see the module docstring."""

    # Scopes are plain names here; the attribute exists because callers of
    # the XSUAA validator read it.
    xsappname = ""

    def __init__(
        self,
        issuer: str,
        audience: str,
        *,
        jwks_url: str = "",
        roles_claims: str = DEFAULT_ROLES_CLAIMS,
    ) -> None:
        self.issuer = issuer.strip()
        self.audience = audience.strip()
        if not self.issuer or not self.audience:
            raise OidcConfigurationError(
                "OIDC_ISSUER needs OIDC_AUDIENCE: a token is accepted only when "
                "it was issued for this application"
            )
        self._jwks_url = jwks_url.strip()
        self._jwks_client: PyJWKClient | None = None
        self.roles_claims = tuple(
            part.strip() for part in roles_claims.split(",") if part.strip()
        )
        for label, url in (("OIDC_ISSUER", self.issuer), ("OIDC_JWKS_URL", self._jwks_url)):
            parts = urlparse(url) if url else None
            if parts is not None and parts.scheme != "https" and (
                parts.hostname or ""
            ) not in _LOOPBACK:
                logger.warning(
                    "%s is not https: token keys are read over a connection "
                    "that is not protected (acceptable inside a compose "
                    "network only)", label,
                )

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "OidcValidator":
        env = os.environ if environ is None else environ
        return cls(
            str(env.get("OIDC_ISSUER") or ""),
            str(env.get("OIDC_AUDIENCE") or ""),
            jwks_url=str(env.get("OIDC_JWKS_URL") or ""),
            roles_claims=str(env.get("OIDC_ROLES_CLAIM") or "") or DEFAULT_ROLES_CLAIMS,
        )

    def _keys(self) -> PyJWKClient:
        if self._jwks_client is None:
            url = self._jwks_url or _discover_jwks_url(self.issuer.rstrip("/"))
            self._jwks_client = PyJWKClient(url, lifespan=3600, timeout=5)
        return self._jwks_client

    def validate(self, token: str) -> dict[str, Any]:
        """The claims of ``token``, or a 401 ``HTTPException`` (the XSUAA
        validator's contract). Blocking: call it in a worker thread."""
        try:
            signing_key = self._keys().get_signing_key_from_jwt(token).key
            payload = jwt.decode(
                token,
                signing_key,
                algorithms=list(ALGORITHMS),
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except OidcConfigurationError as exc:
            logger.error("OIDC: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Unable to verify JWT"
            ) from None
        except jwt.PyJWTError as exc:
            # The class only: PyJWT's text can quote claim values.
            logger.warning("OIDC token refused (%s)", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid JWT ({type(exc).__name__})",
            ) from None
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid JWT"
            )
        return payload

    def granted(self, payload: Mapping[str, Any]) -> set[str]:
        names: set[str] = set()
        for path in self.roles_claims:
            names |= _names(_dig(payload, path))
        for claim in ("scope", "scp"):
            names |= _names(payload.get(claim))
        return names

    def has_scope(self, payload: dict[str, Any], scope: str) -> bool:
        return scope in self.granted(payload)
