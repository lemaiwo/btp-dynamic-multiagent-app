"""The Confluence toolset: configuration, CQL, confinement, the three tools
and every refusal code, against a MockTransport. No network, no Confluence.

The destination is a resolver double (as in tests/test_jira_tools.py):
destination resolution has its own file.

Run:  python -m pytest tests/test_confluence_tools.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
for _var in ("DESTINATION_CLIENT_ID", "DESTINATION_CLIENT_SECRET",
             "DESTINATION_URI", "DESTINATION_TOKEN_URL", "DESTINATION_UAA_URL"):
    os.environ.pop(_var, None)

import httpx  # noqa: E402
import pytest  # noqa: E402

from agents import confluence_text, confluence_tools  # noqa: E402
from agents.confluence_tools import (  # noqa: E402
    BUILTIN_CONFLUENCE_URL,
    CONFLUENCE_API,
    ERROR_CODES,
    MAX_BODY_CHARS,
    MAX_SEARCH_LIMIT,
    ConfluenceClient,
    ConfluenceError,
    build_cql,
    clean_query,
    confine_page_id,
    confluence_toolset,
    normalize_api_base,
    normalize_spaces,
)
from agents.destination import Destination  # noqa: E402

SITE = "https://example.atlassian.net"
SECRET_KEY = "Zq9!bad key"


# --- configuration ------------------------------------------------------------

def test_normalize_spaces_accepts_csv_and_lists_and_dedupes():
    assert normalize_spaces("shERPa, DOCS,, shERPa ") == ["shERPa", "DOCS"]
    assert normalize_spaces(["A", "~user", "B-1_x"]) == ["A", "~user", "B-1_x"]
    assert normalize_spaces(None) == [] and normalize_spaces("") == []


def test_normalize_spaces_refuses_a_bad_key_without_repeating_it():
    with pytest.raises(ValueError) as info:
        normalize_spaces(f"DOCS, {SECRET_KEY}")
    assert str(info.value).startswith("oauth.spaces:") and SECRET_KEY not in str(info.value)
    with pytest.raises(ValueError):
        normalize_spaces('A" OR space = "B')


def test_normalize_api_base_defaults_to_cloud_and_keeps_jiras_rules():
    assert normalize_api_base("") == CONFLUENCE_API == "/wiki/rest/api"
    assert normalize_api_base(None) == "/wiki/rest/api"
    assert normalize_api_base(" /rest/api/ ") == "/rest/api"
    assert normalize_api_base("/") == ""
    for bad in ("https://x.example/wiki", "//x", "rest/api", "/rest/api?x=1", "/a/../b"):
        with pytest.raises(ValueError):
            normalize_api_base(bad)


# --- CQL ----------------------------------------------------------------------

def test_build_cql_text_title_and_space_pin():
    assert (build_cql("Z_QM06_FA_CREATE_INSP_LOT", [])
            == 'type = page AND text ~ "Z_QM06_FA_CREATE_INSP_LOT" ORDER BY lastmodified DESC')
    assert (build_cql("inspection lot", ["shERPa"], in_title=True)
            == 'type = page AND title ~ "inspection lot" AND space in ("shERPa") '
               "ORDER BY lastmodified DESC")
    assert 'space in ("A", "B")' in build_cql("x", ["A", "B"])


def test_build_cql_escapes_quotes_and_backslashes():
    assert build_cql('a" OR type = attachment OR text ~ "b', []) == (
        'type = page AND text ~ "a\\" OR type = attachment OR text ~ \\"b" '
        "ORDER BY lastmodified DESC")
    assert 'text ~ "a\\\\b"' in build_cql("a\\b", [])


def test_clean_query_strips_control_and_format_characters_and_bounds_length():
    assert clean_query("  Z_QM06​  FA\x07\tLOT ") == "Z_QM06 FA LOT"
    for empty in ("", "   ", "​\x00", None):
        with pytest.raises(ConfluenceError) as info:
            clean_query(empty)
        assert info.value.code == "invalid_query"
    with pytest.raises(ConfluenceError) as info:
        clean_query("x" * 201)
    assert info.value.code == "invalid_query"
    assert clean_query("x" * 200) == "x" * 200


def test_confine_page_id():
    assert confine_page_id(" 1241088001 ") == "1241088001"
    assert confine_page_id(1241088001) == "1241088001"
    for bad in ("", "abc", "12/../3", "1" * 21, "-1", None):
        with pytest.raises(ConfluenceError) as info:
            confine_page_id(bad)
        assert info.value.code == "invalid_id"


def test_error_result_shape_and_closed_code_list():
    err = ConfluenceError("not_found", "Confluence has no such page (404)", hint="check the id")
    assert err.as_result() == {"error": {"code": "not_found",
                                         "message": "Confluence has no such page (404)",
                                         "hint": "check the id"}}
    assert ConfluenceError("forbidden", "no").as_result() == {
        "error": {"code": "forbidden", "message": "no"}}
    assert ERROR_CODES == {
        "destination_error", "unauthorized", "forbidden", "not_found", "throttled",
        "confluence_error", "unreachable", "not_json", "invalid_id", "invalid_query",
        "space_not_allowed", "too_large"}
    with pytest.raises(AssertionError):
        ConfluenceError("made_up", "x")


# --- the client against a MockTransport ---------------------------------------

class FakeResolver:
    def __init__(self, url: str = SITE) -> None:
        self.url = url
        self.invalidations = 0
        self.resolves = 0
        self.fail: Exception | None = None

    async def resolve(self, *, force: bool = False) -> Destination:
        self.resolves += 1
        if self.fail is not None:
            raise self.fail
        return Destination(url=self.url, headers={"Authorization": "Basic c2VjcmV0"},
                           expires_at=time.monotonic() + 600,
                           proxy_type=getattr(self, "proxy_type", ""))

    def invalidate(self, principal: str | None = None) -> None:
        self.invalidations += 1


def _page(pid="1795129715", title="Action boxes (FSP)", space="shERPa", body="<p>Hello</p>",
          base=f"{SITE}/wiki"):
    return {
        "id": pid, "type": "page", "title": title,
        "space": {"key": space, "name": "shERPa space"},
        "version": {"number": 7, "when": "2026-09-30T10:11:12.000Z",
                    "by": {"displayName": "A. Author", "accountId": "acc-1",
                           "email": "a@example.com"}},
        "ancestors": [{"id": "1241088001", "title": "FSP Documentation"},
                      {"id": "1241088015", "title": "FSP - Overview"}],
        "body": {"storage": {"value": body, "representation": "storage"}},
        "_links": {"webui": f"/spaces/{space}/pages/{pid}/Action+boxes", "base": base},
    }


def _search_item(pid="1827012613", title="Action boxes (TSP)", space="shERPa",
                 excerpt="calls @@@hl@@@Z_QM06_FA_CREATE_INSP_LOT@@@endhl@@@ &amp; more <b>x</b>"):
    return {
        "content": {"id": pid, "type": "page", "title": title, "space": {"key": space},
                    "version": {"when": "2026-10-01T08:00:00.000Z"}},
        "title": title, "excerpt": excerpt, "lastModified": "2026-10-01T08:00:00.000Z",
        "url": f"/spaces/{space}/pages/{pid}",
    }


class Server:
    """A Confluence double: a route table by path suffix, and what it was sent."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.requests: list[httpx.Request] = []
        self.statuses: list[int] = []  # answered to the next requests, in order

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.statuses:
            status = self.statuses.pop(0)
            if status != 200:
                headers = {"Retry-After": "7"} if status == 429 else {}
                return httpx.Response(status, text="<html>error page</html>", headers=headers)
        for suffix, answer in self.routes.items():
            if request.url.path.endswith(suffix):
                if isinstance(answer, httpx.Response):
                    return answer
                return httpx.Response(200, json=answer)
        return httpx.Response(404, text="nope")

    def params(self, index: int = -1) -> dict[str, list[str]]:
        return parse_qs(urlsplit(str(self.requests[index].url)).query)


def _client(server: Server, **kw) -> tuple[ConfluenceClient, httpx.AsyncClient, FakeResolver]:
    resolver = FakeResolver()
    http = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    return ConfluenceClient(resolver, http, **kw), http, resolver


async def _refused(coro) -> ConfluenceError:
    with pytest.raises(ConfluenceError) as info:
        await coro
    return info.value


async def test_search_builds_cql_server_side_and_answers_the_fields():
    server = Server()
    server.routes["/wiki/rest/api/search"] = {"results": [_search_item()], "totalSize": 3,
                                              "_links": {"next": "/rest/api/search?x"}}
    client, http, _ = _client(server, spaces=["shERPa"])
    async with http:
        out = await client.search_pages('Z_QM06" OR text ~ "x', in_title=False, limit=10)
    sent = server.params()
    assert sent["cql"] == ['type = page AND text ~ "Z_QM06\\" OR text ~ \\"x" '
                           'AND space in ("shERPa") ORDER BY lastmodified DESC']
    assert sent["limit"] == ["10"] and sent["excerpt"] == ["highlight"]
    assert sent["expand"] == ["content.space,content.version"]
    assert server.requests[0].headers["Authorization"] == "Basic c2VjcmV0"
    assert str(server.requests[0].url).startswith(f"{SITE}/wiki/rest/api/search?")
    assert out == {
        "query": 'Z_QM06" OR text ~ "x', "in_title": False, "total": 3, "truncated": True,
        "results": [{"id": "1827012613", "title": "Action boxes (TSP)", "space": "shERPa",
                     "last_modified": "2026-10-01T08:00:00.000Z",
                     "excerpt": "calls Z_QM06_FA_CREATE_INSP_LOT & more x"}],
    }


async def test_search_strips_highlight_markers_from_titles_too():
    server = Server()
    item = _search_item(title="Action @@@hl@@@boxes@@@endhl@@@ (TSP)")
    item["content"]["title"] = "Action boxes (TSP)"
    server.routes["/search"] = {"results": [item], "totalSize": 1}
    client, http, _ = _client(server)
    async with http:
        out = await client.search_pages("boxes")
    assert out["results"][0]["title"] == "Action boxes (TSP)"


async def test_search_in_title_uses_the_title_field_and_an_unpinned_client_adds_no_space_clause():
    server = Server()
    server.routes["/search"] = {"results": [], "totalSize": 0}
    client, http, _ = _client(server)
    async with http:
        out = await client.search_pages("inspection lot", in_title=True)
    assert server.params()["cql"] == [
        'type = page AND title ~ "inspection lot" ORDER BY lastmodified DESC']
    assert out["results"] == [] and out["total"] == 0 and out["truncated"] is False


async def test_search_limit_is_clamped():
    server = Server()
    server.routes["/search"] = {"results": []}
    client, http, _ = _client(server)
    async with http:
        await client.search_pages("x", limit=99)
        await client.search_pages("x", limit=0)
        await client.search_pages("x", limit=-5)
    assert [p["limit"] for p in (server.params(0), server.params(1), server.params(2))] == [
        [str(MAX_SEARCH_LIMIT)], ["1"], ["1"]]


async def test_search_drops_a_result_outside_the_pin_and_cuts_the_excerpt():
    server = Server()
    server.routes["/search"] = {"results": [
        _search_item(pid="1", space="OTHER"),
        _search_item(pid="2", excerpt="y" * 500),
        {"content": {"id": "3", "type": "attachment"}, "title": "file.pdf"},
        "not an object",
    ], "totalSize": 4}
    client, http, _ = _client(server, spaces=["shERPa"])
    async with http:
        out = await client.search_pages("x")
    assert [r["id"] for r in out["results"]] == ["2"]
    assert len(out["results"][0]["excerpt"]) == 300
    assert out["truncated"] is True  # 4 matched, 1 answered


async def test_search_refuses_a_bad_query_before_any_request():
    server = Server()
    client, http, _ = _client(server)
    async with http:
        err = await _refused(client.search_pages("  "))
    assert err.code == "invalid_query" and server.requests == []


async def test_get_page_answers_text_metadata_and_a_url_on_the_sites_host():
    server = Server()
    body = ("<h2>Functional Context</h2><p>Creates an &quot;inspection lot&quot;.</p>"
            "<table><tr><th>Object</th><th>Type</th></tr>"
            "<tr><td>Z_QM06_FA_CREATE_INSP_LOT</td><td>FUNC</td></tr></table>")
    server.routes["/content/1795129715"] = _page(body=body)
    client, http, _ = _client(server, spaces=["shERPa"])
    async with http:
        out = await client.get_page("1795129715")
    assert server.params()["expand"] == ["body.storage,version,space,ancestors"]
    assert out == {
        "id": "1795129715", "title": "Action boxes (FSP)", "space": "shERPa", "version": 7,
        "last_modified": "2026-09-30T10:11:12.000Z", "last_modified_by": "A. Author",
        "ancestors": [{"id": "1241088001", "title": "FSP Documentation"},
                      {"id": "1241088015", "title": "FSP - Overview"}],
        "url": f"{SITE}/wiki/spaces/shERPa/pages/1795129715/Action+boxes",
        "body": ("## Functional Context\n\nCreates an \"inspection lot\".\n\n"
                 "| Object | Type |\n| --- | --- |\n| Z_QM06_FA_CREATE_INSP_LOT | FUNC |"),
        "body_chars": len(out["body"]), "truncated": False,
    }
    assert "acc-1" not in json.dumps(out) and "a@example.com" not in json.dumps(out)


async def test_get_page_url_ignores_a_base_on_another_host():
    server = Server()
    server.routes["/content/1795129715"] = _page(base="https://evil.example/wiki")
    client, http, _ = _client(server)
    async with http:
        out = await client.get_page("1795129715")
    assert out["url"] == f"{SITE}/spaces/shERPa/pages/1795129715/Action+boxes"
    server.routes["/content/1795129715"] = _page(base="")
    server.routes["/content/1795129715"]["_links"]["webui"] = "//evil.example/x"
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handler)) as http2:
        out = await ConfluenceClient(FakeResolver(), http2).get_page("1795129715")
    assert out["url"] == ""


async def test_get_page_outside_the_pin_is_refused_without_its_body():
    server = Server()
    server.routes["/content/1"] = _page(pid="1", space="HR", body="<p>salaries</p>")
    client, http, _ = _client(server, spaces=["shERPa", "DOCS"])
    async with http:
        err = await _refused(client.get_page("1"))
    assert err.code == "space_not_allowed"
    assert "salaries" not in err.message and "salaries" not in (err.hint or "")
    assert err.hint == "allowed spaces: shERPa, DOCS"


async def test_get_page_without_a_space_key_is_refused_when_pinned():
    server = Server()
    page = _page(pid="1")
    del page["space"]
    server.routes["/content/1"] = page
    client, http, _ = _client(server, spaces=["shERPa"])
    async with http:
        assert (await _refused(client.get_page("1"))).code == "space_not_allowed"
    # Unpinned: the page is answered, with an empty space.
    client2, http2, _ = _client(server)
    async with http2:
        assert (await client2.get_page("1"))["space"] == ""


async def test_get_page_body_is_cut_at_the_cap_and_said_so():
    server = Server()
    server.routes["/content/1"] = _page(pid="1", body="<p>" + "x" * (MAX_BODY_CHARS + 5) + "</p>")
    client, http, _ = _client(server)
    async with http:
        out = await client.get_page("1")
    assert len(out["body"]) == MAX_BODY_CHARS
    assert out["body_chars"] == MAX_BODY_CHARS + 5 and out["truncated"] is True


async def test_get_page_over_the_html_cap_is_too_large(monkeypatch):
    monkeypatch.setattr(confluence_text, "MAX_HTML_BYTES", 50)
    server = Server()
    server.routes["/content/1"] = _page(pid="1", body="<p>" + "x" * 100 + "</p>")
    client, http, _ = _client(server)
    async with http:
        assert (await _refused(client.get_page("1"))).code == "too_large"


async def test_get_page_with_a_bad_id_sends_nothing():
    server = Server()
    client, http, _ = _client(server)
    async with http:
        assert (await _refused(client.get_page("../search"))).code == "invalid_id"
    assert server.requests == []


async def test_children_are_listed_after_the_parents_space_was_checked_and_cached():
    server = Server()
    server.routes["/content/1275592750/child/page"] = {
        "results": [{"id": "10", "title": "A (TSP)", "version": {"when": "2026-01-02T03:04:05Z"}},
                    {"id": "11", "title": "B (TSP)"}],
        "start": 0, "limit": 2, "size": 2, "_links": {"next": "/rest/api/content/x?start=2"}}
    server.routes["/content/1275592750"] = _page(pid="1275592750", title="TSP - Overview")
    client, http, _ = _client(server, spaces=["shERPa"])
    async with http:
        out = await client.list_children("1275592750", start=0, limit=2)
        again = await client.list_children("1275592750", start=2, limit=2)
    assert out == {"id": "1275592750", "start": 0, "size": 2, "next_start": 2,
                   "results": [{"id": "10", "title": "A (TSP)",
                                "last_modified": "2026-01-02T03:04:05Z"},
                               {"id": "11", "title": "B (TSP)", "last_modified": ""}]}
    assert again["start"] == 2
    # One parent read for the two calls, then the two child reads.
    paths = [r.url.path for r in server.requests]
    assert paths == ["/wiki/rest/api/content/1275592750",
                     "/wiki/rest/api/content/1275592750/child/page",
                     "/wiki/rest/api/content/1275592750/child/page"]
    assert server.params(0)["expand"] == ["space"]
    assert server.params(1)["start"] == ["0"] and server.params(1)["limit"] == ["2"]
    assert server.params(1)["expand"] == ["version"]


async def test_children_of_a_parent_outside_the_pin_are_refused_unpinned_skips_the_check():
    server = Server()
    server.routes["/content/1/child/page"] = {"results": []}
    server.routes["/content/1"] = _page(pid="1", space="HR")
    client, http, _ = _client(server, spaces=["shERPa"])
    async with http:
        assert (await _refused(client.list_children("1"))).code == "space_not_allowed"
    assert [r.url.path for r in server.requests] == ["/wiki/rest/api/content/1"]
    client2, http2, _ = _client(server)
    async with http2:
        out = await client2.list_children("1")
    assert out["results"] == [] and out["next_start"] is None
    assert server.requests[-1].url.path == "/wiki/rest/api/content/1/child/page"


async def test_children_start_and_limit_are_clamped():
    server = Server()
    server.routes["/child/page"] = {"results": []}
    client, http, _ = _client(server)
    async with http:
        await client.list_children("1", start=-3, limit=500)
        await client.list_children("1", start=99_999, limit=0)
    assert server.params(0)["start"] == ["0"] and server.params(0)["limit"] == ["100"]
    assert server.params(1)["start"] == ["10000"] and server.params(1)["limit"] == ["1"]


# --- refusals by status and transport -----------------------------------------

async def test_a_401_refreshes_the_destination_once_and_retries():
    server = Server()
    server.statuses = [401]
    server.routes["/content/1"] = _page(pid="1")
    client, http, resolver = _client(server)
    async with http:
        out = await client.get_page("1")
    assert out["id"] == "1" and resolver.invalidations == 1 and len(server.requests) == 2


async def test_a_second_401_is_unauthorized_after_one_refresh():
    server = Server()
    server.statuses = [401, 401, 401]
    server.routes["/content/1"] = _page(pid="1")
    client, http, resolver = _client(server)
    async with http:
        err = await _refused(client.get_page("1"))
    assert err.code == "unauthorized" and len(server.requests) == 2
    assert resolver.invalidations == 1 and "error page" not in err.message


@pytest.mark.parametrize("status, code", [(403, "forbidden"), (404, "not_found"),
                                          (429, "throttled"), (500, "confluence_error"),
                                          (418, "confluence_error")])
async def test_statuses_map_to_fixed_codes_without_the_body(status, code, caplog):
    caplog.set_level(logging.WARNING)
    server = Server()
    server.statuses = [status]
    server.routes["/content/1"] = _page(pid="1")
    client, http, _ = _client(server)
    async with http:
        err = await _refused(client.get_page("1"))
    assert err.code == code and "error page" not in err.message
    if status == 429:
        assert err.hint == "retry after 7 seconds"
    if status >= 500 or status == 418:
        assert f"HTTP {status}" in err.message
    assert "error page" not in caplog.text and "Basic" not in caplog.text


async def test_a_non_json_answer_is_not_json():
    server = Server()
    server.routes["/content/1"] = httpx.Response(200, text="<html>sign in</html>",
                                                 headers={"content-type": "text/html"})
    client, http, _ = _client(server)
    async with http:
        assert (await _refused(client.get_page("1"))).code == "not_json"
    server.routes["/content/1"] = httpx.Response(200, content=b"not json {",
                                                 headers={"content-type": "application/json"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handler)) as http2:
        err = await _refused(ConfluenceClient(FakeResolver(), http2).get_page("1"))
    assert err.code == "not_json"


async def test_an_answer_over_the_byte_cap_is_too_large(monkeypatch):
    monkeypatch.setattr(confluence_tools, "MAX_RESPONSE_BYTES", 100)
    server = Server()
    server.routes["/content/1"] = _page(pid="1", body="<p>" + "x" * 1000 + "</p>")
    client, http, _ = _client(server)
    async with http:
        assert (await _refused(client.get_page("1"))).code == "too_large"


async def test_a_transport_error_is_unreachable(caplog):
    caplog.set_level(logging.WARNING)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    client = ConfluenceClient(FakeResolver(), http)
    err = await _refused(client.get_page("1"))
    assert err.code == "unreachable" and "ConnectTimeout" in caplog.text
    assert SITE not in err.message


@pytest.mark.parametrize("url, proxy_type", [("http://confluence.internal", ""),
                                             ("http://virtual.host", "OnPremise"),
                                             (SITE, "OnPremise")])
async def test_a_destination_that_is_not_https_internet_is_refused_before_any_request(
    url, proxy_type, caplog
):
    caplog.set_level(logging.WARNING)
    server = Server()
    server.routes["/content/1"] = _page(pid="1")
    resolver = FakeResolver(url=url)
    resolver.proxy_type = proxy_type
    http = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    async with http:
        err = await _refused(ConfluenceClient(resolver, http).get_page("1"))
    assert err.code == "destination_error" and server.requests == []
    assert url not in err.message and url not in caplog.text


async def test_httpx_does_not_log_the_request_url(caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    server = Server()
    server.routes["/search"] = {"results": []}
    client, http, _ = _client(server, spaces=["shERPa"])
    async with http:
        await client.search_pages("Z_SECRET_NAME")
    assert server.requests, "the request was sent"
    assert "Z_SECRET_NAME" not in caplog.text and "HTTP Request" not in caplog.text
    # Another client's requests are still logged.
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handler)) as other:
        await other.get(f"{SITE}/wiki/rest/api/search?cql=other")
    assert "HTTP Request" in caplog.text


async def test_a_resolver_failure_is_destination_error(caplog):
    caplog.set_level(logging.WARNING)
    from agents.destination import DestinationError

    server = Server()
    client, http, resolver = _client(server)
    resolver.fail = DestinationError("destination JIRA does not exist in the subaccount "
                                     "(https://destination.example/x?token=1)")
    async with http:
        err = await _refused(client.search_pages("x"))
    assert err.code == "destination_error" and server.requests == []
    assert "token=1" not in err.message and "DestinationError" in caplog.text
    assert "token=1" not in caplog.text


# --- the factory --------------------------------------------------------------

def _patch_resolver(monkeypatch, resolver: FakeResolver | None = None) -> FakeResolver:
    fake = resolver or FakeResolver()
    monkeypatch.setattr(confluence_tools, "_build_resolver", lambda destination, key: fake)
    return fake


def test_the_factory_builds_three_tools_and_exposes_its_client(monkeypatch):
    _patch_resolver(monkeypatch)
    toolset = confluence_toolset({"destination": "JIRA", "spaces": "shERPa"})
    assert set(toolset.tools) == {"search_pages", "get_page", "list_children"}
    assert isinstance(toolset.http_client, httpx.AsyncClient)
    for name in ("search_pages", "get_page", "list_children"):
        doc = toolset.tools[name].function.__doc__ or ""
        assert "other people" in doc or "data" in doc


def test_the_factory_refuses_what_it_cannot_build(monkeypatch):
    _patch_resolver(monkeypatch)
    with pytest.raises(ValueError, match="requires auth_mode=destination"):
        confluence_toolset({"destination": "JIRA"}, auth_mode="app_only")
    with pytest.raises(ValueError, match="^oauth.user_context:"):
        confluence_toolset({"destination": "JIRA", "user_context": True})
    with pytest.raises(ValueError, match="requires a 'destination'"):
        confluence_toolset({"spaces": "shERPa"})
    with pytest.raises(ValueError, match="^oauth.spaces:"):
        confluence_toolset({"destination": "JIRA", "spaces": SECRET_KEY})
    with pytest.raises(ValueError, match="api_base"):
        confluence_toolset({"destination": "JIRA", "api_base": "https://x.example/wiki"})
    assert confluence_toolset({"destination": "JIRA"}, auth_mode="destination") is not None


def test_without_a_pin_the_build_warns_once(monkeypatch, caplog):
    _patch_resolver(monkeypatch)
    caplog.set_level(logging.WARNING)
    confluence_toolset({"destination": "JIRA"}, agent_name="reviewer")
    lines = [r.getMessage() for r in caplog.records if "no oauth.spaces" in r.getMessage()]
    assert len(lines) == 1 and "'reviewer'" in lines[0] and "'JIRA'" in lines[0]
    caplog.clear()
    confluence_toolset({"destination": "JIRA", "spaces": "shERPa"})
    assert "no oauth.spaces" not in caplog.text


def test_without_a_binding_the_factory_names_the_server():
    from agents.destination import DestinationError

    with pytest.raises(DestinationError, match=f"{BUILTIN_CONFLUENCE_URL}: no destination"):
        confluence_toolset({"destination": "JIRA"})


async def test_a_tool_answers_a_refusal_never_raises(monkeypatch):
    server = Server()
    server.routes["/content/5"] = _page(pid="5", space="HR")
    _patch_resolver(monkeypatch)
    http = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    toolset = confluence_toolset({"destination": "JIRA", "spaces": "shERPa"}, http=http)
    async with http:
        bad_id = await toolset.tools["get_page"].function("../x")
        other_space = await toolset.tools["get_page"].function("5")
        bad_query = await toolset.tools["search_pages"].function("")
        bad_child = await toolset.tools["list_children"].function("x")
    assert bad_id["error"]["code"] == "invalid_id"
    assert other_space["error"]["code"] == "space_not_allowed"
    assert bad_query["error"]["code"] == "invalid_query"
    assert bad_child["error"]["code"] == "invalid_id"


async def test_an_unexpected_exception_becomes_confluence_error(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    _patch_resolver(monkeypatch)
    toolset = confluence_toolset({"destination": "JIRA"})

    async def explode(*a, **k):
        raise RuntimeError("secret detail https://x.example/?token=1")

    monkeypatch.setattr(ConfluenceClient, "get_page", explode)
    out = await toolset.tools["get_page"].function("1")
    assert out["error"]["code"] == "confluence_error"
    assert "RuntimeError" in out["error"]["message"] and "token" not in json.dumps(out)
    assert "token" not in caplog.text
