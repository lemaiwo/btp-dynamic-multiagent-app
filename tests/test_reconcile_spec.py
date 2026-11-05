"""The reconciliation spec (``agents.reconcile.spec``) and value helpers.

The spec is what a model compiles from mapping rules; code then trusts it to
build ABAP SQL and compare rows, so every identifier, size and shape that could
reach a query or blow up a run is refused here, before anything runs.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from agents.reconcile import BUILTIN_RECONCILE_URL, values
from agents.reconcile.spec import (
    MAX_COLUMNS,
    MAX_SPEC_BYTES,
    ColumnRef,
    Condition,
    ReconcileSpec,
    SpecError,
    Value,
)


def _lookup(**over: Any) -> dict[str, Any]:
    base = {
        "sheet": "Codes",
        "header_row": 2,
        "keys": [{"letter": "a"}],
        "value": {"label": "Target value"},
    }
    base.update(over)
    return base


def _spec(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "table": "ztab",
        "wave": "W1",
        "key": {"csv_column": "Object", "ecc_field": "objnr", "length": 18},
        "columns": {
            "Direct col": {"kind": "direct", "source": "zfld"},
            "Const col": {"kind": "constant", "value": "X", "note": "always X"},
            "Mapped col": {
                "kind": "value_map",
                "sources": ["ccode"],
                "lookups": [_lookup()],
                "on_miss": "default",
                "default": "99",
            },
            "Expr col": {
                "kind": "expression",
                "cases": [
                    {
                        "when": [{"field": "status", "op": "equals", "values": ["A"]}],
                        "then": {"field": "afield"},
                    },
                    {
                        "when": [{"field": "bstat", "op": "blank"}],
                        "then": {"const": "B"},
                    },
                ],
                "otherwise": {"field": "zfld"},
            },
            "Unused col": {"kind": "not_needed"},
            "Open col": {
                "kind": "unresolved",
                "text": "needs another table",
                "needs_table": "ztab2",
            },
        },
    }
    base.update(over)
    return base


def test_package_names_the_builtin_url() -> None:
    assert BUILTIN_RECONCILE_URL == "builtin:reconcile"


def test_full_spec_parses_and_upper_cases_identifiers() -> None:
    spec = ReconcileSpec.model_validate(_spec())
    assert spec.version == 1
    assert spec.table == "ZTAB"
    assert spec.key.ecc_field == "OBJNR"
    assert spec.key.pad_numeric is True
    assert spec.trim is True
    kinds = [rule.kind for rule in spec.columns.values()]
    assert kinds == ["direct", "constant", "value_map", "expression", "not_needed", "unresolved"]
    assert spec.columns["Direct col"].source == "ZFLD"
    assert spec.columns["Mapped col"].lookups[0].keys[0].letter == "A"
    assert spec.columns["Open col"].needs_table == "ZTAB2"


def test_ecc_fields_key_first_then_sorted_union() -> None:
    spec = ReconcileSpec.model_validate(_spec())
    assert spec.ecc_fields() == ["OBJNR", "AFIELD", "BSTAT", "CCODE", "STATUS", "ZFLD"]


def test_ecc_fields_does_not_repeat_the_key() -> None:
    spec = ReconcileSpec.model_validate(
        _spec(columns={"K": {"kind": "direct", "source": "objnr"}})
    )
    assert spec.ecc_fields() == ["OBJNR"]


def test_spec_is_frozen() -> None:
    spec = ReconcileSpec.model_validate(_spec())
    with pytest.raises(ValidationError):
        spec.table = "OTHER"  # type: ignore[misc]


def test_unknown_kind_refused() -> None:
    with pytest.raises(ValidationError):
        ReconcileSpec.model_validate(_spec(columns={"C": {"kind": "magic", "source": "A"}}))


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("key",),
        ("columns", "Direct col"),
        ("columns", "Mapped col", "lookups", 0),
        ("columns", "Mapped col", "lookups", 0, "value"),
        ("columns", "Expr col", "cases", 0),
        ("columns", "Expr col", "cases", 0, "when", 0),
        ("columns", "Expr col", "otherwise"),
    ],
)
def test_extra_key_anywhere_refused(path: tuple[Any, ...]) -> None:
    data = _spec()
    node: Any = data
    for step in path:
        node = node[step]
    node["surprise"] = "x"
    with pytest.raises(ValidationError):
        ReconcileSpec.model_validate(data)


BAD_IDENTS = ["MARA; DROP", "mara x", "A" * 31, "/ABC/X", "", "1ABC"]


@pytest.mark.parametrize("bad", BAD_IDENTS)
def test_bad_table_refused(bad: str) -> None:
    with pytest.raises(ValidationError):
        ReconcileSpec.model_validate(_spec(table=bad))


@pytest.mark.parametrize("bad", BAD_IDENTS)
@pytest.mark.parametrize(
    "rule",
    [
        lambda f: {"kind": "direct", "source": f},
        lambda f: {"kind": "value_map", "sources": [f], "lookups": [_lookup()]},
        lambda f: {
            "kind": "expression",
            "cases": [{"when": [{"field": f, "op": "not_blank"}], "then": {"const": "1"}}],
            "otherwise": {"const": ""},
        },
        lambda f: {"kind": "expression", "cases": [
            {"when": [{"field": "A", "op": "blank"}], "then": {"field": f}}],
            "otherwise": {"const": ""}},
        lambda f: {"kind": "unresolved", "text": "t", "needs_table": f},
    ],
)
def test_bad_field_refused(bad: str, rule: Any) -> None:
    with pytest.raises(ValidationError):
        ReconcileSpec.model_validate(_spec(columns={"C": rule(bad)}))


@pytest.mark.parametrize("bad", BAD_IDENTS)
def test_bad_key_field_refused(bad: str) -> None:
    with pytest.raises(ValidationError):
        ReconcileSpec.model_validate(
            _spec(key={"csv_column": "Object", "ecc_field": bad, "length": 18})
        )


def test_thirty_char_identifier_allowed() -> None:
    spec = ReconcileSpec.model_validate(_spec(table="A" * 30))
    assert spec.table == "A" * 30


def test_non_string_identifier_refused() -> None:
    with pytest.raises(ValidationError):
        ReconcileSpec.model_validate(_spec(table=12))


@pytest.mark.parametrize("data", [{}, {"letter": "B", "label": "Name"}])
def test_column_ref_needs_exactly_one(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ColumnRef.model_validate(data)


@pytest.mark.parametrize("bad", ["AAAA", "", "1", "A1"])
def test_column_ref_bad_letter(bad: str) -> None:
    with pytest.raises(ValidationError):
        ColumnRef.model_validate({"letter": bad})


def test_column_ref_letter_upper_cased() -> None:
    assert ColumnRef.model_validate({"letter": "ab"}).letter == "AB"
    assert ColumnRef.model_validate({"label": "Name"}).label == "Name"


def test_value_map_default_needs_default() -> None:
    rule = {"kind": "value_map", "sources": ["A"], "lookups": [_lookup()], "on_miss": "default"}
    with pytest.raises(ValidationError):
        ReconcileSpec.model_validate(_spec(columns={"C": rule}))


def test_value_map_keys_must_match_sources() -> None:
    rule = {
        "kind": "value_map",
        "sources": ["A"],
        "lookups": [_lookup(keys=[{"letter": "A"}, {"letter": "B"}])],
    }
    with pytest.raises(ValidationError):
        ReconcileSpec.model_validate(_spec(columns={"C": rule}))


def test_value_map_two_keys_two_sources_ok() -> None:
    rule = {
        "kind": "value_map",
        "sources": ["A", "B"],
        "lookups": [_lookup(keys=[{"letter": "A"}, {"letter": "B"}])],
    }
    spec = ReconcileSpec.model_validate(_spec(columns={"C": rule}))
    assert spec.columns["C"].on_miss == "report"


@pytest.mark.parametrize(
    ("op", "vals"),
    [
        ("equals", []),
        ("equals", ["A", "B"]),
        ("in", []),
        ("blank", ["A"]),
        ("not_blank", ["A"]),
    ],
)
def test_condition_value_counts(op: str, vals: list[str]) -> None:
    with pytest.raises(ValidationError):
        Condition.model_validate({"field": "A", "op": op, "values": vals})


def test_condition_ok_shapes() -> None:
    assert Condition.model_validate({"field": "a", "op": "in", "values": ["1", "2"]}).field == "A"
    assert Condition.model_validate({"field": "A", "op": "blank"}).values == []


@pytest.mark.parametrize("data", [{"const": "A", "field": "B"}, {}])
def test_value_needs_exactly_one(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Value.model_validate(data)


def test_value_const_may_be_empty() -> None:
    assert Value.model_validate({"const": ""}).const == ""


def test_too_many_columns_refused() -> None:
    cols = {f"C{i}": {"kind": "not_needed"} for i in range(MAX_COLUMNS + 1)}
    with pytest.raises(ValidationError):
        ReconcileSpec.model_validate(_spec(columns=cols))


def test_max_columns_allowed() -> None:
    cols = {f"C{i}": {"kind": "not_needed"} for i in range(MAX_COLUMNS)}
    assert len(ReconcileSpec.model_validate(_spec(columns=cols)).columns) == MAX_COLUMNS


@pytest.mark.parametrize("name", ["", "x" * 65, "bad\nname", "tab\tname", "nul\x00"])
def test_bad_column_name_refused(name: str) -> None:
    with pytest.raises(ValidationError):
        ReconcileSpec.model_validate(_spec(columns={name: {"kind": "not_needed"}}))


@pytest.mark.parametrize("wave", ["W1/2", "", "x" * 33, "W1;"])
def test_bad_wave_refused(wave: str) -> None:
    with pytest.raises(ValidationError):
        ReconcileSpec.model_validate(_spec(wave=wave))


def test_wave_with_space_dash_underscore_ok() -> None:
    assert ReconcileSpec.model_validate(_spec(wave="Wave 1_a-b")).wave == "Wave 1_a-b"


def test_json_round_trip() -> None:
    spec = ReconcileSpec.model_validate(_spec())
    data = spec.to_json()
    assert isinstance(data, bytes)
    assert ReconcileSpec.from_json(data) == spec


def test_from_json_too_large() -> None:
    data = b" " * (MAX_SPEC_BYTES + 1)
    with pytest.raises(SpecError) as exc:
        ReconcileSpec.from_json(data)
    assert str(exc.value) == "The spec is larger than 1 MiB."


def test_from_json_invalid_json() -> None:
    with pytest.raises(SpecError) as exc:
        ReconcileSpec.from_json(b"{not json")
    assert str(exc.value) == "The spec is not valid JSON."


def test_from_json_invalid_spec_does_not_echo_input() -> None:
    data = json.dumps(_spec(table="SECRET; DROP")).encode()
    with pytest.raises(SpecError) as exc:
        ReconcileSpec.from_json(data)
    assert str(exc.value) == "The spec does not match the reconciliation spec schema."
    assert "SECRET" not in str(exc.value)
    assert exc.value.__cause__ is None


# --- values -----------------------------------------------------------------


def test_clean() -> None:
    assert values.clean(" a ", trim=True) == "a"
    assert values.clean(" ", trim=True) == ""
    assert values.clean(None) == ""
    assert values.clean(" a ", trim=False) == " a "
    assert values.clean(12) == "12"


def test_alpha() -> None:
    assert values.alpha("000123") == "123"
    assert values.alpha("0") == "0"
    assert values.alpha("000") == "0"
    assert values.alpha("00A1") == "00A1"
    assert values.alpha("") == ""
    assert values.alpha("٠١") == "٠١"  # non-ASCII digits stay


def test_as_decimal() -> None:
    assert values.as_decimal("1.000") == Decimal("1")
    assert values.as_decimal(" -2.5 ") == Decimal("-2.5")
    assert values.as_decimal("abc") is None
    assert values.as_decimal("") is None
    assert values.as_decimal(None) is None
    assert values.as_decimal("NaN") is None
    assert values.as_decimal("Infinity") is None


def test_same() -> None:
    assert values.same("1.000", "1", numeric=True)
    assert values.same("abc", "1", numeric=True) is False
    assert values.same("abc", "abc", numeric=True)
    assert values.same("000123", "123", alpha=True)
    assert values.same("000123", "123") is False
    assert values.same(" x ", "x")
    assert values.same(" x ", "x", trim=False) is False
    assert values.same(None, " ")
    assert values.same("1.0", "1") is False


def test_pad_key() -> None:
    assert values.pad_key("12345", 18) == "000000000000012345"
    assert values.pad_key("AB-1", 18) == "AB-1"
    assert values.pad_key("123", 3) == "123"
    assert values.pad_key("12345", 3) == "12345"
