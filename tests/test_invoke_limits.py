"""``agents/invoke_config.py``: the attachment limits and the result-schema
subset, read the same way by storage and by the invoke API.

Run:  .venv/bin/python -m pytest tests/test_invoke_limits.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.invoke_config import (  # noqa: E402
    InvokeConfigError,
    InvokeLimits,
    check_result_schema,
    clean_limits,
    parse_limits,
    validate,
)


def test_defaults():
    assert parse_limits(None) == InvokeLimits(
        ("image/png", "image/jpeg", "application/pdf"), 10 * 1024 * 1024, 5, 20)


def test_clean_limits_drops_unknown_keys_and_checks_the_known_ones():
    assert clean_limits({"max_files": 2, "SECRET_KEY": 1}) == {"max_files": 2}
    for bad in ({"max_files": 0}, {"max_files": True}, {"max_files": "2"},
                {"max_file_bytes": 26 * 1024 * 1024}, {"max_pdf_pages": 101},
                {"media_types": ["image/gif"]}, {"media_types": []}, ["x"]):
        with pytest.raises(InvokeConfigError) as refused:
            clean_limits(bad)
        assert str(refused.value).startswith("invoke_limits")


def test_parse_limits_falls_back_per_key_and_never_widens_a_ceiling():
    raw = json.dumps({"max_files": 99, "max_pdf_pages": 3, "media_types": ["text/html"]})
    limits = parse_limits(raw)
    assert (limits.max_files, limits.max_pdf_pages) == (5, 3)
    assert limits.media_types == InvokeLimits().media_types
    assert parse_limits("not json") == InvokeLimits()


@pytest.mark.parametrize("schema", [
    {"type": "array"},
    {"type": "object", "properties": {"a": {"$ref": "#/x"}}},
    {"type": "object", "oneOf": []},
    {"type": "object", "properties": {"a": {"type": "date"}}},
    {"type": "object", "required": "a"},
    {"type": "object", "properties": {"a": {"enum": [{"x": 1}]}}},
])
def test_a_schema_outside_the_subset_is_refused(schema):
    with pytest.raises(InvokeConfigError):
        check_result_schema(schema)


def test_the_subset_is_enforced():
    schema = check_result_schema({
        "type": "object", "title": "t", "description": "d",
        "properties": {
            "id": {"type": "string", "minLength": 2, "maxLength": 4},
            "n": {"type": "integer", "minimum": 1, "maximum": 3},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 1},
            "flag": {"type": ["boolean", "null"]},
            "more": {"type": "object", "additionalProperties": {"type": "number"}},
        },
        "required": ["id"],
        "additionalProperties": False,
    })
    assert validate(schema, {"id": "ab", "n": 2, "tags": ["x"], "flag": None,
                             "more": {"a": 1.5}}) == []
    problems = validate(schema, {"id": "a", "n": True, "tags": ["x", 1], "flag": "no",
                                 "more": {"a": "x"}, "zz": 1})
    assert problems == [
        "$.id: shorter than 2 characters",
        "$.n: must be of type integer",
        "$.tags: more than 1 items",
        "$.tags[1]: must be of type string",
        "$.flag: must be of type boolean or null",
        "$.more.*: must be of type number",
        "$: has a property the schema does not allow",
    ]
    assert validate(schema, {}) == ["$.id: is required"]
    assert validate(schema, {"id": "ab", "n": 1.0}) == []
    assert validate({"type": "object", "properties": {"e": {"enum": [1]}}}, {"e": True}) == [
        "$.e: must be one of the allowed values"]
