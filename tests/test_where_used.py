"""GET /admin/api/agents/{id}/where-used: who refers to an agent.

The endpoint is the display-side sibling of the delete/disable guard
(``agent_referrers``): it lists peers and workflow steps *including disabled
referrers*, each carrying its ``enabled`` flag, so an operator sees a
switched-off workflow that will need the agent again the day it is switched
back on. The guard itself is covered by test_admin_integrity.py and is left
untouched here.

Each test creates what it needs under a ``wu-`` prefix and removes it, so it
can share a database with the other pytest modules.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "DATABASE_URL", f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_where_used.db'}"
)
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ["MCP_URL_ALLOWLIST"] = ""
os.environ.setdefault("AICORE_AVAILABLE_MODELS", "gpt-4o")

from httpx import ASGITransport, AsyncClient  # noqa: E402

import agents.auth as auth  # noqa: E402
import app as app_module  # noqa: E402
from agents.db import SessionLocal, agent_referrers, init_db  # noqa: E402

SERVERS = [{"url": "https://wu.example.com/mcp", "auth_mode": "none"}]


def agent(name: str, **extra) -> dict:
    return {"name": name, "description": f"{name} d", "instructions": f"{name} i",
            "mcp_servers": SERVERS, **extra}


def step(agent_name: str, position: int, branch_key: str | None = None, **extra) -> dict:
    return {"branch_key": branch_key, "position": position, "agent_name": agent_name,
            "instructions": "x", **extra}


@pytest.fixture
async def client():
    await init_db()
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
        r = await c.get("/admin/api/workflows")
        for w in r.json():
            if w["name"].startswith("wu-"):
                await c.delete(f"/admin/api/workflows/{w['id']}")
        r = await c.get("/admin/api/agents")
        for a in r.json():
            if a["name"].startswith("wu-"):
                await c.delete(f"/admin/api/agents/{a['id']}?force=true")


async def _ids(c: AsyncClient) -> dict[str, int]:
    return {a["name"]: a["id"] for a in (await c.get("/admin/api/agents")).json()}


async def _create(c: AsyncClient, path: str, body: dict) -> int:
    r = await c.post(path, json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_unused_agent_reports_empty_lists(client):
    target = await _create(client, "/admin/api/agents", agent("wu-lonely"))
    r = await client.get(f"/admin/api/agents/{target}/where-used")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "agent": {"id": target, "name": "wu-lonely"},
        "peers": [],
        "workflows": [],
    }


async def test_peers_and_workflows_including_disabled_ones_are_listed(client):
    target = await _create(client, "/admin/api/agents", agent("wu-target"))
    await _create(client, "/admin/api/agents", agent("wu-other"))
    caller = await _create(client, "/admin/api/agents", agent("wu-caller", peers=["wu-target"]))
    caller_off = await _create(
        client, "/admin/api/agents", agent("wu-caller-off", peers=["wu-target"], enabled=False)
    )
    # A peer list naming someone else is not a reference to the target.
    await _create(client, "/admin/api/agents", agent("wu-bystander", peers=["wu-other"]))

    # Main line position 1 and branch "b" position 2 name the target; the
    # branch's first step names another agent, so positions are per branch.
    wf = await _create(client, "/admin/api/workflows", {
        "name": "wu-wf", "api_slug": "wu-wf",
        "branches": [{"key": "b", "description": "b", "position": 1}],
        "steps": [
            step("wu-target", 1, fan_out=True),
            step("wu-other", 1, "b"),
            step("wu-target", 2, "b"),
        ],
    })
    wf_off = await _create(client, "/admin/api/workflows", {
        "name": "wu-wf-off", "enabled": False,
        "steps": [step("wu-target", 1)],
    })
    # A workflow that never names the target does not appear.
    await _create(client, "/admin/api/workflows", {
        "name": "wu-wf-unrelated", "steps": [step("wu-other", 1)],
    })

    r = await client.get(f"/admin/api/agents/{target}/where-used")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent"] == {"id": target, "name": "wu-target"}
    assert body["peers"] == [
        {"id": caller, "name": "wu-caller", "enabled": True},
        {"id": caller_off, "name": "wu-caller-off", "enabled": False},
    ]
    assert body["workflows"] == [
        {
            "id": wf, "name": "wu-wf", "api_slug": "wu-wf", "enabled": True,
            # Main line first, then the branch step -- one entry per step.
            "steps": [{"position": 1, "branch_key": None},
                      {"position": 2, "branch_key": "b"}],
        },
        {
            "id": wf_off, "name": "wu-wf-off", "api_slug": None, "enabled": False,
            "steps": [{"position": 1, "branch_key": None}],
        },
    ]

    # The guard keeps its narrower view: enabled referrers only, by name.
    async with SessionLocal() as session:
        peers, workflows = await agent_referrers(session, "wu-target")
    assert peers == ["wu-caller"]
    assert workflows == ["wu-wf"]


async def test_a_workflow_step_is_matched_by_agent_name_not_by_prefix(client):
    target = await _create(client, "/admin/api/agents", agent("wu-name"))
    await _create(client, "/admin/api/agents", agent("wu-name-longer"))
    await _create(client, "/admin/api/workflows", {
        "name": "wu-wf-prefix", "steps": [step("wu-name-longer", 1)],
    })
    r = await client.get(f"/admin/api/agents/{target}/where-used")
    assert r.status_code == 200
    assert r.json()["workflows"] == []


async def test_unknown_agent_is_404(client):
    r = await client.get("/admin/api/agents/999999/where-used")
    assert r.status_code == 404
    assert r.json()["detail"] == "Agent not found"


# --- admin scope -------------------------------------------------------------
XSAPPNAME = "pydantic-agent-test!t1"
TOKENS = {
    "user-tok": {"sub": "u-1", "user_uuid": "u-1", "scope": [f"{XSAPPNAME}.user"]},
    "admin-tok": {"sub": "u-2", "user_uuid": "u-2",
                  "scope": [f"{XSAPPNAME}.user", f"{XSAPPNAME}.admin"]},
}


class _FakeValidator:
    """The same stand-in tests/test_auth_middleware.py installs: claims are
    looked up by token string, scopes are checked the XSUAA way."""

    xsappname = XSAPPNAME
    client_id = f"sb-{XSAPPNAME}"

    def validate(self, token: str) -> dict:
        claims = TOKENS.get(token)
        if claims is None:
            raise HTTPException(status_code=401, detail="Invalid JWT: fake")
        return dict(claims)

    def has_scope(self, payload: dict, scope: str) -> bool:
        scopes = payload.get("scope") or []
        return f"{self.xsappname}.{scope}" in scopes or scope in scopes


@pytest.fixture
def cf():
    """Run the app as if on CF with an XSUAA binding (fake validator)."""
    saved = (auth._validator, auth._validator_checked, app_module.ON_CF)
    auth._validator, auth._validator_checked = _FakeValidator(), True  # type: ignore[assignment]
    app_module.ON_CF = True
    try:
        yield
    finally:
        auth._validator, auth._validator_checked, app_module.ON_CF = saved


async def test_where_used_requires_the_admin_scope(client, cf):
    # Creation itself needs the admin scope once the validator is on, so
    # everything in this test goes through explicit bearer headers.
    admin = {"Authorization": "Bearer admin-tok"}
    r = await client.post("/admin/api/agents", json=agent("wu-scoped"), headers=admin)
    assert r.status_code == 201, r.text
    target = r.json()["id"]

    r = await client.get(f"/admin/api/agents/{target}/where-used")
    assert r.status_code == 401, r.text

    r = await client.get(f"/admin/api/agents/{target}/where-used",
                         headers={"Authorization": "Bearer user-tok"})
    assert r.status_code == 403, r.text

    r = await client.get(f"/admin/api/agents/{target}/where-used", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["agent"]["name"] == "wu-scoped"

    # Cleanup runs after the fixture is torn down (no validator), but the
    # agent is removed here as well so the scope fixture never has to.
    r = await client.delete(f"/admin/api/agents/{target}", headers=admin)
    assert r.status_code == 204, r.text
