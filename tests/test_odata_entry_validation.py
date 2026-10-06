"""The ``builtin:odata`` server entry of an agent: what the admin API accepts,
what storage keeps, and how its destinations show in credential health.

The entry is the gate that decides which agent may use which OData service
and whether it may write, so the rules are tested at three levels: the
payload model (a 422 naming the field, never the value), the storage cleaner
(the last gate, whatever the caller) and the routes (services must exist in
the catalogue when the agent is saved or imported).
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

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import delete

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
for _v in (
    "DESTINATION_CLIENT_ID",
    "DESTINATION_CLIENT_SECRET",
    "DESTINATION_URI",
    "DESTINATION_TOKEN_URL",
    "DESTINATION_UAA_URL",
    "CONNECTIVITY_CLIENT_ID",
    "CONNECTIVITY_CLIENT_SECRET",
    "CONNECTIVITY_TOKEN_URL",
    "CONNECTIVITY_PROXY_HOST",
    "CONNECTIVITY_PROXY_PORT",
    "CONNECTIVITY_PP_MODE",
):
    os.environ.pop(_v, None)

import app as app_module  # noqa: E402
from agents import admin  # noqa: E402
from agents.admin import AgentPayload, McpServerPayload  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    ODataAuditLog,
    ODataService,
    SessionLocal,
    _clean_destination,
    create_odata_service,
    init_db,
    list_agents,
    prepare_servers,
    upsert_agent,
    validate_odata_service,
)

AGENTS = "/admin/api/agents"
SERVICES = "/admin/api/odata/services"
ENTRY: dict[str, Any] = {
    "url": "builtin:odata",
    "auth_mode": "destination",
    "oauth": {"services": ["purchase-requisitions"], "allow_write": True},
}
SERVICE: dict[str, Any] = {
    "name": "purchase-requisitions",
    "title": "Purchase requisitions",
    "purpose": "Read requisitions and their items",
    "destination": "S4_ODATA_USER",
    "user_context": True,
    "odata_version": "v2",
    "service_path": "/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV",
    "definition": {
        "entity_sets": [
            {
                "name": "A_PurchaseRequisitionItem",
                "keys": [{"name": "PurchaseRequisition"}],
                "operations": ["list", "get"],
                "fields": [{"name": "PurchaseRequisition", "selectable": True}],
            }
        ]
    },
}

# Never part of a refusal: what the refused entry carried.
SECRET = "s3cr3t-value"


def entry(oauth: Any = None, **patch: Any) -> dict[str, Any]:
    out = copy.deepcopy(ENTRY)
    if oauth is not None:
        out["oauth"] = oauth
    out.update(patch)
    return out


def agent(name: str = "pr-agent", servers: list[dict[str, Any]] | None = None, **patch: Any):
    body = {
        "name": name,
        "description": "d",
        "instructions": "i",
        "mcp_servers": [copy.deepcopy(ENTRY)] if servers is None else servers,
    }
    body.update(patch)
    return body


def refusal(data: dict[str, Any], model: Any = McpServerPayload) -> str:
    """The messages of the 422 a body gets, without pydantic's input echo."""
    with pytest.raises(ValidationError) as exc:
        model.model_validate(data)
    errors = exc.value.errors(include_url=False, include_context=False, include_input=False)
    return " | ".join(str(e["msg"]) for e in errors)


async def stored(name: str = "pr-agent") -> list[dict[str, Any]]:
    async with SessionLocal() as s:
        return {r.name: r.mcp_servers for r in await list_agents(s)}[name]


@pytest.fixture(autouse=True)
async def _clean_tables():
    await init_db()
    async with SessionLocal() as s:
        for model in (ODataService, ODataAuditLog, AgentConfig):
            await s.execute(delete(model))
        await s.commit()
    yield


@pytest.fixture
async def client():
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def service(client):
    r = await client.post(SERVICES, json=SERVICE)
    assert r.status_code == 201, r.text
    return r.json()


# --- the payload model --------------------------------------------------------


def test_payload_accepts_the_entry_without_a_destination():
    p = McpServerPayload.model_validate(ENTRY)
    assert p.oauth.to_config() == {"services": ["purchase-requisitions"], "allow_write": True}
    read_only = McpServerPayload.model_validate(entry({"services": ["a", "b"]}))
    assert read_only.oauth.to_config() == {"services": ["a", "b"]}
    off = McpServerPayload.model_validate(entry({"services": ["a"], "allow_write": False}))
    assert off.oauth.to_config() == {"services": ["a"]}


def test_payload_accepts_what_the_api_itself_echoes():
    """``GET`` adds the read-only ``has_client_secret`` to every block; an
    export pasted back, or a UI that saves what it loaded, must not be refused
    for it. It is never stored."""
    p = McpServerPayload.model_validate(entry({"services": ["a"], "has_client_secret": False}))
    assert p.oauth.to_config() == {"services": ["a"]}
    assert "has_client_secret" in refusal(entry({"services": ["a"], "has_client_secret": True}))


def test_url_spelling_does_not_escape_the_rules():
    p = McpServerPayload.model_validate(entry(url=" BUILTIN:OData/ "))
    assert p.url == "builtin:odata"
    assert "destination" in refusal(
        entry({"services": ["a"], "destination": "S4"}, url=" BUILTIN:OData/ ")
    )


@pytest.mark.parametrize(
    "oauth, message",
    [
        ({}, "services"),
        ({"services": []}, "services"),
        ({"services": ["Bad Name"]}, "services"),
        ({"services": ["a", "a"]}, "duplicate"),
        ({"services": ["a"], "destination": "S4"}, "destination"),
        ({"services": ["a"], "user_context": True}, "user_context"),
        ({"services": ["a"], "user_context": False}, "user_context"),
        ({"services": ["a"], "client_id": "x"}, "credential"),
        ({"services": ["a"], "client_secret": "x"}, "credential"),
        ({"services": ["a"], "mailbox": "x@example.com"}, "mailbox"),
        ({"services": ["a"], "dcr": True}, "dcr"),
        ({"services": ["a"], "allow_send": True}, "allow_send"),
        ({"services": ["a"], "theme": {"band": "#000000"}}, "theme"),
        ({"services": [f"s{i}" for i in range(51)]}, "50"),
        ({"services": "purchase-requisitions"}, "services"),
        ({"services": {"a": 1}}, "services"),
        ({"services": [["a"]]}, "services"),
        ({"services": [1]}, "services"),
        ({"services": [None]}, "services"),
        ({"services": ["a"], "allow_write": "true"}, "allow_write"),
        ({"services": ["a"], "allow_write": 1}, "allow_write"),
        ({"services": ["a"], "allow_write": [True]}, "allow_write"),
    ],
)
def test_bad_entries_are_422(oauth, message):
    assert message in refusal(entry(oauth))


def test_null_is_absent_and_never_opens_anything():
    """A client that serialises an unset field as ``null``: read-only, and no
    services is still no services."""
    p = McpServerPayload.model_validate(entry({"services": ["a"], "allow_write": None}))
    assert p.oauth.to_config() == {"services": ["a"]}
    assert "services" in refusal(entry({"services": None}))
    assert "services" in refusal(entry({"services": None, "allow_write": True}))


def test_a_missing_or_non_object_block_is_refused():
    no_block = {"url": "builtin:odata", "auth_mode": "destination"}
    assert "services" in refusal(no_block)
    assert "services" in refusal({**no_block, "oauth": None})
    for bad in ("purchase-requisitions", ["purchase-requisitions"], 1):
        refusal({**no_block, "oauth": bad})


@pytest.mark.parametrize("mode", ["jwt", "none", "oauth2", "app_only", "session"])
def test_only_destination_mode_is_accepted(mode):
    assert "auth_mode=destination" in refusal(entry(auth_mode=mode))
    # ... and no block makes another mode acceptable either.
    for oauth in (
        {"dcr": True},
        {"client_id": "c", "client_secret": "s", "token_url": "https://uaa.example/oauth/token"},
    ):
        refusal(entry(oauth, auth_mode=mode))
    refusal({"url": "builtin:odata", "auth_mode": mode})
    assert "auth_mode=destination" in refusal({"url": "builtin:odata"})


@pytest.mark.parametrize(
    "oauth",
    [
        {"services": [f"Bad {SECRET}"]},
        {"services": [SECRET.upper()]},
        {"services": ["a"], SECRET: 1},
        {"services": ["a"], "allow_write": SECRET},
        {"services": ["a"], "destination": SECRET},
        {"services": ["a"], "client_secret": SECRET},
        {"services": SECRET},
    ],
)
def test_a_refusal_never_repeats_the_value(oauth):
    message = refusal(entry(oauth))
    assert SECRET not in message and SECRET.upper() not in message


def test_destination_and_identity_are_explained():
    for key in ("destination", "user_context"):
        message = refusal(entry({"services": ["a"], key: "S4" if key == "destination" else True}))
        assert "belong to the catalogue service" in message and "duplicate the service" in message


@pytest.mark.parametrize(
    "server",
    [
        {
            "url": "builtin:jira",
            "auth_mode": "destination",
            "oauth": {"destination": "JIRA", "services": ["a"]},
        },
        {
            "url": "builtin:jira",
            "auth_mode": "destination",
            "oauth": {"destination": "JIRA", "allow_write": True},
        },
        {
            "url": "https://x.hana.ondemand.com/mcp",
            "auth_mode": "destination",
            "oauth": {"destination": "MCP", "services": ["a"], "allow_write": True},
        },
        {"url": "builtin:sapnotes", "auth_mode": "none", "oauth": {"services": ["a"]}},
        {
            "url": "https://x.hana.ondemand.com/mcp",
            "auth_mode": "jwt",
            "oauth": {"allow_write": True},
        },
    ],
)
def test_services_and_allow_write_belong_to_builtin_odata_only(server):
    assert "builtin:odata" in refusal(server)


@pytest.mark.parametrize(
    "extra",
    [
        {"services": []},
        {"allow_write": False},
        {"services": None},
        {"allow_write": None},
        {"services": [], "allow_write": False},
        {"services": None, "allow_write": None},
    ],
)
def test_empty_values_of_the_two_keys_do_not_refuse_another_server(extra):
    """Only a non-empty ``services`` or a true ``allow_write`` is refused on
    another server; a client sending every field with its empty value keeps
    working, and nothing of it is stored."""
    for server in (
        {
            "url": "builtin:jira",
            "auth_mode": "destination",
            "oauth": {"destination": "JIRA", **extra},
        },
        {"url": "builtin:sapnotes", "auth_mode": "none", "oauth": {"min_score": "9.0", **extra}},
        {
            "url": "https://x.hana.ondemand.com/mcp",
            "auth_mode": "destination",
            "oauth": {"destination": "MCP", **extra},
        },
    ):
        cfg = McpServerPayload.model_validate(server).oauth.to_config()
        assert "services" not in cfg and "allow_write" not in cfg
    jwt = {"url": "https://x.hana.ondemand.com/mcp", "auth_mode": "jwt", "oauth": extra}
    assert McpServerPayload.model_validate(jwt).oauth.to_config() == {}
    assert "builtin:odata" in refusal(
        {
            "url": "builtin:jira",
            "auth_mode": "destination",
            "oauth": {"destination": "JIRA", **extra, "allow_write": True},
        }
    )


def test_other_destination_servers_validate_as_before():
    jira = McpServerPayload.model_validate(
        {"url": "builtin:jira", "auth_mode": "destination", "oauth": {"destination": "JIRA"}}
    )
    assert jira.oauth.to_config() == {"destination": "JIRA"}
    assert "oauth.destination" in refusal({"url": "builtin:jira", "auth_mode": "destination"})


def test_an_agent_has_at_most_one_odata_entry():
    two = agent(
        servers=[entry({"services": ["a"]}), entry({"services": ["b"]}, url="BUILTIN:ODATA/")]
    )
    assert "at most one builtin:odata" in refusal(two, AgentPayload)
    with_other = agent(servers=[entry(), {"url": "https://x.hana.ondemand.com/mcp"}])
    assert len(AgentPayload.model_validate(with_other).mcp_servers) == 2


# --- storage ------------------------------------------------------------------


def test_storage_keeps_exactly_services_and_true_allow_write():
    assert _clean_destination(
        {
            "services": ["b", "a", "a"],
            "allow_write": "true",
            "destination": "X",
            "user_context": True,
        },
        "builtin:odata",
    ) == {"services": ["b", "a"]}
    assert _clean_destination({"services": ["a"], "allow_write": True}, "builtin:odata") == {
        "services": ["a"],
        "allow_write": True,
    }
    for not_true in (1, "true", "True", "yes", [True], {"a": 1}, False, None):
        assert _clean_destination(
            {"services": ["a"], "allow_write": not_true}, " BUILTIN:OData/ "
        ) == {"services": ["a"]}
    assert _clean_destination(
        {"services": ["a"], "client_secret": SECRET, "mailbox": "x@example.com", "theme": {}},
        "builtin:odata",
    ) == {"services": ["a"]}


@pytest.mark.parametrize(
    "block",
    [
        None,
        {},
        {"services": []},
        {"services": "purchase-requisitions"},
        {"services": {"a": 1}},
        {"services": [1]},
        {"services": ["a", None]},
        {"services": [["a"]]},
        {"services": ["Bad Name"]},
        {"services": [f"bad {SECRET}"]},
        {"services": [f"s{i}" for i in range(51)]},
    ],
)
def test_storage_refuses_what_is_no_list_of_service_names(block):
    with pytest.raises(ValueError) as exc:
        _clean_destination(block, "builtin:odata")
    assert "builtin:odata" in str(exc.value) and SECRET not in str(exc.value)


def test_storage_refuses_a_second_entry_and_any_other_mode():
    with pytest.raises(ValueError, match="at most one builtin:odata"):
        prepare_servers(
            [copy.deepcopy(ENTRY), entry({"services": ["b"]}, url="BUILTIN:ODATA/")], None
        )
    for mode in ("jwt", "none", "oauth2", "app_only", "session"):
        with pytest.raises(ValueError, match="auth_mode=destination"):
            prepare_servers([entry(auth_mode=mode)], None)
    primary, extras, oauth_json = prepare_servers([copy.deepcopy(ENTRY)], None)
    assert primary == {"url": "builtin:odata", "auth_mode": "destination"} and extras == []
    assert json.loads(oauth_json) == ENTRY["oauth"]


def test_storage_of_the_other_destination_built_ins_is_unchanged():
    assert _clean_destination(
        {"destination": "J", "project": "P", "services": ["a"]}, "builtin:jira"
    ) == {
        "destination": "J",
        "project": "P",
        "allow_comment": False,
    }
    assert _clean_destination(
        {"destination": "G", "user_context": True, "allow_write": True}, "builtin:outlook"
    ) == {"destination": "G", "user_context": True, "allow_send": False}
    assert _clean_destination(
        {"destination": "M", "user_context": True, "services": ["a"], "allow_write": True},
        "https://x.hana.ondemand.com/mcp",
    ) == {"destination": "M", "user_context": True}


@pytest.mark.parametrize(
    "url", ["builtin:odata/", " BUILTIN:OData/ ", "Builtin:OData", "builtin:odata//"]
)
async def test_no_spelling_of_the_url_is_stored_that_the_registry_does_not_build(
    client, service, url
):
    """``odata_entries`` (who uses a service) forgives case and a trailing
    slash; the registry's built-in lookup does not. An entry stored as
    ``builtin:odata/`` would block the delete of a service it can never call."""
    from agents.builtins import is_builtin_url

    # Through the API ...
    r = await client.post(AGENTS, json=agent(servers=[entry(url=url)]))
    assert r.status_code == 201, r.text
    assert r.json()["mcp_servers"][0]["url"] == "builtin:odata"
    # ... and for a caller that writes without the payload model.
    async with SessionLocal() as s:
        await upsert_agent(
            s,
            name="direct",
            description="d",
            instructions="i",
            mcp_servers=[
                {"url": "https://x.hana.ondemand.com/mcp", "auth_mode": "jwt"},
                entry(url=url),
            ],
        )
        for row in await list_agents(s):
            urls = [srv["url"] for srv in row.mcp_servers if "odata" in srv["url"].lower()]
            assert urls == ["builtin:odata"] and is_builtin_url(urls[0])
            assert row.mcp_url != url


def test_a_url_that_only_starts_like_the_built_in_is_refused():
    for url in ("builtin:odata/extra", "builtin:odata?x=1", "builtin:odatax"):
        refusal(entry(url=url))
        refusal({"url": url, "auth_mode": "destination", "oauth": {"destination": "S4_ODATA_TECH"}})


async def add_services(*names: str, enabled: bool = True) -> None:
    async with SessionLocal() as s:
        for name in names:
            data = validate_odata_service(
                {**copy.deepcopy(SERVICE), "name": name, "enabled": enabled}
            )
            await create_odata_service(s, data)


async def test_upsert_refuses_a_service_the_catalogue_does_not_have():
    """The shared write point, not only the admin routes: a script or a seed
    that calls ``upsert_agent`` must not store a dangling name. With
    ``allow_write`` it would be a standing grant on whatever service is later
    created under that name."""
    await add_services("purchase-requisitions")
    for services, message in (
        (["nope"], "unknown OData service 'nope'"),
        (["nope", "purchase-requisitions", "gone", "nope"], "unknown OData service 'nope', 'gone'"),
    ):
        async with SessionLocal() as s:
            with pytest.raises(ValueError) as exc:
                await upsert_agent(
                    s,
                    name="pr-agent",
                    description="d",
                    instructions="i",
                    mcp_servers=[entry({"services": services, "allow_write": True})],
                )
            assert str(exc.value) == message
    async with SessionLocal() as s:
        with pytest.raises(ValueError) as exc:
            await upsert_agent(
                s,
                name="pr-agent",
                description="d",
                instructions="i",
                commit=False,
                mcp_servers=[
                    {"url": "https://x.hana.ondemand.com/mcp", "auth_mode": "jwt"},
                    entry({"services": [f"bad {SECRET}"]}),
                ],
            )
        assert "invalid service name" in str(exc.value) and SECRET not in str(exc.value)
    async with SessionLocal() as s:
        assert await list_agents(s) == []
    # A disabled service exists.
    await add_services("stock", enabled=False)
    async with SessionLocal() as s:
        row = await upsert_agent(
            s,
            name="pr-agent",
            description="d",
            instructions="i",
            mcp_servers=[entry({"services": ["stock", "purchase-requisitions"]})],
        )
        assert json.loads(row.oauth_json) == {"services": ["stock", "purchase-requisitions"]}


async def test_the_helper_refuses_a_name_that_is_no_service_name():
    """For a caller that hands over a server list no cleaner has seen."""
    from agents.db import check_odata_services

    async with SessionLocal() as s:
        for bad in (f"Bad {SECRET}", 7, None, ["a"]):
            with pytest.raises(ValueError) as exc:
                await check_odata_services(s, [entry({"services": [bad]})])
            assert str(exc.value) == "invalid service name"
        await check_odata_services(s, [{"url": "builtin:jira", "oauth": {"services": ["nope"]}}])
        await check_odata_services(s, [])


def test_the_existence_check_locks_the_rows_on_postgres_only():
    """Compile-only: SQLite has no row locks, so nothing else in the suite
    would notice the clause missing where it matters."""
    from sqlalchemy.dialects import postgresql, sqlite

    from agents.db import _existing_odata_names_query

    locked = _existing_odata_names_query(["a", "b"], lock=True)
    assert "FOR SHARE" in str(locked.compile(dialect=postgresql.dialect()))
    assert "FOR" not in str(locked.compile(dialect=sqlite.dialect()))
    plain = _existing_odata_names_query(["a", "b"], lock=False)
    assert "FOR" not in str(plain.compile(dialect=postgresql.dialect()))


async def test_upsert_stores_the_exact_shape():
    await add_services("a", "b")
    async with SessionLocal() as s:
        row = await upsert_agent(
            s,
            name="pr-agent",
            description="d",
            instructions="i",
            mcp_servers=[
                {"url": "https://x.hana.ondemand.com/mcp", "auth_mode": "jwt"},
                entry({"services": ["b", "a", "b"], "allow_write": 1, "destination": "X"}),
            ],
        )
        assert json.loads(row.extra_servers_json) == [
            {"url": "builtin:odata", "auth_mode": "destination", "oauth": {"services": ["b", "a"]}}
        ]


# --- agent save: the services must exist --------------------------------------


async def test_saving_stores_exactly_the_contract_shape(client, service):
    r = await client.post(AGENTS, json=agent())
    assert r.status_code == 201, r.text
    assert await stored() == [ENTRY]
    async with SessionLocal() as s:
        row = (await list_agents(s))[0]
        assert json.loads(row.oauth_json) == {
            "services": ["purchase-requisitions"],
            "allow_write": True,
        }
    # Read-only: no allow_write key at all.
    r = await client.put(
        f"{AGENTS}/{r.json()['id']}",
        json=agent(servers=[entry({"services": ["purchase-requisitions"]})]),
    )
    assert r.status_code == 200, r.text
    assert await stored() == [entry({"services": ["purchase-requisitions"]})]


async def test_saving_an_agent_with_an_unknown_service_is_422(client, service):
    unknown = agent(servers=[entry({"services": ["purchase-requisitions", "nope"]})])
    r = await client.post(AGENTS, json=unknown)
    assert r.status_code == 422 and r.json()["detail"] == "unknown OData service 'nope'"
    async with SessionLocal() as s:
        assert await list_agents(s) == []
    r = await client.post(AGENTS, json=agent())
    assert r.status_code == 201, r.text
    r = await client.put(f"{AGENTS}/{r.json()['id']}", json=unknown)
    assert r.status_code == 422 and r.json()["detail"] == "unknown OData service 'nope'"
    assert await stored() == [ENTRY]


@pytest.mark.parametrize(
    "url", ["builtin:odata/", "BUILTIN:ODATA", "  builtin:odata  ", " Builtin:OData/ "]
)
async def test_no_spelling_of_the_url_escapes_the_existence_check(client, service, url):
    """The check reads the prepared entries, the ones about to be stored
    under ``builtin:odata``. Were it to read the raw list with a normaliser
    of its own, a spelling only storage recognised would attach a service
    the catalogue does not have."""
    servers = [
        {"url": "https://x.hana.ondemand.com/mcp", "auth_mode": "jwt"},
        entry({"services": ["purchase-requisitions", "nope"], "allow_write": True}, url=url),
    ]
    # A caller that writes without the payload model ...
    async with SessionLocal() as s:
        with pytest.raises(ValueError) as exc:
            await upsert_agent(
                s, name="direct", description="d", instructions="i", mcp_servers=servers
            )
        assert str(exc.value) == "unknown OData service 'nope'"
    async with SessionLocal() as s:
        assert await list_agents(s) == []
    # ... and the update route, which prepares the servers itself.
    r = await client.post(AGENTS, json=agent())
    assert r.status_code == 201, r.text
    r = await client.put(f"{AGENTS}/{r.json()['id']}", json=agent(servers=servers))
    assert r.status_code == 422 and r.json()["detail"] == "unknown OData service 'nope'"
    assert await stored() == [ENTRY]
    r = await client.post(AGENTS, json=agent("other", servers=servers))
    assert r.status_code == 422 and r.json()["detail"] == "unknown OData service 'nope'"


async def test_the_update_route_checks_the_entries_it_stores(client, service, monkeypatch):
    """One list: what the existence check is handed is what
    ``prepare_servers`` returned for the row."""
    seen: list[Any] = []
    real = admin.check_odata_services

    async def spy(session, servers):
        seen.append(servers)
        await real(session, servers)

    monkeypatch.setattr(admin, "check_odata_services", spy)
    r = await client.post(AGENTS, json=agent())
    servers = [entry(url="BUILTIN:ODATA/"), {"url": "https://x.hana.ondemand.com/mcp"}]
    r = await client.put(f"{AGENTS}/{r.json()['id']}", json=agent(servers=servers))
    assert r.status_code == 200, r.text
    assert seen == [await stored()]
    assert seen[0][0] == ENTRY and seen[0][1]["url"] == "https://x.hana.ondemand.com/mcp"


async def test_a_deleted_service_cannot_be_attached_again(client, service):
    assert (await client.delete(f"{SERVICES}/purchase-requisitions")).status_code == 204
    r = await client.post(AGENTS, json=agent())
    assert r.status_code == 422
    assert r.json()["detail"] == "unknown OData service 'purchase-requisitions'"


async def test_saving_with_a_disabled_service_is_allowed_with_no_error(client):
    r = await client.post(SERVICES, json={**SERVICE, "enabled": False})
    assert r.status_code == 201 and r.json()["enabled"] is False
    r = await client.post(AGENTS, json=agent())
    assert r.status_code == 201, r.text
    assert await stored() == [ENTRY]


async def test_a_second_entry_is_422_at_the_route(client, service):
    r = await client.post(AGENTS, json=agent(servers=[entry(), entry(url="BUILTIN:ODATA")]))
    assert r.status_code == 422
    assert "at most one builtin:odata" in " ".join(e["msg"] for e in r.json()["detail"])


async def test_bad_entries_are_422_at_the_route_and_store_nothing(client, service):
    for oauth in (
        {"services": ["purchase-requisitions"], "destination": "S4_ODATA_TECH"},
        {"services": ["purchase-requisitions"], "user_context": False},
        {"services": ["purchase-requisitions"], "allow_write": "true"},
        {"services": []},
    ):
        r = await client.post(AGENTS, json=agent(servers=[entry(oauth)]))
        assert r.status_code == 422, r.text
    r = await client.post(AGENTS, json=agent(servers=[entry(auth_mode="oauth2")]))
    assert r.status_code == 422
    async with SessionLocal() as s:
        assert await list_agents(s) == []


async def test_delete_in_use_is_409_naming_the_agents(client, service):
    assert (await client.post(AGENTS, json=agent("buyer"))).status_code == 201
    assert (await client.post(AGENTS, json=agent("reader", enabled=False))).status_code == 201
    r = await client.delete(f"{SERVICES}/purchase-requisitions")
    assert r.status_code == 409
    assert r.json()["detail"] == (
        "Service 'purchase-requisitions' is used by agent(s) 'buyer', 'reader'"
    )
    used = (await client.get(f"{SERVICES}/purchase-requisitions")).json()["used_by"]
    assert [(u["agent"], u["enabled"], u["allow_write"]) for u in used] == [
        ("buyer", True, True),
        ("reader", False, True),
    ]


# --- import / export ----------------------------------------------------------


async def test_import_checks_services_against_the_bundle_and_the_database(client, service):
    bundle = {
        "agents": [
            agent("good"),
            agent("bad", servers=[entry({"services": ["nope", "purchase-requisitions", "gone"]})]),
        ]
    }
    r = await client.post("/admin/api/import", json=bundle)
    assert r.status_code == 422
    assert r.json()["detail"] == "Agent 'bad': unknown OData service 'nope', 'gone'"
    # One transaction: the good agent of a refused bundle is not stored either.
    async with SessionLocal() as s:
        assert await list_agents(s) == []
    r = await client.post("/admin/api/import", json={"agents": [agent("good")]})
    assert r.status_code == 200, r.text
    assert await stored("good") == [ENTRY]


async def test_export_round_trips_the_entry(client, service):
    assert (await client.post(AGENTS, json=agent())).status_code == 201
    exported = (await client.get("/admin/api/export")).json()
    server = exported["agents"][0]["mcp_servers"][0]
    assert server["url"] == "builtin:odata" and server["auth_mode"] == "destination"
    assert server["oauth"] == {
        "services": ["purchase-requisitions"],
        "allow_write": True,
        "has_client_secret": False,
    }
    async with SessionLocal() as s:
        await s.execute(delete(AgentConfig))
        await s.commit()
    r = await client.post("/admin/api/import", json=exported)
    assert r.status_code == 200, r.text
    assert await stored() == [ENTRY]
    # ... and onto a landscape whose catalogue lacks the service: refused
    # for a bundle that does not carry it (every export before the catalogue
    # travelled along), restored from one that does.
    async with SessionLocal() as s:
        await s.execute(delete(AgentConfig))
        await s.execute(delete(ODataService))
        await s.commit()
    older = {k: v for k, v in exported.items() if k != "odata_services"}
    r = await client.post("/admin/api/import", json=older)
    assert r.status_code == 422
    assert r.json()["detail"] == "Agent 'pr-agent': unknown OData service 'purchase-requisitions'"
    async with SessionLocal() as s:
        assert await list_agents(s) == []
    r = await client.post("/admin/api/import", json=exported)
    assert r.status_code == 200, r.text
    assert await stored() == [ENTRY]


# --- credential health --------------------------------------------------------


async def test_credential_health_lists_each_attached_services_destination(client, service):
    tech = {**SERVICE, "name": "stock", "destination": "S4_ODATA_TECH", "user_context": False}
    assert (await client.post(SERVICES, json=tech)).status_code == 201
    servers = [entry({"services": ["purchase-requisitions", "stock"]})]
    assert (await client.post(AGENTS, json=agent(servers=servers))).status_code == 201
    assert (await client.post(AGENTS, json=agent("off", enabled=False))).status_code == 201
    d = (await client.get("/admin/api/credential-health")).json()["destinations"]
    assert {
        "agent": "pr-agent",
        "server_key": "builtin:odata",
        "service": "purchase-requisitions",
        "destination": "S4_ODATA_USER",
        "user_context": True,
    }.items() <= d[0].items() and d[0]["state"] == "unbound"
    assert [(e["agent"], e["service"], e["destination"], e["user_context"]) for e in d] == [
        ("pr-agent", "purchase-requisitions", "S4_ODATA_USER", True),
        ("pr-agent", "stock", "S4_ODATA_TECH", False),
    ]
    assert set(d[0]) == {
        "agent",
        "server_key",
        "service",
        "service_enabled",
        "destination",
        "user_context",
        "state",
        "auth_type",
        "error",
    }
    assert [e["service_enabled"] for e in d] == [True, True]


class _Row:
    def __init__(self, name: str, servers: list[dict[str, Any]], enabled: bool = True):
        self.name = name
        self.mcp_servers = servers
        self.enabled = enabled


async def test_health_resolves_as_the_app_and_reports_principal_propagation(client, monkeypatch):
    import agents.destination as dest_mod
    from agents.destination import Destination, DestinationError, DestinationServiceConfig

    for name, destination, user_context in (
        ("purchase-requisitions", "S4_ODATA_USER", True),
        ("stock", "S4_ODATA_TECH", False),
        ("orders", "S4_ODATA_TECH", True),
        ("gone", "S4_GONE", True),
    ):
        body = {**SERVICE, "name": name, "destination": destination, "user_context": user_context}
        assert (await client.post(SERVICES, json=body)).status_code == 201
    rows = [
        _Row(
            "pr-agent",
            [
                {
                    "url": "builtin:odata",
                    "auth_mode": "destination",
                    "oauth": {
                        "services": [
                            "purchase-requisitions",
                            "stock",
                            "orders",
                            "gone",
                            "nope",
                            f"Bad {SECRET}",
                            7,
                        ]
                    },
                },
                {
                    "url": "builtin:jira",
                    "auth_mode": "destination",
                    "oauth": {"destination": "JIRA"},
                },
            ],
        ),
    ]

    async def fake_list_agents(session):
        return rows

    monkeypatch.setattr(admin, "list_agents", fake_list_agents)
    monkeypatch.setattr(
        dest_mod,
        "config_from_environment",
        lambda env: DestinationServiceConfig(
            "id", "secret", "https://uaa/oauth/token", "https://api"
        ),
    )
    built: list[str] = []

    class FakeResolverCls:
        def __init__(self, name, config, **kw):
            self.name = name
            built.append(name)

        async def resolve(self, **kw):
            assert not kw, "health resolves with the app token only"
            if self.name == "S4_ODATA_USER":
                raise DestinationError("uses PrincipalPropagation, which needs the signed-in user")
            if self.name == "S4_GONE":
                raise DestinationError(
                    f"destination 'S4_GONE' does not exist in the subaccount; {SECRET}=never "
                    f"at https://dest.internal/x?token={SECRET}"
                )
            return Destination(
                url="https://s4.internal:44300",
                headers={"Authorization": f"Basic {SECRET}"},
                expires_at=time.monotonic() + 60,
                auth_type="BasicAuthentication",
            )

        async def resolve_properties(self, **kw):
            assert not kw
            if self.name == "S4_GONE":
                raise DestinationError("destination does not exist")

            class Props:
                auth_type = "PrincipalPropagation"

            return Props()

    monkeypatch.setattr(dest_mod, "DestinationResolver", FakeResolverCls)
    out = await admin._destination_health()
    by_service = {e.get("service", e["server_key"]): e for e in out}
    user = by_service["purchase-requisitions"]
    assert user["state"] == "resolvable" and user["auth_type"] == "PrincipalPropagation"
    assert user["error"] is None and "warning" not in user
    tech = by_service["stock"]
    assert tech["state"] == "resolvable" and tech["auth_type"] == "BasicAuthentication"
    assert tech["user_context"] is False and "warning" not in tech
    # A service meant to run as the user on a technical-user destination.
    assert "app-level type" in by_service["orders"]["warning"]
    # Final review C3: a fixed text and a code, never the resolver's own text.
    assert by_service["gone"]["state"] == "error"
    assert by_service["gone"]["error"] == (
        "the destination does not exist in the subaccount of this app's destination "
        "service (not_found)"
    )
    assert SECRET not in json.dumps(out) and "dest.internal" not in json.dumps(out)
    unknown = by_service["nope"]
    assert unknown["state"] == "missing" and unknown["error"] == "unknown OData service"
    assert unknown["service_enabled"] is False and unknown["auth_type"] == ""
    assert user["service_enabled"] is True
    assert unknown["destination"] == "" and unknown["user_context"] is False
    # What is no service name is reported without the value.
    invalid = [e for e in out if e.get("service") == ""]
    assert len(invalid) == 2 and {e["error"] for e in invalid} == {"invalid service name"}
    assert (
        by_service["builtin:jira"]["destination"] == "JIRA"
        and "service" not in by_service["builtin:jira"]
    )
    # One resolver per destination, however many services share it.
    assert sorted(built) == ["JIRA", "S4_GONE", "S4_ODATA_TECH", "S4_ODATA_USER"]
    text = json.dumps(out)
    assert "Authorization" not in text and "s4.internal" not in text
    assert SECRET not in text, "not even the destination service's own error text"


USER_TYPES = [
    "PrincipalPropagation",
    "OAuth2UserTokenExchange",
    "OAuth2JWTBearer",
    "OAuth2SAMLBearerAssertion",
    "SAMLAssertion",
]


def test_the_health_cases_cover_every_user_propagating_type():
    from agents.destination import USER_PROPAGATING_AUTH_TYPES

    assert set(USER_TYPES) == set(USER_PROPAGATING_AUTH_TYPES)


@pytest.mark.parametrize("auth_type", USER_TYPES + ["BasicAuthentication", ""])
async def test_health_of_a_user_service_that_only_resolves_for_a_user(monkeypatch, auth_type):
    """A destination of a user-propagating type hands the application no
    token. For a service that runs as the signed-in user that is the correct
    set-up, not an error; any other type keeps the resolve error."""
    import agents.destination as dest_mod
    from agents.destination import DestinationError, DestinationServiceConfig

    await add_services("purchase-requisitions")
    await add_services("stock", enabled=False)
    rows = [_Row("pr-agent", [entry({"services": ["purchase-requisitions", "stock"]})])]

    async def fake_list_agents(session):
        return rows

    monkeypatch.setattr(admin, "list_agents", fake_list_agents)
    monkeypatch.setattr(
        dest_mod,
        "config_from_environment",
        lambda env: DestinationServiceConfig(
            "id", "secret", "https://uaa/oauth/token", "https://api"
        ),
    )

    class FakeResolverCls:
        def __init__(self, name, config, **kw):
            pass

        async def resolve(self, **kw):
            assert not kw, "health resolves with the app token only"
            raise DestinationError("no token for the application")

        async def resolve_properties(self, **kw):
            assert not kw

            class Props:
                pass

            Props.auth_type = auth_type
            return Props()

    monkeypatch.setattr(dest_mod, "DestinationResolver", FakeResolverCls)
    on, off = await admin._destination_health()
    assert (on["service"], on["service_enabled"]) == ("purchase-requisitions", True)
    assert (off["service"], off["service_enabled"]) == ("stock", False)
    for item in (on, off):
        if auth_type in USER_TYPES:
            assert item["state"] == "resolvable" and item["auth_type"] == auth_type
            assert item["error"] is None and "warning" not in item
        else:
            assert item["state"] == "error" and item["auth_type"] == ""
            # A fixed text and a code, not the resolver's text (final review C3).
            assert item["error"] == "the destination could not be resolved (failed)"


# --- end to end: saved through the API, built by the registry -----------------


@pytest.mark.usefixtures("real_agents_and_mcp")
async def test_an_agent_saved_through_the_api_is_built_with_both_odata_tools(
    client, service, monkeypatch
):
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.models.test import TestModel

    import agents.registry as registry_module

    monkeypatch.setattr(registry_module, "get_model", lambda *a, **k: TestModel())
    r = await client.post(AGENTS, json=agent(servers=[entry(url="builtin:odata/")]))
    assert r.status_code == 201, r.text
    plain = {"url": "builtin:sapnotes", "auth_mode": "none"}
    assert (await client.post(AGENTS, json=agent("notes", servers=[plain]))).status_code == 201

    build = await registry_module.Registry().reload()

    async def seen_by_model(built) -> tuple[set[str], str]:
        seen: dict[str, Any] = {}

        def answer(messages, info):
            seen["tools"] = {t.name for t in info.function_tools}
            seen["instructions"] = info.instructions or ""
            return ModelResponse(parts=[TextPart("ok")])

        await built.run("hello", model=FunctionModel(answer))
        return seen["tools"], seen["instructions"]

    tools, instructions = await seen_by_model(build.specialists["pr-agent"])
    assert {"search_operations", "execute_operation"} <= tools
    assert "- **purchase-requisitions**" in instructions
    assert "Write operations are enabled" in instructions
    tools, instructions = await seen_by_model(build.specialists["notes"])
    assert not ({"search_operations", "execute_operation"} & tools)
    assert "OData" not in instructions


@pytest.mark.parametrize(
    "text, code",
    [
        ("destination 'X' does not exist in the subaccount this app's ...", "not_found"),
        ("destination service returned 500 for 'X': <html>zone-9</html>", "status_500"),
        ("destination service token request returned 401 (invalid_client)", "token_status_401"),
        (
            "could not reach the destination service token endpoint: ConnectError: zone-9",
            "token_unreachable",
        ),
        ("could not reach the destination service for 'X': ConnectError: zone-9", "unreachable"),
        ("destination 'X' returned no authentication token; check ...", "no_credential"),
        ("destination 'X' could not obtain a token from the target (invalid_grant)", "token_error"),
        ("destination 'X' uses PrincipalPropagation, which needs the signed-in user", "needs_user"),
        ("destination 'X' has no URL configured", "no_url"),
        ("destination service answer for 'X' is not JSON", "not_json"),
        ("anything else with zone-9 in it", "failed"),
    ],
)
def test_a_destination_failure_is_answered_as_a_code_and_a_fixed_text(text, code):
    from agents.odata.destinations import destination_failure

    got, message = destination_failure(text)
    assert got == code and message.endswith(f"({code})")
    assert "zone-9" not in message and "'X'" not in message


async def test_health_answers_no_exception_text_for_an_unexpected_failure(monkeypatch, caplog):
    import agents.destination as dest_mod
    from agents.destination import DestinationServiceConfig

    class Boom:
        def __init__(self, name, config, **kw):
            pass

        async def resolve(self, **kw):
            raise RuntimeError(f"boom at https://dest.internal/x?token={SECRET}")

    monkeypatch.setattr(dest_mod, "DestinationResolver", Boom)

    class Service:
        enabled, destination, user_context = True, "S4_ODATA_TECH", False

    config = DestinationServiceConfig("id", "secret", "https://uaa/oauth/token", "https://api")
    with caplog.at_level(logging.WARNING):
        (entry_,) = await admin._odata_destination_health(
            "a", {"services": ["stock"]}, {"stock": Service()}, config, {}
        )
    assert entry_["state"] == "error"
    assert entry_["error"] == "the destination could not be checked (RuntimeError)"
    assert SECRET not in caplog.text and "dest.internal" not in caplog.text
