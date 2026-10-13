"""The invoke API settings of an agent through the admin API and the imports.

``expose_invoke``, ``result_schema`` and ``invoke_limits`` are checked at the
admin gate (`agents.admin.AgentPayload`) by the same functions storage uses
(`agents.invoke_config.check_result_schema` / `clean_limits`), so neither
accepts what the other refuses. A refusal names the field and the rule, never
the value. A writer that does not carry a key (an older client or bundle)
keeps what is stored.

Run:  .venv/bin/python -m pytest tests/test_admin_invoke_fields.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ["MCP_URL_ALLOWLIST"] = ""

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

import app as app_module  # noqa: E402
from agents.db import (  # noqa: E402
    BITBUCKET_APPROVER_INVOKE_MESSAGE,
    SessionLocal,
    get_agent_by_name,
    init_db,
)

PREFIX = "inv-adm-"
SERVERS = [{"url": "https://inv-adm.example.com/mcp", "auth_mode": "none"}]
SCHEMA = {
    "type": "object",
    "properties": {"amount": {"type": "number", "minimum": 0}},
    "required": ["amount"],
    "additionalProperties": False,
}
LIMITS = {"media_types": ["application/pdf"], "max_file_bytes": 2_000_000,
          "max_files": 2, "max_pdf_pages": 4}
# A marker that must never come back in an answer: it sits inside the
# refused values below.
SECRET = "Zx9SecretMarker"
APPROVING = [{"url": "builtin:bitbucket", "auth_mode": "destination", "oauth": {
    "destination": "BITBUCKET", "workspace": "acme-ws",
    "allow_comment": True, "allow_approve": True}}]


def agent(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": PREFIX + name, "description": "d", "instructions": "i",
            "mcp_servers": SERVERS, **extra}


@pytest.fixture
async def client():
    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app_module.app),
                           base_url="http://test") as c:
        yield c
        for a in (await c.get("/admin/api/agents")).json():
            if a["name"].startswith(PREFIX):
                await c.delete(f"/admin/api/agents/{a['id']}?force=true")


async def _row(name: str):
    async with SessionLocal() as session:
        return await get_agent_by_name(session, PREFIX + name)


def _refusal(r, field: str) -> str:
    assert r.status_code == 422, r.text
    assert SECRET not in r.text
    text = json.dumps(r.json())
    assert field in text, text
    return text


# --- create / update ---------------------------------------------------------

async def test_create_accepts_and_stores_the_three(client):
    r = await client.post("/admin/api/agents", json=agent(
        "ok", api_slug="inv-adm-ok", expose_invoke=True,
        result_schema=SCHEMA, invoke_limits=LIMITS))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["expose_invoke"] is True
    assert body["result_schema"] == SCHEMA
    assert body["invoke_limits"] == LIMITS
    # Changing the invoke settings needs no registry rebuild: the invoke
    # route reads the row on every call.
    assert body["reloaded"] is False
    row = await _row("ok")
    assert row.expose_invoke == 1


async def test_create_without_the_keys_is_off_and_empty(client):
    r = await client.post("/admin/api/agents", json=agent("plain"))
    assert r.status_code == 201, r.text
    assert r.json()["expose_invoke"] is False
    assert r.json()["result_schema"] is None
    assert r.json()["invoke_limits"] is None


@pytest.mark.parametrize("bad, field", [
    ({"result_schema": {"type": "object", "pattern": SECRET}}, "result_schema"),
    ({"result_schema": {"type": "array", "title": SECRET}}, "result_schema"),
    ({"result_schema": [SECRET]}, "result_schema"),
    ({"invoke_limits": {"media_types": [SECRET]}}, "invoke_limits"),
    ({"invoke_limits": {"max_files": 99}}, "invoke_limits"),
    ({"invoke_limits": {"max_pdf_pages": True}}, "invoke_limits"),
    ({"invoke_limits": SECRET}, "invoke_limits"),
])
async def test_a_bad_schema_or_limits_is_refused_naming_the_field(client, bad, field):
    r = await client.post("/admin/api/agents", json=agent("bad", **bad))
    _refusal(r, field)
    assert await _row("bad") is None


async def test_expose_invoke_needs_a_slug(client):
    r = await client.post("/admin/api/agents", json=agent("noslug", expose_invoke=True))
    assert r.status_code == 422, r.text
    assert "expose_invoke" in r.text and "api_slug" in r.text
    # And on update: the slug cleared while the switch stays on.
    r = await client.post("/admin/api/agents", json=agent(
        "slugged", api_slug="inv-adm-slugged", expose_invoke=True))
    assert r.status_code == 201, r.text
    r = await client.put(f"/admin/api/agents/{r.json()['id']}",
                         json=agent("slugged", api_slug="", expose_invoke=True))
    assert r.status_code == 422, r.text
    assert "expose_invoke" in r.text
    # Even when the update does not carry the switch (the stored one stays on).
    row = await _row("slugged")
    r = await client.put(f"/admin/api/agents/{row.id}", json=agent("slugged", api_slug=""))
    assert r.status_code == 422, r.text
    assert "expose_invoke" in r.text


async def test_an_approving_bitbucket_agent_is_refused_the_invoke_api(client):
    body = agent("bb", mcp_servers=APPROVING, expose_chat=False,
                 api_slug="inv-adm-bb", expose_invoke=True)
    r = await client.post("/admin/api/agents", json=body)
    assert r.status_code == 422, r.text
    assert "expose_invoke" in r.text
    # Not exposed: stored; switching it on by update is refused as well.
    body["expose_invoke"] = False
    r = await client.post("/admin/api/agents", json=body)
    assert r.status_code == 201, r.text
    body["expose_invoke"] = True
    r = await client.put(f"/admin/api/agents/{r.json()['id']}", json=body)
    assert r.status_code == 422, r.text
    assert "expose_invoke" in r.text
    assert (await _row("bb")).expose_invoke == 0


async def test_the_string_false_is_not_true(client):
    r = await client.post("/admin/api/agents", json=agent(
        "str", api_slug="inv-adm-str", expose_invoke="false"))
    assert r.status_code == 201, r.text
    assert r.json()["expose_invoke"] is False
    assert (await _row("str")).expose_invoke == 0


async def test_update_applies_and_keeps(client):
    r = await client.post("/admin/api/agents", json=agent("upd", api_slug="inv-adm-upd"))
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    r = await client.put(f"/admin/api/agents/{agent_id}", json=agent(
        "upd", api_slug="inv-adm-upd", expose_invoke=True,
        result_schema=SCHEMA, invoke_limits=LIMITS))
    assert r.status_code == 200, r.text
    assert (r.json()["expose_invoke"], r.json()["result_schema"],
            r.json()["invoke_limits"]) == (True, SCHEMA, LIMITS)
    assert r.json()["reloaded"] is False
    # A client that does not carry the keys keeps what is stored.
    r = await client.put(f"/admin/api/agents/{agent_id}",
                         json=agent("upd", api_slug="inv-adm-upd", description="e"))
    assert r.status_code == 200, r.text
    assert (r.json()["expose_invoke"], r.json()["result_schema"],
            r.json()["invoke_limits"]) == (True, SCHEMA, LIMITS)
    # An explicit null / false clears.
    r = await client.put(f"/admin/api/agents/{agent_id}", json=agent(
        "upd", api_slug="inv-adm-upd", expose_invoke=False,
        result_schema=None, invoke_limits=None))
    assert r.status_code == 200, r.text
    assert (r.json()["expose_invoke"], r.json()["result_schema"],
            r.json()["invoke_limits"]) == (False, None, None)
    row = await _row("upd")
    assert (row.expose_invoke, row.result_schema_json, row.invoke_limits_json) == (0, None, None)


async def test_limits_are_stored_cleaned(client):
    r = await client.post("/admin/api/agents", json=agent(
        "clean", invoke_limits={"max_files": 3, "unknown": SECRET,
                                "media_types": ["image/png", "image/png"]}))
    assert r.status_code == 201, r.text
    assert r.json()["invoke_limits"] == {"max_files": 3, "media_types": ["image/png"]}
    assert SECRET not in ((await _row("clean")).invoke_limits_json or "")


# --- import ------------------------------------------------------------------

async def test_import_applies_them_and_export_import_is_symmetric(client):
    r = await client.post("/admin/api/agents", json=agent(
        "exp", api_slug="inv-adm-exp", expose_invoke=True,
        result_schema=SCHEMA, invoke_limits=LIMITS))
    assert r.status_code == 201, r.text
    exported = next(a for a in (await client.get("/admin/api/export")).json()["agents"]
                    if a["name"] == PREFIX + "exp")
    assert (exported["expose_invoke"], exported["result_schema"],
            exported["invoke_limits"]) == (True, SCHEMA, LIMITS)
    await client.delete(f"/admin/api/agents/{r.json()['id']}?force=true")

    r = await client.post("/admin/api/import", json={"agents": [exported]})
    assert r.status_code == 200, r.text
    again = next(a for a in (await client.get("/admin/api/export")).json()["agents"]
                 if a["name"] == PREFIX + "exp")
    assert again == exported

    # A bundle without the keys keeps what is stored.
    legacy = {k: v for k, v in exported.items()
              if k not in ("expose_invoke", "result_schema", "invoke_limits")}
    legacy["description"] = "legacy"
    r = await client.post("/admin/api/import", json={"agents": [legacy]})
    assert r.status_code == 200, r.text
    row = await _row("exp")
    assert row.description == "legacy"
    assert row.expose_invoke == 1
    assert json.loads(row.result_schema_json) == SCHEMA
    assert json.loads(row.invoke_limits_json) == LIMITS


@pytest.mark.parametrize("bad, field", [
    ({"result_schema": {"type": "object", "oneOf": [SECRET]}}, "result_schema"),
    ({"invoke_limits": {"max_file_bytes": SECRET}}, "invoke_limits"),
])
async def test_import_refuses_a_bad_value_naming_the_field(client, bad, field):
    r = await client.post("/admin/api/import", json={"agents": [agent("imp", **bad)]})
    _refusal(r, field)
    assert await _row("imp") is None


async def test_import_refuses_expose_invoke_without_slug_or_on_an_approver(client):
    r = await client.post("/admin/api/import", json={"agents": [
        agent("imp-noslug", expose_invoke=True)]})
    assert r.status_code == 422, r.text
    assert "expose_invoke" in r.text
    r = await client.post("/admin/api/import", json={"agents": [
        agent("imp-bb", mcp_servers=APPROVING, expose_chat=False,
              api_slug="inv-adm-imp-bb", expose_invoke=True)]})
    assert r.status_code == 422, r.text
    assert "expose_invoke" in r.text
    assert await _row("imp-noslug") is None and await _row("imp-bb") is None


# --- scripts/import_bundle.py -------------------------------------------------

def _script():
    spec = importlib.util.spec_from_file_location(
        "import_bundle_script", ROOT / "scripts" / "import_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_import_script_applies_them_and_fails_a_refused_agent(client, capsys):
    script = _script()
    failed = await script.import_bundle({"agents": [
        agent("scr-ok", api_slug="inv-adm-scr-ok", expose_invoke=True,
              result_schema=SCHEMA, invoke_limits=LIMITS),
        agent("scr-bad", result_schema={"type": "object", "if": SECRET}),
    ]})
    out = capsys.readouterr().out
    assert failed == [PREFIX + "scr-bad"]
    assert "result_schema" in out and SECRET not in out
    row = await _row("scr-ok")
    assert row.expose_invoke == 1
    assert json.loads(row.result_schema_json) == SCHEMA
    assert json.loads(row.invoke_limits_json) == LIMITS
    assert await _row("scr-bad") is None
    # The JSON boolean false switches it off (a string is refused: below).
    failed = await script.import_bundle({"agents": [
        agent("scr-ok", api_slug="inv-adm-scr-ok", expose_invoke=False)]})
    assert failed == []
    assert (await _row("scr-ok")).expose_invoke == 0


@pytest.mark.parametrize("value", ["true", "false", 1, None])
async def test_the_import_script_refuses_a_non_boolean_switch(client, capsys, value):
    script = _script()
    failed = await script.import_bundle({"agents": [
        agent("scr-str", api_slug="inv-adm-scr-str", expose_invoke=value)]})
    out = capsys.readouterr().out
    assert failed == [PREFIX + "scr-str"]
    assert script.EXPOSE_INVOKE_NOT_BOOLEAN in out
    assert await _row("scr-str") is None


async def test_the_import_script_imports_a_valid_agent_after_a_refused_one(client, capsys):
    """After the refused agent's rollback the session's instances are
    expired; the next upsert must still write."""
    script = _script()
    # An existing row, so the session holds an instance the rollback expires.
    r = await client.post("/admin/api/agents", json=agent("scr-old"))
    assert r.status_code == 201, r.text
    failed = await script.import_bundle({"agents": [
        agent("scr-old", invoke_limits={"max_files": 999}),
        agent("scr-after", api_slug="inv-adm-scr-after", expose_invoke=True,
              result_schema=SCHEMA),
        agent("scr-old", description="second write after the rollback"),
    ]})
    assert failed == [PREFIX + "scr-old"]
    assert "invoke_limits" in capsys.readouterr().out
    after = await _row("scr-after")
    assert after is not None and after.expose_invoke == 1
    assert json.loads(after.result_schema_json) == SCHEMA
    assert (await _row("scr-old")).description == "second write after the rollback"


async def test_update_without_the_switch_refuses_an_approving_entry_when_stored_on(client):
    r = await client.post("/admin/api/agents", json=agent(
        "keep-on", api_slug="inv-adm-keep-on", expose_invoke=True))
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    before = await _row("keep-on")
    body = agent("keep-on", api_slug="inv-adm-keep-on", mcp_servers=APPROVING,
                 expose_chat=False)
    assert "expose_invoke" not in body
    r = await client.put(f"/admin/api/agents/{agent_id}", json=body)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == BITBUCKET_APPROVER_INVOKE_MESSAGE
    after = await _row("keep-on")
    assert after.expose_invoke == 1
    assert after.mcp_servers == before.mcp_servers
    assert after.expose_chat == before.expose_chat
