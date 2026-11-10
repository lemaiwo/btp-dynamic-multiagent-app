"""Literal masking of statement texts (``agents/hana/masking.py``).

Monitoring views such as ``M_EXPENSIVE_STATEMENTS`` hold the SQL other
sessions ran, with the values they filtered on; only the shape may leave.
"""

from __future__ import annotations

from agents.hana.masking import mask_statement


def test_literals_are_masked() -> None:
    text = "SELECT * FROM T WHERE A = 'x''y' AND B = 42 AND C = 1.5e3 AND D = x'0A'"
    assert mask_statement(text) == "SELECT * FROM T WHERE A = ? AND B = ? AND C = ? AND D = ?"


def test_identifiers_are_kept() -> None:
    text = 'SELECT "My Col", T1.A FROM "SAPABAP1"."ZTAB" T1 WHERE T1.N = N\'secret\''
    assert mask_statement(text) == 'SELECT "My Col", T1.A FROM "SAPABAP1"."ZTAB" T1 WHERE T1.N = ?'


def test_parameters_are_kept() -> None:
    assert mask_statement("SELECT * FROM T WHERE A = ? AND B = :b") == (
        "SELECT * FROM T WHERE A = ? AND B = :b"
    )


def test_cut_at_the_limit() -> None:
    text = "SELECT " + ", ".join(f"COLUMN_{i}" for i in range(200)) + " FROM T"
    masked = mask_statement(text)
    assert len(masked) == 300
    assert masked.endswith("…")
    assert masked.startswith("SELECT COLUMN_0, COLUMN_1")
    assert len(mask_statement(text, limit=50)) == 50


def test_short_text_is_not_cut() -> None:
    assert mask_statement("SELECT 1 FROM DUMMY") == "SELECT ? FROM DUMMY"


def test_unterminated_literal_is_masked_to_the_end() -> None:
    assert mask_statement("SELECT * FROM T WHERE A = 'secret and more") == (
        "SELECT * FROM T WHERE A = ?"
    )
    assert mask_statement('SELECT * FROM T WHERE A = "secret and more') == (
        "SELECT * FROM T WHERE A = ?"
    )


def test_comments_are_dropped() -> None:
    text = "SELECT /* user 'secret' */ A FROM T -- customer 4711\nWHERE B = 1"
    assert mask_statement(text) == "SELECT A FROM T WHERE B = ?"
    assert mask_statement("SELECT A FROM T /* unterminated 'secret'") == "SELECT A FROM T"


def test_password_operand_is_masked() -> None:
    assert mask_statement("ALTER USER BOB PASSWORD Secret123 NO FORCE_FIRST_PASSWORD_CHANGE") == (
        "ALTER USER BOB PASSWORD ? NO FORCE_FIRST_PASSWORD_CHANGE"
    )
    assert mask_statement('CREATE USER BOB PASSWORD "Secret123"') == "CREATE USER BOB PASSWORD ?"


def test_control_characters_are_dropped() -> None:
    text = "SELECT\tA,\r\nB\x00 FROM\x1b T‮ WHERE C = 'z\x07'"
    assert mask_statement(text) == "SELECT A, B FROM T WHERE C = ?"


def test_empty_and_none() -> None:
    assert mask_statement(None) == ""
    assert mask_statement("") == ""
    assert mask_statement(42) == ""  # type: ignore[arg-type]


def test_odd_characters_never_raise() -> None:
    assert mask_statement("SELECT `a` FROM #T WHERE М = 'x'") == "SELECT `a` FROM #T WHERE М = ?"
