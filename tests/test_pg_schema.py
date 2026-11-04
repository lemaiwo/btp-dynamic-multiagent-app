"""``PG_SCHEMA``: a Postgres schema of our own on a shared instance.

The setting rides on the resolved ``DatabaseTarget`` and becomes asyncpg's
``search_path``; at startup the app adopts the tables it already has in
``public`` into the schema when ``PG_SCHEMA_ADOPT`` says so
(agents/pg_schema.py). Every value below is made up, the shapes of another
application's tables included.

Run:  python -m pytest tests/test_pg_schema.py -q
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import logging
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()

import agents.ide.models  # noqa: E402,F401  (the IDE tables on Base, as init_db)
from agents import db as agents_db  # noqa: E402
from agents import pg_schema  # noqa: E402
from agents.db import DatabaseConfigError  # noqa: E402
from tests.test_hana_config import HANA, POSTGRES  # noqa: E402


@pytest.fixture
def env(monkeypatch):
    """A clean database environment; returns a setter for it."""
    for name in ("VCAP_SERVICES", "VCAP_APPLICATION", "DATABASE_URL", "DB_KIND",
                 "HANA_HDI_USER", "HANA_HDI_PASSWORD", "PG_SCHEMA", "PG_SCHEMA_ADOPT"):
        monkeypatch.delenv(name, raising=False)

    def set_(**values):
        for name, value in values.items():
            monkeypatch.setenv(name, value)

    return set_


# --- the setting ----------------------------------------------------------------

def test_the_setting_rides_on_a_postgres_binding(env):
    env(VCAP_SERVICES=json.dumps(POSTGRES), PG_SCHEMA="pydantic_agent")
    target = agents_db._resolve_database()
    assert target.kind == "postgres" and target.schema == "pydantic_agent"
    url, connect_args = agents_db._engine_settings(target)
    assert connect_args["server_settings"] == {"search_path": "pydantic_agent"}
    assert "schema" not in url


def test_without_the_setting_nothing_is_added(env):
    env(VCAP_SERVICES=json.dumps(POSTGRES))
    target = agents_db._resolve_database()
    assert target.schema is None
    assert "server_settings" not in agents_db._engine_settings(target)[1]


@pytest.mark.parametrize("value", [
    "public", "PUBLIC", "Pydantic_Agent", " pydantic_agent ", "pg_x",
    "information_schema", "1abc", "a-b", "a.b", "a" * 64, 'x";drop',
])
def test_an_invalid_name_is_refused_without_being_repeated(env, value):
    env(VCAP_SERVICES=json.dumps(POSTGRES), PG_SCHEMA=value)
    with pytest.raises(DatabaseConfigError) as info:
        agents_db._resolve_database()
    assert value.strip() not in str(info.value)


def test_blank_means_no_schema(env):
    env(VCAP_SERVICES=json.dumps(POSTGRES), PG_SCHEMA="   ")
    assert agents_db._resolve_database().schema is None


@pytest.mark.parametrize("services, url", [
    (HANA, None),
    (None, "sqlite+aiosqlite:///x.db"),
    (None, None),
])
def test_the_setting_is_refused_on_any_other_database(env, services, url):
    values = {"PG_SCHEMA": "pydantic_agent"}
    if services:
        values["VCAP_SERVICES"] = json.dumps(services)
    if url:
        values["DATABASE_URL"] = url
    env(**values)
    with pytest.raises(DatabaseConfigError, match="PostgreSQL only"):
        agents_db._resolve_database()


def test_the_copy_script_sides_follow_the_rule(env):
    env(VCAP_SERVICES=json.dumps({**POSTGRES, **HANA}), DB_KIND="postgres",
        PG_SCHEMA="pydantic_agent")
    assert agents_db.bound_target("postgres").schema == "pydantic_agent"
    assert agents_db.bound_target("hana").schema is None  # ignored, not refused
    assert agents_db._target_from_url("postgresql://u:p@h/d").schema == "pydantic_agent"
    assert agents_db._target_from_url("hana://u:p@h:443/?currentSchema=C").schema is None


def test_a_postgres_database_url_follows_it_too(env):
    env(DATABASE_URL="postgresql://u:p@h/d", PG_SCHEMA="pydantic_agent")
    assert agents_db._resolve_database().schema == "pydantic_agent"


@pytest.mark.parametrize("value, expected", [("true", True), ("false", False), ("", None),
                                             ("  ", None)])
def test_the_switch_rides_on_the_target(env, value, expected):
    env(VCAP_SERVICES=json.dumps(POSTGRES), PG_SCHEMA="pydantic_agent", PG_SCHEMA_ADOPT=value)
    target = agents_db._resolve_database()
    assert target.schema == "pydantic_agent" and target.schema_adopt is expected


@pytest.mark.parametrize("value", ["TRUE", "True", "yes", "1", "on", " true", "false "])
def test_the_switch_is_exactly_true_or_false(env, value):
    env(VCAP_SERVICES=json.dumps(POSTGRES), PG_SCHEMA="pydantic_agent", PG_SCHEMA_ADOPT=value)
    with pytest.raises(DatabaseConfigError) as info:
        agents_db._resolve_database()
    assert str(info.value) == pg_schema.PG_SCHEMA_ADOPT_RULE


def test_the_switch_needs_the_schema(env):
    env(VCAP_SERVICES=json.dumps(POSTGRES), PG_SCHEMA_ADOPT="true")
    with pytest.raises(DatabaseConfigError, match="only together with PG_SCHEMA"):
        agents_db._resolve_database()


@pytest.mark.parametrize("name, value, rule", [
    ("PG_SCHEMA", "Not-Valid", pg_schema.PG_SCHEMA_RULE),
    ("PG_SCHEMA_ADOPT", "yes", pg_schema.PG_SCHEMA_ADOPT_RULE),
])
def test_on_cloud_foundry_the_operator_sees_the_rule(env, name, value, rule):
    # The bound service is read inside a guard that answers "could not be
    # read" for any error; the settings are checked before it.
    env(VCAP_APPLICATION='{"application_name": "app"}', VCAP_SERVICES=json.dumps(POSTGRES),
        PG_SCHEMA="pydantic_agent")
    env(**{name: value})
    with pytest.raises(DatabaseConfigError) as info:
        agents_db._resolve_database()
    assert str(info.value) == rule


# --- the ownership rule and the planner ------------------------------------------

# Another application's tables under two of our names (made up): neither has
# the columns every table of ours has.
OTHER_CHAT_CONVERSATIONS = {"id", "owner", "subject", "created_at", "updated_at"}
OTHER_CHAT_MESSAGES = {"id", "conversation_id", "sender", "body", "created_at"}
# Columns of agent_configs an older version of this app did not have yet.
LATER_AGENT_COLUMNS = {
    "tier", "share_tools", "expose_invoke", "result_schema_json", "invoke_limits_json",
}
SCHEMA = "pydantic_agent"


def _table(name):
    return agents_db.Base.metadata.tables[name]


def _model_columns(name):
    return {c.name for c in _table(name).columns}


def test_another_apps_tables_are_foreign():
    conversations = agents_db.classify_table(_table("chat_conversations"), OTHER_CHAT_CONVERSATIONS)
    assert conversations == ("foreign", "principal")
    messages = agents_db.classify_table(_table("chat_messages"), OTHER_CHAT_MESSAGES)
    assert messages == ("foreign", "position")


def test_an_older_shape_of_ours_is_ours():
    columns = _model_columns("agent_configs") - LATER_AGENT_COLUMNS
    assert agents_db.classify_table(_table("agent_configs"), columns) == ("ours", None)


def test_an_identical_shape_is_ours():
    columns = _model_columns("skill_configs")
    assert agents_db.classify_table(_table("skill_configs"), columns) == ("ours", None)


def test_a_widened_table_refuses():
    columns = _model_columns("skill_configs") | {"legacy_col"}
    assert agents_db.classify_table(_table("skill_configs"), columns) == ("refused", "legacy_col")


async def test_no_column_the_migrations_add_is_required(monkeypatch):
    """The ownership rule relies on it: a column ``_ensure_column`` adds
    later is missing from an older table of ours, so the model must not
    require it (NOT NULL without a server default), or that table would be
    taken for another app's. The columns are the real chain's calls,
    recorded, not a copy of them."""
    added = []

    async def record(conn, table, column, ddl_type):
        added.append((table, column, ddl_type))

    monkeypatch.setattr(agents_db, "_ensure_column", record)
    monkeypatch.setattr(agents_db, "_target", dataclasses.replace(agents_db._target, schema=None))
    await agents_db._create_and_migrate()
    # Every call site ran (none sits behind a branch SQLite skips).
    source = inspect.getsource(agents_db._create_and_migrate)
    assert len(added) == len(re.findall(r"await _ensure_column\(", source))
    pairs = {(table, column) for table, column, _ in added}
    assert {("agent_configs", "auth_mode"), ("agent_configs", "run_timeout_seconds")} <= pairs
    assert sum("NOT NULL" in ddl.upper() for _, _, ddl in added) >= 15
    for table, column, _ in added:
        assert column not in agents_db.required_columns(_table(table)), (table, column)


def _plan(in_schema, in_source, fks=()):
    return agents_db.plan_schema_adoption(
        agents_db.Base.metadata, in_schema=in_schema, in_source=in_source,
        foreign_keys=fks, source="public", target=SCHEMA,
    )


def test_the_planner_moves_ours_skips_the_rest_and_is_empty_afterwards():
    in_source = {
        "agent_configs": _model_columns("agent_configs") - LATER_AGENT_COLUMNS,
        "chat_conversations": OTHER_CHAT_CONVERSATIONS,
        "skill_configs": _model_columns("skill_configs"),
    }
    plan = _plan({"skill_configs"}, in_source)
    assert plan.moves == ["agent_configs"]
    assert plan.foreign == [("chat_conversations", "principal")]
    assert plan.refused == [] and plan.duplicates == ["skill_configs"]
    assert plan.foreign_twins == [] and plan.dangling == [] and plan.shared == []
    assert plan.pending() == ["agent_configs"]
    done = _plan(set(in_source) | {"job_runs"}, {})
    assert done == agents_db.AdoptionPlan([], [], [], [])


def test_a_foreign_twin_is_not_a_duplicate():
    # Every start after the first on a shared instance: ours lives in the
    # schema, the other app's stays in public.
    in_schema = {"chat_conversations", "skill_configs"}
    in_source = {"chat_conversations": OTHER_CHAT_CONVERSATIONS,
                 "skill_configs": _model_columns("skill_configs")}
    plan = _plan(in_schema, in_source)
    assert plan.foreign_twins == [("chat_conversations", "principal")]
    assert plan.duplicates == ["skill_configs"]
    assert plan.foreign == [] and plan.moves == [] and plan.pending() == []


def test_a_foreign_key_into_a_table_that_will_not_be_in_the_schema_is_dangling():
    # A chat_files of ours that an earlier start without PG_SCHEMA created in
    # public, next to another app's chat_conversations it then referenced.
    in_source = {"chat_files": _model_columns("chat_files"),
                 "chat_conversations": OTHER_CHAT_CONVERSATIONS,
                 "skill_configs": _model_columns("skill_configs")}
    fks = [("public", "chat_files", "chat_files_conversation_id_fkey",
            "public", "chat_conversations")]
    plan = _plan(set(), in_source, fks)
    assert plan.dangling == [
        ("chat_files", "chat_files_conversation_id_fkey", "public.chat_conversations")
    ]
    assert "chat_files" not in plan.moves and "skill_configs" in plan.moves
    assert plan.held == ["chat_files"]
    assert plan.pending() == ["chat_files", "skill_configs"]


def test_a_foreign_key_into_a_moved_or_adopted_table_is_fine():
    in_source = {"ide_messages": _model_columns("ide_messages"),
                 "ide_sessions": _model_columns("ide_sessions"),
                 "chat_files": _model_columns("chat_files")}
    fks = [("public", "ide_messages", "ide_messages_session_id_fkey", "public", "ide_sessions"),
           ("public", "chat_files", "chat_files_conversation_id_fkey", SCHEMA,
            "chat_conversations"),
           # Ours in the schema already, into a table of ours that moves: it
           # points into the schema afterwards.
           (SCHEMA, "ide_findings", "ide_findings_session_id_fkey", "public", "ide_sessions")]
    plan = _plan({"chat_conversations", "ide_findings"}, in_source, fks)
    assert plan.dangling == [] and plan.shared == []
    assert set(plan.moves) == {"ide_sessions", "chat_files", "ide_messages"}


@pytest.mark.parametrize("referrer", [
    ("public", "other_audit"),       # a table that stays in public
    ("public", "chat_conversations"),  # a foreign table of one of our names
    ("reporting", "agent_usage"),    # a table of a third schema
])
def test_a_foreign_key_into_a_table_that_would_move_is_shared(referrer):
    ref_schema, ref_table = referrer
    in_source = {"agent_configs": _model_columns("agent_configs"),
                 "chat_conversations": OTHER_CHAT_CONVERSATIONS,
                 "skill_configs": _model_columns("skill_configs")}
    fks = [(ref_schema, ref_table, "fk_agent", "public", "agent_configs")]
    plan = _plan(set(), in_source, fks)
    assert plan.shared == [(f"{ref_schema}.{ref_table}", "fk_agent", "agent_configs")]
    assert plan.moves == ["skill_configs"] and plan.held == ["agent_configs"]
    assert plan.pending() == ["agent_configs", "skill_configs"]


def test_a_self_reference_of_a_moving_table_is_fine():
    in_source = {"skill_configs": _model_columns("skill_configs")}
    fks = [("public", "skill_configs", "fk_self", "public", "skill_configs")]
    plan = _plan(set(), in_source, fks)
    assert plan.shared == [] and plan.dangling == [] and plan.moves == ["skill_configs"]


def test_moves_follow_dependency_order():
    # ide_messages references ide_sessions by foreign key (workflow_steps
    # names its workflow by id only, so that pair has no order to follow).
    plan = _plan(set(), {
        "ide_messages": _model_columns("ide_messages"),
        "ide_sessions": _model_columns("ide_sessions"),
    })
    assert plan.moves == ["ide_sessions", "ide_messages"]


# --- the startup step -------------------------------------------------------------

class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    """Records every statement; answers the catalogue reads from ``rows``
    (columns: ``(schema, table, column)``), ``fks`` (``(schema, table,
    constraint, referenced schema, referenced table)``) and whether the
    schema exists."""

    def __init__(self, rows, fks=(), *, schema_exists=False):
        self.rows, self.fks, self.statements = rows, list(fks), []
        self.schema_exists = schema_exists
        self.dialect = types.SimpleNamespace(name="postgresql")

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.statements.append((sql, dict(params or {})))
        if sql == str(pg_schema._CATALOGUE_COLUMNS):
            return _Result(self.rows)
        if sql == str(pg_schema._CATALOGUE_FOREIGN_KEYS):
            return _Result(self.fks)
        if sql == str(pg_schema._SCHEMA_EXISTS):
            return _Result([(1,)] if self.schema_exists else [])
        return _Result([])

    def texts(self):
        return [s for s, _ in self.statements]

    def ddl(self):
        return [s for s in self.texts() if s.startswith(("ALTER", "CREATE"))]


CATALOGUE = (
    [("public", "agent_configs", c)
     for c in sorted(_model_columns("agent_configs") - LATER_AGENT_COLUMNS)]
    + [("public", "chat_conversations", c) for c in sorted(OTHER_CHAT_CONVERSATIONS)]
    + [("public", "skill_configs", c) for c in sorted(_model_columns("skill_configs"))]
    + [(SCHEMA, "skill_configs", c) for c in sorted(_model_columns("skill_configs"))]
)
DANGLING_FKS = [("public", "chat_files", "chat_files_conversation_id_fkey",
                 "public", "chat_conversations")]
CHAT_FILES = [("public", "chat_files", c) for c in sorted(_model_columns("chat_files"))]


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_true_moves_ours_under_a_lock_timeout_it_resets(caplog):
    conn = FakeConn(CATALOGUE)
    with caplog.at_level(logging.INFO, logger="agents.pg_schema"):
        plan = await agents_db._adopt_schema(conn, SCHEMA, adopt=True)
    texts = conn.texts()
    assert sum(s == str(pg_schema._CATALOGUE_COLUMNS) for s in texts) == 1
    assert sum(s == str(pg_schema._CATALOGUE_FOREIGN_KEYS) for s in texts) == 1
    assert texts[-3:] == [
        "SET LOCAL lock_timeout = '10s'",
        'ALTER TABLE "public"."agent_configs" SET SCHEMA "pydantic_agent"',
        "SET LOCAL lock_timeout = DEFAULT",
    ]
    assert plan.moves == ["agent_configs"] and plan.duplicates == ["skill_configs"]
    assert "chat_conversations" in caplog.text and "principal" in caplog.text
    assert "skill_configs" in caplog.text and "also exists" in caplog.text


async def test_the_lock_comes_first_and_the_schema_is_created_only_when_missing():
    conn = FakeConn(CATALOGUE)
    await agents_db._adopt_schema(conn, SCHEMA, adopt=True)
    (first, params), *_ = conn.statements
    assert first == "SELECT pg_advisory_xact_lock(:key)"
    assert params == {"key": pg_schema.ADVISORY_LOCK_KEY}
    texts = conn.texts()
    create = texts.index('CREATE SCHEMA IF NOT EXISTS "pydantic_agent"')
    assert texts.index(str(pg_schema._SCHEMA_EXISTS)) < create
    assert texts.index(str(pg_schema._CATALOGUE_FOREIGN_KEYS)) < create
    assert create < next(i for i, s in enumerate(texts) if s.startswith("ALTER TABLE"))
    assert conn.statements[texts.index(str(pg_schema._SCHEMA_EXISTS))][1] == {"schema": SCHEMA}

    existing = FakeConn(CATALOGUE, schema_exists=True)
    await agents_db._adopt_schema(existing, SCHEMA, adopt=True)
    assert not any(s.startswith("CREATE SCHEMA") for s in existing.texts())
    assert existing.texts()[0] == "SELECT pg_advisory_xact_lock(:key)"


async def test_the_reads_use_pg_catalog_for_base_tables_and_keys_into_the_source():
    conn = FakeConn([])
    await agents_db._adopt_schema(conn, SCHEMA, source_schema="src")
    columns, params = next((s, p) for s, p in conn.statements
                           if s == str(pg_schema._CATALOGUE_COLUMNS))
    assert "pg_catalog.pg_class" in columns and "relkind IN ('r', 'p')" in columns
    assert "information_schema" not in columns
    assert params == {"schema": SCHEMA, "source": "src"}
    fks, params = next((s, p) for s, p in conn.statements
                       if s == str(pg_schema._CATALOGUE_FOREIGN_KEYS))
    assert "contype = 'f'" in fks and params == {"source": "src"}
    # Keys of the source's tables AND keys into them, from any schema.
    assert "n.nspname = :source OR fn.nspname = :source" in fks


async def test_unset_refuses_when_anything_would_move_and_changes_nothing(caplog):
    conn = FakeConn(CATALOGUE + CHAT_FILES, DANGLING_FKS)
    with caplog.at_level(logging.INFO, logger="agents.pg_schema"):
        with pytest.raises(DatabaseConfigError) as info:
            await agents_db._adopt_schema(conn, SCHEMA)
    message = str(info.value)
    assert "PG_SCHEMA_ADOPT" in message and "2 tables" in message
    assert "true" in message and "false" in message
    assert conn.ddl() == [] and not any("lock_timeout" in s for s in conn.texts())


async def test_unset_starts_when_nothing_would_move(caplog):
    rows = ([("public", "chat_conversations", c) for c in sorted(OTHER_CHAT_CONVERSATIONS)]
            + [(SCHEMA, "skill_configs", c) for c in sorted(_model_columns("skill_configs"))])
    conn = FakeConn(rows, schema_exists=True)
    with caplog.at_level(logging.INFO, logger="agents.pg_schema"):
        plan = await agents_db._adopt_schema(conn, SCHEMA)
    assert plan.foreign == [("chat_conversations", "principal")] and conn.ddl() == []
    assert [r.levelno for r in _warnings(caplog)] == [logging.WARNING]


async def test_false_leaves_every_public_table_with_one_warning_each(caplog):
    widened = [("public", "workflows", c)
               for c in sorted(_model_columns("workflows") | {"legacy_col"})]
    shared_fk = [("public", "other_audit", "fk_agent", "public", "agent_configs")]
    conn = FakeConn(CATALOGUE + CHAT_FILES + widened, DANGLING_FKS + shared_fk)
    with caplog.at_level(logging.INFO, logger="agents.pg_schema"):
        plan = await agents_db._adopt_schema(conn, SCHEMA, adopt=False)
    assert conn.ddl() == ['CREATE SCHEMA IF NOT EXISTS "pydantic_agent"']
    assert not any("lock_timeout" in s for s in conn.texts())
    warned = sorted(r.args[1] for r in _warnings(caplog))
    assert warned == ["agent_configs", "chat_conversations", "chat_files", "workflows"]
    assert all("PG_SCHEMA_ADOPT" in r.getMessage() for r in _warnings(caplog))
    # The public twin of a table already in the schema: said once, below WARNING.
    assert "skill_configs" in caplog.text
    assert plan.pending() == ["agent_configs", "chat_files", "workflows"]


async def test_a_refused_table_stops_the_start_before_any_change():
    widened = sorted(_model_columns("workflows") | {"legacy_col"})
    conn = FakeConn(CATALOGUE + [("public", "workflows", c) for c in widened])
    with pytest.raises(DatabaseConfigError, match="legacy_col"):
        await agents_db._adopt_schema(conn, SCHEMA, adopt=True)
    assert conn.ddl() == []


async def test_a_dangling_foreign_key_stops_the_start_before_any_change():
    conn = FakeConn(CATALOGUE + CHAT_FILES, DANGLING_FKS)
    with pytest.raises(DatabaseConfigError) as info:
        await agents_db._adopt_schema(conn, SCHEMA, adopt=True)
    assert "chat_files_conversation_id_fkey" in str(info.value)
    assert "public.chat_conversations" in str(info.value)
    assert conn.ddl() == []


async def test_a_shared_table_stops_the_start_before_any_change():
    fks = [("reporting", "agent_usage", "fk_agent", "public", "agent_configs")]
    conn = FakeConn(CATALOGUE, fks)
    with pytest.raises(DatabaseConfigError) as info:
        await agents_db._adopt_schema(conn, SCHEMA, adopt=True)
    message = str(info.value)
    assert "reporting.agent_usage" in message and "fk_agent" in message
    assert "public.agent_configs" in message and "PG_SCHEMA_ADOPT" in message
    assert conn.ddl() == []


async def test_no_lock_timeout_is_set_when_nothing_moves():
    conn = FakeConn([])
    await agents_db._adopt_schema(conn, SCHEMA, adopt=True)
    assert not any("lock_timeout" in s for s in conn.texts())


async def test_a_foreign_twin_is_logged_below_warning(caplog):
    rows = (
        [("public", "chat_conversations", c) for c in sorted(OTHER_CHAT_CONVERSATIONS)]
        + [(SCHEMA, "chat_conversations", c)
           for c in sorted(_model_columns("chat_conversations"))]
    )
    conn = FakeConn(rows, schema_exists=True)
    with caplog.at_level(logging.INFO, logger="agents.pg_schema"):
        plan = await agents_db._adopt_schema(conn, SCHEMA)
    assert plan.foreign_twins == [("chat_conversations", "principal")]
    assert not _warnings(caplog)
    assert "chat_conversations" in caplog.text


async def test_nothing_runs_on_sqlite():
    async with agents_db.engine.begin() as conn:  # the suite's SQLite engine
        plan = await agents_db._adopt_schema(conn, SCHEMA, adopt=True)
    assert plan == agents_db.AdoptionPlan([], [], [], [])


async def test_create_and_migrate_runs_the_step_only_with_a_schema(monkeypatch):
    calls = []

    async def fake(conn, schema, *, adopt=None, source_schema="public"):
        calls.append((schema, adopt))
        return agents_db.AdoptionPlan([], [], [], [])

    monkeypatch.setattr(agents_db, "_adopt_schema", fake)
    with_schema = dataclasses.replace(agents_db._target, schema=SCHEMA, schema_adopt=True)
    monkeypatch.setattr(agents_db, "_target", with_schema)
    await agents_db._create_and_migrate()
    assert calls == [(SCHEMA, True)]
    calls.clear()
    monkeypatch.setattr(agents_db, "_target", dataclasses.replace(agents_db._target, schema=None))
    await agents_db._create_and_migrate()
    assert calls == []
