"""``agents.loc_fields``: the one rule for a key repeated in a refusal."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

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
    assert imports == [
        "from __future__ import annotations",
        "import re",
        "import unicodedata",
        "from typing import TYPE_CHECKING, Any",
    ]


# -- the one-line rule ----------------------------------------------------------

LINE_BREAKING = ["\n", "\r", "\t", "\x00", "\x1b", "\x7f", "\x85", "\u2028", "\u2029"]
FORMAT = ["\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u2066", "\u2067", "\u2068",
          "\u2069", "\u200b", "\u200e", "\u200f", "\u2060", "\ufeff", "\u00ad"]


@pytest.mark.parametrize("char", LINE_BREAKING)
def test_a_control_character_or_a_line_separator_is_never_one_line(char):
    for refuse_format in (False, True):
        with pytest.raises(ValueError):
            loc_fields.one_line_text(f"a{char}b", refuse_format=refuse_format)


@pytest.mark.parametrize("char", FORMAT)
def test_a_format_character_is_refused_only_when_asked(char):
    assert loc_fields.one_line_text(f"a{char}b") == f"a{char}b"
    with pytest.raises(ValueError) as err:
        loc_fields.one_line_text(f"a{char}b", refuse_format=True, field="label")
    assert str(err.value) == (
        "label must be one line of text without control or format characters"
    )


def test_the_message_names_the_field_and_the_rule_never_the_text():
    secret = "hunter2-pasted"
    with pytest.raises(ValueError) as err:
        loc_fields.one_line_text(f"{secret}\nx", field="title")
    assert str(err.value) == "title must be one line of text without control characters"
    with pytest.raises(ValueError) as err:
        loc_fields.one_line_text(f"{secret}\nx")
    assert str(err.value) == "must be one line of text without control characters"
    assert loc_fields.one_line_text("Plain text, with ünïcode") == "Plain text, with ünïcode"


def test_the_odata_and_the_ina_gate_use_the_one_rule():
    from agents.ina import models as ina_models

    assert models.one_line is loc_fields.one_line
    assert models._one_line is loc_fields.one_line
    assert ina_models.one_line_strict is loc_fields.one_line_strict


# -- the refusal text -----------------------------------------------------------


class _Row(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    count: int


class _Rows(BaseModel):
    rows: list[_Row]


def test_refusal_text_lists_loc_and_msg_and_never_the_input():
    secret = "https://user:hunter2@host/x?token=abc"
    with pytest.raises(ValidationError) as err:
        _Rows.model_validate({"rows": [{"name": secret, "count": secret, secret: 1}]})
    assert loc_fields.refusal_text(err.value, "body") == (
        "rows.0.count: Input should be a valid integer, unable to parse string as an integer; "
        "rows.0.<unknown field>: Extra inputs are not permitted"
    )
    with pytest.raises(ValidationError) as err:
        _Row.model_validate("not an object")
    assert loc_fields.refusal_text(err.value, "body").startswith("body: ")


def test_refusal_text_names_at_most_twenty_problems():
    rows = [{"name": "n", "count": "x"} for _ in range(25)]
    with pytest.raises(ValidationError) as err:
        _Rows.model_validate({"rows": rows})
    text = loc_fields.refusal_text(err.value, "body")
    lines = text.split("; ")
    assert len(lines) == loc_fields.MAX_REPORTED_ERRORS + 1 == 21
    assert lines[0].startswith("rows.0.count: ") and lines[19].startswith("rows.19.count: ")
    assert lines[-1] == "and 5 more"


def test_every_gate_formats_its_refusal_with_it():
    from agents.data_catalog import routes as dc_routes
    from agents.ina import models as ina_models

    assert models.refusal_text is loc_fields.refusal_text
    assert ina_models.refusal_text is loc_fields.refusal_text
    assert dc_routes.refusal_text is loc_fields.refusal_text
    assert admin_routes.refusal_text is loc_fields.refusal_text
    assert models.loc_path is loc_fields.loc_path
