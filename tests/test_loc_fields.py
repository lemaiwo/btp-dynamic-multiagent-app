"""``agents.loc_fields``: the one rule for a key repeated in a refusal."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()

from agents import loc_fields, step_kinds, validation_errors  # noqa: E402
from agents.odata import admin_routes, models  # noqa: E402

FIELD_LIKE = ["name", "_x", "A1", "a" * 64]
NOT_FIELD_LIKE = ["", "1a", "a b", "a" * 65, "name\n", "https://host/x", "a-b", "é"]
NOT_FIELD_LIKE += [None, 1.5, True, ("a",)]


def test_the_rule_and_the_placeholder_are_what_every_gate_used():
    assert loc_fields.LOC_FIELD_RE.pattern == r"[A-Za-z_][A-Za-z0-9_]{0,63}"
    assert loc_fields.UNKNOWN_FIELD == "<unknown field>"
    assert validation_errors.UNKNOWN_FIELD is loc_fields.UNKNOWN_FIELD


@pytest.mark.parametrize("part", FIELD_LIKE)
def test_a_field_name_is_repeated_by_every_gate(part):
    assert loc_fields.loc_field(part) == part
    assert validation_errors._loc_part(part) == part
    assert step_kinds._loc_part(part) == part
    assert models._loc((part,)) == part
    assert admin_routes._body_loc((part,)) == part


@pytest.mark.parametrize("part", NOT_FIELD_LIKE)
def test_anything_else_is_the_placeholder_in_every_gate(part):
    assert loc_fields.loc_field(part) == "<unknown field>"
    assert validation_errors._loc_part(part) == "<unknown field>"
    assert step_kinds._loc_part(part) == "<unknown field>"
    assert models._loc((part,)) == "<unknown field>"
    assert admin_routes._body_loc((part,)) == "<unknown field>"


def test_a_list_index_is_shown_as_each_gate_showed_it():
    assert validation_errors._loc_part(3) == 3
    assert step_kinds._loc_part(3) == "3"
    assert models._loc(("entity_sets", 0, "x y")) == "entity_sets.0.<unknown field>"
    assert admin_routes._body_loc(("a", 2)) == "a.2"
    assert models._loc(()) == "service" and admin_routes._body_loc(()) == "body"


def test_the_helper_needs_nothing_but_the_standard_library():
    source = Path(loc_fields.__file__).read_text()
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert imports == ["from __future__ import annotations", "import re", "from typing import Any"]
