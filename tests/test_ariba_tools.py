"""``builtin:ariba``: the read-only Ariba Event Management toolset.

Against a scripted Ariba (``tests/ariba_helpers.py``, synthetic data) behind
the real ``DestinationAuth``: what each tool answers, every refusal code, the
pacing and the 429 backoff, and that no credential, name, title or file name
reaches the log.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from agents import ariba_tools
from agents.ariba_tools import (
    ERROR_CODES,
    AribaError,
    activity_summary,
    ariba_toolset,
)
from tests.ariba_helpers import (
    API_KEY,
    BUYER_FILE,
    EVENT,
    EVENT_TITLE,
    FILE_A,
    PROJECT,
    QUESTION,
    REALM,
    SECRETS,
    SUPPLIER_A,
    SUPPLIER_B,
    SUPPLIER_C,
    SUPPLIER_NAME_A,
    TOKEN,
    USER,
    Exploding,
    FakeAriba,
    FakeResolver,
    FakeTime,
    J,
    S,
    bid_row,
    event,
    identifiers,
    standard,
)

NOW_MS = 1_790_000_100_000


def build(fake: FakeAriba, *, resolver: FakeResolver | None = None,
          oauth: dict[str, Any] | None = None):
    clock = FakeTime()
    toolset = ariba_toolset(
        oauth or {"destination": "ARIBA"}, auth_mode="destination",
        resolver=resolver or FakeResolver(), transport=fake.transport(),
        sleep=clock.sleep, clock=clock.clock, wall=lambda: NOW_MS / 1000)
    return toolset, toolset.ariba_client, clock


async def call(toolset, name: str, *args, **kwargs) -> dict[str, Any]:
    return await toolset.tools[name].function(*args, **kwargs)


def code_of(result: dict[str, Any]) -> str:
    assert set(result) == {"error"}, result
    assert result["error"]["code"] in ERROR_CODES
    return result["error"]["code"]


def test_error_codes_are_the_closed_list_of_the_brief():
    assert ERROR_CODES == frozenset({
        "destination_error", "unauthorized", "forbidden", "bids_sealed", "not_found",
        "throttled", "ariba_error", "unreachable", "not_json", "invalid_id", "invalid_days",
        "unknown_supplier", "unknown_document", "too_large", "unsupported_type", "not_a_pdf",
    })
    with pytest.raises(AssertionError):
        AribaError("something_else", "x")


def test_the_toolset_has_the_three_read_tools():
    toolset, _, _ = build(standard())
    assert sorted(toolset.tools) == ["get_event", "list_documents", "list_events"]


# -- list_events ---------------------------------------------------------------

async def test_list_events_answers_ids_project_and_creation():
    fake = standard()
    toolset, _, _ = build(fake)
    result = await call(toolset, "list_events", days=30)
    assert result == {"events": [
        {"event_id": "Doc100200300", "project_id": "WS100200200",
         "created": "2026-09-21T14:13:20Z"},
        {"event_id": "Doc100200301", "project_id": "WS100200201",
         "created": "2026-09-21T14:13:20Z"},
    ], "more": False}
    (request,) = fake.requests
    assert request.method == "GET"
    assert str(request.url).startswith("https://ariba.example.test/api/sourcing-event/v2/prod/")
    params = request.url.params
    since = NOW_MS - 30 * 86_400_000
    assert params["$filter"] == f"(createDateFrom gt {since} and createDateTo lt {NOW_MS})"
    assert params["$top"] == "100"
    # The destination's properties travel with the request, set by DestinationAuth.
    assert params["realm"] == REALM and params["user"] == USER
    assert request.headers["apiKey"] == API_KEY
    assert request.headers["Authorization"] == TOKEN
    assert request.headers["Accept"] == "application/json"


async def test_list_events_skips_rows_without_a_usable_id_and_says_more_at_100():
    rows = identifiers(100)["payload"]
    rows[0]["internalId"] = "../etc"
    rows[1]["parentProjectId"] = {"x": 1}
    rows[2]["createDate"] = "yesterday"
    fake = FakeAriba().on("/events/identifiers", J(200, {"payload": rows}))
    toolset, _, _ = build(fake)
    result = await call(toolset, "list_events")
    assert len(result["events"]) == 99 and result["more"] is True
    assert result["events"][0]["project_id"] is None
    assert result["events"][1]["created"] is None


@pytest.mark.parametrize("days", [0, 366, -1, "30", True, 1.5, None])
async def test_list_events_refuses_days_outside_1_to_365(days):
    fake = standard()
    toolset, _, _ = build(fake)
    assert code_of(await call(toolset, "list_events", days=days)) == "invalid_days"
    assert fake.requests == []


# -- get_event -----------------------------------------------------------------

async def test_get_event_answers_the_event_and_its_suppliers():
    fake = standard()
    toolset, _, _ = build(fake)
    result = await call(toolset, "get_event", EVENT)
    assert result == {
        "event_id": EVENT, "title": EVENT_TITLE, "state": "PendingSelectionState",
        "type": "RFP",
        "suppliers": [
            {"supplier": SUPPLIER_A, "name": SUPPLIER_NAME_A, "responded": True,
             "bid_status": "Participated", "submitted": "2026-10-20T14:30:00.000+0200"},
            {"supplier": SUPPLIER_B, "name": "Example Supplier Beta", "responded": True,
             "bid_status": "Participated", "submitted": "2026-10-20T14:30:00.000+0200"},
            {"supplier": SUPPLIER_C, "name": "Example Supplier Gamma", "responded": False,
             "bid_status": None, "submitted": None},
        ],
    }
    assert fake.paths() == [f"/events/{EVENT}", f"/events/{EVENT}/supplierInvitations"]
    # No contact data reaches the model.
    assert "contact@example.test" not in repr(result)


async def test_get_event_reads_a_payload_wrapped_event_too():
    fake = standard().on(f"/events/{EVENT}", J(200, {"payload": event()}))
    toolset, _, _ = build(fake)
    result = await call(toolset, "get_event", EVENT)
    assert result["title"] == EVENT_TITLE and len(result["suppliers"]) == 3


async def test_text_from_ariba_is_filtered_and_cut():
    title = "A‮B\x00C" + "x" * 400
    fake = standard().on(f"/events/{EVENT}", J(200, event(title=title)))
    toolset, _, _ = build(fake)
    result = await call(toolset, "get_event", EVENT)
    assert result["title"].startswith("ABC")
    assert "‮" not in result["title"] and "\x00" not in result["title"]
    assert result["title"].removesuffix("…[truncated]") == ("ABC" + "x" * 400)[:300]


@pytest.mark.parametrize("bad", ["", "..", ".", "a/b", "Doc 1", "x" * 65, None, 12, "Doc1?x=1",
                                 "%2e%2e"])
async def test_get_event_refuses_an_id_that_is_not_one(bad):
    fake = standard()
    toolset, _, _ = build(fake)
    assert code_of(await call(toolset, "get_event", bad)) == "invalid_id"
    assert fake.requests == []


# -- list_documents ------------------------------------------------------------

async def test_list_documents_lists_the_suppliers_uploads():
    fake = standard()
    toolset, client, _ = build(fake)
    result = await call(toolset, "list_documents", EVENT, SUPPLIER_A)
    assert result == {"documents": [
        {"document_id": "Att0AlphaOffer1", "question": QUESTION, "file_name": FILE_A,
         "content_type": "application/pdf", "size": 2048, "buyer_document": False},
        {"document_id": "Att0BuyerTerms1", "question": QUESTION, "file_name": BUYER_FILE,
         "content_type": None, "size": 2048, "buyer_document": True},
    ]}
    assert fake.paths() == [f"/events/{EVENT}/supplierInvitations",
                            f"/events/{EVENT}/supplierBids"]
    # The ids of the download stay here; the model never names them.
    assert "1000000001" not in repr(result) and "RQAATTACH" not in repr(result)
    ref = client.document_ref(EVENT, SUPPLIER_A, "Att0AlphaOffer1")
    assert (ref.event_id, ref.supplier, ref.document_id) == (EVENT, SUPPLIER_A, "Att0AlphaOffer1")
    assert (ref.item_id, ref.field_id, ref.alternative_id) == (
        "1000000001", "RQAATTACH", "AL0000000001")
    assert (ref.content_type, ref.size) == ("application/pdf", 2048)
    buyer = client.document_ref(EVENT, SUPPLIER_A, "Att0BuyerTerms1")
    assert (buyer.item_id, buyer.field_id) == ("1000000002", "BUYERDOC")


async def test_list_documents_reuses_the_invitations_get_event_read():
    fake = standard()
    toolset, _, _ = build(fake)
    await call(toolset, "get_event", EVENT)
    await call(toolset, "list_documents", EVENT, SUPPLIER_B)
    assert fake.paths().count(f"/events/{EVENT}/supplierInvitations") == 1


async def test_a_document_of_one_responding_supplier_alone_is_no_buyer_document():
    rows = [bid_row(SUPPLIER_A, "1000000002", [{
        "fieldId": "BUYERDOC", "value": {"attachmentValue": {
            "id": "Att0BuyerTerms1", "fileName": BUYER_FILE, "contentLength": 10}}}])]
    fake = standard().on(f"/events/{EVENT}/supplierBids", J(200, {"payload": rows}))
    toolset, _, _ = build(fake)
    result = await call(toolset, "list_documents", EVENT, SUPPLIER_A)
    assert [d["buyer_document"] for d in result["documents"]] == [False]


async def test_list_documents_skips_attachments_without_usable_ids_and_cuts_names():
    long_name = "n" * 300 + ".pdf"
    rows = [bid_row(SUPPLIER_A, "1000000001", [
        {"fieldId": "RQAATTACH", "value": {"attachmentValue": {
            "id": "Att0Long", "fileName": long_name, "contentLength": True,
            "contentType": "text/html; x=<script>"}}},
        {"fieldId": "bad field", "value": {"attachmentValue": {"id": "Att0BadField"}}},
        {"fieldId": "RQAATTACH", "value": {"attachmentValue": {"id": "../x"}}},
    ])]
    fake = standard().on(f"/events/{EVENT}/supplierBids", J(200, {"payload": rows}))
    toolset, _, _ = build(fake)
    (doc,) = (await call(toolset, "list_documents", EVENT, SUPPLIER_A))["documents"]
    assert doc["document_id"] == "Att0Long"
    assert doc["file_name"].removesuffix("…[truncated]") == long_name[:255]
    assert doc["size"] is None and doc["content_type"] is None


async def test_list_documents_refuses_a_supplier_that_is_not_invited():
    fake = standard()
    toolset, _, _ = build(fake)
    assert code_of(await call(toolset, "list_documents", EVENT, "ACM_99999999")) == (
        "unknown_supplier")
    assert code_of(await call(toolset, "list_documents", EVENT, "a/b")) == "invalid_id"
    assert f"/events/{EVENT}/supplierBids" not in fake.paths()


async def test_sealed_bids_are_bids_sealed_and_no_body_is_read():
    body = Exploding()
    fake = standard().on(f"/events/{EVENT}/supplierBids", S(403, body))
    toolset, _, _ = build(fake)
    result = await call(toolset, "list_documents", EVENT, SUPPLIER_A)
    assert code_of(result) == "bids_sealed"
    assert body.read is False


async def test_document_ref_refuses_what_was_not_listed():
    toolset, client, _ = build(standard())
    with pytest.raises(AribaError) as err:
        client.document_ref(EVENT, SUPPLIER_A, "Att0AlphaOffer1")
    assert err.value.code == "unknown_document"
    await call(toolset, "list_documents", EVENT, SUPPLIER_A)
    with pytest.raises(AribaError) as err:
        client.document_ref(EVENT, SUPPLIER_B, "Att0AlphaOffer1")  # another supplier's
    assert err.value.code == "unknown_document"
    with pytest.raises(AribaError) as err:
        client.document_ref(EVENT, SUPPLIER_A, "../x")
    assert err.value.code == "invalid_id"


# -- the HTTP core: statuses, transport, destination -----------------------------

@pytest.mark.parametrize("status,code", [
    (401, "unauthorized"), (403, "forbidden"), (404, "not_found"), (400, "ariba_error"),
    (406, "ariba_error"), (500, "ariba_error"), (503, "ariba_error"), (302, "ariba_error"),
])
async def test_statuses_map_to_fixed_codes_without_the_body(status, code, caplog):
    caplog.set_level(logging.DEBUG)
    first, body = Exploding(), Exploding()
    # httpx itself drains a 401 before DestinationAuth's one retry; the answer
    # this module gets is never read.
    answers = [S(status, first), S(status, body)] if status == 401 else [S(status, body)]
    fake = FakeAriba().on(f"/events/{EVENT}", *answers)
    toolset, _, _ = build(fake)
    assert code_of(await call(toolset, "get_event", EVENT)) == code
    assert body.read is False
    assert SUPPLIER_NAME_A not in caplog.text
    # A redirect is not followed; a 401 is retried once by the destination auth.
    assert len(fake.requests) == (2 if status == 401 else 1)


async def test_an_answer_that_is_not_json_is_not_json():
    fake = FakeAriba().on(f"/events/{EVENT}", lambda r: httpx.Response(
        200, text="<html>sign in</html>", headers={"content-type": "text/html"}))
    toolset, _, _ = build(fake)
    assert code_of(await call(toolset, "get_event", EVENT)) == "not_json"
    fake.on(f"/events/{EVENT}", lambda r: httpx.Response(
        200, content=b"{broken", headers={"content-type": "application/json"}))
    assert code_of(await call(toolset, "get_event", EVENT)) == "not_json"


async def test_an_answer_over_the_cap_is_too_large(monkeypatch):
    monkeypatch.setattr(ariba_tools, "MAX_RESPONSE_BYTES", 100)
    fake = FakeAriba().on(f"/events/{EVENT}", J(200, event(title="x" * 500)))
    toolset, _, _ = build(fake)
    assert code_of(await call(toolset, "get_event", EVENT)) == "too_large"


async def test_a_transport_failure_is_unreachable(caplog):
    caplog.set_level(logging.WARNING)

    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    fake = FakeAriba().on(f"/events/{EVENT}", boom)
    toolset, _, _ = build(fake)
    assert code_of(await call(toolset, "get_event", EVENT)) == "unreachable"
    assert "ConnectError" in caplog.text and REALM not in caplog.text


async def test_a_destination_that_cannot_be_resolved_is_destination_error(caplog):
    caplog.set_level(logging.WARNING)
    fake = standard()
    toolset, _, _ = build(fake, resolver=FakeResolver(fail=True))
    result = await call(toolset, "get_event", EVENT)
    assert code_of(result) == "destination_error"
    assert fake.requests == [] and "tok-SECRET" not in caplog.text
    assert "tok-SECRET" not in repr(result)


@pytest.mark.parametrize("resolver", [
    FakeResolver(proxy_type="OnPremise"),
    FakeResolver(url="http://ariba.example.test/api/sourcing-event/v2/prod"),
])
async def test_only_an_https_internet_destination_is_used(resolver):
    fake = standard()
    toolset, _, _ = build(fake, resolver=resolver)
    assert code_of(await call(toolset, "get_event", EVENT)) == "destination_error"
    assert fake.requests == []


# -- pacing and throttling -----------------------------------------------------

async def test_calls_are_at_least_one_second_apart():
    fake = standard()
    toolset, _, clock = build(fake)
    await call(toolset, "get_event", EVENT)
    assert clock.sleeps == [pytest.approx(1.0)]
    await call(toolset, "list_events")
    assert clock.sleeps == [pytest.approx(1.0), pytest.approx(1.0)]


async def test_a_429_is_repeated_after_5_15_and_30_seconds():
    fake = FakeAriba().on(f"/events/{EVENT}", J(429), J(429), J(429), J(200, event()))
    fake.on(f"/events/{EVENT}/supplierInvitations", J(200, {"payload": []}))
    toolset, _, clock = build(fake)
    result = await call(toolset, "get_event", EVENT)
    assert result["title"] == EVENT_TITLE
    assert clock.sleeps[:3] == [5.0, 15.0, 30.0]
    assert fake.paths().count(f"/events/{EVENT}") == 4


async def test_a_fourth_429_is_throttled():
    fake = FakeAriba().on(f"/events/{EVENT}", J(429))
    toolset, _, clock = build(fake)
    assert code_of(await call(toolset, "get_event", EVENT)) == "throttled"
    assert len(fake.requests) == 4 and clock.sleeps == [5.0, 15.0, 30.0]


# -- the log -------------------------------------------------------------------

async def test_nothing_secret_or_personal_reaches_the_log(caplog):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="httpx")
    fake = standard()
    toolset, _, _ = build(fake)
    await call(toolset, "list_events")
    await call(toolset, "get_event", EVENT)
    await call(toolset, "list_documents", EVENT, SUPPLIER_A)
    fake.on(f"/events/{EVENT}", J(500))
    await call(toolset, "get_event", EVENT)
    for secret in SECRETS:
        assert secret not in caplog.text, secret
    # httpx's own request line (it carries realm and user) is dropped.
    assert "HTTP Request" not in caplog.text


# -- the factory ---------------------------------------------------------------

def test_the_factory_refuses_another_mode_a_user_context_and_no_destination():
    with pytest.raises(ValueError, match="auth_mode=destination"):
        ariba_toolset({"destination": "ARIBA"}, auth_mode="oauth2", resolver=FakeResolver())
    with pytest.raises(ValueError, match="user_context"):
        ariba_toolset({"destination": "ARIBA", "user_context": True},
                      auth_mode="destination", resolver=FakeResolver())
    with pytest.raises(ValueError, match="destination"):
        ariba_toolset({}, auth_mode="destination")


# -- activity_summary ----------------------------------------------------------

@pytest.mark.parametrize("name", ["list_events", "ariba_list_events", "ariba_0_list_events"])
def test_activity_summary_of_list_events(name):
    result = {"events": [{"event_id": "Doc1"}, {"event_id": "Doc2"}], "more": False}
    assert activity_summary(name, result) == "list_events: 2 events"


def test_activity_summary_lines():
    suppliers = [{"responded": True, "name": SUPPLIER_NAME_A}, {"responded": False}]
    assert activity_summary("get_event", {"title": EVENT_TITLE, "suppliers": suppliers}) == (
        "get_event: 2 suppliers, 1 responded")
    assert activity_summary("ariba_list_documents", {"documents": [{"file_name": FILE_A}]}) == (
        "list_documents: 1 documents")
    assert activity_summary("read_document", {"format": "pdf", "pages": 3, "signed": True}) == (
        "read_document: pdf, 3 pages, signed")
    assert activity_summary("read_document", {"format": "pdf", "pages": 1, "signed": False}) == (
        "read_document: pdf, 1 pages, unsigned")
    assert activity_summary("get_event", {"error": {"code": "bids_sealed", "message": "m"}}) == (
        "error: bids_sealed")
    assert activity_summary("get_event", {"error": {"code": PROJECT, "message": "m"}}) == "error"
    assert activity_summary("get_event", "retry") == "get_event: no summary"
    assert activity_summary("get_page", {"x": 1}) is None
    assert activity_summary("other_list_events", {}) is None
