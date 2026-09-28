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
link would loop. Scheduled and API-triggered runs never carry a user token, so
an agent that must run unattended needs ``user_context`` off and an app-level
credential in the destination.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

import httpx

from agents.destination import Destination, DestinationError, DestinationResolver

logger = logging.getLogger(__name__)

# The base URL a destination-backed client is built with. `.invalid` is
# reserved (RFC 2606) and never resolves, so a request that somehow escapes
# the rewrite fails at DNS rather than reaching anything.
PLACEHOLDER_HOST = "destination.invalid"
PLACEHOLDER_BASE = f"https://{PLACEHOLDER_HOST}"


class DestinationUserRequired(DestinationError):
    """A per-user destination was asked for, but no user is signed in."""

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
    ) -> None:
        self._resolver = resolver
        self.user_context = bool(user_context)
        self.expected_hosts = tuple(h.lower() for h in expected_hosts)
        self.server_key = server_key or "destination"
        self._proxy_noted = False

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
        if dest_url.scheme != "https":
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
            request.headers[key] = value

    # -- the flow -----------------------------------------------------------
    async def async_auth_flow(self, request):  # type: ignore[override]
        token, principal = self._user()
        destination = await self._resolve(token, principal)
        self._apply(request, destination)
        response = yield request

        if response.status_code == 401:
            # The cached entry aged out or was revoked. Drop it -- this user's
            # only, under user_context -- and retry exactly once. A second 401
            # is a real refusal and must not become a loop.
            if token:
                if principal:
                    self._resolver.invalidate(principal)
            else:
                self._resolver.invalidate()
            destination = await self._resolve(token, principal, force=True)
            self._apply(request, destination)
            yield request

    def sync_auth_flow(self, request):  # type: ignore[override]
        raise RuntimeError("DestinationAuth supports async clients only")


def destination_http_client(
    resolver: Any,
    *,
    user_context: bool = False,
    expected_hosts: Iterable[str] = (),
    server_key: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 30.0,
) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` whose relative requests go through ``resolver``.

    ``transport`` is a seam for tests; the request a mock transport sees is
    the rewritten one, so a test can assert on the destination's URL and
    headers exactly as the target would.
    """
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
