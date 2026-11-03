"""``agents/aicore_deployments.py``: the AI Core calls behind the embeddings
panel (design addendum l, point 3), against the scripted AI Core of
``tests/aicore_helpers.py`` on a MockTransport. No network, no AI Core, no
database.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from urllib.parse import parse_qs

import httpx
import pytest

from agents import aicore_deployments as aid
from agents.aicore_deployments import AICoreDeployments, AICoreError
from tests.aicore_helpers import (
    API,
    AUTH,
    BODY_MARKER,
    CLIENT_ID,
    SECRET,
    FakeAICore,
    client_for,
    deployment_entry,
    fake_creds,
    model_entry,
)

ENV_NAMES = ("AICORE_CLIENT_ID", "AICORE_CLIENT_SECRET", "AICORE_AUTH_URL",
             "AICORE_BASE_URL", "AICORE_RESOURCE_GROUP", "AICORE_SERVICE_KEY",
             "VCAP_SERVICES")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """A developer's ``.env`` may hold real AI Core credentials, and a test
    that resolves them must never see them (nor reach AI Core: every client
    here is built on a MockTransport)."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    aid.reset()
    yield
    aid.reset()


async def _refused(coro) -> AICoreError:
    with pytest.raises(AICoreError) as info:
        await coro
    return info.value


def _own_log(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records if r.name == aid.__name__)


def _assert_clean(text: str) -> None:
    for forbidden in (SECRET, CLIENT_ID, BODY_MARKER, "https://", "token-"):
        assert forbidden not in text


# --- credentials -------------------------------------------------------------

def _binding(**credentials) -> str:
    base = {"clientid": CLIENT_ID, "clientsecret": SECRET, "url": AUTH,
            "serviceurls": {"AI_API_URL": API}}
    base.update(credentials)
    return json.dumps({"aicore": [{"label": "aicore", "name": "agent-aicore",
                                   "credentials": base}]})


def test_credentials_come_from_the_aicore_binding(monkeypatch):
    monkeypatch.setenv("VCAP_SERVICES", _binding())
    creds = aid.resolve_credentials()
    assert creds.source == "binding"
    assert creds.client_id == CLIENT_ID and creds.client_secret == SECRET
    assert creds.auth_url == f"{AUTH}/oauth/token"
    assert creds.base_url == f"{API}/v2"


def test_the_binding_is_found_by_label_and_urls_are_not_doubled(monkeypatch):
    vcap = {"user-provided": [{"label": "user-provided", "credentials": {}}],
            "aicore": [{"label": "aicore", "credentials": {
                "clientid": CLIENT_ID, "clientsecret": SECRET, "url": f"{AUTH}/oauth/token/",
                "serviceurls": {"AI_API_URL": f"{API}/v2/"}}}]}
    monkeypatch.setenv("VCAP_SERVICES", json.dumps(vcap))
    creds = aid.resolve_credentials()
    assert creds.auth_url == f"{AUTH}/oauth/token"
    assert creds.base_url == f"{API}/v2"


def test_env_credentials_win_over_the_binding_as_in_the_sdk(monkeypatch):
    # gen_ai_hub (AICoreV2Client.from_env) reads AICORE_* before the binding:
    # deploying into another tenant than the one the embeddings are sent to
    # would show a deployment the app never uses.
    monkeypatch.setenv("VCAP_SERVICES", _binding(clientid="other-id"))
    monkeypatch.setenv("AICORE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("AICORE_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("AICORE_AUTH_URL", AUTH)
    monkeypatch.setenv("AICORE_BASE_URL", API)
    creds = aid.resolve_credentials()
    assert creds.source == "env"
    assert creds.client_id == CLIENT_ID
    assert creds.auth_url == f"{AUTH}/oauth/token"
    assert creds.base_url == f"{API}/v2"


def test_incomplete_env_is_unavailable_and_not_mixed_with_the_binding(monkeypatch, caplog):
    monkeypatch.setenv("VCAP_SERVICES", _binding())
    monkeypatch.setenv("AICORE_CLIENT_ID", CLIENT_ID)
    with caplog.at_level(logging.WARNING, logger=aid.__name__):
        with pytest.raises(AICoreError) as info:
            aid.resolve_credentials()
    assert info.value.code == "aicore_unavailable"
    log = _own_log(caplog)
    assert "AICORE_CLIENT_SECRET" in log  # the name of what is missing, never a value
    _assert_clean(log)


@pytest.mark.parametrize("vcap", [
    None,
    "not json {",
    json.dumps({"aicore": "not a list"}),
    json.dumps({"xsuaa": [{"label": "xsuaa", "credentials": {}}]}),
    json.dumps({"aicore": [{"label": "aicore", "credentials": "not an object"}]}),
    # an x509 binding: no client secret
    _binding(clientsecret=None),
    _binding(serviceurls={}),
    _binding(url="http://auth.example.test"),
    _binding(url=f"https://user:{SECRET}@auth.example.test"),
])
def test_no_usable_credentials_is_aicore_unavailable(monkeypatch, caplog, vcap):
    if vcap is not None:
        monkeypatch.setenv("VCAP_SERVICES", vcap)
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        with pytest.raises(AICoreError) as info:
            aid.resolve_credentials()
    assert info.value.code == "aicore_unavailable"
    assert SECRET not in str(info.value) and "https://" not in str(info.value)
    _assert_clean(_own_log(caplog))


def test_a_service_key_variable_is_refused_with_a_hint(monkeypatch, caplog):
    # the SDK reads AICORE_SERVICE_KEY before everything else: answering from
    # the binding here would deploy into another tenant than the model calls use
    monkeypatch.setenv("VCAP_SERVICES", _binding())
    monkeypatch.setenv("AICORE_SERVICE_KEY", json.dumps({"clientid": CLIENT_ID,
                                                         "clientsecret": SECRET}))
    with caplog.at_level(logging.WARNING, logger=aid.__name__):
        with pytest.raises(AICoreError) as info:
            aid.resolve_credentials()
    assert info.value.code == "aicore_unavailable"
    assert "AICORE_SERVICE_KEY" in _own_log(caplog)
    _assert_clean(_own_log(caplog))


def test_a_loopback_http_url_is_accepted_for_local_work(monkeypatch):
    monkeypatch.setenv("AICORE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("AICORE_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("AICORE_AUTH_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AICORE_BASE_URL", "http://localhost:9/v2")
    creds = aid.resolve_credentials()
    assert creds.auth_url == "http://127.0.0.1:9/oauth/token"


def test_credentials_repr_masks_everything_but_the_source():
    text = repr(fake_creds()) + str(fake_creds())
    assert "test" in text
    _assert_clean(text)
    assert "example.test" not in text


def test_resource_group_default_and_env(monkeypatch):
    assert aid.resource_group() == "default"
    monkeypatch.setenv("AICORE_RESOURCE_GROUP", "  ")
    assert aid.resource_group() == "default"
    monkeypatch.setenv("AICORE_RESOURCE_GROUP", " embed-rg ")
    assert aid.resource_group() == "embed-rg"


def test_error_codes_are_a_closed_list_with_fixed_texts():
    assert aid.ERROR_CODES == frozenset(
        {"aicore_unavailable", "token_failed", "model_unknown", "deploy_failed"})
    for code in aid.ERROR_CODES:
        err = AICoreError(code)
        assert err.code == code and err.message
    with pytest.raises(ValueError):
        AICoreError("something_else")


# --- token -------------------------------------------------------------------

async def test_token_is_fetched_with_client_credentials_and_cached_until_expiry():
    server = FakeAICore()
    now = [1000.0]
    client = client_for(server, clock=lambda: now[0])
    await client.list_models()
    await client.list_deployments()
    assert server.token_calls == 1
    token_request = next(r for r in server.requests if r.url.host == "auth.example.test")
    assert token_request.method == "POST"
    assert parse_qs(token_request.content.decode()) == {"grant_type": ["client_credentials"]}
    basic = base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
    assert token_request.headers["Authorization"] == f"Basic {basic}"
    for r in server.api_requests():
        assert r.headers["Authorization"] == "Bearer token-1"
        assert r.headers["AI-Resource-Group"] == "default"
    now[0] += 3600 - 30  # inside the safety margin before expiry
    await client.list_models()
    assert server.token_calls == 2
    assert server.api_requests()[-1].headers["Authorization"] == "Bearer token-2"


async def test_concurrent_calls_on_a_fresh_client_fetch_one_token():
    server = FakeAICore()
    client = client_for(server)
    await asyncio.gather(client.list_models(), client.list_deployments(), client.list_models())
    assert server.token_calls == 1


async def test_a_token_without_a_lifetime_is_used_once_and_not_cached():
    server = FakeAICore()
    server.expires_in = "soon"
    client = client_for(server)
    await client.list_models()
    await client.list_models()
    assert server.token_calls == 2


async def test_the_resource_group_header_follows_the_client_or_the_environment(monkeypatch):
    monkeypatch.setenv("AICORE_RESOURCE_GROUP", "embed-rg")
    server = FakeAICore()
    await client_for(server).list_models()
    await client_for(server, group="pinned").list_models()
    groups = [r.headers["AI-Resource-Group"] for r in server.api_requests()]
    assert groups == ["embed-rg", "pinned"]


@pytest.mark.parametrize("failure", [
    401, 500,
    lambda request: httpx.Response(200, text=f"<html>{BODY_MARKER}</html>"),
    lambda request: httpx.Response(200, json={"token_type": "bearer"}),
    lambda request: httpx.Response(200, json={"access_token": ""}),
    httpx.ConnectError("refused"),
])
async def test_token_failure_is_token_failed_and_nothing_else_is_sent(caplog, failure):
    server = FakeAICore()
    server.fail["token"] = failure
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        err = await _refused(client_for(server).list_models())
    assert err.code == "token_failed"
    assert server.api_requests() == []
    _assert_clean(_own_log(caplog) + str(err))


async def test_a_401_from_ai_core_fetches_a_new_token_once():
    server = FakeAICore()
    seen = []

    def once(request):
        seen.append(request.headers["Authorization"])
        if len(seen) == 1:
            return httpx.Response(401, text=BODY_MARKER)
        return httpx.Response(200, json={"count": 1,
                                         "resources": [deployment_entry("text-embedding-3-large")]})

    server.fail["GET /v2/lm/deployments"] = once
    out = await client_for(server).list_deployments()
    assert seen == ["Bearer token-1", "Bearer token-2"]
    assert [d["model"] for d in out] == ["text-embedding-3-large"]


async def test_a_second_401_is_aicore_unavailable(caplog):
    server = FakeAICore()
    server.fail["GET /v2/lm/deployments"] = 401
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        err = await _refused(client_for(server).list_deployments())
    assert err.code == "aicore_unavailable"
    assert server.token_calls == 2
    assert "401" in _own_log(caplog)
    _assert_clean(_own_log(caplog))


# --- list_models -------------------------------------------------------------

async def test_list_models_keeps_embedding_models_with_their_versions():
    server = FakeAICore()
    server.models += [
        {"model": 42, "versions": []},
        {"executableId": "azure-openai"},
        {"model": "Gemini-Embedding-001", "versions": [{"name": "001"}, {"name": 7}, "x"]},
        {"model": "bad\nembedding-name", "versions": []},
        {"model": "text-embedding-3-small", "versions": "not a list"},
    ]
    out = await client_for(server).list_models()
    assert out == [
        {"model": "Gemini-Embedding-001", "versions": ["001"]},
        {"model": "text-embedding-3-large", "versions": ["1", "2"]},
        {"model": "text-embedding-3-small", "versions": []},
        {"model": "text-embedding-ada-002", "versions": ["2"]},
    ]
    request = server.api_requests()[0]
    assert request.url.path == "/v2/lm/scenarios/foundation-models/models"


async def test_a_model_whose_name_says_embed_is_an_embedding_model():
    # Bedrock's and NVIDIA's names say "embed", not "embedding"
    server = FakeAICore()
    server.models = [model_entry("gpt-4o"),
                     model_entry("amazon--titan-embed-text", ("2",), "aws-bedrock")]
    server.deployments = [deployment_entry("gpt-4o", "RUNNING", "d00000000g1"),
                          deployment_entry("amazon--titan-embed-text", "RUNNING", "d00000000t1")]
    client = client_for(server)
    assert await client.list_models() == [
        {"model": "amazon--titan-embed-text", "versions": ["2"]}]
    assert await client.list_deployments() == [
        {"model": "amazon--titan-embed-text", "status": "RUNNING",
         "deployment_id": "d00000000t1"}]


@pytest.mark.parametrize("failure", [
    403, 500, 302,
    lambda request: httpx.Response(200, text=f"<html>{BODY_MARKER}</html>"),
    lambda request: httpx.Response(200, json=[1, 2]),
    lambda request: httpx.Response(200, json={"resources": "nope"}),
    httpx.ReadTimeout("slow"),
    httpx.ConnectError("refused"),
])
async def test_list_models_failure_is_aicore_unavailable_without_the_body(caplog, failure):
    server = FakeAICore()
    server.fail["GET /v2/lm/scenarios/foundation-models/models"] = failure
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        err = await _refused(client_for(server).list_models())
    assert err.code == "aicore_unavailable"
    log = _own_log(caplog)
    assert log  # the failure is logged, by status or class
    _assert_clean(log + str(err))


async def test_an_answer_over_the_cap_is_refused(monkeypatch, caplog):
    monkeypatch.setattr(aid, "MAX_RESPONSE_BYTES", 1000)
    server = FakeAICore()
    server.models = [model_entry(f"text-embedding-{i}") for i in range(50)]
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        err = await _refused(client_for(server).list_models())
    assert err.code == "aicore_unavailable"
    assert "large" in _own_log(caplog)


async def test_requests_use_30_second_timeouts():
    server = FakeAICore()
    await client_for(server).list_models()
    assert aid.REQUEST_TIMEOUT_SECONDS == 30.0
    for request in server.requests:
        assert request.extensions["timeout"] == {
            "connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0}


async def test_a_request_over_the_total_deadline_is_aicore_unavailable(monkeypatch, caplog):
    # httpx's timeouts are per read; a server that drips its answer is held
    # to one deadline for the whole request.
    monkeypatch.setattr(aid, "REQUEST_TIMEOUT_SECONDS", 0.05)
    server = FakeAICore()

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"resources": []})

    inner = server.handler

    async def handler(request):
        if request.url.path.endswith("/models"):
            return await slow(request)
        return await inner(request)

    client = AICoreDeployments(credentials=fake_creds(), transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        err = await _refused(client.list_models())
    assert err.code == "aicore_unavailable"
    assert "deadline" in _own_log(caplog)


# --- list_deployments --------------------------------------------------------

async def test_list_deployments_answers_embedding_deployments_only():
    server = FakeAICore()
    server.deployments = [
        deployment_entry("gpt-4o", "RUNNING", "d00000000a1"),
        deployment_entry("text-embedding-3-large", "RUNNING", "d00000000b2"),
        deployment_entry("text-embedding-ada-002", "PENDING", "d00000000c3", style="modelName"),
        deployment_entry("text-embedding-3-large", "STOPPED", "d00000000a0"),
        deployment_entry("text-embedding-3-small", 7, "d00000000d4"),
        {"id": "d00000000e5", "status": "RUNNING", "details": None},
        {"status": "RUNNING", "details": {"modelName": "text-embedding-3-large"}},
        deployment_entry("text-embedding-3-large", "RUNNING", "bad id\n"),
    ]
    out = await client_for(server).list_deployments()
    assert out == [
        {"model": "text-embedding-3-large", "status": "STOPPED", "deployment_id": "d00000000a0"},
        {"model": "text-embedding-3-large", "status": "RUNNING", "deployment_id": "d00000000b2"},
        # a status that cannot be read is UNKNOWN: it still counts as live, so
        # deploy() never creates a second, billed deployment beside it
        {"model": "text-embedding-3-small", "status": "UNKNOWN", "deployment_id": "d00000000d4"},
        {"model": "text-embedding-ada-002", "status": "PENDING", "deployment_id": "d00000000c3"},
    ]
    request = server.api_requests()[0]
    assert request.url.path == "/v2/lm/deployments"
    assert request.url.params["scenarioId"] == "foundation-models"


async def test_a_fresh_deployment_without_details_counts_under_its_configuration_name():
    server = FakeAICore()
    server.deployments = [
        {"id": "d000000fresh", "status": "PENDING",
         "configurationName": "text-embedding-3-large-embeddings"},
        # not a configuration this module named: no model to tell
        {"id": "d000000other", "status": "PENDING", "configurationName": "gpt-4o-embeddings"},
        {"id": "d000000plain", "status": "PENDING", "configurationName": "my-config"},
        # a model the details name wins over the configuration's name
        {**deployment_entry("gpt-4o", "RUNNING", "d00000000gpt"),
         "configurationName": "text-embedding-3-large-embeddings"},
    ]
    client = client_for(server)
    assert await client.list_deployments() == [
        {"model": "text-embedding-3-large", "status": "PENDING", "deployment_id": "d000000fresh"}]
    out = await client.deploy("text-embedding-3-large")
    assert out == {"deployment_id": "d000000fresh", "status": "PENDING", "created": False}
    assert server.posted == []


COUNT_MODES = ["total", "page", "absent"]


def _stopped(ids):
    return [deployment_entry("text-embedding-ada-002", "STOPPED", f"d{i:011d}") for i in ids]


def _list_pages(server):
    return [(r.url.params["$top"], r.url.params.get("$skip")) for r in server.api_requests()
            if r.method == "GET" and r.url.path == "/v2/lm/deployments"]


@pytest.mark.parametrize("count_mode", COUNT_MODES)
async def test_deployments_are_read_page_by_page_until_a_short_page(count_mode):
    # STOPPED and DEAD deployments pile up; the RUNNING one is on page two.
    # With ``count`` as the page length, page one says 100 of 100: the list
    # is read on all the same
    server = FakeAICore()
    server.count_mode = count_mode
    server.deployments = _stopped(range(130))
    server.deployments.append(deployment_entry("text-embedding-3-large", "RUNNING", "d0000running"))
    client = client_for(server)
    assert len(await client.list_deployments()) == 131
    out = await client.deploy("text-embedding-3-large")
    assert out == {"deployment_id": "d0000running", "status": "RUNNING", "created": False}
    assert server.posted == []
    assert _list_pages(server) == [("100", None), ("100", "100")] * 2
    assert all(r.url.params["scenarioId"] == "foundation-models" for r in server.api_requests()
               if r.url.path == "/v2/lm/deployments")


@pytest.mark.parametrize("count_mode", COUNT_MODES)
async def test_a_full_last_page_is_followed_by_one_more_read(count_mode):
    # 200 entries are two full pages: only the empty third page ends the list
    server = FakeAICore()
    server.count_mode = count_mode
    server.deployments = _stopped(range(199))
    server.deployments.append(deployment_entry("text-embedding-3-large", "RUNNING", "d0000running"))
    out = await client_for(server).deploy("text-embedding-3-large")
    assert out == {"deployment_id": "d0000running", "status": "RUNNING", "created": False}
    assert server.posted == []
    assert _list_pages(server) == [("100", None), ("100", "100"), ("100", "200")]


@pytest.mark.parametrize("count_mode", COUNT_MODES)
async def test_an_empty_list_is_one_read_and_deploys(count_mode):
    server = FakeAICore()
    server.count_mode = count_mode
    out = await client_for(server).deploy("text-embedding-3-large")
    assert out["created"] is True
    assert _list_pages(server) == [("100", None)]


def _answer(count, resources):
    return httpx.Response(200, json={"count": count, "resources": resources})


def _answering(count, resources):
    return lambda request: _answer(count, resources)


def _pages_ignoring_skip():
    # every read answers page one: page two repeats it
    return _answering(150, _stopped(range(100)))


def _pages_that_overlap():
    # adds up to the count, but page two repeats half of page one: an entry
    # removed between the reads shifted the list, and one was never seen
    calls = []

    def answer(request):
        calls.append(1)
        return _answer(150, _stopped(range(100) if len(calls) == 1 else range(50, 100)))
    return answer


def _count_that_moves():
    # a deployment created between the two reads: page two states 151 while
    # the two pages hold 150, so one entry of the list was not read
    calls = []

    def answer(request):
        calls.append(1)
        if len(calls) == 1:
            return _answer(150, _stopped(range(100)))
        return _answer(151, _stopped(range(100, 150)))
    return answer


def _pages_short_of_the_count():
    # a server whose pages are shorter than $top: the short first page ends
    # the read, and only a total ``count`` tells that 80 entries are missing
    def answer(request):
        skip = int(request.url.params.get("$skip", 0))
        return _answer(130, _stopped(range(skip, skip + 50)))
    return answer


# Factories: a stateful answer is built inside the test, never at collection.
@pytest.mark.parametrize("make_answer", [
    _pages_ignoring_skip,
    _pages_that_overlap,
    _count_that_moves,
    _pages_short_of_the_count,
    lambda: _answering("3", []),
    lambda: _answering(True, []),
    lambda: _answering(-1, []),
    lambda: _answering(5, []),  # an empty page before the count is reached
    lambda: _answering(0, [deployment_entry("text-embedding-3-large")]),
    lambda: lambda request: httpx.Response(200, json={"count": 1, "resources": "x"}),
], ids=["skip-ignored", "overlap", "count-moves", "short-of-count", "count-text", "count-bool",
        "count-negative", "empty-before-count", "more-than-count", "no-list"])
async def test_pages_that_do_not_add_up_are_aicore_unavailable_and_nothing_is_made(make_answer,
                                                                                caplog):
    server = FakeAICore()
    server.fail["GET /v2/lm/deployments"] = make_answer()
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        err = await _refused(client_for(server).deploy("text-embedding-3-large"))
    assert err.code == "aicore_unavailable"
    assert server.posted == []
    assert "deployment list failed" in _own_log(caplog)
    _assert_clean(_own_log(caplog))


async def test_more_pages_than_the_limit_are_refused(monkeypatch):
    monkeypatch.setattr(aid, "PAGE_SIZE", 2)
    monkeypatch.setattr(aid, "MAX_PAGES", 3)
    server = FakeAICore()
    server.deployments = [deployment_entry("text-embedding-ada-002", "STOPPED", f"d{i:011d}")
                          for i in range(7)]
    err = await _refused(client_for(server).list_deployments())
    assert err.code == "aicore_unavailable"


async def test_list_deployments_failure_is_aicore_unavailable(caplog):
    server = FakeAICore()
    server.fail["GET /v2/lm/deployments"] = 503
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        err = await _refused(client_for(server).list_deployments())
    assert err.code == "aicore_unavailable"
    assert "503" in _own_log(caplog)
    _assert_clean(_own_log(caplog))


# --- deploy ------------------------------------------------------------------

@pytest.mark.parametrize("status", ["RUNNING", "PENDING", "UNKNOWN"])
async def test_deploy_answers_a_live_deployment_and_creates_nothing(status):
    server = FakeAICore()
    server.deployments = [deployment_entry("text-embedding-3-large", status, "d000000live")]
    out = await client_for(server).deploy("text-embedding-3-large")
    assert out == {"deployment_id": "d000000live", "status": status, "created": False}
    assert server.posted == []
    assert all(r.method == "GET" for r in server.api_requests())


async def test_deploy_prefers_a_running_deployment_over_a_pending_one():
    server = FakeAICore()
    server.deployments = [
        deployment_entry("text-embedding-3-large", "PENDING", "d00000pending"),
        deployment_entry("text-embedding-3-large", "RUNNING", "d00000running"),
    ]
    out = await client_for(server).deploy("text-embedding-3-large")
    assert out["deployment_id"] == "d00000running"


async def test_deploy_creates_the_configuration_and_the_deployment(caplog):
    server = FakeAICore()
    server.deployments = [deployment_entry("text-embedding-3-large", "STOPPED", "d0000stopped")]
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        out = await client_for(server).deploy("text-embedding-3-large")
    assert out == {"deployment_id": "dnew00000002", "status": "UNKNOWN", "created": True}
    (cfg_path, cfg), (dep_path, dep) = server.posted
    assert cfg_path == "/v2/lm/configurations"
    assert cfg == {
        "name": "text-embedding-3-large-embeddings",
        "executableId": "azure-openai",
        "scenarioId": "foundation-models",
        "parameterBindings": [{"key": "modelName", "value": "text-embedding-3-large"},
                              {"key": "modelVersion", "value": "latest"}],
        "inputArtifactBindings": [],
    }
    assert dep_path == "/v2/lm/deployments" and dep == {"configurationId": "cfg-1"}
    _assert_clean(_own_log(caplog))


async def test_deploy_binds_no_model_version_when_the_executable_declares_none():
    server = FakeAICore()
    server.models.append(model_entry("amazon--titan-embedding-v2", executable="aws-bedrock"))
    await client_for(server).deploy("amazon--titan-embedding-v2")
    cfg = server.posted[0][1]
    assert cfg["executableId"] == "aws-bedrock"
    assert cfg["parameterBindings"] == [{"key": "modelName", "value": "amazon--titan-embedding-v2"}]


async def test_deploy_takes_the_executable_whose_model_name_enum_lists_the_model():
    # the lookup of scripts/deploy_claude.py (find_executable_for_model) first
    server = FakeAICore()
    server.models.append(model_entry("text-embedding-x", executable="azure-openai"))
    server.executables.append({"id": "special-exec", "parameters": [
        {"name": "modelName", "constraints": {"enum": ["text-embedding-x"]}}]})
    await client_for(server).deploy("text-embedding-x")
    assert server.posted[0][1]["executableId"] == "special-exec"


async def test_deploy_without_an_executable_is_deploy_failed_and_creates_nothing(caplog):
    server = FakeAICore()
    server.models.append(model_entry("text-embedding-orphan", executable="gone-exec"))
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        err = await _refused(client_for(server).deploy("text-embedding-orphan"))
    assert err.code == "deploy_failed"
    assert server.posted == []
    assert "executable" in _own_log(caplog)


@pytest.mark.parametrize("model", ["gpt-4o", "text-embedding-never-offered", "", None, 42,
                                   "text-embedding-3-large\nINJECTED"])
async def test_deploy_of_a_model_the_scenario_does_not_offer_is_model_unknown(model):
    server = FakeAICore()
    err = await _refused(client_for(server).deploy(model))
    assert err.code == "model_unknown"
    assert server.posted == []


async def test_deploy_refuses_a_non_string_before_anything_is_sent():
    server = FakeAICore()
    await _refused(client_for(server).deploy(["text-embedding-3-large"]))
    assert server.requests == []


async def test_a_refused_configuration_is_deploy_failed_and_no_deployment_is_made(caplog):
    server = FakeAICore()
    server.fail["POST /v2/lm/configurations"] = 400
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        err = await _refused(client_for(server).deploy("text-embedding-3-large"))
    assert err.code == "deploy_failed"
    assert not any(r.method == "POST" and r.url.path == "/v2/lm/deployments"
                   for r in server.requests)
    assert "400" in _own_log(caplog)
    _assert_clean(_own_log(caplog) + str(err))


@pytest.mark.parametrize("failure", [500, httpx.ReadTimeout("slow"),
                                     lambda request: httpx.Response(202, json={"status": "x"})])
async def test_a_failed_deployment_create_is_deploy_failed_and_never_repeated(caplog, failure):
    server = FakeAICore()
    server.fail["POST /v2/lm/deployments"] = failure
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        err = await _refused(client_for(server).deploy("text-embedding-3-large"))
    assert err.code == "deploy_failed"
    creates = [r for r in server.requests
               if r.method == "POST" and r.url.path == "/v2/lm/deployments"]
    assert len(creates) == 1
    _assert_clean(_own_log(caplog) + str(err))


async def test_a_retry_after_a_failed_deployment_create_reuses_the_configuration():
    server = FakeAICore()
    server.fail["POST /v2/lm/deployments"] = 500
    client = client_for(server)
    assert (await _refused(client.deploy("text-embedding-3-large"))).code == "deploy_failed"
    del server.fail["POST /v2/lm/deployments"]
    out = await client.deploy("text-embedding-3-large")
    assert out["created"] is True
    configs = [b for p, b in server.posted if p.endswith("/configurations")]
    assert len(configs) == 1
    deployment_posts = [json.loads(r.content) for r in server.requests
                        if r.method == "POST" and r.url.path == "/v2/lm/deployments"]
    assert deployment_posts == [{"configurationId": "cfg-1"}, {"configurationId": "cfg-1"}]
    configuration_lists = [r for r in server.api_requests()
                           if r.url.path == "/v2/lm/configurations" and r.method == "GET"]
    assert configuration_lists[0].url.params["scenarioId"] == "foundation-models"
    assert configuration_lists[0].url.params["$top"] == "100"


@pytest.mark.parametrize(("failure", "detail"), [
    (503, "configuration list failed"),
    # count 2: two entries that the count allows, one id twice
    (lambda request: _answer(2, [{"id": "cfg-x"}, {"id": "cfg-x"}]),
     "configuration list failed (an entry came on two pages)"),
], ids=["status", "duplicate id"])
async def test_an_unreadable_configuration_list_makes_a_new_configuration(
    failure, detail, caplog
):
    # a second configuration costs nothing; the deployment list, read in
    # full before, is what keeps a model from being deployed (and billed) twice
    server = FakeAICore()
    server.fail["GET /v2/lm/configurations"] = failure
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        out = await client_for(server).deploy("text-embedding-3-large")
    assert out["created"] is True
    assert [p for p, _ in server.posted] == ["/v2/lm/configurations", "/v2/lm/deployments"]
    assert server.posted[1][1] == {"configurationId": "cfg-1"}
    log = _own_log(caplog)
    assert detail in log
    assert "configuration list not read (aicore_unavailable)" in log
    _assert_clean(log)


async def test_a_failed_deployment_list_still_stops_the_deploy_before_any_configuration():
    server = FakeAICore()
    server.fail["GET /v2/lm/deployments"] = 503
    err = await _refused(client_for(server).deploy("text-embedding-3-large"))
    assert err.code == "aicore_unavailable"
    assert server.posted == []
    assert not any(r.url.path == "/v2/lm/configurations" for r in server.requests)


@pytest.mark.parametrize("change", [
    {"executableId": "aws-bedrock"},
    {"parameterBindings": [{"key": "modelName", "value": "text-embedding-3-large"}]},
    {"parameterBindings": [{"key": "modelName", "value": "text-embedding-3-large"},
                           {"key": "modelVersion", "value": "1"}]},
    {"name": "text-embedding-3-large-embeddings-2"},
    {"inputArtifactBindings": [{"key": "x", "artifactId": "a1"}]},
    {"scenarioId": "other"},
])
async def test_a_configuration_that_differs_is_not_reused(change):
    server = FakeAICore()
    server.configurations = [{
        "id": "cfg-old", "name": "text-embedding-3-large-embeddings",
        "executableId": "azure-openai", "scenarioId": "foundation-models",
        "parameterBindings": [{"key": "modelVersion", "value": "latest"},
                              {"key": "modelName", "value": "text-embedding-3-large"}],
        "inputArtifactBindings": [], **change}]
    await client_for(server).deploy("text-embedding-3-large")
    assert [p for p, _ in server.posted] == ["/v2/lm/configurations", "/v2/lm/deployments"]
    assert server.posted[1][1] == {"configurationId": "cfg-2"}


async def test_an_equal_configuration_is_reused_whatever_the_order_of_its_bindings():
    server = FakeAICore()
    server.configurations = [{
        "id": "cfg-old", "name": "text-embedding-3-large-embeddings",
        "executableId": "azure-openai", "scenarioId": "foundation-models",
        "parameterBindings": [{"key": "modelVersion", "value": "latest"},
                              {"key": "modelName", "value": "text-embedding-3-large"}],
        "inputArtifactBindings": []}]
    out = await client_for(server).deploy("text-embedding-3-large")
    assert out["created"] is True
    assert server.posted == [("/v2/lm/deployments", {"configurationId": "cfg-old"})]


@pytest.mark.parametrize("lost", ["timeout", "500", "no id"])
async def test_an_unconfirmed_create_that_was_made_is_answered_from_the_list(lost, caplog):
    server = FakeAICore()

    def made_but_lost(request):
        server.add_deployment(json.loads(request.content))
        if lost == "timeout":
            raise httpx.ReadTimeout("lost")
        if lost == "500":
            return httpx.Response(500, text=BODY_MARKER)
        return httpx.Response(202, json={"message": "ok"})

    server.fail["POST /v2/lm/deployments"] = made_but_lost
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        out = await client_for(server).deploy("text-embedding-3-large")
    assert out["created"] is True and out["status"] == "PENDING"
    assert out["deployment_id"] == server.deployments[-1]["id"]
    creates = [r for r in server.requests
               if r.method == "POST" and r.url.path == "/v2/lm/deployments"]
    assert len(creates) == 1
    assert "unconfirmed" in _own_log(caplog)
    _assert_clean(_own_log(caplog))


async def test_an_unconfirmed_create_whose_list_fails_is_deploy_failed():
    server = FakeAICore()
    calls = []

    def listing(request):
        calls.append(1)
        if len(calls) > 1:
            return httpx.Response(503)
        return server.page(server.deployments, request)

    server.fail["GET /v2/lm/deployments"] = listing
    server.fail["POST /v2/lm/deployments"] = 500
    err = await _refused(client_for(server).deploy("text-embedding-3-large"))
    assert err.code == "deploy_failed"


async def test_a_401_on_the_deployment_create_is_repeated_once_with_a_new_token():
    server = FakeAICore()
    tokens = []

    def refused_once(request):
        tokens.append(request.headers["Authorization"])
        if len(tokens) == 1:
            return httpx.Response(401, text=BODY_MARKER)
        return httpx.Response(202, json=server.add_deployment(json.loads(request.content)))

    server.fail["POST /v2/lm/deployments"] = refused_once
    out = await client_for(server).deploy("text-embedding-3-large")
    assert out["created"] is True
    assert tokens == ["Bearer token-1", "Bearer token-2"]


async def test_a_second_401_on_the_deployment_create_is_deploy_failed(caplog):
    server = FakeAICore()
    server.fail["POST /v2/lm/deployments"] = 401
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        err = await _refused(client_for(server).deploy("text-embedding-3-large"))
    assert err.code == "deploy_failed"
    creates = [r for r in server.requests
               if r.method == "POST" and r.url.path == "/v2/lm/deployments"]
    assert [r.headers["Authorization"] for r in creates] == ["Bearer token-1", "Bearer token-2"]
    assert "HTTP 401" in _own_log(caplog)


async def test_a_deployment_create_over_the_total_deadline_is_deploy_failed_and_sent_once(
        monkeypatch, caplog):
    monkeypatch.setattr(aid, "REQUEST_TIMEOUT_SECONDS", 0.05)
    server = FakeAICore()

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(202, json={"id": "dlate0000001"})

    inner = server.handler

    async def handler(request):
        if request.method == "POST" and request.url.path == "/v2/lm/deployments":
            server.requests.append(request)
            return await slow(request)
        return await inner(request)

    client = AICoreDeployments(credentials=fake_creds(), transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        err = await _refused(client.deploy("text-embedding-3-large"))
    assert err.code == "deploy_failed"
    assert "deployment create failed (deadline" in _own_log(caplog)
    creates = [r for r in server.requests
               if r.method == "POST" and r.url.path == "/v2/lm/deployments"]
    assert len(creates) == 1


async def test_a_listing_failure_during_deploy_is_aicore_unavailable_and_creates_nothing():
    server = FakeAICore()
    server.fail["GET /v2/lm/deployments"] = 500
    err = await _refused(client_for(server).deploy("text-embedding-3-large"))
    assert err.code == "aicore_unavailable"
    assert server.posted == []


async def test_a_token_failure_during_deploy_is_token_failed():
    server = FakeAICore()
    server.fail["token"] = 401
    err = await _refused(client_for(server).deploy("text-embedding-3-large"))
    assert err.code == "token_failed"


async def test_two_deploys_at_once_in_one_process_create_one_deployment():
    server = FakeAICore()
    client = client_for(server)
    first, second = await asyncio.gather(client.deploy("text-embedding-3-large"),
                                         client.deploy("text-embedding-3-large"))
    assert sorted([first["created"], second["created"]]) == [False, True]
    assert first["deployment_id"] == second["deployment_id"]
    assert len([p for p, _ in server.posted if p.endswith("/deployments")]) == 1


# --- the module functions ----------------------------------------------------

async def test_module_functions_use_one_default_client(monkeypatch):
    server = FakeAICore()
    monkeypatch.setattr(aid, "_default", client_for(server))
    assert [m["model"] for m in await aid.list_models()][0] == "text-embedding-3-large"
    assert await aid.list_deployments() == []
    out = await aid.deploy("text-embedding-ada-002")
    assert out["created"] is True
    assert server.token_calls == 1  # one client, one cached token


def test_default_client_is_built_lazily_and_reset_drops_it(monkeypatch):
    first = aid.default_client()
    assert aid.default_client() is first
    aid.reset()
    assert aid.default_client() is not first


async def test_the_default_client_resolves_credentials_at_the_first_call(monkeypatch):
    # building it never fails; a landscape without AI Core answers the code
    err = await _refused(aid.list_models())
    assert err.code == "aicore_unavailable"


def test_the_module_does_not_import_the_database_layer():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(aid))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any(name.startswith("agents") for name in imported)
    assert "sqlalchemy" not in imported
