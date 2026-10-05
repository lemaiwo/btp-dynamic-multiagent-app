"""The OData catalogue admin API: CRUD and duplicate (plan section 1.5).

The routes are driven through the real app (``/admin/api/odata/...``), so the
include line in ``agents/admin.py`` is covered too. The scope tests mount the
router on a bare app with a stub validator: the suite's app runs without an
XSUAA binding, where every scope check passes.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEST_DB = ROOT / "tests" / "_test_odata_admin_api.db"
TEST_DB.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB}"
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

import app as app_module  # noqa: E402
from agents import auth  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    ODataAuditLog,
    ODataService,
    SessionLocal,
    get_odata_service,
    init_db,
    validate_odata_service,
)

BASE = "/admin/api/odata/services"
ONE = f"{BASE}/purchase-requisitions"

GOOD: dict[str, Any] = {
    "name": "purchase-requisitions",
    "title": "Purchase requisitions",
    "purpose": "Read requisitions and their items",
    "destination": "S4_ODATA_USER",
    "user_context": True,
    "odata_version": "v2",
    "service_path": "/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV",
    "definition": {
        "entity_sets": [
            {
                "name": "A_PurchaseRequisitionItem",
                "title": "Requisition item",
                "keys": [{"name": "PurchaseRequisition"}, {"name": "PurchaseRequisitionItem"}],
                "operations": ["list", "get"],
                "fields": [
                    {"name": "PurchaseRequisition", "selectable": True, "filterable": True},
                    {"name": "PurchaseRequisitionItem", "selectable": True},
                    {
                        "name": "PurReqnReleaseStatus",
                        "label": "Release status",
                        "selectable": True,
                        "filterable": True,
                        "writable": True,
                    },
                ],
            }
        ],
        "operations": [
            {
                "name": "ReleaseItem",
                "kind": "function_import",
                "http_method": "POST",
                "bound_to": "A_PurchaseRequisitionItem",
                "parameters": [{"name": "ReleaseCode"}],
            }
        ],
    },
}
GOOD_CLEAN = validate_odata_service(copy.deepcopy(GOOD))

# Never part of an answer: what a refused body carried.
SECRET = "secret-token"


def good(**patch: Any) -> dict[str, Any]:
    data = copy.deepcopy(GOOD)
    data.update(patch)
    return data


def _agent_row(name: str, *, enabled: int, allow_write: bool = False) -> AgentConfig:
    """An agent attaching the service, written without the save-time cleaner
    (the API cannot store a ``builtin:odata`` entry before R2)."""
    block: dict[str, Any] = {"services": ["purchase-requisitions"]}
    if allow_write:
        block["allow_write"] = True
    return AgentConfig(
        name=name,
        description="d",
        instructions="i",
        mcp_url="builtin:odata",
        auth_mode="destination",
        oauth_json=json.dumps(block),
        enabled=enabled,
    )


@pytest.fixture(autouse=True)
async def _clean_tables():
    """Empty OData tables and no agents; another suite may own the engine."""
    await init_db()
    async with SessionLocal() as s:
        for model in (ODataService, ODataAuditLog, AgentConfig):
            await s.execute(delete(model))
        await s.commit()
    yield


@pytest.fixture
async def client():
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def created(client):
    r = await client.post(BASE, json=GOOD)
    assert r.status_code == 201, r.text
    return r.json()


# --- CRUD ---------------------------------------------------------------------


async def test_crud_round_trip(client):
    r = await client.post(BASE, json=GOOD)
    assert r.status_code == 201 and r.json()["used_by"] == []
    assert r.json()["definition"] == GOOD_CLEAN["definition"]
    listed = (await client.get(BASE)).json()
    assert listed[0].keys() >= {"name", "title", "counts", "has_write", "used_by"}
    assert "definition" not in listed[0]
    assert listed[0]["counts"] == {"entity_sets": 1, "operations": 1}
    r = await client.get(ONE)
    assert r.status_code == 200 and r.json()["definition"] == GOOD_CLEAN["definition"]
    r = await client.put(ONE, json={**GOOD, "purpose": "New"})
    assert r.status_code == 200 and r.json()["purpose"] == "New"
    r = await client.delete(ONE)
    assert r.status_code == 204 and r.content == b""
    assert (await client.get(ONE)).status_code == 404
    assert (await client.get(BASE)).json() == []


async def test_list_is_ordered_by_name(client):
    for name in ("stock", "purchase-requisitions", "business-partners"):
        assert (await client.post(BASE, json=good(name=name))).status_code == 201
    names = [s["name"] for s in (await client.get(BASE)).json()]
    assert names == ["business-partners", "purchase-requisitions", "stock"]


async def test_unknown_service_is_404(client):
    for r in (
        await client.get(f"{BASE}/nope"),
        await client.put(f"{BASE}/nope", json=good(name="nope")),
        await client.delete(f"{BASE}/nope"),
        await client.post(f"{BASE}/nope/duplicate", json={"name": "other"}),
    ):
        assert r.status_code == 404 and r.json() == {"detail": "Service not found"}


async def test_duplicate_name_is_409(client, created):
    r = await client.post(BASE, json=good(title="Another"))
    assert r.status_code == 409
    assert r.json()["detail"] == "Service name 'purchase-requisitions' already exists"
    assert (await client.get(ONE)).json()["title"] == "Purchase requisitions"


async def test_rename_through_put_is_422(client, created):
    r = await client.put(ONE, json=good(name="renamed"))
    assert r.status_code == 422
    assert r.json()["detail"] == "name cannot be changed; duplicate the service instead"
    assert (await client.get(f"{BASE}/renamed")).status_code == 404
    assert (await client.get(ONE)).status_code == 200


async def test_put_replaces_the_definition_and_flags(client, created):
    body = good(enabled=False, user_context=False, destination="S4_ODATA_TECH")
    body["definition"] = {"entity_sets": [], "operations": []}
    r = await client.put(ONE, json=body)
    assert r.status_code == 200
    got = r.json()
    assert got["enabled"] is False and got["user_context"] is False
    assert got["destination"] == "S4_ODATA_TECH"
    assert got["counts"] == {"entity_sets": 0, "operations": 0} and got["has_write"] is False
    assert got["id"] == created["id"]


# --- refusals never echo the input ---------------------------------------------


async def test_invalid_definition_is_422_without_echoing_input(client):
    r = await client.post(BASE, json=good(service_path=f"https://{SECRET}@evil.example"))
    assert r.status_code == 422 and SECRET not in r.text
    assert "service_path" in r.json()["detail"]
    assert (await client.get(BASE)).json() == []


@pytest.mark.parametrize(
    "patch",
    [
        {"name": f"Bad Name {SECRET}"},
        {"destination": f"https://user:{SECRET}@evil.example/"},
        {"odata_version": SECRET},
        {"title": {"nested": SECRET}},
        {"unknown_key": SECRET},
        {"definition": {"entity_sets": [{"name": SECRET + " x", "keys": [], "fields": []}]}},
        {"definition": SECRET},
    ],
)
async def test_refused_fields_are_never_echoed(client, created, patch):
    for r in (
        await client.post(BASE, json=good(**{"name": "other", **patch})),
        await client.put(ONE, json=good(**patch)),
    ):
        assert r.status_code == 422, r.text
        assert SECRET not in r.text
        assert isinstance(r.json()["detail"], str)


@pytest.mark.parametrize(
    "raw",
    [
        f'["{SECRET}"]',
        f'"{SECRET}"',
        f'{{"name": "{SECRET}"',  # cut off: not JSON
        "null",
        "",
    ],
)
async def test_a_body_that_is_no_object_is_422_without_echo(client, created, raw):
    headers = {"content-type": "application/json"}
    for r in (
        await client.post(BASE, content=raw, headers=headers),
        await client.put(ONE, content=raw, headers=headers),
        await client.post(f"{ONE}/duplicate", content=raw, headers=headers),
    ):
        assert r.status_code == 422, r.text
        assert SECRET not in r.text


async def test_a_refused_put_changes_nothing(client, created):
    r = await client.put(ONE, json=good(purpose="   "))
    assert r.status_code == 422
    assert (await client.get(ONE)).json()["purpose"] == GOOD["purpose"]


# --- duplicate ----------------------------------------------------------------


async def test_duplicate_copies_the_definition_under_a_new_identity(client, created):
    r = await client.post(
        f"{ONE}/duplicate",
        json={
            "name": "purchase-requisitions-jobs",
            "destination": "S4_ODATA_TECH",
            "user_context": False,
        },
    )
    assert r.status_code == 201, r.text
    copy_ = r.json()
    assert copy_["user_context"] is False and copy_["definition"] == GOOD_CLEAN["definition"]
    assert copy_["name"] == "purchase-requisitions-jobs"
    assert copy_["destination"] == "S4_ODATA_TECH"
    assert copy_["title"] == "Purchase requisitions (copy)"
    assert copy_["id"] != created["id"] and copy_["used_by"] == []
    for key in ("purpose", "odata_version", "service_path", "enabled", "not_for"):
        assert copy_[key] == created[key], key
    # The original is untouched.
    original = (await client.get(ONE)).json()
    assert original["destination"] == "S4_ODATA_USER" and original["user_context"] is True


async def test_duplicate_keeps_identity_fields_it_is_not_given(client, created):
    r = await client.post(f"{ONE}/duplicate", json={"name": "pr-copy", "title": "  Mine  "})
    assert r.status_code == 201
    assert r.json()["title"] == "Mine"
    assert r.json()["destination"] == "S4_ODATA_USER" and r.json()["user_context"] is True


async def test_duplicate_default_title_fits_the_limit(client):
    assert (await client.post(BASE, json=good(title="T" * 120))).status_code == 201
    r = await client.post(f"{ONE}/duplicate", json={"name": "pr-copy"})
    assert r.status_code == 201, r.text
    assert r.json()["title"].endswith(" (copy)") and len(r.json()["title"]) == 120


async def test_duplicate_onto_a_taken_name_is_409(client, created):
    for name in ("purchase-requisitions", "taken"):
        if name == "taken":
            assert (await client.post(BASE, json=good(name=name))).status_code == 201
        r = await client.post(f"{ONE}/duplicate", json={"name": name})
        assert r.status_code == 409
        assert r.json()["detail"] == f"Service name '{name}' already exists"
    assert len((await client.get(BASE)).json()) == 2


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": f"Bad {SECRET}"},
        {"name": "ok-name", "destination": f"https://{SECRET}@evil.example"},
        {"name": "ok-name", "title": "   "},
        {"name": "ok-name", "title": "T" * 121},
        {"name": "ok-name", "user_context": SECRET},
        {"name": "ok-name", "definition": {"entity_sets": [SECRET]}},
        {"name": "ok-name", "service_path": f"/{SECRET}"},
    ],
)
async def test_duplicate_refuses_a_bad_body_without_echo(client, created, body):
    r = await client.post(f"{ONE}/duplicate", json=body)
    assert r.status_code == 422, r.text
    assert SECRET not in r.text
    assert len((await client.get(BASE)).json()) == 1


# --- used_by and delete -------------------------------------------------------


async def test_used_by_names_the_agents_in_list_and_detail(client, created):
    async with SessionLocal() as s:
        s.add(_agent_row("buyer", enabled=1, allow_write=True))
        s.add(_agent_row("reader", enabled=0))
        await s.commit()
    for got in ((await client.get(ONE)).json(), (await client.get(BASE)).json()[0]):
        assert [(u["agent"], u["enabled"], u["allow_write"]) for u in got["used_by"]] == [
            ("buyer", True, True),
            ("reader", False, False),
        ]
    # A service nobody attaches stays empty, in the same list call.
    assert (await client.post(BASE, json=good(name="stock"))).status_code == 201
    by_name = {s["name"]: s["used_by"] for s in (await client.get(BASE)).json()}
    assert by_name["stock"] == [] and len(by_name["purchase-requisitions"]) == 2


async def test_delete_in_use_is_409_naming_enabled_and_disabled_agents(client, created):
    """Rows written directly; the same path through the agent API is the
    xfail below."""
    async with SessionLocal() as s:
        s.add(_agent_row("buyer", enabled=1))
        s.add(_agent_row("reader", enabled=0))
        await s.commit()
    r = await client.delete(ONE)
    assert r.status_code == 409
    assert r.json()["detail"] == (
        "Service 'purchase-requisitions' is used by agent(s) 'buyer', 'reader'"
    )
    async with SessionLocal() as s:
        assert await get_odata_service(s, "purchase-requisitions") is not None
    # Once nobody uses it any more, it goes.
    async with SessionLocal() as s:
        await s.execute(delete(AgentConfig))
        await s.commit()
    assert (await client.delete(ONE)).status_code == 204


@pytest.mark.xfail(strict=True, reason="R2")
async def test_delete_in_use_is_409_naming_the_agents(client, created):
    """Through the agent API: red until R2 lets an agent store the entry."""
    r = await client.post(
        "/admin/api/agents",
        json={
            "name": "buyer",
            "description": "d",
            "instructions": "i",
            "mcp_servers": [
                {
                    "url": "builtin:odata",
                    "auth_mode": "destination",
                    "oauth": {"services": ["purchase-requisitions"], "allow_write": True},
                }
            ],
        },
    )
    assert r.status_code in (200, 201), r.text
    r = await client.delete(ONE)
    assert r.status_code == 409 and "'buyer'" in r.json()["detail"]


# --- authorization ------------------------------------------------------------


def _routes():
    from agents.odata.admin_routes import router

    return router.routes


def test_every_route_requires_the_admin_scope():
    from agents.auth import require_admin

    routes = _routes()
    assert routes
    for route in routes:
        deps = [d.call for d in route.dependant.dependencies]
        assert require_admin in deps, route.path


def test_the_app_serves_the_routes_under_admin_api_odata():
    """Read from the OpenAPI document: this FastAPI keeps an included router
    as one lazy entry in ``app.routes``, so the paths are not listed there."""
    paths = app_module.app.openapi()["paths"]
    served = {
        (path, method.upper())
        for path, item in paths.items()
        if "/odata/" in path
        for method in item
    }
    for expected in (
        (BASE, "GET"),
        (BASE, "POST"),
        (f"{BASE}/{{name}}", "GET"),
        (f"{BASE}/{{name}}", "PUT"),
        (f"{BASE}/{{name}}", "DELETE"),
        (f"{BASE}/{{name}}/duplicate", "POST"),
    ):
        assert expected in served, expected
    # Nothing of the catalogue is served outside /admin.
    assert all(path.startswith("/admin/api/odata/") for path, _ in served)


class _StubValidator:
    xsappname = "app"

    def __init__(self, claims: dict[str, dict[str, Any]]) -> None:
        self._claims = claims

    def validate(self, token: str) -> dict[str, Any]:
        if token not in self._claims:
            raise HTTPException(status_code=401, detail="Invalid token")
        return self._claims[token]

    def has_scope(self, payload: dict[str, Any], scope: str) -> bool:
        return scope in (payload.get("scope") or [])


async def test_without_the_admin_scope_every_route_is_refused(monkeypatch, created):
    """Behaviour, not wiring: each route and method, with no token (401), a
    signed-in user without the scope and a developer (403). Nothing is
    read, changed or deleted on a refusal."""
    validator = _StubValidator(
        {
            "usr": {"user_name": "u", "scope": ["user"]},
            "dev": {"user_name": "d", "scope": ["developer", "user", "a2a"]},
            "adm": {"user_name": "a", "scope": ["admin"]},
        }
    )
    monkeypatch.setattr(auth, "get_validator", lambda: validator)
    token = auth.current_claims.set(None)
    try:
        from agents.odata.admin_routes import router

        bare = FastAPI()
        bare.include_router(router, prefix="/admin")
        calls = [
            (m, route.path.replace("{name}", "purchase-requisitions"))
            for route in router.routes
            for m in route.methods - {"HEAD"}
        ]
        assert len(calls) >= 6
        async with AsyncClient(transport=ASGITransport(app=bare), base_url="http://test") as c:
            for method, path in calls:
                url = f"/admin{path}"
                body = {"name": "pr-copy"} if path.endswith("/duplicate") else GOOD
                kwargs = {} if method in ("GET", "DELETE") else {"json": body}
                r = await c.request(method, url, **kwargs)
                assert r.status_code == 401, (method, path, r.status_code)
                for bearer in ("usr", "dev"):
                    r = await c.request(
                        method, url, headers={"Authorization": f"Bearer {bearer}"}, **kwargs
                    )
                    assert r.status_code == 403, (method, path, bearer, r.status_code)
                    assert r.json() == {"detail": "Admin scope required"}
            # The refusals left the catalogue as it was.
            r = await c.get("/admin/api/odata/services", headers={"Authorization": "Bearer adm"})
            assert r.status_code == 200
            assert [s["name"] for s in r.json()] == ["purchase-requisitions"]
            assert r.json()[0]["purpose"] == GOOD["purpose"]
    finally:
        auth.current_claims.reset(token)
