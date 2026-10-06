"""Worklist fields on ``SessionOut`` (B10 carry-forward from the U6 worklist).

``GET /sessions`` (and every route answering a ``SessionOut``) carries, so
the worklist needs no ``GET /sessions/{id}`` per row:

- ``objects``: the distinct object names of the session's files (scratch
  notes are not objects), in path order, at most ``OBJECTS_SHOWN``;
- ``objects_total``: how many distinct object names there are;
- ``changed_objects``: distinct object names with a ``new`` or ``modified``
  file;
- ``findings_count``: the findings of a diagnose session; ``null`` for a
  change session.

The list is computed with a fixed number of queries, whatever the number of
sessions (no N+1).

Run:  python -m pytest tests/test_ide_worklist_fields.py -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

from agents.auth import require_developer  # noqa: E402
from agents.db import SessionLocal, engine, init_db  # noqa: E402
from agents.ide import store  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeConventions,
    IdeFinding,
    IdeSession,
    IdeWorkspaceFile,
)
from agents.ide.paths import object_for  # noqa: E402
from agents.ide.routes import router as ide_router  # noqa: E402

USERS = {"alice": {"user_name": "alice", "scope": ["developer"]}}
ALICE = {"x-test-user": "alice"}


def _fake_developer(request: Request) -> dict:
    user = request.headers.get("x-test-user", "")
    if user not in USERS:
        raise HTTPException(status_code=403, detail="Developer scope required")
    return USERS[user]


@pytest.fixture(autouse=True)
async def _clean():
    await init_db()
    async with SessionLocal() as db:
        for model in (IdeFinding, IdeWorkspaceFile, IdeSession, IdeConventions):
            await db.execute(model.__table__.delete())
        await db.commit()
        await store.upsert_conventions(db, "DEMO", label="Demo")
        await store.upsert_conventions(db, "SANDBOX", label="Sandbox",
                                       non_production=True, actor="test-admin")


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(ide_router)
    app.dependency_overrides[require_developer] = _fake_developer
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


async def _session(session_type: str = "change") -> str:
    async with SessionLocal() as db:
        s = await store.create_session(
            db, owner="alice", title="t",
            target="SANDBOX" if session_type == "diagnose" else "DEMO",
            session_type=session_type)
        return s.id


async def _file(sid: str, path: str, state: str) -> None:
    obj = object_for(path)
    async with SessionLocal() as db:
        db.add(IdeWorkspaceFile(
            session_id=sid, path=path, state=state,
            object_type=obj[0] if obj else None,
            object_name=obj[1] if obj else None,
            origin_source="o" if state != "new" else None,
            proposed_source="p" if state != "read" else None))
        await db.commit()


async def _finding(sid: str, ref: str) -> None:
    async with SessionLocal() as db:
        db.add(IdeFinding(session_id=sid, kind="dump", ref_id=ref, title="t"))
        await db.commit()


def _by_id(rows: list[dict]) -> dict[str, dict]:
    return {r["id"]: r for r in rows}


async def test_session_carries_its_objects_and_changes(client):
    sid = await _session()
    await _file(sid, "src/CLAS/zcl_b.clas.abap", "modified")
    await _file(sid, "src/CLAS/zcl_b.clas.testclasses.abap", "read")
    await _file(sid, "src/CLAS/zcl_a.clas.abap", "read")
    await _file(sid, "src/PROG/zreport.prog.abap", "new")
    await _file(sid, "notes/impact.md", "new")  # scratch: not an object
    r = await client.get("/ide/api/sessions", headers=ALICE)
    row = _by_id(r.json())[sid]
    assert row["objects"] == ["ZCL_A", "ZCL_B", "ZREPORT"]
    assert row["objects_total"] == 3
    assert row["changed_objects"] == 2
    assert row["findings_count"] is None
    # Same fields on the detail and on single-session answers.
    d = (await client.get(f"/ide/api/sessions/{sid}", headers=ALICE)).json()
    assert (d["objects"], d["objects_total"], d["changed_objects"]) == (
        ["ZCL_A", "ZCL_B", "ZREPORT"], 3, 2)


async def test_objects_are_capped_with_a_total(client):
    sid = await _session()
    for i in range(store.OBJECTS_SHOWN + 3):
        await _file(sid, f"src/CLAS/zcl_{i:02d}.clas.abap", "read")
    row = _by_id((await client.get("/ide/api/sessions", headers=ALICE)).json())[sid]
    assert row["objects"] == [f"ZCL_{i:02d}" for i in range(store.OBJECTS_SHOWN)]
    assert row["objects_total"] == store.OBJECTS_SHOWN + 3
    assert row["changed_objects"] == 0


async def test_empty_session(client):
    sid = await _session()
    row = _by_id((await client.get("/ide/api/sessions", headers=ALICE)).json())[sid]
    assert (row["objects"], row["objects_total"], row["changed_objects"],
            row["findings_count"]) == ([], 0, 0, None)


async def test_diagnose_session_counts_findings(client):
    sid = await _session("diagnose")
    for ref in ("D1", "D2", "D3"):
        await _finding(sid, ref)
    other = await _session("diagnose")
    rows = _by_id((await client.get("/ide/api/sessions", headers=ALICE)).json())
    assert rows[sid]["findings_count"] == 3
    assert rows[other]["findings_count"] == 0


async def test_new_session_answer_has_the_fields(client):
    r = await client.post("/ide/api/sessions", json={"title": "x", "target": "DEMO"},
                          headers=ALICE)
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["objects"], body["objects_total"], body["changed_objects"],
            body["findings_count"]) == ([], 0, 0, None)


async def _count_statements(client) -> int:
    seen: list[str] = []

    def before(conn, cursor, statement, *args):
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    try:
        r = await client.get("/ide/api/sessions", headers=ALICE)
        assert r.status_code == 200
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)
    return len(seen)


async def test_list_is_not_n_plus_one(client):
    async def populate(n: int):
        for _ in range(n):
            sid = await _session()
            await _file(sid, "src/CLAS/zcl_x.clas.abap", "modified")
            did = await _session("diagnose")
            await _finding(did, "D")

    await populate(1)
    few = await _count_statements(client)
    await populate(6)
    many = await _count_statements(client)
    assert many == few
