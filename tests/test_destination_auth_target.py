"""An absolute URL gets the destination's credential only on the
destination's own scheme, host and port (or https on the default port of an
expected API host): a paging link from a hostile or tampered answer must not
carry the credential over plain http or to another port of the same host."""

from __future__ import annotations

import time

import httpx
import pytest

from agents.destination import Destination
from agents.destination_auth import DestinationError, destination_http_client


class _Resolver:
    name = "FAKE"

    def __init__(self, url: str) -> None:
        self.url = url

    async def resolve(self, *, force: bool = False, user_token=None, principal=None) -> Destination:
        return Destination(
            url=self.url,
            headers={"Authorization": "Bearer secret-token"},
            expires_at=time.monotonic() + 60,
            auth_type="OAuth2ClientCredentials",
        )

    def invalidate(self, principal: str | None = None) -> None:  # pragma: no cover
        pass


def _client(dest_url: str, sent: list[httpx.Request], **kw) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={})

    return destination_http_client(
        _Resolver(dest_url), transport=httpx.MockTransport(handler), **kw
    )


@pytest.mark.parametrize(
    "link",
    [
        "http://api.example.com/plain",
        "https://api.example.com:8443/x",
        "http://api.example.com:443/x",
        "https://api.example.com:80/x",
    ],
)
async def test_other_scheme_or_port_on_the_destination_host_is_refused(link):
    sent: list[httpx.Request] = []
    async with _client("https://api.example.com", sent) as http:
        with pytest.raises(DestinationError, match="refusing to send"):
            await http.get(link)
    assert sent == [], "nothing was sent, so no Authorization left"


@pytest.mark.parametrize(
    "link", ["https://api.example.com/next?page=2", "https://api.example.com:443/next"]
)
async def test_same_scheme_host_and_default_port_passes(link):
    sent: list[httpx.Request] = []
    async with _client("https://api.example.com/base", sent) as http:
        r = await http.get(link)
    assert r.status_code == 200
    (req,) = sent
    assert req.headers["Authorization"] == "Bearer secret-token"
    assert req.url.scheme == "https" and req.url.host == "api.example.com"


async def test_a_destination_on_its_own_port_allows_only_that_port():
    sent: list[httpx.Request] = []
    async with _client("https://api.example.com:8443", sent) as http:
        await http.get("https://api.example.com:8443/next")
        with pytest.raises(DestinationError, match="refusing to send"):
            await http.get("https://api.example.com/next")
    assert len(sent) == 1 and sent[0].url.port == 8443


@pytest.mark.parametrize(
    "link",
    ["http://graph.microsoft.com/v1.0/x", "https://graph.microsoft.com:8443/v1.0/x"],
)
async def test_an_expected_host_needs_https_on_the_default_port(link):
    sent: list[httpx.Request] = []
    async with _client(
        "https://proxy.example/graph", sent, expected_hosts=("graph.microsoft.com",)
    ) as http:
        with pytest.raises(DestinationError, match="refusing to send"):
            await http.get(link)
        await http.get("https://graph.microsoft.com:443/v1.0/x?$skiptoken=1")
        await http.get("https://graph.microsoft.com/v1.0/x?$skiptoken=2")
    assert len(sent) == 2
    assert all(r.headers["Authorization"] == "Bearer secret-token" for r in sent)
