"""Fakes for the agent-register tests: a resolver, a recording transport.

Shared by every ``tests/test_nexus_*.py``. Nothing here makes a real HTTP
call: the register is an ``httpx.MockTransport``.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from dataclasses import dataclass, field

import httpx

from agents.destination import Destination

REGISTER_URL = "https://register.example.test"


class FakeResolver:
    """A ``DestinationResolver`` stand-in that records how it was asked."""

    def __init__(self, name="NEXUS_AI_PLATFORM", token="tech-token", *, fail=None,
                 per_user=None, url=REGISTER_URL, user_auth_type="OAuth2JWTBearer",
                 system_user=False, marks_per_user=True, ttl=600.0):
        self.name, self.token, self.fail, self.per_user = name, token, fail, per_user or {}
        self.url = url
        # How a resolve WITH a user token answers (a user-propagating destination by default).
        self.user_auth_type, self.system_user = user_auth_type, system_user
        self.marks_per_user, self.ttl = marks_per_user, ttl
        self.calls: list[dict] = []
        self.invalidated: list[str | None] = []
        self.property_calls: list[bool] = []   # the ``force`` of each resolve_properties
        self.hang = False                      # resolve_properties never answers

    async def resolve(self, *, force=False, user_token=None, principal=None):
        self.calls.append({"user_token": user_token, "principal": principal, "force": force})
        if self.fail is not None:
            raise self.fail
        token = self.per_user.get(user_token, self.token) if user_token else self.token
        dest = Destination(url=self.url, headers={"Authorization": f"Bearer {token}"},
                           expires_at=time.monotonic() + self.ttl)
        if user_token:
            dest = dataclasses.replace(dest, auth_type=self.user_auth_type,
                                       per_user=self.marks_per_user,
                                       system_user=self.system_user)
        return dest

    async def resolve_properties(self, *, force=False):
        self.property_calls.append(force)
        if self.hang:
            await asyncio.sleep(3600)
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
    set_cookie: str | None = None                           # answered on every call
    refuse: bool = False                    # every call: connection refused
    refuse_text: str = "connection refused"  # the text of that ConnectError
    status: int | None = None               # every action answers this status
    requests: list[dict] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request):
            action = request.url.path.rsplit("/", 1)[-1]
            self.requests.append({"action": action, "path": request.url.path,
                                  "auth": request.headers.get("Authorization"),
                                  "headers": dict(request.headers),
                                  "body": json.loads(request.content or b"{}")})
            if self.refuse:
                raise httpx.ConnectError(self.refuse_text, request=request)
            if self.hang:
                await asyncio.sleep(3600)
            headers = {"Set-Cookie": self.set_cookie} if self.set_cookie else None
            status = self.status if self.status is not None else self.statuses.get(action, 200)
            return httpx.Response(status, headers=headers,
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


async def register_on(monkeypatch, recorder, *, queue_max=2000, user=None, timeout=5):
    """Switch the register on with a recording transport (no lifecycle).

    ``user`` is the resolver of the user destination (``None``: none configured);
    ``timeout`` is the client's deadline per call.
    """
    from agents.nexus import config, logged_model, register, sender, state
    from agents.nexus.client import RegisterClient
    state.reset_for_tests()
    state.set_user_forwarding(user is not None)
    state.set_send_payload(config.settings().send_payload)   # as the lifecycle does
    state.set_landscape(config.settings().landscape)
    sender.reset_for_tests()
    register.reset_for_tests()
    logged_model.reset_for_tests()
    client = RegisterClient(FakeResolver(), user, timeout=timeout,
                            transport=recorder.transport())
    register.configure(client)
    sender.start(client, queue_max=queue_max)
    state.set_on("NEXUS_AI_PLATFORM")
    return client
