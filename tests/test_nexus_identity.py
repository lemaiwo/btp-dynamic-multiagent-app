"""The agent register: user runs are logged as the user, with a technical fallback.

A model call made while a signed-in user's JWT is bound is logged through the
user destination (the register sees the user); anything else, and any failure
of that exchange, goes through the technical destination. Both calls of one
model request use the same identity, and the JWT never reaches a log line.
"""
import logging
from contextlib import contextmanager

import jwt as pyjwt
import pytest
from pydantic_ai import Agent

from agents.auth import current_claims, current_jwt, current_principal, principal_from_token
from agents.destination import DestinationError
from agents.nexus import logged_model, register, sender, state
from agents.nexus.context import Identity, nexus_run
from agents.nexus.logged_model import wrap
from tests.nexus_helpers import FakeResolver, Recorder, register_on, scripted_model

pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")

USER_CLAIMS = {"user_name": "alice@example.test", "origin": "idp",
               "user_uuid": "11111111-2222-3333-4444-555555555555",
               "grant_type": "authorization_code"}
USER_JWT = pyjwt.encode(USER_CLAIMS, key="k" * 32, algorithm="HS256")
SERVICE_CLAIMS = {"grant_type": "client_credentials", "client_id": "x", "sub": "x"}
SERVICE_JWT = pyjwt.encode(SERVICE_CLAIMS, key="k" * 32, algorithm="HS256")
USER_TOKEN = "user-nexus-token"


@contextmanager
def bound(token, claims):
    """Bind a JWT and its validated claims, as the request middleware does."""
    jwt_mark, claims_mark = current_jwt.set(token), current_claims.set(claims)
    try:
        yield
    finally:
        current_claims.reset(claims_mark)
        current_jwt.reset(jwt_mark)


def user_resolver(**kw):
    return FakeResolver("NEXUS_AI_PLATFORM_USER", per_user={USER_JWT: USER_TOKEN}, **kw)


@pytest.fixture
async def on(monkeypatch):
    monkeypatch.delenv("NEXUS_SEND_PAYLOAD", raising=False)
    monkeypatch.delenv("NEXUS_FALLBACK_USE_CASE", raising=False)
    rec = Recorder()

    async def start(user):
        await register_on(monkeypatch, rec, user=user)
        return rec

    yield start
    await sender.stop(flush_seconds=0.1)
    state.reset_for_tests()
    sender.reset_for_tests()
    register.reset_for_tests()
    logged_model.reset_for_tests()


async def _one_model_call():
    with nexus_run("run-1"):
        await Agent(wrap(scripted_model(), "gpt-4o")).run("hi")
    await sender.drain()


def _auths(rec):
    return [(r["action"], r["auth"]) for r in rec.requests]


async def test_user_jwt_goes_through_the_user_destination(on):
    user = user_resolver()
    rec = await on(user)
    with bound(USER_JWT, USER_CLAIMS):
        await _one_model_call()
        assert _auths(rec) == [("logRequest", f"Bearer {USER_TOKEN}"),
                               ("logResponse", f"Bearer {USER_TOKEN}")]
        assert user.calls and all(c["user_token"] == USER_JWT for c in user.calls)
        assert sender.counters()["user_fallback"] == 0


async def test_client_credentials_token_is_technical(on):
    user = user_resolver()
    rec = await on(user)
    with bound(SERVICE_JWT, SERVICE_CLAIMS):
        await _one_model_call()
        assert _auths(rec) == [("logRequest", "Bearer tech-token"),
                               ("logResponse", "Bearer tech-token")]
        assert user.calls == [] and sender.counters()["user_fallback"] == 0


async def test_no_jwt_is_technical(on):
    user = user_resolver()
    rec = await on(user)
    await _one_model_call()
    assert _auths(rec) == [("logRequest", "Bearer tech-token"),
                           ("logResponse", "Bearer tech-token")]
    assert user.calls == []


async def test_no_user_destination_is_technical(on):
    rec = await on(None)
    with bound(USER_JWT, USER_CLAIMS):
        await _one_model_call()
        assert _auths(rec) == [("logRequest", "Bearer tech-token"),
                               ("logResponse", "Bearer tech-token")]
        assert sender.counters()["user_fallback"] == 0


async def test_exchange_fails_falls_back_to_technical_and_counts(on):
    user = user_resolver(fail=DestinationError("x"))
    rec = await on(user)
    with bound(USER_JWT, USER_CLAIMS):
        await _one_model_call()
        assert _auths(rec) == [("logRequest", "Bearer tech-token"),
                               ("logResponse", "Bearer tech-token")]
        counts = sender.counters()
        assert counts["user_fallback"] == 1          # the request decided; the response followed
        assert counts["sent"] == 2 and counts["failed"] == 0 and counts["dropped"] == 0


async def test_pair_never_split(on):
    identity = Identity(jwt=USER_JWT, principal="alice")
    # The request went as the user; the exchange then fails: the response stays the user's.
    user = user_resolver()
    rec = await on(user)
    sender.enqueue("logRequest", {"correlationId": "c-1"}, identity)
    await sender.drain()
    user.fail = DestinationError("x")
    sender.enqueue("logResponse", {"correlationId": "c-1"}, identity)
    await sender.drain()
    assert _auths(rec) == [("logRequest", f"Bearer {USER_TOKEN}"),
                           ("logResponse", f"Bearer {USER_TOKEN}")]
    # The request fell back; the exchange then works again: the response stays technical.
    rec.requests.clear()
    sender.enqueue("logRequest", {"correlationId": "c-2"}, identity)
    await sender.drain()
    user.fail = None
    calls_before = len(user.calls)
    sender.enqueue("logResponse", {"correlationId": "c-2"}, identity)
    await sender.drain()
    assert _auths(rec) == [("logRequest", "Bearer tech-token"),
                           ("logResponse", "Bearer tech-token")]
    assert len(user.calls) == calls_before        # the response did not even ask
    assert sender.counters()["user_fallback"] == 1


async def test_registration_is_always_technical(on, monkeypatch):
    user = user_resolver()
    rec = await on(user)
    with bound(USER_JWT, USER_CLAIMS):

        async def rows():
            return []

        monkeypatch.setattr(register, "_load_rows", rows)
        rec.answers["registerAgents"] = {"value": []}
        assert await register.push_once()
        assert _auths(rec) == [("registerAgents", "Bearer tech-token")]
        assert user.calls == []


async def test_jwt_never_logged(on, caplog):
    caplog.set_level(logging.DEBUG)
    # The exchange fails with the token in its text; the register refuses; then a 401.
    user = user_resolver(fail=DestinationError(f"exchange of {USER_JWT} refused"))
    rec = await on(user)
    with bound(USER_JWT, USER_CLAIMS):
        rec.statuses["logRequest"] = 500
        await _one_model_call()
        user.fail = None
        rec.statuses["logRequest"] = 401
        await _one_model_call()
        assert len(rec.requests) == 4
        assert f"Bearer {USER_TOKEN}" in [r["auth"] for r in rec.requests]
        for text in (USER_JWT, USER_TOKEN, "eyJ"):
            assert text not in caplog.text
        assert all(USER_JWT not in str(k) for k in sender.counters())


async def test_job_run_principal_comes_from_the_jwt(on):
    user = user_resolver()
    await on(user)
    with bound(USER_JWT, USER_CLAIMS):
        mark = current_principal.set("svc-runas")        # "Run now": the run-as user is bound
        try:
            await _one_model_call()
        finally:
            current_principal.reset(mark)
        assert user.calls
        assert {c["principal"] for c in user.calls} == {principal_from_token(USER_JWT)}
        assert principal_from_token(USER_JWT) != "svc-runas"
