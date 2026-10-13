"""IDE workspace files, object open/search and lint over read-only ARC-1.

The routes call ARC-1 as the signed-in user through ``agents.ide.arc1``.
This suite pins:

* ``open`` stores a ``read`` file at the abapGit path, from SAPRead with an
  explicit ``type``; ``refresh`` re-reads the origin and keeps the proposal;
* ``lint`` runs SAPLint ``lint`` on the proposal and stores parsed findings;
* ``search`` sends only ``query``/``maxResults``;
* ``Arc1Client`` refuses a data-preview SAPRead (403) before building any
  connection, refuses without a user token (424 ``user_token_required``),
  and maps a ``DestinationUserRequired`` -- bare or inside an
  ``ExceptionGroup`` -- to the same 424;
* an ARC-1 error payload (live ``SAP_AUTHENTICATION_FAILED`` shape), as an
  MCP ``isError`` result or as plain text, is a 502 with the payload's code
  on every route and is never stored as source or parsed as an empty
  result;
* sources over 1 MB are refused (413), a scratch file cannot be linted;
* the real ``PerRunMCPServer``: each direct call on a ``for_run`` copy opens
  and closes its own MCP session (mocked transport);
* path traversal is 422, another user's session is 404.

No network: a fake client records the calls.

Run:  python -m pytest tests/test_ide_files.py -q
"""

from __future__ import annotations

import json
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
from pydantic_ai.exceptions import ModelRetry  # noqa: E402

from agents.auth import current_jwt, require_developer  # noqa: E402
from agents.db import SessionLocal, init_db  # noqa: E402
from agents.destination_auth import DestinationUserRequired  # noqa: E402
from agents.ide import arc1  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeArtifact,
    IdeConventions,
    IdeMessage,
    IdeSession,
    IdeWorkspaceFile,
)
from agents.ide.routes import router as ide_router  # noqa: E402
from agents.ide.store import create_session, upsert_conventions  # noqa: E402

USERS = {
    "alice": {"user_name": "alice", "scope": ["developer"]},
    "bob": {"user_name": "bob", "scope": ["developer"]},
}

CANNED_LINT = json.dumps([
    {"rule": "keyword_case", "message": "Keyword should be upper case",
     "line": 3, "column": 5, "severity": "Error"},
    {"key": "line_length", "description": "Line too long",
     "start": {"row": 7, "col": 1}, "severity": "Warning"},
    # live abaplint shape (SAPLint lint, captured 2026-10-03)
    {"rule": "obsolete_statement", "message": "Statement \"MOVE\" is obsolete",
     "line": 8, "column": 5, "endLine": 8, "endColumn": 20, "severity": "warning"},
])

# Live ARC-1 error payload (SAPSearch, captured 2026-10-03), generic target.
LIVE_ERROR = json.dumps({
    "error": "SAP_AUTHENTICATION_FAILED",
    "message": "SAP authentication failed for target T1/100 after principal "
               "propagation.",
    "target": "T1/100", "identity": "per-user", "requestId": "REQ-186",
    "retryable": True,
})

CANNED_SEARCH = json.dumps([
    {"objectType": "CLAS/OC", "objectName": "ZCL_X", "packageName": "ZPKG",
     "description": "Demo class", "uri": "/sap/bc/adt/oo/classes/zcl_x"},
])


class FakeArc1:
    """Records calls; answers from a per-tool queue or a default."""

    def __init__(self):
        self.calls: list[tuple[str, dict, str, str]] = []
        self.answers: dict[str, list[str]] = {}
        self.raise_exc: Exception | None = None

    def factory(self, target: str, destination: str = "", **_kwargs):
        fake = self

        class _Client:
            async def call(self, tool, args):
                fake.calls.append((tool, dict(args), target, destination))
                if fake.raise_exc is not None:
                    raise fake.raise_exc
                queue = fake.answers.get(tool) or []
                return queue.pop(0) if queue else f"* source of {args.get('name')}"

        return _Client()


# A file opened by the route has no revision and nothing checked yet.
UNCHECKED = {"revision": 0, "base_status": None, "syntax_status": None}
# B10: ... but its SAP base is known (``base_status="sap"``).
OPENED = {**UNCHECKED, "base_status": "sap"}
VERSIONS_CALL = ("SAPRead", {"type": "VERSIONS", "objectType": "CLAS",
                             "name": "ZCL_X"}, "T1", "arc1-abap-readonly")


def _reads(fake) -> list:
    """The source reads, without the version-marker reads."""
    return [c for c in fake.calls if c[1].get("type") != "VERSIONS"]


def _fake_developer(request: Request) -> dict:
    user = request.headers.get("x-test-user", "")
    if user not in USERS:
        raise HTTPException(status_code=403, detail="Developer scope required")
    return USERS[user]


@pytest.fixture(autouse=True)
async def _clean_db():
    await init_db()
    async with SessionLocal() as db:
        for model in (IdeWorkspaceFile, IdeArtifact, IdeMessage, IdeSession,
                      IdeConventions):
            await db.execute(model.__table__.delete())
        await db.commit()
        await upsert_conventions(db, "T1", label="Target one",
                                 destination="arc1-abap-readonly")
    arc1._SERVERS.clear()
    yield
    arc1._SERVERS.clear()


@pytest.fixture
def fake(monkeypatch):
    f = FakeArc1()
    monkeypatch.setattr(arc1, "get_arc1_client", f.factory)
    return f


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(ide_router)
    app.dependency_overrides[require_developer] = _fake_developer
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


@pytest.fixture
async def sid():
    async with SessionLocal() as db:
        row = await create_session(db, owner="alice", title="S", target="T1")
        return row.id


def _as(user: str) -> dict:
    return {"x-test-user": user}


PATH = "src/CLAS/zcl_x.clas.abap"


async def _open(client, sid, type="CLAS", name="ZCL_X", user="alice"):
    return await client.post(f"/ide/api/sessions/{sid}/open",
                             json={"type": type, "name": name}, headers=_as(user))


# --- open / files / file -----------------------------------------------------


async def test_open_stores_read_file_at_abapgit_path(client, fake, sid):
    r = await _open(client, sid, name="zcl_x")
    assert r.status_code == 200, r.text
    assert r.json() == {"path": PATH, "state": "read",
                        "object_type": "CLAS", "object_name": "ZCL_X", **OPENED}
    assert fake.calls == [("SAPRead", {"type": "CLAS", "name": "ZCL_X"},
                           "T1", "arc1-abap-readonly"), VERSIONS_CALL]

    r = await client.get(f"/ide/api/sessions/{sid}/files", headers=_as("alice"))
    assert r.status_code == 200
    assert r.json() == [{"path": PATH, "state": "read",
                         "object_type": "CLAS", "object_name": "ZCL_X", **OPENED}]

    r = await client.get(f"/ide/api/sessions/{sid}/file", params={"path": PATH},
                         headers=_as("alice"))
    assert r.status_code == 200
    assert r.json() == {"path": PATH, "state": "read",
                        "object_type": "CLAS", "object_name": "ZCL_X",
                        # the fake's VERSIONS answer is not a revision list
                        **OPENED, "origin_version": None, "syntax": [],
                        "origin_source": "* source of ZCL_X",
                        "proposed_source": None, "lint": []}


async def test_open_and_refresh_store_the_version_marker(client, fake, sid):
    """B10: the user-side open and refresh set the base as the agent's
    ``open_object`` does: source, version marker, ``base_status="sap"`` and
    when it was checked."""
    versions = json.dumps([{"id": "rev-7", "date": "2026-10-01T00:00:00Z"}])
    fake.answers["SAPRead"] = ["origin v1", versions]
    r = await _open(client, sid)
    assert r.status_code == 200, r.text
    r = await client.get(f"/ide/api/sessions/{sid}/file", params={"path": PATH},
                         headers=_as("alice"))
    assert (r.json()["origin_version"], r.json()["base_status"]) == ("rev-7", "sap")
    async with SessionLocal() as db:
        row = (await db.execute(IdeWorkspaceFile.__table__.select())).first()
    assert row.base_checked_at is not None
    fake.answers["SAPRead"] = ["origin v2", json.dumps([{"id": "rev-8"}])]
    r = await client.post(f"/ide/api/sessions/{sid}/file/refresh",
                          params={"path": PATH}, headers=_as("alice"))
    assert (r.json()["origin_source"], r.json()["origin_version"]) == (
        "origin v2", "rev-8")


async def test_refresh_gives_a_new_file_its_base(client, fake, sid):
    """A file a run created (``new``, no base) that exists in SAP becomes
    ``modified`` on refresh."""
    async with SessionLocal() as db:
        db.add(IdeWorkspaceFile(session_id=sid, path=PATH, object_type="CLAS",
                                object_name="ZCL_X", state="new",
                                proposed_source="mine", revision=1))
        await db.commit()
    r = await client.post(f"/ide/api/sessions/{sid}/file/refresh",
                          params={"path": PATH}, headers=_as("alice"))
    assert r.status_code == 200, r.text
    assert (r.json()["state"], r.json()["base_status"],
            r.json()["proposed_source"]) == ("modified", "sap", "mine")


async def test_open_again_keeps_a_proposal(client, fake, sid):
    await _open(client, sid)
    async with SessionLocal() as db:
        f = (await db.execute(IdeWorkspaceFile.__table__.select())).first()
        await db.execute(IdeWorkspaceFile.__table__.update().values(
            proposed_source="new", state="modified"))
        await db.commit()
    assert f is not None
    fake.answers["SAPRead"] = ["fresh origin"]
    r = await _open(client, sid)
    assert r.json()["state"] == "modified"
    r = await client.get(f"/ide/api/sessions/{sid}/file", params={"path": PATH},
                         headers=_as("alice"))
    assert r.json()["origin_source"] == "fresh origin"
    assert r.json()["proposed_source"] == "new"


async def test_open_unsupported_type_is_422_and_not_called(client, fake, sid):
    r = await _open(client, sid, type="TABLE_QUERY", name="MARA")
    assert r.status_code == 422
    r = await _open(client, sid, type="CLAS", name="  ")
    assert r.status_code == 422
    assert fake.calls == []


async def test_get_unknown_file_is_404(client, fake, sid):
    r = await client.get(f"/ide/api/sessions/{sid}/file", params={"path": PATH},
                         headers=_as("alice"))
    assert r.status_code == 404


# --- refresh ------------------------------------------------------------------


async def test_refresh_updates_origin_and_keeps_proposal(client, fake, sid):
    await _open(client, sid)
    async with SessionLocal() as db:
        await db.execute(IdeWorkspaceFile.__table__.update().values(
            proposed_source="proposal", state="modified"))
        await db.commit()
    fake.answers["SAPRead"] = ["origin v2"]
    r = await client.post(f"/ide/api/sessions/{sid}/file/refresh",
                          params={"path": PATH}, headers=_as("alice"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["origin_source"] == "origin v2"
    assert body["proposed_source"] == "proposal"
    assert body["state"] == "modified"
    assert _reads(fake)[-1][:2] == ("SAPRead", {"type": "CLAS", "name": "ZCL_X"})
    assert fake.calls[-1] == VERSIONS_CALL


async def test_refresh_reads_class_include(client, fake, sid):
    path = "src/CLAS/zcl_x.clas.testclasses.abap"
    async with SessionLocal() as db:
        db.add(IdeWorkspaceFile(session_id=sid, path=path, object_type="CLAS",
                                object_name="ZCL_X", state="read"))
        await db.commit()
    r = await client.post(f"/ide/api/sessions/{sid}/file/refresh",
                          params={"path": path}, headers=_as("alice"))
    assert r.status_code == 200, r.text
    assert _reads(fake)[-1][1] == {"type": "CLAS", "name": "ZCL_X",
                                   "include": "testclasses"}


async def test_refresh_scratch_file_is_422(client, fake, sid):
    async with SessionLocal() as db:
        db.add(IdeWorkspaceFile(session_id=sid, path="notes/impact.md",
                                state="new", proposed_source="x"))
        await db.commit()
    r = await client.post(f"/ide/api/sessions/{sid}/file/refresh",
                          params={"path": "notes/impact.md"}, headers=_as("alice"))
    assert r.status_code == 422
    assert fake.calls == []


# --- lint ---------------------------------------------------------------------


async def test_lint_returns_parsed_findings_and_stores_them(client, fake, sid):
    await _open(client, sid)
    async with SessionLocal() as db:
        await db.execute(IdeWorkspaceFile.__table__.update().values(
            proposed_source="proposed code", state="modified"))
        await db.commit()
    fake.answers["SAPLint"] = [CANNED_LINT]
    r = await client.post(f"/ide/api/sessions/{sid}/file/lint",
                          params={"path": PATH}, headers=_as("alice"))
    assert r.status_code == 200, r.text
    expected = [
        {"line": 3, "column": 5, "severity": "error",
         "message": "Keyword should be upper case", "rule": "keyword_case"},
        {"line": 7, "column": 1, "severity": "warning",
         "message": "Line too long", "rule": "line_length"},
        {"line": 8, "column": 5, "severity": "warning",
         "message": 'Statement "MOVE" is obsolete', "rule": "obsolete_statement"},
    ]
    assert r.json() == expected
    tool, args, *_ = fake.calls[-1]
    assert tool == "SAPLint"
    assert args["action"] == "lint" and args["source"] == "proposed code"

    r = await client.get(f"/ide/api/sessions/{sid}/file", params={"path": PATH},
                         headers=_as("alice"))
    assert r.json()["lint"] == expected


async def test_lint_falls_back_to_origin(client, fake, sid):
    await _open(client, sid)
    fake.answers["SAPLint"] = ["[]"]
    r = await client.post(f"/ide/api/sessions/{sid}/file/lint",
                          params={"path": PATH}, headers=_as("alice"))
    assert r.status_code == 200
    assert r.json() == []
    assert fake.calls[-1][1]["source"] == "* source of ZCL_X"


def test_parse_findings_tolerates_other_shapes():
    assert arc1.parse_findings("not json") == []
    out = arc1.parse_findings(json.dumps(
        {"errors": [{"message": "m", "line": 1, "column": 2, "rule": "r"}],
         "warnings": [{"message": "w", "line": 4}]}))
    assert [(f["severity"], f["line"]) for f in out] == [("error", 1), ("warning", 4)]


# --- search -------------------------------------------------------------------


async def test_search_passes_only_query_and_max_results(client, fake, sid):
    fake.answers["SAPSearch"] = [CANNED_SEARCH]
    r = await client.get("/ide/api/objects/search",
                         params={"target": "T1", "q": "ZCL_*"}, headers=_as("alice"))
    assert r.status_code == 200, r.text
    assert r.json() == [{"type": "CLAS/OC", "name": "ZCL_X", "package": "ZPKG",
                         "description": "Demo class"}]
    assert fake.calls == [("SAPSearch", {"query": "ZCL_*", "maxResults": 50},
                           "T1", "arc1-abap-readonly")]


async def test_search_unknown_target_is_422(client, fake):
    r = await client.get("/ide/api/objects/search",
                         params={"target": "NOPE", "q": "Z*"}, headers=_as("alice"))
    assert r.status_code == 422
    assert fake.calls == []


# --- errors surfaced by the client ----------------------------------------------


async def test_route_maps_user_required_to_424(client, fake, sid):
    fake.raise_exc = arc1.Arc1UserRequired()
    r = await _open(client, sid)
    assert r.status_code == 424
    assert r.json()["code"] == "user_token_required"


async def test_route_maps_refusal_to_403(client, fake, sid):
    fake.raise_exc = arc1.Arc1Refused("nope")
    r = await _open(client, sid)
    assert r.status_code == 403
    assert r.json()["code"] == "readonly_refused"


# --- traversal and ownership ----------------------------------------------------


@pytest.mark.parametrize("bad", ["../etc/passwd", "/abs/path", "src/../../x", ""])
async def test_path_traversal_is_422(client, fake, sid, bad):
    for method, url in (("GET", "file"), ("POST", "file/refresh"),
                        ("POST", "file/lint")):
        r = await client.request(method, f"/ide/api/sessions/{sid}/{url}",
                                 params={"path": bad}, headers=_as("alice"))
        assert r.status_code == 422, (url, bad, r.text)
    assert fake.calls == []


async def test_bob_gets_404_everywhere(client, fake, sid):
    await _open(client, sid)
    fake.calls.clear()
    h = _as("bob")
    assert (await client.get(f"/ide/api/sessions/{sid}/files", headers=h)).status_code == 404
    for method, url in (("GET", "file"), ("POST", "file/refresh"),
                        ("POST", "file/lint")):
        r = await client.request(method, f"/ide/api/sessions/{sid}/{url}",
                                 params={"path": PATH}, headers=h)
        assert r.status_code == 404, url
    r = await _open(client, sid, user="bob")
    assert r.status_code == 404
    assert fake.calls == []


# --- Arc1Client itself ----------------------------------------------------------


class _FakeRun:
    def __init__(self, exc=None, result="ok"):
        self.exc, self.result, self.calls = exc, result, []

    async def direct_call_tool(self, name, args):
        self.calls.append((name, args))
        if self.exc is not None:
            raise self.exc
        return self.result


class _FakeServer:
    def __init__(self, run):
        self.run = run

    async def for_run(self, ctx):
        return self.run


@pytest.fixture
def built(monkeypatch):
    """Captures create_mcp_server calls; returns a server around ``_FakeRun``."""
    state = {"calls": [], "run": _FakeRun()}

    def _create(name, base_url, auth_mode="jwt", tool_prefix=None, oauth=None, **kw):
        state["calls"].append((name, base_url, auth_mode, oauth))
        return _FakeServer(state["run"])

    monkeypatch.setattr(arc1.shared, "create_mcp_server", _create)
    return state


@pytest.fixture
def jwt():
    token = current_jwt.set("user.jwt.token")
    yield
    current_jwt.reset(token)


async def test_client_refuses_table_query_before_building(built, jwt):
    client = arc1.Arc1Client("T1", "arc1-abap-readonly")
    with pytest.raises(HTTPException) as exc:
        await client.call("SAPRead", {"type": "TABLE_QUERY", "name": "MARA"})
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        await client.call("SAPWrite", {"type": "CLAS", "name": "ZCL_X"})
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        await client.call("SAPRead", {"name": "ZCL_X"})  # type is required
    assert exc.value.status_code == 403
    assert built["calls"] == [] and built["run"].calls == []


async def test_client_uses_destination_as_user(built, jwt, monkeypatch):
    monkeypatch.delenv("IDE_ARC1_URL_T1", raising=False)
    client = arc1.Arc1Client("T1", "arc1-abap-readonly")
    assert await client.call("SAPRead", {"type": "CLAS", "name": "ZCL_X"}) == "ok"
    name, url, mode, oauth = built["calls"][0]
    assert name == "ide-T1" and mode == "destination"
    assert url.endswith("/mcp")
    assert oauth == {"destination": "arc1-abap-readonly", "user_context": True}
    # cached per target
    await client.call("SAPRead", {"type": "CLAS", "name": "ZCL_Y"})
    assert len(built["calls"]) == 1


async def test_client_without_jwt_is_424_and_not_built(built):
    assert current_jwt.get() in (None, "")
    client = arc1.Arc1Client("T1", "arc1-abap-readonly")
    with pytest.raises(arc1.Arc1UserRequired) as exc:
        await client.call("SAPRead", {"type": "CLAS", "name": "ZCL_X"})
    assert exc.value.status_code == 424
    assert exc.value.code == "user_token_required"
    assert built["calls"] == []


@pytest.mark.parametrize("wrap", [False, True])
async def test_destination_user_required_maps_to_424(built, jwt, wrap):
    err: Exception = DestinationUserRequired("https://destination.invalid/mcp",
                                             "arc1-abap-readonly")
    if wrap:
        err = ExceptionGroup("task group", [ExceptionGroup("inner", [err])])
    built["run"].exc = err
    client = arc1.Arc1Client("T1", "arc1-abap-readonly")
    with pytest.raises(arc1.Arc1UserRequired) as exc:
        await client.call("SAPRead", {"type": "CLAS", "name": "ZCL_X"})
    assert exc.value.status_code == 424


async def test_tool_error_maps_to_502(built, jwt):
    built["run"].exc = ModelRetry("object not found")
    client = arc1.Arc1Client("T1", "arc1-abap-readonly")
    with pytest.raises(arc1.Arc1Error) as exc:
        await client.call("SAPRead", {"type": "CLAS", "name": "ZCL_NOPE"})
    assert exc.value.status_code == 502
    assert "object not found" in exc.value.detail


async def test_local_fallback_jwt_mode(built, monkeypatch):
    client = arc1.Arc1Client("t-1", "")
    monkeypatch.delenv("IDE_ARC1_URL_T_1", raising=False)
    with pytest.raises(arc1.Arc1Error) as exc:
        await client.call("SAPRead", {"type": "CLAS", "name": "ZCL_X"})
    assert exc.value.status_code == 424
    monkeypatch.setenv("IDE_ARC1_URL_T_1", "http://localhost:3000/mcp")
    await client.call("SAPRead", {"type": "CLAS", "name": "ZCL_X"})
    assert built["calls"] == [("ide-t-1", "http://localhost:3000/mcp", "jwt", None)]


# --- ARC-1 error payloads through the real client ---------------------------------


@pytest.fixture
def real_client(monkeypatch, built, jwt):
    """Routes use the real Arc1Client (fake MCP run underneath)."""
    monkeypatch.setattr(arc1, "get_arc1_client", arc1.Arc1Client)
    return built


def _error_as(built, how: str) -> None:
    if how == "isError":
        # pydantic-ai raises ModelRetry with the text of an isError result.
        built["run"].exc = ModelRetry(LIVE_ERROR)
    else:
        built["run"].result = LIVE_ERROR


def _assert_live_error(r) -> None:
    assert r.status_code == 502, r.text
    body = r.json()
    assert body["code"] == "sap_authentication_failed"
    assert body["request_id"] == "REQ-186" and body["retryable"] is True
    assert "T1/100" in body["detail"] and "REQ-186" in body["detail"]
    assert "principal propagation" in body["detail"]


@pytest.mark.parametrize("how", ["isError", "plain"])
async def test_error_payload_on_open_stores_nothing(client, real_client, sid, how):
    _error_as(real_client, how)
    _assert_live_error(await _open(client, sid))
    async with SessionLocal() as db:
        rows = (await db.execute(IdeWorkspaceFile.__table__.select())).all()
    assert rows == []


@pytest.mark.parametrize("how", ["isError", "plain"])
async def test_error_payload_on_refresh_keeps_origin(client, real_client, sid, how):
    real_client["run"].result = "good origin"
    assert (await _open(client, sid)).status_code == 200
    _error_as(real_client, how)
    r = await client.post(f"/ide/api/sessions/{sid}/file/refresh",
                          params={"path": PATH}, headers=_as("alice"))
    _assert_live_error(r)
    r = await client.get(f"/ide/api/sessions/{sid}/file", params={"path": PATH},
                         headers=_as("alice"))
    assert r.json()["origin_source"] == "good origin"


@pytest.mark.parametrize("how", ["isError", "plain"])
async def test_error_payload_on_lint_is_not_empty_result(client, real_client, sid, how):
    real_client["run"].result = "good origin"
    await _open(client, sid)
    async with SessionLocal() as db:
        await db.execute(IdeWorkspaceFile.__table__.update().values(
            lint_json=json.dumps([{"line": 1, "column": 1, "severity": "error",
                                   "message": "old", "rule": "r"}])))
        await db.commit()
    _error_as(real_client, how)
    r = await client.post(f"/ide/api/sessions/{sid}/file/lint",
                          params={"path": PATH}, headers=_as("alice"))
    _assert_live_error(r)
    r = await client.get(f"/ide/api/sessions/{sid}/file", params={"path": PATH},
                         headers=_as("alice"))
    assert [f["message"] for f in r.json()["lint"]] == ["old"]


@pytest.mark.parametrize("how", ["isError", "plain"])
async def test_error_payload_on_search_is_not_empty_result(client, real_client, how):
    _error_as(real_client, how)
    r = await client.get("/ide/api/objects/search",
                         params={"target": "T1", "q": "ZCL_*"}, headers=_as("alice"))
    _assert_live_error(r)


def test_source_mentioning_error_is_not_a_payload():
    assert arc1.arc1_error_from_text("SAPRead", "T1", '{"error": "x"}') is None
    assert arc1.arc1_error_from_text("SAPRead", "T1", "CLASS zcl_error ...") is None
    assert arc1.arc1_error_from_text("SAPRead", "T1", "[1, 2]") is None
    err = arc1.arc1_error_from_text("SAPRead", "T1", '{"error": "Weird Code!", "retryable": false}')
    assert err is not None and err.code == "weird_code" and err.status_code == 502


# --- size cap and scratch lint ------------------------------------------------------


async def test_open_and_refresh_refuse_source_over_1mb(client, fake, sid):
    big = "x" * (1024 * 1024 + 1)
    fake.answers["SAPRead"] = [big]
    r = await _open(client, sid)
    assert r.status_code == 413 and r.json()["code"] == "source_too_large"
    async with SessionLocal() as db:
        assert (await db.execute(IdeWorkspaceFile.__table__.select())).all() == []
    await _open(client, sid)  # default small source
    fake.answers["SAPRead"] = [big]
    r = await client.post(f"/ide/api/sessions/{sid}/file/refresh",
                          params={"path": PATH}, headers=_as("alice"))
    assert r.status_code == 413
    r = await client.get(f"/ide/api/sessions/{sid}/file", params={"path": PATH},
                         headers=_as("alice"))
    assert r.json()["origin_source"] == "* source of ZCL_X"


async def test_lint_scratch_file_is_422(client, fake, sid):
    async with SessionLocal() as db:
        db.add(IdeWorkspaceFile(session_id=sid, path="notes/impact.md",
                                state="new", proposed_source="# notes"))
        await db.commit()
    r = await client.post(f"/ide/api/sessions/{sid}/file/lint",
                          params={"path": "notes/impact.md"}, headers=_as("alice"))
    assert r.status_code == 422
    assert fake.calls == []


# --- the real PerRunMCPServer: one MCP session per direct call ---------------------


class _McpStub:
    """Minimal streamable-HTTP MCP server answering with JSON (no SSE)."""

    def __init__(self):
        self.log: list[tuple[str, str | None, str | None]] = []
        self.sessions = 0

    def handler(self, request):
        import httpx

        sid = request.headers.get("mcp-session-id")
        if request.method == "DELETE":
            self.log.append(("DELETE", None, sid))
            return httpx.Response(200)
        if request.method == "GET":
            return httpx.Response(405)
        msg = json.loads(request.content)
        method = msg.get("method")
        self.log.append(("POST", method, sid))
        if "id" not in msg:  # notification
            return httpx.Response(202)
        if method == "initialize":
            self.sessions += 1
            result = {
                "protocolVersion": msg["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "stub", "version": "1"},
            }
            headers = {"mcp-session-id": f"s{self.sessions}"}
        elif method == "tools/call":
            result = {"content": [{"type": "text",
                                   "text": f"src {msg['params']['arguments']['name']}"}],
                      "isError": False}
            headers = {}
        else:
            result, headers = {}, {}
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": msg["id"], "result": result},
            headers={"content-type": "application/json", **headers},
        )


async def test_real_per_run_server_opens_and_closes_a_session_per_call(monkeypatch, jwt):
    import httpx

    from agents.shared import PerRunMCPServer

    stub = _McpStub()
    server = PerRunMCPServer(
        url="https://arc1.example/mcp",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(stub.handler)),
    )
    runs: list = []
    orig_for_run = PerRunMCPServer.for_run

    async def _spy_for_run(self, ctx):
        run = await orig_for_run(self, ctx)
        runs.append(run)
        return run

    monkeypatch.setattr(PerRunMCPServer, "for_run", _spy_for_run)
    monkeypatch.setattr(arc1.shared, "create_mcp_server",
                        lambda *a, **k: server)

    client = arc1.Arc1Client("T1", "arc1-abap-readonly")
    assert await client.call("SAPRead", {"type": "CLAS", "name": "ZCL_A"}) == "src ZCL_A"
    assert await client.call("SAPRead", {"type": "CLAS", "name": "ZCL_B"}) == "src ZCL_B"

    # Two calls, two copies, two MCP sessions; each closed after its call.
    assert len(runs) == 2 and runs[0] is not runs[1]
    assert all(r is not server for r in runs)
    assert stub.sessions == 2
    assert [m for m in stub.log if m[1] == "initialize"] == [
        ("POST", "initialize", None), ("POST", "initialize", None)]
    calls = [m for m in stub.log if m[1] == "tools/call"]
    assert [c[2] for c in calls] == ["s1", "s2"]
    assert not any(r.is_running for r in runs)
    assert not server.is_running  # the shared object was never entered



# --- final fix round: no diff-base change while a run holds the workspace (FIX-9)


async def _set_running(sid: str) -> None:
    async with SessionLocal() as db:
        await db.execute(IdeSession.__table__.update()
                         .where(IdeSession.__table__.c.id == sid)
                         .values(status="running", run_id="r1"))
        await db.commit()


async def test_open_while_running_is_409_and_not_called(client, fake, sid):
    await _set_running(sid)
    r = await _open(client, sid)
    assert r.status_code == 409
    assert r.json()["code"] == "run_in_progress"
    assert fake.calls == []


async def test_refresh_while_running_is_409_and_keeps_origin(client, fake, sid):
    await _open(client, sid)
    calls = len(fake.calls)
    await _set_running(sid)
    fake.answers["SAPRead"] = ["changed in SAP"]
    r = await client.post(f"/ide/api/sessions/{sid}/file/refresh",
                          params={"path": PATH}, headers=_as("alice"))
    assert r.status_code == 409
    assert r.json()["code"] == "run_in_progress"
    assert len(fake.calls) == calls
    r = await client.get(f"/ide/api/sessions/{sid}/file", params={"path": PATH},
                         headers=_as("alice"))
    assert r.json()["origin_source"] == "* source of ZCL_X"



# --- review minors: base writes outside a run vs. a propose approve ----------
#
# Open, refresh and a finding's "open source" change a file's ``state``
# (a proposal byte-equal to SAP's source is dropped) outside any run. A
# propose approve pins the proposed revisions under the session row lock,
# so these writers take the same lock -- after their ARC-1 read, which may
# take seconds -- and check the run lock again under it.


def _racing_factory(fake, sid, monkeypatch, *, start_run=False):
    """A fake ARC-1 whose source read starts a run meanwhile (or not), and
    a spy that records when the session row lock is taken."""
    from agents.ide import store as store_module

    events: list[str] = []
    real_lock = store_module.lock_session_row

    async def lock(db, session_id):
        events.append("lock")
        await real_lock(db, session_id)

    monkeypatch.setattr(store_module, "lock_session_row", lock)
    inner = fake.factory

    def factory(target, destination="", **kw):
        client = inner(target, destination, **kw)
        real_call = client.call

        async def call(tool, args):
            events.append(f"{tool}:{args.get('type')}")
            if start_run and tool == "SAPRead" and args.get("type") != "VERSIONS":
                await _set_running(sid)
            return await real_call(tool, args)

        client.call = call
        return client

    monkeypatch.setattr(arc1, "get_arc1_client", factory)
    return events


async def _proposal_equal_to_sap(sid):
    async with SessionLocal() as db:
        await db.execute(IdeWorkspaceFile.__table__.update().values(
            proposed_source="same as SAP", state="modified", revision=2))
        await db.commit()


@pytest.mark.parametrize("route", ["open", "refresh"])
async def test_base_write_takes_the_session_lock_after_the_read(
        client, fake, sid, monkeypatch, route):
    await _open(client, sid)
    await _proposal_equal_to_sap(sid)
    events = _racing_factory(fake, sid, monkeypatch)
    fake.answers["SAPRead"] = ["same as SAP"]
    if route == "open":
        r = await _open(client, sid)
    else:
        r = await client.post(f"/ide/api/sessions/{sid}/file/refresh",
                              params={"path": PATH}, headers=_as("alice"))
    assert r.status_code == 200, r.text
    assert "lock" in events
    assert events.index("lock") > events.index("SAPRead:CLAS")
    async with SessionLocal() as db:
        row = (await db.execute(IdeWorkspaceFile.__table__.select())).one()
    assert (row.state, row.proposed_source) == ("read", None)


@pytest.mark.parametrize("route", ["open", "refresh"])
async def test_run_started_during_the_read_is_409_and_nothing_written(
        client, fake, sid, monkeypatch, route):
    await _open(client, sid)
    await _proposal_equal_to_sap(sid)
    _racing_factory(fake, sid, monkeypatch, start_run=True)
    fake.answers["SAPRead"] = ["same as SAP"]
    if route == "open":
        r = await _open(client, sid)
    else:
        r = await client.post(f"/ide/api/sessions/{sid}/file/refresh",
                              params={"path": PATH}, headers=_as("alice"))
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "run_in_progress"
    async with SessionLocal() as db:
        row = (await db.execute(IdeWorkspaceFile.__table__.select())).one()
    assert (row.state, row.proposed_source, row.revision) == (
        "modified", "same as SAP", 2)
    assert row.origin_source == "* source of ZCL_X"


# --- review B-ide-3: a lint is not stored while a run holds the workspace ----
#
# A run rewrites the proposals; a lint stored meanwhile would describe a
# source the file no longer holds. Same rule as open/refresh: refused before
# the ARC-1 call, and checked again under the session row lock after it.


async def _stored_lint(sid):
    async with SessionLocal() as db:
        return (await db.execute(IdeWorkspaceFile.__table__.select())).one().lint_json


async def test_lint_while_running_is_409_and_not_called(client, fake, sid):
    await _open(client, sid)
    calls = len(fake.calls)
    before = await _stored_lint(sid)
    await _set_running(sid)
    fake.answers["SAPLint"] = [CANNED_LINT]
    r = await client.post(f"/ide/api/sessions/{sid}/file/lint",
                          params={"path": PATH}, headers=_as("alice"))
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "run_in_progress"
    assert len(fake.calls) == calls
    assert await _stored_lint(sid) == before


async def test_run_started_during_the_lint_is_409_and_nothing_stored(
        client, fake, sid, monkeypatch):
    await _open(client, sid)
    before = await _stored_lint(sid)
    inner = fake.factory

    def factory(target, destination="", **kw):
        client_ = inner(target, destination, **kw)
        real_call = client_.call

        async def call(tool, args):
            if tool == "SAPLint":
                await _set_running(sid)
            return await real_call(tool, args)

        client_.call = call
        return client_

    monkeypatch.setattr(arc1, "get_arc1_client", factory)
    fake.answers["SAPLint"] = [CANNED_LINT]
    r = await client.post(f"/ide/api/sessions/{sid}/file/lint",
                          params={"path": PATH}, headers=_as("alice"))
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "run_in_progress"
    assert await _stored_lint(sid) == before


# --- final fix round: refusals carry their own code on every route (FIX-10) ---


@pytest.mark.parametrize("route", ["open", "refresh", "lint", "search"])
async def test_every_direct_route_answers_readonly_refused(client, fake, sid, route):
    await _open(client, sid)
    async with SessionLocal() as db:
        await db.execute(IdeWorkspaceFile.__table__.update().values(
            proposed_source="x", state="modified"))
        await db.commit()
    fake.raise_exc = arc1.Arc1Refused("table contents are not readable")
    if route == "open":
        r = await _open(client, sid)
    elif route == "search":
        r = await client.get("/ide/api/objects/search", params={"target": "T1", "q": "Z"},
                             headers=_as("alice"))
    else:
        r = await client.post(f"/ide/api/sessions/{sid}/file/{route}",
                              params={"path": PATH}, headers=_as("alice"))
    assert r.status_code == 403
    assert r.json()["code"] == "readonly_refused"
    assert r.json()["detail"].startswith("Refused in the read-only IDE")



# --- final fix round: destination errors stay on the server (FIX-12) -----------


async def test_destination_error_text_is_not_sent_to_the_client(built, jwt, caplog):
    import logging

    from agents.destination import DestinationError

    built["run"].exc = DestinationError(
        "destination 'arc1-abap-readonly' lookup failed: 401 from "
        "https://tenant.dest.example/destination-configuration?client_secret=S3CRET"
    )
    client = arc1.Arc1Client("T1", "arc1-abap-readonly")
    caplog.set_level(logging.WARNING, logger="agents.ide.arc1")
    with pytest.raises(arc1.Arc1Error) as exc:
        await client.call("SAPRead", {"type": "CLAS", "name": "ZCL_X"})
    assert exc.value.status_code == 502
    assert exc.value.code == "destination_error"
    body = json.dumps(exc.value.body())
    assert "S3CRET" not in body and "tenant.dest.example" not in body
    assert "S3CRET" in caplog.text  # the detail is logged for the operator


def test_arc1_find_survives_a_cyclic_cause_chain():
    a = RuntimeError("a")
    group = ExceptionGroup("g", [a])
    a.__context__ = group  # a cycle through the group
    assert arc1._find(a, KeyError) is None
    assert arc1._find(group, RuntimeError) is a


# --- B10 fix round 1: a "found" source that is no source ------------------------


@pytest.mark.parametrize("answer", ["", "  \n", "Class ZCL_X does not exist"])
async def test_open_unusable_source_is_refused_and_nothing_stored(client, fake, sid,
                                                                  answer):
    fake.answers["SAPRead"] = [answer]
    r = await _open(client, sid)
    assert r.status_code == 502, r.text
    assert r.json()["code"] == "no_source"
    # No marker read for an object that was not found.
    assert [c[1].get("type") for c in fake.calls] == ["CLAS"]
    r = await client.get(f"/ide/api/sessions/{sid}/files", headers=_as("alice"))
    assert r.json() == []


async def test_refresh_unusable_source_keeps_the_base(client, fake, sid):
    assert (await _open(client, sid)).status_code == 200
    fake.answers["SAPRead"] = ["Object ZCL_X not found"]
    r = await client.post(f"/ide/api/sessions/{sid}/file/refresh",
                          params={"path": PATH}, headers=_as("alice"))
    assert r.status_code == 502
    assert r.json()["code"] == "no_source"
    r = await client.get(f"/ide/api/sessions/{sid}/file", params={"path": PATH},
                         headers=_as("alice"))
    assert r.json()["origin_source"] == "* source of ZCL_X"


def _during_sap_read(monkeypatch, fake, write) -> None:
    """Run ``write`` (an async callable) inside every source SAPRead: the
    ARC-1 read can take seconds, and a run can start and end meanwhile."""
    inner = fake.factory

    def factory(target, destination="", **kw):
        client_ = inner(target, destination, **kw)
        real_call = client_.call

        async def call(tool, args):
            if tool == "SAPRead" and args.get("type") != "VERSIONS":
                await write()
            return await real_call(tool, args)

        client_.call = call
        return client_

    monkeypatch.setattr(arc1, "get_arc1_client", factory)


async def _run_writes_revision_3() -> None:
    async with SessionLocal() as db:
        await db.execute(IdeWorkspaceFile.__table__.update().values(
            proposed_source="NEW proposal from the run", revision=3))
        await db.commit()


async def test_refresh_keeps_a_proposal_a_run_wrote_during_the_read(
    client, fake, sid, monkeypatch
):
    """The row is read again under the session lock: a stale identity-mapped
    copy (loaded before the ARC-1 read) would drop the run's new proposal."""
    await _open(client, sid)
    async with SessionLocal() as db:
        await db.execute(IdeWorkspaceFile.__table__.update().values(
            proposed_source="old proposal", state="modified", revision=2))
        await db.commit()
    _during_sap_read(monkeypatch, fake, _run_writes_revision_3)
    # SAP's source now equals the proposal the stale copy still holds.
    fake.answers["SAPRead"] = ["old proposal"]
    r = await client.post(f"/ide/api/sessions/{sid}/file/refresh",
                          params={"path": PATH}, headers=_as("alice"))
    assert r.status_code == 200, r.text
    async with SessionLocal() as db:
        row = (await db.execute(IdeWorkspaceFile.__table__.select())).one()
    assert row.proposed_source == "NEW proposal from the run"
    assert row.state == "modified"
    assert row.revision == 3


async def test_open_keeps_a_proposal_a_run_wrote_during_the_read(
    client, fake, sid, monkeypatch
):
    """``POST .../open`` (and a finding's open, same helper) re-reads the
    row under the lock too."""
    await _open(client, sid)
    async with SessionLocal() as db:
        await db.execute(IdeWorkspaceFile.__table__.update().values(
            proposed_source="old proposal", state="modified", revision=2))
        await db.commit()
    _during_sap_read(monkeypatch, fake, _run_writes_revision_3)
    fake.answers["SAPRead"] = ["old proposal"]
    r = await client.post(f"/ide/api/sessions/{sid}/open",
                          json={"type": "CLAS", "name": "ZCL_X"},
                          headers=_as("alice"))
    assert r.status_code == 200, r.text
    async with SessionLocal() as db:
        row = (await db.execute(IdeWorkspaceFile.__table__.select())).one()
    assert row.proposed_source == "NEW proposal from the run"
    assert row.revision == 3


async def test_lint_locks_the_session_before_the_file_row(client, fake, sid, lock_trail):
    """Order session row -> file row, like refresh/open: a lint and a refresh
    of the same file cannot deadlock on Postgres."""
    await _open(client, sid)
    fake.answers["SAPLint"] = [CANNED_LINT]
    lock_trail.clear()
    r = await client.post(f"/ide/api/sessions/{sid}/file/lint",
                          params={"path": PATH}, headers=_as("alice"))
    assert r.status_code == 200, r.text
    lock_trail.assert_lock_before_writes_to("ide_workspace_files")
