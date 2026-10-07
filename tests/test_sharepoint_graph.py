"""How ``builtin:sharepoint`` fetches the pinned file (``SharePointFile``).

Covered: the three Graph reads, the download through a separate client (no
``Authorization``, no redirect, the site's own host only), the cache per file
version, the size cap, and that nothing Graph says is passed on.

No network: Graph and the download host are mock transports.

Run:  python -m pytest tests/test_sharepoint_graph.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import httpx  # noqa: E402
import pytest  # noqa: E402

import agents.sharepoint_tools as tools  # noqa: E402
from agents.sharepoint_tools import SharePointFile  # noqa: E402
from agents.sharepoint_workbook import Refused  # noqa: E402
from tests.sharepoint_helpers import PINS, FakeGraph, build_workbook  # noqa: E402

WORKBOOK = build_workbook()


def _file(graph: FakeGraph) -> SharePointFile:
    return SharePointFile(graph.client(), PINS["site"], PINS["library"], PINS["path"],
                          download_transport=graph.download_transport())


async def _refused(graph: FakeGraph) -> Refused:
    with pytest.raises(Refused) as refused:
        await _file(graph).fetch()
    return refused.value


async def test_site_library_and_item_are_resolved_with_three_graph_reads():
    graph = FakeGraph(WORKBOOK)
    data, modified = await _file(graph).fetch()
    assert data == WORKBOOK and modified == "2026-01-02T08:00:00Z"
    assert [r.url.path for r in graph.requests] == [
        "/v1.0/sites/example.sharepoint.com:/sites/planning",
        "/v1.0/sites/example.sharepoint.com,g1,g2/drives",
        "/v1.0/drives/b!drive1/root:/Team/Planning 2026.xlsx",
    ]
    assert all(r.method == "GET" for r in graph.requests)


async def test_the_download_carries_no_credential_of_the_graph_client():
    graph = FakeGraph(WORKBOOK)
    await _file(graph).fetch()
    (download,) = graph.downloads
    assert download.url.host == "example.sharepoint.com"
    assert "authorization" not in download.headers
    assert "cookie" not in download.headers
    # And Graph never saw the download.
    assert all(r.url.host == "graph.microsoft.com" for r in graph.requests)


@pytest.mark.parametrize("url", [
    "https://example.sharepoint.com.evil.test/x",
    "https://evil.test/x?host=example.sharepoint.com",
    "http://example.sharepoint.com/x",
    "https://user@example.sharepoint.com/x",
    "https://example.sharepoint.com:8443/x",
    "https://other.sharepoint.com/x",
    "",
    None,
    17,
])
async def test_a_download_url_on_another_host_is_never_requested(url, caplog):
    graph = FakeGraph(WORKBOOK, item={"@microsoft.graph.downloadUrl": url})
    refused = await _refused(graph)
    assert refused.code == "download_refused"
    assert graph.downloads == []
    assert "evil" not in refused.message and "evil" not in caplog.text


async def test_a_redirect_of_the_download_is_not_followed():
    graph = FakeGraph(WORKBOOK, status={"download": 302})
    refused = await _refused(graph)
    assert refused.code == "download_redirect"
    assert len(graph.downloads) == 1
    assert "elsewhere" not in refused.message


async def test_one_file_version_is_downloaded_once():
    graph = FakeGraph(WORKBOOK)
    source = _file(graph)
    await source.fetch()
    await source.fetch()
    assert len(graph.downloads) == 1
    # The library is resolved once; the item is read every time (its eTag).
    assert len(graph.requests) == 4
    graph.etag = '"v2"'
    await source.fetch()
    assert len(graph.downloads) == 2


async def test_the_cache_expires_and_a_file_without_etag_is_not_cached():
    graph = FakeGraph(WORKBOOK)
    source = _file(graph)
    await source.fetch()
    _, etag, data = source._file
    source._file = (0.0, etag, data)  # the entry's deadline has passed
    await source.fetch()
    assert len(graph.downloads) == 2
    bare = FakeGraph(WORKBOOK, item={"eTag": None, "cTag": None})
    source = _file(bare)
    await source.fetch()
    await source.fetch()
    assert len(bare.downloads) == 2


async def test_a_file_over_the_cap_is_refused_before_and_during_the_download(monkeypatch):
    graph = FakeGraph(WORKBOOK, item={"size": 21 * 1024 * 1024})
    assert (await _refused(graph)).code == "too_large"
    assert graph.downloads == []
    # Graph says it is small; the stream says otherwise.
    monkeypatch.setattr(tools, "MAX_FILE_BYTES", 100)
    lying = FakeGraph(WORKBOOK, item={"size": 10})
    assert (await _refused(lying)).code == "too_large"


@pytest.mark.parametrize("step, status, code", [
    ("site", 401, "graph_unauthorized"),
    ("site", 403, "graph_forbidden"),
    ("site", 404, "site_not_found"),
    ("drives", 404, "site_not_found"),
    ("item", 404, "file_not_found"),
    ("item", 429, "graph_throttled"),
    ("item", 503, "graph_error"),
    ("download", 403, "download_failed"),
])
async def test_graph_failures_become_codes_and_never_graphs_text(step, status, code):
    refused = await _refused(FakeGraph(WORKBOOK, status={step: status}))
    assert refused.code == code
    assert "graph-text" not in refused.message and "graph-text" not in (refused.hint or "")


async def test_unknown_library_folder_and_non_workbook_are_refused():
    graph = FakeGraph(WORKBOOK)
    source = SharePointFile(graph.client(), PINS["site"], "Missing", PINS["path"],
                            download_transport=graph.download_transport())
    with pytest.raises(Refused) as refused:
        await source.fetch()
    assert refused.value.code == "library_not_found"
    assert (await _refused(FakeGraph(WORKBOOK, item={"file": None, "folder": {}}))).code \
        == "not_a_file"
    assert (await _refused(FakeGraph(b"<html>sign in</html>"))).code == "not_a_workbook"


async def test_a_transport_error_is_a_refusal_without_the_exception_text():
    def boom(request):
        raise httpx.ConnectError("dns: secret-host.internal")

    client = httpx.AsyncClient(base_url="https://graph.microsoft.com",
                               transport=httpx.MockTransport(boom))
    source = SharePointFile(client, PINS["site"], PINS["library"], PINS["path"])
    with pytest.raises(Refused) as refused:
        await source.fetch()
    assert refused.value.code == "graph_unreachable"
    assert "secret-host" not in refused.value.message


# --- Leak checks: what the other side says, the token, the download URL ------
#
# ``MARKER`` stands for any text the remote side controls (an error body, a
# header, an exception text). ``SECRETS`` are the Graph token and the parts of
# the pre-authenticated download URL. None of them may be in what a refusal
# says, in what a traceback of it prints, or in a log line of ANY logger at
# DEBUG (httpx itself logs every request URL at INFO).

MARKER = "rem0te-mark3r"
SECRETS = ("graph-token", "tempauth", "t0k", "download.aspx")


def _said(refused: Refused) -> str:
    """Everything a caller or a log could print of a refusal."""
    return " ".join([
        refused.code, refused.message, refused.hint or "", str(refused), repr(refused),
        repr(refused.as_error()),
        "".join(traceback.format_exception(type(refused), refused, refused.__traceback__)),
    ])


def _loud(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    for name in ("httpx", "httpcore", "agents.sharepoint_tools"):
        caplog.set_level(logging.DEBUG, logger=name)


class MarkedGraph(FakeGraph):
    """A :class:`FakeGraph` whose failing ``step`` answers with ``MARKER`` in
    the body and in a header, as Graph or SharePoint would with their text."""

    def __init__(self, step: str, status: int, **kw) -> None:
        super().__init__(WORKBOOK, **kw)
        self.step, self.code = step, status

    def _marked(self) -> httpx.Response:
        return httpx.Response(
            self.code,
            headers={"WWW-Authenticate": f'Bearer error_description="{MARKER}"',
                     "Location": f"https://{MARKER}.test/x", "x-ms-diagnostics": MARKER},
            json={"error": {"code": MARKER, "message": f"{MARKER} for tenant {MARKER}",
                            "innerError": {"request-id": MARKER}}})

    def _graph(self, request):
        answer = super()._graph(request)
        path = request.url.path
        step = "item" if "/root:/" in path else "drives" if path.endswith("/drives") else "site"
        return self._marked() if step == self.step else answer

    def _download(self, request):
        self.downloads.append(request)
        return self._marked()


@pytest.mark.parametrize("step, status, code", [
    ("site", 401, "graph_unauthorized"),
    ("site", 403, "graph_forbidden"),
    ("site", 404, "site_not_found"),
    ("site", 500, "graph_error"),
    ("drives", 403, "graph_forbidden"),
    ("drives", 404, "site_not_found"),
    ("item", 401, "graph_unauthorized"),
    ("item", 404, "file_not_found"),
    ("item", 429, "graph_throttled"),
    ("item", 503, "graph_error"),
    ("item", 302, "graph_error"),
    ("download", 401, "download_failed"),
    ("download", 403, "download_failed"),
    ("download", 404, "download_failed"),
    ("download", 500, "download_failed"),
    ("download", 302, "download_redirect"),
    ("download", 307, "download_redirect"),
])
async def test_remote_text_is_in_no_refusal_and_no_log_line(step, status, code, caplog):
    _loud(caplog)
    refused = await _refused(MarkedGraph(step, status))
    assert refused.code == code
    said = _said(refused)
    assert MARKER not in said and MARKER not in caplog.text
    for secret in SECRETS:
        assert secret not in said and secret not in caplog.text


async def test_a_200_from_graph_that_is_not_an_object_is_refused_without_its_text(caplog):
    _loud(caplog)

    def odd(request):
        return httpx.Response(200, text=f"<html>{MARKER}</html>")

    client = httpx.AsyncClient(base_url="https://graph.microsoft.com",
                               transport=httpx.MockTransport(odd))
    with pytest.raises(Refused) as refused:
        await SharePointFile(client, PINS["site"], PINS["library"], PINS["path"]).fetch()
    assert refused.value.code == "graph_error"
    assert MARKER not in _said(refused.value) and MARKER not in caplog.text


async def test_the_token_and_the_download_url_are_never_logged(caplog, monkeypatch):
    _loud(caplog)
    graph = FakeGraph(WORKBOOK)
    source = _file(graph)
    await source.fetch()
    await source.fetch()
    assert len(graph.downloads) == 1
    # The other clients of this process still log their requests.
    assert "graph.microsoft.com/v1.0/sites" in caplog.text
    said = [
        _said(await _refused(FakeGraph(b"<html>sign in</html>"))),
        _said(await _refused(FakeGraph(WORKBOOK, status={"download": 302}))),
        _said(await _refused(FakeGraph(WORKBOOK, status={"download": 403}))),
        _said(await _refused(FakeGraph(WORKBOOK, item={"size": 21 * 1024 * 1024}))),
    ]
    monkeypatch.setattr(tools, "MAX_FILE_BYTES", 100)
    said.append(_said(await _refused(FakeGraph(WORKBOOK, item={"size": 10}))))
    for secret in SECRETS:
        assert secret not in caplog.text
        assert all(secret not in text for text in said)


@pytest.mark.parametrize("url", [
    "https://evil.test/x?tempauth=t0k",
    "https://example.sharepoint.com.evil.test/download.aspx?tempauth=t0k",
    "http://example.sharepoint.com/download.aspx?tempauth=t0k",
    "https://t0k@example.sharepoint.com/x",
    "https://example.sharepoint.com:8443/download.aspx?tempauth=t0k",
    "https://[bad/download.aspx?tempauth=t0k",
    "//example.sharepoint.com/download.aspx?tempauth=t0k",
    "https://example.sharepoint.com\\@evil.test/download.aspx?tempauth=t0k",
])
async def test_a_refused_download_url_is_neither_requested_nor_printed(url, caplog):
    _loud(caplog)
    graph = FakeGraph(WORKBOOK, item={"@microsoft.graph.downloadUrl": url})
    refused = await _refused(graph)
    assert refused.code == "download_refused" and graph.downloads == []
    said = _said(refused)
    for secret in ("t0k", "tempauth", "evil", "download.aspx"):
        assert secret not in said and secret not in caplog.text


async def test_a_failing_download_connection_gives_no_exception_text(caplog):
    _loud(caplog)
    graph = FakeGraph(WORKBOOK)

    def boom(request):
        raise httpx.ConnectError(f"{MARKER}: {request.url}")

    source = SharePointFile(graph.client(), PINS["site"], PINS["library"], PINS["path"],
                            download_transport=httpx.MockTransport(boom))
    with pytest.raises(Refused) as refused:
        await source.fetch()
    assert refused.value.code == "download_failed"
    said = _said(refused.value)
    for secret in (MARKER, *SECRETS):
        assert secret not in said and secret not in caplog.text


async def test_a_failing_graph_connection_gives_no_exception_text(caplog):
    _loud(caplog)

    def boom(request):
        raise httpx.ConnectError(f"{MARKER}: {request.headers.get('authorization')}")

    client = httpx.AsyncClient(base_url="https://graph.microsoft.com",
                               headers={"Authorization": "Bearer graph-token"},
                               transport=httpx.MockTransport(boom))
    with pytest.raises(Refused) as refused:
        await SharePointFile(client, PINS["site"], PINS["library"], PINS["path"]).fetch()
    assert refused.value.code == "graph_unreachable"
    said = _said(refused.value)
    for secret in (MARKER, "graph-token"):
        assert secret not in said and secret not in caplog.text


@pytest.mark.parametrize("error, code", [
    ("destination", "destination_error"),
    ("token", "token_failed"),
])
async def test_a_destination_or_token_failure_gives_no_provider_text(error, code, caplog):
    from agents.client_credentials import ClientCredentialsError
    from agents.destination import DestinationError

    _loud(caplog)
    raised = {"destination": DestinationError, "token": ClientCredentialsError}[error]

    class Failing(httpx.Auth):
        def auth_flow(self, request):
            raise raised(f"{MARKER}: invalid_client for https://login.example/{MARKER}")
            yield request  # pragma: no cover

    client = httpx.AsyncClient(base_url="https://graph.microsoft.com", auth=Failing(),
                               transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(Refused) as refused:
        await SharePointFile(client, PINS["site"], PINS["library"], PINS["path"]).fetch()
    assert refused.value.code == code
    assert MARKER not in _said(refused.value) and MARKER not in caplog.text


async def test_the_download_client_ignores_the_environment_and_follows_nothing(monkeypatch):
    seen: dict = {}
    real = httpx.AsyncClient

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(tools.httpx, "AsyncClient", spy)
    graph = FakeGraph(WORKBOOK)
    source = SharePointFile(real(base_url="https://graph.microsoft.com",
                                 transport=httpx.MockTransport(graph._graph)),
                            PINS["site"], PINS["library"], PINS["path"],
                            download_transport=graph.download_transport())
    await source.fetch()
    assert seen["follow_redirects"] is False and seen["trust_env"] is False
    assert not {"auth", "headers", "cookies", "base_url"} & set(seen)


# --- After review: deadline, unexpected errors, wire encoding, the filter ----


def _secrets_absent(caplog, *texts: str, extra: tuple[str, ...] = ()) -> None:
    for secret in (*SECRETS, *extra):
        assert secret not in caplog.text
        assert all(secret not in text for text in texts)


class _Drip(httpx.AsyncByteStream):
    """A body that never ends and never pauses long enough for a read timeout."""

    async def __aiter__(self):
        while True:
            yield b"x"
            await asyncio.sleep(0.01)


async def test_a_download_that_never_ends_is_cut_off_and_frees_the_lock(monkeypatch, caplog):
    _loud(caplog)
    monkeypatch.setattr(tools, "DOWNLOAD_DEADLINE_SECONDS", 0.1)
    graph = FakeGraph(WORKBOOK)
    drips = [True]

    def download(request):
        if drips:
            drips.pop()
            return httpx.Response(200, stream=_Drip())
        return httpx.Response(200, content=WORKBOOK)

    source = SharePointFile(graph.client(), PINS["site"], PINS["library"], PINS["path"],
                            download_transport=httpx.MockTransport(download))
    with pytest.raises(Refused) as refused:
        await asyncio.wait_for(source.fetch(), 5)
    assert refused.value.code == "download_failed"
    assert not source._lock.locked() and source._file is None
    _secrets_absent(caplog, _said(refused.value))
    # The next call is not queued behind the one that was cut off.
    data, _ = await asyncio.wait_for(source.fetch(), 5)
    assert data == WORKBOOK


@pytest.mark.parametrize("error", [
    httpx.InvalidURL(f"{MARKER} in URL"),
    RuntimeError(f"{MARKER}: Bearer graph-token"),
    TimeoutError(MARKER),
    KeyError(MARKER),
])
async def test_an_unexpected_error_of_a_graph_read_is_read_failed(error, caplog):
    _loud(caplog)

    def boom(request):
        raise error

    client = httpx.AsyncClient(base_url="https://graph.microsoft.com",
                               headers={"Authorization": "Bearer graph-token"},
                               transport=httpx.MockTransport(boom))
    with pytest.raises(Refused) as refused:
        await SharePointFile(client, PINS["site"], PINS["library"], PINS["path"]).fetch()
    assert refused.value.code == "read_failed"
    _secrets_absent(caplog, _said(refused.value), extra=(MARKER,))
    assert type(error).__name__ in caplog.text  # the class is what the log says


async def test_an_unexpected_error_of_the_auth_class_is_read_failed(caplog):
    _loud(caplog)

    class Odd(httpx.Auth):
        def auth_flow(self, request):
            raise ValueError(f"{MARKER}: client_secret=graph-token")
            yield request  # pragma: no cover

    client = httpx.AsyncClient(base_url="https://graph.microsoft.com", auth=Odd(),
                               transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(Refused) as refused:
        await SharePointFile(client, PINS["site"], PINS["library"], PINS["path"]).fetch()
    assert refused.value.code == "read_failed"
    _secrets_absent(caplog, _said(refused.value), extra=(MARKER,))


@pytest.mark.parametrize("make, code", [
    (lambda url: httpx.InvalidURL(f"{MARKER} {url}"), "read_failed"),
    (lambda url: RuntimeError(f"{MARKER} {url}"), "read_failed"),
    (lambda url: OSError(f"{MARKER} {url}"), "read_failed"),
    # Not this module's deadline: still a failed download, still no text.
    (lambda url: TimeoutError(f"{MARKER} {url}"), "download_failed"),
])
async def test_an_unexpected_error_of_the_download_is_a_refusal(make, code, caplog):
    _loud(caplog)
    graph = FakeGraph(WORKBOOK)

    def boom(request):
        raise make(request.url)

    source = SharePointFile(graph.client(), PINS["site"], PINS["library"], PINS["path"],
                            download_transport=httpx.MockTransport(boom))
    with pytest.raises(Refused) as refused:
        await source.fetch()
    assert refused.value.code == code
    assert not tools._downloading.get() and tools._in_flight == []
    _secrets_absent(caplog, _said(refused.value), extra=(MARKER,))


@pytest.mark.parametrize("step", ["graph", "download"])
async def test_a_cancellation_is_not_turned_into_a_refusal(step):
    graph = FakeGraph(WORKBOOK)

    def cancelled(request):
        raise asyncio.CancelledError

    client = graph.client() if step == "download" else httpx.AsyncClient(
        base_url="https://graph.microsoft.com", transport=httpx.MockTransport(cancelled))
    source = SharePointFile(client, PINS["site"], PINS["library"], PINS["path"],
                            download_transport=httpx.MockTransport(cancelled))
    with pytest.raises(asyncio.CancelledError):
        await source.fetch()
    assert not source._lock.locked()
    assert not tools._downloading.get() and tools._in_flight == []


async def test_the_file_path_is_percent_encoded_on_the_wire():
    graph = FakeGraph(WORKBOOK)
    await _file(graph).fetch()
    item = graph.requests[-1]
    assert item.url.raw_path == b"/v1.0/drives/b!drive1/root:/Team/Planning%202026.xlsx"
    assert item.url.query == b""


async def test_a_path_with_url_syntax_stays_one_path_on_the_wire():
    # The save-time gate refuses these characters; this class must not depend
    # on it: nothing of the path may become a query string or a fragment.
    seen: list[httpx.Request] = []

    def graph(request):
        seen.append(request)
        path = request.url.path
        if path.endswith("/drives"):
            return httpx.Response(200, json={"value": [{"id": "b!drive1", "name": "Documents"}]})
        if path.startswith("/v1.0/drives/"):
            return httpx.Response(404)
        return httpx.Response(200, json={"id": "example.sharepoint.com,g1,g2"})

    client = httpx.AsyncClient(base_url="https://graph.microsoft.com",
                               transport=httpx.MockTransport(graph))
    source = SharePointFile(client, PINS["site"], PINS["library"], "Team/a?b=1#c%2Fd&e.xlsx")
    with pytest.raises(Refused) as refused:
        await source.fetch()
    assert refused.value.code == "file_not_found"
    item = seen[-1]
    assert item.url.raw_path == b"/v1.0/drives/b!drive1/root:/Team/a%3Fb%3D1%23c%252Fd%26e.xlsx"
    assert item.url.query == b"" and item.url.fragment == ""
    assert item.url.path == "/v1.0/drives/b!drive1/root:/Team/a?b=1#c%2Fd&e.xlsx"


def _httpx_filters() -> list:
    return [f for f in logging.getLogger("httpx").filters
            if type(f).__name__ == "_NoDownloadUrl"]


async def test_a_logging_setup_that_drops_the_filter_does_not_uncover_the_url(caplog):
    _loud(caplog)
    assert len(_httpx_filters()) == 1
    httpx_log = logging.getLogger("httpx")
    # What a non-incremental dictConfig / fileConfig naming ``httpx`` does.
    for installed in list(httpx_log.filters):
        httpx_log.removeFilter(installed)
    graph = FakeGraph(WORKBOOK)
    await _file(graph).fetch()
    refused = await _refused(FakeGraph(WORKBOOK, status={"download": 302}))
    assert len(graph.downloads) == 1
    assert "graph.microsoft.com/v1.0/sites" in caplog.text
    _secrets_absent(caplog, _said(refused))
    assert len(_httpx_filters()) == 1


async def test_installing_twice_and_a_reload_leave_one_filter():
    import importlib

    tools._install_once()
    tools._install_once()
    assert len(_httpx_filters()) == 1
    importlib.reload(tools)
    assert len(_httpx_filters()) == 1


async def test_a_line_about_the_download_from_another_context_is_dropped_too(caplog):
    # The context variable names the downloading task. A record about the same
    # URL that is created elsewhere (another task, a thread) is recognised by
    # what it says: the URL in flight or its query string.
    import contextvars

    _loud(caplog)
    graph = FakeGraph(WORKBOOK)
    httpx_log = logging.getLogger("httpx")

    def download(request):
        elsewhere = contextvars.Context()
        elsewhere.run(httpx_log.info, 'HTTP Request: GET %s "HTTP/1.1 200 OK"', request.url)
        elsewhere.run(httpx_log.info, "retrying ?%s", request.url.query.decode())
        elsewhere.run(httpx_log.info, "another client: %s", "https://graph.microsoft.com/v1.0/me")
        elsewhere.run(httpx_log.info, "%s %s", "too few arguments")  # cannot be rendered
        return httpx.Response(200, content=WORKBOOK)

    source = SharePointFile(graph.client(), PINS["site"], PINS["library"], PINS["path"],
                            download_transport=httpx.MockTransport(download))
    await source.fetch()
    assert "another client: https://graph.microsoft.com/v1.0/me" in caplog.text
    assert "too few arguments" not in caplog.text
    _secrets_absent(caplog)
    # Nothing is in flight afterwards: the same words are an ordinary line again.
    assert tools._in_flight == []
    httpx_log.info("after the download: %s", "tempauth-is-just-a-word-now")
    assert "tempauth-is-just-a-word-now" in caplog.text
