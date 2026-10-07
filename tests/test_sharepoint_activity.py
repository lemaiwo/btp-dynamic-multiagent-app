"""What a run's activity keeps of a ``builtin:sharepoint`` tool result.

The activity of an API-triggered run is stored with the run
(``job_runs.activity_json``) and served by ``GET /admin/api/runs/{id}``. For
the two SharePoint tools it holds counts and a refusal's code, never the head
of the result; for every other tool the preview is what it was.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)

from agents import registry  # noqa: E402
from agents.run_activity import recording  # noqa: E402
from agents.sharepoint_tools import activity_summary, sharepoint_toolset  # noqa: E402
from tests.sharepoint_helpers import PINS, VIEWS, FakeGraph, build_workbook  # noqa: E402

MEMBER = "Zz Planted Member"
TEAM = "zz-planted-team"
LOOKED_UP = "zz-planted-id"
MARKS = (MEMBER, TEAM, LOOKED_UP, "zz-planted", "Planted")
WORKBOOK = build_workbook(
    {2026: {(MEMBER, "Presence"): {"2026-01-05": "H", "2026-01-06": "H"},
            (MEMBER, "Guard"): {"2026-01-06": "GDI"}}},
    team=[(MEMBER, TEAM, LOOKED_UP, "u-1"), ("Bob Sample", "Basis", 1002, "u-bob")])
PLAIN = ("read_table", "read_calendar")
PREFIXED = ("sharepoint_read_table", "sharepoint_read_calendar")
NUMBERED = ("sharepoint_0_read_table", "sharepoint_1_read_calendar")


async def _results() -> tuple[dict, dict]:
    graph = FakeGraph(WORKBOOK)
    toolset = sharepoint_toolset({**PINS, "views": VIEWS, "destination": "GRAPH"},
                                 http=graph.client(),
                                 download_transport=graph.download_transport())
    table = await toolset.tools["read_table"].function(view="team")
    calendar = await toolset.tools["read_calendar"].function(
        view="planning", date_from="2026-01-05", date_to="2026-01-06")
    # The premise: the results do hold what was typed into the workbook.
    assert MEMBER in json.dumps(table) and TEAM in json.dumps(table)
    assert LOOKED_UP in json.dumps(table)
    assert MEMBER in json.dumps(calendar) and LOOKED_UP in json.dumps(calendar)
    assert calendar["conflicts"]
    return table, calendar


async def _activity(parts: list) -> dict:
    """The activity of a run in which a specialist got ``parts`` back, through
    the registry's own event handler and the recorder of a job run."""
    async def events():
        for part in parts:
            yield FunctionToolCallEvent(part=ToolCallPart(
                tool_name=part.tool_name, args={"view": "team"},
                tool_call_id=part.tool_call_id))
            yield FunctionToolResultEvent(result=part)

    with recording("run-1") as activity:
        await registry._make_progress_handler("planner")(None, events())
        return activity.to_dict()


def _returned(names, table, calendar) -> list:
    return [ToolReturnPart(tool_name=names[0], content=table, tool_call_id="c0"),
            ToolReturnPart(tool_name=names[1], content=calendar, tool_call_id="c1")]


@pytest.mark.parametrize("names", [PLAIN, PREFIXED, NUMBERED])
async def test_a_result_is_recorded_as_counts_never_as_content(names):
    table, calendar = await _results()
    activity = await _activity(_returned(names, table, calendar))
    stored = json.dumps(activity, ensure_ascii=False)
    assert not any(mark in stored for mark in MARKS)
    assert [e["output"] for e in activity["events"]] == [
        "read_table: 2 rows, 0 skipped",
        "read_calendar: 2 runs, 1 conflicts, 0 skipped, 0 unmapped",
    ]
    assert [(e["tool"], e["status"]) for e in activity["events"]] == [
        (names[0], "ok"), (names[1], "ok")]


@pytest.mark.parametrize("names", [PLAIN, PREFIXED])
async def test_a_refusal_is_recorded_as_its_code_only(names):
    refusal = {"error": {"code": "unknown_view",
                         "message": "there is no view of that name for this tool",
                         "hint": f"views: {MEMBER}"}}
    odd = {"error": {"code": f"{MEMBER} said", "message": TEAM}}
    activity = await _activity([
        ToolReturnPart(tool_name=names[0], content=refusal, tool_call_id="c0"),
        ToolReturnPart(tool_name=names[1], content=odd, tool_call_id="c1"),
        ToolReturnPart(tool_name=names[0], content={"error": LOOKED_UP}, tool_call_id="c2"),
        # What the framework sends back when the arguments did not validate.
        RetryPromptPart(tool_name=names[1], content=f"{MEMBER} is not a date",
                        tool_call_id="c3"),
        ToolReturnPart(tool_name=names[0], content=f"{TEAM} as text", tool_call_id="c4"),
        ToolReturnPart(tool_name=names[1], content={"runs": MEMBER, "unmapped": TEAM,
                                                    "skipped_rows": True},
                       tool_call_id="c5"),
    ])
    assert not any(mark in json.dumps(activity) for mark in MARKS)
    assert [e["output"] for e in activity["events"]] == [
        "error: unknown_view", "error", "error", "read_calendar: no summary",
        "read_table: no summary",
        "read_calendar: ? runs, ? conflicts, ? skipped, ? unmapped",
    ]
    assert activity["events"][3]["status"] == "error"


def _before(result) -> str:
    """``registry._short_tool_output`` as it was before the SharePoint rule."""
    try:
        content = getattr(result, "content", None)
        if content is None and hasattr(result, "model_response_str"):
            content = result.model_response_str()
        text = content if isinstance(content, str) else registry._PREVIEW_REPR.repr(content)
    except Exception:  # noqa: BLE001
        text = ""
    text = text.strip()
    return text[:600] + "…" if len(text) > 600 else text


async def test_every_other_tools_preview_is_what_it_was():
    table, calendar = await _results()
    others = [
        ToolReturnPart(tool_name="SAPRead", content="  REPORT zdemo.\n" * 80, tool_call_id="a"),
        ToolReturnPart(tool_name="search_messages", content={"messages": [{"id": 1}] * 90},
                       tool_call_id="b"),
        ToolReturnPart(tool_name="list_issues", content=[1, "two", None], tool_call_id="c"),
        ToolReturnPart(tool_name="load_skill", content="", tool_call_id="d"),
        RetryPromptPart(tool_name="SAPRead", content="unknown object", tool_call_id="e"),
        # Names that only resemble the two tools keep the generic preview.
        ToolReturnPart(tool_name="read_tables", content=table, tool_call_id="f"),
        ToolReturnPart(tool_name="outlook_read_calendar", content=calendar, tool_call_id="g"),
        ToolReturnPart(tool_name="sharepoint_read_table_x", content=table, tool_call_id="h"),
        ToolReturnPart(tool_name="Read_Table", content={"rows": []}, tool_call_id="i"),
        object(),
    ]
    for part in others:
        assert registry._short_tool_output(part) == _before(part)
        assert activity_summary(getattr(part, "tool_name", None),
                                getattr(part, "content", None)) is None
    activity = await _activity(others[:5])
    assert [e["output"] for e in activity["events"]] == [
        " ".join(_before(p).split())[:399] + "…" if len(" ".join(_before(p).split())) > 400
        else " ".join(_before(p).split()) for p in others[:5]]


def test_the_summary_takes_counts_only_from_whole_numbers():
    assert activity_summary("read_table", {"rows": [1, 2], "skipped_rows": 3}) == \
        "read_table: 2 rows, 3 skipped"
    assert activity_summary("read_table", {"rows": [], "skipped_rows": "3"}) == \
        "read_table: 0 rows, ? skipped"
    assert activity_summary("read_calendar", None) == "read_calendar: no summary"
    assert activity_summary(None, {"rows": []}) is None
    assert activity_summary(7, {"rows": []}) is None
