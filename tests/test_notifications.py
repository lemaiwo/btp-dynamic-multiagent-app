"""Finished-run notifications: ``/admin/api/notifications``.

The list is derived from ``job_runs`` and ``workflow_runs``; the only stored
state is one read marker per admin (``admin_notification_state``). Most tests
mount the router on a bare app, where the test binds ``current_principal``
itself (the real app's middleware binds it from the request, and without an
XSUAA binding that is always the same local principal). One test drives the
real app, so the include line in ``agents/admin.py`` is covered too.

Run:  python -m pytest tests/test_notifications.py -q
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import app as app_module  # noqa: E402
from agents import auth, notifications  # noqa: E402
from agents.db import (  # noqa: E402
    AdminNotificationState,
    JobRun,
    SessionLocal,
    WorkflowRun,
    init_db,
)

LIST = "/admin/api/notifications"
SEEN = "/admin/api/notifications/seen"
FIXED = {"detail": "up_to must be a timestamp with a time zone"}
ITEM_KEYS = {
    "kind", "run_id", "name", "status", "trigger", "created_by", "finished_at", "unread",
}

# Never part of an answer: what a refused body carried.
SECRET = "S3cr3t_Zx9!tok"


def now() -> datetime:
    return datetime.now(timezone.utc)


def ago(**delta: float) -> datetime:
    return now() - timedelta(**delta)


def parsed(value: str) -> datetime:
    """A timestamp of an answer: always aware, always UTC."""
    stamp = datetime.fromisoformat(value)
    assert stamp.tzinfo is not None, value
    assert stamp.utcoffset() == timedelta(0), value
    return stamp


async def agent_run(finished_at: datetime | None, name: str = "nightly", **patch: Any) -> str:
    run_id = str(uuid.uuid4())
    values: dict[str, Any] = {
        "id": run_id, "agent_id": 1, "agent_name": name, "trigger": "manual",
        "status": "success", "finished_at": finished_at, "created_by": "someone",
        # Never selected by the routes.
        "summary": SECRET, "report_json": SECRET, "activity_json": SECRET, "error": SECRET,
    }
    values.update(patch)
    async with SessionLocal() as session:
        session.add(JobRun(**values))
        await session.commit()
    return run_id


async def workflow_run(finished_at: datetime | None, name: str = "intake", **patch: Any) -> str:
    run_id = str(uuid.uuid4())
    values: dict[str, Any] = {
        "id": run_id, "workflow_id": 1, "workflow_name": name, "trigger": "schedule",
        "status": "failed", "finished_at": finished_at, "created_by": None,
        "summary": SECRET, "error": SECRET,
    }
    values.update(patch)
    async with SessionLocal() as session:
        session.add(WorkflowRun(**values))
        await session.commit()
    return run_id


async def set_marker(principal: str, seen_at: datetime) -> None:
    async with SessionLocal() as session:
        await session.execute(
            delete(AdminNotificationState).where(AdminNotificationState.principal == principal)
        )
        session.add(AdminNotificationState(principal=principal, seen_at=seen_at))
        await session.commit()


async def markers() -> dict[str, datetime]:
    async with SessionLocal() as session:
        rows = (await session.execute(select(AdminNotificationState))).scalars().all()
    return {row.principal: row.seen_at.replace(tzinfo=timezone.utc) for row in rows}


@pytest.fixture(autouse=True)
async def _db():
    await init_db()
    async with SessionLocal() as session:
        for model in (JobRun, WorkflowRun, AdminNotificationState):
            await session.execute(delete(model))
        await session.commit()
    yield


@pytest.fixture
async def client():
    bare = FastAPI()
    bare.include_router(notifications.router, prefix="/admin")
    async with AsyncClient(transport=ASGITransport(app=bare), base_url="http://test") as c:
        yield c


async def call(client, method: str, url: str, principal: str | None = "alice", **kwargs: Any):
    """One request with ``principal`` bound, as the app's middleware would."""
    token = auth.current_principal.set(principal)
    try:
        return await client.request(method, url, **kwargs)
    finally:
        auth.current_principal.reset(token)


async def listing(client, principal: str | None = "alice") -> dict[str, Any]:
    r = await call(client, "GET", LIST, principal)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"items", "unread_count", "seen_at"}
    for item in body["items"]:
        assert set(item) == ITEM_KEYS
        parsed(item["finished_at"])
    parsed(body["seen_at"])
    return body


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------
async def test_both_kinds_are_merged_newest_first(client):
    a_old = await agent_run(ago(hours=4))
    w_mid = await workflow_run(ago(hours=3))
    a_new = await agent_run(ago(hours=2), name="report", status="failed", trigger="schedule",
                            created_by=None)
    w_new = await workflow_run(ago(hours=1), status="success", trigger="manual",
                               created_by="bob")
    body = await listing(client)
    assert [(i["kind"], i["run_id"]) for i in body["items"]] == [
        ("workflow", w_new), ("agent", a_new), ("workflow", w_mid), ("agent", a_old),
    ]
    assert body["items"][0] == {
        "kind": "workflow", "run_id": w_new, "name": "intake", "status": "success",
        "trigger": "manual", "created_by": "bob", "unread": False,
        "finished_at": body["items"][0]["finished_at"],
    }
    assert body["items"][1]["name"] == "report"
    assert body["items"][1]["status"] == "failed"
    assert body["items"][1]["trigger"] == "schedule"
    assert body["items"][1]["created_by"] is None


async def test_a_run_that_has_not_finished_is_not_listed(client):
    await agent_run(None, status="running")
    await workflow_run(None, status="running")
    done = await agent_run(ago(minutes=5))
    body = await listing(client)
    assert [i["run_id"] for i in body["items"]] == [done]


async def test_only_the_last_seven_days_are_listed_and_counted(client):
    await set_marker("alice", ago(days=30))
    await agent_run(ago(days=7, minutes=5))
    await workflow_run(ago(days=8))
    inside_a = await agent_run(ago(days=6, hours=23))
    inside_w = await workflow_run(ago(days=1))
    body = await listing(client)
    assert [i["run_id"] for i in body["items"]] == [inside_w, inside_a]
    assert body["unread_count"] == 2


async def test_at_most_fifty_items_in_total(client):
    await set_marker("alice", ago(days=1))
    agents = [await agent_run(ago(minutes=2 * n + 1)) for n in range(40)]
    workflows = [await workflow_run(ago(minutes=2 * n + 2)) for n in range(40)]
    body = await listing(client)
    expected = [run for pair in zip(agents, workflows) for run in pair][:50]
    assert [i["run_id"] for i in body["items"]] == expected
    # More unread than listed: the count is of the window, not of the page.
    assert all(i["unread"] for i in body["items"])
    assert body["unread_count"] == 80


async def test_fifty_of_one_kind_do_not_hide_the_other(client):
    await set_marker("alice", ago(days=1))
    newest = await workflow_run(ago(minutes=1))
    for n in range(55):
        await agent_run(ago(minutes=10 + n))
    body = await listing(client)
    assert len(body["items"]) == 50
    assert body["items"][0]["run_id"] == newest
    assert body["unread_count"] == 56


async def test_unread_is_finished_after_the_marker(client):
    seen = ago(hours=2)
    await set_marker("alice", seen)
    before = await agent_run(ago(hours=3))
    exactly = await workflow_run(seen)
    after_a = await agent_run(ago(hours=1))
    after_w = await workflow_run(ago(minutes=30))
    body = await listing(client)
    unread = {i["run_id"]: i["unread"] for i in body["items"]}
    assert unread == {after_w: True, after_a: True, exactly: False, before: False}
    assert body["unread_count"] == 2
    assert parsed(body["seen_at"]) == seen
    # Reading the list does not move the marker.
    assert (await markers())["alice"] == seen


async def test_the_first_call_sets_the_marker_to_now(client):
    await agent_run(ago(hours=1))
    await workflow_run(ago(minutes=1))
    assert await markers() == {}
    start = now()
    body = await listing(client)
    end = now()
    assert body["unread_count"] == 0
    assert [i["unread"] for i in body["items"]] == [False, False]
    assert start <= parsed(body["seen_at"]) <= end
    stored = await markers()
    assert stored == {"alice": parsed(body["seen_at"])}
    # A run that finishes afterwards is unread, and the marker stays.
    later = await agent_run(now() + timedelta(milliseconds=5))
    again = await listing(client)
    assert again["seen_at"] == body["seen_at"]
    assert again["unread_count"] == 1
    assert again["items"][0] == {**again["items"][0], "run_id": later, "unread": True}


async def test_two_principals_have_separate_markers(client):
    await set_marker("alice", ago(hours=5))
    await set_marker("bob", ago(hours=1))
    await agent_run(ago(hours=2))
    assert (await listing(client, "alice"))["unread_count"] == 1
    assert (await listing(client, "bob"))["unread_count"] == 0
    newest = (await listing(client, "alice"))["items"][0]["finished_at"]
    r = await call(client, "POST", SEEN, "alice", json={"up_to": newest})
    assert r.status_code == 200
    assert (await listing(client, "alice"))["unread_count"] == 0
    stored = await markers()
    assert stored["alice"] == parsed(newest)
    assert stored["bob"] != stored["alice"]


async def test_without_a_principal_the_marker_is_keyed_local(client):
    body = await listing(client, None)
    assert await markers() == {"local": parsed(body["seen_at"])}
    r = await call(client, "POST", SEEN, None, json={"up_to": ago(hours=1).isoformat()})
    assert r.status_code == 200
    assert set(await markers()) == {"local"}


async def test_the_principal_is_never_taken_from_the_request(client):
    r = await call(
        client, "GET", f"{LIST}?principal=mallory", "alice",
        headers={"X-Principal": "mallory", "X-Forwarded-User": "mallory"},
    )
    assert r.status_code == 200
    assert set(await markers()) == {"alice"}


async def test_a_naive_finished_at_is_answered_as_aware_utc(client):
    """SQLite and SAP HANA hand a timestamp back without an offset (UTC)."""
    finished = (now() - timedelta(hours=1)).replace(tzinfo=None, microsecond=123456)
    await agent_run(finished)
    await workflow_run(finished - timedelta(minutes=1))
    body = await listing(client)
    stamp = body["items"][0]["finished_at"]
    assert stamp == finished.replace(tzinfo=timezone.utc).isoformat()
    assert stamp.endswith("+00:00")
    assert body["items"][1]["finished_at"].endswith("+00:00")
    assert body["seen_at"].endswith("+00:00")


async def test_no_other_column_of_a_run_is_answered(client):
    await agent_run(ago(minutes=3))
    await workflow_run(ago(minutes=2))
    r = await call(client, "GET", LIST)
    assert r.status_code == 200
    assert SECRET not in r.text


# ---------------------------------------------------------------------------
# The marker
# ---------------------------------------------------------------------------
async def test_seen_moves_the_marker_to_up_to(client):
    await set_marker("alice", ago(hours=5))
    await agent_run(ago(hours=3))
    await workflow_run(ago(hours=1))
    body = await listing(client)
    assert body["unread_count"] == 2
    older = body["items"][1]["finished_at"]
    r = await call(client, "POST", SEEN, json={"up_to": older})
    assert r.status_code == 200, r.text
    assert r.json() == {"seen_at": older}
    body = await listing(client)
    assert [i["unread"] for i in body["items"]] == [True, False]
    assert body["unread_count"] == 1
    assert body["seen_at"] == older


async def test_the_marker_only_moves_forward(client):
    seen = ago(hours=1)
    await set_marker("alice", seen)
    r = await call(client, "POST", SEEN, json={"up_to": ago(hours=6).isoformat()})
    assert r.status_code == 200
    assert parsed(r.json()["seen_at"]) == seen
    assert (await markers())["alice"] == seen
    r = await call(client, "POST", SEEN, json={"up_to": seen.isoformat()})
    assert parsed(r.json()["seen_at"]) == seen


async def test_up_to_in_the_future_is_clamped_to_now(client):
    await set_marker("alice", ago(hours=1))
    start = now()
    r = await call(client, "POST", SEEN, json={"up_to": (now() + timedelta(days=2)).isoformat()})
    end = now()
    assert r.status_code == 200
    assert start <= parsed(r.json()["seen_at"]) <= end
    assert (await markers())["alice"] == parsed(r.json()["seen_at"])


async def test_up_to_with_another_offset_is_the_same_instant(client):
    await set_marker("alice", ago(hours=5))
    instant = ago(hours=2).replace(microsecond=0)
    local = instant.astimezone(timezone(timedelta(hours=2)))
    assert local.isoformat().endswith("+02:00")
    r = await call(client, "POST", SEEN, json={"up_to": local.isoformat()})
    assert r.status_code == 200
    assert r.json() == {"seen_at": instant.isoformat()}
    r = await call(client, "POST", SEEN, json={
        "up_to": (instant + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    })
    assert r.json() == {"seen_at": (instant + timedelta(minutes=1)).isoformat()}


async def test_seen_without_a_marker_sets_it_to_now(client):
    start = now()
    r = await call(client, "POST", SEEN, json={"up_to": ago(hours=3).isoformat()})
    end = now()
    assert r.status_code == 200
    assert start <= parsed(r.json()["seen_at"]) <= end
    assert await markers() == {"alice": parsed(r.json()["seen_at"])}


async def test_a_marker_inserted_meanwhile_is_read_not_overwritten(client, monkeypatch):
    """Two first calls of one admin at once: the loser of the insert reads
    the winner's row."""
    winner = ago(minutes=10)
    real = notifications._read_marker
    reads = 0

    async def racing(session, principal):
        nonlocal reads
        reads += 1
        found = await real(session, principal)
        if reads == 1:
            assert found is None
            await set_marker(principal, winner)
        return found

    monkeypatch.setattr(notifications, "_read_marker", racing)
    body = await listing(client)
    assert reads == 2
    assert parsed(body["seen_at"]) == winner
    assert await markers() == {"alice": winner}


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{}",
        b"null",
        b"[]",
        b'["' + SECRET.encode() + b'"]',
        b'"' + SECRET.encode() + b'"',
        b"{" + SECRET.encode(),
        b"\xff\xfe" + SECRET.encode(),
        b'{"up_to": null}',
        b'{"up_to": 1791547200}',
        b'{"up_to": true}',
        b'{"up_to": ["2026-10-09T12:00:00+00:00"]}',
        b'{"up_to": "' + SECRET.encode() + b'"}',
        b'{"up_to": ""}',
        b'{"up_to": "2026-10-09T12:00:00"}',
        b'{"up_to": "2026-10-09"}',
        b'{"up_to": "0001-01-01T00:00:00+14:00"}',
        b'{"up_to": "2026-10-09T12:00:00+00:00", "principal": "' + SECRET.encode() + b'"}',
        b'{"' + SECRET.encode() + b'": "2026-10-09T12:00:00+00:00"}',
        b'{"up_to": "2026-10-09T12:00:00+00:00", "pad": "' + b"x" * 5000 + b'"}',
    ],
)
async def test_any_other_body_is_refused_with_the_fixed_text(client, body):
    seen = ago(hours=4)
    await set_marker("alice", seen)
    r = await call(
        client, "POST", SEEN, content=body, headers={"content-type": "application/json"}
    )
    assert r.status_code == 422, r.text
    assert r.json() == FIXED
    assert SECRET not in r.text
    assert (await markers())["alice"] == seen


# ---------------------------------------------------------------------------
# Who may call
# ---------------------------------------------------------------------------
def test_every_route_requires_the_admin_scope():
    routes = notifications.router.routes
    assert {(r.path, m) for r in routes for m in r.methods} == {
        ("/api/notifications", "GET"), ("/api/notifications/seen", "POST"),
    }
    for route in routes:
        deps = [d.call for d in route.dependant.dependencies]
        assert auth.require_admin in deps, route.path


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


async def test_without_the_admin_scope_both_routes_are_refused(client, monkeypatch):
    validator = _StubValidator(
        {
            "usr": {"user_name": "u", "scope": ["user"]},
            "dev": {"user_name": "d", "scope": ["developer", "user", "a2a"]},
            "adm": {"user_name": "a", "scope": ["admin"]},
        }
    )
    monkeypatch.setattr(auth, "get_validator", lambda: validator)
    await agent_run(ago(minutes=1))
    token = auth.current_claims.set(None)
    try:
        for method, url, kwargs in (
            ("GET", LIST, {}),
            ("POST", SEEN, {"json": {"up_to": now().isoformat()}}),
        ):
            r = await call(client, method, url, "mallory", **kwargs)
            assert r.status_code == 401, (method, r.status_code)
            for bearer in ("usr", "dev"):
                r = await call(
                    client, method, url, "mallory",
                    headers={"Authorization": f"Bearer {bearer}"}, **kwargs,
                )
                assert r.status_code == 403, (method, bearer, r.status_code)
                assert r.json() == {"detail": "Admin scope required"}
        # A refusal reads and stores nothing.
        assert await markers() == {}
        r = await call(client, "GET", LIST, "alice", headers={"Authorization": "Bearer adm"})
        assert r.status_code == 200
        assert len(r.json()["items"]) == 1
    finally:
        auth.current_claims.reset(token)


# ---------------------------------------------------------------------------
# The real app
# ---------------------------------------------------------------------------
async def test_the_app_serves_both_routes_under_admin_api():
    paths = app_module.app.openapi()["paths"]
    served = {
        (path, method.upper())
        for path, item in paths.items()
        if "notifications" in path
        for method in item
    }
    assert served == {(LIST, "GET"), (SEEN, "POST")}
    run_id = await agent_run(ago(minutes=1))
    async with AsyncClient(
        transport=ASGITransport(app=app_module.app), base_url="http://test"
    ) as c:
        r = await c.get(LIST)
        assert r.status_code == 200, r.text
        assert [i["run_id"] for i in r.json()["items"]] == [run_id]
        assert r.json()["unread_count"] == 0
        r = await c.post(SEEN, json={"up_to": SECRET})
        assert r.status_code == 422
        assert r.json() == FIXED
        r = await c.post(SEEN, json={"up_to": r.headers.get("date") or ""})
        assert r.status_code == 422
        r = await c.post(SEEN, json={"up_to": now().isoformat()})
        assert r.status_code == 200, r.text
        parsed(r.json()["seen_at"])
    # One marker, for the principal the middleware bound.
    assert len(await markers()) == 1
