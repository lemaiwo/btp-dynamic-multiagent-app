"""The SELECT guard of ``builtin:hana`` (``agents/hana/guard.py``).

On the first landscape the database user can read and write business
schemas, so this guard, not the database, keeps model-written SQL on the
monitoring views. Each refused statement carries the marker ``ZQX`` so the
test can prove that a refusal never repeats any part of the SQL.
"""

from __future__ import annotations

import pytest

from agents.hana.guard import MAX_SQL_CHARS, SelectRefused, check_select

ACCEPTED = [
    "SELECT * FROM M_SERVICES",
    "select host, port from m_services where service_name = 'indexserver'",
    "SELECT TOP 10 * FROM SYS.M_EXPENSIVE_STATEMENTS ORDER BY DURATION_MICROSEC DESC",
    "WITH x AS (SELECT * FROM M_LOCKS) SELECT * FROM x",
    "SELECT u.USER_NAME, c.CONNECTION_ID FROM SYS.USERS u "
    "JOIN M_CONNECTIONS c ON c.USER_NAME = u.USER_NAME",
    'SELECT * FROM "SYS"."M_TABLES"',
    "SELECT * FROM M_SERVICES WHERE HOST = 'a;b'",
    "SELECT * FROM M_SERVICES WHERE HOST = '-- not a comment'",
    "SELECT * FROM M_SERVICES WHERE HOST = '/* nor this */'",
    "SELECT * FROM M_SERVICES WHERE HOST = 'INTO x'",
    "SELECT * FROM M_SERVICES WHERE HOST = 'DROP TABLE BKPF'",
    "SELECT * FROM M_SERVICES WHERE HOST = 'it''s'",
    "SELECT * FROM M_SERVICES WHERE PORT IN (SELECT PORT FROM M_SERVICE_STATISTICS)",
    # more accepted shapes
    "SELECT * FROM _SYS_STATISTICS.HOST_WORKLOAD",
    "SELECT * FROM sys.m_tables",
    "SELECT 1 FROM SYS.DUMMY",
    "SELECT\n*\nFROM\tM_SERVICES\r\nWHERE PORT > 1",
    "SELECT a.HOST FROM M_SERVICES a, SYS.M_HOST_INFORMATION b WHERE a.HOST = b.HOST",
    "SELECT * FROM M_SERVICES AS s LEFT OUTER JOIN M_VOLUMES AS v ON s.PORT = v.PORT",
    "SELECT * FROM M_SERVICES CROSS JOIN M_VOLUMES",
    "SELECT * FROM M_SERVICES s INNER JOIN (SELECT PORT FROM M_VOLUMES) v ON s.PORT = v.PORT",
    "SELECT * FROM (SELECT HOST FROM M_SERVICES) t WHERE t.HOST = 'a'",
    "SELECT (SELECT COUNT(*) FROM M_CONNECTIONS) AS N FROM M_SERVICES",
    "SELECT HOST FROM M_SERVICES UNION ALL SELECT HOST FROM SYS.M_VOLUMES",
    "SELECT HOST FROM M_SERVICES INTERSECT SELECT HOST FROM M_VOLUMES",
    "SELECT HOST FROM M_SERVICES EXCEPT SELECT HOST FROM M_VOLUMES",
    "SELECT * FROM M_SERVICES WHERE EXISTS (SELECT 1 FROM M_VOLUMES v WHERE v.PORT = 1)",
    "WITH a AS (SELECT * FROM M_LOCKS), b (h) AS (SELECT HOST FROM a) SELECT * FROM b, a",
    # a CTE named like a business table: the CTE wins in SQL scoping
    "WITH BKPF AS (SELECT * FROM M_LOCKS) SELECT * FROM BKPF",
    'SELECT "INTO", "FROM" FROM M_SERVICES',
    "SELECT EXTRACT(YEAR FROM START_TIME) FROM M_SERVICES",
    "SELECT TRIM(BOTH ' ' FROM HOST) FROM M_SERVICES",
    "SELECT SUM(x) OVER (PARTITION BY HOST ORDER BY PORT) FROM M_SERVICES",
    "SELECT * FROM M_SERVICES WHERE A = x'0A' AND B = N'abc' AND C = 1.5e3",
    "SELECT * FROM M_SERVICES ORDER BY HOST LIMIT 10 OFFSET 5",
    "SELECT HOST, COUNT(*) FROM M_SERVICES GROUP BY HOST HAVING COUNT(*) > 1",
    "SELECT SYS.SOME_SCALAR(1) FROM M_SERVICES",
    "SELECT * FROM M_SERVICES   \n",
]

REFUSED = [
    ("SELECT * FROM M_SERVICES; DROP TABLE ZQX", "one_statement"),
    ("SELECT * FROM M_SERVICES WHERE ZQX = 1;", "one_statement"),
    ("SELECT * FROM M_SERVICES -- ZQX", "no_comments"),
    ("SELECT * FROM M_SERVICES /* ZQX */", "no_comments"),
    ("SELECT * FROM M_SERVICES // ZQX", "no_comments"),
    ("INSERT INTO ZQX VALUES (1)", "select_only"),
    ("UPDATE ZQX SET A = 1", "select_only"),
    ("UPSERT ZQX VALUES (1) WITH PRIMARY KEY", "select_only"),
    ("DELETE FROM ZQX", "select_only"),
    ("MERGE INTO ZQX USING M_X ON 1 = 1", "select_only"),
    ("CREATE TABLE ZQX (A INT)", "select_only"),
    ("ALTER TABLE ZQX ADD (B INT)", "select_only"),
    ("DROP TABLE ZQX", "select_only"),
    ("GRANT SELECT ON ZQX TO PUBLIC", "select_only"),
    ("CALL ZQX()", "select_only"),
    ("DO BEGIN SELECT * FROM ZQX; END", "one_statement"),
    ("DO BEGIN DELETE FROM ZQX END", "select_only"),
    ("EXPLAIN PLAN FOR SELECT * FROM ZQX", "select_only"),
    ("SET SCHEMA ZQX", "select_only"),
    ("(SELECT * FROM M_SERVICES WHERE ZQX = 1)", "select_only"),
    ("", "select_only"),
    ("   ", "select_only"),
    ("SELECT HOST INTO :ZQX FROM M_SERVICES", "no_into"),
    ("SELECT HOST FROM M_SERVICES INTO ZQX", "no_into"),
    ("SELECT * FROM M_LOCKS WHERE ZQX = 1 FOR UPDATE", "no_for_update"),
    ("SELECT * FROM M_LOCKS ZQX FOR UPDATE", "no_for_update"),
    ("SELECT * FROM M_LOCKS WHERE ZQX = 1 FOR SHARE LOCK", "no_for_update"),
    ("SELECT * FROM M_LOCKS WHERE ZQX = 1 FOR UPDATE NOWAIT", "no_for_update"),
    ("SELECT * FROM M_LOCKS FOR SYSTEM_TIME AS OF '2026-01-01' WHERE ZQX = 1", "select_only"),
    ("SELECT * FROM M_LOCKS WHERE ZQX = 1 FOR JSON", "select_only"),
    ("SELECT * FROM SAPHANADB.BKPF WHERE ZQX = 1", "table_not_allowed"),
    ("SELECT * FROM BKPF WHERE ZQX = 1", "table_not_allowed"),
    ('SELECT * FROM "sys"."m_tables" WHERE ZQX = 1', "table_not_allowed"),
    ('SELECT * FROM "m_tables" WHERE ZQX = 1', "table_not_allowed"),
    ('SELECT * FROM "BKPF" WHERE ZQX = 1', "table_not_allowed"),
    ("SELECT * FROM _SYS_REPO.X WHERE ZQX = 1", "table_not_allowed"),
    ("SELECT * FROM SYS.SOME_FUNCTION(1) WHERE ZQX = 1", "table_not_allowed"),
    ("SELECT * FROM M_FUNCTION(1) WHERE ZQX = 1", "table_not_allowed"),
    ("SELECT * FROM M_SERVICES, BKPF WHERE ZQX = 1", "table_not_allowed"),
    ("SELECT * FROM M_SERVICES a, M_VOLUMES b, ZQX c", "table_not_allowed"),
    ("SELECT * FROM (SELECT * FROM BKPF) WHERE ZQX = 1", "table_not_allowed"),
    ("SELECT * FROM M_SERVICES, LATERAL (SELECT * FROM M_VOLUMES) ZQX", "table_not_allowed"),
    ("SELECT * FROM JSON_TABLE(ZQX, '$' COLUMNS (A INT PATH '$.a'))", "table_not_allowed"),
    ("SELECT * FROM SERIES_GENERATE_INTEGER(1, 1, 10) ZQX", "table_not_allowed"),
    ("SELECT * FROM SYS.SERIES_GENERATE_INTEGER(1, 1, 10) ZQX", "table_not_allowed"),
    ("SELECT * FROM ZQX.SYS.M_TABLES", "table_not_allowed"),
    ("SELECT * FROM SYS.M_TABLES.ZQX", "table_not_allowed"),
    ("SELECT * FROM :ZQX", "table_not_allowed"),
    ("SELECT * FROM M_SERVICES AS OF UTCTIMESTAMP '2026' ZQX", "table_not_allowed"),
    ("SELECT * FROM M_SERVICES TABLESAMPLE BERNOULLI (10) ZQX", "table_not_allowed"),
    ("SELECT * FROM M_SERVICES ZQX (A, B)", "table_not_allowed"),
    ("SELECT * FROM ((SELECT * FROM M_SERVICES)) ZQX", "table_not_allowed"),
    ("SELECT * FROM (M_SERVICES JOIN BKPF ON 1 = 1) ZQX", "table_not_allowed"),
    # every table reference anywhere, not only the top-level FROM
    ("SELECT HOST FROM M_SERVICES UNION SELECT ZQX FROM BKPF", "table_not_allowed"),
    ("SELECT HOST FROM M_SERVICES UNION ALL SELECT ZQX FROM BKPF", "table_not_allowed"),
    ("SELECT HOST FROM M_SERVICES INTERSECT SELECT ZQX FROM BKPF", "table_not_allowed"),
    ("SELECT HOST FROM M_SERVICES EXCEPT SELECT ZQX FROM BKPF", "table_not_allowed"),
    ("SELECT HOST FROM M_SERVICES MINUS SELECT ZQX FROM BKPF", "table_not_allowed"),
    ("SELECT (SELECT MAX(ZQX) FROM BKPF) FROM M_SERVICES", "table_not_allowed"),
    ("SELECT * FROM M_SERVICES WHERE ZQX IN (SELECT A FROM BKPF)", "table_not_allowed"),
    ("SELECT * FROM M_SERVICES WHERE ZQX IN (SELECT A FROM M_VOLUMES, BKPF)", "table_not_allowed"),
    ("SELECT * FROM M_SERVICES WHERE EXISTS (SELECT 1 FROM BKPF) AND ZQX = 1", "table_not_allowed"),
    ("SELECT * FROM M_SERVICES WHERE ((((ZQX IN (SELECT A FROM BKPF)))))", "table_not_allowed"),
    ("WITH x AS (SELECT * FROM BKPF) SELECT * FROM x WHERE ZQX = 1", "table_not_allowed"),
    ("WITH x AS (SELECT * FROM M_LOCKS) SELECT * FROM x, BKPF WHERE ZQX = 1", "table_not_allowed"),
    # a CTE cannot reach itself or a later CTE: that name is the real table
    ("WITH BKPF AS (SELECT * FROM BKPF) SELECT * FROM BKPF WHERE ZQX = 1", "table_not_allowed"),
    ("WITH a AS (SELECT * FROM b), b AS (SELECT * FROM M_X) SELECT ZQX FROM a",
     "table_not_allowed"),
    ('WITH x AS (SELECT * FROM M_LOCKS) SELECT * FROM "x" WHERE ZQX = 1', "table_not_allowed"),
    ("SELECT * FROM M_SERVICES s JOIN BKPF b ON s.ZQX = b.A", "table_not_allowed"),
    ("SELECT * FROM M_SERVICES s LEFT OUTER JOIN BKPF b ON s.ZQX = b.A", "table_not_allowed"),
    ("SELECT * FROM M_SERVICES s JOIN (SELECT * FROM BKPF) b ON s.ZQX = b.A", "table_not_allowed"),
    ("SELECT * FROM M_SERVICES s JOIN M_VOLUMES v ON s.ZQX = v.A, BKPF", "table_not_allowed"),
    ("SELECT * FROM M_SERVICES s CROSS APPLY BKPF ZQX", "table_not_allowed"),
    ("SELECT * FROM M_X, (SELECT * FROM M_Y) y, BKPF ZQX", "table_not_allowed"),
    ("SELECT * FROM M_X a JOIN M_Y b ON a.x = b.x JOIN BKPF ZQX ON 1 = 1", "table_not_allowed"),
    ("SELECT * FROM M_X WHERE ZQX = ANY (SELECT a FROM BKPF)", "table_not_allowed"),
    ("SELECT'ZQX'FROM BKPF", "table_not_allowed"),
    ("SELECT * FROM \"_SYS_BIC\".\"pkg/ZQX\" ('PLACEHOLDER' = ('$$a$$', 'b'))",
     "table_not_allowed"),
    ("SELECT * FROM M_X WHERE ZQX = 1 ", "unreadable"),
    ('SEL"ECT" * FROM M_X WHERE ZQX = 1', "select_only"),
    ("SELECT EXTRACT(YEAR FROM (SELECT MAX(ZQX) FROM BKPF)) FROM M_SERVICES", "table_not_allowed"),
    ("SELECT SAPHANADB.ZQX(1) FROM M_SERVICES", "table_not_allowed"),
    ('SELECT "ZQX"(1) FROM M_SERVICES', "table_not_allowed"),
    ("SELECT A.B.ZQX(1) FROM M_SERVICES", "table_not_allowed"),
    ("SELECT 1 FROM DUMMY WHERE ZQX = 1", "table_not_allowed"),
    ("SELECT ZQX.NEXTVAL FROM M_SERVICES", "select_only"),
    ("SELECT * FROM M_SERVICES WITH HINT (ZQX)", "select_only"),
    ("SELECT * FROM M_SERVICES WHERE A = (WITH ZQX AS (SELECT 1 FROM M_X) SELECT 1 FROM ZQX)",
     "select_only"),
    ("SELECT * FROM M_SERVICES WHERE ZQX = (SELECT 1 FROM M_X) - (DELETE FROM BKPF)",
     "select_only"),
    # reading the statement
    ("SELECT * FROM M_SERVICES WHERE HOST = 'ZQX", "unreadable"),
    ('SELECT * FROM "ZQX WHERE 1 = 1', "unreadable"),
    ("SELECT * FROM M_SERVICES WHERE X = x'ZQ'", "unreadable"),
    ("SELECT * FROM M_SERVICES /* ZQX", "unreadable"),
    ("SELECT * FROM M_SERVICES WHERE (ZQX = 1", "unreadable"),
    ("SELECT * FROM M_SERVICES WHERE ZQX = 1)", "unreadable"),
    ("SELECT * FROM M_SERVICES WHERE ZQX = 1 ", "unreadable"),
    ("SELECT * FROM М_SERVICES WHERE ZQX = 1", "unreadable"),
    ('SELECT * FROM "М_SERVICES" WHERE ZQX = 1', "table_not_allowed"),
    ("SELECT * FROM M_SERVICES WHERE ZQX = 1\x00", "unreadable"),
    ("SELECT * FROM M_SERVICES WHERE ZQX = '‮'", "unreadable"),
    ("SELECT * FROM #ZQX", "unreadable"),
    ("SELECT * FROM M_SERVICES WHERE ZQX = `a`", "unreadable"),
    ("SELECT * FROM M_SERVICES WHERE 1FROM ZQX", "unreadable"),
    ('SELECT * FROM "" WHERE ZQX = 1', "unreadable"),
]


@pytest.mark.parametrize("sql", ACCEPTED)
def test_accepted(sql: str) -> None:
    check_select(sql)


@pytest.mark.parametrize(("sql", "rule"), REFUSED)
def test_refused_names_the_rule_never_the_sql(sql: str, rule: str) -> None:
    with pytest.raises(SelectRefused) as caught:
        check_select(sql)
    assert caught.value.rule == rule
    message = str(caught.value)
    assert rule in message
    assert "ZQX" not in message
    assert "BKPF" not in message
    assert all("ZQX" not in str(arg) for arg in caught.value.args)


def test_refusal_is_a_value_error() -> None:
    with pytest.raises(ValueError):
        check_select("DROP TABLE ZQX")


def test_length_limit() -> None:
    base = "SELECT * FROM M_SERVICES WHERE HOST = '"
    at_limit = base + "a" * (MAX_SQL_CHARS - len(base) - 1) + "'"
    assert len(at_limit) == MAX_SQL_CHARS == 4000
    check_select(at_limit)
    with pytest.raises(SelectRefused) as caught:
        check_select(at_limit[:-1] + "ZQX'")
    assert caught.value.rule == "too_long"
    assert "ZQX" not in str(caught.value)


@pytest.mark.parametrize("value", [None, 1, b"SELECT * FROM M_SERVICES", ["SELECT"]])
def test_non_string_is_refused(value: object) -> None:
    with pytest.raises(SelectRefused) as caught:
        check_select(value)  # type: ignore[arg-type]
    assert caught.value.rule == "unreadable"
