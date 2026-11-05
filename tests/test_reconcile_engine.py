"""The reconciliation compare engine (``agents.reconcile.engine``).

The engine is the deterministic half of a reconciliation: given a compiled
spec, already-built code-table lookups and already-read source rows, it decides
per CSV cell whether the migrated value is what the rule says. No I/O happens
here, so every rule kind is tested with synthetic rows and inline lookups.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from agents.reconcile.engine import (
    Comparer,
    CsvError,
    EngineResult,
    Mismatch,
    NotChecked,
    iter_csv,
    normalise_key,
)
from agents.reconcile.spec import KeySpec, ReconcileSpec

KEY = KeySpec(csv_column="Object", ecc_field="OBJNR", length=18)
LOOKUP = {"sheet": "Codes", "header_row": 1, "keys": [{"letter": "B"}], "value": {"letter": "C"}}


@dataclass(frozen=True)
class Lookup:
    """Same shape as the workbook lookup (``entries`` + ``get``), built inline."""

    entries: dict[tuple[str, ...], str]
    duplicates: int = 0

    def get(self, key: tuple[str, ...]) -> str | None:
        return self.entries.get(key)


def _spec(columns: dict[str, Any], **over: Any) -> ReconcileSpec:
    raw: dict[str, Any] = {
        "table": "ZTAB",
        "wave": "W1",
        "key": KEY.model_dump(),
        "columns": columns,
    }
    raw.update(over)
    return ReconcileSpec.model_validate(raw)


def _one(
    rule: dict[str, Any],
    csv_value: str,
    ecc: dict[str, Any],
    lookups: dict[tuple[str, int], Lookup] | None = None,
    **spec_over: Any,
) -> tuple[EngineResult, Comparer]:
    comparer = Comparer(_spec({"Col": rule}, **spec_over), lookups or {})
    comparer.compare("K1", 2, {"Object": "K1", "Col": csv_value}, {"OBJNR": "K1", **ecc})
    return comparer.result(), comparer


def _matched(rule: dict[str, Any], csv_value: str, ecc: dict[str, Any], **kw: Any) -> bool:
    result, _ = _one(rule, csv_value, ecc, **kw)
    stats = result.stats["Col"]
    assert stats.checked == 1 and stats.matched + stats.mismatched == 1
    return stats.matched == 1


# --- iter_csv -----------------------------------------------------------------


def test_iter_csv_bom_quotes_embedded_comma_and_newline() -> None:
    data = '﻿Object,Text\r\n1,"a, b"\r\n2,"line1\nline2"\r\n'.encode()
    header, rows = iter_csv(data)
    assert header == ["Object", "Text"]
    assert list(rows) == [
        (2, {"Object": "1", "Text": "a, b"}),
        (3, {"Object": "2", "Text": "line1\nline2"}),
    ]


def test_iter_csv_duplicate_header() -> None:
    with pytest.raises(CsvError) as exc:
        iter_csv(b"A,B,A\n1,2,3\n")
    assert exc.value.code == "duplicate_header"


def test_iter_csv_too_many_columns() -> None:
    header = ",".join(f"C{i}" for i in range(301))
    with pytest.raises(CsvError) as exc:
        iter_csv(header.encode() + b"\n")
    assert exc.value.code == "too_many_columns"


def test_iter_csv_not_utf8() -> None:
    with pytest.raises(CsvError) as exc:
        iter_csv("Object,Text\n1,café\n".encode("latin-1"))
    assert exc.value.code == "not_utf8"


def test_iter_csv_empty() -> None:
    with pytest.raises(CsvError) as exc:
        iter_csv(b"")
    assert exc.value.code == "no_header"


def test_iter_csv_error_text_is_fixed() -> None:
    with pytest.raises(CsvError) as exc:
        iter_csv(b"secret,secret\n")
    assert "secret" not in str(exc.value)


def test_iter_csv_malformed_rows_counted_and_skipped() -> None:
    header, rows = iter_csv(b"A,B\n1,2\n1,2,3\n4\n\n5,6\n")
    assert header == ["A", "B"]
    assert [n for n, _ in rows] == [2, 6]
    assert rows.malformed == 2


# --- normalise_key -------------------------------------------------------------


def test_normalise_key_pads_digits() -> None:
    assert normalise_key("12345", KEY) == "000000000000012345"
    assert normalise_key(" 12345 ", KEY) == "000000000000012345"


def test_normalise_key_no_padding_when_off() -> None:
    key = KeySpec(csv_column="Object", ecc_field="OBJNR", length=18, pad_numeric=False)
    assert normalise_key("12345", key) == "12345"


@pytest.mark.parametrize("raw", [None, "", "   ", "AB'C", "A;B", "Aé", "X" * 19])
def test_normalise_key_refuses(raw: str | None) -> None:
    assert normalise_key(raw, KEY) is None


def test_normalise_key_allows_safe_punctuation() -> None:
    assert normalise_key("AB-1_2.3/4 5", KEY) == "AB-1_2.3/4 5"


def test_key_of_counts_invalid_and_duplicate_keys() -> None:
    comparer = Comparer(_spec({"Col": {"kind": "constant", "value": "X"}}), {})
    assert comparer.key_of({"Object": "1"}) == "000000000000000001"
    assert comparer.key_of({"Object": ""}) is None
    assert comparer.key_of({}) is None
    assert comparer.key_of({"Object": "01"}) == "000000000000000001"
    result = comparer.result()
    assert result.rows_total == 4
    assert result.invalid_keys == 2
    assert result.duplicate_keys == 1


# --- direct --------------------------------------------------------------------


def test_direct_match_and_mismatch() -> None:
    rule = {"kind": "direct", "source": "MATNR"}
    assert _matched(rule, "M1", {"MATNR": "M1"})
    result, _ = _one(rule, "M2", {"MATNR": "M1"})
    assert result.mismatches == [
        Mismatch(key="K1", column="Col", kind="direct", source="M1", expected="M1",
                 actual="M2", reason="value_differs")
    ]


def test_direct_alpha() -> None:
    rule = {"kind": "direct", "source": "MATNR", "alpha": True}
    assert _matched(rule, "123", {"MATNR": "000123"})
    assert not _matched({"kind": "direct", "source": "MATNR"}, "123", {"MATNR": "000123"})


def test_direct_numeric() -> None:
    rule = {"kind": "direct", "source": "MENGE", "numeric": True}
    assert _matched(rule, "1.000", {"MENGE": "1"})
    assert not _matched(rule, "1.5", {"MENGE": "1"})


def test_direct_non_string_ecc_value() -> None:
    assert _matched({"kind": "direct", "source": "MENGE"}, "5", {"MENGE": 5})


def test_blank_equals_single_space_with_trim() -> None:
    rule = {"kind": "direct", "source": "MATNR"}
    assert _matched(rule, "", {"MATNR": " "})
    assert _matched(rule, " ", {"MATNR": ""})
    assert _matched(rule, " ", {"MATNR": None})
    assert not _matched(rule, "", {"MATNR": " "}, trim=False)


# --- constant ------------------------------------------------------------------


def test_constant() -> None:
    rule = {"kind": "constant", "value": "EA"}
    assert _matched(rule, "EA", {})
    result, _ = _one(rule, "PC", {})
    assert result.mismatches[0].expected == "EA"
    assert result.mismatches[0].source == ""


# --- value_map -----------------------------------------------------------------


def _vm(**over: Any) -> dict[str, Any]:
    rule: dict[str, Any] = {"kind": "value_map", "sources": ["MTART"], "lookups": [LOOKUP]}
    rule.update(over)
    return rule


def test_value_map_hit_in_first_lookup() -> None:
    lookups = {("Col", 0): Lookup({("A",): "NEW_A"})}
    assert _matched(_vm(), "NEW_A", {"MTART": "A"}, lookups=lookups)
    result, _ = _one(_vm(), "OTHER", {"MTART": "A"}, lookups=lookups)
    m = result.mismatches[0]
    assert (m.expected, m.actual, m.reason, m.source) == ("NEW_A", "OTHER", "value_differs", "A")


def test_value_map_fallback_to_second_lookup() -> None:
    rule = _vm(lookups=[LOOKUP, {**LOOKUP, "sheet": "Groups"}])
    lookups = {("Col", 0): Lookup({("X",): "NX"}), ("Col", 1): Lookup({("A",): "NEW_A"})}
    assert _matched(rule, "NEW_A", {"MTART": "A"}, lookups=lookups)


def test_value_map_first_lookup_wins() -> None:
    rule = _vm(lookups=[LOOKUP, {**LOOKUP, "sheet": "Groups"}])
    lookups = {("Col", 0): Lookup({("A",): "FIRST"}), ("Col", 1): Lookup({("A",): "SECOND"})}
    assert _matched(rule, "FIRST", {"MTART": "A"}, lookups=lookups)


def test_value_map_lookup_key_is_trimmed() -> None:
    lookups = {("Col", 0): Lookup({("1",): "ONE"})}
    assert _matched(_vm(), "ONE", {"MTART": " 1 "}, lookups=lookups)


def test_value_map_no_alpha_keeps_leading_zero_codes_distinct() -> None:
    lookups = {("Col", 0): Lookup({("0010",): "PADDED", ("10",): "PLAIN"})}
    assert _matched(_vm(), "PADDED", {"MTART": "0010"}, lookups=lookups)
    assert _matched(_vm(), "PLAIN", {"MTART": "10"}, lookups=lookups)
    result, _ = _one(_vm(), "PLAIN", {"MTART": "010"}, lookups=lookups)
    assert result.mismatches[0].reason == "no_xref_entry"


def test_value_map_alpha_is_opt_in_and_both_directions() -> None:
    table = {("Col", 0): Lookup({("1",): "ONE"})}
    assert not _matched(_vm(), "ONE", {"MTART": "0001"}, lookups=table)
    # SAP NUMC "0001" against a code-table cell Excel stored as the number 1
    assert _matched(_vm(alpha=True), "ONE", {"MTART": "0001"}, lookups=table)
    padded = {("Col", 0): Lookup({("0010",): "TEN"})}
    assert _matched(_vm(alpha=True), "TEN", {"MTART": "10"}, lookups=padded)


def test_value_map_alpha_exact_still_wins() -> None:
    lookups = {("Col", 0): Lookup({("0010",): "PADDED", ("10",): "PLAIN"})}
    assert _matched(_vm(alpha=True), "PADDED", {"MTART": "0010"}, lookups=lookups)
    assert _matched(_vm(alpha=True), "PLAIN", {"MTART": "10"}, lookups=lookups)


def test_value_map_exact_in_later_lookup_beats_alpha_in_earlier() -> None:
    rule = _vm(alpha=True, lookups=[LOOKUP, {**LOOKUP, "sheet": "Groups"}])
    lookups = {("Col", 0): Lookup({("10",): "ALPHA_HIT"}), ("Col", 1): Lookup({("0010",): "EXACT"})}
    assert _matched(rule, "EXACT", {"MTART": "0010"}, lookups=lookups)


def test_value_map_miss_report() -> None:
    lookups = {("Col", 0): Lookup({("A",): "NEW_A"})}
    result, _ = _one(_vm(), "ANY", {"MTART": "Z"}, lookups=lookups)
    assert result.stats["Col"].mismatched == 1
    m = result.mismatches[0]
    assert (m.reason, m.expected, m.source) == ("no_xref_entry", "", "Z")


def test_value_map_miss_default() -> None:
    lookups = {("Col", 0): Lookup({("A",): "NEW_A"})}
    rule = _vm(on_miss="default", default="DEF")
    assert _matched(rule, "DEF", {"MTART": "Z"}, lookups=lookups)
    assert not _matched(rule, "Z", {"MTART": "Z"}, lookups=lookups)


def test_value_map_miss_source() -> None:
    lookups = {("Col", 0): Lookup({("A",): "NEW_A"})}
    rule = _vm(on_miss="source")
    assert _matched(rule, "Z", {"MTART": "Z"}, lookups=lookups)


def test_value_map_miss_source_uses_first_non_blank_source() -> None:
    two = {**LOOKUP, "keys": [{"letter": "A"}, {"letter": "B"}]}
    rule = _vm(sources=["MTART", "MATKL"], lookups=[two], on_miss="source")
    lookups = {("Col", 0): Lookup({("A", "G1"): "AG"})}
    assert _matched(rule, "G2", {"MTART": " ", "MATKL": "G2"}, lookups=lookups)
    assert _matched(rule, "B", {"MTART": "B", "MATKL": "G2"}, lookups=lookups)


def test_value_map_all_sources_blank_expects_blank() -> None:
    lookups = {("Col", 0): Lookup({("A",): "NEW_A"})}
    assert _matched(_vm(), "", {"MTART": " "}, lookups=lookups)
    result, _ = _one(_vm(), "X", {"MTART": ""}, lookups=lookups)
    assert result.mismatches[0].reason == "value_differs"
    assert result.mismatches[0].expected == ""


def test_value_map_two_sources() -> None:
    two = {**LOOKUP, "keys": [{"letter": "A"}, {"letter": "B"}]}
    rule = _vm(sources=["MTART", "MATKL"], lookups=[two])
    lookups = {("Col", 0): Lookup({("A", "G1"): "AG"})}
    assert _matched(rule, "AG", {"MTART": "A", "MATKL": "G1"}, lookups=lookups)
    result, _ = _one(rule, "AG", {"MTART": "A", "MATKL": "G2"}, lookups=lookups)
    assert result.mismatches[0].source == "A | G2"


def test_value_map_without_lookup_is_refused() -> None:
    with pytest.raises(ValueError):
        Comparer(_spec({"Col": _vm()}), {})


# --- expression ----------------------------------------------------------------


def _expr(cases: list[dict[str, Any]], otherwise: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "expression", "cases": cases, "otherwise": otherwise}


def _case(when: list[dict[str, Any]], then: dict[str, Any]) -> dict[str, Any]:
    return {"when": when, "then": then}


def test_expression_equals_and_otherwise() -> None:
    rule = _expr([_case([{"field": "F1", "op": "equals", "values": ["A"]}], {"const": "X"})],
                 {"const": "Y"})
    assert _matched(rule, "X", {"F1": "A"})
    assert _matched(rule, "Y", {"F1": "B"})
    assert not _matched(rule, "X", {"F1": "B"})


def test_expression_in() -> None:
    rule = _expr([_case([{"field": "F1", "op": "in", "values": ["A", "B"]}], {"const": "X"})],
                 {"const": "Y"})
    assert _matched(rule, "X", {"F1": "B"})
    assert _matched(rule, "Y", {"F1": "C"})


def test_expression_blank_and_not_blank() -> None:
    rule = _expr(
        [
            _case([{"field": "F1", "op": "blank"}], {"const": "EMPTY"}),
            _case([{"field": "F1", "op": "not_blank"}], {"const": "FULL"}),
        ],
        {"const": "NEVER"},
    )
    assert _matched(rule, "EMPTY", {"F1": " "})
    assert _matched(rule, "EMPTY", {"F1": None})
    assert _matched(rule, "FULL", {"F1": "v"})


def test_expression_conditions_are_anded() -> None:
    rule = _expr(
        [_case([{"field": "F1", "op": "equals", "values": ["A"]},
                {"field": "F2", "op": "equals", "values": ["B"]}], {"const": "AB"})],
        {"const": "NO"},
    )
    assert _matched(rule, "AB", {"F1": "A", "F2": "B"})
    assert _matched(rule, "NO", {"F1": "A", "F2": "C"})


def test_expression_first_case_wins() -> None:
    rule = _expr(
        [
            _case([{"field": "F1", "op": "not_blank"}], {"const": "FIRST"}),
            _case([{"field": "F1", "op": "equals", "values": ["A"]}], {"const": "SECOND"}),
        ],
        {"const": "NO"},
    )
    assert _matched(rule, "FIRST", {"F1": "A"})


def test_expression_value_field() -> None:
    rule = _expr([_case([{"field": "F1", "op": "equals", "values": ["A"]}], {"field": "F2"})],
                 {"field": "F3"})
    assert _matched(rule, "two", {"F1": "A", "F2": "two", "F3": "three"})
    assert _matched(rule, "three", {"F1": "B", "F2": "two", "F3": "three"})
    result, _ = _one(rule, "nope", {"F1": "B", "F2": "two", "F3": "three"})
    m = result.mismatches[0]
    assert (m.kind, m.source, m.expected) == ("expression", "three", "three")


def _numc_expr(**over: Any) -> dict[str, Any]:
    rule = _expr([_case([{"field": "F1", "op": "equals", "values": ["1"]}], {"const": "X"}),
                  _case([{"field": "F1", "op": "in", "values": ["2", "3"]}], {"const": "Z"})],
                 {"const": "Y"})
    rule.update(over)
    return rule


def test_expression_conditions_honour_alpha() -> None:
    assert _matched(_numc_expr(), "Y", {"F1": "0001"})
    assert _matched(_numc_expr(alpha=True), "X", {"F1": "0001"})
    assert _matched(_numc_expr(alpha=True), "Z", {"F1": "003"})


def test_expression_conditions_honour_numeric() -> None:
    assert _matched(_numc_expr(numeric=True), "X", {"F1": "1.00"})
    assert _matched(_numc_expr(), "Y", {"F1": "1.00"})


def test_expression_equals_is_trimmed() -> None:
    rule = _expr([_case([{"field": "F1", "op": "equals", "values": ["A"]}], {"const": "X"})],
                 {"const": "Y"})
    assert _matched(rule, "X", {"F1": " A "})


# --- not_needed / unresolved ----------------------------------------------------


def test_not_needed() -> None:
    rule = {"kind": "not_needed"}
    assert _matched(rule, "", {})
    assert _matched(rule, " ", {})
    result, _ = _one(rule, "x", {})
    m = result.mismatches[0]
    assert (m.reason, m.expected, m.actual) == ("must_be_blank", "", "x")


def test_unresolved_is_not_checked_never_mismatch() -> None:
    columns = {
        "U1": {"kind": "unresolved", "text": "depends on another table", "needs_table": "ZOTHER"},
        "U2": {"kind": "unresolved", "text": "unclear"},
    }
    comparer = Comparer(_spec(columns), {})
    for n in range(3):
        comparer.compare(f"K{n}", n + 2, {"U1": "a", "U2": "b"}, {"OBJNR": f"K{n}"})
    result = comparer.result()
    assert result.mismatches == []
    assert result.stats["U1"].not_checked == 3
    assert result.stats["U1"].checked == 0
    assert result.not_checked == [
        NotChecked(column="U1", kind="unresolved", reason="needs_other_table",
                   needs_table="ZOTHER"),
        NotChecked(column="U2", kind="unresolved", reason="unresolved", needs_table=None),
    ]


# --- rows, missing, duplicates, caps ----------------------------------------------


def test_missing_in_ecc() -> None:
    comparer = Comparer(_spec({"Col": {"kind": "constant", "value": "X"}}), {})
    comparer.compare("K9", 7, {"Col": "Y"}, None)
    result = comparer.result()
    assert result.missing_in_ecc == [("K9", 7)]
    assert result.rows_compared == 0
    assert result.stats["Col"].checked == 0
    assert result.mismatches == []


def test_duplicate_csv_keys_both_compared() -> None:
    comparer = Comparer(_spec({"Col": {"kind": "direct", "source": "MATNR"}}), {})
    ecc = {"OBJNR": "000000000000000001", "MATNR": "M"}
    for row_no, value in ((2, "M"), (3, "N")):
        row = {"Object": "1", "Col": value}
        key = comparer.key_of(row)
        assert key is not None
        comparer.compare(key, row_no, row, ecc)
    result = comparer.result()
    assert result.duplicate_keys == 1
    assert result.rows_compared == 2
    assert (result.stats["Col"].matched, result.stats["Col"].mismatched) == (1, 1)


def test_mismatch_cap_keeps_counters_exact() -> None:
    comparer = Comparer(_spec({"Col": {"kind": "constant", "value": "X"}}), {}, mismatch_cap=3)
    for n in range(5):
        comparer.compare(f"K{n}", n + 2, {"Col": "Y"}, {"OBJNR": f"K{n}"})
    comparer.compare("K5", 7, {"Col": "X"}, {"OBJNR": "K5"})
    result = comparer.result()
    assert len(result.mismatches) == 3
    assert result.mismatches_dropped == 2
    assert result.stats["Col"].mismatched == 5
    assert result.stats["Col"].matched == 1
    assert result.mismatch_count == 5


def test_missing_in_ecc_cap() -> None:
    comparer = Comparer(_spec({"Col": {"kind": "constant", "value": "X"}}), {}, mismatch_cap=2)
    for n in range(4):
        comparer.compare(f"K{n}", n + 2, {"Col": "X"}, None)
    result = comparer.result()
    assert len(result.missing_in_ecc) == 2
    assert result.missing_in_ecc_dropped == 2
    assert result.missing_count == 4


def test_result_takes_malformed_rows() -> None:
    comparer = Comparer(_spec({"Col": {"kind": "constant", "value": "X"}}), {})
    assert comparer.result(malformed_rows=3).malformed_rows == 3


@pytest.mark.parametrize("rule", [{"kind": "not_needed"},
                                  {"kind": "unresolved", "text": "unclear"}])
def test_csv_column_missing_from_row_is_refused(rule: dict[str, Any]) -> None:
    comparer = Comparer(_spec({"Col": rule}), {})
    with pytest.raises(CsvError) as exc:
        comparer.compare("K1", 2, {"Object": "K1"}, {"OBJNR": "K1"})
    assert exc.value.code == "column_missing"
    assert comparer.result().stats["Col"].checked == 0


def test_key_of_contract_once_per_row() -> None:
    """Each call is one data row: calling twice for the same row counts a duplicate."""
    comparer = Comparer(_spec({"Col": {"kind": "not_needed"}}), {}, mismatch_cap=2)
    row = {"Object": "1"}
    comparer.key_of(row, row_no=2)
    comparer.key_of(row, row_no=2)
    for row_no in (3, 4, 5):
        assert comparer.key_of({"Object": "bad'key"}, row_no=row_no) is None
    result = comparer.result()
    assert (result.rows_total, result.duplicate_keys, result.invalid_keys) == (5, 1, 3)
    assert result.invalid_key_rows == [3, 4]  # capped like the other lists


def test_iter_csv_not_utf8_late_in_file() -> None:
    body = b"".join(b"%d,ok\n" % n for n in range(5000))
    header, rows = iter_csv(b"Object,Text\n" + body + b"9,caf\xe9\n")
    assert header == ["Object", "Text"]
    with pytest.raises(CsvError) as exc:
        list(rows)
    assert exc.value.code == "not_utf8"


def test_normalise_key_full_match_only() -> None:
    assert normalise_key("AB\nCD", KEY) is None


def test_missing_columns() -> None:
    comparer = Comparer(_spec({"Col": {"kind": "not_needed"}, "Two": {"kind": "not_needed"}}), {})
    assert comparer.missing_columns(["Object", "Col"]) == ["Two"]
    assert comparer.missing_columns(["Col", "Two"]) == ["Object"]


def test_needed_fields_equals_spec() -> None:
    spec = _spec({
        "A": {"kind": "direct", "source": "MATNR"},
        "B": _vm(),
        "C": _expr([_case([{"field": "F1", "op": "blank"}], {"field": "F2"})], {"const": "x"}),
    })
    comparer = Comparer(spec, {("B", 0): Lookup({})})
    assert comparer.needed_fields() == spec.ecc_fields()


def test_stats_kind_per_column() -> None:
    spec = _spec({"A": {"kind": "direct", "source": "MATNR"}, "B": {"kind": "not_needed"}})
    stats = Comparer(spec, {}).result().stats
    assert {c: s.kind for c, s in stats.items()} == {"A": "direct", "B": "not_needed"}


def test_end_to_end_with_iter_csv() -> None:
    spec = _spec({"Material": {"kind": "direct", "source": "MATNR"},
                  "Unit": {"kind": "constant", "value": "EA"}})
    data = b"Object,Material,Unit\n1,M1,EA\n2,M2,PC\n,M3,EA\n3,M3\n4,M4,EA\n"
    ecc = {
        "000000000000000001": {"OBJNR": "000000000000000001", "MATNR": "M1"},
        "000000000000000002": {"OBJNR": "000000000000000002", "MATNR": "M2"},
    }
    comparer = Comparer(spec, {})
    _, rows = iter_csv(data)
    for row_no, row in rows:
        key = comparer.key_of(row)
        if key is not None:
            comparer.compare(key, row_no, row, ecc.get(key))
    result = comparer.result(malformed_rows=rows.malformed)
    assert (result.rows_total, result.rows_compared, result.malformed_rows) == (4, 2, 1)
    assert result.invalid_keys == 1
    assert result.missing_in_ecc == [("000000000000000004", 6)]
    assert [(m.column, m.actual) for m in result.mismatches] == [("Unit", "PC")]
