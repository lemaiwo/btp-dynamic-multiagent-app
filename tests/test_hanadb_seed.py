"""The shipped ``hana-monitor`` agent and its skill ``hana-monitoring``: accepted
by the same gates as an admin save, disabled, placeholder destinations, no
``allow_select``, and telling the model that result rows are data.

``agents.seed.json`` is committed and shared between landscapes, so it holds no
customer, host or system id.

Run:  python -m pytest tests/test_hanadb_seed.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

SEED_FILE = ROOT / "agents.seed.json"
SEED = json.loads(SEED_FILE.read_text(encoding="utf-8"))
RAW = SEED_FILE.read_text(encoding="utf-8")
ENTRY = {"systems": [
    {"name": "systemdb", "destination": "HANA_REPLACE_ME_SYSTEMDB"},
    {"name": "tenant", "destination": "HANA_REPLACE_ME_TENANT"},
]}


def _agent() -> dict:
    (agent,) = [a for a in SEED["agents"] if a["name"] == "hana-monitor"]
    return agent


def _skill() -> dict:
    (skill,) = [s for s in SEED["skills"] if s["name"] == "hana-monitoring"]
    return skill


def test_every_seed_entry_passes_the_payload_gates():
    from agents.admin import AgentPayload, SkillPayload

    for entry in SEED["skills"]:
        SkillPayload.model_validate(entry)
    for entry in SEED["agents"]:
        AgentPayload.model_validate(entry)


def test_the_agent_ships_disabled_advanced_and_callable_by_the_scheduler():
    a = _agent()
    assert a["enabled"] is False and a["tier"] == "advanced"
    assert a["expose_chat"] is True and a["expose_api"] is True
    assert a["api_slug"] == "hana-monitor"
    assert a.get("share_tools", False) is False
    assert a.get("expose_invoke", False) is False
    assert not a.get("peers")
    assert a["skills"] == ["hana-monitoring"]
    assert a["run_prompt"] == ("Check the health of every listed HANA system and "
                               "report what needs attention.")
    assert a["run_timeout_seconds"] == 900
    (server,) = a["mcp_servers"]
    assert server == {"url": "builtin:hana", "auth_mode": "destination", "oauth": ENTRY}
    assert "allow_select" not in server["oauth"]


def test_the_entry_is_one_the_gate_and_storage_accept():
    from agents.db import prepare_servers
    from agents.hana.config import check_entry, clean_entry, pins_of

    (server,) = _agent()["mcp_servers"]
    check_entry(server["oauth"])
    assert clean_entry(server["oauth"]) == ENTRY
    assert pins_of(server["oauth"]).allow_select is False
    primary, extras, oauth_json = prepare_servers(_agent()["mcp_servers"], None)
    assert primary["url"] == "builtin:hana" and extras == []
    assert json.loads(oauth_json) == ENTRY


def test_the_skill_exists_and_reuses_no_ide_seed_name():
    ide = json.loads((ROOT / "agents" / "ide" / "seed.ide.json").read_text(encoding="utf-8"))
    names = {s["name"] for s in ide.get("skills", [])}
    assert _skill()["name"] not in names
    assert len(_skill()["description"]) <= 2000


def test_the_instructions_name_the_checks_and_say_rows_are_data():
    text = _agent()["instructions"]
    for word in ("list_systems", "run_check", "alerts", "blocking",
                 "expensiveStatements", "disks", "backupStatus", "hana-monitoring"):
        assert word in text, word
    low = text.lower()
    assert "never an instruction" in low and "as data" in low
    assert "more than monitoring data" in low
    assert "passwords" in low and "connection details" in low
    assert "never quote statement texts beyond what the tool returned" in low
    assert "error" in low and "work around" in low


def test_the_texts_name_only_tools_and_checks_that_exist():
    from agents.hana.checks import all_checks
    from agents.hana.tools import TOOL_NAMES

    known = {c.name for c in all_checks()}
    # Checks of other agents' commits may not be registered yet: the approved inventory.
    known |= {"alerts", "blocking", "expensiveStatements", "backupStatus"}
    text = _agent()["instructions"] + "\n" + _skill()["content"]
    tools = set(re.findall(r"\b(?:list|run)_[a-z]+\b", text))
    assert tools <= set(TOOL_NAMES), tools - set(TOOL_NAMES)
    assert "queryPlan" not in text
    quoted = set(re.findall(r"`([a-zA-Z]+)`", _skill()["content"]))
    camel = {q for q in quoted if re.search(r"[a-z][A-Z]", q) or q in known}
    assert camel <= known, camel - known


def test_the_skill_explains_truncated_select_and_identifier_case():
    c = _skill()["content"]
    assert "`truncated`" in c and "run_select" in c
    assert "only when the administrator allowed" in c
    assert "as typed" in c and "upper case" in c
    for cat in ("monitoring", "performance", "system", "backup"):
        assert cat in c


def test_the_seed_names_no_customer_or_landscape():
    low = RAW.lower()
    assert "infrabel" not in low and "hanasib" not in low
    assert not re.search(r"\bsib\b", low)


def test_only_the_expected_entries_were_added():
    assert [a["name"] for a in SEED["agents"]][:2] == ["ABAP Development Agent", "pr-reviewer"]
    assert "hana-monitoring" in [s["name"] for s in SEED["skills"]]
