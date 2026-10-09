"""Bitbucket Cloud pull requests as an in-process toolset (``builtin:bitbucket``).

An agent that lists the pseudo-URL ``builtin:bitbucket`` reads the pull
requests of ONE workspace through the REST API 2.0 and, when its entry says
so, comments on them or approves them, as a technical user.

**Scope is config, never a tool argument.** The workspace, the repositories,
the target branch and the write switches are pinned in the entry
(``agents/bitbucket_config.py`` is the one reading of it). No host, URL or
destination name ever comes from the model.

**Every failure is** ``{"error": {code, message, hint?}}`` **with a fixed
text**, never an exception and never Bitbucket's text: not its response body,
not a header value, not an exception's text, not a URL. A log line names the
step and the exception class or the HTTP status.

Setup in brief: an HTTP destination to ``https://api.bitbucket.org`` (with or
without ``/2.0``), ``BasicAuthentication`` with the technical user's e-mail
address and an API token with the scopes ``read:repository:bitbucket``,
``read:pullrequest:bitbucket``, ``write:pullrequest:bitbucket`` and
``read:user:bitbucket``. The destination holds the credential; this module
stores none.

This file holds the transport: where a request may go (the destination's host
and nowhere else, one same-host redirect, same-host paging links), how often
it is repeated (429 only) and how a failure is said.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any, Awaitable, Callable

import httpx

from agents.bitbucket_config import BUILTIN_BITBUCKET_URL, Pins
from agents.destination import Destination, DestinationError
from agents.destination_auth import (
    PLACEHOLDER_BASE,
    PLACEHOLDER_HOST,
    DestinationAuth,
    resolver_for,
)

logger = logging.getLogger(__name__)

API_ROOT = "/2.0"
BACKOFF_SECONDS = (2.0, 4.0, 8.0)
_REDIRECTS = (301, 302, 303, 307, 308)
_ADMIN = "an administrator must check the Bitbucket destination"

# True in the task that is inside ``BitbucketClient._send``, for that time only.
_sending: contextvars.ContextVar[bool] = contextvars.ContextVar("bitbucket_sending", default=False)


class _NoRequestUrl(logging.Filter):
    """Drops what ``httpx`` logs about a request of this toolset.

    ``httpx`` logs ``HTTP Request: GET <url> ...`` at INFO for every request.
    Here that URL is partly somebody else's text: a redirect target and a
    paging link come from Bitbucket, a file path from a repository. ``app.py``
    raises that logger to WARNING; a script, a test or a debug session does
    not. Only the records made in the sending task are dropped (the context
    variable); every other client is logged as before.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not _sending.get()


def _install_once() -> None:
    """Puts the filter on the ``httpx`` logger unless one is there.

    Called at import and again before every request: a logging setup that
    replaces the logger's filters must not uncover the URLs. Recognised by
    name, so that a reloaded module does not stack a second one.
    """
    log = logging.getLogger("httpx")
    for installed in log.filters:
        kind = type(installed)
        if kind.__name__ == _NoRequestUrl.__name__ and kind.__module__ == __name__:
            return
    log.addFilter(_NoRequestUrl())


_install_once()


class Refused(Exception):
    """A refused call: ``code`` is stable, ``message`` a fixed text."""

    def __init__(self, code: str, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint

    def as_error(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            error["hint"] = self.hint
        return {"error": error}


def _status_refusal(status: int) -> Refused:
    """The refusal for an answer with an unexpected status. Status only."""
    if status == 401:
        return Refused("bitbucket_unauthorized",
                       "Bitbucket refused the credential (HTTP 401)", _ADMIN)
    if status == 403:
        return Refused("bitbucket_forbidden",
                       "the technical user has no access to this (HTTP 403)", _ADMIN)
    if status == 404:
        return Refused("not_found",
                       "Bitbucket has no such repository, pull request or file (HTTP 404)")
    if status == 429:
        return Refused("bitbucket_throttled",
                       "Bitbucket is throttling requests (HTTP 429)", "try again later")
    return Refused("bitbucket_error", f"Bitbucket answered HTTP {int(status)}")


def _same_host_target(value: Any, sent: httpx.URL) -> httpx.URL | None:
    """A redirect or paging target, if it stays where the request went:
    https, the same host and port, no userinfo. ``None`` for anything else.

    The value is used as given: an absolute-path value replaces path and
    query of ``sent`` as raw bytes, an absolute URL is handed to httpx whole;
    nothing is taken apart and put together again (the diff redirect holds a
    ``%0D`` that a decode and re-encode would lose). Only printable ASCII
    without a backslash is a target at all: anything else httpx would have to
    rewrite before it could send it. A relative reference without a leading
    ``/`` is not resolved, it is no target.
    """
    if not isinstance(value, str) or not value:
        return None
    if any(not "!" <= c <= "~" or c == "\\" for c in value):
        return None
    try:
        if value.startswith("/"):
            if value.startswith("//"):
                return None
            return sent.copy_with(raw_path=value.encode("ascii"))
        target = httpx.URL(value)
    except (httpx.InvalidURL, UnicodeError, ValueError, TypeError):
        return None
    if (target.scheme != "https" or sent.scheme != "https" or target.userinfo
            or not target.host
            or target.host.lower() != (sent.host or "").lower()
            or (target.port or 443) != (sent.port or 443)):
        return None
    return target


class BitbucketAuth(DestinationAuth):
    """``DestinationAuth`` that tolerates a destination URL with or without
    the API root, and that moves a request onto the destination byte for byte.

    The toolset always asks for ``/2.0/...``; a destination may be defined as
    ``https://api.bitbucket.org`` or ``https://api.bitbucket.org/2.0``, and
    the root is sent once either way.

    The base class builds the target path from the *decoded* path of the
    request, which is right for the APIs it was written for and wrong for a
    file path: an encoded ``/`` in a segment would become a separator, an
    encoded ``%`` a bare one, an encoded control character an invalid URL.
    So the move is made here on the raw path, and the base class then sees a
    request that already names the destination's host: its https and host
    rules run as always and it adds the destination's headers.
    """

    def send_through(self, request: httpx.Request, destination: Destination) -> None:
        if (request.url.host or "").lower() == PLACEHOLDER_HOST:
            try:
                dest_url = httpx.URL(destination.url)
            except (httpx.InvalidURL, ValueError, TypeError):
                dest_url = None
            # Anything but https with a host is the base class's to refuse.
            if dest_url is not None and dest_url.scheme == "https" and dest_url.host:
                prefix = dest_url.raw_path.partition(b"?")[0].rstrip(b"/")
                path, mark, query = request.url.raw_path.partition(b"?")
                root = API_ROOT.encode("ascii")
                if prefix.endswith(root) and (path == root or path.startswith(root + b"/")):
                    path = path[len(root):]
                request.url = request.url.copy_with(
                    scheme=dest_url.scheme, host=dest_url.host, port=dest_url.port,
                    raw_path=(prefix + path or b"/") + mark + query)
                # httpx set Host when the request was built, from the placeholder.
                request.headers["Host"] = request.url.netloc.decode("ascii")
        super().send_through(request, destination)


def build_http_client(
    oauth: dict[str, Any],
    server_key: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Any = None,
) -> httpx.AsyncClient:
    """The client of one ``builtin:bitbucket`` entry. ``transport`` and
    ``resolver`` are test seams. Redirects are never followed by httpx:
    ``BitbucketClient._get_following`` decides about the one it follows."""
    return httpx.AsyncClient(
        base_url=PLACEHOLDER_BASE,
        # No expected host: the credential goes to the destination's host only.
        auth=BitbucketAuth(resolver or resolver_for(oauth, server_key), server_key=server_key),
        timeout=httpx.Timeout(30.0),
        transport=transport,
        follow_redirects=False,
    )


class BitbucketClient:
    """The requests of one toolset: sequential, with fixed-text failures."""

    def __init__(self, http: httpx.AsyncClient, pins: Pins, *,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._http, self.pins, self._sleep = http, pins, sleep
        self._turn = asyncio.Lock()      # the calls of one toolset are sequential

    async def _send(self, method: str, url: Any, *, params: Any = None,
                    json: Any = None) -> httpx.Response:
        """One request; a 429 is repeated after each of ``BACKOFF_SECONDS``.

        Nothing of a failure but its class reaches the log, and nothing at
        all the caller: the text of an exception may hold a URL or what the
        other side said.
        """
        async with self._turn:
            for wait in (*BACKOFF_SECONDS, None):
                _install_once()
                quiet = _sending.set(True)
                try:
                    response = await self._http.request(
                        method, url, params=params, json=json)
                except DestinationError as e:
                    logger.warning("%s: destination failed (%s)",
                                   BUILTIN_BITBUCKET_URL, type(e).__name__)
                    raise Refused("destination_error", "the BTP destination could not be used",
                                  _ADMIN) from None
                except httpx.HTTPError as e:
                    logger.warning("%s: Bitbucket unreachable (%s)",
                                   BUILTIN_BITBUCKET_URL, type(e).__name__)
                    raise Refused("bitbucket_unreachable", "Bitbucket could not be reached",
                                  "try again later") from None
                except Exception as e:  # noqa: BLE001 - its text is not ours to pass on
                    logger.warning("%s: request failed (%s)",
                                   BUILTIN_BITBUCKET_URL, type(e).__name__)
                    raise Refused("bitbucket_error",
                                  "the request to Bitbucket could not be made") from None
                finally:
                    _sending.reset(quiet)
                if response.status_code != 429:
                    return response
                if wait is not None:
                    await self._sleep(wait)
            raise _status_refusal(429)

    def _json(self, response: httpx.Response, *, ok: tuple[int, ...] = (200,)) -> dict[str, Any]:
        if response.status_code not in ok:
            raise _status_refusal(response.status_code)
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise Refused("bitbucket_error", "Bitbucket answered without a JSON object")
        return body

    async def _get_following(self, url: Any, params: Any = None) -> httpx.Response:
        """A GET that follows ONE redirect, on the host the request went to.

        Bitbucket answers the diff of a pull request with a 302 to the same
        host. The client itself follows nothing: a redirect elsewhere would
        get the destination's credential.
        """
        response = await self._send("GET", url, params=params)
        if response.status_code not in _REDIRECTS:
            return response
        target = _same_host_target(response.headers.get("location"), response.request.url)
        if target is not None:
            response = await self._send("GET", target)
        if target is None or response.status_code in _REDIRECTS:
            raise Refused("bitbucket_error",
                          "Bitbucket redirected to a place this tool does not follow")
        return response

    async def _pages(self, url: Any, params: Any, *,
                     max_pages: int) -> tuple[list[dict[str, Any]], bool]:
        """The ``values`` of a paginated list and whether more were left
        behind ``max_pages``. A ``next`` link is followed as given, on the
        same host only."""
        values: list[dict[str, Any]] = []
        for _ in range(max_pages):
            response = await self._send("GET", url, params=params)
            body = self._json(response)
            page = body.get("values")
            if isinstance(page, list):
                values.extend(v for v in page if isinstance(v, dict))
            following = body.get("next")
            if not following:
                return values, False
            url = _same_host_target(following, response.request.url)
            if url is None:
                raise Refused("bitbucket_error",
                              "Bitbucket's paging link leaves the host; not followed")
            params = None
        return values, True
