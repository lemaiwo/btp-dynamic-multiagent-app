"""Shared doubles for the ``tests/test_odata_*.py`` suites. No network.

* :class:`FakeResolver` stands in for the destination service (the same
  double as in ``tests/test_destination_builtins.py``).
* :func:`service_payload` is one valid catalogue service in the shape
  ``ODataService.to_dict()`` hands to the client and the tools.
* :class:`Sap` + :func:`sap_v2` are the SAP side: an ``httpx.MockTransport``
  behind the real ``destination_http_client``, so a test sees the request
  exactly as SAP would (rewritten URL, destination headers).

Placeholders only: no real host, destination or user name belongs here.
"""

from __future__ import annotations

import copy
import time
from typing import Any, Callable

import httpx

from agents.destination import Destination
from agents.destination_auth import destination_http_client
from agents.odata.models import ODataServicePayload

SAP_URL = "https://s4.internal:44300"
SERVICE_PATH = "/sap/opu/odata/sap/SRV"


class FakeResolver:
    def __init__(
        self, url: str = SAP_URL, headers: dict[str, str] | None = None, name: str = "S4_ODATA_USER"
    ):
        self.name = name
        self.url = url
        self.headers = headers if headers is not None else {"Authorization": "Bearer dest-token"}
        self.calls: list[tuple[str | None, str | None]] = []

    async def resolve(self, *, force: bool = False, user_token=None, principal=None) -> Destination:
        self.calls.append((user_token, principal))
        headers = dict(self.headers)
        if user_token and "Authorization" in headers:
            headers["Authorization"] = f"Bearer user-token-of-{principal}"
        auth_type = "OAuth2UserTokenExchange" if user_token else "OAuth2ClientCredentials"
        return Destination(
            url=self.url,
            headers=headers,
            expires_at=time.monotonic() + 60,
            auth_type=auth_type,
            per_user=bool(user_token),
        )

    def invalidate(self, principal: str | None = None) -> None:
        pass


def _field(
    name: str,
    *,
    selectable: bool = True,
    filterable: bool = False,
    writable: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "name": name,
        "selectable": selectable,
        "filterable": filterable,
        "writable": writable,
        **extra,
    }


_DEFINITION: dict[str, Any] = {
    "entity_sets": [
        {
            "name": "A_PurchaseRequisitionHeader",
            "title": "Purchase requisitions",
            "keys": [{"name": "PurchaseRequisition"}],
            "operations": ["list", "get"],
            "fields": [
                _field("PurchaseRequisition", filterable=True),
                _field("PurReqnDescription"),
                _field("CreatedByUser", selectable=False, personal_data=True),
            ],
            "navigations": [
                {
                    "name": "to_PurchaseReqnItem",
                    "target": "A_PurchaseRequisitionItem",
                    "collection": True,
                },
            ],
        },
        {
            "name": "A_PurchaseRequisitionItem",
            "title": "Purchase requisition items",
            "keys": [{"name": "PurchaseRequisition"}, {"name": "PurchaseRequisitionItem"}],
            "operations": ["list", "get"],
            "fields": [
                _field("PurchaseRequisition", filterable=True),
                _field("PurchaseRequisitionItem", filterable=True),
                _field("Plant", filterable=True),
                _field("Material"),
                _field("CreatedByUser", selectable=False, personal_data=True),
            ],
            "navigations": [
                {
                    "name": "to_PurchaseReqn",
                    "target": "A_PurchaseRequisitionHeader",
                    "collection": False,
                },
            ],
        },
    ],
    "operations": [],
}


def service_payload(**overrides: Any) -> dict[str, Any]:
    """A valid v2 catalogue service as a JSON-ready dict.

    ``overrides`` replace top-level keys (``definition=...`` replaces the
    whole definition). The result went through ``ODataServicePayload``, so a
    helper change that makes it unsaveable fails here, not in a client test.
    """
    data: dict[str, Any] = {
        "name": "purchase-requisitions",
        "title": "Purchase requisitions",
        "purpose": "Read purchase requisitions and their items",
        "destination": "S4_ODATA_USER",
        "user_context": False,
        "odata_version": "v2",
        "service_path": SERVICE_PATH,
        "definition": copy.deepcopy(_DEFINITION),
    }
    data.update(overrides)
    return ODataServicePayload.model_validate(data).model_dump(mode="json")


class Sap:
    """A scripted SAP back end that records every request it got.

    Each answer is used once, in order; the last one repeats. An answer is a
    JSON-able object (sent as 200 ``application/json``), an
    ``httpx.Response``, or a callable taking the request.
    """

    def __init__(self, *answers: Any):
        self.requests: list[httpx.Request] = []
        self._answers = list(answers)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests), len(self._answers)) - 1
        answer = self._answers[index] if index >= 0 else {"d": {"results": []}}
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def sap_v2(
    handler: Sap | Callable[[httpx.Request], httpx.Response],
    *,
    resolver: FakeResolver | None = None,
    user_context: bool = False,
) -> httpx.AsyncClient:
    """The destination-backed client an OData toolset would build, on a mock SAP."""
    respond = handler.handler if isinstance(handler, Sap) else handler
    return destination_http_client(
        resolver or FakeResolver(),
        user_context=user_context,
        server_key="builtin:odata",
        transport=httpx.MockTransport(respond),
    )


def v2_error(status: int, code: str, text: str) -> httpx.Response:
    """The V2 JSON error envelope SAP Gateway sends."""
    return httpx.Response(
        status, json={"error": {"code": code, "message": {"lang": "en", "value": text}}}
    )
