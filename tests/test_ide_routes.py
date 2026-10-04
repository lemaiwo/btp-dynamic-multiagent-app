"""IDE REST API (contract §1.2): sessions, approve, artifacts, conventions, admin.

Ownership is the point of this suite: another user's session answers 404 on
every ``{sid}`` route (no existence leak), the admin overview carries
metadata only, and conventions writes need the admin scope.

Run:  python -m pytest tests/test_ide_routes.py -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "DATABASE_URL", f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_ide_routes.db'}"
)
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from agents.auth import current_principal, require_admin, require_developer  # noqa: E402
from agents.db import SessionLocal, init_db  # noqa: E402
from agents.ide import store as ide_store  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeApproval,
    IdeArtifact,
    IdeComment,
    IdeConventions,
    IdeFileRevision,
    IdeMessage,
    IdeSession,
    IdeWorkspaceFile,
)
from agents.ide.review_routes import router as review_router  # noqa: E402
from agents.ide.routes import _principal  # noqa: E402
from agents.ide.routes import router as ide_router  # noqa: E402
from agents.ide.store import add_artifact, add_message, upsert_conventions  # noqa: E402

USERS = {
    "alice": {"user_name": "alice", "scope": ["developer"]},
    "bob": {"user_name": "bob", "scope": ["developer"]},
    "admin": {"user_name": "admin", "scope": ["developer", "admin"]},
    "nameless": {"scope": ["developer"]},
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


class _PrincipalMiddleware:
    """Binds ``current_principal`` from ``x-test-principal``, as app.py's
    middleware binds it from the validated token."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        value = dict(scope.get("headers") or []).get(b"x-test-principal")
        token = current_principal.set(value.decode() if value else None)
        try:
            await self.app(scope, receive, send)
        finally:
            current_principal.reset(token)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(review_router)
    app.include_router(ide_router)
    app.dependency_overrides[require_developer] = _fake_developer
    app.dependency_overrides[require_admin] = _fake_admin
    app.add_middleware(_PrincipalMiddleware)
    return app


@pytest.fixture(autouse=True)
async def _clean_db():
    await init_db()
    async with SessionLocal() as db:
        for model in (IdeComment, IdeFileRevision, IdeApproval, IdeWorkspaceFile,
                      IdeArtifact, IdeMessage, IdeSession, IdeConventions):
            await db.execute(model.__table__.delete())
        await db.commit()
        await upsert_conventions(db, "T1", label="Target one", namespace="Z")
    yield


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=_app()),
                           base_url="http://test") as c:
        yield c


def _as(user: str) -> dict:
    return {"x-test-user": user}


async def _create(client, user="alice", title="My session", target="T1"):
    r = await client.post("/ide/api/sessions", json={"title": title, "target": target},
                          headers=_as(user))
    assert r.status_code == 201, r.text
    return r.json()


# --- principal ------------------------------------------------------------


def test_principal_from_claims_and_401_when_empty():
    assert _principal({"user_name": "alice"}) == "alice"
    with pytest.raises(HTTPException) as exc:
        _principal({})
    assert exc.value.status_code == 401


async def test_route_without_identity_is_401(client):
    r = await client.get("/ide/api/sessions", headers=_as("nameless"))
    assert r.status_code == 401


async def test_current_principal_wins_over_claims_user_name(client):
    headers = {**_as("bob"), "x-test-principal": "alice"}
    r = await client.post("/ide/api/sessions", json={"title": "t", "target": "T1"},
                          headers=headers)
    assert r.status_code == 201 and r.json()["owner"] == "alice"
    sid = r.json()["id"]
    r = await client.get(f"/ide/api/sessions/{sid}", headers=_as("alice"))
    assert r.status_code == 200
    r = await client.get(f"/ide/api/sessions/{sid}", headers=_as("bob"))
    assert r.status_code == 404


async def test_unauthenticated_caller_is_refused(client):
    r = await client.get("/ide/api/sessions", headers=_as("nobody"))
    assert r.status_code == 403


# --- me -------------------------------------------------------------------


async def test_me_lists_targets(client):
    r = await client.get("/ide/api/me", headers=_as("alice"))
    assert r.status_code == 200
    body = r.json()
    assert body["principal"] == "alice"
    assert body["targets"] == ["T1"]
    assert isinstance(body["is_admin"], bool)


async def test_me_reports_diagnose_retention_days_default(client):
    r = await client.get("/ide/api/me", headers=_as("alice"))
    assert r.status_code == 200
    assert r.json()["diagnose_retention_days"] == 14


@pytest.mark.parametrize("days", [30, 0])
async def test_me_reports_diagnose_retention_days_override(client, monkeypatch, days):
    # 0 means diagnose sessions are kept until deleted; the UI says so.
    monkeypatch.setattr(ide_store, "IDE_DIAGNOSE_RETENTION_DAYS", days)
    r = await client.get("/ide/api/me", headers=_as("alice"))
    assert r.status_code == 200
    assert r.json()["diagnose_retention_days"] == days


# --- sessions CRUD ----------------------------------------------------------


async def test_create_list_get_patch_delete(client):
    s = await _create(client)
    assert s["stage"] == "chat" and s["status"] == "idle"
    assert s["owner"] == "alice" and s["target"] == "T1"
    sid = s["id"]

    second = await _create(client, title="Second")
    r = await client.get("/ide/api/sessions", headers=_as("alice"))
    assert r.status_code == 200
    assert [x["id"] for x in r.json()] == [second["id"], sid]

    r = await client.get(f"/ide/api/sessions/{sid}", headers=_as("alice"))
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == sid and body["artifacts"] == [] and body["files"] == []

    r = await client.patch(f"/ide/api/sessions/{sid}", json={"title": "Renamed"},
                           headers=_as("alice"))
    assert r.status_code == 200 and r.json()["title"] == "Renamed"

    r = await client.delete(f"/ide/api/sessions/{sid}", headers=_as("alice"))
    assert r.status_code == 204
    r = await client.get(f"/ide/api/sessions/{sid}", headers=_as("alice"))
    assert r.status_code == 404


async def test_list_only_own_sessions(client):
    await _create(client, user="alice")
    r = await client.get("/ide/api/sessions", headers=_as("bob"))
    assert r.status_code == 200 and r.json() == []


async def test_create_unknown_target_is_422(client):
    r = await client.post("/ide/api/sessions", json={"title": "x", "target": "NOPE"},
                          headers=_as("alice"))
    assert r.status_code == 422


@pytest.mark.parametrize("title", ["", "   ", "x" * 201])
async def test_create_invalid_title_is_422(client, title):
    r = await client.post("/ide/api/sessions", json={"title": title, "target": "T1"},
                          headers=_as("alice"))
    assert r.status_code == 422


async def test_delete_cascades_children(client):
    s = await _create(client)
    async with SessionLocal() as db:
        await add_message(db, s["id"], stage="chat", role="user", content="hi")
        await add_artifact(db, s["id"], stage="design", kind="design", content="d")
    r = await client.delete(f"/ide/api/sessions/{s['id']}", headers=_as("alice"))
    assert r.status_code == 204
    async with SessionLocal() as db:
        for model in (IdeMessage, IdeArtifact):
            rows = (await db.execute(model.__table__.select())).all()
            assert rows == []


async def test_delete_while_running_is_409(client):
    s = await _create(client)
    async with SessionLocal() as db:
        row = await db.get(IdeSession, s["id"])
        row.status = "running"
        await db.commit()
        await add_message(db, s["id"], stage="chat", role="user", content="hi")
    r = await client.delete(f"/ide/api/sessions/{s['id']}", headers=_as("alice"))
    assert r.status_code == 409 and r.json()["code"] == "run_in_progress"
    r = await client.get(f"/ide/api/sessions/{s['id']}/messages", headers=_as("alice"))
    assert r.status_code == 200 and len(r.json()) == 1


async def test_messages_use_content_field(client):
    s = await _create(client)
    async with SessionLocal() as db:
        await add_message(db, s["id"], stage="chat", role="user", content="hi")
        await add_message(db, s["id"], stage="chat", role="assistant",
                          content="hello", activity_json='{"events": []}')
    r = await client.get(f"/ide/api/sessions/{s['id']}/messages", headers=_as("alice"))
    assert r.status_code == 200
    msgs = r.json()
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "hi"), ("assistant", "hello")]
    keys = {"id", "stage", "role", "content", "created_at", "has_activity"}
    assert [set(m) for m in msgs] == [keys, keys]
    # The activity itself is served per message, not in the list.
    assert [m["has_activity"] for m in msgs] == [False, True]


# --- ownership: 404 on every {sid} route -----------------------------------

SID_ROUTES = [
    ("GET", "/ide/api/sessions/{sid}", None),
    ("PATCH", "/ide/api/sessions/{sid}", {"title": "stolen"}),
    ("DELETE", "/ide/api/sessions/{sid}", None),
    ("POST", "/ide/api/sessions/{sid}/approve", None),
    ("GET", "/ide/api/sessions/{sid}/artifacts", None),
    ("GET", "/ide/api/sessions/{sid}/artifacts/{aid}", None),
    ("GET", "/ide/api/sessions/{sid}/messages", None),
    ("POST", "/ide/api/sessions/{sid}/messages", {"text": "hi"}),
    ("POST", "/ide/api/sessions/{sid}/request-changes", {"note": "f"}),
    ("POST", "/ide/api/sessions/{sid}/cancel", None),
    ("POST", "/ide/api/sessions/{sid}/report", None),
    ("POST", "/ide/api/sessions/{sid}/handover", None),
]


@pytest.mark.parametrize("method,path,body", SID_ROUTES)
async def test_other_users_session_is_404(client, method, path, body):
    s = await _create(client, user="alice")
    async with SessionLocal() as db:
        art = await add_artifact(db, s["id"], stage="design", kind="design",
                                 content="secret design")
    url = path.format(sid=s["id"], aid=art.id)
    r = await client.request(method, url, json=body, headers=_as("bob"))
    assert r.status_code == 404, r.text
    assert "secret" not in r.text
    # alice's session is untouched
    r = await client.get(f"/ide/api/sessions/{s['id']}", headers=_as("alice"))
    assert r.status_code == 200
    assert r.json()["title"] == "My session" and r.json()["stage"] == "chat"


async def test_artifact_of_another_session_is_404(client):
    a = await _create(client, user="alice")
    b = await _create(client, user="alice", title="Other")
    async with SessionLocal() as db:
        art = await add_artifact(db, b["id"], stage="design", kind="design",
                                 content="d")
    r = await client.get(f"/ide/api/sessions/{a['id']}/artifacts/{art.id}",
                         headers=_as("alice"))
    assert r.status_code == 404


# --- artifacts --------------------------------------------------------------


async def test_artifacts_list_and_get(client):
    s = await _create(client)
    async with SessionLocal() as db:
        await add_artifact(db, s["id"], stage="design", kind="design", content="v1")
        v2 = await add_artifact(db, s["id"], stage="design", kind="design",
                                content="v2")
        await add_artifact(db, s["id"], stage="plan", kind="plan", content="p")
    r = await client.get(f"/ide/api/sessions/{s['id']}/artifacts?kind=design",
                         headers=_as("alice"))
    assert r.status_code == 200
    assert [a["version"] for a in r.json()] == [2, 1]
    assert r.json()[0]["content"] == "v2"

    r = await client.get(f"/ide/api/sessions/{s['id']}/artifacts/{v2.id}",
                         headers=_as("alice"))
    assert r.status_code == 200 and r.json()["content"] == "v2"

    r = await client.get(f"/ide/api/sessions/{s['id']}", headers=_as("alice"))
    summaries = r.json()["artifacts"]
    assert len(summaries) == 3
    assert all("content" not in a for a in summaries)


# --- approve ----------------------------------------------------------------


async def test_approve_moves_one_stage(client):
    s = await _create(client)
    r = await client.post(f"/ide/api/sessions/{s['id']}/approve", headers=_as("alice"))
    assert r.status_code == 200 and r.json()["stage"] == "design"


async def test_approve_gate_refusal_is_409_with_code(client):
    s = await _create(client)
    await client.post(f"/ide/api/sessions/{s['id']}/approve", headers=_as("alice"))
    r = await client.post(f"/ide/api/sessions/{s['id']}/approve", headers=_as("alice"))
    assert r.status_code == 409
    assert r.json()["code"] == "missing_artifact"
    assert r.json()["detail"]


async def test_approve_while_running_is_409(client):
    s = await _create(client)
    async with SessionLocal() as db:
        row = await db.get(IdeSession, s["id"])
        row.status = "running"
        await db.commit()
    r = await client.post(f"/ide/api/sessions/{s['id']}/approve", headers=_as("alice"))
    assert r.status_code == 409 and r.json()["code"] == "run_in_progress"


async def test_approve_unknown_stage_is_409(client):
    s = await _create(client)
    async with SessionLocal() as db:
        row = await db.get(IdeSession, s["id"])
        row.stage = "bogus"
        await db.commit()
    r = await client.post(f"/ide/api/sessions/{s['id']}/approve", headers=_as("alice"))
    assert r.status_code == 409 and r.json()["code"] == "invalid_stage"


# --- conventions ------------------------------------------------------------


async def test_conventions_read_as_developer(client):
    r = await client.get("/ide/api/conventions", headers=_as("alice"))
    assert r.status_code == 200 and [c["target"] for c in r.json()] == ["T1"]
    r = await client.get("/ide/api/conventions/T1", headers=_as("alice"))
    assert r.status_code == 200 and r.json()["namespace"] == "Z"
    r = await client.get("/ide/api/conventions/NOPE", headers=_as("alice"))
    assert r.status_code == 404


async def test_conventions_write_needs_admin(client):
    body = {"label": "Changed", "package": "ZPKG"}
    r = await client.put("/ide/api/conventions/T1", json=body, headers=_as("alice"))
    assert r.status_code == 403
    r = await client.put("/ide/api/conventions/T1", json=body, headers=_as("admin"))
    assert r.status_code == 200
    assert r.json()["label"] == "Changed" and r.json()["namespace"] == "Z"
    # D4: PUT no longer creates a target (POST /conventions does).
    r = await client.put("/ide/api/conventions/T2", json={"label": "New"},
                         headers=_as("admin"))
    assert r.status_code == 404 and r.json()["code"] == "unknown_target"


async def test_conventions_write_rejects_bad_values(client):
    r = await client.put("/ide/api/conventions/T1",
                         json={"clean_core_level": "Z"}, headers=_as("admin"))
    assert r.status_code == 422
    r = await client.put("/ide/api/conventions/T1", json={"bogus": 1},
                         headers=_as("admin"))
    assert r.status_code == 422


# --- admin overview ---------------------------------------------------------


async def test_admin_sessions_requires_admin(client):
    await _create(client, user="alice")
    r = await client.get("/ide/api/admin/sessions", headers=_as("alice"))
    assert r.status_code == 403


async def test_admin_sessions_metadata_only(client):
    s = await _create(client, user="alice")
    async with SessionLocal() as db:
        await add_message(db, s["id"], stage="chat", role="user", content="secret")
        await add_artifact(db, s["id"], stage="design", kind="design",
                           content="secret")
    r = await client.get("/ide/api/admin/sessions", headers=_as("admin"))
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert set(rows[0]) == {"id", "owner", "title", "target", "type", "stage", "status",
                            "created_at", "updated_at"}
    assert "secret" not in r.text


# --- diagnose sessions (phase 1c): create, me, conventions, handover ----------

from sqlalchemy import func, select  # noqa: E402

from agents.ide.stages import build_prompt  # noqa: E402


async def _flag(target: str = "T1", value: bool = True) -> None:
    async with SessionLocal() as db:
        await upsert_conventions(db, target, actor="test-admin", non_production=value)


async def _count(model) -> int:
    async with SessionLocal() as db:
        return (await db.execute(select(func.count()).select_from(model))).scalar_one()


async def _diagnose(client, user="alice", title="Dump in ZREPORT", target="T1"):
    r = await client.post(
        "/ide/api/sessions",
        json={"title": title, "target": target, "type": "diagnose"},
        headers=_as(user),
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_create_diagnose_requires_non_production_target(client):
    body = {"title": "d", "target": "T1", "type": "diagnose"}
    r = await client.post("/ide/api/sessions", json=body, headers=_as("alice"))
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "target_not_non_production"
    assert await _count(IdeSession) == 0
    # The flag is the server's, never the caller's.
    r = await client.post("/ide/api/sessions", json={**body, "non_production": True},
                          headers=_as("alice"))
    assert r.status_code == 422 and await _count(IdeSession) == 0
    # A target without conventions stays "unknown", whatever the type.
    r = await client.post("/ide/api/sessions", json={**body, "target": "NOPE"},
                          headers=_as("alice"))
    assert r.status_code == 422 and "code" not in r.json()
    r = await client.post("/ide/api/sessions", json={**body, "type": "other"},
                          headers=_as("alice"))
    assert r.status_code == 422 and await _count(IdeSession) == 0


async def test_create_diagnose_flag_is_reread_on_every_create(client):
    await _flag()
    await _diagnose(client)
    await _flag(value=False)
    r = await client.post("/ide/api/sessions",
                          json={"title": "d", "target": "T1", "type": "diagnose"},
                          headers=_as("alice"))
    assert r.status_code == 422 and r.json()["code"] == "target_not_non_production"
    assert await _count(IdeSession) == 1


async def test_create_diagnose_ok(client):
    await _flag()
    s = await _diagnose(client)
    assert s["type"] == "diagnose" and s["stage"] == "investigate"
    assert s["owner"] == "alice" and s["target"] == "T1" and s["status"] == "idle"
    assert s["target_non_production"] is True and "masked" not in s
    r = await client.get(f"/ide/api/sessions/{s['id']}", headers=_as("alice"))
    assert r.status_code == 200 and r.json()["type"] == "diagnose"
    assert r.json()["target_non_production"] is True
    r = await client.get("/ide/api/sessions", headers=_as("alice"))
    assert [(x["type"], x["target_non_production"]) for x in r.json()] == [
        ("diagnose", True)]
    # The flag is read per response: taken away, the session reads as production.
    await _flag(value=False)
    r = await client.get(f"/ide/api/sessions/{s['id']}", headers=_as("alice"))
    assert r.json()["target_non_production"] is False
    r = await client.get("/ide/api/sessions", headers=_as("alice"))
    assert r.json()[0]["target_non_production"] is False


async def test_create_defaults_to_change_and_accepts_explicit_change(client):
    s = await _create(client)
    assert s["type"] == "change" and s["stage"] == "chat"
    assert s["target_non_production"] is False
    r = await client.post("/ide/api/sessions",
                          json={"title": "c", "target": "T1", "type": "change"},
                          headers=_as("alice"))
    assert r.status_code == 201 and r.json()["type"] == "change"


async def test_me_lists_diagnose_targets(client):
    async with SessionLocal() as db:
        await upsert_conventions(db, "T2", label="Two", actor="test-admin", non_production=True)
        await upsert_conventions(db, "T3", label="Three")
    r = await client.get("/ide/api/me", headers=_as("alice"))
    assert r.status_code == 200
    assert r.json()["targets"] == ["T1", "T2", "T3"]
    assert r.json()["diagnose_targets"] == ["T2"]


async def test_conventions_non_production_admin_only(client):
    r = await client.get("/ide/api/conventions/T1", headers=_as("alice"))
    assert r.json()["non_production"] is False
    r = await client.put("/ide/api/conventions/T1", json={"non_production": True},
                         headers=_as("alice"))
    assert r.status_code == 403
    r = await client.get("/ide/api/conventions/T1", headers=_as("alice"))
    assert r.json()["non_production"] is False
    r = await client.get("/ide/api/me", headers=_as("alice"))
    assert r.json()["diagnose_targets"] == []

    r = await client.put("/ide/api/conventions/T1", json={"non_production": True},
                         headers=_as("admin"))
    assert r.status_code == 200 and r.json()["non_production"] is True
    # Omitted: the stored value stays.
    r = await client.put("/ide/api/conventions/T1", json={"label": "L"},
                         headers=_as("admin"))
    assert r.status_code == 200 and r.json()["non_production"] is True
    r = await client.get("/ide/api/conventions", headers=_as("alice"))
    assert [c["non_production"] for c in r.json()] == [True]
    r = await client.put("/ide/api/conventions/T1", json={"non_production": False},
                         headers=_as("admin"))
    assert r.status_code == 200 and r.json()["non_production"] is False


@pytest.mark.parametrize("value", ["true", "yes", 1, 0, [True], {"a": 1}])
async def test_conventions_non_production_must_be_a_real_boolean(client, value):
    r = await client.put("/ide/api/conventions/T1", json={"non_production": value},
                         headers=_as("admin"))
    assert r.status_code == 422, r.text
    r = await client.get("/ide/api/conventions/T1", headers=_as("alice"))
    assert r.json()["non_production"] is False


async def test_handover_creates_change_session_with_report(client):
    await _flag()
    d = await _diagnose(client, title="Dump in ZREPORT")
    async with SessionLocal() as db:
        await add_artifact(db, d["id"], stage="investigate", kind="report",
                           content="OLD REPORT")
        await add_artifact(db, d["id"], stage="investigate", kind="report",
                           content="## Summary\nDivision by zero in ZREPORT")
        await add_message(db, d["id"], stage="investigate", role="user",
                          content="private investigation chat")
        db.add(IdeWorkspaceFile(session_id=d["id"], path="src/zreport.prog.abap",
                                state="read", origin_source="REPORT zreport."))
        await db.commit()

    r = await client.post(f"/ide/api/sessions/{d['id']}/handover",
                          headers=_as("alice"))
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["id"] != d["id"]
    assert new["type"] == "change" and new["stage"] == "chat"
    assert new["owner"] == "alice" and new["target"] == "T1"
    assert new["title"] == "Change: Dump in ZREPORT" and new["status"] == "idle"

    r = await client.get(f"/ide/api/sessions/{new['id']}", headers=_as("alice"))
    body = r.json()
    assert [(a["kind"], a["stage"], a["version"]) for a in body["artifacts"]] == [
        ("report", "chat", 1)]
    assert body["files"] == []  # only the report travels
    r = await client.get(f"/ide/api/sessions/{new['id']}/artifacts?kind=report",
                         headers=_as("alice"))
    assert [a["content"] for a in r.json()] == [
        "## Summary\nDivision by zero in ZREPORT"]
    r = await client.get(f"/ide/api/sessions/{new['id']}/messages",
                         headers=_as("alice"))
    assert [(m["stage"], m["role"], m["content"]) for m in r.json()] == [
        ("chat", "system",
         "Started from diagnose session 'Dump in ZREPORT' (report v2).")]
    assert "private investigation chat" not in r.text

    # The new session is the owner's alone; the diagnose session is unchanged.
    r = await client.get(f"/ide/api/sessions/{new['id']}", headers=_as("bob"))
    assert r.status_code == 404
    r = await client.get(f"/ide/api/sessions/{d['id']}", headers=_as("alice"))
    assert r.json()["type"] == "diagnose" and r.json()["stage"] == "investigate"
    assert len(r.json()["artifacts"]) == 2

    # The change session's chat prompt shows the handed-over report.
    async with SessionLocal() as db:
        row = await db.get(IdeSession, new["id"])
        _, prompt = await build_prompt(db, row, "Fix it")
    assert "### Diagnosis report (version 1)\n## Summary\nDivision by zero" in prompt
    assert "OLD REPORT" not in prompt


async def test_handover_owner_is_the_caller_not_the_token_name(client):
    await _flag()
    headers = {**_as("bob"), "x-test-principal": "alice"}
    d = await _diagnose(client)
    async with SessionLocal() as db:
        await add_artifact(db, d["id"], stage="investigate", kind="report", content="R")
    r = await client.post(f"/ide/api/sessions/{d['id']}/handover", headers=headers)
    assert r.status_code == 201 and r.json()["owner"] == "alice"


async def test_handover_title_is_cut_to_200(client):
    await _flag()
    d = await _diagnose(client, title="x" * 200)
    async with SessionLocal() as db:
        await add_artifact(db, d["id"], stage="investigate", kind="report", content="R")
    r = await client.post(f"/ide/api/sessions/{d['id']}/handover",
                          headers=_as("alice"))
    assert r.status_code == 201
    assert r.json()["title"] == ("Change: " + "x" * 200)[:200]


async def test_handover_refusals(client):
    await _flag()
    before = None

    async def refused(sid, code, user="alice"):
        r = await client.post(f"/ide/api/sessions/{sid}/handover", headers=_as(user))
        assert r.status_code == 409, r.text
        assert r.json()["code"] == code
        assert await _count(IdeSession) == before

    # A change session, even one that holds a report.
    c = await _create(client)
    async with SessionLocal() as db:
        await add_artifact(db, c["id"], stage="chat", kind="report", content="R")
    d = await _diagnose(client)
    before = await _count(IdeSession)
    await refused(c["id"], "not_diagnose")

    # No report yet (another kind does not count).
    async with SessionLocal() as db:
        await add_artifact(db, d["id"], stage="investigate", kind="note", content="N")
    await refused(d["id"], "missing_artifact")

    # Another user's session: 404 before any gate, with or without a report.
    async with SessionLocal() as db:
        await add_artifact(db, d["id"], stage="investigate", kind="report",
                           content="secret report")
    r = await client.post(f"/ide/api/sessions/{d['id']}/handover", headers=_as("bob"))
    assert r.status_code == 404 and "secret" not in r.text
    r = await client.post("/ide/api/sessions/nope/handover", headers=_as("alice"))
    assert r.status_code == 404
    assert await _count(IdeSession) == before
    r = await client.get("/ide/api/sessions", headers=_as("bob"))
    assert r.json() == []

    # While a run is in progress.
    async with SessionLocal() as db:
        row = await db.get(IdeSession, d["id"])
        row.status = "running"
        await db.commit()
    await refused(d["id"], "run_in_progress")
    assert await _count(IdeArtifact) == 3


async def test_handover_refused_when_target_lost_non_production(client):
    """The report of a raw run must not move into a change session on a
    target that is now treated as production."""
    await _flag()
    d = await _diagnose(client)
    async with SessionLocal() as db:
        await add_artifact(db, d["id"], stage="investigate", kind="report",
                           content="raw report")
    await _flag(value=False)
    before = (await _count(IdeSession), await _count(IdeArtifact),
              await _count(IdeMessage))
    r = await client.post(f"/ide/api/sessions/{d['id']}/handover",
                          headers=_as("alice"))
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "target_not_non_production"
    assert "raw report" not in r.text
    assert (await _count(IdeSession), await _count(IdeArtifact),
            await _count(IdeMessage)) == before

    # Conventions gone altogether read the same way.
    async with SessionLocal() as db:
        await db.execute(IdeConventions.__table__.delete())
        await db.commit()
    r = await client.post(f"/ide/api/sessions/{d['id']}/handover",
                          headers=_as("alice"))
    assert r.status_code == 409 and r.json()["code"] == "target_not_non_production"

    # A change session still answers not_diagnose first; another user 404.
    await _flag(value=False)
    c = await _create(client)
    r = await client.post(f"/ide/api/sessions/{c['id']}/handover", headers=_as("alice"))
    assert r.json()["code"] == "not_diagnose"
    r = await client.post(f"/ide/api/sessions/{d['id']}/handover", headers=_as("bob"))
    assert r.status_code == 404

    # Flagged again: the handover works.
    await _flag()
    r = await client.post(f"/ide/api/sessions/{d['id']}/handover",
                          headers=_as("alice"))
    assert r.status_code == 201, r.text


async def test_handover_needs_the_developer_scope(client):
    r = await client.post("/ide/api/sessions/x/handover", headers=_as("nobody"))
    assert r.status_code == 403
    r = await client.post("/ide/api/sessions/x/report", headers=_as("nobody"))
    assert r.status_code == 403


async def test_report_on_a_change_session_is_409_json(client):
    s = await _create(client)
    r = await client.post(f"/ide/api/sessions/{s['id']}/report", headers=_as("alice"))
    assert r.status_code == 409 and r.json()["code"] == "not_diagnose"
    assert r.headers["content-type"].startswith("application/json")
    assert await _count(IdeMessage) == 0


# --- worklist fields (Task B7): waiting, open_comments, unresolved_comments ------

from sqlalchemy import event  # noqa: E402

from agents.db import engine  # noqa: E402

PROP_PATH = "src/CLAS/zcl_demo.clas.abap"


async def _worklist_session(user: str, *, session_type: str = "change",
                            stage: str = "chat") -> str:
    async with SessionLocal() as db:
        row = IdeSession(owner=user, title=f"{session_type}-{stage}", target="T1",
                         session_type=session_type, stage=stage, status="idle")
        db.add(row)
        await db.commit()
        return row.id


async def _comment(sid: str, state: str) -> None:
    async with SessionLocal() as db:
        db.add(IdeComment(session_id=sid, anchor="document", kind="design",
                          version=1, paragraph=0, body="please rename", state=state))
        await db.commit()


async def _worklist_fixture() -> dict[str, str]:
    """One alice session per ``waiting`` reason, one without, one of bob's."""
    await _flag()
    ids = {
        "approval": await _worklist_session("alice", session_type="diagnose",
                                            stage="investigate"),
        "comments": await _worklist_session("alice", stage="chat"),
        "changes": await _worklist_session("alice", stage="propose"),
        "document": await _worklist_session("alice", stage="design"),
        "none": await _worklist_session("alice", stage="chat"),
        "bob": await _worklist_session("bob", stage="design"),
    }
    async with SessionLocal() as db:
        db.add(IdeApproval(session_id=ids["approval"], action="trace_start",
                           params_json="{}", status="pending"))
        db.add(IdeWorkspaceFile(session_id=ids["changes"], path=PROP_PATH,
                                object_type="CLAS", object_name="ZCL_DEMO",
                                state="new", proposed_source="CLASS zcl_demo.",
                                revision=1))
        await db.commit()
    for sid in (ids["document"], ids["bob"]):
        async with SessionLocal() as db:
            await add_artifact(db, sid, stage="design", kind="design", content="d1")
    await _comment(ids["comments"], "addressed")
    await _comment(ids["comments"], "open")
    await _comment(ids["none"], "open")
    await _comment(ids["none"], "open")
    await _comment(ids["none"], "sent")
    await _comment(ids["none"], "dismissed")
    await _comment(ids["bob"], "open")
    return ids


async def test_list_sessions_carries_worklist_fields(client):
    ids = await _worklist_fixture()
    r = await client.get("/ide/api/sessions", headers=_as("alice"))
    assert r.status_code == 200, r.text
    got = {s["id"]: (s["waiting"], s["open_comments"], s["unresolved_comments"])
           for s in r.json()}
    assert got == {
        ids["approval"]: ("approval", 0, 0),
        ids["comments"]: ("comments", 1, 1),
        ids["changes"]: ("changes", 0, 0),
        ids["document"]: ("document", 0, 0),
        ids["none"]: (None, 2, 3),
    }
    # Bob sees only his own session, with his own counts.
    r = await client.get("/ide/api/sessions", headers=_as("bob"))
    assert [(s["id"], s["waiting"], s["open_comments"]) for s in r.json()] == [
        (ids["bob"], "document", 1)]


async def test_get_session_carries_worklist_fields(client):
    ids = await _worklist_fixture()
    for reason in ("approval", "comments", "changes", "document"):
        r = await client.get(f"/ide/api/sessions/{ids[reason]}", headers=_as("alice"))
        assert r.status_code == 200 and r.json()["waiting"] == reason
    r = await client.get(f"/ide/api/sessions/{ids['none']}", headers=_as("alice"))
    assert (r.json()["waiting"], r.json()["open_comments"],
            r.json()["unresolved_comments"]) == (None, 2, 3)
    # PATCH answers the same Session shape.
    r = await client.patch(f"/ide/api/sessions/{ids['none']}", json={"title": "x"},
                           headers=_as("alice"))
    assert r.status_code == 200 and r.json()["unresolved_comments"] == 3
    r = await client.get(f"/ide/api/sessions/{ids['bob']}", headers=_as("alice"))
    assert r.status_code == 404


async def test_list_sessions_uses_a_fixed_number_of_queries(client):
    await _worklist_fixture()
    for _ in range(10):
        await _worklist_session("alice", stage="design")

    statements: list[str] = []

    def _count_select(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _count_select)
    try:
        r = await client.get("/ide/api/sessions", headers=_as("alice"))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _count_select)
    assert r.status_code == 200 and len(r.json()) == 15
    # sessions + conventions + waiting_for (<= 4) + comment counts (1)
    # + object summaries (1) + findings counts (1, only with a diagnose
    # session in the list).
    assert len(statements) <= 9, statements


async def test_admin_sessions_stay_metadata_only_with_worklist(client):
    await _worklist_fixture()
    r = await client.get("/ide/api/admin/sessions", headers=_as("admin"))
    assert r.status_code == 200
    for row in r.json():
        assert set(row) == {"id", "owner", "title", "target", "type", "stage",
                            "status", "created_at", "updated_at"}
    assert "please rename" not in r.text


async def test_revise_route_is_removed(client):
    """B9: ``/revise`` is replaced by ``/request-changes``; the old path no
    longer exists for anyone (404/405, never a run)."""
    s = await _create(client, user="alice")
    r = await client.post(f"/ide/api/sessions/{s['id']}/revise",
                          json={"feedback": "f"}, headers=_as("alice"))
    assert r.status_code in (404, 405)
    assert "/ide/api/sessions/{sid}/revise" not in [r.path for r in ide_router.routes]


# --- timestamps: always UTC with an explicit offset ----------------------
#
# SQLite hands ``DateTime(timezone=True)`` back naive. Every stored value is
# UTC, so a naive one must still be served with an offset: a browser reads an
# offset-less ISO string as local time.


def _iso_helpers():
    from agents.ide import approvals, findings, models, routes

    return {
        "models.iso_utc": lambda v: models.iso_utc(v),
        "routes._iso": routes._iso,
        "approvals._iso": approvals._iso,
        "findings.finding_out": lambda v: findings.finding_out(
            type("Row", (), {"id": "f", "kind": "dump", "ref_id": "r",
                             "title": "t", "program": None, "include": None,
                             "line": None, "occurred_at": None,
                             "created_at": v})()
        )["created_at"],
        "IdeSession.meta": lambda v: IdeSession(
            id="s", owner="o", title="t", target="T1", created_at=v,
            updated_at=v,
        ).meta()["updated_at"],
    }


_ISO_HELPERS = ["models.iso_utc", "routes._iso", "approvals._iso",
                "findings.finding_out", "IdeSession.meta"]


@pytest.mark.parametrize("name", _ISO_HELPERS)
def test_naive_datetime_is_served_as_utc_with_offset(name):
    from datetime import datetime

    out = _iso_helpers()[name](datetime(2026, 10, 4, 12, 30, 5, 123456))
    assert out == "2026-10-04T12:30:05.123456+00:00", (name, out)


@pytest.mark.parametrize("name", _ISO_HELPERS)
def test_aware_datetime_is_served_unchanged(name):
    from datetime import datetime, timedelta, timezone

    value = datetime(2026, 10, 4, 14, 30, 5, tzinfo=timezone(timedelta(hours=2)))
    assert _iso_helpers()[name](value) == value.isoformat(), name
    assert _iso_helpers()[name](None) is None, name


async def test_fresh_session_updated_at_parses_to_now_utc(client):
    from datetime import datetime, timezone

    sid = (await _create(client))["id"]
    for url in ("/ide/api/sessions", f"/ide/api/sessions/{sid}"):
        r = await client.get(url, headers=_as("alice"))
        assert r.status_code == 200, r.text
        body = r.json()
        row = body[0] if isinstance(body, list) else body
        row = row.get("session", row)
        stamp = datetime.fromisoformat(row["updated_at"])
        assert stamp.tzinfo is not None, row["updated_at"]
        assert abs((datetime.now(timezone.utc) - stamp).total_seconds()) < 5
