"""A scripted SAP AI Core for the ``agents/aicore_deployments.py`` tests:
the token endpoint and the AI API behind an ``httpx.MockTransport``. Synthetic
hosts, ids and credentials only; nothing here reaches a network.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from agents.aicore_deployments import AICoreCredentials, AICoreDeployments

API = "https://api.example.test"
AUTH = "https://auth.example.test"
CLIENT_ID = "client-id-for-tests"
SECRET = "not-a-real-secret-123"
BODY_MARKER = "BODY-MARKER-must-not-be-logged"


def fake_creds() -> AICoreCredentials:
    return AICoreCredentials(
        client_id=CLIENT_ID, client_secret=SECRET,
        auth_url=f"{AUTH}/oauth/token", base_url=f"{API}/v2", source="test")


def model_entry(name, versions=("1",), executable="azure-openai"):
    return {"model": name, "executableId": executable,
            "versions": [{"name": v, "isLatest": i == 0, "deprecated": False}
                         for i, v in enumerate(versions)]}


def deployment_entry(model, status="RUNNING", did="d0000000001", *, style="backend"):
    details = ({"resources": {"backend_details": {"model": {"name": model, "version": "1"}}}}
               if style == "backend" else {"modelName": model})
    return {"id": did, "status": status, "details": details, "configurationId": "c1",
            "configurationName": "something-else"}


class FakeAICore:
    """Scripted token endpoint and AI API. ``fail`` maps ``"METHOD /path"``
    (or ``"token"``) to a status, a callable or an exception to raise.
    ``count_mode`` is what a list answer's ``count`` means: ``total`` (the
    whole list), ``page`` (the entries of this page) or ``absent``. Which one
    AI Core sends is not verified, so the module must read every page in
    each mode."""

    def __init__(self):
        self.models = [
            model_entry("gpt-4o", ("2024-08-06",)),
            model_entry("text-embedding-3-large", ("1", "2")),
            model_entry("text-embedding-ada-002", ("2",)),
            model_entry("anthropic--claude-4.6-opus", executable="aws-bedrock"),
        ]
        self.executables = [
            {"id": "azure-openai", "parameters": [
                {"name": "modelName", "type": "string"},
                {"name": "modelVersion", "type": "string", "default": "latest"}]},
            {"id": "aws-bedrock", "parameters": [{"name": "modelName", "type": "string"}]},
        ]
        self.deployments: list[dict] = []
        self.configurations: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.posted: list[tuple[str, dict]] = []
        self.fail: dict[str, object] = {}
        self.token_calls = 0
        self.expires_in: object = 3600
        self.token_seq = 0
        self.count_mode = "total"

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
            return self.page(self.deployments, request)
        if key == "GET /v2/lm/configurations":
            return self.page(self.configurations, request)
        if key == "POST /v2/lm/configurations":
            body = json.loads(request.content)
            self.posted.append((path, body))
            self.configurations.append({**body, "id": f"cfg-{len(self.configurations) + 1}"})
            return httpx.Response(201, json={"id": self.configurations[-1]["id"],
                                             "message": "ok"})
        if key == "POST /v2/lm/deployments":
            body = json.loads(request.content)
            self.posted.append((path, body))
            return httpx.Response(202, json={**self.add_deployment(body), "message": "ok"})
        return httpx.Response(404, text=BODY_MARKER)

    def add_deployment(self, body: dict) -> dict:
        """What a create makes: a PENDING deployment whose details name no
        model yet, only its configuration."""
        config = next(c for c in self.configurations if c["id"] == body["configurationId"])
        did = f"dnew{len(self.posted):08d}"
        self.deployments.append({"id": did, "status": "PENDING", "configurationId": config["id"],
                                 "configurationName": config["name"]})
        return {"id": did, "status": "UNKNOWN"}

    def page(self, items: list, request: httpx.Request) -> httpx.Response:
        top = int(request.url.params.get("$top", 10_000))
        skip = int(request.url.params.get("$skip", 0))
        resources = items[skip:skip + top]
        if self.count_mode == "absent":
            return httpx.Response(200, json={"resources": resources})
        count = len(items) if self.count_mode == "total" else len(resources)
        return httpx.Response(200, json={"count": count, "resources": resources})

    def api_requests(self):
        return [r for r in self.requests if r.url.host == "api.example.test"]


def client_for(server: FakeAICore, **kw) -> AICoreDeployments:
    kw.setdefault("credentials", fake_creds())
    return AICoreDeployments(transport=httpx.MockTransport(server.handler), **kw)
