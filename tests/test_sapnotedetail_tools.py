"""Tests for the me.sap.com note-detail toolset."""

from __future__ import annotations

import asyncio
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


def _json_handler(payload: dict, seen: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url.params["q"])
        return httpx.Response(200, json=payload)
    return handler


def _client(handler, **kw) -> SapNoteDetailClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SapNoteDetailClient(http, "SESSION=x", **kw)


@pytest.mark.asyncio
async def test_get_details_returns_one_entry_per_note():
    seen: list[str] = []
    client = _client(_json_handler(load_fixture(), seen))
    result = await client.get_details(["3771065", "3747649"])
    assert result["count"] == 2
    assert sorted(seen) == ["3747649", "3771065"]
    assert [n["note"] for n in result["notes"]] == ["3771065", "3747649"]


@pytest.mark.asyncio
async def test_get_details_reports_an_empty_list_without_calling_out():
    seen: list[str] = []
    client = _client(_json_handler(load_fixture(), seen))
    result = await client.get_details([])
    assert result == {"notes": [], "count": 0, "unavailable": 0, "session_expired": False}
    assert seen == []


@pytest.mark.asyncio
async def test_get_details_never_exceeds_the_concurrency_cap():
    live = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1
        return httpx.Response(200, json=load_fixture())

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SapNoteDetailClient(http, "SESSION=x", max_concurrency=3)
    await client.get_details([str(n) for n in range(3000000, 3000012)])
    assert peak <= 3, f"ran {peak} concurrent requests against a cap of 3"


@pytest.mark.asyncio
async def test_a_dead_session_stops_the_batch_and_keeps_earlier_results():
    """100 real answers beat throwing the run away, but do not retry a corpse."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(200, json=load_fixture())
        return httpx.Response(403)

    client = _client(handler, max_concurrency=1)
    result = await client.get_details(["1000001", "1000002", "1000003", "1000004"])

    assert result["session_expired"] is True
    assert sum(1 for n in result["notes"] if n["status"] == "ok") == 2
    # The two after the failure are reported, not silently dropped.
    assert result["count"] == 4
    assert result["unavailable"] == 2
    # And no further requests were made once the session was known dead.
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_an_html_body_counts_as_an_expired_session():
    """This endpoint refuses an anonymous caller with 200 + HTML, not a 401."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>bootstrap</html>",
                              headers={"content-type": "text/html"})

    result = await _client(handler, max_concurrency=1).get_details(["1000001"])
    assert result["session_expired"] is True


@pytest.mark.asyncio
async def test_one_flaky_note_does_not_end_the_batch():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["q"] == "1000002":
            return httpx.Response(500, json={})
        return httpx.Response(200, json=load_fixture())

    result = await _client(handler).get_details(["1000001", "1000002", "1000003"])
    assert result["session_expired"] is False
    assert result["unavailable"] == 1
    assert sum(1 for n in result["notes"] if n["status"] == "ok") == 2


def test_toolset_exposes_one_batched_tool():
    from agents.sapnotedetail_tools import sapnotedetail_toolset

    http = httpx.AsyncClient(transport=httpx.MockTransport(_json_handler({})))
    toolset = sapnotedetail_toolset({}, http=http, cookie="SESSION=x")
    assert sorted(toolset.tools) == ["get_note_details"]


def test_toolset_takes_a_list_not_a_single_note():
    """129 notes must be one tool call, not 129 LLM turns."""
    import inspect

    from agents.sapnotedetail_tools import sapnotedetail_toolset

    http = httpx.AsyncClient(transport=httpx.MockTransport(_json_handler({})))
    toolset = sapnotedetail_toolset({}, http=http, cookie="SESSION=x")
    params = inspect.signature(toolset.tools["get_note_details"].function).parameters
    assert list(params) == ["notes"]
