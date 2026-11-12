"""The ``builtin:hana`` checks of ``agents/hana/checks/security.py``:
the security checks of Phase 3.

Pinned here: the module's check names (registration order), each check's
category, arguments and parts, that every statement passes the SELECT guard
with ``fixed=True`` and selects named columns, that each check answers the
expected shape and binds on the fake driver, and what never leaves the
database: no password column beyond a named list of flags and policy columns,
no certificate body, no key material, no audit trail, no free text, no path,
and no ini read without a named-key predicate. The shared invariants are in
``tests/test_hanadb_checks.py``; the helpers are the monitoring tests'.
"""

from __future__ import annotations

import re

import pytest

from agents.hana import checks as registry
from agents.hana.checks import security
from agents.hana.sqltokens import tokenize
from tests.test_hanadb_checks_monitoring import assert_registered, assert_single, run_on_fake

EXPECTED: tuple[str, ...] = (
    "users", "roles", "inspectUser", "certificates", "pwdPolicy", "encryptionStatus",
    "privilegeAnalysis", "grantChains", "securityScan", "auditLog",
)

# The only words holding PASSWORD a statement may name: the flag and time
# columns of SYS.USERS and the view M_PASSWORD_POLICY (PROPERTY / VALUE).
PASSWORD_ALLOWED = frozenset({
    "PASSWORD_CHANGE_NEEDED", "IS_PASSWORD_LIFETIME_CHECK_ENABLED",
    "LAST_PASSWORD_CHANGE_TIME", "IS_PASSWORD_ENABLED", "M_PASSWORD_POLICY",
})

# CERTIFICATES (SAP HANA 2.0 SPS08 reference) has CERTIFICATE_NAME,
# CERTIFICATE_ID, SUBJECT_COMMON_NAME, ISSUER_COMMON_NAME, VALID_FROM,
# VALID_UNTIL, COMMENT, SUBJECT_DISTINGUISHED_NAME, ISSUER_DISTINGUISHED_NAME,
# BASIC_CONSTRAINTS, SUBJECT_ALT_NAME, PUBLIC_KEY_ALGORITHM, PUBLIC_KEY_LENGTH,
# KEY_USAGE, SIGNATURE_ALGORITHM, VERSION, SERIAL_NUMBER, FINGERPRINT and
# CERTIFICATE (the body, an NCLOB). Never selected of them:
CERTIFICATE_NEVER = frozenset({
    "CERTIFICATE", "COMMENT", "SUBJECT_ALT_NAME", "FINGERPRINT",
})

# Key material and the views that hold it.
KEY_MATERIAL = frozenset({
    "USED_ROOT_KEY_HASH", "KEY_PAGE_ENTRY_COUNT", "ENCRYPTION_ROOT_KEYS",
    "M_PERSISTENCE_ENCRYPTION_KEYS", "PSE_CERTIFICATES", "PSES", "CREDENTIALS",
    "PRIVATE_KEY", "ROOT_KEY", "KEY_HASH",
})

# The audit trail, personal data and paths.
NEVER = frozenset({
    "AUDIT_LOG", "M_AUDIT_LOG", "ALL_AUDIT_LOG", "AUDIT_LOG_ENTRIES", "XSA_AUDIT_LOG",
    "USER_PARAMETERS", "EXTERNAL_IDENTITY", "ADMIN_GIVEN_PASSWORD", "PATH", "M_VOLUMES",
    "STATEMENT_STRING",
})


# Per view the module reads, the columns a statement may name. A column that
# is not listed fails the allow-list test until somebody adds it here on
# purpose (and says why it may leave the database).
_USER_ALLOWED = frozenset({
    "USER_NAME", "USERGROUP_NAME", "CREATOR", "CREATE_TIME", "VALID_FROM", "VALID_UNTIL",
    "USER_DEACTIVATED", "DEACTIVATION_TIME", "LAST_SUCCESSFUL_CONNECT",
    "LAST_INVALID_CONNECT_ATTEMPT", "INVALID_CONNECT_ATTEMPTS", "PASSWORD_CHANGE_NEEDED",
    "IS_PASSWORD_LIFETIME_CHECK_ENABLED", "LAST_PASSWORD_CHANGE_TIME", "IS_PASSWORD_ENABLED",
})
_INI_ALLOWED = frozenset({"LAYER_NAME", "SECTION", "FILE_NAME"})  # "KEY"/"VALUE" are quoted
VIEW_COLUMNS: dict[str, frozenset[str]] = {
    "USERS": _USER_ALLOWED,
    "ROLES": frozenset({"ROLE_SCHEMA_NAME", "ROLE_NAME", "CREATOR", "CREATE_TIME"}),
    "GRANTED_ROLES": frozenset({"GRANTEE", "GRANTEE_TYPE", "ROLE_SCHEMA_NAME", "ROLE_NAME",
                                "GRANTOR", "IS_GRANTABLE"}),
    "GRANTED_PRIVILEGES": frozenset({"GRANTEE", "GRANTEE_TYPE", "OBJECT_TYPE", "SCHEMA_NAME",
                                     "OBJECT_NAME", "PRIVILEGE", "GRANTOR", "IS_GRANTABLE"}),
    "CERTIFICATES": frozenset({"CERTIFICATE_NAME", "CERTIFICATE_ID",
                               "SUBJECT_DISTINGUISHED_NAME", "ISSUER_DISTINGUISHED_NAME",
                               "SERIAL_NUMBER", "VALID_FROM", "VALID_UNTIL",
                               "PUBLIC_KEY_ALGORITHM", "PUBLIC_KEY_LENGTH",
                               "SIGNATURE_ALGORITHM"}),
    "M_PASSWORD_POLICY": frozenset({"PROPERTY"}),
    "M_ENCRYPTION_OVERVIEW": frozenset({"SCOPE", "IS_ENCRYPTION_ACTIVE", "LAST_CHANGE_TIME"}),
    "M_PERSISTENCE_ENCRYPTION_STATUS": frozenset({
        "HOST", "PORT", "ENCRYPTION_ACTIVE", "ENCRYPTION_ACTIVE_AFTER_NEXT_SAVEPOINT",
        "IS_LATEST_ROOT_KEY_VERSION", "DATA_CONVERSION_ACTIVE"}),
    "M_INIFILE_CONTENTS": _INI_ALLOWED,
    "AUDIT_POLICIES": frozenset({
        "AUDIT_POLICY_NAME", "EVENT_ACTION", "EVENT_STATUS", "EVENT_LEVEL",
        "IS_AUDIT_POLICY_ACTIVE", "IS_VALID", "USER_NAME", "EXCEPT_USER_NAME", "OBJECT_TYPE",
        "OBJECT_SCHEMA", "OBJECT_NAME", "TRAIL_TYPE", "RETENTION_PERIOD"}),
}

# Words that are SQL, functions, the schema, or aliases the module defines:
# never a column of a view.
SQL_WORDS = frozenset({
    "SELECT", "TOP", "FROM", "WHERE", "AND", "OR", "NOT", "IN", "IS", "NULL", "AS", "CAST",
    "NVARCHAR", "ORDER", "BY", "GROUP", "DESC", "ASC", "LEFT", "JOIN", "ON", "UNION", "ALL",
    "CASE", "WHEN", "THEN", "ELSE", "END", "COUNT", "SUM", "COALESCE", "DAYS_BETWEEN",
    "CURRENT_TIMESTAMP", "SYS",
    # table aliases
    "R", "G", "F",
    # column aliases
    "GRANTEE_COUNT", "DAYS_TO_EXPIRY", "TOTAL_USERS", "ACTIVE_USERS", "DEACTIVATED_USERS",
    "ACTIVE_CHANGE_NEEDED", "ACTIVE_LIFETIME_UNCHECKED", "ACTIVE_WITH_INVALID_ATTEMPTS",
    "SYSTEM_PRIVILEGE_COUNT", "GRANTABLE_COUNT", "ROLE_COUNT", "PARENT_ROLE", "VIA_ROLE",
    "FINDING", "POLICY_COUNT", "ACTIVE_POLICY_COUNT",
})


def _views(sql: str) -> set[str]:
    tokens = tokenize(sql)
    return {tokens[i + 2].value for i, t in enumerate(tokens[:-2])
            if t.is_word("SYS") and tokens[i + 1].is_symbol(".")}


def test_every_selected_column_is_on_its_views_allow_list() -> None:
    for check, statement in _statements():
        views = _views(statement.sql)
        assert views and views <= set(VIEW_COLUMNS), (check.name, statement.label, views)
        allowed = set().union(*(VIEW_COLUMNS[v] for v in views)) | views
        stray = _words(statement.sql) - SQL_WORDS - allowed
        assert not stray, (check.name, statement.label, sorted(stray))
        quoted = {t.text for t in tokenize(statement.sql) if t.kind not in ("word", "string",
                                                                            "symbol", "param",
                                                                            "number")}
        assert quoted <= {'"KEY"', '"VALUE"'}, (check.name, statement.label, quoted)


def test_no_word_names_a_comment_secret_token_or_passphrase() -> None:
    for check, statement in _statements():
        for token in tokenize(statement.sql):
            text = token.text.upper()
            for part in ("COMMENT", "SECRET", "TOKEN", "PASSPHRASE"):
                assert part not in text, (check.name, statement.label, part)


def test_roles_counts_grantees_once_not_per_role() -> None:
    sql = registry.get_check("roles").statements[0].sql
    assert "LEFT JOIN (SELECT ROLE_SCHEMA_NAME, ROLE_NAME, COUNT(GRANTEE)" in sql
    assert "GROUP BY ROLE_SCHEMA_NAME, ROLE_NAME" in sql
    assert "G.ROLE_SCHEMA_NAME IS NULL AND R.ROLE_SCHEMA_NAME IS NULL" in sql
    # No correlated subquery in the select list.
    assert sql.index("FROM SYS.ROLES R") < sql.index("SELECT ROLE_SCHEMA_NAME")


def test_privilege_analysis_is_bounded_without_a_grantee() -> None:
    check = registry.get_check("privilegeAnalysis")
    powerful, counts, roles = (s.sql for s in check.statements)
    assert "OBJECT_TYPE = 'SYSTEMPRIVILEGE' AND PRIVILEGE IN (" in powerful
    assert "OBJECT_TYPE = 'SYSTEMPRIVILEGE'" in counts
    for sql in (counts, roles):
        assert ("((CAST(? AS NVARCHAR(256)) IS NULL AND GRANTEE_TYPE = 'USER')"
                " OR GRANTEE = ?)") in sql
    assert "object privileges not counted" in check.purpose


def _statements():
    for check in security.CHECKS:
        for statement in check.statements:
            yield check, statement


def _words(sql: str) -> set[str]:
    return {t.value for t in tokenize(sql) if t.kind == "word"}


def _all_words() -> set[str]:
    return set().union(*(_words(s.sql) for _, s in _statements()))


def test_the_security_checks_are_pinned_and_registered() -> None:
    assert tuple(c.name for c in security.CHECKS) == EXPECTED
    for check in security.CHECKS:
        assert registry.get_check(check.name) is check
        assert check.category == "security"
    assert sorted(c.name for c in registry.all_checks() if c.category == "security") == sorted(
        EXPECTED)


def test_every_purpose_fits_and_no_statement_text_column() -> None:
    for check in security.CHECKS:
        assert len(check.purpose) <= 120
        assert check.masked_columns == frozenset()
        assert check.requires_select is False


def test_no_password_column_beyond_the_flags() -> None:
    for check, statement in _statements():
        for word in _words(statement.sql):
            if "PASSWORD" in word:
                assert word in PASSWORD_ALLOWED, (check.name, statement.label)
    # A string literal or quoted name is not a word token: none may hold it.
    for _, statement in _statements():
        for token in tokenize(statement.sql):
            if token.kind != "word":
                assert "PASSWORD" not in token.text.upper() or token.text in (
                    "'password_lifetime_unchecked'",)


def test_no_certificate_body_or_comment() -> None:
    words = _all_words()
    assert not words & CERTIFICATE_NEVER
    sql = registry.get_check("certificates").statements[0].sql
    assert "SYS.CERTIFICATES" in sql
    assert {"SUBJECT_DISTINGUISHED_NAME", "ISSUER_DISTINGUISHED_NAME", "SERIAL_NUMBER",
            "VALID_UNTIL"} <= _words(sql)


def test_no_key_material() -> None:
    words = _all_words()
    assert not words & KEY_MATERIAL
    for word in words:
        assert not (word.endswith("_HASH") or "PRIVATE" in word), word


def test_no_audit_trail_personal_data_or_path() -> None:
    words = _all_words()
    assert not words & NEVER
    for _, statement in _statements():
        assert "AUDIT_LOG" not in statement.sql.upper()
        assert "*" not in statement.sql


def test_ini_contents_are_read_for_named_keys_only() -> None:
    seen = 0
    for check, statement in _statements():
        if "M_INIFILE_CONTENTS" not in _words(statement.sql):
            continue
        seen += 1
        sql = statement.sql
        assert "FILE_NAME = 'global.ini'" in sql, check.name
        # Every SECTION predicate is paired with a key named exactly.
        sections = re.findall(r"SECTION = '[^']+'", sql)
        keyed = re.findall(r"SECTION = '[^']+' AND \"KEY\" (?:IN \(|= ')", sql)
        assert sections and len(sections) == len(keyed), (check.name, statement.label)
        assert "LIKE" not in _words(sql)
        for word in ("path", "keystore", "truststore", "basepath"):
            assert word not in sql.lower(), (check.name, word)
    assert seen == 3


def test_users_shows_flags_not_identities() -> None:
    words = _words(registry.get_check("users").statements[0].sql)
    assert {"USER_DEACTIVATED", "VALID_UNTIL", "LAST_SUCCESSFUL_CONNECT",
            "INVALID_CONNECT_ATTEMPTS", "PASSWORD_CHANGE_NEEDED"} <= words


def test_persistence_encryption_reads_status_flags_only() -> None:
    sql = registry.get_check("encryptionStatus").statements[1].sql
    assert "M_PERSISTENCE_ENCRYPTION_STATUS" in sql
    key_words = {w for w in _words(sql) if "KEY" in w}
    assert key_words == {"IS_LATEST_ROOT_KEY_VERSION"}


def test_grant_chains_say_one_level_per_part() -> None:
    check = registry.get_check("grantChains")
    assert "one level per part" in check.purpose
    assert "roles matched by name" in check.purpose
    assert "not read" in registry.get_check("auditLog").purpose
    assert "no score" in registry.get_check("securityScan").purpose


@pytest.mark.parametrize("name", ["inspectUser", "grantChains"])
async def test_object_is_required(monkeypatch, name: str) -> None:
    answer, executes = await run_on_fake(monkeypatch, name, ["A"], [("x",), ("y",)])
    assert answer["error"]["code"] == "invalid_argument"
    assert answer["error"]["argument"] == "object"
    assert executes == []


async def test_users(monkeypatch) -> None:
    assert_registered("users", "security", {"top", "object"})
    columns = ["USER_NAME", "USER_DEACTIVATED"]
    rows = [("APP_USER", "FALSE"), ("OLD_USER", "TRUE")]
    answer, executes = await run_on_fake(monkeypatch, "users", columns, rows)
    assert_single(answer, columns)
    assert executes[0][2] == (51, None, None)
    answer, executes = await run_on_fake(monkeypatch, "users", columns, rows,
                                         object="APP_USER", top=5)
    assert executes[0][2] == (6, "APP_USER", "APP_USER")


async def test_roles(monkeypatch) -> None:
    assert_registered("roles", "security", {"top", "schema"})
    columns = ["ROLE_SCHEMA_NAME", "ROLE_NAME", "GRANTEE_COUNT"]
    rows = [(None, "MONITORING", 3), ("APP", "APP_READ", 0)]
    answer, executes = await run_on_fake(monkeypatch, "roles", columns, rows, schema="APP")
    assert_single(answer, columns)
    assert executes[0][2] == (51, "APP", "APP")


async def test_inspect_user(monkeypatch) -> None:
    parts = ("user", "roles", "privileges")
    check = assert_registered("inspectUser", "security", {"top", "object"}, parts=parts)
    assert check.required == frozenset({"object"})
    columns = ["NAME", "FLAG"]
    rows = [("A", "TRUE"), ("B", "FALSE")]
    answer, executes = await run_on_fake(monkeypatch, "inspectUser", columns, rows,
                                         object="APP_USER", top=10)
    assert [p["label"] for p in answer["parts"]] == list(parts)
    assert [e[2] for e in executes] == [("APP_USER",), (11, "APP_USER"), (11, "APP_USER")]


async def test_certificates(monkeypatch) -> None:
    assert_registered("certificates", "security", {"top"})
    columns = ["CERTIFICATE_NAME", "VALID_UNTIL", "DAYS_TO_EXPIRY"]
    rows = [("ca_one", "2027-01-01", 50), ("ca_two", "2030-01-01", 1200)]
    answer, executes = await run_on_fake(monkeypatch, "certificates", columns, rows)
    assert_single(answer, columns)
    assert executes[0][2] == (51,)


async def test_pwd_policy(monkeypatch) -> None:
    parts = ("policy", "users")
    assert_registered("pwdPolicy", "security", set(), parts=parts)
    columns = ["PROPERTY", "VALUE"]
    rows = [("minimal_password_length", "8"), ("maximum_invalid_connect_attempts", "6")]
    answer, executes = await run_on_fake(monkeypatch, "pwdPolicy", columns, rows)
    assert [p["label"] for p in answer["parts"]] == list(parts)
    assert all(p["row_count"] == 2 for p in answer["parts"])
    assert [e[2] if len(e) > 2 else () for e in executes] == [(), ()]


async def test_encryption_status(monkeypatch) -> None:
    parts = ("overview", "persistence", "settings")
    assert_registered("encryptionStatus", "security", {"top"}, parts=parts)
    columns = ["SCOPE", "IS_ENCRYPTION_ACTIVE"]
    rows = [("PERSISTENCE", "TRUE"), ("LOG", "FALSE")]
    answer, executes = await run_on_fake(monkeypatch, "encryptionStatus", columns, rows)
    assert [p["label"] for p in answer["parts"]] == list(parts)
    assert [e[2] if len(e) > 2 else () for e in executes] == [(), (51,), ()]


async def test_privilege_analysis(monkeypatch) -> None:
    parts = ("powerful", "privilege_counts", "role_counts")
    check = assert_registered("privilegeAnalysis", "security", {"top", "object"}, parts=parts)
    assert "'USER ADMIN'" in check.statements[0].sql
    columns = ["GRANTEE", "PRIVILEGE_COUNT"]
    rows = [("APP_ADMIN", 40), ("APP_USER", 3)]
    answer, executes = await run_on_fake(monkeypatch, "privilegeAnalysis", columns, rows)
    assert [p["label"] for p in answer["parts"]] == list(parts)
    assert [e[2] for e in executes] == [(51, None, None)] * 3


async def test_grant_chains(monkeypatch) -> None:
    parts = ("roles", "nested_roles", "privileges", "role_privileges")
    check = assert_registered("grantChains", "security", {"top", "object"}, parts=parts)
    assert check.required == frozenset({"object"})
    columns = ["ROLE_NAME", "PRIVILEGE"]
    rows = [("MONITORING", "CATALOG READ"), ("APP_READ", "SELECT")]
    answer, executes = await run_on_fake(monkeypatch, "grantChains", columns, rows,
                                         object="APP_USER")
    assert [p["label"] for p in answer["parts"]] == list(parts)
    assert [e[2] for e in executes] == [(51, "APP_USER")] * 4


async def test_security_scan(monkeypatch) -> None:
    parts = ("user_findings", "system_user", "audit_policies", "settings")
    check = assert_registered("securityScan", "security", {"top"}, parts=parts)
    assert "UNION ALL" in check.statements[0].sql
    columns = ["FINDING", "USER_NAME"]
    rows = [("never_connected", "APP_USER"), ("invalid_connect_attempts", "OLD_USER")]
    answer, executes = await run_on_fake(monkeypatch, "securityScan", columns, rows)
    assert [p["label"] for p in answer["parts"]] == list(parts)
    assert [e[2] if len(e) > 2 else () for e in executes] == [(51,), (), (), ()]


async def test_audit_log(monkeypatch) -> None:
    parts = ("policies", "settings")
    check = assert_registered("auditLog", "security", {"top"}, parts=parts)
    assert "'auditing configuration'" in check.statements[1].sql
    columns = ["AUDIT_POLICY_NAME", "IS_AUDIT_POLICY_ACTIVE"]
    rows = [("policy_one", "TRUE"), ("policy_two", "FALSE")]
    answer, executes = await run_on_fake(monkeypatch, "auditLog", columns, rows)
    assert [p["label"] for p in answer["parts"]] == list(parts)
    assert [e[2] if len(e) > 2 else () for e in executes] == [(51,), ()]
