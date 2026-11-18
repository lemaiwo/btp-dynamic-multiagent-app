"""Extra HTTP headers for the model call that is being made right now.

A model wrapper (the agent register's ``NexusLoggedModel``) knows things about
one model call that the HTTP client sending it does not, such as the
correlation id AI Core should label its observability record with. It
publishes them here with :func:`sending` around the wrapped call; the HTTP
client of the model reads :data:`current_call_headers` when it sends.

Standard library only and nothing of the app is imported, so any client
module can import it without a cycle.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

current_call_headers: ContextVar[dict[str, str] | None] = ContextVar(
    "current_call_headers", default=None
)


@contextmanager
def sending(headers: Mapping[str, str] | None) -> Iterator[None]:
    """Publish ``headers`` for the calls made inside the block.

    ``None`` / empty publishes nothing AND hides a binding of an enclosing
    block: a call that has no headers of its own (a direct provider's model)
    must never go out with those of another call that is still open around it.

    The binding is cleared on every exit. A token can only be reset in the
    Context it was made in: when the block is closed from another one (an
    async generator finalised by another task), resetting raises
    ``ValueError`` and the variable is set to ``None`` in the closing context
    instead, so no exit ever raises here.
    """
    token = current_call_headers.set(dict(headers) if headers else None)
    try:
        yield
    finally:
        try:
            current_call_headers.reset(token)
        except ValueError:
            current_call_headers.set(None)


__all__ = ["current_call_headers", "sending"]
