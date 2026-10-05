"""`execute_operation` for reads (Task O4): the catalogue as the data boundary.

SAP is an ``httpx.MockTransport`` behind the real ``destination_http_client``,
so an assertion on a request is an assertion on what the back end would have
received, and `sap.requests == []` proves a refusal came before any request.
"""

from __future__ import annotations

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

from agents.auth import current_jwt, current_principal  # noqa: E402
from agents.destination import DestinationError  # noqa: E402
from agents.odata import tools as tools_module  # noqa: E402
from agents.odata.models import validate_odata_service  # noqa: E402
from agents.odata.tools import DEFAULT_TOP, MAX_EXPAND, MAX_TOP, odata_toolset  # noqa: E402
from tests.odata_helpers import FakeResolver, Sap, v2_error  # noqa: E402

HEADER = "A_PurchaseRequisitionHeader"
ITEM = "A_PurchaseRequisitionItem"
SERVICE_PATH = "/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV"
SECRET_USER = "JDOE-HIDDEN"


def _field(name: str, **kw: Any) -> dict[str, Any]:
    return {"name": name, **kw}


def _service(**kw: Any) -> dict[str, Any]:
    """A service the way the registry snapshot holds it (`to_dict()`)."""
    clean = validate_odata_service(kw)
    return {**clean, "id": 1, "counts": {}, "has_write": True, "used_by": []}


_DEFINITION: dict[str, Any] = {
    "entity_sets": [
        {
            "name": HEADER,
            "keys": [{"name": "PurchaseRequisition"}],
            "operations": ["list", "get"],
            "fields": [
                _field("PurchaseRequisition", selectable=True, filterable=True),
                _field("PurReqnDescription", selectable=True),
                _field("CreatedByUser", personal_data=True),
            ],
            "navigations": [
                {"name": "to_PurchaseReqnItem", "target": ITEM, "collection": True},
                {"name": "to_Items2", "target": ITEM, "collection": True},
                {"name": "to_Items3", "target": ITEM, "collection": True},
                {"name": "to_Items4", "target": ITEM, "collection": True},
                {"name": "to_Delivery", "target": "A_PurReqAddDelivery", "collection": True},
                {"name": "to_Gone", "target": "A_NotInCatalogue", "collection": True},
            ],
        },
        {
            "name": ITEM,
            "keys": [{"name": "PurchaseRequisition"}, {"name": "PurchaseRequisitionItem"}],
            "operations": ["list", "get", "update"],
            "fields": [
                _field("PurchaseRequisition", selectable=True, filterable=True),
                _field("PurchaseRequisitionItem", selectable=True),
                _field("PurReqnReleaseStatus", selectable=True, filterable=True),
                _field("RequestedQuantity", writable=True),
                _field("CreatedByUser", personal_data=True),
            ],
            "navigations": [
                {"name": "to_PurchaseReqn", "target": HEADER, "collection": False},
            ],
        },
        {
            # In the catalogue, no operation ticked.
            "name": "A_PurReqAddDelivery",
            "keys": [{"name": "Id"}],
            "operations": [],
            "fields": [_field("Id", selectable=True)],
        },
        {
            "name": "A_Note",
            "keys": [{"name": "Id"}],
            "operations": ["create"],
            "fields": [_field("Id"), _field("Note", writable=True)],
        },
    ],
    "operations": [
        {
            "name": "Release",
            "kind": "function_import",
            "http_method": "POST",
            "enabled": True,
            "changes_data": True,
        },
        {
            "name": "CountOpen",
            "kind": "function_import",
            "http_method": "GET",
            "enabled": True,
            "changes_data": False,
        },
        {
            "name": "Off",
            "kind": "function_import",
            "http_method": "GET",
            "enabled": False,
            "changes_data": False,
        },
    ],
}

USER_SERVICE = _service(
    name="purchase-requisitions",
    title="Purchase requisitions",
    purpose="Read purchase requisitions and their items",
    destination="S4_ODATA_USER",
    user_context=True,
    odata_version="v2",
    service_path=SERVICE_PATH,
    definition=_DEFINITION,
)
TECH_SERVICE = _service(
    name="purchase-requisitions-jobs",
    title="Purchase requisitions for jobs",
    purpose="Read purchase requisitions in scheduled runs",
    destination="S4_ODATA_TECH",
    user_context=False,
    odata_version="v2",
    service_path=SERVICE_PATH,
    definition=_DEFINITION,
)
SNAPSHOT = {s["name"]: s for s in (USER_SERVICE, TECH_SERVICE)}
BASE = {"service": "purchase-requisitions", "target": ITEM, "operation": "list"}
TECH = {**BASE, "service": "purchase-requisitions-jobs"}
CTX = SimpleNamespace(run_id="run-1")


def _item(number: str, item: str) -> dict[str, Any]:
    """An item row as SAP Gateway sends it: more than the catalogue releases."""
    return {
        "__metadata": {
            "uri": f"https://s4.internal:44300{SERVICE_PATH}/{ITEM}('{number}','{item}')",
            "type": "API.A_PurchaseRequisitionItemType",
            "etag": "W/\"datetimeoffset'2026-01-01T00%3A00%3A00Z'\"",
        },
        "PurchaseRequisition": number,
        "PurchaseRequisitionItem": item,
        "PurReqnReleaseStatus": "B",
        "RequestedQuantity": "5",
        "CreatedByUser": SECRET_USER,
        "to_PurchaseReqn": {"__deferred": {"uri": f"https://s4.internal:44300/x/{SECRET_USER}"}},
    }


def _page(rows: list[dict], count: int | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"results": rows}
    if count is not None:
        d["__count"] = str(count)
    return {"d": d}


class World:
    """One toolset on a mock SAP, with every destination resolution recorded."""

    def __init__(
        self,
        *answers: Any,
        oauth: dict[str, Any] | None = None,
        snapshot: dict[str, dict] | None = None,
        **factory_args: Any,
    ) -> None:
        self.sap = Sap(*answers)
        self.resolved: list[tuple[str, str | None, str | None]] = []
        self.built: list[str] = []
        world = self

        class Recording(FakeResolver):
            async def resolve(self, *, force=False, user_token=None, principal=None):
                world.resolved.append((self.name, user_token, principal))
                return await super().resolve(
                    force=force, user_token=user_token, principal=principal
                )

        def factory(destination: str) -> FakeResolver:
            world.built.append(destination)
            return Recording(name=destination)

        self.toolset = odata_toolset(
            oauth
            or {"services": ["purchase-requisitions", "purchase-requisitions-jobs"]},
            auth_mode="destination",
            services=snapshot or SNAPSHOT,
            agent_name="buyer",
            transport=self.sap.transport(),
            resolver_factory=factory_args.pop("resolver_factory", factory),
            **factory_args,
        )

    async def run(self, tool: str, **args: Any) -> dict:
        function = self.toolset.tools[tool].function
        if tool == "execute_operation":
            return await function(CTX, **args)
        return await function(**args)

    @property
    def requests(self) -> list[httpx.Request]:
        return self.sap.requests


@pytest.fixture
def alice():
    """Alice is signed in for the duration of the test."""
    jwt = current_jwt.set("jwt-a")
    principal = current_principal.set("alice@example.com")
    try:
        yield
    finally:
        current_principal.reset(principal)
        current_jwt.reset(jwt)


# -- the request a read sends ------------------------------------------------


async def test_list_defaults_to_the_selectable_fields_and_top_50(alice):
    w = World(_page([_item("10", "00010"), _item("10", "00020")], count=2))
    out = await w.run("execute_operation", **BASE)
    request = w.requests[0]
    q = request.url.params
    assert q["$select"] == "PurchaseRequisition,PurchaseRequisitionItem,PurReqnReleaseStatus"
    assert q["$top"] == str(DEFAULT_TOP) == "50"
    assert "$skip" not in q and "$filter" not in q and "$expand" not in q
    assert request.method == "GET"
    assert request.url.host == "s4.internal"
    assert request.url.path == f"{SERVICE_PATH}/{ITEM}"
    assert set(out) == {"items", "count", "truncated"}
    assert out["count"] == 2 and out["truncated"] is False
    assert out["items"][0] == {
        "PurchaseRequisition": "10",
        "PurchaseRequisitionItem": "00010",
        "PurReqnReleaseStatus": "B",
    }


async def test_top_is_capped_at_200(alice):
    w = World(_page([], count=0))
    await w.run("execute_operation", **BASE, top=5000)
    assert w.requests[0].url.params["$top"] == str(MAX_TOP) == "200"


async def test_filter_select_orderby_and_skip_are_sent_as_parameters(alice):
    w = World(_page([_item("10", "00010")], count=40))
    out = await w.run(
        "execute_operation",
        **BASE,
        select=["PurchaseRequisition"],
        filter="PurReqnReleaseStatus eq 'B' and PurchaseRequisition ne '1&$top=9'",
        orderby=["PurchaseRequisition desc"],
        top=1,
        skip=20,
    )
    q = w.requests[0].url.params
    assert q["$select"] == "PurchaseRequisition"
    assert q["$filter"] == "PurReqnReleaseStatus eq 'B' and PurchaseRequisition ne '1&$top=9'"
    assert q["$orderby"] == "PurchaseRequisition desc"
    assert q.get_list("$top") == ["1"] and q["$skip"] == "20"
    assert out["items"] == [{"PurchaseRequisition": "10"}]
    assert out["next_skip"] == 21


# -- nothing the catalogue did not release comes back ------------------------


async def test_a_non_selectable_value_never_reaches_the_result(alice):
    header = {
        "__metadata": {"uri": f"https://s4.internal:44300/h('{SECRET_USER}')"},
        "PurchaseRequisition": "10",
        "PurReqnDescription": "Pens",
        "CreatedByUser": SECRET_USER,
        "to_PurchaseReqnItem": {"results": [_item("10", "00010")]},
        "to_Delivery": {"results": [{"Id": SECRET_USER}]},
    }
    w = World(_page([header], count=1), {"d": header})
    listed = await w.run(
        "execute_operation",
        **{**BASE, "target": HEADER},
        expand=["to_PurchaseReqnItem"],
    )
    got = await w.run(
        "execute_operation",
        **{**BASE, "target": HEADER, "operation": "get"},
        key={"PurchaseRequisition": "10"},
        expand=["to_PurchaseReqnItem"],
    )
    assert listed["items"][0] == {
        "PurchaseRequisition": "10",
        "PurReqnDescription": "Pens",
        "to_PurchaseReqnItem": [
            {
                "PurchaseRequisition": "10",
                "PurchaseRequisitionItem": "00010",
                "PurReqnReleaseStatus": "B",
            }
        ],
    }
    assert got["item"] == listed["items"][0]
    for out in (listed, got):
        text = json.dumps(out)
        for forbidden in (SECRET_USER, "CreatedByUser", "__metadata", "__deferred", "s4.internal"):
            assert forbidden not in text
    for request in w.requests:
        sent = request.url.params["$select"]
        assert "CreatedByUser" not in sent and "RequestedQuantity" not in sent
        assert "to_PurchaseReqnItem/PurReqnReleaseStatus" in sent
        assert request.url.params["$expand"] == "to_PurchaseReqnItem"


# -- refusals ----------------------------------------------------------------


@pytest.mark.parametrize(
    "args, code",
    [
        ({"service": "other"}, "unknown_service"),
        ({"service": 7}, "unknown_service"),
        ({"target": "A_Nope"}, "unknown_target"),
        ({"target": None}, "unknown_target"),
        ({"target": "Release"}, "unknown_target"),  # an operation is not an entity set
        ({"target": "A_PurReqAddDelivery"}, "operation_disabled"),
        ({"target": "A_Note"}, "operation_disabled"),  # create only
        ({"operation": "peek"}, "invalid_argument"),
        ({"select": ["CreatedByUser"]}, "field_not_selectable"),
        ({"select": ["RequestedQuantity"]}, "field_not_selectable"),
        ({"select": ["Nope"]}, "unknown_field"),
        ({"select": "PurchaseRequisition"}, "invalid_argument"),
        ({"select": ["*"]}, "unknown_field"),
        ({"orderby": ["CreatedByUser desc"]}, "field_not_selectable"),
        ({"orderby": ["Plant sideways"]}, "invalid_argument"),
        ({"orderby": ["PurchaseRequisition desc,CreatedByUser"]}, "invalid_argument"),
        ({"orderby": ["Nope"]}, "unknown_field"),
        ({"filter": "CreatedByUser eq 'X'"}, "field_not_filterable"),
        ({"filter": "RequestedQuantity gt 1"}, "field_not_filterable"),
        ({"filter": "Nope eq 'X'"}, "unknown_field"),
        ({"filter": "to_PurchaseReqn/CreatedByUser eq 'X'"}, "invalid_argument"),
        ({"filter": "PurchaseRequisition eq '1'" + " " * 1000}, "invalid_argument"),
        ({"filter": ["PurchaseRequisition eq '1'"]}, "invalid_argument"),
        ({"expand": ["to_Nope"]}, "unknown_field"),
        ({"expand": ["CreatedByUser"]}, "unknown_field"),
        ({"expand": "to_PurchaseReqn"}, "invalid_argument"),
        ({"operation": "get"}, "invalid_key"),
        ({"operation": "get", "key": {"PurchaseRequisition": "10"}}, "invalid_key"),
        (
            {
                "operation": "get",
                "key": {"PurchaseRequisition": "10", "PurchaseRequisitionItem": "../x"},
            },
            "invalid_key",
        ),
        # a key without a navigation is not a list
        ({"key": {"PurchaseRequisition": "1", "PurchaseRequisitionItem": "1"}}, "invalid_argument"),
        ({"navigation": "to_PurchaseReqn"}, "invalid_key"),
        ({"skip": -1}, "invalid_argument"),
        ({"skip": "3"}, "invalid_argument"),
        ({"top": 0}, "invalid_argument"),
        ({"top": True}, "invalid_argument"),
        ({"top": "10"}, "invalid_argument"),
        ({"body": {"x": 1}}, "invalid_argument"),
        ({"params": {"x": 1}}, "invalid_argument"),
        ({"etag": "W/1"}, "invalid_argument"),
    ],
)
async def test_refusals_happen_before_any_request(alice, args, code):
    w = World()
    out = await w.run("execute_operation", **{**BASE, **args})
    assert set(out) == {"error"}
    assert out["error"]["code"] == code, out
    assert w.requests == [] and w.resolved == []


async def test_a_refusal_never_repeats_a_value(alice):
    w = World()
    poison = "x' or CreatedByUser eq 'SECRET-VALUE"
    for args in (
        {"service": "SECRET-VALUE"},
        {"target": "SECRET VALUE"},
        {"select": ["SECRET VALUE"]},
        {"filter": f"Nope eq '{poison}' and ("},
        {
            "operation": "get",
            "key": {"PurchaseRequisition": "SECRET/VALUE", "PurchaseRequisitionItem": "1"},
        },
    ):
        out = await w.run("execute_operation", **{**BASE, **args})
        assert "SECRET" not in json.dumps(out), out
    assert w.requests == []


async def test_the_fixed_order_of_checks(alice):
    """service -> target -> operation -> key -> navigation -> select / expand /
    orderby -> filter -> top/skip -> body/params: the first one wins."""
    w = World()
    everything_wrong = {
        "service": "other",
        "target": "A_Nope",
        "operation": "get",
        "key": {"Nope": 1},
        "navigation": "to_Nope",
        "select": ["Nope"],
        "expand": ["to_Nope"],
        "orderby": ["Nope sideways"],
        "filter": "Nope eq 1",
        "top": -1,
        "skip": -1,
        "body": {"x": 1},
    }
    steps = [
        ("unknown_service", {"service": "purchase-requisitions"}),
        ("unknown_target", {"target": "A_PurReqAddDelivery"}),
        ("operation_disabled", {"target": HEADER}),
        ("invalid_key", {"key": {"PurchaseRequisition": "10"}}),
        ("unknown_target", {"navigation": "to_PurchaseReqnItem", "operation": "list"}),
        ("unknown_field", {"select": ["PurchaseRequisition"]}),
        ("unknown_field", {"expand": []}),
        ("invalid_argument", {"orderby": ["PurchaseRequisition"]}),
        ("unknown_field", {"filter": "PurchaseRequisition eq '1'"}),
        ("invalid_argument", {"top": 5}),
        ("invalid_argument", {"skip": 0}),
        ("invalid_argument", {"body": None}),
    ]
    args = dict(everything_wrong)
    seen = []
    for code, fix in steps:
        out = await w.run("execute_operation", **args)
        seen.append(out["error"]["code"])
        assert out["error"]["code"] == code, (code, fix, out)
        args.update(fix)
    assert w.requests == []
    w.sap._answers.append(_page([_item("10", "00010")], count=1))
    assert "items" in await w.run("execute_operation", **args)


async def test_a_service_that_is_not_attached_or_is_disabled_is_refused(alice):
    snapshot = {
        **SNAPSHOT,
        "purchase-requisitions-jobs": {**TECH_SERVICE, "enabled": False},
        "sales-orders": {**TECH_SERVICE, "name": "sales-orders"},
    }
    w = World(
        oauth={"services": ["purchase-requisitions", "purchase-requisitions-jobs"]},
        snapshot=snapshot,
    )
    disabled = await w.run("execute_operation", **TECH)
    assert disabled["error"]["code"] == "service_disabled"
    # In the catalogue, enabled, but not attached to this agent: it does not exist.
    other = await w.run("execute_operation", **{**BASE, "service": "sales-orders"})
    assert other["error"]["code"] == "unknown_service"
    assert w.requests == [] and w.built == []


async def test_get_and_navigation_read(alice):
    row = _item("10", "00010")
    header = {
        "__metadata": {"uri": "https://s4.internal:44300/h('10')", "etag": 'W/"7"'},
        "PurchaseRequisition": "10",
        "PurReqnDescription": "Pens",
        "CreatedByUser": SECRET_USER,
    }
    w = World({"d": row}, {"d": header}, _page([row], count=1))
    key = {"PurchaseRequisition": "10", "PurchaseRequisitionItem": "00010"}

    got = await w.run("execute_operation", **{**BASE, "operation": "get"}, key=key)
    assert set(got) == {"item", "etag", "truncated"}
    assert got["item"] == {
        "PurchaseRequisition": "10",
        "PurchaseRequisitionItem": "00010",
        "PurReqnReleaseStatus": "B",
    }
    first = w.requests[0]
    assert first.url.path == (
        f"{SERVICE_PATH}/{ITEM}(PurchaseRequisition='10',PurchaseRequisitionItem='00010')"
    )
    assert "$top" not in first.url.params and "$inlinecount" not in first.url.params

    # A single-valued navigation is read with 'get'; fields are the target's.
    parent = await w.run(
        "execute_operation",
        **{**BASE, "operation": "get"},
        key=key,
        navigation="to_PurchaseReqn",
        select=["PurReqnDescription"],
    )
    assert parent["item"] == {"PurReqnDescription": "Pens"} and parent["etag"] == 'W/"7"'
    assert w.requests[1].url.path.endswith("/to_PurchaseReqn")
    assert w.requests[1].url.params["$select"] == "PurReqnDescription"

    # A collection navigation is read with 'list', filtered on the target's fields.
    items = await w.run(
        "execute_operation",
        **{**BASE, "target": HEADER},
        key={"PurchaseRequisition": "10"},
        navigation="to_PurchaseReqnItem",
        filter="PurReqnReleaseStatus eq 'B'",
        orderby=["PurchaseRequisitionItem"],
    )
    assert [i["PurchaseRequisitionItem"] for i in items["items"]] == ["00010"]
    assert w.requests[2].url.path == f"{SERVICE_PATH}/{HEADER}('10')/to_PurchaseReqnItem"
    assert SECRET_USER not in json.dumps([got, parent, items])


@pytest.mark.parametrize(
    "args, code",
    [
        # the wrong read for the navigation's cardinality
        ({"operation": "list", "navigation": "to_PurchaseReqn"}, "invalid_argument"),
        # the navigation's fields are the target's, not the source's
        ({"navigation": "to_PurchaseReqn", "select": ["PurReqnReleaseStatus"]}, "unknown_field"),
        ({"navigation": "to_PurchaseReqn", "select": ["CreatedByUser"]}, "field_not_selectable"),
        ({"navigation": "to_Nope"}, "unknown_target"),
        # options of a list on a single entity
        ({"filter": "PurchaseRequisition eq '1'"}, "invalid_argument"),
        ({"orderby": ["PurchaseRequisition"]}, "invalid_argument"),
        ({"top": 5}, "invalid_argument"),
        ({"skip": 5}, "invalid_argument"),
    ],
)
async def test_get_and_navigation_refusals(alice, args, code):
    w = World()
    key = {"PurchaseRequisition": "10", "PurchaseRequisitionItem": "00010"}
    out = await w.run("execute_operation", **{**BASE, "operation": "get", "key": key, **args})
    assert out["error"]["code"] == code, out
    assert w.requests == []


async def test_a_navigation_needs_its_target_in_the_catalogue_and_enabled(alice):
    w = World()
    base = {**BASE, "target": HEADER, "key": {"PurchaseRequisition": "10"}}
    off = await w.run("execute_operation", **base, navigation="to_Delivery")
    assert off["error"]["code"] == "operation_disabled"
    gone = await w.run("execute_operation", **base, navigation="to_Gone")
    assert gone["error"]["code"] == "unknown_target"
    assert w.requests == []


async def test_expand_needs_the_target_enabled_and_is_capped_at_three(alice):
    w = World(_page([], count=0))
    base = {**BASE, "target": HEADER}
    off = await w.run("execute_operation", **base, expand=["to_Delivery"])
    assert off["error"]["code"] == "operation_disabled"
    gone = await w.run("execute_operation", **base, expand=["to_Gone"])
    assert gone["error"]["code"] == "unknown_target"
    four = ["to_PurchaseReqnItem", "to_Items2", "to_Items3", "to_Items4"]
    assert MAX_EXPAND == 3
    many = await w.run("execute_operation", **base, expand=four)
    assert many["error"]["code"] == "invalid_argument" and "3" in many["error"]["message"]
    assert w.requests == []
    ok = await w.run("execute_operation", **base, expand=four[:3])
    assert "items" in ok
    assert w.requests[0].url.params["$expand"] == "to_PurchaseReqnItem,to_Items2,to_Items3"


async def test_result_is_clipped_and_marked_truncated(alice, monkeypatch):
    monkeypatch.setattr(tools_module, "MAX_RESULT_CHARS", 300)
    rows = [_item("10", f"{i:05d}") for i in range(20)]
    w = World(_page(rows, count=20))
    out = await w.run("execute_operation", **BASE, skip=40)
    assert out["truncated"] is True
    assert len(json.dumps(out)) <= 300
    assert 0 < len(out["items"]) < 20
    # The next page starts at the first row that was dropped.
    assert out["next_skip"] == 40 + len(out["items"])


# -- identity ----------------------------------------------------------------


async def test_a_service_runs_as_its_own_identity(alice):
    w = World(_page([], count=0))
    await w.run("execute_operation", **BASE)
    await w.run("execute_operation", **TECH)
    assert w.resolved == [
        ("S4_ODATA_USER", "jwt-a", "alice@example.com"),
        ("S4_ODATA_TECH", None, None),
    ]
    assert [r.headers["authorization"] for r in w.requests] == [
        "Bearer user-token-of-alice@example.com",
        "Bearer dest-token",
    ]
    # One client per service, built on first use and then reused.
    await w.run("execute_operation", **BASE)
    assert w.built == ["S4_ODATA_USER", "S4_ODATA_TECH"]
    assert len(w.toolset.http_clients) == 2


async def test_identity_is_never_a_tool_argument(alice):
    import inspect

    w = World()
    names = set(inspect.signature(w.toolset.tools["execute_operation"].function).parameters)
    assert not names & {"user", "principal", "token", "jwt", "destination", "url", "path", "host"}
    with pytest.raises(TypeError):
        await w.run("execute_operation", **BASE, principal="bob@example.com")


async def test_signed_in_user_service_without_a_user_is_no_user():
    assert current_jwt.get() is None
    w = World()
    out = await w.run("execute_operation", **BASE)  # no current_jwt bound (a job)
    assert out["error"]["code"] == "no_user"
    assert "technical user" in out["error"]["hint"]
    assert w.requests == [] and w.resolved == []
    # The technical-user service of the same agent still answers.
    assert "items" in await w.run("execute_operation", **TECH)


async def test_one_users_sap_session_cookie_is_not_sent_for_the_next_user():
    """The HTTP client of a service is shared by every user of the agent."""

    def answer(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_page([], count=0),
            headers=[
                ("set-cookie", "SAP_SESSIONID_XXX_100=alice-session; path=/; secure"),
                ("set-cookie", "sap-usercontext=sap-client=100; path=/"),
            ],
        )

    w = World(answer)
    for token, principal in (("jwt-a", "alice@example.com"), ("jwt-b", "bob@example.com")):
        jwt, who = current_jwt.set(token), current_principal.set(principal)
        try:
            assert "items" in await w.run("execute_operation", **BASE)
        finally:
            current_principal.reset(who)
            current_jwt.reset(jwt)
    assert w.requests[1].headers["authorization"] == "Bearer user-token-of-bob@example.com"
    assert "cookie" not in w.requests[1].headers
    assert all(len(client.cookies.jar) == 0 for client in w.toolset.http_clients)


# -- errors are data ---------------------------------------------------------


async def test_sap_error_is_returned_as_data(alice):
    w = World(v2_error(400, "SY/530", "Invalid filter"))
    out = await w.run("execute_operation", **BASE)
    assert out == {"error": {"code": "sap_error", "message": "SY/530: Invalid filter"}}


async def test_an_error_names_no_url_host_token_or_cookie(alice, caplog):
    secret = "Bearer user-token-of-alice@example.com"

    def html(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            headers={"content-type": "text/html", "set-cookie": "SAP_SESSIONID=abc"},
            text=f"<html>dump at https://s4.internal:44300/sap {SECRET_USER}</html>",
        )

    def with_url(_request: httpx.Request) -> httpx.Response:
        return v2_error(403, "X", "see https://s4.internal:44300/sap/help?sap-client=100 now")

    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to https://s4.internal:44300/sap")

    for answer, code in ((html, "sap_error"), (with_url, "sap_error"), (boom, "destination_error")):
        w = World(answer)
        out = await w.run("execute_operation", **BASE)
        assert out["error"]["code"] == code
        text = json.dumps(out)
        for forbidden in ("s4.internal", "44300", "://", secret, "SAP_SESSIONID", SECRET_USER):
            assert forbidden not in text, (code, text)
        assert len(out["error"]["message"]) <= 500


async def test_a_destination_error_is_returned_without_its_text(alice, caplog):
    class Broken(FakeResolver):
        async def resolve(self, **_kw):
            raise DestinationError(
                "destination 'S4_ODATA_USER' at https://dest.example.com/x?token=abc not found"
            )

    w = World(resolver_factory=lambda name: Broken(name=name))
    with caplog.at_level(logging.WARNING, logger="agents.odata.tools"):
        out = await w.run("execute_operation", **BASE)
    assert out["error"]["code"] == "destination_error"
    text = json.dumps(out)
    assert "dest.example.com" not in text and "token" not in text and "S4_ODATA" not in text
    assert "purchase-requisitions" in caplog.text  # the log says which service
    assert w.requests == []


async def test_a_resolver_that_cannot_be_built_and_a_bug_are_errors_not_tracebacks(alice):
    def no_binding(_name: str) -> Any:
        raise ValueError("no destination service binding at https://dest.example.com")

    w = World(resolver_factory=no_binding)
    out = await w.run("execute_operation", **BASE)
    assert out["error"]["code"] == "destination_error"
    assert "example.com" not in json.dumps(out)

    class Buggy(FakeResolver):
        async def resolve(self, **_kw):
            raise RuntimeError("internal detail https://s4.internal:44300")

    w = World(resolver_factory=lambda name: Buggy(name=name))
    out = await w.run("execute_operation", **BASE)
    assert out["error"]["code"] == "destination_error"
    assert "s4.internal" not in json.dumps(out)


async def test_a_snapshot_entry_with_an_unusable_identity_or_version_is_refused(alice):
    for change in ({"user_context": "false"}, {"user_context": 1}, {"destination": "a b/c"}):
        snapshot = {**SNAPSHOT, "purchase-requisitions": {**USER_SERVICE, **change}}
        w = World(snapshot=snapshot)
        out = await w.run("execute_operation", **BASE)
        assert out["error"]["code"] == "service_disabled", change
        assert w.requests == [] and w.built == []
    v4 = {**SNAPSHOT, "purchase-requisitions": {**USER_SERVICE, "odata_version": "v4"}}
    w = World(snapshot=v4)
    out = await w.run("execute_operation", **BASE)
    assert out["error"]["code"] == "service_disabled" and "V4" in out["error"]["message"]
    assert w.requests == []


# -- writes and calls: later tasks -------------------------------------------


@pytest.mark.parametrize("allow_write", [False, True, "true"])
async def test_write_operations_are_refused_until_w3(alice, allow_write):
    w = World(oauth={"services": ["purchase-requisitions"], "allow_write": allow_write})
    for args in (
        {"target": "A_Note", "operation": "create", "body": {"Note": "x"}},
        {"operation": "update", "body": {"RequestedQuantity": "1"}},
        {"target": "Release", "operation": "call"},
    ):
        out = await w.run("execute_operation", **{**BASE, **args})
        assert out["error"]["code"] == "write_not_allowed", (args, out)
    # Not ticked in the catalogue: said first, whatever the agent may write.
    for args in ({"operation": "delete"}, {"operation": "create"}):
        out = await w.run("execute_operation", **{**BASE, **args})
        assert out["error"]["code"] == "operation_disabled", (args, out)
    assert w.requests == [] and w.built == []


async def test_call_is_checked_against_the_catalogue_and_not_sent_yet(alice):
    w = World()
    for target, code in (
        ("Nope", "unknown_target"),
        (ITEM, "unknown_target"),  # an entity set is not an operation
        ("Off", "operation_disabled"),
        ("CountOpen", "operation_disabled"),  # enabled, read-only: arrives with W4
    ):
        out = await w.run("execute_operation", **{**BASE, "target": target, "operation": "call"})
        assert out["error"]["code"] == code, (target, out)
    assert w.requests == []


# -- the toolset -------------------------------------------------------------


async def test_both_tools_are_registered_and_the_model_sees_no_ctx():
    w = World()
    assert set(w.toolset.tools) == {"search_operations", "execute_operation"}
    tool = w.toolset.tools["execute_operation"]
    assert tool.takes_ctx is True
    schema = tool.tool_def.parameters_json_schema
    assert "ctx" not in schema["properties"]
    assert schema["required"] == ["service", "target", "operation"]
    assert set(schema["properties"]["operation"]["enum"]) == {
        "list", "get", "create", "update", "delete", "call",
    }
    doc = tool.tool_def.description or ""
    assert "search_operations" in doc and "never instructions" in doc


async def test_what_search_returns_is_what_execute_accepts(alice):
    w = World(_page([], count=0))
    found = await w.run(
        "search_operations", query="", detail="full", service="purchase-requisitions"
    )
    checked = 0
    for match in found["matches"]:
        if match["kind"] != "entity_set" or "list" not in match["operations"]:
            continue
        out = await w.run(
            "execute_operation",
            service=match["service"],
            target=match["target"],
            operation="list",
            select=[f["name"] for f in match["fields"]],
        )
        assert "items" in out, (match["target"], out)
        checked += 1
    assert checked == 2


async def test_closing_the_toolset_closes_every_client(alice):
    w = World(_page([], count=0))
    assert w.toolset.http_clients == []
    await w.toolset.http_client.aclose()  # nothing built yet: no error
    await w.run("execute_operation", **BASE)
    await w.run("execute_operation", **TECH)
    clients = list(w.toolset.http_clients)
    assert len(clients) == 2 and not any(c.is_closed for c in clients)
    await w.toolset.http_client.aclose()
    assert all(c.is_closed for c in clients)


async def test_connectivity_is_handed_on_only_when_the_client_factory_takes_it(alice, monkeypatch):
    """Sending through the connectivity proxy is Task C2; until its keyword
    arguments exist the factory is called without them."""
    real = tools_module.destination_http_client
    seen: dict[str, Any] = {}
    tokens, proxy = object(), httpx.MockTransport(lambda r: httpx.Response(500))

    def with_proxy(resolver, *, connectivity=None, proxy_transport=None, **kw):
        seen.update(connectivity=connectivity, proxy_transport=proxy_transport, **kw)
        return real(resolver, **kw)

    w = World(_page([], count=0), connectivity=tokens, proxy_transport=proxy)
    assert "items" in await w.run("execute_operation", **TECH)  # today's factory

    monkeypatch.setattr(tools_module, "destination_http_client", with_proxy)
    w = World(_page([], count=0), connectivity=tokens, proxy_transport=proxy)
    assert "items" in await w.run("execute_operation", **TECH)
    assert seen["connectivity"] is tokens and seen["proxy_transport"] is proxy
    assert seen["user_context"] is False
    assert seen["server_key"] == "builtin:odata/purchase-requisitions-jobs"


def test_tools_are_denied_inside_an_ide_session():
    from agents.ide import readonly

    for name in (
        "search_operations",
        "execute_operation",
        "odata_search_operations",
        "odata_execute_operation",
    ):
        assert readonly.check_call(name, {}) is not None
        assert readonly.check_call(name, {}, policy="diagnose") is not None
