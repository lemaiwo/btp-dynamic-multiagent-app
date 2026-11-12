"""A scripted SAP Ariba Event Management API for the ``builtin:ariba`` tests.

Every value here is synthetic: the keys and the structure follow what the
real API answered (2026-11-12), the names, titles, ids and file names are
made up. ``SECRETS`` are the values that must never reach a log or a tool
answer they do not belong in.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx

from agents.destination import Destination, DestinationError

BASE = "https://ariba.example.test/api/sourcing-event/v2/prod"
PREFIX = "/api/sourcing-event/v2/prod"
TOKEN = "Bearer tok-SECRET"
API_KEY = "key-SECRET"
REALM = "realm-SECRET"
USER = "user-SECRET"
EVENT = "Doc100200300"
PROJECT = "WS100200299"
SUPPLIER_A = "ACM_10000001"
SUPPLIER_B = "ACM_10000002"
SUPPLIER_C = "ACM_10000003"   # invited, no bid
SUPPLIER_NAME_A = "Example Supplier Alpha"
SUPPLIER_NAME_B = "Example Supplier Beta"
EVENT_TITLE = "Synthetic tender for test rolling stock"
QUESTION = "Upload your technical offer"
FILE_A = "alpha-offer.pdf"
FILE_B = "beta-offer.pdf"
BUYER_FILE = "terms-and-conditions.pdf"
SECRETS = (TOKEN, "tok-SECRET", API_KEY, REALM, USER, SUPPLIER_NAME_A, SUPPLIER_NAME_B,
           EVENT_TITLE, QUESTION, FILE_A, FILE_B, BUYER_FILE)


class FakeResolver:
    """Resolves the one synthetic destination; ``fail`` raises instead."""

    name = "ARIBA"

    def __init__(self, url: str = BASE, proxy_type: str = "Internet",
                 fail: bool = False) -> None:
        self.url, self.proxy_type, self.fail = url, proxy_type, fail
        self.resolved = 0
        self.invalidated = 0

    async def resolve(self, *, force: bool = False, user_token=None, principal=None):
        self.resolved += 1
        if self.fail:
            raise DestinationError(f"lookup of {self.url} failed with {TOKEN}")
        return Destination(
            url=self.url, headers={"Authorization": TOKEN, "apiKey": API_KEY},
            expires_at=time.monotonic() + 60, proxy_type=self.proxy_type,
            queries={"realm": REALM, "user": USER, "passwordAdapter": "PasswordAdapter1"},
            static_headers=frozenset({"apikey"}))

    def invalidate(self, principal: str | None = None) -> None:
        self.invalidated += 1


class FakeTime:
    """A monotonic clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Exploding(httpx.AsyncByteStream):
    """A body that records whether anybody read it."""

    def __init__(self) -> None:
        self.read = False

    async def __aiter__(self):
        self.read = True
        yield b"error page with " + SUPPLIER_NAME_A.encode()


def J(status: int = 200, body: Any = None, headers: dict[str, str] | None = None
      ) -> Callable[[httpx.Request], httpx.Response]:
    """A route answer: a fresh JSON response per call."""
    return lambda request: httpx.Response(status, json=body, headers=headers)


def S(status: int, stream: httpx.AsyncByteStream | None = None
      ) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, stream=stream or Exploding())


class FakeAriba:
    """Routes by path below the destination's prefix; a list of answers is
    used one per call (the last one repeats)."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.requests: list[httpx.Request] = []

    def on(self, path: str, *answers: Callable[[httpx.Request], httpx.Response]) -> FakeAriba:
        self.routes[path] = list(answers)
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        assert path.startswith(PREFIX), path
        answers = self.routes.get(path[len(PREFIX):])
        if not answers:
            return httpx.Response(404)
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        return answer(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def paths(self) -> list[str]:
        return [r.url.path[len(PREFIX):] for r in self.requests]


# -- synthetic answers ---------------------------------------------------------

def identifiers(count: int = 2) -> dict[str, Any]:
    rows = [{"internalId": f"Doc{100200300 + n}", "parentProjectId": f"WS{100200200 + n}",
             "createDate": 1_790_000_000_000 + n} for n in range(count)]
    return {"payload": rows}


def event(**over: Any) -> dict[str, Any]:
    body = {"internalId": EVENT, "parentProjectId": PROJECT, "title": EVENT_TITLE,
            "eventStateName": "PendingSelectionState", "eventTypeName": "RFP",
            "createDate": "2026-10-01T09:00:00.000+0200", "items": None}
    body.update(over)
    return body


def invitation(invitation_id: str, name: str, *, bid: bool) -> dict[str, Any]:
    row: dict[str, Any] = {
        "invitationId": invitation_id, "organization": {"name": name, "address": {}},
        "mainContact": {"name": "Contact Person", "emailAddress": "contact@example.test"},
    }
    if bid:
        row.update(hasBid=True, supplierBidStatus="Participated",
                   submissionDate="2026-10-20T14:30:00.000+0200")
    return row


def invitations() -> dict[str, Any]:
    return {"payload": [invitation(SUPPLIER_A, SUPPLIER_NAME_A, bid=True),
                        invitation(SUPPLIER_B, SUPPLIER_NAME_B, bid=True),
                        invitation(SUPPLIER_C, "Example Supplier Gamma", bid=False)]}


def attachment_term(att_id: str, file_name: str, field_id: str = "RQAATTACH",
                    content_type: str | None = "application/pdf", size: int = 2048
                    ) -> dict[str, Any]:
    value: dict[str, Any] = {"id": att_id, "fileName": file_name, "contentLength": size}
    if content_type is not None:
        value["contentType"] = content_type
    return {"title": QUESTION, "fieldId": field_id, "valueTypeName": "Attachment",
            "value": {"attachmentValue": value}}


def bid_row(invitation_id: str, item_id: str, terms: list[dict[str, Any]],
            title: str = QUESTION) -> dict[str, Any]:
    return {"eventId": EVENT, "invitationId": invitation_id, "itemId": item_id,
            "alternativeId": "AL0000000001", "bidStatus": "Accepted",
            "item": {"itemId": item_id, "title": title, "terms": terms}}


def bids() -> dict[str, Any]:
    """Two responding suppliers: one own offer each, plus the buyer's document
    that Ariba repeats in the rows of both."""
    money = {"title": "Price", "fieldId": "PRICE", "valueTypeName": "Money",
             "value": {"moneyValue": {"amount": 1, "currency": "EUR"}}}
    return {"payload": [
        bid_row(SUPPLIER_A, "1000000001", [attachment_term("Att0AlphaOffer1", FILE_A)]),
        bid_row(SUPPLIER_A, "1000000002", [attachment_term(
            "Att0BuyerTerms1", BUYER_FILE, field_id="BUYERDOC", content_type=None)]),
        bid_row(SUPPLIER_A, "1000000003", [money]),
        bid_row(SUPPLIER_B, "1000000001", [attachment_term("Att0BetaOffer01", FILE_B)]),
        bid_row(SUPPLIER_B, "1000000002", [attachment_term(
            "Att0BuyerTerms1", BUYER_FILE, field_id="BUYERDOC", content_type=None)]),
        bid_row(SUPPLIER_B, "1000000003", []),
    ]}


def standard(fake: FakeAriba | None = None) -> FakeAriba:
    """The happy event: identifiers, the event, its invitations and bids."""
    fake = fake or FakeAriba()
    fake.on("/events/identifiers", J(200, identifiers()))
    fake.on(f"/events/{EVENT}", J(200, event()))
    fake.on(f"/events/{EVENT}/supplierInvitations", J(200, invitations()))
    fake.on(f"/events/{EVENT}/supplierBids", J(200, bids()))
    return fake
