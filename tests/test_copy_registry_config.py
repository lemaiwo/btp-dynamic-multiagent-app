"""``scripts/copy_registry_config.py``: the configuration, with its secrets,
from one database to another.

Run here between two SQLite files through the script's own command line
(``--from-env`` / ``--to-env``); the same copy into a real HDI container is
in ``tests/test_hana_integration.py`` and from a real Postgres at the end of
this file (opt-in, see ``tests/pg.py``).

Run:  python -m pytest tests/test_copy_registry_config.py -q
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()

from sqlalchemy import DateTime, event, func, select, update  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import agents.ide.models  # noqa: E402,F401
from agents.db import (  # noqa: E402
    AgentConfig,
    Base,
    JobRun,
    McpOAuthClient,
    McpOAuthState,
    McpOAuthToken,
    ODataService,
    OrchestratorConfig,
    SkillConfig,
    Workflow,
    WorkflowBranch,
    WorkflowStep,
)
from agents.ide.models import IdeAuditLog, IdeConventions  # noqa: E402
from tests import pg  # noqa: E402
from tests.registry_rows import (  # noqa: E402
    MARKER_AGENT,
    MARKER_CLIENT,
    MARKER_TOKEN,
    MARKERS,
    STAMP,
    seed_source,
    seed_target,
)

_spec = importlib.util.spec_from_file_location(
    "copy_registry_config", ROOT / "scripts" / "copy_registry_config.py")
copy_script = importlib.util.module_from_spec(_spec)
sys.modules["copy_registry_config"] = copy_script
_spec.loader.exec_module(copy_script)


class Db:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.url = f"sqlite+aiosqlite:///{path}"
        self.engine = create_async_engine(self.url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False,
                                           class_=AsyncSession)

    async def create(self) -> Db:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        return self

    async def all(self, model) -> list:
        async with self.sessions() as s:
            return list((await s.execute(select(model))).scalars())

    async def count(self, model) -> int:
        async with self.sessions() as s:
            return (await s.execute(select(func.count()).select_from(model))).scalar_one()


@pytest.fixture
async def dbs(tmp_path, monkeypatch):
    source = await Db(tmp_path / "source.db").create()
    target = await Db(tmp_path / "target.db").create()
    await seed_source(source.sessions)
    await seed_target(target.sessions)
    monkeypatch.setenv("OLD_DB", source.url)
    monkeypatch.setenv("NEW_DB", target.url)
    for name in ("VCAP_SERVICES", "VCAP_APPLICATION", "DB_KIND"):
        monkeypatch.delenv(name, raising=False)
    yield source, target
    await source.engine.dispose()
    await target.engine.dispose()


ARGS = ["--from-env", "OLD_DB", "--to-env", "NEW_DB"]


async def main(argv: list[str]) -> int:
    """The script's entry point, as a process would run it: it starts an
    event loop of its own, so from an async test it runs in a thread."""
    return await asyncio.to_thread(copy_script.main, argv)


async def run(capsys, *extra: str) -> tuple[int, str, str]:
    code = await main([*ARGS, *extra])
    out = capsys.readouterr()
    return code, out.out, out.err


def no_secret(*texts: str) -> None:
    for text in texts:
        for marker in MARKERS:
            assert marker not in text
        assert "MARKER" not in text and "client_secret" not in text


# --- dry run ---------------------------------------------------------------------------


async def test_the_default_is_a_dry_run_that_names_and_counts(dbs, capsys, caplog):
    source, target = dbs
    before = [(a.name, a.instructions) for a in await target.all(AgentConfig)]
    with caplog.at_level(logging.DEBUG):
        code, out, err = await run(capsys)

    assert code == 0 and err == ""
    assert out.splitlines() == [
        "sqlite -> sqlite: dry run",
        "  skill_configs: insert 1 (triage-skill), replace 0, unchanged 0, only in target 0",
        "  agent_configs: insert 1 (reader), replace 1 (seeded), unchanged 0, "
        "only in target 3 (local-0, local-1, local-2)",
        "  orchestrator_config: insert 0, replace 1, unchanged 0, only in target 0",
        "  workflows: insert 1 (mail-triage), replace 0, unchanged 0, "
        "only in target 3 (local-wf-0, local-wf-1, local-wf-2)",
        "  odata_services: insert 1 (stock), replace 0, unchanged 0, only in target 0",
        "  ide_conventions: insert 1 (DEV), replace 0, unchanged 0, only in target 0, "
        "audited 1 (DEV)",
        "  mcp_oauth_clients: insert 1, replace 0, unchanged 0, only in target 0",
        "  mcp_oauth_tokens: insert 1, replace 0, unchanged 0, only in target 0",
        "  workflow_branches: insert 1 (mail-triage), replace 0, unchanged 0",
        "  workflow_steps: insert 1 (mail-triage), replace 0, unchanged 0",
        "  not copied: " + ", ".join(copy_script.NOT_COPIED),
        "nothing was written; run again with --apply",
    ]
    no_secret(out, err, caplog.text)
    assert "alice@example.com" not in out and "mcp.example.invalid" not in out
    assert [(a.name, a.instructions) for a in await target.all(AgentConfig)] == before
    assert await target.count(SkillConfig) == 0 and await target.count(McpOAuthToken) == 0


def test_what_is_copied_and_what_is_not():
    assert set(copy_script.COPIED) == {
        "agent_configs", "skill_configs", "orchestrator_config", "workflows",
        "workflow_branches", "workflow_steps", "odata_services", "ide_conventions",
        "mcp_oauth_clients", "mcp_oauth_tokens",
    }
    assert set(copy_script.NOT_COPIED) == set(Base.metadata.tables) - set(copy_script.COPIED)
    for name in ("job_runs", "workflow_runs", "workflow_item_runs", "workflow_step_runs",
                 "odata_audit_log", "ide_audit_log", "ide_sessions", "ide_comments",
                 "mcp_oauth_states"):
        assert name in copy_script.NOT_COPIED


def test_every_copied_timestamp_column_is_zone_aware():
    """The copy hands every database an aware UTC instant; a plain
    TIMESTAMP column on Postgres would refuse one."""
    for name in copy_script.COPIED:
        for column in Base.metadata.tables[name].columns:
            if isinstance(column.type, DateTime):
                assert column.type.timezone, f"{name}.{column.name}"


# --- apply -----------------------------------------------------------------------------


async def test_apply_copies_the_configuration_with_its_secrets(dbs, capsys, caplog):
    source, target = dbs
    with caplog.at_level(logging.DEBUG):
        code, out, err = await run(capsys, "--apply")
    assert code == 0 and err == "" and out.startswith("sqlite -> sqlite: APPLIED")
    no_secret(out, err, caplog.text)
    # What the operator has to do next, in words and nothing else.
    tail = out.splitlines()[-4:]
    assert tail[0] == "next steps:"
    assert "source should be stopped or no longer used" in tail[1]
    assert "restart the app on the target (or Reload" in tail[2]
    assert "user tokens" in tail[3] and "stale" in tail[3] and "overwrites" in tail[3]

    agents_ = {a.name: a for a in await target.all(AgentConfig)}
    reader = agents_["reader"]
    assert json.loads(reader.oauth_json) == {"client_id": "cid", "client_secret": MARKER_AGENT}
    assert json.loads(reader.extra_servers_json) == [{"url": "builtin:odata"}]
    assert (reader.auth_mode, reader.api_slug, len(reader.instructions)) == (
        "oauth2", "reader", 6000)
    # Replaced by the source row; the target's own rows are left alone.
    assert (agents_["seeded"].description, agents_["seeded"].instructions) == (
        "from the source", "source text")
    assert {n for n in agents_ if n.startswith("local-")} == {"local-0", "local-1", "local-2"}
    assert (await target.all(OrchestratorConfig))[0].instructions == "source orchestrator"
    assert [s.name for s in await target.all(SkillConfig)] == ["triage-skill"]
    assert [s.name for s in await target.all(ODataService)] == ["stock"]
    (convention,) = await target.all(IdeConventions)
    assert (convention.target, convention.label, convention.non_production) == (
        "DEV", "Development", True)
    (client,) = await target.all(McpOAuthClient)
    assert client.client_secret == MARKER_CLIENT
    (token,) = await target.all(McpOAuthToken)
    assert (token.user_id, token.access_token) == ("alice@example.com", MARKER_TOKEN)
    # History and flow state stay where they are.
    assert await target.count(JobRun) == 0 and await target.count(McpOAuthState) == 0


async def test_ids_are_the_targets_and_children_follow_their_workflow(dbs, capsys):
    source, target = dbs
    assert (await run(capsys, "--apply"))[0] == 0
    (source_workflow,) = await source.all(Workflow)
    workflows = {w.name: w for w in await target.all(Workflow)}
    copied = workflows["mail-triage"]
    assert copied.id != source_workflow.id  # three local workflows came first
    branches = [b for b in await target.all(WorkflowBranch) if b.workflow_id == copied.id]
    assert [(b.key, b.description) for b in branches] == [("abap", "ABAP")]
    steps = sorted((s.branch_key or "", s.position, s.agent_name)
                   for s in await target.all(WorkflowStep) if s.workflow_id == copied.id)
    assert steps == [("", 1, "reader"), ("", 2, "seeded"), ("abap", 1, "reader")]
    # The step of the target's own workflow is still its own.
    others = [s for s in await target.all(WorkflowStep) if s.workflow_id != copied.id]
    assert [(s.agent_name, s.instructions) for s in others] == [("local-0", "local")]
    source_ids = {a.name: a.id for a in await source.all(AgentConfig)}
    target_ids = {a.name: a.id for a in await target.all(AgentConfig)}
    assert target_ids["reader"] != source_ids["reader"]


async def test_a_second_run_changes_nothing(dbs, capsys):
    source, target = dbs
    assert (await run(capsys, "--apply"))[0] == 0
    stamps = {a.name: a.updated_at for a in await target.all(AgentConfig)}
    for extra in ((), ("--apply",)):
        code, out, _ = await run(capsys, *extra)
        assert code == 0
        lines = [line for line in out.splitlines() if ": insert " in line]
        assert len(lines) == 10
        assert all("insert 0, replace 0" in line for line in lines), out
    assert {a.name: a.updated_at for a in await target.all(AgentConfig)} == stamps


async def test_a_changed_workflow_replaces_its_steps_and_only_its(dbs, capsys):
    source, target = dbs
    assert (await run(capsys, "--apply"))[0] == 0
    async with source.sessions() as s:
        await s.execute(update(WorkflowStep).where(WorkflowStep.position == 2)
                        .values(instructions="changed"))
        await s.execute(update(AgentConfig).where(AgentConfig.name == "reader")
                        .values(description="changed", updated_at=STAMP))
        await s.commit()
    code, out, _ = await run(capsys, "--apply")
    assert code == 0
    assert "  agent_configs: insert 0, replace 1 (reader), unchanged 1, only in" in out
    assert "  workflow_steps: insert 0, replace 1 (mail-triage), unchanged 0" in out
    assert "  workflow_branches: insert 0, replace 0, unchanged 1" in out
    steps = await target.all(WorkflowStep)
    assert sorted(s.instructions for s in steps) == ["changed", "do", "do", "local"]


async def test_timestamps_arrive_as_the_same_instant(dbs, capsys):
    source, target = dbs
    assert (await run(capsys, "--apply"))[0] == 0
    reader = next(a for a in await target.all(AgentConfig) if a.name == "reader")
    (token,) = await target.all(McpOAuthToken)
    for value in (reader.created_at, reader.updated_at, token.expires_at):
        assert copy_script._instant(value) == STAMP  # to the microsecond


def test_an_instant_is_utc_whatever_the_database_handed_back():
    naive = datetime(2026, 3, 4, 5, 6, 7, 123457)
    aware = naive.replace(tzinfo=timezone.utc)
    elsewhere = aware.astimezone(timezone(timedelta(hours=2)))
    assert copy_script._instant(naive) == aware  # HANA, SQLite: naive IS UTC
    assert copy_script._instant(aware) == aware  # Postgres
    assert copy_script._instant(elsewhere) == aware
    assert copy_script._instant(elsewhere).utcoffset() == timedelta(0)
    assert copy_script._instant(None) is None and copy_script._instant("x") == "x"


# --- the source is only read; the target is all or nothing ------------------------------


class Statements(list):
    """Every statement sent to either file while the block runs."""

    def __enter__(self) -> Statements:
        def listen(conn, cursor, statement, parameters, context, executemany):
            self.append((Path(conn.engine.url.database).name, " ".join(statement.split())))

        self._listen = listen
        event.listen(Engine, "before_cursor_execute", listen)
        return self

    def __exit__(self, *exc) -> None:
        event.remove(Engine, "before_cursor_execute", self._listen)

    def to(self, name: str) -> list[str]:
        return [statement for file, statement in self if file == name]


async def test_no_statement_that_writes_reaches_the_source(dbs, capsys):
    with Statements() as seen:
        assert (await run(capsys))[0] == 0
        assert (await run(capsys, "--apply"))[0] == 0
    source = seen.to("source.db")
    assert len(source) >= 20
    assert all(s.startswith("SELECT ") for s in source), [
        s for s in source if not s.startswith("SELECT ")][:3]
    written = [s for s in seen.to("target.db")
               if s.startswith(("INSERT", "UPDATE", "DELETE"))]
    assert len(written) >= 12


async def test_a_dry_run_sends_the_target_no_write_either(dbs, capsys):
    with Statements() as seen:
        assert (await run(capsys))[0] == 0
    assert not [s for s in seen.to("target.db")
                if s.startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"))]


async def test_a_failure_writes_nothing_and_shows_no_value(dbs, capsys, caplog, monkeypatch):
    """The copy breaks in the middle (here: when it comes to the workflow
    steps, with an error whose text quotes a row, as a driver's does).
    Nothing of it stays, and that text is not repeated."""
    source, target = dbs

    async def breaks(*args, **kwargs):
        raise RuntimeError(f"INSERT failed, parameters: ('{MARKER_AGENT}', '{MARKER_TOKEN}')")

    monkeypatch.setattr(copy_script, "_copy_children", breaks)
    with caplog.at_level(logging.DEBUG):
        code, out, err = await run(capsys, "--apply")
    assert code == 1 and out == ""
    assert err == "copy failed (RuntimeError); nothing was written to the target\n"
    no_secret(out, err, caplog.text)
    # Skills, agents, tokens were inserted before it broke: gone again.
    assert await target.count(SkillConfig) == 0
    assert await target.count(McpOAuthToken) == 0
    seeded = next(a for a in await target.all(AgentConfig) if a.name == "seeded")
    assert seeded.instructions == "target text"


async def test_a_source_that_fails_to_close_after_the_commit_is_not_a_failed_copy(
    dbs, capsys, monkeypatch
):
    """The target is committed; then letting go of the source goes wrong.
    "Nothing was written" would be false, and an operator would run it
    again or, worse, believe the target is empty."""
    source, target = dbs
    real = copy_script._let_go

    async def fails_to_close(connection):
        await real(connection)
        return "InterfaceError"

    monkeypatch.setattr(copy_script, "_let_go", fails_to_close)
    code, out, err = await run(capsys, "--apply")
    assert code == 0 and out.startswith("sqlite -> sqlite: APPLIED")
    assert err == ("the configuration WAS written to the target; closing the source "
                   "connection failed afterwards (InterfaceError)\n")
    assert "nothing was written" not in out + err
    assert await target.count(SkillConfig) == 1
    # A dry run that meets the same trouble has written nothing and says so.
    code, out, err = await run(capsys)
    assert code == 0 and err == "" and out.endswith("run again with --apply\n")


async def test_let_go_reports_a_class_and_never_raises():
    class Broken:
        async def rollback(self):
            raise ConnectionError(f"lost {MARKER_TOKEN}")

        async def close(self):
            raise AssertionError("not reached")

    assert await copy_script._let_go(Broken()) == "ConnectionError"


# --- rows only the target has; a unique value held by one of them ------------------------


async def test_a_slug_held_by_a_target_only_row_is_refused_by_name(dbs, capsys):
    """Otherwise a bare IntegrityError at the first write. Checked before
    anything is written, for agents and workflows alike."""
    source, target = dbs
    async with target.sessions() as s:
        await s.execute(update(AgentConfig).where(AgentConfig.name == "local-0")
                        .values(api_slug="reader"))
        await s.commit()
    for extra in ((), ("--apply",)):
        code, out, err = await run(capsys, *extra)
        assert code == 2 and out == ""
        assert err == (
            "refused: agent_configs: the api_slug of source 'reader' is held by "
            "target-only 'local-0'. Change or remove it on one side; nothing was written.\n")
    assert await target.count(SkillConfig) == 0

    async with target.sessions() as s:
        await s.execute(update(AgentConfig).where(AgentConfig.name == "local-0")
                        .values(api_slug=None))
        await s.execute(update(Workflow).where(Workflow.name == "local-wf-1")
                        .values(api_slug="mail-triage"))
        await s.commit()
    code, out, err = await run(capsys, "--apply")
    assert code == 2 and "workflows: the api_slug of source 'mail-triage' is held by " \
        "target-only 'local-wf-1'" in err


async def test_two_replaced_rows_may_swap_a_unique_value(dbs, capsys):
    source, target = dbs
    assert (await run(capsys, "--apply"))[0] == 0
    async with source.sessions() as s:
        await s.execute(update(AgentConfig).where(AgentConfig.name == "reader")
                        .values(api_slug=None))
        await s.execute(update(AgentConfig).where(AgentConfig.name == "seeded")
                        .values(api_slug="reader"))
        await s.execute(update(AgentConfig).where(AgentConfig.name == "reader")
                        .values(api_slug="seeded"))
        await s.commit()
    code, out, err = await run(capsys, "--apply")
    assert code == 0, err
    slugs = {a.name: a.api_slug for a in await target.all(AgentConfig)}
    assert (slugs["reader"], slugs["seeded"]) == ("seeded", "reader")


async def test_rows_only_in_the_target_are_listed_and_left_alone(dbs, capsys):
    source, target = dbs
    async with target.sessions() as s:
        s.add(SkillConfig(name="seeded-default", description="d", content="c"))
        s.add(IdeConventions(target="OLD", label="gone from the source"))
        await s.commit()
    for extra in ((), ("--apply",), ("--apply",)):
        code, out, _ = await run(capsys, *extra)
        assert code == 0
        assert "only in target 1 (seeded-default)" in out
        assert "only in target 1 (OLD)" in out
        assert "only in target 3 (local-0, local-1, local-2)" in out
    assert {s.name for s in await target.all(SkillConfig)} == {"seeded-default", "triage-skill"}


# --- the audit of a flagged target --------------------------------------------------------


async def _audit(db: Db) -> list[tuple]:
    rows = sorted(await db.all(IdeAuditLog), key=lambda r: (r.target, r.action))
    return [(r.principal, r.session_id, r.target, r.action, json.loads(r.params_json),
             r.outcome) for r in rows]


async def test_setting_the_flag_is_audited_as_the_app_would(dbs, capsys):
    source, target = dbs
    async with source.sessions() as s:
        s.add(IdeConventions(target="QAS", label="Quality", non_production=True,
                             destination="ARC1_QAS"))
        s.add(IdeConventions(target="PRD", label="Production", destination="ARC1_PRD"))
        await s.commit()
    code, out, _ = await run(capsys)
    assert code == 0 and "audited 2 (DEV, QAS)" in out
    assert await target.count(IdeAuditLog) == 0  # a dry run writes none

    code, out, _ = await run(capsys, "--apply")
    assert code == 0 and "audited 2 (DEV, QAS)" in out
    actor = "script:copy_registry_config"
    assert await _audit(target) == [
        (actor, "-", "DEV", "conventions_flag",
         {"non_production": {"old": None, "new": True}}, "ok"),
        (actor, "-", "QAS", "conventions_destination",
         {"destination": {"old": None, "new": "ARC1_QAS"}}, "ok"),
        (actor, "-", "QAS", "conventions_flag",
         {"non_production": {"old": None, "new": True}}, "ok"),
    ]
    # The source's own audit log is history: not copied, not touched.
    assert await source.count(IdeAuditLog) == 0
    # Nothing changed: no further row.
    code, out, _ = await run(capsys, "--apply")
    assert code == 0 and "audited" not in out
    assert len(await _audit(target)) == 3


async def test_a_changed_flag_or_destination_is_audited_with_old_and_new(dbs, capsys):
    source, target = dbs
    async with source.sessions() as s:
        s.add(IdeConventions(target="QAS", non_production=True, destination="ARC1_QAS"))
        s.add(IdeConventions(target="PRD", destination="ARC1_PRD"))
        await s.commit()
    assert (await run(capsys, "--apply"))[0] == 0
    async with source.sessions() as s:
        await s.execute(update(IdeConventions).where(IdeConventions.target == "QAS")
                        .values(destination="ARC1_NEW"))        # flagged: destination
        await s.execute(update(IdeConventions).where(IdeConventions.target == "DEV")
                        .values(non_production=False))           # the flag removed
        await s.execute(update(IdeConventions).where(IdeConventions.target == "PRD")
                        .values(destination="ARC1_OTHER", label="x"))  # never flagged
        await s.commit()
    async with target.sessions() as s:
        await s.execute(IdeAuditLog.__table__.delete())
        await s.commit()
    code, out, _ = await run(capsys, "--apply")
    assert code == 0 and "replace 3 (DEV, PRD, QAS)" in out and "audited 2 (DEV, QAS)" in out
    actor = "script:copy_registry_config"
    assert await _audit(target) == [
        (actor, "-", "DEV", "conventions_flag",
         {"non_production": {"old": True, "new": False}}, "ok"),
        (actor, "-", "QAS", "conventions_destination",
         {"destination": {"old": "ARC1_QAS", "new": "ARC1_NEW"}}, "ok"),
    ]


async def test_the_audit_rows_go_with_a_failed_copy(dbs, capsys, monkeypatch):
    source, target = dbs

    async def breaks(*args, **kwargs):
        raise RuntimeError("later")

    monkeypatch.setattr(copy_script, "_copy_children", breaks)
    assert (await run(capsys, "--apply"))[0] == 1
    assert await target.count(IdeAuditLog) == 0 and await target.count(IdeConventions) == 0


def test_the_engines_never_quote_parameters(monkeypatch):
    target = copy_script.agents_db._target_from_url("sqlite+aiosqlite:///:memory:")
    assert copy_script._engine(target).sync_engine.hide_parameters is True


# --- refusals ---------------------------------------------------------------------------


async def test_the_same_database_twice_is_refused(dbs, capsys):
    code = await main(["--from-env", "OLD_DB", "--to-env", "OLD_DB", "--apply"])
    out = capsys.readouterr()
    assert code == 2 and out.out == ""
    assert out.err == "refused: Source and target are the same database\n"


async def test_a_target_without_the_schema_is_refused(dbs, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("NEW_DB", f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
    code, out, err = await run(capsys, "--apply")
    assert code == 2 and out == ""
    assert err.startswith("refused: The target has no usable table skill_configs. Start the app")


@pytest.mark.parametrize("argv, says", [
    (["--to-env", "NEW_DB"], "Name the from database with --from OR --from-env"),
    (["--from-env", "OLD_DB", "--from", "postgres", "--to-env", "NEW_DB"], "--from OR"),
    (["--from-env", "OLD_DB"], "Name the to database"),
    (["--from-env", "NOT_SET_ANYWHERE", "--to-env", "NEW_DB"], "is not set"),
    (["--from", "postgres", "--to-env", "NEW_DB"], "No postgres database service is bound"),
    (["--from-env", "OLD_DB", "--to", "hana"], "No hana database service is bound"),
])
async def test_what_cannot_run_is_refused_with_a_reason(dbs, capsys, argv, says):
    code = await main(argv)
    out = capsys.readouterr()
    assert code == 2 and out.out == "" and says in out.err
    assert "sqlite" not in out.err  # no URL


@pytest.mark.parametrize("flag", ["--from", "--to"])
def test_a_refused_argument_value_is_not_repeated(capsys, flag):
    """What gets pasted there by mistake is a URL with its password; argparse
    repeats a refused ``choices`` value, so the kinds are checked by hand."""
    pasted = "postgresql://someone:Secr3t-pw@db.example.internal:5432/prod"
    with pytest.raises(SystemExit) as stopped:
        copy_script.main([flag, pasted, "--to-env", "NEW_DB"])
    out = capsys.readouterr()
    assert stopped.value.code == 2
    shown = out.out + out.err
    assert f"argument {flag}: must be 'postgres' or 'hana'" in shown
    for part in (pasted, "Secr3t-pw", "someone", "db.example.internal"):
        assert part not in shown


BOTH = {
    "postgresql-db": [{"credentials": {
        "hostname": "pg.example.internal", "port": 5432, "username": "u",
        "password": "pg-pass", "dbname": "d"}}],
    "hana": [{"plan": "hdi-shared", "credentials": {
        "host": "hana.example.internal", "port": "443", "schema": "C1", "user": "RT",
        "password": "rt-pass", "hdi_user": "DT", "hdi_password": "dt-pass"}}],
}


def test_both_bindings_are_resolved_by_the_apps_own_rules(monkeypatch):
    monkeypatch.setenv("VCAP_SERVICES", json.dumps(BOTH))
    for kind_var in ("", "postgres", "hana"):  # DB_KIND plays no part
        monkeypatch.setenv("DB_KIND", kind_var)
        source = copy_script._target("postgres", None, "from")
        target = copy_script._target("hana", None, "to")
        assert (source.kind, target.kind) == ("postgres", "hana")
        url, connect_args = copy_script.agents_db._engine_settings(target)
        assert "encrypt=true" in url and "sslValidateCertificate=true" in url
        assert copy_script._identity(source) != copy_script._identity(target)
    assert copy_script._identity(target) == copy_script._identity(
        copy_script._target("hana", None, "to"))


def test_the_script_can_be_imported_with_both_services_bound():
    """As a task of the app both are bound and ``DB_KIND`` may be unset;
    ``agents.db`` alone would refuse to start then."""
    import os
    import subprocess

    environment = {"PATH": os.environ.get("PATH", ""), "VCAP_SERVICES": json.dumps(BOTH),
                   "VCAP_APPLICATION": "{\"application_name\": \"app\"}"}
    result = subprocess.run(
        [sys.executable, "scripts/copy_registry_config.py", "--from", "hana", "--to", "hana"],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 2, result.stderr[-400:]
    assert result.stderr == "refused: Source and target are the same database\n"
    for secret in ("pg-pass", "rt-pass", "dt-pass"):
        assert secret not in result.stdout + result.stderr


# --- from a real Postgres (opt-in) ------------------------------------------------------


@pytest.mark.skipif(pg.postgres_url() is None, reason=pg.SKIP_REASON)
async def test_postgres_and_back_keep_every_instant_and_secret(tmp_path):
    """Postgres hands back aware datetimes and takes them; SQLite (like SAP
    HANA) stores naive UTC. Copied there and back again, nothing moved."""
    there = await Db(tmp_path / "there.db").create()
    back_again = None
    try:
        async with pg.private_sessions() as source, pg.private_sessions() as back_again:
            await seed_source(source)
            source_engine, back_engine = source.kw["bind"], back_again.kw["bind"]
            await copy_script.copy_config(source_engine, there.engine, apply=True)
            await copy_script.copy_config(there.engine, back_engine, apply=True)
            for plans in (
                await copy_script.copy_config(source_engine, there.engine, apply=True),
                await copy_script.copy_config(there.engine, back_engine, apply=True),
                await copy_script.copy_config(source_engine, back_engine, apply=False),
            ):
                assert all(not p.insert and not p.replace for p in plans), [
                    p.line() for p in plans]
            async with back_again() as s:
                reader = (await s.execute(
                    select(AgentConfig).where(AgentConfig.name == "reader"))).scalar_one()
                token = (await s.execute(select(McpOAuthToken))).scalar_one()
            assert reader.created_at == STAMP and reader.created_at.tzinfo is not None
            assert token.expires_at == STAMP and token.access_token == MARKER_TOKEN
            assert json.loads(reader.oauth_json)["client_secret"] == MARKER_AGENT
    finally:
        await there.engine.dispose()
