"""The ``backup`` checks of ``builtin:hana`` (``agents/hana/checks/backup.py``).

Per check: registered with its category and arguments, its SQL reads only the
monitoring views, and run on the fake driver with two scripted rows it answers
the expected shape and binds. The helpers are the monitoring tests'.
"""

from __future__ import annotations

from agents.hana import checks as registry
from agents.hana.sqltokens import tokenize
from tests.test_hanadb_checks_monitoring import assert_registered, assert_single, run_on_fake

PARTS = ("last_successful", "recent", "progress", "log_settings")


def test_the_two_backup_checks() -> None:
    names = [c.name for c in registry.all_checks() if c.category == "backup"]
    assert sorted(names) == ["backupList", "backupStatus"]


def _words(sql: str) -> set[str]:
    return {t.value for t in tokenize(sql) if t.kind == "word"}


def test_no_path_or_free_text_column_is_selected() -> None:
    for name in ("backupStatus", "backupList"):
        for statement in registry.get_check(name).statements:
            words = _words(statement.sql)
            assert not words & {"DESTINATION_PATH", "EXTERNAL_BACKUP_ID", "BACKUP_ID_PATH"}
            assert '"COMMENT"' not in statement.sql


def test_log_settings_read_named_keys_only() -> None:
    sql = registry.get_check("backupStatus").statements[3].sql
    assert "FILE_NAME = 'global.ini'" in sql
    assert "parameter_file" not in sql and "basepath" not in sql
    assert '"KEY" IN (' in sql


def test_recent_backups_carry_no_message() -> None:
    recent = registry.get_check("backupStatus").statements[1]
    assert "MESSAGE" not in _words(recent.sql)


def test_last_successful_is_bounded_to_90_days() -> None:
    check = registry.get_check("backupStatus")
    assert "ADD_DAYS(CURRENT_TIMESTAMP, -90)" in check.statements[0].sql
    assert "90 days" in check.purpose


def test_backup_list_windows_the_catalogue_before_the_join() -> None:
    sql = registry.get_check("backupList").statements[0].sql
    join = sql.index("JOIN SYS.M_BACKUP_CATALOG_FILES")
    window = sql.index("ADD_SECONDS(CURRENT_TIMESTAMP, -3600 * ?)")
    assert window < join
    assert sql.index("FROM (SELECT") < window


async def test_backup_status(monkeypatch) -> None:
    assert_registered("backupStatus", "backup", {"top", "hours"}, parts=PARTS)
    columns = ["ENTRY_TYPE_NAME", "STATE_NAME"]
    rows = [("complete data backup", "successful"), ("log backup", "failed")]
    answer, executes = await run_on_fake(monkeypatch, "backupStatus", columns, rows,
                                         hours=168, top=10)
    assert "error" not in answer, answer
    assert [p["label"] for p in answer["parts"]] == list(PARTS)
    for part in answer["parts"]:
        assert part["columns"] == columns and part["row_count"] == 2
    assert [e[2] if len(e) > 2 else () for e in executes] == [(), (11, 168), (11,), ()]


async def test_backup_list(monkeypatch) -> None:
    check = assert_registered("backupList", "backup", {"top", "hours"})
    assert "M_BACKUP_CATALOG_FILES" in check.statements[0].sql
    columns = ["BACKUP_ID", "ENTRY_TYPE_NAME", "STATE_NAME", "SYS_START_TIME",
               "SYS_END_TIME", "BACKUP_SIZE", "FILE_COUNT", "DESTINATION_TYPE_NAME"]
    rows = [(2, "complete data backup", "successful", "t", "t", 100, 3, "backint"),
            (1, "differential data backup", "failed", "t", "t", None, 0, None)]
    answer, executes = await run_on_fake(monkeypatch, "backupList", columns, rows)
    assert_single(answer, columns)
    assert executes[0][2] == (51, 24)
