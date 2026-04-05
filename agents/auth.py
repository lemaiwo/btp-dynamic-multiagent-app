"""XSUAA JWT authentication and JWT-forwarding context.

Provides:
- `current_jwt` contextvar — holds the bound user's JWT for the current request,
  read by the MCP httpx auth to forward it to MCP servers.
- `XsuaaValidator` — validates incoming JWTs against the XSUAA JWKS and checks
  required scopes.
- FastAPI dependencies `require_user` and `require_admin`.
"""

from __future__ import annotations

import json
import logging
import os
from contextvars import ContextVar
from functools import lru_cache
from typing import Any

import httpx
import jwt
from fastapi import HTTPException, Request, status
from jwt import PyJWKClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-request JWT context for MCP forwarding
# ---------------------------------------------------------------------------
current_jwt: ContextVar[str | None] = ContextVar("current_jwt", default=None)


def set_current_jwt(token: str | None) -> object:
    return current_jwt.set(token)


def reset_current_jwt(marker: object) -> None:
    current_jwt.reset(marker)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# XSUAA credentials from VCAP_SERVICES
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def get_xsuaa_credentials() -> dict[str, Any] | None:
    vcap = os.environ.get("VCAP_SERVICES")
    if not vcap:
        return None
    try:
        services = json.loads(vcap)
    except Exception:
        logger.exception("Failed to parse VCAP_SERVICES")
        return None
    xsuaa = services.get("xsuaa") or []
    if not xsuaa:
        return None
    return xsuaa[0].get("credentials")


def get_xsappname() -> str:
    creds = get_xsuaa_credentials()
    if creds:
        return creds.get("xsappname", "pydantic-agent")
    return os.environ.get("XSAPPNAME", "pydantic-agent")


# ---------------------------------------------------------------------------
# JWT validation
# ---------------------------------------------------------------------------
class XsuaaValidator:
    """Validates XSUAA-issued JWTs against the tenant's JWKS endpoint."""

    def __init__(self, credentials: dict[str, Any]):
        self.credentials = credentials
        self.client_id = credentials["clientid"]
        self.xsappname = credentials.get("xsappname", "pydantic-agent")
        uaa_url = credentials.get("url", "").rstrip("/")
        self.uaa_url = uaa_url
        self.jwks_client = PyJWKClient(f"{uaa_url}/token_keys")
        verification_key = credentials.get("verificationkey")
        self._verification_key = verification_key

    def validate(self, token: str) -> dict[str, Any]:
        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(token).key
        except Exception as e:
            logger.warning("JWKS lookup failed, falling back to verificationkey: %s", e)
            if not self._verification_key:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Unable to verify JWT",
                )
            signing_key = self._verification_key

        try:
            payload = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=self.client_id,
                options={"verify_aud": True},
            )
        except jwt.InvalidAudienceError:
            # XSUAA sometimes issues without 'aud'; retry with aud check disabled
            payload = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                options={"verify_aud": False},
            )
        except jwt.PyJWTError as e:
            logger.warning("JWT validation failed: %s", e)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid JWT: {e}"
            )

        return payload

    def has_scope(self, payload: dict[str, Any], scope: str) -> bool:
        scopes = payload.get("scope") or []
        full = f"{self.xsappname}.{scope}"
        return full in scopes or scope in scopes


_validator: XsuaaValidator | None = None
_validator_checked = False


def get_validator() -> XsuaaValidator | None:
    global _validator, _validator_checked
    if _validator_checked:
        return _validator
    _validator_checked = True
    creds = get_xsuaa_credentials()
    if creds:
        _validator = XsuaaValidator(creds)
    else:
        logger.warning("No XSUAA binding found; running without JWT validation (dev mode)")
    return _validator


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------
async def require_user(request: Request) -> dict[str, Any]:
    """Ensure a valid JWT is present (any authenticated user)."""
    validator = get_validator()
    token = _extract_token(request)

    if validator is None:
        # Dev mode (no XSUAA): allow and return anonymous user
        return {"user_name": "local-dev", "scope": []}

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token"
        )
    return validator.validate(token)


async def require_admin(request: Request) -> dict[str, Any]:
    """Ensure caller holds the `<xsappname>.admin` scope."""
    validator = get_validator()
    token = _extract_token(request)

    if validator is None:
        return {"user_name": "local-dev", "scope": ["admin"]}

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token"
        )
    payload = validator.validate(token)
    if not validator.has_scope(payload, "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin scope required",
        )
    return payload
