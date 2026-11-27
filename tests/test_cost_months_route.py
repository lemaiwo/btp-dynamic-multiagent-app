"""``GET /admin/api/cost/months`` (``agents/cost_routes.py`` over
``agents/cost_months.py``): per month the actual AI Core cost from the usage
service (``agents/btp_usage.py``, faked here) next to the estimate from the
model usage ledger and the admin's prices."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event, update

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from agents import btp_usage, cost_months, cost_routes  # noqa: E402
from agents.auth import current_principal, require_admin  # noqa: E402
from agents.db import (  # noqa: E402
    ModelPrice,
    ModelUsage,
    OrchestratorConfig,
    SessionLocal,
    engine,
    init_db,
)

URL = "/admin/api/cost/months"
NOW = datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc)
SCALE = 10**9


@pytest.fixture(autouse=True)
async def clean(monkeypatch):
    await init_db()
    async with SessionLocal() as session:
        await session.execute(delete(ModelPrice))
        await session.execute(delete(ModelUsage))
        await session.execute(update(OrchestratorConfig).values(
            capacity_unit_price=None, prices_updated_at=None))
        await session.commit()
    monkeypatch.setattr(cost_months, "_utcnow", lambda: NOW)
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


def usage_service(monkeypatch, state="ok", months=None):
    calls = []

    async def fake(first, last):
        calls.append((first, last))
        return btp_usage.UsageAnswer(state, months or {})

    monkeypatch.setattr(cost_routes.btp_usage, "monthly_cost", fake)
    return calls


async def prices(cu="2", rates=None):
    """CU price ``cu`` euros; rates in CU per 1,000 tokens (input, output, cache)."""
    rates = rates if rates is not None else {"gpt-4o": (1, 2, None)}
    async with SessionLocal() as session:
        for model, (i, o, c) in rates.items():
            session.add(ModelPrice(model=model, input_rate=i * SCALE, output_rate=o * SCALE,
                                   cache_read_rate=None if c is None else c * SCALE))
        await session.execute(update(OrchestratorConfig).values(
            capacity_unit_price=None if cu is None else int(cu) * SCALE))
        await session.commit()


async def ledger(source, model, at, tokens_in=1000, tokens_out=1000, cached=0, requests=1,
                 ref="r1"):
    async with SessionLocal() as session:
        session.add(ModelUsage(source=source, ref_id=ref, model=model, input_tokens=tokens_in,
                               output_tokens=tokens_out, cache_read_tokens=cached,
                               requests=requests, created_at=at))
        await session.commit()


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_the_route_needs_the_admin_scope():
    route = next(r for r in cost_routes.router.routes if r.path == "/api/cost/months")
    assert require_admin in [d.dependency for d in route.dependencies]


@pytest.mark.parametrize("months", ["0", "13", "-1", "x"])
async def test_months_must_be_1_to_12(client, monkeypatch, months):
    usage_service(monkeypatch)
    response = await client.get(URL, params={"months": months})
    assert response.status_code == 422


async def test_the_months_newest_first_with_actual_and_estimate(client, monkeypatch):
    calls = usage_service(monkeypatch, months={
        "2026-11": {"amount": 12.0, "currency": "EUR", "capacity_units": 8.0,
                    "estimated": True, "metrics": [{"name": "capacity_units",
                                                    "usage": 8.0, "cost": 12.0}]},
        "2026-10": {"amount": 7.5, "currency": "EUR", "capacity_units": 5.0,
                    "estimated": False, "metrics": []},
    })
    await prices()
    # 1,000 in + 1,000 out of gpt-4o = 1 + 2 = 3 CU = 6 EUR per row.
    await ledger("job", "gpt-4o", utc(2026, 11, 3, 12))
    await ledger("chat", "gpt-4o", utc(2026, 11, 1, 0, 0, 1), ref="c1")
    await ledger("workflow_step", "gpt-4o", utc(2026, 10, 31, 23, 59, 59), ref="w1")
    await ledger("job", "gpt-4o", utc(2026, 8, 15), ref="old")   # outside 3 months
    response = await client.get(URL, params={"months": 3})
    assert response.status_code == 200
    data = response.json()
    assert calls == [(datetime(2026, 9, 1).date(), datetime(2026, 11, 1).date())]
    assert data["usage_service"] == "ok" and data["currency"] == "EUR"
    assert [m["month"] for m in data["months"]] == ["2026-11", "2026-10", "2026-09"]
    nov, oct_, sep = data["months"]
    assert nov["actual"] == {"amount": 12.0, "currency": "EUR", "capacity_units": 8.0,
                             "estimated": True}
    assert nov["actual_status"] == "ok"
    assert nov["estimated"] == {"amount": 12.0, "currency": "EUR", "complete": True}
    assert nov["by_source"] == {"job": 6.0, "workflow_step": None, "chat": 6.0}
    assert nov["by_model"] == [{"model": "gpt-4o", "input_tokens": 2000,
                                "output_tokens": 2000, "cache_read_tokens": 0,
                                "requests": 2, "amount": 12.0}]
    assert oct_["actual"]["amount"] == 7.5 and oct_["actual"]["estimated"] is False
    assert oct_["by_source"] == {"job": None, "workflow_step": 6.0, "chat": None}
    assert sep["actual"] is None and sep["actual_status"] == "no_data"
    assert sep["estimated"] is None and sep["by_model"] == []
    assert sep["by_source"] == {"job": None, "workflow_step": None, "chat": None}


async def test_the_default_is_six_months_with_the_current_one(client, monkeypatch):
    usage_service(monkeypatch)
    data = (await client.get(URL)).json()
    assert [m["month"] for m in data["months"]] == [
        "2026-11", "2026-10", "2026-09", "2026-08", "2026-07", "2026-06"]


async def test_a_year_boundary(client, monkeypatch):
    monkeypatch.setattr(cost_months, "_utcnow", lambda: utc(2027, 1, 2))
    usage_service(monkeypatch)
    data = (await client.get(URL, params={"months": 3})).json()
    assert [m["month"] for m in data["months"]] == ["2027-01", "2026-12", "2026-11"]


@pytest.mark.parametrize("state", ["off", "no_subaccount", "usage_unavailable",
                                   "unauthorized", "destination_error"])
async def test_without_the_usage_service_the_estimates_stay(client, monkeypatch, state):
    usage_service(monkeypatch, state=state)
    await prices()
    await ledger("job", "gpt-4o", utc(2026, 11, 3))
    data = (await client.get(URL, params={"months": 2})).json()
    assert data["usage_service"] == state
    for month in data["months"]:
        assert month["actual"] is None and month["actual_status"] == state
    assert data["months"][0]["estimated"] == {"amount": 6.0, "currency": "EUR",
                                              "complete": True}


async def test_a_month_of_two_currencies_alone_is_unreadable(client, monkeypatch):
    async def fake(first, last):
        return btp_usage.UsageAnswer("ok", {"2026-10": {
            "amount": 7.5, "currency": "EUR", "capacity_units": 5.0,
            "estimated": False, "metrics": []}}, frozenset({"2026-11"}))

    monkeypatch.setattr(cost_routes.btp_usage, "monthly_cost", fake)
    data = (await client.get(URL, params={"months": 3})).json()
    assert data["usage_service"] == "ok"
    nov, oct_, sep = data["months"]
    assert nov["actual"] is None and nov["actual_status"] == "unreadable"
    assert oct_["actual"]["amount"] == 7.5 and oct_["actual_status"] == "ok"
    assert sep["actual"] is None and sep["actual_status"] == "no_data"


async def test_a_model_without_a_rate(client, monkeypatch):
    usage_service(monkeypatch, state="off")
    await prices()
    await ledger("job", "gpt-4o", utc(2026, 11, 3))
    await ledger("job", "claude-x", utc(2026, 11, 4), ref="r2")
    data = (await client.get(URL, params={"months": 1})).json()
    [nov] = data["months"]
    assert nov["estimated"] == {"amount": 6.0, "currency": "EUR", "complete": False}
    by_model = {m["model"]: m["amount"] for m in nov["by_model"]}
    assert by_model == {"claude-x": None, "gpt-4o": 6.0}
    assert [m["model"] for m in nov["by_model"]] == ["claude-x", "gpt-4o"]


async def test_no_cu_price_means_no_estimate(client, monkeypatch):
    usage_service(monkeypatch, state="off")
    await prices(cu=None)
    await ledger("job", "gpt-4o", utc(2026, 11, 3))
    [nov] = (await client.get(URL, params={"months": 1})).json()["months"]
    assert nov["estimated"] is None and nov["by_source"]["job"] is None
    assert nov["by_model"][0]["amount"] is None and nov["by_model"][0]["requests"] == 1


async def test_at_most_one_ledger_statement_per_month(client, monkeypatch):
    usage_service(monkeypatch, state="off")
    await prices()
    for day in range(1, 6):
        await ledger("job", "gpt-4o", utc(2026, 11, day), ref=f"r{day}")
    statements = []

    def record(conn, cursor, statement, params, context, executemany):
        if "model_usage" in statement:
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        response = await client.get(URL, params={"months": 12})
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert response.status_code == 200
    assert 1 <= len(statements) <= 12
    for statement in statements:
        assert "strftime" not in statement.lower() and "extract" not in statement.lower()
