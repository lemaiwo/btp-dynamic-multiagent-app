"""The InA query table and its helpers in ``agents.db``.

Addendum (k), part 2, item 1 of the web chat design: ``ina_queries`` (HANA
schema generation 9) mirrors ``odata_services``: a unique name, the
identity a call carries, the description a ``Describe`` read, and the same
``expected_updated_at`` compare-and-set. SQLite here; every statement is
also compiled for HANA by ``tests/conftest.py``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, inspect
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from agents import db as agents_db  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    InaQuery,
    SessionLocal,
    StaleRow,
    create_ina_query,
    delete_ina_query,
    engine,
    get_ina_query,
    ina_entries,
    ina_query_referrers,
    init_db,
    list_ina_queries,
    update_ina_query,
)
from agents.ina import BUILTIN_INA_URL  # noqa: E402
from agents.ina.models import InaDefinition, validate_ina_query  # noqa: E402

GOOD: dict[str, Any] = {
    "name": "sales-by-region",
    "title": "Sales by region",
    "purpose": "Net sales per region and month",
    "not_for": "Order level detail; use the sales orders service",
    "destination": "S4_INA_USER",
    "user_context": True,
    "query": "2CZQ_SALES_BY_REGION",
    "package": "ZANALYTICS",
    "definition": {
        "dimensions": [
            {"name": "0CALMONTH", "label": "Calendar month", "key_field": "0CALMONTH.KEY"},
            {"name": "ZREGION", "label": "Region"},
        ],
        "measures": [
            {"name": "ZNETSALES", "label": "Net sales", "unit": "EUR"},
            {"name": "ZQUANTITY", "label": "Quantity"},
        ],
        "variables": [
            {"name": "ZV_FYEAR", "label": "Fiscal year", "mandatory": True, "type": "single"},
        ],
    },
    "metadata_fetched_at": "2026-11-03T09:30:00+01:00",
}

INA_SERVER = {
    "url": "builtin:ina",
    "auth_mode": "destination",
    "oauth": {"queries": ["sales-by-region"]},
}
PLAIN_SERVER = {"url": "https://mcp.example.com/mcp", "auth_mode": "jwt"}


def good(**patch: Any) -> dict[str, Any]:
    data = copy.deepcopy(GOOD)
    data.update(patch)
    return data


def instant(value: datetime) -> datetime:
    """A stored timestamp as an aware UTC instant (SQLite hands back naive UTC)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@pytest.fixture(autouse=True)
async def _clean_tables():
    """Each test starts with no queries and no agents (the database is the
    session's, shared with every other suite in the process)."""
    await init_db()
    async with SessionLocal() as s:
        for model in (InaQuery, AgentConfig):
            await s.execute(delete(model))
        await s.commit()
    yield


def _agent_row(
    name: str, *, enabled: int, primary: dict, extras: list[dict] | None = None
) -> AgentConfig:
    """An agent row as storage holds it, written without the save-time cleaner."""
    return AgentConfig(
        name=name,
        description="d",
        instructions="i",
        mcp_url=primary["url"],
        auth_mode=primary["auth_mode"],
        oauth_json=json.dumps(primary["oauth"]) if primary.get("oauth") else None,
        extra_servers_json=json.dumps(extras) if extras else None,
        enabled=enabled,
    )


async def _created(**patch: Any) -> InaQuery:
    async with SessionLocal() as s:
        return await create_ina_query(s, validate_ina_query(good(**patch)))


# --- the table ------------------------------------------------------------------


async def test_init_db_creates_the_table_and_is_idempotent():
    await init_db()
    await init_db()
    async with engine.connect() as c:
        names = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    assert "ina_queries" in names


def test_column_widths_fit_what_validation_accepts():
    """SQLite ignores VARCHAR limits; Postgres and HANA do not. A value the
    gate accepts must fit its column, or the first save on CF fails."""
    cols = InaQuery.__table__.c
    assert cols.name.type.length >= 64
    assert cols.title.type.length >= 120
    assert cols.purpose.type.length >= 200 and cols.not_for.type.length >= 200
    assert cols.destination.type.length >= 200
    assert cols.query.type.length >= 128
    assert cols.package.type.length >= 64
    # Not described yet is NULL, not an empty object.
    assert cols.definition_json.nullable is True
    assert cols.metadata_fetched_at.nullable is True


def test_updated_at_has_no_default_of_its_own_for_an_update():
    """The update owns the stamp (`_next_odata_stamp`); ``onupdate`` would be
    whole seconds on SQLite and the transaction's start on Postgres."""
    column = InaQuery.__table__.c.updated_at
    assert column.onupdate is None and column.server_onupdate is None
    assert column.server_default is not None


def test_the_name_is_unique_by_a_named_constraint():
    """HDI needs a name for every unique constraint (an ``.hdbindex``)."""
    names = {c.name for c in InaQuery.__table__.constraints if c.name}
    assert "uq_ina_queries_name" in names


# --- create, read, list, delete ---------------------------------------------------


async def test_create_get_list_and_delete():
    async with SessionLocal() as s:
        row = await create_ina_query(s, validate_ina_query(GOOD))
        assert isinstance(row.id, int)
        got = await get_ina_query(s, "sales-by-region")
        assert got is not None and got.query == "2CZQ_SALES_BY_REGION"
        assert got.to_dict()["definition"]["measures"][0]["name"] == "ZNETSALES"
        assert [r.name for r in await list_ina_queries(s)] == ["sales-by-region"]
        assert await delete_ina_query(s, got) is True
        assert await get_ina_query(s, "sales-by-region") is None
        assert await get_ina_query(s, "never-there") is None


async def test_list_is_ordered_by_name():
    for name in ("zz-last", "aa-first", "mm-middle"):
        await _created(name=name)
    async with SessionLocal() as s:
        assert [r.name for r in await list_ina_queries(s)] == ["aa-first", "mm-middle", "zz-last"]


async def test_a_plain_dict_goes_through_the_gate_before_it_is_stored():
    """Storage is the last gate: a caller without the admin payload (an
    import script) cannot store what `validate_ina_query` refuses."""
    async with SessionLocal() as s:
        row = await create_ina_query(s, good(name="from-a-dict"))
        assert row.title == "Sales by region"
        with pytest.raises(ValueError) as refused:
            await create_ina_query(s, good(name="bad", destination="no spaces allowed"))
        assert str(refused.value).startswith("destination:")
        assert "no spaces allowed" not in str(refused.value)
        assert [r.name for r in await list_ina_queries(s)] == ["from-a-dict"]


async def test_to_dict_shape_and_stored_types():
    row = await _created()
    d = row.to_dict()
    assert set(d) == {
        "id", "name", "title", "purpose", "not_for", "destination", "user_context",
        "query", "package", "definition", "metadata_fetched_at", "enabled",
        "created_at", "updated_at", "used_by",
    }
    assert d["user_context"] is True and d["enabled"] is True
    assert d["definition"] == InaDefinition.model_validate(GOOD["definition"]).model_dump(
        mode="json"
    )
    assert d["metadata_fetched_at"] == "2026-11-03T08:30:00Z"
    assert isinstance(d["id"], int) and d["created_at"] and d["updated_at"]
    assert d["used_by"] == []
    used = [{"agent_id": 1, "agent": "a", "enabled": True, "expose_api": False,
             "api_slug": None}]
    assert row.to_dict(used_by=used)["used_by"] == used
    # Booleans are Integer 0/1 in the table, like ODataService.
    assert (row.user_context, row.enabled) == (1, 1)


async def test_the_definition_is_stored_as_the_models_own_serialisation():
    data = validate_ina_query(GOOD)
    row = await _created()
    assert row.definition_json == data.definition.model_dump_json()


async def test_a_query_not_described_yet_stores_null():
    row = await _created(definition=None, metadata_fetched_at=None)
    assert row.definition_json is None
    assert row.to_dict()["definition"] is None
    assert row.to_export()["definition"] is None


async def test_export_is_the_payload_and_reimports_through_the_gate_unchanged():
    """SQLite drops the offset; the export must still be the gate's own dump,
    so an export is importable as it is and stable from then on."""
    await _created(enabled=False, user_context=False, not_for="", package="")
    async with SessionLocal() as s:
        row = await get_ina_query(s, "sales-by-region")
    exported = row.to_export()
    assert set(exported) == set(validate_ina_query(GOOD).model_dump())
    assert exported["metadata_fetched_at"] == "2026-11-03T08:30:00Z"
    assert exported["enabled"] is False and exported["user_context"] is False
    assert validate_ina_query(exported).model_dump(mode="json") == exported
    # No id and no row timestamp: portable between landscapes.
    assert not {"id", "created_at", "updated_at"} & set(exported)


def test_an_unreadable_definition_reads_back_as_not_described(caplog):
    """A broken row still lists and can be repaired; the log names the row,
    never its content."""
    caplog.set_level(logging.WARNING, logger="agents.db")
    row = InaQuery(name="broken", definition_json='{"dimensions": "secret-ish"')
    assert row.to_dict()["definition"] is None
    row.definition_json = "[1, 2]"  # JSON, but no object
    assert row.to_export()["definition"] is None
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "broken" in messages and "secret-ish" not in messages


# --- the name -------------------------------------------------------------------


async def test_a_taken_name_is_refused_with_the_field_and_the_rule():
    await _created()
    async with SessionLocal() as s:
        with pytest.raises(ValueError) as refused:
            await create_ina_query(s, validate_ina_query(good(title="Other")))
        assert str(refused.value) == "name: already exists"
        assert len(await list_ina_queries(s)) == 1


async def test_a_taken_name_without_commit_keeps_the_callers_transaction():
    """An import adds several rows in one transaction; one taken name must
    not throw away the rows before it."""
    async with SessionLocal() as s:
        await create_ina_query(s, validate_ina_query(good(name="first")), commit=False)
        with pytest.raises(ValueError, match="already exists"):
            await create_ina_query(s, validate_ina_query(good(name="first")), commit=False)
        await s.commit()
    async with SessionLocal() as s:
        assert [r.name for r in await list_ina_queries(s)] == ["first"]


async def test_losing_the_name_race_reads_like_the_lookup(monkeypatch):
    """Two saves of one name can both pass the lookup; the constraint then
    refuses the second, as the same ValueError and not chained (an
    IntegrityError carries the statement and its parameters)."""
    async def nobody_there(session, name):
        return None

    async with SessionLocal() as s:
        await create_ina_query(s, validate_ina_query(good(name="kept")), commit=False)
        await create_ina_query(s, validate_ina_query(good(name="raced")), commit=False)
        monkeypatch.setattr(agents_db, "get_ina_query", nobody_there)
        with pytest.raises(ValueError, match="^name: already exists$") as refused:
            await create_ina_query(s, validate_ina_query(good(name="raced")), commit=False)
        assert refused.value.__cause__ is None and refused.value.__suppress_context__
        await s.commit()
    monkeypatch.undo()
    async with SessionLocal() as s:
        assert [r.name for r in await list_ina_queries(s)] == ["kept", "raced"]


async def test_the_unique_constraint_backs_the_name_check():
    await _created()
    async with SessionLocal() as s:
        s.add(InaQuery(
            name="sales-by-region", title="t", purpose="p", destination="S4_INA_TECH",
            query="Q",
        ))
        with pytest.raises(IntegrityError):
            await s.commit()


# --- update: compare-and-set on updated_at -----------------------------------------


async def test_an_update_with_the_loaded_stamp_replaces_and_moves_the_stamp():
    loaded = await _created()
    changed = good(
        title="Sales per region", destination="S4_INA_TECH", user_context=False,
        enabled=False, package="", not_for="", metadata_fetched_at="2026-11-04T10:00:00Z",
    )
    async with SessionLocal() as s:
        row = await update_ina_query(
            s, "sales-by-region", validate_ina_query(changed),
            expected_updated_at=loaded.updated_at,
        )
    assert instant(row.updated_at) > instant(loaded.updated_at)
    async with SessionLocal() as s:
        stored = await get_ina_query(s, "sales-by-region")
    assert stored.to_export() == validate_ina_query(changed).model_dump(mode="json")
    assert instant(stored.updated_at) == instant(row.updated_at)


async def test_a_moved_stamp_is_a_stale_row_and_changes_nothing():
    loaded = await _created()
    async with SessionLocal() as s:
        newer = await update_ina_query(
            s, "sales-by-region", validate_ina_query(good(title="Newer tab")),
            expected_updated_at=loaded.updated_at,
        )
    async with SessionLocal() as s:
        with pytest.raises(StaleRow) as stale:
            await update_ina_query(
                s, "sales-by-region",
                validate_ina_query(good(title="Older tab", destination="S4_INA_TECH")),
                expected_updated_at=loaded.updated_at,
            )
    # Names nothing of the payload; the route turns it into a 409.
    assert "Older tab" not in str(stale.value) and "S4_INA_TECH" not in str(stale.value)
    async with SessionLocal() as s:
        stored = await get_ina_query(s, "sales-by-region")
    assert stored.title == "Newer tab" and stored.destination == "S4_INA_USER"
    assert instant(stored.updated_at) == instant(newer.updated_at)


async def test_the_same_instant_matches_however_it_is_written():
    """Instants are compared, not texts: SQLite hands back naive UTC, a
    client may send the same moment with an offset."""
    loaded = await _created()
    as_utc = instant(loaded.updated_at)
    in_brussels = as_utc.astimezone(timezone(timedelta(hours=1)))
    async with SessionLocal() as s:
        row = await update_ina_query(
            s, "sales-by-region", validate_ina_query(good(title="One")),
            expected_updated_at=in_brussels,
        )
    async with SessionLocal() as s:
        await update_ina_query(
            s, "sales-by-region", validate_ina_query(good(title="Two")),
            expected_updated_at=instant(row.updated_at).replace(tzinfo=None),
        )
        assert (await get_ina_query(s, "sales-by-region")).title == "Two"


async def test_without_a_stamp_the_update_replaces_unconditionally():
    await _created()
    async with SessionLocal() as s:
        await update_ina_query(s, "sales-by-region", validate_ina_query(good(title="A")))
    async with SessionLocal() as s:
        await update_ina_query(s, "sales-by-region", good(title="B"))
        assert (await get_ina_query(s, "sales-by-region")).title == "B"


async def test_an_update_in_the_same_clock_tick_still_gets_a_later_stamp(monkeypatch):
    loaded = await _created()
    frozen = instant(loaded.updated_at) - timedelta(hours=1)  # a clock that even ran back
    monkeypatch.setattr(agents_db, "_odata_now", lambda: frozen)
    stamps = [instant(loaded.updated_at)]
    for title in ("One", "Two", "Three"):
        async with SessionLocal() as s:
            row = await update_ina_query(
                s, "sales-by-region", validate_ina_query(good(title=title)),
                expected_updated_at=stamps[-1],
            )
        stamps.append(instant(row.updated_at))
    assert stamps == sorted(set(stamps)) and len(stamps) == 4
    assert stamps[1] - stamps[0] == timedelta(microseconds=1)


async def test_two_updates_from_the_same_loaded_state_cannot_both_win():
    """Both tabs loaded the same row and save at once: one is stored, the
    other is a StaleRow. On SQLite the write lock the update takes first
    orders them; on Postgres and HANA the row lock does."""
    loaded = await _created()
    for round_ in range(5):
        async with SessionLocal() as s:
            stamp = (await get_ina_query(s, "sales-by-region")).updated_at

        async def save(title: str, destination: str) -> str:
            async with SessionLocal() as s:
                try:
                    await update_ina_query(
                        s, "sales-by-region",
                        validate_ina_query(good(title=title, destination=destination)),
                        expected_updated_at=stamp,
                    )
                except StaleRow:
                    return "stale"
            return title

        answers = await asyncio.gather(
            save(f"A{round_}", "S4_INA_USER"), save(f"B{round_}", "S4_INA_TECH")
        )
        assert sorted(a == "stale" for a in answers) == [False, True], answers
        winner = next(a for a in answers if a != "stale")
        async with SessionLocal() as s:
            stored = await get_ina_query(s, "sales-by-region")
        assert stored.title == winner
        assert stored.destination == ("S4_INA_USER" if winner.startswith("A") else "S4_INA_TECH")
    assert instant(stored.updated_at) > instant(loaded.updated_at)


def test_the_statement_the_update_reads_with_is_for_update_on_postgres():
    """What makes compare-then-write atomic on Postgres and HANA: the row is
    read with an exclusive lock, by name, so a second writer waits there and
    then reads the committed row of the first."""
    from sqlalchemy.dialects import postgresql, sqlite

    query = agents_db._locked_ina_query("sales-by-region")
    on_postgres = str(query.compile(dialect=postgresql.dialect()))
    assert on_postgres.rstrip().endswith("FOR UPDATE")
    assert "WHERE ina_queries.name = " in on_postgres
    assert "ina_queries.updated_at" in on_postgres.split("FROM")[0]
    assert "FOR " not in str(query.compile(dialect=sqlite.dialect()))


async def test_an_update_refuses_another_name_and_answers_none_for_an_unknown_one():
    """Agents attach a query by name; a rename would detach them silently."""
    await _created()
    async with SessionLocal() as s:
        with pytest.raises(ValueError) as refused:
            await update_ina_query(s, "sales-by-region", validate_ina_query(good(name="other")))
        assert str(refused.value).startswith("name: cannot be changed")
        assert await update_ina_query(s, "never-there", good(name="never-there")) is None
    async with SessionLocal() as s:
        assert (await get_ina_query(s, "sales-by-region")).title == "Sales by region"


async def test_an_update_without_the_description_keeps_the_described_one():
    """``definition`` and ``metadata_fetched_at`` are what ``Describe`` read
    from SAP, not what an admin types: a save that leaves the keys out (the
    edit form) keeps them; an explicit ``null`` clears them."""
    await _created()
    typed = {k: v for k, v in good(title="Edited").items()
             if k not in ("definition", "metadata_fetched_at")}
    async with SessionLocal() as s:
        await update_ina_query(s, "sales-by-region", validate_ina_query(typed))
    async with SessionLocal() as s:
        row = await get_ina_query(s, "sales-by-region")
        assert row.title == "Edited"
        assert row.to_dict()["definition"]["dimensions"][0]["name"] == "0CALMONTH"
        assert row.to_dict()["metadata_fetched_at"] == "2026-11-03T08:30:00Z"
        await update_ina_query(
            s, "sales-by-region",
            validate_ina_query({**typed, "definition": None, "metadata_fetched_at": None}),
        )
    async with SessionLocal() as s:
        row = await get_ina_query(s, "sales-by-region")
        assert row.definition_json is None and row.metadata_fetched_at is None


async def test_update_without_commit_flushes_and_moves_the_stamp():
    loaded = await _created()
    async with SessionLocal() as s:
        row = await update_ina_query(
            s, "sales-by-region", validate_ina_query(good(title="Flushed")), commit=False
        )
        assert row.title == "Flushed"
        assert instant(row.updated_at) > instant(loaded.updated_at)
        await s.rollback()
    async with SessionLocal() as s:
        assert (await get_ina_query(s, "sales-by-region")).title == "Sales by region"


# --- the agents that name a query --------------------------------------------------


def test_ina_entries_picks_only_builtin_ina_blocks():
    assert BUILTIN_INA_URL == "builtin:ina"
    servers = [
        PLAIN_SERVER,
        {"url": "builtin:odata", "auth_mode": "destination", "oauth": {"services": ["a"]}},
        INA_SERVER,
        {"url": " BUILTIN:InA/ ", "auth_mode": "destination", "oauth": {"queries": ["b"]}},
        {"url": "builtin:ina", "auth_mode": "destination"},
        {"url": "builtin:ina", "auth_mode": "destination", "oauth": "queries"},
        {"url": None},
        "builtin:ina",
    ]
    assert ina_entries(servers) == [INA_SERVER["oauth"], {"queries": ["b"]}]
    assert ina_entries([]) == []


async def test_referrers_from_stored_rows():
    other = {"url": "builtin:ina", "auth_mode": "destination",
             "oauth": {"queries": ["stock-ageing", "sales-by-region", 7, None]}}
    async with SessionLocal() as s:
        s.add(_agent_row("analyst", enabled=1, primary=INA_SERVER))
        s.add(_agent_row("reader", enabled=0, primary=PLAIN_SERVER, extras=[other]))
        s.add(_agent_row("plain", enabled=1, primary=PLAIN_SERVER))
        # Two entries naming one query: the agent appears once.
        s.add(_agent_row("twice", enabled=1, primary=INA_SERVER, extras=[INA_SERVER]))
        await s.commit()
        found = await ina_query_referrers(s)
        assert set(found) == {"sales-by-region", "stock-ageing"}
        users = found["sales-by-region"]
        assert [u["agent"] for u in users] == ["analyst", "reader", "twice"]
        reader = next(u for u in users if u["agent"] == "reader")
        assert set(reader) == {"agent_id", "agent", "enabled", "expose_api", "api_slug"}
        assert reader["enabled"] is False and reader["expose_api"] is False
        assert [u["agent"] for u in found["stock-ageing"]] == ["reader"]
        narrowed = await ina_query_referrers(s, "stock-ageing")
        assert list(narrowed) == ["stock-ageing"]
        assert await ina_query_referrers(s, "nobody-uses-it") == {}
