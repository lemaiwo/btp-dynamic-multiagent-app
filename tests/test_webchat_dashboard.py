"""The web chat's dashboards (design addendum (i)): the spec
(``agents/webchat/dashboard.py``), the tool ``create_dashboard``
(``agents/webchat/docgen.py``), the preview and the download.

Every refusal is a fixed text with the place (``widgets[<index>].<field>``)
and the rule, never a value of the spec.

Run:  .venv/bin/python -m pytest tests/test_webchat_dashboard.py -q
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from agents import auth  # noqa: E402
from agents.auth import current_jwt, current_principal  # noqa: E402
from agents.db import (  # noqa: E402
    ChatConversation,
    ChatFile,
    ChatMessage,
    SessionLocal,
    init_db,
)
from agents.validation_errors import install_validation_handler  # noqa: E402
from agents.webchat import dashboard, docgen, preview, routes, run, store  # noqa: E402
from agents.webchat.dashboard import (  # noqa: E402
    DASHBOARD_MEDIA_TYPE,
    DashboardError,
    build_dashboard,
    parse_dashboard,
    validate_spec,
)

ALICE = "alice@example.com"
API = "/chat/api"

DATA = {"columns": ["month", "sales", "costs"],
        "rows": [["2026-01-01", 120, 80.5], ["2026-02-01", None, 90], ["2026-03-01", 140, 95]]}


def good_spec() -> dict[str, Any]:
    return {
        "title": "Q1 sales",
        "description": "Sales and costs per month.\nFrom the uploaded workbook.",
        "widgets": [
            {"type": "bar", "title": "Sales vs costs", "data": copy.deepcopy(DATA),
             "x": "month", "y": ["sales", "costs"], "stacked": True, "unit": "EUR",
             "format": "currency", "size": 2, "note": "February sales missing."},
            {"type": "kpi", "title": "Revenue", "value": 260, "unit": "EUR", "delta": -4.5,
             "delta_label": "vs Q4", "trend": [1, 2.5, 3]},
            {"type": "table", "title": "Raw data", "data": copy.deepcopy(DATA)},
        ],
    }


def refused(mutate) -> DashboardError:
    spec = good_spec()
    mutate(spec)
    with pytest.raises(DashboardError) as caught:
        validate_spec(spec)
    return caught.value


def w(index: int):
    """A mutation helper: the widget at ``index`` of a spec."""
    return lambda spec: spec["widgets"][index]


# ---------------------------------------------------------------------------
# The spec
# ---------------------------------------------------------------------------
def test_a_good_spec_round_trips_as_compact_json_with_defaults():
    data = build_dashboard(validate_spec(good_spec()))
    assert b": " not in data and b", " not in data  # compact
    stored = json.loads(data)
    assert stored["widgets"][0]["y"] == ["sales", "costs"]
    assert stored["widgets"][2] == {"type": "table", "title": "Raw data", "data": DATA,
                                    "size": 1}
    # Integers stay integers; defaults are filled in, absent options left out.
    assert stored["widgets"][1]["value"] == 260 and stored["widgets"][1]["size"] == 1
    assert "stacked" not in json.dumps(stored["widgets"][2])
    assert parse_dashboard(data) == stored


@pytest.mark.parametrize("mutate, message", [
    (lambda s: s["widgets"][0].update(y=["profit"]),
     "widgets[0].y: not a column of data"),
    (lambda s: s["widgets"][0].update(y=["sales", "profit"]),
     "widgets[0].y[1]: not a column of data"),
    (lambda s: s["widgets"][0].update(x="quarter"), "widgets[0].x: not a column of data"),
    (lambda s: s["widgets"][0].update(y=["month"]),
     "widgets[0].y: column must hold numbers or null in every row"),
    (lambda s: s["widgets"][0].update(y=["sales", "sales"]), "widgets[0].y: names a column twice"),
    (lambda s: s["widgets"][0].update(type="pie", stacked=False),
     "widgets[0].y: a pie chart has exactly one series"),
    (lambda s: s["widgets"][0].update(type="scatter", stacked=False),
     "widgets[0].y: a scatter chart has exactly one series"),
    (lambda s: s["widgets"][0].update(type="scatter", stacked=False, y=["sales"]),
     "widgets[0].x: column must hold numbers or null in every row"),
    (lambda s: s["widgets"][0].update(type="line"),
     "widgets[0].stacked: only bar and area charts can be stacked"),
    (lambda s: s["widgets"][0].update(colour="red"), "widgets[0].colour: unknown key"),
    (lambda s: s["widgets"][0].update(format="euro"),
     "widgets[0].format: must be one of number, percent or currency"),
    (lambda s: s["widgets"][0].update(size=3), "widgets[0].size: must be 1 or 2"),
    (lambda s: s["widgets"][0].update(note="n" * 301), "widgets[0].note: at most 300 characters"),
    (lambda s: s["widgets"][0].update(unit="u" * 13), "widgets[0].unit: at most 12 characters"),
    (lambda s: s["widgets"][0].update(title="t" * 121),
     "widgets[0].title: at most 120 characters"),
    (lambda s: s["widgets"][0].pop("x"), "widgets[0].x: is required"),
    (lambda s: s["widgets"][0]["data"]["rows"].append(["2026-04-01", 1]),
     "widgets[0].data.rows[3]: a row must have one cell per column"),
    (lambda s: s["widgets"][0]["data"]["rows"][1].__setitem__(1, True),
     "widgets[0].data.rows[1][1]: a cell must be a finite number, a text or null"),
    (lambda s: s["widgets"][0]["data"]["rows"][1].__setitem__(1, float("inf")),
     "widgets[0].data.rows[1][1]: a cell must be a finite number, a text or null"),
    (lambda s: s["widgets"][0]["data"]["rows"][1].__setitem__(1, {"v": 1}),
     "widgets[0].data.rows[1][1]: a cell must be a finite number, a text or null"),
    (lambda s: s["widgets"][0]["data"]["rows"][0].__setitem__(0, "x" * 201),
     "widgets[0].data.rows[0][0]: a text cell has at most 200 characters"),
    (lambda s: s["widgets"][0]["data"].update(columns=["month", "month", "costs"]),
     "widgets[0].data.columns: names a column twice"),
    (lambda s: s["widgets"][2]["data"].update(columns=[f"c{i}" for i in range(21)]),
     "widgets[2].data.columns: at most 20 items"),
    (lambda s: s["widgets"][2]["data"].update(rows=[["a", 1, 2]] * 2001),
     "widgets[2].data.rows: at most 2000 items"),
    (lambda s: s["widgets"][0].update(y=[f"c{i}" for i in range(9)]),
     "widgets[0].y: at most 8 items"),
    (lambda s: s["widgets"][0].update(y=[]), "widgets[0].y: at least 1 item"),
    (lambda s: s["widgets"][1].update(value=True),
     "widgets[1].value: must be a finite number or a text"),
    (lambda s: s["widgets"][1].update(value="v" * 41),
     "widgets[1].value: a text value has at most 40 characters"),
    (lambda s: s["widgets"][1].update(delta=float("nan")),
     "widgets[1].delta: must be a finite number"),
    (lambda s: s["widgets"][1].update(trend=[1] * 61), "widgets[1].trend: at most 60 items"),
    (lambda s: s["widgets"][1].update(trend=[1, "2"]),
     "widgets[1].trend[1]: must be a finite number"),
    (lambda s: s["widgets"][1].update(delta_label="d" * 61),
     "widgets[1].delta_label: at most 60 characters"),
    (lambda s: s["widgets"][1].update(size=True), "widgets[1].size: must be a number"),
    (lambda s: s["widgets"][1].update(data=DATA), "widgets[1].data: unknown key"),
    (lambda s: s["widgets"][2].update(x="month"), "widgets[2].x: unknown key"),
    (lambda s: s["widgets"][2].update(type="gauge"),
     "widgets[2]: type must be one of bar, line, area, pie, scatter, kpi, table"),
    (lambda s: s["widgets"][2].pop("type"), "widgets[2]: type is required"),
    (lambda s: s.update(widgets=[]), "widgets: at least 1 item"),
    (lambda s: s.update(widgets=s["widgets"] * 5), "widgets: at most 12 items"),
    (lambda s: s.update(title="t" * 201), "title: at most 200 characters"),
    (lambda s: s.update(title=" "), "title: must not be empty"),
    (lambda s: s.update(title="a\x00b"), "title: contains a control character"),
    (lambda s: s.update(description="d" * 2001), "description: at most 2000 characters"),
    (lambda s: s.update(theme="dark"), "theme: unknown key"),
])
def test_every_rule_names_the_place_and_the_rule(mutate, message):
    error = refused(mutate)
    assert str(error) == message and error.code == "invalid_argument"


def test_a_single_series_names_y_and_a_refusal_never_holds_a_value():
    secret = "SECRET-COLUMN-NAME"
    error = refused(lambda s: s["widgets"][0].update(y=[secret]))
    assert str(error) == "widgets[0].y: not a column of data"
    error = refused(lambda s: s["widgets"][0].update(**{secret: 1}))
    # An unknown key is named only when it has the form of a name.
    assert str(error) == "widgets[0]: unknown key"
    error = refused(lambda s: s["widgets"][1].update(value=["SECRET"]))
    assert "SECRET" not in str(error)


def test_area_can_stack_and_scatter_takes_a_numeric_x():
    spec = good_spec()
    spec["widgets"][0].update(type="area")
    validate_spec(spec)
    spec["widgets"][0].update(type="scatter", stacked=False, x="sales", y=["costs"])
    validate_spec(spec)


def test_the_json_cap_and_the_byte_cap(monkeypatch):
    spec = good_spec()
    spec["widgets"][2]["data"]["rows"] = [["x" * 200, 1, 2]] * 1000
    error = refused(lambda s: s.update(widgets=spec["widgets"]))
    assert str(error) == "dashboard: at most 200000 characters of JSON"
    assert error.code == "too_large"
    monkeypatch.setattr(dashboard, "MAX_DASHBOARD_BYTES", 100)
    with pytest.raises(DashboardError) as caught:
        build_dashboard(validate_spec(good_spec()))
    assert caught.value.code == "too_large"


@pytest.mark.parametrize("data", [
    b"not json", b'{"title": NaN, "widgets": []}', b"\xff\xfe", b"[]", b"{}",
    json.dumps({**good_spec(), "extra": 1}).encode(),
])
def test_parse_refuses_anything_but_a_valid_spec(data):
    with pytest.raises(ValueError):
        parse_dashboard(data)


def test_parse_refuses_bytes_over_the_cap():
    with pytest.raises(ValueError):
        parse_dashboard(b" " * (dashboard.MAX_DASHBOARD_BYTES + 1))


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------
class Saved:
    def __init__(self) -> None:
        self.files: list[tuple[str, str, bytes]] = []

    async def __call__(self, name: str, media_type: str, data: bytes) -> dict[str, Any]:
        self.files.append((name, media_type, data))
        n = len(self.files)
        return {"file_id": f"f{n}", "name": name, "media_type": media_type,
                "size": len(data), "download_url": f"/chat/api/files/f{n}"}


@pytest.fixture
def run_ctx():
    saved = Saved()
    ctx = docgen.ChatRunContext(conversation_id="c", message_id="m", principal="p",
                                save=saved)
    token = docgen.current_chat_run.set(ctx)
    yield ctx, saved
    docgen.current_chat_run.reset(token)


async def test_the_tool_stores_the_checked_spec(run_ctx):
    ctx, saved = run_ctx
    spec = good_spec()
    out = await docgen.create_dashboard("../q1/sales.dashboard.json", spec["title"],
                                        spec["widgets"], description=spec["description"])
    name, media_type, data = saved.files[0]
    assert out == {"file_id": "f1", "name": "sales.dashboard.json",
                   "media_type": DASHBOARD_MEDIA_TYPE, "size": len(data),
                   "download_url": "/chat/api/files/f1", "title": "Q1 sales", "widgets": 3}
    assert media_type == "application/vnd.sap-btp-agent.dashboard+json"
    assert json.loads(data) == parse_dashboard(data)
    assert ctx.created == 1 and ctx.files[0]["file_id"] == "f1"
    out = await docgen.create_dashboard("plain.json", "T", spec["widgets"][1:2])
    assert out["name"] == "plain.dashboard.json" and out["widgets"] == 1


async def test_the_tool_refuses_with_a_fixed_text_and_stores_nothing(run_ctx):
    ctx, saved = run_ctx
    spec = good_spec()
    spec["widgets"][3:] = [{"type": "bar", "title": "x", "data": DATA, "x": "month",
                            "y": ["nope"]}]
    out = await docgen.create_dashboard("d", "T", spec["widgets"])
    assert out == {"error": {"code": "invalid_argument",
                             "message": "widgets[3].y: not a column of data"}}
    assert (await docgen.create_dashboard("", "T", spec["widgets"]))["error"]["code"] == (
        "invalid_name")
    big = [{"type": "table", "title": "t",
            "data": {"columns": ["a"], "rows": [["x" * 200]] * 1500}}]
    assert (await docgen.create_dashboard("d", "T", big))["error"]["code"] == "too_large"
    assert saved.files == [] and ctx.created == 0


async def test_the_tool_counts_in_the_ten_files_per_run(run_ctx):
    ctx, saved = run_ctx
    widgets = good_spec()["widgets"]
    for _ in range(docgen.MAX_FILES_PER_RUN):
        assert "file_id" in await docgen.create_dashboard("d", "T", widgets)
    out = await docgen.create_dashboard("d", "T", widgets)
    assert out["error"]["code"] == "too_many_files"


async def test_without_a_web_chat_run_no_dashboard_is_created():
    out = await docgen.create_dashboard("d", "T", good_spec()["widgets"])
    assert out["error"]["code"] == "invalid_argument"


def test_the_tool_is_a_document_tool_with_a_description():
    assert "create_dashboard" in docgen.TOOL_NAMES
    toolset = docgen.docgen_toolset()
    assert sorted(toolset.tools) == sorted(docgen.TOOL_NAMES)
    text = toolset.tools["create_dashboard"].description
    for phrase in ("dashboard", "never invent", "1-8 series", "ISO", "kpi", "table",
                   "one measure per axis"):
        assert phrase in text, phrase


def test_the_turn_records_a_dashboard_like_the_other_documents():
    assert run._docgen_output({"file_id": "f", "name": "a.dashboard.json", "size": 9}) == (
        "a.dashboard.json (9 bytes)")
    assert run._docgen_output({"error": {"code": "invalid_argument"}}) == (
        "error: invalid_argument")


# ---------------------------------------------------------------------------
# Stored row, preview, download, context
# ---------------------------------------------------------------------------
def bound_app() -> Any:
    inner = FastAPI()
    inner.include_router(routes.router)
    install_validation_handler(inner)

    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        p = current_principal.set(ALICE)
        j = current_jwt.set("jwt-of-alice")
        try:
            await inner(scope, receive, send)
        finally:
            current_principal.reset(p)
            current_jwt.reset(j)

    return asgi


@pytest.fixture
async def client(monkeypatch):
    saved = (auth._validator, auth._validator_checked)
    auth._validator, auth._validator_checked = None, True
    monkeypatch.setenv("AICORE_AVAILABLE_MODELS", "gpt-5")
    monkeypatch.setattr(run, "catalog", run.ModelCatalog())
    await init_db()

    async def wipe() -> None:
        async with SessionLocal() as s:
            for model in (ChatFile, ChatMessage, ChatConversation):
                await s.execute(delete(model))
            await s.commit()

    await wipe()
    async with AsyncClient(transport=ASGITransport(app=bound_app()),
                           base_url="http://test") as c:
        yield c
    await wipe()
    auth._validator, auth._validator_checked = saved


async def stored_dashboard() -> tuple[str, dict[str, Any]]:
    """A dashboard created by the tool through the store, as a run does."""
    async with SessionLocal() as db:
        conv = await store.create_conversation(db, ALICE, title=None, model=None)
        message = ChatMessage(conversation_id=conv.id, position=0, role="assistant",
                              status="running", content="")
        db.add(message)
        await db.commit()
        cid, mid = conv.id, message.id

    async def save(name: str, media_type: str, data: bytes) -> dict[str, Any]:
        async with SessionLocal() as db:
            return await store.add_generated_file(
                db, conversation_id=cid, message_id=mid, principal=ALICE, name=name,
                media_type=media_type, data=data)

    ctx = docgen.ChatRunContext(conversation_id=cid, message_id=mid, principal=ALICE,
                                save=save)
    token = docgen.current_chat_run.set(ctx)
    try:
        spec = good_spec()
        out = await docgen.create_dashboard("sales", spec["title"], spec["widgets"])
    finally:
        docgen.current_chat_run.reset(token)
    return cid, out


async def test_the_row_is_stored_and_the_preview_serves_the_spec(client):
    _, out = await stored_dashboard()
    async with SessionLocal() as db:
        row = (await db.execute(select(ChatFile).where(ChatFile.id == out["file_id"]))
               ).scalar_one()
    assert (row.kind, row.media_type, row.name) == (
        "generated", DASHBOARD_MEDIA_TYPE, "sales.dashboard.json")
    r = await client.get(f"{API}/files/{out['file_id']}/preview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"kind": "dashboard", "name": "sales.dashboard.json",
                    "dashboard": json.loads(row.data)}
    assert body["dashboard"]["widgets"][0]["type"] == "bar"


async def test_the_dashboard_is_never_served_inline(client):
    _, out = await stored_dashboard()
    for query in ("", "?inline=1"):
        r = await client.get(f"{API}/files/{out['file_id']}{query}")
        assert r.status_code == 200
        assert r.headers["content-disposition"].startswith("attachment;")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["content-type"].startswith(DASHBOARD_MEDIA_TYPE)
    assert DASHBOARD_MEDIA_TYPE not in routes.INLINE_TYPES
    assert DASHBOARD_MEDIA_TYPE not in preview.INLINE_TYPES


async def test_unreadable_dashboard_bytes_are_422(client):
    async with SessionLocal() as db:
        conv = await store.create_conversation(db, ALICE, title=None, model=None)
        rows = []
        for data in (b"{not json", json.dumps({"title": "T", "widgets": [
                {"type": "kpi", "title": "k", "value": 1, "evil": "<script>"}]}).encode()):
            row = ChatFile(conversation_id=conv.id, principal=ALICE, kind="generated",
                           name="x.dashboard.json", media_type=DASHBOARD_MEDIA_TYPE,
                           size=len(data), data=data)
            db.add(row)
            rows.append(row)
        await db.commit()
        ids = [row.id for row in rows]
    for fid in ids:
        r = await client.get(f"{API}/files/{fid}/preview")
        assert r.status_code == 422
        assert r.json() == {"detail": preview.MSG_UNREADABLE}


async def test_the_context_view_lists_the_tool_as_a_document_tool(client):
    cid, _ = await stored_dashboard()
    r = await client.get(f"{API}/conversations/{cid}/context")
    assert r.status_code == 200, r.text
    documents = [t["name"] for t in r.json()["tools"] if t["kind"] == "document"]
    assert documents == list(docgen.TOOL_NAMES) and "create_dashboard" in documents
