"""``builtin:ariba``: the read-only Ariba Event Management toolset.

Against a scripted Ariba (``tests/ariba_helpers.py``, synthetic data) behind
the real ``DestinationAuth``: what each tool answers, every refusal code, the
pacing and the 429 backoff, and that no credential, name, title or file name
reaches the log.
"""

from __future__ import annotations

import dataclasses
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic_ai import BinaryContent, ToolReturn

from agents import ariba_tools
from agents.ariba_tools import (
    ERROR_CODES,
    AribaError,
    activity_summary,
    ariba_toolset,
    opaque_document_id,
)
from tests.ariba_helpers import (
    API_KEY,
    ATT_ALPHA,
    ATT_BETA,
    ATT_BUYER,
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
from tests.ariba_pdf_helpers import (
    PAGE_TEXT,
    SIGNER,
    signed_pdf,
    unsigned_pdf,
    xlsx_like,
)

NOW_MS = 1_790_000_100_000
ALPHA_DOC = opaque_document_id(EVENT, SUPPLIER_A, "1000000001", "RQAATTACH", ATT_ALPHA)
BUYER_DOC = opaque_document_id(EVENT, SUPPLIER_A, "1000000002", "BUYERDOC", ATT_BUYER)
BETA_DOC = opaque_document_id(EVENT, SUPPLIER_B, "1000000001", "RQAATTACH", ATT_BETA)
SHEET_DOC = opaque_document_id(EVENT, SUPPLIER_A, "1000000001", "RQAATTACH", "Att0Sheet")


def build(fake: FakeAriba, *, resolver: FakeResolver | None = None,
          oauth: dict[str, Any] | None = None):
    clock = FakeTime()
    toolset = ariba_toolset(
        oauth or {"destination": "ARIBA"}, auth_mode="destination",
        resolver=resolver or FakeResolver(), transport=fake.transport(),
        sleep=clock.sleep, clock=clock.clock, wall=lambda: NOW_MS / 1000)
    return toolset, toolset.ariba_client, clock


async def call(toolset, name: str, *args, run_id: str | None = "run-1",
               **kwargs) -> dict[str, Any]:
    """A tool's function as pydantic-ai calls it: with a context (only
    ``run_id`` is read) when the tool takes one."""
    tool = toolset.tools[name]
    if tool.takes_ctx:
        args = (SimpleNamespace(run_id=run_id), *args)
    return await tool.function(*args, **kwargs)


def code_of(result: dict[str, Any]) -> str:
    assert set(result) == {"error"}, result
    assert result["error"]["code"] in ERROR_CODES
    return result["error"]["code"]


def test_error_codes_are_the_closed_list_of_the_brief():
    assert ERROR_CODES == frozenset({
        "destination_error", "unauthorized", "forbidden", "bids_sealed", "not_found",
        "throttled", "ariba_error", "unreachable", "not_json", "invalid_id", "invalid_days",
        "unknown_supplier", "unknown_document", "too_large", "unsupported_type", "not_a_pdf",
        "document_budget",
    })
    with pytest.raises(AssertionError):
        AribaError("something_else", "x")


def test_the_toolset_has_the_four_read_tools():
    toolset, _, _ = build(standard())
    assert sorted(toolset.tools) == ["get_event", "list_documents", "list_events",
                                     "read_document"]


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
        {"document_id": ALPHA_DOC, "readable": True, "question": QUESTION,
         "file_name": FILE_A, "content_type": "application/pdf", "size": 2048,
         "buyer_document": False},
        {"document_id": BUYER_DOC, "readable": True, "question": QUESTION,
         "file_name": BUYER_FILE, "content_type": None, "size": 2048,
         "buyer_document": True},
    ]}
    assert fake.paths() == [f"/events/{EVENT}/supplierInvitations",
                            f"/events/{EVENT}/supplierBids"]
    # The ids of the download stay here; the model never names them, nor
    # Ariba's own attachment id.
    for kept in ("1000000001", "RQAATTACH", ATT_ALPHA, ATT_BUYER):
        assert kept not in repr(result)
    ref = client.document_ref(EVENT, SUPPLIER_A, ALPHA_DOC)
    assert (ref.event_id, ref.supplier, ref.document_id) == (EVENT, SUPPLIER_A, ALPHA_DOC)
    assert ref.attachment_id == ATT_ALPHA
    assert (ref.item_id, ref.field_id, ref.alternative_id) == (
        "1000000001", "RQAATTACH", "AL0000000001")
    assert (ref.content_type, ref.size) == ("application/pdf", 2048)
    buyer = client.document_ref(EVENT, SUPPLIER_A, BUYER_DOC)
    assert (buyer.item_id, buyer.field_id, buyer.attachment_id) == (
        "1000000002", "BUYERDOC", ATT_BUYER)


def test_a_document_id_is_short_opaque_and_stable():
    first = opaque_document_id(EVENT, SUPPLIER_A, "1000000001", "RQAATTACH", ATT_ALPHA)
    assert first == ALPHA_DOC and len(first) <= 16
    assert first.isalnum() and first == first.lower()
    others = {opaque_document_id(EVENT, SUPPLIER_A, "1000000001", "RQAATTACH", other)
              for other in (ATT_BETA, ATT_BUYER)}
    others.add(opaque_document_id(EVENT, SUPPLIER_B, "1000000001", "RQAATTACH", ATT_ALPHA))
    others.add(opaque_document_id(EVENT, SUPPLIER_A, "1000000002", "RQAATTACH", ATT_ALPHA))
    assert first not in others and len(others) == 4


async def test_two_uploads_whose_short_ids_collide_get_distinct_ids(monkeypatch):
    real = ariba_tools.opaque_document_id

    def colliding(*parts, length=ariba_tools.DOCUMENT_ID_CHARS):
        # Every id the same at the short length: only a longer one tells them apart.
        return "d0000000000" if length == ariba_tools.DOCUMENT_ID_CHARS else real(
            *parts, length=length)

    monkeypatch.setattr(ariba_tools, "opaque_document_id", colliding)
    fake = standard()
    toolset, client, _ = build(fake)
    first = (await call(toolset, "list_documents", EVENT, SUPPLIER_A))["documents"]
    ids = [d["document_id"] for d in first]
    assert len(set(ids)) == 2 and ids[0] == "d0000000000"
    again = (await call(toolset, "list_documents", EVENT, SUPPLIER_A))["documents"]
    assert [d["document_id"] for d in again] == ids
    assert {client.document_ref(EVENT, SUPPLIER_A, i).attachment_id for i in ids} == {
        ATT_ALPHA, ATT_BUYER}


@pytest.mark.parametrize("att_id,wire", [
    ("Zx7Qa+Lm2/Pq9R=", b"Zx7Qa+Lm2%2FPq9R="),
    ("Ab3Cd+Ef4Gh5Ij6", b"Ab3Cd+Ef4Gh5Ij6"),
    ("Kl7Mn8Op9Qr0St=", b"Kl7Mn8Op9Qr0St="),
    ("Uv1/Wx2/Yz3/Ab4", b"Uv1%2FWx2%2FYz3%2FAb4"),
])
async def test_an_attachment_id_goes_out_as_one_path_segment(att_id, wire):
    rows = [bid_row(SUPPLIER_A, "1000000001", [{
        "fieldId": "RQAATTACH", "value": {"attachmentValue": {
            "id": att_id, "fileName": FILE_A, "contentType": "application/pdf"}}}])]
    path = f"/events/{EVENT}/supplierBids/{SUPPLIER_A}/attachments/{att_id}/content"
    fake = standard().on(f"/events/{EVENT}/supplierBids", J(200, {"payload": rows}))
    fake.on(path, document(unsigned_pdf()))
    toolset, _, _ = build(fake)
    (doc,) = (await call(toolset, "list_documents", EVENT, SUPPLIER_A))["documents"]
    assert doc["readable"] is True
    answer = await call(toolset, "read_document", EVENT, SUPPLIER_A, doc["document_id"])
    assert isinstance(answer, ToolReturn)
    request = fake.requests[-1]
    raw = request.url.raw_path.partition(b"?")[0]
    assert raw == (b"/api/sourcing-event/v2/prod/events/" + EVENT.encode()
                   + b"/supplierBids/" + SUPPLIER_A.encode() + b"/attachments/" + wire
                   + b"/content")
    assert request.url.host == "ariba.example.test"
    assert request.url.params["realm"] == REALM


async def test_an_upload_that_cannot_be_addressed_is_listed_unreadable():
    long_name = "n" * 300 + ".pdf"
    rows = [
        bid_row(SUPPLIER_A, "1000000001", [
            {"fieldId": "RQAATTACH", "value": {"attachmentValue": {
                "id": "Att0Long", "fileName": long_name, "contentLength": True,
                "contentType": "text/html; x=<script>"}}},
            {"fieldId": "bad field", "value": {"attachmentValue": {
                "id": "Att0BadField", "fileName": "a.pdf"}}},
            {"fieldId": "RQAATTACH", "value": {"attachmentValue": {
                "id": "../x", "fileName": "b.pdf"}}},
            {"fieldId": "RQAATTACH", "value": {"attachmentValue": {
                "id": "a" * 129, "fileName": "c.pdf"}}},
            {"value": {"attachmentValue": {"id": ATT_BUYER, "fileName": BUYER_FILE}}},
        ]),
        {**bid_row(SUPPLIER_A, "x", [{"fieldId": "RQAATTACH", "value": {
            "attachmentValue": {"id": "Att0NoItem", "fileName": "d.pdf"}}}]),
         "itemId": None, "item": {"title": QUESTION, "terms": [{
             "fieldId": "RQAATTACH", "value": {"attachmentValue": {
                 "id": "Att0NoItem", "fileName": "d.pdf"}}}]}},
        bid_row(SUPPLIER_B, "1000000002", [{"fieldId": "BUYERDOC", "value": {
            "attachmentValue": {"id": ATT_BUYER, "fileName": BUYER_FILE}}}]),
    ]
    fake = standard().on(f"/events/{EVENT}/supplierBids", J(200, {"payload": rows}))
    toolset, client, _ = build(fake)
    result = await call(toolset, "list_documents", EVENT, SUPPLIER_A)
    docs = result["documents"]
    assert [d["readable"] for d in docs] == [True, False, False, False, False, False]
    assert docs[0]["document_id"] and all(d["document_id"] is None for d in docs[1:])
    assert docs[0]["file_name"].removesuffix("…[truncated]") == long_name[:255]
    assert docs[0]["size"] is None and docs[0]["content_type"] is None
    assert [d["file_name"] for d in docs[1:]] == ["a.pdf", "b.pdf", "c.pdf", BUYER_FILE,
                                                  "d.pdf"]
    # Unreadable, and still compared by Ariba's id: the buyer's file.
    assert [d["buyer_document"] for d in docs] == [False, False, False, False, True, False]
    assert all(d["question"] == QUESTION for d in docs)
    assert activity_summary("list_documents", result) == (
        "list_documents: 6 documents, 5 unreadable")
    assert len(client._documents) == 1


async def test_list_documents_reuses_the_invitations_get_event_read():
    fake = standard()
    toolset, _, _ = build(fake)
    await call(toolset, "get_event", EVENT)
    await call(toolset, "list_documents", EVENT, SUPPLIER_B)
    assert fake.paths().count(f"/events/{EVENT}/supplierInvitations") == 1


async def test_a_document_of_one_responding_supplier_alone_is_no_buyer_document():
    rows = [bid_row(SUPPLIER_A, "1000000002", [{
        "fieldId": "BUYERDOC", "value": {"attachmentValue": {
            "id": ATT_BUYER, "fileName": BUYER_FILE, "contentLength": 10}}}])]
    fake = standard().on(f"/events/{EVENT}/supplierBids", J(200, {"payload": rows}))
    toolset, _, _ = build(fake)
    result = await call(toolset, "list_documents", EVENT, SUPPLIER_A)
    assert [d["buyer_document"] for d in result["documents"]] == [False]


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
        client.document_ref(EVENT, SUPPLIER_A, ALPHA_DOC)
    assert err.value.code == "unknown_document"
    await call(toolset, "list_documents", EVENT, SUPPLIER_A)
    with pytest.raises(AribaError) as err:
        client.document_ref(EVENT, SUPPLIER_B, ALPHA_DOC)  # another supplier's
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
    assert activity_summary("ariba_list_documents", {"documents": [
        {"file_name": FILE_A, "readable": True}]}) == "list_documents: 1 documents"
    assert activity_summary("list_documents", {"documents": [
        {"readable": True}, {"readable": False, "file_name": FILE_A}]}) == (
        "list_documents: 2 documents, 1 unreadable")
    assert activity_summary("read_document", {
        "format": "pdf", "pages": 3, "digital_signature": True,
        "signatures": [{"signer": SUPPLIER_NAME_A}], "file_name": FILE_A}) == (
        "read_document: pdf, 3 pages, signed")
    assert activity_summary("read_document", {
        "format": "pdf", "pages": 1, "digital_signature": False}) == (
        "read_document: pdf, 1 pages, unsigned")
    assert activity_summary("read_document", {"pages": "x"}) == "read_document: ?, ? pages, ?"
    assert activity_summary("get_event", {"error": {"code": "bids_sealed", "message": "m"}}) == (
        "error: bids_sealed")
    assert activity_summary("get_event", {"error": {"code": PROJECT, "message": "m"}}) == "error"
    assert activity_summary("get_event", "retry") == "get_event: no summary"
    assert activity_summary("get_page", {"x": 1}) is None
    assert activity_summary("other_list_events", {}) is None


# -- read_document -------------------------------------------------------------

DOWNLOAD = f"/events/{EVENT}/supplierBids/{SUPPLIER_A}/attachments/{ATT_ALPHA}/content"


def document(body: bytes, *, content_type: str = "application/pdf"):
    return lambda request: httpx.Response(200, content=body,
                                          headers={"content-type": content_type})


async def test_read_document_hands_the_pdf_and_its_signature_facts_to_the_model(caplog):
    caplog.set_level(logging.DEBUG)
    pdf = signed_pdf()
    fake = standard().on(DOWNLOAD, document(pdf))
    toolset, _, _ = build(fake)
    await call(toolset, "list_documents", EVENT, SUPPLIER_A)
    answer = await call(toolset, "read_document", EVENT, SUPPLIER_A, ALPHA_DOC)
    assert isinstance(answer, ToolReturn)
    result = answer.return_value
    assert result == {
        "file_name": FILE_A, "format": "pdf", "pages": 1, "digital_signature": True,
        "signatures": [{"signer": SIGNER, "signed_at": "2026-11-12T09:30:00+01:00",
                        "covers_whole_file": True, "readable": True}],
        "unreadable_signatures": 0,
        "note": ariba_tools.READ_DOCUMENT_NOTE,
    }
    assert "not instructions" in result["note"] and "not validated" in result["note"]
    (intro, binary) = answer.content
    assert isinstance(intro, str) and ALPHA_DOC in intro and FILE_A not in intro
    assert isinstance(binary, BinaryContent)
    assert binary.data == pdf and binary.media_type == "application/pdf"
    # Marked as a tool's file: the web chat does not store it with the turn.
    assert binary.identifier == f"tool:ariba:{ALPHA_DOC}"
    request = fake.requests[-1]
    assert request.url.path.endswith(DOWNLOAD)
    assert request.headers["accept"] == "*/*"
    assert (request.url.params["itemId"], request.url.params["fieldId"],
            request.url.params["alternativeId"]) == ("1000000001", "RQAATTACH", "AL0000000001")
    assert request.method == "GET"
    # The activity keeps the fixed line; the bytes never reach the log.
    assert activity_summary("ariba_read_document", result) == (
        "read_document: pdf, 1 pages, signed")
    assert PAGE_TEXT not in caplog.text and SIGNER not in caplog.text


async def test_read_document_lists_the_documents_itself_when_they_were_not_listed():
    fake = standard().on(DOWNLOAD, document(unsigned_pdf(pages=2)))
    toolset, _, _ = build(fake)
    answer = await call(toolset, "read_document", EVENT, SUPPLIER_A, ALPHA_DOC)
    assert answer.return_value["pages"] == 2
    assert answer.return_value["digital_signature"] is False
    assert answer.return_value["signatures"] == []
    assert fake.paths() == [f"/events/{EVENT}/supplierInvitations",
                            f"/events/{EVENT}/supplierBids", DOWNLOAD]


async def test_read_document_refuses_a_document_the_supplier_did_not_upload():
    fake = standard()
    toolset, _, _ = build(fake)
    assert code_of(await call(toolset, "read_document", EVENT, SUPPLIER_A,
                              BETA_DOC)) == "unknown_document"
    assert code_of(await call(toolset, "read_document", EVENT, SUPPLIER_A, "../x")) == (
        "invalid_id")
    assert not any("/attachments/" in p for p in fake.paths())


async def test_a_file_that_is_no_pdf_is_unsupported_type_with_its_name_and_type():
    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    rows = [bid_row(SUPPLIER_A, "1000000001", [{
        "fieldId": "RQAATTACH", "value": {"attachmentValue": {
            "id": "Att0Sheet", "fileName": "prices.xlsx", "contentType": xlsx}}}])]
    path = f"/events/{EVENT}/supplierBids/{SUPPLIER_A}/attachments/Att0Sheet/content"
    fake = standard().on(f"/events/{EVENT}/supplierBids", J(200, {"payload": rows}))
    fake.on(path, document(xlsx_like(), content_type=xlsx))
    toolset, _, _ = build(fake)
    result = await call(toolset, "read_document", EVENT, SUPPLIER_A, SHEET_DOC)
    assert code_of(result) == "unsupported_type"
    assert result["error"]["file_name"] == "prices.xlsx"
    assert result["error"]["content_type"] == xlsx


async def test_a_declared_pdf_whose_bytes_are_not_one_is_not_a_pdf():
    fake = standard().on(DOWNLOAD, document(b"<html>sign in</html>" * 10))
    toolset, _, _ = build(fake)
    result = await call(toolset, "read_document", EVENT, SUPPLIER_A, ALPHA_DOC)
    assert code_of(result) == "not_a_pdf"
    assert (result["error"]["file_name"], result["error"]["content_type"]) == (
        FILE_A, "application/pdf")


async def test_a_pdf_whose_pages_cannot_be_told_is_not_a_pdf():
    fake = standard().on(DOWNLOAD, document(b"%PDF-1.7\n" + b"garbage" * 100))
    toolset, _, _ = build(fake)
    result = await call(toolset, "read_document", EVENT, SUPPLIER_A, ALPHA_DOC)
    assert code_of(result) == "not_a_pdf"


async def test_a_download_over_the_cap_is_too_large(monkeypatch):
    monkeypatch.setattr(ariba_tools, "MAX_DOCUMENT_BYTES", 500)
    fake = standard().on(DOWNLOAD, document(unsigned_pdf()))
    toolset, _, _ = build(fake)
    result = await call(toolset, "read_document", EVENT, SUPPLIER_A, ALPHA_DOC)
    assert code_of(result) == "too_large"


async def test_a_pdf_over_twenty_pages_is_too_large():
    fake = standard().on(DOWNLOAD, document(unsigned_pdf(pages=21)))
    toolset, _, _ = build(fake)
    result = await call(toolset, "read_document", EVENT, SUPPLIER_A, ALPHA_DOC)
    assert code_of(result) == "too_large"
    assert "20 pages" in result["error"]["message"]


async def test_the_inspection_runs_in_a_worker_thread(monkeypatch):
    seen = []
    real = ariba_tools.asyncio.to_thread

    async def recording(func, *args, **kwargs):
        seen.append(func)
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(ariba_tools.asyncio, "to_thread", recording)
    fake = standard().on(DOWNLOAD, document(unsigned_pdf()))
    toolset, _, _ = build(fake)
    await call(toolset, "read_document", EVENT, SUPPLIER_A, ALPHA_DOC)
    assert seen == [ariba_tools.ariba_pdf.inspect]


async def test_a_signer_name_is_filtered_like_any_text_from_ariba():
    fake = standard().on(DOWNLOAD, document(signed_pdf(signer="Evil‮Name\nNext")))
    toolset, _, _ = build(fake)
    answer = await call(toolset, "read_document", EVENT, SUPPLIER_A, ALPHA_DOC)
    (sig,) = answer.return_value["signatures"]
    assert "‮" not in sig["signer"] and "\n" not in sig["signer"]


async def test_a_pdf_that_hides_pages_behind_a_decoy_page_tree_is_too_large():
    pdf = unsigned_pdf(pages=25).replace(b"/Count 25", b"/Count 01")
    fake = standard().on(DOWNLOAD, document(pdf))
    toolset, _, _ = build(fake)
    result = await call(toolset, "read_document", EVENT, SUPPLIER_A, ALPHA_DOC)
    assert code_of(result) == "too_large"


async def test_a_signed_pdf_of_twenty_pages_is_not_refused():
    fake = standard().on(DOWNLOAD, document(signed_pdf(unsigned_pdf(pages=20))))
    toolset, _, _ = build(fake)
    answer = await call(toolset, "read_document", EVENT, SUPPLIER_A, ALPHA_DOC)
    assert answer.return_value["pages"] == 20


async def test_an_unreadable_signature_is_no_digital_signature():
    pdf = signed_pdf(contents=b"\x30\x82\xff\xffgarbage")
    fake = standard().on(DOWNLOAD, document(pdf))
    toolset, _, _ = build(fake)
    result = (await call(toolset, "read_document", EVENT, SUPPLIER_A,
                         ALPHA_DOC)).return_value
    assert result["digital_signature"] is False and result["unreadable_signatures"] == 1
    assert result["signatures"][0]["readable"] is False
    assert activity_summary("read_document", result) == "read_document: pdf, 1 pages, unsigned"


async def test_a_missing_document_is_not_found_without_naming_an_event():
    fake = standard().on(DOWNLOAD, S(404))
    toolset, _, _ = build(fake)
    result = await call(toolset, "read_document", EVENT, SUPPLIER_A, ALPHA_DOC)
    assert code_of(result) == "not_found"
    assert "document" in result["error"]["message"]
    assert "event" not in result["error"]["message"]
    # An event 404 keeps its own text.
    fake.on(f"/events/{EVENT}", J(404))
    result = await call(toolset, "get_event", EVENT)
    assert "event" in result["error"]["message"]


# -- the per-run document budget -----------------------------------------------

READ = (EVENT, SUPPLIER_A, ALPHA_DOC)


async def test_the_seventh_document_of_a_run_is_refused_and_another_run_has_its_own():
    assert (ariba_tools.MAX_RUN_DOCUMENTS, ariba_tools.MAX_RUN_PAGES,
            ariba_tools.MAX_RUN_BYTES) == (6, 60, 25 * 1024 * 1024)
    fake = standard().on(DOWNLOAD, document(unsigned_pdf()))
    toolset, _, _ = build(fake)
    for _ in range(6):
        assert isinstance(await call(toolset, "read_document", *READ, run_id="r1"),
                          ToolReturn)
    downloads = sum(r.url.path.endswith(DOWNLOAD) for r in fake.requests)
    refused = await call(toolset, "read_document", *READ, run_id="r1")
    assert code_of(refused) == "document_budget"
    assert refused["error"]["message"] == (
        "this run has read as many documents as one run may")
    assert "separate sub-agent tasks" in refused["error"]["hint"]
    # Refused before the download.
    assert sum(r.url.path.endswith(DOWNLOAD) for r in fake.requests) == downloads
    assert isinstance(await call(toolset, "read_document", *READ, run_id="r2"), ToolReturn)
    assert activity_summary("read_document", refused) == "error: document_budget"


async def test_the_pages_of_a_run_are_bounded_by_the_page_upper_bound(monkeypatch):
    monkeypatch.setattr(ariba_tools, "MAX_RUN_PAGES", 3)
    fake = standard().on(DOWNLOAD, document(unsigned_pdf(pages=2)))
    toolset, _, _ = build(fake)
    assert isinstance(await call(toolset, "read_document", *READ), ToolReturn)
    assert code_of(await call(toolset, "read_document", *READ)) == "document_budget"
    # The refused one consumed nothing: one more page still fits.
    monkeypatch.setattr(ariba_tools, "MAX_RUN_PAGES", 4)
    assert isinstance(await call(toolset, "read_document", *READ), ToolReturn)


async def test_the_bytes_of_a_run_are_bounded_after_the_download(monkeypatch):
    pdf = unsigned_pdf()
    monkeypatch.setattr(ariba_tools, "MAX_RUN_BYTES", len(pdf) + len(pdf) // 2)
    fake = standard().on(DOWNLOAD, document(pdf))
    toolset, client, _ = build(fake)
    await call(toolset, "list_documents", EVENT, SUPPLIER_A)
    # No listed size: only the bytes read can tell.
    key = (EVENT, SUPPLIER_A, ALPHA_DOC)
    client._documents[key] = dataclasses.replace(client._documents[key], size=None)
    assert isinstance(await call(toolset, "read_document", *READ), ToolReturn)
    downloads = sum(r.url.path.endswith(DOWNLOAD) for r in fake.requests)
    assert code_of(await call(toolset, "read_document", *READ)) == "document_budget"
    assert sum(r.url.path.endswith(DOWNLOAD) for r in fake.requests) == downloads + 1


async def test_a_listed_size_over_the_bytes_left_is_refused_before_the_download(monkeypatch):
    monkeypatch.setattr(ariba_tools, "MAX_RUN_BYTES", 1000)  # the listed size is 2048
    fake = standard().on(DOWNLOAD, document(unsigned_pdf()))
    toolset, _, _ = build(fake)
    assert code_of(await call(toolset, "read_document", *READ)) == "document_budget"
    assert not any(r.url.path.endswith(DOWNLOAD) for r in fake.requests)


async def test_a_refused_document_does_not_consume_the_budget(monkeypatch):
    monkeypatch.setattr(ariba_tools, "MAX_RUN_DOCUMENTS", 1)
    fake = standard().on(DOWNLOAD, document(xlsx_like(), content_type="application/zip"))
    toolset, _, _ = build(fake)
    assert code_of(await call(toolset, "read_document", *READ)) == "not_a_pdf"
    fake.on(DOWNLOAD, document(unsigned_pdf(pages=25)))
    assert code_of(await call(toolset, "read_document", *READ)) == "too_large"
    fake.on(DOWNLOAD, document(unsigned_pdf()))
    assert isinstance(await call(toolset, "read_document", *READ), ToolReturn)
    assert code_of(await call(toolset, "read_document", *READ)) == "document_budget"


async def test_a_call_without_a_run_id_shares_one_bucket_and_the_table_is_bounded(monkeypatch):
    monkeypatch.setattr(ariba_tools, "MAX_RUN_DOCUMENTS", 1)
    monkeypatch.setattr(ariba_tools, "MAX_RUN_BUDGETS", 2)
    fake = standard().on(DOWNLOAD, document(unsigned_pdf()))
    toolset, client, _ = build(fake)
    assert isinstance(await call(toolset, "read_document", *READ, run_id=None), ToolReturn)
    assert code_of(await call(toolset, "read_document", *READ, run_id=None)
                   ) == "document_budget"
    for run in ("a", "b", "c"):
        await call(toolset, "read_document", *READ, run_id=run)
    assert len(client._budgets) <= 2


async def test_a_long_run_keeps_its_budget_while_it_keeps_reading(monkeypatch):
    # The hour counts from the run's LAST read, not its first.
    monkeypatch.setattr(ariba_tools, "MAX_RUN_DOCUMENTS", 2)
    fake = standard().on(DOWNLOAD, document(unsigned_pdf()))
    toolset, _, clock = build(fake)
    assert isinstance(await call(toolset, "read_document", *READ, run_id="long"), ToolReturn)
    clock.now += 3000
    assert isinstance(await call(toolset, "read_document", *READ, run_id="long"), ToolReturn)
    clock.now += 3000
    assert code_of(await call(toolset, "read_document", *READ, run_id="long")) == (
        "document_budget")
    # A refused call is an access too.
    clock.now += 3000
    assert code_of(await call(toolset, "read_document", *READ, run_id="long")) == (
        "document_budget")


async def test_an_eviction_during_the_download_does_not_reset_the_counters(monkeypatch):
    monkeypatch.setattr(ariba_tools, "MAX_RUN_DOCUMENTS", 2)
    monkeypatch.setattr(ariba_tools, "MAX_RUN_BUDGETS", 1)
    fake = standard()
    toolset, client, _ = build(fake)
    pdf = unsigned_pdf()
    evict = {"on": False}

    def download(request):
        if evict["on"]:
            client._budget("intruder")  # another run's read pushes this run out
        return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})

    fake.on(DOWNLOAD, download)
    assert isinstance(await call(toolset, "read_document", *READ, run_id="r1"), ToolReturn)
    evict["on"] = True
    assert isinstance(await call(toolset, "read_document", *READ, run_id="r1"), ToolReturn)
    evict["on"] = False
    assert code_of(await call(toolset, "read_document", *READ, run_id="r1")) == (
        "document_budget")
