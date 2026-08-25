"""Tests for app-only (client_credentials) auth and the Outlook app-only path.

Two things are being checked, and they fail in different ways.

The auth itself: tokens are cached until they nearly expire, a burst of
concurrent calls fetches one token rather than N, a 401 forces exactly one
refetch, and a refusal from the token endpoint surfaces the provider's own
error text instead of a generic failure.

The Outlook toolset under app-only auth: every Graph path moves from ``/me`` to
``/users/{mailbox}``, a build without a mailbox is refused rather than silently
falling back to ``/me``, and ``send_reply`` exists only when the config opts in.
That last one is the security-relevant assertion in this file — holding
Mail.Send must not be enough to give an agent a send tool.

No network, no mailbox, no browser.

Run:  python tests/test_client_credentials.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ["MCP_URL_ALLOWLIST"] = ""

import httpx  # noqa: E402

from agents.client_credentials import (  # noqa: E402
    ClientCredentialsAuth,
    ClientCredentialsError,
    config_from_oauth,
)
from agents.outlook_tools import OutlookClient, outlook_toolset  # noqa: E402

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


CC_OAUTH = {
    "client_id": "app-id",
    "client_secret": "app-secret",
    "token_url": "https://login.microsoftonline.com/t/oauth2/v2.0/token",
    "scope": "https://graph.microsoft.com/.default",
    "mailbox": "service@example.com",
}


class _TokenServer:
    """Counts token requests and hands out a new token each time."""

    def __init__(self, expires_in: int | None = 3600, status: int = 200,
                 body: dict | None = None) -> None:
        self.calls = 0
        self.expires_in = expires_in
        self.status = status
        self.body = body

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.status != 200:
            return httpx.Response(self.status, json=self.body or {})
        payload: dict = {"access_token": f"token-{self.calls}", "token_type": "Bearer"}
        if self.expires_in is not None:
            payload["expires_in"] = self.expires_in
        return httpx.Response(200, json=payload)


def _auth(server: _TokenServer, key: str = "builtin:outlook") -> ClientCredentialsAuth:
    """Auth whose *token* calls hit the mock server, leaving API calls alone."""
    return ClientCredentialsAuth(
        key, config_from_oauth(CC_OAUTH),
        token_transport=httpx.MockTransport(server.handler),
    )


async def main() -> None:
    print("\n-- config --")

    cfg = config_from_oauth(CC_OAUTH)
    check("token_url is taken as given", cfg.token_url.endswith("/v2.0/token"))
    check("mailbox comes through", cfg.mailbox == "service@example.com")
    check("allow_send defaults off", cfg.allow_send is False)

    derived = config_from_oauth({
        "client_id": "a", "client_secret": "b", "uaa_url": "https://x.example.com/",
    })
    check("uaa_url derives the token endpoint",
          derived.token_url == "https://x.example.com/oauth/token", derived.token_url)

    for bad, why in [
        ({"client_id": "a", "client_secret": "b"}, "no token_url"),
        ({"client_secret": "b", "token_url": "https://x/t"}, "no client_id"),
        ({"client_id": "a", "token_url": "https://x/t"}, "no client_secret"),
    ]:
        try:
            config_from_oauth(bad)
            check(f"rejects config with {why}", False, "accepted")
        except ClientCredentialsError:
            check(f"rejects config with {why}", True)

    print("\n-- token caching --")

    server = _TokenServer()
    auth = _auth(server)

    async def call(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get("https://graph.microsoft.com/v1.0/me")

    seen: list[str] = []

    def graph(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization", ""))
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        auth=auth, transport=httpx.MockTransport(graph)
    ) as c:
        await call(c)
        check("a token is fetched on first use", server.calls == 1, str(server.calls))
        check("the token is attached as a bearer",
              seen[-1] == "Bearer token-1", seen[-1])
        await call(c)
        check("the second call reuses the cached token", server.calls == 1,
              str(server.calls))

        # Ten at once against a cold cache: without the lock each would
        # fetch its own token.
        auth._token = None  # noqa: SLF001 - forcing a cold cache is the point
        before = server.calls
        await asyncio.gather(*[call(c) for _ in range(10)])
        check("concurrent calls share one token fetch",
              server.calls == before + 1, f"{server.calls - before} fetches")

    print("\n-- expiry --")

    server = _TokenServer(expires_in=30)  # below the 60s skew: always stale
    auth = _auth(server, "k")
    async with httpx.AsyncClient(
        auth=auth, transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={}))
    ) as c:
        await c.get("https://graph.microsoft.com/v1.0/me")
        await c.get("https://graph.microsoft.com/v1.0/me")
        # A token expiring inside the skew window is never reused: it could
        # expire in flight.
        check("a token inside the expiry skew is refetched",
              server.calls == 2, str(server.calls))

    print("\n-- rejection --")

    server = _TokenServer(status=401, body={
        "error": "invalid_client",
        "error_description": "AADSTS7000215: Invalid client secret provided.",
    })
    auth = _auth(server, "k")
    async with httpx.AsyncClient(
        auth=auth, transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={}))
    ) as c:
        try:
            await c.get("https://graph.microsoft.com/v1.0/me")
            check("a refused token request raises", False, "no exception")
        except ClientCredentialsError as exc:
            check("a refused token request raises", True)
            check("the provider's own error code survives",
                  "AADSTS7000215" in str(exc), str(exc))

    print("\n-- 401 forces exactly one refetch --")

    server = _TokenServer()
    auth = _auth(server, "k")
    attempts: list[str] = []

    def always_401(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers.get("Authorization", ""))
        return httpx.Response(401, json={"error": "expired"})

    async with httpx.AsyncClient(
        auth=auth, transport=httpx.MockTransport(always_401)
    ) as c:
        await c.get("https://graph.microsoft.com/v1.0/me")
    check("a 401 is retried once, not looped", len(attempts) == 2, str(attempts))
    check("the retry carries a freshly fetched token",
          attempts[0] != attempts[1], str(attempts))

    print("\n-- Graph paths under an app-only token --")

    paths: list[str] = []

    def graph_paths(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/childFolders"):
            return httpx.Response(200, json={"value": [
                {"displayName": "agent", "id": "FOLDER-1"},
            ]})
        return httpx.Response(200, json={"value": [], "id": "M1"})

    async with httpx.AsyncClient(
        base_url="https://graph.microsoft.com",
        transport=httpx.MockTransport(graph_paths),
    ) as c:
        app_only = OutlookClient(c, mailbox="service@example.com")
        await app_only.list_pending("agent")
        check("app-only reads /users/{mailbox}, never /me",
              all("/users/service@example.com/" in p for p in paths),
              str(paths))
        check("no path contains /me/", not any("/me/" in p for p in paths), str(paths))

        paths.clear()
        delegated = OutlookClient(c)
        await delegated.list_pending("agent")
        check("delegated still reads /me", all("/me/" in p for p in paths), str(paths))

        paths.clear()
        await app_only.send_reply("M1", "hello")
        check("send_reply posts to the mailbox's reply endpoint",
              paths == ["/v1.0/users/service@example.com/messages/M1/reply"], str(paths))

    print("\n-- the send tool is opt-in --")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as c:
        default = outlook_toolset(dict(CC_OAUTH), http=c,
                                  auth_mode="app_only")
        names = set(await _tool_names(default))
        check("send_reply is absent by default", "send_reply" not in names, str(names))
        check("the read and draft tools are present",
              {"list_pending", "get_message", "create_reply_draft",
               "move_message"} <= names, str(names))

        opted_in = outlook_toolset({**CC_OAUTH, "allow_send": True}, http=c,
                                   auth_mode="app_only")
        names = set(await _tool_names(opted_in))
        check("send_reply appears only when allow_send is set",
              "send_reply" in names, str(names))

        # The token's own permissions must not be what decides this.
        not_opted = outlook_toolset({**CC_OAUTH, "allow_send": False}, http=c,
                                    auth_mode="app_only")
        check("allow_send=False keeps the tool away",
              "send_reply" not in set(await _tool_names(not_opted)))

    print("\n-- a mailbox is required for app-only --")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as c:
        without = {k: v for k, v in CC_OAUTH.items() if k != "mailbox"}
        try:
            outlook_toolset(without, http=c, auth_mode="app_only")
            check("app-only without a mailbox is refused", False, "accepted")
        except ValueError as exc:
            check("app-only without a mailbox is refused", True)
            check("the error explains why there is no /me fallback",
                  "no user" in str(exc) or "/me" in str(exc), str(exc))

        # Delegated is the one case where an absent mailbox is correct.
        ok = outlook_toolset({}, http=c, auth_mode="oauth2")
        check("delegated without a mailbox is fine", ok is not None)

    print("\n-- lookback parsing --")

    from agents.outlook_tools import parse_lookback

    for value, minutes in [
        ("90m", 90), ("5h", 300), ("2d", 2880), ("1w", 10080),
        ("  3h  ", 180), ("2D", 2880), ("0.5h", 30),
        (48, 2880), ("48", 2880),   # bare numbers are hours
    ]:
        check(f"{value!r} -> {minutes} minutes",
              parse_lookback(value) == minutes, str(parse_lookback(value)))

    for empty in (None, "", "   "):
        check(f"{empty!r} means no window", parse_lookback(empty) is None)

    for bad in ("soon", "5x", "h", "-3h", "0", "0h", "abc2d"):
        try:
            parse_lookback(bad)
            check(f"rejects {bad!r}", False, "accepted")
        except ValueError:
            check(f"rejects {bad!r}", True)

    print("\n-- the window is a ceiling, not a default --")

    windows: list[str] = []

    def capture(request: httpx.Request) -> httpx.Response:
        windows.append(request.url.params.get("$filter") or "")
        return httpx.Response(200, json={"value": []})

    async with httpx.AsyncClient(
        base_url="https://graph.microsoft.com", transport=httpx.MockTransport(capture)
    ) as c:
        unbounded = OutlookClient(c, mailbox="m@e.com")
        await unbounded.list_pending("inbox")
        check("no config and no argument means no filter at all",
              windows[-1] == "", windows[-1])

        await unbounded.list_pending("inbox", lookback_minutes=60)
        check("an argument alone applies a filter",
              windows[-1].startswith("receivedDateTime ge "), windows[-1])

        bounded = OutlookClient(c, mailbox="m@e.com", lookback_minutes=120)
        await bounded.list_pending("inbox")
        check("config alone applies a filter",
              windows[-1].startswith("receivedDateTime ge "), windows[-1])

        # The point of the ceiling: a prompt cannot talk its way past it.
        check("a wider request is clamped to the configured ceiling",
              bounded._window(10080) == 120, str(bounded._window(10080)))
        check("a narrower request is honoured",
              bounded._window(30) == 30, str(bounded._window(30)))
        check("an absent request falls back to the ceiling",
              bounded._window(None) == 120, str(bounded._window(None)))

    print("\n-- the filter and the sort agree --")

    # Graph rejects $filter+$orderby unless the filtered property leads the
    # sort. Both use receivedDateTime; asserting it stops a future reorder
    # from producing a 400 that only shows up against a live mailbox.
    sorts: list[str] = []

    def capture_sort(request: httpx.Request) -> httpx.Response:
        sorts.append(request.url.params.get("$orderby") or "")
        return httpx.Response(200, json={"value": []})

    async with httpx.AsyncClient(
        base_url="https://graph.microsoft.com", transport=httpx.MockTransport(capture_sort)
    ) as c:
        await OutlookClient(c, lookback_minutes=60).list_pending("inbox")
        check("the sort leads with the filtered property",
              sorts[-1].startswith("receivedDateTime"), sorts[-1])

    print("\n-- auth_mode fits its column --")

    from agents.db import AUTH_MODE_MAX_LENGTH, VALID_AUTH_MODES

    # This is the assertion that was missing when "client_credentials" (18
    # chars) shipped into a varchar(16). Every suite here runs on SQLite, which
    # ignores VARCHAR limits entirely, so nothing failed until Postgres rejected
    # the first insert in a deployed environment. Checking the values against
    # the declared width is the only thing that catches it without a real
    # Postgres in the loop.
    for mode in sorted(VALID_AUTH_MODES):
        check(f"{mode!r} fits in varchar({AUTH_MODE_MAX_LENGTH})",
              len(mode) <= AUTH_MODE_MAX_LENGTH,
              f"{len(mode)} chars")

    from agents.db import AgentConfig
    declared = AgentConfig.__table__.c.auth_mode.type.length
    check("the column width matches the constant the check uses",
          declared == AUTH_MODE_MAX_LENGTH, f"column={declared}")

    print("\n-- admin validation --")

    from agents.admin import McpServerPayload

    ok = McpServerPayload(url="builtin:outlook", auth_mode="app_only",
                          oauth={"client_id": "a", "client_secret": "b",
                                 "token_url": "https://login.microsoftonline.com/t/token",
                                 "mailbox": "service@example.com"})
    check("a complete app-only server is accepted",
          ok.auth_mode == "app_only")
    check("allow_send is absent from the stored config unless set",
          "allow_send" not in ok.oauth.to_config(), str(ok.oauth.to_config()))

    on = McpServerPayload(url="builtin:outlook", auth_mode="app_only",
                          oauth={"client_id": "a", "client_secret": "b",
                                 "token_url": "https://x/t",
                                 "mailbox": "m@example.com", "allow_send": True})
    check("allow_send survives into the stored config",
          on.oauth.to_config().get("allow_send") is True)

    for bad, why in [
        ({"client_secret": "b", "token_url": "https://x/t", "mailbox": "m@e.com"},
         "no client_id"),
        ({"client_id": "a", "client_secret": "b", "mailbox": "m@e.com"},
         "no token_url"),
        ({"client_id": "a", "client_secret": "b", "token_url": "https://x/t"},
         "no mailbox on a builtin"),
        ({"dcr": True}, "DCR, which cannot hold application permissions"),
    ]:
        try:
            McpServerPayload(url="builtin:outlook", auth_mode="app_only",
                             oauth=bad)
            check(f"rejects app-only config with {why}", False, "accepted")
        except Exception:
            check(f"rejects app-only config with {why}", True)

    # A real MCP server has no mailbox concept; only builtins require one.
    remote = McpServerPayload(
        url="https://mcp.example.hana.ondemand.com/mcp",
        auth_mode="app_only",
        oauth={"client_id": "a", "client_secret": "b", "token_url": "https://x/t"})
    check("a remote MCP server needs no mailbox", remote is not None)

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


async def _tool_names(toolset) -> list[str]:
    """Tool names registered on a FunctionToolset, across pydantic-ai versions."""
    tools = getattr(toolset, "tools", None)
    if isinstance(tools, dict):
        return list(tools)
    if tools is not None:
        return [getattr(t, "name", str(t)) for t in tools]
    return list(getattr(toolset, "_tools", {}))


if __name__ == "__main__":
    asyncio.run(main())
