"""A refused request never gets its own input back, on any route of the app.

FastAPI's default 422 carries each error's ``input`` (the refused value, or
the whole refused body) and ``ctx``. An admin who pastes a credential into a
server entry that validation refuses would read it back in the answer, and
so would every log or proxy that stores response bodies. The app answers
``detail[]`` of ``loc`` / ``msg`` / ``type`` only (``agents/validation_errors``),
and the save-time rules name the field instead of quoting free text.

Run:  python -m pytest tests/test_admin_validation_errors.py -q
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEST_DB = ROOT / "tests" / "_test_admin_validation_errors.db"
TEST_DB.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB}"
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ.pop("MCP_URL_ALLOWLIST", None)

import app as app_module  # noqa: E402
from agents.db import init_db  # noqa: E402
from agents.ide.routes import install_validation_handler  # noqa: E402
from agents.ide.schemas import CommentCreate  # noqa: E402

# Never part of an answer: what a refused body carried. Upper case and
# punctuation keep it from being a legal slug, service name or address.
SECRET = "S3cr3t_Zx9!tok"
JSON = {"content-type": "application/json"}
MCP = "https://tools.cfapps.eu10.hana.ondemand.com/mcp"


def agent(**patch: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "val-agent",
        "description": "d",
        "instructions": "i",
        "mcp_servers": [{"url": MCP, "auth_mode": "jwt"}],
    }
    data.update(patch)
    return data


def with_server(**server: Any) -> dict[str, Any]:
    return agent(mcp_servers=[server])


@pytest.fixture(autouse=True)
async def _db():
    await init_db()
    yield


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app_module.app),
                           base_url="http://test") as c:
        yield c


def assert_clean_422(r, secret: str = SECRET) -> list[dict[str, Any]]:
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, list) and detail, detail
    for item in detail:
        assert set(item) == {"loc", "msg", "type"}, item
        assert isinstance(item["loc"], list)
        assert isinstance(item["msg"], str) and item["msg"]
    assert secret not in r.text
    assert secret not in json.dumps(r.json(), ensure_ascii=False)
    for key, value in r.headers.items():
        assert secret not in key and secret not in value
    return detail


# --- the refused value itself (FastAPI's `input`) ----------------------------

REFUSED_AGENT = with_server(url=MCP, auth_mode=SECRET)

ROUTES = [
    pytest.param("POST", "/admin/api/agents", REFUSED_AGENT, id="agent-create"),
    pytest.param("PUT", "/admin/api/agents/1", REFUSED_AGENT, id="agent-update"),
    pytest.param("POST", "/admin/api/agents",
                 agent(name={"password": SECRET}), id="agent-create-type"),
    pytest.param("POST", "/admin/api/import", {"agents": [REFUSED_AGENT]},
                 id="import"),
    pytest.param("POST", "/admin/api/import", {"agents": SECRET}, id="import-type"),
    pytest.param("POST", "/admin/api/skills",
                 {"name": {"token": SECRET}, "description": "d", "content": "c"},
                 id="skill-create"),
    pytest.param("PUT", "/admin/api/skills/1",
                 {"name": "s", "description": [SECRET], "content": "c"},
                 id="skill-update"),
    pytest.param("POST", "/admin/api/workflows",
                 {"name": "wf", "run_timeout_seconds": SECRET}, id="workflow-create"),
    pytest.param("PUT", "/admin/api/workflows/1",
                 {"name": "wf", "steps": [{"position": SECRET}]}, id="workflow-update"),
    pytest.param("PUT", "/admin/api/orchestrator", {"instructions": [SECRET]},
                 id="orchestrator"),
]


@pytest.mark.parametrize("method, path, body", ROUTES)
async def test_refused_body_is_not_echoed(client, method, path, body):
    r = await client.request(method, path, json=body)
    detail = assert_clean_422(r)
    assert detail[0]["loc"][0] == "body"


async def test_whole_body_of_the_wrong_type_is_not_echoed(client):
    # `input` is the entire body here.
    r = await client.post("/admin/api/agents", json=[SECRET])
    assert_clean_422(r)


async def test_broken_json_is_not_echoed(client):
    r = await client.post("/admin/api/agents",
                          content=('{"name": "' + SECRET + '", ').encode(),
                          headers=JSON)
    assert_clean_422(r)


# --- messages written by this app (`msg`) ------------------------------------

SMTP = {"url": "builtin:smtp", "auth_mode": "destination"}
OUTLOOK = {"url": "builtin:outlook", "auth_mode": "destination"}

MESSAGES = [
    pytest.param(with_server(url=f"builtin:{SECRET}", auth_mode="none"),
                 "built-in", id="unknown-builtin"),
    pytest.param(with_server(url=f"https://[{SECRET}]/mcp", auth_mode="jwt"),
                 "url", id="urlsplit-error"),
    pytest.param(with_server(url=f"https://exa mple.hana.ondemand.com/{SECRET}",
                             auth_mode="jwt"),
                 "url", id="httpurl-error"),
    pytest.param(with_server(url=f"https://{SECRET}@x.hana.ondemand.com/mcp",
                             auth_mode="jwt"),
                 "credentials", id="userinfo"),
    pytest.param(with_server(url=MCP, auth_mode="oauth2",
                             oauth={"client_id": "c", "client_secret": "x",
                                    "authorize_url": f"https://a.example:{SECRET}/a",
                                    "token_url": "https://a.example/t"}),
                 "oauth.authorize_url", id="oauth-url"),
    pytest.param(with_server(**OUTLOOK, oauth={"destination": "D", "mailbox": "m@example.com",
                                                "lookback": SECRET}),
                 "lookback", id="lookback"),
    pytest.param(with_server(**OUTLOOK, oauth={"destination": "D", "mailbox": "m@example.com",
                                                "lookback": f"-5{SECRET}"}),
                 "lookback", id="lookback-2"),
    pytest.param(with_server(url="builtin:jira", auth_mode="destination",
                             oauth={"destination": "D", "project": "P",
                                    "api_base": f"https://x.example/{SECRET}"}),
                 "api_base", id="api-base"),
    pytest.param(with_server(url="builtin:jira", auth_mode="destination",
                             oauth={"destination": "D", "project": "P",
                                    "api_base": f"rest/{SECRET}"}),
                 "api_base", id="api-base-relative"),
    pytest.param(with_server(url="builtin:jira", auth_mode="destination",
                             oauth={"destination": "D", "project": "P",
                                    "api_base": f"/rest?{SECRET}"}),
                 "api_base", id="api-base-query"),
    pytest.param(agent(api_slug=SECRET), "api_slug", id="api-slug"),
    pytest.param(with_server(**SMTP, oauth={"destination": "D", "recipients": SECRET}),
                 "oauth.recipients", id="recipients"),
    pytest.param(with_server(**SMTP, oauth={"destination": "D",
                                            "recipients": "a@example.com",
                                            "theme": {SECRET: "x"}}),
                 "theme", id="theme-unknown-key"),
    pytest.param(with_server(url=f"{MCP}?token={SECRET}", auth_mode="oauth2",
                             oauth={"dcr": True, "theme": {"band": "#112233"}}),
                 "oauth.theme", id="theme-on-a-url"),
    pytest.param(with_server(url="builtin:odata", auth_mode="destination",
                             oauth={"services": ["a"], SECRET: 1}),
                 "builtin:odata", id="odata-stray-key"),
    pytest.param(with_server(url="builtin:odata", auth_mode="destination",
                             oauth={"services": [SECRET]}),
                 "oauth.services", id="odata-service-name"),
    pytest.param(with_server(url="builtin:sapnotes", auth_mode="none",
                             oauth={"min_score": SECRET}),
                 "at most", id="min-score"),
]


@pytest.mark.parametrize("body, names", MESSAGES)
async def test_message_names_the_field_not_the_value(client, body, names):
    r = await client.post("/admin/api/agents", json=body)
    detail = assert_clean_422(r)
    assert any(names in item["msg"] for item in detail), detail
    # The same rule on update and inside an import bundle.
    assert_clean_422(await client.put("/admin/api/agents/1", json=body))
    assert_clean_422(await client.post("/admin/api/import", json={"agents": [body]}))


async def test_workflow_slug_is_not_echoed(client):
    r = await client.post("/admin/api/workflows", json={"name": "wf", "api_slug": SECRET})
    detail = assert_clean_422(r)
    assert detail[0]["loc"] == ["body", "api_slug"]


async def test_closed_set_values_are_still_named(client):
    """A value from a closed set is not the client's text: it stays."""
    r = await client.post("/admin/api/agents", json=with_server(
        url="builtin:jira", auth_mode="destination",
        oauth={"destination": "D", "project": "P", "user_context": True}))
    detail = assert_clean_422(r)
    assert "builtin:jira has no signed-in user" in detail[0]["msg"]


# --- unencodable text ---------------------------------------------------------

SURROGATES = [
    pytest.param("POST", "/admin/api/agents",
                 b'{"name":"a","mcp_servers":[{"url":"x","auth_mode":"\\ud83d"}]}',
                 id="agent-create"),
    pytest.param("PUT", "/admin/api/agents/1",
                 b'{"name":{"k":"\\ud83d"},"mcp_servers":[]}', id="agent-update"),
    pytest.param("POST", "/admin/api/agents",
                 b'{"name":"a","mcp_servers":[{"url":"builtin:\\ud83d","auth_mode":"none"}]}',
                 id="agent-url"),
    pytest.param("POST", "/admin/api/import", b'{"agents":"\\ud83d"}', id="import"),
    pytest.param("POST", "/admin/api/skills",
                 b'{"name":["\\udc00"],"description":"d","content":"c"}', id="skill"),
    pytest.param("POST", "/admin/api/workflows",
                 b'{"name":"w","run_timeout_seconds":"\\ud83d"}', id="workflow"),
    pytest.param("POST", "/admin/api/workflows",
                 b'{"name":"w","api_slug":"\\ud83d"}', id="workflow-slug"),
]


@pytest.mark.parametrize("method, path, raw", SURROGATES)
async def test_lone_surrogate_is_a_clean_422(client, method, path, raw):
    r = await client.request(method, path, content=raw, headers=JSON)
    assert_clean_422(r)
    r.content.decode("utf-8")  # the answer is valid UTF-8


# --- what did not change ------------------------------------------------------

async def test_catalogue_routes_keep_their_string_detail(client):
    """`agents/odata/admin_routes.py` validates in the handler and answers a
    string on purpose; the handler here must not touch it."""
    r = await client.post("/admin/api/odata/services",
                          json={"name": "Not A Name", "title": SECRET})
    assert r.status_code == 422, r.text
    assert isinstance(r.json()["detail"], str) and r.json()["detail"]
    assert SECRET not in r.text


async def test_http_exceptions_keep_their_shape(client):
    r = await client.get("/admin/api/agents/999999")
    assert r.status_code == 404
    assert r.json() == {"detail": "Agent not found"}


# --- every route, not a list of prefixes --------------------------------------

class _Other(BaseModel):
    name: str


def _bare_app() -> FastAPI:
    app = FastAPI()

    @app.post("/other")
    async def other(body: _Other) -> dict:
        return {"name": body.name}

    @app.get("/q")
    async def q(n: int) -> dict:
        return {"n": n}

    install_validation_handler(app)
    return app


async def test_a_route_outside_admin_and_ide_is_included():
    """Deny by default: a route added tomorrow under a new prefix must not
    have to remember to opt in."""
    async with AsyncClient(transport=ASGITransport(app=_bare_app()),
                           base_url="http://test") as c:
        r = await c.post("/other", json={"name": {"k": SECRET}})
        assert_clean_422(r)
        r = await c.get("/q", params={"n": SECRET})
        detail = assert_clean_422(r)
        assert detail[0]["loc"] == ["query", "n"]


def test_the_app_installs_the_shared_handler():
    from agents import validation_errors

    handler = app_module.app.exception_handlers.get(RequestValidationError)
    assert handler is validation_errors.validation_error
    # Still importable from where app.py and the IDE tests take it.
    assert install_validation_handler is validation_errors.install_validation_handler


# --- review round 1 -----------------------------------------------------------

def http_workflow(name: str, **config: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {"destination": "D", "path": "/v1/items"}
    cfg.update(config)
    return {"name": name, "steps": [{"position": 1, "kind": "http", "config": cfg}]}


def transform_workflow(name: str, **regex: Any) -> dict[str, Any]:
    return {"name": name, "steps": [
        {"position": 1, "kind": "transform", "config": {"regex": regex}}]}


def assert_refused_without(r, status: int, secret: str = SECRET) -> str:
    assert r.status_code == status, r.text
    assert secret not in r.text
    for key, value in r.headers.items():
        assert secret not in key and secret not in value
    return r.text


STEP_CONFIGS = [
    pytest.param(http_workflow("x", path=f"/v1/items?api_key={SECRET}"), "query",
                 id="path-query"),
    pytest.param(http_workflow("x", path=f"https://{SECRET}.example/v1"), "path",
                 id="path-url"),
    pytest.param(http_workflow("x", path=f"v1/{SECRET}"), "path", id="path-relative"),
    pytest.param(http_workflow("x", path=f"/v1/../{SECRET}"), "path", id="path-dots"),
    pytest.param(http_workflow("x", path=f"/v1//{SECRET}"), "path", id="path-empty-segment"),
    pytest.param(transform_workflow("x", pattern=f"({SECRET}"), "regex", id="regex"),
    pytest.param(transform_workflow("x", pattern=f"(?P<{SECRET}>a)"), "regex",
                 id="regex-group-name"),
    pytest.param(http_workflow("x", **{SECRET + " key": 1}), "<unknown field>",
                 id="unknown-config-key"),
]


@pytest.mark.parametrize("body, names", STEP_CONFIGS)
async def test_refused_step_config_is_not_echoed(client, body, names):
    """The workflow gate answers a string `detail` (400); the http path with
    a query string is exactly where a token gets pasted."""
    text = assert_refused_without(await client.post("/admin/api/workflows", json=body), 400)
    assert names in text
    # Update of an existing workflow, and the import bundle.
    r = await client.post("/admin/api/workflows", json=http_workflow("val-wf-existing"))
    assert r.status_code in (201, 400), r.text  # 400: created by an earlier case
    rows = (await client.get("/admin/api/workflows")).json()
    wf_id = next(w["id"] for w in rows if w["name"] == "val-wf-existing")
    assert_refused_without(await client.put(f"/admin/api/workflows/{wf_id}", json=body), 400)
    r = await client.post("/admin/api/import", json={"workflows": [body]})
    assert r.status_code >= 400, r.text
    assert_refused_without(r, r.status_code)


async def test_regex_flags_are_counted_not_quoted(client):
    r = await client.post("/admin/api/workflows",
                          json=transform_workflow("x", pattern="a", flags="iQZQ"))
    assert r.status_code == 400 and "3 unknown regex flag" in r.text, r.text
    assert "QZQ" not in r.text


async def test_a_known_misplaced_config_key_is_still_named(client):
    r = await client.post("/admin/api/workflows", json=http_workflow("x", bogus_key=1))
    assert r.status_code == 400 and "bogus_key" in r.text, r.text


async def test_recipients_are_named_by_position(client):
    body = with_server(**SMTP, oauth={
        "destination": "D",
        "recipients": f"a@example.com, {SECRET}, b@example.com, c@example.com, {SECRET}-2"})
    detail = assert_clean_422(await client.post("/admin/api/agents", json=body))
    assert "entries 2 and 5" in detail[0]["msg"], detail
    body = with_server(**SMTP, oauth={"destination": "D", "recipients": SECRET})
    detail = assert_clean_422(await client.post("/admin/api/agents", json=body))
    assert "entry 1" in detail[0]["msg"], detail


# pydantic's own text and locations (bare app with the IDE's contract models)

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = ""


def _contract_app() -> FastAPI:
    app = FastAPI()

    @app.post("/comment")
    async def comment(body: CommentCreate) -> dict:
        return {}

    @app.post("/strict")
    async def strict(body: Strict) -> dict:
        return {}

    install_validation_handler(app)
    return app


async def test_union_tag_message_does_not_quote_the_tag():
    async with AsyncClient(transport=ASGITransport(app=_contract_app()),
                           base_url="http://test") as c:
        r = await c.post("/comment", json={"anchor": SECRET, "body": "b"})
        detail = assert_clean_422(r)
        assert detail[0]["type"] == "union_tag_invalid"
        assert "anchor" in detail[0]["msg"] or "tag" in detail[0]["msg"]


async def test_unknown_key_is_named_only_when_it_looks_like_a_field():
    async with AsyncClient(transport=ASGITransport(app=_contract_app()),
                           base_url="http://test") as c:
        r = await c.post("/strict", json={f"https://x.example/?t={SECRET}": 1})
        detail = assert_clean_422(r)
        assert detail[0]["type"] == "extra_forbidden"
        assert detail[0]["loc"] == ["body", "<unknown field>"]
        r = await c.post("/strict", json={"tittle": 1, "x" * 65: 2, "bad\n": 3})
        locs = [d["loc"] for d in assert_clean_422(r)]
        assert ["body", "tittle"] in locs
        assert locs.count(["body", "<unknown field>"]) == 2
