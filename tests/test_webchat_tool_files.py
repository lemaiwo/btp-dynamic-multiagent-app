"""A file another tool stores during a web chat turn gets a file card.

The document tools of ``agents/webchat/docgen.py`` emit their ``file`` part
themselves. A file stored by any other tool through ``docgen.store_file``
(the reconcile report, a HANA export) must get one ``file`` part and event
as well, once, also when the turn fails afterwards: the file exists.

Run:  .venv/bin/python -m pytest tests/test_webchat_tool_files.py -q
"""

# The harness fixtures are imported by name and then requested as arguments.
# ruff: noqa: F811

from __future__ import annotations

import json

from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall

from agents.webchat import docgen
from tests.test_webchat_api import (  # noqa: F401 - fixtures used by name
    API,
    Script,
    _clean,
    client,
    form,
    frames,
    new_conversation,
    use_model,
)

CSV = "text/csv"


def _with_export_tool(agent) -> None:
    @agent.tool_plain
    async def export_result(name: str) -> dict:
        """Stores a file of the answer as a tool outside docgen does."""
        ctx = docgen.current_chat_run.get()
        info = await docgen.store_file(ctx, f"{name}.csv", CSV, lambda: b"a,b\n1,2\n")
        return {"file_id": info.get("file_id"), "rows": 1}


def _calls_export(then):
    async def stream(messages, info):
        if any(isinstance(p, ToolReturnPart) for p in messages[-1].parts):
            async for chunk in then(messages, info):
                yield chunk
            return
        yield {0: DeltaToolCall(name="export_result",
                                json_args=json.dumps({"name": "export"}),
                                tool_call_id="exp-1")}
    return stream


async def _answer(messages, info):
    yield "Exported."


async def _fail(messages, info):
    raise RuntimeError("model went away")
    yield ""  # pragma: no cover


def _file_parts(parts):
    return [p for p in parts if p["type"] == "file"]


async def test_a_file_stored_by_a_tool_gets_one_file_part(client, monkeypatch,
                                                          real_agents_and_mcp):
    agent = use_model(monkeypatch, real_agents_and_mcp, _calls_export(_answer))
    _with_export_tool(agent)
    cid = await new_conversation(client)
    r = await client.post(f"{API}/conversations/{cid}/messages", **form(text="Export"))
    assert r.status_code == 200
    events = frames(r.text)
    names = [e for e, _ in events]
    file_events = [d for e, d in events if e == "file"]
    assert len(file_events) == 1
    ev = file_events[0]
    assert ev["name"] == "export.csv" and ev["media_type"] == CSV and ev["size"] == 8
    # The card comes before the usage line, which stays the last part.
    assert names.index("file") < names.index("usage")

    detail = (await client.get(f"{API}/conversations/{cid}")).json()
    assistant = detail["messages"][1]
    parts = _file_parts(assistant["parts"])
    assert parts == [{"type": "file", "file_id": ev["file_id"], "name": "export.csv",
                      "media_type": CSV, "size": 8}]
    assert assistant["parts"][-1]["type"] == "usage"


async def test_a_tool_file_of_a_failed_turn_still_gets_its_card(client, monkeypatch,
                                                                real_agents_and_mcp):
    agent = use_model(monkeypatch, real_agents_and_mcp, _calls_export(_fail))
    _with_export_tool(agent)
    cid = await new_conversation(client)
    r = await client.post(f"{API}/conversations/{cid}/messages", **form(text="Export"))
    events = frames(r.text)
    assert [e for e, _ in events].count("file") == 1
    assert "error" in [e for e, _ in events]
    detail = (await client.get(f"{API}/conversations/{cid}")).json()
    assistant = detail["messages"][1]
    assert assistant["status"] == "failed"
    assert len(_file_parts(assistant["parts"])) == 1


async def test_a_document_tool_file_is_not_shown_twice(client, monkeypatch,
                                                       real_agents_and_mcp):
    script = Script()
    use_model(monkeypatch, real_agents_and_mcp, script.workbook_then_text)
    cid = await new_conversation(client)
    r = await client.post(f"{API}/conversations/{cid}/messages", **form())
    events = frames(r.text)
    assert [e for e, _ in events].count("file") == 1
    detail = (await client.get(f"{API}/conversations/{cid}")).json()
    assert len(_file_parts(detail["messages"][1]["parts"])) == 1
