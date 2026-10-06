"""Typed response models of ``/ide/api`` and the exported API schema.

The models in ``agents.ide.schemas`` are the contract the UI's fake backend
is tested against, so every JSON route declares one, the committed schema
file is what ``export_schema()`` produces, and the serialisers emit exactly
the fields of plan §1.2 -- nothing missing, nothing extra.

Run:  python -m pytest tests/test_ide_schemas.py -q
"""

from __future__ import annotations

import os
import sys
import typing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from pydantic import BaseModel, ValidationError  # noqa: E402
from sqlalchemy import update  # noqa: E402

from agents.auth import require_admin, require_developer  # noqa: E402
from agents.db import SessionLocal, init_db  # noqa: E402
from agents.ide import schemas  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeArtifact,
    IdeFileRevision,
    IdeMessage,
    IdeSession,
    IdeWorkspaceFile,
)
from agents.ide.review_routes import router as review_router  # noqa: E402
from agents.ide.routes import router as ide_router  # noqa: E402
from agents.ide.store import add_message, upsert_conventions  # noqa: E402

SCHEMA_FILE = ROOT / "ui5-ide" / "webapp" / "test" / "contract" / "ide-api.schema.json"

# Routes that answer a server-sent event stream, not JSON.
SSE_ROUTES = {
    ("POST", "/ide/api/sessions/{sid}/messages"),
    ("POST", "/ide/api/sessions/{sid}/request-changes"),
    ("POST", "/ide/api/sessions/{sid}/report"),
}

USERS = {
    "alice": {"user_name": "alice", "scope": ["developer"]},
    "admin": {"user_name": "admin", "scope": ["developer", "admin"]},
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


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(ide_router)
    app.dependency_overrides[require_developer] = _fake_developer
    app.dependency_overrides[require_admin] = _fake_admin
    return app


@pytest.fixture(autouse=True)
async def _clean_db():
    await init_db()
    async with SessionLocal() as db:
        for model in (IdeFileRevision, IdeWorkspaceFile, IdeArtifact, IdeMessage,
                      IdeSession):
            await db.execute(model.__table__.delete())
        await db.commit()
        await upsert_conventions(db, "DEMO", label="Demo", actor="test-admin", non_production=True)
    yield


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=_app()),
                           base_url="http://test") as c:
        yield c


ALICE = {"x-test-user": "alice"}
ADMIN = {"x-test-user": "admin"}
PATH = "src/CLAS/zcl_demo.clas.abap"

SESSION_KEYS = {
    "id", "owner", "title", "target", "type", "stage", "status", "created_at",
    "updated_at", "requests_used", "request_cap", "target_non_production",
    "pins", "waiting", "open_comments", "unresolved_comments",
    # B10 carry-forward: the worklist's object line and counts.
    "objects", "objects_total", "changed_objects", "findings_count",
}
FILE_SUMMARY_KEYS = {
    "path", "state", "object_type", "object_name", "revision", "base_status",
    "syntax_status",
}
FILE_DETAIL_KEYS = FILE_SUMMARY_KEYS | {
    "origin_source", "proposed_source", "origin_version", "lint", "syntax",
}


async def _create(client, title="My session") -> dict:
    r = await client.post("/ide/api/sessions", json={"title": title, "target": "DEMO"},
                          headers=ALICE)
    assert r.status_code == 201, r.text
    return r.json()


def _model_of(annotation) -> type | None:
    """The BaseModel a response model names, directly or as ``list[...]``."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    if typing.get_origin(annotation) is list:
        return _model_of(typing.get_args(annotation)[0])
    return None


# --- every route is typed ---------------------------------------------------


def test_every_ide_route_has_a_response_model():
    untyped = []
    for route in ide_router.routes:
        assert isinstance(route, APIRoute)
        for method in route.methods:
            key = (method, route.path)
            if key in SSE_ROUTES or route.status_code == 204:
                continue
            model = _model_of(route.response_model)
            if model is None or model.__module__ != "agents.ide.schemas":
                untyped.append(key)
    assert untyped == []


def test_sse_and_204_routes_exist():
    """The exemptions above name real routes (a renamed route must not slip
    through as "SSE")."""
    keys = {(m, r.path) for r in (*ide_router.routes, *review_router.routes)
            for m in r.methods}
    assert SSE_ROUTES <= keys
    assert ("DELETE", "/ide/api/sessions/{sid}") in keys


# --- the route map ----------------------------------------------------------

def _all_routes() -> list[APIRoute]:
    routes = [*ide_router.routes, *review_router.routes]
    assert all(isinstance(r, APIRoute) for r in routes)
    return routes


def test_every_ide_route_declares_its_models():
    """A route answers 204, an event stream (declared in ``responses``), or
    JSON described by a ``response_model`` -- nothing undeclared."""
    undeclared = sorted(
        f"{m} {r.path}"
        for r in _all_routes() for m in r.methods
        if r.status_code != 204 and r.response_model is None
        and not schemas.is_stream_route(r)
    )
    assert undeclared == [], (
        "routes without response_model= (or SSE responses=): " + ", ".join(undeclared))


def test_stream_routes_are_exactly_the_sse_routes():
    streams = {(m, r.path) for r in _all_routes() for m in r.methods
               if schemas.is_stream_route(r)}
    assert streams == SSE_ROUTES


def test_route_map_known_entries():
    routes = schemas.route_map()
    assert routes["GET /sessions"] == {
        "response": "SessionOut", "request": None, "list": True, "stream": False}
    assert routes["POST /sessions"] == {
        "response": "SessionOut", "request": "SessionCreate", "list": False,
        "stream": False}
    assert routes["POST /sessions/{sid}/messages"] == {
        "response": None, "request": "MessageBody", "list": False, "stream": True}
    assert routes["POST /sessions/{sid}/request-changes"] == {
        "response": None, "request": "RequestChangesBody", "list": False,
        "stream": True}
    assert routes["POST /sessions/{sid}/report"] == {
        "response": None, "request": None, "list": False, "stream": True}
    assert routes["POST /sessions/{sid}/approve"]["request"] == "ApproveBody"
    assert routes["PATCH /sessions/{sid}/comments/{cid}"] == {
        "response": "CommentOut", "request": "CommentPatch", "list": False,
        "stream": False}
    assert routes["GET /sessions/{sid}/file/revisions"] == {
        "response": "FileRevisionOut", "request": None, "list": True, "stream": False}
    for key in ("DELETE /sessions/{sid}", "DELETE /sessions/{sid}/comments/{cid}"):
        assert routes[key] == {
            "response": None, "request": None, "list": False, "stream": False}


def test_route_map_covers_every_route_in_sorted_order():
    routes = schemas.route_map()
    expected = {f"{m} {r.path.removeprefix('/ide/api')}"
                for r in _all_routes() for m in r.methods}
    assert set(routes) == expected
    assert list(routes) == sorted(routes)


def test_route_map_names_resolve_in_defs():
    exported = schemas.export_schema()
    defs, routes = exported["$defs"], exported["x-routes"]
    assert routes == schemas.route_map()
    unresolved = sorted(
        f"{key}: {entry[part]}"
        for key, entry in routes.items() for part in ("response", "request")
        if entry[part] is not None and entry[part] not in defs
    )
    assert unresolved == [], "x-routes names models missing from $defs: " + ", ".join(
        unresolved)


# --- the exported schema ----------------------------------------------------


def test_schema_file_is_current():
    assert SCHEMA_FILE.is_file(), "run scripts/export_ide_schema.py"
    assert SCHEMA_FILE.read_text(encoding="utf-8") == schemas.render_schema(), (
        "ide-api.schema.json is stale: run .venv/bin/python scripts/export_ide_schema.py"
    )


def test_schema_names_every_contract_model():
    defs = schemas.export_schema()["$defs"]
    for name in (
        "SessionOut", "SessionDetailOut", "ArtifactSummaryOut", "ArtifactOut",
        "MessageOut", "ActivityOut", "ToolEventOut", "TodoOut", "FileSummaryOut",
        "FileDetailOut", "FileRevisionOut", "LintFindingOut", "CommentCreate",
        "CommentPatch", "CommentOut", "ConventionsOut", "ConventionsCreate",
        "MeOut", "FindingOut", "FindingDetailOut", "FindingOpenOut", "ApprovalOut",
        "ObjectHitOut", "AdminSessionRowOut", "SeedRefreshOut", "SyntaxResultOut",
        "ErrorOut", "PinsOut", "SyntaxItemOut", "SessionCreate", "SessionPatch",
        "MessageBody", "OpenBody", "ApprovalDecision",
    ):
        assert name in defs, name
    assert list(defs) == sorted(defs)
    # Every reference resolves inside the one file.
    text = schemas.render_schema()
    for ref in {part.split('"')[0] for part in text.split('"#/$defs/')[1:]}:
        assert ref in defs, ref


def test_session_out_schema_requires_every_field():
    session = schemas.export_schema()["$defs"]["SessionOut"]
    assert set(session["properties"]) == SESSION_KEYS
    assert set(session["required"]) == SESSION_KEYS


# --- serialised shapes ------------------------------------------------------


async def test_session_out_fields(client):
    s = await _create(client)
    assert set(s) == SESSION_KEYS
    assert s["target_non_production"] is True
    assert s["pins"] == {} and s["waiting"] is None
    assert (s["open_comments"], s["unresolved_comments"]) == (0, 0)
    assert "masked" not in s

    r = await client.get("/ide/api/sessions", headers=ALICE)
    assert [set(x) for x in r.json()] == [SESSION_KEYS]
    r = await client.get(f"/ide/api/sessions/{s['id']}", headers=ALICE)
    assert set(r.json()) == SESSION_KEYS | {"artifacts", "files"}


async def test_pins_are_served_without_empty_keys(client):
    s = await _create(client)
    async with SessionLocal() as db:
        await db.execute(update(IdeSession).where(IdeSession.id == s["id"]).values(
            pins_json='{"design": 2, "files": {"%s": 3}, "bogus": 1}' % PATH))
        await db.commit()
    r = await client.get(f"/ide/api/sessions/{s['id']}", headers=ALICE)
    assert r.json()["pins"] == {"design": 2, "files": {PATH: 3}}


async def test_artifact_summary_has_based_on(client):
    s = await _create(client)
    async with SessionLocal() as db:
        db.add(IdeArtifact(session_id=s["id"], stage="plan", kind="plan",
                           content="p", version=1, based_on_json='{"design": 2}'))
        db.add(IdeArtifact(session_id=s["id"], stage="design", kind="design",
                           content="d", version=1))
        await db.commit()
    r = await client.get(f"/ide/api/sessions/{s['id']}", headers=ALICE)
    arts = {a["kind"]: a for a in r.json()["artifacts"]}
    assert set(arts["plan"]) == {"id", "stage", "kind", "version", "created_at",
                                 "based_on"}
    assert arts["plan"]["based_on"] == {"design": 2}
    assert arts["design"]["based_on"] is None
    r = await client.get(f"/ide/api/sessions/{s['id']}/artifacts", headers=ALICE)
    assert {a["kind"]: a["based_on"] for a in r.json()} == {
        "plan": {"design": 2}, "design": None}
    assert all("content" in a for a in r.json())


async def test_messages_list_has_no_activity_but_flag(client):
    s = await _create(client)
    async with SessionLocal() as db:
        await add_message(db, s["id"], stage="chat", role="user", content="hi")
        await add_message(db, s["id"], stage="chat", role="assistant", content="a",
                          activity_json='{"events": [], "plan": [], "dropped": 0}')
    r = await client.get(f"/ide/api/sessions/{s['id']}/messages", headers=ALICE)
    assert r.status_code == 200
    msgs = r.json()
    keys = {"id", "stage", "role", "content", "created_at", "has_activity"}
    assert [set(m) for m in msgs] == [keys, keys]
    assert [m["has_activity"] for m in msgs] == [False, True]


async def _seed_file(sid: str, *, syntax_status=None, base_status="sap") -> None:
    async with SessionLocal() as db:
        db.add(IdeWorkspaceFile(
            session_id=sid, path=PATH, object_type="CLAS", object_name="ZCL_DEMO",
            origin_source="old", proposed_source="new", state="modified",
            revision=2, origin_version="v-7", base_status=base_status,
            lint_json='[{"line": 1, "column": 2, "severity": "error", '
                      '"message": "m", "rule": "r"}, "junk"]',
        ))
        db.add(IdeFileRevision(session_id=sid, path=PATH, revision=1,
                               proposed_source="older", syntax_status="errors"))
        db.add(IdeFileRevision(
            session_id=sid, path=PATH, revision=2, proposed_source="new",
            syntax_status=syntax_status,
            syntax_json='[{"line": 3, "message": "x", "severity": "warning"}]'
            if syntax_status else None,
        ))
        await db.commit()


async def test_file_detail_has_revision_and_base_status(client):
    s = await _create(client)
    await _seed_file(s["id"], syntax_status="ok")
    r = await client.get(f"/ide/api/sessions/{s['id']}/file", params={"path": PATH},
                         headers=ALICE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == FILE_DETAIL_KEYS
    assert (body["revision"], body["base_status"], body["origin_version"]) == (
        2, "sap", "v-7")
    assert body["syntax_status"] == "ok"  # of revision 2, not of revision 1
    assert body["syntax"] == [{"line": 3, "message": "x", "severity": "warning"}]
    # A stored lint item that is not a finding is left out, not a 500.
    assert body["lint"] == [{"line": 1, "column": 2, "severity": "error",
                             "message": "m", "rule": "r"}]


async def test_file_summary_fields_and_unchecked_syntax(client):
    s = await _create(client)
    await _seed_file(s["id"], syntax_status=None, base_status=None)
    r = await client.get(f"/ide/api/sessions/{s['id']}/files", headers=ALICE)
    assert [set(f) for f in r.json()] == [FILE_SUMMARY_KEYS]
    f = r.json()[0]
    assert (f["revision"], f["base_status"], f["syntax_status"]) == (2, None, None)
    r = await client.get(f"/ide/api/sessions/{s['id']}", headers=ALICE)
    assert r.json()["files"] == [f]
    r = await client.get(f"/ide/api/sessions/{s['id']}/file", params={"path": PATH},
                         headers=ALICE)
    assert r.json()["syntax"] == [] and r.json()["syntax_status"] is None


async def test_admin_listing_is_metadata_only(client):
    await _create(client)
    r = await client.get("/ide/api/admin/sessions", headers=ADMIN)
    assert r.status_code == 200
    assert [set(x) for x in r.json()] == [{
        "id", "owner", "title", "target", "type", "stage", "status",
        "created_at", "updated_at"}]


async def test_errors_keep_detail_and_code(client):
    s = await _create(client)
    async with SessionLocal() as db:
        await db.execute(update(IdeSession).where(IdeSession.id == s["id"])
                         .values(status="running"))
        await db.commit()
    r = await client.delete(f"/ide/api/sessions/{s['id']}", headers=ALICE)
    assert r.status_code == 409
    assert r.json() == {"detail": r.json()["detail"], "code": "run_in_progress"}


# --- input models -----------------------------------------------------------


def test_comment_create_is_a_union_on_anchor():
    file = schemas.CommentCreate.model_validate({
        "anchor": "file", "path": PATH, "revision": 1, "line_start": 2,
        "line_end": 3, "body": "why?"})
    assert file.root.anchor == "file"
    doc = schemas.CommentCreate.model_validate({
        "anchor": "document", "kind": "design", "version": 1, "paragraph": 0,
        "body": "why?"})
    assert doc.root.anchor == "document"
    for bad in (
        {"anchor": "file", "path": PATH, "revision": 1, "line_start": 2,
         "line_end": 3, "body": "x", "kind": "design"},          # extra field
        {"anchor": "line", "body": "x"},                          # unknown anchor
        {"anchor": "document", "kind": "design", "version": "1", "paragraph": 0,
         "body": "x"},                                            # not an int
        {"anchor": "document", "kind": "design", "version": 1, "paragraph": 0,
         "body": ""},                                             # empty body
        {"anchor": "document", "kind": "design", "version": 1, "paragraph": 0,
         "body": "x" * 4001},
    ):
        with pytest.raises(ValidationError):
            schemas.CommentCreate.model_validate(bad)


def test_comment_patch_is_body_xor_state():
    assert schemas.CommentPatch.model_validate({"body": "b"}).body == "b"
    assert schemas.CommentPatch.model_validate({"state": "dismissed"}).state == "dismissed"
    for bad in ({}, {"body": "b", "state": "open"}, {"state": "sent"},
                {"state": "addressed"}, {"body": "b", "x": 1}):
        with pytest.raises(ValidationError):
            schemas.CommentPatch.model_validate(bad)


def test_conventions_create_is_strict():
    ok = schemas.ConventionsCreate.model_validate({"target": "DEMO",
                                                   "non_production": True})
    assert ok.non_production is True
    for bad in ({"target": "DEMO", "non_production": "yes"},
                {"target": "bad target"}, {"target": "DEMO", "other": 1}):
        with pytest.raises(ValidationError):
            schemas.ConventionsCreate.model_validate(bad)
