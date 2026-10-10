"""Credential health answers fixed texts for every destination (finding B-int-3).

The OData branch already answered a fixed text ending in a code; the branch
for every other destination-mode server answered ``str(e)[:400]``, which can
quote the destination service's answer or a URL. The resolver's own text
goes to the log only.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest  # noqa: E402

from agents.destination import Destination, DestinationError  # noqa: E402

LEAK = "body-from-service https://dest.example/path?token=abc"


class _Row:
    def __init__(self, name, servers, enabled=True):
        self.name = name
        self.mcp_servers = servers
        self.enabled = enabled


class _NoSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def health(monkeypatch):
    import agents.admin as admin
    import agents.destination as dest_mod
    from agents.destination import DestinationServiceConfig

    def install(rows, failures):
        monkeypatch.setattr(admin, "SessionLocal", lambda: _NoSession())

        async def fake_list_agents(session):
            return rows

        monkeypatch.setattr(admin, "list_agents", fake_list_agents)
        monkeypatch.setattr(
            dest_mod, "config_from_environment",
            lambda env: DestinationServiceConfig("id", "secret", "https://uaa/t", "https://api"),
        )

        class FakeResolverCls:
            def __init__(self, name, config, **kw):
                self.name = name

            async def _answer(self):
                failure = failures.get(self.name)
                if failure is not None:
                    raise failure
                return Destination(url="https://x.example", headers={},
                                   expires_at=time.monotonic() + 60, auth_type="NoAuthentication")

            async def resolve(self, **kw):
                return await self._answer()

            async def resolve_properties(self, **kw):
                return await self._answer()

        monkeypatch.setattr(dest_mod, "DestinationResolver", FakeResolverCls)
        return admin._destination_health

    return install


def _jira(dest):
    return {"url": "builtin:jira", "auth_mode": "destination",
            "oauth": {"destination": dest, "project": "ABC"}}


async def test_a_destination_error_is_answered_as_a_fixed_text_with_a_code(health, caplog):
    run = health(
        [_Row("A", [_jira("STATUS"), _jira("GONE"), _jira("ODD")])],
        {
            "STATUS": DestinationError(
                f"destination service returned 400 for destination 'STATUS': {LEAK}"),
            "GONE": DestinationError(
                "destination 'GONE' does not exist in the subaccount of this app's "
                f"destination service; {LEAK}"),
            "ODD": DestinationError(f"something unforeseen: {LEAK}"),
        },
    )
    with caplog.at_level(logging.WARNING, logger="agents.admin"):
        out = await run()
    by_dest = {e["destination"]: e for e in out}
    assert all(e["state"] == "error" for e in out)
    assert by_dest["GONE"]["error"].endswith("(not_found)")
    assert by_dest["ODD"]["error"].endswith("(failed)")
    assert by_dest["STATUS"]["error"].endswith(")")
    text = json.dumps(out)
    assert "body-from-service" not in text and "token=abc" not in text
    # The detail is logged, not lost.
    logged = [r for r in caplog.records
              if r.name == "agents.admin" and r.levelno == logging.WARNING]
    assert len(logged) == 3
    assert any("GONE" in r.getMessage() for r in logged)


async def test_an_unexpected_exception_answers_its_class_only(health, caplog):
    run = health([_Row("A", [_jira("BOOM")])], {"BOOM": RuntimeError(LEAK)})
    with caplog.at_level(logging.WARNING, logger="agents.admin"):
        out = await run()
    (entry,) = out
    assert entry["state"] == "error"
    assert "RuntimeError" in entry["error"]
    assert "body-from-service" not in entry["error"]


async def test_the_smtp_branch_is_held_to_the_same_rule(health):
    run = health(
        [_Row("A", [{"url": "builtin:smtp", "auth_mode": "destination",
                     "oauth": {"destination": "MAIL"}}])],
        {"MAIL": DestinationError(f"destination service returned 403: {LEAK}")},
    )
    (entry,) = await run()
    assert "body-from-service" not in entry["error"]
