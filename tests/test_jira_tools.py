"""The Jira toolset: JQL construction, ceilings, and repeat-run safety.

Three things are being checked, and they fail in different ways.

JQL: the query is built here, never supplied by the agent, so a configured
project cannot be swapped out from a tool call and a value containing a quote
cannot append clauses of its own.

Ceilings: `project` and `status` are pinned by configuration; `lookback` can
be narrowed by a call but never widened.

Repeat-run safety: an issue this account already commented on is dropped from
the listing. That is the record the Outlook agent never had -- with nothing
able to mark a message handled, every run re-sent the same replies. Jira
carries the record in the issue itself.

The destination is stubbed with a resolver double rather than a MockTransport:
these tests are about Jira, and destination resolution has its own file.

No network, no Jira, no browser.

Run:  python tests/test_jira_tools.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)
os.environ["MCP_URL_ALLOWLIST"] = ""

import httpx  # noqa: E402

from agents.destination import Destination  # noqa: E402
from agents.jira_tools import (  # noqa: E402
    BUILTIN_JIRA_URL,
    DEFAULT_MAX_ISSUES,
    JiraClient,
    build_jql,
    jira_toolset,
)

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILED, PASSED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}   {detail}")


class FakeResolver:
    """Stands in for DestinationResolver, and counts invalidations."""

    def __init__(self, url: str = "https://jira.example") -> None:
        self.url = url
        self.invalidations = 0
        self.resolves = 0

    async def resolve(self, *, force: bool = False) -> Destination:
        self.resolves += 1
        return Destination(
            url=self.url,
            headers={"Authorization": "Bearer jira-token"},
            expires_at=time.monotonic() + 600,
        )

    def invalidate(self) -> None:
        self.invalidations += 1


def _issue(key="ABC-1", summary="Login fails", comments=None, status="Open") -> dict:
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "status": {"name": status},
            "reporter": {"name": "jsmith", "displayName": "J Smith"},
            "updated": "2026-08-20T09:00:00.000+0000",
            "description": "It fails.",
            "comment": {"comments": comments or []},
        },
    }


def _comment(author="someone", body="hi") -> dict:
    return {"id": "1", "author": {"name": author}, "body": body}


def _responder(issues, *, myself="agent-svc", capture=None, status=200):
    """A Jira double that records the search body it was sent.

    The body is parsed rather than substring-matched: asserting against JSON
    text couples the test to httpx's separator choices, and '"maxResults": 50'
    silently never matches the compact form httpx actually emits.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rest/api/2/myself"):
            return httpx.Response(200, json={"name": myself})
        if capture is not None:
            capture["url"] = str(request.url)
            capture["sent"] = json.loads(request.content) if request.content else {}
            capture["jql"] = capture["sent"].get("jql", "")
        if status != 200:
            return httpx.Response(status, json={"errorMessages": ["bad JQL"]})
        return httpx.Response(200, json={"issues": issues})

    return handle


def _client(handler, **kw):
    """A JiraClient wired to a MockTransport, plus the client to close."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JiraClient(FakeResolver(), http, **kw), http


async def _raises(coro) -> Exception | None:
    try:
        await coro
    except Exception as exc:  # noqa: BLE001 — the test is what kind
        return exc
    return None


def test_jql() -> None:
    print("\n-- JQL construction --")
    check("project and status",
          build_jql("ABC", "Open", None)
          == 'project = "ABC" AND status = "Open" ORDER BY updated ASC',
          detail=build_jql("ABC", "Open", None))
    check("project only",
          build_jql("ABC", "", None) == 'project = "ABC" ORDER BY updated ASC')
    check("status only",
          build_jql("", "Open", None) == 'status = "Open" ORDER BY updated ASC')
    check("neither is still valid JQL",
          build_jql("", "", None) == "ORDER BY updated ASC")
    check("the lookback window becomes a clause",
          build_jql("ABC", "", 120)
          == 'project = "ABC" AND updated >= "-120m" ORDER BY updated ASC',
          detail=build_jql("ABC", "", 120))
    # A value that closed the quote could otherwise append clauses of its own.
    check("an embedded quote is escaped",
          build_jql('AB"C', "", None) == 'project = "AB\\"C" ORDER BY updated ASC',
          detail=build_jql('AB"C', "", None))
    check("an embedded backslash is escaped",
          build_jql("AB\\C", "", None) == 'project = "AB\\\\C" ORDER BY updated ASC',
          detail=build_jql("AB\\C", "", None))


async def test_filters() -> None:
    print("\n-- configured values and ceilings --")

    capture: dict = {}
    client, http = _client(_responder([], capture=capture), project="ABC")
    async with http:
        await client.list_issues(project="OTHER")
    check("a configured project beats the agent's argument",
          'project = "ABC"' in capture["jql"] and "OTHER" not in capture["jql"],
          detail=capture["jql"])

    capture = {}
    client, http = _client(_responder([], capture=capture), project="")
    async with http:
        await client.list_issues(project="OTHER")
    check("the agent's value is used when config is blank",
          'project = "OTHER"' in capture["jql"], detail=capture["jql"])

    capture = {}
    client, http = _client(
        _responder([], capture=capture), project="ABC", lookback_minutes=1440)
    async with http:
        await client.list_issues(lookback_minutes=60)
    check("the agent may narrow the window",
          'updated >= "-60m"' in capture["jql"], detail=capture["jql"])

    capture = {}
    client, http = _client(
        _responder([], capture=capture), project="ABC", lookback_minutes=60)
    async with http:
        await client.list_issues(lookback_minutes=100000)
    check("the agent may not widen the window",
          'updated >= "-60m"' in capture["jql"], detail=capture["jql"])

    capture = {}
    client, http = _client(
        _responder([], capture=capture), project="ABC", lookback_minutes=60)
    async with http:
        await client.list_issues()
    check("the ceiling applies when the agent supplies nothing",
          'updated >= "-60m"' in capture["jql"], detail=capture["jql"])

    capture = {}
    client, http = _client(_responder([], capture=capture), project="ABC")
    async with http:
        await client.list_issues()
    check("no ceiling and no request omits the clause entirely",
          "updated >=" not in capture["jql"], detail=capture["jql"])

    capture = {}
    client, http = _client(_responder([], capture=capture), project="ABC")
    async with http:
        await client.list_issues(limit=5000)
    check("an absurd limit is clamped", capture["sent"]["maxResults"] == 50,
          detail=str(capture["sent"]))

    capture = {}
    client, http = _client(_responder([], capture=capture), project="ABC")
    async with http:
        await client.list_issues(limit=3)
    check("a reasonable limit passes through", capture["sent"]["maxResults"] == 3,
          detail=str(capture["sent"]))


async def test_answered_filtering() -> None:
    print("\n-- repeat-run safety --")

    issues = [_issue("ABC-1", comments=[_comment(author="agent-svc")])]
    client, http = _client(_responder(issues, myself="agent-svc"), project="ABC")
    async with http:
        result = await client.list_issues()
    check("an issue this account already answered is dropped", result == [],
          detail=str(result))

    issues = [_issue("ABC-1", comments=[_comment(author="jsmith")])]
    client, http = _client(_responder(issues, myself="agent-svc"), project="ABC")
    async with http:
        result = await client.list_issues()
    check("an issue answered only by others is kept",
          [i["key"] for i in result] == ["ABC-1"], detail=str(result))

    client, http = _client(_responder([_issue("ABC-1")]), project="ABC")
    async with http:
        result = await client.list_issues()
    check("an uncommented issue is kept and summarised",
          result and result[0]["summary"] == "Login fails"
          and result[0]["status"] == "Open"
          and result[0]["reporter"] == "J Smith",
          detail=str(result))

    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rest/api/2/myself"):
            calls["n"] += 1
            return httpx.Response(200, json={"name": "agent-svc"})
        return httpx.Response(200, json={"issues": []})

    client, http = _client(handle, project="ABC")
    async with http:
        await client.list_issues()
        await client.list_issues()
    check("the account identity is fetched once across calls", calls["n"] == 1,
          detail=str(calls["n"]))


async def test_transport() -> None:
    print("\n-- transport behaviour --")

    seen = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rest/api/2/myself"):
            return httpx.Response(200, json={"name": "agent-svc"})
        seen["n"] += 1
        if seen["n"] == 1:
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"issues": []})

    resolver = FakeResolver()
    http = httpx.AsyncClient(transport=httpx.MockTransport(flaky))
    client = JiraClient(resolver, http, project="ABC")
    async with http:
        result = await client.list_issues()
    check("a 401 invalidates the destination and retries",
          result == [] and resolver.invalidations == 1 and seen["n"] == 2,
          detail=f"invalidations={resolver.invalidations} calls={seen['n']}")

    def always401(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rest/api/2/myself"):
            return httpx.Response(200, json={"name": "agent-svc"})
        return httpx.Response(401, json={})

    client, http = _client(always401, project="ABC")
    async with http:
        err = await _raises(client.list_issues())
    check("a second 401 raises rather than looping",
          isinstance(err, httpx.HTTPStatusError), detail=repr(err))

    client, http = _client(_responder([], status=400), project="ABC")
    async with http:
        err = await _raises(client.list_issues())
    check("a rejected search reports the JQL it sent",
          err is not None and 'project = "ABC"' in str(err), detail=str(err))


DEST_ENV = {
    "DESTINATION_CLIENT_ID": "client",
    "DESTINATION_CLIENT_SECRET": "secret",
    "DESTINATION_TOKEN_URL": "https://uaa.example/oauth/token",
    "DESTINATION_URI": "https://dest.example",
}


def _tool_names(toolset) -> list[str]:
    """Tool names registered on a FunctionToolset, across pydantic-ai versions.

    Copied from tests/test_client_credentials.py for the same reason it exists
    there: the attribute has moved between releases.
    """
    tools = getattr(toolset, "tools", None)
    if isinstance(tools, dict):
        return list(tools)
    if tools is not None:
        return [getattr(t, "name", str(t)) for t in tools]
    return list(getattr(toolset, "_tools", {}))


def _with_dest_env() -> None:
    for key, value in DEST_ENV.items():
        os.environ[key] = value


def _without_dest_env() -> None:
    for key in DEST_ENV:
        os.environ.pop(key, None)
    os.environ.pop("DESTINATION_UAA_URL", None)
    os.environ.pop("VCAP_SERVICES", None)


def _build(oauth, http=None):
    return jira_toolset(
        oauth,
        http=http or httpx.AsyncClient(transport=httpx.MockTransport(_responder([]))),
        auth_mode="destination",
    )


def _sync_raises(fn) -> Exception | None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — the test is what kind
        return exc
    return None


async def test_read_and_write() -> None:
    print("\n-- get_issue and add_comment --")

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rest/api/2/myself"):
            return httpx.Response(200, json={"name": "agent-svc"})
        return httpx.Response(200, json=_issue(
            "ABC-1", comments=[_comment(author="jsmith", body="any news?")]))

    client, http = _client(handle, project="ABC")
    async with http:
        issue = await client.get_issue("ABC-1")
    check("get_issue returns the issue and its comment thread",
          issue["key"] == "ABC-1" and issue["comments"][0]["body"] == "any news?",
          detail=str(issue))

    capture: dict = {}

    def record(request: httpx.Request) -> httpx.Response:
        capture["method"] = request.method
        capture["path"] = request.url.path
        capture["sent"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "10", "author": {"name": "agent-svc"}})

    client, http = _client(record, project="ABC")
    async with http:
        await client.add_comment("ABC-1", "Try clearing the cache.")
    check("add_comment POSTs to the issue's comment endpoint",
          capture["method"] == "POST"
          and capture["path"] == "/rest/api/2/issue/ABC-1/comment",
          detail=f"{capture.get('method')} {capture.get('path')}")
    check("the body is a plain wiki-markup string, not ADF",
          capture["sent"] == {"body": "Try clearing the cache."},
          detail=str(capture.get("sent")))

    client, http = _client(record, project="ABC")
    async with http:
        err = await _raises(client.add_comment("ABC-1", "   "))
    check("an empty comment is refused", isinstance(err, ValueError), detail=repr(err))


def test_toolset_build() -> None:
    print("\n-- toolset construction and gating --")
    _with_dest_env()

    names = _tool_names(_build({"destination": "BC_ELIAGROUP_APIHUB_JIRA",
                                "project": "ABC"}))
    check("the read tools are present",
          "list_issues" in names and "get_issue" in names, detail=str(names))
    # The security-relevant assertion in this file: holding a credential that
    # can write must not be enough to give an agent a comment tool.
    check("add_comment is absent when commenting is off",
          "add_comment" not in names, detail=str(names))

    names = _tool_names(_build({"destination": "BC_ELIAGROUP_APIHUB_JIRA",
                                "project": "ABC", "allow_comment": True}))
    check("add_comment appears when commenting is opted into",
          "add_comment" in names, detail=str(names))

    err = _sync_raises(lambda: _build({"project": "ABC"}))
    check("a build without a destination name is refused",
          isinstance(err, ValueError) and "destination" in str(err), detail=repr(err))

    err = _sync_raises(lambda: _build({"destination": "X", "lookback": "soon"}))
    check("a bad lookback fails at build time, not mid-run",
          isinstance(err, ValueError), detail=repr(err))

    http = httpx.AsyncClient(transport=httpx.MockTransport(_responder([])))
    toolset = _build({"destination": "BC_ELIAGROUP_APIHUB_JIRA"}, http=http)
    check("the toolset exposes its http client for the registry to close",
          getattr(toolset, "http_client", None) is http)

    _without_dest_env()
    err = _sync_raises(lambda: jira_toolset(
        {"destination": "BC_ELIAGROUP_APIHUB_JIRA"},
        http=httpx.AsyncClient(), auth_mode="destination"))
    check("a missing binding names the variables to set",
          err is not None and "DESTINATION_CLIENT_ID" in str(err), detail=repr(err))
    _with_dest_env()

    from agents.builtins import BUILTIN_URLS, is_builtin_url

    check("builtin:jira is registered",
          BUILTIN_JIRA_URL in BUILTIN_URLS and is_builtin_url("builtin:jira"),
          detail=str(sorted(BUILTIN_URLS)))


async def main() -> None:
    test_jql()
    await test_filters()
    await test_answered_filtering()
    await test_transport()
    await test_read_and_write()
    test_toolset_build()
    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
