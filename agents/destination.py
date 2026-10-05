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
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Hashable, Mapping
from urllib.parse import quote_plus

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

# ... and of this form as query parameters on every request (`sap-client`).
_STATIC_QUERY_PREFIX = "URL.queries."

# `ProxyType` of a destination that is reached through the Cloud Connector.
PROXY_TYPE_ON_PREMISE = "OnPremise"

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


def _token_digest(user_token: str) -> str:
    return hashlib.sha256(user_token.encode("utf-8")).hexdigest()


def _cache_owner(principal: str | None, user_token: str) -> str:
    """Whose cache entries these are: the principal, else ``token:<digest>``."""
    if principal:
        return principal
    return "token:" + _token_digest(user_token)


def _cache_key(principal: str | None, user_token: str) -> tuple[str, str]:
    """The key of one per-user cache entry: ``(owner, sha256 of the token)``.

    A principal alone is not enough. The principal and the token reach this
    module separately, and they can name different people: a job run started
    by an admin carries the admin's JWT while ``run_as`` sets the principal to
    the agent's run-as user. Keyed by principal only, the admin's credential
    would be filed under that user and served to the user's own next request.
    With the digest in the key an entry is served only to a caller presenting
    the very token it was obtained with. The price is one extra fetch when a
    user's JWT is refreshed. The raw token is never part of a key.
    """
    return _cache_owner(principal, user_token), _token_digest(user_token)


def _drop_owner(cache: "OrderedDict[tuple[str, str], Any]", owner: str) -> None:
    for key in [k for k in cache if k[0] == owner]:
        del cache[key]


def _owners(cache: "OrderedDict[tuple[str, str], Any]") -> list[str]:
    # De-duplicated, in the order of each owner's most recently used entry.
    return list(dict.fromkeys(reversed([k[0] for k in cache])))[::-1]


def _store(
    cache: "OrderedDict[tuple[str, str], Any]",
    key: tuple[str, str],
    value: Any,
    deadline_of: Callable[[Any], float],
) -> None:
    """Put ``value`` at the fresh end of a per-user cache and keep it bounded.

    The same owner's expired entries go first: with the token digest in the
    key, every refreshed JWT leaves a dead entry behind, and those must not
    take the slots (:data:`PER_USER_CACHE_MAX`) that live ones need. Other
    owners' entries are left to the LRU.
    """
    now = time.monotonic()
    for stale in [
        k for k, v in cache.items() if k[0] == key[0] and deadline_of(v) <= now
    ]:
        del cache[stale]
    cache.pop(key, None)
    cache[key] = value
    while len(cache) > PER_USER_CACHE_MAX:
        cache.popitem(last=False)


def _scrub(text: str, *secrets: str | None) -> str:
    """``text`` with every given secret replaced by ``***``.

    The remote side's own message is the useful part of an error, but a
    service (or a proxy in front of it) that echoes the request must not get
    a client secret or a user's token into a :class:`DestinationError`, which
    other modules log and some hand to a model.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
            # A form body echoed back carries the value percent-encoded.
            encoded = quote_plus(secret)
            if encoded != secret:
                text = text.replace(encoded, "***")
    return text


class _KeyedLocks:
    """One :class:`asyncio.Lock` per cache key, kept only while in use.

    A single lock per resolver made every user wait for whichever fetch was
    in flight (up to the 30 s timeout each), and keying the cache by token
    digest made misses more frequent. Per key, overlapping calls for the same
    user and token still fetch once, and different users do not queue behind
    each other. A lock is dropped when its last holder or waiter leaves, so
    the table is bounded by the calls in flight, not by the users ever seen.
    """

    def __init__(self) -> None:
        self._locks: dict[Hashable, list[Any]] = {}  # key -> [lock, users]

    @property
    def idle(self) -> bool:
        return not self._locks

    @asynccontextmanager
    async def hold(self, key: Hashable) -> AsyncIterator[None]:
        # No await between the lookup and the count, so two tasks cannot
        # create two locks for one key.
        entry = self._locks.get(key)
        if entry is None:
            entry = self._locks[key] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0 and self._locks.get(key) is entry:
                del self._locks[key]


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
    # ``ProxyType``: "OnPremise" (through the connectivity proxy and the Cloud
    # Connector), "Internet", or empty when the response did not say.
    proxy_type: str = ""
    # ``CloudConnectorLocationId``: which Cloud Connector, when the subaccount
    # has more than one.
    location_id: str = ""
    # ``URL.queries.<name>`` properties, sent as query parameters.
    queries: dict[str, str] = field(default_factory=dict)


# Destination property names whose value is a credential. Matched
# case-insensitively as a substring, so `mail.password`, `Password`,
# `clientSecret` and `tokenServicePassword` are all covered.
_SECRET_PROPERTY_MARKERS = ("password", "secret", "token", "key")


class DestinationProperties(Mapping[str, str]):
    """A destination's raw ``destinationConfiguration``, read-only.

    For destinations that are not an HTTP endpoint -- a ``MAIL`` destination
    has no ``URL``, only ``mail.smtp.host``, ``mail.user``, ``mail.password``
    and friends -- the properties themselves are the resolution. Some of them
    are credentials, so ``repr()``/``str()`` list property *names* only: a
    value that reaches a log line through ``logger.exception``, a traceback
    frame or an f-string must not print the secret.

    ``expires_at`` follows the same rule as :class:`Destination`: a
    :func:`time.monotonic` deadline with the skew already subtracted.
    """

    __slots__ = ("_values", "expires_at", "auth_type")

    def __init__(self, values: Mapping[str, Any], *, expires_at: float) -> None:
        self._values = {str(k): "" if v is None else str(v) for k, v in values.items()}
        self.expires_at = expires_at
        self.auth_type = self._values.get("Authentication", "").strip()

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    @staticmethod
    def is_secret(name: str) -> bool:
        lowered = name.lower()
        return any(marker in lowered for marker in _SECRET_PROPERTY_MARKERS)

    def __repr__(self) -> str:
        shown = ", ".join(
            f"{k}=***" if self.is_secret(k) else k for k in sorted(self._values)
        )
        return f"DestinationProperties({shown})"

    __str__ = __repr__


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
        # The raw properties, for destinations used by what they carry rather
        # than by a URL (MAIL). App-level only: no built-in resolves those
        # per user. Same lifetime rules as `_cached`.
        self._cached_properties: DestinationProperties | None = None
        # Per-user results, least recently used first, keyed by principal
        # AND token digest (see _cache_key). Kept apart from the app-level
        # entry: the two are different credentials, and a user's token must
        # never be handed to a request made as the app.
        self._per_user: OrderedDict[tuple[str, str], Destination] = OrderedDict()
        # The app-level slot and the raw properties share one lock; per-user
        # fetches lock per cache key, so users do not wait for each other.
        self._lock = asyncio.Lock()
        self._user_locks = _KeyedLocks()

    def invalidate(self, principal: str | None = None) -> None:
        """Drop a cached destination, so the next resolve re-fetches.

        Without ``principal`` the app-level entry goes; with one, every
        entry of that principal, whatever token it was resolved with. A 401
        from the target under one user says nothing about anyone else's token.
        """
        if principal is None:
            self._cached = None
            self._cached_properties = None
        else:
            _drop_owner(self._per_user, principal)

    def invalidate_all(self) -> None:
        self._cached = None
        self._cached_properties = None
        self._per_user.clear()

    @property
    def cached_principals(self) -> list[str]:
        """The principals with a per-user entry, each once. For tests and diagnostics.

        A token resolved without a principal shows as ``token:<digest>``.
        """
        return _owners(self._per_user)

    @staticmethod
    def user_key(principal: str | None, user_token: str) -> str:
        """The name a user's entries are filed under, for :meth:`invalidate`.

        ``principal`` when there is one, else ``"token:" + sha256(user_token)``
        (never the raw token). Same meaning as
        :meth:`ConnectivityTokens.user_key`: it is not the whole cache key,
        which also carries the token's digest (``_cache_key``).
        """
        return _cache_owner(principal, user_token)

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
        under ``principal`` together with a digest of the token, never in the
        app-level slot, and is served again only when both match: a
        credential obtained with one user's token is never handed to a call
        that presents another token, whatever principal it names. A refreshed
        JWT for the same user therefore resolves once more. Without
        ``user_token`` the behaviour is unchanged.
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
        key = _cache_key(principal, user_token)
        hit = self._per_user.get(key)
        if not force and hit is not None and time.monotonic() < hit.expires_at:
            self._per_user.move_to_end(key)
            return hit
        async with self._user_locks.hold(key):
            # Re-check: an overlapping call for this user and token may have
            # fetched while we waited.
            hit = self._per_user.get(key)
            if not force and hit is not None and time.monotonic() < hit.expires_at:
                self._per_user.move_to_end(key)
                return hit
            resolved = await self._fetch(user_token=user_token)
            _store(self._per_user, key, resolved, lambda d: d.expires_at)
            return resolved

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(30.0), transport=self._transport)

    async def resolve_properties(self, *, force: bool = False) -> DestinationProperties:
        """The destination's raw ``destinationConfiguration``, fetched or cached.

        For destinations that carry settings rather than a URL -- a ``MAIL``
        destination's ``mail.smtp.host``, ``mail.user``, ``mail.password``.
        Resolved with the app's own token, never a user's, and cached like
        :meth:`resolve`: until the returned token nears expiry, or
        :data:`DEFAULT_LIFETIME_SECONDS` when the response carries none. The
        values include the destination's credential; the returned mapping's
        ``repr`` masks them, and nothing here logs them.
        """
        current = self._cached_properties
        if not force and current is not None and time.monotonic() < current.expires_at:
            return current
        async with self._lock:
            current = self._cached_properties
            if (
                not force
                and current is not None
                and time.monotonic() < current.expires_at
            ):
                return current
            payload = await self._fetch_payload()
            config = (payload or {}).get("destinationConfiguration") or {}
            if not isinstance(config, dict) or not config:
                raise DestinationError(
                    f"destination {self.name!r} returned no configuration"
                )
            lifetime = DEFAULT_LIFETIME_SECONDS
            tokens = (payload or {}).get("authTokens") or []
            if tokens and isinstance(tokens[0], dict):
                try:
                    lifetime = int(float(tokens[0].get("expires_in")))
                except (TypeError, ValueError):
                    lifetime = DEFAULT_LIFETIME_SECONDS
            deadline = time.monotonic() + max(lifetime - EXPIRY_SKEW_SECONDS, 1)
            resolved = DestinationProperties(config, expires_at=deadline)
            self._cached_properties = resolved
            return resolved

    async def _fetch(self, *, user_token: str | None = None) -> Destination:
        payload = await self._fetch_payload(user_token=user_token)
        return self._destination_from(
            payload,
            per_user=bool(user_token),
            secrets=(user_token, self._config.client_secret),
        )

    async def _fetch_payload(self, *, user_token: str | None = None) -> Any:
        """The destination service's "find destination" response, as JSON."""
        async with self._client() as http:
            token = await self._service_token(http)
            headers = {"Authorization": f"Bearer {token}"}
            if user_token:
                headers[USER_TOKEN_HEADER] = user_token
            # What a remote message may echo back and must not carry on.
            secrets = (user_token, token, self._config.client_secret)
            failure = None
            try:
                response = await http.get(
                    f"{self._config.api_url}{DESTINATION_PATH}{self.name}",
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                failure = f"{type(exc).__name__}: {_scrub(str(exc), *secrets)}"
            if failure is not None:
                # Raised outside the except block on purpose: a chained httpx
                # error would print its unscrubbed text in any traceback log.
                raise DestinationError(
                    f"could not reach the destination service for {self.name!r}: "
                    f"{failure}"
                )
            if response.status_code == 404:
                raise DestinationError(
                    f"destination {self.name!r} does not exist in the subaccount "
                    f"this app's destination service instance belongs to"
                )
            if response.status_code >= 400:
                raise DestinationError(
                    f"destination service returned {response.status_code} for "
                    f"{self.name!r}: {_scrub(response.text, *secrets)[:400]}"
                )
            # A 200 that is not the service's JSON object -- a proxy's login
            # page, say -- is reported as such, without the body: it is
            # somebody else's page and may carry anything.
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if not isinstance(payload, dict):
                raise DestinationError(
                    f"destination service answer for {self.name!r} is not JSON"
                )
            return payload

    async def _service_token(self, http: httpx.AsyncClient) -> str:
        secret = self._config.client_secret
        failure = None
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
            failure = f"{type(exc).__name__}: {_scrub(str(exc), secret)}"
        if failure is not None:
            raise DestinationError(
                f"could not reach the destination service token endpoint: {failure}"
            )
        if response.status_code >= 400:
            raise DestinationError(
                f"destination service token request returned "
                f"{response.status_code}: {_scrub(response.text, secret)[:400]}"
            )
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise DestinationError("destination service token response is not JSON")
        token = str(body.get("access_token") or "")
        if not token:
            raise DestinationError(
                "destination service token response carried no access_token"
            )
        return token

    def _destination_from(
        self,
        payload: Any,
        *,
        per_user: bool = False,
        secrets: tuple[str | None, ...] = (),
    ) -> Destination:
        # `secrets` is what the service was sent and might echo in an error
        # text: the user's token and the client secret.
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
        proxy_type = str(config.get("ProxyType") or "").strip()
        location_id = str(config.get("CloudConnectorLocationId") or "").strip()
        queries: dict[str, str] = {
            key[len(_STATIC_QUERY_PREFIX):]: str(value).strip()
            for key, value in config.items()
            if key.startswith(_STATIC_QUERY_PREFIX)
            and key[len(_STATIC_QUERY_PREFIX):]
            and str(value or "").strip()
        }
        lifetime = DEFAULT_LIFETIME_SECONDS
        tokens = (payload or {}).get("authTokens") or []
        if not tokens:
            # An OnPremise PrincipalPropagation destination carries no token
            # of its own: the user's identity travels to the Cloud Connector
            # in the connectivity proxy's headers (agents.destination_auth).
            # So "no authTokens" is the normal answer here -- but only for a
            # resolution made for a user. Resolved as the application it must
            # fail, whatever `require_credential` says: accepting it would
            # leave a request with no caller identity at all.
            principal_propagation = (
                auth_type == "PrincipalPropagation"
                and proxy_type == PROXY_TYPE_ON_PREMISE
            )
            if principal_propagation and not per_user:
                raise DestinationError(
                    f"destination {self.name!r} uses PrincipalPropagation, which "
                    f"needs the signed-in user; resolve it with user context or "
                    f"use a technical-user destination"
                )
            if (
                principal_propagation
                or any(k.lower() == "authorization" for k in headers)
                or not self.require_credential
            ):
                deadline = time.monotonic() + max(lifetime - EXPIRY_SKEW_SECONDS, 1)
                return Destination(
                    url=url, headers=headers, expires_at=deadline,
                    auth_type=auth_type, per_user=per_user,
                    proxy_type=proxy_type, location_id=location_id,
                    queries=queries,
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
            # The service's own text, which says what to fix -- but it is
            # remote text that gets logged and may reach a model, so it is
            # scrubbed and cut like every other echoed message here.
            detail = _scrub(str(token["error"]), *secrets)[:400]
            raise DestinationError(
                f"destination {self.name!r} could not obtain a token {what}: "
                f"{detail}"
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
            proxy_type=proxy_type, location_id=location_id, queries=queries,
        )


async def fetch_service_token(
    config: DestinationServiceConfig, http: httpx.AsyncClient
) -> str:
    """A client-credentials token for the destination service itself.

    For a caller that asks the service something other than "find
    destination" (listing what exists). The very request a resolver makes,
    so there is one place that knows the grant; nothing is cached and no
    resolver's cache is touched. Raises :class:`DestinationError` whose text
    has the client secret scrubbed.
    """
    return await DestinationResolver("", config)._service_token(http)


def scrub(text: str, *secrets: str | None) -> str:
    """Public name of the scrub every error text of this module goes through."""
    return _scrub(text, *secrets)


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


# --- connectivity service (OnPremise destinations) ---------------------------
#
# A destination with ProxyType "OnPremise" is not reachable directly: requests
# go through the connectivity service's HTTP proxy, which wants a token of its
# own in `Proxy-Authorization`. That token is the application's
# (client_credentials) for a technical-user destination, or one exchanged from
# the signed-in user's JWT for principal propagation. Sending them is
# agents.destination_auth's job; this part only reads the binding and keeps
# the tokens.

JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"


@dataclass(frozen=True)
class ConnectivityConfig:
    """Credentials and proxy address of the connectivity service binding."""

    client_id: str
    # Out of repr() for the same reason as DestinationServiceConfig's.
    client_secret: str = field(repr=False)
    token_url: str
    proxy_host: str
    proxy_port: int


def _proxy_port(value: Any) -> int | None:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def _connectivity_from_vcap(raw: str | None) -> ConnectivityConfig | None:
    if not raw:
        return None
    try:
        services = json.loads(raw)
        bindings = services.get("connectivity") or []
    except (TypeError, ValueError, AttributeError):
        # Same rule as _config_from_vcap: an unusable VCAP_SERVICES must not
        # stop the environment fallback.
        logger.warning("Could not parse VCAP_SERVICES for connectivity", exc_info=True)
        return None
    if not isinstance(bindings, list):
        logger.warning(
            "VCAP_SERVICES 'connectivity' is not a list; ignoring the binding"
        )
        return None
    for entry in bindings:
        if not isinstance(entry, dict):
            continue
        creds = entry.get("credentials") or {}
        if not isinstance(creds, dict):
            continue
        client_id = str(creds.get("clientid") or "").strip()
        secret = str(creds.get("clientsecret") or "").strip()
        uaa = str(creds.get("token_service_url") or creds.get("url") or "").strip()
        host = str(creds.get("onpremise_proxy_host") or "").strip()
        port = _proxy_port(
            creds.get("onpremise_proxy_http_port") or creds.get("onpremise_proxy_port")
        )
        if client_id and secret and uaa and host and port:
            return ConnectivityConfig(
                client_id=client_id,
                client_secret=secret,
                token_url=_token_url_from_uaa(uaa),
                proxy_host=host,
                proxy_port=port,
            )
    return None


def connectivity_config_from_environment(
    environ: Mapping[str, str],
) -> ConnectivityConfig | None:
    """The connectivity service binding, or None when there is none.

    VCAP_SERVICES first, then CONNECTIVITY_* (a local run against a service
    key), the same order as :func:`config_from_environment`. None is a normal
    answer: only OnPremise destinations need the binding, and the caller says
    so when one is used without it.
    """
    from_vcap = _connectivity_from_vcap(environ.get("VCAP_SERVICES"))
    if from_vcap is not None:
        return from_vcap

    client_id = str(environ.get("CONNECTIVITY_CLIENT_ID") or "").strip()
    secret = str(environ.get("CONNECTIVITY_CLIENT_SECRET") or "").strip()
    token_url = str(environ.get("CONNECTIVITY_TOKEN_URL") or "").strip()
    uaa = str(environ.get("CONNECTIVITY_UAA_URL") or "").strip()
    if not token_url and uaa:
        token_url = _token_url_from_uaa(uaa)
    host = str(environ.get("CONNECTIVITY_PROXY_HOST") or "").strip()
    port = _proxy_port(environ.get("CONNECTIVITY_PROXY_PORT"))

    if client_id and secret and token_url and host and port:
        return ConnectivityConfig(
            client_id=client_id,
            client_secret=secret,
            token_url=token_url,
            proxy_host=host,
            proxy_port=port,
        )
    return None


class ConnectivityTokens:
    """Tokens for the connectivity proxy: the application's, and one per user.

    Same shape as :class:`DestinationResolver`'s cache, for the same reasons:
    in memory only, refetched :data:`EXPIRY_SKEW_SECONDS` early, and the
    per-user tokens in their own bounded LRU, apart from the app-level slot.
    The two are different identities towards the Cloud Connector -- a user
    token decides *who the SAP system sees* -- so neither method ever answers
    from the other's cache, and :meth:`user_token` has no fall-back to the
    application's token.
    """

    def __init__(
        self,
        config: ConnectivityConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport
        # (token, monotonic deadline with the skew already subtracted)
        self._app: tuple[str, float] | None = None
        # Keyed by principal AND digest of the JWT exchanged (_cache_key).
        self._per_user: OrderedDict[tuple[str, str], tuple[str, float]] = OrderedDict()
        # The app token has its own lock; user tokens lock per cache key.
        self._lock = asyncio.Lock()
        self._user_locks = _KeyedLocks()

    def invalidate(self, principal: str | None = None) -> None:
        """Drop the app token, or with ``principal`` every token of that user.

        A 407 from the proxy under one user says nothing about anyone else's
        token. ``principal`` is what :meth:`user_key` returns: the principal
        the token was requested with, or ``token:<digest>`` when there was
        none. Every entry filed under it goes, whichever JWT it came from.
        """
        if principal is None:
            self._app = None
        else:
            _drop_owner(self._per_user, principal)

    def invalidate_all(self) -> None:
        self._app = None
        self._per_user.clear()

    @property
    def cached_principals(self) -> list[str]:
        """The principals with a per-user token, each once. For tests and diagnostics."""
        return _owners(self._per_user)

    @staticmethod
    def user_key(principal: str | None, user_jwt: str) -> str:
        """The name a user's tokens are filed under, for :meth:`invalidate`.

        ``principal`` when there is one, else ``"token:" + sha256(user_jwt)``.
        It is *not* the whole cache key: an entry is additionally bound to the
        digest of the JWT it was exchanged from, so ``invalidate(user_key(p,
        jwt))`` drops every token of ``p`` while a lookup only ever returns
        the one obtained with the JWT presented. Never contains the raw JWT.
        """
        return _cache_owner(principal, user_jwt)

    async def app_token(self, *, force: bool = False) -> str:
        """The application's token (client_credentials), fetched or cached."""
        current = self._app
        if not force and current is not None and time.monotonic() < current[1]:
            return current[0]
        async with self._lock:
            current = self._app
            if not force and current is not None and time.monotonic() < current[1]:
                return current[0]
            fetched = await self._request({"grant_type": "client_credentials"})
            self._app = fetched
            return fetched[0]

    async def user_token(
        self, user_jwt: str, principal: str | None, *, force: bool = False
    ) -> str:
        """A token for the signed-in user, exchanged from their XSUAA JWT.

        Cached under ``principal`` together with a digest of the JWT, never
        in the app slot, and served again only when both match: the token
        decides who the SAP system sees, so one exchanged from another JWT is
        never returned, whatever principal the call names. A refreshed JWT for
        the same user is exchanged once more. Without a JWT this raises:
        answering with the application's token would make the proxy call run
        under the wrong identity.
        """
        if not user_jwt or not isinstance(user_jwt, str):
            raise DestinationError(
                "the connectivity service needs the signed-in user's token for "
                "principal propagation, and none was given"
            )
        key = _cache_key(principal, user_jwt)
        hit = self._per_user.get(key)
        if not force and hit is not None and time.monotonic() < hit[1]:
            self._per_user.move_to_end(key)
            return hit[0]
        async with self._user_locks.hold(key):
            hit = self._per_user.get(key)
            if not force and hit is not None and time.monotonic() < hit[1]:
                self._per_user.move_to_end(key)
                return hit[0]
            fetched = await self._request(
                {
                    "grant_type": JWT_BEARER_GRANT,
                    "assertion": user_jwt,
                    "token_format": "jwt",
                    "response_type": "token",
                },
                secrets=(user_jwt,),
                what="user token",
            )
            _store(self._per_user, key, fetched, lambda entry: entry[1])
            return fetched[0]

    async def _request(
        self,
        form: dict[str, str],
        *,
        secrets: tuple[str, ...] = (),
        what: str = "token",
    ) -> tuple[str, float]:
        data = {
            **form,
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
        }
        secrets = (self.config.client_secret, *secrets)
        failure = None
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0), transport=self._transport
        ) as http:
            try:
                response = await http.post(
                    self.config.token_url,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.HTTPError as exc:
                failure = f"{type(exc).__name__}: {_scrub(str(exc), *secrets)}"
        if failure is not None:
            # Outside the except block: no chained, unscrubbed httpx error.
            raise DestinationError(
                f"could not reach the connectivity service token endpoint: {failure}"
            )
        if response.status_code >= 400:
            raise DestinationError(
                f"connectivity service {what} request returned "
                f"{response.status_code}: {_scrub(response.text, *secrets)[:400]}"
            )
        try:
            body = response.json() or {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        token = str(body.get("access_token") or "")
        if not token:
            raise DestinationError(
                f"connectivity service {what} response carried no access_token"
            )
        try:
            lifetime = int(float(body.get("expires_in")))
        except (TypeError, ValueError):
            lifetime = DEFAULT_LIFETIME_SECONDS
        deadline = time.monotonic() + max(lifetime - EXPIRY_SKEW_SECONDS, 1)
        return token, deadline


def connectivity_from_environment() -> ConnectivityTokens | None:
    """Connectivity tokens from the ambient binding, or None without one.

    None rather than an error, unlike :func:`resolver_from_environment`: an
    app with only Internet destinations has no connectivity binding and needs
    none. Each call returns a new instance with an empty cache, so a caller
    keeps the one it built for as long as its HTTP client lives.
    """
    import os

    config = connectivity_config_from_environment(os.environ)
    return ConnectivityTokens(config) if config is not None else None
