"""What only Postgres can show about the OData catalogue and its audit log.

SKIPPED unless ``TEST_POSTGRES_URL`` names a throw-away database; how to run
them, and what the URL must never point at, is in ``tests/pg.py``. Each test
works in a schema of its own on an engine of its own (never the suite's
SQLite engine) and leaves nothing behind.

The SQLite suites prove the order of the calls and the compiled statements
(``tests/test_odata_stale_write.py``, ``tests/test_odata_admin_api.py``);
these prove that the database then really does what those rely on.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()

from agents.db import (  # noqa: E402
    AgentConfig,
    ODataAuditLog,
    ODataService,
    create_odata_service,
    existing_odata_service_names,
    get_odata_service,
    validate_odata_service,
)
from agents.odata import admin_routes  # noqa: E402
from agents.odata.audit import StoredWriteRecorder  # noqa: E402
from agents.odata.tools import WriteAudit  # noqa: E402
from tests import pg  # noqa: E402
from tests.odata_helpers import service_payload  # noqa: E402

pytestmark = pytest.mark.skipif(pg.postgres_url() is None, reason=pg.SKIP_REASON)

BASE = "/admin/api/odata/services"
ONE = f"{BASE}/stock-levels"
FIELD = "expected_updated_at"
STALE = "Service 'stock-levels' was changed since it was loaded; reload it and save again"
# How long a statement that must WAIT for a lock is watched not finishing.
WAITS = 1.0


def svc(**patch: Any) -> dict[str, Any]:
    return service_payload(name="stock-levels", **patch)


def instant(text: str) -> datetime:
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


@pytest.fixture
async def sessions(monkeypatch):
    """A session maker on this test's own schema; the catalogue routes use it."""
    async with pg.private_sessions() as maker:
        monkeypatch.setattr(admin_routes, "SessionLocal", maker)
        yield maker


@pytest.fixture
async def client(sessions):
    """The catalogue router on a bare app: no lifespan, no other route, so
    nothing here reaches the process-wide engine. Without an XSUAA binding
    the admin check passes."""
    app = FastAPI()
    app.include_router(admin_routes.router, prefix="/admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def created(client):
    r = await client.post(BASE, json=svc())
    assert r.status_code == 201, r.text
    return r.json()


# --- the update route's stale-write check ----------------------------------------


async def test_two_concurrent_saves_from_the_same_loaded_state_one_wins(client, created):
    """Both read the row ``FOR UPDATE``: the second waits for the first's
    commit, is handed the committed row and answers 409."""
    for round_ in range(5):
        stamp = (await client.get(ONE)).json()["updated_at"]
        answers = await asyncio.gather(
            client.put(ONE, json={**svc(title=f"A{round_}"), FIELD: stamp}),
            client.put(ONE, json={**svc(title=f"B{round_}", user_context=True), FIELD: stamp}),
        )
        assert sorted(a.status_code for a in answers) == [200, 409], [a.text for a in answers]
        loser = next(a for a in answers if a.status_code == 409)
        assert loser.json() == {"detail": STALE}
        winner = next(a for a in answers if a.status_code == 200).json()
        stored = (await client.get(ONE)).json()
        assert stored == winner


async def test_the_stamp_a_client_got_back_is_never_a_false_409(client, created, sessions):
    """GET -> PUT -> PUT with the answered stamps; ``timestamptz`` keeps the
    microsecond, and the text the API emits parses back to the stored value."""
    loaded = (await client.get(ONE)).json()
    assert loaded["updated_at"] == created["updated_at"]
    first = await client.put(ONE, json={**svc(title="One"), FIELD: loaded["updated_at"]})
    assert first.status_code == 200, first.text
    second = await client.put(ONE, json={**svc(title="Two"), FIELD: first.json()["updated_at"]})
    assert second.status_code == 200, second.text
    stamps = [instant(x["updated_at"]) for x in (loaded, first.json(), second.json())]
    assert stamps == sorted(set(stamps)) and len(stamps) == 3

    answered = second.json()["updated_at"]
    assert (await client.get(ONE)).json()["updated_at"] == answered
    assert [s["updated_at"] for s in (await client.get(BASE)).json()] == [answered]
    async with sessions() as s:
        row = await get_odata_service(s, "stock-levels")
        assert row.updated_at.tzinfo is not None
        assert row.updated_at == instant(answered)  # to the microsecond
    # The first answer's stamp is a stale one now.
    third = await client.put(ONE, json={**svc(title="Three"), FIELD: first.json()["updated_at"]})
    assert third.status_code == 409 and third.json() == {"detail": STALE}


async def test_a_stamp_survives_the_database_with_its_microsecond(client, created, monkeypatch):
    from agents import db as db_module

    odd = datetime.now(timezone.utc).replace(microsecond=123457) + timedelta(seconds=5)
    monkeypatch.setattr(db_module, "_odata_now", lambda: odd)
    r = await client.put(ONE, json={**svc(title="T"), FIELD: created["updated_at"]})
    assert r.status_code == 200, r.text
    assert instant(r.json()["updated_at"]) == odd
    assert instant((await client.get(ONE)).json()["updated_at"]) == odd


# --- delete against an agent save ---------------------------------------------------


async def test_delete_waits_for_an_agent_save_and_then_finds_the_service_in_use(
    client, created, sessions
):
    """The save holds ``FOR SHARE`` on the service it attaches; the delete's
    ``FOR UPDATE`` waits for its commit and its referrer check, a new
    statement under READ COMMITTED, then sees the agent."""
    delete_call: asyncio.Task[Any] | None = None
    try:
        async with sessions() as save:
            found = await existing_odata_service_names(save, ["stock-levels"], lock=True)
            assert found == {"stock-levels"}
            save.add(
                AgentConfig(
                    name="buyer",
                    description="d",
                    instructions="i",
                    mcp_url="builtin:odata",
                    auth_mode="destination",
                    oauth_json=json.dumps({"services": ["stock-levels"]}),
                    enabled=1,
                )
            )
            await save.flush()

            delete_call = asyncio.create_task(client.delete(ONE))
            done, _ = await asyncio.wait({delete_call}, timeout=WAITS)
            assert not done, "the delete did not wait for the agent save"
            await save.commit()

        answer = await asyncio.wait_for(delete_call, timeout=30)
        assert answer.status_code == 409
        assert answer.json()["detail"] == "Service 'stock-levels' is used by agent(s) 'buyer'"
        assert (await client.get(ONE)).status_code == 200
    finally:
        if delete_call is not None and not delete_call.done():
            delete_call.cancel()
            await asyncio.gather(delete_call, return_exceptions=True)


# --- create inside a caller's transaction ---------------------------------------------


async def test_a_service_created_without_commit_goes_with_the_rollback(sessions):
    async with sessions() as s:
        await create_odata_service(s, validate_odata_service(svc()), commit=False)
        assert await get_odata_service(s, "stock-levels") is not None
        await s.rollback()
    async with sessions() as s:
        assert await get_odata_service(s, "stock-levels") is None
        assert (await s.execute(select(func.count()).select_from(ODataService))).scalar_one() == 0


# --- the audit table ------------------------------------------------------------------


def record(**patch: Any) -> WriteAudit:
    base: dict[str, Any] = {
        "call_id": "c" * 32,
        "agent": "buyer",
        "run_id": "run-1",
        "service": "stock-levels",
        "target": "A_Stock",
        "operation": "update",
        "key": {"Material": "M-1"},
        "fields": ("Quantity",),
        "outcome": "intent",
        "phase": None,
        "status": None,
        "sent_as": "user@example.com",
        "run_principal": "user@example.com",
        "token_digest": "d" * 64,
    }
    base.update(patch)
    return WriteAudit(**base)


async def _audit_rows(sessions) -> list[ODataAuditLog]:
    async with sessions() as s:
        return list((await s.execute(select(ODataAuditLog).order_by(ODataAuditLog.id))).scalars())


async def test_a_call_id_is_stored_once(sessions):
    recorder = StoredWriteRecorder(session_factory=sessions)
    await recorder.intent(record())
    with pytest.raises(IntegrityError):
        await recorder.intent(record(agent="another"))
    rows = await _audit_rows(sessions)
    assert [(r.call_id, r.agent, r.outcome) for r in rows] == [("c" * 32, "buyer", "intent")]


async def test_a_row_is_finalised_once_and_a_second_result_changes_nothing(sessions):
    recorder = StoredWriteRecorder(session_factory=sessions)
    token = await recorder.intent(record())
    await recorder.result(token, record(outcome="ok", phase="write", status=200))
    (first,) = await _audit_rows(sessions)
    assert (first.outcome, first.phase, first.http_status) == ("ok", "write", 200)

    # The conditional UPDATE (`outcome = 'intent'`) matches no row now.
    await recorder.result(token, record(outcome="sap_error", phase="write", status=500))
    (second,) = await _audit_rows(sessions)
    assert (second.outcome, second.phase, second.http_status) == ("ok", "write", 200)
    assert second.finished_at == first.finished_at
    # Nor does closing it as "never sent".
    assert await recorder.abandon(record()) is False
    (third,) = await _audit_rows(sessions)
    assert (third.outcome, third.phase) == ("ok", "write")


async def test_audit_timestamps_round_trip_as_utc_instants(sessions):
    before = datetime.now(timezone.utc)
    recorder = StoredWriteRecorder(session_factory=sessions)
    token = await recorder.intent(record())
    (open_row,) = await _audit_rows(sessions)
    assert open_row.finished_at is None
    await recorder.result(token, record(outcome="ok", phase="write", status=204))
    after = datetime.now(timezone.utc)

    (row,) = await _audit_rows(sessions)
    assert row.created_at.tzinfo is not None and row.finished_at.tzinfo is not None
    assert before <= row.created_at <= row.finished_at <= after
    served = row.to_dict()
    assert instant(served["created_at"]) == row.created_at
    assert instant(served["finished_at"]) == row.finished_at
