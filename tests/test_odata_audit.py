"""The stored audit of ``builtin:odata`` writes (Task W5).

The real toolset on a mock SAP (``httpx.MockTransport`` behind the real
``destination_http_client``), recording through the real
``StoredWriteRecorder`` into a real SQLite file of this suite's own. The SAP
double looks into the table when a request arrives, which is how "the intent
row exists before anything is sent" is asserted.

Also: retention (parsing, floor, ``0``, the purge and where it runs), the
admin read route, the registry wiring and the draining of result tasks.

No network.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import jwt as pyjwt
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text

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

import app as app_module  # noqa: E402
from agents import auth  # noqa: E402
from agents import registry as registry_module  # noqa: E402
from agents.auth import current_claims, current_jwt, current_principal  # noqa: E402
from agents.builtins import build_builtin_toolset  # noqa: E402
from agents.db import (  # noqa: E402
    ODATA_AUDIT_MAX_RETENTION_DAYS,
    ODataAuditLog,
    SessionLocal,
    init_db,
    purge_odata_audit,
)
from agents.odata import audit as audit_module  # noqa: E402
from agents.odata import tools as tools_module  # noqa: E402
from agents.odata.audit import StoredWriteRecorder, stored_recorder  # noqa: E402
from agents.odata.models import validate_odata_service  # noqa: E402
from agents.odata.tools import WriteAudit, odata_toolset  # noqa: E402
from tests.odata_helpers import FakeResolver, v2_error  # noqa: E402

ALICE = "alice@example.com"
ITEM = "A_PurchaseRequisitionItem"
SERVICE_PATH = "/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV"
KEY = {"PurchaseRequisition": "10000001", "PurchaseRequisitionItem": "00010"}
COOKIE_VALUE = "COOKIE-VALUE-9f3"
CSRF_VALUE = "CSRF-VALUE-71c"
RAW_ETAG = "W/\"datetimeoffset'2026-01-01T00%3A00%3A00Z'\""
BODY_VALUE = "SECRET-QTY-77"
CTX = SimpleNamespace(run_id="run-1")
UPDATE = {"operation": "update", "key": KEY, "body": {"RequestedQuantity": BODY_VALUE}}
AUDIT_URL = "/admin/api/odata/audit"


def _catalogue() -> dict[str, dict]:
    fields = [
        {"name": "PurchaseRequisition", "selectable": True, "filterable": True, "writable": True},
        {"name": "PurchaseRequisitionItem", "selectable": True, "writable": True},
        {"name": "RequestedQuantity", "selectable": True, "writable": True},
        {"name": "Plant", "selectable": True, "writable": True},
    ]
    definition = {
        "entity_sets": [
            {
                "name": ITEM,
                "keys": [{"name": "PurchaseRequisition"}, {"name": "PurchaseRequisitionItem"}],
                "operations": ["list", "get", "create", "update", "delete"],
                "fields": fields,
            }
        ]
    }
    out = {}
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
    return out


async def all_rows() -> list[ODataAuditLog]:
    async with SessionLocal() as s:
        return list((await s.execute(select(ODataAuditLog).order_by(ODataAuditLog.id))).scalars())


async def raw_rows() -> str:
    """Every column of every row, as the database holds it."""
    async with SessionLocal() as s:
        rows = (await s.execute(text("SELECT * FROM odata_audit_log"))).all()
    return json.dumps([list(map(str, row)) for row in rows])


class Sap:
    """SAP with one CSRF session; notes what the audit table held per request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.rows_seen: list[list[tuple[str, str, str | None]]] = []
        self.write_answer: Any = None

    @property
    def writes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method != "GET"]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.rows_seen.append(
            [(r.operation, r.outcome, r.finished_at) for r in await all_rows()]
        )
        if request.headers.get("X-CSRF-Token") == "Fetch":
            return httpx.Response(
                200,
                headers=[
                    ("X-CSRF-Token", CSRF_VALUE),
                    ("Set-Cookie", f"SAP_SESSIONID_XXX_100={COOKIE_VALUE}; path=/; secure"),
                ],
                json={"d": {"EntitySets": [ITEM]}},
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "d": {
                        "__metadata": {"etag": RAW_ETAG},
                        **KEY,
                        "RequestedQuantity": "5",
                        "Plant": "1000",
                    }
                },
            )
        answer = self.write_answer
        if callable(answer):
            answer = answer(request)
            if asyncio.iscoroutine(answer):
                answer = await answer
        if isinstance(answer, httpx.Response):
            return answer
        if request.method == "POST" and "X-HTTP-Method" not in request.headers:
            created = {
                "d": {
                    "__metadata": {"etag": RAW_ETAG},
                    "PurchaseRequisition": "10000009",
                    "PurchaseRequisitionItem": "00010",
                    "Plant": "1000",
                }
            }
            return httpx.Response(201, json=created)
        return httpx.Response(204)


class World:
    def __init__(self, recorder: Any = None, **kw: Any) -> None:
        self.sap = Sap()
        self.resolved: list[str] = []
        world = self

        class Recording(FakeResolver):
            async def resolve(self, *, force=False, user_token=None, principal=None):
                world.resolved.append(self.name)
                return await super().resolve(
                    force=force, user_token=user_token, principal=principal
                )

        self.recorder = recorder if recorder is not None else StoredWriteRecorder()
        self.toolset = odata_toolset(
            kw.pop("oauth", {"services": ["pr", "pr-jobs"], "allow_write": True}),
            auth_mode="destination",
            services=_catalogue(),
            agent_name="pr-release-job",
            transport=httpx.MockTransport(self.sap.handler),
            resolver_factory=lambda name: Recording(name=name),
            recorder=self.recorder,
            **kw,
        )

    async def run(self, **args: Any) -> dict:
        call = {"service": "pr", "target": ITEM, **args}
        return await self.toolset.tools["execute_operation"].function(CTX, **call)


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


def _record(**patch: Any) -> WriteAudit:
    base: dict[str, Any] = {
        "call_id": "c" * 32,
        "agent": "buyer",
        "run_id": "run-1",
        "service": "pr",
        "target": ITEM,
        "operation": "update",
        "key": dict(KEY),
        "fields": ("RequestedQuantity",),
        "outcome": "intent",
        "phase": "token",
        "status": None,
        "sent_as": ALICE,
        "run_principal": ALICE,
        "token_digest": "d" * 64,
    }
    base.update(patch)
    return WriteAudit(**base)


def _audit_lines(caplog: Any, level: int | None = None) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "agents.odata.audit" and (level is None or r.levelno == level)
    ]


# -- intent, then result ---------------------------------------------------------


async def test_an_update_writes_intent_before_the_request_and_the_result_after(alice):
    w = World()
    started = datetime.now(timezone.utc) - timedelta(seconds=5)
    assert await w.run(**UPDATE) == {"ok": True, "status": 204}
    # What SAP saw in the table when each request arrived: the row was there,
    # open, before the CSRF fetch and before the write itself.
    assert [r.headers.get("X-CSRF-Token") == "Fetch" for r in w.sap.requests] == [True, False]
    assert w.sap.rows_seen == [[("update", "intent", None)], [("update", "intent", None)]]
    (row,) = await all_rows()
    assert (row.agent, row.run_id, row.service, row.target, row.operation) == (
        "pr-release-job", "run-1", "pr", ITEM, "update",
    )
    assert (row.outcome, row.phase, row.http_status) == ("ok", "write", 204)
    assert json.loads(row.key_json) == KEY and row.created_key_json is None
    assert json.loads(row.body_fields_json) == ["RequestedQuantity"]
    assert len(row.call_id) == 32 and row.finished_at is not None
    created = row.created_at.replace(tzinfo=timezone.utc)
    finished = row.finished_at.replace(tzinfo=timezone.utc)
    assert started <= created <= finished <= datetime.now(timezone.utc) + timedelta(seconds=5)


async def test_a_read_and_a_refused_call_write_no_row(alice):
    w = World()
    assert (await w.run(operation="get", key=KEY))["item"]["Plant"] == "1000"
    assert (await w.run(operation="update", key=KEY, body={"Nope": "1"}))["error"]["code"] == (
        "unknown_field"
    )
    closed = World(oauth={"services": ["pr"], "allow_write": False})
    assert (await closed.run(**UPDATE))["error"]["code"] == "write_not_allowed"
    assert await all_rows() == [] and closed.sap.requests == []


async def test_create_and_delete_rows_and_the_created_key(alice):
    w = World()
    out = await w.run(operation="create", body={"Plant": "1000"})
    assert out["item"]["PurchaseRequisition"] == "10000009"
    assert (await w.run(operation="delete", key=KEY))["ok"] is True
    create, remove = await all_rows()
    assert (create.operation, create.outcome, create.http_status, create.key_json) == (
        "create", "ok", 201, None,
    )
    assert json.loads(create.created_key_json) == {
        "PurchaseRequisition": "10000009", "PurchaseRequisitionItem": "00010",
    }
    assert json.loads(create.body_fields_json) == ["Plant"]
    assert (remove.operation, remove.outcome, remove.http_status) == ("delete", "ok", 204)
    assert json.loads(remove.key_json) == KEY and json.loads(remove.body_fields_json) == []
    assert create.call_id != remove.call_id


async def test_a_sap_error_and_a_destination_failure_end_with_their_outcome(alice):
    w = World()
    w.sap.write_answer = v2_error(403, "SY/1", "No authorization")
    assert (await w.run(**UPDATE))["error"]["code"] == "sap_error"

    class Down(FakeResolver):
        async def resolve(self, **_kw):
            from agents.destination import DestinationError

            raise DestinationError("destination service said 503")

    refused_world = odata_toolset(
        {"services": ["pr"], "allow_write": True},
        auth_mode="destination",
        services=_catalogue(),
        agent_name="buyer",
        transport=httpx.MockTransport(w.sap.handler),
        resolver_factory=lambda name: Down(name=name),
        recorder=StoredWriteRecorder(),
    )
    out = await refused_world.tools["execute_operation"].function(
        CTX, service="pr", target=ITEM, **UPDATE
    )
    assert out["error"]["code"] == "destination_error"
    sap_error, refused = await all_rows()
    assert (sap_error.outcome, sap_error.phase, sap_error.http_status) == (
        "sap_error", "write", 403,
    )
    assert (refused.outcome, refused.phase, refused.http_status) == ("refused", "token", None)
    assert sap_error.finished_at is not None and refused.finished_at is not None


async def test_no_body_value_token_cookie_or_etag_is_stored_or_logged(alice, caplog):
    w = World()
    with caplog.at_level(logging.DEBUG, logger="agents"):
        read = await w.run(operation="get", key=KEY)
        out = await w.run(**{**UPDATE, "etag": read["etag"]})
        assert out["ok"] is True
        await w.run(operation="create", body={"Plant": BODY_VALUE})
    # The values did travel: this is not a test of an empty request.
    write = w.sap.writes[0]
    assert BODY_VALUE in write.content.decode() and write.headers["If-Match"] == RAW_ETAG
    assert COOKIE_VALUE in write.headers["Cookie"] and write.headers["X-CSRF-Token"] == CSRF_VALUE
    stored = await raw_rows()
    assert "10000001" in stored  # the key is there, by design
    for secret in (BODY_VALUE, f"jwt-of-{ALICE}", "user-token-of", COOKIE_VALUE, CSRF_VALUE,
                   "datetimeoffset", read["etag"], "Bearer"):
        assert secret not in stored, secret
        assert secret not in caplog.text, secret
    # One log line per stored result, with the fields and without the key values.
    lines = _audit_lines(caplog, logging.INFO)
    assert len(lines) == 2 and "outcome=ok" in lines[0] and "operation=update" in lines[0]
    assert "fields=['RequestedQuantity']" in lines[0] and "10000001" not in caplog.text


def _token(**claims: Any) -> str:
    return pyjwt.encode(claims, "k" * 32, algorithm="HS256")


async def test_sent_as_and_run_principal_are_both_stored_and_can_differ():
    """A job run carries the trigger's token while the principal names the
    run-as user: the row says whose credential SAP saw AND who the run was."""
    w = World()
    claims = {"user_uuid": "uuid-of-alice", "email": ALICE, "jti": "a1", "iat": 1}
    alice_jwt = _token(**claims)
    bound = current_claims.set(claims)
    try:
        with signed_in("job-user", token=alice_jwt):
            assert (await w.run(**UPDATE))["ok"] is True
            assert (await w.run(**{**UPDATE, "service": "pr-jobs"}))["ok"] is True
    finally:
        current_claims.reset(bound)
    # Claims that are not this token's: the digest names the sender, never a guess.
    with signed_in("job-user", token=alice_jwt):
        await w.run(**UPDATE)
    as_user, technical, unnamed = await all_rows()
    digest = hashlib.sha256(alice_jwt.encode()).hexdigest()
    assert (as_user.sent_as, as_user.run_principal, as_user.token_digest) == (
        "uuid-of-alice", "job-user", digest,
    )
    assert (technical.sent_as, technical.run_principal, technical.token_digest) == (
        "technical:S4_ODATA_TECH", "job-user", None,
    )
    assert (unnamed.sent_as, unnamed.run_principal) == (f"token:{digest}", "job-user")
    assert alice_jwt not in await raw_rows()


# -- no intent, no write -----------------------------------------------------------


async def test_an_intent_that_cannot_be_written_means_no_request(alice, caplog):
    def no_database() -> Any:
        raise RuntimeError("connection refused: secret-dsn")

    w = World(recorder=StoredWriteRecorder(session_factory=no_database))
    with caplog.at_level(logging.DEBUG, logger="agents"):
        for call in (UPDATE, {"operation": "create", "body": {"Plant": "1"}},
                     {"operation": "delete", "key": KEY}):
            out = await w.run(**call)
            assert out["error"]["code"] == "audit_unavailable", out
            assert "nothing was changed" in out["error"]["message"]
    assert w.sap.requests == [] and w.resolved == [] and w.toolset.http_clients == []
    assert await all_rows() == []
    assert "secret-dsn" not in caplog.text and "secret-dsn" not in json.dumps(out)
    assert len(_audit_lines(caplog, logging.ERROR)) == 3


async def test_a_database_that_refuses_the_row_means_no_request(alice, monkeypatch):
    """The real session, a statement the database refuses (the call id is
    unique): nothing is sent for the second call."""
    monkeypatch.setattr(tools_module.uuid, "uuid4", lambda: SimpleNamespace(hex="f" * 32))
    w = World()
    assert (await w.run(**UPDATE))["ok"] is True
    out = await w.run(**UPDATE)
    assert out["error"]["code"] == "audit_unavailable"
    assert len(w.sap.writes) == 1 and len(await all_rows()) == 1


async def test_a_slow_intent_means_no_request_even_if_its_row_lands(alice, monkeypatch, caplog):
    monkeypatch.setattr(tools_module, "AUDIT_INTENT_TIMEOUT_SECONDS", 0.05)

    class CommitsThenHangs(StoredWriteRecorder):
        async def intent(self, record: WriteAudit) -> int:
            await super().intent(record)
            await asyncio.Event().wait()
            return 0

    w = World(recorder=CommitsThenHangs())
    with caplog.at_level(logging.DEBUG, logger="agents"):
        out = await w.run(**UPDATE)
    assert out["error"]["code"] == "audit_unavailable" and w.sap.requests == []
    # The row is there and stays an intent; the log line with its call id is
    # what says that it was never acted on.
    (row,) = await all_rows()
    assert (row.outcome, row.finished_at) == ("intent", None)
    (line,) = _audit_lines(caplog, logging.ERROR)
    assert "intent not confirmed" in line and "the write was not sent" in line
    assert f"call_id={row.call_id}" in line


async def test_a_cancelled_intent_leaves_a_row_explained_by_the_log(alice, caplog):
    stored = asyncio.Event()

    class CommitsThenHangs(StoredWriteRecorder):
        async def intent(self, record: WriteAudit) -> int:
            await super().intent(record)
            stored.set()
            await asyncio.Event().wait()
            return 0

    w = World(recorder=CommitsThenHangs())
    with caplog.at_level(logging.DEBUG, logger="agents"):
        task = asyncio.create_task(w.run(**UPDATE))
        await asyncio.wait_for(stored.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert w.sap.requests == []
    (row,) = await all_rows()
    assert row.outcome == "intent"
    (line,) = _audit_lines(caplog, logging.WARNING)
    assert "intent interrupted, the write was not sent" in line
    assert f"call_id={row.call_id}" in line and "10000001" not in caplog.text


# -- the result finalises a row exactly once ------------------------------------------


async def test_the_result_finalises_its_row_exactly_once(caplog):
    recorder = StoredWriteRecorder()
    record = _record()
    row_id = await recorder.intent(record)
    assert isinstance(row_id, int)
    done = _record(outcome="ok", phase="write", status=204)
    await recorder.result(row_id, done)
    (row,) = await all_rows()
    first = (row.outcome, row.phase, row.http_status, row.finished_at)
    assert first[:3] == ("ok", "write", 204) and first[3] is not None
    # A second result for the same row changes nothing, and is logged.
    with caplog.at_level(logging.DEBUG, logger="agents"):
        await recorder.result(
            row_id, _record(outcome="cancelled", phase="token", status=500, created_key=KEY)
        )
    (row,) = await all_rows()
    assert (row.outcome, row.phase, row.http_status, row.finished_at) == first
    assert row.created_key_json is None
    (line,) = _audit_lines(caplog, logging.ERROR)
    assert "result NOT recorded" in line and "outcome=cancelled" in line
    assert f"call_id={record.call_id}" in line and "10000001" not in line


async def test_a_result_touches_only_the_row_of_its_own_call():
    recorder = StoredWriteRecorder()
    mine = await recorder.intent(_record(call_id="a" * 32))
    other = await recorder.intent(_record(call_id="b" * 32))
    # The right id with another call's record: refused, both rows stay open.
    await recorder.result(other, _record(call_id="a" * 32, outcome="ok", phase="write"))
    await recorder.result(mine + 1000, _record(call_id="a" * 32, outcome="ok", phase="write"))
    assert [r.outcome for r in await all_rows()] == ["intent", "intent"]
    for bad_token in (None, "1", True, 1.0):
        with pytest.raises(TypeError):
            await recorder.result(bad_token, _record(outcome="ok", phase="write"))
    for bad in ({"outcome": "intent"}, {"outcome": "done"}, {"outcome": "ok", "phase": "sent"}):
        with pytest.raises(ValueError):
            await recorder.result(mine, _record(call_id="a" * 32, **{"phase": "write", **bad}))
    assert [r.outcome for r in await all_rows()] == ["intent", "intent"]
    await recorder.result(mine, _record(call_id="a" * 32, outcome="unknown", phase="write"))
    assert [r.outcome for r in await all_rows()] == ["unknown", "intent"]


async def test_the_intent_row_is_always_an_intent_whatever_the_record_says():
    recorder = StoredWriteRecorder()
    await recorder.intent(_record(outcome="ok", phase="write", status=204, created_key=KEY))
    (row,) = await all_rows()
    assert (row.outcome, row.phase, row.http_status, row.created_key_json, row.finished_at) == (
        "intent", "token", None, None, None,
    )


async def test_each_step_uses_a_session_of_its_own_and_closes_it():
    opened: list[Any] = []

    def factory() -> Any:
        session = SessionLocal()
        opened.append(session)
        return session

    recorder = StoredWriteRecorder(session_factory=factory)
    row_id = await recorder.intent(_record())
    # Committed: another session sees it at once.
    assert [r.outcome for r in await all_rows()] == ["intent"]
    await recorder.result(row_id, _record(outcome="ok", phase="write", status=204))
    assert len(opened) == 2 and opened[0] is not opened[1]
    assert not any(session.in_transaction() for session in opened)


async def test_values_longer_than_their_column_are_cut_not_refused():
    columns = ODataAuditLog.__table__.c
    recorder = StoredWriteRecorder()
    await recorder.intent(
        _record(sent_as="s" * 400, run_principal="p" * 400, agent="a" * 100, run_id="r" * 100)
    )
    (row,) = await all_rows()
    assert len(row.sent_as) == columns.sent_as.type.length == 255
    assert len(row.run_principal) == columns.run_principal.type.length == 255
    assert len(row.agent) == columns.agent.type.length and len(row.run_id) == 64


def test_the_table_fits_what_a_record_carries_and_is_indexed_for_an_operator():
    columns = ODataAuditLog.__table__.c
    assert set(columns.keys()) == {
        "id", "created_at", "finished_at", "call_id", "agent", "run_id", "sent_as",
        "run_principal", "token_digest", "service", "target", "operation", "key_json",
        "created_key_json", "body_fields_json", "phase", "outcome", "http_status",
    }
    assert columns.call_id.type.length >= 32 and columns.token_digest.type.length >= 64
    assert columns.service.type.length >= 64 and columns.target.type.length >= 128
    assert len("technical:") + 200 <= columns.sent_as.type.length
    assert len("token:") + 64 <= columns.sent_as.type.length
    for value in ("create", "update", "delete"):
        assert len(value) <= columns.operation.type.length
    for value in ("intent", *tools_module.WRITE_OUTCOMES):
        assert len(value) <= columns.outcome.type.length
    for value in ("token", "write"):
        assert len(value) <= columns.phase.type.length
    indexed = {
        tuple(c.name for c in index.columns) for index in ODataAuditLog.__table__.indexes
    }
    assert {("created_at",), ("service", "created_at"), ("sent_as", "created_at"),
            ("outcome", "created_at")} <= indexed
    unique = {
        tuple(c.name for c in constraint.columns)
        for constraint in ODataAuditLog.__table__.constraints
        if type(constraint).__name__ == "UniqueConstraint"
    }
    assert ("call_id",) in unique


# -- a row that never got its result ---------------------------------------------------


async def test_a_process_that_dies_after_the_send_leaves_a_distinguishable_row(alice, caplog):
    """Intent written, write sent, no result (here: the result cannot be
    stored): the row stays open and says so; nothing pretends it was fine."""

    class DiesBeforeTheResult(StoredWriteRecorder):
        async def result(self, token: Any, record: WriteAudit) -> None:
            raise RuntimeError("process is gone")

    w = World(recorder=DiesBeforeTheResult())
    with caplog.at_level(logging.DEBUG, logger="agents"):
        assert (await w.run(**UPDATE))["ok"] is True
        await asyncio.sleep(0)
    assert len(w.sap.writes) == 1
    (row,) = await all_rows()
    assert (row.outcome, row.phase, row.http_status, row.finished_at) == (
        "intent", "token", None, None,
    )
    # Distinguishable from every finished row ...
    assert (await World().run(**UPDATE))["ok"] is True
    open_rows = [r for r in await all_rows() if r.outcome == "intent"]
    assert [r.id for r in open_rows] == [row.id]
    # ... and what was known went to the log with the row's call id.
    (line,) = _audit_lines(caplog, logging.ERROR)
    assert "result NOT recorded" in line and f"call_id={row.call_id}" in line
    assert "outcome=ok" in line and "phase=write" in line
    # The module says how to read such a row.
    doc = " ".join((audit_module.__doc__ or "").split())
    assert "the write may have been sent; the process stopped, or the run was cancelled, " \
           "before the outcome was recorded" in doc
    assert "intent interrupted, the write was not sent" in doc


async def test_a_cancelled_run_still_gets_its_result_row(alice):
    w = World()
    arrived, never = asyncio.Event(), asyncio.Event()

    async def hang(_request: httpx.Request) -> httpx.Response:
        arrived.set()
        await never.wait()
        return httpx.Response(204)

    w.sap.write_answer = hang
    task = asyncio.create_task(w.run(**UPDATE))
    await asyncio.wait_for(arrived.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (row,) = await all_rows()
    assert (row.outcome, row.phase, row.http_status) == ("cancelled", "write", None)
    assert row.finished_at is not None


# -- fail closed, and the wiring ------------------------------------------------------


async def test_without_a_storing_recorder_no_write_is_sent(alice):
    toolset = odata_toolset(
        {"services": ["pr"], "allow_write": True},
        auth_mode="destination",
        services=_catalogue(),
        agent_name="buyer",
        transport=httpx.MockTransport(lambda request: pytest.fail("a request was sent")),
        resolver_factory=lambda name: FakeResolver(name=name),
    )
    out = await toolset.tools["execute_operation"].function(
        CTX, service="pr", target=ITEM, **UPDATE
    )
    assert out["error"]["code"] == "audit_not_configured"
    found = await toolset.tools["search_operations"].function("", detail="full")
    assert {op for m in found["matches"] for op in m["operations"]} == {"list", "get"}
    assert await all_rows() == []


async def test_the_registry_factory_always_passes_the_storing_recorder():
    toolset = build_builtin_toolset(
        "builtin:odata",
        {"services": ["pr"], "allow_write": True},
        "destination",
        context={"odata_services": _catalogue(), "agent_name": "buyer"},
    )
    assert toolset.recorder is stored_recorder()
    assert isinstance(toolset.recorder, StoredWriteRecorder)
    # The recorder, not the toolset, owns the result tasks.
    assert toolset.audit_tasks is stored_recorder().tasks
    found = await toolset.tools["search_operations"].function("", detail="full")
    assert "update" in {op for m in found["matches"] for op in m["operations"]}
    # No other built-in is handed a recorder.
    import inspect

    assert inspect.getsource(build_builtin_toolset).count("recorder=") == 1


def test_the_tool_description_names_the_refusal_codes():
    w = World()
    doc = " ".join((w.toolset.tools["execute_operation"].tool_def.description or "").split())
    for code in ("not_available", "audit_unavailable", "invalid_etag", "audit_not_configured",
                 "write_outcome_unknown"):
        assert f"'{code}'" in doc, code


# -- draining the result tasks ---------------------------------------------------------


class _SlowResult(StoredWriteRecorder):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def result(self, token: Any, record: WriteAudit) -> None:
        await self.release.wait()
        await super().result(token, record)


async def test_a_result_still_being_stored_is_held_by_the_recorder_and_drained(
    alice, monkeypatch, caplog
):
    monkeypatch.setattr(tools_module, "AUDIT_RESULT_TIMEOUT_SECONDS", 0.05)
    recorder = _SlowResult()
    w = World(recorder=recorder)
    assert w.toolset.audit_tasks is recorder.tasks
    assert (await w.run(**UPDATE))["ok"] is True  # answered; the result is still being stored
    (task,) = recorder.tasks
    assert [r.outcome for r in await all_rows()] == ["intent"]
    # A drain waits, gives up after its timeout, cancels nothing, raises nothing.
    with caplog.at_level(logging.DEBUG, logger="agents"):
        assert await recorder.drain(0.05) == 1
    assert not task.done() and "still being stored" in caplog.text
    recorder.release.set()
    assert await recorder.drain(5) == 0
    assert task.done() and not recorder.tasks
    assert [r.outcome for r in await all_rows()] == ["ok"]
    assert await recorder.drain(0) == 0  # nothing pending: returns at once


async def test_a_retired_build_drains_its_recorders_once_and_survives_a_failure(monkeypatch):
    calls: list[str] = []

    class Rec:
        def __init__(self, name: str, fail: bool = False) -> None:
            self.name, self.fail = name, fail

        async def drain(self) -> int:
            calls.append(self.name)
            if self.fail:
                raise RuntimeError("boom")
            return 0

    shared, broken = Rec("shared"), Rec("broken", fail=True)
    servers = [
        SimpleNamespace(recorder=shared),
        SimpleNamespace(recorder=shared),
        SimpleNamespace(recorder=broken),
        SimpleNamespace(),  # an MCP server: no recorder
        SimpleNamespace(recorder=None),
    ]
    build = SimpleNamespace(mcp_clients=servers, in_flight=SimpleNamespace(value=0))
    await registry_module._drain_audit_recorders(build)
    assert calls == ["shared", "broken"]
    # ... and the retire path of the registry calls it for an idle build only.
    seen: list[Any] = []

    async def spy(retired: Any) -> None:
        seen.append(retired)

    monkeypatch.setattr(registry_module, "_drain_audit_recorders", spy)
    busy = SimpleNamespace(mcp_clients=[], in_flight=SimpleNamespace(value=1))
    idle = SimpleNamespace(mcp_clients=[], in_flight=SimpleNamespace(value=0))
    reg = registry_module.Registry()
    reg._retired = [busy, idle]
    import agents.job_runner as job_runner
    import agents.workflow_runner as workflow_runner

    monkeypatch.setattr(job_runner, "_tasks", set())
    monkeypatch.setattr(workflow_runner, "_tasks", set())
    await reg._close_idle_retired()
    assert seen == [idle] and reg._retired == [busy]


# -- retention ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, days, warned",
    [
        (None, 365, False),
        ("", 365, False),
        ("365", 365, False),
        ("30", 30, False),
        ("7", 7, False),
        ("0", 0, False),  # keep forever: said at start-up, not here
        ("6", 7, True),
        ("1", 7, True),
        ("-5", 365, True),  # a mistake must neither switch the purge off nor empty the log
        ("14 days", 365, True),
        ("1e3", 365, True),
        ("36500", 36500, False),
        ("36501", 36500, True),
        ("99999999999999999999", 36500, True),
    ],
)
def test_retention_parsing(monkeypatch, caplog, raw, days, warned):
    if raw is None:
        monkeypatch.delenv("ODATA_AUDIT_RETENTION_DAYS", raising=False)
    else:
        monkeypatch.setenv("ODATA_AUDIT_RETENTION_DAYS", raw)
    with caplog.at_level(logging.WARNING):
        assert audit_module.retention_days() == days
    assert bool(caplog.records) is warned, caplog.text
    if raw and warned:
        assert "ODATA_AUDIT_RETENTION_DAYS" in caplog.text
        if len(raw) > 2:  # the value itself is never repeated
            assert raw not in caplog.text


def test_the_setting_is_read_at_import_and_bounded():
    assert audit_module.ODATA_AUDIT_RETENTION_DAYS == audit_module.retention_days()
    assert app_module.ODATA_AUDIT_RETENTION_DAYS == audit_module.ODATA_AUDIT_RETENTION_DAYS
    assert audit_module.RETENTION_MIN_DAYS == 7
    assert audit_module.RETENTION_MAX_DAYS == ODATA_AUDIT_MAX_RETENTION_DAYS == 36_500


async def _aged_rows() -> None:
    now = datetime.now(timezone.utc)
    recorder = StoredWriteRecorder()
    plan = (("1" * 32, 400, "ok"), ("2" * 32, 400, "intent"),
            ("3" * 32, 20, "ok"), ("4" * 32, 0, "intent"))
    ids = [await recorder.intent(_record(call_id=call_id)) for call_id, _age, _outcome in plan]
    async with SessionLocal() as s:
        for row_id, (_call_id, age, outcome) in zip(ids, plan):
            row = await s.get(ODataAuditLog, row_id)
            row.created_at = now - timedelta(days=age)
            row.outcome = outcome
        await s.commit()


async def test_the_purge_deletes_by_age_only_and_commits_itself():
    await _aged_rows()
    async with SessionLocal() as s:
        for off in (0, -5, True, None, "30"):
            assert await purge_odata_audit(s, off) == 0  # type: ignore[arg-type]
        # A huge value cannot overflow the cutoff: it is the bound, and deletes nothing.
        assert await purge_odata_audit(s, 10**12) == 0
        assert await purge_odata_audit(s, 365) == 2  # the old ones, open intent included
        assert not s.in_transaction()
    assert [r.call_id[0] for r in await all_rows()] == ["3", "4"]
    doc = " ".join((purge_odata_audit.__doc__ or "").split())
    assert "COMMITS the session it is given" in doc


async def test_the_purge_runs_in_the_retention_pass_of_the_app(monkeypatch):
    for name in ("IDE_SESSION_RETENTION_DAYS", "IDE_DIAGNOSE_RETENTION_DAYS",
                 "IDE_AUDIT_RETENTION_DAYS"):
        monkeypatch.setattr(app_module, name, 0)
    await _aged_rows()
    monkeypatch.setattr(app_module, "ODATA_AUDIT_RETENTION_DAYS", 0)
    await app_module._purge_ide_sessions()
    assert len(await all_rows()) == 4  # 0 = keep forever
    monkeypatch.setattr(app_module, "ODATA_AUDIT_RETENTION_DAYS", 30)
    await app_module._purge_ide_sessions()
    assert [r.call_id[0] for r in await all_rows()] == ["3", "4"]

    # Its own session and its own error handling: a failing purge fails nothing.
    async def broken(_session: Any, _days: int) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(app_module, "purge_odata_audit", broken)
    assert await app_module._purge_ide_sessions() == 0


async def test_start_up_warns_when_rows_are_kept_forever_and_shutdown_drains(monkeypatch, caplog):
    async def noop(*_a: Any, **_k: Any) -> None:
        return None

    drained: list[bool] = []

    async def drain() -> int:
        drained.append(True)
        return 2

    monkeypatch.setattr(app_module.ide_runner, "cancel_all", noop)
    monkeypatch.setattr(app_module.registry, "reload", noop)
    monkeypatch.setattr(app_module.dynamic_chat_app, "refresh", lambda: None)
    monkeypatch.setattr(app_module, "seed_from_file_if_empty", noop)
    monkeypatch.setattr(app_module, "ensure_ide_seed", noop)
    monkeypatch.setattr(app_module, "DB_HEARTBEAT_SECONDS", 0)
    monkeypatch.setattr(app_module, "TOKEN_KEEPWARM_SECONDS", 0)
    monkeypatch.setattr(app_module.odata_audit, "drain", drain)
    await _aged_rows()

    def warnings() -> list[str]:
        return [
            r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "ODATA_AUDIT_RETENTION_DAYS" in r.getMessage()
        ]

    monkeypatch.setattr(app_module, "ODATA_AUDIT_RETENTION_DAYS", 0)
    with caplog.at_level(logging.INFO):
        async with app_module.lifespan(app_module.app):
            assert len(await all_rows()) == 4
            assert len(warnings()) == 1 and "never purged" in warnings()[0]
            assert drained == []
        assert drained == [True]
        assert "2 OData audit result(s) not stored at shutdown" in caplog.text
        caplog.clear()
        monkeypatch.setattr(app_module, "ODATA_AUDIT_RETENTION_DAYS", 365)
        async with app_module.lifespan(app_module.app):
            assert [r.call_id[0] for r in await all_rows()] == ["3", "4"]  # purged at start-up
        assert warnings() == []


# -- the admin read route ----------------------------------------------------------------


@pytest.fixture
async def client():
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_rows() -> None:
    now = datetime.now(timezone.utc)
    recorder = StoredWriteRecorder()
    plan = [
        # call, service, sent_as, final outcome, age in hours
        ("1", "pr", ALICE, "ok", 72),
        ("2", "pr", ALICE, "sap_error", 48),
        ("3", "pr-jobs", "technical:S4_ODATA_TECH", "ok", 24),
        ("4", "pr", "bob@example.com", None, 2),
        ("5", "pr", ALICE, "unknown", 1),
    ]
    for call, service, sent_as, outcome, _age in plan:
        record = _record(call_id=call * 32, service=service, sent_as=sent_as)
        row_id = await recorder.intent(record)
        if outcome:
            await recorder.result(
                row_id, _record(call_id=call * 32, outcome=outcome, phase="write", status=204)
            )
    async with SessionLocal() as s:
        for call, *_rest, age in plan:
            row = (
                await s.execute(select(ODataAuditLog).where(ODataAuditLog.call_id == call * 32))
            ).scalar_one()
            row.created_at = now - timedelta(hours=age)
        await s.commit()


def _calls(response: httpx.Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["call_id"][0] for item in response.json()["items"]]


async def test_the_route_lists_newest_first_with_everything_an_audit_needs(client):
    await _seed_rows()
    r = await client.get(AUDIT_URL)
    assert _calls(r) == ["5", "4", "3", "2", "1"]
    body = r.json()
    assert body["limit"] == 100 and body["more"] is False
    assert set(body) == {"items", "limit", "more"}
    newest, open_row = body["items"][0], body["items"][1]
    assert set(newest) == {
        "id", "call_id", "created_at", "finished_at", "agent", "run_id", "service", "target",
        "operation", "key", "created_key", "fields", "phase", "outcome", "status", "sent_as",
        "run_principal", "token_digest",
    }
    assert newest["key"] == KEY and newest["fields"] == ["RequestedQuantity"]
    assert (newest["outcome"], newest["phase"], newest["status"]) == ("unknown", "write", 204)
    assert newest["created_at"].endswith("+00:00") and newest["finished_at"].endswith("+00:00")
    assert (open_row["outcome"], open_row["finished_at"], open_row["status"]) == (
        "intent", None, None,
    )


async def test_the_route_filters_exactly_and_bounds_the_answer(client):
    await _seed_rows()
    assert _calls(await client.get(AUDIT_URL, params={"service": "pr-jobs"})) == ["3"]
    assert _calls(await client.get(AUDIT_URL, params={"sent_as": ALICE})) == ["5", "2", "1"]
    assert _calls(await client.get(AUDIT_URL, params={"sent_as": "alice"})) == []  # no prefix match
    assert _calls(await client.get(AUDIT_URL, params={"outcome": "intent"})) == ["4"]
    assert _calls(await client.get(AUDIT_URL, params={"outcome": "ok", "service": "pr"})) == ["1"]
    since = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    assert _calls(await client.get(AUDIT_URL, params={"since": since})) == ["5", "4", "3"]
    zulu = (datetime.now(timezone.utc) - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _calls(await client.get(AUDIT_URL, params={"since": zulu})) == ["5", "4", "3"]
    # An offset is honoured: the same instant written in another zone.
    plus2 = (datetime.now(timezone(timedelta(hours=2))) - timedelta(hours=30)).isoformat()
    assert _calls(await client.get(AUDIT_URL, params={"since": plus2})) == ["5", "4", "3"]
    r = await client.get(AUDIT_URL, params={"limit": "2"})
    assert _calls(r) == ["5", "4"] and r.json()["more"] is True and r.json()["limit"] == 2
    r = await client.get(AUDIT_URL, params={"limit": "5"})
    assert len(_calls(r)) == 5 and r.json()["more"] is False
    assert _calls(await client.get(AUDIT_URL, params={"limit": "500"})) == ["5", "4", "3", "2", "1"]


@pytest.mark.parametrize(
    "params, names",
    [
        ({"limit": "0"}, "limit"),
        ({"limit": "501"}, "limit"),
        ({"limit": "-1"}, "limit"),
        ({"limit": "ten-SECRET"}, "limit"),
        ({"limit": "99999999999999999999"}, "limit"),
        ({"limit": "٣"}, "limit"),
        ({"service": "Not A Service SECRET"}, "service"),
        ({"service": ""}, "service"),
        ({"outcome": "error-SECRET"}, "outcome"),
        ({"since": "yesterday-SECRET"}, "since"),
        ({"since": "2026-13-45"}, "since"),
        ({"sent_as": ""}, "sent_as"),
        ({"sent_as": "x" * 256}, "sent_as"),
        ({"sent_as": "a\nSECRET"}, "sent_as"),
        ({"principal": "SECRET"}, "query"),
        ({"offset": "5"}, "query"),
    ],
)
async def test_a_refused_parameter_is_named_and_never_echoed(client, params, names):
    r = await client.get(AUDIT_URL, params=params)
    assert r.status_code == 422, (params, r.text)
    assert r.json()["detail"].startswith(names + ":") and "SECRET" not in r.text


async def test_a_parameter_given_twice_is_refused(client):
    r = await client.get(AUDIT_URL + "?service=pr&service=pr-jobs")
    assert r.status_code == 422 and r.json()["detail"].startswith("query:")


async def test_the_audit_cannot_be_written_through_the_api():
    await _seed_rows()
    from agents.odata.admin_routes import router

    # On the router alone: in the app an unmatched method falls through to
    # the chat mount at "/", which is not what this is about.
    bare = FastAPI()
    bare.include_router(router, prefix="/admin")
    async with AsyncClient(transport=ASGITransport(app=bare), base_url="http://test") as c:
        assert (await c.get(AUDIT_URL)).status_code == 200
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            r = await c.request(method, AUDIT_URL)
            assert r.status_code == 405, (method, r.status_code)
    assert len(await all_rows()) == 5
    audit_routes = [route for route in router.routes if "audit" in route.path]
    assert [(route.path, sorted(route.methods)) for route in audit_routes] == [
        ("/api/odata/audit", ["GET"])
    ]
    doc = " ".join((audit_routes[0].endpoint.__doc__ or "").split())
    assert "personal data" in doc and "admins only" in doc


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


async def test_the_route_needs_the_admin_scope(monkeypatch):
    await _seed_rows()
    validator = _StubValidator(
        {
            "usr": {"user_name": "u", "scope": ["user"]},
            "dev": {"user_name": "d", "scope": ["developer", "user", "a2a"]},
            "adm": {"user_name": "a", "scope": ["admin"]},
        }
    )
    monkeypatch.setattr(auth, "get_validator", lambda: validator)
    token = current_claims.set(None)
    try:
        from agents.odata.admin_routes import router

        bare = FastAPI()
        bare.include_router(router, prefix="/admin")
        async with AsyncClient(transport=ASGITransport(app=bare), base_url="http://test") as c:
            r = await c.get(AUDIT_URL)
            assert r.status_code == 401 and "items" not in r.text
            r = await c.get(AUDIT_URL, headers={"Authorization": "Bearer nobody"})
            assert r.status_code == 401
            for bearer in ("usr", "dev"):
                r = await c.get(AUDIT_URL, headers={"Authorization": f"Bearer {bearer}"})
                assert r.status_code == 403, bearer
                assert r.json() == {"detail": "Admin scope required"}
                assert "10000001" not in r.text
            r = await c.get(AUDIT_URL, headers={"Authorization": "Bearer adm"})
            assert r.status_code == 200 and len(r.json()["items"]) == 5
    finally:
        current_claims.reset(token)
