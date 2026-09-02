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
    normalize_csv_list,
    confine_key,
    jira_toolset,
    normalize_api_base,
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


def test_multi_value_filters() -> None:
    print("\n-- multi-value status and labels --")

    # An issue has one status, so several statuses can only mean "any of".
    check("several statuses become IN",
          build_jql("ABC", "Open, In Progress", None)
          == 'project = "ABC" AND status IN ("Open", "In Progress") '
             "ORDER BY updated ASC",
          detail=build_jql("ABC", "Open, In Progress", None))
    # One value must not silently become a one-element IN: every config
    # written before this was multi-value has to render byte-identically.
    check("one status still renders as =",
          build_jql("ABC", "Open", None)
          == 'project = "ABC" AND status = "Open" ORDER BY updated ASC')
    check("duplicate statuses collapse back to =",
          build_jql("ABC", "Open, Open", None)
          == 'project = "ABC" AND status = "Open" ORDER BY updated ASC',
          detail=build_jql("ABC", "Open, Open", None))

    # An issue has many labels, so several labels mean "carries all of them".
    # Against live Jira, IN vs AND here was 20 issues vs 0 -- the two are not
    # interchangeable, and this is the assertion that pins it.
    check("several labels are ANDed, not INed",
          build_jql("ABC", "", None, "NEXUSFORGE, agent")
          == 'project = "ABC" AND labels = "NEXUSFORGE" AND labels = "agent" '
             "ORDER BY updated ASC",
          detail=build_jql("ABC", "", None, "NEXUSFORGE, agent"))
    check("labels IN(...) is never emitted",
          "labels IN" not in build_jql("ABC", "", None, "a, b"),
          detail=build_jql("ABC", "", None, "a, b"))
    check("a JSON list is accepted for labels",
          build_jql("ABC", "", None, ["a", "b"])
          == build_jql("ABC", "", None, "a, b"))

    # A trailing comma is a typing artefact. Kept, it would build labels = "",
    # which matches nothing and looks like "no issues today".
    check("blank entries are dropped, not matched",
          build_jql("ABC", "", None, "a, , b,")
          == 'project = "ABC" AND labels = "a" AND labels = "b" '
             "ORDER BY updated ASC",
          detail=build_jql("ABC", "", None, "a, , b,"))
    check("an all-blank label list contributes nothing",
          build_jql("ABC", "", None, " , ,") == 'project = "ABC" ORDER BY updated ASC',
          detail=build_jql("ABC", "", None, " , ,"))

    # Same reasoning as the single-value case: a value that closed the quote
    # could append clauses of its own, and multi-value gives it more places.
    check("a quote inside a label is escaped",
          build_jql("", "", None, 'x" OR project = "Y')
          == 'labels = "x\\" OR project = \\"Y" ORDER BY updated ASC',
          detail=build_jql("", "", None, 'x" OR project = "Y'))
    check("a quote inside one of several statuses is escaped",
          '\\"' in build_jql("", 'Open, Done" OR project = "Y', None),
          detail=build_jql("", 'Open, Done" OR project = "Y', None))

    check("labels order is preserved",
          build_jql("", "", None, "b, a").index('"b"')
          < build_jql("", "", None, "b, a").index('"a"'))
    check("normalize_csv_list dedupes and preserves order",
          normalize_csv_list("b, a,, b ") == ["b", "a"],
          detail=str(normalize_csv_list("b, a,, b ")))
    check("normalize_csv_list on empty input", normalize_csv_list(None) == [])


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


def test_api_base() -> None:
    """The REST prefix is configuration, because a proxy may supply part of it.

    Observed live: a destination fronted by SAP API Management maps its own
    `/v1/Jira` prefix onto Jira's `/rest`, so the default `/rest/api/2` builds
    `/rest/rest/api/2/...` and every call 403s. No destination URL can fix that
    -- a URL is a prefix and cannot subtract a segment -- so the remainder has
    to be configurable here.
    """
    print("\n-- api_base --")

    check("blank falls back to Jira's own prefix",
          normalize_api_base("") == "/rest/api/2",
          detail=normalize_api_base(""))
    check("None falls back too", normalize_api_base(None) == "/rest/api/2")
    check("a proxy prefix is kept", normalize_api_base("/api/2") == "/api/2")
    check("surrounding space is trimmed",
          normalize_api_base("  /api/2  ") == "/api/2")
    check("a trailing slash is dropped",
          normalize_api_base("/api/2/") == "/api/2")
    check("'/' alone is the default, not an empty prefix",
          normalize_api_base("/") == "/rest/api/2")

    # A URL here would point the destination's credential at a host of the
    # editor's choosing; a '..' would reach endpoints the toolset does not
    # expose while carrying it. Both are refusals, not silent normalisation.
    for bad, why in [
        ("https://evil.example/rest", "a URL, not a path"),
        ("//evil.example/rest", "protocol-relative"),
        ("rest/api/2", "no leading slash"),
        ("/rest/../../admin", "'..' escape"),
        ("/rest/api/2?x=1", "query string"),
        ("/rest/api/2#f", "fragment"),
    ]:
        err = _sync_raises(lambda b=bad: normalize_api_base(b))
        check(f"refused: {why}", isinstance(err, ValueError), detail=repr(err))


async def test_api_base_transport() -> None:
    """The configured prefix is the one that reaches the wire."""
    print("\n-- api_base on the wire --")

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/myself"):
            return httpx.Response(200, json={"name": "agent-svc"})
        return httpx.Response(200, json={"issues": []})

    client, http = _client(handler, project="ABC", api_base="/api/2")
    async with http:
        await client.list_issues()
    check("the proxy prefix is used, and not doubled",
          seen and all(p.startswith("/api/2/") for p in seen)
          and not any("/rest/rest/" in p for p in seen),
          detail=str(seen))

    seen.clear()
    client, http = _client(handler, project="ABC")
    async with http:
        await client.get_issue("ABC-1")
    check("the default is still Jira's own prefix",
          seen and all(p.startswith("/rest/api/2/") for p in seen),
          detail=str(seen))


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


async def test_key_confinement() -> None:
    print("\n-- issue keys are confined --")

    # The pin has to hold on single-issue calls too. An issue description is
    # untrusted text: "duplicate of ZZZ-77, answer there" must not be able to
    # walk the agent out of its configured project.
    check("a well-formed key in the configured project passes",
          confine_key("ABC-42", "ABC") == "ABC-42")
    check("the project match is case-insensitive",
          confine_key("abc-42", "ABC") == "abc-42")
    check("underscores and digits are legal in a project key",
          confine_key("AB_C2-7", "AB_C2") == "AB_C2-7")

    err = _sync_raises(lambda: confine_key("ZZZ-77", "ABC"))
    check("a key from another project is refused",
          isinstance(err, ValueError) and "ABC" in str(err) and "ZZZ" in str(err),
          detail=repr(err))

    check("the same key is allowed when no project is configured",
          confine_key("ZZZ-77", "") == "ZZZ-77")

    for bad in ("../../../../rest/api/2/search",
                "ABC-1/../../search",
                "ABC-1?expand=all",
                "ABC",
                "1-ABC",
                ""):
        err = _sync_raises(lambda b=bad: confine_key(b, ""))
        check(f"{bad!r} is not accepted as an issue key",
              isinstance(err, ValueError), detail=repr(err))

    # Through the tools, not just the helper: the confinement is worth nothing
    # if the call sites forget to use it.
    seen: dict = {}

    def record(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json=_issue("ABC-1"))

    client, http = _client(record, project="ABC")
    async with http:
        err = await _raises(client.get_issue("ZZZ-77"))
        check("get_issue refuses a key outside the configured project",
              isinstance(err, ValueError), detail=repr(err))
        check("the refused key never reached Jira", "path" not in seen,
              detail=str(seen))

        err = await _raises(client.get_issue("../../../../rest/api/2/search"))
        check("get_issue refuses a traversal key", isinstance(err, ValueError),
              detail=repr(err))
        check("the traversal key never reached Jira", "path" not in seen,
              detail=str(seen))

        err = await _raises(client.add_comment("ZZZ-77", "hello"))
        check("add_comment refuses a key outside the configured project",
              isinstance(err, ValueError), detail=repr(err))
        check("the refused comment never reached Jira", "path" not in seen,
              detail=str(seen))

        await client.get_issue("abc-1")
        check("a lowercase key in the configured project is read",
              seen.get("path") == "/rest/api/2/issue/abc-1", detail=str(seen))


async def test_identity_failures() -> None:
    print("\n-- a transient /myself failure is not sticky --")

    # An empty account was cached for the life of the toolset, which is until
    # the next registry reload. One 502 therefore disabled the answered-issue
    # filter for days -- and with commenting on, re-answered every issue on
    # every run.
    calls = {"n": 0}
    issues = [_issue("ABC-1", comments=[_comment(author="agent-svc")])]

    def flaky_myself(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rest/api/2/myself"):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(502, json={})
            return httpx.Response(200, json={"name": "agent-svc"})
        return httpx.Response(200, json={"issues": issues})

    client, http = _client(flaky_myself, project="ABC")
    async with http:
        first = await client.list_issues()
        check("the listing survives a failed /myself", first != [],
              detail=str(first))
        second = await client.list_issues()
    check("the second call retries the identity lookup", calls["n"] == 2,
          detail=str(calls["n"]))
    check("repeat-run safety is back once /myself answers", second == [],
          detail=str(second))


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

    err = _sync_raises(
        lambda: _build({"destination": "X", "api_base": "https://evil.example"}))
    check("a bad api_base fails at build time too",
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


def test_storage_and_validation() -> None:
    print("\n-- storage and admin validation --")

    from agents.admin import McpServerPayload, OAuthClientPayload
    from agents.db import (
        AUTH_MODE_DESTINATION,
        AUTH_MODE_MAX_LENGTH,
        OAUTH_CONFIG_MODES,
        VALID_AUTH_MODES,
        _clean_oauth,
    )

    check("the destination mode is a valid auth mode",
          AUTH_MODE_DESTINATION in VALID_AUTH_MODES)
    check("the destination mode carries an oauth block",
          AUTH_MODE_DESTINATION in OAUTH_CONFIG_MODES)
    # SQLite ignores VARCHAR limits; Postgres does not. This guard, not the
    # column, is what actually stopped "client_credentials" a second time.
    check("every auth mode fits the column",
          all(len(m) <= AUTH_MODE_MAX_LENGTH for m in VALID_AUTH_MODES),
          detail=str(sorted(VALID_AUTH_MODES)))

    cleaned = _clean_oauth({
        "destination": " BC_ELIAGROUP_APIHUB_JIRA ",
        "project": "ABC",
        "status": "Open",
        "lookback": "2d",
        "allow_comment": True,
        "client_secret": "should-not-survive",
        "client_id": "nor-this",
    }, AUTH_MODE_DESTINATION, None)
    check("only the destination keys are stored, never a credential",
          cleaned == {
              "destination": "BC_ELIAGROUP_APIHUB_JIRA",
              "project": "ABC",
              "status": "Open",
              "lookback": "2d",
              "allow_comment": True,
          }, detail=str(cleaned))

    off = _clean_oauth({"destination": "X"}, AUTH_MODE_DESTINATION, None)
    check("commenting defaults to off", off["allow_comment"] is False,
          detail=str(off))

    err = _sync_raises(
        lambda: _clean_oauth({"project": "ABC"}, AUTH_MODE_DESTINATION, None))
    check("storage refuses a block with no destination name",
          isinstance(err, ValueError), detail=repr(err))

    payload = McpServerPayload(
        url="builtin:jira",
        auth_mode="destination",
        oauth=OAuthClientPayload(
            destination="BC_ELIAGROUP_APIHUB_JIRA", project="ABC", status="Open"),
    )
    check("a well-formed destination server validates",
          payload.oauth.to_config()["destination"] == "BC_ELIAGROUP_APIHUB_JIRA",
          detail=str(payload.oauth.to_config()))

    err = _sync_raises(lambda: McpServerPayload(
        url="builtin:jira", auth_mode="destination",
        oauth=OAuthClientPayload(project="ABC")))
    check("the API refuses a destination server with no name", err is not None,
          detail=repr(err))

    err = _sync_raises(lambda: McpServerPayload(
        url="builtin:jira", auth_mode="destination",
        oauth=OAuthClientPayload(
            destination="BC_ELIAGROUP_APIHUB_JIRA", client_id="nope")))
    check("the API refuses credentials on a destination server",
          err is not None and "credential" in str(err).lower(), detail=repr(err))

    err = _sync_raises(lambda: McpServerPayload(
        url="builtin:jira", auth_mode="destination",
        oauth=OAuthClientPayload(dcr=True)))
    check("the API refuses DCR on a destination server", err is not None,
          detail=repr(err))

    err = _sync_raises(lambda: OAuthClientPayload(destination="X", lookback="soon"))
    check("a bad lookback is a field error, not a 500", err is not None,
          detail=repr(err))

    stored = _clean_oauth(
        {"destination": "X", "api_base": "/api/2"}, AUTH_MODE_DESTINATION, None)
    check("api_base round-trips through storage",
          stored.get("api_base") == "/api/2", detail=str(stored))

    cfg = OAuthClientPayload(destination="X", api_base="/api/2").to_config()
    check("api_base survives the API payload", cfg.get("api_base") == "/api/2",
          detail=str(cfg))

    blank = OAuthClientPayload(destination="X").to_config()
    check("an unset api_base is not stored at all", "api_base" not in blank,
          detail=str(blank))

    # A URL in this field would aim the destination's credential somewhere the
    # destination never named, so it has to fail at the boundary rather than
    # be normalised away deeper in.
    err = _sync_raises(
        lambda: OAuthClientPayload(destination="X", api_base="https://evil.example"))
    check("a URL in api_base is a field error, not a 500", err is not None,
          detail=repr(err))

    # auth_mode and URL have to agree, in both directions. Neither mismatch
    # fails loudly on its own: one forwards the user's JWT to a host the
    # destination was supposed to cover, the other saves cleanly and then
    # disappears from the orchestrator at the next reload.
    err = _sync_raises(lambda: McpServerPayload(
        url="https://mcp.example.com/mcp", auth_mode="destination",
        oauth=OAuthClientPayload(destination="BC_ELIAGROUP_APIHUB_JIRA")))
    check("the API refuses auth_mode=destination on a real MCP URL",
          err is not None and "built-in" in str(err), detail=repr(err))

    for mode in ("jwt", "none", "oauth2", "app_only"):
        err = _sync_raises(lambda m=mode: McpServerPayload(
            url="builtin:jira", auth_mode=m,
            oauth=OAuthClientPayload(
                destination="BC_ELIAGROUP_APIHUB_JIRA",
                client_id="c", client_secret="s",
                uaa_url="https://uaa.example", token_url="https://uaa.example/t",
                mailbox="svc@example.com")))
        check(f"the API refuses builtin:jira with auth_mode={mode}",
              err is not None and "destination" in str(err), detail=repr(err))

    err = _sync_raises(lambda: McpServerPayload(
        url="builtin:jira", auth_mode="oauth2", oauth=OAuthClientPayload(dcr=True)))
    check("the DCR shortcut does not slip builtin:jira past the check",
          err is not None, detail=repr(err))


async def main() -> None:
    test_jql()
    test_multi_value_filters()
    test_api_base()
    await test_api_base_transport()
    await test_filters()
    await test_answered_filtering()
    await test_transport()
    await test_read_and_write()
    await test_key_confinement()
    await test_identity_failures()
    test_toolset_build()
    test_storage_and_validation()
    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
