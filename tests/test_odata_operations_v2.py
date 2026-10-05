"""V2 function imports through ``execute_operation`` (Task W4).

In SAP the real business steps -- release, approve, post, cancel -- are
function imports, so this suite is mostly about what must NOT happen: a call
that changes data without both switches, without a record, with a value that
turns into a second query option, twice after a failure, or one that hands
back entity data the catalogue never released.

The real toolset on a mock SAP (``httpx.MockTransport`` behind the real
``destination_http_client``), recording through the real
``StoredWriteRecorder`` into this process's test database. The SAP double
looks into the audit table when a request arrives, which is how "the intent
row exists before anything is sent" is asserted.

No network.
"""

from __future__ import annotations

import asyncio
import copy
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
from agents.odata.client import (  # noqa: E402
    CallPlan,
    ODataClient,
    ODataError,
    call_changes_data,
)
from agents.odata.models import (  # noqa: E402
    OperationDef,
    ServiceDefinition,
    validate_odata_service,
)
from agents.odata.search import operation_changes_data, search_catalogue  # noqa: E402
from agents.odata.tools import odata_toolset  # noqa: E402
from agents.odata.v2 import MAX_PARAM_VALUE_CHARS, V2Dialect  # noqa: E402
from agents.odata.v4 import V4Dialect  # noqa: E402
from tests.odata_helpers import FakeResolver, v2_error  # noqa: E402

ALICE = "alice@example.com"
BOB = "bob@example.com"
ITEM = "A_PurchaseRequisitionItem"
ITEM_TYPE = "API_PURCHASEREQ_PROCESS_SRV.A_PurchaseRequisitionItemType"
SERVICE_PATH = "/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV"
V4_PATH = "/sap/opu/odata4/sap/api_purchasereq/srvd_a2x/sap/purchaserequisition/0001"
COOKIE_NAME = "SAP_SESSIONID_XXX_100"
KEY = {"PurchaseRequisition": "10000001", "PurchaseRequisitionItem": "00010"}
RELEASE = {**KEY, "ReleaseCode": "01"}
RAW_ETAG = "W/\"datetimeoffset'2026-01-01T00%3A00%3A00Z'\""
SECRET_USER = "JDOE-HIDDEN"
PARAM_VALUE = "PARAM-VALUE-5c1"
CTX = SimpleNamespace(run_id="run-1")
KEY_PARAMS = [{"name": "PurchaseRequisition"}, {"name": "PurchaseRequisitionItem"}]
# Every parameter type the V2 dialect writes, with a value and its literal.
TYPED = {
    "Text": ("Edm.String", "O'Neil & co", "'O''Neil & co'"),
    "Flag": ("Edm.Boolean", True, "true"),
    "Small": ("Edm.Byte", 7, "7"),
    "Signed": ("Edm.SByte", -7, "-7"),
    "Short": ("Edm.Int16", 300, "300"),
    "Count": ("Edm.Int32", 12, "12"),
    "Big": ("Edm.Int64", 9000000000, "9000000000L"),
    "Amount": ("Edm.Decimal", "12.50", "12.50M"),
    "Ratio": ("Edm.Double", 1.5, "1.5d"),
    "Single": ("Edm.Single", "2.5", "2.5f"),
    "On": ("Edm.DateTime", "2026-10-05T00:00:00", "datetime'2026-10-05T00:00:00'"),
    "At": ("Edm.DateTimeOffset", "2026-10-05T12:00:00+02:00",
           "datetimeoffset'2026-10-05T12:00:00+02:00'"),
    "Clock": ("Edm.Time", "PT12H30M", "time'PT12H30M'"),
    "Id": ("Edm.Guid", "0050568D-393C-1EDB-9B8C-2F3C4D5E6F70",
           "guid'0050568D-393C-1EDB-9B8C-2F3C4D5E6F70'"),
}


def _op(name: str, method: str = "POST", **kw: Any) -> dict[str, Any]:
    return {"name": name, "kind": "function_import", "http_method": method, "enabled": True, **kw}


def _entity_set(name: str, operations: list[str], entity_type: str = ITEM_TYPE) -> dict[str, Any]:
    return {
        "name": name,
        "entity_type": entity_type,
        "keys": [{"name": "PurchaseRequisition"}, {"name": "PurchaseRequisitionItem"}],
        "operations": operations,
        "fields": [
            {"name": "PurchaseRequisition", "selectable": True, "filterable": True},
            {"name": "PurchaseRequisitionItem", "selectable": True},
            {"name": "PurReqnReleaseStatus", "selectable": True},
            {"name": "Plant", "selectable": True, "writable": True},
            {"name": "CreatedByUser", "personal_data": True},
        ],
    }


def _operations() -> list[dict[str, Any]]:
    return [
        _op("ReleaseItem", parameters=[
            *KEY_PARAMS, {"name": "ReleaseCode"}, {"name": "Note", "required": False},
        ]),
        # `changes_data` not said: the model's default, a write although a GET.
        _op("Recalculate", "GET"),
        _op("CountOpen", "GET", changes_data=False,
            parameters=[{"name": "Plant", "required": False}]),
        # Stored as "only reads", but a POST: a write here all the same.
        _op("PostMarkedReading", "POST", changes_data=False),
        _op("Off", "POST", enabled=False),
        _op("OffReading", "GET", enabled=False, changes_data=False),
        _op("Approve", bound_to=ITEM,
            parameters=[*KEY_PARAMS, {"name": "Comment", "required": False}]),
        _op("ItemStatus", "GET", changes_data=False, bound_to=ITEM, parameters=KEY_PARAMS),
        _op("HiddenItem", "GET", changes_data=False, bound_to="A_Hidden", parameters=KEY_PARAMS),
        _op("Untyped", "GET", changes_data=False, bound_to="A_Untyped", parameters=KEY_PARAMS),
        _op("Typed", "GET", changes_data=False, parameters=[
            {"name": name, "type": edm, "required": False} for name, (edm, _, _) in TYPED.items()
        ]),
        _op("Odd", "GET", changes_data=False, parameters=[
            {"name": "Address", "type": "API_PR.Address", "required": False},
            {"name": "Plants", "type": "Collection(Edm.String)", "required": False},
            {"name": "Blob", "type": "Edm.Binary", "required": False},
        ]),
        _op("BoundWithoutKeyParams", bound_to=ITEM, parameters=[{"name": "Comment"}]),
    ]


def catalogue(
    operations: list[dict[str, Any]] | None = None,
    entity_sets: list[dict[str, Any]] | None = None,
) -> dict[str, dict]:
    """``pr`` (as the signed-in user), ``pr-jobs`` (technical) and ``pr-v4``."""
    definition = {
        "entity_sets": [
            _entity_set(ITEM, ["list", "get"]),
            _entity_set("A_Hidden", []),  # in the catalogue, readable by nobody
            _entity_set("A_Untyped", ["get"], entity_type=""),
            *(entity_sets or []),
        ],
        "operations": _operations() if operations is None else operations,
    }
    out: dict[str, dict] = {}
    for name, destination, as_user in (
        ("pr", "S4_ODATA_USER", True),
        ("pr-jobs", "S4_ODATA_TECH", False),
    ):
        clean = validate_odata_service(
            {
                "name": name,
                "title": "Purchase requisitions",
                "purpose": "Purchase requisitions and their items",
                "destination": destination,
                "user_context": as_user,
                "odata_version": "v2",
                "service_path": SERVICE_PATH,
                "definition": definition,
            }
        )
        out[name] = {**clean, "id": 1, "counts": {}, "has_write": True, "used_by": []}
    v4 = validate_odata_service(
        {
            "name": "pr-v4",
            "title": "Purchase requisitions (V4)",
            "purpose": "Purchase requisitions and their items",
            "destination": "S4_ODATA_USER",
            "user_context": True,
            "odata_version": "v4",
            "service_path": V4_PATH,
            "definition": {
                "entity_sets": [_entity_set(ITEM, ["list", "get"])],
                "operations": [
                    {"name": "Release", "qualified_name": "SRV.Release", "kind": "action",
                     "http_method": "POST", "enabled": True, "changes_data": True},
                    {"name": "CountOpen", "qualified_name": "SRV.CountOpen", "kind": "function",
                     "http_method": "GET", "enabled": True, "changes_data": False},
                ],
            },
        }
    )
    out["pr-v4"] = {**v4, "id": 3, "counts": {}, "has_write": True, "used_by": []}
    return out


def entity(key: dict[str, str] = KEY, type_name: str | None = ITEM_TYPE) -> dict[str, Any]:
    """One item as Gateway returns it, with a field the catalogue keeps back."""
    metadata: dict[str, Any] = {"uri": "https://s4.internal:44300" + SERVICE_PATH, "etag": RAW_ETAG}
    if type_name is not None:
        metadata["type"] = type_name
    return {
        "__metadata": metadata,
        **key,
        "PurReqnReleaseStatus": "05",
        "Plant": "1000",
        "CreatedByUser": SECRET_USER,
    }


async def all_rows() -> list[ODataAuditLog]:
    async with SessionLocal() as s:
        return list((await s.execute(select(ODataAuditLog).order_by(ODataAuditLog.id))).scalars())


class Sap:
    """SAP: one CSRF session per caller; notes the audit table per request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.rows_seen: list[list[tuple[str, str, str]]] = []
        self.issued: dict[str, tuple[str, str]] = {}
        self.crossed: list[str] = []
        self.answer: Any = None  # a Response, a JSON body, or a callable(request)
        self.read_item: Any = None

    @property
    def calls(self) -> list[httpx.Request]:
        """Everything but the CSRF token requests."""
        return [r for r in self.requests if r.headers.get("X-CSRF-Token") != "Fetch"]

    @property
    def fetches(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.headers.get("X-CSRF-Token") == "Fetch"]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.rows_seen.append([(r.operation, r.target, r.outcome) for r in await all_rows()])
        caller = request.headers.get("Authorization", "").removeprefix("Bearer ")
        await asyncio.sleep(0)  # let another run in, as a real round trip would
        if request.headers.get("X-CSRF-Token") == "Fetch":
            token, cookie = f"T-{caller}", f"S-{caller}"
            self.issued[caller] = (token, cookie)
            return httpx.Response(
                200,
                headers=[
                    ("X-CSRF-Token", token),
                    ("Set-Cookie", f"{COOKIE_NAME}={cookie}; path=/; secure; HttpOnly"),
                ],
                json={"d": {"EntitySets": [ITEM]}},
            )
        if f"/{ITEM}(" in request.url.path:  # a plain entity read
            return httpx.Response(200, json={"d": self.read_item or entity()})
        if "X-CSRF-Token" in request.headers:
            token, cookie = request.headers["X-CSRF-Token"], request.headers.get("Cookie", "")
            expected = self.issued.get(caller)
            if not (expected and token == expected[0] and f"{COOKIE_NAME}={expected[1]}" in cookie):
                self.crossed.append(caller)
                return httpx.Response(403, headers={"x-csrf-token": "Required"}, text="CSRF")
        answer = self.answer
        if callable(answer):
            answer = answer(request)
            if asyncio.iscoroutine(answer):
                answer = await answer
        if isinstance(answer, httpx.Response):
            return answer
        if answer is not None:
            return httpx.Response(200, json=answer)
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

        self.recorder = StoredWriteRecorder() if recorder is ... else recorder
        self.toolset = odata_toolset(
            oauth if oauth is not None else {"services": ["pr", "pr-jobs"], "allow_write": True},
            auth_mode="destination",
            services=services or catalogue(),
            agent_name="buyer",
            transport=httpx.MockTransport(self.sap.handler),
            resolver_factory=factory,
            recorder=self.recorder,
        )

    async def call(self, target: str, **args: Any) -> dict:
        full = {"service": "pr", "target": target, "operation": "call", **args}
        return await self.toolset.tools["execute_operation"].function(CTX, **full)

    async def run(self, **args: Any) -> dict:
        return await self.toolset.tools["execute_operation"].function(CTX, **args)

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


READ_ONLY = {"services": ["pr", "pr-jobs"]}


class signed_in:
    def __init__(self, principal: str, token: str | None = None) -> None:
        self.principal, self.token = principal, token or f"jwt-of-{principal}"

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


def code(out: dict) -> str | None:
    return out.get("error", {}).get("code")


# -- the happy paths --------------------------------------------------------------


async def test_post_function_import_sends_typed_parameters_and_a_csrf_token(alice):
    w = World()
    out = await w.call("ReleaseItem", params=RELEASE)
    assert out == {
        "ok": True, "status": 204, "returned": "nothing", "result": None, "truncated": False,
    }
    fetch, sent = w.sap.requests
    assert fetch.method == "GET" and fetch.headers["X-CSRF-Token"] == "Fetch"
    assert sent.method == "POST" and sent.url.path == f"{SERVICE_PATH}/ReleaseItem"
    assert dict(sent.url.params) == {
        "PurchaseRequisition": "'10000001'",
        "PurchaseRequisitionItem": "'00010'",
        "ReleaseCode": "'01'",
        "$format": "json",
    }
    assert sent.headers["X-CSRF-Token"] == f"T-user-token-of-{ALICE}"
    assert f"{COOKIE_NAME}=S-user-token-of-{ALICE}" in sent.headers["Cookie"]
    assert sent.content == b"" and "If-Match" not in sent.headers
    assert sent.url.host == "s4.internal"


async def test_a_changing_call_is_recorded_intent_first_with_parameter_names_only(alice):
    w = World()
    params = {**RELEASE, "Note": PARAM_VALUE}
    assert (await w.call("ReleaseItem", params=params))["ok"] is True
    # What SAP saw in the table when each request arrived: the row, open,
    # before the CSRF token request and before the call itself.
    assert w.sap.rows_seen == [[("call", "ReleaseItem", "intent")]] * 2
    (row,) = await all_rows()
    assert (row.agent, row.run_id, row.service, row.target, row.operation) == (
        "buyer", "run-1", "pr", "ReleaseItem", "call",
    )
    assert (row.outcome, row.phase, row.http_status) == ("ok", "write", 204)
    assert json.loads(row.body_fields_json) == list(params)
    assert row.key_json is None and row.created_key_json is None
    assert row.finished_at is not None and row.sent_as
    async with SessionLocal() as s:
        from sqlalchemy import text

        stored = json.dumps([list(map(str, r)) for r in await s.execute(
            text("SELECT * FROM odata_audit_log")
        )])
    for value in (PARAM_VALUE, "10000001", "T-user-token", "S-user-token", "jwt-of-"):
        assert value not in stored, value


async def test_get_function_import_marked_changes_data_needs_allow_write_and_a_token(alice):
    closed = World(READ_ONLY)
    assert code(await closed.call("Recalculate")) == "write_not_allowed"
    assert await closed.untouched()
    w = World()
    out = await w.call("Recalculate")
    assert out["ok"] is True and out["status"] == 204
    fetch, sent = w.sap.requests
    # A GET, and all the same sent like a write: token, cookie, a record.
    assert sent.method == "GET" and sent.url.path.endswith("/Recalculate")
    assert sent.headers["X-CSRF-Token"] == f"T-user-token-of-{ALICE}" and "Cookie" in sent.headers
    (row,) = await all_rows()
    assert (row.operation, row.target, row.outcome) == ("call", "Recalculate", "ok")


async def test_get_function_import_not_changing_data_runs_without_allow_write(alice):
    for oauth in (READ_ONLY, None):
        w = World(oauth)
        w.sap.answer = {"d": {"CountOpen": 7}}
        out = await w.call("CountOpen", params={"Plant": "1000"})
        assert out == {
            "ok": True, "status": 200, "returned": "value", "result": 7, "truncated": False,
        }
        (sent,) = w.sap.requests  # no CSRF token request
        assert sent.method == "GET" and sent.url.path == f"{SERVICE_PATH}/CountOpen"
        assert dict(sent.url.params) == {"Plant": "'1000'", "$format": "json"}
        assert "X-CSRF-Token" not in sent.headers and "Cookie" not in sent.headers
        assert await all_rows() == []  # a read is not recorded


async def test_a_reading_call_needs_no_recorder_and_a_changing_one_does(alice):
    w = World(recorder=None)
    w.sap.answer = {"d": {"CountOpen": 3}}
    assert (await w.call("CountOpen"))["result"] == 3
    before = len(w.sap.requests)
    for target, args in (("ReleaseItem", {"params": RELEASE}), ("Recalculate", {}),
                         ("PostMarkedReading", {}), ("Approve", {"key": KEY})):
        assert code(await w.call(target, **args)) == "audit_not_configured", target
    assert len(w.sap.requests) == before and await all_rows() == []


async def test_changes_data_defaults_to_true_when_the_catalogue_does_not_say(alice):
    stored = catalogue()["pr"]["definition"]["operations"]
    assert next(o for o in stored if o["name"] == "Recalculate")["changes_data"] is True
    # Also when the key is missing from what is stored: never "only reads".
    assert operation_changes_data({"http_method": "GET"}) is True
    assert operation_changes_data({"http_method": "GET", "changes_data": None}) is True
    assert operation_changes_data({"http_method": "GET", "changes_data": "false"}) is True
    assert operation_changes_data({"http_method": "GET", "changes_data": 0}) is True
    assert operation_changes_data({"http_method": "GET", "changes_data": False}) is False
    assert operation_changes_data({"changes_data": False}) is True  # no method: not a GET


@pytest.mark.parametrize(
    "method, changes, write",
    [("GET", False, False), ("GET", True, True), ("POST", True, True), ("POST", False, True)],
)
def test_only_a_get_marked_as_not_changing_is_a_read(method, changes, write):
    op = OperationDef(
        name="X", kind="function_import", http_method=method, enabled=True, changes_data=changes
    )
    assert call_changes_data(op) is write
    assert operation_changes_data(op.model_dump()) is write  # search and execute agree


async def test_a_post_marked_as_not_changing_is_still_a_write(alice):
    closed = World(READ_ONLY)
    assert code(await closed.call("PostMarkedReading")) == "write_not_allowed"
    assert await closed.untouched()
    found = await closed.search(query="", detail="full")
    assert "PostMarkedReading" not in {m["target"] for m in found["matches"]}
    w = World()
    assert (await w.call("PostMarkedReading"))["ok"] is True
    fetch, sent = w.sap.requests
    assert sent.method == "POST" and sent.headers["X-CSRF-Token"]
    (row,) = await all_rows()
    assert (row.operation, row.target) == ("call", "PostMarkedReading")
    full = await w.search(query="PostMarkedReading", detail="full")
    assert full["matches"][0]["changes_data"] is True  # what a call of it IS


# -- the check order: nothing built or sent before every check ---------------------


async def test_the_gates_of_a_call_in_their_order(alice):
    """Each case fails exactly one gate; the earlier gate wins when two fail."""
    bad = {"params": {"Nope": 1}}
    cases: list[tuple[World, dict[str, Any], str]] = [
        (World(), {"service": "nope", "target": "ReleaseItem", **bad}, "unknown_service"),
        (World(), {"target": "ReleaseItem", "operation": "CALL", **bad}, "invalid_argument"),
        (World(), {"target": "Nope", **bad}, "unknown_target"),
        (World(), {"target": ITEM, **bad}, "unknown_target"),  # an entity set is no operation
        (World(READ_ONLY), {"target": "Off", **bad}, "operation_disabled"),  # before the switch
        (World(READ_ONLY), {"target": "OffReading", **bad}, "operation_disabled"),
        (World(READ_ONLY, recorder=None), {"target": "ReleaseItem", **bad}, "write_not_allowed"),
        (World(recorder=None), {"target": "ReleaseItem", **bad}, "audit_not_configured"),
        (World(), {"target": "ReleaseItem", **bad}, "invalid_argument"),
        (World(), {"target": "Approve", "key": {"PurchaseRequisition": "1"}}, "invalid_key"),
        (World(), {"target": "Approve", "key": KEY, "etag": RAW_ETAG}, "invalid_etag"),
    ]
    for w, args, expected in cases:
        out = await w.run(**{"service": "pr", "operation": "call", **args})
        assert code(out) == expected, (args, out)
        assert await w.untouched(), args


async def test_a_disabled_operation_is_operation_disabled(alice):
    w = World()
    for target in ("Off", "OffReading"):
        out = await w.call(target)
        assert code(out) == "operation_disabled", out
    assert await w.untouched()
    found = await w.search(query="", detail="full")
    assert not {"Off", "OffReading"} & {m["target"] for m in found["matches"]}


async def test_missing_required_and_unknown_parameters_are_refused(alice):
    w = World()
    missing = {k: v for k, v in RELEASE.items() if k != "ReleaseCode"}
    cases: list[tuple[str, dict[str, Any], str]] = [
        ("ReleaseItem", {"params": missing}, "'ReleaseCode'"),
        ("ReleaseItem", {"params": {**missing, "ReleaseCode": None}}, "'ReleaseCode'"),
        ("ReleaseItem", {}, "needs parameter"),
        ("ReleaseItem", {"params": {**RELEASE, "Extra": "1"}}, "'Extra'"),
        ("ReleaseItem", {"params": {**RELEASE, "$top": "1"}}, "that name"),
        ("ReleaseItem", {"params": {**RELEASE, "releasecode": "1"}}, "'releasecode'"),
        ("ReleaseItem", {"params": {**RELEASE, 5: "1"}}, "that name"),
        ("ReleaseItem", {"params": [RELEASE]}, "params must be an object"),
        ("ReleaseItem", {"params": "ReleaseCode=01"}, "params must be an object"),
        ("ReleaseItem", {"params": RELEASE, "body": {"Plant": "1"}}, "are not used"),
        ("ReleaseItem", {"params": RELEASE, "filter": "Plant eq '1'"}, "are not used"),
        ("ReleaseItem", {"params": RELEASE, "top": 5}, "are not used"),
        ("ReleaseItem", {"params": RELEASE, "navigation": "to_Item"}, "are not used"),
        ("ReleaseItem", {"params": RELEASE, "key": KEY}, "takes no key"),
        ("ReleaseItem", {"params": RELEASE, "etag": "h-whatever"}, "etag is used only"),
        ("CountOpen", {"key": KEY}, "takes no key"),
        ("CountOpen", {"etag": "h-whatever"}, "etag is used only"),
        ("ItemStatus", {"key": KEY, "etag": "h-whatever"}, "etag is used only"),
    ]
    for target, args, said in cases:
        out = await w.call(target, **args)
        assert code(out) == "invalid_argument", (target, args, out)
        assert said in out["error"]["message"], (args, out)
    assert await w.untouched()


@pytest.mark.parametrize(
    "name, value",
    [
        ("Text", {"a": 1}), ("Text", ["a"]), ("Text", True), ("Text", 1.5),
        ("Text", "line\nbreak"), ("Text", "x" * (MAX_PARAM_VALUE_CHARS + 1)),
        ("Flag", "yes"), ("Flag", 1),
        ("Small", 256), ("Small", -1), ("Signed", 128), ("Short", 40000),
        ("Count", 2**31), ("Count", "12 or 1 eq 1"), ("Count", 1.5), ("Count", True),
        ("Big", 2**63), ("Big", "9L"),
        ("Amount", "12,50"), ("Amount", "1e5"), ("Amount", float("nan")),
        ("Ratio", "abc"), ("Ratio", float("inf")),
        ("On", "2026-10-05"), ("On", "2026-10-05T00:00:00'&$top=1"), ("On", 20261005),
        ("At", "2026-10-05T12:00:00"), ("Clock", "12:30"),
        ("Id", "not-a-guid"), ("Id", "0050568D-393C-1EDB-9B8C-2F3C4D5E6F70'"),
    ],
)
async def test_a_value_without_the_form_of_its_type_is_refused(alice, name, value):
    w = World()
    out = await w.call("Typed", params={name: value})
    assert code(out) == "invalid_argument", out
    message = json.dumps(out)
    assert f"'{name}'" in message and TYPED[name][0] in message
    if isinstance(value, str):
        assert value not in message  # a refusal never repeats the value
    assert await w.untouched()


async def test_complex_collection_and_unknown_types_are_refused_never_guessed(alice):
    w = World()
    for name, value in (("Address", "Main street 1"), ("Address", {"Street": "x"}),
                        ("Plants", ["1000"]), ("Plants", "1000"), ("Blob", "AAEC")):
        out = await w.call("Odd", params={name: value})
        assert code(out) == "invalid_argument" and f"'{name}'" in out["error"]["message"], out
    assert await w.untouched()
    # Without them the operation itself is callable: the type is the reason.
    assert (await w.call("Odd"))["ok"] is True


# -- parameter encoding ------------------------------------------------------------


async def test_every_edm_type_is_written_as_its_v2_literal(alice):
    w = World()
    out = await w.call("Typed", params={name: value for name, (_, value, _) in TYPED.items()})
    assert out["ok"] is True, out
    (sent,) = w.sap.requests
    assert dict(sent.url.params) == {
        **{name: literal for name, (_, _, literal) in TYPED.items()}, "$format": "json",
    }
    # An optional parameter that is not given (or null) is not sent at all.
    await w.call("Typed", params={"Count": 3, "Text": None})
    assert dict(w.sap.requests[-1].url.params) == {"Count": "3", "$format": "json"}
    # The forms a model also writes: a number for a text, text for a number.
    await w.call("Typed", params={"Text": 4500000001, "Count": "12", "Flag": "false", "Big": "5"})
    assert dict(w.sap.requests[-1].url.params) == {
        "Text": "'4500000001'", "Flag": "false", "Count": "12", "Big": "5L", "$format": "json",
    }


@pytest.mark.parametrize(
    "crafted",
    [
        "01'&$top=1&x='",
        "01&$format=xml",
        "01#fragment",
        "01/../../../other/Service",
        "01?sap-client=000",
        "01' or '1' eq '1",
        "01%26%24expand%3Dto_User",
        "01;DROP",
        "01é€+ =",
    ],
)
async def test_a_crafted_value_stays_one_parameter_value(alice, crafted):
    w = World()
    out = await w.call("ReleaseItem", params={**RELEASE, "ReleaseCode": crafted})
    assert out["ok"] is True, out
    sent = w.sap.calls[-1]
    # Still this path, still exactly these options, and the value arrives
    # whole, as one quoted literal with its quotes doubled.
    assert sent.url.path == f"{SERVICE_PATH}/ReleaseItem" and sent.url.fragment == ""
    assert sorted(sent.url.params.keys()) == sorted([*RELEASE, "$format"])
    assert sent.url.params["ReleaseCode"] == "'" + crafted.replace("'", "''") + "'"
    assert sent.url.params["$format"] == "json"
    raw = sent.url.query.decode("ascii")
    assert raw.count("&") == 3 and "#" not in raw and "$top" not in raw


def test_the_dialect_builds_the_request_and_refuses_what_is_not_declared():
    op = OperationDef(
        name="ReleaseItem", kind="function_import", http_method="POST", enabled=True,
        parameters=[{"name": "ReleaseCode"}, {"name": "Note", "required": False}],
    )
    dialect = V2Dialect()
    assert dialect.call_request(SERVICE_PATH, op, None, {"ReleaseCode": "01"}) == (
        "POST", f"{SERVICE_PATH}/ReleaseItem", {"ReleaseCode": "'01'", "$format": "json"}, None,
    )
    for key, params in (
        (None, {"ReleaseCode": "01", "Other": "x"}),   # not declared
        ({"Other": "x"}, {"ReleaseCode": "01"}),       # a key field that is no parameter
        ({"ReleaseCode": "02"}, {"ReleaseCode": "01"}),  # twice
        (None, {}),                                    # required
        (None, ["ReleaseCode"]),
    ):
        with pytest.raises(ODataError) as refused:
            dialect.call_request(SERVICE_PATH, op, key, params)
        assert refused.value.code == "invalid_argument"
    assert getattr(V4Dialect(), "supports_call", False) is not True
    assert not hasattr(V4Dialect(), "call_request")


def test_a_call_plan_shows_names_only():
    definition = ServiceDefinition.model_validate(catalogue()["pr"]["definition"])
    client = ODataClient(
        None, {"service_path": SERVICE_PATH, "definition": definition}, V2Dialect()  # type: ignore[arg-type]
    )
    plan = client.check_call(
        definition.operation("ReleaseItem"), params={**RELEASE, "Note": PARAM_VALUE}
    )
    assert isinstance(plan, CallPlan) and plan.fields == (*RELEASE, "Note")
    assert plan.changes is True and plan.operation == "call" and plan.body is None
    for value in (PARAM_VALUE, "10000001", SERVICE_PATH):
        assert value not in repr(plan) and value not in str(plan)
    # The client is the last gate: it refuses a disabled or a foreign operation itself.
    with pytest.raises(ODataError) as refused:
        client.check_call(definition.operation("Off"))
    assert refused.value.code == "operation_disabled"
    foreign = definition.operation("ReleaseItem").model_copy(update={"http_method": "GET"})
    with pytest.raises(ODataError) as refused:
        client.check_call(foreign, params=RELEASE)
    assert refused.value.code == "unknown_target"
    # ... and an ETag where none belongs, or one that is not a single entity tag.
    for name, args in (
        ("CountOpen", {"etag": RAW_ETAG}),
        ("ReleaseItem", {"params": RELEASE, "etag": RAW_ETAG}),
        ("ItemStatus", {"key": KEY, "etag": RAW_ETAG}),
        ("Approve", {"key": KEY, "etag": "*"}),
        ("Approve", {"key": KEY, "etag": f"{RAW_ETAG}, {RAW_ETAG}"}),
    ):
        with pytest.raises(ODataError) as refused:
            client.check_call(definition.operation(name), **args)
        assert refused.value.code == "invalid_argument", name
    plan = client.check_call(definition.operation("Approve"), key=KEY, etag=RAW_ETAG)
    assert plan.etag == RAW_ETAG
    v4 = ODataClient(
        None, {"service_path": SERVICE_PATH, "definition": definition}, V4Dialect()  # type: ignore[arg-type]
    )
    with pytest.raises(ODataError) as refused:
        v4.check_call(definition.operation("CountOpen"))
    assert refused.value.code == "operation_disabled"


# -- bound operations --------------------------------------------------------------


async def test_bound_operation_checks_the_key_of_its_entity_set(alice):
    w = World()
    for key in (None, {}, {"PurchaseRequisition": "10000001"}, {**KEY, "Extra": "1"},
                {**KEY, "PurchaseRequisitionItem": "00010/x"}, {**KEY, "PurchaseRequisition": ""},
                {**KEY, "PurchaseRequisition": {"a": 1}}):
        out = await w.call("Approve", key=key)
        assert code(out) == "invalid_key", (key, out)
    # The key fields are parameters of a bound V2 function import: once, in 'key'.
    out = await w.call("Approve", key=KEY, params={"PurchaseRequisition": "10000001"})
    assert code(out) == "invalid_argument" and "'key' only" in out["error"]["message"]
    # Nothing is sent under a name the catalogue's parameter list does not declare.
    out = await w.call("BoundWithoutKeyParams", key=KEY, params={"Comment": "x"})
    assert code(out) == "invalid_argument" and "declares no parameter" in out["error"]["message"]
    assert await w.untouched()

    w.sap.answer = {"d": entity()}
    out = await w.call("Approve", key=KEY, params={"Comment": PARAM_VALUE})
    assert out["ok"] is True and out["returned"] == "entity"
    sent = w.sap.calls[-1]
    assert sent.method == "POST" and sent.url.path == f"{SERVICE_PATH}/Approve"
    assert dict(sent.url.params) == {
        "PurchaseRequisition": "'10000001'",
        "PurchaseRequisitionItem": "'00010'",
        "Comment": f"'{PARAM_VALUE}'",
        "$format": "json",
    }
    (row,) = await all_rows()
    assert (row.operation, row.target, row.outcome) == ("call", "Approve", "ok")
    assert json.loads(row.key_json) == KEY  # the key of the bound entity
    assert json.loads(row.body_fields_json) == ["Comment"]  # names, and not the key's
    assert PARAM_VALUE not in (row.body_fields_json or "") + (row.key_json or "")


async def test_a_bound_call_takes_an_etag_handle_and_never_a_raw_etag(alice):
    w = World()
    # A raw ETag, a made-up handle and another entity's handle are all refused.
    got = await w.run(service="pr", target=ITEM, operation="get", key=KEY)
    handle = got["etag"]
    assert handle.startswith("h-") and RAW_ETAG not in json.dumps(got)
    other = {**KEY, "PurchaseRequisitionItem": "00020"}
    before = len(w.sap.requests)
    for etag, key in ((RAW_ETAG, KEY), ("h-" + "x" * 32, KEY), ("*", KEY), (handle, other)):
        out = await w.call("Approve", key=key, etag=etag)
        assert code(out) == "invalid_etag", (etag, out)
    with signed_in(BOB):
        assert code(await w.call("Approve", key=KEY, etag=handle)) == "invalid_etag"
    assert len(w.sap.requests) == before and await all_rows() == []
    # The handle of this entity is translated to If-Match, as for an update.
    out = await w.call("Approve", key=KEY, etag=handle)
    assert out["ok"] is True and "etag" not in out
    assert w.sap.calls[-1].headers["If-Match"] == RAW_ETAG
    # The version it stood for is gone with the call.
    assert code(await w.call("Approve", key=KEY, etag=handle)) == "invalid_etag"


async def test_sap_demanding_an_etag_is_etag_required(alice):
    w = World()
    w.sap.answer = httpx.Response(428, json={"error": {"code": "X", "message": {"value": "need"}}})
    out = await w.call("Approve", key=KEY)
    assert code(out) == "etag_required" and "'get'" in out["error"]["hint"]
    (row,) = await all_rows()
    assert (row.outcome, row.phase, row.http_status) == ("sap_error", "write", 428)


# -- what a call returns -----------------------------------------------------------


@pytest.mark.parametrize("wrapped", [False, True])
async def test_an_entity_of_the_bound_set_is_cut_to_its_selectable_fields(alice, wrapped):
    w = World(READ_ONLY)
    row = entity()
    w.sap.answer = {"d": {"ItemStatus": row} if wrapped else row}
    out = await w.call("ItemStatus", key=KEY)
    assert out["returned"] == "entity" and out["result"] == {
        **KEY, "PurReqnReleaseStatus": "05", "Plant": "1000",
    }
    text = json.dumps(out)
    assert SECRET_USER not in text and "__metadata" not in text and "etag" not in text
    assert "2026-01-01" not in text and "s4.internal" not in text


async def test_a_collection_of_the_bound_set_is_cut_row_by_row(alice):
    w = World(READ_ONLY)
    rows = [entity(), entity({**KEY, "PurchaseRequisitionItem": "00020"})]
    for answer in ({"d": {"results": rows}}, {"d": rows}, {"d": {"ItemStatus": {"results": rows}}}):
        w.sap.answer = answer
        out = await w.call("ItemStatus", key=KEY)
        assert out["returned"] == "entities" and len(out["result"]) == 2
        assert all(set(r) == {*KEY, "PurReqnReleaseStatus", "Plant"} for r in out["result"])
        assert SECRET_USER not in json.dumps(out)


@pytest.mark.parametrize(
    "answer",
    [
        {"d": entity(type_name="API_PURCHASEREQ_PROCESS_SRV.A_SupplierType")},  # another type
        {"d": entity(type_name=None)},  # no stated type
        {"d": {"results": [entity(), entity(type_name="OTHER.Type")]}},  # one row is not
        {"d": {"results": [entity(), "text"]}},
        {"d": {"ItemStatus": {"__metadata": {"type": "API.Address"}, "Street": SECRET_USER}}},
        {"d": {"Street": SECRET_USER, "City": "x"}},  # a complex value, unwrapped
        {"d": {"results": [SECRET_USER, "b"]}},  # a list of values
        {"d": [SECRET_USER]},
    ],
)
async def test_what_cannot_be_tied_to_the_bound_entity_set_is_withheld(alice, answer):
    w = World(READ_ONLY)
    w.sap.answer = answer
    out = await w.call("ItemStatus", key=KEY)
    assert out == {
        "ok": True, "status": 200, "returned": "withheld", "result": None, "truncated": False,
    }


async def test_no_raw_entity_data_without_a_catalogue_set(alice):
    """Unbound, bound to a set nobody may read, or to one without a type: confirmation only."""
    w = World()
    for target, args in (
        ("CountOpen", {}),                      # not bound
        ("ReleaseItem", {"params": RELEASE}),   # not bound, changes data
        ("HiddenItem", {"key": KEY}),           # bound to a set with no read enabled
        ("Untyped", {"key": KEY}),              # bound to a set whose type is not known
    ):
        for answer in ({"d": entity()}, {"d": {"results": [entity()]}},
                       {"d": {target: entity()}}):
            w.sap.answer = answer
            out = await w.call(target, **args)
            assert out["ok"] is True and out["returned"] == "withheld", (target, out)
            assert out["result"] is None
            text = json.dumps(out)
            assert SECRET_USER not in text and "10000001" not in text and "1000" not in text
    # A set without a type is tied to nothing, also not to an entity without one.
    w.sap.answer = {"d": entity(type_name="")}
    out = await w.call("Untyped", key=KEY)
    assert (out["returned"], out["result"]) == ("withheld", None)


async def test_a_value_is_returned_by_a_reading_call_only(alice):
    w = World()
    for value in (7, "05", True, 1.5, ""):
        for answer in ({"d": {"CountOpen": value}}, {"d": value}):
            w.sap.answer = answer
            out = await w.call("CountOpen")
            assert (out["returned"], out["result"]) == ("value", value), (answer, out)
    for answer in ({"d": None}, {"d": {"CountOpen": None}}, httpx.Response(204),
                   httpx.Response(200, content=b"")):
        w.sap.answer = answer
        out = await w.call("CountOpen")
        assert (out["ok"], out["returned"], out["result"]) == (True, "nothing", None)
    # A call that changes data answers with the confirmation, whatever SAP sent.
    w.sap.answer = {"d": {"ReleaseItem": "DOC-4711"}}
    out = await w.call("ReleaseItem", params=RELEASE)
    assert out == {
        "ok": True, "status": 200, "returned": "withheld", "result": None, "truncated": False,
    }


async def test_a_long_value_is_cut_and_says_so(alice):
    from agents.odata.tools import MAX_RESULT_CHARS

    w = World(READ_ONLY)
    # A text that long is no scalar answer (a serialised document): not shown at all.
    w.sap.answer = {"d": {"CountOpen": "x" * (MAX_RESULT_CHARS * 2)}}
    out = await w.call("CountOpen")
    assert (out["returned"], out["result"], out["truncated"]) == ("withheld", None, False)
    w.sap.answer = {"d": {"results": [entity() for _ in range(2000)]}}
    out = await w.call("ItemStatus", key=KEY)
    assert out["truncated"] is True and 0 < len(out["result"]) < 2000
    assert len(json.dumps(out)) <= MAX_RESULT_CHARS


# -- success is recognised, failure is told apart -----------------------------------


LOGON_PAGE = httpx.Response(
    200,
    headers={"content-type": "text/html", "set-cookie": "sap-login-XSRF=abc; path=/"},
    text="<html><form action='/sap/bc/sec/login'>Log on</form></html>",
)


@pytest.mark.parametrize(
    "answer",
    [
        LOGON_PAGE,
        httpx.Response(200, json={"error": {"code": "X", "message": {"value": "no"}}}),
        httpx.Response(200, json={"d": {}, "error": {"code": "X"}}),
        httpx.Response(200, json=["d"]),
        httpx.Response(200, json={"value": 1}),  # not a V2 answer
        httpx.Response(200, text="OK"),
        httpx.Response(202),
        httpx.Response(204, text="unexpected"),
    ],
)
async def test_a_logon_page_at_200_is_not_the_success_of_a_changing_call(alice, answer):
    w = World()
    w.sap.answer = answer
    out = await w.call("ReleaseItem", params=RELEASE)
    assert code(out) == "write_outcome_unknown", out
    assert "read the affected record" in out["error"]["hint"]
    assert "Log on" not in json.dumps(out) and "/sap/bc" not in json.dumps(out)
    assert len(w.sap.calls) == 1  # not sent again
    (row,) = await all_rows()
    assert (row.operation, row.outcome, row.phase) == ("call", "unknown", "write")
    # The session the page answered for is ended: the next call fetches a fresh token.
    w.sap.answer = None
    assert (await w.call("ReleaseItem", params=RELEASE))["ok"] is True
    assert len(w.sap.fetches) == 2


async def test_a_logon_page_at_200_is_not_the_answer_of_a_reading_call(alice):
    w = World(READ_ONLY)
    for answer in (LOGON_PAGE, httpx.Response(200, text="7"),
                   httpx.Response(200, json={"error": {"code": "X", "message": {"value": "no"}}}),
                   httpx.Response(200, json={"d": {"CountOpen": 7}, "error": {"code": "X"}}),
                   httpx.Response(200, json={"value": 7}), httpx.Response(200, json=[7])):
        w.sap.answer = answer
        out = await w.call("CountOpen")
        assert code(out) == "sap_error", out
        assert "Log on" not in json.dumps(out)
    w.sap.answer = httpx.Response(302, headers={"location": "https://evil.example/x"})
    assert code(await w.call("CountOpen")) == "sap_error"
    assert all(r.url.host == "s4.internal" for r in w.sap.requests)  # no redirect followed


async def test_a_sap_error_of_a_changing_call_is_recorded_and_not_repeated(alice):
    w = World()
    w.sap.answer = v2_error(400, "ME/123", "Release not possible via s4.internal:44300/sap/x")
    out = await w.call("ReleaseItem", params=RELEASE)
    assert code(out) == "sap_error" and out["error"]["message"].startswith("ME/123: ")
    assert "s4.internal" not in json.dumps(out)
    assert len(w.sap.calls) == 1
    (row,) = await all_rows()
    assert (row.outcome, row.phase, row.http_status) == ("sap_error", "write", 400)


@pytest.mark.parametrize(
    "target, args", [("ReleaseItem", {"params": RELEASE}), ("Recalculate", {})]
)
async def test_a_changing_call_is_never_sent_again_after_a_transport_error(alice, target, args):
    w = World()

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection reset https://s4.internal:44300/sap?x=1")

    w.sap.answer = broken
    out = await w.call(target, **args)
    assert code(out) == "write_outcome_unknown", out
    assert "cannot be undone" in out["error"]["hint"] and "s4.internal" not in json.dumps(out)
    assert len(w.sap.calls) == 1  # a GET that changes data is not retried either
    (row,) = await all_rows()
    assert (row.operation, row.outcome, row.phase, row.http_status) == (
        "call", "unknown", "write", None,
    )


async def test_a_connection_that_was_never_made_changed_nothing(alice):
    w = World()

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    w.sap.answer = refused
    out = await w.call("ReleaseItem", params=RELEASE)
    assert code(out) == "destination_error" and "nothing was changed" in out["error"]["message"]
    (row,) = await all_rows()
    assert (row.outcome, row.phase) == ("refused", "token")


async def test_a_stale_csrf_token_is_renewed_exactly_once(alice):
    w = World()
    assert (await w.call("ReleaseItem", params=RELEASE))["ok"] is True
    # SAP forgets the session: the next call is refused unprocessed, once.
    w.sap.issued.clear()
    assert (await w.call("ReleaseItem", params=RELEASE))["ok"] is True
    assert len(w.sap.fetches) == 2 and len(w.sap.calls) == 3 and len(w.sap.crossed) == 1
    # Refused again after the renewal: the error is the answer, no third send.
    always = httpx.Response(403, headers={"x-csrf-token": "Required"}, text="CSRF")
    w.sap.answer = always
    before = len(w.sap.calls)
    out = await w.call("ReleaseItem", params=RELEASE)
    assert code(out) == "sap_error" and len(w.sap.calls) == before + 2
    rows = await all_rows()
    assert [r.outcome for r in rows] == ["ok", "ok", "sap_error"]


async def test_a_cancelled_changing_call_is_recorded_as_cancelled(alice):
    w = World()
    arrived, release = asyncio.Event(), asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        arrived.set()
        await release.wait()
        return httpx.Response(204)

    w.sap.answer = slow
    task = asyncio.ensure_future(w.call("ReleaseItem", params=RELEASE))
    await arrived.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (row,) = await all_rows()
    assert (row.operation, row.outcome, row.phase) == ("call", "cancelled", "write")


async def test_no_signed_in_user_is_refused_before_anything_is_recorded_or_sent():
    w = World()
    for target, args in (("ReleaseItem", {"params": RELEASE}), ("Approve", {"key": KEY}),
                         ("Recalculate", {})):
        out = await w.call(target, **args)
        assert code(out) == "no_user", (target, out)
    assert w.sap.requests == [] and w.built == [] and await all_rows() == []
    assert code(await w.call("CountOpen")) == "no_user" and w.sap.requests == []
    # The technical-user twin of the same agent runs, as the destination's user.
    out = await w.call("ReleaseItem", service="pr-jobs", params=RELEASE)
    assert out["ok"] is True
    assert w.sap.calls[-1].headers["Authorization"] == "Bearer dest-token"
    (row,) = await all_rows()
    assert row.sent_as == "technical:S4_ODATA_TECH" and row.token_digest is None


async def test_two_users_calling_at_the_same_time_through_one_toolset():
    w = World()
    barrier = asyncio.Barrier(2)

    async def gate(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            await asyncio.wait_for(barrier.wait(), 5)  # both calls are in flight at once
        return httpx.Response(204)

    w.sap.answer = gate

    async def as_user(principal: str, item: str) -> dict:
        with signed_in(principal):
            return await w.call(
                "ReleaseItem", params={**RELEASE, "PurchaseRequisitionItem": item}
            )

    outs = await asyncio.gather(as_user(ALICE, "00010"), as_user(BOB, "00020"))
    assert all(out["ok"] is True for out in outs), outs
    assert w.sap.crossed == []  # nobody's token or cookie travelled with the other's call
    sent = {r.headers["Authorization"]: r for r in w.sap.calls}
    for principal, item in ((ALICE, "00010"), (BOB, "00020")):
        request = sent[f"Bearer user-token-of-{principal}"]
        assert request.headers["X-CSRF-Token"] == f"T-user-token-of-{principal}"
        assert request.headers["Cookie"] == f"{COOKIE_NAME}=S-user-token-of-{principal}"
        assert request.url.params["PurchaseRequisitionItem"] == f"'{item}'"
    rows = await all_rows()
    assert {r.run_principal for r in rows} == {ALICE, BOB}
    assert len({r.token_digest for r in rows}) == 2 and len({r.call_id for r in rows}) == 2
    assert [r.outcome for r in rows] == ["ok", "ok"]


# -- search offers exactly what execute runs ----------------------------------------

_SWITCH_CODES = {
    "unknown_service", "service_disabled", "unknown_target", "operation_disabled",
    "write_not_allowed", "not_available", "audit_not_configured",
}
CHANGING = {"ReleaseItem", "Recalculate", "PostMarkedReading", "Approve", "BoundWithoutKeyParams"}
READING = {"CountOpen", "ItemStatus", "HiddenItem", "Untyped", "Typed", "Odd"}


@pytest.mark.parametrize(
    "allow_write, recorded, expected",
    [
        (True, True, CHANGING | READING),
        (False, True, READING),
        (True, False, READING),  # no record, no write: not offered either
        ("true", True, READING),  # the switch is exactly True
    ],
)
async def test_whatever_search_returns_execute_does_not_refuse_by_a_switch(
    alice, allow_write, recorded, expected
):
    oauth = {"services": ["pr", "pr-jobs", "pr-v4"], "allow_write": allow_write}
    w = World(oauth, recorder=... if recorded else None)
    offered: list[tuple[str, str, str]] = []
    operations: dict[str, set[str]] = {}
    for service in ("pr", "pr-jobs", "pr-v4"):
        found = await w.search(query="", service=service)
        assert found["total"] == len(found["matches"]) > 0
        for match in found["matches"]:
            assert match["operations"], match
            offered += [(service, match["target"], op) for op in match["operations"]]
            if match["kind"] == "operation":
                assert match["operations"] == ["call"]
                operations.setdefault(service, set()).add(match["target"])
    # Exactly the callable ones, per service; a V4 operation is never offered.
    assert operations == {"pr": expected, "pr-jobs": expected}
    for service, target, operation in offered:
        out = await w.run(service=service, target=target, operation=operation)
        assert code(out) not in _SWITCH_CODES, (service, target, operation, out)
    # ... and nothing else can be called: every other operation is refused by a switch.
    for service in ("pr", "pr-jobs"):
        for target in (CHANGING | READING | {"Off", "OffReading"}) - expected:
            out = await w.call(target, service=service)
            assert code(out) in _SWITCH_CODES, (service, target, out)
    for target in ("Release", "CountOpen"):
        out = await w.call(target, service="pr-v4")
        expected_code = (
            "not_available"
            if target == "CountOpen" or (allow_write is True and recorded)
            else "write_not_allowed"
            if allow_write is not True
            else "audit_not_configured"
        )
        assert code(out) == expected_code, (target, out)


async def test_search_shows_parameters_and_what_a_call_changes(alice):
    w = World()
    found = await w.search(query="ReleaseItem", detail="full", service="pr")
    match = next(m for m in found["matches"] if m["target"] == "ReleaseItem")
    assert match["kind"] == "operation" and match["operations"] == ["call"]
    assert match["changes_data"] is True and match["bound_to"] is None
    assert match["parameters"] == [
        {"name": "PurchaseRequisition", "type": "Edm.String", "required": True},
        {"name": "PurchaseRequisitionItem", "type": "Edm.String", "required": True},
        {"name": "ReleaseCode", "type": "Edm.String", "required": True},
        {"name": "Note", "type": "Edm.String", "required": False},
    ]
    by_target = {
        m["target"]: m
        for query in ("ItemStatus", "HiddenItem", "CountOpen", "Approve")
        for m in (await w.search(query=query, detail="full", service="pr"))["matches"]
    }
    assert by_target["ItemStatus"]["bound_to"] == ITEM  # a set this agent can read
    assert by_target["HiddenItem"]["bound_to"] is None  # one it cannot see is not named
    assert by_target["CountOpen"]["changes_data"] is False
    assert by_target["Approve"]["changes_data"] is True
    assert "A_Hidden" not in json.dumps(by_target)


def test_search_catalogue_lists_operations_by_call_version_and_switch():
    services = [{**service, "name": name} for name, service in catalogue().items()]

    def operations(**kw: Any) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for service in ("pr", "pr-jobs", "pr-v4"):
            found = search_catalogue(services, "", service=service, **kw)
            assert found["total"] == len(found["matches"])
            for match in found["matches"]:
                if match["kind"] == "operation":
                    out.setdefault(match["service"], set()).add(match["target"])
        return out

    assert operations(allow_write=True) == {}  # the shipped default: none
    assert operations(allow_write=True, allow_call=True, call_versions=()) == {}
    v2 = {"allow_call": True, "call_versions": {"v2"}}
    assert operations(**v2) == {"pr": READING, "pr-jobs": READING}
    assert operations(allow_write=True, **v2) == {
        "pr": READING | CHANGING, "pr-jobs": READING | CHANGING,
    }
    # The entry's switch decides, not whether entities of that version can be written.
    assert operations(allow_write=True, write_versions=(), **v2)["pr"] == READING | CHANGING
    assert operations(allow_write="yes", **v2)["pr"] == READING


async def test_the_tool_description_explains_calls():
    w = World()
    tool = w.toolset.tools["execute_operation"].tool_def
    text = " ".join((tool.description or "").split()) + " " + " ".join(
        " ".join(str(p.get("description", "")).split())
        for p in tool.parameters_json_schema["properties"].values()
    )
    assert "CANNOT BE UNDONE" in text and "'call'" in text
    assert "never repeat a call without first reading the affected record" in text
    assert "named exactly as search_operations lists them" in text
    assert "'withheld'" in text and '"returned"' in text and "'bound_to'" in text
    search = " ".join((w.toolset.tools["search_operations"].tool_def.description or "").split())
    schema = w.toolset.tools["search_operations"].tool_def.parameters_json_schema
    assert "'changes_data'" in search + json.dumps(schema)


async def test_refused_changing_calls_are_logged_without_values(alice, caplog):
    import logging

    def lines() -> list[str]:
        return [r.getMessage() for r in caplog.records
                if r.name == "agents.odata.audit" and r.levelno == logging.WARNING]

    secret = {**RELEASE, "ReleaseCode": PARAM_VALUE}
    with caplog.at_level(logging.DEBUG, logger="agents"):
        for w, target, expected in (
            (World(READ_ONLY), "ReleaseItem", "write_not_allowed"),
            (World(recorder=None), "ReleaseItem", "audit_not_configured"),
            (World(), "Off", "operation_disabled"),
        ):
            before = len(lines())
            assert code(await w.call(target, params=secret)) == expected
            (line,) = lines()[before:]
            assert f"target='{target}'" in line and f"operation=call code={expected}" in line
        # A reading call that is refused is no write attempt.
        before = len(lines())
        assert code(await World().call("OffReading")) == "operation_disabled"
        assert lines()[before:] == []
    assert PARAM_VALUE not in caplog.text


def test_the_catalogue_of_this_suite_is_not_shared_between_tests():
    first, second = catalogue(), catalogue()
    first["pr"]["definition"]["operations"].clear()
    assert second["pr"]["definition"]["operations"] and copy.deepcopy(second) == second


# -- the declared return entity set (task M1) ----------------------------------------

SUPPLIER = "A_Supplier"
SUPPLIER_TYPE = "API_PURCHASEREQ_PROCESS_SRV.A_SupplierType"


def returning() -> dict[str, dict]:
    """Operations that say what they return, over sets with different reads."""

    def returns(name: str, many: bool) -> dict[str, Any]:
        return {"returns": {"entity_set": name, "collection": many}}

    reading = {"changes_data": False}
    return catalogue(
        [
            _op("OpenItems", "GET", **reading, **returns(ITEM, True)),
            _op("OneItem", "GET", **reading, **returns(ITEM, False)),
            _op("PostItem", "POST", **returns(ITEM, False)),
            _op("ManyFromGetOnly", "GET", **reading, **returns("A_GetOnly", True)),
            _op("OneFromGetOnly", "GET", **reading, **returns("A_GetOnly", False)),
            _op("OneFromListOnly", "GET", **reading, **returns("A_ListOnly", False)),
            _op("ManyFromListOnly", "GET", **reading, **returns("A_ListOnly", True)),
            _op("FromHidden", "GET", **reading, **returns("A_Hidden", False)),
            _op("FromUntyped", "GET", **reading, **returns("A_Untyped", False)),
            # Bound to an item (the KEY), returns a supplier (the RESULT).
            _op("SupplierOfItem", "GET", **reading, bound_to=ITEM, parameters=KEY_PARAMS,
                **returns(SUPPLIER, False)),
            _op("CountOpen", "GET", **reading),
        ],
        [
            _entity_set("A_GetOnly", ["get"]),
            _entity_set("A_ListOnly", ["list"]),
            {**_entity_set(SUPPLIER, ["list", "get"], entity_type=SUPPLIER_TYPE),
             "fields": [
                 {"name": "PurchaseRequisition", "selectable": True},
                 {"name": "PurchaseRequisitionItem", "selectable": True},
                 {"name": "Plant"},  # not released for a supplier
                 {"name": "PurReqnReleaseStatus"},
             ]},
        ],
    )


CUT = {**KEY, "PurReqnReleaseStatus": "05", "Plant": "1000"}
WITHHELD = {"ok": True, "status": 200, "returned": "withheld", "result": None, "truncated": False}


async def test_an_unbound_call_returns_entities_of_its_declared_set_cut_like_a_read(alice):
    w = World(READ_ONLY, services=returning())
    rows = [entity(), entity({**KEY, "PurchaseRequisitionItem": "00020"})]
    for answer in ({"d": {"results": rows}}, {"d": {"OpenItems": {"results": rows}}}):
        w.sap.answer = answer
        out = await w.call("OpenItems")
        assert out["returned"] == "entities" and out["result"][0] == CUT, out
        assert len(out["result"]) == 2
        text = json.dumps(out)
        assert SECRET_USER not in text and "__metadata" not in text and "etag" not in text
    for answer in ({"d": entity()}, {"d": {"OneItem": entity()}}):
        w.sap.answer = answer
        out = await w.call("OneItem")
        assert (out["returned"], out["result"]) == ("entity", CUT), out
        assert SECRET_USER not in json.dumps(out) and "s4.internal" not in json.dumps(out)


async def test_a_changing_call_shows_the_entity_of_its_declared_set(alice):
    w = World(services=returning())
    w.sap.answer = {"d": entity()}
    out = await w.call("PostItem")
    assert (out["ok"], out["returned"], out["result"]) == (True, "entity", CUT), out
    w.sap.answer = {"d": {"PostItem": "DOC-4711"}}  # still never a scalar
    assert await w.call("PostItem") == WITHHELD


@pytest.mark.parametrize(
    "target, answer",
    [
        # Every entity must say it is of the declared set's type.
        ("OneItem", {"d": entity(type_name=SUPPLIER_TYPE)}),
        ("OneItem", {"d": entity(type_name=None)}),
        ("OpenItems", {"d": {"results": [entity(), entity(type_name=SUPPLIER_TYPE)]}}),
        # One was declared and many came, or the other way round.
        ("OneItem", {"d": {"results": [entity()]}}),
        ("OpenItems", {"d": entity()}),
        # The read that fits the shape is not enabled on the declared set.
        ("ManyFromGetOnly", {"d": {"results": [entity()]}}),
        ("OneFromListOnly", {"d": entity()}),
        ("FromHidden", {"d": entity()}),
        ("FromUntyped", {"d": entity(type_name="")}),
        # Not an entity at all.
        ("OneItem", {"d": {"Street": SECRET_USER}}),
        ("OpenItems", {"d": {"results": [SECRET_USER]}}),
    ],
)
async def test_a_result_that_does_not_fit_the_declared_set_is_withheld(alice, target, answer):
    w = World(READ_ONLY, services=returning())
    w.sap.answer = answer
    assert await w.call(target) == WITHHELD


async def test_the_read_that_fits_the_shape_is_enough(alice):
    w = World(READ_ONLY, services=returning())
    w.sap.answer = {"d": entity()}
    assert (await w.call("OneFromGetOnly"))["returned"] == "entity"
    w.sap.answer = {"d": {"results": [entity()]}}
    assert (await w.call("ManyFromListOnly"))["returned"] == "entities"


async def test_returns_decides_the_result_and_bound_to_the_key(alice):
    w = World(READ_ONLY, services=returning())
    w.sap.answer = {"d": entity(type_name=SUPPLIER_TYPE)}
    out = await w.call("SupplierOfItem", key=KEY)
    # The key is the item's; the result is cut to the SUPPLIER's selectable fields.
    assert (out["returned"], out["result"]) == ("entity", KEY), out
    assert w.sap.calls[-1].url.params["PurchaseRequisitionItem"] == "'00010'"
    # An entity of the bound set's type is not what was declared.
    w.sap.answer = {"d": entity()}
    assert await w.call("SupplierOfItem", key=KEY) == WITHHELD
    out = await w.call("SupplierOfItem")
    assert code(out) == "invalid_key"


@pytest.mark.parametrize(
    "value, shown",
    [
        (7, 7), (1.5, 1.5), (True, True), (False, False), ("", ""), ("05", "05"),
        ("x" * 1000, "x" * 1000),
        ('{"a": 1}', '{"a": 1}'),  # one short line is a value, whatever it spells
        ("  padded \n", "padded"),
    ],
)
async def test_a_scalar_of_a_reading_call_is_a_number_a_boolean_or_one_short_line(
    alice, value, shown
):
    w = World(READ_ONLY, services=returning())
    w.sap.answer = {"d": {"CountOpen": value}}
    out = await w.call("CountOpen")
    assert (out["returned"], out["result"]) == ("value", shown), out
    assert type(out["result"]) is type(shown)


@pytest.mark.parametrize(
    "value",
    [
        "x" * 1001,
        "line one\nline two",
        '{\n  "CreatedByUser": "' + SECRET_USER + '"\n}',  # serialised JSON
        "<Item>\r\n<User>" + SECRET_USER + "</User></Item>",
        "tab\tseparated",
        "zero\u200bwidth",
        "bidi\u202eoverride",
        "line\u2028separator",
    ],
)
async def test_a_text_that_is_not_one_short_line_is_withheld(alice, value):
    w = World(READ_ONLY, services=returning())
    for answer in ({"d": {"CountOpen": value}}, {"d": value}):
        w.sap.answer = answer
        out = await w.call("CountOpen")
        assert out == WITHHELD
        assert SECRET_USER not in json.dumps(out)
