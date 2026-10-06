"""An httpx auth that reaches a REST API through a BTP destination.

The built-in toolsets (Gmail, Outlook, Teams, SAP notes) issue requests against
a fixed base URL -- ``https://graph.microsoft.com`` and the like -- and hand the
credential problem to an :class:`httpx.Auth`. This is the auth for the case
where both the URL and the credential come from a BTP destination, the way
:mod:`agents.jira_tools` and :mod:`agents.slack_tools` already work, without
rewriting each client to ask a resolver before every call.

How it fits together:

* the toolset builds its client with ``base_url=PLACEHOLDER_BASE``, so every
  relative request lands on the host ``destination.invalid``;
* per request, this auth resolves the destination and rewrites that host (and
  path prefix) to the destination's URL, then sets the destination's headers
  -- the ``Authorization`` the service minted, plus any ``URL.headers.*``
  static properties;
* with ``user_context=True`` the destination is resolved **as the signed-in
  user**: the request-bound XSUAA JWT (``agents.auth.current_jwt``) is sent to
  the destination service as ``X-user-token``, which makes a user-propagating
  destination (OAuth2UserTokenExchange, OAuth2JWTBearer,
  OAuth2SAMLBearerAssertion, PrincipalPropagation) return that user's token;
* a 401 from the target drops the cached entry -- that user's, or the app's --
  and retries exactly once.

Two things are refused on purpose. The destination's credential is never sent
to a host the destination did not name (a Graph ``@odata.nextLink`` back to
Graph is fine; anything else is not), and nothing in the server's config block
can change the host: the URL is the destination's, full stop. A destination
that fronts the API with a proxy therefore works, and a config edit cannot
redirect a credential.

``user_context=True`` without a JWT in context raises
:class:`DestinationUserRequired`. That is deliberately *not*
``OAuthAuthorizationRequired``: there is nothing to sign in to, and a sign-in
link would loop. An agent that must run unattended, with no user behind the
run, needs ``user_context`` off and an app-level credential in the destination.

"No JWT in context" is not the same as "a scheduled or API-triggered run",
though. A run started from a request ("Run now", an API trigger) is a task
created inside that request and inherits its ``current_jwt``, while
``agents.auth.run_as`` sets ``current_principal`` to the agent's run-as user.
In such a run the token and
the principal read by ``DestinationAuth._user`` can name **different
identities** -- the destination is resolved with the trigger's token under the
run-as principal. That is why every per-user cache behind this auth
(``DestinationResolver``, ``ConnectivityTokens``, the Outlook and Teams
caches, the OData CSRF session store in ``agents/odata/session.py``) keys on
the principal AND a digest of the token: an entry is only served to a caller
presenting the token it was obtained with.

OnPremise destinations (``ProxyType: OnPremise``, a virtual host behind a
Cloud Connector) are reached through the connectivity service's HTTP proxy,
and only by a client built for it: :func:`routed_auth` builds the auth and
the :class:`OnPremiseRouter` together, and ``destination_http_client(...,
connectivity=...)`` uses it. Today that is the OData toolset, its
``$metadata`` preview and its test call; every other built-in refuses an
OnPremise destination and says that it cannot reach one. The mechanism is the
one ``scripts/probe_odata_connectivity.py`` proved against a landscape: the
request keeps the destination's ``http://`` URL, goes to the proxy in HTTP
forward mode (no CONNECT tunnel) and carries a per-request
``Proxy-Authorization``.

* **Technical user** (``user_context`` off): the application's connectivity
  token in ``Proxy-Authorization``; the destination's own ``Authorization``
  travels to the target as for an Internet destination.
* **Signed-in user** (``user_context`` on): the destination must be
  ``PrincipalPropagation``, and no credential travels next to the user's
  identity -- nothing stored in the destination, and no ``Authorization``
  the caller set: either would let the target answer as somebody else and
  pass for principal propagation. The identity travels in one of two ways
  (:data:`DEFAULT_PP_MODE`, env ``CONNECTIVITY_PP_MODE``): ``exchange`` puts
  a token exchanged from the user's JWT at the connectivity instance's XSUAA
  into ``Proxy-Authorization``; ``header`` puts the application's token
  there and the user's JWT into ``SAP-Connectivity-Authentication``. The
  variable is read when a user's OnPremise request needs it and at no other
  time; a value that is neither mode refuses that request (it never picks a
  mechanism nobody chose). No JWT bound is :class:`DestinationUserRequired`
  before any token or proxy call; there is no path from a user run to the
  application's identity.
* **Whose identity.** The user is the owner of the JWT bound to the run. A
  job started with "Run now" carries the JWT of whoever started it, so it
  runs in SAP as that person, whatever run-as principal the job names.
* ``SAP-Connectivity-SCC-Location_ID`` is sent when the destination names a
  Cloud Connector location.
* ``http://`` is accepted for an OnPremise destination that goes through the
  proxy, and for nothing else; an ``https://`` OnPremise URL is refused,
  because a CONNECT tunnel would carry the request's ``Proxy-Authorization``
  to the target instead of the proxy.
* The connectivity headers (``Proxy-Authorization``,
  ``SAP-Connectivity-Authentication``,
  ``SAP-Connectivity-Technical-Authentication``,
  ``SAP-Connectivity-SCC-Location_ID``) are set by the flow only. A caller
  and a subclass cannot set them, and the router refuses a request that
  carries a proxy token without having been shaped for the proxy, so the
  proxy token never leaves on the direct path.
* A 407 from the proxy drops the connectivity token that was used (that
  user's only, and only if nobody renewed it meanwhile) and retries once,
  like the 401 rule.

Two rules hold for EVERY destination, OnPremise or not:

* a destination's ``URL.headers.*`` cannot set the connectivity headers
  above or ``Host``: such a property is dropped;
* a destination's ``URL.queries.*`` properties (``sap-client``, ...) are
  added to every request; a parameter the caller already sends wins,
  whatever its case. A client built for the connectivity route (the OData
  callers) additionally skips query names that start with ``$`` -- a
  destination must not add ``$filter`` or ``$expand`` behind the argument
  checks of the tools -- and takes one spelling per name.

A client built with ``connectivity`` has the router as its transport, so
httpx applies no ``HTTP_PROXY``/``HTTPS_PROXY``/``NO_PROXY`` from the
environment to it, on either path: nothing outside the binding can change
where a request with a proxy token goes.
"""

from __future__ import annotations

import dataclasses
import inspect
import logging
import os
import re
from collections.abc import Mapping
from typing import Any, Iterable
from urllib.parse import parse_qsl, quote

import httpx

from agents.destination import (
    PROXY_TYPE_ON_PREMISE,
    ConnectivityConfig,
    Destination,
    DestinationError,
    DestinationResolver,
)

logger = logging.getLogger(__name__)

# The base URL a destination-backed client is built with. `.invalid` is
# reserved (RFC 2606) and never resolves, so a request that somehow escapes
# the rewrite fails at DNS rather than reaching anything.
PLACEHOLDER_HOST = "destination.invalid"
PLACEHOLDER_BASE = f"https://{PLACEHOLDER_HOST}"

# --- OnPremise: the connectivity proxy ---------------------------------------
ON_PREMISE = PROXY_TYPE_ON_PREMISE
PROXY_AUTH_HEADER = "Proxy-Authorization"
SCC_LOCATION_HEADER = "SAP-Connectivity-SCC-Location_ID"
PP_HEADER = "SAP-Connectivity-Authentication"
TECHNICAL_AUTH_HEADER = "SAP-Connectivity-Technical-Authentication"
# Decide which identity the proxy and the Cloud Connector see, so only the
# flow sets them: removed from every request before it leaves (lower case).
_IDENTITY_HEADERS = frozenset(
    h.lower()
    for h in (PROXY_AUTH_HEADER, SCC_LOCATION_HEADER, PP_HEADER, TECHNICAL_AUTH_HEADER)
)
# What a destination's `URL.headers.*` can never set: the above, and where
# the request goes.
_DESTINATION_RESERVED = _IDENTITY_HEADERS | {"host"}
PRINCIPAL_PROPAGATION = "PrincipalPropagation"
# How a signed-in user's identity reaches the proxy; see the module docstring.
PP_MODES = ("exchange", "header")
# `exchange` is what SAP recommends. Which one a landscape accepts is settled
# by the probe's `--user` run; `CONNECTIVITY_PP_MODE` switches without a build.
DEFAULT_PP_MODE = "exchange"
PP_MODE_ENV = "CONNECTIVITY_PP_MODE"
# Set on a request shaped for the proxy; `OnPremiseRouter` sends nothing
# through the proxy without it and nothing with it on the direct path.
PROXY_ROUTE_EXTENSION = "agents.destination_auth.connectivity_proxy"
_LOCATION_ID = re.compile(r"[A-Za-z0-9_.:@-]{1,128}")
_PROXY_HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")


class OnPremiseRefused(DestinationError):
    """An OnPremise destination that cannot be used as it is configured.

    ``admin_text`` is a fixed text that names the destination and nothing
    else of the landscape: what an admin screen may show as it is.
    """

    def __init__(self, message: str, *, admin_text: str) -> None:
        super().__init__(message)
        self.admin_text = admin_text


PP_MODE_TEXT = f"{PP_MODE_ENV} must be exchange or header"
PROXY_REFUSED_TEXT = "HTTP 407 from the connectivity proxy"


def proxy_refused_hint(user_context: bool) -> str:
    """What to check after a 407 of the connectivity proxy. Fixed text: the
    proxy's own answer is never read."""
    hint = (
        "the connectivity proxy refused the request before it reached SAP: check this "
        "app's connectivity service binding and the destination's CloudConnectorLocationId"
    )
    if user_context:
        hint += (
            f"; for a service that acts as the signed-in user also {PP_MODE_ENV} "
            "(exchange or header) and the Cloud Connector's trust configuration for "
            "principal propagation"
        )
    return hint


def went_through_proxy(response: httpx.Response) -> bool:
    """Whether the request this response answers was sent through the
    connectivity proxy (shaped and marked for it by :class:`DestinationAuth`).

    What tells a 407 of the connectivity proxy from a 407 of anything else:
    the route the request took, not the status."""
    try:
        return response.request.extensions.get(PROXY_ROUTE_EXTENSION) is True
    except RuntimeError:  # a response nobody attached a request to
        return False


def pp_mode_from_environment(environ: Mapping[str, str] | None = None) -> str:
    """The principal-propagation mode: ``CONNECTIVITY_PP_MODE``, else the default.

    A value that is neither mode is refused, without the value in the text:
    falling back would send a user's identity by a mechanism nobody chose.
    """
    raw = str((os.environ if environ is None else environ).get(PP_MODE_ENV) or "")
    mode = raw.strip().lower()
    if not mode:
        return DEFAULT_PP_MODE
    if mode not in PP_MODES:
        raise OnPremiseRefused(PP_MODE_TEXT, admin_text=PP_MODE_TEXT)
    return mode


@dataclasses.dataclass(frozen=True)
class _Route:
    """That an auth was built together with its transport (:func:`routed_auth`).

    ``tokens`` are the ``ConnectivityTokens``, or None when the app has no
    connectivity binding. Only the factory makes one.
    """

    tokens: Any


# `destination_http_client(connectivity=...)`: not passed at all is a caller
# that knows nothing of OnPremise; passed as None is one without a binding.
_NOT_GIVEN: Any = object()


def proxy_url_of(config: ConnectivityConfig) -> str:
    """``http://<proxy host>:<port>`` of the connectivity binding."""
    host = str(config.proxy_host or "")
    if not _PROXY_HOST.fullmatch(host) or not 0 < int(config.proxy_port) < 65536:
        raise DestinationError(
            "the connectivity service binding names no usable on-premise proxy host and port"
        )
    return f"http://{host}:{int(config.proxy_port)}"


def _is_on_premise(destination: Destination) -> bool:
    return (destination.proxy_type or "").strip().lower() == ON_PREMISE.lower()


def _scheme_of(url: str) -> str:
    try:
        return httpx.URL(url).scheme
    except httpx.InvalidURL:
        return ""


class DestinationUserRequired(DestinationError):
    """A per-user destination was asked for, but no user token is bound.

    Raised only when ``current_jwt`` is empty. A run started from a request
    that carried a bearer token inherits that token and does not get here,
    whatever its principal (see the module docstring), so the message's
    "scheduled and API-triggered runs carry no user token" describes the
    token-less run only.
    """

    def __init__(self, server_key: str, destination: str) -> None:
        self.server_key = server_key
        self.destination = destination
        super().__init__(
            f"{server_key} is configured to act as the signed-in user through "
            f"destination {destination!r}, but this run has no signed-in user: "
            f"scheduled and API-triggered runs carry no user token. Either "
            f"trigger this agent from the chat, or turn 'Act as signed-in "
            f"user' off and give the destination an app-level credential "
            f"(OAuth2ClientCredentials, or a static URL.headers.Authorization)."
        )


def _host_of(url: str) -> str:
    return (httpx.URL(url).host or "").lower()


class DestinationAuth(httpx.Auth):
    """Resolve a destination per request and send the request through it.

    ``expected_hosts`` are the API hosts the toolset was written against. A
    destination pointing elsewhere is accepted -- it is a proxy the subaccount
    admin chose -- but said once in the log, because a wrong destination
    otherwise shows up only as a puzzling 404 from the wrong server.
    """

    requires_request_body = False
    requires_response_body = False

    def __init__(
        self,
        resolver: Any,
        *,
        user_context: bool = False,
        expected_hosts: Iterable[str] = (),
        server_key: str = "",
        retry_on_401: bool = True,
        pp_mode: str | None = None,
        _route: _Route | None = None,
    ) -> None:
        self._resolver = resolver
        self.user_context = bool(user_context)
        # False for a caller whose resolver is new for the one request it
        # makes: there is no aged cache entry a second attempt could fix, so
        # a retry would only repeat a refused logon. It turns the 407 retry
        # off as well: such a caller's connectivity tokens are new too.
        self.retry_on_401 = bool(retry_on_401)
        # Set by `routed_auth` only, which builds the router with it: an auth
        # that knows the connectivity tokens on a client without the router
        # would send a proxy token on the direct path.
        if _route is not None and not isinstance(_route, _Route):
            raise TypeError("a connectivity-aware auth is built by routed_auth()")
        self._route = _route
        self._connectivity = _route.tokens if _route is not None else None
        if pp_mode is not None and pp_mode not in PP_MODES:
            raise ValueError(f"pp_mode must be one of {', '.join(PP_MODES)}")
        # None: the environment's, read when a user's OnPremise request needs it.
        self._pp_mode = pp_mode
        self._route_noted = False
        self._queries_noted = False
        self.expected_hosts = tuple(h.lower() for h in expected_hosts)
        self.server_key = server_key or "destination"
        self._proxy_noted = False
        # Where `_apply` last pointed a request; see `_shape`.
        self._applied: httpx.URL | None = None

    @property
    def destination_name(self) -> str:
        return str(getattr(self._resolver, "name", "") or "")

    # -- identity -----------------------------------------------------------
    def _user(self) -> tuple[str | None, str | None]:
        """``(jwt, principal)`` of the signed-in user, or ``(None, None)``."""
        if not self.user_context:
            return None, None
        from agents.auth import current_jwt, current_principal, principal_from_token

        token = current_jwt.get()
        if not token:
            raise DestinationUserRequired(self.server_key, self.destination_name)
        principal = current_principal.get() or principal_from_token(token)
        return token, principal

    async def _resolve(
        self, token: str | None, principal: str | None, *, force: bool = False
    ) -> Destination:
        if token:
            return await self._resolver.resolve(
                force=force, user_token=token, principal=principal
            )
        return await self._resolver.resolve(force=force)

    # -- request shaping ----------------------------------------------------
    def _target(self, request: httpx.Request, destination: Destination) -> httpx.URL:
        """Where the request goes once the destination is known.

        A placeholder URL is rewritten onto the destination; an absolute one
        is allowed only when it already names the destination's host or one
        of the expected API hosts (a paging link), and refused otherwise.
        """
        dest_url = httpx.URL(destination.url)
        if not self._through_proxy(destination) and dest_url.scheme != "https":
            raise DestinationError(
                f"destination {self.destination_name!r} must use https://, "
                f"not {dest_url.scheme!r}: its credential travels with every request"
            )
        dest_host = (dest_url.host or "").lower()
        if not dest_host:
            raise DestinationError(f"destination {self.destination_name!r} has no host in its URL")
        if self.expected_hosts and dest_host not in self.expected_hosts and not self._proxy_noted:
            self._proxy_noted = True
            logger.info(
                "%s: destination %r points at %s rather than %s; treating it as a proxy",
                self.server_key, self.destination_name, dest_host,
                " or ".join(self.expected_hosts),
            )
        host = (request.url.host or "").lower()
        if host == PLACEHOLDER_HOST:
            prefix = dest_url.path.rstrip("/")
            path = request.url.path
            return request.url.copy_with(
                scheme=dest_url.scheme,
                host=dest_url.host,
                port=dest_url.port,
                path=f"{prefix}{path}" if prefix else path,
            )
        if host == dest_host or host in self.expected_hosts:
            return request.url
        raise DestinationError(
            f"{self.server_key}: refusing to send destination {self.destination_name!r}'s "
            f"credential to {host!r}; the destination names {dest_host!r}"
        )

    def _apply(self, request: httpx.Request, destination: Destination) -> None:
        target = self._target(request, destination)
        if target != request.url:
            request.url = target
            # httpx set Host when the request was built, from the placeholder.
            request.headers["Host"] = target.netloc.decode("ascii")
        for key, value in destination.headers.items():
            if key.lower() not in _DESTINATION_RESERVED:
                request.headers[key] = value
        self._add_queries(request, destination)
        self._applied = request.url

    def _add_queries(self, request: httpx.Request, destination: Destination) -> None:
        """Append the destination's ``URL.queries.*`` the request lacks.

        The caller's own query is left byte for byte as it is (an OData
        ``$filter`` must not be re-encoded on the way); a name the caller
        already sends wins, compared without case because SAP reads
        ``sap-client`` that way. Applied again on a retry it adds nothing.

        A client built for the connectivity route is an OData caller, and
        for those a destination's ``$``-prefixed names are skipped (said
        once per auth, with the destination's name only): ``$filter``,
        ``$expand``, ``$top``, ``$format`` or ``$skiptoken`` from a
        destination would get behind the argument checks of the tools. Such
        a caller also takes one spelling per name, the destination's first.
        """
        if not destination.queries:
            return
        odata = self._route is not None
        raw = request.url.query
        present = {
            name.lower()
            for name, _ in parse_qsl(raw.decode("ascii", "replace"), keep_blank_values=True)
        }
        pairs: list[str] = []
        skipped = False
        for name, value in destination.queries.items():
            name = str(name)
            if name.lower() in present:
                continue
            if odata:
                if name.startswith("$"):
                    skipped = True
                    continue
                present.add(name.lower())
            pairs.append(f"{quote(name, safe='')}={quote(str(value), safe='')}")
        if skipped and not self._queries_noted:
            self._queries_noted = True
            logger.warning(
                "%s: destination %r has URL.queries properties that start with '$'; "
                "they are not sent (OData query options come from the tool only)",
                self.server_key,
                self.destination_name,
            )
        extra = "&".join(pairs).encode("ascii")
        if extra:
            request.url = request.url.copy_with(query=raw + b"&" + extra if raw else extra)

    # -- OnPremise ----------------------------------------------------------
    def _through_proxy(self, destination: Destination) -> bool:
        """Whether requests for ``destination`` go through the connectivity
        proxy; raises when it is OnPremise and cannot.

        The one place that allows ``http://``: an OnPremise destination on
        an auth built for the connectivity route (``routed_auth``), with the
        connectivity tokens to really send it through the proxy. Such an
        auth refuses an OnPremise destination that is not ``http://`` and one
        it has no binding for (:class:`OnPremiseRefused`). Any other auth --
        a built-in that knows nothing of OnPremise -- refuses an ``http://``
        one saying so, and leaves an ``https://`` one to the rules every
        destination had before (it is not routed, and gets no proxy token).
        """
        if not _is_on_premise(destination):
            return False
        name = self.destination_name
        scheme = _scheme_of(destination.url)
        if self._route is None:
            # A built-in that was not built for the connectivity route
            # (Gmail, Jira, MCP ...): a binding would not help it.
            if scheme == "http":
                text = (
                    f"{self.server_key} cannot reach OnPremise destinations (only OData "
                    f"services go through the connectivity proxy)"
                )
                raise OnPremiseRefused(f"{text}: destination {name!r}", admin_text=text)
            return False
        if scheme != "http":
            raise OnPremiseRefused(
                f"OnPremise destination {name!r} must use "
                f"http://<virtual host>:<port>: the connectivity proxy forwards plain "
                f"HTTP and the Cloud Connector tunnel is what encrypts it",
                admin_text=(
                    f"OnPremise destination '{name}' must use an http:// address (virtual "
                    f"host and port): the Cloud Connector tunnel is what encrypts it"
                ),
            )
        if self._connectivity is None:
            text = (
                f"destination '{name}' is an OnPremise destination, but this app has no "
                f"connectivity service binding"
            )
            raise OnPremiseRefused(text, admin_text=text)
        return True

    def _mode(self) -> str:
        """The principal-propagation mode of this request: the one the auth
        was built with, else the environment's -- read here, by a user's
        OnPremise request only. Raises for a value that is neither mode."""
        return self._pp_mode if self._pp_mode is not None else pp_mode_from_environment()

    async def _token(self, fetch: Any, drop: Any, refused: str | None) -> str:
        """A connectivity token; after a 407, one that is not the refused one.

        ``refused`` is the token the proxy just answered 407 to. It is
        dropped only when it is still the cached one: when another request
        renewed it meanwhile, that newer token is used and nothing is
        fetched again. So a 407 costs at most one token request, also for
        the application's token that every user shares in ``header`` mode.
        """
        token = await fetch()
        if refused is not None and token == refused:
            drop()
            token = await fetch()
        return token

    async def _proxy_headers(
        self,
        destination: Destination,
        token: str | None,
        principal: str | None,
        *,
        refused: dict[str, str] | None = None,
    ) -> dict[str, str] | None:
        """The connectivity headers of one attempt; ``None`` = not through the proxy.

        The ONE function that decides which identity the proxy sees. A user
        run (``user_context``) has no branch that reaches the application's
        identity: without the user's JWT it raises, on a destination that is
        not ``PrincipalPropagation`` it raises, and in ``header`` mode the
        application's token only opens the proxy while the user's JWT rides
        next to it. Every refusal comes before a token is asked for.

        ``refused`` is what this function answered for the attempt the proxy
        just refused with a 407 (see ``_token``).
        """
        if not self._through_proxy(destination):
            return None
        name = self.destination_name
        auth_type = (destination.auth_type or "").strip()
        location = destination.location_id or ""
        if location and not _LOCATION_ID.fullmatch(location):
            raise DestinationError(
                f"destination {name!r} has a CloudConnectorLocationId that is not a plain "
                f"identifier; refusing to send it as a header"
            )
        tokens = self._connectivity
        headers: dict[str, str] = {}
        old = (refused or {}).get(PROXY_AUTH_HEADER, "").removeprefix("Bearer ") or None
        mode = ""

        async def app() -> str:
            return await self._token(tokens.app_token, tokens.invalidate, old)

        if self.user_context:
            if not token:
                raise DestinationUserRequired(self.server_key, name)
            if auth_type != PRINCIPAL_PROPAGATION:
                raise OnPremiseRefused(
                    f"{self.server_key}: OnPremise destination {name!r} is set to act as "
                    f"the signed-in user, which needs Authentication PrincipalPropagation; "
                    f"its own credential is never sent next to a user's identity",
                    admin_text=(
                        f"OnPremise destination '{name}' cannot act as the signed-in user: "
                        f"its Authentication must be PrincipalPropagation"
                    ),
                )
            mode = self._mode()  # raises for an unknown value: before any token call
            if mode == "exchange":
                exchanged = await self._token(
                    lambda: tokens.user_token(token, principal),
                    lambda: tokens.invalidate(tokens.user_key(principal, token)),
                    old,
                )
                headers[PROXY_AUTH_HEADER] = f"Bearer {exchanged}"
            else:
                headers[PROXY_AUTH_HEADER] = f"Bearer {await app()}"
                headers[PP_HEADER] = f"Bearer {token}"
        else:
            if auth_type == PRINCIPAL_PROPAGATION:
                raise OnPremiseRefused(
                    f"destination {name!r} uses PrincipalPropagation, which needs the "
                    f"signed-in user; resolve it with user context or use a "
                    f"technical-user destination",
                    admin_text=(
                        f"OnPremise destination '{name}' propagates the signed-in user: a "
                        f"service that runs as a technical user needs a destination with a "
                        f"stored credential"
                    ),
                )
            headers[PROXY_AUTH_HEADER] = f"Bearer {await app()}"
        if location:
            headers[SCC_LOCATION_HEADER] = location
        if not self._route_noted:
            self._route_noted = True
            logger.info(
                "%s: destination %r goes through the connectivity proxy as %s",
                self.server_key,
                name,
                f"the signed-in user ({mode})" if self.user_context else "a technical user",
            )
        return headers

    def on_resolved(self, destination: Destination) -> None:
        """Called with every destination the flow resolved, before any rule
        looks at it -- also one that is then refused. For a subclass that
        reports what was resolved; it decides nothing."""

    def send_through(self, request: httpx.Request, destination: Destination) -> None:
        """Shape ``request`` for ``destination``, just before it is sent.

        The extension point of this class: called once per attempt with the
        destination that attempt resolved. A subclass may refuse the
        destination first (raise a :class:`DestinationError`; nothing is
        sent), must call ``super().send_through(...)`` to get the URL
        rewrite, the https and host rules and the destination's headers,
        and may pin headers of its own afterwards. It does not decide where
        the request goes: the flow sends only a request that still points
        at the scheme, host and port those rules chose (``_shape``).
        """
        self._apply(request, destination)

    def _shape(
        self,
        request: httpx.Request,
        destination: Destination,
        proxy: dict[str, str] | None = None,
    ) -> None:
        """``send_through``, then the check that it left the target alone.

        The https and host rules run inside ``_apply``. An override that
        never reaches it, or that moves the request afterwards, would send
        the destination's credential past them, so such a request is not
        sent. No ``await`` lies between the two steps: ``_applied`` cannot
        be another request's.

        ``proxy`` is what ``_proxy_headers`` answered for this attempt. The
        connectivity headers are set here, last, and any the request already
        carries are removed first: they cannot come from the destination, the
        caller or an override of ``send_through``. On a user's request
        through the proxy no ``Authorization`` is left either, whoever set it.
        """
        self._applied = None
        if proxy is not None and self.user_context:
            # A user's request through the proxy: what the destination
            # stores is not even handed to the shaping code.
            destination = dataclasses.replace(destination, headers={})
        self.send_through(request, destination)
        applied, self._applied = self._applied, None
        url = request.url
        if applied is None or (url.scheme, url.host, url.port) != (
            applied.scheme,
            applied.host,
            applied.port,
        ):
            raise DestinationError(
                f"{self.server_key}: the request for destination "
                f"{self.destination_name!r} does not point where the destination "
                f"rules put it; refusing to send"
            )
        for name in _IDENTITY_HEADERS:
            if name in request.headers:
                del request.headers[name]
        if proxy is not None and self.user_context and "authorization" in request.headers:
            # The caller's, or a subclass's: a credential next to the user's
            # identity is what a user run never sends.
            del request.headers["authorization"]
        request.extensions.pop(PROXY_ROUTE_EXTENSION, None)
        if proxy is not None:
            dest_url = httpx.URL(destination.url)
            if (url.scheme, url.host, url.port) != ("http", dest_url.host, dest_url.port):
                # Also an absolute link to the virtual host on another port
                # or over https: the proxy token goes where the destination
                # points and nowhere else.
                raise DestinationError(
                    f"{self.server_key}: a request for OnPremise destination "
                    f"{self.destination_name!r} must go to the destination's own "
                    f"http:// address; refusing to send"
                )
            for name, value in proxy.items():
                request.headers[name] = value
            request.extensions[PROXY_ROUTE_EXTENSION] = True
        elif self._connectivity is not None and url.scheme != "https":
            raise DestinationError(
                f"{self.server_key}: the request for destination "
                f"{self.destination_name!r} is not https://; refusing to send"
            )

    # -- the flow -----------------------------------------------------------
    async def async_auth_flow(self, request):  # type: ignore[override]
        token, principal = self._user()
        destination = await self._resolve(token, principal)
        self.on_resolved(destination)
        proxy = await self._proxy_headers(destination, token, principal)
        self._shape(request, destination, proxy)
        response = yield request

        if not self.retry_on_401:
            return
        if response.status_code == 407 and proxy is not None:
            # The proxy refused its token: aged out or revoked. Drop the one
            # that was used -- this user's only in exchange mode, and only if
            # it is still the cached one -- and retry exactly once. The
            # destination was not the problem and stays cached.
            proxy = await self._proxy_headers(destination, token, principal, refused=proxy)
            self._shape(request, destination, proxy)
            yield request
        elif response.status_code == 401:
            # The cached entry aged out or was revoked. Drop it -- this user's
            # only, under user_context -- and retry exactly once. A second 401
            # is a real refusal and must not become a loop.
            if token:
                if principal:
                    self._resolver.invalidate(principal)
            else:
                self._resolver.invalidate()
            destination = await self._resolve(token, principal, force=True)
            self.on_resolved(destination)
            proxy = await self._proxy_headers(destination, token, principal)
            self._shape(request, destination, proxy)
            yield request

    def sync_auth_flow(self, request):  # type: ignore[override]
        raise RuntimeError("DestinationAuth supports async clients only")


class OnPremiseRouter(httpx.AsyncBaseTransport):
    """Send a request through the connectivity proxy, or directly. Never both
    ways, and never a proxy token on the direct path.

    Two conditions pick the proxy, and both must hold: the request was shaped
    for it by :class:`DestinationAuth` (``PROXY_ROUTE_EXTENSION``) and it is
    an ``http://`` request carrying ``Proxy-Authorization``. The direct path
    takes only an ``https://`` request with none of the proxy's identity
    headers. Anything in between is a defect somewhere above and is refused
    here, where it would otherwise leave: a header alone must not route (a
    hand-built request could carry one), and a mark alone must not either.
    """

    def __init__(
        self, *, direct: httpx.AsyncBaseTransport, proxied: httpx.AsyncBaseTransport
    ) -> None:
        self._direct = direct
        self._proxied = proxied

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        marked = request.extensions.get(PROXY_ROUTE_EXTENSION) is True
        token = PROXY_AUTH_HEADER in request.headers
        user = PP_HEADER in request.headers
        scheme = request.url.scheme
        if marked and token and scheme == "http":
            failure: str | None = None
            try:
                return await self._proxied.handle_async_request(request)
            except httpx.LocalProtocolError as exc:
                # h11 quotes the header bytes it refuses: the class only.
                failure = type(exc).__name__
            # Raised outside the `except`: no chained error with the header in it.
            raise DestinationError(
                f"the request could not be sent through the connectivity proxy: {failure}"
            )
        if not marked and not token and not user and scheme == "https":
            return await self._direct.handle_async_request(request)
        raise DestinationError(
            "refusing to send: the request is neither a plain https:// request nor one "
            "shaped for the connectivity proxy"
        )

    async def aclose(self) -> None:
        try:
            await self._direct.aclose()
        finally:
            await self._proxied.aclose()


def routed_auth(
    resolver: Any,
    connectivity: Any,
    *,
    auth_class: type[DestinationAuth] = DestinationAuth,
    direct: httpx.AsyncBaseTransport | None = None,
    proxied: httpx.AsyncBaseTransport | None = None,
    **auth_args: Any,
) -> tuple[DestinationAuth, httpx.AsyncBaseTransport | None]:
    """The auth and the transport of a client that may reach OnPremise
    destinations: ``(auth, transport)``, to be given to ONE ``httpx`` client.

    The only place a connectivity-aware auth is built, so that it never
    exists without the router that keeps its proxy token off the direct
    path. ``connectivity`` is the app's ``ConnectivityTokens``, or None
    when there is no binding: the auth then refuses an OnPremise
    destination with the missing binding as the reason, and the transport
    is ``direct`` as it was given.

    ``direct`` and ``proxied`` are seams for tests. Left out, the direct
    side is httpx's own transport and the proxied side one pointed at the
    binding's on-premise proxy; building them sends nothing. ``trust_env``
    is off for the proxied side, as in the probe.
    """
    if not (inspect.isclass(auth_class) and issubclass(auth_class, DestinationAuth)):
        raise TypeError("auth_class must be a DestinationAuth")
    transport: httpx.AsyncBaseTransport | None = direct
    if connectivity is not None:
        if proxied is None:
            proxied = httpx.AsyncHTTPTransport(
                proxy=httpx.Proxy(proxy_url_of(connectivity.config)), trust_env=False
            )
        transport = OnPremiseRouter(
            direct=direct if direct is not None else httpx.AsyncHTTPTransport(),
            proxied=proxied,
        )
    auth = auth_class(resolver, _route=_Route(connectivity), **auth_args)
    return auth, transport


def destination_http_client(
    resolver: Any,
    *,
    user_context: bool = False,
    expected_hosts: Iterable[str] = (),
    server_key: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 30.0,
    connectivity: Any = _NOT_GIVEN,
    proxy_transport: httpx.AsyncBaseTransport | None = None,
    pp_mode: str | None = None,
) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` whose relative requests go through ``resolver``.

    ``transport`` is a seam for tests; the request a mock transport sees is
    the rewritten one, so a test can assert on the destination's URL and
    headers exactly as the target would.

    ``connectivity`` (``ConnectivityTokens``) lets the client reach OnPremise
    destinations through the connectivity proxy; ``proxy_transport`` is the
    test seam of that side and ``pp_mode`` the principal-propagation mode
    (default: the environment's, read per user request). Passed as None it
    says "this caller would, but the app has no connectivity binding": the
    refusal of an OnPremise destination then names the binding. Not passed
    at all, the client is exactly the one this function always returned.
    """
    if connectivity is not _NOT_GIVEN:
        auth, routed = routed_auth(
            resolver,
            connectivity,
            direct=transport,
            proxied=proxy_transport,
            user_context=user_context,
            expected_hosts=expected_hosts,
            server_key=server_key,
            pp_mode=pp_mode,
        )
        return httpx.AsyncClient(
            base_url=PLACEHOLDER_BASE,
            auth=auth,
            timeout=httpx.Timeout(timeout),
            transport=routed,
        )
    return httpx.AsyncClient(
        base_url=PLACEHOLDER_BASE,
        auth=DestinationAuth(
            resolver,
            user_context=user_context,
            expected_hosts=expected_hosts,
            server_key=server_key,
        ),
        timeout=httpx.Timeout(timeout),
        transport=transport,
    )


def resolver_for(
    oauth: dict[str, Any],
    server_key: str,
    *,
    destination: str | None = None,
    require_credential: bool = True,
) -> DestinationResolver:
    """The resolver a destination-mode built-in should use.

    The name comes from ``destination`` or the config block's ``destination``
    key; an empty name is refused here, at build time, with a message naming
    the server, rather than from inside the first tool call.
    """
    from agents.destination import resolver_from_environment

    name = (
        destination if destination is not None else str(oauth.get("destination") or "")
    ).strip()
    if not name:
        raise ValueError(
            f"{server_key} with auth_mode 'destination' requires a 'destination' "
            f"in its config: the name of the BTP destination holding the target's "
            f"URL and credential"
        )
    return resolver_from_environment(
        name, server_key=server_key, require_credential=require_credential
    )


def user_context_of(oauth: dict[str, Any]) -> bool:
    """Whether a destination-mode config acts as the signed-in user.

    ``is True`` rather than ``bool()``: the JSON string ``"false"`` is truthy,
    and this switch decides whose token a request carries.
    """
    return (oauth or {}).get("user_context") is True
