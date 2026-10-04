"""Findings routes of a diagnose session: list, detail, open source.

What this suite pins:

* every route checks the developer scope and the owner first: another
  user's session is 404, and a finding id is only looked up inside the owned
  session (a finding of another session -- the caller's own or not -- is 404);
* the list never carries the detail text;
* on a ``non_production`` target the detail route serves the text stored
  with the finding, and ``?refresh=true`` re-reads it live;
* on a target that is *not* (or no longer) ``non_production`` neither the
  stored text is served nor SAP read: 409 ``target_not_non_production``;
* a detail read writes nothing (row counts and the stored text unchanged);
* the live re-read always uses ``policy="diagnose"``, and its arguments are built
  from the finding's kind and ``ref_id`` only;
* "open source" maps program/include to a workspace file through
  ``paths.resolve_include`` and reads it with SAPRead and an explicit type;
* ARC-1 failures answer 424 ``user_token_required`` / 502 ``sap_*``.

No network: either ``arc1.get_arc1_client`` is replaced (a recording fake),
or the real ``Arc1Client`` runs with its ``_send`` replaced, so its policy
check is the real one.

Run:  python -m pytest tests/test_ide_findings_routes.py -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

(ROOT / "tests" / "_test_ide_findings_routes.db").unlink(missing_ok=True)
os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_ide_findings_routes.db'}",
)
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from agents.auth import current_jwt, require_developer  # noqa: E402
from agents.db import SessionLocal, init_db  # noqa: E402
from agents.ide import arc1  # noqa: E402
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
from agents.ide.paths import object_for, path_for, resolve_include  # noqa: E402
from agents.ide.routes import router as ide_router  # noqa: E402
from agents.ide.store import (  # noqa: E402
    create_session,
    upsert_conventions,
    upsert_findings,
)

FIXTURES = ROOT / "tests" / "fixtures" / "ide_diagnose"
DUMP_DETAIL = (FIXTURES / "dump_detail.json").read_text()
GATEWAY_DETAIL = (FIXTURES / "gateway_error_detail.json").read_text()
ARC1_ERROR = (FIXTURES / "arc1_error.json").read_text()

# What must never leave a target that lost its flag (the fixtures carry them).
SENTINELS = [
    "DEVUSER01", "jane.doe@example.com", "123456789012",
    "BE71 0961 2345 6769", "RAWSENTINEL",
]
STORED_RAW = "STORED-RAW-TEXT of the dump, raised by DEVUSER01"

ALL_MODELS = (IdeFinding, IdeApproval, IdeAuditLog, IdeWorkspaceFile,
              IdeArtifact, IdeMessage, IdeSession, IdeConventions)

USERS = {
    "alice": {"user_name": "alice", "scope": ["developer"]},
    "bob": {"user_name": "bob", "scope": ["developer"]},
}


# A file opened by the route has no revision and nothing checked yet.
UNCHECKED = {"revision": 0, "base_status": None, "syntax_status": None}


def _pool(cls: str, suffix: str) -> str:
    """A class-pool include name: the class padded with ``=`` to 30."""
    return cls.ljust(30, "=") + suffix


DUMP_REF = "20261003101530DEVHOST_DEMO_00"
CALC_CP = _pool("ZCL_DEMO_CALC", "CP")


def _fake_developer(request: Request) -> dict:
    user = request.headers.get("x-test-user", "")
    if user not in USERS:
        raise HTTPException(status_code=403, detail="Developer scope required")
    return USERS[user]


class _JwtMiddleware:
    """Binds ``current_jwt`` from ``x-test-jwt``, as app.py's middleware
    binds it from the bearer token."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        value = dict(scope.get("headers") or []).get(b"x-test-jwt")
        token = current_jwt.set(value.decode() if value else None)
        try:
            await self.app(scope, receive, send)
        finally:
            current_jwt.reset(token)


class FakeArc1:
    """Replaces ``arc1.get_arc1_client``: records how each client was built
    and what it was asked."""

    def __init__(self):
        self.built: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.answer: str | None = None
        self.raise_exc: Exception | None = None

    def factory(self, target: str, destination: str = "", policy: str = "change"):
        fake = self
        fake.built.append({"target": target, "destination": destination,
                           "policy": policy})

        class _Client:
            async def call(self, tool, args):
                fake.calls.append((tool, dict(args)))
                if fake.raise_exc is not None:
                    raise fake.raise_exc
                if fake.answer is not None:
                    return fake.answer
                return f"* source of {args.get('name')}"

        return _Client()


@pytest.fixture(autouse=True)
async def _clean_db():
    await init_db()
    async with SessionLocal() as db:
        for model in ALL_MODELS:
            await db.execute(model.__table__.delete())
        await db.commit()
        await upsert_conventions(db, "T1", label="Target one",
                                 destination="arc1-abap-readonly",
                                 actor="test-admin", non_production=True)
    arc1._SERVERS.clear()
    yield
    arc1._SERVERS.clear()


@pytest.fixture
def fake(monkeypatch):
    f = FakeArc1()
    monkeypatch.setattr(arc1, "get_arc1_client", f.factory)
    return f


@pytest.fixture
def wire(monkeypatch):
    """The real ``Arc1Client`` (policy check, JWT check) with only
    the MCP round trip replaced. ``sent`` is what would have gone to ARC-1."""
    state = {"sent": [], "built": [], "answer": DUMP_DETAIL}
    real_factory = arc1.get_arc1_client

    def factory(target, destination="", policy="change"):
        state["built"].append({"policy": policy})
        return real_factory(target, destination, policy)

    async def _send(self, tool, args):
        state["sent"].append((tool, dict(args), self.policy))
        return state["answer"]

    monkeypatch.setattr(arc1, "get_arc1_client", factory)
    monkeypatch.setattr(arc1.Arc1Client, "_send", _send)
    return state


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(ide_router)
    app.dependency_overrides[require_developer] = _fake_developer
    app.add_middleware(_JwtMiddleware)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


def _as(user: str, jwt: bool = True) -> dict:
    headers = {"x-test-user": user}
    if jwt:
        headers["x-test-jwt"] = "user.jwt.token"
    return headers


async def _session(owner="alice", session_type="diagnose") -> str:
    async with SessionLocal() as db:
        row = await create_session(db, owner=owner, title="S", target="T1",
                                   session_type=session_type)
        return row.id


async def _finding(sid: str, **fields) -> str:
    item = {"kind": "dump", "ref_id": DUMP_REF,
            "title": "COMPUTE_INT_ZERODIVIDE", **fields}
    async with SessionLocal() as db:
        [row] = await upsert_findings(db, sid, [item])
        return row.id


async def _flag(value: bool) -> None:
    async with SessionLocal() as db:
        await upsert_conventions(db, "T1", actor="test-admin", non_production=value)


async def _counts() -> dict[str, int]:
    out = {}
    async with SessionLocal() as db:
        for model in ALL_MODELS:
            out[model.__tablename__] = (
                await db.execute(select(func.count()).select_from(model))
            ).scalar_one()
    return out


async def _stored(fid: str) -> IdeFinding:
    async with SessionLocal() as db:
        return await db.get(IdeFinding, fid)


def _url(sid: str, fid: str = "", tail: str = "") -> str:
    base = f"/ide/api/sessions/{sid}/findings"
    return f"{base}/{fid}{tail}" if fid else base


# --- list --------------------------------------------------------------------


async def test_list_findings_owner_scoped(client):
    sid = await _session()
    other = await _session(owner="bob")
    first = await _finding(sid, ref_id="D1", program="ZREPORT", line=3,
                           occurred_at="2026-10-03T10:00:00Z", detail=STORED_RAW)
    second = await _finding(sid, kind="trace", ref_id="T1", title="ZREPORT")
    await _finding(other, ref_id="BOBS")

    r = await client.get(_url(sid), headers=_as("alice"))
    assert r.status_code == 200, r.text
    rows = r.json()
    assert {row["id"] for row in rows} == {first, second}
    by_id = {row["id"]: row for row in rows}
    assert set(by_id[first]) == {
        "id", "kind", "ref_id", "title", "program", "include", "line",
        "occurred_at", "created_at",
    }
    assert by_id[first]["program"] == "ZREPORT" and by_id[first]["line"] == 3
    assert STORED_RAW not in r.text  # the list is metadata only

    # Another user's session, and one that does not exist: the same 404.
    r = await client.get(_url(sid), headers=_as("bob"))
    assert r.status_code == 404 and "D1" not in r.text
    r = await client.get(_url("nope"), headers=_as("alice"))
    assert r.status_code == 404


async def test_list_findings_newest_first(client):
    sid = await _session()
    ids = []
    for i in range(3):
        ids.append(await _finding(sid, ref_id=f"D{i}"))
    async with SessionLocal() as db:
        from datetime import datetime, timedelta, timezone

        for i, fid in enumerate(ids):
            row = await db.get(IdeFinding, fid)
            row.created_at = datetime(2026, 10, 3, tzinfo=timezone.utc) + timedelta(minutes=i)
        await db.commit()
    r = await client.get(_url(sid), headers=_as("alice"))
    assert [row["id"] for row in r.json()] == list(reversed(ids))


@pytest.mark.parametrize("method,tail", [
    ("GET", ""), ("GET", "/{fid}"), ("GET", "/{fid}?refresh=true"),
    ("POST", "/{fid}/open"),
])
async def test_every_route_checks_scope_then_owner(client, fake, method, tail):
    sid = await _session()
    fid = await _finding(sid, program="ZREPORT", detail=STORED_RAW)
    url = _url(sid) + tail.format(fid=fid)
    before = await _counts()

    r = await client.request(method, url, headers={"x-test-user": "nobody"})
    assert r.status_code == 403
    r = await client.request(method, url, headers=_as("bob"))
    assert r.status_code == 404
    assert r.json() == {"detail": "Session not found"}
    assert fake.built == [] and fake.calls == []
    assert await _counts() == before


@pytest.mark.parametrize("method,tail", [("GET", ""), ("GET", "?refresh=true"),
                                         ("POST", "/open")])
async def test_finding_is_resolved_inside_the_owned_session_only(
    client, fake, method, tail
):
    mine = await _session()
    mine_too = await _session()
    bobs = await _session(owner="bob")
    in_other = await _finding(mine_too, program="ZREPORT", detail=STORED_RAW)
    in_bobs = await _finding(bobs, program="ZREPORT", detail=STORED_RAW)

    for fid in (in_other, in_bobs, "nope"):
        r = await client.request(method, _url(mine, fid, tail), headers=_as("alice"))
        assert r.status_code == 404, r.text
        assert r.json() == {"detail": "Finding not found"}
    assert fake.built == [] and fake.calls == []


# --- detail ------------------------------------------------------------------


async def test_detail_serves_stored_text_on_non_production(client, fake):
    sid = await _session()
    fid = await _finding(sid, program=CALC_CP, line=12, detail=STORED_RAW)
    before = await _counts()

    r = await client.get(_url(sid, fid), headers=_as("alice"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["detail"] == STORED_RAW
    assert body["finding"]["id"] == fid and "detail" not in body["finding"]
    assert fake.built == [] and fake.calls == []  # no ARC-1 call
    assert await _counts() == before


async def test_detail_refresh_rereads_live_and_writes_nothing(client, fake):
    sid = await _session()
    fid = await _finding(sid, detail=STORED_RAW)
    fake.answer = DUMP_DETAIL
    before = await _counts()

    r = await client.get(_url(sid, fid, "?refresh=true"), headers=_as("alice"))
    assert r.status_code == 200, r.text
    detail = r.json()["detail"]
    assert STORED_RAW not in detail
    # Raw on a non_production target: the dump as ARC-1 sent it.
    assert "Runtime Error: COMPUTE_INT_ZERODIVIDE" in detail
    assert "DEVUSER01" in detail
    assert fake.calls == [("SAPDiagnose", {"action": "dumps", "id": DUMP_REF})]
    assert fake.built == [{"target": "T1", "destination": "arc1-abap-readonly",
                           "policy": "diagnose"}]
    assert await _counts() == before
    assert (await _stored(fid)).detail == STORED_RAW  # not replaced


async def test_detail_without_stored_text_reads_live(client, fake):
    sid = await _session()
    fid = await _finding(sid)
    fake.answer = DUMP_DETAIL
    r = await client.get(_url(sid, fid), headers=_as("alice"))
    assert r.status_code == 200, r.text
    assert "COMPUTE_INT_ZERODIVIDE" in r.json()["detail"]
    assert len(fake.calls) == 1
    assert (await _stored(fid)).detail is None


@pytest.mark.parametrize("refresh", ["", "?refresh=true"])
async def test_detail_is_refused_once_the_flag_is_gone(client, wire, refresh):
    """The target lost its ``non_production`` flag after a raw run stored
    text: the stored text is not served and SAP is not read."""
    sid = await _session()
    fid = await _finding(sid, program=CALC_CP, line=12, detail=STORED_RAW)
    await _flag(False)
    before = await _counts()

    r = await client.get(_url(sid, fid, refresh), headers=_as("alice"))
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "target_not_non_production"
    assert STORED_RAW not in r.text
    for sentinel in SENTINELS:
        assert sentinel not in r.text, sentinel
    assert wire["built"] == [] and wire["sent"] == []
    # Nothing written: no row added, the finding untouched.
    assert await _counts() == before
    assert (await _stored(fid)).detail == STORED_RAW


async def test_detail_refused_when_conventions_are_gone(client, wire):
    """No conventions row reads as production: the same refusal, and
    the stored text is not served."""
    sid = await _session()
    fid = await _finding(sid, detail=STORED_RAW)
    async with SessionLocal() as db:
        await db.execute(IdeConventions.__table__.delete())
        await db.commit()
    r = await client.get(_url(sid, fid), headers=_as("alice"))
    assert r.status_code == 409 and r.json()["code"] == "target_not_non_production"
    assert STORED_RAW not in r.text
    assert wire["sent"] == []


async def test_detail_uses_diagnose_policy(client, fake):
    sid = await _session()
    fid = await _finding(sid)
    fake.answer = DUMP_DETAIL
    await client.get(_url(sid, fid), headers=_as("alice"))
    await _flag(False)
    r = await client.get(_url(sid, fid), headers=_as("alice"))
    assert r.status_code == 409
    # The second read built no client at all.
    assert [b["policy"] for b in fake.built] == ["diagnose"]


@pytest.mark.parametrize("kind,ref,expected", [
    ("dump", "D-1", {"action": "dumps", "id": "D-1"}),
    ("trace", "ATRA_1", {"action": "traces", "id": "ATRA_1", "analysis": "hitlist"}),
    ("gateway_error", "/sap/bc/adt/gw/errorlog/0A1B2C3D4E5F",
     {"action": "gateway_errors",
      "detailUrl": "/sap/bc/adt/gw/errorlog/0A1B2C3D4E5F"}),
    ("gateway_error", "Frontend Error:0A1B2C3D4E5F",
     {"action": "gateway_errors", "id": "0A1B2C3D4E5F",
      "errorType": "Frontend Error"}),
])
async def test_detail_arguments_per_kind(client, fake, kind, ref, expected):
    sid = await _session()
    fid = await _finding(sid, kind=kind, ref_id=ref)
    fake.answer = GATEWAY_DETAIL if kind == "gateway_error" else DUMP_DETAIL
    r = await client.get(_url(sid, fid, "?refresh=true"), headers=_as("alice"))
    assert r.status_code == 200, r.text
    assert fake.calls == [("SAPDiagnose", expected)]
    assert r.json()["detail"]


@pytest.mark.parametrize("kind,ref", [
    ("auth_check", "S_TCODE:SE38"),
    ("odata_call", "/sap/opu/odata/sap/ZDEMO_SRV"),
    # A gateway reference that is not a detail path on the ADT error log.
    ("gateway_error", "/sap/bc/adt/gw/errorlog/../../../oo/classes/zcl_x"),
    ("gateway_error", "//evil.example.com/sap/bc/adt/gw/errorlog/1"),
    ("gateway_error", "/sap/opu/odata/sap/ZDEMO_SRV"),
    ("gateway_error", "/sap/bc/adt/gw/errorlog/1?user=DEVUSER01"),
    ("gateway_error", "/sap/bc/adt/gw/errorlog/%2e%2e/x"),
    ("gateway_error", "no-colon"),
    ("dump", "has blank"),
    ("trace", "x'; drop"),
    # Dump and trace ids may hold ``/`` and ``.``, never a traversal.
    ("dump", "../../oo/classes/zcl_x"),
    ("dump", "a/../b"),
    ("dump", "//evil.example.com/x"),
    ("trace", "ATRA/../1"),
    ("trace", "a//b"),
    # Percent-encoded traversal and a smuggled query.
    ("dump", "%2e%2e/%2e%2e/oo/classes/zcl_x"),
    ("dump", "D-1?user=DEVUSER01"),
    ("trace", "ATRA_1%2f..%2fx"),
    ("trace", "ATRA_1?analysis=statements"),
    ("gateway_error", "/sap/bc/adt/gw/errorlog/a%2fb"),
    ("gateway_error", "Frontend Error:0A1B?x=1"),
    ("gateway_error", "Frontend Error:0A%2fB"),
])
async def test_detail_without_a_live_source_is_422_no_detail(client, fake, kind, ref):
    sid = await _session()
    fid = await _finding(sid, kind=kind, ref_id=ref)
    r = await client.get(_url(sid, fid, "?refresh=true"), headers=_as("alice"))
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "no_detail"
    assert fake.calls == []


async def test_detail_of_a_change_session_is_409(client, fake):
    sid = await _session(session_type="change")
    fid = await _finding(sid, detail=STORED_RAW)
    for tail in ("", "?refresh=true"):
        r = await client.get(_url(sid, fid, tail), headers=_as("alice"))
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "not_diagnose"
        assert STORED_RAW not in r.text
    assert fake.built == []


async def test_detail_without_user_token_is_424(client, wire):
    sid = await _session()
    fid = await _finding(sid)
    r = await client.get(_url(sid, fid), headers=_as("alice", jwt=False))
    assert r.status_code == 424, r.text
    assert r.json()["code"] == "user_token_required"
    assert wire["sent"] == []


async def test_detail_arc1_error_is_502_with_code(client, fake):
    sid = await _session()
    fid = await _finding(sid)
    fake.raise_exc = arc1.arc1_error_from_text("SAPDiagnose", "T1", ARC1_ERROR)
    assert fake.raise_exc is not None
    before = await _counts()
    r = await client.get(_url(sid, fid), headers=_as("alice"))
    assert r.status_code == 502, r.text
    assert r.json()["code"] == "sap_authentication_failed"
    assert await _counts() == before


async def test_detail_never_sends_a_user_filter(client, wire):
    """The arguments come from kind and ref_id; whatever the finding's other
    fields hold, the diagnose policy (no ``user``) is what the client checks."""
    sid = await _session()
    fid = await _finding(sid, program="DEVUSER01", include="DEVUSER01")
    await client.get(_url(sid, fid), headers=_as("alice"))
    [(tool, args, policy)] = wire["sent"]
    assert tool == "SAPDiagnose" and policy == "diagnose"
    assert set(args) == {"action", "id"}


# --- open source -------------------------------------------------------------


async def _open(client, sid, fid, user="alice"):
    return await client.post(_url(sid, fid, "/open"), headers=_as(user))


# B10: an opened file has its SAP base; the version-marker read is not a
# source read.
OPENED = {**UNCHECKED, "base_status": "sap"}


def _reads(fake) -> list:
    return [c for c in fake.calls if c[1].get("type") != "VERSIONS"]


async def test_open_class_main_line_exact(client, fake):
    sid = await _session()
    fid = await _finding(sid, program=CALC_CP, include=CALC_CP, line=12)
    r = await _open(client, sid, fid)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "file": {"path": "src/CLAS/zcl_demo_calc.clas.abap", "state": "read",
                 "object_type": "CLAS", "object_name": "ZCL_DEMO_CALC", **OPENED},
        "line": 12, "hint": None,
    }
    assert _reads(fake) == [("SAPRead", {"type": "CLAS", "name": "ZCL_DEMO_CALC"})]
    # B10: the version marker is read too, through the same client.
    assert ("SAPRead", {"type": "VERSIONS", "objectType": "CLAS",
                        "name": "ZCL_DEMO_CALC"}) in fake.calls
    # The read belongs to the session: its type is the policy.
    assert fake.built == [{"target": "T1", "destination": "arc1-abap-readonly",
                           "policy": "diagnose"}]
    r = await client.get(f"/ide/api/sessions/{sid}/file",
                         params={"path": "src/CLAS/zcl_demo_calc.clas.abap"},
                         headers=_as("alice"))
    assert r.json()["origin_source"] == "* source of ZCL_DEMO_CALC"


async def test_open_method_include_gives_hint(client, fake):
    sid = await _session()
    fid = await _finding(sid, program=CALC_CP,
                         include=_pool("ZCL_DEMO_CALC", "CM001"), line=12)
    r = await _open(client, sid, fid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file"]["path"] == "src/CLAS/zcl_demo_calc.clas.abap"
    assert body["line"] is None
    assert body["hint"] == "method include CM001, line 12"
    assert _reads(fake) == [("SAPRead", {"type": "CLAS", "name": "ZCL_DEMO_CALC"})]


async def test_open_method_include_without_line(client, fake):
    sid = await _session()
    fid = await _finding(sid, program=CALC_CP,
                         include=_pool("ZCL_DEMO_CALC", "CM00A"))
    body = (await _open(client, sid, fid)).json()
    assert body["line"] is None and body["hint"] == "method include CM00A"


async def test_open_method_include_named_only_by_the_program(client, fake):
    """A dump can name the method include as its program and leave the
    include empty: the hint must still say where the error is."""
    sid = await _session()
    fid = await _finding(sid, program=_pool("ZCL_DEMO_CALC", "CM001"), line=12)
    r = await _open(client, sid, fid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file"]["path"] == "src/CLAS/zcl_demo_calc.clas.abap"
    assert body["line"] is None
    assert body["hint"] == "method include CM001, line 12"


async def test_open_local_implementations_section(client, fake):
    sid = await _session()
    fid = await _finding(sid, program=CALC_CP,
                         include=_pool("ZCL_DEMO_CALC", "CCIMP"), line=7)
    r = await _open(client, sid, fid)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "file": {"path": "src/CLAS/zcl_demo_calc.clas.implementations.abap",
                 "state": "read", "object_type": "CLAS",
                 "object_name": "ZCL_DEMO_CALC", **OPENED},
        "line": 7, "hint": None,
    }
    assert _reads(fake) == [("SAPRead", {"type": "CLAS", "name": "ZCL_DEMO_CALC",
                                      "include": "implementations"})]


async def test_open_program_and_include(client, fake):
    sid = await _session()
    prog = await _finding(sid, ref_id="P", program="ZDEMO_REPORT",
                          include="ZDEMO_REPORT", line=4)
    incl = await _finding(sid, ref_id="I", program="SAPLZDEMO_FG",
                          include="LZDEMO_FGU01", line=9)
    r = await _open(client, sid, prog)
    assert r.json() == {
        "file": {"path": "src/PROG/zdemo_report.prog.abap", "state": "read",
                 "object_type": "PROG", "object_name": "ZDEMO_REPORT", **OPENED},
        "line": 4, "hint": None,
    }
    r = await _open(client, sid, incl)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "file": {"path": "src/INCL/lzdemo_fgu01.prog.abap", "state": "read",
                 "object_type": "INCL", "object_name": "LZDEMO_FGU01", **OPENED},
        "line": 9, "hint": None,
    }
    assert _reads(fake) == [
        ("SAPRead", {"type": "PROG", "name": "ZDEMO_REPORT"}),
        ("SAPRead", {"type": "INCL", "name": "LZDEMO_FGU01"}),
    ]
    # An include file can be refreshed like any other object file.
    r = await client.post(f"/ide/api/sessions/{sid}/file/refresh",
                          params={"path": "src/INCL/lzdemo_fgu01.prog.abap"},
                          headers=_as("alice"))
    assert r.status_code == 200, r.text
    assert _reads(fake)[-1] == ("SAPRead", {"type": "INCL", "name": "LZDEMO_FGU01"})


@pytest.mark.parametrize("fields", [
    {},  # no program
    {"program": "SAPLZDEMO_FG"},  # a function pool has no source of its own
    {"program": "ZIF_DEMO======================IP"},
    {"program": "Z REPORT"},
    {"program": "ZREPORT", "include": "<SYSINI>"},
])
async def test_open_unresolvable_422(client, fake, fields):
    sid = await _session()
    fid = await _finding(sid, **fields)
    before = await _counts()
    r = await _open(client, sid, fid)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "no_source"
    assert fake.calls == []
    assert await _counts() == before


async def test_open_while_running_is_409(client, fake):
    sid = await _session()
    fid = await _finding(sid, program="ZDEMO_REPORT")
    async with SessionLocal() as db:
        row = await db.get(IdeSession, sid)
        row.status = "running"
        await db.commit()
    r = await _open(client, sid, fid)
    assert r.status_code == 409 and r.json()["code"] == "run_in_progress"
    assert fake.calls == []


async def test_open_without_user_token_is_424(client, wire):
    sid = await _session()
    fid = await _finding(sid, program="ZDEMO_REPORT")
    r = await client.post(_url(sid, fid, "/open"),
                          headers=_as("alice", jwt=False))
    assert r.status_code == 424 and r.json()["code"] == "user_token_required"
    assert wire["sent"] == []
    assert (await _counts())["ide_workspace_files"] == 0


async def test_open_arc1_error_is_502_and_stores_nothing(client, fake):
    sid = await _session()
    fid = await _finding(sid, program="ZDEMO_REPORT")
    fake.raise_exc = arc1.arc1_error_from_text("SAPRead", "T1", ARC1_ERROR)
    r = await _open(client, sid, fid)
    assert r.status_code == 502, r.text
    assert r.json()["code"] == "sap_authentication_failed"
    assert (await _counts())["ide_workspace_files"] == 0


async def test_open_is_refused_on_a_target_that_lost_the_flag(client, wire):
    sid = await _session()
    fid = await _finding(sid, program="ZDEMO_REPORT", line=2)
    await _flag(False)
    r = await _open(client, sid, fid)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "target_not_non_production"
    assert wire["built"] == [] and wire["sent"] == []
    assert (await _counts())["ide_workspace_files"] == 0


# --- paths -------------------------------------------------------------------


@pytest.mark.parametrize("program,include,expected", [
    # class pools
    (CALC_CP, CALC_CP, ("CLAS", "ZCL_DEMO_CALC", None, True)),
    (CALC_CP, None, ("CLAS", "ZCL_DEMO_CALC", None, True)),
    (CALC_CP, "", ("CLAS", "ZCL_DEMO_CALC", None, True)),
    (CALC_CP, _pool("ZCL_DEMO_CALC", "CM001"), ("CLAS", "ZCL_DEMO_CALC", None, False)),
    (CALC_CP, _pool("ZCL_DEMO_CALC", "CM00Z"), ("CLAS", "ZCL_DEMO_CALC", None, False)),
    (CALC_CP, _pool("ZCL_DEMO_CALC", "CU"), ("CLAS", "ZCL_DEMO_CALC", None, False)),
    (CALC_CP, _pool("ZCL_DEMO_CALC", "CO"), ("CLAS", "ZCL_DEMO_CALC", None, False)),
    (CALC_CP, _pool("ZCL_DEMO_CALC", "CI"), ("CLAS", "ZCL_DEMO_CALC", None, False)),
    (CALC_CP, _pool("ZCL_DEMO_CALC", "CCIMP"),
     ("CLAS", "ZCL_DEMO_CALC", "implementations", True)),
    (CALC_CP, _pool("ZCL_DEMO_CALC", "CCDEF"),
     ("CLAS", "ZCL_DEMO_CALC", "definitions", True)),
    (CALC_CP, _pool("ZCL_DEMO_CALC", "CCMAC"),
     ("CLAS", "ZCL_DEMO_CALC", "macros", True)),
    (CALC_CP, _pool("ZCL_DEMO_CALC", "CCAU"),
     ("CLAS", "ZCL_DEMO_CALC", "testclasses", True)),
    # short padding, lower case, a namespace, a 30-character class name
    ("zcl_x=CP", "zcl_x=CM001", ("CLAS", "ZCL_X", None, False)),
    (_pool("/ABC/CL_X", "CP"), _pool("/ABC/CL_X", "CCAU"),
     ("CLAS", "/ABC/CL_X", "testclasses", True)),
    ("ZCL_" + "A" * 26 + "CP", "ZCL_" + "A" * 26 + "CM001",
     ("CLAS", "ZCL_" + "A" * 26, None, False)),
    # programs and includes
    ("ZDEMO_REPORT", "ZDEMO_REPORT", ("PROG", "ZDEMO_REPORT", None, True)),
    ("ZDEMO_REPORT", None, ("PROG", "ZDEMO_REPORT", None, True)),
    ("ZREPORT_CP", "ZREPORT_CP", ("PROG", "ZREPORT_CP", None, True)),
    ("ZDEMO_REPORT", "ZDEMO_REPORT_F01", ("INCL", "ZDEMO_REPORT_F01", None, True)),
    ("SAPLZDEMO_FG", "LZDEMO_FGU01", ("INCL", "LZDEMO_FGU01", None, True)),
    # nothing to open
    ("SAPLZDEMO_FG", "SAPLZDEMO_FG", None),
    ("SAPLZDEMO_FG", None, None),
    (None, None, None),
    ("", "ZDEMO_REPORT_F01", None),
    ("ZIF_DEMO======================IP", None, None),
    ("Z REPORT", None, None),
    ("ZREPORT", "<SYSINI>", None),
    ("ZREPORT", "../etc", None),
    ("Z" * 41, None, None),
    (12, None, None),
    ("ZREPORT", 12, None),
    ("=====CP", None, None),
])
def test_resolve_include_table(program, include, expected):
    assert resolve_include(program, include) == expected


def test_include_files_round_trip():
    assert path_for("INCL", "LZDEMO_FGU01") == "src/INCL/lzdemo_fgu01.prog.abap"
    assert object_for("src/INCL/lzdemo_fgu01.prog.abap") == ("INCL", "LZDEMO_FGU01", None)
    # The type comes from the directory: the same file name under PROG is a program.
    assert object_for("src/PROG/lzdemo_fgu01.prog.abap") == ("PROG", "LZDEMO_FGU01", None)
