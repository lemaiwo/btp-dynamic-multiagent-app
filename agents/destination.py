"""Reaching a service through a BTP destination.

The destination service holds the target's URL and its credential, and -- for
an ``OAuth2ClientCredentials`` destination -- performs the token exchange
itself, handing back a ready ``Authorization`` header. That is the whole
appeal: this application stores no credential for the target at all, and
rotating it is something the destination's owner does without touching us.

Contrast :mod:`agents.client_credentials`, which authenticates *as* the
application and therefore must hold a client secret in our own database. Here
the only thing configured is a name.

This module knows nothing about what sits behind the destination.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
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
    client_secret: str
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
    ) -> None:
        self.name = (name or "").strip()
        self._config = config
        # A constructor seam for tests. Patching httpx.AsyncClient globally
        # instead would also intercept the caller's own API traffic.
        self._transport = transport
        self._cached: Destination | None = None
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        """Drop the cached destination, so the next resolve re-fetches."""
        self._cached = None

    async def resolve(self, *, force: bool = False) -> Destination:
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

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(30.0), transport=self._transport)

    async def _fetch(self) -> Destination:
        async with self._client() as http:
            token = await self._service_token(http)
            try:
                response = await http.get(
                    f"{self._config.api_url}{DESTINATION_PATH}{self.name}",
                    headers={"Authorization": f"Bearer {token}"},
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
        return self._destination_from(payload)

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

    def _destination_from(self, payload: Any) -> Destination:
        config = (payload or {}).get("destinationConfiguration") or {}
        url = str(config.get("URL") or "").strip().rstrip("/")
        if not url:
            raise DestinationError(f"destination {self.name!r} has no URL configured")

        headers: dict[str, str] = {}
        lifetime = DEFAULT_LIFETIME_SECONDS
        tokens = (payload or {}).get("authTokens") or []
        if tokens:
            token = tokens[0] or {}
            if token.get("error"):
                raise DestinationError(
                    f"destination {self.name!r} could not obtain a token from the "
                    f"target: {token['error']}"
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
        return Destination(url=url, headers=headers, expires_at=deadline)
