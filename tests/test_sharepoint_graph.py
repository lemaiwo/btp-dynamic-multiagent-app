"""How ``builtin:sharepoint`` fetches the pinned file (``SharePointFile``).

Covered: the three Graph reads, the download through a separate client (no
``Authorization``, no redirect, the site's own host only), the cache per file
version, the size cap, and that nothing Graph says is passed on.

No network: Graph and the download host are mock transports.

Run:  python -m pytest tests/test_sharepoint_graph.py
"""

from __future__ import annotations

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
