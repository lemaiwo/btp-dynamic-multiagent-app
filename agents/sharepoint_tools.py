"""One SharePoint workbook as an in-process, read-only toolset.

An agent opts in with the pseudo-URL ``builtin:sharepoint``;
``agents/builtins.py`` dispatches to it. It reads ONE Excel workbook through
Microsoft Graph and hands the model the views an admin declared, and nothing
else of the file.

**Scope is config, not a tool argument.** ``site``, ``library`` and ``path``
pin the file; ``views`` names what may be read from it
(:mod:`agents.sharepoint_views`). The two tools take a view name, and
``read_calendar`` two dates: no site, path, sheet, range, URL or destination
ever comes from the model, and text in the workbook is data, never an
instruction. What a view lets through is decided in
:mod:`agents.sharepoint_workbook` (pinned columns only; a calendar cell only
as the status its code maps to).

Two auth modes, both an application identity (nobody is signed in on a
scheduled run, and there is no per-user mode in this version):

* ``destination`` -- through a BTP destination, via ``DestinationAuth``. The
  destination holds the credential. ``user_context`` is refused.
* ``app_only`` -- client credentials stored with the server entry, via
  ``ClientCredentialsAuth``.

**How the file is fetched.** Three Graph reads resolve site, library and
item. The content then comes from the item's pre-authenticated download URL
with a *separate* client: it sends no ``Authorization`` (the URL is the
credential, and the Graph token must not travel to another host), follows no
redirect, ignores the environment's proxy and netrc settings, and is used
only for an ``https`` URL on the host the ``site`` pin names. The download
URL is never logged or returned: not by this module, and not by ``httpx``,
which logs every request URL at INFO (a filter on its logger drops the lines
of a download in progress, whatever level the process logs at). The bytes are
kept in memory per file version (eTag) for a few minutes, so the tool calls of
one run download once.

Every refusal is ``{"error": {code, message, hint?}}``, never an exception,
and nothing Graph says reaches the model: a status and a fixed text only.

**Setup, in brief.** An Entra ID app registration with the *application*
permission ``Sites.Selected`` (admin consent), granted ``read`` on the one
site by an administrator (``POST /sites/{site-id}/permissions``). For
``destination``: a destination of type HTTP to ``https://graph.microsoft.com``,
``OAuth2ClientCredentials``, token service URL
``https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token``, scope
``https://graph.microsoft.com/.default``. ``site`` is
``<tenant>.sharepoint.com:/sites/<site>``, ``library`` the document library's
name as Graph lists it (``scripts/probe_sharepoint.py`` prints the names).
Not yet run against a real tenant.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

from agents.outlook_tools import GRAPH_V1
from agents.sharepoint_views import site_host
from agents.sharepoint_workbook import MAX_FILE_BYTES, Refused, check_archive

__all__ = ["SharePointFile", "BUILTIN_SHAREPOINT_URL"]

logger = logging.getLogger(__name__)

BUILTIN_SHAREPOINT_URL = "builtin:sharepoint"

AUTH_MODE_APP_ONLY = "app_only"
AUTH_MODE_DESTINATION = "destination"
SUPPORTED_AUTH_MODES = (AUTH_MODE_APP_ONLY, AUTH_MODE_DESTINATION)

# How long downloaded bytes of one file version are reused.
FILE_CACHE_TTL_SECONDS = 300
# How long the resolved library id is trusted.
DRIVE_CACHE_TTL_SECONDS = 900
DOWNLOAD_TIMEOUT_SECONDS = 60.0
# A result larger than this is refused, not cut off: a list that ends early
# would read as "nobody else is planned".
MAX_RESULT_CHARS = 60_000

_ADMIN = "an administrator must check the SharePoint server entry"

# True in the task that is downloading, for the time of the download only.
_downloading: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "sharepoint_downloading", default=False)


class _NoDownloadUrl(logging.Filter):
    """Drops what ``httpx`` logs about a download in progress.

    ``httpx`` logs ``HTTP Request: GET <url> ...`` at INFO for every request.
    The download URL carries its own token, so that line would put a
    credential into the log of any process that logs ``httpx`` at INFO
    (``app.py`` raises that logger to WARNING; a script, a test or a debug
    session does not). A record is created in the task that sends the
    request, so the context variable names exactly the download's lines: the
    Graph requests and every other client are logged as before.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not _downloading.get()


logging.getLogger("httpx").addFilter(_NoDownloadUrl())


def _status_refusal(step: str, status: int) -> Refused:
    """The refusal for a Graph answer that is not a 200. Status only."""
    if status == 401:
        return Refused("graph_unauthorized", "Microsoft Graph refused the credential (HTTP 401)",
                       _ADMIN)
    if status == 403:
        return Refused("graph_forbidden",
                       "the application has no access to the site (HTTP 403)", _ADMIN)
    if status == 404:
        return Refused(f"{step}_not_found", f"the configured {step} was not found (HTTP 404)",
                       _ADMIN)
    if status == 429:
        return Refused("graph_throttled", "Microsoft Graph is throttling requests (HTTP 429)",
                       "try again later")
    return Refused("graph_error", f"Microsoft Graph answered HTTP {status}")


class SharePointFile:
    """The one pinned file: resolves it, downloads it, caches it per version.

    Takes an ``httpx.AsyncClient`` so the caller owns authentication and tests
    can inject a mock transport. ``download_transport`` is the test seam of
    the download client, which this class builds itself so that it can never
    be one that carries a credential.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        site: str,
        library: str,
        path: str,
        *,
        download_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = http
        self.site = site
        self.library = library
        self.path = path
        self._host = site_host(site)
        self._download_transport = download_transport
        self._drive: tuple[float, str] | None = None
        # (deadline, eTag, bytes): one file, one version.
        self._file: tuple[float, str, bytes] | None = None
        self._lock = asyncio.Lock()

    async def _get(self, step: str, path: str, **params: str) -> dict[str, Any]:
        from agents.client_credentials import ClientCredentialsError
        from agents.destination import DestinationError

        try:
            r = await self._http.get(f"{GRAPH_V1}{path}", params=params or None)
        except DestinationError as e:
            logger.warning("builtin:sharepoint: destination failed (%s)", type(e).__name__)
            raise Refused("destination_error", "the BTP destination could not be used",
                          _ADMIN) from None
        except ClientCredentialsError:
            logger.warning("builtin:sharepoint: no application token")
            raise Refused("token_failed", "no application token could be obtained",
                          _ADMIN) from None
        except httpx.HTTPError as e:
            logger.warning("builtin:sharepoint: Graph unreachable (%s)", type(e).__name__)
            raise Refused("graph_unreachable", "Microsoft Graph could not be reached",
                          "try again later") from None
        if r.status_code != 200:
            raise _status_refusal(step, r.status_code)
        try:
            body = r.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise Refused("graph_error", "Microsoft Graph answered without a JSON object")
        return body

    async def _drive_id(self) -> str:
        if self._drive is not None and self._drive[0] > time.monotonic():
            return self._drive[1]
        host, _, site_path = self.site.partition(":")
        site = await self._get("site", f"/sites/{host}:{quote(site_path)}", **{"$select": "id"})
        site_id = site.get("id")
        if not isinstance(site_id, str) or not site_id:
            raise Refused("graph_error", "Microsoft Graph answered without a site id")
        drives = await self._get(
            "site", f"/sites/{quote(site_id, safe=',')}/drives",
            **{"$select": "id,name", "$top": "200"},
        )
        wanted = self.library.casefold()
        found = [d.get("id") for d in drives.get("value") or []
                 if isinstance(d, dict) and str(d.get("name") or "").casefold() == wanted]
        if len(found) != 1 or not isinstance(found[0], str) or not found[0]:
            raise Refused("library_not_found",
                          "the site has no document library of the configured name", _ADMIN)
        self._drive = (time.monotonic() + DRIVE_CACHE_TTL_SECONDS, found[0])
        return found[0]

    def _checked_download_url(self, value: Any) -> httpx.URL:
        """The download URL, if it is https on the site's own host."""
        try:
            url = httpx.URL(value) if isinstance(value, str) else None
        except httpx.InvalidURL:
            url = None
        if url is None or url.scheme != "https" or (url.host or "").lower() != self._host \
                or url.userinfo or url.port not in (None, 443):
            # Never the URL: it is a credential.
            logger.warning("builtin:sharepoint: download URL refused (not https on the site host)")
            raise Refused("download_refused",
                          "the download location is not on the configured SharePoint host")
        return url

    async def _download(self, url: httpx.URL) -> bytes:
        chunks: list[bytes] = []
        size = 0
        # Set and reset in this coroutine: httpx must not log this URL.
        quiet = _downloading.set(True)
        try:
            # A client of its own: no auth, no redirect, no proxy/netrc from
            # the environment. The Graph client must never send this request.
            async with httpx.AsyncClient(
                follow_redirects=False,
                trust_env=False,
                timeout=httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS),
                transport=self._download_transport,
            ) as client:
                async with client.stream("GET", url) as r:
                    if r.status_code != 200:
                        code = "download_redirect" if 300 <= r.status_code < 400 \
                            else "download_failed"
                        raise Refused(code, f"the download answered HTTP {r.status_code}")
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_FILE_BYTES:
                            raise Refused("too_large",
                                          "the workbook is larger than this tool reads")
                        chunks.append(chunk)
        except httpx.HTTPError as e:
            logger.warning("builtin:sharepoint: download failed (%s)", type(e).__name__)
            raise Refused("download_failed", "the workbook could not be downloaded",
                          "try again later") from None
        finally:
            _downloading.reset(quiet)
        return b"".join(chunks)

    async def fetch(self) -> tuple[bytes, str]:
        """The workbook's bytes and its last-modified time (ISO, from Graph)."""
        async with self._lock:
            drive = await self._drive_id()
            path = "/".join(quote(part, safe="") for part in self.path.split("/"))
            item = await self._get("file", f"/drives/{quote(drive, safe='!')}/root:/{path}")
            if not isinstance(item.get("file"), dict):
                raise Refused("not_a_file", "the configured path is not a file", _ADMIN)
            size = item.get("size")
            if type(size) is int and size > MAX_FILE_BYTES:
                raise Refused("too_large", "the workbook is larger than this tool reads")
            modified = item.get("lastModifiedDateTime")
            modified = modified if isinstance(modified, str) else ""
            etag = item.get("eTag") or item.get("cTag")
            etag = etag if isinstance(etag, str) and etag else None
            cached = self._file
            if etag and cached and cached[1] == etag and cached[0] > time.monotonic():
                return cached[2], modified
            url = self._checked_download_url(item.get("@microsoft.graph.downloadUrl"))
            data = await self._download(url)
            check_archive(data)
            # Without a version there is nothing to compare: not cached.
            self._file = (time.monotonic() + FILE_CACHE_TTL_SECONDS, etag, data) if etag else None
            return data, modified
