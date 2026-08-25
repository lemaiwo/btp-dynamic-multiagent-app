"""Destination service binding resolution and destination lookup.

Two things are checked, and they fail in different ways.

Binding resolution: VCAP_SERVICES on Cloud Foundry, DESTINATION_* variables
locally, VCAP winning when both are present, and a malformed VCAP falling
through to the environment rather than taking the app down.

Destination resolution: the name reaches the right URL, the result is cached
until shortly before its token expires, concurrent callers share one fetch,
and an unknown destination says which name it could not find.

Every HTTP call goes through an injected httpx.MockTransport. Global patching
of httpx.AsyncClient is banned here: it previously leaked from the token
client into the API client and produced a failure that read like a bug in the
application rather than in the test.

No network, no destination service, no browser.

Run:  python tests/test_destination.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)

from agents.destination import (  # noqa: E402
    Destination,
    DestinationError,
    DestinationResolver,
    DestinationServiceConfig,
    config_from_environment,
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


VCAP = json.dumps({
    "destination": [{
        "name": "pydantic-agent-destination",
        "credentials": {
            "clientid": "vcap-client",
            "clientsecret": "vcap-secret",
            "url": "https://elia.authentication.eu10.hana.ondemand.com",
            "uri": "https://destination-configuration.cfapps.eu10.hana.ondemand.com",
        },
    }]
})

ENV = {
    "DESTINATION_CLIENT_ID": "env-client",
    "DESTINATION_CLIENT_SECRET": "env-secret",
    "DESTINATION_TOKEN_URL": "https://env/oauth/token",
    "DESTINATION_URI": "https://env-api",
}


def test_binding_resolution() -> None:
    print("\n-- binding resolution --")

    cfg = config_from_environment({"VCAP_SERVICES": VCAP})
    check("VCAP binding is read in full", cfg == DestinationServiceConfig(
        client_id="vcap-client",
        client_secret="vcap-secret",
        token_url="https://elia.authentication.eu10.hana.ondemand.com/oauth/token",
        api_url="https://destination-configuration.cfapps.eu10.hana.ondemand.com",
    ), detail=repr(cfg))

    both = config_from_environment({"VCAP_SERVICES": VCAP, **ENV})
    check("VCAP wins over stale DESTINATION_* variables",
          both is not None and both.client_id == "vcap-client")

    local = config_from_environment({**ENV, "DESTINATION_URI": "https://env-api/"})
    check("environment fallback works for local development",
          local is not None and local.client_id == "env-client")
    check("api_url loses its trailing slash",
          local is not None and local.api_url == "https://env-api")

    derived = config_from_environment({
        "DESTINATION_CLIENT_ID": "env-client",
        "DESTINATION_CLIENT_SECRET": "env-secret",
        "DESTINATION_UAA_URL": "https://elia.authentication.eu10.hana.ondemand.com/",
        "DESTINATION_URI": "https://env-api",
    })
    check("uaa_url derives the token endpoint",
          derived is not None and derived.token_url
          == "https://elia.authentication.eu10.hana.ondemand.com/oauth/token",
          detail=repr(derived))

    complete = config_from_environment({
        "DESTINATION_CLIENT_ID": "env-client",
        "DESTINATION_CLIENT_SECRET": "env-secret",
        "DESTINATION_UAA_URL": "https://elia.example/oauth/token",
        "DESTINATION_URI": "https://env-api",
    })
    check("an already-complete token URL is not doubled",
          complete is not None
          and complete.token_url == "https://elia.example/oauth/token")

    check("no binding at all yields None", config_from_environment({}) is None)

    partial = config_from_environment({
        "DESTINATION_CLIENT_ID": "env-client",
        "DESTINATION_URI": "https://env-api",
    })
    check("a half-filled environment yields None", partial is None)

    salvaged = config_from_environment({"VCAP_SERVICES": "{not json", **ENV})
    check("malformed VCAP falls through instead of raising",
          salvaged is not None and salvaged.client_id == "env-client")

    empty_creds = json.dumps({"destination": [{"name": "broken", "credentials": {}}]})
    check("a VCAP entry with no credentials is skipped",
          config_from_environment({"VCAP_SERVICES": empty_creds}) is None)

    wrong_shape = config_from_environment({"VCAP_SERVICES": "[]", **ENV})
    check("a wrongly-shaped but valid-JSON VCAP falls through instead of raising",
          wrong_shape is not None and wrong_shape.client_id == "env-client",
          detail=repr(wrong_shape))


CONFIG = DestinationServiceConfig(
    client_id="client",
    client_secret="secret",
    token_url="https://uaa.example/oauth/token",
    api_url="https://dest.example",
)


def _payload(expires_in: str | None = "3600") -> dict:
    token: dict = {
        "type": "Bearer",
        "value": "jira-token",
        "http_header": {"key": "Authorization", "value": "Bearer jira-token"},
    }
    if expires_in is not None:
        token["expires_in"] = expires_in
    return {
        "destinationConfiguration": {
            "Name": "BC_ELIAGROUP_APIHUB_JIRA",
            "URL": "https://jira.example/",
            "Authentication": "OAuth2ClientCredentials",
            "ProxyType": "Internet",
        },
        "authTokens": [token],
    }


class Recorder:
    """A MockTransport that counts what it was asked for."""

    def __init__(self, payload=None, status=200, token_status=200):
        self.payload = _payload() if payload is None else payload
        self.status = status
        self.token_status = token_status
        self.token_calls = 0
        self.destination_calls = 0
        self.last_path = ""
        self.last_auth = ""

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            self.token_calls += 1
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "bad_client"})
            return httpx.Response(200, json={"access_token": "svc", "expires_in": 3600})
        self.destination_calls += 1
        self.last_path = request.url.path
        self.last_auth = request.headers.get("Authorization", "")
        if self.status != 200:
            return httpx.Response(self.status, json={"ErrorMessage": "nope"})
        return httpx.Response(200, json=self.payload)


def _resolver(rec: Recorder, name: str = "BC_ELIAGROUP_APIHUB_JIRA") -> "DestinationResolver":
    return DestinationResolver(name, CONFIG, transport=rec.transport())


async def _raises(coro) -> Exception | None:
    try:
        await coro
    except Exception as exc:  # noqa: BLE001 — the test is what kind
        return exc
    return None


async def test_resolution() -> None:
    print("\n-- destination resolution --")

    rec = Recorder()
    dest = await _resolver(rec).resolve()
    check("URL comes back without its trailing slash",
          dest.url == "https://jira.example", detail=dest.url)
    check("the ready Authorization header is carried through",
          dest.headers == {"Authorization": "Bearer jira-token"},
          detail=repr(dest.headers))
    check("the destination name is in the request path",
          rec.last_path.endswith(
              "/destination-configuration/v1/destinations/"
              "BC_ELIAGROUP_APIHUB_JIRA"),
          detail=rec.last_path)
    check("the service token authenticates the lookup",
          rec.last_auth == "Bearer svc", detail=rec.last_auth)

    rec = Recorder()
    resolver = _resolver(rec)
    await resolver.resolve()
    await resolver.resolve()
    check("a second resolve uses the cache", rec.destination_calls == 1,
          detail=str(rec.destination_calls))

    await resolver.resolve(force=True)
    check("force bypasses the cache", rec.destination_calls == 2,
          detail=str(rec.destination_calls))

    resolver.invalidate()
    await resolver.resolve()
    check("invalidate forces a refetch", rec.destination_calls == 3,
          detail=str(rec.destination_calls))

    # Expiry is asserted as arithmetic on expires_at. A test that waits an
    # hour to prove an hour-long cache expires is not a test.
    rec = Recorder(payload=_payload(expires_in="3600"))
    before = time.monotonic()
    dest = await _resolver(rec).resolve()
    check("the expiry skew is subtracted from the lifetime",
          3538 <= dest.expires_at - before <= 3541,
          detail=str(dest.expires_at - before))

    rec = Recorder(payload=_payload(expires_in="10"))
    before = time.monotonic()
    dest = await _resolver(rec).resolve()
    check("a lifetime shorter than the skew still yields a future deadline",
          dest.expires_at > before, detail=str(dest.expires_at - before))

    rec = Recorder(payload=_payload(expires_in=None))
    before = time.monotonic()
    dest = await _resolver(rec).resolve()
    check("a missing expires_in falls back to the default lifetime",
          238 <= dest.expires_at - before <= 241,
          detail=str(dest.expires_at - before))

    rec = Recorder()
    resolver = _resolver(rec)
    first = await resolver.resolve()
    resolver._cached = Destination(
        url=first.url, headers=first.headers, expires_at=time.monotonic() - 1
    )
    await resolver.resolve()
    check("a stale cache is refetched", rec.destination_calls == 2,
          detail=str(rec.destination_calls))

    rec = Recorder()
    resolver = _resolver(rec)
    await asyncio.gather(*(resolver.resolve() for _ in range(5)))
    check("five concurrent resolves issue one fetch", rec.destination_calls == 1,
          detail=str(rec.destination_calls))

    print("\n-- destination failures --")

    err = await _raises(_resolver(Recorder(status=404)).resolve())
    check("an unknown destination is a DestinationError",
          isinstance(err, DestinationError), detail=repr(err))
    check("the error names the destination it looked for",
          "BC_ELIAGROUP_APIHUB_JIRA" in str(err), detail=str(err))

    err = await _raises(_resolver(Recorder(token_status=401)).resolve())
    check("a token failure is a DestinationError",
          isinstance(err, DestinationError), detail=repr(err))

    err = await _raises(_resolver(
        Recorder(payload={"destinationConfiguration": {"Name": "x"}, "authTokens": []})
    ).resolve())
    check("a destination with no URL is rejected",
          isinstance(err, DestinationError), detail=repr(err))

    broken = _payload()
    broken["authTokens"][0] = {"error": "invalid_client"}
    err = await _raises(_resolver(Recorder(payload=broken)).resolve())
    check("the target's own token error is surfaced",
          isinstance(err, DestinationError) and "invalid_client" in str(err),
          detail=str(err))


async def main() -> None:
    test_binding_resolution()
    await test_resolution()
    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
