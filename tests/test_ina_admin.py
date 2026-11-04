"""The admin API of the InA analytical queries: ``/admin/api/ina/...``.

Design addendum (k), part 2 point 4. The routes are driven through the real
app, so the include line in ``agents/admin.py`` is covered too; the scope
test mounts the router on a bare app with a stub validator, as
``tests/test_odata_admin_api.py`` does (the suite's app runs without an
XSUAA binding, where every scope check passes). SAP is a mock transport
behind the module's seams (``_resolver``, ``_transport``, ``_connectivity``),
with the recorded answers and doubles of ``tests/ina_helpers.py``.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
for _v in (
    "VCAP_SERVICES",
    "VCAP_APPLICATION",
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
from agents.chat_app import dynamic_chat_app  # noqa: E402
from agents.db import (  # noqa: E402
    INA_NAME_FIXED,
    INA_NAME_TAKEN,
    AgentConfig,
    InaQuery,
    SessionLocal,
    create_ina_query,
    get_ina_query,
    init_db,
)
from agents.ina import admin_calls, admin_routes  # noqa: E402
from agents.odata import admin_routes as odata_routes  # noqa: E402
from tests.ina_helpers import (  # noqa: E402
    DESTINATION,
    QUERY,
    SIA_URL,
    Sap,
    definition,
    fixture,
    grid,
)
from tests.odata_helpers import FakeResolver  # noqa: E402

BASE = "/admin/api/ina/queries"
NAME = "sales-by-month"
ONE = f"{BASE}/{NAME}"
# Never part of an answer or a log line: what a refused body carried, and a
# value SAP answered.
SECRET = "Zq9!secret-value"
VALUE = 4711.25
NAMES = ["0CALMONTH", "ZREGION", "0AMOUNT", "0QUANTITY", "0P_CALYEAR", "ZV_REGION", "ZV_PERIOD"]


def query(name: str = NAME, **patch: Any) -> dict[str, Any]:
    """A registration as the client posts it: not described yet."""
    data: dict[str, Any] = {
        "name": name,
        "title": "Sales by month",
        "purpose": "Net sales and quantities per month and region",
        "not_for": "Forecasts",
        "destination": DESTINATION,
        "user_context": False,
        "query": QUERY,
        "package": "",
        "enabled": True,
    }
    data.update(patch)
    return data


def optional_variables() -> dict[str, Any]:
    """The recorded definition without its mandatory variable."""
    data = definition().model_dump(mode="json")
    for variable in data["variables"]:
        variable["mandatory"] = False
    return data


def total(measure: str = "0AMOUNT") -> dict[str, Any]:
    """The one total row of a run without ``group_by``."""
    return grid([], [[]], [measure], [{"Value": VALUE, "Unit": "EUR"}])


async def add_agent(name: str, *queries: str, enabled: int = 1) -> None:
    """An agent naming the queries, written as a row: these tests are about
    the query routes, not the agent save gate."""
    async with SessionLocal() as s:
        s.add(AgentConfig(
            name=name, description="d", instructions="i", mcp_url="builtin:ina",
            auth_mode="destination", oauth_json=json.dumps({"queries": list(queries)}),
            enabled=enabled,
        ))
        await s.commit()


def bearer(user: str) -> dict[str, str]:
    token = jwt.encode({"user_uuid": user, "scope": ["app.admin"]}, "k" * 32, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


class Remote:
    """The destination and SAP of one test."""

    def __init__(self) -> None:
        self.sap = Sap()
        self.sap.answers = [fixture("describe_ok.json")]
        self.resolver: Any = FakeResolver(url=SIA_URL, name=DESTINATION)
        self.asked_for: list[str] = []
        self.handler: Any = None

    @property
    def requests(self) -> list[httpx.Request]:
        return self.sap.requests


@pytest.fixture
def remote(monkeypatch) -> Remote:
    state = Remote()

    def resolver(name: str) -> Any:
        state.asked_for.append(name)
        return state.resolver

    def transport() -> httpx.AsyncBaseTransport:
        return httpx.MockTransport(state.handler or state.sap.handler)

    monkeypatch.setattr(admin_calls, "_resolver", resolver)
    monkeypatch.setattr(admin_calls, "_transport", transport)
    monkeypatch.setattr(admin_calls, "_connectivity", lambda: None)
    return state


@pytest.fixture(autouse=True)
async def _clean_tables():
    await init_db()
    async with SessionLocal() as s:
        for model in (InaQuery, AgentConfig):
            await s.execute(delete(model))
        await s.commit()
    yield


@pytest.fixture(autouse=True)
def _every_slot_is_given_back():
    assert admin_calls._slots._value == admin_calls.MAX_CONCURRENT_CALLS
    yield
    assert admin_calls._slots._value == admin_calls.MAX_CONCURRENT_CALLS


@pytest.fixture
async def client():
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def created(client) -> dict[str, Any]:
    r = await client.post(BASE, json=query())
    assert r.status_code == 201, r.text
    return r.json()


async def described(client, remote) -> dict[str, Any]:
    """The query, created and described through the routes."""
    assert (await client.post(BASE, json=query())).status_code == 201
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == 200, r.text
    return (await client.get(ONE)).json()


# --- CRUD ---------------------------------------------------------------------


async def test_crud_round_trip(client):
    r = await client.post(BASE, json=query())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["described"] is False and body["definition"] is None
    assert body["metadata_fetched_at"] is None and body["used_by"] == []
    assert body["updated_at"]
    listed = (await client.get(BASE)).json()
    assert [q["name"] for q in listed] == [NAME]
    assert "definition" not in listed[0]
    assert listed[0].keys() >= {
        "name", "title", "purpose", "not_for", "destination", "user_context", "query",
        "package", "enabled", "described", "metadata_fetched_at", "updated_at", "used_by",
    }
    r = await client.get(ONE)
    assert r.status_code == 200 and r.json()["title"] == "Sales by month"
    r = await client.put(ONE, json=query(purpose="New purpose"))
    assert r.status_code == 200, r.text
    assert r.json()["purpose"] == "New purpose"
    assert r.json()["reloaded"] is False and r.json()["reload_failed"] is False
    r = await client.delete(ONE)
    assert r.status_code == 204 and r.content == b""
    assert r.headers["x-ina-reloaded"] == "false"
    assert r.headers["x-ina-reload-failed"] == "false"
    assert (await client.get(ONE)).status_code == 404
    assert (await client.get(BASE)).json() == []


async def test_a_created_definition_is_stored_and_marked_described(client):
    r = await client.post(BASE, json=query(definition=definition().model_dump(mode="json")))
    assert r.status_code == 201, r.text
    assert r.json()["described"] is True and r.json()["metadata_fetched_at"]
    assert r.json()["definition"] == definition().model_dump(mode="json")
    (row,) = (await client.get(BASE)).json()
    assert row["described"] is True


async def test_the_list_is_ordered_by_name(client):
    for name in ("stock-levels", NAME, "cost-centres"):
        assert (await client.post(BASE, json=query(name))).status_code == 201
    assert [q["name"] for q in (await client.get(BASE)).json()] == [
        "cost-centres", NAME, "stock-levels",
    ]


@pytest.mark.parametrize("name", ["nope", "Not-A-Name", "a" * 70, "x%00"])
async def test_an_unknown_query_is_404(client, name):
    path = f"{BASE}/{name}"
    for r in (
        await client.get(path),
        await client.put(path, json=query(name="nope")),
        await client.delete(path),
        await client.post(f"{path}/describe"),
        await client.post(f"{path}/test"),
    ):
        assert r.status_code == 404 and r.json() == {"detail": "Query not found"}


async def test_a_duplicate_name_is_409(client, created):
    r = await client.post(BASE, json=query(title="Another"))
    assert r.status_code == 409 and r.json() == {"detail": INA_NAME_TAKEN}
    assert (await client.get(ONE)).json()["title"] == "Sales by month"


async def test_a_rename_through_put_is_409(client, created):
    r = await client.put(ONE, json=query(name="renamed"))
    assert r.status_code == 409 and r.json() == {"detail": INA_NAME_FIXED}
    assert (await client.get(f"{BASE}/renamed")).status_code == 404
    assert (await client.get(ONE)).json()["updated_at"] == created["updated_at"]


async def test_a_put_with_the_loaded_stamp_saves_and_answers_the_next_one(client, created):
    r = await client.put(
        ONE, json={**query(title="Second"), "expected_updated_at": created["updated_at"]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Second" and r.json()["updated_at"] != created["updated_at"]
    # A save from the tab that still holds the first stamp is refused.
    r2 = await client.put(
        ONE, json={**query(title="Stale"), "expected_updated_at": created["updated_at"]}
    )
    assert r2.status_code == 409
    assert r2.json() == {
        "detail": f"Query '{NAME}' was changed since it was loaded; reload it and save again"
    }
    assert (await client.get(ONE)).json()["title"] == "Second"
    # The answer's stamp is the one the next save sends.
    r3 = await client.put(
        ONE, json={**query(title="Third"), "expected_updated_at": r.json()["updated_at"]}
    )
    assert r3.status_code == 200, r3.text


@pytest.mark.parametrize(
    "value, detail",
    [
        (17, "expected_updated_at: Input should be a valid string"),
        (f"yesterday {SECRET}", "expected_updated_at: expected the updated_at this query "
         "was loaded with"),
    ],
)
async def test_an_expected_stamp_that_is_no_timestamp_is_422(client, created, value, detail):
    r = await client.put(ONE, json={**query(), "expected_updated_at": value})
    assert r.status_code == 422 and r.json() == {"detail": detail}


async def test_a_put_without_the_stamp_is_one_warning(client, created, caplog):
    with caplog.at_level(logging.WARNING, logger=admin_routes.logger.name):
        r = await client.put(ONE, json=query(title=f"Title {SECRET}"))
    assert r.status_code == 200
    (line,) = [rec.getMessage() for rec in caplog.records if rec.name == admin_routes.logger.name]
    assert NAME in line and "expected_updated_at" in line and SECRET not in line


async def test_a_form_save_keeps_what_describe_stored(client, remote):
    stored = await described(client, remote)
    assert stored["described"] is True
    r = await client.put(ONE, json=query(title="Edited"))
    assert r.status_code == 200
    assert r.json()["definition"] == stored["definition"]
    assert r.json()["metadata_fetched_at"] == stored["metadata_fetched_at"]
    r = await client.put(ONE, json={**query(), "definition": None})
    assert r.json()["described"] is False and r.json()["metadata_fetched_at"] is None


@pytest.mark.parametrize("enabled", [1, 0])
async def test_delete_is_409_while_an_agent_names_the_query(client, created, enabled):
    await add_agent("analyst", NAME, enabled=enabled)
    r = await client.delete(ONE)
    assert r.status_code == 409
    assert r.json() == {"detail": f"Query '{NAME}' is used by agent(s) 'analyst'"}
    assert (await client.get(ONE)).status_code == 200
    (row,) = (await client.get(BASE)).json()
    assert [u["agent"] for u in row["used_by"]] == ["analyst"]


# --- refusals never echo the input --------------------------------------------


@pytest.mark.parametrize(
    "patch",
    [
        {"title": f"two\nlines {SECRET}"},
        {"destination": f"https://user:{SECRET}@evil.example/"},
        {"query": ""},
        {"user_context": SECRET},
        {"unknown_key": SECRET},
        {"definition": {"dimensions": [{"name": f"{SECRET}\n"}]}},
        {"definition": SECRET},
    ],
)
async def test_refused_fields_are_never_echoed(client, created, patch):
    for r in (
        await client.post(BASE, json={**query("other"), **patch}),
        await client.put(ONE, json={**query(), **patch}),
    ):
        assert r.status_code == 422, r.text
        assert SECRET not in r.text and isinstance(r.json()["detail"], str)
    assert [q["name"] for q in (await client.get(BASE)).json()] == [NAME]


@pytest.mark.parametrize(
    "raw", [f'["{SECRET}"]', f'"{SECRET}"', f'{{"name": "{SECRET}"', "null", ""]
)
async def test_a_body_that_is_no_object_is_422_without_echo(client, created, raw):
    for method, path in (("POST", BASE), ("PUT", ONE)):
        r = await client.request(
            method, path, content=raw, headers={"Content-Type": "application/json"}
        )
        assert r.status_code == 422 and r.json() == {"detail": "body: expected a JSON object"}


async def test_a_body_over_the_cap_is_413_before_it_is_parsed(client):
    big = json.dumps(query(title="x" * (admin_routes.MAX_BODY_BYTES + 10)))
    r = await client.post(BASE, content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json() == {"detail": "Request body too large"}


# --- reload of a query in use ---------------------------------------------------


class Live:
    """A running build as the routes see it: which queries it holds, and its
    reloads counted."""

    def __init__(self, holds: set[str] | None = None) -> None:
        self.holds = holds or set()
        self.reloads = 0
        self.fail: Exception | None = None

    def holds_ina_query(self, name: str) -> bool:
        return name in self.holds

    def holds_odata_service(self, name: str) -> bool:
        return False

    async def reload(self) -> None:
        self.reloads += 1
        if self.fail is not None:
            raise self.fail


@pytest.fixture
def live(monkeypatch) -> Live:
    world = Live()
    monkeypatch.setattr(odata_routes, "_live_registry", lambda: world)
    monkeypatch.setattr(dynamic_chat_app, "refresh", lambda: None)
    return world


@pytest.fixture
def rebuilt(monkeypatch) -> list[Any]:
    """The data catalogue sources rebuilt after a change."""
    from agents.data_catalog import importer

    calls: list[Any] = []

    async def rebuild_safely(sources=None, *, what="change"):
        calls.append(sources)
        return {"imported": 0, "removed": 0, "vectors": 0}

    monkeypatch.setattr(importer, "rebuild_safely", rebuild_safely)
    return calls


async def test_a_put_of_a_query_in_use_reloads_and_rebuilds_its_source(
    client, created, live, rebuilt
):
    await add_agent("analyst", NAME)
    r = await client.put(ONE, json=query(enabled=False))
    assert r.status_code == 200, r.text
    assert r.json()["reloaded"] is True and r.json()["reload_failed"] is False
    assert live.reloads == 1 and rebuilt == [[("ina", NAME)]]


async def test_a_put_of_a_query_nobody_uses_does_not_reload(client, created, live, rebuilt):
    r = await client.put(ONE, json=query(enabled=False))
    assert r.json()["reloaded"] is False and live.reloads == 0
    assert rebuilt == [[("ina", NAME)]]


async def test_a_delete_of_a_query_the_build_still_holds_reloads(client, created, live, rebuilt):
    live.holds = {NAME}
    r = await client.delete(ONE)
    assert r.status_code == 204
    assert r.headers["x-ina-reloaded"] == "true" and r.headers["x-ina-reload-failed"] == "false"
    assert live.reloads == 1 and rebuilt == [[("ina", NAME)]]


async def test_a_reload_that_fails_after_the_save_is_reported_not_raised(
    client, created, live, rebuilt, caplog
):
    await add_agent("analyst", NAME)
    live.fail = RuntimeError(f"token endpoint said {SECRET}")
    with caplog.at_level(logging.ERROR):
        r = await client.put(ONE, json=query(title="Saved anyway"))
    assert r.status_code == 200
    assert r.json()["reloaded"] is False and r.json()["reload_failed"] is True
    assert (await client.get(ONE)).json()["title"] == "Saved anyway"
    assert SECRET not in caplog.text


async def test_a_create_reloads_nothing(client, live, rebuilt):
    assert (await client.post(BASE, json=query())).status_code == 201
    assert live.reloads == 0 and rebuilt == []


# --- credential health ------------------------------------------------------------


async def test_credential_health_lists_each_attached_query_once(client, created, monkeypatch):
    await client.post(BASE, json=query("stock-levels", user_context=True, enabled=False))
    await add_agent("analyst", NAME, "stock-levels", "gone")
    r = await client.get("/admin/api/credential-health")
    assert r.status_code == 200, r.text
    entries = [e for e in r.json()["destinations"] if e["server_key"] == "builtin:ina"]
    assert [(e["query"], e["query_enabled"], e["state"]) for e in entries] == [
        (NAME, True, "unbound"),
        ("stock-levels", False, "unbound"),
        ("gone", False, "missing"),
    ]
    first = entries[0]
    assert first.keys() >= {
        "agent", "server_key", "query", "query_enabled", "destination", "user_context",
        "state", "auth_type", "error",
    }
    assert first["agent"] == "analyst" and first["destination"] == DESTINATION
    assert entries[1]["user_context"] is True
    assert entries[2]["error"] == "unknown InA query"


# --- authorization ------------------------------------------------------------


def test_every_route_requires_the_admin_scope():
    from agents.auth import require_admin

    assert admin_routes.router.routes
    for route in admin_routes.router.routes:
        deps = [d.call for d in route.dependant.dependencies]
        assert require_admin in deps, route.path


def test_the_app_serves_the_routes_under_admin_api_ina():
    paths = app_module.app.openapi()["paths"]
    served = {(p, m.upper()) for p, item in paths.items() if "/ina/" in p for m in item}
    assert served == {
        (BASE, "GET"),
        (BASE, "POST"),
        (f"{BASE}/{{name}}", "GET"),
        (f"{BASE}/{{name}}", "PUT"),
        (f"{BASE}/{{name}}", "DELETE"),
        (f"{BASE}/{{name}}/describe", "POST"),
        (f"{BASE}/{{name}}/test", "POST"),
    }


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


async def test_without_the_admin_scope_every_route_is_refused(monkeypatch, remote, created):
    validator = _StubValidator({
        "usr": {"user_name": "u", "scope": ["user"]},
        "dev": {"user_name": "d", "scope": ["developer", "user", "a2a"]},
        "adm": {"user_name": "a", "scope": ["admin"]},
    })
    monkeypatch.setattr(auth, "get_validator", lambda: validator)
    token = auth.current_claims.set(None)
    try:
        bare = FastAPI()
        bare.include_router(admin_routes.router, prefix="/admin")
        calls = [
            (m, route.path.replace("{name}", NAME))
            for route in admin_routes.router.routes
            for m in route.methods - {"HEAD"}
        ]
        assert len(calls) == 7
        async with AsyncClient(transport=ASGITransport(app=bare), base_url="http://test") as c:
            for method, path in calls:
                url = f"/admin{path}"
                kwargs = {} if method in ("GET", "DELETE") else {"json": query()}
                if path.endswith(("/describe", "/test")):
                    kwargs = {"json": {}}
                r = await c.request(method, url, **kwargs)
                assert r.status_code == 401, (method, path, r.status_code)
                for who in ("usr", "dev"):
                    r = await c.request(
                        method, url, headers={"Authorization": f"Bearer {who}"}, **kwargs
                    )
                    assert r.status_code == 403, (method, path, who, r.status_code)
                    assert r.json() == {"detail": "Admin scope required"}
            r = await c.get("/admin/api/ina/queries", headers={"Authorization": "Bearer adm"})
            assert r.status_code == 200 and [q["name"] for q in r.json()] == [NAME]
        assert remote.requests == [], "no refusal reached SAP"
        async with SessionLocal() as s:
            row = await get_ina_query(s, NAME)
        assert row is not None and row.definition_json is None
    finally:
        auth.current_claims.reset(token)


def test_the_sap_calls_never_import_the_database_layer():
    import ast

    tree = ast.parse((ROOT / "agents" / "ina" / "admin_calls.py").read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert "agents.db" not in imported and "agents.admin" not in imported


async def test_a_stored_row_written_around_the_gate_still_lists(client):
    async with SessionLocal() as s:
        await create_ina_query(s, query())
        row = await get_ina_query(s, NAME)
        row.definition_json = "not json"
        await s.commit()
    (listed,) = (await client.get(BASE)).json()
    assert listed["described"] is False
    assert copy.deepcopy(listed)["name"] == NAME
