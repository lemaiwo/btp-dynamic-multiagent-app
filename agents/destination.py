"""Reaching a service through a BTP destination.

The destination service holds the target's URL and its credential, and -- for
an ``OAuth2ClientCredentials`` destination -- performs the token exchange
itself, handing back a ready ``Authorization`` header. That is the whole
appeal: this application stores no credential for the target at all, and
rotating it is something the destination's owner does without touching us.

Contrast :mod:`agents.client_credentials`, which authenticates *as* the
application and therefore must hold a client secret in our own database. Here
the only thing configured is a name.

A destination can also act **as the signed-in user**. Sending that user's
XSUAA JWT as ``X-user-token`` makes an ``OAuth2UserTokenExchange``,
``OAuth2JWTBearer``, ``OAuth2SAMLBearerAssertion`` or ``PrincipalPropagation``
destination hand back a token *for that user*, which is how a built-in can
read a person's mailbox or post under their name without this app holding a
per-user refresh token. Those results are cached per principal, separately
from the app-level result, and bounded.

This module knows nothing about what sits behind the destination.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

logger = logging.getLogger(__name__)

# Refetch this many seconds before the token actually expires, so a request
# that takes a moment to reach the server does not arrive holding a token that
# expired in flight.
EXPIRY_SKEW_SECONDS = 60

# Used when the destination response omits expires_in. Short on purpose:
# re-resolving is one cheap call, and guessing long risks 401 loops.
DEFAULT_LIFETIME_SECONDS = 300

DESTINATION_PATH = "/destination-configuration/v1/destinations/"

# Additional destination properties of this form are sent as request headers.
_STATIC_HEADER_PREFIX = "URL.headers."

# The header the destination service reads the end user's token from, for the
# user-propagating authentication types. Case-insensitive on the wire; this
# spelling is the one the service documents.
USER_TOKEN_HEADER = "X-user-token"

# How many principals' resolutions one resolver keeps. A busy multi-user agent
# must not grow this without limit; the least recently used entry goes first.
PER_USER_CACHE_MAX = 256

# Destination authentication types that act as the caller rather than as the
# application. Reported in diagnostics; the resolver itself does not branch
# on them, because the service does the right thing given the header.
USER_PROPAGATING_AUTH_TYPES = frozenset({
    "OAuth2UserTokenExchange",
    "OAuth2JWTBearer",
    "OAuth2SAMLBearerAssertion",
    "PrincipalPropagation",
    "SAMLAssertion",
})

MISSING_BINDING_MESSAGE = (
    "no destination service binding found. On Cloud Foundry, bind a "
    "'destination' service instance to the app. Locally, set "
    "DESTINATION_CLIENT_ID, DESTINATION_CLIENT_SECRET, DESTINATION_URI and "
    "either DESTINATION_TOKEN_URL or DESTINATION_UAA_URL -- all four come from "
    "'cf service-key <instance> <key>'"
)


class DestinationError(RuntimeError):
    """The destination could not be resolved.

    Carries the service's own message where there is one: a 404 naming the
    destination is far more useful than "request failed".
    """


@dataclass(frozen=True)
class DestinationServiceConfig:
    """Credentials for the destination service itself, not for the target."""

    client_id: str
    # Kept out of repr(): a config that reaches a log line through
    # logger.exception or a traceback frame must not print the secret.
    client_secret: str = field(repr=False)
    token_url: str
    api_url: str


def _token_url_from_uaa(uaa: str) -> str:
    """The token endpoint for an XSUAA base URL.

    Accepts a URL that already names the endpoint, because a service key's
    ``url`` does not but a hand-written .env entry often does.
    """
    base = str(uaa).strip().rstrip("/")
    if base.endswith("/oauth/token"):
        return base
    return f"{base}/oauth/token"


def _config_from_vcap(raw: str | None) -> DestinationServiceConfig | None:
    if not raw:
        return None
    try:
        services = json.loads(raw)
        destinations = services.get("destination") or []
    except (TypeError, ValueError, AttributeError):
        # A malformed or wrongly-shaped VCAP_SERVICES must not stop the env
        # fallback: locally it is sometimes set to something hand-edited and
        # half-finished, or to valid JSON that isn't the object we expect.
        logger.warning("Could not parse VCAP_SERVICES for destination", exc_info=True)
        return None
    if not isinstance(destinations, list):
        # Same defence as the except above, for the other half of the shape:
        # {"destination": "oops"} would iterate the string's characters and
        # raise an AttributeError nothing catches. Any unusable shape falls
        # through to the environment fallback.
        logger.warning(
            "VCAP_SERVICES 'destination' is not a list; ignoring the binding"
        )
        return None
    for entry in destinations:
        creds = (entry or {}).get("credentials") or {}
        client_id = str(creds.get("clientid") or "").strip()
        secret = str(creds.get("clientsecret") or "").strip()
        uaa = str(creds.get("url") or "").strip()
        api = str(creds.get("uri") or "").strip().rstrip("/")
        if client_id and secret and uaa and api:
            return DestinationServiceConfig(
                client_id=client_id,
                client_secret=secret,
                token_url=_token_url_from_uaa(uaa),
                api_url=api,
            )
    return None


def config_from_environment(
    environ: Mapping[str, str],
) -> DestinationServiceConfig | None:
    """The destination service binding, or None when there is none.

    VCAP_SERVICES first, so a deployed app always uses its real binding even if
    stale DESTINATION_* variables are also present in the environment.
    """
    from_vcap = _config_from_vcap(environ.get("VCAP_SERVICES"))
    if from_vcap is not None:
        return from_vcap

    client_id = str(environ.get("DESTINATION_CLIENT_ID") or "").strip()
    secret = str(environ.get("DESTINATION_CLIENT_SECRET") or "").strip()
    api = str(environ.get("DESTINATION_URI") or "").strip().rstrip("/")
    token_url = str(environ.get("DESTINATION_TOKEN_URL") or "").strip()
    uaa = str(environ.get("DESTINATION_UAA_URL") or "").strip()
    if not token_url and uaa:
        token_url = _token_url_from_uaa(uaa)

    if client_id and secret and api and token_url:
        return DestinationServiceConfig(
            client_id=client_id,
            client_secret=secret,
            token_url=token_url,
            api_url=api,
        )
    return None


@dataclass(frozen=True)
class Destination:
    """A resolved destination: where to send requests, and what to send with them.

    ``expires_at`` is a :func:`time.monotonic` deadline that already has
    :data:`EXPIRY_SKEW_SECONDS` subtracted, so callers compare against it
    directly rather than re-deriving the margin.
    """

    url: str
    headers: dict[str, str]
    expires_at: float
    # The destination's ``Authentication`` property, for diagnostics only:
    # "OAuth2ClientCredentials", "OAuth2UserTokenExchange", "NoAuthentication"
    # and so on. Empty when the response did not say.
    auth_type: str = ""
    # True when this was resolved with a user's token, so a log line can say
    # whose credential a request carried.
    per_user: bool = False


class DestinationResolver:
    """Resolves one named destination, caching until its token nears expiry.

    One instance per destination name. The cached value is held in memory
    only: it lasts minutes, is re-fetchable at any time without a human, and
    persisting it would add a second place for a credential to leak from.
    """

    def __init__(
        self,
        name: str,
        config: DestinationServiceConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        require_credential: bool = True,
    ) -> None:
        self.name = (name or "").strip()
        self._config = config
        # A constructor seam for tests. Patching httpx.AsyncClient globally
        # instead would also intercept the caller's own API traffic.
        self._transport = transport
        # Whether a destination that hands back no credential at all is a
        # configuration error. True for every target that needs one; False
        # for a public API (NVD) where the destination only supplies a URL
        # and, at most, a static header.
        self.require_credential = require_credential
        self._cached: Destination | None = None
        # Per-principal results, least recently used first. Kept apart from
        # the app-level entry: the two are different credentials, and a
        # user's token must never be handed to a request made as the app.
        self._per_user: OrderedDict[str, Destination] = OrderedDict()
        self._lock = asyncio.Lock()

    def invalidate(self, principal: str | None = None) -> None:
        """Drop a cached destination, so the next resolve re-fetches.

        Without ``principal`` the app-level entry goes; with one, only that
        principal's entry. A 401 from the target under one user says nothing
        about anyone else's token.
        """
        if principal is None:
            self._cached = None
        else:
            self._per_user.pop(principal, None)

    def invalidate_all(self) -> None:
        self._cached = None
        self._per_user.clear()

    @property
    def cached_principals(self) -> list[str]:
        """The principals with a live per-user entry. For tests and diagnostics."""
        return list(self._per_user)

    @staticmethod
    def _user_key(principal: str | None, user_token: str) -> str:
        # Keyed by principal when the caller knows it. A token without a
        # principal is keyed by its digest, so two users are never mixed up
        # and the raw token is never a dictionary key that a debugger prints.
        if principal:
            return principal
        return "token:" + hashlib.sha256(user_token.encode("utf-8")).hexdigest()

    async def resolve(
        self,
        *,
        force: bool = False,
        user_token: str | None = None,
        principal: str | None = None,
    ) -> Destination:
        """The destination, fetched or from cache.

        With ``user_token`` (the signed-in user's XSUAA JWT) the destination
        service is asked to resolve *for that user*: the token goes along as
        ``X-user-token``, which is what makes a user-propagating
        authentication type return that person's token. The result is cached
        under ``principal`` (or a digest of the token), never in the
        app-level slot. Without ``user_token`` the behaviour is unchanged.
        """
        if user_token:
            return await self._resolve_for_user(user_token, principal, force=force)
        current = self._cached
        if not force and current is not None and time.monotonic() < current.expires_at:
            return current
        async with self._lock:
            # Re-check: another task may have fetched while we waited.
            current = self._cached
            if (
                not force
                and current is not None
                and time.monotonic() < current.expires_at
            ):
                return current
            resolved = await self._fetch()
            self._cached = resolved
            return resolved

    async def _resolve_for_user(
        self, user_token: str, principal: str | None, *, force: bool
    ) -> Destination:
        key = self._user_key(principal, user_token)
        hit = self._per_user.get(key)
        if not force and hit is not None and time.monotonic() < hit.expires_at:
            self._per_user.move_to_end(key)
            return hit
        async with self._lock:
            hit = self._per_user.get(key)
            if not force and hit is not None and time.monotonic() < hit.expires_at:
                self._per_user.move_to_end(key)
                return hit
            resolved = await self._fetch(user_token=user_token)
            self._per_user.pop(key, None)
            self._per_user[key] = resolved
            while len(self._per_user) > PER_USER_CACHE_MAX:
                self._per_user.popitem(last=False)
            return resolved

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(30.0), transport=self._transport)

    async def _fetch(self, *, user_token: str | None = None) -> Destination:
        async with self._client() as http:
            token = await self._service_token(http)
            headers = {"Authorization": f"Bearer {token}"}
            if user_token:
                headers[USER_TOKEN_HEADER] = user_token
            try:
                response = await http.get(
                    f"{self._config.api_url}{DESTINATION_PATH}{self.name}",
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                raise DestinationError(
                    f"could not reach the destination service for {self.name!r}: {exc}"
                ) from exc
            if response.status_code == 404:
                raise DestinationError(
                    f"destination {self.name!r} does not exist in the subaccount "
                    f"this app's destination service instance belongs to"
                )
            if response.status_code >= 400:
                raise DestinationError(
                    f"destination service returned {response.status_code} for "
                    f"{self.name!r}: {response.text[:400]}"
                )
            payload = response.json()
        return self._destination_from(payload, per_user=bool(user_token))

    async def _service_token(self, http: httpx.AsyncClient) -> str:
        try:
            response = await http.post(
                self._config.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise DestinationError(
                f"could not reach the destination service token endpoint: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise DestinationError(
                f"destination service token request returned "
                f"{response.status_code}: {response.text[:400]}"
            )
        token = str((response.json() or {}).get("access_token") or "")
        if not token:
            raise DestinationError(
                "destination service token response carried no access_token"
            )
        return token

    def _destination_from(self, payload: Any, *, per_user: bool = False) -> Destination:
        config = (payload or {}).get("destinationConfiguration") or {}
        url = str(config.get("URL") or "").strip().rstrip("/")
        if not url:
            raise DestinationError(f"destination {self.name!r} has no URL configured")
        auth_type = str(config.get("Authentication") or "").strip()

        # Static headers set as `URL.headers.<Name>` additional properties.
        # This is how a destination carries a long-lived bearer token the
        # target issues itself (a Slack bot token): NoAuthentication plus
        # `URL.headers.Authorization = Bearer ...`.
        headers: dict[str, str] = {
            key[len(_STATIC_HEADER_PREFIX):]: str(value).strip()
            for key, value in config.items()
            if key.startswith(_STATIC_HEADER_PREFIX)
            and key[len(_STATIC_HEADER_PREFIX):]
            and str(value or "").strip()
        }
        lifetime = DEFAULT_LIFETIME_SECONDS
        tokens = (payload or {}).get("authTokens") or []
        if not tokens:
            if (
                any(k.lower() == "authorization" for k in headers)
                or not self.require_credential
            ):
                deadline = time.monotonic() + max(lifetime - EXPIRY_SKEW_SECONDS, 1)
                return Destination(
                    url=url, headers=headers, expires_at=deadline,
                    auth_type=auth_type, per_user=per_user,
                )
            # A destination created with NoAuthentication resolves perfectly
            # well and hands back no credential at all. Saying so here beats
            # the bare 401-after-one-retry the caller would otherwise report,
            # which points at the target rather than at the destination.
            hint = (
                " (it was resolved with the user's token, so a user-propagating "
                "type such as OAuth2UserTokenExchange would also do)"
                if per_user else ""
            )
            raise DestinationError(
                f"destination {self.name!r} returned no authentication token; "
                f"check its Authentication type in the subaccount -- this "
                f"integration needs OAuth2ClientCredentials, or NoAuthentication "
                f"with a URL.headers.Authorization property, and this "
                f"destination carries neither{hint}"
            )
        token = tokens[0] or {}
        if token.get("error"):
            what = "for the signed-in user" if per_user else "from the target"
            raise DestinationError(
                f"destination {self.name!r} could not obtain a token {what}: "
                f"{token['error']}"
            )
        header = token.get("http_header") or {}
        key = str(header.get("key") or "").strip()
        value = str(header.get("value") or "").strip()
        if key and value:
            headers[key] = value
        elif token.get("type") and token.get("value"):
            headers["Authorization"] = f"{token['type']} {token['value']}"
        try:
            lifetime = int(float(token.get("expires_in")))
        except (TypeError, ValueError):
            lifetime = DEFAULT_LIFETIME_SECONDS

        deadline = time.monotonic() + max(lifetime - EXPIRY_SKEW_SECONDS, 1)
        return Destination(
            url=url, headers=headers, expires_at=deadline,
            auth_type=auth_type, per_user=per_user,
        )


def resolver_from_environment(
    name: str, *, server_key: str = "", require_credential: bool = True
) -> DestinationResolver:
    """A resolver for ``name`` from the ambient destination service binding.

    Raises rather than returning None when there is no binding: a toolset
    built against nothing would fail later with an AttributeError from inside
    a tool call, which tells an operator nothing about what to fix.
    ``server_key`` only prefixes that message.
    """
    import os

    config = config_from_environment(os.environ)
    if config is None:
        prefix = f"{server_key}: " if server_key else ""
        raise DestinationError(f"{prefix}{MISSING_BINDING_MESSAGE}")
    return DestinationResolver(name, config, require_credential=require_credential)
