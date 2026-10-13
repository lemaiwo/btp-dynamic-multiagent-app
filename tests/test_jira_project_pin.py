"""A ``builtin:jira`` entry must pin its project (review finding B-int-1).

Without ``project`` the JQL has no ``project =`` clause and ``confine_key``
accepts any key, so the agent reaches every issue the destination's
credential can see. The admin gate refuses a new or edited entry without the
pin; a row stored before the rule still builds (a landscape may hold one),
with one WARNING.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx  # noqa: E402
import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from agents.admin import AgentPayload, McpServerPayload, OAuthClientPayload  # noqa: E402
from agents.jira_tools import confine_key, jira_toolset  # noqa: E402

DEST_ENV = {
    "DESTINATION_CLIENT_ID": "client",
    "DESTINATION_CLIENT_SECRET": "secret",
    "DESTINATION_TOKEN_URL": "https://uaa.example/oauth/token",
    "DESTINATION_URI": "https://dest.example",
}


@pytest.fixture
def dest_env(monkeypatch):
    for key, value in DEST_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("VCAP_SERVICES", raising=False)


def _jira(**oauth) -> McpServerPayload:
    return McpServerPayload(
        url="builtin:jira", auth_mode="destination",
        oauth=OAuthClientPayload(destination="MY_JIRA", **oauth),
    )


@pytest.mark.parametrize("project", [None, "", "   "])
def test_the_gate_refuses_a_jira_entry_without_a_project(project):
    kwargs = {} if project is None else {"project": project}
    with pytest.raises(ValidationError) as err:
        _jira(**kwargs)
    text = str(err.value)
    assert "oauth.project" in text


def test_the_refusal_names_the_field_not_the_value():
    with pytest.raises(ValidationError) as err:
        McpServerPayload(
            url="builtin:jira", auth_mode="destination",
            oauth=OAuthClientPayload(destination="SECRET_DEST_NAME", status="Waiting"),
        )
    text = str(err.value)
    assert "oauth.project" in text
    assert "SECRET_DEST_NAME" not in text.split("input_value")[0]


def test_the_gate_accepts_a_jira_entry_with_a_project():
    payload = _jira(project="ABC")
    assert payload.oauth.to_config()["project"] == "ABC"


def test_trailing_slash_and_case_spellings_are_held_to_the_rule():
    with pytest.raises(ValidationError):
        McpServerPayload(
            url=" Builtin:Jira/ ", auth_mode="destination",
            oauth=OAuthClientPayload(destination="MY_JIRA"),
        )


def test_an_agent_save_with_a_projectless_jira_entry_is_refused():
    with pytest.raises(ValidationError) as err:
        AgentPayload.model_validate({
            "name": "helpdesk",
            "description": "d",
            "instructions": "i",
            "mcp_servers": [{
                "url": "builtin:jira", "auth_mode": "destination",
                "oauth": {"destination": "MY_JIRA"},
            }],
        })
    assert "oauth.project" in str(err.value)


def test_other_builtins_do_not_need_a_project():
    McpServerPayload(
        url="builtin:slack", auth_mode="destination",
        oauth=OAuthClientPayload(destination="MY_SLACK"),
    )


def _build(oauth):
    return jira_toolset(
        oauth,
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        auth_mode="destination",
    )


def test_a_stored_entry_without_a_project_still_builds_with_one_warning(dest_env, caplog):
    with caplog.at_level(logging.WARNING, logger="agents.jira_tools"):
        toolset = _build({"destination": "MY_JIRA"})
    assert toolset is not None
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and r.name == "agents.jira_tools"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "project" in message


def test_the_build_warning_names_the_agent_when_known(dest_env, caplog):
    with caplog.at_level(logging.WARNING, logger="agents.jira_tools"):
        jira_toolset(
            {"destination": "MY_JIRA"},
            http=httpx.AsyncClient(),
            auth_mode="destination",
            agent_name="helpdesk",
        )
    messages = [r.getMessage() for r in caplog.records if r.name == "agents.jira_tools"]
    assert len(messages) == 1 and "helpdesk" in messages[0]


def test_an_entry_with_a_project_builds_without_a_warning(dest_env, caplog):
    with caplog.at_level(logging.WARNING, logger="agents.jira_tools"):
        _build({"destination": "MY_JIRA", "project": "ABC"})
    assert not [r for r in caplog.records if r.name == "agents.jira_tools"]


def test_confine_key_without_a_pin_still_accepts_any_project():
    # Kept for the stored rows that predate the rule; the docstring says so.
    assert confine_key("ZZZ-77", "") == "ZZZ-77"
    with pytest.raises(ValueError):
        confine_key("ZZZ-77", "ABC")
    assert "no pin" in (confine_key.__doc__ or "").lower()
