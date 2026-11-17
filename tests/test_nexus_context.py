"""The agent-name and run-id context variables of the agent-register logging."""
import asyncio

from agents.nexus.context import (
    ORCHESTRATOR,
    PERSONAL_AGENT,
    calling_agent,
    current_agent_name,
    current_nexus_run_id,
    nexus_run,
)


def test_defaults():
    assert current_agent_name.get() == ORCHESTRATOR == "orchestrator"
    assert PERSONAL_AGENT == "personal-agent"
    assert current_nexus_run_id.get() is None


def test_inner_nexus_run_keeps_the_outer_id():
    with nexus_run("job-1") as outer:
        with nexus_run("other") as inner:
            assert inner == outer == "job-1" == current_nexus_run_id.get()
    assert current_nexus_run_id.get() is None


def test_run_id_is_cut_to_64():
    with nexus_run("r" * 80) as rid:
        assert len(rid) == 64


async def test_tasks_inherit_and_names_reset():
    seen = []

    async def child():
        seen.append((current_agent_name.get(), current_nexus_run_id.get()))

    with nexus_run("run-1"), calling_agent("invoice-assistant"):
        await asyncio.create_task(child())
    assert seen == [("invoice-assistant", "run-1")]
    assert current_agent_name.get() == "orchestrator"


def _capture(token, claims):
    from agents.auth import current_claims, current_jwt
    from agents.nexus.context import capture_identity
    marks = current_jwt.set(token), current_claims.set(claims)
    try:
        return capture_identity()
    finally:
        current_claims.reset(marks[1])
        current_jwt.reset(marks[0])


def test_capture_identity_takes_only_a_users_token():
    user = {"user_name": "alice", "origin": "idp", "user_uuid": "u-1"}
    found = _capture("user.jwt.value", user)
    assert found is not None and found.jwt == "user.jwt.value" and found.principal == "u-1"
    assert "user.jwt.value" not in repr(found)
    assert _capture("t", {"origin": "idp", "sub": "s"}).principal == "s"
    assert _capture("t", {"grant_type": "client_credentials", "user_name": "x"}) is None
    assert _capture("t", {"client_id": "x", "sub": "x"}) is None      # no user claims
    assert _capture("t", None) is None                                # not validated
    assert _capture(None, user) is None                               # no token
