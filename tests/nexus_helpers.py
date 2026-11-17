"""Fakes for the agent-register tests: a resolver, a recording transport.

Shared by every ``tests/test_nexus_*.py``. Nothing here makes a real HTTP
call: the register is an ``httpx.MockTransport``.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import httpx

from agents.destination import Destination

REGISTER_URL = "https://register.example.test"


class FakeResolver:
    """A ``DestinationResolver`` stand-in that records how it was asked."""

    def __init__(self, name="NEXUS_AI_PLATFORM", token="tech-token", *, fail=None,
                 per_user=None, url=REGISTER_URL):
        self.name, self.token, self.fail, self.per_user = name, token, fail, per_user or {}
        self.url = url
        self.calls: list[dict] = []
        self.invalidated: list[str | None] = []

    async def resolve(self, *, force=False, user_token=None, principal=None):
        self.calls.append({"user_token": user_token, "principal": principal, "force": force})
        if self.fail is not None:
            raise self.fail
        token = self.per_user.get(user_token, self.token) if user_token else self.token
        return Destination(url=self.url, headers={"Authorization": f"Bearer {token}"},
                           expires_at=time.monotonic() + 600)

    async def resolve_properties(self, *, force=False):
        if self.fail is not None:
            raise self.fail
        return {"Name": self.name}

    def invalidate(self, principal=None):
        self.invalidated.append(principal)


@dataclass
class Recorder:
    """httpx transport that records register calls and answers by script."""

    statuses: dict[str, int] = field(default_factory=dict)   # action -> status
    answers: dict[str, object] = field(default_factory=dict)  # action -> JSON
    hang: bool = False
    requests: list[dict] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request):
            action = request.url.path.rsplit("/", 1)[-1]
            self.requests.append({"action": action, "path": request.url.path,
                                  "auth": request.headers.get("Authorization"),
                                  "headers": dict(request.headers),
                                  "body": json.loads(request.content or b"{}")})
            if self.hang:
                await asyncio.sleep(3600)
            return httpx.Response(self.statuses.get(action, 200),
                                  json=self.answers.get(action, {"accepted": True}))
        return httpx.MockTransport(handler)

    def of(self, action):
        return [r for r in self.requests if r["action"] == action]


def scripted_model(usages=((10, 2),), *, name="scripted", finish_reason=None, fail=None):
    """FunctionModel answering text with the given (input, output) tokens per call, in turn."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.usage import RequestUsage

    seq = list(usages)

    def answer(messages, info):
        if fail is not None:
            raise fail
        i, o = seq.pop(0) if len(seq) > 1 else seq[0]
        return ModelResponse(parts=[TextPart("ok")],
                             usage=RequestUsage(input_tokens=i, output_tokens=o),
                             model_name=name, finish_reason=finish_reason)
    return FunctionModel(answer, model_name=name)


async def register_on(monkeypatch, recorder, *, queue_max=2000):
    """Switch the register on with a recording transport (no lifecycle)."""
    from agents.nexus import logged_model, register, sender, state
    from agents.nexus.client import RegisterClient
    state.reset_for_tests()
    sender.reset_for_tests()
    register.reset_for_tests()
    logged_model.reset_for_tests()
    client = RegisterClient(FakeResolver(), timeout=5, transport=recorder.transport())
    register.configure(client)
    sender.start(client, queue_max=queue_max)
    state.set_on("NEXUS_AI_PLATFORM")
    return client
