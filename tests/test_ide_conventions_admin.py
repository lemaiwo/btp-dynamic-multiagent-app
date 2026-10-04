"""Conventions create, update and clear (contract §1.2, decision D4).

Every write is admin-only: a developer reads the conventions but never
writes them. ``POST /conventions`` is the only way to create a target;
``PUT /conventions/{target}`` updates an existing one (404 otherwise) and
can clear text fields. ``non_production`` takes a strict boolean and cannot
be cleared; taking it away deletes nothing, it only makes the diagnose
sessions on the target refuse their runs.

Run:  python -m pytest tests/test_ide_conventions_admin.py -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

(ROOT / "tests" / "_test_ide_conventions_admin.db").unlink(missing_ok=True)
os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_ide_conventions_admin.db'}",
)
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import json  # noqa: E402
import logging  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from agents import registry as registry_module  # noqa: E402
from agents.auth import require_admin, require_developer  # noqa: E402
from agents.db import SessionLocal, init_db  # noqa: E402
from agents.ide import schemas, store  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeAuditLog,
    IdeConventions,
    IdeMessage,
    IdeSession,
)
from agents.ide.routes import router as ide_router  # noqa: E402

USERS = {
    "dev": {"user_name": "DEVUSER01", "scope": ["developer"]},
    "admin": {"user_name": "jane.doe@example.com", "scope": ["developer", "admin"]},
}


def _fake_developer(request: Request) -> dict:
    user = request.headers.get("x-test-user", "")
    if user not in USERS:
        raise HTTPException(status_code=403, detail="Developer scope required")
    return USERS[user]


def _fake_admin(request: Request) -> dict:
    claims = _fake_developer(request)
    if "admin" not in claims["scope"]:
        raise HTTPException(status_code=403, detail="Admin scope required")
    return claims


DEV = {"x-test-user": "dev"}
ADMIN = {"x-test-user": "admin"}


@pytest.fixture(autouse=True)
async def _clean_db():
    await init_db()
    async with SessionLocal() as db:
        for model in (IdeAuditLog, IdeMessage, IdeSession, IdeConventions):
            await db.execute(model.__table__.delete())
        await db.commit()
        await store.upsert_conventions(db, "DEMO", label="Demo", namespace="Z",
                                       package="ZDEMO", destination="arc1-abap-readonly")
    saved = registry_module.registry._build
    registry_module.registry._build = None  # a run that starts ends agent_missing
    yield
    registry_module.registry._build = saved


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(ide_router)
    app.dependency_overrides[require_developer] = _fake_developer
    app.dependency_overrides[require_admin] = _fake_admin
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


async def _row(target: str) -> IdeConventions | None:
    async with SessionLocal() as db:
        return await db.get(IdeConventions, target)


# --- POST /conventions ---------------------------------------------------------


async def test_create_as_admin(client):
    body = {"target": "DEMO2", "label": "Second", "namespace": "/ACME/",
            "non_production": True}
    r = await client.post("/ide/api/conventions", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["target"] == "DEMO2" and out["label"] == "Second"
    assert out["namespace"] == "/ACME/" and out["non_production"] is True
    # Omitted fields take the model defaults.
    assert out["package"] == "" and out["clean_core_level"] == "A"
    r = await client.get("/ide/api/conventions/DEMO2", headers=DEV)
    assert r.status_code == 200 and r.json()["label"] == "Second"


async def test_create_minimal_defaults_to_production(client):
    r = await client.post("/ide/api/conventions", json={"target": "DEMO2"},
                          headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["non_production"] is False


async def test_create_duplicate_is_409_and_keeps_the_row(client):
    r = await client.post("/ide/api/conventions",
                          json={"target": "DEMO", "label": "Overwritten"},
                          headers=ADMIN)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "target_exists"
    assert (await _row("DEMO")).label == "Demo"


async def test_create_as_developer_is_403(client):
    r = await client.post("/ide/api/conventions", json={"target": "DEMO2"},
                          headers=DEV)
    assert r.status_code == 403
    assert await _row("DEMO2") is None


@pytest.mark.parametrize("value", ["true", "yes", 1, 0, [True], "false"])
async def test_create_non_production_must_be_a_real_boolean(client, value):
    r = await client.post("/ide/api/conventions",
                          json={"target": "DEMO2", "non_production": value},
                          headers=ADMIN)
    assert r.status_code == 422, r.text
    assert await _row("DEMO2") is None


@pytest.mark.parametrize("target", [
    "", "bad target", "a/b", "x" * 65, "DEMO;DROP", "ü", ".", "..", "-x",
])
async def test_create_rejects_bad_target_names(client, target):
    r = await client.post("/ide/api/conventions", json={"target": target},
                          headers=ADMIN)
    assert r.status_code == 422, r.text
    # One rule, in the schema: the standard validation answer, no custom code.
    assert isinstance(r.json()["detail"], list) and "code" not in r.json()
    async with SessionLocal() as db:
        assert [c.target for c in await store.list_conventions(db)] == ["DEMO"]


def test_target_pattern_is_the_single_shared_rule():
    import re

    from agents.ide import routes

    assert schemas.TARGET_PATTERN == r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$"
    assert not hasattr(routes, "_TARGET_PATTERN")
    assert re.fullmatch(schemas.TARGET_PATTERN, "A" + "x" * 63)
    assert not re.fullmatch(schemas.TARGET_PATTERN, "A" + "x" * 64)
    assert not re.fullmatch(schemas.TARGET_PATTERN, "_DEMO")


async def test_create_rejects_unknown_fields_and_clear(client):
    for extra in ({"bogus": 1}, {"clear": ["label"]}):
        r = await client.post("/ide/api/conventions",
                              json={"target": "DEMO2", **extra}, headers=ADMIN)
        assert r.status_code == 422, r.text
    assert await _row("DEMO2") is None


# --- PUT /conventions/{target} -------------------------------------------------


async def test_put_unknown_target_is_404_and_creates_nothing(client):
    r = await client.put("/ide/api/conventions/NEW", json={"label": "New"},
                         headers=ADMIN)
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "unknown_target"
    assert await _row("NEW") is None


async def test_put_as_developer_is_403(client):
    r = await client.put("/ide/api/conventions/DEMO", json={"label": "X"},
                         headers=DEV)
    assert r.status_code == 403
    r = await client.put("/ide/api/conventions/DEMO", json={"clear": ["package"]},
                         headers=DEV)
    assert r.status_code == 403
    row = await _row("DEMO")
    assert (row.label, row.package) == ("Demo", "ZDEMO")


async def test_put_clear_empties_the_field(client):
    r = await client.put("/ide/api/conventions/DEMO",
                         json={"label": "Renamed", "clear": ["package", "destination"]},
                         headers=ADMIN)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["label"] == "Renamed" and out["namespace"] == "Z"
    assert out["package"] == "" and out["destination"] == ""
    row = await _row("DEMO")
    assert (row.package, row.destination, row.namespace) == ("", "", "Z")


async def test_put_set_and_clear_same_field_is_422(client):
    r = await client.put("/ide/api/conventions/DEMO",
                         json={"package": "Z", "clear": ["package"]}, headers=ADMIN)
    assert r.status_code == 422, r.text
    assert (await _row("DEMO")).package == "ZDEMO"


@pytest.mark.parametrize("field", ["non_production", "clean_core_level", "target",
                                   "updated_at", "bogus"])
async def test_put_clear_only_offers_text_fields(client, field):
    r = await client.put("/ide/api/conventions/DEMO", json={"clear": [field]},
                         headers=ADMIN)
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("value", ["true", 1, 0, "false"])
async def test_put_non_production_must_be_a_real_boolean(client, value):
    r = await client.put("/ide/api/conventions/DEMO", json={"non_production": value},
                         headers=ADMIN)
    assert r.status_code == 422, r.text
    assert (await _row("DEMO")).non_production is False


# --- un-flagging a target -------------------------------------------------------


async def test_unflagging_keeps_sessions_and_refuses_their_runs(client):
    r = await client.put("/ide/api/conventions/DEMO", json={"non_production": True},
                         headers=ADMIN)
    assert r.status_code == 200 and r.json()["non_production"] is True
    r = await client.post("/ide/api/sessions",
                          json={"title": "Dump", "target": "DEMO", "type": "diagnose"},
                          headers=DEV)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    r = await client.put("/ide/api/conventions/DEMO", json={"non_production": False},
                         headers=ADMIN)
    assert r.status_code == 200 and r.json()["non_production"] is False

    # Nothing is deleted: the session is still there for its owner ...
    r = await client.get(f"/ide/api/sessions/{sid}", headers=DEV)
    assert r.status_code == 200 and r.json()["target_non_production"] is False
    # ... but a run is refused before any stream opens.
    r = await client.post(f"/ide/api/sessions/{sid}/messages",
                          json={"text": "why the dump?"}, headers=DEV)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "target_not_non_production"
    async with SessionLocal() as db:
        row = await db.get(IdeSession, sid)
        assert (row.status, row.run_id) == ("idle", None)


# --- store helpers --------------------------------------------------------------


async def test_store_create_and_update():
    async with SessionLocal() as db:
        with pytest.raises(store.ConventionsError) as err:
            await store.create_conventions(db, "DEMO", label="x")
        assert err.value.code == "target_exists"
    async with SessionLocal() as db:
        row = await store.create_conventions(db, "DEMO2", label="Two")
        assert row.target == "DEMO2" and row.label == "Two"
        assert await store.update_conventions(db, "NOPE", {"label": "x"}, []) is None
        row = await store.update_conventions(db, "DEMO", {"namespace": "Y"}, ["package"])
        assert (row.namespace, row.package, row.label) == ("Y", "", "Demo")
        with pytest.raises(ValueError):
            await store.update_conventions(db, "DEMO", {}, ["non_production"])
        with pytest.raises(ValueError):
            await store.create_conventions(db, "DEMO3", bogus="x")


# --- audit trail of the non_production flag -------------------------------------


ADMIN_PRINCIPAL = USERS["admin"]["user_name"]


async def _audit_rows() -> list[IdeAuditLog]:
    # One write can stage a flag and a destination row together: within the
    # same timestamp the flag row comes first.
    async with SessionLocal() as db:
        rows = list((await db.execute(select(IdeAuditLog))).scalars())
    return sorted(rows, key=lambda r: (r.ts, r.action != "conventions_flag"))


def _flag_change(row: IdeAuditLog, target: str, old, new) -> None:
    assert row.principal == ADMIN_PRINCIPAL and row.target == target
    assert (row.action, row.outcome) == ("conventions_flag", "ok")
    assert json.loads(row.params_json) == {"non_production": {"old": old, "new": new}}


def _audit_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == "agents.ide.audit" and r.levelno == logging.INFO]


async def test_create_with_flag_is_audited(client, caplog):
    caplog.set_level(logging.INFO, logger="agents.ide.audit")
    r = await client.post("/ide/api/conventions",
                          json={"target": "DEMO2", "non_production": True,
                                "free_text": "SECRET-FREE-TEXT",
                                "destination": "arc1-abap-readonly"},
                          headers=ADMIN)
    assert r.status_code == 201, r.text
    # The flag row holds the flag only; the destination has its own row
    # (``conventions_destination``, see below).
    row, dest = await _audit_rows()
    _flag_change(row, "DEMO2", None, True)
    assert "SECRET-FREE-TEXT" not in row.params_json
    assert "arc1-abap-readonly" not in row.params_json
    assert dest.action == "conventions_destination"
    assert "SECRET-FREE-TEXT" not in dest.params_json
    line, dest_line = _audit_lines(caplog)
    assert "conventions_flag" in line and "DEMO2" in line and ADMIN_PRINCIPAL in line
    assert "SECRET-FREE-TEXT" not in line and "arc1-abap-readonly" not in line
    assert "conventions_destination" in dest_line
    assert "SECRET-FREE-TEXT" not in dest_line


async def test_create_without_flag_writes_no_audit(client, caplog):
    caplog.set_level(logging.INFO, logger="agents.ide.audit")
    for body in ({"target": "DEMO2"}, {"target": "DEMO3", "non_production": False}):
        r = await client.post("/ide/api/conventions", json=body, headers=ADMIN)
        assert r.status_code == 201, r.text
    assert await _audit_rows() == [] and _audit_lines(caplog) == []


async def test_flag_flips_are_audited_both_ways(client, caplog):
    caplog.set_level(logging.INFO, logger="agents.ide.audit")
    r = await client.put("/ide/api/conventions/DEMO",
                         json={"non_production": True, "free_text": "SECRET-FREE-TEXT"},
                         headers=ADMIN)
    assert r.status_code == 200, r.text
    r = await client.put("/ide/api/conventions/DEMO", json={"non_production": False},
                         headers=ADMIN)
    assert r.status_code == 200, r.text
    on, off = [r for r in await _audit_rows() if r.action == "conventions_flag"]
    _flag_change(on, "DEMO", False, True)
    _flag_change(off, "DEMO", True, False)
    # Each flip also names the destination in force (see below).
    lines = _audit_lines(caplog)
    assert len(lines) == 4 and not any("SECRET-FREE-TEXT" in x for x in lines)


async def test_unchanged_flag_writes_no_audit(client, caplog):
    caplog.set_level(logging.INFO, logger="agents.ide.audit")
    for body in ({"non_production": False}, {"label": "Renamed"},
                 {"clear": ["package"]}):
        r = await client.put("/ide/api/conventions/DEMO", json=body, headers=ADMIN)
        assert r.status_code == 200, r.text
    assert await _audit_rows() == [] and _audit_lines(caplog) == []


async def test_refused_writes_leave_no_audit(client, caplog):
    caplog.set_level(logging.INFO, logger="agents.ide.audit")
    r = await client.put("/ide/api/conventions/DEMO", json={"non_production": True},
                         headers=DEV)
    assert r.status_code == 403
    r = await client.post("/ide/api/conventions",
                          json={"target": "DEMO2", "non_production": True}, headers=DEV)
    assert r.status_code == 403
    r = await client.post("/ide/api/conventions",
                          json={"target": "DEMO", "non_production": True}, headers=ADMIN)
    assert r.status_code == 409
    r = await client.put("/ide/api/conventions/NOPE", json={"non_production": True},
                         headers=ADMIN)
    assert r.status_code == 404
    assert await _audit_rows() == [] and _audit_lines(caplog) == []
    assert (await _row("DEMO")).non_production is False


async def test_store_flag_change_needs_an_actor():
    async with SessionLocal() as db:
        with pytest.raises(ValueError):
            await store.update_conventions(db, "DEMO", {"non_production": True}, [])
    assert (await _row("DEMO")).non_production is False
    assert await _audit_rows() == []


# --- destination changes on a non_production target (N5) ----------------------
#
# The destination decides which ARC-1 server a diagnose session reads raw
# runtime data from; on a flagged target a change of it is audited like the
# flag itself (same transaction, old and new value only).


def _destination_change(row: IdeAuditLog, target: str, old, new) -> None:
    assert row.principal == ADMIN_PRINCIPAL and row.target == target
    assert (row.action, row.outcome) == ("conventions_destination", "ok")
    assert json.loads(row.params_json) == {"destination": {"old": old, "new": new}}


async def _flag_demo(client) -> None:
    r = await client.put("/ide/api/conventions/DEMO", json={"non_production": True},
                         headers=ADMIN)
    assert r.status_code == 200, r.text


async def test_destination_change_on_flagged_target_is_audited(client, caplog):
    await _flag_demo(client)
    caplog.set_level(logging.INFO, logger="agents.ide.audit")
    r = await client.put("/ide/api/conventions/DEMO",
                         json={"destination": "arc1-other",
                               "free_text": "SECRET-FREE-TEXT"},
                         headers=ADMIN)
    assert r.status_code == 200, r.text
    _in_force, row = await _dest_rows()
    _destination_change(row, "DEMO", "arc1-abap-readonly", "arc1-other")
    assert "SECRET-FREE-TEXT" not in row.params_json
    [line] = _audit_lines(caplog)
    assert "conventions_destination" in line and "arc1-other" in line
    assert "SECRET-FREE-TEXT" not in line


async def test_destination_cleared_and_set_on_flagged_target_is_audited(client):
    await _flag_demo(client)
    r = await client.put("/ide/api/conventions/DEMO", json={"clear": ["destination"]},
                         headers=ADMIN)
    assert r.status_code == 200, r.text
    r = await client.put("/ide/api/conventions/DEMO",
                         json={"destination": "arc1-abap-readonly"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    _in_force, cleared, set_ = await _dest_rows()
    _destination_change(cleared, "DEMO", "arc1-abap-readonly", None)
    _destination_change(set_, "DEMO", None, "arc1-abap-readonly")


async def test_destination_change_with_the_flag_is_audited_in_one_go(client):
    r = await client.put("/ide/api/conventions/DEMO",
                         json={"non_production": True, "destination": "arc1-other"},
                         headers=ADMIN)
    assert r.status_code == 200, r.text
    flag, dest = await _audit_rows()
    _flag_change(flag, "DEMO", False, True)
    _destination_change(dest, "DEMO", "arc1-abap-readonly", "arc1-other")
    # Unflagging together with a change: still a flagged target's change.
    r = await client.put("/ide/api/conventions/DEMO",
                         json={"non_production": False, "destination": "arc1-third"},
                         headers=ADMIN)
    assert r.status_code == 200, r.text
    rows = await _audit_rows()
    assert len(rows) == 4
    _destination_change(rows[3], "DEMO", "arc1-other", "arc1-third")


async def test_create_flagged_target_with_destination_is_audited(client):
    r = await client.post("/ide/api/conventions",
                          json={"target": "DEMO2", "non_production": True,
                                "destination": "arc1-abap-readonly"},
                          headers=ADMIN)
    assert r.status_code == 201, r.text
    flag, dest = await _audit_rows()
    _flag_change(flag, "DEMO2", None, True)
    _destination_change(dest, "DEMO2", None, "arc1-abap-readonly")


async def test_destination_unchanged_or_production_writes_no_audit(client, caplog):
    caplog.set_level(logging.INFO, logger="agents.ide.audit")
    # Production target: not audited.
    r = await client.put("/ide/api/conventions/DEMO",
                         json={"destination": "arc1-other"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    r = await client.post("/ide/api/conventions",
                          json={"target": "DEMO2", "destination": "arc1-x"},
                          headers=ADMIN)
    assert r.status_code == 201, r.text
    assert await _audit_rows() == []
    # Flagged (which names the destination in force), then the same value
    # again: nothing more to record.
    await _flag_demo(client)
    before = [row.action for row in await _audit_rows()]
    assert before == ["conventions_flag", "conventions_destination"]
    r = await client.put("/ide/api/conventions/DEMO",
                         json={"destination": "arc1-other", "label": "L"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert [row.action for row in await _audit_rows()] == before


async def test_store_destination_change_on_flagged_target_needs_an_actor():
    async with SessionLocal() as db:
        await store.update_conventions(db, "DEMO", {"non_production": True}, [],
                                       actor=ADMIN_PRINCIPAL)
    async with SessionLocal() as db:
        with pytest.raises(ValueError):
            await store.update_conventions(db, "DEMO", {"destination": "x"}, [])
    assert (await _row("DEMO")).destination == "arc1-abap-readonly"


# --- the destination in force when the flag changes (re-review) ----------------


async def _dest_rows() -> list[IdeAuditLog]:
    return [r for r in await _audit_rows() if r.action == "conventions_destination"]


async def test_flag_change_names_the_destination_in_force(client):
    # DEMO has a destination; the flag goes on, then off, destination unchanged.
    await _flag_demo(client)
    [on] = await _dest_rows()
    _destination_change(on, "DEMO", "arc1-abap-readonly", "arc1-abap-readonly")
    r = await client.put("/ide/api/conventions/DEMO", json={"non_production": False},
                         headers=ADMIN)
    assert r.status_code == 200, r.text
    _on, off = await _dest_rows()
    _destination_change(off, "DEMO", "arc1-abap-readonly", "arc1-abap-readonly")


async def test_flag_change_without_any_destination_writes_no_destination_row(client):
    r = await client.put("/ide/api/conventions/DEMO",
                         json={"clear": ["destination"]}, headers=ADMIN)
    assert r.status_code == 200, r.text
    await _flag_demo(client)
    assert await _dest_rows() == []
    assert [r.action for r in await _audit_rows()] == ["conventions_flag"]


async def test_unflagged_target_changing_destination_writes_no_row(client):
    for body in ({"destination": "arc1-other"}, {"clear": ["destination"]}):
        r = await client.put("/ide/api/conventions/DEMO", json=body, headers=ADMIN)
        assert r.status_code == 200, r.text
    assert await _audit_rows() == []


async def test_no_destination_is_null_in_create_and_update(client):
    await _flag_demo(client)
    r = await client.put("/ide/api/conventions/DEMO", json={"clear": ["destination"]},
                         headers=ADMIN)
    assert r.status_code == 200, r.text
    r = await client.put("/ide/api/conventions/DEMO", json={"destination": "arc1-x"},
                         headers=ADMIN)
    assert r.status_code == 200, r.text
    r = await client.post("/ide/api/conventions",
                          json={"target": "DEMO2", "non_production": True,
                                "destination": "arc1-y"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    params = [json.loads(r.params_json)["destination"] for r in await _dest_rows()]
    assert {"old": "arc1-abap-readonly", "new": None} in params
    assert {"old": None, "new": "arc1-x"} in params
    assert {"old": None, "new": "arc1-y"} in params
    assert all("" not in (p["old"], p["new"]) for p in params)
