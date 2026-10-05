"""Comment, revision and message-activity routes (plan §1.1/§1.2, Task B6).

Ownership first: every route answers 404 for another user's session, and a
comment, revision or message of another session is "not found" under the
caller's own session, never "forbidden". Comment text is plain text stored
and returned verbatim; the user can make only the user transitions of the
comment state machine (no route sets ``sent`` or ``addressed``).

Run:  python -m pytest tests/test_ide_review_routes.py -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

(ROOT / "tests" / "_test_ide_review_routes.db").unlink(missing_ok=True)
os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_ide_review_routes.db'}",
)
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import json  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import select, update  # noqa: E402

from agents.auth import require_admin, require_developer  # noqa: E402
from agents.db import SessionLocal, init_db  # noqa: E402
from agents.ide import store  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeArtifact,
    IdeAuditLog,
    IdeComment,
    IdeConventions,
    IdeFileRevision,
    IdeMessage,
    IdeSession,
    IdeWorkspaceFile,
)
from agents.ide.review_routes import router as review_router  # noqa: E402
from agents.ide.routes import router as ide_router  # noqa: E402

USERS = {
    "alice": {"user_name": "alice", "scope": ["developer"]},
    "bob": {"user_name": "bob", "scope": ["developer"]},
    "viewer": {"user_name": "viewer", "scope": ["user"]},
}
PATH = "src/CLAS/zcl_demo.clas.abap"


def _fake_developer(request: Request) -> dict:
    user = request.headers.get("x-test-user", "")
    claims = USERS.get(user)
    if claims is None or "developer" not in claims["scope"]:
        raise HTTPException(status_code=403, detail="Developer scope required")
    return claims


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(review_router)
    app.include_router(ide_router)
    app.dependency_overrides[require_developer] = _fake_developer
    app.dependency_overrides[require_admin] = _fake_developer
    return app


@pytest.fixture(autouse=True)
async def _clean_db():
    await init_db()
    async with SessionLocal() as db:
        for model in (IdeComment, IdeFileRevision, IdeWorkspaceFile, IdeArtifact,
                      IdeMessage, IdeAuditLog, IdeSession, IdeConventions):
            await db.execute(model.__table__.delete())
        await db.commit()
        await store.upsert_conventions(db, "DEMO", label="Demo",
                                       actor="test-admin", non_production=True)
    yield


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=_app()),
                           base_url="http://test") as c:
        yield c


def _as(user: str) -> dict:
    return {"x-test-user": user}


ALICE = _as("alice")
BOB = _as("bob")


async def _create(client, user="alice", type_="change") -> str:
    r = await client.post("/ide/api/sessions",
                          json={"title": "t", "target": "DEMO", "type": type_},
                          headers=_as(user))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _seed_file(sid: str, revisions=("line1\nline2\nline3\n", "r2a\nr2b\n")):
    """A workspace file whose latest revision is ``len(revisions)``."""
    async with SessionLocal() as db:
        db.add(IdeWorkspaceFile(
            session_id=sid, path=PATH, object_type="CLAS", object_name="ZCL_DEMO",
            origin_source="old", proposed_source=revisions[-1], state="modified",
            revision=len(revisions), base_status="sap",
        ))
        for n, text in enumerate(revisions, start=1):
            db.add(IdeFileRevision(
                session_id=sid, path=PATH, revision=n, proposed_source=text,
                run_id=f"run-{n}",
                syntax_status="errors" if n == 1 else None,
                syntax_json='[{"line": 1, "message": "bad", "severity": "error"}]'
                if n == 1 else None,
            ))
        await db.commit()


async def _seed_artifact(sid: str, kind="design", version=1):
    async with SessionLocal() as db:
        db.add(IdeArtifact(session_id=sid, stage=kind if kind != "note" else "plan",
                           kind=kind, content="# Doc\n\npara", version=version))
        await db.commit()


def _file_comment(**over) -> dict:
    body = {"anchor": "file", "path": PATH, "revision": 1, "line_start": 1,
            "line_end": 2, "body": "Why this?"}
    body.update(over)
    return body


def _doc_comment(**over) -> dict:
    body = {"anchor": "document", "kind": "design", "version": 1, "paragraph": 0,
            "body": "Explain"}
    body.update(over)
    return body


async def _post_comment(client, sid, payload, headers=ALICE):
    return await client.post(f"/ide/api/sessions/{sid}/comments", json=payload,
                             headers=headers)


async def _set_state(cid: str, state: str):
    async with SessionLocal() as db:
        # A ``sent`` comment belongs to a run (``resolve_comments`` scope).
        await db.execute(update(IdeComment).where(IdeComment.id == cid)
                         .values(state=state,
                                 sent_run_id="run-1" if state == "sent" else None))
        await db.commit()


COMMENT_KEYS = {
    "id", "anchor", "path", "revision", "line_start", "line_end", "kind",
    "version", "paragraph", "body", "quote", "state", "answer", "created_at",
    "updated_at",
}


# --- typing -----------------------------------------------------------------


def test_every_review_route_is_typed_and_developer_scoped():
    from agents.ide import schemas

    keys = set()
    for route in review_router.routes:
        assert isinstance(route, APIRoute)
        for method in route.methods:
            keys.add((method, route.path))
        if route.status_code == 204 or route.path.endswith("/request-changes"):
            continue  # 204, or a server-sent event stream
        model = route.response_model
        inner = getattr(model, "__args__", (model,))[0]
        assert inner.__module__ == schemas.__name__, route.path
    assert keys == {
        ("GET", "/ide/api/sessions/{sid}/comments"),
        ("POST", "/ide/api/sessions/{sid}/comments"),
        ("PATCH", "/ide/api/sessions/{sid}/comments/{cid}"),
        ("DELETE", "/ide/api/sessions/{sid}/comments/{cid}"),
        ("GET", "/ide/api/sessions/{sid}/file/revisions"),
        ("GET", "/ide/api/sessions/{sid}/messages/{mid}/activity"),
        ("POST", "/ide/api/sessions/{sid}/request-changes"),
    }
    deps = {d.dependency for d in review_router.dependencies}
    assert require_developer in deps


async def test_without_developer_scope_every_route_is_refused(client):
    sid = await _create(client)
    viewer = _as("viewer")
    for method, url in (
        ("GET", f"/ide/api/sessions/{sid}/comments"),
        ("POST", f"/ide/api/sessions/{sid}/comments"),
        ("PATCH", f"/ide/api/sessions/{sid}/comments/x"),
        ("DELETE", f"/ide/api/sessions/{sid}/comments/x"),
        ("GET", f"/ide/api/sessions/{sid}/file/revisions?path={PATH}"),
        ("GET", f"/ide/api/sessions/{sid}/file?path={PATH}&revision=1"),
        ("GET", f"/ide/api/sessions/{sid}/messages/x/activity"),
    ):
        r = await client.request(method, url, headers=viewer, json={})
        assert r.status_code == 403, (method, url)


# --- ownership --------------------------------------------------------------


async def test_another_owners_session_is_404_on_every_route(client):
    sid = await _create(client)
    await _seed_file(sid)
    await _seed_artifact(sid)
    cid = (await _post_comment(client, sid, _doc_comment())).json()["id"]
    async with SessionLocal() as db:
        msg = await store.add_message(
            db, sid, stage="chat", role="assistant", content="a",
            activity_json='{"events": [], "plan": [], "dropped": 0}')
    for method, url, body in (
        ("GET", f"/ide/api/sessions/{sid}/comments", None),
        ("POST", f"/ide/api/sessions/{sid}/comments", _doc_comment()),
        ("PATCH", f"/ide/api/sessions/{sid}/comments/{cid}", {"body": "x"}),
        ("PATCH", f"/ide/api/sessions/{sid}/comments/{cid}", {"state": "dismissed"}),
        ("DELETE", f"/ide/api/sessions/{sid}/comments/{cid}", None),
        ("GET", f"/ide/api/sessions/{sid}/file/revisions?path={PATH}", None),
        ("GET", f"/ide/api/sessions/{sid}/file?path={PATH}&revision=1", None),
        ("GET", f"/ide/api/sessions/{sid}/file?path={PATH}", None),
        ("GET", f"/ide/api/sessions/{sid}/messages/{msg.id}/activity", None),
    ):
        r = await client.request(method, url, headers=BOB, json=body)
        assert r.status_code == 404, (method, url, r.text)
    # Nothing changed for alice.
    r = await client.get(f"/ide/api/sessions/{sid}/comments", headers=ALICE)
    assert [(c["id"], c["body"], c["state"]) for c in r.json()] == [
        (cid, "Explain", "open")]


async def test_unknown_session_is_404(client):
    r = await client.get("/ide/api/sessions/nope/comments", headers=ALICE)
    assert r.status_code == 404


async def test_comment_of_another_session_is_not_found_under_mine(client):
    mine = await _create(client)
    theirs = await _create(client, user="bob")
    await _seed_artifact(theirs)
    r = await _post_comment(client, theirs, _doc_comment(), headers=BOB)
    foreign = r.json()["id"]
    for method, body in (("PATCH", {"body": "x"}), ("PATCH", {"state": "dismissed"}),
                         ("DELETE", None)):
        r = await client.request(
            method, f"/ide/api/sessions/{mine}/comments/{foreign}", headers=ALICE,
            json=body)
        assert r.status_code == 404, (method, r.text)
        assert r.json()["code"] == "comment_not_found"
    async with SessionLocal() as db:
        row = await store.get_comment(db, theirs, foreign)
    assert (row.body, row.state) == ("Explain", "open")


async def test_another_sessions_comments_are_not_listed(client):
    mine = await _create(client)
    theirs = await _create(client, user="bob")
    await _seed_artifact(theirs)
    await _post_comment(client, theirs, _doc_comment(), headers=BOB)
    r = await client.get(f"/ide/api/sessions/{mine}/comments", headers=ALICE)
    assert r.status_code == 200 and r.json() == []


# --- create -----------------------------------------------------------------


async def test_create_file_comment(client):
    sid = await _create(client)
    await _seed_file(sid)
    r = await _post_comment(client, sid, _file_comment(revision=1, line_start=2,
                                                       line_end=3))
    assert r.status_code == 201, r.text
    c = r.json()
    assert set(c) == COMMENT_KEYS
    assert (c["anchor"], c["path"], c["revision"], c["line_start"], c["line_end"],
            c["state"], c["answer"]) == ("file", PATH, 1, 2, 3, "open", None)
    assert (c["kind"], c["version"], c["paragraph"]) == (None, None, None)


async def test_create_document_comment(client):
    sid = await _create(client)
    await _seed_artifact(sid, "design", 1)
    r = await _post_comment(client, sid, _doc_comment(paragraph=3))
    assert r.status_code == 201, r.text
    c = r.json()
    assert (c["anchor"], c["kind"], c["version"], c["paragraph"]) == (
        "document", "design", 1, 3)
    assert (c["path"], c["revision"], c["line_start"]) == (None, None, None)


async def test_body_is_plain_text_stored_and_returned_verbatim(client):
    sid = await _create(client)
    await _seed_artifact(sid)
    text = "<script>alert(1)</script> & <b>bold</b>\n  `x`"
    r = await _post_comment(client, sid, _doc_comment(body=text))
    assert r.status_code == 201
    assert r.json()["body"] == text
    r = await client.get(f"/ide/api/sessions/{sid}/comments", headers=ALICE)
    assert r.json()[0]["body"] == text
    assert r.headers["content-type"].startswith("application/json")
    async with SessionLocal() as db:
        row = (await db.execute(select(IdeComment))).scalar_one()
    assert row.body == text


@pytest.mark.parametrize("payload", [
    _file_comment(path="src/CLAS/zcl_other.clas.abap"),   # path not in session
    _file_comment(revision=3),                             # > file's revision
    _file_comment(line_start=3, line_end=2),               # end before start
    _file_comment(line_start=1, line_end=4),               # past the revision's text
    _doc_comment(version=2),                               # unknown version
    _doc_comment(kind="plan"),                             # unknown kind
])
async def test_invalid_anchor_is_422(client, payload):
    sid = await _create(client)
    await _seed_file(sid)
    await _seed_artifact(sid, "design", 1)
    r = await _post_comment(client, sid, payload)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "invalid_anchor"


async def test_anchor_of_another_session_is_invalid(client):
    mine = await _create(client)
    theirs = await _create(client, user="bob")
    await _seed_file(theirs)
    await _seed_artifact(theirs)
    for payload in (_file_comment(), _doc_comment()):
        r = await _post_comment(client, mine, payload)
        assert r.status_code == 422 and r.json()["code"] == "invalid_anchor"


@pytest.mark.parametrize("payload", [
    _doc_comment(body=""),
    _doc_comment(body="x" * 4001),
    _doc_comment(paragraph=-1),
    _doc_comment(version="1"),
    _doc_comment(state="addressed"),
    _file_comment(kind="design"),
    {"anchor": "line", "body": "x"},
])
async def test_request_model_rejects_bad_bodies(client, payload):
    sid = await _create(client)
    await _seed_file(sid)
    await _seed_artifact(sid)
    r = await _post_comment(client, sid, payload)
    assert r.status_code == 422, r.text


async def test_blank_body_is_invalid_body(client):
    sid = await _create(client)
    await _seed_artifact(sid)
    r = await _post_comment(client, sid, _doc_comment(body="   \n "))
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_body"


async def test_body_of_4000_chars_is_accepted(client):
    sid = await _create(client)
    await _seed_artifact(sid)
    r = await _post_comment(client, sid, _doc_comment(body="x" * 4000))
    assert r.status_code == 201


async def test_diagnose_session_takes_no_comments(client):
    sid = await _create(client, type_="diagnose")
    await _seed_artifact(sid, "report", 1)
    r = await _post_comment(client, sid, _doc_comment(kind="report"))
    assert r.status_code == 409
    assert r.json()["code"] == "comments_not_allowed"
    async with SessionLocal() as db:
        assert (await db.execute(select(IdeComment))).first() is None


# --- list -------------------------------------------------------------------


async def test_list_oldest_first_and_state_filter(client):
    sid = await _create(client)
    await _seed_artifact(sid)
    ids = []
    for n in range(3):
        r = await _post_comment(client, sid, _doc_comment(body=f"c{n}", paragraph=n))
        ids.append(r.json()["id"])
    await _set_state(ids[1], "sent")
    r = await client.get(f"/ide/api/sessions/{sid}/comments", headers=ALICE)
    assert [c["id"] for c in r.json()] == ids
    r = await client.get(f"/ide/api/sessions/{sid}/comments", params={"state": "open"},
                         headers=ALICE)
    assert [c["id"] for c in r.json()] == [ids[0], ids[2]]
    r = await client.get(f"/ide/api/sessions/{sid}/comments", params={"state": "sent"},
                         headers=ALICE)
    assert [c["id"] for c in r.json()] == [ids[1]]
    r = await client.get(f"/ide/api/sessions/{sid}/comments",
                         params={"state": "bogus"}, headers=ALICE)
    assert r.status_code == 422


# --- patch ------------------------------------------------------------------


async def _one_comment(client) -> tuple[str, str]:
    sid = await _create(client)
    await _seed_artifact(sid)
    r = await _post_comment(client, sid, _doc_comment())
    return sid, r.json()["id"]


async def test_patch_body_while_open(client):
    sid, cid = await _one_comment(client)
    r = await client.patch(f"/ide/api/sessions/{sid}/comments/{cid}",
                           json={"body": "<i>new</i>"}, headers=ALICE)
    assert r.status_code == 200, r.text
    assert set(r.json()) == COMMENT_KEYS
    assert (r.json()["body"], r.json()["state"]) == ("<i>new</i>", "open")


async def test_quote_created_cleaned_returned_and_patched(client):
    sid = await _create(client)
    await _seed_artifact(sid)
    r = await _post_comment(client, sid, _doc_comment())
    assert r.status_code == 201 and r.json()["quote"] is None
    r = await _post_comment(client, sid, _doc_comment(
        quote="<b>Goal</b>\n‮speed " + "x" * 300))
    assert r.status_code == 201, r.text
    q = r.json()["quote"]
    assert q.startswith("<b>Goal</b> speed x") and len(q) == 200
    cid = r.json()["id"]
    r = await client.patch(f"/ide/api/sessions/{sid}/comments/{cid}",
                           json={"quote": "other"}, headers=ALICE)
    assert r.status_code == 200, r.text
    assert (r.json()["quote"], r.json()["body"]) == ("other", "Explain")
    r = await client.patch(f"/ide/api/sessions/{sid}/comments/{cid}",
                           json={"body": "new"}, headers=ALICE)
    assert (r.json()["quote"], r.json()["body"]) == ("other", "new")
    r = await client.patch(f"/ide/api/sessions/{sid}/comments/{cid}",
                           json={"quote": None}, headers=ALICE)
    assert r.status_code == 200 and r.json()["quote"] is None
    listed = (await client.get(f"/ide/api/sessions/{sid}/comments",
                               headers=ALICE)).json()
    assert all("quote" in c for c in listed)
    for bad in ({"quote": 5}, {"quote": "q", "state": "dismissed"}):
        r = await client.patch(f"/ide/api/sessions/{sid}/comments/{cid}",
                               json=bad, headers=ALICE)
        assert r.status_code == 422, bad


@pytest.mark.parametrize("state", ["sent", "addressed", "dismissed"])
async def test_patch_body_when_not_open_is_409(client, state):
    sid, cid = await _one_comment(client)
    await _set_state(cid, state)
    r = await client.patch(f"/ide/api/sessions/{sid}/comments/{cid}",
                           json={"body": "x"}, headers=ALICE)
    assert r.status_code == 409
    assert r.json()["code"] == "comment_not_editable"


@pytest.mark.parametrize("payload", [
    {"body": "x", "state": "dismissed"},
    {},
    {"state": "sent"},
    {"state": "addressed"},
    {"body": ""},
    {"body": "x" * 4001},
    {"answer": "done"},
])
async def test_patch_rejects_bad_bodies(client, payload):
    sid, cid = await _one_comment(client)
    r = await client.patch(f"/ide/api/sessions/{sid}/comments/{cid}", json=payload,
                           headers=ALICE)
    assert r.status_code == 422, r.text
    async with SessionLocal() as db:
        row = await store.get_comment(db, sid, cid)
    assert (row.body, row.state, row.answer) == ("Explain", "open", None)


@pytest.mark.parametrize("start,target,ok", [
    ("open", "dismissed", True),
    ("addressed", "dismissed", True),
    ("addressed", "open", True),
    ("dismissed", "open", True),
    ("open", "open", False),
    ("sent", "open", False),
    ("sent", "dismissed", False),
    ("dismissed", "dismissed", False),
])
async def test_user_transitions(client, start, target, ok):
    sid, cid = await _one_comment(client)
    await _set_state(cid, start)
    r = await client.patch(f"/ide/api/sessions/{sid}/comments/{cid}",
                           json={"state": target}, headers=ALICE)
    if ok:
        assert r.status_code == 200, r.text
        assert r.json()["state"] == target
    else:
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "invalid_transition"
        async with SessionLocal() as db:
            assert (await store.get_comment(db, sid, cid)).state == start


async def test_patch_unknown_comment_is_404(client):
    sid, _ = await _one_comment(client)
    r = await client.patch(f"/ide/api/sessions/{sid}/comments/nope",
                           json={"state": "dismissed"}, headers=ALICE)
    assert r.status_code == 404 and r.json()["code"] == "comment_not_found"


async def test_answer_is_served(client):
    sid, cid = await _one_comment(client)
    await _set_state(cid, "sent")
    async with SessionLocal() as db:
        await store.resolve_comments(db, sid, [(cid, "Done: " + "y" * 600)],
                                     run_id="run-1")
    r = await client.get(f"/ide/api/sessions/{sid}/comments", headers=ALICE)
    c = r.json()[0]
    assert c["state"] == "addressed" and len(c["answer"]) == 500


# --- delete -----------------------------------------------------------------


async def test_delete_open_comment(client):
    sid, cid = await _one_comment(client)
    r = await client.delete(f"/ide/api/sessions/{sid}/comments/{cid}", headers=ALICE)
    assert r.status_code == 204 and r.content == b""
    r = await client.get(f"/ide/api/sessions/{sid}/comments", headers=ALICE)
    assert r.json() == []


@pytest.mark.parametrize("state", ["sent", "addressed", "dismissed"])
async def test_delete_when_not_open_is_409(client, state):
    sid, cid = await _one_comment(client)
    await _set_state(cid, state)
    r = await client.delete(f"/ide/api/sessions/{sid}/comments/{cid}", headers=ALICE)
    assert r.status_code == 409 and r.json()["code"] == "comment_not_editable"


async def test_delete_unknown_comment_is_404(client):
    sid, _ = await _one_comment(client)
    r = await client.delete(f"/ide/api/sessions/{sid}/comments/nope", headers=ALICE)
    assert r.status_code == 404 and r.json()["code"] == "comment_not_found"


# --- revisions --------------------------------------------------------------


async def test_revisions_newest_first(client):
    sid = await _create(client)
    await _seed_file(sid)
    r = await client.get(f"/ide/api/sessions/{sid}/file/revisions",
                         params={"path": PATH}, headers=ALICE)
    assert r.status_code == 200, r.text
    revs = r.json()
    assert [set(x) for x in revs] == [
        {"revision", "run_id", "created_at", "chars", "syntax_status"}] * 2
    assert [(x["revision"], x["run_id"], x["chars"], x["syntax_status"])
            for x in revs] == [
        (2, "run-2", len("r2a\nr2b\n"), None),
        (1, "run-1", len("line1\nline2\nline3\n"), "errors")]
    assert all("proposed_source" not in x for x in revs)


async def test_revisions_of_unknown_path_is_404(client):
    sid = await _create(client)
    await _seed_file(sid)
    r = await client.get(f"/ide/api/sessions/{sid}/file/revisions",
                         params={"path": "src/CLAS/zcl_none.clas.abap"}, headers=ALICE)
    assert r.status_code == 404


async def test_revisions_bad_path_is_422(client):
    sid = await _create(client)
    r = await client.get(f"/ide/api/sessions/{sid}/file/revisions",
                         params={"path": "../etc/passwd"}, headers=ALICE)
    assert r.status_code == 422


async def test_revisions_of_another_sessions_file_are_unreachable(client):
    mine = await _create(client)
    theirs = await _create(client, user="bob")
    await _seed_file(theirs)
    r = await client.get(f"/ide/api/sessions/{mine}/file/revisions",
                         params={"path": PATH}, headers=ALICE)
    assert r.status_code == 404
    r = await client.get(f"/ide/api/sessions/{mine}/file",
                         params={"path": PATH, "revision": 1}, headers=ALICE)
    assert r.status_code == 404


# --- file ?revision= --------------------------------------------------------


async def test_file_revision_serves_that_revisions_source(client):
    sid = await _create(client)
    await _seed_file(sid)
    r = await client.get(f"/ide/api/sessions/{sid}/file",
                         params={"path": PATH, "revision": 1}, headers=ALICE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposed_source"] == "line1\nline2\nline3\n"
    assert body["origin_source"] == "old"
    assert body["syntax_status"] == "errors"
    assert body["syntax"] == [{"line": 1, "message": "bad", "severity": "error"}]
    assert body["revision"] == 2  # the file's latest, as in FileSummaryOut


async def test_file_revision_2_and_latest(client):
    sid = await _create(client)
    await _seed_file(sid, revisions=("a\n", "b\n", "c\n"))
    r = await client.get(f"/ide/api/sessions/{sid}/file",
                         params={"path": PATH, "revision": 2}, headers=ALICE)
    assert r.status_code == 200 and r.json()["proposed_source"] == "b\n"
    r = await client.get(f"/ide/api/sessions/{sid}/file", params={"path": PATH},
                         headers=ALICE)
    assert r.status_code == 200 and r.json()["proposed_source"] == "c\n"
    assert r.json()["syntax_status"] is None


async def test_unknown_revision_is_404(client):
    sid = await _create(client)
    await _seed_file(sid)
    r = await client.get(f"/ide/api/sessions/{sid}/file",
                         params={"path": PATH, "revision": 9}, headers=ALICE)
    assert r.status_code == 404
    assert r.json()["code"] == "unknown_revision"


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1.5", "+1", "１"])
async def test_bad_revision_is_unknown_revision(client, value):
    """Not a revision of this file, whatever the spelling: 404."""
    sid = await _create(client)
    await _seed_file(sid)
    r = await client.get(f"/ide/api/sessions/{sid}/file",
                         params={"path": PATH, "revision": value}, headers=ALICE)
    assert r.status_code == 404
    assert r.json()["code"] == "unknown_revision"


def test_file_route_is_served_by_ide_router_only():
    """One ``GET .../file`` (with ``?revision=``): the router order in
    ``app.py`` cannot change which one answers."""
    paths = [r.path for r in review_router.routes]
    assert "/ide/api/sessions/{sid}/file" not in paths
    assert "/ide/api/sessions/{sid}/file" in [r.path for r in ide_router.routes]


async def test_syntax_statuses_load_only_the_latest_revision(client):
    """The file list's syntax status is the latest revision's, and only
    that revision's row is read (not every revision of the file)."""
    from agents.ide import routes

    sid = await _create(client)
    await _seed_file(sid, revisions=("a\n", "b\n", "c\n"))
    async with SessionLocal() as db:
        await db.execute(update(IdeFileRevision).where(
            IdeFileRevision.session_id == sid, IdeFileRevision.revision == 3,
        ).values(syntax_status="ok"))
        await db.commit()
        row = (await db.execute(select(IdeWorkspaceFile).where(
            IdeWorkspaceFile.session_id == sid))).scalar_one()
        seen: list = []
        real = db.execute

        class _Rows:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

        async def spy(stmt, *a, **kw):
            rows = (await real(stmt, *a, **kw)).all()
            seen.extend(rows)
            return _Rows(rows)

        db.execute = spy
        out = await routes._syntax_statuses(db, sid, [row])
    assert out == {PATH: "ok"}
    assert len(seen) == 1


# --- message activity -------------------------------------------------------


ACTIVITY = {
    "events": [
        {"ts": "2026-10-03T10:00:00+00:00", "agent": "abap-developer", "kind": "tool",
         "id": "call-1", "tool": "SAPRead", "detail": "type=CLAS", "status": "ok",
         "output": "src", "ended": "2026-10-03T10:00:01+00:00"},
        {"ts": "2026-10-03T10:00:02+00:00", "agent": "abap-developer",
         "kind": "note", "detail": "thinking"},
        "junk",
    ],
    "plan": [{"content": "Read the class", "status": "completed"}, {"x": 1}],
    "dropped": 4,
}


async def _message(sid, role="assistant", activity=ACTIVITY):
    async with SessionLocal() as db:
        return await store.add_message(
            db, sid, stage="chat", role=role, content="m",
            activity_json=json.dumps(activity) if activity is not None else None)


async def test_activity_of_an_assistant_message(client):
    sid = await _create(client)
    msg = await _message(sid)
    r = await client.get(f"/ide/api/sessions/{sid}/messages/{msg.id}/activity",
                         headers=ALICE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"events", "plan", "dropped"}
    assert body["dropped"] == 4
    assert [e["kind"] for e in body["events"]] == ["tool", "note"]
    assert body["events"][0]["tool"] == "SAPRead"
    assert body["plan"] == [{"content": "Read the class", "status": "completed"}]


async def test_activity_keeps_a_tool_event_code(client):
    """``runner`` sets ``code`` on a refused or unstored-proposal tool event;
    the response model must not filter it out."""
    sid = await _create(client)
    activity = {"events": [{"kind": "tool", "id": "c1", "tool": "SAPQuery",
                            "status": "error", "output": "refused",
                            "code": "readonly_refused"}],
                "plan": [], "dropped": 0}
    msg = await _message(sid, activity=activity)
    r = await client.get(f"/ide/api/sessions/{sid}/messages/{msg.id}/activity",
                         headers=ALICE)
    assert r.status_code == 200
    assert r.json()["events"][0]["code"] == "readonly_refused"


async def test_activity_of_a_user_message_is_no_activity(client):
    sid = await _create(client)
    msg = await _message(sid, role="user", activity=None)
    r = await client.get(f"/ide/api/sessions/{sid}/messages/{msg.id}/activity",
                         headers=ALICE)
    assert r.status_code == 404
    assert r.json()["code"] == "no_activity"


async def test_damaged_activity_is_no_activity(client):
    sid = await _create(client)
    async with SessionLocal() as db:
        msg = await store.add_message(db, sid, stage="chat", role="assistant",
                                      content="m", activity_json="{not json")
    r = await client.get(f"/ide/api/sessions/{sid}/messages/{msg.id}/activity",
                         headers=ALICE)
    assert r.status_code == 404 and r.json()["code"] == "no_activity"


async def test_activity_of_another_sessions_message_is_404(client):
    mine = await _create(client)
    theirs = await _create(client, user="bob")
    other_mine = await _create(client)
    msg = await _message(theirs)
    own_other = await _message(other_mine)
    for sid, mid in ((mine, msg.id), (mine, own_other.id), (mine, "nope")):
        r = await client.get(f"/ide/api/sessions/{sid}/messages/{mid}/activity",
                             headers=ALICE)
        assert r.status_code == 404
        assert r.json()["code"] == "message_not_found"


# --- audit trail survives a session delete ----------------------------------


async def test_audit_row_survives_deleting_its_session(client):
    sid = await _create(client)
    await _seed_file(sid)
    await _seed_artifact(sid)
    await _post_comment(client, sid, _doc_comment())
    async with SessionLocal() as db:
        await store.add_audit(db, principal="alice", session_id=sid, target="DEMO",
                              action="trace_deny", params={}, outcome="denied")
    r = await client.delete(f"/ide/api/sessions/{sid}", headers=ALICE)
    assert r.status_code == 204, r.text
    async with SessionLocal() as db:
        audits = (await db.execute(
            select(IdeAuditLog).where(IdeAuditLog.session_id == sid))).scalars().all()
        assert len(audits) == 1
        for model in (IdeComment, IdeFileRevision, IdeWorkspaceFile, IdeArtifact):
            left = (await db.execute(
                select(model).where(model.session_id == sid))).first()
            assert left is None, model.__name__


# --- stored values outside the contract enums ---------------------------------


async def test_bad_stored_enum_values_are_coerced_not_500(client, caplog):
    """Hand-written rows with values outside the response enums: every
    route still answers 200 with a safe member, and a WARNING is logged
    (without the stored value)."""
    sid = await _create(client)
    await _seed_file(sid)
    await _seed_artifact(sid)
    r = await _post_comment(client, sid, _file_comment())
    cid = r.json()["id"]
    await _message(sid)
    async with SessionLocal() as db:
        await db.execute(update(IdeSession).where(IdeSession.id == sid)
                         .values(stage="bogus-stage", status="weird-status"))
        await db.execute(update(IdeArtifact).where(IdeArtifact.session_id == sid)
                         .values(kind="memo-kind", stage="bogus-stage"))
        await db.execute(update(IdeMessage).where(IdeMessage.session_id == sid)
                         .values(role="tool-role"))
        await db.execute(update(IdeWorkspaceFile)
                         .where(IdeWorkspaceFile.session_id == sid)
                         .values(state="deleted-state"))
        await db.execute(update(IdeComment).where(IdeComment.id == cid)
                         .values(state="weird-state", anchor="line-anchor"))
        await db.commit()
    with caplog.at_level("WARNING"):
        detail = await client.get(f"/ide/api/sessions/{sid}", headers=ALICE)
        msgs = await client.get(f"/ide/api/sessions/{sid}/messages", headers=ALICE)
        comments = await client.get(f"/ide/api/sessions/{sid}/comments", headers=ALICE)
        listing = await client.get("/ide/api/sessions", headers=ALICE)
    for r in (detail, msgs, comments, listing):
        assert r.status_code == 200, r.text
    body = detail.json()
    assert (body["stage"], body["status"]) == ("done", "idle")
    assert body["artifacts"][0]["kind"] == "note"
    assert body["artifacts"][0]["stage"] == "done"
    assert body["files"][0]["state"] == "read"
    assert msgs.json()[0]["role"] == "system"
    assert comments.json()[0]["state"] == "dismissed"
    assert comments.json()[0]["anchor"] == "document"
    assert "Coerced" in caplog.text
    for raw in ("bogus-stage", "weird-status", "memo-kind", "tool-role",
                "deleted-state", "weird-state", "line-anchor"):
        assert raw not in caplog.text


async def test_comment_with_lone_surrogate_round_trips_over_http(client):
    # httpx would refuse to encode a lone surrogate itself; send the escaped
    # JSON a browser produces for a quote cut inside an emoji.
    sid = await _create(client)
    await _seed_artifact(sid)
    raw = json.dumps(_doc_comment(body="abc\ud83d", quote="abc\ud83d"))
    r = await client.post(f"/ide/api/sessions/{sid}/comments", content=raw,
                          headers={**ALICE, "content-type": "application/json"})
    assert r.status_code == 201, r.text
    assert r.json()["body"] == "abc" and r.json()["quote"] == "abc"
    r = await client.get(f"/ide/api/sessions/{sid}/comments", headers=ALICE)
    assert r.status_code == 200
    assert [(c["body"], c["quote"]) for c in r.json()] == [("abc", "abc")]
