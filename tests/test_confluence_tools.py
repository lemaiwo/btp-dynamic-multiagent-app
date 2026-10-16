"""The Confluence toolset: configuration, CQL, confinement, the three tools
and every refusal code, against a MockTransport. No network, no Confluence.

The destination is a resolver double (as in tests/test_jira_tools.py):
destination resolution has its own file.

Run:  python -m pytest tests/test_confluence_tools.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
for _var in ("DESTINATION_CLIENT_ID", "DESTINATION_CLIENT_SECRET",
             "DESTINATION_URI", "DESTINATION_TOKEN_URL", "DESTINATION_UAA_URL"):
    os.environ.pop(_var, None)

import pytest  # noqa: E402

from agents.confluence_tools import (  # noqa: E402
    CONFLUENCE_API,
    ERROR_CODES,
    ConfluenceError,
    build_cql,
    clean_query,
    confine_page_id,
    normalize_api_base,
    normalize_spaces,
)

SITE = "https://example.atlassian.net"
SECRET_KEY = "Zq9!bad key"


# --- configuration ------------------------------------------------------------

def test_normalize_spaces_accepts_csv_and_lists_and_dedupes():
    assert normalize_spaces("shERPa, DOCS,, shERPa ") == ["shERPa", "DOCS"]
    assert normalize_spaces(["A", "~user", "B-1_x"]) == ["A", "~user", "B-1_x"]
    assert normalize_spaces(None) == [] and normalize_spaces("") == []


def test_normalize_spaces_refuses_a_bad_key_without_repeating_it():
    with pytest.raises(ValueError) as info:
        normalize_spaces(f"DOCS, {SECRET_KEY}")
    assert str(info.value).startswith("oauth.spaces:") and SECRET_KEY not in str(info.value)
    with pytest.raises(ValueError):
        normalize_spaces('A" OR space = "B')


def test_normalize_api_base_defaults_to_cloud_and_keeps_jiras_rules():
    assert normalize_api_base("") == CONFLUENCE_API == "/wiki/rest/api"
    assert normalize_api_base(None) == "/wiki/rest/api"
    assert normalize_api_base(" /rest/api/ ") == "/rest/api"
    assert normalize_api_base("/") == ""
    for bad in ("https://x.example/wiki", "//x", "rest/api", "/rest/api?x=1", "/a/../b"):
        with pytest.raises(ValueError):
            normalize_api_base(bad)


# --- CQL ----------------------------------------------------------------------

def test_build_cql_text_title_and_space_pin():
    assert (build_cql("Z_QM06_FA_CREATE_INSP_LOT", [])
            == 'type = page AND text ~ "Z_QM06_FA_CREATE_INSP_LOT" ORDER BY lastmodified DESC')
    assert (build_cql("inspection lot", ["shERPa"], in_title=True)
            == 'type = page AND title ~ "inspection lot" AND space in ("shERPa") '
               "ORDER BY lastmodified DESC")
    assert 'space in ("A", "B")' in build_cql("x", ["A", "B"])


def test_build_cql_escapes_quotes_and_backslashes():
    assert build_cql('a" OR type = attachment OR text ~ "b', []) == (
        'type = page AND text ~ "a\\" OR type = attachment OR text ~ \\"b" '
        "ORDER BY lastmodified DESC")
    assert 'text ~ "a\\\\b"' in build_cql("a\\b", [])


def test_clean_query_strips_control_and_format_characters_and_bounds_length():
    assert clean_query("  Z_QM06​  FA\x07\tLOT ") == "Z_QM06 FA LOT"
    for empty in ("", "   ", "​\x00", None):
        with pytest.raises(ConfluenceError) as info:
            clean_query(empty)
        assert info.value.code == "invalid_query"
    with pytest.raises(ConfluenceError) as info:
        clean_query("x" * 201)
    assert info.value.code == "invalid_query"
    assert clean_query("x" * 200) == "x" * 200


def test_confine_page_id():
    assert confine_page_id(" 1241088001 ") == "1241088001"
    assert confine_page_id(1241088001) == "1241088001"
    for bad in ("", "abc", "12/../3", "1" * 21, "-1", None):
        with pytest.raises(ConfluenceError) as info:
            confine_page_id(bad)
        assert info.value.code == "invalid_id"


def test_error_result_shape_and_closed_code_list():
    err = ConfluenceError("not_found", "Confluence has no such page (404)", hint="check the id")
    assert err.as_result() == {"error": {"code": "not_found",
                                         "message": "Confluence has no such page (404)",
                                         "hint": "check the id"}}
    assert ConfluenceError("forbidden", "no").as_result() == {
        "error": {"code": "forbidden", "message": "no"}}
    assert ERROR_CODES == {
        "destination_error", "unauthorized", "forbidden", "not_found", "throttled",
        "confluence_error", "unreachable", "not_json", "invalid_id", "invalid_query",
        "space_not_allowed", "too_large"}
    with pytest.raises(AssertionError):
        ConfluenceError("made_up", "x")
