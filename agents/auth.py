"""XSUAA JWT authentication and JWT-forwarding context.

Provides:
- `current_jwt` contextvar — holds the bound user's JWT for the current request,
  read by the MCP httpx auth to forward it to MCP servers.
- `current_principal` / `current_claims` contextvars — the identity and the
  validated claims of the bound token, set once per request by
  ``JWTBindingMiddleware`` so the dependencies below never decode twice.
- `XsuaaValidator` — validates incoming JWTs against the XSUAA JWKS and checks
  required scopes.
- FastAPI dependencies `require_user`, `require_admin`, `require_a2a` and
  `require_jobscheduler`.

Fail-closed switch
------------------
``AUTH_REQUIRED`` decides what happens when no XSUAA binding can be resolved.
It defaults to ``true`` whenever ``VCAP_APPLICATION`` is set (a Cloud Foundry
container) and ``false`` otherwise (local development, tests). When it is true
and there is no ``xsuaa`` entry in ``VCAP_SERVICES``, ``get_validator`` raises
``AuthConfigurationError`` and the app refuses to start (``app.py`` lifespan)
instead of serving every route to everyone with a single warning line. Set
``AUTH_REQUIRED=false`` explicitly to run an unbound container open, e.g. a
throwaway Docker/Kyma deployment behind some other gate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import lru_cache
from typing import Any, AsyncIterator

import httpx
import jwt
from fastapi import HTTPException, Request, status
from jwt import PyJWKClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-request JWT context for MCP forwarding
# ---------------------------------------------------------------------------
current_jwt: ContextVar[str | None] = ContextVar("current_jwt", default=None)

# Stable identifier for the calling user, used to key per-user OAuth2 tokens
# (auth_mode="oauth2"). Derived from the validated XSUAA JWT on CF.
current_principal: ContextVar[str | None] = ContextVar("current_principal", default=None)

# Validated claims of the bound JWT, set by JWTBindingMiddleware after it has
# checked the signature/audience once. The FastAPI dependencies reuse these
# instead of decoding (and possibly refetching the JWKS) a second time. None
# means "not validated on this request" (no token, or no middleware in front).
current_claims: ContextVar[dict[str, Any] | None] = ContextVar("current_claims", default=None)

# Public base URL of the current request (scheme://host as seen by the
# approuter), used to build the OAuth2 redirect_uri for the callback.
current_base_url: ContextVar[str | None] = ContextVar("current_base_url", default=None)


def set_current_jwt(token: str | None) -> object:
    return current_jwt.set(token)


def reset_current_jwt(marker: object) -> None:
    current_jwt.reset(marker)  # type: ignore[arg-type]


def principal_from_token(token: str | None) -> str | None:
    """Derive a stable user id from the bound JWT.

    On CF the token is validated against XSUAA first (so the principal is
    cryptographically trustworthy); the user id is taken from ``user_uuid``,
    falling back to ``sub`` / ``user_name@origin`` / ``email``. Without an
    XSUAA binding (local dev) the claims are read from the unverified token,
    or a constant ``local-dev`` principal is used when there is no token, so
    the OAuth2 flow is still exercisable locally.
    """
    validator = get_validator()
    if token:
        try:
            if validator is not None:
                payload = validator.validate(token)
            else:
                payload = jwt.decode(token, options={"verify_signature": False})
        except Exception:
            logger.warning("Could not derive principal from token", exc_info=True)
            return None
        return _principal_claim(payload)
    return None if validator is not None else "local-dev"


class InvalidToken(Exception):
    """A bearer token was presented but did not validate."""


async def authenticate(token: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """Validate ``token`` once and return ``(claims, principal)``.

    The one place a request's token is checked: ``JWTBindingMiddleware`` calls
    it and stores both results in ``current_claims`` / ``current_principal``.
    Validation runs in a worker thread because ``PyJWKClient`` fetches the JWK
    set with a *blocking* urllib call; on the event loop one slow XSUAA round
    trip would stall every in-flight stream.

    - With an XSUAA binding: the signature, expiry and audience are verified;
      a token that fails raises ``InvalidToken`` (the caller answers 401).
    - Without one (local dev): the claims are read unverified, and a missing
      token maps to the constant ``local-dev`` principal, so the per-user
      OAuth2 flow stays exercisable locally.
    """
    validator = get_validator()
    if not token:
        return (None, None) if validator is not None else (None, "local-dev")
    if validator is None:
        try:
            payload = jwt.decode(token, options={"verify_signature": False})
        except Exception:
            logger.warning("Could not decode bearer token (dev mode)", exc_info=True)
            return None, None
        return payload, _principal_claim(payload)
    try:
        payload = await asyncio.to_thread(validator.validate, token)
    except HTTPException as e:
        raise InvalidToken(str(e.detail)) from e
    return payload, _principal_claim(payload)


def _principal_claim(payload: dict[str, Any]) -> str | None:
    for key in ("user_uuid", "sub"):
        v = payload.get(key)
        if v:
            return str(v)
    user_name = payload.get("user_name") or payload.get("email")
    if user_name:
        origin = payload.get("origin")
        return f"{user_name}@{origin}" if origin else str(user_name)
    return None


# Claims that identify one issued token; compared one by one below.
_TOKEN_IDENTITY_CLAIMS = ("jti", "iat", "exp", "iss", "user_uuid", "sub", "user_name", "origin")


def bound_token_principal() -> str | None:
    """The principal of the token bound to this context, or ``None``.

    For code that must name whose token is *sent* (an audit record), as
    opposed to ``current_principal``, which ``run_as`` rebinds to the run-as
    user while the trigger's token stays bound.

    The answer comes from ``current_claims`` -- the claims the middleware
    validated -- and only when they demonstrably belong to ``current_jwt``:
    the token is decoded WITHOUT verification (no JWKS fetch, no second
    validation) merely to compare its identifying claims with the bound
    ones. ``None`` when no token or no claims are bound, when the token
    cannot be decoded, when any identifying claim differs (stale claims
    next to another token), or when the claims name no principal. The
    unverified decode is never the source of the answer.
    """
    token = current_jwt.get()
    claims = current_claims.get()
    if not token or not isinstance(claims, dict):
        return None
    try:
        own = jwt.decode(token, options={"verify_signature": False})
    except Exception:  # noqa: BLE001 - not a JWT we can compare against
        return None
    if not isinstance(own, dict):
        return None
    if any(own.get(name) != claims.get(name) for name in _TOKEN_IDENTITY_CLAIMS):
        return None
    return _principal_claim(claims)


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
        # lifespan: how long the fetched JWK set is reused before another
        # blocking HTTPS round trip to XSUAA. PyJWT's default is 300s, which
        # buys nothing here: rotation is handled by `get_signing_key`, which
        # force-refreshes whenever a token names a `kid` the cached set does
        # not contain. An hour simply means 12x fewer refetches.
        # timeout: PyJWT defaults to 30s, long enough that an unreachable
        # XSUAA would stall the request rather than fail it. Five seconds is
        # past the point where the `verificationkey` fallback below is the
        # better answer.
        self.jwks_client = PyJWKClient(
            f"{uaa_url}/token_keys", lifespan=3600, timeout=5
        )
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
        except jwt.MissingRequiredClaimError as e:
            # XSUAA sometimes issues tokens without an `aud` claim at all
            # (notably client_credentials tokens). Those are accepted only when
            # the token demonstrably belongs to *this* app: its client_id is
            # ours, or it carries a scope under our xsappname. A token that
            # HAS an `aud` naming another app never reaches this branch --
            # PyJWT raises InvalidAudienceError for it, which is a 401 below.
            if getattr(e, "claim", None) != "aud":
                logger.warning("JWT validation failed: %s", e)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid JWT: {e}"
                )
            try:
                payload = jwt.decode(
                    token,
                    signing_key,
                    algorithms=["RS256"],
                    options={"verify_aud": False},
                )
            except jwt.PyJWTError as e2:
                logger.warning("JWT validation failed: %s", e2)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid JWT: {e2}"
                )
            if not self._issued_for_this_app(payload):
                logger.warning(
                    "JWT without aud rejected: client_id=%r is not ours and no "
                    "%s.* scope is present", payload.get("client_id"), self.xsappname,
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid JWT: token was not issued for this application",
                )
        except jwt.PyJWTError as e:
            logger.warning("JWT validation failed: %s", e)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid JWT: {e}"
            )

        return payload

    def _issued_for_this_app(self, payload: dict[str, Any]) -> bool:
        """Whether an ``aud``-less token belongs to this app.

        XSUAA writes the OAuth client into both ``client_id`` and ``cid``;
        either matching our client is proof enough. Failing that, a scope
        prefixed with our xsappname could only have been granted by our own
        xs-security.json.
        """
        for claim in ("client_id", "cid"):
            if payload.get(claim) == self.client_id:
                return True
        prefix = f"{self.xsappname}."
        scopes = payload.get("scope") or []
        return any(isinstance(sc, str) and sc.startswith(prefix) for sc in scopes)

    def has_scope(self, payload: dict[str, Any], scope: str) -> bool:
        scopes = payload.get("scope") or []
        full = f"{self.xsappname}.{scope}"
        return full in scopes or scope in scopes


_validator: XsuaaValidator | None = None
_validator_checked = False


class AuthConfigurationError(RuntimeError):
    """Raised when AUTH_REQUIRED is on but no XSUAA binding can be resolved."""


def auth_required() -> bool:
    """Whether the process must refuse to run without an XSUAA binding.

    ``AUTH_REQUIRED`` wins when set (``true``/``false``, case-insensitive);
    otherwise it is on exactly when ``VCAP_APPLICATION`` marks a CF container.
    """
    raw = (os.environ.get("AUTH_REQUIRED") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return "VCAP_APPLICATION" in os.environ


def get_validator() -> XsuaaValidator | None:
    """The process-wide validator, or None when running open (dev mode).

    Raises ``AuthConfigurationError`` instead of returning None when
    ``auth_required()`` is true, so a misbound container fails loudly (the
    lifespan calls this first) rather than serving every route to anyone.
    """
    global _validator, _validator_checked
    if _validator_checked:
        return _validator
    creds = get_xsuaa_credentials()
    if not creds and auth_required():
        raise AuthConfigurationError(
            "AUTH_REQUIRED is on (default on Cloud Foundry) but no XSUAA "
            "credentials were found in VCAP_SERVICES. Bind the xsuaa service "
            "instance, or set AUTH_REQUIRED=false to knowingly run without "
            "JWT validation."
        )
    _validator_checked = True
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
#
# ``JWTBindingMiddleware`` validates the bearer token once per request and
# leaves the claims in ``current_claims``; these reuse them. They only fall
# back to validating the token themselves when the middleware is not in front
# (a router mounted in a test, say). Deliberately `def`, not `async def`:
# that fallback reaches PyJWKClient, which fetches the JWK set with a
# *blocking* urllib call. Declared sync, FastAPI runs them in a threadpool
# and a slow refresh costs only the request that triggered it.
# ---------------------------------------------------------------------------
def _validated_claims(request: Request, validator: XsuaaValidator) -> dict[str, Any]:
    claims = current_claims.get()
    if claims is not None:
        return claims
    token = _extract_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token"
        )
    return validator.validate(token)


def _require_scope(request: Request, scope: str, detail: str) -> dict[str, Any]:
    validator = get_validator()
    if validator is None:
        return {"user_name": "local-dev", "scope": [scope]}
    payload = _validated_claims(request, validator)
    if not validator.has_scope(payload, scope):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
    return payload


def require_user(request: Request) -> dict[str, Any]:
    """Ensure a valid JWT is present (any authenticated user)."""
    validator = get_validator()
    if validator is None:
        # Dev mode (no XSUAA): allow and return anonymous user
        return {"user_name": "local-dev", "scope": []}
    return _validated_claims(request, validator)


def require_admin(request: Request) -> dict[str, Any]:
    """Ensure caller holds the `<xsappname>.admin` scope."""
    return _require_scope(request, "admin", "Admin scope required")


def require_developer(request: Request) -> dict[str, Any]:
    """Ensure the caller holds `<xsappname>.developer` (the ABAP IDE)."""
    return _require_scope(request, "developer", "Developer scope required")


def require_a2a(request: Request) -> dict[str, Any]:
    """Ensure the caller holds the `<xsappname>.a2a` scope.

    The approuter enforces the same scope on its `/a2a` route, but the backend
    has a public CF route of its own, so the check has to live here too.
    """
    return _require_scope(request, "a2a", "A2A scope required")


def require_jobscheduler(request: Request) -> dict[str, Any]:
    """Ensure the caller holds the `<xsappname>.JOBSCHEDULER` scope.

    Granted to the jobscheduler service instance via `grant-as-authority-to-apps`
    in xs-security.json, so only the scheduler can trigger runs.
    """
    return _require_scope(request, "JOBSCHEDULER", "Job scheduler scope required")


# ---------------------------------------------------------------------------
# Non-interactive identity for scheduled / API-triggered runs
# ---------------------------------------------------------------------------
# Env vars that can carry the app's public (approuter) URL, most specific
# first. A2A_PUBLIC_URL is the same approuter host under a different name and
# is already configured on deployments that expose the A2A endpoint, so it is
# a sound fallback rather than a second source of truth.
PUBLIC_BASE_URL_VARS = ("PUBLIC_BASE_URL", "A2A_PUBLIC_URL")


def public_base_url() -> str | None:
    """The app's public (approuter) URL, for code that has no request.

    The single resolver for this concept: the request middleware uses it as
    the override for the OAuth2 redirect_uri (falling back to the forwarded
    headers when neither var is set), and ``run_as`` uses it as the only
    source, since a scheduled run has no request to derive a host from.
    """
    for var in PUBLIC_BASE_URL_VARS:
        value = (os.environ.get(var) or "").strip().rstrip("/")
        if value:
            return value
    return None


@asynccontextmanager
async def run_as(principal: str) -> AsyncIterator[None]:
    """Bind a non-interactive identity for a scheduled or API-triggered run.

    Binds the principal and the base URL, and nothing else: ``current_jwt``
    is neither set nor cleared here. What token the run carries therefore
    depends on where it was started:

    * a run started outside a request, or by a request without a bearer
      token, has no JWT bound. It can reach servers on auth_mode "oauth2"
      (per-user token store, keyed by this principal), "none", or an
      app-level credential; "jwt" servers and destinations with
      ``user_context`` refuse it;
    * a run started from a request is a task created inside that request
      (``job_runner.start_run``; a workflow run behaves the same:
      ``workflow_runner.start_workflow_run`` creates its task in the request
      and each agent step enters ``run_as``), so it inherits whatever bearer token the
      request carried: the admin's own JWT for "Run now", the caller's token
      for an API trigger. In such a run ``current_principal`` is the agent's
      run-as user while ``current_jwt`` is the token of whoever triggered
      it: **the two can name different identities**. That is intended (the
      run keeps the trigger's token).

    Anything that caches per user must therefore not key on the principal
    alone when the cached thing was obtained with the bound token: key on the
    principal AND a digest of that token (``agents.destination._cache_key``,
    ``agents.outlook_tools.owner_cache_key``), or one user is served what
    another user's token fetched.

    current_base_url must be set for PerUserOAuth2Auth to resolve its DCR
    client, and no request exists to derive it from — hence public_base_url().
    """
    base_url = public_base_url()
    if not base_url:
        raise RuntimeError(
            "PUBLIC_BASE_URL (or A2A_PUBLIC_URL) must be set for "
            "API-triggered runs; there is no request to derive the callback "
            "URL from."
        )
    marker_principal = current_principal.set(principal)
    marker_base = current_base_url.set(base_url)
    try:
        yield
    finally:
        current_principal.reset(marker_principal)
        current_base_url.reset(marker_base)
