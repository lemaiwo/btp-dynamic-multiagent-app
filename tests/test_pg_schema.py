"""``PG_SCHEMA``: a Postgres schema of our own on a shared instance.

The setting rides on the resolved ``DatabaseTarget`` and becomes asyncpg's
``search_path``; at startup the app adopts the tables it already has in
``public`` into the schema (design:
docs/superpowers/specs/2026-11-04-postgres-schema-design.md). Every value
below is made up, except the two recorded shapes of the other app's tables
on EC (probe of 2026-11-04).

Run:  python -m pytest tests/test_pg_schema.py -q
"""

from __future__ import annotations

import dataclasses
import json
import logging
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
from agents.db import DatabaseConfigError  # noqa: E402
from tests.test_hana_config import HANA, POSTGRES  # noqa: E402


@pytest.fixture
def env(monkeypatch):
    """A clean database environment; returns a setter for it."""
    for name in ("VCAP_SERVICES", "VCAP_APPLICATION", "DATABASE_URL", "DB_KIND",
                 "HANA_HDI_USER", "HANA_HDI_PASSWORD", "PG_SCHEMA"):
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


# --- the ownership rule and the planner ------------------------------------------

# The other app's two tables on EC, as recorded by the probe of 2026-11-04
# (after our failed starts had added agent_ref and access to the first one).
THEIR_CHAT_CONVERSATIONS = {"id", "user_id", "title", "created_at", "updated_at", "agent_ref", "access"}
THEIR_CHAT_MESSAGES = {"id", "conversation_id", "role", "content", "created_at"}
# Columns of agent_configs that 2.22.0 (EC's version) did not have yet.
LATER_AGENT_COLUMNS = {"tier", "share_tools", "expose_invoke", "result_schema_json", "invoke_limits_json"}


def _table(name):
    return agents_db.Base.metadata.tables[name]


def _model_columns(name):
    return {c.name for c in _table(name).columns}


def test_the_other_apps_tables_are_foreign():
    assert agents_db.classify_table(_table("chat_conversations"), THEIR_CHAT_CONVERSATIONS) == ("foreign", "principal")
    assert agents_db.classify_table(_table("chat_messages"), THEIR_CHAT_MESSAGES) == ("foreign", "position")


def test_a_2_22_0_shaped_table_of_ours_is_ours():
    columns = _model_columns("agent_configs") - LATER_AGENT_COLUMNS
    assert agents_db.classify_table(_table("agent_configs"), columns) == ("ours", None)


def test_an_identical_shape_is_ours():
    assert agents_db.classify_table(_table("skill_configs"), _model_columns("skill_configs")) == ("ours", None)


def test_a_widened_table_refuses():
    columns = _model_columns("skill_configs") | {"legacy_col"}
    assert agents_db.classify_table(_table("skill_configs"), columns) == ("refused", "legacy_col")


def test_every_later_not_null_column_has_a_server_default():
    # The rule relies on it: a column _ensure_column adds as NOT NULL must not be "required".
    ensured = {
        ("agent_configs", "tier"), ("agent_configs", "share_tools"), ("agent_configs", "expose_invoke"),
        ("agent_configs", "expose_api"), ("agent_configs", "expose_chat"),
        ("chat_conversations", "access"), ("chat_user_agents", "deep"),
        ("ide_conventions", "non_production"), ("ide_sessions", "session_type"),
        ("ide_workspace_files", "revision"), ("odata_services", "kind"),
        ("orchestrator_config", "embeddings_enabled"), ("workflow_steps", "kind"),
    }
    for table, column in ensured:
        assert column not in agents_db.required_columns(_table(table)), (table, column)


def test_the_planner_moves_ours_skips_the_rest_and_is_empty_afterwards():
    md = agents_db.Base.metadata
    in_source = {
        "agent_configs": _model_columns("agent_configs") - LATER_AGENT_COLUMNS,
        "chat_conversations": THEIR_CHAT_CONVERSATIONS,
        "skill_configs": _model_columns("skill_configs"),
    }
    plan = agents_db.plan_schema_adoption(md, in_schema={"skill_configs"}, in_source=in_source)
    assert plan.moves == ["agent_configs"]
    assert plan.foreign == [("chat_conversations", "principal")]
    assert plan.refused == [] and plan.duplicates == ["skill_configs"]
    done = agents_db.plan_schema_adoption(md, in_schema=set(in_source) | {"job_runs"}, in_source={})
    assert done == agents_db.AdoptionPlan([], [], [], [])


def test_moves_follow_dependency_order():
    # ide_messages references ide_sessions by foreign key (workflow_steps
    # names its workflow by id only, so that pair has no order to follow).
    md = agents_db.Base.metadata
    plan = agents_db.plan_schema_adoption(md, in_schema=set(), in_source={
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


class FakeConn:
    """Records every statement; answers the catalogue read from ``rows``."""

    def __init__(self, rows):
        self.rows, self.statements = rows, []
        self.dialect = types.SimpleNamespace(name="postgresql")

    async def execute(self, stmt, params=None):
        self.statements.append((str(stmt), dict(params or {})))
        if "information_schema.columns" in str(stmt):
            return _Result(self.rows)
        return _Result([])


CATALOGUE = (
    [("public", "agent_configs", c) for c in sorted(_model_columns("agent_configs") - LATER_AGENT_COLUMNS)]
    + [("public", "chat_conversations", c) for c in sorted(THEIR_CHAT_CONVERSATIONS)]
    + [("public", "skill_configs", c) for c in sorted(_model_columns("skill_configs"))]
    + [("pydantic_agent", "skill_configs", c) for c in sorted(_model_columns("skill_configs"))]
)


async def test_the_step_creates_the_schema_reads_once_and_moves_ours(caplog):
    conn = FakeConn(CATALOGUE)
    with caplog.at_level(logging.INFO, logger="agents.db"):
        plan = await agents_db._adopt_schema(conn, "pydantic_agent")
    texts = [s for s, _ in conn.statements]
    assert texts[0] == 'CREATE SCHEMA IF NOT EXISTS "pydantic_agent"'
    assert sum("information_schema.columns" in s for s in texts) == 1
    assert texts[-1] == 'ALTER TABLE "public"."agent_configs" SET SCHEMA "pydantic_agent"'
    assert plan.moves == ["agent_configs"] and plan.duplicates == ["skill_configs"]
    assert "chat_conversations" in caplog.text and "principal" in caplog.text
    assert "skill_configs" in caplog.text and "also exists" in caplog.text


async def test_the_read_filters_base_tables_and_both_schemas():
    conn = FakeConn([])
    await agents_db._adopt_schema(conn, "pydantic_agent", source_schema="src")
    stmt, params = next((s, p) for s, p in conn.statements if "information_schema.columns" in s)
    assert "table_type = 'BASE TABLE'" in stmt
    assert params == {"schema": "pydantic_agent", "source": "src"}


async def test_a_refused_table_stops_the_start_before_any_move():
    rows = CATALOGUE + [("public", "workflows", c) for c in sorted(_model_columns("workflows") | {"legacy_col"})]
    conn = FakeConn(rows)
    with pytest.raises(DatabaseConfigError, match="legacy_col"):
        await agents_db._adopt_schema(conn, "pydantic_agent")
    assert not any(s.startswith("ALTER TABLE") for s, _ in conn.statements)


async def test_nothing_runs_on_sqlite():
    async with agents_db.engine.begin() as conn:  # the suite's SQLite engine
        plan = await agents_db._adopt_schema(conn, "pydantic_agent")
    assert plan == agents_db.AdoptionPlan([], [], [], [])


async def test_create_and_migrate_runs_the_step_only_with_a_schema(monkeypatch):
    calls = []

    async def fake(conn, schema, *, source_schema="public"):
        calls.append(schema)
        return agents_db.AdoptionPlan([], [], [], [])

    monkeypatch.setattr(agents_db, "_adopt_schema", fake)
    monkeypatch.setattr(agents_db, "_target", dataclasses.replace(agents_db._target, schema="pydantic_agent"))
    await agents_db._create_and_migrate()
    assert calls == ["pydantic_agent"]
    calls.clear()
    monkeypatch.setattr(agents_db, "_target", dataclasses.replace(agents_db._target, schema=None))
    await agents_db._create_and_migrate()
    assert calls == []
