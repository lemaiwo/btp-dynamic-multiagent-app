"""``GET`` / ``PUT /admin/api/model-prices`` (``agents/cost_routes.py``): the
admin's price table, replaced as a whole under ``expected_updated_at``.
A refusal names the field and the rule, never a submitted value."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from agents import cost_routes  # noqa: E402
from agents.auth import current_principal, require_admin  # noqa: E402
from agents.db import (  # noqa: E402
    ModelPrice,
    ModelUsage,
    OrchestratorConfig,
    SessionLocal,
    init_db,
)

URL = "/admin/api/model-prices"
EMPTY = {"currency": "EUR", "capacity_unit_price": None, "prices": [],
         "unpriced_models": [], "updated_at": None}


@pytest.fixture(autouse=True)
async def clean():
    await init_db()
    async with SessionLocal() as session:
        await session.execute(delete(ModelPrice))
        await session.execute(delete(ModelUsage))
        await session.execute(update(OrchestratorConfig).values(
            capacity_unit_price=None, prices_updated_at=None))
        await session.commit()
    yield


@pytest.fixture
async def client():
    app = FastAPI()

    @app.middleware("http")
    async def bind_principal(request: Request, call_next):
        token = current_principal.set("admin@example.com")
        try:
            return await call_next(request)
        finally:
            current_principal.reset(token)

    app.include_router(cost_routes.router, prefix="/admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


def body(expected=None, cu="7.5", prices=None) -> dict:
    return {
        "expected_updated_at": expected,
        "capacity_unit_price": cu,
        "prices": prices if prices is not None else [
            {"model": "gpt-4o", "input_rate": "0.5", "output_rate": 2, "cache_read_rate": "0.05"},
            {"model": "anthropic--claude-4-sonnet", "input_rate": "1", "output_rate": "4"},
        ],
    }


async def _seed_usage(*models: str) -> None:
    async with SessionLocal() as session:
        for n, model in enumerate(models):
            session.add(ModelUsage(source="job", ref_id=f"run-{n}", model=model,
                                   input_tokens=10, output_tokens=5, requests=1))
        await session.commit()


def test_every_route_needs_the_admin_scope():
    routes = [r for r in cost_routes.router.routes if r.path.endswith("/model-prices")]
    assert {m for r in routes for m in r.methods} == {"GET", "PUT"}
    for route in routes:
        assert require_admin in [d.dependency for d in route.dependencies]


async def test_the_admin_router_serves_the_price_routes():
    from agents.admin import router as admin_router

    app = FastAPI()
    app.include_router(admin_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        assert (await http.get(URL)).json() == EMPTY


async def test_an_empty_table(client):
    response = await client.get(URL)
    assert response.status_code == 200
    assert response.json() == EMPTY


async def test_unpriced_models_are_the_ledger_models_without_a_rate(client):
    await _seed_usage("zeta-model", "gpt-4o", "alpha-model", "zeta-model")
    assert (await client.get(URL)).json()["unpriced_models"] == [
        "alpha-model", "gpt-4o", "zeta-model"]
    saved = (await client.put(URL, json=body())).json()
    assert saved["unpriced_models"] == ["alpha-model", "zeta-model"]


async def test_a_first_save_stores_and_answers_the_stamp(client, caplog):
    with caplog.at_level(logging.INFO, logger="agents.cost_routes"):
        response = await client.put(URL, json=body())
    assert response.status_code == 200
    answer = response.json()
    assert answer["capacity_unit_price"] == "7.5" and answer["currency"] == "EUR"
    assert answer["prices"] == [
        {"model": "anthropic--claude-4-sonnet", "input_rate": "1", "output_rate": "4",
         "cache_read_rate": None},
        {"model": "gpt-4o", "input_rate": "0.5", "output_rate": "2", "cache_read_rate": "0.05"},
    ]
    assert answer["updated_at"] is not None
    assert (await client.get(URL)).json() == answer
    async with SessionLocal() as session:
        rows = {r.model: r for r in (await session.execute(select(ModelPrice))).scalars()}
        orch = await session.get(OrchestratorConfig, 1)
    assert rows["gpt-4o"].input_rate == 500_000_000
    assert rows["gpt-4o"].cache_read_rate == 50_000_000
    assert rows["anthropic--claude-4-sonnet"].cache_read_rate is None
    assert orch.capacity_unit_price == 7_500_000_000
    lines = [r.getMessage() for r in caplog.records if r.name == "agents.cost_routes"]
    assert lines == ["model prices saved by admin@example.com: 2 models"]
    assert "0.5" not in caplog.text and "7.5" not in caplog.text


async def test_a_stale_stamp_is_409_and_changes_nothing(client):
    first = (await client.put(URL, json=body())).json()
    second = await client.put(URL, json=body(first["updated_at"], cu="8"))
    assert second.status_code == 200 and second.json()["updated_at"] != first["updated_at"]
    stale = await client.put(URL, json=body(first["updated_at"], cu="9", prices=[]))
    assert stale.status_code == 409
    assert stale.json() == {"detail": "The prices were changed meanwhile: reload them"}
    assert (await client.get(URL)).json() == second.json()


async def test_null_on_a_saved_table_is_stale(client):
    await client.put(URL, json=body())
    response = await client.put(URL, json=body(None, prices=[]))
    assert response.status_code == 409


async def test_a_stamp_on_a_never_saved_table_is_stale(client):
    response = await client.put(URL, json=body("2026-11-01T10:00:00+00:00"))
    assert response.status_code == 409
    assert (await client.get(URL)).json() == EMPTY


async def test_the_stamp_is_compared_as_an_instant(client):
    first = (await client.put(URL, json=body())).json()
    shifted = first["updated_at"].replace("+00:00", "Z")
    assert (await client.put(URL, json=body(shifted, cu="8"))).status_code == 200


async def test_only_the_cu_price_moves_the_stamp(client):
    first = (await client.put(URL, json=body())).json()
    second = (await client.put(URL, json=body(first["updated_at"], cu=None))).json()
    assert second["capacity_unit_price"] is None
    assert second["updated_at"] != first["updated_at"]


async def test_an_empty_list_clears_every_rate(client):
    first = (await client.put(URL, json=body())).json()
    cleared = (await client.put(URL, json=body(first["updated_at"], prices=[]))).json()
    assert cleared["prices"] == []


SECRETISH = "VALUE-7f3a"


@pytest.mark.parametrize("payload, detail", [
    ({"capacity_unit_price": None, "prices": []},
     "expected_updated_at: required (null for a table never saved)"),
    ({"expected_updated_at": None, "prices": []},
     "capacity_unit_price: required (null for no price)"),
    ({"expected_updated_at": None, "capacity_unit_price": None},
     "prices: required"),
    ({"expected_updated_at": SECRETISH, "capacity_unit_price": None, "prices": []},
     "expected_updated_at: a timestamp as the API answers it, or null"),
    ({"expected_updated_at": 5, "capacity_unit_price": None, "prices": []},
     "expected_updated_at: a timestamp as the API answers it, or null"),
    ({"expected_updated_at": None, "capacity_unit_price": SECRETISH, "prices": []},
     "capacity_unit_price: a number from 0 to 1000000 with at most 9 decimals"),
    ({"expected_updated_at": None, "capacity_unit_price": None, "prices": SECRETISH},
     "prices: a list of at most 200 rows"),
    ({"expected_updated_at": None, "capacity_unit_price": None,
      "prices": [{"model": f"m{n}", "input_rate": 1, "output_rate": 1} for n in range(201)]},
     "prices: a list of at most 200 rows"),
    ({"expected_updated_at": None, "capacity_unit_price": None, "prices": [SECRETISH]},
     "prices[0]: an object"),
    ({"expected_updated_at": None, "capacity_unit_price": None,
      "prices": [{"model": SECRETISH, "input_rate": 1, "output_rate": 1},
                 {"model": SECRETISH, "input_rate": 1, "output_rate": 1}]},
     "prices[1].model: a model of an earlier row"),
    ({"expected_updated_at": None, "capacity_unit_price": None,
      "prices": [{"model": "", "input_rate": 1, "output_rate": 1}]},
     "prices[0].model: 1 to 128 characters without control characters"),
    ({"expected_updated_at": None, "capacity_unit_price": None,
      "prices": [{"model": SECRETISH + "x" * 129, "input_rate": 1, "output_rate": 1}]},
     "prices[0].model: 1 to 128 characters without control characters"),
    ({"expected_updated_at": None, "capacity_unit_price": None,
      "prices": [{"model": SECRETISH + "\n", "input_rate": 1, "output_rate": 1}]},
     "prices[0].model: 1 to 128 characters without control characters"),
    ({"expected_updated_at": None, "capacity_unit_price": None,
      "prices": [{"model": 5, "input_rate": 1, "output_rate": 1}]},
     "prices[0].model: 1 to 128 characters without control characters"),
    ({"expected_updated_at": None, "capacity_unit_price": None,
      "prices": [{"model": "m", "input_rate": SECRETISH, "output_rate": 1}]},
     "prices[0].input_rate: a number from 0 to 1000000 with at most 9 decimals"),
    ({"expected_updated_at": None, "capacity_unit_price": None,
      "prices": [{"model": "m", "input_rate": 1}]},
     "prices[0].output_rate: required"),
    ({"expected_updated_at": None, "capacity_unit_price": None,
      "prices": [{"model": "m", "input_rate": 1, "output_rate": 1, "cache_read_rate": True}]},
     "prices[0].cache_read_rate: a number from 0 to 1000000 with at most 9 decimals"),
    ({"expected_updated_at": None, "capacity_unit_price": None,
      "prices": [{"model": "m", "input_rate": 1, "output_rate": 1, SECRETISH: 1}]},
     "prices[0]: unknown key"),
    ({"expected_updated_at": None, "capacity_unit_price": None, "prices": [], SECRETISH: 1},
     "body: unknown key"),
    ([SECRETISH], "body: a JSON object"),
])
async def test_a_refusal_names_the_field_never_the_value(client, payload, detail):
    response = await client.put(URL, json=payload)
    assert response.status_code == 422
    assert response.json() == {"detail": detail}
    assert SECRETISH not in response.text
    assert (await client.get(URL)).json() == EMPTY


async def test_a_body_that_is_not_json_is_refused(client):
    response = await client.put(URL, content=b"{" + SECRETISH.encode(),
                                headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert response.json() == {"detail": "body: a JSON object"}


async def test_a_body_over_64_kb_is_413(client):
    big = json.dumps(body()) + " " * 65_536
    response = await client.put(URL, content=big.encode(),
                                headers={"content-type": "application/json"})
    assert response.status_code == 413
    assert response.json() == {"detail": "The body is too large"}
    assert (await client.get(URL)).json() == EMPTY


async def test_a_missing_settings_row_is_503(client):
    async with SessionLocal() as session:
        await session.execute(delete(OrchestratorConfig))
        await session.commit()
    try:
        response = await client.put(URL, json=body())
        assert response.status_code == 503
        assert response.json() == {"detail": "The settings row is missing: restart the app"}
    finally:
        await init_db()
