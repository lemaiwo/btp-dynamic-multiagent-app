"""Which agent makes a model call, and which run it belongs to.

The model wrapper reads both on every call and sends them to the agent
register as ``agent`` and ``runId``. They are context variables so that a
specialist reached by delegation, a deep sub-agent and any task started
inside a run see the values of the code that started them (asyncio copies
the context when a task is created).

Both context managers are synchronous on purpose: the classic chat and A2A
wrap the ``asyncio.create_task`` call that starts a run with them, and the
task must be created while the values are bound.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

ORCHESTRATOR = "orchestrator"
PERSONAL_AGENT = "personal-agent"
# The register keeps ``agent`` and ``runId`` in at most 64 characters.
MAX_ID = 64

current_agent_name: ContextVar[str] = ContextVar("nexus_agent_name", default=ORCHESTRATOR)
current_nexus_run_id: ContextVar[str | None] = ContextVar("nexus_run_id", default=None)


@dataclass(frozen=True)
class Identity:
    """The signed-in user a register call may be sent as.

    Captured when the model call is made and carried to the sender, which
    runs in another task. The token is left out of the ``repr`` so that a
    log line or a traceback that prints the object does not print it.
    """

    jwt: str = field(repr=False)
    principal: str | None = None


def capture_identity() -> Identity | None:
    """The signed-in user of the current model call, or ``None`` (technical).

    Nothing is captured unless a user destination is configured
    (:func:`agents.nexus.state.user_forwarding`). Then a user only when a JWT
    is bound AND the middleware's validated claims are a user's: not a
    client-credentials token (the job scheduler's, another application's),
    and carrying ``user_name`` or ``origin``. The principal is
    :func:`agents.auth.bound_token_principal`, which answers only when those
    claims belong to the bound JWT (compared on an unverified decode: no
    second validation, no key fetch on the model-call path), never
    ``current_principal``: in a "Run now" job that is the agent's run-as user
    while the JWT is the trigger's. Never raises: anything unexpected is
    technical.
    """
    try:
        from agents.auth import bound_token_principal, current_claims, current_jwt
        from agents.nexus import state

        if not state.user_forwarding():
            return None
        token = current_jwt.get()
        claims = current_claims.get()
        if not token or not isinstance(token, str) or not isinstance(claims, dict):
            return None
        if claims.get("grant_type") == "client_credentials":
            return None
        if not (claims.get("user_name") or claims.get("origin")):
            return None
        principal = bound_token_principal()
        if principal is None:
            return None
        return Identity(jwt=token, principal=principal)
    except Exception:  # noqa: BLE001 - logging must never break a model call
        return None


@contextmanager
def nexus_run(run_id: str) -> Iterator[str | None]:
    """Bind the run id for the calls made inside, and yield the id in effect.

    An id that is already bound wins: a run started inside another run (a
    job's agent delegating, a workflow step running an agent) belongs to the
    outer run, so all its calls group under one id. A blank id binds nothing.
    """
    outer = current_nexus_run_id.get()
    if outer is not None:
        yield outer
        return
    value = (run_id or "").strip()[:MAX_ID]
    if not value:
        yield None
        return
    token = current_nexus_run_id.set(value)
    try:
        yield value
    finally:
        current_nexus_run_id.reset(token)


@contextmanager
def calling_agent(name: str) -> Iterator[None]:
    """Name the agent whose model calls are made inside.

    Stored as given; the name is trimmed and cut when it is sent.
    """
    token = current_agent_name.set(name)
    try:
        yield
    finally:
        current_agent_name.reset(token)
