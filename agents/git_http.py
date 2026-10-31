"""The transport of ``builtin:git``: one HTTP core for the three providers.

Where a request may go (the destination's https host and nowhere else; one
same-host redirect of a read, same-host paging links), how often it is
repeated (a 429 only, after 2, 4 and 8 s), how much of an answer is read
(every body up to a cap, while it is read; the body of an error status
never) and how a failure is said (a closed list of codes with fixed texts:
never the provider's text, a header value, an exception's text or a URL).

A request that changes something is sent once. When it may have been
processed although no answer was read (a timeout after it left, a 5xx, an
answer over its cap, a 2xx that is not what the provider documents) the
outcome is ``outcome_unknown``, never a success and never "nothing
happened".

The request-line log filter is the one of :mod:`agents.bitbucket_tools`
(imported, not copied): ``httpx`` logs every request URL at INFO, and here
the URL holds repository paths, branch names and file paths.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from agents.bitbucket_tools import BitbucketAuth, _install_once, _same_host_target, _sending
from agents.destination import Destination, DestinationError
from agents.destination_auth import PLACEHOLDER_BASE, resolver_for

logger = logging.getLogger(__name__)

BACKOFF_SECONDS = (2.0, 4.0, 8.0)
MAX_JSON_BYTES = 2_000_000
MAX_FILE_BYTES = 1_000_000
TIMEOUT_SECONDS = 30.0
_REDIRECTS = (301, 302, 303, 307, 308)
_ADMIN = "an administrator must check the Git destination"

ERROR_CODES = frozenset({
    "destination_error", "unauthorized", "forbidden", "not_found", "throttled",
    "provider_error", "provider_refused", "unreachable", "not_json", "result_too_large",
    "redirect_refused", "repository_not_allowed", "invalid_argument", "invalid_path",
    "branch_not_allowed", "pipeline_file", "binary_file", "file_too_large",
    "already_exists", "branch_moved", "outcome_unknown",
})
# Failures of a request that say it never left: no connection was made. After
# any other failure a write may have been processed.
_NEVER_LEFT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
               httpx.UnsupportedProtocol)
_LINK_NEXT_RE = re.compile(r'<([^<>]+)>\s*;\s*rel="?next"?')


class GitRefused(Exception):
    """A refused call: ``code`` is one of ``ERROR_CODES``, ``message`` a
    fixed text, ``hint`` a fixed text or names from the configuration."""

    def __init__(self, code: str, message: str, hint: str | None = None) -> None:
        assert code in ERROR_CODES, code
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint

    def as_error(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            error["hint"] = self.hint
        return {"error": error}


def outcome_unknown() -> GitRefused:
    return GitRefused(
        "outcome_unknown",
        "the change may or may not have been made: no answer could be read",
        "do not send it again; check the branch or pull request first, or tell the user")


def status_refusal(status: int) -> GitRefused:
    """The refusal for an answer with an unexpected status. Status only."""
    if status == 401:
        return GitRefused("unauthorized", "the provider refused the credential (HTTP 401)",
                          _ADMIN)
    if status == 403:
        return GitRefused("forbidden", "the technical user has no access to this (HTTP 403)",
                          _ADMIN)
    if status == 404:
        return GitRefused("not_found",
                          "the provider has no such repository, branch, file or pull "
                          "request (HTTP 404)")
    if status == 429:
        return GitRefused("throttled", "the provider is throttling requests (HTTP 429)",
                          "try again later")
    if status in (400, 409, 422):
        return GitRefused("provider_refused",
                          f"the provider refused the request (HTTP {int(status)}); "
                          "nothing was changed")
    return GitRefused("provider_error", f"the provider answered HTTP {int(status)}")


class GitAuth(BitbucketAuth):
    """The destination auth of ``builtin:git``.

    The move onto the destination is made on the RAW path (``BitbucketAuth``,
    whose API-root rule never applies to the paths sent here): a GitLab
    project is one segment ``group%2Fproject``, and the base class, which
    rebuilds the path from its decoded form, would make that two segments.
    An OnPremise destination is refused before anything is sent: this
    toolset does not route through the connectivity proxy (out of v1).
    """

    def send_through(self, request: httpx.Request, destination: Destination) -> None:
        if str(getattr(destination, "proxy_type", "") or "") == "OnPremise":
            raise DestinationError(
                f"{self.server_key}: an OnPremise destination is not supported; "
                "refusing to send")
        super().send_through(request, destination)


def build_http_client(
    oauth: dict[str, Any],
    server_key: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Any = None,
) -> httpx.AsyncClient:
    """The client of one ``builtin:git`` entry. ``transport`` and ``resolver``
    are test seams. No expected host: the credential goes to the destination's
    own host only. httpx follows no redirect: ``GitHttp.get`` decides about
    the one it follows."""
    return httpx.AsyncClient(
        base_url=PLACEHOLDER_BASE,
        auth=GitAuth(resolver or resolver_for(oauth, server_key), server_key=server_key),
        timeout=httpx.Timeout(TIMEOUT_SECONDS),
        transport=transport,
        follow_redirects=False,
    )


@dataclass
class Answer:
    """What this module keeps of a response: the status, three headers, the
    body of a 2xx (empty for any other status) and whether the body was over
    the cap (then empty)."""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    over: bool = False
    url: httpx.URL | None = None

    def json(self) -> Any:
        """The JSON of a 200/201 answer, or a refusal."""
        if self.status not in (200, 201):
            raise status_refusal(self.status)
        if self.over:
            raise GitRefused("result_too_large", "the provider's answer is too large to read")
        media = self.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media and not (media == "application/json" or media.endswith("+json")):
            raise GitRefused("not_json", "the provider answered without JSON (a sign-in page?)",
                             _ADMIN)
        try:
            return jsonlib.loads(self.body)
        except ValueError:
            raise GitRefused("not_json", "the provider answered without JSON",
                             _ADMIN) from None

    def obj(self) -> dict[str, Any]:
        data = self.json()
        if not isinstance(data, dict):
            raise GitRefused("provider_error", "the provider answered without a JSON object")
        return data

    def next_link(self) -> str | None:
        """The ``rel="next"`` target of a ``Link`` header (GitHub, GitLab)."""
        match = _LINK_NEXT_RE.search(self.headers.get("link", ""))
        return match.group(1) if match else None


class GitHttp:
    """The requests of one toolset: sequential, capped, with fixed-text
    failures. ``headers`` are sent with every request (an adapter's
    ``Accept``); ``sleep`` is a test seam."""

    def __init__(self, http: httpx.AsyncClient, *, server_key: str,
                 headers: dict[str, str] | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._http, self._server_key, self._sleep = http, server_key, sleep
        self._headers = dict(headers or {})
        self._turn = asyncio.Lock()      # the calls of one toolset are sequential

    async def _exchange(self, method: str, url: Any, cap: int, **kw: Any) -> Answer:
        """One request and at most ``cap`` bytes of its answer, read piece by
        piece: nothing is read into memory to be measured afterwards."""
        headers = {**self._headers, "Accept-Encoding": "identity", **kw.pop("headers", {})}
        request = self._http.build_request(method, url, headers=headers, **kw)
        live = await self._http.send(request, stream=True)
        try:
            pieces: list[bytes] = []
            size, over = 0, False
            if 200 <= live.status_code < 300:
                async for piece in live.aiter_bytes():
                    size += len(piece)
                    if size > cap:
                        over, pieces = True, []
                        break
                    pieces.append(piece)
            kept = {name: live.headers[name] for name in ("location", "content-type", "link")
                    if name in live.headers}
            return Answer(live.status_code, kept, b"".join(pieces), over, live.request.url)
        finally:
            await live.aclose()

    async def send(self, method: str, url: Any, *, cap: int = MAX_JSON_BYTES,
                   write: bool = False, **kw: Any) -> Answer:
        """One request; a 429 is repeated after each of ``BACKOFF_SECONDS``
        (it says "not processed"), nothing else is. With ``write`` every
        failure after which the request may have been processed is
        ``outcome_unknown``: any transport failure but those of
        ``_NEVER_LEFT``, and an answer over the cap."""
        async with self._turn:
            for wait in (*BACKOFF_SECONDS, None):
                _install_once()
                quiet = _sending.set(True)
                try:
                    answer = await self._exchange(method, url, cap, **kw)
                except DestinationError as e:
                    logger.warning("%s: destination failed (%s)", self._server_key,
                                   type(e).__name__)
                    # Also after a 401 whose retry could not be prepared: the
                    # provider refused that request, nothing was changed.
                    raise GitRefused("destination_error",
                                     "the BTP destination could not be used", _ADMIN) from None
                except httpx.HTTPError as e:
                    logger.warning("%s: the provider is unreachable (%s)", self._server_key,
                                   type(e).__name__)
                    if write and not isinstance(e, _NEVER_LEFT):
                        raise outcome_unknown() from None
                    raise GitRefused("unreachable", "the provider could not be reached",
                                     "try again later") from None
                except Exception as e:  # noqa: BLE001 - its text is not ours to pass on
                    logger.warning("%s: request failed (%s)", self._server_key,
                                   type(e).__name__)
                    if write:
                        raise outcome_unknown() from None
                    raise GitRefused("provider_error",
                                     "the request to the provider could not be made") from None
                finally:
                    _sending.reset(quiet)
                if answer.status != 429:
                    if answer.over and write:
                        raise outcome_unknown()
                    return answer
                if wait is not None:
                    await self._sleep(wait)
            raise status_refusal(429)

    async def get(self, url: Any, *, params: Any = None, cap: int = MAX_JSON_BYTES,
                  headers: dict[str, str] | None = None) -> Answer:
        """A GET that follows ONE redirect, on the host the request went to;
        a redirect elsewhere, or a second one, is ``redirect_refused``."""
        extra = {"headers": headers} if headers else {}
        answer = await self.send("GET", url, params=params, cap=cap, **extra)
        if answer.status not in _REDIRECTS:
            return answer
        target = _same_host_target(answer.headers.get("location"), answer.url)
        if target is not None:
            answer = await self.send("GET", target, cap=cap, **extra)
        if target is None or answer.status in _REDIRECTS:
            raise GitRefused("redirect_refused",
                             "the provider redirected to a place this tool does not follow")
        return answer

    def follow(self, link: Any, answer: Answer) -> httpx.URL:
        """A paging link, if it stays on the host the request went to."""
        target = _same_host_target(link, answer.url) if answer.url is not None else None
        if target is None:
            raise GitRefused("redirect_refused",
                             "the provider's paging link leaves the host; not followed")
        return target

    async def write(self, method: str, url: Any, *, ok: tuple[int, ...] = (200, 201),
                    refusals: dict[int, GitRefused] | None = None, **kw: Any) -> Answer:
        """One request that changes something, sent once. Three outcomes: an
        ``ok`` answer (done); a refusal by a 4xx status (the provider said no:
        not done); ``outcome_unknown`` (any 5xx, any other 2xx or 3xx, an
        unreadable answer: it may be done)."""
        answer = await self.send(method, url, write=True, **kw)
        status = answer.status
        if status in ok:
            return answer
        if refusals and status in refusals:
            raise refusals[status]
        if 400 <= status < 500:
            raise status_refusal(status)
        logger.warning("%s: the answer to a write could not be read (HTTP %s)",
                       self._server_key, int(status))
        raise outcome_unknown()
