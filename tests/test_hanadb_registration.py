"""``builtin:hana`` at the admin gate and in storage: the storage cleaner,
the gate's 422 texts (field and rule, never the value), one entry per agent,
the canonical URL, the refusal of other auth modes and of the hana keys on
another entry, and the gate's block equal to storage's.

The toolset factory and the registry build come in a later task; until
``builtin:hana`` is in ``agents.builtins._FACTORIES`` the fixture below adds
the URL to ``BUILTIN_URLS`` for these tests only (a no-op afterwards).

Run:  python -m pytest tests/test_hanadb_registration.py
"""

from __future__ import annotations

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
os.environ.pop("MCP_URL_ALLOWLIST", None)

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

URL = "builtin:hana"
SECRET = "Zx9secret"
BASE = {"systems": [{"name": "prd", "destination": "HANA_PRD"}]}
FULL = {"systems": [{"name": "prd", "destination": "HANA_PRD"},
                    {"name": "sysdb", "destination": "HANA.SYS-DB"}],
        "allow_select": True}
ONE_ENTRY = "an agent may have at most one builtin:hana entry"
MODE_TEXT = "builtin:hana requires auth_mode=destination"
NOTES = {"url": "builtin:sapnotes", "auth_mode": "none"}
GIT = {"url": "builtin:git", "auth_mode": "destination",
       "oauth": {"destination": "GITHUB", "provider": "github", "repositories": ["acme/web"]}}
BITBUCKET = {"url": "builtin:bitbucket", "auth_mode": "destination",
             "oauth": {"destination": "BB", "workspace": "acme-ws",
                       "repositories": ["svc-a"]}}


@pytest.fixture(autouse=True)
def hana_is_known(monkeypatch):
    import agents.builtins as builtins

    if URL not in builtins.BUILTIN_URLS:
        monkeypatch.setattr(builtins, "BUILTIN_URLS", builtins.BUILTIN_URLS | {URL})


def _server(block: Any, url: str = URL, mode: str = "destination") -> dict[str, Any]:
    return {"url": url, "auth_mode": mode, "oauth": block}


def _copy(block: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(block))


# --- storage ------------------------------------------------------------------

@pytest.mark.parametrize("block", [BASE, FULL])
def test_storage_keeps_exactly_the_checked_block(block):
    from agents.db import _clean_oauth

    assert _clean_oauth(_copy(block), "destination", None, url="Builtin:HANA/") == block


def test_storage_stores_allow_select_only_as_true_and_no_echoed_key():
    from agents.db import _clean_oauth

    stored = _clean_oauth({**_copy(BASE), "allow_select": False,
                           "has_client_secret": False, "user_context": False},
                          "destination", None, url=URL)
    assert stored == BASE and "allow_select" not in stored
    stored = _clean_oauth({**_copy(BASE), "allow_select": True}, "destination", None, url=URL)
    assert stored["allow_select"] is True


@pytest.mark.parametrize("change, field", [
    ({"user_context": True}, "oauth.user_context"),
    ({"allow_select": "true"}, "oauth.allow_select"),
    ({"allow_select": 1}, "oauth.allow_select"),
    ({"destination": SECRET}, "oauth.destination"),
    ({"systems": []}, "oauth.systems"),
    ({"systems": [{"name": SECRET, "destination": "D"}]}, "oauth.systems[0].name"),
    ({"systems": [{"name": "prd", "destination": f" {SECRET}"}]},
     "oauth.systems[0].destination"),
    ({"client_secret": SECRET}, "oauth: a builtin:hana entry holds only"),
    ({SECRET: 1}, "oauth: a builtin:hana entry holds only"),
])
def test_storage_refuses_by_itself_and_names_no_value(change, field):
    from agents.db import _clean_oauth

    with pytest.raises(ValueError) as info:
        _clean_oauth({**_copy(BASE), **change}, "destination", None, url=URL)
    assert str(info.value).startswith(field) and SECRET not in str(info.value)


@pytest.mark.parametrize("mode", ["app_only", "oauth2", "jwt", "none"])
def test_storage_refuses_every_other_auth_mode(mode):
    from agents.db import prepare_servers

    with pytest.raises(ValueError, match="requires auth_mode=destination"):
        prepare_servers([_server(_copy(BASE), mode=mode)], None)


@pytest.mark.parametrize("oauth", [None, [], "x"])
def test_storage_refuses_an_entry_without_a_config_object(oauth):
    from agents.db import prepare_servers

    with pytest.raises(ValueError, match="builtin:hana entry needs a config object"):
        prepare_servers([_server(oauth)], None)
    with pytest.raises(ValueError, match="builtin:hana entry needs a config object"):
        prepare_servers([{"url": URL, "auth_mode": "destination"}], None)


@pytest.mark.parametrize("spelling", [URL, "builtin:hana/", "Builtin:HANA/", " BUILTIN:HANA "])
def test_storage_stores_the_url_in_the_canonical_spelling(spelling):
    from agents.db import prepare_servers

    primary, _, oauth_json = prepare_servers([_server(_copy(BASE), spelling)], None)
    assert primary["url"] == URL and json.loads(oauth_json) == BASE


TWICE = [(URL, URL), (URL, "builtin:hana/"), ("Builtin:HANA", URL)]


def _two(first: str, second: str) -> list[dict[str, Any]]:
    other = {"systems": [{"name": "zx9", "destination": "ZX9_OTHER"}], "allow_select": True}
    return [_server(_copy(BASE), first), _server(other, second)]


@pytest.mark.parametrize("first, second", TWICE)
def test_storage_refuses_a_second_hana_entry_in_any_spelling(first, second):
    from agents.db import prepare_servers

    with pytest.raises(ValueError) as refused:
        prepare_servers(_two(first, second), None)
    assert str(refused.value).startswith(ONE_ENTRY) and "zx9" not in str(refused.value)
    with pytest.raises(ValueError, match="^" + ONE_ENTRY):
        prepare_servers([_two(first, second)[0], NOTES, _two(first, second)[1]], None)


@pytest.mark.parametrize("first, second", TWICE)
def test_the_gate_refuses_a_second_hana_entry_in_any_spelling(first, second):
    from agents.admin import AgentPayload

    with pytest.raises(ValidationError) as refused:
        AgentPayload.model_validate({"name": "hana-two", "description": "d",
                                     "instructions": "i", "mcp_servers": _two(first, second)})
    text = json.dumps(refused.value.errors(include_input=False, include_context=False,
                                           include_url=False))
    assert ONE_ENTRY in text and "zx9" not in text.replace(ONE_ENTRY, "")


def test_the_single_entry_message_is_the_one_of_the_config_module():
    from agents.db import HANA_SINGLE_ENTRY_MESSAGE
    from agents.hana.config import HANA_SINGLE_ENTRY_MESSAGE as config_message

    assert HANA_SINGLE_ENTRY_MESSAGE is config_message
    assert HANA_SINGLE_ENTRY_MESSAGE.startswith(ONE_ENTRY)


def test_one_hana_entry_next_to_a_git_and_a_bitbucket_entry_is_stored():
    from agents.db import prepare_servers

    _, extras, _ = prepare_servers([NOTES, _server(_copy(FULL)), _copy(GIT),
                                    _copy(BITBUCKET)], None)
    assert [e["url"] for e in extras] == [URL, "builtin:git", "builtin:bitbucket"]
    assert extras[0]["oauth"] == FULL


# --- the admin gate -----------------------------------------------------------

def _payload(oauth: Any, mode: str = "destination", url: str = URL):
    from agents.admin import McpServerPayload

    return McpServerPayload.model_validate({"url": url, "auth_mode": mode, "oauth": oauth})


def _refusal(oauth: Any, mode: str = "destination", url: str = URL) -> str:
    with pytest.raises(ValidationError) as info:
        _payload(oauth, mode, url)
    msgs = [e["msg"] for e in info.value.errors()]
    assert all(SECRET not in m for m in msgs)
    return " | ".join(msgs)


@pytest.mark.parametrize("block", [BASE, FULL])
def test_the_gate_accepts_an_entry_and_hands_storage_the_same_block(block):
    assert _payload(_copy(block)).oauth.to_config() == block


@pytest.mark.parametrize("block", [BASE, FULL])
def test_the_gates_block_round_trips_through_storage(block):
    from agents.admin import AgentPayload
    from agents.db import prepare_servers

    payload = AgentPayload.model_validate({
        "name": "hana-reader", "description": "d", "instructions": "i",
        "mcp_servers": [NOTES, _server(_copy(block), "Builtin:HANA/"), _copy(GIT)]})
    servers = payload.to_servers_list()
    sent = next(s for s in servers if s["url"] == URL)
    assert sent["oauth"] == block
    _, extras, _ = prepare_servers(servers, None)
    assert next(e for e in extras if e["url"] == URL)["oauth"] == block


def test_the_gate_stores_allow_select_false_as_absent():
    assert _payload({**_copy(BASE), "allow_select": False}).oauth.to_config() == BASE
    assert _payload({**_copy(BASE), "allow_select": None}).oauth.to_config() == BASE


@pytest.mark.parametrize("change, said", [
    ({"systems": None}, "oauth.systems: required, a list of 1 to 10"),
    ({"systems": []}, "oauth.systems: required, a list of 1 to 10"),
    ({"systems": [SECRET]}, "oauth.systems[0]: each system is an object"),
    ({"systems": [{"name": SECRET, "destination": "D"}]}, "oauth.systems[0].name: required"),
    ({"systems": [{"name": "prd", "destination": f"{SECRET} "}]},
     "oauth.systems[0].destination: required, a destination name"),
    ({"systems": [{"name": "prd", "destination": "D", "host": SECRET}]},
     "oauth.systems[0]: a system holds only name, destination"),
    ({"systems": [{"name": "prd", "destination": "D"}, {"name": "prd", "destination": "E"}]},
     "oauth.systems[1].name: duplicate"),
    ({"allow_select": "true"}, "oauth.allow_select: must be the JSON boolean"),
    ({"allow_select": 1}, "oauth.allow_select: must be the JSON boolean"),
    ({"user_context": True}, "oauth.user_context: builtin:hana has no signed-in user"),
    ({"destination": SECRET}, "oauth.destination: a builtin:hana entry has no destination"),
    ({SECRET: "x"}, "oauth: a builtin:hana entry holds only"),
    ({"client_secret": SECRET}, "oauth: a builtin:hana entry holds only"),
    ({"workspace": "acme"}, "oauth: a builtin:hana entry holds only"),
    ({"provider": "github"}, "oauth: a builtin:hana entry holds only"),
])
def test_the_gate_refuses_and_names_the_field_never_the_value(change, said):
    assert said in _refusal({**_copy(BASE), **change})


@pytest.mark.parametrize("oauth", [None, [], "x"])
def test_the_gate_refuses_an_entry_without_a_config_object(oauth):
    assert "builtin:hana entry needs a config object" in _refusal(oauth)


def test_the_gate_refuses_an_entry_with_no_oauth_key():
    from agents.admin import McpServerPayload

    with pytest.raises(ValidationError) as info:
        McpServerPayload.model_validate({"url": URL, "auth_mode": "destination"})
    assert "builtin:hana entry needs a config object" in str(info.value)


@pytest.mark.parametrize("mode", ["app_only", "oauth2", "jwt", "none", "session"])
def test_the_gate_refuses_every_other_auth_mode(mode):
    text = _refusal(_copy(BASE), mode)
    assert MODE_TEXT in text and "each database only through the BTP destination" in text


@pytest.mark.parametrize("url, block", [
    ("builtin:jira", {"destination": "J", "project": "P", "allow_select": True}),
    ("builtin:jira", {"destination": "J", "project": "P",
                      "systems": [{"name": "prd", "destination": "D"}]}),
    ("builtin:slack", {"destination": "S", "allow_select": True}),
])
def test_the_hana_keys_belong_to_a_hana_entry_only(url, block):
    assert "belong to a builtin:hana entry only" in _refusal(block, url=url)


def test_a_git_entry_with_a_hana_key_is_refused_by_its_own_reading():
    assert "builtin:git entry holds only" in _refusal(
        {**GIT["oauth"], "allow_select": True}, url="builtin:git")
