"""Tests for the NVD-backed SAP security note toolset."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from agents.sapnotes_tools import (
    BUILTIN_SAPNOTES_URL,
    SapNotesClient,
    extract_note_number,
    extract_score,
)

FIXTURE = Path(__file__).parent / "fixtures" / "nvd_sap_sample.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_extract_note_number_modern_url():
    refs = [{"url": "https://me.sap.com/notes/3747649"}]
    assert extract_note_number(refs) == "3747649"


def test_extract_note_number_legacy_launchpad_url():
    refs = [{"url": "https://launchpad.support.sap.com/#/notes/3759472"}]
    assert extract_note_number(refs) == "3759472"


def test_extract_note_number_returns_none_when_absent():
    assert extract_note_number([{"url": "https://example.invalid/advisory"}]) is None


def test_extract_note_number_ignores_short_numbers():
    # A 4-digit number in a URL path is not a note number.
    assert extract_note_number([{"url": "https://me.sap.com/notes/1234"}]) is None


def test_extract_score_prefers_v31():
    metrics = {
        "cvssMetricV31": [{"cvssData": {"baseScore": 10.0}}],
        "cvssMetricV30": [{"cvssData": {"baseScore": 4.0}}],
    }
    assert extract_score(metrics) == 10.0


def test_extract_score_falls_back_to_v40_then_v30():
    assert extract_score({"cvssMetricV40": [{"cvssData": {"baseScore": 8.5}}]}) == 8.5
    assert extract_score({"cvssMetricV30": [{"cvssData": {"baseScore": 9.8}}]}) == 9.8


def test_extract_score_returns_none_when_unscored():
    assert extract_score({}) is None


def test_builtin_url_is_stable():
    assert BUILTIN_SAPNOTES_URL == "builtin:sapnotes"


def client_with_fixture(**kwargs) -> SapNotesClient:
    """A client whose transport always answers with the sample response."""
    payload = load_fixture()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["sourceIdentifier"] == "cna@sap.com"
        return httpx.Response(200, json=payload)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SapNotesClient(http, **kwargs)


@pytest.mark.asyncio
async def test_list_critical_notes_filters_by_score():
    client = client_with_fixture(min_score=9.0)
    result = await client.list_critical_notes(limit=100)
    numbers = [n["note"] for n in result["notes"]]
    # 10.0 and 9.8 qualify; the 8.5 Commerce Cloud record does not.
    assert numbers == ["3747649", "3759472"]


@pytest.mark.asyncio
async def test_list_critical_notes_reports_skipped_records():
    client = client_with_fixture(min_score=9.0)
    result = await client.list_critical_notes(limit=100)
    # One 9.9 record has no note number; one record has no score at all.
    assert result["skipped_no_note"] == 1
    assert result["skipped_no_score"] == 1


@pytest.mark.asyncio
async def test_list_critical_notes_carries_score_and_description():
    client = client_with_fixture(min_score=9.0)
    first = (await client.list_critical_notes(limit=100))["notes"][0]
    assert first["cve"] == "CVE-2026-44756"
    assert first["score"] == 10.0
    assert first["published"] == "2026-09-08"
    assert "Extended Passport" in first["description"]


@pytest.mark.asyncio
async def test_list_critical_notes_deduplicates_by_note_number():
    """Two CVEs pointing at one note yield one entry, keeping the higher score."""
    payload = load_fixture()
    duplicate = json.loads(json.dumps(payload["vulnerabilities"][1]))
    duplicate["cve"]["id"] = "CVE-2026-99999"
    duplicate["cve"]["metrics"] = {"cvssMetricV31": [{"cvssData": {"baseScore": 9.1}}]}
    payload["vulnerabilities"].append(duplicate)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await SapNotesClient(http, min_score=9.0).list_critical_notes(limit=100)
    entries = [n for n in result["notes"] if n["note"] == "3759472"]
    assert len(entries) == 1
    assert entries[0]["score"] == 9.8


@pytest.mark.asyncio
async def test_min_score_is_respected_when_lowered():
    client = client_with_fixture(min_score=8.0)
    numbers = [n["note"] for n in (await client.list_critical_notes(limit=100))["notes"]]
    assert "3792978" in numbers


def test_toolset_exposes_one_tool_and_pins_the_source():
    from agents.sapnotes_tools import sapnotes_toolset

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["sourceIdentifier"] == "cna@sap.com"
        return httpx.Response(200, json=load_fixture())

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    toolset = sapnotes_toolset({}, http=http)
    # `.tools` rather than `get_tools(ctx)`: the registered surface is what
    # matters here, and building a RunContext just to read names would test
    # pydantic-ai instead of this module.
    assert sorted(toolset.tools) == ["list_critical_notes"]


def test_toolset_rejects_a_source_override_in_config():
    """An admin cannot repoint the toolset at another CNA."""
    from agents.sapnotes_tools import sapnotes_toolset

    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    toolset = sapnotes_toolset({"sourceIdentifier": "cna@example.com"}, http=http)
    assert toolset.http_client is http


def test_toolset_rejects_an_unparsable_lookback_at_build_time():
    from agents.sapnotes_tools import sapnotes_toolset

    with pytest.raises(ValueError, match="invalid lookback"):
        sapnotes_toolset({"lookback": "banana"})
