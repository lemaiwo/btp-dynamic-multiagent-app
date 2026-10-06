"""The OData toolset end to end on an OnPremise destination.

``odata_toolset`` -> ``ODataClient`` -> ``destination_http_client`` ->
``DestinationAuth`` + ``OnPremiseRouter``, all real; only the destination
service, the connectivity tokens, the connectivity proxy and SAP behind it
are doubles. Covered: a read as the technical user and as the signed-in
user goes through the proxy and nothing leaves directly; a write fetches its
CSRF token and is sent through the proxy, with the audit intent recorded
before anything is sent; a 407 of the proxy reaches the model as
``proxy_refused`` (never as an answer of SAP); a write the proxy refused is
sent again once, and reaches SAP at most once.

Run:  python -m pytest tests/test_odata_onpremise_tools.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
for _v in (
    "DESTINATION_CLIENT_ID",
    "DESTINATION_CLIENT_SECRET",
    "DESTINATION_URI",
    "DESTINATION_TOKEN_URL",
    "DESTINATION_UAA_URL",
    "CONNECTIVITY_CLIENT_ID",
    "CONNECTIVITY_CLIENT_SECRET",
    "CONNECTIVITY_TOKEN_URL",
    "CONNECTIVITY_PROXY_HOST",
    "CONNECTIVITY_PROXY_PORT",
    "CONNECTIVITY_PP_MODE",
):
    os.environ.pop(_v, None)

import httpx  # noqa: E402

from tests.odata_helpers import FakeConnectivity, FakeResolver, OnPremiseResolver  # noqa: E402
from tests.test_odata_write_guard import (  # noqa: E402
    ALICE,
    ITEM,
    KEY,
    World,
    _row,
    signed_in,
)

UPDATE = {"operation": "update", "key": KEY, "body": {"RequestedQuantity": "7"}}
PAGE = {"d": {"results": [_row()["d"]], "__count": "1"}}
PROXY_AUTH = "Proxy-Authorization"
TECHNICAL = "Bearer technical-credential"
# What the proxy says in a 407; none of it may reach the model or the audit.
PROXY_BODY = {"error": {"code": "X/1", "message": {"value": "tenant-zone-9"}}}
MODEL_HINT = (
    "the connectivity proxy refused the request before it reached SAP; nothing was "
    "changed. An administrator has to fix the connectivity setup: tell the user instead "
    "of retrying"
)


class OnPrem:
    """A toolset whose two services are OnPremise: ``pr`` as the signed-in
    user (PrincipalPropagation), ``pr-jobs`` as a technical user (Basic)."""

    def __init__(self, *, internet: bool = False) -> None:
        """``internet``: the same toolset (with the connectivity tokens) on
        Internet destinations, so every request takes the direct path."""
        self.tokens = FakeConnectivity()
        self.direct: list[httpx.Request] = []
        self.proxy: list[httpx.Request] = []
        self.statuses: list[int] = []
        # Whether the proxy refuses this request with a 407.
        self.refuse: Callable[[httpx.Request], bool] = lambda request: False

        async def answer(request: httpx.Request) -> httpx.Response:
            if self.refuse(request):
                self.statuses.append(407)
                return httpx.Response(407, json=PROXY_BODY)
            response = await self.world.sap.handler(request)
            self.statuses.append(response.status_code)
            return response

        async def directly(request: httpx.Request) -> httpx.Response:
            self.direct.append(request)
            return await answer(request)

        async def through_proxy(request: httpx.Request) -> httpx.Response:
            # A copy: a retry re-shapes the very same request object.
            self.proxy.append(
                httpx.Request(request.method, request.url, headers=request.headers)
            )
            return await answer(request)

        def resolver(destination: str) -> Any:
            if internet:
                return FakeResolver(name=destination)
            if destination == "S4_ODATA_USER":
                return OnPremiseResolver(name=destination, auth_type="PrincipalPropagation")
            technical = OnPremiseResolver(name=destination)
            # The mock SAP keys a session by this value, and its CSRF token
            # must be a plain token: no space as in a Basic credential.
            technical.headers = {"Authorization": TECHNICAL}
            return technical

        self.world = World(
            connectivity=self.tokens,
            transport=httpx.MockTransport(directly),
            proxy_transport=httpx.MockTransport(through_proxy),
            resolver_factory=resolver,
        )
        self.world.sap.read_answer = PAGE

    async def run(self, **args: Any) -> dict:
        return await self.world.run(**args)

    @property
    def proxy_writes(self) -> list[httpx.Request]:
        return [r for r in self.proxy if r.method != "GET"]


def is_write(request: httpx.Request) -> bool:
    return request.method != "GET"


async def test_a_read_as_the_technical_user_goes_through_the_proxy():
    w = OnPrem()
    out = await w.run(service="pr-jobs", operation="list")
    assert "error" not in out and len(out["items"]) == 1
    assert w.direct == []
    (sent,) = w.proxy
    assert sent.url.scheme == "http" and sent.url.host == "s4.internal"
    assert sent.url.params["sap-client"] == "100"
    assert sent.headers[PROXY_AUTH] == "Bearer APP"
    assert sent.headers["Authorization"] == TECHNICAL
    assert sent.headers["SAP-Connectivity-SCC-Location_ID"] == "LOC1"
    assert w.tokens.calls == [("app",)]


async def test_a_read_as_the_signed_in_user_carries_only_that_user():
    w = OnPrem()
    with signed_in(ALICE):
        out = await w.run(service="pr", operation="list")
    assert "error" not in out and len(out["items"]) == 1
    assert w.direct == []
    (sent,) = w.proxy
    assert sent.headers[PROXY_AUTH] == f"Bearer UX-{ALICE}"
    assert "Authorization" not in sent.headers
    assert w.tokens.calls == [("user", f"jwt-of-{ALICE}", ALICE)]


async def test_a_read_without_a_signed_in_user_is_no_user_and_sends_nothing():
    w = OnPrem()
    out = await w.run(service="pr", operation="list")
    assert out["error"]["code"] == "no_user"
    assert w.proxy == [] and w.direct == [] and w.tokens.calls == []


async def test_a_write_fetches_its_token_and_is_sent_through_the_proxy():
    w = OnPrem()
    with signed_in(ALICE):
        out = await w.run(service="pr", target=ITEM, **UPDATE)
    assert out.get("ok") is True, out
    assert w.direct == []
    fetch, write = w.proxy
    assert fetch.headers["X-CSRF-Token"] == "Fetch" and write.method != "GET"
    for sent in (fetch, write):
        assert sent.headers[PROXY_AUTH] == f"Bearer UX-{ALICE}"
        assert "Authorization" not in sent.headers
        assert sent.url.scheme == "http" and sent.url.params["sap-client"] == "100"
    # The audit intent was recorded before anything was resolved or sent.
    recorder = w.world.recorder
    assert [r.outcome for r in recorder.intents] == ["intent"]
    assert recorder.seen_at_intent[0][0] == 0 and w.statuses[0] == 200
    assert [r.outcome for r in w.world.audits] == ["ok"]


async def test_a_407_reaches_the_model_as_proxy_refused_not_as_sap(caplog):
    caplog.set_level(logging.WARNING)
    w = OnPrem()
    w.refuse = lambda request: True
    out = await w.run(service="pr-jobs", operation="list")
    # For the model: what happened and what to do, nothing an agent cannot act on.
    assert out["error"] == {
        "code": "proxy_refused",
        "message": "HTTP 407 from the connectivity proxy",
        "hint": MODEL_HINT,
    }
    assert "tenant-zone-9" not in str(out) and "X/1" not in str(out)
    assert "CONNECTIVITY_PP_MODE" not in str(out) and "OData service" not in str(out)
    # Refused, the token dropped, tried once more, refused: no third request.
    assert w.statuses == [407, 407] and w.tokens.invalidated == [None]
    assert w.direct == []
    # One line for the operator, with the service.
    said = [r.getMessage() for r in caplog.records if "connectivity proxy" in r.getMessage()]
    assert len(said) == 1 and "'pr-jobs'" in said[0] and "tenant-zone-9" not in caplog.text

    w = OnPrem()
    w.refuse = lambda request: True
    with signed_in(ALICE):
        out = await w.run(service="pr", operation="list")
    assert out["error"]["code"] == "proxy_refused" and out["error"]["hint"] == MODEL_HINT
    assert w.tokens.invalidated == [ALICE]


async def test_a_407_on_the_direct_path_is_an_answer_of_the_service_not_of_the_proxy():
    """Decided by where the request went, not by the status alone."""
    w = OnPrem(internet=True)
    w.refuse = lambda request: True
    out = await w.run(service="pr-jobs", operation="list")
    assert out["error"]["code"] == "sap_error"
    assert out["error"]["message"] == "HTTP 407 from the OData service"
    assert "tenant-zone-9" not in str(out) and "connectivity" not in str(out)
    assert w.proxy == [] and len(w.direct) == 1 and w.tokens.calls == []

    # A write: audited as any other error the service answered.
    w = OnPrem(internet=True)
    w.refuse = is_write
    out = await w.run(service="pr-jobs", target=ITEM, **UPDATE)
    assert out["error"]["code"] == "sap_error" and "tenant-zone-9" not in str(out)
    (result,) = w.world.audits
    assert (result.outcome, result.phase, result.status) == ("sap_error", "write", 407)
    assert w.proxy == []


async def test_an_unknown_pp_mode_refuses_a_write_before_anything_is_sent(monkeypatch):
    monkeypatch.setenv("CONNECTIVITY_PP_MODE", "s3cret-typo")
    w = OnPrem()
    with signed_in(ALICE):
        out = await w.run(service="pr", target=ITEM, **UPDATE)
        read = await w.run(service="pr", operation="list")
    assert out["error"]["code"] == "destination_error" and "nothing was changed" in str(out)
    assert read["error"]["code"] == "destination_error"
    assert "s3cret-typo" not in str(out) + str(read)
    assert w.proxy == [] and w.direct == [] and w.tokens.calls == []
    assert [r.outcome for r in w.world.recorder.intents] == ["intent"]
    (result,) = w.world.audits
    assert (result.outcome, result.phase) == ("refused", "token")


async def test_a_write_the_proxy_refused_once_reaches_sap_exactly_once():
    w = OnPrem()
    refused: list[httpx.Request] = []

    def once(request: httpx.Request) -> bool:
        if is_write(request) and not refused:
            refused.append(request)
            return True
        return False

    w.refuse = once
    out = await w.run(service="pr-jobs", target=ITEM, **UPDATE)
    assert out.get("ok") is True, out
    # Two attempts left this app; the proxy stopped the first, so SAP got one.
    assert len(w.proxy_writes) == 2 and len(w.world.sap.writes) == 1
    assert w.statuses == [200, 407, 204]
    assert [r.outcome for r in w.world.audits] == ["ok"]
    assert w.direct == []


async def test_a_write_the_proxy_keeps_refusing_is_sent_twice_and_never_reaches_sap():
    w = OnPrem()
    w.refuse = is_write
    out = await w.run(service="pr-jobs", target=ITEM, **UPDATE)
    assert out["error"]["code"] == "proxy_refused"
    assert "tenant-zone-9" not in str(out)
    assert len(w.proxy_writes) == 2 and w.world.sap.writes == []
    assert w.statuses == [200, 407, 407]
    # Recorded as an intent, then as refused: nothing was changed in SAP.
    assert [r.outcome for r in w.world.recorder.intents] == ["intent"]
    (result,) = w.world.audits
    assert result.outcome == "refused" and result.status == 407
    # A request did leave this app (to the proxy), which is what the phase says.
    assert result.phase == "write"
