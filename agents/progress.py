"""Per-request progress side-channel.

A delegated specialist runs inside an opaque orchestrator tool call, so its
intermediate steps (the MCP tools it calls while working) are invisible to the
chat UI. There is no first-class Pydantic AI API for forwarding a nested
agent's events to the parent run's stream yet — the feature is still an open
request upstream (pydantic/pydantic-ai#2382, milestone 2026-07, the planned
`RunContext`-based event bus).

Until that lands we bridge it ourselves with a contextvar, mirroring how
``agents.auth.current_jwt`` carries the user token into the run: the chat
endpoint installs a per-request ``sink`` before driving the orchestrator, and
each specialist's ``event_stream_handler`` calls :func:`report_progress`, which
pushes a :class:`ProgressUpdate` through that sink and into the response stream.

When the upstream event-bus API ships, only :func:`report_progress` and the
chat endpoint's sink need to change; specialists keep calling the same helper.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProgressUpdate:
    """A single 'specialist is working' notification.

    ``agent`` is the specialist name; ``text`` is a short human-readable line
    (e.g. a tool it is about to call). The chat endpoint decides how to render
    it — this type is deliberately UI-protocol agnostic so this module does not
    depend on the Vercel/AG-UI chunk types.
    """

    agent: str
    text: str


# Set by the chat endpoint per request; read indirectly via report_progress().
# Default None means "no one is listening" (e.g. A2A runs, tests), in which
# case progress reports are silently dropped.
current_progress: ContextVar[Callable[[ProgressUpdate], None] | None] = ContextVar(
    "current_progress", default=None
)


def report_progress(agent: str, text: str) -> None:
    """Forward a progress line to the active request's sink, if any.

    Safe to call from anywhere inside an agent run: when no sink is installed
    (or it raises) the call is a no-op, so specialists never fail because of
    progress reporting.
    """
    sink = current_progress.get()
    if sink is None:
        return
    try:
        sink(ProgressUpdate(agent=agent, text=text))
    except Exception:  # noqa: BLE001 — progress must never break a run
        logger.debug("progress sink raised; dropping update", exc_info=True)
