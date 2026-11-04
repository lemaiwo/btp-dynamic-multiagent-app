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

import json
import sys
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
