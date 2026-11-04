"""The admin API of the InA analytical queries: ``/admin/api/ina/...``.

Design addendum (k), part 2 point 4. The routes are driven through the real
app, so the include line in ``agents/admin.py`` is covered too; the scope
test mounts the router on a bare app with a stub validator, as
``tests/test_odata_admin_api.py`` does (the suite's app runs without an
XSUAA binding, where every scope check passes). SAP is a mock transport
behind the module's seams (``_resolver``, ``_transport``, ``_connectivity``),
with the recorded answers of ``tests/test_ina_client.py``.
"""

from __future__ import annotations

import asyncio
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
    update_ina_query,
)
from agents.ina import admin_calls, admin_routes  # noqa: E402
from agents.ina.errors import ERROR_CODES  # noqa: E402
from agents.odata import admin_routes as odata_routes  # noqa: E402
from tests.odata_helpers import FakeResolver, OnPremiseResolver  # noqa: E402
from tests.test_ina_client import (  # noqa: E402
    DESTINATION,
    QUERY,
    SIA_URL,
    Sap,
    definition,
    fixture,
    grid,
)

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


# --- describe -----------------------------------------------------------------


async def test_describe_reads_sap_stores_the_definition_and_answers_names(client, remote, created):
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "dimensions": 2,
        "measures": 2,
        "variables": 3,
        "names": NAMES,
        "truncated": False,
        "skipped": 0,
        "metadata_fetched_at": body["metadata_fetched_at"],
        "reloaded": False,
        "reload_failed": False,
    }
    assert body["metadata_fetched_at"]
    stored = (await client.get(ONE)).json()
    assert stored["described"] is True
    assert stored["definition"] == definition().model_dump(mode="json")
    assert stored["metadata_fetched_at"] == body["metadata_fetched_at"]
    # The row moved: a form loaded before the describe must reload.
    assert stored["updated_at"] != created["updated_at"]
    assert remote.asked_for == [DESTINATION]
    # GetServerInfo once, then the Metadata request; nothing else.
    assert [(q.method, q.url.path) for q in remote.requests] == [
        ("GET", "/sap/bw/ina/GetServerInfo"),
        ("POST", "/sap/bw/ina/GetResponse"),
    ]
    assert "Metadata" in json.loads(remote.requests[1].content)


async def test_describe_takes_an_empty_object_and_refuses_any_key(client, remote, created):
    assert (await client.post(f"{ONE}/describe", json={})).status_code == 200
    r = await client.post(f"{ONE}/describe", json={"destination": SECRET})
    assert r.status_code == 422 and SECRET not in r.text
    assert r.json() == {"detail": "destination: Extra inputs are not permitted"}


async def test_a_describe_sap_refuses_stores_nothing_and_names_the_code(client, remote, created):
    remote.sap.answers = [fixture("query_unknown.json")]
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == 502
    assert r.headers["x-ina-error"] == "query_unknown"
    assert r.json() == {"detail": "SAP does not know the InA query of this registration"}
    stored = (await client.get(ONE)).json()
    assert stored["described"] is False and stored["updated_at"] == created["updated_at"]


@pytest.mark.parametrize(
    "answer, status, code",
    [
        (httpx.Response(401), 502, "unauthorized"),
        (httpx.Response(403), 502, "forbidden"),
        (httpx.Response(200, text=f"<html>{SECRET}</html>"), 502, "not_json"),
    ],
)
async def test_a_refused_server_info_is_the_clients_code(
    client, remote, created, answer, status, code
):
    remote.sap.info_answer = lambda request: answer
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == status and r.headers["x-ina-error"] == code
    assert code in ERROR_CODES and SECRET not in r.text


async def test_describe_as_the_signed_in_user_needs_one(client, remote):
    assert (await client.post(BASE, json=query(user_context=True))).status_code == 201
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == 424 and r.headers["x-ina-error"] == "user_token_required"
    assert remote.asked_for == [] and remote.requests == []
    # With the admin's own token, the destination is asked as that user.
    headers = bearer("alice")
    r = await client.post(f"{ONE}/describe", headers=headers)
    assert r.status_code == 200, r.text
    ((token, principal),) = {c for c in remote.resolver.calls}
    assert token == headers["Authorization"].removeprefix("Bearer ") and principal


async def test_a_refused_destination_answers_its_admin_text(client, remote, created):
    # OnPremise without a connectivity binding: refused before anything is sent.
    remote.resolver = OnPremiseResolver(name=DESTINATION)
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == 502 and r.headers["x-ina-error"] == "destination_error"
    assert DESTINATION in r.json()["detail"] and "connectivity" in r.json()["detail"]
    assert remote.requests == []


async def test_a_describe_over_the_budget_is_504(client, remote, created, monkeypatch):
    monkeypatch.setattr(admin_calls, "CALL_BUDGET_SECONDS", 0.05)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return remote.sap.handler(request)

    remote.handler = slow
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == 504 and r.headers["x-ina-error"] == "timeout"
    assert (await client.get(ONE)).json()["described"] is False


async def test_a_query_changed_while_it_was_described_is_not_overwritten(
    client, remote, created, monkeypatch
):
    real = admin_calls.read_description

    async def read_and_edit(stored: dict[str, Any]):
        description = await real(stored)
        async with SessionLocal() as s:
            await update_ina_query(s, NAME, {**query(), "destination": "OTHER_DEST"})
        return description

    monkeypatch.setattr(admin_calls, "read_description", read_and_edit)
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == 409
    assert r.json() == {
        "detail": f"Query '{NAME}' was changed while it was described; describe it again"
    }
    stored = (await client.get(ONE)).json()
    assert stored["destination"] == "OTHER_DEST" and stored["described"] is False


async def test_a_query_deleted_while_it_was_described_is_404(client, remote, created, monkeypatch):
    real = admin_calls.read_description

    async def read_and_delete(stored: dict[str, Any]):
        description = await real(stored)
        async with SessionLocal() as s:
            await s.execute(delete(InaQuery))
            await s.commit()
        return description

    monkeypatch.setattr(admin_calls, "read_description", read_and_delete)
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == 404 and r.json() == {"detail": "Query not found"}


async def test_describe_logs_one_line_without_content(client, remote, created, caplog):
    with caplog.at_level(logging.INFO, logger=admin_calls.logger.name):
        await client.post(f"{ONE}/describe")
    lines = [r.getMessage() for r in caplog.records if r.name == admin_calls.logger.name]
    (line,) = [ln for ln in lines if "describe" in ln]
    assert NAME in line and "code=-" in line and "Net amount" not in line


# --- test ---------------------------------------------------------------------


async def test_the_test_runs_the_first_measure_with_top_1_and_answers_no_value(client, remote):
    assert (
        await client.post(BASE, json=query(definition=optional_variables()))
    ).status_code == 201
    remote.sap.answers = [total()]
    r = await client.post(f"{ONE}/test")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"ok", "status", "duration_ms", "columns"}
    assert body["ok"] is True and body["status"] == 200
    assert isinstance(body["duration_ms"], int) and body["columns"] == ["0AMOUNT"]
    assert str(VALUE) not in r.text and "EUR" not in r.text
    (post,) = remote.sap.posts
    sent = json.dumps(json.loads(post.content))
    assert "0AMOUNT" in sent and "0QUANTITY" not in sent
    assert '"MaxResultRecords": 2' in sent  # top 1, plus the one that says "more"


async def test_the_test_of_an_undescribed_query_describes_first_and_stores_nothing(
    client, remote, created
):
    patched = fixture("describe_ok.json")
    remote.sap.answers = [patched, total()]
    # The recorded description has a mandatory variable; make it optional.
    for variable in patched["Cube"]["Variables"]:
        variable["Mandatory"] = False
    r = await client.post(f"{ONE}/test")
    assert r.status_code == 200, r.text
    assert r.json()["columns"] == ["0AMOUNT"]
    assert len(remote.sap.posts) == 2
    stored = (await client.get(ONE)).json()
    assert stored["described"] is False and stored["updated_at"] == created["updated_at"]


async def test_a_mandatory_variable_is_a_refusal_before_the_run_is_sent(client, remote):
    defined = definition().model_dump(mode="json")
    assert (await client.post(BASE, json=query(definition=defined))).status_code == 201
    r = await client.post(f"{ONE}/test")
    assert r.status_code == 422 and r.headers["x-ina-error"] == "variable_required"
    assert r.json() == {"detail": admin_calls.VARIABLE_REQUIRED_TEXT}
    assert remote.sap.posts == []


async def test_a_sia_description_makes_the_test_refuse_before_the_run_is_sent(client, remote):
    # SIA's describe answer (2026-11-04) marks its mandatory variables with
    # ``InputType``; a Test of the described query sent a run without them,
    # and SAP refused it (``run_sia_variables_missing.json``).
    remote.sap.answers = [fixture("describe_sia.json"), fixture("run_sia_variables_missing.json")]
    assert (await client.post(BASE, json=query())).status_code == 201
    assert (await client.post(f"{ONE}/describe")).status_code == 200
    stored = (await client.get(ONE)).json()["definition"]["variables"]
    assert [v["name"] for v in stored if v["mandatory"]] == [
        "!CDS_P_P_DISPLAYCURRENCY",
        "!CDS_P_P_DATEFUNCTION",
    ]
    r = await client.post(f"{ONE}/test")
    assert r.status_code == 422 and r.headers["x-ina-error"] == "variable_required"
    assert r.json() == {"detail": admin_calls.VARIABLE_REQUIRED_TEXT}
    assert len(remote.sap.posts) == 1  # the describe; no run was sent


async def test_a_test_sap_refuses_is_the_clients_code(client, remote):
    assert (
        await client.post(BASE, json=query(definition=optional_variables()))
    ).status_code == 201
    remote.sap.answers = [httpx.Response(500, json={"Messages": [
        {"Type": 2, "Text": f"Fehler {SECRET} at https://sia.internal/x?sap-password=p"}
    ]})]
    r = await client.post(f"{ONE}/test")
    assert r.status_code == 502 and r.headers["x-ina-error"] == "sap_error"
    assert "sia.internal" not in r.text and "sap-password" not in r.text


async def test_a_disabled_query_may_be_tested(client, remote):
    defined = optional_variables()
    body = query(definition=defined, enabled=False)
    assert (await client.post(BASE, json=body)).status_code == 201
    remote.sap.answers = [total()]
    assert (await client.post(f"{ONE}/test")).json()["ok"] is True


async def test_the_test_refuses_a_body_key(client, remote, created):
    r = await client.post(f"{ONE}/test", json={"variables": {"0P_CALYEAR": SECRET}})
    assert r.status_code == 422 and SECRET not in r.text
    assert remote.requests == []


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


async def test_a_describe_of_a_query_in_use_reloads(client, remote, created, live, rebuilt):
    await add_agent("analyst", NAME)
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == 200, r.text
    assert r.json()["reloaded"] is True and live.reloads == 1
    assert rebuilt == [[("ina", NAME)]]


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


# --- the SAP calls: slots, one client builder, refusals (review round 1) ----------


async def test_a_third_call_while_two_run_is_busy_and_sends_nothing(client, remote, created):
    gate = asyncio.Event()

    async def held(request: httpx.Request) -> httpx.Response:
        await gate.wait()
        return remote.sap.handler(request)

    remote.handler = held
    first = asyncio.create_task(client.post(f"{ONE}/describe"))
    second = asyncio.create_task(client.post(f"{ONE}/test"))
    for _ in range(200):
        if admin_calls._slots.locked():
            break
        await asyncio.sleep(0.01)
    assert admin_calls._slots.locked()
    for path in (f"{ONE}/describe", f"{ONE}/test"):
        r = await client.post(path)
        assert r.status_code == 429 and r.headers["x-ina-error"] == "busy"
        assert r.json() == {"detail": admin_calls.BUSY_TEXT}
    asked = len(remote.asked_for)
    gate.set()
    assert (await first).status_code == 200
    await second
    assert asked == 2, "the refused calls resolved nothing"


async def test_a_test_as_the_signed_in_user_needs_one(client, remote):
    body = query(user_context=True, definition=optional_variables())
    assert (await client.post(BASE, json=body)).status_code == 201
    r = await client.post(f"{ONE}/test")
    assert r.status_code == 424 and r.headers["x-ina-error"] == "user_token_required"
    assert r.json() == {"detail": admin_calls.USER_REQUIRED_TEXT}
    assert remote.asked_for == [] and remote.requests == []


async def test_a_stray_value_error_inside_the_client_is_no_destination_text(
    client, remote, created, monkeypatch, caplog
):
    async def broken(self):
        raise ValueError(f"field {SECRET} at https://sia.internal/x")

    monkeypatch.setattr(admin_calls.InaClient, "describe", broken)
    with caplog.at_level(logging.WARNING):
        r = await client.post(f"{ONE}/describe")
    assert r.status_code == 502 and r.headers["x-ina-error"] == "destination_error"
    assert r.json() == {"detail": admin_calls._FAILED_TEXT}
    assert SECRET not in caplog.text and "sia.internal" not in caplog.text
    assert "ValueError" in caplog.text


async def test_a_destination_that_cannot_be_built_is_a_destination_error(
    client, remote, created, monkeypatch, caplog
):
    from agents.destination import DestinationError

    def refused(name: str) -> Any:
        raise DestinationError(f"no binding at https://dest.internal/?token={SECRET}")

    monkeypatch.setattr(admin_calls, "_resolver", refused)
    with caplog.at_level(logging.WARNING):
        r = await client.post(f"{ONE}/describe")
    assert r.status_code == 502 and r.headers["x-ina-error"] == "destination_error"
    assert SECRET not in r.text and SECRET not in caplog.text


def test_the_tools_and_the_admin_calls_build_one_client():
    import ast

    from agents.ina import client as ina_client
    from agents.ina import tools as ina_tools

    assert admin_calls.build_ina_http_client is ina_client.build_ina_http_client
    assert ina_tools.build_ina_http_client is ina_client.build_ina_http_client
    for module in ("tools.py", "admin_calls.py"):
        tree = ast.parse((ROOT / "agents" / "ina" / module).read_text(encoding="utf-8"))
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        assert "destination_http_client" not in names and "NoCookieJar" not in names, module


async def test_the_one_builder_keeps_no_cookie_and_refuses_onpremise_without_a_binding():
    from agents.ina.client import build_ina_http_client
    from agents.odata.session import NoCookieJar

    sent: list[httpx.Request] = []
    http = build_ina_http_client(
        OnPremiseResolver(name=DESTINATION),
        user_context=False,
        server_key="builtin:ina/x",
        connectivity=None,
        transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)),
    )
    async with http:
        assert isinstance(http.cookies.jar, NoCookieJar)
        with pytest.raises(Exception) as info:
            await http.get("/sap/bw/ina/GetServerInfo")
    assert "connectivity service binding" in getattr(info.value, "admin_text", "")
    assert sent == []


async def test_a_description_the_gate_refuses_is_logged_by_rule_and_stores_nothing(
    client, remote, created, monkeypatch, caplog
):
    from agents.ina.models import InaDefinition, InaDimension
    from agents.ina.protocol import InaDescription

    async def twice(stored: dict[str, Any]) -> InaDescription:
        bad = InaDefinition.model_construct(
            dimensions=[InaDimension(name=SECRET), InaDimension(name=SECRET)],
            measures=[],
            variables=[],
        )
        return InaDescription(definition=bad)

    monkeypatch.setattr(admin_calls, "read_description", twice)
    with caplog.at_level(logging.WARNING, logger=admin_routes.logger.name):
        r = await client.post(f"{ONE}/describe")
    assert r.status_code == 502 and r.headers["x-ina-error"] == "sap_error"
    assert r.json() == {"detail": admin_routes._STORE_TEXT}
    (line,) = [rec.getMessage() for rec in caplog.records if rec.name == admin_routes.logger.name]
    assert "definition.dimensions" in line and "distinct" in line and SECRET not in line
    stored = (await client.get(ONE)).json()
    assert stored["described"] is False and stored["updated_at"] == created["updated_at"]


async def test_describe_answers_what_was_cut_and_left_out(client, remote, created, monkeypatch):
    from agents.ina.protocol import InaDescription

    async def cut(stored: dict[str, Any]) -> InaDescription:
        return InaDescription(definition=definition(), skipped=["0ODD", "0ODDER"], truncated=True)

    monkeypatch.setattr(admin_calls, "read_description", cut)
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == 200, r.text
    assert r.json()["truncated"] is True and r.json()["skipped"] == 2


async def test_a_describe_that_changes_nothing_reloads_nothing(client, remote, live, rebuilt):
    await described(client, remote)
    await add_agent("analyst", NAME)
    rebuilt.clear()
    before = (await client.get(ONE)).json()
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == 200, r.text
    assert r.json()["reloaded"] is False and r.json()["reload_failed"] is False
    assert live.reloads == 0 and rebuilt == []
    after = (await client.get(ONE)).json()
    # The stamp says when SAP was read; the definition is the same.
    assert after["definition"] == before["definition"]
    assert after["metadata_fetched_at"] != before["metadata_fetched_at"]
