"""`execute_operation` for writes (Task W3): the two switches and the ETag handle.

SAP is an ``httpx.MockTransport`` behind the real ``destination_http_client``.
The double knows which CSRF token and session cookie it gave to which caller
(the caller being whoever the destination's ``Authorization`` header names),
so a token or cookie that travels with the wrong identity shows as a
crossing, and ``sap.requests == []`` proves a refusal came before any request.

No network, no database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
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

from agents.auth import current_claims, current_jwt, current_principal  # noqa: E402
from agents.odata import tools as tools_module  # noqa: E402
from agents.odata import urls as urls_module  # noqa: E402
from agents.odata.models import validate_odata_service  # noqa: E402
from agents.odata.tools import WriteAudit, odata_toolset  # noqa: E402
from tests.odata_helpers import FakeResolver, v2_error  # noqa: E402

ALICE = "alice@example.com"
BOB = "bob@example.com"
ITEM = "A_PurchaseRequisitionItem"
SERVICE_PATH = "/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV"
COOKIE_NAME = "SAP_SESSIONID_XXX_100"
KEY = {"PurchaseRequisition": "10000001", "PurchaseRequisitionItem": "00010"}
OTHER_KEY = {"PurchaseRequisition": "10000001", "PurchaseRequisitionItem": "00020"}
KEYED = f"{SERVICE_PATH}/{ITEM}(PurchaseRequisition='10000001',PurchaseRequisitionItem='00010')"
# The ETag of a Gateway entity is often its last-changed timestamp.
STAMP = "2026-01-01T00%3A00%3A00Z"
RAW_ETAG = f"W/\"datetimeoffset'{STAMP}'\""
NEW_STAMP = "2026-02-02T02%3A02%3A02Z"
NEW_ETAG = f"W/\"datetimeoffset'{NEW_STAMP}'\""
SECRET_USER = "JDOE-HIDDEN"
CTX = SimpleNamespace(run_id="run-1")
ALL_OPS = ["list", "get", "create", "update", "delete"]


def _field(name: str, **kw: Any) -> dict[str, Any]:
    return {"name": name, **kw}


def _entity_set(name: str, operations: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "keys": [{"name": "PurchaseRequisition"}, {"name": "PurchaseRequisitionItem"}],
        "operations": operations,
        "fields": [
            _field("PurchaseRequisition", selectable=True, filterable=True, writable=True),
            _field("PurchaseRequisitionItem", selectable=True, writable=True),
            _field("PurReqnReleaseStatus", selectable=True, filterable=True),
            _field("RequestedQuantity", selectable=True, writable=True),
            _field("Plant", selectable=True, writable=True),
            _field("CreatedByUser", personal_data=True),
        ],
        "navigations": [{"name": "to_Twin", "target": "A_Twin", "collection": False}],
    }


def snapshot(
    *, update_enabled: bool = True, operations: list[str] | None = None, user_context: bool = True
) -> dict[str, dict]:
    """The catalogue: service ``pr`` (and its technical twin ``pr-jobs``)."""
    if operations is None:
        operations = ["list", "get", "update"] if update_enabled else ["list", "get"]
    definition = {
        "entity_sets": [
            _entity_set(ITEM, operations),
            _entity_set("A_Twin", ALL_OPS),
            _entity_set("A_ReadOnly", ["list", "get"]),
        ],
        "operations": [
            {
                "name": "Release",
                "kind": "function_import",
                "http_method": "POST",
                "enabled": True,
                "changes_data": True,
            },
        ],
    }
    out = {}
    for name, destination, as_user in (
        ("pr", "S4_ODATA_USER", user_context),
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
    return out


def _row(key: dict[str, str] = KEY, etag: str = RAW_ETAG) -> dict[str, Any]:
    return {
        "d": {
            "__metadata": {"uri": "https://s4.internal:44300" + KEYED, "etag": etag},
            **key,
            "PurReqnReleaseStatus": "B",
            "RequestedQuantity": "5",
            "Plant": "1000",
            "CreatedByUser": SECRET_USER,
        }
    }


class Sap:
    """SAP for reads and writes: one session (token + cookie) per caller."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.issued: dict[str, tuple[str, str]] = {}
        self.crossed: list[str] = []
        self.write_answer: Any = None  # a Response, or a callable(request)
        self.read_answer: Any = None

    @property
    def writes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method != "GET"]

    @property
    def fetches(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.headers.get("X-CSRF-Token") == "Fetch"]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
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
        if request.method == "GET":
            answer = self.read_answer
            if callable(answer):
                answer = answer(request)
            if isinstance(answer, httpx.Response):
                return answer
            return httpx.Response(200, json=answer or _row())
        token = request.headers.get("X-CSRF-Token", "")
        cookie = request.headers.get("Cookie", "")
        expected = self.issued.get(caller)
        if not (expected and token == expected[0] and f"{COOKIE_NAME}={expected[1]}" in cookie):
            self.crossed.append(caller)
            return httpx.Response(403, headers={"x-csrf-token": "Required"}, text="CSRF")
        answer = self.write_answer
        if callable(answer):
            answer = answer(request)
            if asyncio.iscoroutine(answer):
                answer = await answer
        if isinstance(answer, httpx.Response):
            return answer
        if request.method == "POST" and "X-HTTP-Method" not in request.headers:
            return httpx.Response(201, json=_row(etag=NEW_ETAG))
        return httpx.Response(204)


class World:
    """One toolset on the mock SAP, with the audit hook recorded."""

    def __init__(
        self,
        oauth: dict[str, Any] | None = None,
        catalogue: dict[str, dict] | None = None,
        **kw: Any,
    ) -> None:
        self.sap = Sap()
        self.built: list[str] = []
        self.resolved: list[tuple[str, str | None]] = []
        self.audits: list[WriteAudit] = []
        world = self

        class Recording(FakeResolver):
            async def resolve(self, *, force=False, user_token=None, principal=None):
                world.resolved.append((self.name, user_token))
                return await super().resolve(
                    force=force, user_token=user_token, principal=principal
                )

        def factory(destination: str) -> FakeResolver:
            world.built.append(destination)
            return Recording(name=destination)

        async def on_write(record: WriteAudit) -> None:
            world.audits.append(record)

        kw.setdefault("on_write", on_write)
        kw.setdefault("resolver_factory", factory)
        self.toolset = odata_toolset(
            oauth if oauth is not None else {"services": ["pr", "pr-jobs"], "allow_write": True},
            auth_mode="destination",
            services=catalogue or snapshot(),
            agent_name="buyer",
            transport=httpx.MockTransport(self.sap.handler),
            **kw,
        )

    async def run(self, **args: Any) -> dict:
        call = {"service": "pr", "target": ITEM, **args}
        return await self.toolset.tools["execute_operation"].function(CTX, **call)

    async def search(self, **args: Any) -> dict:
        return await self.toolset.tools["search_operations"].function(**args)

    @property
    def nothing_sent(self) -> bool:
        return self.sap.requests == [] and self.resolved == [] and self.audits == []


class signed_in:
    """Bind a signed-in user for the current task, as the JWT middleware does."""

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


UPDATE = {"operation": "update", "key": KEY, "body": {"RequestedQuantity": "7"}}


async def run_update(catalogue: dict[str, dict], oauth: dict[str, Any]) -> tuple[dict, World]:
    w = World(oauth, catalogue)
    return await w.run(**UPDATE), w


# -- the two switches ---------------------------------------------------------


@pytest.mark.parametrize(
    "catalogue_op, allow_write, expected",
    [
        (False, False, "operation_disabled"),
        (False, True, "operation_disabled"),
        (True, False, "write_not_allowed"),
        (True, "true", "write_not_allowed"),
        (True, 1, "write_not_allowed"),
        (True, None, "write_not_allowed"),
        (True, True, None),
    ],
)
async def test_both_switches_are_needed(alice, catalogue_op, allow_write, expected):
    out, w = await run_update(
        snapshot(update_enabled=catalogue_op), {"services": ["pr"], "allow_write": allow_write}
    )
    if expected:
        assert out["error"]["code"] == expected
        assert w.nothing_sent and w.built == []
    else:
        assert out == {"ok": True, "status": 204}
        (write,) = w.sap.writes
        assert write.headers["X-HTTP-Method"] == "MERGE" and write.url.path == KEYED
        assert json.loads(write.content) == {"RequestedQuantity": "7"}
        assert "If-Match" not in write.headers


@pytest.mark.parametrize(
    "operation, args",
    [
        ("create", {"body": {"PurchaseRequisition": "10000001", "Plant": "1000"}}),
        ("delete", {"key": KEY}),
    ],
)
async def test_the_same_for_create_and_delete(alice, operation, args):
    call = {"operation": operation, **args}
    off = World(catalogue=snapshot(operations=["list", "get"]))
    assert (await off.run(**call))["error"]["code"] == "operation_disabled"
    for flag in (False, "true", 1):
        closed = World({"services": ["pr"], "allow_write": flag}, snapshot(operations=ALL_OPS))
        out = await closed.run(**call)
        assert out["error"]["code"] == "write_not_allowed"
        assert "not enabled for this agent" in out["error"]["hint"]
        assert closed.nothing_sent
    assert off.nothing_sent
    w = World(catalogue=snapshot(operations=ALL_OPS))
    out = await w.run(**call)
    (write,) = w.sap.writes
    if operation == "create":
        assert out["status"] == 201 and write.method == "POST"
        assert out["item"]["Plant"] == "1000" and "CreatedByUser" not in out["item"]
    else:
        assert out == {"ok": True, "status": 204} and write.method == "DELETE"


async def test_the_fixed_order_of_write_checks(alice):
    w = World({"services": ["pr", "gone"], "allow_write": False}, snapshot())
    bad = {"operation": "delete", "key": {"x": 1}, "body": {"Nope": 1}, "etag": "zzz"}
    steps = [
        ({**bad, "service": "nope"}, "unknown_service"),
        ({**bad, "operation": "drop"}, "invalid_argument"),
        ({**bad, "target": "A_Nope"}, "unknown_target"),
        (bad, "operation_disabled"),  # delete is not ticked on ITEM
        ({**bad, "operation": "update"}, "write_not_allowed"),
    ]
    for args, code in steps:
        assert (await w.run(**args))["error"]["code"] == code, args
    assert w.nothing_sent

    w = World(catalogue=snapshot(operations=ALL_OPS))
    steps = [
        ({**bad, "operation": "update"}, "invalid_key"),
        ({**bad, "operation": "update", "key": KEY}, "unknown_field"),
        ({**UPDATE, "etag": "zzz", "select": ["Plant"]}, "invalid_argument"),
        ({**UPDATE, "etag": "zzz"}, "invalid_etag"),
    ]
    for args, code in steps:
        assert (await w.run(**args))["error"]["code"] == code, args
    assert w.nothing_sent and w.built == []


async def test_unknown_and_not_writable_body_fields_are_refused_before_any_request(alice):
    w = World()
    out = await w.run(**{**UPDATE, "body": {"Nope": 1}})
    assert out["error"]["code"] == "unknown_field" and "Nope" in out["error"]["message"]
    out = await w.run(**{**UPDATE, "body": {"PurReqnReleaseStatus": "05"}})
    assert out["error"]["code"] == "field_not_writable"
    assert "PurReqnReleaseStatus" in out["error"]["message"] and "05" not in json.dumps(out)
    # Not selectable, not writable: the field is not even confirmed to the caller as hidden.
    out = await w.run(**{**UPDATE, "body": {"CreatedByUser": "X"}})
    assert out["error"]["code"] == "field_not_writable"
    assert w.nothing_sent


async def test_create_needs_a_body_and_update_needs_a_key(alice):
    w = World(catalogue=snapshot(operations=ALL_OPS))
    for args, code in (
        ({"operation": "create"}, "invalid_argument"),
        ({"operation": "create", "body": {}}, "invalid_argument"),
        ({"operation": "create", "body": {"Plant": "1"}, "key": KEY}, "invalid_argument"),
        ({"operation": "update", "body": {"Plant": "1"}}, "invalid_key"),
        ({"operation": "update", "body": {"Plant": "1"}, "key": {"PurchaseRequisition": "1"}},
         "invalid_key"),
        ({"operation": "update", "key": KEY}, "invalid_argument"),
        ({"operation": "delete"}, "invalid_key"),
        ({"operation": "delete", "key": KEY, "body": {"Plant": "1"}}, "invalid_argument"),
        ({"operation": "create", "body": {"Plant": "1"}, "etag": "h-x"}, "invalid_argument"),
        ({**UPDATE, "filter": "Plant eq '1'"}, "invalid_argument"),
        ({**UPDATE, "navigation": "to_Twin"}, "invalid_argument"),
        ({**UPDATE, "params": {"a": 1}}, "invalid_argument"),
    ):
        out = await w.run(**args)
        assert out["error"]["code"] == code, (args, out)
    assert w.nothing_sent


async def test_key_fields_in_an_update_body_are_refused(alice):
    w = World()
    out = await w.run(**{**UPDATE, "body": {"PurchaseRequisitionItem": "00020", "Plant": "1"}})
    assert out["error"]["code"] == "invalid_argument"
    assert "PurchaseRequisitionItem" in out["error"]["message"] and w.nothing_sent


async def test_body_values_must_be_json_scalars(alice):
    w = World()
    for value in ({"to_Twin": {"Plant": "1"}}, [1, 2]):
        out = await w.run(**{**UPDATE, "body": {"Plant": value}})
        assert out["error"]["code"] == "invalid_argument", out
    assert w.nothing_sent


async def test_null_clears_a_writable_field(alice):
    w = World()
    assert (await w.run(**{**UPDATE, "body": {"Plant": None}}))["ok"] is True
    assert json.loads(w.sap.writes[0].content) == {"Plant": None}


async def test_a_read_only_entry_does_not_even_list_writes_in_search(alice):
    catalogue = snapshot(operations=ALL_OPS)
    closed = World({"services": ["pr"]}, catalogue)
    found = await closed.search(query="", detail="full", service="pr")
    for match in found["matches"]:
        assert not set(match.get("operations", [])) & {"create", "update", "delete"}
        assert not any(f.get("writable") for f in match.get("fields", []))
    assert "Release" not in {m["target"] for m in found["matches"]}
    # What search offers with the switch on, execute accepts.
    w = World(catalogue=catalogue)
    found = await w.search(query="", detail="full", service="pr")
    item = next(m for m in found["matches"] if m["target"] == ITEM)
    assert {"create", "update", "delete"} <= set(item["operations"])
    writable = [f["name"] for f in item["fields"] if f["writable"]]
    assert "Plant" in writable and "PurReqnReleaseStatus" not in writable
    out = await w.run(operation="update", key=KEY, body={"Plant": "2000"})
    assert out == {"ok": True, "status": 204}


async def test_write_through_a_signed_in_user_service_without_user_is_no_user():
    assert current_jwt.get() is None
    w = World()
    out = await w.run(**UPDATE)
    assert out["error"]["code"] == "no_user" and w.nothing_sent
    # A create has no key and no handle to look up; it is refused all the same,
    # before the decision to send.
    creating = World(catalogue=snapshot(operations=ALL_OPS))
    out = await creating.run(operation="create", body={"Plant": "1000"})
    assert out["error"]["code"] == "no_user" and creating.nothing_sent and creating.built == []
    # The technical-user service of the same agent still writes.
    assert (await w.run(**{**UPDATE, "service": "pr-jobs"})) == {"ok": True, "status": 204}
    assert w.sap.writes[0].headers["Authorization"] == "Bearer dest-token"


async def test_function_imports_stay_refused(alice):
    w = World()
    out = await w.run(target="Release", operation="call")
    assert out["error"]["code"] == "write_not_allowed" and w.nothing_sent


# -- the ETag handle ----------------------------------------------------------


def _everything(*results: Any) -> str:
    return json.dumps(results)


async def test_etag_argument_reaches_if_match(alice):
    w = World()
    got = await w.run(operation="get", key=KEY)
    handle = got["etag"]
    assert isinstance(handle, str) and handle and STAMP not in handle
    out = await w.run(**{**UPDATE, "etag": handle})
    assert out == {"ok": True, "status": 204}
    assert w.sap.writes[0].headers["If-Match"] == RAW_ETAG


async def test_the_raw_etag_appears_in_no_tool_result(alice):
    w = World(catalogue=snapshot(operations=ALL_OPS))
    w.sap.write_answer = lambda r: (
        httpx.Response(204, headers={"ETag": NEW_ETAG})
        if r.headers.get("X-HTTP-Method") == "MERGE"
        else None
    )
    got = await w.run(operation="get", key=KEY)
    listed = await w.run(operation="list")
    updated = await w.run(**{**UPDATE, "etag": got["etag"]})
    created = await w.run(operation="create", body={**KEY, "Plant": "1000"})
    raw = await w.run(**{**UPDATE, "etag": RAW_ETAG})
    w.sap.write_answer = v2_error(412, "SY/1", "Precondition failed")
    stale = await w.run(**{**UPDATE, "etag": created["etag"]})
    text = _everything(got, listed, updated, created, raw, stale)
    for secret in (STAMP, NEW_STAMP, "datetimeoffset", SECRET_USER):
        assert secret not in text
    # ... while the answers of an update and a create carry a usable handle.
    # (The created entity is the one just updated, at the same version: one handle.)
    assert updated["etag"] != got["etag"] and created["etag"] == updated["etag"]
    assert stale["error"]["code"] == "sap_error"
    assert "read the entity again" in stale["error"]["hint"]
    assert [r.headers.get("If-Match") for r in w.sap.writes] == [RAW_ETAG, None, NEW_ETAG]


async def test_a_raw_etag_from_the_model_is_refused_and_never_forwarded(alice):
    w = World(catalogue=snapshot(operations=ALL_OPS))
    for etag in (RAW_ETAG, 'W/"1"', "*", '"a", "b"', "h-" + "A" * 32, 7):
        for operation in ("update", "delete"):
            args = {"operation": operation, "key": KEY, "etag": etag}
            if operation == "update":
                args["body"] = {"Plant": "1"}
            out = await w.run(**args)
            assert out["error"]["code"] == "invalid_etag", (etag, out)
            assert "get" in out["error"]["hint"] and str(etag) not in json.dumps(out)
    assert w.nothing_sent


async def test_a_read_carries_a_handle_only_where_this_agent_can_write(alice):
    # 1. no allow_write: nothing at all.
    closed = World({"services": ["pr"]}, snapshot(operations=ALL_OPS))
    assert "etag" not in await closed.run(operation="get", key=KEY)
    # 2. allow_write, but neither update nor delete on the entity set.
    w = World(catalogue=snapshot(operations=["list", "get", "create"]))
    assert "etag" not in await w.run(operation="get", key=KEY)
    assert "etag" not in await w.run(operation="get", key=KEY, target="A_ReadOnly")
    # 3. a list never carries one; a get through a navigation neither (the
    #    entity it leads to has its own key).
    w = World()
    assert "etag" not in await w.run(operation="list")
    assert "etag" not in await w.run(operation="get", key=KEY, navigation="to_Twin")
    assert "etag" in await w.run(operation="get", key=KEY)
    assert len(w.toolset.etag_handles) == 1


async def test_a_handle_is_bound_to_the_user_and_the_token_it_was_issued_for():
    w = World()
    with signed_in(ALICE):
        handle = (await w.run(operation="get", key=KEY))["etag"]
    with signed_in(BOB):
        out = await w.run(**{**UPDATE, "etag": handle})
        assert out["error"]["code"] == "invalid_etag"
    with signed_in(ALICE, token="jwt-of-alice-after-refresh"):
        out = await w.run(**{**UPDATE, "etag": handle})
        assert out["error"]["code"] == "invalid_etag"
    # Somebody else's token under Alice's name (a run-as job) is not Alice either.
    with signed_in(ALICE, token=f"jwt-of-{BOB}"):
        assert (await w.run(**{**UPDATE, "etag": handle}))["error"]["code"] == "invalid_etag"
    # No user at all: the service itself refuses first.
    assert (await w.run(**{**UPDATE, "etag": handle}))["error"]["code"] == "no_user"
    assert w.sap.writes == [] and w.audits == []
    with signed_in(ALICE):
        assert (await w.run(**{**UPDATE, "etag": handle}))["ok"] is True
    assert w.sap.writes[0].headers["If-Match"] == RAW_ETAG


async def test_a_handle_is_for_one_entity_of_one_entity_set_of_one_service(alice):
    w = World(catalogue=snapshot(operations=ALL_OPS, user_context=False))
    handle = (await w.run(operation="get", key=KEY))["etag"]
    for other in (
        {"key": OTHER_KEY},
        {"target": "A_Twin"},
        {"service": "pr-jobs"},
    ):
        for operation in ("update", "delete"):
            args = {"operation": operation, "key": KEY, "etag": handle, **other}
            if operation == "update":
                args["body"] = {"Plant": "1"}
            out = await w.run(**args)
            assert out["error"]["code"] == "invalid_etag", (other, out)
    assert w.sap.writes == []
    # The same key written another way is the same entity.
    out = await w.run(operation="delete", key=dict(reversed(list(KEY.items()))), etag=handle)
    assert out == {"ok": True, "status": 204}
    assert w.sap.writes[0].headers["If-Match"] == RAW_ETAG
    # Used for a change that went through: the version it stood for is gone.
    out = await w.run(operation="delete", key=KEY, etag=handle)
    assert out["error"]["code"] == "invalid_etag"


async def test_a_handle_expires(alice, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(tools_module, "_now", lambda: clock[0])
    w = World()
    handle = (await w.run(operation="get", key=KEY))["etag"]
    clock[0] += tools_module.ETAG_HANDLE_TTL_SECONDS - 1
    assert (await w.run(**{**UPDATE, "etag": handle}))["ok"] is True
    handle = (await w.run(operation="get", key=KEY))["etag"]
    clock[0] += tools_module.ETAG_HANDLE_TTL_SECONDS
    out = await w.run(**{**UPDATE, "etag": handle})
    assert out["error"]["code"] == "invalid_etag" and len(w.sap.writes) == 1
    # An expired entry does not stay in memory.
    await w.run(operation="get", key=OTHER_KEY)
    assert len(w.toolset.etag_handles) == 1


async def test_the_handle_store_is_bounded(alice, monkeypatch):
    monkeypatch.setattr(tools_module, "ETAG_HANDLE_MAX", 3)
    w = World()
    handles = []
    for n in range(6):
        key = {"PurchaseRequisition": "10000001", "PurchaseRequisitionItem": f"000{n}0"}
        handles.append((key, (await w.run(operation="get", key=key))["etag"]))
    assert len(w.toolset.etag_handles) == 3 and len({h for _, h in handles}) == 6
    key, oldest = handles[0]
    out = await w.run(operation="update", key=key, body={"Plant": "1"}, etag=oldest)
    assert out["error"]["code"] == "invalid_etag"
    key, newest = handles[-1]
    assert (await w.run(operation="update", key=key, body={"Plant": "1"}, etag=newest))["ok"]
    # Reading one entity again and again takes one slot, and the handle stays.
    again = [(await w.run(operation="get", key=KEY))["etag"] for _ in range(5)]
    assert len(set(again)) == 1 and len(w.toolset.etag_handles) <= 3
    assert "W/" not in repr(w.toolset.etag_handles) and STAMP not in repr(w.toolset.etag_handles)


async def test_without_a_handle_sap_decides(alice):
    w = World()
    w.sap.write_answer = httpx.Response(428, text="Precondition Required")
    out = await w.run(**UPDATE)
    assert out["error"]["code"] == "etag_required" and "get" in out["error"]["hint"]
    assert "If-Match" not in w.sap.writes[0].headers


# -- results and errors -------------------------------------------------------


async def test_a_created_entity_is_cut_to_the_selectable_fields_and_clipped(alice, monkeypatch):
    w = World(catalogue=snapshot(operations=ALL_OPS))
    out = await w.run(operation="create", body={**KEY, "Plant": "1000"})
    assert out["item"] == {**KEY, "PurReqnReleaseStatus": "B", "RequestedQuantity": "5",
                           "Plant": "1000"}
    assert out["status"] == 201 and out["truncated"] is False
    # The handle of the answer is the created entity's.
    assert (await w.run(**{**UPDATE, "etag": out["etag"]}))["ok"] is True
    assert w.sap.writes[-1].headers["If-Match"] == NEW_ETAG
    monkeypatch.setattr(tools_module, "MAX_RESULT_CHARS", 120)
    out = await w.run(operation="create", body={**KEY, "Plant": "1000"})
    assert out["truncated"] is True and len(json.dumps(out)) <= 120


async def test_every_client_code_reaches_the_model_as_short_data(alice, caplog):
    w = World(catalogue=snapshot(operations=ALL_OPS))

    def broken(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("reset by https://s4.internal:44300/sap?sap-client=100")

    def unreachable(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to s4.internal:44300")

    cases = [
        (broken, "write_outcome_unknown", "unknown"),
        (unreachable, "destination_error", "refused"),
        (httpx.Response(200, text="<html>sign in</html>"), "write_outcome_unknown", "unknown"),
        (v2_error(400, "SY/530", "Plant 1000 at https://s4.internal:44300/sap/x is locked"),
         "sap_error", "sap_error"),
        (v2_error(403, "SY/1", "No authorization"), "sap_error", "sap_error"),
    ]
    with caplog.at_level(logging.DEBUG, logger="agents"):
        for answer, code, outcome in cases:
            w.sap.write_answer = answer
            for call in (UPDATE, {"operation": "create", "body": {"Plant": "1"}}):
                before = len(w.sap.writes)
                out = await w.run(**call)
                assert set(out) == {"error"} and out["error"]["code"] == code, (answer, out)
                assert set(out["error"]) <= {"code", "message", "hint"}
                assert len(out["error"]["message"]) <= 500
                assert len(w.sap.writes) == before + 1  # never sent a second time
                assert w.audits[-1].outcome == outcome
                text = json.dumps(out)
                for secret in ("s4.internal", "44300", "/sap", "T-user-token", "S-user-token",
                               COOKIE_NAME, "jwt-of-", "S4_ODATA_USER", "user-token-of"):
                    assert secret not in text, (secret, text)
                if code == "write_outcome_unknown":
                    assert "blindly" in out["error"]["hint"]
                if answer is unreachable:
                    assert "nothing was changed" in out["error"]["message"]
    for secret in ("jwt-of-", "T-user-token", "S-user-token", "s4.internal", STAMP):
        assert secret not in caplog.text


async def test_an_unexpected_failure_while_writing_is_an_unknown_outcome(alice, caplog):
    w = World()

    def bug(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("detail https://s4.internal:44300/sap?token=abc")

    w.sap.write_answer = bug
    with caplog.at_level(logging.DEBUG, logger="agents.odata"):
        out = await w.run(**UPDATE)
    assert out["error"]["code"] == "write_outcome_unknown" and len(w.sap.writes) == 1
    assert "blindly" in out["error"]["hint"]
    assert "s4.internal" not in caplog.text and "token=abc" not in caplog.text
    assert [a.outcome for a in w.audits] == ["unknown"]


async def test_a_destination_that_fails_before_the_write_changed_nothing(alice, caplog):
    from agents.destination import DestinationError

    class Down(FakeResolver):
        async def resolve(self, **_kw):
            raise DestinationError("destination service said 503 at https://dest.example/x")

    w = World(resolver_factory=lambda name: Down(name=name))
    with caplog.at_level(logging.DEBUG, logger="agents.odata.tools"):
        out = await w.run(**UPDATE)
    assert out["error"]["code"] == "destination_error"
    assert "nothing was changed" in out["error"]["message"]
    assert "dest.example" not in json.dumps(out) and w.sap.writes == []
    assert [a.outcome for a in w.audits] == ["refused"]


# -- the audit hook -----------------------------------------------------------


async def test_the_hook_is_called_once_per_write_that_was_to_be_sent(alice):
    w = World(catalogue=snapshot(operations=ALL_OPS))
    # Refused before the decision to send: no call.
    await w.run(**{**UPDATE, "body": {"Nope": 1}})
    await w.run(**{**UPDATE, "etag": "zzz"})
    await w.run(operation="get", key=KEY)
    await w.run(operation="list")
    assert w.audits == []
    await w.run(operation="update", key=KEY, body={"Plant": "SECRET-VALUE", "RequestedQuantity": 1})
    await w.run(operation="create", body={**KEY, "Plant": "SECRET-VALUE"})
    w.sap.write_answer = v2_error(400, "SY/530", "locked")
    await w.run(operation="delete", key=KEY)
    assert [(a.operation, a.outcome, a.status) for a in w.audits] == [
        ("update", "ok", 204),
        ("create", "ok", 201),
        ("delete", "sap_error", 400),
    ]
    update, create, delete = w.audits
    assert update == WriteAudit(
        agent="buyer",
        run_id="run-1",
        service="pr",
        target=ITEM,
        operation="update",
        key=KEY,
        fields=("Plant", "RequestedQuantity"),
        outcome="ok",
        status=204,
        sent_as=update.sent_as,
        run_principal=ALICE,
        token_digest=update.token_digest,
    )
    assert create.key is None
    assert create.fields == ("PurchaseRequisition", "PurchaseRequisitionItem", "Plant")
    assert delete.fields == () and delete.key == KEY
    for record in w.audits:
        assert "SECRET-VALUE" not in repr(record) and "jwt-of-" not in repr(record)
        assert record.token_digest and record.token_digest not in ("", f"jwt-of-{ALICE}")


async def test_the_hook_gets_the_identity_of_the_token_that_was_sent():
    """A job run keeps the trigger's token while the principal names the run-as user."""
    w = World()
    claims = current_claims.set({"user_uuid": "uuid-of-alice", "email": ALICE})
    try:
        with signed_in("job-user", token=f"jwt-of-{ALICE}"):
            assert (await w.run(**UPDATE))["ok"] is True
    finally:
        current_claims.reset(claims)
    (record,) = w.audits
    assert record.run_principal == "job-user" and record.sent_as == "uuid-of-alice"
    assert w.resolved[-1] == ("S4_ODATA_USER", f"jwt-of-{ALICE}")
    # Without validated claims, the sender is named by the token's digest, never by the principal.
    with signed_in("job-user", token=f"jwt-of-{BOB}"):
        await w.run(**UPDATE)
    record = w.audits[-1]
    assert record.sent_as == "token:" + record.token_digest and record.run_principal == "job-user"
    assert record.token_digest != w.audits[0].token_digest
    # A technical-user service sends the destination's credential, whoever is signed in.
    with signed_in(ALICE):
        await w.run(**{**UPDATE, "service": "pr-jobs"})
    record = w.audits[-1]
    assert record.sent_as == "technical:S4_ODATA_TECH" and record.token_digest is None
    assert record.run_principal == ALICE


async def test_the_hook_is_called_when_the_write_is_cancelled(alice):
    w = World()
    arrived, never = asyncio.Event(), asyncio.Event()

    async def hang(_request: httpx.Request) -> httpx.Response:
        arrived.set()
        await never.wait()
        return httpx.Response(204)

    w.sap.write_answer = hang
    task = asyncio.create_task(w.run(**UPDATE))
    await asyncio.wait_for(arrived.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [(a.operation, a.outcome, a.status) for a in w.audits] == [("update", "cancelled", None)]
    # The toolset is still usable.
    w.sap.write_answer = None
    assert (await w.run(**UPDATE))["ok"] is True and len(w.audits) == 2


async def test_a_failing_hook_does_not_change_what_the_write_answered(alice, caplog):
    async def failing(_record: WriteAudit) -> None:
        raise RuntimeError("audit store down: secret-detail")

    w = World(on_write=failing)
    with caplog.at_level(logging.DEBUG, logger="agents.odata.tools"):
        out = await w.run(**UPDATE)
    assert out == {"ok": True, "status": 204}
    assert "RuntimeError" in caplog.text and "secret-detail" not in caplog.text


async def test_without_a_hook_a_write_just_runs(alice):
    w = World(on_write=None)
    assert (await w.run(**UPDATE)) == {"ok": True, "status": 204}


# -- two users at once ---------------------------------------------------------


async def test_two_users_writing_at_the_same_time_through_one_toolset():
    w = World(catalogue=snapshot(operations=ALL_OPS))
    w.sap.read_answer = lambda r: _row(
        etag=f'W/"etag-of-{r.headers["Authorization"].removeprefix("Bearer user-token-of-")}"'
    )
    handles: dict[str, list[str]] = {ALICE: [], BOB: []}

    async def as_user(principal: str) -> None:
        with signed_in(principal):
            for n in range(6):
                got = await w.run(operation="get", key=KEY)
                handles[principal].append(got["etag"])
                body = {"Plant": f"{principal[0]}{n}"}
                out = await w.run(operation="update", key=KEY, body=body, etag=got["etag"])
                assert out == {"ok": True, "status": 204}, out

    await asyncio.gather(as_user(ALICE), as_user(BOB))
    assert w.sap.crossed == [] and len(w.toolset.http_clients) == 1
    assert len(w.sap.writes) == 12 and len(w.sap.fetches) == 2
    for write in w.sap.writes:
        who = write.headers["Authorization"].removeprefix("Bearer user-token-of-")
        assert who in (ALICE, BOB)
        assert json.loads(write.content)["Plant"][0] == who[0]
        assert write.headers["X-CSRF-Token"] == f"T-user-token-of-{who}"
        assert write.headers["Cookie"] == f"{COOKIE_NAME}=S-user-token-of-{who}"
        assert write.headers["If-Match"] == f'W/"etag-of-{who}"'
    order = [write.headers["Authorization"] for write in w.sap.writes]
    assert order != sorted(order)  # the two users really were interleaved
    assert {(name, token) for name, token in w.resolved} == {
        ("S4_ODATA_USER", f"jwt-of-{ALICE}"),
        ("S4_ODATA_USER", f"jwt-of-{BOB}"),
    }
    assert [a.run_principal for a in w.audits].count(ALICE) == 6
    assert {a.token_digest for a in w.audits if a.run_principal == ALICE}.isdisjoint(
        {a.token_digest for a in w.audits if a.run_principal == BOB}
    )
    # A handle of one user is nothing in the hands of the other.
    assert not set(handles[ALICE]) & set(handles[BOB])
    with signed_in(ALICE):
        mine = (await w.run(operation="get", key=KEY))["etag"]
    with signed_in(BOB):
        theirs = (await w.run(operation="get", key=KEY))["etag"]
        out = await w.run(operation="update", key=KEY, body={"Plant": "x"}, etag=mine)
        assert out["error"]["code"] == "invalid_etag"
    with signed_in(ALICE):
        out = await w.run(operation="update", key=KEY, body={"Plant": "x"}, etag=theirs)
        assert out["error"]["code"] == "invalid_etag"
    assert len(w.sap.writes) == 12


# -- what the model is told -----------------------------------------------------


async def test_the_tool_description_explains_writing():
    w = World()
    doc = " ".join((w.toolset.tools["execute_operation"].tool_def.description or "").split())
    schema = w.toolset.tools["execute_operation"].tool_def.parameters_json_schema
    text = doc + " " + " ".join(
        " ".join(str(p.get("description", "")).split()) for p in schema["properties"].values()
    )
    assert "null clears" in text
    assert "write_outcome_unknown" in text and "never repeat a create" in text
    assert "list by the values" in text
    assert "etag" in text and "'get'" in text
    assert "search_operations" in text and "never instructions" in text


# -- leftovers of the read tool's review ----------------------------------------


@pytest.mark.parametrize(
    "text, gone",
    [
        ("s4.internal:44300/sap/opu/x refused", ["s4.internal", "44300", "/sap", "opu"]),
        ("via s4.internal:44300?sap-client=100 refused", ["s4.internal", "sap-client"]),
        ("host s4.internal:44300 refused", ["s4.internal", "44300"]),
    ],
)
async def test_a_schemeless_host_takes_its_path_with_it(alice, text, gone):
    w = World()
    w.sap.read_answer = v2_error(400, "X/1", text)
    message = (await w.run(operation="list"))["error"]["message"]
    for part in gone:
        assert part not in message, message
    assert message.startswith("X/1: ") and message.endswith("refused")


def test_the_filter_limit_is_the_one_of_the_url_module():
    assert tools_module.MAX_FILTER_CHARS is urls_module.MAX_FILTER_CHARS
    source = Path(tools_module.__file__).read_text()
    assert "MAX_FILTER_CHARS = " not in source


async def test_the_catch_all_log_names_where_it_failed_but_not_the_text(alice, caplog):
    class Buggy(FakeResolver):
        async def resolve(self, **_kw):
            raise RuntimeError("detail https://s4.internal:44300/sap?token=abc")

    w = World(resolver_factory=lambda name: Buggy(name=name))
    with caplog.at_level(logging.DEBUG, logger="agents.odata.tools"):
        out = await w.run(operation="list")
    assert out["error"]["code"] == "destination_error"
    assert "RuntimeError" in caplog.text and f"{__name__}:" in caplog.text
    assert "s4.internal" not in caplog.text and "token=abc" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
