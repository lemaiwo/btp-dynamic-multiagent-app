"""A diagnose session whose target is no longer flagged ``non_production``.

The flag is checked when the session is created; it can be taken away (or the
conventions row deleted) afterwards. From then on the session reads nothing
from SAP and starts no run: every route that would call ARC-1 and every
message or report run answers 409 ``target_not_non_production``, and nothing
is sent. What only reads this app's own rows, a *denial* and the delete keep
working. A change session never looks at the flag.

Run:  python -m pytest tests/test_ide_lost_flag.py -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

(ROOT / "tests" / "_test_ide_lost_flag.db").unlink(missing_ok=True)
os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_ide_lost_flag.db'}",
)
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from agents import registry as registry_module  # noqa: E402
from agents.auth import current_jwt, require_developer  # noqa: E402
from agents.db import SessionLocal, init_db  # noqa: E402
from agents.ide import arc1, runner, stages  # noqa: E402
from agents.ide.approvals import normalize_request  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeApproval,
    IdeArtifact,
    IdeAuditLog,
    IdeConventions,
    IdeFinding,
    IdeMessage,
    IdeSession,
    IdeWorkspaceFile,
)
from agents.ide.routes import router as ide_router  # noqa: E402
from agents.ide.stages import StageGateError  # noqa: E402
from agents.ide.store import (  # noqa: E402
    add_approval,
    create_session,
    upsert_conventions,
    upsert_findings,
)

ALL_MODELS = (IdeFinding, IdeApproval, IdeAuditLog, IdeWorkspaceFile,
              IdeArtifact, IdeMessage, IdeSession, IdeConventions)
CODE = "target_not_non_production"
STORED_RAW = "STORED-RAW-TEXT of the dump, raised by DEVUSER01"
FILE_PATH = "src/PROG/zdemo_report.prog.abap"
TRACE = {"processType": "http", "objectType": "url", "maxExecutions": 1,
         "expiresHours": 1, "description": "slow list"}
HEADERS = {"x-test-user": "alice", "x-test-jwt": "user.jwt.token"}


def _fake_developer(request: Request) -> dict:
    if request.headers.get("x-test-user") != "alice":
        raise HTTPException(status_code=403, detail="Developer scope required")
    return {"user_name": "alice", "scope": ["developer"]}


class _JwtMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        value = dict(scope.get("headers") or []).get(b"x-test-jwt")
        token = current_jwt.set(value.decode() if value else None)
        try:
            await self.app(scope, receive, send)
        finally:
            current_jwt.reset(token)


@pytest.fixture(autouse=True)
async def _clean_db():
    await init_db()
    async with SessionLocal() as db:
        for model in ALL_MODELS:
            await db.execute(model.__table__.delete())
        await db.commit()
        await upsert_conventions(db, "T1", label="Target one",
                                 destination="arc1-abap-readonly",
                                 non_production=True)
    arc1._SERVERS.clear()
    saved = registry_module.registry._build
    registry_module.registry._build = None  # a run that starts ends agent_missing
    yield
    registry_module.registry._build = saved
    arc1._SERVERS.clear()


@pytest.fixture
def sent(monkeypatch):
    """Every ARC-1 client built and every call made; none reaches a network."""
    seen: list = []

    def factory(target, destination="", policy="change", masking=True):
        seen.append(("built", policy, masking))

        class _Client:
            async def call(self, tool, args):
                seen.append((tool, dict(args)))
                if tool == "SAPLint":
                    return "[]"
                return f"* source of {args.get('name')}"

        return _Client()

    monkeypatch.setattr(arc1, "get_arc1_client", factory)
    return seen


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(ide_router)
    app.dependency_overrides[require_developer] = _fake_developer
    app.add_middleware(_JwtMiddleware)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


async def _session(session_type: str = "diagnose") -> str:
    """A session on T1 with one workspace file, created while T1 is flagged."""
    async with SessionLocal() as db:
        row = await create_session(db, owner="alice", title="S", target="T1",
                                   session_type=session_type)
        db.add(IdeWorkspaceFile(
            session_id=row.id, path=FILE_PATH, object_type="PROG",
            object_name="ZDEMO_REPORT", state="read", origin_source="WRITE 1.",
        ))
        await db.commit()
        return row.id


async def _finding(sid: str, **fields) -> str:
    item = {"kind": "dump", "ref_id": "20261003101530DEVHOST_DEMO_00",
            "title": "COMPUTE_INT_ZERODIVIDE", "program": "ZDEMO_REPORT",
            **fields}
    async with SessionLocal() as db:
        [row] = await upsert_findings(db, sid, [item])
        return row.id


async def _lose_flag(how: str) -> None:
    async with SessionLocal() as db:
        if how == "flag_off":
            await upsert_conventions(db, "T1", non_production=False)
        else:  # the conventions row is gone
            await db.execute(IdeConventions.__table__.delete())
            await db.commit()


async def _row(sid: str) -> IdeSession:
    async with SessionLocal() as db:
        return await db.get(IdeSession, sid)


async def _messages(sid: str) -> list[IdeMessage]:
    async with SessionLocal() as db:
        return list((await db.execute(
            select(IdeMessage).where(IdeMessage.session_id == sid))).scalars())


def _refused(r) -> None:
    assert r.status_code == 409, r.text
    assert r.json()["code"] == CODE
    assert STORED_RAW not in r.text


LOST = pytest.mark.parametrize("how", ["flag_off", "no_row"])


# --- runs --------------------------------------------------------------------


@LOST
@pytest.mark.parametrize("tail,body", [
    ("messages", {"text": "why the dump?"}), ("report", None),
])
async def test_run_routes_answer_409_before_the_stream_opens(
    client, sent, how, tail, body
):
    sid = await _session()
    await _lose_flag(how)
    r = await client.post(f"/ide/api/sessions/{sid}/{tail}", json=body,
                          headers=HEADERS)
    _refused(r)
    assert r.headers["content-type"].startswith("application/json")
    row = await _row(sid)
    assert (row.status, row.run_id) == ("idle", None)
    assert await _messages(sid) == [] and sent == []


@LOST
@pytest.mark.parametrize("report", [False, True])
async def test_gate_refuses_a_diagnose_run(how, report):
    sid = await _session()
    await _lose_flag(how)
    async with SessionLocal() as db:
        session = await db.get(IdeSession, sid)
        with pytest.raises(StageGateError) as e:
            await stages.assert_can_run(db, session, revise=False, report=report)
    assert e.value.code == CODE


@pytest.mark.parametrize("report", [False, True])
async def test_start_refuses_even_when_the_gate_let_it_through(
    monkeypatch, report
):
    """Defence in depth: ``_start`` reads the switch itself."""
    sid = await _session()
    await _lose_flag("flag_off")

    async def open_gate(db, session, **kwargs):
        return None

    monkeypatch.setattr(runner, "assert_can_run", open_gate)
    with pytest.raises(StageGateError) as e:
        await runner._start(sid, "alice", None if report else "x", None, report)
    assert e.value.code == CODE
    row = await _row(sid)
    assert (row.status, row.run_id) == ("idle", None)
    assert await _messages(sid) == []


async def test_run_starts_while_the_target_is_flagged(client, sent):
    sid = await _session()
    r = await client.post(f"/ide/api/sessions/{sid}/messages",
                          json={"text": "why?"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")


# --- direct reads --------------------------------------------------------------


@LOST
@pytest.mark.parametrize("tail", ["", "?refresh=true"])
@pytest.mark.parametrize("stored", [STORED_RAW, None])
async def test_finding_detail_is_refused(client, sent, how, tail, stored):
    """Neither the live read nor the stored (raw) text."""
    sid = await _session()
    fid = await _finding(sid, detail=stored)
    await _lose_flag(how)
    r = await client.get(f"/ide/api/sessions/{sid}/findings/{fid}{tail}",
                         headers=HEADERS)
    _refused(r)
    assert sent == []


@LOST
async def test_finding_open_is_refused(client, sent, how):
    sid = await _session()
    fid = await _finding(sid)
    await _lose_flag(how)
    r = await client.post(f"/ide/api/sessions/{sid}/findings/{fid}/open",
                          headers=HEADERS)
    _refused(r)
    assert sent == []


@LOST
@pytest.mark.parametrize("tail", ["file/refresh", "file/lint"])
async def test_file_refresh_and_lint_are_refused(client, sent, how, tail):
    sid = await _session()
    await _lose_flag(how)
    r = await client.post(f"/ide/api/sessions/{sid}/{tail}",
                          params={"path": FILE_PATH}, headers=HEADERS)
    _refused(r)
    assert sent == []


@LOST
async def test_open_object_is_refused(client, sent, how):
    sid = await _session()
    await _lose_flag(how)
    r = await client.post(f"/ide/api/sessions/{sid}/open",
                          json={"type": "PROG", "name": "ZOTHER"},
                          headers=HEADERS)
    _refused(r)
    assert sent == []
    async with SessionLocal() as db:
        files = (await db.execute(select(IdeWorkspaceFile))).scalars().all()
    assert [f.path for f in files] == [FILE_PATH]


async def test_direct_reads_work_while_the_target_is_flagged(client, sent):
    sid = await _session()
    fid = await _finding(sid)
    base = f"/ide/api/sessions/{sid}"
    for r in (
        await client.get(f"{base}/findings/{fid}", headers=HEADERS),
        await client.post(f"{base}/findings/{fid}/open", headers=HEADERS),
        await client.post(f"{base}/file/refresh", params={"path": FILE_PATH},
                          headers=HEADERS),
        await client.post(f"{base}/file/lint", params={"path": FILE_PATH},
                          headers=HEADERS),
        await client.post(f"{base}/open", json={"type": "PROG", "name": "ZOTHER"},
                          headers=HEADERS),
    ):
        assert r.status_code == 200, r.text
    assert [s[0] for s in sent if s[0] != "built"] == [
        "SAPDiagnose", "SAPRead", "SAPRead", "SAPLint", "SAPRead",
    ]


# --- what keeps working ----------------------------------------------------------


@LOST
async def test_stored_rows_deny_and_delete_keep_working(client, sent, how):
    sid = await _session()
    fid = await _finding(sid, detail=STORED_RAW)
    async with SessionLocal() as db:
        db.add(IdeMessage(session_id=sid, stage="investigate", role="user",
                          content="why the dump?"))
        db.add(IdeArtifact(session_id=sid, stage="investigate", kind="report",
                           content="# Report", version=1))
        await db.commit()
        approval = await add_approval(
            db, sid, run_id="r1", tool_call_id="c1", action="trace_start",
            params=normalize_request("trace_start", TRACE),
        )
        aid = approval.id
    await _lose_flag(how)
    base = f"/ide/api/sessions/{sid}"

    r = await client.get(f"{base}/messages", headers=HEADERS)
    assert r.status_code == 200 and "why the dump?" in r.text
    r = await client.get(f"{base}/artifacts", headers=HEADERS)
    assert r.status_code == 200 and [a["kind"] for a in r.json()] == ["report"]
    artifact_id = r.json()[0]["id"]
    r = await client.get(f"{base}/artifacts/{artifact_id}", headers=HEADERS)
    assert r.status_code == 200 and r.json()["content"] == "# Report"
    r = await client.get(f"{base}/findings", headers=HEADERS)
    assert r.status_code == 200 and [f["id"] for f in r.json()] == [fid]
    assert STORED_RAW not in r.text  # the list never carried the detail
    r = await client.get(f"{base}/files", headers=HEADERS)
    assert r.status_code == 200 and [f["path"] for f in r.json()] == [FILE_PATH]
    r = await client.get(f"{base}/file", params={"path": FILE_PATH},
                         headers=HEADERS)
    assert r.status_code == 200 and r.json()["origin_source"] == "WRITE 1."
    r = await client.get(f"{base}/approvals", headers=HEADERS)
    assert r.status_code == 200
    assert [(a["id"], a["status"]) for a in r.json()] == [(aid, "pending")]

    r = await client.post(f"{base}/approvals/{aid}", json={"decision": "deny"},
                          headers=HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "denied", r.text

    r = await client.delete(base, headers=HEADERS)
    assert r.status_code == 204
    assert await _row(sid) is None
    assert sent == []


# --- change sessions never look at the flag ----------------------------------------


@LOST
async def test_change_session_is_unaffected(client, sent, how):
    sid = await _session("change")
    await _lose_flag(how)
    base = f"/ide/api/sessions/{sid}"
    if how == "flag_off":  # without a conventions row there is no target to call
        for r in (
            await client.post(f"{base}/file/refresh", params={"path": FILE_PATH},
                              headers=HEADERS),
            await client.post(f"{base}/file/lint", params={"path": FILE_PATH},
                              headers=HEADERS),
            await client.post(f"{base}/open",
                              json={"type": "PROG", "name": "ZOTHER"},
                              headers=HEADERS),
        ):
            assert r.status_code == 200, r.text
        assert [s[0] for s in sent if s[0] != "built"] == [
            "SAPRead", "SAPLint", "SAPRead",
        ]
        assert {s[1] for s in sent if s[0] == "built"} == {"change"}
    async with SessionLocal() as db:
        session = await db.get(IdeSession, sid)
        await stages.assert_can_run(db, session, revise=False)
    r = await client.post(f"{base}/messages", json={"text": "hello"},
                          headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    assert [m.content for m in await _messages(sid) if m.role == "user"] == ["hello"]
