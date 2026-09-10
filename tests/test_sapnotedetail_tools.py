"""Tests for the me.sap.com note-detail toolset."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import httpx
import pytest

from agents.sapnotedetail_tools import (
    BUILTIN_SAPNOTEDETAIL_URL,
    SapNoteDetailClient,
    parse_detail,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sapnote_detail.json"
NOTE = "3771065"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_builtin_url_is_stable():
    assert BUILTIN_SAPNOTEDETAIL_URL == "builtin:sapnotedetail"


def test_parse_detail_reports_ok_and_echoes_the_note():
    parsed = parse_detail(load_fixture(), NOTE)
    assert parsed["note"] == NOTE
    assert parsed["status"] == "ok"


def test_parse_detail_extracts_the_header():
    parsed = parse_detail(load_fixture(), NOTE)
    assert parsed["note_type"] == "SAP Security Note"
    assert "CVE-2018-2424" in parsed["title"]


def test_parse_detail_extracts_validity():
    parsed = parse_detail(load_fixture(), NOTE)
    entry = parsed["validity"][0]
    assert set(entry) == {"software_component", "from", "to"}
    assert entry["software_component"] == "HDB"
    assert entry["from"] == "1.00"


def test_parse_detail_reads_support_packages_from_the_patch_list():
    """`SupportPackage` is empty on every note sampled; the level is in
    `SupportPackagePatch`. Reading the wrong one returns nothing for every
    note while looking like it works."""
    parsed = parse_detail(load_fixture(), NOTE)
    packages = parsed["support_packages"]
    assert packages, "the fixture note must carry at least one fixing level"
    first = packages[0]
    assert set(first) == {"component_version", "support_package", "patch"}
    assert first["component_version"] == "SAPUI5 CLIENT RT AS JAVA 7.40"
    assert first["support_package"] == "SP010"
    assert first["patch"] == "000014"


def test_parse_detail_degrades_on_an_unexpected_shape():
    """A private API can change without notice; a shape change must not raise."""
    parsed = parse_detail({"something": "else"}, NOTE)
    assert parsed["status"] == "unavailable"
    assert parsed["note"] == NOTE
    assert parsed["support_packages"] == []


def test_parse_detail_degrades_on_garbage():
    for junk in (None, [], "text", 42):
        parsed = parse_detail(junk, NOTE)
        assert parsed["status"] == "unavailable"
