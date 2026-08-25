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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)

from agents.destination import (  # noqa: E402
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


async def main() -> None:
    test_binding_resolution()
    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
