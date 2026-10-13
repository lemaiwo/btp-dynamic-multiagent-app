"""The session target's ARC-1 server acts as the signed-in developer.

``diagnose.is_target_server`` decides which server gets the diagnose policy
(dumps, traces, ...). Recognised by destination name alone, an ARC-1 entry
whose ``user_context`` an admin left off would run every MCP read of a
diagnose run in SAP as the destination's technical user, while the app's own
ARC-1 calls (open_object, base check, syntax check, trace arming) run as the
developer. So a server is the target's only when its ``DestinationAuth``
acts as the signed-in user, and the registry build warns about such an entry.

Run:  python -m pytest tests/test_ide_diagnose_target.py -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from pydantic_ai import ModelRetry  # noqa: E402
from pydantic_ai.models.test import TestModel  # noqa: E402

from agents.deep import DeepState, WorkspaceScope, current_workspace  # noqa: E402
from agents.destination_auth import DestinationAuth  # noqa: E402
from agents.ide import diagnose  # noqa: E402
from agents.ide.diagnose import DiagnoseRun, current_diagnose  # noqa: E402
from agents.ide.readonly import ReadOnlyGuard  # noqa: E402

pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")

DEST = "arc1-a5-target"


def _server(user_context: bool, destination: str = DEST):
    """An MCP server as ``_destination_mcp_server`` builds it, minus the rest."""
    auth = DestinationAuth(
        SimpleNamespace(name=destination), user_context=user_context
    )
    return SimpleNamespace(http_client=SimpleNamespace(auth=auth))


def _run() -> DiagnoseRun:
    return DiagnoseRun(
        session_id="s-a5", owner="alice", target="T1", run_id="r", destination=DEST
    )


def test_target_server_requires_user_context():
    assert diagnose.is_target_server(_server(user_context=True), _run()) is True
    assert diagnose.is_target_server(_server(user_context=False), _run()) is False


def test_target_server_still_requires_the_destination():
    other = _server(user_context=True, destination="another-destination")
    assert diagnose.is_target_server(other, _run()) is False


class _Arc1Stub:
    """A wrapped ARC-1 server with one ``SAPDiagnose`` tool; records calls."""

    def __init__(self, user_context: bool):
        self.http_client = _server(user_context).http_client
        self.calls: list = []

    async def get_tools(self, ctx):
        return {"SAPDiagnose": object()}

    async def call_tool(self, name, tool_args, ctx, tool):
        self.calls.append(tool_args.get("action"))
        return "[]"


@pytest.fixture
def diagnose_bound():
    scope = WorkspaceScope(
        session_id="s-a5", state=DeepState(run_id="s-a5"), session_type="diagnose"
    )
    token = current_workspace.set(scope)
    run_token = current_diagnose.set(_run())
    try:
        yield scope
    finally:
        current_diagnose.reset(run_token)
        current_workspace.reset(token)


async def test_technical_user_server_refuses_diagnose_data(diagnose_bound):
    """No user_context: the change policy applies, a dump read is refused as a
    tool error (ModelRetry), and nothing reaches ARC-1."""
    stub = _Arc1Stub(user_context=False)
    guard = ReadOnlyGuard(stub)
    with pytest.raises(ModelRetry):
        await guard.call_tool(
            "SAPDiagnose", {"action": "dumps"}, SimpleNamespace(retry=0), object()
        )
    assert stub.calls == []


async def test_user_context_server_allows_diagnose_data(diagnose_bound):
    stub = _Arc1Stub(user_context=True)
    guard = ReadOnlyGuard(stub)
    result = await guard.call_tool(
        "SAPDiagnose", {"action": "dumps"}, SimpleNamespace(retry=0), object()
    )
    assert result == "[]"
    assert stub.calls == ["dumps"]


async def test_registry_warns_once_for_technical_user_ide_target(monkeypatch, caplog):
    """An ARC-1 entry on an IDE target's destination without user_context is
    named once at build; one with user_context, or on another destination,
    is not."""
    from agents import registry as registry_module
    from agents.db import SessionLocal, delete_agent, init_db, upsert_agent
    from agents.ide.models import IdeConventions

    monkeypatch.setenv("DESTINATION_URI", "https://destination.example.test")
    monkeypatch.setenv("DESTINATION_TOKEN_URL", "https://auth.example.test/oauth/token")
    monkeypatch.setenv("DESTINATION_CLIENT_ID", "id")
    monkeypatch.setenv("DESTINATION_CLIENT_SECRET", "not-a-secret")
    monkeypatch.setattr(registry_module, "get_model", lambda *a, **k: TestModel())

    def entry(destination: str, user_context: bool | None) -> dict:
        oauth: dict = {"destination": destination}
        if user_context is not None:
            oauth["user_context"] = user_context
        return {
            "url": "https://arc1.example.test/mcp",
            "auth_mode": "destination",
            "oauth": oauth,
        }

    await init_db()
    names = ("a5-technical", "a5-user", "a5-elsewhere")
    specs = (entry(DEST, None), entry(DEST, True), entry("unrelated-destination", False))
    ids: list[int] = []
    async with SessionLocal() as s:
        s.add(IdeConventions(target="A5T", destination=DEST))
        for name, spec in zip(names, specs):
            row = await upsert_agent(s, name=name, description="d", instructions="i",
                                     mcp_servers=[spec])
            ids.append(row.id)
        await s.commit()
    try:
        with caplog.at_level("WARNING", logger="agents.registry"):
            await registry_module.build_orchestrator()
    finally:
        async with SessionLocal() as s:
            for agent_id in ids:
                await delete_agent(s, agent_id)
            row = await s.get(IdeConventions, "A5T")
            if row is not None:
                await s.delete(row)
            await s.commit()

    hits = [r.getMessage() for r in caplog.records if "user_context" in r.getMessage()]
    assert len(hits) == 1, hits
    assert names[0] in hits[0] and DEST in hits[0]
    assert names[1] not in hits[0] and names[2] not in hits[0]
