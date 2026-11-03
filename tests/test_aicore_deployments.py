"""``agents/aicore_deployments.py``: the AI Core calls behind the embeddings
panel (design addendum l, point 3), against a MockTransport. No network, no
AI Core, no database.
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
from agents.aicore_deployments import AICoreCredentials, AICoreDeployments, AICoreError

API = "https://api.example.test"
AUTH = "https://auth.example.test"
CLIENT_ID = "client-id-for-tests"
SECRET = "not-a-real-secret-123"
BODY_MARKER = "BODY-MARKER-must-not-be-logged"
ENV_NAMES = ("AICORE_CLIENT_ID", "AICORE_CLIENT_SECRET", "AICORE_AUTH_URL",
             "AICORE_BASE_URL", "AICORE_RESOURCE_GROUP", "VCAP_SERVICES")


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


def _creds() -> AICoreCredentials:
    return AICoreCredentials(
        client_id=CLIENT_ID, client_secret=SECRET,
        auth_url=f"{AUTH}/oauth/token", base_url=f"{API}/v2", source="test")


def _model(name, versions=("1",), executable="azure-openai"):
    return {"model": name, "executableId": executable,
            "versions": [{"name": v, "isLatest": i == 0, "deprecated": False}
                         for i, v in enumerate(versions)]}


def _deployment(model, status="RUNNING", did="d0000000001", *, style="backend"):
    details = ({"resources": {"backend_details": {"model": {"name": model, "version": "1"}}}}
               if style == "backend" else {"modelName": model})
    return {"id": did, "status": status, "details": details, "configurationId": "c1"}


class FakeAICore:
    """Scripted token endpoint and AI API. ``fail`` maps ``"METHOD /path"``
    (or ``"token"``) to a status, a callable or an exception to raise."""

    def __init__(self):
        self.models = [
            _model("gpt-4o", ("2024-08-06",)),
            _model("text-embedding-3-large", ("1", "2")),
            _model("text-embedding-ada-002", ("2",)),
            _model("anthropic--claude-4.6-opus", executable="aws-bedrock"),
        ]
        self.executables = [
            {"id": "azure-openai", "parameters": [
                {"name": "modelName", "type": "string"},
                {"name": "modelVersion", "type": "string", "default": "latest"}]},
            {"id": "aws-bedrock", "parameters": [{"name": "modelName", "type": "string"}]},
        ]
        self.deployments: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.posted: list[tuple[str, dict]] = []
        self.fail: dict[str, object] = {}
        self.token_calls = 0
        self.expires_in: object = 3600
        self.token_seq = 0

    def _fail(self, key, request):
        rule = self.fail.get(key)
        if rule is None:
            return None
        if isinstance(rule, BaseException):
            raise rule
        if callable(rule):
            return rule(request)
        return httpx.Response(rule, text=f"error {BODY_MARKER} {SECRET}")

    async def handler(self, request: httpx.Request) -> httpx.Response:
        # A real request suspends; without this two concurrent calls on a
        # MockTransport would never interleave.
        await asyncio.sleep(0)
        self.requests.append(request)
        if request.url.host == "auth.example.test":
            self.token_calls += 1
            failed = self._fail("token", request)
            if failed is not None:
                return failed
            self.token_seq += 1
            return httpx.Response(200, json={
                "access_token": f"token-{self.token_seq}", "token_type": "bearer",
                "expires_in": self.expires_in})
        path = request.url.path
        key = f"{request.method} {path}"
        failed = self._fail(key, request)
        if failed is not None:
            return failed
        if key == "GET /v2/lm/scenarios/foundation-models/models":
            return httpx.Response(200, json={"count": len(self.models), "resources": self.models})
        if key == "GET /v2/lm/scenarios/foundation-models/executables":
            return httpx.Response(200, json={"resources": self.executables})
        if key == "GET /v2/lm/deployments":
            return httpx.Response(200, json={"resources": self.deployments})
        if key == "POST /v2/lm/configurations":
            body = json.loads(request.content)
            self.posted.append((path, body))
            return httpx.Response(201, json={"id": f"cfg-{len(self.posted)}", "message": "ok"})
        if key == "POST /v2/lm/deployments":
            body = json.loads(request.content)
            self.posted.append((path, body))
            did = f"dnew{len(self.posted):08d}"
            model = next(b["parameterBindings"][0]["value"] for p, b in self.posted
                         if p.endswith("/configurations"))
            self.deployments.append(_deployment(model, "PENDING", did))
            return httpx.Response(202, json={"id": did, "status": "UNKNOWN", "message": "ok"})
        return httpx.Response(404, text=BODY_MARKER)

    def api_requests(self):
        return [r for r in self.requests if r.url.host == "api.example.test"]


def _client(server: FakeAICore, **kw) -> AICoreDeployments:
    kw.setdefault("credentials", _creds())
    return AICoreDeployments(transport=httpx.MockTransport(server.handler), **kw)


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


def test_a_loopback_http_url_is_accepted_for_local_work(monkeypatch):
    monkeypatch.setenv("AICORE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("AICORE_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("AICORE_AUTH_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AICORE_BASE_URL", "http://localhost:9/v2")
    creds = aid.resolve_credentials()
    assert creds.auth_url == "http://127.0.0.1:9/oauth/token"


def test_credentials_repr_masks_everything_but_the_source():
    text = repr(_creds()) + str(_creds())
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
    client = _client(server, clock=lambda: now[0])
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
    client = _client(server)
    await asyncio.gather(client.list_models(), client.list_deployments(), client.list_models())
    assert server.token_calls == 1


async def test_a_token_without_a_lifetime_is_used_once_and_not_cached():
    server = FakeAICore()
    server.expires_in = "soon"
    client = _client(server)
    await client.list_models()
    await client.list_models()
    assert server.token_calls == 2


async def test_the_resource_group_header_follows_the_client_or_the_environment(monkeypatch):
    monkeypatch.setenv("AICORE_RESOURCE_GROUP", "embed-rg")
    server = FakeAICore()
    await _client(server).list_models()
    await _client(server, group="pinned").list_models()
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
        err = await _refused(_client(server).list_models())
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
        return httpx.Response(200, json={"resources": [_deployment("text-embedding-3-large")]})

    server.fail["GET /v2/lm/deployments"] = once
    out = await _client(server).list_deployments()
    assert seen == ["Bearer token-1", "Bearer token-2"]
    assert [d["model"] for d in out] == ["text-embedding-3-large"]


async def test_a_second_401_is_aicore_unavailable(caplog):
    server = FakeAICore()
    server.fail["GET /v2/lm/deployments"] = 401
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        err = await _refused(_client(server).list_deployments())
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
    out = await _client(server).list_models()
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
    server.models = [_model("gpt-4o"), _model("amazon--titan-embed-text", ("2",), "aws-bedrock")]
    server.deployments = [_deployment("gpt-4o", "RUNNING", "d00000000g1"),
                          _deployment("amazon--titan-embed-text", "RUNNING", "d00000000t1")]
    client = _client(server)
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
        err = await _refused(_client(server).list_models())
    assert err.code == "aicore_unavailable"
    log = _own_log(caplog)
    assert log  # the failure is logged, by status or class
    _assert_clean(log + str(err))


async def test_an_answer_over_the_cap_is_refused(monkeypatch, caplog):
    monkeypatch.setattr(aid, "MAX_RESPONSE_BYTES", 1000)
    server = FakeAICore()
    server.models = [_model(f"text-embedding-{i}") for i in range(50)]
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        err = await _refused(_client(server).list_models())
    assert err.code == "aicore_unavailable"
    assert "large" in _own_log(caplog)


async def test_requests_use_30_second_timeouts():
    server = FakeAICore()
    await _client(server).list_models()
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

    client = AICoreDeployments(credentials=_creds(), transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        err = await _refused(client.list_models())
    assert err.code == "aicore_unavailable"
    assert "deadline" in _own_log(caplog)


# --- list_deployments --------------------------------------------------------

async def test_list_deployments_answers_embedding_deployments_only():
    server = FakeAICore()
    server.deployments = [
        _deployment("gpt-4o", "RUNNING", "d00000000a1"),
        _deployment("text-embedding-3-large", "RUNNING", "d00000000b2"),
        _deployment("text-embedding-ada-002", "PENDING", "d00000000c3", style="modelName"),
        _deployment("text-embedding-3-large", "STOPPED", "d00000000a0"),
        _deployment("text-embedding-3-small", 7, "d00000000d4"),
        {"id": "d00000000e5", "status": "RUNNING", "details": None},
        {"status": "RUNNING", "details": {"modelName": "text-embedding-3-large"}},
        _deployment("text-embedding-3-large", "RUNNING", "bad id\n"),
    ]
    out = await _client(server).list_deployments()
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


async def test_list_deployments_failure_is_aicore_unavailable(caplog):
    server = FakeAICore()
    server.fail["GET /v2/lm/deployments"] = 503
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        err = await _refused(_client(server).list_deployments())
    assert err.code == "aicore_unavailable"
    assert "503" in _own_log(caplog)
    _assert_clean(_own_log(caplog))


# --- deploy ------------------------------------------------------------------

@pytest.mark.parametrize("status", ["RUNNING", "PENDING", "UNKNOWN"])
async def test_deploy_answers_a_live_deployment_and_creates_nothing(status):
    server = FakeAICore()
    server.deployments = [_deployment("text-embedding-3-large", status, "d000000live")]
    out = await _client(server).deploy("text-embedding-3-large")
    assert out == {"deployment_id": "d000000live", "status": status, "created": False}
    assert server.posted == []
    assert all(r.method == "GET" for r in server.api_requests())


async def test_deploy_prefers_a_running_deployment_over_a_pending_one():
    server = FakeAICore()
    server.deployments = [
        _deployment("text-embedding-3-large", "PENDING", "d00000pending"),
        _deployment("text-embedding-3-large", "RUNNING", "d00000running"),
    ]
    out = await _client(server).deploy("text-embedding-3-large")
    assert out["deployment_id"] == "d00000running"


async def test_deploy_creates_the_configuration_and_the_deployment(caplog):
    server = FakeAICore()
    server.deployments = [_deployment("text-embedding-3-large", "STOPPED", "d0000stopped")]
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        out = await _client(server).deploy("text-embedding-3-large")
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
    server.models.append(_model("amazon--titan-embedding-v2", executable="aws-bedrock"))
    await _client(server).deploy("amazon--titan-embedding-v2")
    cfg = server.posted[0][1]
    assert cfg["executableId"] == "aws-bedrock"
    assert cfg["parameterBindings"] == [{"key": "modelName", "value": "amazon--titan-embedding-v2"}]


async def test_deploy_takes_the_executable_whose_model_name_enum_lists_the_model():
    # the lookup of scripts/deploy_claude.py (find_executable_for_model) first
    server = FakeAICore()
    server.models.append(_model("text-embedding-x", executable="azure-openai"))
    server.executables.append({"id": "special-exec", "parameters": [
        {"name": "modelName", "constraints": {"enum": ["text-embedding-x"]}}]})
    await _client(server).deploy("text-embedding-x")
    assert server.posted[0][1]["executableId"] == "special-exec"


async def test_deploy_without_an_executable_is_deploy_failed_and_creates_nothing(caplog):
    server = FakeAICore()
    server.models.append(_model("text-embedding-orphan", executable="gone-exec"))
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        err = await _refused(_client(server).deploy("text-embedding-orphan"))
    assert err.code == "deploy_failed"
    assert server.posted == []
    assert "executable" in _own_log(caplog)


@pytest.mark.parametrize("model", ["gpt-4o", "text-embedding-never-offered", "", None, 42,
                                   "text-embedding-3-large\nINJECTED"])
async def test_deploy_of_a_model_the_scenario_does_not_offer_is_model_unknown(model):
    server = FakeAICore()
    err = await _refused(_client(server).deploy(model))
    assert err.code == "model_unknown"
    assert server.posted == []


async def test_deploy_refuses_a_non_string_before_anything_is_sent():
    server = FakeAICore()
    await _refused(_client(server).deploy(["text-embedding-3-large"]))
    assert server.requests == []


async def test_a_refused_configuration_is_deploy_failed_and_no_deployment_is_made(caplog):
    server = FakeAICore()
    server.fail["POST /v2/lm/configurations"] = 400
    with caplog.at_level(logging.DEBUG, logger=aid.__name__):
        err = await _refused(_client(server).deploy("text-embedding-3-large"))
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
        err = await _refused(_client(server).deploy("text-embedding-3-large"))
    assert err.code == "deploy_failed"
    creates = [r for r in server.requests
               if r.method == "POST" and r.url.path == "/v2/lm/deployments"]
    assert len(creates) == 1
    _assert_clean(_own_log(caplog) + str(err))


async def test_a_listing_failure_during_deploy_is_aicore_unavailable_and_creates_nothing():
    server = FakeAICore()
    server.fail["GET /v2/lm/deployments"] = 500
    err = await _refused(_client(server).deploy("text-embedding-3-large"))
    assert err.code == "aicore_unavailable"
    assert server.posted == []


async def test_a_token_failure_during_deploy_is_token_failed():
    server = FakeAICore()
    server.fail["token"] = 401
    err = await _refused(_client(server).deploy("text-embedding-3-large"))
    assert err.code == "token_failed"


async def test_two_deploys_at_once_in_one_process_create_one_deployment():
    server = FakeAICore()
    client = _client(server)
    first, second = await asyncio.gather(client.deploy("text-embedding-3-large"),
                                         client.deploy("text-embedding-3-large"))
    assert sorted([first["created"], second["created"]]) == [False, True]
    assert first["deployment_id"] == second["deployment_id"]
    assert len([p for p, _ in server.posted if p.endswith("/deployments")]) == 1


# --- the module functions ----------------------------------------------------

async def test_module_functions_use_one_default_client(monkeypatch):
    server = FakeAICore()
    monkeypatch.setattr(aid, "_default", _client(server))
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
