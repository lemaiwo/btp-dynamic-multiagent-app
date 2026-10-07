"""What only a real SAP HANA HDI container can show.

SKIPPED unless ``TEST_HANA_SERVICE_KEY`` holds the service key of a
throw-away ``hdi-shared`` container; how to run it, how the key is wired in
and what it must never point at is in ``tests/hana.py``. One container, so
this suite runs serially and leaves the schema deployed and every table
empty.

Three things are proven here that the SQLite suites cannot prove:

* ``init_db`` deploys the models through HDI, twice without a change, and
  undeploys an artifact that is no longer generated;
* the statements that replaced ``RETURNING`` and the comparisons on a
  ``Text`` column (``tests/test_hana_portable_sql.py`` proves their shape)
  really run on ``NCLOB`` columns and keep their guarantees when two
  connections race;
* the scenarios of ``tests/test_odata_postgres.py`` hold on HANA as well:
  ``FOR UPDATE`` and ``FOR SHARE LOCK`` wait, a stamp survives, the audit
  rows are written once.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()

import app as app_module  # noqa: E402
from agents import admin as admin_module  # noqa: E402
from agents import db as agents_db  # noqa: E402
from agents import hana_hdi  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    Base,
    ODataAuditLog,
    ODataService,
    OrchestratorConfig,
    SkillConfig,
    create_odata_service,
    existing_odata_service_names,
    get_odata_service,
    validate_odata_service,
)
from agents.ide import runner, stages, store  # noqa: E402
from agents.ide import seed as seed_module  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeAuditLog,
    IdeComment,
    IdeSession,
    IdeWorkspaceFile,
    utcnow,
)
from agents.odata import admin_routes  # noqa: E402
from agents.odata.audit import StoredWriteRecorder  # noqa: E402
from agents.odata.tools import WriteAudit  # noqa: E402
from tests import hana as hana_db  # noqa: E402
from tests import ide_concurrency  # noqa: E402
from tests.odata_helpers import service_payload  # noqa: E402

pytestmark = pytest.mark.skipif(hana_db.service_key() is None, reason=hana_db.SKIP_REASON)

OWNER = "alice"
BASE = "/admin/api/odata/services"
ONE = f"{BASE}/stock-levels"
FIELD = "expected_updated_at"
STALE = "Service 'stock-levels' was changed since it was loaded; reload it and save again"
# How long a statement that must WAIT for a lock is watched not finishing.
WAITS = 1.5
INDEX = "ix_ide_comments_session_state"

_deployed = False


@pytest.fixture
async def hana(monkeypatch):
    """The container, bound the way a ``hana`` binding binds it: ``init_db``
    and every module that opens sessions use it for this test. The schema is
    deployed once per run; every test starts and ends with empty tables."""
    global _deployed
    async with hana_db.container() as bound:
        monkeypatch.setattr(agents_db, "engine", bound.engine)
        monkeypatch.setattr(agents_db, "_hana_hdi", bound.hdi)
        for module in (agents_db, runner, seed_module, admin_routes, admin_module):
            monkeypatch.setattr(module, "SessionLocal", bound.sessions)
        if not _deployed:
            await agents_db.init_db()
            _deployed = True
        await bound.empty()
        try:
            yield bound
        finally:
            await bound.empty()


@pytest.fixture
def deploys(monkeypatch):
    """What each ``init_db`` of the test did in the container."""
    results: list[hana_hdi.DeployResult] = []
    real = hana_hdi.deploy

    async def spy(credentials, metadata):
        result = await real(credentials, metadata)
        results.append(result)
        return result

    monkeypatch.setattr(hana_hdi, "deploy", spy)
    return results


async def _index_exists(bound, name: str) -> bool:
    async with bound.sessions() as s:
        count = (await s.execute(
            text("SELECT COUNT(*) FROM SYS.INDEXES WHERE SCHEMA_NAME = CURRENT_SCHEMA "
                 "AND INDEX_NAME = :name"), {"name": name.upper()})).scalar_one()
    return count == 1


# --- init_db: deploy, idempotent, undeploy --------------------------------------------


async def test_init_db_deploys_is_idempotent_and_undeploys_what_is_gone(
    hana, deploys, monkeypatch
):
    generated = hana_hdi.artifacts(Base.metadata)
    path = f"src/{INDEX}.hdbindex"
    assert path in generated

    # Twice in a row on a container that holds the schema: nothing to do.
    await agents_db.init_db()
    await agents_db.init_db()
    assert [r.changed for r in deploys] == [False, False]
    assert deploys[-1].deployed == tuple(generated)
    assert await _index_exists(hana, INDEX)
    async with hana.sessions() as s:
        assert (await s.get(OrchestratorConfig, 1)).instructions

    # One index leaves the generated set: the third run undeploys it.
    real = hana_hdi.artifacts
    monkeypatch.setattr(
        hana_hdi, "artifacts",
        lambda metadata: {p: c for p, c in real(metadata).items() if p != path},
    )
    try:
        await agents_db.init_db()
        assert deploys[-1].changed and deploys[-1].undeployed == (path,)
        assert not await _index_exists(hana, INDEX)
        # Still idempotent with the smaller set.
        await agents_db.init_db()
        assert not deploys[-1].changed
    finally:
        # Back to the real models, whatever happened above.
        monkeypatch.setattr(hana_hdi, "artifacts", real)
        await agents_db.init_db()
    assert deploys[-1].changed and deploys[-1].undeployed == ()
    assert await _index_exists(hana, INDEX)


async def test_two_instances_starting_together_both_end_with_the_schema(
    hana, deploys, monkeypatch
):
    """Both find the container behind and both deploy; HDI refuses one of
    them something the other already did. Neither start may fail."""
    path = f"src/{INDEX}.hdbindex"
    real = hana_hdi.artifacts
    monkeypatch.setattr(
        hana_hdi, "artifacts",
        lambda metadata: {p: c for p, c in real(metadata).items() if p != path},
    )
    try:
        await asyncio.gather(agents_db.init_db(), agents_db.init_db())
        assert not await _index_exists(hana, INDEX)
    finally:
        monkeypatch.setattr(hana_hdi, "artifacts", real)
        await asyncio.gather(agents_db.init_db(), agents_db.init_db())
    assert await _index_exists(hana, INDEX)
    await agents_db.init_db()
    assert not deploys[-1].changed


async def test_a_refused_artifact_fails_with_hdis_message_and_changes_nothing(
    hana, deploys, monkeypatch
):
    real = hana_hdi.artifacts
    broken = "src/zz_broken.hdbtable"
    monkeypatch.setattr(
        hana_hdi, "artifacts",
        lambda metadata: {
            **real(metadata),
            broken: "COLUMN TABLE zz_broken (\n\tid NOSUCHTYPE\n)\n",
        },
    )
    with pytest.raises(hana_hdi.HdiError) as failed:
        await agents_db.init_db()
    message = str(failed.value)
    assert message.startswith("HDI MAKE failed: ") and broken in message
    for secret in (hana.hdi.password, hana.hdi.certificate or "\0", hana.hdi.host):
        assert secret not in message
    # The make changed nothing deployed: the next start with the real models
    # has nothing to make or undeploy, and removes the file that was refused.
    monkeypatch.setattr(hana_hdi, "artifacts", real)
    await agents_db.init_db()
    assert not deploys[-1].changed
    assert await _work_files(hana) == set(real(Base.metadata))


# --- one transaction, the container lock, the file system ------------------------------

GEN = hana_hdi.HANA_SCHEMA_GENERATION
PROBE_INDEX = "src/zz_probe_ix.hdbindex"
PROBE_INDEX_DDL = "INDEX zz_probe_ix ON ide_sessions (title)\n"
PROBE_TABLE = "src/zz_guard_probe.hdbtable"
PROBE_TABLE_DDL = "COLUMN TABLE zz_guard_probe (\n\tid INTEGER,\n\tPRIMARY KEY (id)\n)\n"


def _generated() -> dict[str, str]:
    return hana_hdi.artifacts(Base.metadata)


async def _deploy(bound, files: dict[str, str], *, generation: int = GEN,
                  allow_drop: bool = False) -> hana_hdi.DeployResult:
    return await asyncio.to_thread(
        hana_hdi.deploy_files, bound.hdi, files, generation=generation,
        allow_drop=allow_drop)


def _design_time(bound, procedure: str, like: str, rows: list[tuple]) -> list:
    """One HDI call of its own, committed: how a test puts the container's
    file system into a state a deployment has to cope with."""
    connection = hana_hdi._connect(bound.hdi)
    try:
        cursor = connection.cursor()
        cursor.execute(f"CREATE LOCAL TEMPORARY COLUMN TABLE #T LIKE _SYS_DI.{like}")
        hana_hdi._fill(cursor, "#T", rows)
        out = "?, ?, ?, ?" if procedure in ("LIST", "LIST_DEPLOYED", "READ") else "?, ?, ?"
        sets = hana_hdi._call(
            cursor,
            f'CALL "{bound.hdi.schema}#DI".{procedure}(#T, _SYS_DI.T_NO_PARAMETERS, {out})')
        connection.commit()
        return sets
    finally:
        connection.close()


async def _work_files(bound) -> set[str]:
    sets = await asyncio.to_thread(
        _design_time, bound, "LIST", "TT_FILESFOLDERS", [(hana_hdi.ROOT,)])
    return set(hana_hdi._listing("LIST", sets))


async def _record(bound) -> hana_hdi._Record:
    sets = await asyncio.to_thread(
        _design_time, bound, "READ", "TT_FILESFOLDERS", [(hana_hdi.GENERATION_FILE,)])
    return hana_hdi._parse_record(sets)


async def _recorded_generation(bound) -> int:
    return (await _record(bound)).generation


async def _write_record(bound, content: bytes) -> None:
    await asyncio.to_thread(
        _design_time, bound, "WRITE", "TT_FILESFOLDERS_CONTENT",
        [(hana_hdi.GENERATION_FILE, content)])


async def _record_generation(bound, generation: int, *, made: bool = True,
                             files: dict[str, str] | None = None) -> None:
    """Write the record directly: no deploy ever lowers it."""
    digest = hana_hdi.schema_digest(files if files is not None else _generated())
    await _write_record(bound, hana_hdi._record_bytes(generation, made, digest))


async def _table_exists(bound, name: str) -> bool:
    async with bound.sessions() as s:
        count = (await s.execute(
            text("SELECT COUNT(*) FROM SYS.TABLES WHERE SCHEMA_NAME = CURRENT_SCHEMA "
                 "AND TABLE_NAME = :name"), {"name": name.upper()})).scalar_one()
    return count == 1


async def test_the_design_time_connection_does_not_autocommit(hana):
    """hdbcli connects with autocommit ON; the container lock lives in the
    client's transaction, so the deploy's connection must not."""
    from hdbcli import dbapi

    def defaults() -> tuple[bool, bool]:
        plain = dbapi.connect(
            address=hana.hdi.host, port=hana.hdi.port, user=hana.hdi.user,
            password=hana.hdi.password, encrypt=True, sslValidateCertificate=True,
            sslTrustStore=hana.hdi.certificate)
        ours = hana_hdi._connect(hana.hdi)
        try:
            return plain.getautocommit(), ours.getautocommit()
        finally:
            plain.close()
            ours.close()

    assert await asyncio.to_thread(defaults) == (True, False)


async def test_the_container_records_the_generation_it_holds(hana):
    await agents_db.init_db()
    assert await _record(hana) == hana_hdi._Record(
        GEN, True, hana_hdi.load_history()[GEN])
    assert hana_hdi.load_history()[GEN] == hana_hdi.schema_digest(_generated())


async def test_the_container_lock_outlives_hdis_own_commits(hana):
    """HDI commits every call itself. If that also ended the lock, two
    deploys would only be serialised until the first WRITE. Measured here:
    a second connection cannot take the lock after the holder's WRITE,
    DELETE and MAKE, and gets it the moment the holder commits."""
    api = f'"{hana.hdi.schema}#DI"'
    path = "meta/zz_lock_probe.txt"

    def probe() -> list[tuple[str, object]]:
        seen: list[tuple[str, object]] = []
        holder, other = hana_hdi._connect(hana.hdi), hana_hdi._connect(hana.hdi)
        try:
            a, b = holder.cursor(), other.cursor()
            for name, like in (("#W", "TT_FILESFOLDERS_CONTENT"), ("#F", "TT_FILESFOLDERS"),
                               ("#E", "TT_FILESFOLDERS"), ("#P", "TT_FILESFOLDERS_PARAMETERS")):
                a.execute(f"CREATE LOCAL TEMPORARY COLUMN TABLE {name} LIKE _SYS_DI.{like}")
            hana_hdi._fill(a, "#W", [("meta/", None), (path, b"x")])
            hana_hdi._fill(a, "#F", [(path,)])
            holder.commit()

            def second(step: str) -> None:
                try:
                    hana_hdi._call(b, f"CALL {api}.LOCK(1500, _SYS_DI.T_NO_PARAMETERS, ?, ?, ?)")
                    seen.append((step, "got it"))
                except Exception as exc:  # noqa: BLE001 -- the code is the answer
                    seen.append((step, getattr(exc, "errorcode", None)))
                other.rollback()

            def first(step: str, arguments: str) -> None:
                sets = hana_hdi._call(
                    a, f"CALL {api}.{step}({arguments}_SYS_DI.T_NO_PARAMETERS, ?, ?, ?)")
                assert hana_hdi._errors(sets, hana.hdi) == [], step
                second(f"after {step}")

            first("LOCK", "30000, ")
            first("WRITE", "#W, ")
            first("DELETE", "#F, ")
            first("MAKE", "#E, #E, #P, ")
            holder.commit()
            second("after the commit")
            return seen
        finally:
            holder.close()
            other.close()

    assert await asyncio.to_thread(probe) == [
        ("after LOCK", 131), ("after WRITE", 131), ("after DELETE", 131),
        ("after MAKE", 131), ("after the commit", "got it"),
    ]


async def test_no_record_is_recognised_positively_and_then_written(hana):
    """A container without a record (fresh, or from before records): HDI's
    LIST answers its file-not-found codes, and only that counts as "none"."""
    generated = _generated()
    await asyncio.to_thread(
        _design_time, hana, "DELETE", "TT_FILESFOLDERS", [(hana_hdi.GENERATION_FILE,)])
    sets = await asyncio.to_thread(
        _design_time, hana, "LIST", "TT_FILESFOLDERS", [(hana_hdi.GENERATION_FILE,)])
    codes = {code for result in sets if "MESSAGE_CODE" in result.columns
             for severity, code in zip(result.column("SEVERITY"),
                                       result.column("MESSAGE_CODE"), strict=True)
             if severity == "ERROR"}
    assert hana_hdi._FILE_NOT_FOUND in codes and codes <= hana_hdi._NOT_FOUND_COMPANIONS
    assert hana_hdi._record_exists(sets, hana.hdi) is False
    # A folder that is not there is a different answer: not "no record".
    other = await asyncio.to_thread(
        _design_time, hana, "LIST", "TT_FILESFOLDERS", [("zz_no_such_folder/",)])
    with pytest.raises(hana_hdi.HdiError):
        hana_hdi._record_exists(other, hana.hdi)

    result = await _deploy(hana, generated)
    assert not result.changed
    assert await _record(hana) == hana_hdi._Record(GEN, True, hana_hdi.schema_digest(generated))


async def test_an_unreadable_record_stops_the_deploy(hana):
    generated = _generated()
    older = {p: c for p, c in generated.items() if "ide_comments" not in p}
    try:
        for content in (b"not json", b'{"generation": 1}'):
            await _write_record(hana, content)
            with pytest.raises(hana_hdi.HdiError, match="schema record .* cannot be read"):
                await _deploy(hana, older, allow_drop=True)
            assert await _table_exists(hana, "ide_comments")
            assert await _work_files(hana) == set(generated)
    finally:
        await _record_generation(hana, GEN)
    assert not (await _deploy(hana, generated)).changed


async def test_a_newer_generation_that_is_not_made_or_not_deployed_refuses_the_start(hana):
    """The record is ahead of the schema: an older version neither deploys
    nor starts. ``init_db`` raises, so the app does not come up."""
    generated = _generated()
    older = {p: c for p, c in generated.items() if "ide_comments" not in p}
    future = {**generated, "src/zz_future.hdbindex": "INDEX zz_future ON ide_sessions (title)\n"}
    try:
        for made, files in ((False, generated), (True, future)):
            await _record_generation(hana, GEN + 1, made=made, files=files)
            for allow_drop in (False, True):
                with pytest.raises(hana_hdi.HdiError, match="unfinished or changed deployment"):
                    await _deploy(hana, older, allow_drop=allow_drop)
            with pytest.raises(hana_hdi.HdiError, match=f"schema generation {GEN + 1}"):
                await agents_db.init_db()
            assert await _table_exists(hana, "ide_comments")
            assert await _index_exists(hana, INDEX)
            assert await _work_files(hana) == set(generated)
            assert (await _record(hana)).generation == GEN + 1
        # The version of that generation finishes it (here: nothing to make).
        await _record_generation(hana, GEN + 1, made=False)
        assert not (await _deploy(hana, generated, generation=GEN + 1)).changed
        assert await _record(hana) == hana_hdi._Record(
            GEN + 1, True, hana_hdi.schema_digest(generated))
        assert (await _deploy(hana, older)).newer_generation == GEN + 1
    finally:
        await _record_generation(hana, GEN)


async def test_hdi_commits_its_own_calls_whatever_the_client_does(hana):
    """The fact the deploy is built around: a WRITE is still there after
    the client's ROLLBACK."""
    path = "src/zz_rolled_back.hdbindex"

    def write_and_roll_back() -> None:
        connection = hana_hdi._connect(hana.hdi)
        try:
            cursor = connection.cursor()
            cursor.execute(
                "CREATE LOCAL TEMPORARY COLUMN TABLE #T LIKE _SYS_DI.TT_FILESFOLDERS_CONTENT")
            hana_hdi._fill(
                cursor, "#T", [(path, b"INDEX zz_rolled_back ON ide_sessions (target)\n")])
            connection.commit()
            hana_hdi._call(
                cursor,
                f'CALL "{hana.hdi.schema}#DI".WRITE(#T, _SYS_DI.T_NO_PARAMETERS, ?, ?, ?)')
            connection.rollback()
        finally:
            connection.close()

    generated = _generated()
    try:
        await asyncio.to_thread(write_and_roll_back)
        assert path in await _work_files(hana)
    finally:
        # Nothing to make; the leftover file is removed all the same.
        assert not (await _deploy(hana, generated)).changed
    assert await _work_files(hana) == set(generated)


async def test_a_failed_make_after_a_delete_leaves_a_container_the_next_deploy_can_use(hana):
    """A stale file (deleted from the file system by the deploy) plus one
    broken artifact (refused by the make). HDI has committed the DELETE and
    the WRITE; the make changed nothing deployed. The file system and the
    deployed state now differ, and a clean deploy afterwards must cope: no
    DELETE of the file that is gone, the stale index undeployed, the broken
    file removed."""
    generated = _generated()
    try:
        await _deploy(hana, {**generated, PROBE_INDEX: PROBE_INDEX_DDL})
        assert await _index_exists(hana, "zz_probe_ix")

        broken = {**generated, "src/zz_broken.hdbindex": "INDEX zz_broken ON no_such_table (x)\n"}
        with pytest.raises(hana_hdi.HdiError, match="HDI MAKE failed"):
            await _deploy(hana, broken)
        # Deployed: untouched. File system: as the failed deploy left it.
        assert await _index_exists(hana, "zz_probe_ix")
        assert PROBE_INDEX not in await _work_files(hana)
        assert "src/zz_broken.hdbindex" in await _work_files(hana)
        # And failing again changes nothing about that.
        with pytest.raises(hana_hdi.HdiError, match="HDI MAKE failed"):
            await _deploy(hana, broken)
    finally:
        result = await _deploy(hana, generated)
    assert result.changed and result.undeployed == (PROBE_INDEX,)
    assert not await _index_exists(hana, "zz_probe_ix")
    assert await _work_files(hana) == set(generated)
    assert not (await _deploy(hana, generated)).changed


async def test_file_system_and_deployed_state_may_differ(hana):
    """A deployed file that is gone from the file system is undeployed
    without a DELETE (HDI refuses to delete what is not there); a file that
    was written but never made is deleted and is nothing to undeploy."""
    generated = _generated()
    leftover = "src/zz_leftover.hdbindex"
    try:
        await _deploy(hana, {**generated, PROBE_INDEX: PROBE_INDEX_DDL})
        await asyncio.to_thread(
            _design_time, hana, "DELETE", "TT_FILESFOLDERS", [(PROBE_INDEX,)])
        await asyncio.to_thread(
            _design_time, hana, "WRITE", "TT_FILESFOLDERS_CONTENT",
            [(leftover, b"INDEX zz_leftover ON ide_sessions (target)\n")])
        assert leftover in await _work_files(hana)
        assert PROBE_INDEX not in await _work_files(hana)
        assert await _index_exists(hana, "zz_probe_ix")
    finally:
        result = await _deploy(hana, generated)
    assert result.changed and result.undeployed == (PROBE_INDEX,)
    assert not await _index_exists(hana, "zz_probe_ix")
    assert not await _index_exists(hana, "zz_leftover")
    assert await _work_files(hana) == set(generated)


async def test_an_older_app_version_changes_nothing_and_rows_survive(hana):
    """The container holds generation N+1; an app of generation N starts
    with one index and one table less. Nothing is written, made or
    undeployed, and the rows of the table it does not know are still there."""
    generated = _generated()
    sid = await ide_concurrency.new_session(hana.sessions)
    comment = await ide_concurrency.new_comment(hana.sessions, sid)
    older = {p: c for p, c in generated.items() if "ide_comments" not in p}
    assert {f"src/{INDEX}.hdbindex", "src/ide_comments.hdbtable"} <= set(generated) - set(older)
    try:
        # A newer version with the same files only raises the record.
        newer = await _deploy(hana, generated, generation=GEN + 1)
        assert not newer.changed and await _recorded_generation(hana) == GEN + 1

        for allow_drop in (False, True):
            result = await _deploy(hana, older, generation=GEN, allow_drop=allow_drop)
            assert (result.changed, result.undeployed) == (False, ())
            assert result.newer_generation == GEN + 1
        assert await _index_exists(hana, INDEX)
        assert await _table_exists(hana, "ide_comments")
        assert await ide_concurrency.states(hana.sessions, sid) == {comment: "open"}
        assert await _recorded_generation(hana) == GEN + 1
        assert await _work_files(hana) == set(generated)
    finally:
        await _record_generation(hana, GEN)
    assert not (await _deploy(hana, generated)).changed


async def test_init_db_of_an_older_version_starts_on_the_newer_schema(hana, deploys, caplog):
    try:
        await _deploy(hana, _generated(), generation=GEN + 2)
        with caplog.at_level("WARNING", logger="agents.hana_hdi"):
            await agents_db.init_db()
        assert deploys[-1].newer_generation == GEN + 2
        assert f"holds schema generation {GEN + 2}" in caplog.text
        assert f"is generation {GEN}" in caplog.text
        async with hana.sessions() as s:
            assert await s.get(OrchestratorConfig, 1) is not None
    finally:
        await _record_generation(hana, GEN)


async def test_a_table_is_dropped_only_with_the_switch(hana, monkeypatch):
    generated = _generated()
    try:
        await _deploy(hana, {**generated, PROBE_TABLE: PROBE_TABLE_DDL})
        assert await _table_exists(hana, "zz_guard_probe")

        monkeypatch.delenv("HANA_HDI_ALLOW_DROP", raising=False)
        with pytest.raises(hana_hdi.HdiError) as refused:
            await agents_db.init_db()
        assert "would drop the table(s) zz_guard_probe" in str(refused.value)
        assert "HANA_HDI_ALLOW_DROP=true" in str(refused.value)
        assert await _table_exists(hana, "zz_guard_probe")
        assert PROBE_TABLE in await _work_files(hana)

        monkeypatch.setenv("HANA_HDI_ALLOW_DROP", "true")
        await agents_db.init_db()
        assert not await _table_exists(hana, "zz_guard_probe")
    finally:
        await _deploy(hana, generated, allow_drop=True)
    assert await _work_files(hana) == set(generated)


async def test_the_runtime_user_cannot_run_ddl(hana):
    """Why the schema goes through HDI at all."""
    with pytest.raises(DBAPIError):
        async with hana.engine.begin() as connection:
            await connection.execute(text("CREATE TABLE zz_not_allowed (a INTEGER)"))


async def test_every_table_and_column_of_the_models_is_there(hana):
    async with hana.sessions() as s:
        for table in Base.metadata.sorted_tables:
            assert (await s.execute(select(table).limit(1))).all() == [], table.name


async def test_the_heartbeat_runs(hana):
    async with hana.sessions() as s:
        assert (await s.execute(app_module.HEARTBEAT)).scalar_one() == 1


# --- helpers ------------------------------------------------------------------------------


async def _session(bound, stage: str = "design", **values) -> str:
    async with bound.sessions() as db:
        s = await store.create_session(db, owner=OWNER, title="t", target="DEMO")
        s.stage = stage
        await db.commit()
        if values:
            await db.execute(update(IdeSession).where(IdeSession.id == s.id).values(**values))
            await db.commit()
        return s.id


async def _comment(bound, sid: str, *, state: str = "open", run: str | None = None,
                   body: str = "b") -> str:
    async with bound.sessions() as db:
        c = IdeComment(session_id=sid, anchor="document", kind="design", version=1,
                       paragraph=0, body=body, state=state, sent_run_id=run)
        db.add(c)
        await db.commit()
        return c.id


async def _states(bound, sid: str) -> dict[str, str]:
    async with bound.sessions() as db:
        rows = await db.execute(
            select(IdeComment.id, IdeComment.state).where(IdeComment.session_id == sid))
        return dict(rows.all())


async def _stored(bound, sid: str, column: str):
    async with bound.sessions() as db:
        return (await db.execute(
            select(getattr(IdeSession, column)).where(IdeSession.id == sid)
        )).scalar_one_or_none()


# --- the statements that replaced RETURNING ---------------------------------------------------


async def test_purge_removes_old_idle_sessions_and_their_children(hana):
    old = utcnow() - timedelta(days=90)
    gone = await _session(hana, updated_at=old)
    busy = await _session(hana, updated_at=old, status="running", run_id="r")
    fresh = await _session(hana)
    kept = {sid: await _comment(hana, sid, body="x" * 6000) for sid in (gone, busy, fresh)}
    async with hana.sessions() as db:
        assert await store.purge_sessions_older_than(db, 30) == 1
        assert await store.purge_sessions_older_than(db, 30) == 0
    async with hana.sessions() as db:
        assert set((await db.execute(select(IdeSession.id))).scalars()) == {busy, fresh}
        assert set((await db.execute(select(IdeComment.id))).scalars()) == {
            kept[busy], kept[fresh]}


async def test_audit_purge_counts_what_it_deleted(hana):
    async with hana.sessions() as db:
        for age in (400, 400, 1):
            row = await store.add_audit(
                db, principal="p", session_id="s", target="T",
                action=store.AUDIT_ACTIONS[0], params={}, outcome="ok")
            await db.execute(update(IdeAuditLog).where(IdeAuditLog.id == row.id)
                             .values(ts=utcnow() - timedelta(days=age)))
        await db.commit()
        assert await store.purge_audit_older_than(db, 365) == 2
        assert await store.purge_audit_older_than(db, 365) == 0


async def test_startup_reset_frees_ghosts_and_reopens_their_comments(hana):
    ghost = await _session(hana, status="running", run_id="dead",
                           updated_at=utcnow() - timedelta(hours=1))
    live = await _session(hana, status="running", run_id="live")
    sent = {sid: await _comment(hana, sid, state="sent", run=run)
            for sid, run in ((ghost, "dead"), (live, "live"))}
    async with hana.sessions() as db:
        assert await store.reset_running_ide_sessions(db, min_age_s=60) == 1
    assert await _stored(hana, ghost, "status") == "idle"
    assert await _stored(hana, live, "status") == "running"
    assert await _states(hana, ghost) == {sent[ghost]: "open"}
    assert await _states(hana, live) == {sent[live]: "sent"}


async def test_comments_are_sent_and_reopened_by_run(hana):
    sid = await _session(hana)
    first, second = await _comment(hana, sid), await _comment(hana, sid)
    other = await _comment(hana, sid, state="sent", run="run-0")
    async with hana.sessions() as db:
        moved = await store.mark_comments_sent(db, sid, "run-1")
        await db.commit()
    assert sorted(c.id for c in moved) == sorted([first, second])
    assert {(c.state, c.sent_run_id) for c in moved} == {("sent", "run-1")}
    async with hana.sessions() as db:
        reopened = await store.reopen_sent_comments(db, sid, run_id="run-1")
        await db.commit()
    assert sorted(reopened) == sorted([first, second])
    assert await _states(hana, sid) == {first: "open", second: "open", other: "sent"}


# --- Text columns: pins, approve, seed --------------------------------------------------------


async def test_pins_and_approve_work_on_an_nclob_column(hana):
    sid = await _session(hana)
    async with hana.sessions() as db:
        session = await db.get(IdeSession, sid)
        await store.set_pin(db, session, "design", 1)
        await store.set_file_pins(db, session, {"src/CLAS/zcl_a.clas.abap": 2})
    assert json.loads(await _stored(hana, sid, "pins_json")) == {
        "design": 1, "files": {"src/CLAS/zcl_a.clas.abap": 2}}

    sid = await _session(hana)
    async with hana.sessions() as db:
        await store.add_artifact(db, sid, stage="design", kind="design", content="# D")
        session = await db.get(IdeSession, sid)
        assert (await stages.approve(db, session)).stage == "plan"
        await store.add_artifact(db, sid, stage="plan", kind="plan", content="# P")
        assert (await stages.approve(db, session)).stage == "propose"
    assert json.loads(await _stored(hana, sid, "pins_json")) == {"design": 1, "plan": 1}


async def test_two_concurrent_pin_writers_both_land(hana):
    """Compare-and-set under a row lock: neither overwrites the other."""
    sid = await _session(hana)

    async def pin(kind: str, version: int) -> None:
        async with hana.sessions() as db:
            session = await db.get(IdeSession, sid)
            await store.set_pin(db, session, kind, version)

    for round_ in range(1, 4):
        await asyncio.gather(pin("design", round_), pin("plan", round_),
                             pin("review", round_))
        assert json.loads(await _stored(hana, sid, "pins_json")) == {
            "design": round_, "plan": round_, "review": round_}


async def test_a_text_compare_waits_for_the_writer_and_then_sees_its_change(hana):
    """``text_unchanged`` locks the row it compares: a second compare of the
    same row waits for the first transaction and then reads what it wrote."""
    async with hana.sessions() as db:
        db.add(SkillConfig(name="portable-sk", description="d", content="v1"))
        await db.commit()
    second: asyncio.Task[bool] | None = None

    async def late() -> bool:
        async with hana.sessions() as db:
            done = await seed_module._refresh_text(
                db, SkillConfig, "content", "portable-sk", "v1", "from the second")
            await db.commit()
            return done

    try:
        async with hana.sessions() as first:
            assert await seed_module._refresh_text(
                first, SkillConfig, "content", "portable-sk", "v1", "from the first")
            second = asyncio.create_task(late())
            done, _ = await asyncio.wait({second}, timeout=WAITS)
            assert not done, "the second compare did not wait for the first writer"
            await first.commit()
        assert await asyncio.wait_for(second, timeout=30) is False
    finally:
        if second is not None and not second.done():
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
    async with hana.sessions() as db:
        assert (await db.execute(select(SkillConfig.content))).scalar_one() == "from the first"


async def test_the_seed_is_inserted_and_refreshed(hana, tmp_path):
    def seed(directory: str, version: int, text_: str, history=None) -> Path:
        folder = tmp_path / directory
        folder.mkdir()
        path = folder / "seed.ide.json"
        path.write_text(json.dumps({
            "version": version,
            "skills": [{"name": "hana-sk", "description": "d", "content": text_ + " skill"}],
            "agents": [{"name": "hana-ag", "description": "d", "instructions": text_,
                        "mcp_servers": [{"url": "http://example.invalid/mcp",
                                         "auth_mode": "none"}]}],
        }))
        if history is not None:
            (folder / "seed_history.json").write_text(json.dumps(history))
        return path

    long_text = "v1 " + "x" * 9000  # past any NVARCHAR(5000) shortcut
    assert await seed_module.ensure_ide_seed(seed("v1", 1, long_text)) == {
        "skills_added": 1, "agents_added": 1}
    history = {"agents": {"hana-ag": [seed_module.text_hash(long_text)]},
               "skills": {"hana-sk": [seed_module.text_hash(long_text + " skill")]}}
    result = await seed_module.refresh_ide_seed(seed("v2", 2, "v2 text", history))
    assert result == {"updated": ["skill:hana-sk", "agent:hana-ag"],
                      "skipped_edited": [], "added": []}
    again = await seed_module.refresh_ide_seed(seed("v3", 3, "v3 text", history))
    assert again["skipped_edited"] == ["skill:hana-sk", "agent:hana-ag"]
    async with hana.sessions() as db:
        assert (await db.execute(select(AgentConfig.instructions))).scalar_one() == "v2 text"


async def test_the_migration_repair_would_not_run_on_hana(hana):
    """Why ``init_db`` leaves ``_resync_ide_revisions`` out on HANA: it
    compares two NCLOB columns, which HANA refuses."""
    sid = await _session(hana)
    async with hana.sessions() as db:
        db.add(IdeWorkspaceFile(session_id=sid, path="src/CLAS/zcl_a.clas.abap",
                                state="new", proposed_source="a", revision=1,
                                object_type="CLAS", object_name="ZCL_A"))
        await db.commit()
    with pytest.raises(DBAPIError):
        async with hana.engine.begin() as connection:
            await agents_db._resync_ide_revisions(connection)


# --- the same races as on Postgres (tests/test_ide_postgres.py) -----------------------------


@pytest.mark.parametrize("scenario", ide_concurrency.SCENARIOS, ids=lambda s: s.__name__)
async def test_race(hana, scenario):
    await scenario(hana.sessions)


# --- the areas no other case touches: one round trip each -----------------------------------


async def test_job_runs_are_created_finished_and_listed(hana):
    async with hana.sessions() as s:
        agent = AgentConfig(name="job-agent", description="d", instructions="i",
                            mcp_url="http://example.invalid/mcp", enabled=1)
        s.add(agent)
        await s.commit()
        run = await agents_db.create_job_run(
            s, agent=agent, trigger="api", created_by="alice",
            scheduler={"host": "jobs.example.invalid", "job_id": "1", "schedule_id": "2",
                       "run_id": "3"})
        assert (await agents_db.active_job_run(s, agent.id)).id == run.id
        await agents_db.finish_job_run(
            s, run.id, status="succeeded", summary="s" * 6000,
            report={"sections": ["a"]}, activity={"tools": [{"name": "t"}]})
        assert await agents_db.active_job_run(s, agent.id) is None
        stored = await agents_db.get_job_run(s, run.id)
        assert (stored.status, len(stored.summary)) == ("succeeded", 6000)
        assert [r.id for r in await agents_db.list_job_runs(s, agent_id=agent.id)] == [run.id]
        assert [r.id for r in await agents_db.list_job_runs(s)] == [run.id]
        assert await agents_db.sweep_stale_runs(s, all_running=True) == 0


async def test_workflow_runs_with_item_and_step_runs(hana):
    async with hana.sessions() as s:
        workflow = agents_db.Workflow(name="wf", description="d", enabled=1)
        s.add(workflow)
        await s.commit()
        run = await agents_db.create_workflow_run(s, workflow=workflow, trigger="api",
                                                  created_by="alice")
        assert (await agents_db.active_workflow_run(s, workflow.id)).id == run.id
        item = await agents_db.create_item_run(
            s, run_id=run.id, item_key="k1", title="t" * 3000, branches=["abap"])
        step = await agents_db.create_step_run(
            s, run_id=run.id, item_run_id=item.id, branch_key="abap", position=1,
            agent_name="reader")
        await agents_db.finish_step_run(s, step.id, status="succeeded", output="o" * 9000)
        await agents_db.finish_item_run(s, item.id, status="succeeded")
        await agents_db.finish_workflow_run(
            s, run.id, status="succeeded", summary="done", counts={"items_total": 1})
        assert (await agents_db.get_workflow_run(s, run.id)).status == "succeeded"
        assert [i.id for i in await agents_db.list_item_runs(s, run.id)] == [item.id]
        steps = await agents_db.list_step_runs(s, run.id)
        assert [(x.id, len(x.output)) for x in steps] == [(step.id, 9000)]
        assert [r.id for r in await agents_db.list_workflow_runs(
            s, workflow_id=workflow.id)] == [run.id]
        assert await agents_db.active_workflow_run(s, workflow.id) is None
        assert await agents_db.sweep_stale_workflow_runs(s, all_running=True) == 0


async def test_oauth_tokens_states_and_clients_are_stored(hana):
    key = "https://mcp.example.invalid/mcp"
    soon = datetime.now(timezone.utc) + timedelta(minutes=10)
    async with hana.sessions() as s:
        await agents_db.upsert_user_token(
            s, user_id="alice", server_key=key, access_token="a" * 6000,
            refresh_token="r1", scope="read", expires_at=soon)
        await agents_db.upsert_user_token(
            s, user_id="alice", server_key=key, access_token="second",
            refresh_token=None, expires_at=soon)
        token = await agents_db.get_user_token(s, "alice", key)
        assert token.access_token == "second"
        assert set(await agents_db.get_user_tokens(s, "alice", [key])) == {key}
        await agents_db.delete_user_token(s, "alice", key)
        assert await agents_db.get_user_token(s, "alice", key) is None

        await agents_db.save_oauth_client(
            s, server_key=key, authorize_url="https://idp.example.invalid/authorize",
            token_url="https://idp.example.invalid/token", client_id="cid",
            client_secret=None, scope=None, redirect_uri="https://app.example.invalid/cb")
        assert (await agents_db.get_oauth_client(s, key)).client_id == "cid"
        await agents_db.delete_oauth_client(s, key)
        assert await agents_db.get_oauth_client(s, key) is None

        await agents_db.save_oauth_state(
            s, state="st-1", user_id="alice", server_key=key, code_verifier="v",
            redirect_uri="https://app.example.invalid/cb", expires_at=soon)
        popped = await agents_db.pop_oauth_state(s, "st-1")
        assert (popped.user_id, popped.code_verifier) == ("alice", "v")
        assert await agents_db.pop_oauth_state(s, "st-1") is None


@pytest.fixture
async def admin(hana, monkeypatch):
    """The admin router on a bare app (no lifespan). Reloading the registry
    is not what is under test: it is switched off."""
    async def no_reload(*args, **kwargs):
        return None

    monkeypatch.setattr(admin_module.registry, "reload", no_reload)
    app = FastAPI()
    app.include_router(admin_module.router)  # the router carries /admin itself
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


SERVERS = [{"url": "http://example.invalid/mcp", "auth_mode": "none"}]


async def test_agents_skills_and_workflows_through_the_admin_api(admin, hana):
    r = await admin.post("/admin/api/skills", json={
        "name": "hana-skill", "description": "d", "content": "c" * 7000})
    assert r.status_code == 201, r.text
    skill = r.json()
    r = await admin.put(f"/admin/api/skills/{skill['id']}", json={
        "name": "hana-skill", "description": "d2", "content": "changed"})
    assert r.status_code == 200, r.text

    agents = {}
    for name in ("reader", "drafter"):
        r = await admin.post("/admin/api/agents", json={
            "name": name, "description": f"{name} d", "instructions": "i" * 7000,
            "mcp_servers": SERVERS, "skills": ["hana-skill"],
            "run_as_principal": "svc@example.com"})
        assert r.status_code == 201, r.text
        agents[name] = r.json()
    r = await admin.put(f"/admin/api/agents/{agents['reader']['id']}", json={
        "name": "reader", "description": "changed", "instructions": "i2",
        "mcp_servers": SERVERS, "peers": ["drafter"], "api_slug": "reader",
        "run_as_principal": "svc@example.com"})
    assert r.status_code == 200, r.text
    listed = (await admin.get("/admin/api/agents")).json()
    assert sorted(a["name"] for a in listed) == ["drafter", "reader"]
    # A second agent with the same slug: the unique index answers.
    r = await admin.put(f"/admin/api/agents/{agents['drafter']['id']}", json={
        "name": "drafter", "description": "d", "instructions": "i",
        "mcp_servers": SERVERS, "api_slug": "reader",
        "run_as_principal": "svc@example.com"})
    assert r.status_code in (409, 422), r.text

    workflow = {
        "name": "triage", "description": "triage", "api_slug": "triage",
        "run_as_principal": "svc@example.com", "run_timeout_seconds": 1800,
        "skip_seen_items": True, "max_parallel_items": 1, "on_unknown_branch": "fail",
        "enabled": True,
        "branches": [{"key": "abap", "description": "ABAP", "position": 1}],
        "steps": [
            {"branch_key": None, "position": 1, "agent_name": "reader",
             "instructions": "triage", "fan_out": True, "step_timeout_seconds": 600},
            {"branch_key": None, "position": 2, "agent_name": "drafter",
             "instructions": "draft", "fan_out": False, "step_timeout_seconds": 600},
            {"branch_key": "abap", "position": 1, "agent_name": "reader",
             "instructions": "analyze", "fan_out": False, "step_timeout_seconds": 600},
        ],
    }
    r = await admin.post("/admin/api/workflows", json=workflow)
    assert r.status_code == 201, r.text
    wid = r.json()["id"]
    r = await admin.put(f"/admin/api/workflows/{wid}",
                        json={**workflow, "description": "changed"})
    assert r.status_code == 200, r.text
    assert (await admin.get(f"/admin/api/workflows/{wid}")).json()["description"] == "changed"
    assert len((await admin.get("/admin/api/workflows")).json()) == 1
    # In use by the workflow: the agent delete is refused, then allowed.
    r = await admin.delete(f"/admin/api/agents/{agents['drafter']['id']}")
    assert r.status_code == 409, r.text
    assert (await admin.delete(f"/admin/api/workflows/{wid}")).status_code == 204
    for agent in agents.values():
        assert (await admin.delete(f"/admin/api/agents/{agent['id']}")).status_code == 204
    assert (await admin.delete(f"/admin/api/skills/{skill['id']}")).status_code == 204


async def test_an_agent_attaches_a_catalogue_service_under_its_row_lock(admin, client, created):
    """The agent save checks the service with ``FOR SHARE LOCK`` and the
    catalogue write locks the row ``FOR UPDATE``; both through the routes."""
    entry = {"url": "builtin:odata", "auth_mode": "destination",
             "oauth": {"services": ["stock-levels"], "allow_write": True}}
    r = await admin.post("/admin/api/agents", json={
        "name": "buyer", "description": "d", "instructions": "i", "mcp_servers": [entry]})
    assert r.status_code == 201, r.text
    r = await client.put(ONE, json={**svc(title="Changed"), FIELD: created["updated_at"]})
    assert r.status_code == 200, r.text
    assert (await client.delete(ONE)).status_code == 409
    unknown = {**entry, "oauth": {"services": ["no-such-service"]}}
    r = await admin.post("/admin/api/agents", json={
        "name": "other", "description": "d", "instructions": "i", "mcp_servers": [unknown]})
    assert r.status_code == 422, r.text
    (buyer,) = (await admin.get("/admin/api/agents")).json()
    assert (await admin.delete(f"/admin/api/agents/{buyer['id']}")).status_code == 204
    assert (await client.delete(ONE)).status_code == 204


# --- scripts/copy_registry_config.py into the container ------------------------------------


async def test_the_configuration_is_copied_into_the_container_with_its_secrets(hana, tmp_path):
    """From a local SQLite file (its stand-in for the old database) into
    HANA: secrets arrive, ids are HANA's own, instants are the same, a
    second run finds nothing to do, and the way back is equal too."""
    from tests import registry_rows as rows
    from tests.test_copy_registry_config import Db, copy_script

    source = await Db(tmp_path / "old.db").create()
    back = await Db(tmp_path / "back.db").create()
    try:
        await rows.seed_source(source.sessions)
        await rows.seed_target(hana.sessions)

        planned = await copy_script.copy_config(source.engine, hana.engine, apply=False)
        async with hana.sessions() as s:
            skills = await s.execute(select(func.count()).select_from(SkillConfig))
            assert skills.scalar_one() == 0
        applied = await copy_script.copy_config(source.engine, hana.engine, apply=True)
        assert [p.line() for p in applied] == [p.line() for p in planned]
        assert {p.table: (len(p.insert), len(p.replace)) for p in applied} == {
            "skill_configs": (1, 0), "agent_configs": (1, 1), "orchestrator_config": (0, 1),
            "workflows": (1, 0), "odata_services": (1, 0), "ide_conventions": (1, 0),
            "mcp_oauth_clients": (1, 0), "mcp_oauth_tokens": (1, 0),
            "workflow_branches": (1, 0), "workflow_steps": (1, 0),
        }

        async with hana.sessions() as s:
            agents = {a.name: a for a in (await s.execute(select(AgentConfig))).scalars()}
            token = (await s.execute(select(agents_db.McpOAuthToken))).scalar_one()
            client_row = (await s.execute(select(agents_db.McpOAuthClient))).scalar_one()
            workflow = (await s.execute(select(agents_db.Workflow).where(
                agents_db.Workflow.name == "mail-triage"))).scalar_one()
            steps = list((await s.execute(select(agents_db.WorkflowStep).where(
                agents_db.WorkflowStep.workflow_id == workflow.id))).scalars())
            job_runs = (await s.execute(
                select(func.count()).select_from(agents_db.JobRun))).scalar_one()
        reader = agents["reader"]
        assert json.loads(reader.oauth_json)["client_secret"] == rows.MARKER_AGENT
        assert len(reader.instructions) == 6000
        assert (token.access_token, client_row.client_secret) == (
            rows.MARKER_TOKEN, rows.MARKER_CLIENT)
        assert agents["seeded"].instructions == "source text"
        assert {"local-0", "local-1", "local-2"} <= set(agents)
        assert instant(reader.created_at) == rows.STAMP  # to the microsecond
        assert instant(token.expires_at) == rows.STAMP
        assert sorted((x.branch_key or "", x.position) for x in steps) == [
            ("", 1), ("", 2), ("abap", 1)]
        assert job_runs == 0  # history is not copied

        again = await copy_script.copy_config(source.engine, hana.engine, apply=True)
        assert all(not p.insert and not p.replace for p in again), [p.line() for p in again]

        # HANA -> SQLite (naive UTC out, same instants in), then compared
        # with where it came from.
        await copy_script.copy_config(hana.engine, back.engine, apply=True)
        async with back.sessions() as s:
            returned = (await s.execute(
                select(AgentConfig).where(AgentConfig.name == "reader"))).scalar_one()
        assert instant(returned.created_at) == rows.STAMP
        assert returned.oauth_json == reader.oauth_json
    finally:
        await source.engine.dispose()
        await back.engine.dispose()


# --- the run lock --------------------------------------------------------------------------------


async def test_of_two_concurrent_run_starts_exactly_one_wins(hana):
    for _ in range(3):
        sid = await _session(hana, stage="chat")
        answers = await asyncio.gather(
            *(runner._start(sid, OWNER, f"question {n}") for n in range(4)),
            return_exceptions=True,
        )
        won = [a for a in answers if not isinstance(a, BaseException)]
        lost = [a for a in answers if isinstance(a, BaseException)]
        assert len(won) == 1, answers
        assert {type(a) for a in lost} == {stages.StageGateError}
        assert {a.code for a in lost} == {"run_in_progress"}
        assert await _stored(hana, sid, "run_id") == won[0].run_id


async def test_a_session_row_lock_makes_the_next_one_wait(hana):
    sid = await _session(hana)
    waiting: asyncio.Task[None] | None = None

    async def second() -> None:
        async with hana.sessions() as db:
            await store.lock_session_row(db, sid)
            await db.commit()

    try:
        async with hana.sessions() as first:
            await store.lock_session_row(first, sid)
            waiting = asyncio.create_task(second())
            done, _ = await asyncio.wait({waiting}, timeout=WAITS)
            assert not done, "the second FOR UPDATE did not wait"
            await first.commit()
        await asyncio.wait_for(waiting, timeout=30)
    finally:
        if waiting is not None and not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)


async def test_a_stale_run_lock_is_reclaimed_by_the_row_as_it_was_seen(hana):
    """The reclaim compares ``updated_at`` with the value it read: a stamp
    with microseconds must come back from a TIMESTAMP column unchanged."""
    stamp = (utcnow() - timedelta(days=1)).replace(microsecond=123457)
    sid = await _session(hana, status="running", run_id="dead", updated_at=stamp)
    sent = await _comment(hana, sid, state="sent", run="dead")
    assert await runner.release_stale(sid) is True
    assert await _stored(hana, sid, "status") == "idle"
    assert await _states(hana, sid) == {sent: "open"}
    assert await runner.release_stale(sid) is False


# --- the scenarios of tests/test_odata_postgres.py ------------------------------------------


def svc(**patch: Any) -> dict[str, Any]:
    return service_payload(name="stock-levels", **patch)


def instant(value: Any) -> datetime:
    """A stamp as a UTC instant; HANA hands timestamps back naive (UTC)."""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(
        value, str) else value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


@pytest.fixture
async def client(hana):
    """The catalogue router on a bare app (no lifespan, no other route)."""
    app = FastAPI()
    app.include_router(admin_routes.router, prefix="/admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def created(client):
    r = await client.post(BASE, json=svc())
    assert r.status_code == 201, r.text
    return r.json()


async def test_two_concurrent_saves_from_the_same_loaded_state_one_wins(client, created):
    for round_ in range(3):
        stamp = (await client.get(ONE)).json()["updated_at"]
        answers = await asyncio.gather(
            client.put(ONE, json={**svc(title=f"A{round_}"), FIELD: stamp}),
            client.put(ONE, json={**svc(title=f"B{round_}", user_context=True), FIELD: stamp}),
        )
        assert sorted(a.status_code for a in answers) == [200, 409], [a.text for a in answers]
        loser = next(a for a in answers if a.status_code == 409)
        assert loser.json() == {"detail": STALE}
        winner = next(a for a in answers if a.status_code == 200).json()
        outcome = {key: winner.pop(key, None) for key in ("reloaded", "reload_failed")}
        assert all(type(value) is bool for value in outcome.values()), outcome
        assert (await client.get(ONE)).json() == winner


async def test_the_stamp_a_client_got_back_is_never_a_false_409(client, created, hana):
    loaded = (await client.get(ONE)).json()
    assert loaded["updated_at"] == created["updated_at"]
    first = await client.put(ONE, json={**svc(title="One"), FIELD: loaded["updated_at"]})
    assert first.status_code == 200, first.text
    second = await client.put(ONE, json={**svc(title="Two"), FIELD: first.json()["updated_at"]})
    assert second.status_code == 200, second.text
    stamps = [instant(x["updated_at"]) for x in (loaded, first.json(), second.json())]
    assert stamps == sorted(set(stamps)) and len(stamps) == 3

    answered = second.json()["updated_at"]
    assert (await client.get(ONE)).json()["updated_at"] == answered
    assert [s["updated_at"] for s in (await client.get(BASE)).json()] == [answered]
    async with hana.sessions() as s:
        row = await get_odata_service(s, "stock-levels")
        assert instant(row.updated_at) == instant(answered)  # to the microsecond
    third = await client.put(ONE, json={**svc(title="Three"), FIELD: first.json()["updated_at"]})
    assert third.status_code == 409 and third.json() == {"detail": STALE}


async def test_a_stamp_survives_the_database_with_its_microsecond(client, created, monkeypatch):
    odd = datetime.now(timezone.utc).replace(microsecond=123457) + timedelta(seconds=5)
    monkeypatch.setattr(agents_db, "_odata_now", lambda: odd)
    r = await client.put(ONE, json={**svc(title="T"), FIELD: created["updated_at"]})
    assert r.status_code == 200, r.text
    assert instant(r.json()["updated_at"]) == odd
    assert instant((await client.get(ONE)).json()["updated_at"]) == odd


async def test_delete_waits_for_an_agent_save_and_then_finds_the_service_in_use(
    client, created, hana
):
    """The save holds ``FOR SHARE LOCK`` on the service it attaches; the
    delete's ``FOR UPDATE`` waits for its commit and then sees the agent."""
    delete_call: asyncio.Task[Any] | None = None
    try:
        async with hana.sessions() as save:
            found = await existing_odata_service_names(save, ["stock-levels"], lock=True)
            assert found == {"stock-levels"}
            save.add(AgentConfig(
                name="buyer", description="d", instructions="i", mcp_url="builtin:odata",
                auth_mode="destination",
                oauth_json=json.dumps({"services": ["stock-levels"]}), enabled=1,
            ))
            await save.flush()

            delete_call = asyncio.create_task(client.delete(ONE))
            done, _ = await asyncio.wait({delete_call}, timeout=WAITS)
            assert not done, "the delete did not wait for the agent save"
            await save.commit()

        answer = await asyncio.wait_for(delete_call, timeout=30)
        assert answer.status_code == 409
        assert answer.json()["detail"] == "Service 'stock-levels' is used by agent(s) 'buyer'"
        assert (await client.get(ONE)).status_code == 200
    finally:
        if delete_call is not None and not delete_call.done():
            delete_call.cancel()
            await asyncio.gather(delete_call, return_exceptions=True)


async def test_a_service_created_without_commit_goes_with_the_rollback(hana):
    async with hana.sessions() as s:
        await create_odata_service(s, validate_odata_service(svc()), commit=False)
        assert await get_odata_service(s, "stock-levels") is not None
        await s.rollback()
    async with hana.sessions() as s:
        assert await get_odata_service(s, "stock-levels") is None
        assert (await s.execute(select(func.count()).select_from(ODataService))).scalar_one() == 0


def record(**patch: Any) -> WriteAudit:
    base: dict[str, Any] = {
        "call_id": "c" * 32, "agent": "buyer", "run_id": "run-1",
        "service": "stock-levels", "target": "A_Stock", "operation": "update",
        "key": {"Material": "M-1"}, "fields": ("Quantity",), "outcome": "intent",
        "phase": None, "status": None, "sent_as": "user@example.com",
        "run_principal": "user@example.com", "token_digest": "d" * 64,
    }
    base.update(patch)
    return WriteAudit(**base)


async def _audit_rows(bound) -> list[ODataAuditLog]:
    async with bound.sessions() as s:
        rows = await s.execute(select(ODataAuditLog).order_by(ODataAuditLog.id))
        return list(rows.scalars())


async def test_a_call_id_is_stored_once(hana):
    recorder = StoredWriteRecorder(session_factory=hana.sessions)
    await recorder.intent(record())
    with pytest.raises(IntegrityError):
        await recorder.intent(record(agent="another"))
    rows = await _audit_rows(hana)
    assert [(r.call_id, r.agent, r.outcome) for r in rows] == [("c" * 32, "buyer", "intent")]


async def test_a_row_is_finalised_once_and_a_second_result_changes_nothing(hana):
    recorder = StoredWriteRecorder(session_factory=hana.sessions)
    token = await recorder.intent(record())
    await recorder.result(token, record(outcome="ok", phase="write", status=200))
    (first,) = await _audit_rows(hana)
    assert (first.outcome, first.phase, first.http_status) == ("ok", "write", 200)

    await recorder.result(token, record(outcome="sap_error", phase="write", status=500))
    (second,) = await _audit_rows(hana)
    assert (second.outcome, second.phase, second.http_status) == ("ok", "write", 200)
    assert second.finished_at == first.finished_at
    assert await recorder.abandon(record()) is False


async def test_audit_timestamps_round_trip_as_utc_instants(hana):
    before = datetime.now(timezone.utc)
    recorder = StoredWriteRecorder(session_factory=hana.sessions)
    token = await recorder.intent(record())
    (open_row,) = await _audit_rows(hana)
    assert open_row.finished_at is None
    await recorder.result(token, record(outcome="ok", phase="write", status=204))
    after = datetime.now(timezone.utc)

    (row,) = await _audit_rows(hana)
    assert before <= instant(row.created_at) <= instant(row.finished_at) <= after
    served = row.to_dict()
    assert instant(served["created_at"]) == instant(row.created_at)
    assert instant(served["finished_at"]) == instant(row.finished_at)
