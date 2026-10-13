"""Stale-write protection of the OData catalogue (``expected_updated_at``).

A service holds the identity agents act under in SAP (``destination``,
``user_context``) and what they may write. With two admin tabs open, a save
from the older one must not replace what the newer one stored: the client
sends the ``updated_at`` it loaded and the route refuses when the row has
moved on.

SQLite has no row locks, so the Postgres side is covered the way the delete
route's lock is: the order of the calls, one session, and the statement as
Postgres would receive it.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import app as app_module  # noqa: E402
from agents import db as db_module  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    ODataAuditLog,
    ODataService,
    SessionLocal,
    get_odata_service,
    init_db,
    update_odata_service,
    validate_odata_service,
)

BASE = "/admin/api/odata/services"
ONE = f"{BASE}/stock-levels"
STALE = "Service 'stock-levels' was changed since it was loaded; reload it and save again"
FIELD = "expected_updated_at"

GOOD: dict[str, Any] = {
    "name": "stock-levels",
    "title": "Stock levels",
    "purpose": "Read material stock per plant",
    "destination": "S4_ODATA_USER",
    "user_context": True,
    "odata_version": "v2",
    "service_path": "/sap/opu/odata/sap/API_MATERIAL_STOCK_SRV",
    "definition": {
        "entity_sets": [
            {
                "name": "A_MatlStkInAcctMod",
                "title": "Stock",
                "keys": [{"name": "Material"}],
                "operations": ["list", "get"],
                "fields": [{"name": "Material", "selectable": True, "filterable": True}],
            }
        ],
        "operations": [],
    },
}

# Never part of an answer: what a refused value carried.
SECRET = "secret-token"


def good(**patch: Any) -> dict[str, Any]:
    data = copy.deepcopy(GOOD)
    data.update(patch)
    return data


def other_identity(**patch: Any) -> dict[str, Any]:
    """The save an older tab would send: another identity, an empty definition."""
    body = good(destination="S4_ODATA_TECH", user_context=False, **patch)
    body["definition"] = {"entity_sets": [], "operations": []}
    return body


def instant(text: str) -> datetime:
    """An emitted ``updated_at`` as an aware UTC instant (naive means UTC)."""
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


@pytest.fixture(autouse=True)
async def _clean_tables():
    await init_db()
    async with SessionLocal() as s:
        for model in (ODataService, ODataAuditLog, AgentConfig):
            await s.execute(delete(model))
        await s.commit()
    yield


@pytest.fixture
async def client():
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def created(client):
    r = await client.post(BASE, json=GOOD)
    assert r.status_code == 201, r.text
    return r.json()


# --- the update route -----------------------------------------------------------


async def test_a_matching_timestamp_updates_and_answers_a_newer_one(client, created):
    r = await client.put(ONE, json=other_identity(**{FIELD: created["updated_at"]}))
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["destination"] == "S4_ODATA_TECH" and got["user_context"] is False
    assert instant(got["updated_at"]) > instant(created["updated_at"])
    # The answer carries what GET and the list now serve.
    assert (await client.get(ONE)).json()["updated_at"] == got["updated_at"]
    listed = (await client.get(BASE)).json()
    assert [s["updated_at"] for s in listed] == [got["updated_at"]]
    # Not a stored field.
    assert FIELD not in got


async def test_a_stale_timestamp_is_409_and_changes_nothing(client, created):
    first = await client.put(ONE, json=good(title="Newer tab", **{FIELD: created["updated_at"]}))
    assert first.status_code == 200
    before = (await client.get(ONE)).json()

    r = await client.put(ONE, json=other_identity(**{FIELD: created["updated_at"]}))
    assert r.status_code == 409
    assert r.json() == {"detail": STALE}

    after = (await client.get(ONE)).json()
    assert after == before
    assert after["destination"] == "S4_ODATA_USER" and after["user_context"] is True
    assert after["definition"] == validate_odata_service(good())["definition"]
    assert after["updated_at"] == first.json()["updated_at"]


async def test_a_timestamp_from_the_future_is_stale_too(client, created):
    later = (instant(created["updated_at"]) + timedelta(seconds=1)).isoformat()
    r = await client.put(ONE, json=other_identity(**{FIELD: later}))
    assert r.status_code == 409 and r.json() == {"detail": STALE}
    assert (await client.get(ONE)).json()["updated_at"] == created["updated_at"]


async def test_without_the_field_the_route_updates_as_before(client, created):
    await client.put(ONE, json=good(title="Somebody else"))
    r = await client.put(ONE, json=other_identity())
    assert r.status_code == 200
    assert r.json()["destination"] == "S4_ODATA_TECH"


async def test_two_updates_in_a_row_with_the_answered_timestamp_both_succeed(client, created):
    first = await client.put(ONE, json=good(title="One", **{FIELD: created["updated_at"]}))
    assert first.status_code == 200
    second = await client.put(
        ONE, json=good(title="Two", **{FIELD: first.json()["updated_at"]})
    )
    assert second.status_code == 200 and second.json()["title"] == "Two"
    assert instant(second.json()["updated_at"]) > instant(first.json()["updated_at"])
    # ... and the first answer's timestamp is now a stale one.
    third = await client.put(ONE, json=good(title="Three", **{FIELD: first.json()["updated_at"]}))
    assert third.status_code == 409
    assert (await client.get(ONE)).json()["title"] == "Two"


async def test_the_same_instant_matches_however_it_is_written(client, created):
    plus_two = timezone(timedelta(hours=2))
    spellings = [
        lambda m: m.replace(tzinfo=None).isoformat(),
        lambda m: m.replace(tzinfo=None).isoformat() + "Z",
        lambda m: m.isoformat(),
        lambda m: m.astimezone(plus_two).isoformat(),
    ]
    stamp = created["updated_at"]
    for spell in spellings:
        # Each save moves the row on, so each spelling is of the latest answer.
        text = spell(instant(stamp))
        assert instant(text) == instant(stamp)
        r = await client.put(ONE, json=good(**{FIELD: text}))
        assert r.status_code == 200, (text, r.text)
        stamp = r.json()["updated_at"]


@pytest.mark.parametrize("value", [123, 1.5, True, False, [SECRET], {"at": SECRET}])
async def test_a_value_that_is_no_string_is_422_without_echo(client, created, value):
    r = await client.put(ONE, json=other_identity(**{FIELD: value}))
    assert r.status_code == 422, r.text
    assert r.json() == {"detail": f"{FIELD}: Input should be a valid string"}
    assert (await client.get(ONE)).json() == created


@pytest.mark.parametrize(
    "value",
    [
        SECRET,
        "",
        "2026-01-31",  # a date is not what the API emits
        "2026-01-31 10:00:00",
        "2026-13-45T10:00:00",
        "2026-01-31T10:00:00+99:00",
        "0001-01-01T00:00:00+14:00",
        "2026-01-31T10:00:00" + "0" * 40,
        "２０２６-01-31T10:00:00",
        "2026-01-31T10:00:00 " + SECRET,
        "2026-01-31T10:00:00\n",
    ],
)
async def test_a_string_that_is_no_timestamp_is_422_without_echo(client, created, value):
    r = await client.put(ONE, json=other_identity(**{FIELD: value}))
    assert r.status_code == 422, r.text
    assert r.json()["detail"].startswith(f"{FIELD}: ")
    assert SECRET not in r.text
    for part in filter(None, value.split()):
        assert part not in r.text
    assert not any(ch.isdigit() for ch in r.json()["detail"])
    assert (await client.get(ONE)).json() == created


async def test_null_means_not_checked(client, created):
    await client.put(ONE, json=good(title="Somebody else"))
    r = await client.put(ONE, json=other_identity(**{FIELD: None}))
    assert r.status_code == 200 and r.json()["destination"] == "S4_ODATA_TECH"


@pytest.mark.parametrize("given", ["absent", None])
async def test_an_unchecked_replace_is_said_once_at_warning(client, created, caplog, given):
    """Opt-in stays (a script may leave the field out), but an unconditional
    replace is said at WARNING -- once, not also at INFO -- with no body value."""
    caplog.set_level(logging.INFO, logger="agents.odata.admin_routes")
    body = other_identity() if given == "absent" else other_identity(**{FIELD: given})
    r = await client.put(ONE, json=body)
    assert r.status_code == 200
    lines = [
        rec for rec in caplog.records
        if rec.name == "agents.odata.admin_routes" and FIELD in rec.getMessage()
    ]
    assert [rec.levelno for rec in lines] == [logging.WARNING]
    message = lines[0].getMessage()
    assert "'stock-levels'" in message and "S4_ODATA_TECH" not in message


# --- the order of the answers: payload 422, field 422, 404, name 422, 409 ----------


async def test_a_refused_payload_is_answered_before_the_timestamp_is_judged(client, created):
    r = await client.put(ONE, json=good(odata_version="v9", **{FIELD: SECRET}))
    assert r.status_code == 422 and not r.json()["detail"].startswith(f"{FIELD}: ")
    assert SECRET not in r.text


async def test_a_malformed_timestamp_on_an_unknown_service_is_422(client):
    r = await client.put(ONE, json=good(**{FIELD: SECRET}))
    assert r.status_code == 422 and r.json()["detail"].startswith(f"{FIELD}: ")


async def test_a_rename_is_refused_before_a_stale_timestamp_is(client, created):
    await client.put(ONE, json=good(title="Newer"))
    r = await client.put(ONE, json=good(name="renamed", **{FIELD: created["updated_at"]}))
    assert r.status_code == 422
    assert r.json()["detail"] == "name cannot be changed; duplicate the service instead"


async def test_the_field_is_no_way_around_the_unknown_key_rule(client, created):
    body = good(**{FIELD: created["updated_at"], "expected_updatedat": created["updated_at"]})
    r = await client.put(ONE, json=body)
    assert r.status_code == 422
    assert (await client.get(ONE)).json() == created


async def test_create_still_refuses_the_field(client):
    r = await client.post(BASE, json=good(**{FIELD: "2026-01-31T10:00:00"}))
    assert r.status_code == 422


async def test_a_stale_timestamp_on_an_unknown_service_is_404(client):
    r = await client.put(ONE, json=good(**{FIELD: "2026-01-31T10:00:00"}))
    assert r.status_code == 404


async def test_a_refused_rename_with_a_matching_timestamp_changes_nothing(client, created):
    r = await client.put(f"{ONE}", json=good(name="renamed", **{FIELD: created["updated_at"]}))
    assert r.status_code == 422
    assert (await client.get(ONE)).json() == created


# --- updated_at moves on every update --------------------------------------------


async def test_an_update_in_the_same_clock_tick_still_gets_a_later_timestamp(
    client, created, monkeypatch
):
    frozen = instant(created["updated_at"]) - timedelta(hours=1)  # a clock that even ran back
    monkeypatch.setattr(db_module, "_odata_now", lambda: frozen)
    stamps = [instant(created["updated_at"])]
    stamp = created["updated_at"]
    for title in ("One", "Two", "Three"):
        r = await client.put(ONE, json=good(title=title, **{FIELD: stamp}))
        assert r.status_code == 200, r.text
        stamp = r.json()["updated_at"]
        stamps.append(instant(stamp))
    assert stamps == sorted(set(stamps)) and len(stamps) == 4
    assert stamps[1] - stamps[0] == timedelta(microseconds=1)


async def test_an_update_that_changes_no_field_still_moves_the_timestamp(client, created):
    r = await client.put(ONE, json=good(**{FIELD: created["updated_at"]}))
    assert r.status_code == 200
    assert instant(r.json()["updated_at"]) > instant(created["updated_at"])
    # So the tab that loaded before it is stale, as for any other save.
    again = await client.put(ONE, json=good(**{FIELD: created["updated_at"]}))
    assert again.status_code == 409


async def test_update_odata_service_bumps_the_timestamp_without_commit_too(created):
    """The bundle import writes with ``commit=False``; its rows move on as well."""
    async with SessionLocal() as s:
        row = await get_odata_service(s, "stock-levels")
        before = instant(row.updated_at.isoformat())
        row = await update_odata_service(s, row, validate_odata_service(good()), commit=False)
        assert instant(row.updated_at.isoformat()) > before
        await s.commit()
    async with SessionLocal() as s:
        row = await get_odata_service(s, "stock-levels")
        assert instant(row.updated_at.isoformat()) > before


# --- the check and the write are one locked transaction ----------------------------


def _trace_update(monkeypatch) -> list[tuple[str, int]]:
    """Record ``(step, id(session))`` of the update route's steps and fail on
    an unlocked read of the service."""
    from agents.odata import admin_routes

    trail: list[tuple[str, int]] = []

    def spy(step: str, real):
        async def wrapper(session, *args: Any, **kwargs: Any):
            trail.append((step, id(session)))
            return await real(session, *args, **kwargs)

        return wrapper

    async def unlocked(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("update read the service without locking it")

    for step, attr in (
        ("serialise", "begin_exclusive_write"),
        ("lock", "_lock_odata_service"),
        ("update", "update_odata_service"),
    ):
        monkeypatch.setattr(admin_routes, attr, spy(step, getattr(admin_routes, attr)))
    monkeypatch.setattr(admin_routes, "get_odata_service", unlocked)
    return trail


async def test_update_locks_the_row_before_it_compares_and_writes(client, created, monkeypatch):
    trail = _trace_update(monkeypatch)
    r = await client.put(ONE, json=good(title="T", **{FIELD: created["updated_at"]}))
    assert r.status_code == 200
    assert [step for step, _ in trail] == ["serialise", "lock", "update"]
    # One session, so one transaction: the lock is held until the update commits.
    assert len({session for _, session in trail}) == 1


async def test_update_without_the_field_locks_the_row_as_well(client, created, monkeypatch):
    trail = _trace_update(monkeypatch)
    assert (await client.put(ONE, json=good(title="T"))).status_code == 200
    assert [step for step, _ in trail] == ["serialise", "lock", "update"]


async def test_a_stale_update_stops_after_the_lock(client, created, monkeypatch):
    await client.put(ONE, json=good(title="Newer"))
    trail = _trace_update(monkeypatch)
    r = await client.put(ONE, json=good(**{FIELD: created["updated_at"]}))
    assert r.status_code == 409
    assert [step for step, _ in trail] == ["serialise", "lock"]


def test_the_statement_the_update_reads_with_is_for_update_on_postgres():
    """What makes compare-then-write atomic on Postgres: the row is read with
    an exclusive lock, by name, so a second writer waits here and then reads
    the committed row of the first."""
    from sqlalchemy.dialects import postgresql, sqlite

    from agents.odata import admin_routes

    query = admin_routes._locked_service_query("stock-levels")
    on_postgres = str(query.compile(dialect=postgresql.dialect()))
    assert on_postgres.rstrip().endswith("FOR UPDATE")
    assert "WHERE odata_services.name = " in on_postgres
    assert "odata_services.updated_at" in on_postgres.split("FROM")[0]  # the value compared
    assert "FOR " not in str(query.compile(dialect=sqlite.dialect()))


_SQLITE_ONLY = pytest.mark.skipif(
    db_module.engine.dialect.name != "sqlite", reason="about SQLite's write lock"
)


@_SQLITE_ONLY
async def test_begin_exclusive_write_takes_the_sqlite_write_lock(caplog):
    """SQLite's stand-in for the row lock: the transaction is a writer from
    its first statement, so a second one waits instead of reading a row the
    first is about to replace. Nothing is sent on Postgres."""
    caplog.set_level(logging.WARNING, logger="agents.db")
    async with SessionLocal() as s:
        await db_module.begin_exclusive_write(s)
        raw = await (await s.connection()).get_raw_connection()
        assert raw.driver_connection.in_transaction
        await s.rollback()
    assert not caplog.records


@_SQLITE_ONLY
async def test_begin_exclusive_write_warns_when_the_transaction_is_already_open(caplog):
    """Called too late it can take no lock: what was read before it is
    already stale-prone. That must not pass silently."""
    caplog.set_level(logging.WARNING, logger="agents.db")
    async with SessionLocal() as s:
        await s.execute(delete(ODataAuditLog))  # a write: the driver transaction is open
        await db_module.begin_exclusive_write(s)
        await s.rollback()
    warnings = [r for r in caplog.records if r.name == "agents.db"]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert "begin_exclusive_write" in warnings[0].getMessage()


def test_the_column_has_no_default_of_its_own_for_an_update():
    """`_next_odata_stamp` owns ``updated_at`` on update; a second source
    (``onupdate=now()``) would be whole seconds on SQLite and the start of
    the transaction on Postgres. Creation still stamps."""
    column = ODataService.__table__.c.updated_at
    assert column.onupdate is None and column.server_onupdate is None
    assert column.server_default is not None


async def test_creation_stamps_updated_at(created):
    assert created["updated_at"] and instant(created["updated_at"]) <= datetime.now(timezone.utc)


# --- the answer is what THIS save wrote ----------------------------------------------


def _right_after_the_next_commit(monkeypatch, interloper) -> None:
    """Run ``interloper`` once, directly after the next commit: another
    writer landing between a save's commit and its answer."""
    real = AsyncSession.commit
    armed = [True]

    async def commit(self):
        await real(self)
        if armed[0]:
            armed[0] = False
            await interloper()

    monkeypatch.setattr(AsyncSession, "commit", commit)


async def test_the_answer_is_this_saves_state_not_a_later_writers(client, created, monkeypatch):
    """Otherwise the client holds a valid stamp for a state it never wrote
    and its next save replaces that state without a 409."""

    async def unconditional_writer():
        async with SessionLocal() as s:
            row = await get_odata_service(s, "stock-levels")
            data = validate_odata_service(other_identity(title="Other"))
            await update_odata_service(s, row, data)

    _right_after_the_next_commit(monkeypatch, unconditional_writer)
    r = await client.put(ONE, json=good(title="Mine", **{FIELD: created["updated_at"]}))
    assert r.status_code == 200, r.text
    mine = r.json()
    assert mine["title"] == "Mine" and mine["destination"] == "S4_ODATA_USER"
    stored = (await client.get(ONE)).json()
    assert stored["title"] == "Other" and stored["destination"] == "S4_ODATA_TECH"
    assert instant(mine["updated_at"]) < instant(stored["updated_at"])
    again = await client.put(ONE, json=good(title="Mine again", **{FIELD: mine["updated_at"]}))
    assert again.status_code == 409
    assert (await client.get(ONE)).json() == stored


async def test_a_delete_right_after_the_commit_does_not_fail_the_save(
    client, created, monkeypatch
):
    async def deleter():
        async with SessionLocal() as s:
            await s.execute(delete(ODataService))
            await s.commit()

    _right_after_the_next_commit(monkeypatch, deleter)
    r = await client.put(ONE, json=good(title="Mine", **{FIELD: created["updated_at"]}))
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Mine"
    assert (await client.get(ONE)).status_code == 404


async def test_a_failing_referrer_read_fails_the_save_before_anything_is_stored(
    client, created, monkeypatch
):
    """The answer is read before the commit: an error there is an error
    answer for a write that did not happen, never for one that did."""
    from agents.odata import admin_routes

    async def broken(*args: Any, **kwargs: Any):
        raise RuntimeError("agents unreadable")

    with monkeypatch.context() as patched:
        patched.setattr(admin_routes, "odata_service_referrers", broken)
        with pytest.raises(RuntimeError):
            await client.put(ONE, json=other_identity(**{FIELD: created["updated_at"]}))
    assert (await client.get(ONE)).json() == created


# --- a save without the check is visible in the log ----------------------------------


def _unchecked(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        # A WARNING: a replace that can overwrite a concurrent edit (B-odata-3).
        if r.name == "agents.odata.admin_routes" and r.levelno == logging.WARNING
    ]


@pytest.mark.parametrize("patch", [{}, {FIELD: None}], ids=["absent", "null"])
async def test_a_save_without_the_check_is_logged_with_service_and_principal(
    client, created, caplog, patch
):
    caplog.set_level(logging.INFO, logger="agents.odata.admin_routes")
    r = await client.put(ONE, json=other_identity(title=SECRET, **patch))
    assert r.status_code == 200
    lines = _unchecked(caplog)
    assert len(lines) == 1
    # Without an XSUAA binding the middleware binds this principal.
    assert "stock-levels" in lines[0] and lines[0].endswith(" by local-dev")
    assert FIELD in lines[0]
    # No body: neither a field value nor the identity it sets.
    assert SECRET not in lines[0] and "S4_ODATA_TECH" not in lines[0]


async def test_a_checked_save_and_a_refused_one_log_no_such_line(client, created, caplog):
    caplog.set_level(logging.INFO, logger="agents.odata.admin_routes")
    assert (
        await client.put(ONE, json=good(title="T", **{FIELD: created["updated_at"]}))
    ).status_code == 200
    assert (await client.put(ONE, json=good(name="renamed"))).status_code == 422
    assert (await client.put(f"{BASE}/nope", json=good(name="nope"))).status_code == 404
    assert _unchecked(caplog) == []


async def test_two_saves_from_the_same_loaded_state_cannot_both_win(client, created):
    """Both tabs loaded the same row and save at once: one is stored, the
    other is told to reload. Here it is SQLite's write lock that orders them;
    on Postgres the row lock does (see the statement test above)."""
    for round_ in range(5):
        stamp = (await client.get(ONE)).json()["updated_at"]
        answers = await asyncio.gather(
            client.put(ONE, json=good(title=f"A{round_}", **{FIELD: stamp})),
            client.put(ONE, json=other_identity(title=f"B{round_}", **{FIELD: stamp})),
        )
        assert sorted(a.status_code for a in answers) == [200, 409], [a.text for a in answers]
        winner = next(a for a in answers if a.status_code == 200).json()
        stored = (await client.get(ONE)).json()
        assert stored["title"] == winner["title"]
        assert stored["destination"] == winner["destination"]
        assert stored["updated_at"] == winner["updated_at"]


# --- the delete route is unchanged ----------------------------------------------------


async def test_delete_knows_no_expected_updated_at(client, created):
    """The check is the update route's alone; delete ignores the parameter,
    as it ignores any other."""
    await client.put(ONE, json=good(title="Newer"))
    r = await client.delete(ONE, params={FIELD: created["updated_at"]})
    assert r.status_code == 204
    assert (await client.get(ONE)).status_code == 404
