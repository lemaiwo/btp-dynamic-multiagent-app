"""OData V4 writes, actions and functions through ``execute_operation`` (Task V43).

The V2 write guard must hold identically for V4, so this suite is again
mostly about what must NOT happen: a change without both switches, without
an intent row committed first, with a raw ETag in front of the model, twice
after a failure, with a value that becomes a second path segment, or one
that hands back entity data the catalogue never released.

The real toolset on a mock SAP (``httpx.MockTransport`` behind the real
``destination_http_client``), recording through the real
``StoredWriteRecorder`` into this process's test database. The SAP double
looks into the audit table when a request arrives, which is how "the intent
row exists before anything is sent" is asserted.

The payload shapes follow the OData 4.0 JSON format; none was recorded from
a live system. No network.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select

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

from agents.auth import current_jwt, current_principal  # noqa: E402
from agents.db import ODataAuditLog, SessionLocal, init_db  # noqa: E402
from agents.odata.audit import StoredWriteRecorder  # noqa: E402
from agents.odata.calls import uncallable_operations  # noqa: E402
from agents.odata.client import (  # noqa: E402
    CallPlan,
    ODataClient,
    ODataError,
    call_refusal,
)
from agents.odata.models import (  # noqa: E402
    OperationDef,
    ServiceDefinition,
    validate_odata_service,
)
from agents.odata.tools import odata_toolset  # noqa: E402
from agents.odata.v2 import V2Dialect  # noqa: E402
from agents.odata.v4 import V4Dialect  # noqa: E402
from tests.odata_helpers import FakeResolver  # noqa: E402
from tests.test_odata_write_guard import SWITCH_CASES  # noqa: E402

ALICE = "alice@example.com"
ITEM = "A_PurchaseRequisitionItem"
ITEM_TYPE = "SRV.ItemType"
V4_PATH = "/sap/opu/odata4/sap/api_purchasereq/srvd_a2x/sap/purchaserequisition/0001"
COOKIE_NAME = "SAP_SESSIONID_XXX_100"
KEY = {"PurchaseRequisition": "10000001", "PurchaseRequisitionItem": "00010"}
KEYED = f"{V4_PATH}/{ITEM}(PurchaseRequisition='10000001',PurchaseRequisitionItem='00010')"
CONTEXT = f"$metadata#{ITEM}/$entity"
RAW_ETAG = 'W/"20260101000000.0000000"'
NEW_ETAG = 'W/"20260202020202.0000000"'
SECRET_USER = "JDOE-HIDDEN"
BODY_VALUE = "BODY-VALUE-7d2"
CTX = SimpleNamespace(run_id="run-1")
ALL_OPS = ["list", "get", "create", "update", "delete"]
GUID = "0050568D-393C-1EDB-9B8C-2F3C4D5E6F70"
# Every type the V4 dialect writes: (EDM type, a value, its JSON form, its URI literal).
TYPED = {
    "Text": ("Edm.String", "O'Neil & co", "O'Neil & co", "'O''Neil & co'"),
    "Flag": ("Edm.Boolean", True, True, "true"),
    "Small": ("Edm.Byte", 7, 7, "7"),
    "Signed": ("Edm.SByte", -7, -7, "-7"),
    "Short": ("Edm.Int16", "300", 300, "300"),
    "Count": ("Edm.Int32", 12, 12, "12"),
    "Big": ("Edm.Int64", "9000000000", 9000000000, "9000000000"),
    "Amount": ("Edm.Decimal", "12.50", 12.5, "12.50"),
    "Ratio": ("Edm.Double", 1.5, 1.5, "1.5"),
    "Single": ("Edm.Single", "2.5", 2.5, "2.5"),
    "On": ("Edm.Date", "2026-10-05", "2026-10-05", "2026-10-05"),
    "At": ("Edm.DateTimeOffset", "2026-10-05T12:00:00Z", "2026-10-05T12:00:00Z",
           "2026-10-05T12:00:00Z"),
    "Clock": ("Edm.TimeOfDay", "12:30:00", "12:30:00", "12:30:00"),
    "Lead": ("Edm.Duration", "P1DT2H", "P1DT2H", "duration'P1DT2H'"),
    "Id": ("Edm.Guid", GUID, GUID, GUID),
}


def _action(name: str, **kw: Any) -> dict[str, Any]:
    return {"name": name, "qualified_name": f"SRV.{name}", "kind": "action",
            "http_method": "POST", "enabled": True, **kw}


def _function(name: str, **kw: Any) -> dict[str, Any]:
    return {"name": name, "qualified_name": f"SRV.{name}", "kind": "function",
            "http_method": "GET", "enabled": True, **kw}


def _entity_set(name: str, operations: list[str], entity_type: str = ITEM_TYPE) -> dict[str, Any]:
    typed = [
        {"name": field, "type": edm, "selectable": True, "writable": True}
        for field, (edm, _, _, _) in TYPED.items()
    ]
    return {
        "name": name,
        "entity_type": entity_type,
        "keys": [{"name": "PurchaseRequisition"}, {"name": "PurchaseRequisitionItem"}],
        "operations": operations,
        "fields": [
            {"name": "PurchaseRequisition", "selectable": True, "writable": True},
            {"name": "PurchaseRequisitionItem", "selectable": True, "writable": True},
            {"name": "PurReqnReleaseStatus", "selectable": True},
            {"name": "Plant", "selectable": True, "writable": True},
            {"name": "RequestedQuantity", "type": "Edm.Decimal", "selectable": True,
             "writable": True},
            {"name": "Blob", "type": "Edm.Binary", "writable": True},
            {"name": "Address", "type": "SRV.Address", "writable": True},
            {"name": "CreatedByUser", "personal_data": True},
            *typed,
        ],
    }


def _operations(enabled: bool = True) -> list[dict[str, Any]]:
    operations = [
        _action("Release", parameters=[
            {"name": "ReleaseCode"},
            {"name": "Quantity", "type": "Edm.Decimal", "required": False},
            {"name": "Count", "type": "Edm.Int64", "required": False},
            {"name": "Note", "required": False},
        ]),
        _action("Approve", bound_to=ITEM, parameters=[{"name": "Comment", "required": False}]),
        # Stored as "only reads", but an action: a write all the same.
        _action("MarkedReading", changes_data=False),
        _action("Copy", bound_to=ITEM, returns={"entity_set": ITEM, "collection": False}),
        _function("CountOpen", changes_data=False, parameters=[
            {"name": "Plant"}, {"name": "Limit", "type": "Edm.Int32", "required": False},
        ]),
        _function("ItemStatus", changes_data=False, bound_to=ITEM, parameters=[
            {"name": "AsOf", "type": "Edm.Date", "required": False},
        ]),
        # `changes_data` not said: the model's default, a write although a GET.
        _function("Recalculate"),
        _function("OpenItems", changes_data=False,
                  returns={"entity_set": ITEM, "collection": True}),
        _function("Typed", changes_data=False, parameters=[
            {"name": name, "type": edm, "required": False}
            for name, (edm, _, _, _) in TYPED.items()
        ]),
        _function("HiddenItem", changes_data=False, bound_to="A_Hidden"),
        _action("Off", enabled=False),
        # Enabled, but never callable as the catalogue declares them.
        _action("NeedsComplex", parameters=[{"name": "Address", "type": "SRV.Address"}]),
        _function("BoundToKeyless", changes_data=False, bound_to="A_Keyless"),
    ]
    if not enabled:
        operations = [{**op, "enabled": False} for op in operations]
    return operations


UNCALLABLE = {"NeedsComplex": "parameter_type", "BoundToKeyless": "bound_set_without_key"}


def catalogue(item_ops: list[str] | None = None, enabled: bool = True) -> dict[str, dict]:
    """``pr4`` (as the signed-in user) and ``pr4-jobs`` (technical), both V4."""
    definition = {
        "entity_sets": [
            _entity_set(ITEM, ALL_OPS if item_ops is None else item_ops),
            _entity_set("A_Hidden", []),  # in the catalogue, readable by nobody
            _entity_set("A_Other", ["list", "get"], entity_type="SRV.OtherType"),
            {**_entity_set("A_Keyless", ["list"]), "keys": []},
        ],
        "operations": _operations(enabled),
    }
    out: dict[str, dict] = {}
    for name, destination, as_user in (
        ("pr4", "S4_ODATA_USER", True),
        ("pr4-jobs", "S4_ODATA_TECH", False),
    ):
        clean = validate_odata_service(
            {
                "name": name,
                "title": "Purchase requisitions (V4)",
                "purpose": "Purchase requisitions and their items",
                "destination": destination,
                "user_context": as_user,
                "odata_version": "v4",
                "service_path": V4_PATH,
                "definition": definition,
            }
        )
        out[name] = {**clean, "id": 1, "counts": {}, "has_write": True, "used_by": []}
    return out


def entity(
    key: dict[str, str] = KEY, context: str | None = CONTEXT, etag: str | None = RAW_ETAG,
    **extra: Any,
) -> dict[str, Any]:
    """One item as a V4 service returns it, with a field the catalogue keeps back."""
    control: dict[str, Any] = {}
    if context is not None:
        control["@odata.context"] = context
    if etag is not None:
        control["@odata.etag"] = etag
    return {
        **control,
        **key,
        "PurReqnReleaseStatus": "05",
        "Plant": "1000",
        "RequestedQuantity": 5,
        "CreatedByUser": SECRET_USER,
        **extra,
    }


def v4_error(status: int, code: str, text: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": text}})


async def all_rows() -> list[ODataAuditLog]:
    async with SessionLocal() as s:
        return list((await s.execute(select(ODataAuditLog).order_by(ODataAuditLog.id))).scalars())


class Sap:
    """A V4 service: one CSRF session per caller; notes the audit table per request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.rows_seen: dict[int, list[tuple[str, str, str]]] = {}
        self.issued: dict[str, tuple[str, str]] = {}
        self.answer: Any = None  # a Response, a JSON body, or a callable(request)
        self.read_item: Any = None

    @staticmethod
    def _is_fetch(request: httpx.Request) -> bool:
        return request.headers.get("X-CSRF-Token") == "Fetch"

    @staticmethod
    def _is_plain_read(request: httpx.Request) -> bool:
        last = request.url.path.rsplit("/", 1)[-1]
        return (
            request.method == "GET"
            and last.startswith(ITEM + "(")
            and "X-CSRF-Token" not in request.headers
        )

    @property
    def fetches(self) -> list[httpx.Request]:
        return [r for r in self.requests if self._is_fetch(r)]

    @property
    def calls(self) -> list[httpx.Request]:
        """Everything but the CSRF token requests and the plain entity reads."""
        return [r for r in self.requests if not self._is_fetch(r) and not self._is_plain_read(r)]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.rows_seen[len(self.requests) - 1] = [
            (r.operation, r.target, r.outcome) for r in await all_rows()
        ]
        caller = request.headers.get("Authorization", "").removeprefix("Bearer ")
        await asyncio.sleep(0)
        if self._is_fetch(request):
            token, cookie = f"T-{caller}-{len(self.fetches)}", f"S-{caller}"
            self.issued[caller] = (token, cookie)
            return httpx.Response(
                200,
                headers=[
                    ("X-CSRF-Token", token),
                    ("Set-Cookie", f"{COOKIE_NAME}={cookie}; path=/; secure; HttpOnly"),
                ],
                json={"@odata.context": "$metadata", "value": [{"name": ITEM, "url": ITEM}]},
            )
        if self._is_plain_read(request):
            return httpx.Response(200, json=self.read_item or entity())
        if "X-CSRF-Token" in request.headers:
            token, cookie = request.headers["X-CSRF-Token"], request.headers.get("Cookie", "")
            expected = self.issued.get(caller)
            if not (expected and token == expected[0] and f"{COOKIE_NAME}={expected[1]}" in cookie):
                return httpx.Response(403, headers={"x-csrf-token": "Required"}, text="CSRF")
        elif request.method != "GET":
            return httpx.Response(403, headers={"x-csrf-token": "Required"}, text="CSRF")
        answer = self.answer
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, httpx.Response):
            return answer
        if answer is not None:
            return httpx.Response(200, json=answer)
        if request.method == "POST" and request.url.path == f"{V4_PATH}/{ITEM}":
            return httpx.Response(201, json=entity(etag=NEW_ETAG))
        return httpx.Response(204)


class World:
    def __init__(
        self,
        oauth: dict[str, Any] | None = None,
        services: dict[str, dict] | None = None,
        recorder: Any = ...,
    ) -> None:
        self.sap = Sap()
        self.built: list[str] = []
        self.resolved: list[str] = []
        world = self

        class Recording(FakeResolver):
            async def resolve(self, *, force=False, user_token=None, principal=None):
                world.resolved.append(self.name)
                return await super().resolve(
                    force=force, user_token=user_token, principal=principal
                )

        def factory(destination: str) -> FakeResolver:
            world.built.append(destination)
            return Recording(name=destination)

        self.toolset = odata_toolset(
            oauth if oauth is not None else {"services": ["pr4", "pr4-jobs"], "allow_write": True},
            auth_mode="destination",
            services=services or catalogue(),
            agent_name="buyer",
            transport=httpx.MockTransport(self.sap.handler),
            resolver_factory=factory,
            recorder=StoredWriteRecorder() if recorder is ... else recorder,
        )

    async def run(self, **args: Any) -> dict:
        full = {"service": "pr4", "target": ITEM, **args}
        return await self.toolset.tools["execute_operation"].function(CTX, **full)

    async def call(self, target: str, **args: Any) -> dict:
        return await self.run(target=target, operation="call", **args)

    async def search(self, **args: Any) -> dict:
        return await self.toolset.tools["search_operations"].function(**args)

    async def untouched(self) -> bool:
        """Nothing resolved, built or sent, and nothing recorded."""
        return (
            self.sap.requests == []
            and self.resolved == []
            and self.built == []
            and await all_rows() == []
        )


READ_ONLY = {"services": ["pr4", "pr4-jobs"]}
UPDATE = {"operation": "update", "key": KEY, "body": {"RequestedQuantity": "7.50"}}
CREATE = {"operation": "create", "body": {"PurchaseRequisition": "10000001", "Plant": "1000"}}
DELETE = {"operation": "delete", "key": KEY}


def code(out: dict) -> str | None:
    return out.get("error", {}).get("code")


class signed_in:
    def __init__(self, principal: str) -> None:
        self.principal, self.token = principal, f"jwt-of-{principal}"

    def __enter__(self) -> None:
        self._jwt = current_jwt.set(self.token)
        self._principal = current_principal.set(self.principal)

    def __exit__(self, *exc: object) -> None:
        current_principal.reset(self._principal)
        current_jwt.reset(self._jwt)


@pytest.fixture
def alice():
    with signed_in(ALICE):
        yield


@pytest.fixture(autouse=True)
async def _empty_audit_table():
    """Another suite may own the engine: never assume a fresh file."""
    await init_db()
    async with SessionLocal() as s:
        await s.execute(delete(ODataAuditLog))
        await s.commit()
    yield


def _definition() -> ServiceDefinition:
    return ServiceDefinition.model_validate(catalogue()["pr4"]["definition"])


# -- entity writes ---------------------------------------------------------------


async def test_an_update_is_a_patch_with_if_match_and_a_json_body(alice):
    w = World()
    got = await w.run(operation="get", key=KEY)
    handle = got["etag"]
    assert RAW_ETAG not in json.dumps(got) and "@odata.etag" not in json.dumps(got)
    w.sap.answer = httpx.Response(204, headers={"ETag": NEW_ETAG})
    out = await w.run(
        operation="update", key=KEY, etag=handle,
        body={"RequestedQuantity": "7.50", "Count": 3, "Plant": None},
    )
    assert (out["ok"], out["status"]) == (True, 204)
    assert out["etag"] not in (handle, RAW_ETAG, NEW_ETAG)  # a new opaque handle
    assert NEW_ETAG not in json.dumps(out)
    (write,) = w.sap.calls
    assert write.method == "PATCH" and write.url.path == KEYED
    assert "X-HTTP-Method" not in write.headers
    assert write.headers["If-Match"] == RAW_ETAG
    assert write.headers["Content-Type"] == "application/json"
    assert write.headers["OData-MaxVersion"] == "4.0"
    assert json.loads(write.content) == {"RequestedQuantity": 7.5, "Count": 3, "Plant": None}
    # Numbers stay numbers: not the text a V2 body carries.
    assert b'"RequestedQuantity": 7.5' in write.content
    # The CSRF token came from a fetch on the service root, as in V2.
    (fetch,) = w.sap.fetches
    assert fetch.method == "GET" and fetch.url.path == V4_PATH + "/"
    assert write.headers["X-CSRF-Token"] == w.sap.issued["user-token-of-" + ALICE][0]


async def test_an_update_answered_with_the_entity_yields_its_new_etag_as_a_handle(alice):
    w = World()
    w.sap.answer = entity(etag=NEW_ETAG)  # 200 with the changed entity, no ETag header
    out = await w.run(**UPDATE)
    assert (out["ok"], out["status"]) == (True, 200) and out["etag"]
    assert NEW_ETAG not in json.dumps(out) and SECRET_USER not in json.dumps(out)
    w.sap.answer = None
    assert (await w.run(operation="delete", key=KEY, etag=out["etag"])) == {
        "ok": True, "status": 204,
    }
    assert w.sap.calls[-1].headers["If-Match"] == NEW_ETAG


async def test_a_create_is_a_post_that_returns_the_item_and_an_etag_handle(alice):
    w = World()
    out = await w.run(**CREATE)
    (write,) = w.sap.calls
    assert write.method == "POST" and write.url.path == f"{V4_PATH}/{ITEM}"
    assert json.loads(write.content) == {"PurchaseRequisition": "10000001", "Plant": "1000"}
    assert out["status"] == 201 and out["item"]["Plant"] == "1000"
    assert out["item"]["RequestedQuantity"] == 5
    assert "CreatedByUser" not in out["item"] and not any("@" in k for k in out["item"])
    # The ETag of `@odata.etag`, as a handle: the raw value is nowhere.
    assert out["etag"] and NEW_ETAG not in json.dumps(out)
    w.sap.answer = None
    again = await w.run(operation="delete", key=KEY, etag=out["etag"])
    assert again == {"ok": True, "status": 204}
    assert w.sap.calls[-1].headers["If-Match"] == NEW_ETAG


async def test_a_delete_is_a_delete_with_if_match(alice):
    w = World()
    handle = (await w.run(operation="get", key=KEY))["etag"]
    out = await w.run(operation="delete", key=KEY, etag=handle)
    assert out == {"ok": True, "status": 204}
    (write,) = w.sap.calls
    assert write.method == "DELETE" and write.url.path == KEYED
    assert write.headers["If-Match"] == RAW_ETAG and write.content == b""
    # Without a handle nothing is sent as If-Match: SAP decides.
    assert (await w.run(**DELETE)) == {"ok": True, "status": 204}
    assert "If-Match" not in w.sap.calls[-1].headers


async def test_a_raw_etag_from_the_model_is_never_forwarded(alice):
    w = World()
    for etag in (RAW_ETAG, "*", 'W/"made-up"'):
        out = await w.run(**UPDATE, etag=etag)
        assert code(out) == "invalid_etag", out
        out = await w.call("Approve", key=KEY, etag=etag)
        assert code(out) == "invalid_etag", out
    assert w.sap.calls == [] and await all_rows() == []


async def test_a_stale_csrf_token_is_renewed_and_the_write_repeated_once(alice):
    w = World()
    assert (await w.run(**DELETE)) == {"ok": True, "status": 204}
    w.sap.issued.clear()  # SAP ended the session
    assert (await w.run(**DELETE)) == {"ok": True, "status": 204}
    assert len(w.sap.fetches) == 2 and [r.method for r in w.sap.calls] == ["DELETE"] * 3
    # Refused twice: the answer is the refusal, after exactly two sends.
    w = World()
    w.sap.answer = lambda request: httpx.Response(403, headers={"x-csrf-token": "Required"})
    out = await w.call("Release", params={"ReleaseCode": "01"})
    assert code(out) == "sap_error" and "nothing was changed" in out["error"]["hint"]
    assert len(w.sap.calls) == 2 and len(w.sap.fetches) == 2


@pytest.mark.parametrize("status, expected, hint", [
    (412, "sap_error", "read the entity again and retry with its etag"),
    (428, "etag_required", "read the entity with 'get' first"),
])
async def test_412_and_428_carry_the_hints_of_v2(alice, status, expected, hint):
    for call in (UPDATE, DELETE, {"target": "Approve", "operation": "call", "key": KEY}):
        w = World()
        w.sap.answer = v4_error(status, "SY/530", "Precondition")
        out = await w.run(**call)
        assert code(out) == expected and hint in out["error"]["hint"], out
        assert len(w.sap.calls) == 1  # never repeated
    w = World()
    w.sap.answer = v4_error(428, "SY/530", "Precondition")
    out = await w.call("Release", params={"ReleaseCode": "01"})
    assert code(out) == "etag_required" and "not bound to an entity" in out["error"]["hint"]


@pytest.mark.parametrize("answer", [
    lambda: httpx.Response(200, headers={"content-type": "text/html"}, text="<html>Sign in"),
    lambda: httpx.Response(200, json={"error": {"code": "X", "message": "no"}}),
    lambda: httpx.Response(200, json=[1, 2]),
    lambda: httpx.Response(202),
])
async def test_success_is_recognised_positively(alice, answer):
    for call in (UPDATE, {"target": "Approve", "operation": "call", "key": KEY}):
        w = World()
        w.sap.answer = answer()
        out = await w.run(**call)
        assert code(out) == "write_outcome_unknown", out
        assert len(w.sap.calls) == 1
        row = (await all_rows())[-1]
        assert (row.outcome, row.phase) == ("unknown", "write")


async def test_a_v4_error_envelope_reaches_the_model_as_code_and_text_only(alice):
    w = World()
    w.sap.answer = httpx.Response(400, json={"error": {
        "code": "MM/123", "message": "Quantity not allowed",
        "details": [{"message": "host s4.internal"}], "innererror": {"user": SECRET_USER},
    }})
    out = await w.run(**UPDATE)
    assert out["error"] == {"code": "sap_error", "message": "MM/123: Quantity not allowed"}


# -- the body --------------------------------------------------------------------


def test_update_request_is_a_plain_patch():
    assert V4Dialect().update_request(KEYED, {"Plant": "1"}) == ("PATCH", {})
    assert V4Dialect().supports_write is True and V4Dialect().supports_call is True


def test_every_known_type_is_encoded_and_numbers_stay_numbers():
    item = _definition().entity_set(ITEM)
    body = {name: value for name, (_, value, _, _) in TYPED.items()}
    encoded = V4Dialect().encode_body(item, body)
    assert encoded == {name: sent for name, (_, _, sent, _) in TYPED.items()}
    for name, (_, _, sent, _) in TYPED.items():
        assert type(encoded[name]) is type(sent), name
    # A number or its text: the same JSON number either way.
    for edm, given, sent in (
        ("Edm.Decimal", 12, 12), ("Edm.Decimal", 12.5, 12.5), ("Edm.Decimal", "12", 12),
        ("Edm.Decimal", "-0.125", -0.125), ("Edm.Int64", 9000000000, 9000000000),
        ("Edm.Int64", "-9223372036854775808", -(2**63)), ("Edm.Double", "3", 3.0),
        ("Edm.Double", 3, 3.0), ("Edm.String", 4500000001, "4500000001"),
        ("Edm.Boolean", "false", False),
    ):
        out = V4Dialect()._json_value(edm, given)
        assert out == sent and type(out) is type(sent), (edm, given)
    assert all(V4Dialect()._json_value(edm, None) is None for edm, _, _, _ in TYPED.values())


@pytest.mark.parametrize("name, value", [
    ("Amount", "abc"), ("Amount", "1e5"), ("Amount", True), ("Amount", float("nan")),
    # More digits than a JSON number carries exactly: refused, never rounded.
    ("Amount", "0.12345678901234567890123"), ("Amount", "12345678901234567.25"),
    ("Big", 2**63), ("Big", "1.5"), ("Big", 1.5), ("Count", 2**31), ("Small", -1),
    ("Flag", "yes"), ("Flag", 1), ("Text", 1.5), ("Text", "a\x00b"),
    ("On", "2026-02-30"), ("On", "05.10.2026"), ("At", "2026-10-05T12:00:00"),
    ("Clock", "25:00:00"), ("Lead", "P"), ("Id", "not-a-guid"), ("Ratio", "x"),
    ("Ratio", 10**400), pytest.param("Amount", 10**5000, id="Amount-huge"),
    ("Blob", "AAEC"), ("Address", "x"),
])
def test_a_value_without_the_form_of_its_type_is_refused_by_name(name, value):
    item = _definition().entity_set(ITEM)
    with pytest.raises(ODataError) as refused:
        V4Dialect().encode_body(item, {name: value})
    assert refused.value.code == "invalid_argument" and repr(name) in refused.value.message
    if isinstance(value, str) and len(value) > 5:
        assert value not in refused.value.message


async def test_a_body_is_checked_before_anything_is_sent(alice):
    w = World()
    for body, expected in (
        ({"Nope": 1}, "unknown_field"),
        ({"PurReqnReleaseStatus": "B"}, "field_not_writable"),
        ({"Plant": {"deep": 1}}, "invalid_argument"),
        ({"Plant": [BODY_VALUE]}, "invalid_argument"),
        ({"Amount": "0.12345678901234567890123"}, "invalid_argument"),
        ({"PurchaseRequisition": "999"}, "invalid_argument"),  # a key cannot be changed
    ):
        out = await w.run(operation="update", key=KEY, body=body)
        assert code(out) == expected, (body, out)
        assert BODY_VALUE not in json.dumps(out)
    assert await w.untouched()


# -- actions and functions: the request -------------------------------------------


async def test_a_bound_action_posts_after_the_key_with_its_parameters_as_json(alice):
    w = World()
    out = await w.call("Approve", key=KEY, params={"Comment": "fine"})
    assert out == {"ok": True, "status": 204, "returned": "nothing", "result": None,
                   "truncated": False}
    (call,) = w.sap.calls
    assert call.method == "POST" and call.url.path == f"{KEYED}/SRV.Approve"
    assert call.url.query == b"" and json.loads(call.content) == {"Comment": "fine"}
    assert call.headers["Content-Type"] == "application/json"
    assert call.headers["OData-MaxVersion"] == "4.0"
    assert call.headers["X-CSRF-Token"] and "If-Match" not in call.headers
    # With the handle of a read of that entity, If-Match carries the raw ETag.
    handle = (await w.run(operation="get", key=KEY))["etag"]
    await w.call("Approve", key=KEY, etag=handle)
    assert w.sap.calls[-1].headers["If-Match"] == RAW_ETAG
    assert json.loads(w.sap.calls[-1].content) == {}


async def test_an_unbound_action_posts_to_its_name_with_typed_json(alice):
    w = World()
    out = await w.call("Release", params={
        "ReleaseCode": "01", "Quantity": "12.50", "Count": "9000000000", "Note": None,
    })
    assert out["ok"] is True
    (call,) = w.sap.calls
    assert call.method == "POST" and call.url.path == f"{V4_PATH}/Release"
    assert call.url.query == b""
    # Numbers stay numbers; an optional parameter passed as null is left out.
    assert json.loads(call.content) == {"ReleaseCode": "01", "Quantity": 12.5, "Count": 9000000000}
    await w.call("MarkedReading")
    assert w.sap.calls[-1].url.path == f"{V4_PATH}/MarkedReading"
    assert json.loads(w.sap.calls[-1].content) == {}


async def test_a_function_is_a_get_with_its_parameters_as_literals_in_the_path(alice):
    w = World(READ_ONLY)
    w.sap.answer = {"@odata.context": "$metadata#Edm.Int32", "value": 3}
    out = await w.call("CountOpen", params={"Plant": "1000", "Limit": 5})
    assert out == {"ok": True, "status": 200, "returned": "value", "result": 3,
                   "truncated": False}
    (call,) = w.sap.calls
    assert call.method == "GET" and call.url.path == f"{V4_PATH}/CountOpen(Plant='1000',Limit=5)"
    assert call.url.query == b"" and call.content == b""
    assert call.headers["OData-MaxVersion"] == "4.0"
    # A call that only reads: no CSRF token, no session cookie, no record.
    assert w.sap.fetches == [] and "X-CSRF-Token" not in call.headers
    assert "Cookie" not in call.headers and await all_rows() == []
    # Bound: after the key segment, by its qualified name.
    await w.call("ItemStatus", key=KEY, params={"AsOf": "2026-10-05"})
    assert w.sap.calls[-1].url.path == f"{KEYED}/SRV.ItemStatus(AsOf=2026-10-05)"
    await w.call("ItemStatus", key=KEY)
    assert w.sap.calls[-1].url.path == f"{KEYED}/SRV.ItemStatus()"


async def test_every_function_parameter_type_is_a_v4_literal(alice):
    w = World(READ_ONLY)
    w.sap.answer = {"@odata.context": "$metadata#Edm.Int32", "value": 1}
    for name, (_, value, _, literal) in TYPED.items():
        out = await w.call("Typed", params={name: value})
        assert out.get("ok") is True, (name, out)
        assert w.sap.calls[-1].url.path == f"{V4_PATH}/Typed({name}={literal})", name


@pytest.mark.parametrize("value", [
    "a/b", "x')/A_Other", "a%2Fb", "a?b", "a#b", "a\\b", "..", "a\nb", "x" * 300,
    {"a": 1}, ["a"], 10**400,
])
async def test_a_function_parameter_cannot_become_a_second_path_segment(alice, value):
    w = World(READ_ONLY)
    out = await w.call("CountOpen", params={"Plant": value})
    assert code(out) == "invalid_argument" and "'Plant'" in out["error"]["message"], out
    assert await w.untouched()


async def test_call_arguments_are_checked_before_anything_is_sent(alice):
    w = World()
    for target, args in (
        ("Release", {}),                                          # required
        ("Release", {"params": {"ReleaseCode": None}}),
        ("Release", {"params": {"ReleaseCode": "01", "Other": 1}}),  # not declared
        ("Release", {"params": {"ReleaseCode": "01", "Quantity": "x"}}),
        ("Release", {"params": {"ReleaseCode": {"a": 1}}}),
        ("Release", {"params": {"ReleaseCode": "01"}, "key": KEY}),  # not bound
        ("Release", {"params": {"ReleaseCode": "01"}, "body": {"Plant": "1"}}),
        ("CountOpen", {"params": {"Plant": "1", "Limit": "many"}}),
        ("CountOpen", {"params": {"Plant": "1"}, "etag": "x"}),
    ):
        out = await w.call(target, **args)
        assert code(out) == "invalid_argument", (target, args, out)
    for key in (None, {}, {"PurchaseRequisition": "1"}, {**KEY, "PurchaseRequisition": "a/b"}):
        out = await w.call("Approve", key=key)
        assert code(out) == "invalid_key", (key, out)
        assert "this operation" in out["error"]["message"] or "key 'Purchase" in \
            out["error"]["message"]
    # A hidden set is never named by a refusal of its operation's key.
    out = await w.call("HiddenItem", key={})
    assert code(out) == "invalid_key" and "A_Hidden" not in json.dumps(out)
    assert await w.untouched()


def test_call_request_refuses_what_the_catalogue_check_would_have():
    dialect, definition = V4Dialect(), _definition()
    item = definition.entity_set(ITEM)
    approve = definition.operation("Approve")
    assert dialect.call_request(V4_PATH, approve, KEY, {"Comment": "x"}, bound=item) == (
        "POST", f"{KEYED}/SRV.Approve", {}, {"Comment": "x"},
    )
    count = definition.operation("CountOpen")
    assert dialect.call_request(V4_PATH, count, None, {"Plant": "O'Neil"}) == (
        "GET", f"{V4_PATH}/CountOpen(Plant='O''Neil')", {}, None,
    )
    for operation, key, params, bound in (
        (approve, KEY, None, None),                                   # the set is needed
        (approve, KEY, None, definition.entity_set("A_Other")),       # ... and it is that set
        (approve, None, None, item),
        (approve, KEY, {"Nope": 1}, item),
        (approve, KEY, ["Comment"], item),
        # Not a confined, namespace-qualified name.
        (approve.model_copy(update={"qualified_name": "SRV.Approve/../x"}), KEY, None, item),
        (approve.model_copy(update={"qualified_name": ""}), KEY, None, item),
        (approve.model_copy(update={"qualified_name": "Approve"}), KEY, None, item),
        (count.model_copy(update={"name": "Count Open"}), None, {"Plant": "1"}, None),
        # A kind and a method that do not belong together, or to V4.
        (approve.model_copy(update={"http_method": "GET"}), KEY, None, item),
        (count.model_copy(update={"http_method": "POST"}), None, {"Plant": "1"}, None),
        (count.model_copy(update={"kind": "function_import"}), None, {"Plant": "1"}, None),
        (count, None, {}, None),                                      # required
    ):
        with pytest.raises(ODataError) as refused:
            dialect.call_request(V4_PATH, operation, key, params, bound=bound)
        assert refused.value.code in ("invalid_argument", "invalid_key")
        assert V4_PATH not in refused.value.message


def test_a_call_plan_shows_names_only_and_carries_the_body():
    definition = _definition()
    client = ODataClient(
        None, {"service_path": V4_PATH, "definition": definition}, V4Dialect()  # type: ignore[arg-type]
    )
    plan = client.check_call(
        definition.operation("Release"), params={"ReleaseCode": "01", "Note": BODY_VALUE}
    )
    assert isinstance(plan, CallPlan) and plan.fields == ("ReleaseCode", "Note")
    assert plan.body == {"ReleaseCode": "01", "Note": BODY_VALUE} and plan.query == {}
    assert plan.changes is True and plan.method == "POST"
    assert BODY_VALUE not in repr(plan) and V4_PATH not in repr(plan)
    plan = client.check_call(
        definition.operation("Approve"), key=KEY, params={"Comment": None}, etag=RAW_ETAG
    )
    assert plan.fields == () and plan.key == KEY and plan.etag == RAW_ETAG
    assert "10000001" not in repr(plan)
    plan = client.check_call(definition.operation("CountOpen"), params={"Plant": "1000"})
    assert (plan.changes, plan.body, plan.fields) == (False, None, ("Plant",))
    with pytest.raises(ODataError) as refused:
        client.check_call(definition.operation("Release"),
                          params={"ReleaseCode": "01", "Note": "x" * 1_100_000})
    assert refused.value.code == "invalid_argument" and "larger" in refused.value.message


# -- the two switches ---------------------------------------------------------------


@pytest.mark.parametrize("catalogue_op, allow_write, expected", SWITCH_CASES)
async def test_the_write_guard_truth_table_holds_for_v4(
    alice, catalogue_op, allow_write, expected
):
    services = catalogue(
        item_ops=ALL_OPS if catalogue_op else ["list", "get"], enabled=catalogue_op
    )
    for call in (
        UPDATE,
        CREATE,
        DELETE,
        {"target": "Release", "operation": "call", "params": {"ReleaseCode": "01"}},
        {"target": "Approve", "operation": "call", "key": KEY},
        {"target": "MarkedReading", "operation": "call"},
        {"target": "Recalculate", "operation": "call"},
    ):
        w = World({"services": ["pr4"], "allow_write": allow_write}, services)
        out = await w.run(**call)
        if expected:
            assert code(out) == expected, (call, out)
            assert await w.untouched()
        else:
            assert "error" not in out, (call, out)
            assert len(w.sap.calls) == 1 and len(w.sap.fetches) == 1
            (row,) = await all_rows()
            assert row.outcome == "ok"
            async with SessionLocal() as s:
                await s.execute(delete(ODataAuditLog))
                await s.commit()


async def test_an_action_is_a_write_whatever_the_catalogue_says(alice):
    definition = _definition()
    for name in ("Release", "Approve", "MarkedReading", "Recalculate"):
        assert definition.operation(name).is_write() is True
    closed = World(READ_ONLY)
    for name in ("Release", "MarkedReading", "Recalculate"):
        out = await closed.call(name, params={"ReleaseCode": "01"} if name == "Release" else None)
        assert code(out) == "write_not_allowed", (name, out)
    assert await closed.untouched()
    offered = {m["target"] for m in (await closed.search(query="", service="pr4"))["matches"]}
    assert {"CountOpen", "ItemStatus", "OpenItems", "Typed"} <= offered
    assert not {"Release", "Approve", "MarkedReading", "Recalculate", "Copy"} & offered
    # With both switches: sent like a write (CSRF session), and recorded.
    w = World()
    for name, method in (("MarkedReading", "POST"), ("Recalculate", "GET")):
        assert (await w.call(name))["ok"] is True
        call = w.sap.calls[-1]
        assert call.method == method and call.headers["X-CSRF-Token"] and call.headers["Cookie"]
    assert w.sap.calls[-1].url.path == f"{V4_PATH}/Recalculate()"
    assert [(r.operation, r.target, r.outcome) for r in await all_rows()] == [
        ("call", "MarkedReading", "ok"), ("call", "Recalculate", "ok"),
    ]


@pytest.mark.parametrize("allow_write", [True, False])
async def test_whatever_search_offers_execute_does_not_refuse_by_a_switch(alice, allow_write):
    w = World({"services": ["pr4", "pr4-jobs"], "allow_write": allow_write})
    switch_codes = {"unknown_service", "service_disabled", "unknown_target",
                    "operation_disabled", "write_not_allowed", "not_available"}
    offered: list[tuple[str, str]] = []
    for match in (await w.search(query="", service="pr4"))["matches"]:
        assert match["operations"], match
        offered += [(match["target"], op) for op in match["operations"]]
    for target, operation in offered:
        out = await w.run(target=target, operation=operation)
        assert code(out) not in switch_codes, (target, operation, out)
    called = {target for target, operation in offered if operation == "call"}
    reading = {"CountOpen", "ItemStatus", "OpenItems", "Typed"}
    changing = {"Release", "Approve", "MarkedReading", "Copy", "Recalculate"}
    # `HiddenItem` reads, bound to a set nobody may read: callable, and offered.
    assert called - {"HiddenItem"} == (reading | changing if allow_write else reading)
    assert not called & (set(UNCALLABLE) | {"Off"})
    kinds = {operation for _, operation in offered}
    assert ({"create", "update", "delete"} <= kinds) is allow_write
    # What is enabled and never offered is exactly what the admin API lists.
    assert {(u["name"], u["reason"]) for u in uncallable_operations(
        catalogue()["pr4"]["definition"], "v4"
    )} == set(UNCALLABLE.items())
    if allow_write:
        for name in UNCALLABLE:
            out = await w.call(name)
            assert code(out) == "not_available" and "catalogue setting" in out["error"]["hint"]


def _operation(**kw: Any) -> OperationDef:
    return OperationDef.model_validate(_action("X", **kw))


@pytest.mark.parametrize("operation, keys, dialect, allow_write, reason", [
    (_operation(), None, V4Dialect(), True, None),
    (_operation(enabled=False), None, V4Dialect(), True, "operation_disabled"),
    (_operation(), None, V4Dialect(), False, "write_not_allowed"),
    (_operation(changes_data=False), None, V4Dialect(), False, "write_not_allowed"),
    (OperationDef.model_validate(_function("F")), None, V4Dialect(), False, "write_not_allowed"),
    (OperationDef.model_validate(_function("F", changes_data=False)), None, V4Dialect(), False,
     None),
    # A kind of the other version is never sent with this version's rules.
    (_operation(), None, V2Dialect(), True, "calls_not_available"),
    (OperationDef.model_validate({**_action("X"), "kind": "function_import"}), None,
     V4Dialect(), True, "calls_not_available"),
    (_operation(), None, None, True, "calls_not_available"),
    # Bound: the set is there and has a key. The key travels in the path,
    # so no parameter has to carry it.
    (_operation(bound_to=ITEM), None, V4Dialect(), True, "bound_set_missing"),
    (_operation(bound_to=ITEM), [], V4Dialect(), True, "bound_set_without_key"),
    (_operation(bound_to=ITEM), list(KEY), V4Dialect(), True, None),
    *[
        (_operation(parameters=[{"name": "P", "type": edm}]), None, V4Dialect(), True,
         "parameter_type")
        for edm in ("SRV.Address", "Collection(Edm.String)", "Edm.Binary", "Edm.Stream",
                    "Edm.DateTime", "Edm.Time")
    ],
    (_operation(parameters=[{"name": "P", "type": "SRV.Address", "required": False}]),
     None, V4Dialect(), True, None),
    *[
        (_operation(parameters=[{"name": "P", "type": edm}]), None, V4Dialect(), True, None)
        for edm, _, _, _ in TYPED.values()
    ],
])
def test_call_refusal_is_the_one_rule_for_v4_too(operation, keys, dialect, allow_write, reason):
    assert call_refusal(operation, keys, dialect, allow_write=allow_write) == reason


# -- the audit ----------------------------------------------------------------------


async def test_a_v4_update_is_recorded_intent_first_with_field_names_only(alice):
    w = World()
    out = await w.run(
        operation="update", key=KEY, body={"Plant": BODY_VALUE, "RequestedQuantity": "7.50"}
    )
    assert out["ok"] is True
    # The intent row was committed before the token fetch and before the PATCH.
    assert w.sap.rows_seen == {0: [("update", ITEM, "intent")], 1: [("update", ITEM, "intent")]}
    (row,) = await all_rows()
    assert (row.operation, row.target, row.service, row.agent) == ("update", ITEM, "pr4", "buyer")
    assert (row.outcome, row.phase, row.http_status) == ("ok", "write", 204)
    assert json.loads(row.key_json) == KEY
    assert json.loads(row.body_fields_json) == ["Plant", "RequestedQuantity"]
    assert row.run_id == "run-1" and row.finished_at is not None and row.sent_as
    stored = json.dumps({c.name: str(getattr(row, c.name)) for c in row.__table__.columns})
    for value in (BODY_VALUE, "T-user-token", "S-user-token", "jwt-of-", "20260101000000"):
        assert value not in stored, value


async def test_a_v4_action_is_recorded_intent_first_with_parameter_names_only(alice):
    w = World()
    out = await w.call("Approve", key=KEY, params={"Comment": BODY_VALUE})
    assert out["ok"] is True
    intent = [("call", "Approve", "intent")]
    assert w.sap.rows_seen == {0: intent, 1: intent}  # the token fetch, then the POST
    (row,) = await all_rows()
    assert (row.operation, row.target, row.outcome, row.phase, row.http_status) == (
        "call", "Approve", "ok", "write", 204,
    )
    assert json.loads(row.key_json) == KEY and json.loads(row.body_fields_json) == ["Comment"]
    stored = json.dumps({c.name: str(getattr(row, c.name)) for c in row.__table__.columns})
    assert BODY_VALUE not in stored
    # A refused action is a result row too, with SAP's status.
    w.sap.answer = v4_error(400, "MM/1", "No")
    assert code(await w.call("Release", params={"ReleaseCode": "01"})) == "sap_error"
    row = (await all_rows())[-1]
    assert (row.target, row.outcome, row.phase, row.http_status) == (
        "Release", "sap_error", "write", 400,
    )


class _NoIntent:
    """A recorder whose store is down."""

    records_writes = True

    async def intent(self, record: Any) -> Any:
        raise RuntimeError("the audit table cannot be written")

    async def result(self, token: Any, record: Any) -> None:
        return None


class _Unmarked(_NoIntent):
    records_writes = "yes"


async def test_without_an_intent_row_nothing_is_sent(alice):
    for call in (
        UPDATE, CREATE, DELETE,
        {"target": "Approve", "operation": "call", "key": KEY},
        {"target": "Recalculate", "operation": "call"},
    ):
        w = World(recorder=_NoIntent())
        out = await w.run(**call)
        assert code(out) == "audit_unavailable", (call, out)
        assert w.sap.requests == []
        # A toolset that cannot record at all offers and sends no write.
        w = World(recorder=_Unmarked())
        out = await w.run(**call)
        assert code(out) == "audit_not_configured", (call, out)
        assert w.sap.requests == []


async def test_a_write_through_a_signed_in_user_service_needs_the_user():
    w = World()
    for call in (UPDATE, {"target": "Approve", "operation": "call", "key": KEY},
                 {"target": "Release", "operation": "call", "params": {"ReleaseCode": "01"}}):
        out = await w.run(**call)
        assert code(out) == "no_user", (call, out)
    assert w.sap.requests == [] and await all_rows() == []
    # The technical twin writes as the destination's own user.
    out = await w.run(service="pr4-jobs", **UPDATE)
    assert out["ok"] is True
    (row,) = await all_rows()
    assert row.sent_as == "technical:S4_ODATA_TECH" and row.token_digest is None


# -- what a call answers --------------------------------------------------------------

WITHHELD = {"ok": True, "status": 200, "returned": "withheld", "result": None, "truncated": False}


@pytest.mark.parametrize("context", [
    CONTEXT,
    f"https://s4.internal:44300{V4_PATH}/$metadata#{ITEM}/$entity",
    f"../$metadata#{ITEM}(PurchaseRequisition,Plant)/$entity",
    f"$metadata#{ITEM_TYPE}",
])
async def test_a_returned_entity_of_the_declared_set_is_cut_to_its_readable_fields(
    alice, context
):
    w = World()
    w.sap.answer = entity(context=context)
    out = await w.call("Copy", key=KEY)
    assert (out["ok"], out["returned"]) == (True, "entity"), out
    assert out["result"] == {**KEY, "PurReqnReleaseStatus": "05", "Plant": "1000",
                             "RequestedQuantity": 5}
    text = json.dumps(out)
    assert SECRET_USER not in text and RAW_ETAG not in text and "@odata" not in text
    assert "etag" not in out


@pytest.mark.parametrize("answer", [
    entity(context=None),                                       # says nothing about itself
    entity(context="$metadata#A_Other/$entity"),                # another set
    entity(context="$metadata#SRV.OtherType"),                  # another type
    entity(context=f"$metadata#{ITEM}"),                        # a collection's context
    entity(context=f"$metadata#{ITEM}('1')/to_Twin/$entity"),   # reached through a navigation
    entity(context=f"https://x.example/{ITEM}/$entity"),        # not a metadata context
    entity(**{"@odata.type": "#SRV.DerivedType"}),              # a derived type
    {"@odata.context": f"$metadata#{ITEM}", "value": [entity(context=None)]},  # many for one
    {"@odata.context": "$metadata#SRV.Address", "Street": SECRET_USER},
    {"@odata.context": "$metadata#Collection(Edm.String)", "value": [SECRET_USER]},
    {"@odata.context": "$metadata#Edm.String", "value": SECRET_USER},  # a write's scalar
    {"@odata.context": CONTEXT},
])
async def test_anything_else_a_call_returns_is_withheld(alice, answer):
    w = World()
    w.sap.answer = answer
    out = await w.call("Copy", key=KEY)
    assert out == WITHHELD
    (row,) = await all_rows()
    assert row.outcome == "ok"  # the call worked; only its answer is not shown


async def test_a_returned_collection_and_a_scalar_of_a_reading_function(alice):
    w = World(READ_ONLY)
    w.sap.answer = {"@odata.context": f"$metadata#{ITEM}", "value": [
        entity(context=None), entity({**KEY, "PurchaseRequisitionItem": "00020"}, context=None),
    ]}
    out = await w.call("OpenItems")
    assert out["returned"] == "entities" and len(out["result"]) == 2
    assert SECRET_USER not in json.dumps(out) and "@odata" not in json.dumps(out)
    # One entity where a collection was declared, or rows of another type.
    for answer in (
        entity(),
        {"@odata.context": "$metadata#A_Other", "value": [entity(context=None)]},
        {"@odata.context": f"$metadata#{ITEM}", "value": [
            entity(context=None, **{"@odata.type": "#SRV.OtherType"}),
        ]},
        {"@odata.context": f"$metadata#{ITEM}", "value": [SECRET_USER]},
    ):
        w.sap.answer = answer
        assert (await w.call("OpenItems")) == WITHHELD, answer
    # A scalar: numbers and booleans; a text only when it is one short plain line.
    for value, shown in (
        (3, 3), (True, True), (1.5, 1.5), ("released", "released"),
        ('{"CreatedByUser":"' + SECRET_USER + '"}', None), ("<a>" + SECRET_USER + "</a>", None),
        ("two\nlines", None), ("x" * 1001, None),
    ):
        w.sap.answer = {"@odata.context": "$metadata#Edm.String", "value": value}
        out = await w.call("CountOpen", params={"Plant": "1000"})
        assert out["returned"] == ("value" if shown is not None else "withheld"), value
        assert out["result"] == shown and SECRET_USER not in json.dumps(out)
    # No content, in each of its V4 forms.
    for answer in (httpx.Response(204), {"@odata.context": "$metadata#Edm.Null", "value": None},
                   {"@odata.null": True}):
        w.sap.answer = answer
        out = await w.call("CountOpen", params={"Plant": "1000"})
        assert (out["returned"], out["result"]) == ("nothing", None), answer
    # An error envelope with a 200, or no V4 answer at all, is not a result.
    for answer in ({"error": {"code": "X", "message": "no"}}, [1, 2]):
        w.sap.answer = answer
        out = await w.call("CountOpen", params={"Plant": "1000"})
        assert code(out) == "sap_error", answer


def test_parse_call_recognises_the_v4_shapes_positively():
    parse = V4Dialect().parse_call
    assert parse(None, "X") == ("none", None)
    assert parse({"@odata.null": True}, "X") == ("none", None)
    assert parse({"@odata.context": "c", "value": None}, "X") == ("none", None)
    assert parse({"@odata.context": "c", "value": 3}, "X") == ("value", 3)
    assert parse({"value": "a"}, "X") == ("value", "a")
    assert parse({"@odata.context": "c", "value": [1]}, "X") == ("collection", [1])
    row = {"@odata.context": CONTEXT, "Plant": "1"}
    assert parse(row, "X") == ("entity", row)
    # An entity whose one property is called `value` is an entity, not a wrapper.
    row = {"@odata.context": CONTEXT, "value": "1"}
    assert parse(row, "X") == ("entity", row)
    assert parse({"@odata.context": "c", "value": {"a": 1}}, "X") == ("other", None)
    assert parse({"@odata.context": "c"}, "X") == ("other", None)
    assert parse({}, "X") == ("other", None)
    for payload in ([], "text", 3, {"error": {"code": "X"}}, {"error": "x", "value": 1}):
        with pytest.raises(ODataError) as refused:
            parse(payload, "X")
        assert refused.value.code == "sap_error"
