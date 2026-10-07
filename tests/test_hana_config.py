"""Which database the app uses: PostgreSQL, SAP HANA (HDI container) or SQLite.

The choice is made from the environment while ``agents.db`` is imported
(``_resolve_database``), so these tests call the resolver with an environment
of their own; the suite's engine is not touched. Every credential below is
made up.

Run:  python -m pytest tests/test_hana_config.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()

from sqlalchemy.engine import make_url  # noqa: E402

from agents import db as agents_db  # noqa: E402
from agents import hana_hdi  # noqa: E402
from agents.db import DatabaseConfigError, OrchestratorConfig, SessionLocal  # noqa: E402

PG_PASSWORD = "pg-s3cret"
RT_PASSWORD = "rt-p@ss/w:rd?"
DT_PASSWORD = "dt-s3cret"
CERTIFICATE = "-----BEGIN CERTIFICATE-----\nMIIFAKE\n-----END CERTIFICATE-----"
SECRETS = (PG_PASSWORD, RT_PASSWORD, DT_PASSWORD, "MIIFAKE")

POSTGRES = {"postgresql-db": [{"credentials": {
    "hostname": "pg.example.internal", "port": 5432, "username": "pguser",
    "password": PG_PASSWORD, "dbname": "appdb", "sslrootcert": "PGCA",
}}]}
HANA = {"hana": [{"plan": "hdi-shared", "credentials": {
    "host": "hana.example.internal", "port": "443", "schema": "CONTAINER_1",
    "user": "CONTAINER_1_RT", "password": RT_PASSWORD,
    "hdi_user": "CONTAINER_1_DT", "hdi_password": DT_PASSWORD,
    "certificate": CERTIFICATE, "driver": "com.sap.db.jdbc.Driver",
    "url": "jdbc:sap://hana.example.internal:443?encrypt=true",
}}]}


@pytest.fixture
def env(monkeypatch):
    """A clean database environment; returns a setter for it."""
    for name in ("VCAP_SERVICES", "VCAP_APPLICATION", "DATABASE_URL", "DB_KIND",
                 "HANA_HDI_USER", "HANA_HDI_PASSWORD"):
        monkeypatch.delenv(name, raising=False)

    def set_(**values):
        for name, value in values.items():
            if name == "VCAP_SERVICES" and not isinstance(value, str):
                value = json.dumps(value)
            monkeypatch.setenv(name, value)

    return set_


# --- one binding ----------------------------------------------------------------


def test_a_postgres_binding_is_resolved_as_before(env):
    env(VCAP_SERVICES=POSTGRES)
    target = agents_db._resolve_database()
    assert target.kind == "postgres" and target.hdi is None
    assert target.url == (
        f"postgresql+asyncpg://pguser:{PG_PASSWORD}@pg.example.internal:5432/appdb?ssl=require"
    )
    assert target.ssl_ca == "PGCA"
    assert agents_db._resolve_database_url() == target.url
    url, connect_args = agents_db._engine_settings(target)
    assert url == target.url.partition("?")[0]
    assert set(connect_args) == {"ssl"}


def test_a_hana_binding_gives_the_runtime_user_and_the_design_time_user(env):
    env(VCAP_SERVICES=HANA)
    target = agents_db._resolve_database()
    assert target.kind == "hana"
    url = make_url(target.url)
    assert url.drivername == "hana+aiohdbcli"
    assert (url.username, url.password) == ("CONTAINER_1_RT", RT_PASSWORD)
    assert (url.host, url.port) == ("hana.example.internal", 443)
    assert dict(url.query) == {
        "encrypt": "true", "sslValidateCertificate": "true",
        "currentSchema": "CONTAINER_1",
    }
    assert target.hdi == hana_hdi.HdiCredentials(
        host="hana.example.internal", port=443, schema="CONTAINER_1",
        user="CONTAINER_1_DT", password=DT_PASSWORD, certificate=CERTIFICATE,
    )


def test_the_hana_certificate_is_the_trust_store_and_stays_out_of_the_url(env):
    env(VCAP_SERVICES=HANA)
    target = agents_db._resolve_database()
    url, connect_args = agents_db._engine_settings(target)
    assert connect_args == {"sslTrustStore": CERTIFICATE}
    assert "MIIFAKE" not in url and url == target.url


def test_the_unused_ca_global_is_gone():
    assert not hasattr(agents_db, "_vcap_ssl_ca")


def test_the_import_script_shows_the_database_without_its_password(env):
    source = (ROOT / "scripts" / "import_bundle.py").read_text()
    assert 'print(f"  db     {DATABASE_URL}")' not in source
    assert "hide_password=True" in source


def test_nothing_that_describes_the_target_shows_a_credential(env):
    env(VCAP_SERVICES=HANA)
    target = agents_db._resolve_database()
    shown = repr(target) + str(target) + repr(target.hdi) + str(target.hdi)
    assert not any(secret in shown for secret in SECRETS)
    assert "CONTAINER_1_DT" not in shown


def test_a_hana_binding_without_design_time_user_resolves_but_cannot_deploy(env):
    """Plan ``schema``: a runtime user only. Refused where tables are made."""
    creds = {k: v for k, v in HANA["hana"][0]["credentials"].items()
             if not k.startswith("hdi_")}
    env(VCAP_SERVICES={"hana": [{"credentials": creds}]})
    target = agents_db._resolve_database()
    assert target.kind == "hana" and target.hdi is None


def test_an_incomplete_hana_binding_is_refused_not_replaced_by_sqlite(env):
    creds = dict(HANA["hana"][0]["credentials"], password="")
    env(VCAP_SERVICES={"hana": [{"credentials": creds}]},
        DATABASE_URL="sqlite+aiosqlite:///:memory:")
    with pytest.raises(DatabaseConfigError) as refused:
        agents_db._resolve_database()
    assert "password" in str(refused.value)
    assert not any(secret in str(refused.value) for secret in SECRETS)


def test_of_several_hana_bindings_the_hdi_container_is_taken(env):
    """A plain schema (no design-time user) bound next to the container, and
    listed first: the app can only create its tables in the container."""
    container = HANA["hana"][0]
    schema_only = {"plan": "schema", "credentials": {
        k: v for k, v in container["credentials"].items() if not k.startswith("hdi_")
    } | {"schema": "PLAIN_SCHEMA", "user": "PLAIN_USER"}}
    unnamed_plan = {"credentials": dict(container["credentials"], schema="BY_HDI_USER")}
    for instances, schema in (
        ([schema_only, container], "CONTAINER_1"),
        ([container, schema_only], "CONTAINER_1"),
        ([schema_only, unnamed_plan], "BY_HDI_USER"),  # recognised by its hdi_user
        ([schema_only], "PLAIN_SCHEMA"),  # nothing better: the first, refused at init_db
    ):
        env(VCAP_SERVICES={"hana": instances})
        target = agents_db._resolve_database()
        assert make_url(target.url).query["currentSchema"] == schema
        assert (target.hdi is not None) is (schema != "PLAIN_SCHEMA")


# --- on Cloud Foundry the app never ends on SQLite ------------------------------------

CF = '{"application_name": "app"}'


@pytest.mark.parametrize("services, database_url, says", [
    ("{not json", None, "VCAP_SERVICES is not valid JSON"),
    ("{not json", "postgresql+asyncpg://u:p@h:5432/d", "VCAP_SERVICES is not valid JSON"),
    ({"hana": [{"credentials": "oops"}]}, None, "credentials of the bound hana service"),
    ({"postgresql-db": [{"name": "x"}]}, None, "credentials of the bound postgresql-db"),
    ({"postgresql-db": ["oops"]}, "sqlite+aiosqlite:///:memory:", "credentials of the bound"),
    ({"xsuaa": [{"credentials": {}}]}, None, "no database service is bound"),
    ({}, None, "no database service is bound"),
    (None, None, "no database service is bound"),
    ({}, "sqlite+aiosqlite:///./agents_registry.db", "SQLite"),
])
def test_on_cloud_foundry_a_broken_or_missing_binding_refuses_the_start(
    env, services, database_url, says
):
    """Locally these end on SQLite; a deployed app would then run on a file
    that the next restart deletes, with nobody told."""
    values = {"VCAP_APPLICATION": CF}
    if services is not None:
        values["VCAP_SERVICES"] = services
    if database_url is not None:
        values["DATABASE_URL"] = database_url
    env(**values)
    with pytest.raises(DatabaseConfigError) as refused:
        agents_db._resolve_database()
    message = str(refused.value)
    assert says in message
    for value in ("oops", "u:p@h", "not json", "agents_registry"):
        assert value not in message
    assert refused.value.__cause__ is None


def test_on_cloud_foundry_a_binding_or_a_database_url_is_used_as_anywhere(env):
    env(VCAP_APPLICATION=CF, VCAP_SERVICES=HANA)
    assert agents_db._resolve_database().kind == "hana"
    env(VCAP_SERVICES=POSTGRES)
    assert agents_db._resolve_database().kind == "postgres"
    env(VCAP_SERVICES={}, DATABASE_URL="postgresql://u:p@h:5432/d")
    assert agents_db._resolve_database().kind == "postgres"


@pytest.mark.parametrize("services", ["{not json", {"hana": [{"credentials": "oops"}]}, {}])
def test_locally_the_same_environment_still_falls_back(env, services):
    """No VCAP_APPLICATION: a developer's machine, as before."""
    env(VCAP_SERVICES=services)
    assert agents_db._resolve_database().kind == "sqlite"
    env(VCAP_APPLICATION="")
    assert agents_db._resolve_database().kind == "sqlite"


# --- both bound -------------------------------------------------------------------


def test_both_bound_without_db_kind_refuses_the_start(env):
    env(VCAP_SERVICES={**POSTGRES, **HANA})
    with pytest.raises(DatabaseConfigError) as refused:
        agents_db._resolve_database()
    message = str(refused.value)
    assert "DB_KIND" in message and "'postgres'" in message and "'hana'" in message
    assert not any(secret in message for secret in SECRETS)
    assert "example.internal" not in message


@pytest.mark.parametrize("value, kind", [
    ("postgres", "postgres"), ("hana", "hana"), (" HANA ", "hana"), ("Postgres", "postgres"),
])
def test_db_kind_names_one_of_two_bound_databases(env, value, kind):
    env(VCAP_SERVICES={**POSTGRES, **HANA}, DB_KIND=value)
    assert agents_db._resolve_database().kind == kind


def test_db_kind_with_another_value_is_refused(env):
    env(VCAP_SERVICES={**POSTGRES, **HANA}, DB_KIND="oracle")
    with pytest.raises(DatabaseConfigError, match="DB_KIND must be 'postgres' or 'hana'"):
        agents_db._resolve_database()


@pytest.mark.parametrize("bound, wanted", [(POSTGRES, "hana"), (HANA, "postgres")])
def test_db_kind_naming_a_database_that_is_not_bound_is_refused(env, bound, wanted):
    """Never answered with the other one."""
    env(VCAP_SERVICES=bound, DB_KIND=wanted)
    with pytest.raises(DatabaseConfigError, match="no such database service is bound"):
        agents_db._resolve_database()


def test_db_kind_agreeing_with_the_one_binding_changes_nothing(env):
    env(VCAP_SERVICES=HANA, DB_KIND="hana")
    assert agents_db._resolve_database().kind == "hana"


def test_importing_the_database_layer_fails_when_both_are_bound():
    """The refusal is the app not starting, with a message and no secret."""
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "VCAP_SERVICES": json.dumps({**POSTGRES, **HANA}),
    }
    result = subprocess.run(
        [sys.executable, "-c", "import agents.db"], cwd=ROOT, env=environment,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode != 0
    assert "DatabaseConfigError" in result.stderr and "DB_KIND" in result.stderr
    assert not any(secret in result.stdout + result.stderr for secret in SECRETS)


# --- DATABASE_URL -------------------------------------------------------------------


def test_without_anything_the_local_sqlite_file_is_used(env):
    target = agents_db._resolve_database()
    assert (target.kind, target.url) == ("sqlite", "sqlite+aiosqlite:///./agents_registry.db")


@pytest.mark.parametrize("given, resolved", [
    ("postgres://u:p@h:5432/d", "postgresql+asyncpg://u:p@h:5432/d"),
    ("postgresql://u:p@h:5432/d", "postgresql+asyncpg://u:p@h:5432/d"),
    ("postgresql+asyncpg://u:p@h:5432/d", "postgresql+asyncpg://u:p@h:5432/d"),
])
def test_a_postgres_url_is_normalised_as_before(env, given, resolved):
    env(DATABASE_URL=given)
    target = agents_db._resolve_database()
    assert (target.kind, target.url) == ("postgres", resolved)


def test_db_kind_is_about_bindings_only(env):
    env(DATABASE_URL="sqlite+aiosqlite:///:memory:", DB_KIND="hana")
    assert agents_db._resolve_database().kind == "sqlite"


def test_a_binding_wins_over_database_url(env):
    env(VCAP_SERVICES=HANA, DATABASE_URL="sqlite+aiosqlite:///:memory:")
    assert agents_db._resolve_database().kind == "hana"


@pytest.mark.parametrize("scheme", ["hana", "hana+hdbcli", "hana+aiohdbcli"])
def test_a_hana_url_always_gets_the_async_driver_and_validated_tls(env, scheme):
    env(DATABASE_URL=f"{scheme}://RT:secret@hana.example.internal:443/?currentSchema=C1")
    target = agents_db._resolve_database()
    url = make_url(target.url)
    assert target.kind == "hana" and url.drivername == "hana+aiohdbcli"
    assert url.query["encrypt"] == "true"
    assert url.query["sslValidateCertificate"] == "true"
    assert url.query["currentSchema"] == "C1"
    assert target.hdi is None  # no design-time user given


@pytest.mark.parametrize("query", [
    "encrypt=false", "sslValidateCertificate=false",
    "encrypt=false&sslValidateCertificate=false", "encrypt=0",
    # The driver reads its options whatever their case: a second spelling
    # next to the fixed one must not survive.
    "ENCRYPT=false", "Encrypt=FALSE&SSLVALIDATECERTIFICATE=false",
    "sslvalidatecertificate=false", "encrypt=true&ENCRYPT=false",
    # Validated, but against any host name: not validated.
    "sslHostNameInCertificate=*", "SSLHOSTNAMEINCERTIFICATE=other.example",
    "ENCRYPT=false&sslHostNameInCertificate=*",
])
def test_a_hana_url_cannot_switch_tls_off(env, query, caplog):
    env(DATABASE_URL=f"hana://RT:secret@hana.example.internal:443/?currentSchema=C1&{query}")
    with caplog.at_level("WARNING", logger="agents.db"):
        url = make_url(agents_db._resolve_database().url)
    assert dict(url.query) == {
        "currentSchema": "C1", "encrypt": "true", "sslValidateCertificate": "true",
    }
    assert "encrypted and validated" in caplog.text
    assert "secret" not in caplog.text and "example.internal" not in caplog.text


def test_a_hana_url_that_asks_for_what_it_gets_is_not_warned_about(env, caplog):
    env(DATABASE_URL="hana://RT:secret@hana.example.internal:443/"
                     "?currentSchema=C1&ENCRYPT=TRUE&sslValidateCertificate=true")
    with caplog.at_level("WARNING", logger="agents.db"):
        url = make_url(agents_db._resolve_database().url)
    assert dict(url.query) == {
        "currentSchema": "C1", "encrypt": "true", "sslValidateCertificate": "true",
    }
    assert caplog.text == ""


def test_a_hana_url_takes_the_design_time_user_from_the_environment(env):
    env(DATABASE_URL="hana://RT:secret@hana.example.internal:443/?currentSchema=C1",
        HANA_HDI_USER="C1_DT", HANA_HDI_PASSWORD=DT_PASSWORD)
    target = agents_db._resolve_database()
    assert target.hdi == hana_hdi.HdiCredentials(
        host="hana.example.internal", port=443, schema="C1", user="C1_DT",
        password=DT_PASSWORD,
    )


def test_a_hana_url_without_schema_has_no_container_to_deploy_to(env):
    env(DATABASE_URL="hana://RT:secret@hana.example.internal:443",
        HANA_HDI_USER="C1_DT", HANA_HDI_PASSWORD=DT_PASSWORD)
    assert agents_db._resolve_database().hdi is None


def test_a_malformed_hana_url_is_refused_without_repeating_it(env):
    env(DATABASE_URL="hana://RT:secret@hana.example.internal:notaport")
    with pytest.raises(DatabaseConfigError) as refused:
        agents_db._resolve_database()
    assert "secret" not in str(refused.value)
    assert refused.value.__cause__ is None and refused.value.__suppress_context__


def test_a_hana_url_without_host_is_refused(env):
    env(DATABASE_URL="hana://")
    with pytest.raises(DatabaseConfigError, match="names no host"):
        agents_db._resolve_database()


# --- init_db on HANA ------------------------------------------------------------------


class _HanaEngine:
    """Stands in for the engine: only its dialect name is read. ``begin`` is
    how the other path starts ``create_all``; reaching it is the failure."""

    dialect = type("Dialect", (), {"name": "hana"})()

    def begin(self):
        raise AssertionError("create_all / _ensure_column must not run on HANA")


@pytest.fixture
async def hana_init(monkeypatch):
    await agents_db.init_db()  # the suite's SQLite, for the bootstrap row
    calls: list[tuple] = []

    async def deploy(credentials, metadata):
        calls.append((credentials, metadata))

    monkeypatch.setattr(agents_db, "engine", _HanaEngine())
    monkeypatch.setattr(agents_db.hana_hdi, "deploy", deploy)
    for step in ("_backfill_ide_revisions", "_resync_ide_revisions", "_ensure_column"):
        monkeypatch.setattr(agents_db, step, _HanaEngine().begin)
    return calls


async def test_init_db_on_hana_deploys_the_models_instead_of_create_all(hana_init, monkeypatch):
    credentials = hana_hdi.HdiCredentials("h", 443, "C1", "C1_DT", DT_PASSWORD)
    monkeypatch.setattr(agents_db, "_hana_hdi", credentials)
    await agents_db.init_db()
    assert hana_init == [(credentials, agents_db.Base.metadata)]
    assert "ide_sessions" in agents_db.Base.metadata.tables  # IDE models registered
    # The step every database needs still ran (on the suite's SQLite here).
    async with SessionLocal() as session:
        assert await session.get(OrchestratorConfig, 1) is not None


async def test_init_db_on_hana_without_design_time_user_is_refused(hana_init, monkeypatch):
    monkeypatch.setattr(agents_db, "_hana_hdi", None)
    with pytest.raises(DatabaseConfigError) as refused:
        await agents_db.init_db()
    assert "hdi-shared" in str(refused.value) and "HANA_HDI_USER" in str(refused.value)
    assert hana_init == []


async def test_a_failed_deploy_stops_the_start(hana_init, monkeypatch):
    async def failing(credentials, metadata):
        raise hana_hdi.HdiError("HDI MAKE failed: src/x.hdbtable: boom")

    monkeypatch.setattr(agents_db, "_hana_hdi",
                        hana_hdi.HdiCredentials("h", 443, "C1", "C1_DT", DT_PASSWORD))
    monkeypatch.setattr(agents_db.hana_hdi, "deploy", failing)
    with pytest.raises(hana_hdi.HdiError, match="boom"):
        await agents_db.init_db()


async def test_init_db_elsewhere_is_the_path_it_always_was(monkeypatch):
    async def never(*args, **kwargs):
        raise AssertionError("no HDI deploy on SQLite or Postgres")

    monkeypatch.setattr(agents_db.hana_hdi, "deploy", never)
    await agents_db.init_db()
    await agents_db.init_db()
