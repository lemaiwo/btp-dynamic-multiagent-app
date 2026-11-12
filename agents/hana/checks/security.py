"""``builtin:hana`` checks of the category ``security``.

Each check is data: ``register(Check(...))`` at import, with its SQL as a
constant and every variable part a ``?`` bind (see :mod:`.base`).

Phase 3: users, roles, privileges, certificates, the password policy,
encryption and audit settings as the monitoring user can read them (the user
needs CATALOG READ to see other users' rows; AUDIT_POLICIES also needs CATALOG
READ or AUDIT ADMIN).

**What never leaves the database** (pinned by
``tests/test_hanadb_checks_security.py``): a password or password hash (only
the flags and times of ``SYS.USERS`` whose name holds ``PASSWORD``, and the
property names and values of ``M_PASSWORD_POLICY``); a certificate body
(``CERTIFICATES.CERTIFICATE``, an NCLOB) or its ``COMMENT``; key material (the
root key hash of ``M_PERSISTENCE_ENCRYPTION_STATUS``, ``ENCRYPTION_ROOT_KEYS``,
``M_PERSISTENCE_ENCRYPTION_KEYS`` are never read); the audit trail itself
(``AUDIT_LOG`` / ``M_AUDIT_LOG`` / ``ALL_AUDIT_LOG`` are never read: they hold
statement texts with their values); a user's external identity and parameters
(``EXTERNAL_IDENTITY``, ``USER_PARAMETERS``: personal data such as an e-mail
address); a file-system path (``M_VOLUMES.PATH``, the audit trail path, the key
store paths of ``[communication]``). The configuration is read from
``M_INIFILE_CONTENTS`` for named keys only, never a whole section.

The target is on-premise SAP HANA 2.0 (SPS07). Columns are taken from the SAP
HANA SQL and System Views Reference; a column documented for HANA Cloud only is
left out. No statement of this module has run on a landscape yet.
"""

from __future__ import annotations

from agents.hana.checks.base import Check, Statement, register

# The optional filter on a name: NULL (not given) keeps every row. The CAST
# gives the untyped NULL bind a type (as in performance.py).
_OPTIONAL_USER = "(CAST(? AS NVARCHAR(256)) IS NULL OR USER_NAME = ?)"
_OPTIONAL_GRANTEE = "(CAST(? AS NVARCHAR(256)) IS NULL OR GRANTEE = ?)"

# The columns of SYS.USERS that are shown: names, times and flags. Of the
# columns whose name holds PASSWORD only the flags and the change time
# (ADMIN_GIVEN_PASSWORD is left out: it says nothing a check needs). HANA has
# no "locked" column: a user is locked out while INVALID_CONNECT_ATTEMPTS has
# reached the policy's maximum_invalid_connect_attempts (pwdPolicy).
_USER_COLUMNS = (
    "USER_NAME, USERGROUP_NAME, CREATOR, CREATE_TIME, VALID_FROM, VALID_UNTIL,"
    " USER_DEACTIVATED, DEACTIVATION_TIME, LAST_SUCCESSFUL_CONNECT,"
    " LAST_INVALID_CONNECT_ATTEMPT, INVALID_CONNECT_ATTEMPTS, PASSWORD_CHANGE_NEEDED,"
    " IS_PASSWORD_LIFETIME_CHECK_ENABLED, LAST_PASSWORD_CHANGE_TIME, IS_PASSWORD_ENABLED"
)

# on-prem: hana-cli reads USER_NAME, USERGROUP_NAME, CREATOR, CREATE_TIME
# with `USER_NAME LIKE ?`; here the validity, deactivation, connect and
# password-flag columns are added and the filter is an exact name (`object`).
# on-prem: not yet run
USERS = Check(
    name="users",
    category="security",
    purpose=("Database users with validity, deactivation, connect and password flags; never a"
             " password, hash or identity."),
    source="bin/users.js",
    statements=(Statement(
        "",
        f"SELECT TOP ? {_USER_COLUMNS}"
        " FROM SYS.USERS"
        f" WHERE {_OPTIONAL_USER}"
        " ORDER BY USER_NAME",
        ("top_plus_one", "object", "object"),
    ),),
    args=frozenset({"top", "object"}),
)

# on-prem: hana-cli reads ROLE_SCHEMA_NAME, ROLE_NAME, CREATOR, CREATE_TIME
# with LIKE filters; here the schema filter is exact (`schema`; a global role
# has no schema and is listed only without the filter), COMMENT is left out
# (free text) and the number of direct grantees is added from GRANTED_ROLES.
# A role's schema may be NULL, so the count compares it NULL-safe.
# on-prem: not yet run
ROLES = Check(
    name="roles",
    category="security",
    purpose="Roles with creator, creation time and the number of direct grantees; no comment.",
    source="bin/roles.js",
    statements=(Statement(
        "",
        "SELECT TOP ? R.ROLE_SCHEMA_NAME, R.ROLE_NAME, R.CREATOR, R.CREATE_TIME,"
        " (SELECT COUNT(G.GRANTEE) FROM SYS.GRANTED_ROLES G"
        " WHERE G.ROLE_NAME = R.ROLE_NAME"
        " AND (G.ROLE_SCHEMA_NAME = R.ROLE_SCHEMA_NAME"
        " OR (G.ROLE_SCHEMA_NAME IS NULL AND R.ROLE_SCHEMA_NAME IS NULL))) AS GRANTEE_COUNT"
        " FROM SYS.ROLES R"
        " WHERE (CAST(? AS NVARCHAR(256)) IS NULL OR R.ROLE_SCHEMA_NAME = ?)"
        " ORDER BY R.ROLE_SCHEMA_NAME, R.ROLE_NAME",
        ("top_plus_one", "schema", "schema"),
    ),),
    args=frozenset({"top", "schema"}),
)

# on-prem: hana-cli reads `SELECT *` from USERS and USER_PARAMETERS; here the
# user header has the named columns of `users`, USER_PARAMETERS is left out
# (its values are personal data: e-mail, client, locale) and roles and
# privileges are names only, each part bounded by `top`.
# on-prem: not yet run
INSPECT_USER = Check(
    name="inspectUser",
    category="security",
    purpose=("One user (object): header, granted roles and granted privileges by type; names"
             " only, no parameters."),
    source="bin/inspectUser.js",
    statements=(
        Statement(
            "user",
            f"SELECT {_USER_COLUMNS}"
            " FROM SYS.USERS"
            " WHERE USER_NAME = ?",
            ("object",),
        ),
        Statement(
            "roles",
            "SELECT TOP ? ROLE_SCHEMA_NAME, ROLE_NAME, GRANTOR, IS_GRANTABLE"
            " FROM SYS.GRANTED_ROLES"
            " WHERE GRANTEE = ?"
            " ORDER BY ROLE_SCHEMA_NAME, ROLE_NAME",
            ("top_plus_one", "object"),
        ),
        Statement(
            "privileges",
            "SELECT TOP ? OBJECT_TYPE, SCHEMA_NAME, OBJECT_NAME, PRIVILEGE, GRANTOR,"
            " IS_GRANTABLE"
            " FROM SYS.GRANTED_PRIVILEGES"
            " WHERE GRANTEE = ?"
            " ORDER BY OBJECT_TYPE, SCHEMA_NAME, OBJECT_NAME, PRIVILEGE",
            ("top_plus_one", "object"),
        ),
    ),
    args=frozenset({"top", "object"}),
    required=frozenset({"object"}),
)

# on-prem: hana-cli reads `SELECT TOP 100 *` from CERTIFICATES, which returns
# the certificate itself (CERTIFICATE, an NCLOB) and COMMENT. Here: names,
# distinguished names, serial number, validity, days left and the key and
# signature algorithms. SUBJECT_ALT_NAME (host names) and FINGERPRINT are left
# out as well. Expiring first.
# on-prem: not yet run
CERTIFICATES = Check(
    name="certificates",
    category="security",
    purpose=("Certificates in the store: subject, issuer, serial, validity, algorithms; never"
             " the certificate body."),
    source="bin/certificates.js",
    statements=(Statement(
        "",
        "SELECT TOP ? CERTIFICATE_NAME, CERTIFICATE_ID, SUBJECT_DISTINGUISHED_NAME,"
        " ISSUER_DISTINGUISHED_NAME, SERIAL_NUMBER, VALID_FROM, VALID_UNTIL,"
        " DAYS_BETWEEN(CURRENT_TIMESTAMP, VALID_UNTIL) AS DAYS_TO_EXPIRY,"
        " PUBLIC_KEY_ALGORITHM, PUBLIC_KEY_LENGTH, SIGNATURE_ALGORITHM"
        " FROM SYS.CERTIFICATES"
        " ORDER BY VALID_UNTIL, CERTIFICATE_NAME",
        ("top_plus_one",),
    ),),
)

# on-prem: hana-cli shows a policy from its own text and counts users; the
# effective policy is M_PASSWORD_POLICY (PROPERTY, VALUE: lengths, lifetimes,
# the invalid connect limit; no password). The user counts are one row of
# SUM(CASE ...) over SYS.USERS. User group policies are not read.
# on-prem: not yet run
PWD_POLICY = Check(
    name="pwdPolicy",
    category="security",
    purpose=("Effective password policy properties and user counts by state; no user group"
             " policies, no passwords."),
    source="bin/pwdPolicy.js",
    statements=(
        Statement(
            "policy",
            "SELECT PROPERTY, \"VALUE\""
            " FROM SYS.M_PASSWORD_POLICY"
            " ORDER BY PROPERTY",
            (),
        ),
        Statement(
            "users",
            "SELECT COUNT(USER_NAME) AS TOTAL_USERS,"
            " SUM(CASE WHEN USER_DEACTIVATED = 'FALSE' THEN 1 ELSE 0 END) AS ACTIVE_USERS,"
            " SUM(CASE WHEN USER_DEACTIVATED = 'TRUE' THEN 1 ELSE 0 END) AS DEACTIVATED_USERS,"
            " SUM(CASE WHEN USER_DEACTIVATED = 'FALSE' AND PASSWORD_CHANGE_NEEDED = 'TRUE'"
            " THEN 1 ELSE 0 END) AS ACTIVE_CHANGE_NEEDED,"
            " SUM(CASE WHEN USER_DEACTIVATED = 'FALSE' AND IS_PASSWORD_ENABLED = 'TRUE'"
            " AND IS_PASSWORD_LIFETIME_CHECK_ENABLED = 'FALSE'"
            " THEN 1 ELSE 0 END) AS ACTIVE_LIFETIME_UNCHECKED,"
            " SUM(CASE WHEN USER_DEACTIVATED = 'FALSE' AND INVALID_CONNECT_ATTEMPTS > 0"
            " THEN 1 ELSE 0 END) AS ACTIVE_WITH_INVALID_ATTEMPTS"
            " FROM SYS.USERS",
            (),
        ),
    ),
    args=frozenset(),
)

# The encryption and TLS settings read by name (hana-cli's IN lists; its
# `KEY LIKE '%encrypt%'` and `'%internal%'` patterns are not ported: default
# deny, and the [communication] section also holds key store paths).
_ENCRYPTION_SETTINGS = (
    "(SECTION = 'persistence' AND \"KEY\" IN ('encryption', 'encryption_algorithm',"
    " 'encryption_key_type'))"
    " OR (SECTION = 'communication' AND \"KEY\" IN ('ssl', 'sslenforce', 'sslcipher',"
    " 'sslprotocol'))"
)

# on-prem: hana-cli reads ini keys, M_VOLUMES (with PATH) and the backup
# catalogue. Here: M_ENCRYPTION_OVERVIEW (data volumes, redo log, backup),
# M_PERSISTENCE_ENCRYPTION_STATUS per service (status flags only, never
# USED_ROOT_KEY_HASH or KEY_PAGE_ENTRY_COUNT) and the named settings.
# M_VOLUMES (a server path) and the backup catalogue (backupStatus) are dropped.
# on-prem: not yet run
ENCRYPTION_STATUS = Check(
    name="encryptionStatus",
    category="security",
    purpose=("Encryption at rest per scope and service, TLS settings by name; no key, key hash"
             " or volume path."),
    source="bin/encryptionStatus.js",
    statements=(
        Statement(
            "overview",
            "SELECT SCOPE, IS_ENCRYPTION_ACTIVE, LAST_CHANGE_TIME"
            " FROM SYS.M_ENCRYPTION_OVERVIEW"
            " ORDER BY SCOPE",
            (),
        ),
        Statement(
            "persistence",
            "SELECT TOP ? HOST, PORT, ENCRYPTION_ACTIVE, ENCRYPTION_ACTIVE_AFTER_NEXT_SAVEPOINT,"
            " IS_LATEST_ROOT_KEY_VERSION, DATA_CONVERSION_ACTIVE"
            " FROM SYS.M_PERSISTENCE_ENCRYPTION_STATUS"
            " ORDER BY HOST, PORT",
            ("top_plus_one",),
        ),
        Statement(
            "settings",
            "SELECT LAYER_NAME, SECTION, \"KEY\", \"VALUE\""
            " FROM SYS.M_INIFILE_CONTENTS"
            " WHERE FILE_NAME = 'global.ini'"
            f" AND ({_ENCRYPTION_SETTINGS})"
            " ORDER BY SECTION, \"KEY\", LAYER_NAME",
            (),
        ),
    ),
)

# The system privileges that administer users, roles, data, keys, trust or the
# audit trail, or read the whole catalogue: their grantees are what an audit
# asks for first.
_POWERFUL_PRIVILEGES = (
    "'DATA ADMIN', 'USER ADMIN', 'ROLE ADMIN', 'CATALOG READ', 'DATABASE ADMIN',"
    " 'INIFILE ADMIN', 'AUDIT ADMIN', 'AUDIT OPERATOR', 'TRUST ADMIN', 'CREDENTIAL ADMIN',"
    " 'ENCRYPTION ROOT KEY ADMIN', 'BACKUP ADMIN', 'SERVICE ADMIN', 'SESSION ADMIN',"
    " 'LOG ADMIN', 'RESOURCE ADMIN', 'LICENSE ADMIN', 'EXPORT', 'IMPORT', 'DEVELOPMENT'"
)

# on-prem: hana-cli lists one user's system privileges, object privileges and
# roles (the current user by default). Here across grantees, optionally one
# (`object`): direct grantees of the powerful system privileges, and privilege
# and role counts per grantee. Grants through roles are not resolved (see
# grantChains).
# on-prem: not yet run
PRIVILEGE_ANALYSIS = Check(
    name="privilegeAnalysis",
    category="security",
    purpose=("Direct grantees of powerful system privileges, privilege and role counts per"
             " grantee; role grants not resolved."),
    source="bin/privilegeAnalysis.js",
    statements=(
        Statement(
            "powerful",
            "SELECT TOP ? PRIVILEGE, GRANTEE, GRANTEE_TYPE, GRANTOR, IS_GRANTABLE"
            " FROM SYS.GRANTED_PRIVILEGES"
            " WHERE OBJECT_TYPE = 'SYSTEMPRIVILEGE'"
            f" AND PRIVILEGE IN ({_POWERFUL_PRIVILEGES})"
            f" AND {_OPTIONAL_GRANTEE}"
            " ORDER BY PRIVILEGE, GRANTEE",
            ("top_plus_one", "object", "object"),
        ),
        Statement(
            "privilege_counts",
            "SELECT TOP ? GRANTEE, GRANTEE_TYPE, COUNT(PRIVILEGE) AS PRIVILEGE_COUNT,"
            " SUM(CASE WHEN OBJECT_TYPE = 'SYSTEMPRIVILEGE' THEN 1 ELSE 0 END)"
            " AS SYSTEM_PRIVILEGE_COUNT,"
            " SUM(CASE WHEN IS_GRANTABLE = 'TRUE' THEN 1 ELSE 0 END) AS GRANTABLE_COUNT"
            " FROM SYS.GRANTED_PRIVILEGES"
            f" WHERE {_OPTIONAL_GRANTEE}"
            " GROUP BY GRANTEE, GRANTEE_TYPE"
            " ORDER BY PRIVILEGE_COUNT DESC, GRANTEE",
            ("top_plus_one", "object", "object"),
        ),
        Statement(
            "role_counts",
            "SELECT TOP ? GRANTEE, GRANTEE_TYPE, COUNT(ROLE_NAME) AS ROLE_COUNT,"
            " SUM(CASE WHEN IS_GRANTABLE = 'TRUE' THEN 1 ELSE 0 END) AS GRANTABLE_COUNT"
            " FROM SYS.GRANTED_ROLES"
            f" WHERE {_OPTIONAL_GRANTEE}"
            " GROUP BY GRANTEE, GRANTEE_TYPE"
            " ORDER BY ROLE_COUNT DESC, GRANTEE",
            ("top_plus_one", "object", "object"),
        ),
    ),
    args=frozenset({"top", "object"}),
)

# The roles granted directly to the grantee (`object`), as a subquery: the
# roles a nested part starts from.
_DIRECT_ROLES = "(SELECT ROLE_NAME FROM SYS.GRANTED_ROLES WHERE GRANTEE = ?)"

# on-prem: hana-cli recurses over GRANTED_ROLES in JavaScript. Here one level
# per part: the grantee's direct roles, the roles granted to those roles, the
# grantee's direct privileges and the privileges of its direct roles. A deeper
# level is another call with a role as `object`. Roles are matched by name
# (two schema-local roles of one name in different schemas are not told apart).
# on-prem: not yet run
GRANT_CHAINS = Check(
    name="grantChains",
    category="security",
    purpose=("Roles and privileges of a grantee (object), one level per part; go deeper by"
             " calling again with a role."),
    source="bin/grantChains.js",
    statements=(
        Statement(
            "roles",
            "SELECT TOP ? ROLE_SCHEMA_NAME, ROLE_NAME, GRANTOR, IS_GRANTABLE"
            " FROM SYS.GRANTED_ROLES"
            " WHERE GRANTEE = ?"
            " ORDER BY ROLE_SCHEMA_NAME, ROLE_NAME",
            ("top_plus_one", "object"),
        ),
        Statement(
            "nested_roles",
            "SELECT TOP ? GRANTEE AS PARENT_ROLE, ROLE_SCHEMA_NAME, ROLE_NAME, GRANTOR"
            " FROM SYS.GRANTED_ROLES"
            " WHERE GRANTEE_TYPE = 'ROLE'"
            f" AND GRANTEE IN {_DIRECT_ROLES}"
            " ORDER BY PARENT_ROLE, ROLE_SCHEMA_NAME, ROLE_NAME",
            ("top_plus_one", "object"),
        ),
        Statement(
            "privileges",
            "SELECT TOP ? OBJECT_TYPE, SCHEMA_NAME, OBJECT_NAME, PRIVILEGE, GRANTOR,"
            " IS_GRANTABLE"
            " FROM SYS.GRANTED_PRIVILEGES"
            " WHERE GRANTEE = ?"
            " ORDER BY OBJECT_TYPE, SCHEMA_NAME, OBJECT_NAME, PRIVILEGE",
            ("top_plus_one", "object"),
        ),
        Statement(
            "role_privileges",
            "SELECT TOP ? GRANTEE AS VIA_ROLE, OBJECT_TYPE, SCHEMA_NAME, OBJECT_NAME,"
            " PRIVILEGE"
            " FROM SYS.GRANTED_PRIVILEGES"
            " WHERE GRANTEE_TYPE = 'ROLE'"
            f" AND GRANTEE IN {_DIRECT_ROLES}"
            " ORDER BY VIA_ROLE, OBJECT_TYPE, SCHEMA_NAME, OBJECT_NAME, PRIVILEGE",
            ("top_plus_one", "object"),
        ),
    ),
    args=frozenset({"top", "object"}),
    required=frozenset({"object"}),
)

# The settings a scan reports, by name: data volume encryption, TLS and the
# global audit switch (hana-cli's three keys).
_SCAN_SETTINGS = (
    "(SECTION = 'persistence' AND \"KEY\" = 'encryption')"
    " OR (SECTION = 'communication' AND \"KEY\" = 'ssl')"
    " OR (SECTION = 'auditing configuration' AND \"KEY\" = 'global_auditing_state')"
)

# One row per user finding; a user may appear under several findings.
_USER_FINDINGS = (
    "SELECT 'password_lifetime_unchecked' AS FINDING, USER_NAME FROM SYS.USERS"
    " WHERE USER_DEACTIVATED = 'FALSE' AND IS_PASSWORD_ENABLED = 'TRUE'"
    " AND IS_PASSWORD_LIFETIME_CHECK_ENABLED = 'FALSE'"
    " UNION ALL SELECT 'invalid_connect_attempts' AS FINDING, USER_NAME FROM SYS.USERS"
    " WHERE USER_DEACTIVATED = 'FALSE' AND INVALID_CONNECT_ATTEMPTS > 0"
    " UNION ALL SELECT 'never_connected' AS FINDING, USER_NAME FROM SYS.USERS"
    " WHERE USER_DEACTIVATED = 'FALSE' AND LAST_SUCCESSFUL_CONNECT IS NULL"
    " UNION ALL SELECT 'validity_ended' AS FINDING, USER_NAME FROM SYS.USERS"
    " WHERE USER_DEACTIVATED = 'FALSE' AND VALID_UNTIL < CURRENT_TIMESTAMP"
)

# on-prem: hana-cli counts users and reads three ini keys and the active audit
# policies, then scores them; the score is not ported. Here the findings are
# rows the model reads: users per finding, the SYSTEM user's state, audit
# policy counts and the three settings by name.
# on-prem: not yet run
SECURITY_SCAN = Check(
    name="securityScan",
    category="security",
    purpose=("Findings as rows: user findings, SYSTEM user state, audit policy counts, three"
             " settings; no score."),
    source="bin/securityScan.js",
    statements=(
        Statement(
            "user_findings",
            "SELECT TOP ? FINDING, USER_NAME"
            f" FROM ({_USER_FINDINGS}) F"
            " ORDER BY FINDING, USER_NAME",
            ("top_plus_one",),
        ),
        Statement(
            "system_user",
            "SELECT USER_NAME, USER_DEACTIVATED, DEACTIVATION_TIME, LAST_SUCCESSFUL_CONNECT,"
            " INVALID_CONNECT_ATTEMPTS, IS_PASSWORD_LIFETIME_CHECK_ENABLED"
            " FROM SYS.USERS"
            " WHERE USER_NAME = 'SYSTEM'",
            (),
        ),
        Statement(
            "audit_policies",
            "SELECT COUNT(AUDIT_POLICY_NAME) AS POLICY_COUNT,"
            " SUM(CASE WHEN IS_AUDIT_POLICY_ACTIVE = 'TRUE' THEN 1 ELSE 0 END)"
            " AS ACTIVE_POLICY_COUNT"
            " FROM SYS.AUDIT_POLICIES",
            (),
        ),
        Statement(
            "settings",
            "SELECT LAYER_NAME, SECTION, \"KEY\", \"VALUE\""
            " FROM SYS.M_INIFILE_CONTENTS"
            " WHERE FILE_NAME = 'global.ini'"
            f" AND ({_SCAN_SETTINGS})"
            " ORDER BY SECTION, \"KEY\", LAYER_NAME",
            (),
        ),
    ),
)

# The audit settings read by name. hana-cli reads the section 'auditing',
# which does not exist on HANA 2.0 (the section is 'auditing configuration'),
# and the whole of it; the trail paths (default_audit_trail_path and the
# like) are not among these keys.
_AUDIT_SETTINGS = (
    "SECTION = 'auditing configuration' AND \"KEY\" IN ('global_auditing_state',"
    " 'default_audit_trail_type', 'emergency_audit_trail_type', 'alert_audit_trail_type',"
    " 'critical_audit_trail_type', 'audit_statement_length', 'minimal_retention_period')"
)

# on-prem: hana-cli reads the auditing section and `SELECT *` from
# AUDIT_POLICIES. Here the policies' named columns (PRINCIPAL_*, CREATE_TIME
# and the authentication-method columns are HANA Cloud and left out) and the
# named settings. The audit trail (AUDIT_LOG and its kin) is never read.
# on-prem: not yet run
AUDIT_LOG = Check(
    name="auditLog",
    category="security",
    purpose=("Audit policies and the named audit settings; the audit log entries themselves"
             " are not read."),
    source="bin/auditLog.js",
    statements=(
        Statement(
            "policies",
            "SELECT TOP ? AUDIT_POLICY_NAME, EVENT_ACTION, EVENT_STATUS, EVENT_LEVEL,"
            " IS_AUDIT_POLICY_ACTIVE, IS_VALID, USER_NAME, EXCEPT_USER_NAME, OBJECT_TYPE,"
            " OBJECT_SCHEMA, OBJECT_NAME, TRAIL_TYPE, RETENTION_PERIOD"
            " FROM SYS.AUDIT_POLICIES"
            " ORDER BY AUDIT_POLICY_NAME, EVENT_ACTION",
            ("top_plus_one",),
        ),
        Statement(
            "settings",
            "SELECT LAYER_NAME, SECTION, \"KEY\", \"VALUE\""
            " FROM SYS.M_INIFILE_CONTENTS"
            " WHERE FILE_NAME = 'global.ini'"
            f" AND ({_AUDIT_SETTINGS})"
            " ORDER BY \"KEY\", LAYER_NAME",
            (),
        ),
    ),
)

CHECKS: tuple[Check, ...] = (
    USERS, ROLES, INSPECT_USER, CERTIFICATES, PWD_POLICY, ENCRYPTION_STATUS,
    PRIVILEGE_ANALYSIS, GRANT_CHAINS, SECURITY_SCAN, AUDIT_LOG,
)

register(*CHECKS)
