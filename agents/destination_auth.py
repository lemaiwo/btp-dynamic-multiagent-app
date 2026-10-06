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
and only by a caller that hands this auth the connectivity tokens
(``connectivity=``) and sends through :class:`OnPremiseRouter` --
``destination_http_client(..., connectivity=...)`` does both. The mechanism
is the one ``scripts/probe_odata_connectivity.py`` proved against a
landscape: the request keeps the destination's ``http://`` URL, goes to the
proxy in HTTP forward mode (no CONNECT tunnel) and carries a per-request
``Proxy-Authorization``.

* **Technical user** (``user_context`` off): the application's connectivity
  token in ``Proxy-Authorization``; the destination's own ``Authorization``
  travels to the target as for an Internet destination.
* **Signed-in user** (``user_context`` on): the destination must be
  ``PrincipalPropagation``, and nothing stored in the destination is sent --
  a credential next to the user's identity would let the target answer as
  the technical user and pass for principal propagation. The identity
  travels in one of two ways (:data:`DEFAULT_PP_MODE`, env
  ``CONNECTIVITY_PP_MODE``): ``exchange`` puts a token exchanged from the
  user's JWT at the connectivity instance's XSUAA into
  ``Proxy-Authorization``; ``header`` puts the application's token there and
  the user's JWT into ``SAP-Connectivity-Authentication``. No JWT bound is
  :class:`DestinationUserRequired` before any token or proxy call; there is
  no path from a user run to the application's identity.
* ``SAP-Connectivity-SCC-Location_ID`` is sent when the destination names a
  Cloud Connector location.
* ``http://`` is accepted for an OnPremise destination that goes through the
  proxy, and for nothing else; an ``https://`` OnPremise URL is refused,
  because a CONNECT tunnel would carry the request's ``Proxy-Authorization``
  to the target instead of the proxy.
* The three connectivity headers are set by the flow only. A destination's
  ``URL.headers.*``, a caller and a subclass cannot set them, and the router
  refuses a request that carries one without having been shaped for the
  proxy, so the proxy token never leaves on the direct path.
* A 407 from the proxy drops the connectivity token that was used (that
  user's only) and retries once, like the 401 rule.

A destination's ``URL.queries.*`` properties (``sap-client``, ...) are added
to every request, OnPremise or not; a parameter the caller already sends
wins, whatever its case.
"""

from __future__ import annotations

import dataclasses
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
# Decide which identity the proxy and the Cloud Connector see, so only the
# flow sets them (compared in lower case).
_RESERVED_HEADERS = frozenset(
    h.lower() for h in (PROXY_AUTH_HEADER, SCC_LOCATION_HEADER, PP_HEADER)
)
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


def pp_mode_from_environment(environ: Mapping[str, str] | None = None) -> str:
    """The principal-propagation mode: ``CONNECTIVITY_PP_MODE``, else the default.

    A value that is neither mode falls back to the default with a warning
    (the value itself is not logged). That is safe because both modes are
    user modes: neither is a way to the technical user.
    """
    raw = str((os.environ if environ is None else environ).get(PP_MODE_ENV) or "")
    mode = raw.strip().lower()
    if not mode:
        return DEFAULT_PP_MODE
    if mode not in PP_MODES:
        logger.warning(
            "%s is not one of %s; using %r", PP_MODE_ENV, " | ".join(PP_MODES), DEFAULT_PP_MODE
        )
        return DEFAULT_PP_MODE
    return mode


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
        connectivity: Any = None,
        pp_mode: str | None = None,
    ) -> None:
        self._resolver = resolver
        self.user_context = bool(user_context)
        # False for a caller whose resolver is new for the one request it
        # makes: there is no aged cache entry a second attempt could fix, so
        # a retry would only repeat a refused logon. It turns the 407 retry
        # off as well: such a caller's connectivity tokens are new too.
        self.retry_on_401 = bool(retry_on_401)
        # `ConnectivityTokens`, or None: then no request of this auth goes
        # through the connectivity proxy. A caller that passes it must send
        # through an `OnPremiseRouter` (`connectivity_transport`).
        self._connectivity = connectivity
        if pp_mode is None:
            pp_mode = pp_mode_from_environment()
        if pp_mode not in PP_MODES:
            raise ValueError(f"pp_mode must be one of {', '.join(PP_MODES)}")
        self.pp_mode = pp_mode
        self._route_noted = False
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
            if key.lower() not in _RESERVED_HEADERS:
                request.headers[key] = value
        self._add_queries(request, destination)
        self._applied = request.url

    @staticmethod
    def _add_queries(request: httpx.Request, destination: Destination) -> None:
        """Append the destination's ``URL.queries.*`` the request lacks.

        The caller's own query is left byte for byte as it is (an OData
        ``$filter`` must not be re-encoded on the way); a name the caller
        already sends wins, compared without case because SAP reads
        ``sap-client`` that way. Applied again on a retry it adds nothing.
        """
        if not destination.queries:
            return
        raw = request.url.query
        present = {
            name.lower()
            for name, _ in parse_qsl(raw.decode("ascii", "replace"), keep_blank_values=True)
        }
        extra = "&".join(
            f"{quote(str(name), safe='')}={quote(str(value), safe='')}"
            for name, value in destination.queries.items()
            if str(name).lower() not in present
        ).encode("ascii")
        if extra:
            request.url = request.url.copy_with(query=raw + b"&" + extra if raw else extra)

    # -- OnPremise ----------------------------------------------------------
    def _through_proxy(self, destination: Destination) -> bool:
        """Whether requests for ``destination`` go through the connectivity
        proxy; raises when it is OnPremise and cannot.

        The one place that allows ``http://``: an OnPremise destination, and
        only with the connectivity tokens to really send it through the
        proxy. Without them an ``http://`` OnPremise destination is refused
        with the reason; an ``https://`` one is left to the rules every
        destination had before (it is not routed, and gets no proxy token).
        """
        if not _is_on_premise(destination):
            return False
        scheme = _scheme_of(destination.url)
        if self._connectivity is None:
            if scheme == "http":
                raise DestinationError(
                    f"destination {self.destination_name!r} is an OnPremise destination, "
                    f"but this app has no connectivity service binding"
                )
            return False
        if scheme != "http":
            raise DestinationError(
                f"OnPremise destination {self.destination_name!r} must use "
                f"http://<virtual host>:<port>: the connectivity proxy forwards plain "
                f"HTTP and the Cloud Connector tunnel is what encrypts it"
            )
        return True

    async def _proxy_headers(
        self,
        destination: Destination,
        token: str | None,
        principal: str | None,
        *,
        force: bool = False,
    ) -> dict[str, str] | None:
        """The connectivity headers of one attempt; ``None`` = not through the proxy.

        The ONE function that decides which identity the proxy sees. A user
        run (``user_context``) has no branch that reaches the application's
        identity: without the user's JWT it raises, on a destination that is
        not ``PrincipalPropagation`` it raises, and in ``header`` mode the
        application's token only opens the proxy while the user's JWT rides
        next to it. Every refusal comes before a token is asked for.
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
        if self.user_context:
            if not token:
                raise DestinationUserRequired(self.server_key, name)
            if auth_type != PRINCIPAL_PROPAGATION:
                raise DestinationError(
                    f"{self.server_key}: OnPremise destination {name!r} is set to act as "
                    f"the signed-in user, which needs Authentication PrincipalPropagation; "
                    f"its own credential is never sent next to a user's identity"
                )
            if self.pp_mode == "exchange":
                exchanged = await tokens.user_token(token, principal, force=force)
                headers[PROXY_AUTH_HEADER] = f"Bearer {exchanged}"
            else:
                app = await tokens.app_token(force=force)
                headers[PROXY_AUTH_HEADER] = f"Bearer {app}"
                headers[PP_HEADER] = f"Bearer {token}"
        else:
            if auth_type == PRINCIPAL_PROPAGATION:
                raise DestinationError(
                    f"destination {name!r} uses PrincipalPropagation, which needs the "
                    f"signed-in user; resolve it with user context or use a "
                    f"technical-user destination"
                )
            app = await tokens.app_token(force=force)
            headers[PROXY_AUTH_HEADER] = f"Bearer {app}"
        if location:
            headers[SCC_LOCATION_HEADER] = location
        if not self._route_noted:
            self._route_noted = True
            logger.info(
                "%s: destination %r goes through the connectivity proxy as %s",
                self.server_key,
                name,
                f"the signed-in user ({self.pp_mode})" if self.user_context else "a technical user",
            )
        return headers

    def _drop_proxy_token(self, token: str | None, principal: str | None) -> None:
        """Forget the connectivity token the last attempt used -- that one only."""
        tokens = self._connectivity
        if self.user_context and self.pp_mode == "exchange" and token:
            tokens.invalidate(tokens.user_key(principal, token))
        else:
            tokens.invalidate()

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
        caller or an override of ``send_through``.
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
        for name in _RESERVED_HEADERS:
            if name in request.headers:
                del request.headers[name]
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
        proxy = await self._proxy_headers(destination, token, principal)
        self._shape(request, destination, proxy)
        response = yield request

        if not self.retry_on_401:
            return
        if response.status_code == 407 and proxy is not None:
            # The proxy refused its token: aged out or revoked. Drop the one
            # that was used -- this user's only in exchange mode -- and retry
            # exactly once with a new one. The destination was not the
            # problem and stays cached.
            self._drop_proxy_token(token, principal)
            proxy = await self._proxy_headers(destination, token, principal, force=True)
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


def connectivity_transport(
    connectivity: Any,
    *,
    direct: httpx.AsyncBaseTransport | None = None,
    proxied: httpx.AsyncBaseTransport | None = None,
) -> OnPremiseRouter:
    """The transport of a client whose auth was given ``connectivity``.

    ``direct`` and ``proxied`` are seams for tests. Left out, the direct side
    is httpx's own transport and the proxied side one pointed at the
    binding's on-premise proxy. Building it sends nothing. ``trust_env`` is
    off for the proxied side, as in the probe: nothing in the environment
    may change where a request with a proxy token goes.

    Because the router is passed to the client as its transport, httpx does
    not apply ``HTTP_PROXY``/``HTTPS_PROXY`` from the environment to such a
    client -- on either path.
    """
    if proxied is None:
        proxied = httpx.AsyncHTTPTransport(
            proxy=httpx.Proxy(proxy_url_of(connectivity.config)), trust_env=False
        )
    if direct is None:
        direct = httpx.AsyncHTTPTransport()
    return OnPremiseRouter(direct=direct, proxied=proxied)


def destination_http_client(
    resolver: Any,
    *,
    user_context: bool = False,
    expected_hosts: Iterable[str] = (),
    server_key: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 30.0,
    connectivity: Any = None,
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
    (default: the environment's). Without ``connectivity`` the client is
    exactly the one this function always returned, and an ``http://``
    OnPremise destination is refused with the missing binding as the reason.
    """
    if connectivity is not None:
        return httpx.AsyncClient(
            base_url=PLACEHOLDER_BASE,
            auth=DestinationAuth(
                resolver,
                user_context=user_context,
                expected_hosts=expected_hosts,
                server_key=server_key,
                connectivity=connectivity,
                pp_mode=pp_mode,
            ),
            timeout=httpx.Timeout(timeout),
            transport=connectivity_transport(
                connectivity, direct=transport, proxied=proxy_transport
            ),
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
