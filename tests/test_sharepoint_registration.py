"""``builtin:sharepoint`` as a registered built-in: factory, storage, the
save-time gate, credential health, and that an ABAP Assistant session never
gets its tools.

The entry decides what an agent may read, so three things are pinned here on
top of "it is accepted": a refusal never repeats what the admin typed, a pin
is stored in exactly the form that was checked (never repaired on the way),
and the row holds only the entry's own keys.

Run:  python -m pytest tests/test_sharepoint_registration.py
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ.pop("MCP_URL_ALLOWLIST", None)
for _var in ("DESTINATION_CLIENT_ID", "DESTINATION_CLIENT_SECRET",
             "DESTINATION_URI", "DESTINATION_TOKEN_URL", "DESTINATION_UAA_URL"):
    os.environ.pop(_var, None)

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from agents.destination import Destination, DestinationError  # noqa: E402
from tests.sharepoint_helpers import PINS, VIEWS  # noqa: E402

URL = "builtin:sharepoint"
DEST = {"destination": "GRAPH", **PINS, "views": VIEWS}
TOKEN_URL = "https://login.example.com/tenant/oauth2/v2.0/token"
APP_ONLY = {"client_id": "cid", "client_secret": "s3cret-Value_1",
            "token_url": TOKEN_URL,
            "scope": "https://graph.example.com/.default", **PINS, "views": VIEWS}
# Never part of an answer or a log line: what a refused entry carried. Upper
# case and punctuation keep it from being a legal view name, site or status.
SECRET = "S3cr3t_Zx9!tok"


# --- factory ----------------------------------------------------------------

def test_sharepoint_is_a_known_builtin():
    from agents.builtins import BUILTIN_URLS, is_builtin_url

    assert URL in BUILTIN_URLS
    assert is_builtin_url("BUILTIN:SharePoint")


def test_the_factory_builds_it_and_names_the_server_without_a_binding():
    from agents.builtins import build_builtin_toolset

    toolset = build_builtin_toolset(URL, APP_ONLY, "app_only")
    assert set(toolset.tools) == {"read_table", "read_calendar"}
    with pytest.raises(DestinationError, match=f"{URL}: no destination service binding"):
        build_builtin_toolset(URL, DEST, "destination")
    with pytest.raises(ValueError, match="requires a 'destination'"):
        build_builtin_toolset(URL, {**PINS, "views": VIEWS}, "destination")


@pytest.mark.parametrize("mode, block", [("destination", DEST), ("app_only", APP_ONLY)])
def test_the_factory_refuses_user_context_in_both_modes(mode, block):
    from agents.builtins import build_builtin_toolset

    with pytest.raises(ValueError, match="no per-user mode"):
        build_builtin_toolset(URL, {**block, "user_context": True}, mode)


# --- storage ----------------------------------------------------------------

def test_storage_keeps_exactly_the_entrys_own_keys_on_a_destination():
    from agents.db import _clean_oauth

    posted = {**DEST, "destination": " GRAPH ", "user_context": True, "allow_send": True,
              "allow_comment": True, "allow_write": True, "services": ["s"],
              "mailbox": "a@example.com", "team": "t", "client_id": "cid",
              "client_secret": "hunter2", "theme": {"band": "#000000"},
              "lookback": "2d", "recipients": "a@example.com", "something": "else"}
    assert _clean_oauth(posted, "destination", None, url=URL) == {
        "destination": "GRAPH", **PINS, "views": VIEWS}


def test_storage_on_app_only_keeps_the_credential_and_preserves_a_blank_secret():
    from agents.db import _clean_oauth

    stored = _clean_oauth({**APP_ONLY, "mailbox": "a@example.com", "allow_send": True,
                           "user_context": True, "destination": "GRAPH", "team": "t",
                           "lookback": "2d", "recipients": "a@example.com"},
                          "app_only", None, url="Builtin:SharePoint/")
    assert stored == APP_ONLY
    again = _clean_oauth({**APP_ONLY, "client_secret": ""}, "app_only", stored, url=URL)
    assert again == APP_ONLY


def _bad_views() -> list[tuple[str, Any]]:
    """Refused ``views`` values, each carrying SECRET where an admin could
    have typed something, with the field the refusal must name."""
    def planning(**change: Any) -> dict[str, Any]:
        views = copy.deepcopy(VIEWS)
        views["planning"].update(change)
        return views

    def team(**change: Any) -> dict[str, Any]:
        views = copy.deepcopy(VIEWS)
        views["team"].update(change)
        return views

    p = "oauth.views.planning"
    return [
        ("oauth.views", None),
        ("oauth.views", {}),
        ("oauth.views", {SECRET: VIEWS["team"]}),
        ("oauth.views.team", {"team": SECRET}),
        ("oauth.views.team.kind", {"team": {"kind": SECRET}}),
        ("oauth.views.team", team(**{SECRET: 1})),
        ("oauth.views.team", team(sheet=SECRET)),
        ("oauth.views.team.table", team(table=SECRET)),
        ("oauth.views.team.columns", team(columns=["a\n" + SECRET])),
        ("oauth.views.team.columns", team(columns=[SECRET, SECRET])),
        ("oauth.views.team.columns", team(columns=SECRET)),
        (p, planning(**{SECRET: SECRET})),
        (f"{p}.sheet", planning(sheet=SECRET + "[")),
        (f"{p}.sheet", planning(sheet="{year}{year}" + SECRET)),
        (f"{p}.date_row", planning(date_row=0)),
        (f"{p}.date_row", planning(date_row=SECRET)),
        (f"{p}.first_row", planning(first_row=True)),
        (f"{p}.first_date_column", planning(first_date_column="4" + SECRET)),
        (f"{p}.labels", planning(labels={"member": "A", SECRET: "B"})),
        (f"{p}.labels.member", planning(labels={"member": SECRET})),
        (f"{p}.kinds", planning(kinds=None)),
        (f"{p}.kinds", planning(kinds=["a\u2028" + SECRET])),
        (f"{p}.kinds", planning(kinds=[SECRET, SECRET])),
        (f"{p}.stop_at", planning(stop_at=SECRET * 10)),
        (f"{p}.codes", planning(codes={"H": SECRET + " x"})),
        (f"{p}.codes", planning(codes={SECRET * 2: "available"})),
        (f"{p}.codes", planning(codes={"H": "unmapped", SECRET: SECRET})),
        (f"{p}.lookup", planning(lookup={"view": "team", "on": "Name", "add": ["ID"],
                                         SECRET: 1})),
        (f"{p}.lookup.view", planning(lookup={"view": SECRET, "on": "Name", "add": ["ID"]})),
        (f"{p}.lookup", planning(lookup={"view": "team", "on": SECRET, "add": ["ID"]})),
        (f"{p}.lookup", planning(lookup={"view": "team", "on": "Name", "add": [SECRET]})),
        (f"{p}.lookup.add", planning(lookup={"view": "team", "on": "Name",
                                             "add": ["ID", "Status"]})),
        (f"{p}.conflict.kind", planning(conflict={"kind": SECRET, "against": "Presence",
                                                  "when": ["unavailable"]})),
        (f"{p}.conflict.when", planning(conflict={"kind": "Guard", "against": "Presence",
                                                  "when": [SECRET]})),
    ]


BAD_VIEWS = _bad_views()
# A pin that is refused, with SECRET in it. Each would be accepted or changed
# by a cleaner that trims, stringifies or normalises before the check.
BAD_PINS: list[tuple[str, Any]] = [
    ("site", ""),
    ("site", "https://example.sharepoint.com/sites/" + SECRET),
    ("site", "example.sharepoint.com:/sites/" + SECRET),
    ("site", PINS["site"] + " "),
    ("site", " " + PINS["site"]),
    ("library", ""),
    ("library", SECRET + "/x"),
    ("library", "Documents "),
    ("library", "Documénts" + SECRET),
    ("path", ""),
    ("path", "Team/" + SECRET + ".docx"),
    ("path", "Team/../" + SECRET + ".xlsx"),
    ("path", PINS["path"] + " "),
    ("path", "\t" + PINS["path"]),
    ("path", "Team/Planning é " + SECRET.replace("!", "") + ".xlsx"),
]


@pytest.mark.parametrize("field, value", BAD_VIEWS)
@pytest.mark.parametrize("mode, block", [("destination", DEST), ("app_only", APP_ONLY)])
def test_storage_refuses_a_bad_view_without_the_value(mode, block, field, value):
    from agents.db import _clean_oauth

    with pytest.raises(ValueError) as refused:
        _clean_oauth({**block, "views": value}, mode, None, url=URL)
    assert str(refused.value).startswith(field + ":"), str(refused.value)
    assert SECRET not in str(refused.value)


@pytest.mark.parametrize("key, value", BAD_PINS + [
    ("site", 5), ("library", ["Documents"]), ("path", {"x": SECRET}), ("path", None),
])
@pytest.mark.parametrize("mode, block", [("destination", DEST), ("app_only", APP_ONLY)])
def test_storage_refuses_a_pin_that_is_not_in_its_exact_form(mode, block, key, value):
    from agents.db import _clean_oauth

    with pytest.raises(ValueError) as refused:
        _clean_oauth({**block, key: value}, mode, None, url=URL)
    assert str(refused.value).startswith(f"oauth.{key}:"), str(refused.value)
    assert SECRET not in str(refused.value)


def test_storage_refuses_the_wrong_mode_and_a_missing_destination():
    from agents.db import _clean_oauth

    for mode in ("oauth2", "jwt", "none", "session"):
        with pytest.raises(ValueError, match="^builtin:sharepoint requires auth_mode"):
            _clean_oauth(DEST, mode, None, url=URL)
    for block in ({**PINS, "views": VIEWS}, {**DEST, "destination": "  "},
                  {**DEST, "destination": {"name": SECRET}}, None, SECRET):
        with pytest.raises(ValueError) as refused:
            _clean_oauth(block, "destination", None, url=URL)
        assert str(refused.value).startswith("destination server requires")
        assert SECRET not in str(refused.value)
    for missing in ("client_id", "client_secret", "token_url"):
        block = {k: v for k, v in APP_ONLY.items() if k != missing}
        with pytest.raises(ValueError, match="^client_credentials server requires"):
            _clean_oauth(block, "app_only", None, url=URL)


def test_storage_stores_the_checked_form_of_the_views_never_the_raw_one():
    from agents.db import _clean_oauth
    from agents.sharepoint_views import clean_views

    raw = copy.deepcopy(VIEWS)
    raw["planning"]["sheet"] = "  {year} "
    raw["planning"]["stop_at"] = " Summary "
    raw["team"]["columns"] = [" Name", "Team ", "ID"]
    stored = _clean_oauth({**DEST, "views": raw}, "destination", None, url=URL)
    assert stored["views"] == VIEWS != raw
    assert stored["views"] == clean_views(raw)
    # Stable: what an edit reads back and posts again is stored unchanged.
    assert _clean_oauth(stored, "destination", None, url=URL) == stored
    # And not the caller's objects: a later change of the posted block
    # cannot reach what was checked.
    assert stored["views"] is not raw and stored["views"]["team"] is not raw["team"]


def test_other_servers_store_what_they_did_before():
    from agents.db import _clean_oauth

    teams = _clean_oauth({"destination": "D", "team": "t", **PINS, "views": VIEWS},
                         "destination", None, url="builtin:teams")
    assert teams == {"destination": "D", "team": "t", "allow_send": False}
    outlook = _clean_oauth({**APP_ONLY, "mailbox": "a@example.com"}, "app_only", None,
                           url="builtin:outlook")
    assert set(outlook) == {"client_id", "client_secret", "token_url", "scope",
                            "mailbox", "allow_send"}


# --- admin gate -------------------------------------------------------------

def _payload(mode: str = "destination", url: str = URL, **oauth):
    from agents.admin import McpServerPayload

    return McpServerPayload(url=url, auth_mode=mode, oauth=oauth)


def _refusal(mode: str, block: dict[str, Any], url: str = URL) -> str:
    with pytest.raises(ValidationError) as refused:
        _payload(mode, url, **block)
    # `errors()` without input, as the 422 handler answers; `str()` of a
    # pydantic error quotes the input by design.
    errors = refused.value.errors(include_input=False, include_context=False,
                                  include_url=False)
    text = " | ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in errors)
    assert SECRET not in text
    return text


@pytest.mark.parametrize("mode, block", [("destination", DEST), ("app_only", APP_ONLY)])
def test_admin_hands_storage_exactly_the_block_it_checked(mode, block):
    from agents.db import prepare_servers

    payload = _payload(mode, **block)
    seen = payload.oauth.to_config()
    assert seen == block
    primary, extras, oauth_json = prepare_servers(
        [{"url": payload.url, "auth_mode": payload.auth_mode, "oauth": seen}], None)
    stored = json.loads(oauth_json)
    assert primary == {"url": URL, "auth_mode": mode} and extras == []
    assert set(stored) == set(seen)
    for key in seen:
        assert stored[key] == seen[key], key


def test_admin_needs_no_mailbox_on_app_only():
    assert "mailbox" not in _payload("app_only", **APP_ONLY).oauth.to_config()


def test_admin_passes_the_views_on_in_their_checked_form():
    raw = copy.deepcopy(VIEWS)
    raw["planning"]["sheet"] = " {year} "
    assert _payload(**{**DEST, "views": raw}).oauth.to_config()["views"] == VIEWS


@pytest.mark.parametrize("mode, change, message", [
    ("jwt", {}, "requires auth_mode=destination"),
    ("oauth2", {}, "requires auth_mode=destination"),
    ("none", {}, "requires auth_mode=destination"),
    ("session", {}, "requires auth_mode=destination"),
    ("destination", {"user_context": True}, "oauth.user_context"),
    ("destination", {"user_context": "true"}, "oauth.user_context"),
    ("app_only", {"user_context": True}, "oauth.user_context"),
    ("destination", {"destination": ""}, "requires oauth.destination"),
    ("destination", {"destination": SECRET}, "oauth.destination must be"),
    ("destination", {"client_id": "c"}, "stores no credential"),
    ("destination", {"dcr": True}, "oauth.site"),
    ("app_only", {"client_id": ""}, "requires oauth.client_id"),
    ("app_only", {"token_url": ""}, "requires oauth.token_url"),
    ("app_only", {"token_url": "http://login.example.com/" + SECRET}, "oauth.token_url"),
])
def test_admin_refuses_by_field_and_never_repeats_a_value(mode, change, message):
    base = APP_ONLY if mode == "app_only" else DEST
    assert message in _refusal(mode, {**base, **change})


@pytest.mark.parametrize("key, value", BAD_PINS + [
    ("site", 5), ("library", ["Documents"]), ("path", {"x": SECRET}),
    ("path", "x" * 500 + SECRET),
])
@pytest.mark.parametrize("mode, block", [("destination", DEST), ("app_only", APP_ONLY)])
def test_admin_refuses_a_pin_that_is_not_in_its_exact_form(mode, block, key, value):
    assert f"oauth.{key}" in _refusal(mode, {**block, key: value})


@pytest.mark.parametrize("field, value", BAD_VIEWS + [("oauth.views", [SECRET]),
                                                     ("oauth.views", SECRET)])
@pytest.mark.parametrize("mode, block", [("destination", DEST), ("app_only", APP_ONLY)])
def test_admin_refuses_a_bad_view_by_field(mode, block, field, value):
    assert field in _refusal(mode, {**block, "views": value})


def test_the_pins_and_views_belong_to_this_entry_only():
    for key, value in (("site", PINS["site"]), ("library", "Documents"),
                       ("path", PINS["path"]), ("views", VIEWS)):
        text = _refusal("destination", {"destination": "D", "team": "t", key: value},
                        url="builtin:teams")
        assert "belong to a builtin:sharepoint entry only" in text
    _payload(url="builtin:teams", destination="D", team="t")


# --- over HTTP: the answer and the log ---------------------------------------

@pytest.fixture
async def client():
    import app as app_module
    from agents.db import init_db

    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app_module.app),
                           base_url="http://test") as c:
        yield c


def _agent(name: str, mode: str, oauth: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "description": "d", "instructions": "i",
            "mcp_servers": [{"url": URL, "auth_mode": mode, "oauth": oauth}]}


def _marked(mode: str) -> dict[str, Any]:
    """An entry with SECRET in every field an admin types."""
    views = copy.deepcopy(VIEWS)
    views["team"].update(table=SECRET, columns=[SECRET])
    views["planning"].update(
        sheet=SECRET + "[", first_date_column=SECRET, labels={"member": SECRET},
        kinds=[SECRET], stop_at=SECRET * 10, codes={SECRET: SECRET},
        lookup={"view": SECRET, "on": SECRET, "add": [SECRET]},
        conflict={"kind": SECRET, "against": SECRET, "when": [SECRET]})
    views[SECRET] = {"kind": SECRET}
    block: dict[str, Any] = {
        "site": "example.sharepoint.com:/sites/" + SECRET, "library": SECRET + "/x",
        "path": "Team/" + SECRET + ".docx", "views": views,
        "mailbox": SECRET, "team": SECRET, SECRET: SECRET,
    }
    if mode == "destination":
        block["destination"] = SECRET
    else:
        block.update(client_id=SECRET, client_secret=SECRET,
                     token_url="http://login.example.com/" + SECRET, scope=SECRET)
    return block


def _assert_refused_clean(r, caplog) -> str:
    assert r.status_code in (400, 422), r.text
    assert SECRET not in r.text
    for key, value in r.headers.items():
        assert SECRET not in key and SECRET not in value
    assert SECRET not in caplog.text
    if r.status_code == 422:
        for item in r.json()["detail"]:
            assert set(item) == {"loc", "msg", "type"}, item
    return r.text


@pytest.mark.parametrize("mode", ["destination", "app_only"])
async def test_a_refused_entry_is_never_echoed_field_after_field(client, caplog, mode):
    """SECRET in every field; then one field after the other is made valid,
    so each later rule refuses with the marker still in what follows it."""
    caplog.set_level(logging.DEBUG)
    good = dict(DEST if mode == "destination" else APP_ONLY)
    marked = _marked(mode)
    # The order the gate refuses in; the credential itself (client id, secret,
    # scope) is free text and made valid with the last step.
    order = ["views", "site", "library", "path",
             "destination" if mode == "destination" else "token_url"]
    seen = set()
    for fixed in range(len(order)):
        block = {**marked, **{k: good[k] for k in order[:fixed]}}
        r = await client.post("/admin/api/agents", json=_agent(f"sp-{mode}", mode, block))
        seen.add(_assert_refused_clean(r, caplog))
    assert len(seen) == len(order), seen  # one rule per step, not one early refusal
    # Only keys the entry does not have are left marked: accepted and dropped.
    stray = {k: v for k, v in marked.items() if k not in good and k not in order}
    assert stray
    r = await client.post("/admin/api/agents", json=_agent(f"sp-{mode}", mode,
                                                           {**stray, **good}))
    assert r.status_code in (200, 201), r.text
    assert SECRET not in r.text
    assert SECRET not in caplog.text


@pytest.mark.parametrize("field, value", BAD_VIEWS)
async def test_a_refused_view_is_never_echoed_over_http(client, caplog, field, value):
    caplog.set_level(logging.DEBUG)
    r = await client.post("/admin/api/agents",
                          json=_agent("sp-view", "destination", {**DEST, "views": value}))
    assert field in _assert_refused_clean(r, caplog)


@pytest.mark.parametrize("key, value", BAD_PINS)
async def test_a_refused_pin_is_never_echoed_over_http(client, caplog, key, value):
    caplog.set_level(logging.DEBUG)
    r = await client.post("/admin/api/agents",
                          json=_agent("sp-pin", "app_only", {**APP_ONLY, key: value}))
    assert f"oauth.{key}" in _assert_refused_clean(r, caplog)


@pytest.mark.parametrize("mode, block", [("destination", DEST), ("app_only", APP_ONLY)])
async def test_a_saved_entry_holds_what_the_gate_saw_and_shows_no_secret(
    client, caplog, mode, block
):
    from agents.db import SessionLocal, get_agent_by_name

    caplog.set_level(logging.DEBUG)
    # The SQLite driver's own DEBUG lines print every bound parameter, the
    # stored row included: a property of that driver's debug log for every
    # credential in the database, not of this entry.
    caplog.set_level(logging.INFO, logger="aiosqlite")
    name = f"sp-saved-{mode}"
    posted = {**block, "mailbox": "a@example.com", "allow_send": True, "team": "t"}
    r = await client.post("/admin/api/agents", json=_agent(name, mode, posted))
    assert r.status_code in (200, 201), r.text
    async with SessionLocal() as session:
        row = await get_agent_by_name(session, name)
        (server,) = row.mcp_servers
    assert server == {"url": URL, "auth_mode": mode, "oauth": block}
    secret = APP_ONLY["client_secret"]
    assert secret not in r.text and secret not in caplog.text
    listed = await client.get("/admin/api/agents")
    assert listed.status_code == 200 and secret not in listed.text
    (answered,) = [a for a in listed.json() if a["name"] == name]
    shown = answered["mcp_servers"][0]["oauth"]
    assert shown["views"] == VIEWS and {k: shown[k] for k in PINS} == PINS
    # An edit posts back what it read (blank secret): nothing changes.
    r = await client.put(f"/admin/api/agents/{row.id}", json=_agent(name, mode, shown))
    assert r.status_code == 200, r.text
    async with SessionLocal() as session:
        row = await get_agent_by_name(session, name)
        assert row.mcp_servers == [server]


# --- credential health ------------------------------------------------------

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


async def test_credential_health_reports_the_sharepoint_destination(monkeypatch):
    import agents.admin as admin
    import agents.destination as dest_mod
    from agents.destination import DestinationServiceConfig

    rows = [_Row("Sync", [{"url": URL, "auth_mode": "destination", "oauth": DEST}])]
    monkeypatch.setattr(admin, "SessionLocal", lambda: _NoSession())

    async def fake_list_agents(session):
        return rows

    monkeypatch.setattr(admin, "list_agents", fake_list_agents)
    (unbound,) = await admin._destination_health()
    assert unbound["state"] == "unbound" and unbound["destination"] == "GRAPH"

    monkeypatch.setattr(
        dest_mod, "config_from_environment",
        lambda env: DestinationServiceConfig("id", "secret", "https://uaa/oauth/token",
                                             "https://api"),
    )

    class FakeResolverCls:
        def __init__(self, name, config, **kw):
            self.name = name

        async def resolve(self, **kw):
            assert "user_token" not in kw
            return Destination(url="https://graph.example.com",
                               headers={"Authorization": "Bearer s3cr3t"},
                               expires_at=time.monotonic() + 60,
                               auth_type="OAuth2ClientCredentials")

    monkeypatch.setattr(dest_mod, "DestinationResolver", FakeResolverCls)
    (entry,) = await admin._destination_health()
    assert entry["agent"] == "Sync" and entry["server_key"] == URL
    assert entry["state"] == "resolvable" and entry["auth_type"] == "OAuth2ClientCredentials"
    assert entry["user_context"] is False and "warning" not in entry
    text = json.dumps(entry)
    assert "s3cr3t" not in text and "sharepoint.com" not in text and "TeamMembers" not in text


# --- ABAP Assistant ---------------------------------------------------------

@pytest.mark.parametrize("policy", ["change", "diagnose"])
def test_the_tools_are_denied_in_an_abap_assistant_session(policy):
    from agents.ide.readonly import POLICIES, READONLY_POLICY, check_call

    for tool in ("read_table", "read_calendar"):
        assert tool not in READONLY_POLICY
        assert all(tool not in rules for rules in POLICIES.values())
        assert check_call(tool, {"view": "team"}, policy) is not None
