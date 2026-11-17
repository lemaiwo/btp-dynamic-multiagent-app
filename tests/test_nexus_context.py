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



def _jwt(claims):
    import jwt as pyjwt
    return pyjwt.encode(claims, key="k" * 32, algorithm="HS256")


def _capture(token, claims, *, forwarding=True):
    from agents.auth import current_claims, current_jwt
    from agents.nexus import state
    from agents.nexus.context import capture_identity
    state.reset_for_tests()
    state.set_user_forwarding(forwarding)
    marks = current_jwt.set(token), current_claims.set(claims)
    try:
        return capture_identity()
    finally:
        current_claims.reset(marks[1])
        current_jwt.reset(marks[0])
        state.reset_for_tests()


def test_capture_identity_takes_only_a_users_token():
    user = {"user_name": "alice", "origin": "idp", "user_uuid": "u-1"}
    token = _jwt(user)
    found = _capture(token, user)
    assert found is not None and found.jwt == token and found.principal == "u-1"
    assert token not in repr(found)
    origin_only = {"origin": "idp", "sub": "s"}
    assert _capture(_jwt(origin_only), origin_only).principal == "s"
    cc = {"grant_type": "client_credentials", "user_name": "x", "sub": "x"}
    assert _capture(_jwt(cc), cc) is None
    service = {"client_id": "x", "sub": "x"}
    assert _capture(_jwt(service), service) is None                   # no user claims
    assert _capture(token, None) is None                              # not validated
    assert _capture(None, user) is None                               # no token


def test_capture_identity_needs_claims_of_the_bound_token():
    user = {"user_name": "alice", "origin": "idp", "user_uuid": "u-1"}
    other = _jwt({"user_name": "bob", "origin": "idp", "user_uuid": "u-2"})
    assert _capture(other, user) is None                              # stale claims
    assert _capture("not.a.jwt", user) is None


def test_capture_identity_needs_user_forwarding():
    user = {"user_name": "alice", "origin": "idp", "user_uuid": "u-1"}
    assert _capture(_jwt(user), user, forwarding=False) is None


def test_user_forwarding_flag_defaults_off_and_set_off_clears_it():
    from agents.nexus import state
    state.reset_for_tests()
    assert state.user_forwarding() is False
    state.set_user_forwarding(True)
    assert state.user_forwarding() is True
    state.set_off("off (test)")
    assert state.user_forwarding() is False
    state.set_user_forwarding(True)
    state.reset_for_tests()
    assert state.user_forwarding() is False
