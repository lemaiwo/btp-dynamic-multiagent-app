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
each specialist's ``event_stream_handler`` reports its work here, which pushes a
:class:`ProgressUpdate` through that sink and into the response stream.

The chat endpoint (``agents/chat_app.py``) turns these updates into live UI:
``tool_start``/``tool_end`` become native tool cards (a pulsing "Running" badge
that flips to a green "Completed"); ``note`` feeds the "working…" heartbeat that
fills the otherwise-silent gaps (model calls, MCP connects, synthesis).

When the upstream event-bus API ships, only the ``report_*`` helpers and the
chat endpoint's sink need to change; specialists keep calling the same helpers.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Literal

logger = logging.getLogger(__name__)

ProgressKind = Literal[
    "tool_start", "tool_end", "note", "delegation_start", "delegation_end", "message"
]


@dataclass(frozen=True)
class ProgressUpdate:
    """A single 'specialist is working' notification.

    ``agent`` is the specialist name. ``kind`` selects how the chat endpoint
    renders it:

    - ``tool_start`` — the specialist is about to call ``tool_name`` (with
      ``args``); rendered as a tool card entering its "Running" state.
    - ``tool_end`` — that call (matched by ``tool_call_id``) returned; the card
      flips to "Completed" (``ok``) or "Error". ``output`` is a short preview.
    - ``note`` — a free-text phase hint (e.g. "Composing the answer…") used as
      the label of the live "working…" heartbeat; emits no card of its own.

    The type is deliberately UI-protocol agnostic so this module does not depend
    on the Vercel/AG-UI chunk types.
    """

    agent: str
    kind: ProgressKind
    text: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    args: Any | None = None
    output: str | None = None
    ok: bool = True


# Set by the chat endpoint per request; read indirectly via the report_* helpers.
# Default None means "no one is listening" (e.g. A2A runs, tests), in which
# case progress reports are silently dropped.
current_progress: ContextVar[Callable[[ProgressUpdate], None] | None] = ContextVar(
    "current_progress", default=None
)


def _emit(update: ProgressUpdate) -> None:
    """Forward an update to the active request's sink, if any.

    Safe to call from anywhere inside an agent run: when no sink is installed
    (or it raises) the call is a no-op, so specialists never fail because of
    progress reporting.
    """
    sink = current_progress.get()
    if sink is None:
        return
    try:
        sink(update)
    except Exception:  # noqa: BLE001 — progress must never break a run
        logger.debug("progress sink raised; dropping update", exc_info=True)


def report_tool_start(
    agent: str, tool_call_id: str, tool_name: str, args: Any | None = None
) -> None:
    """A specialist is starting an MCP tool call."""
    _emit(
        ProgressUpdate(
            agent=agent,
            kind="tool_start",
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args=args,
        )
    )


def report_tool_end(
    agent: str, tool_call_id: str, *, ok: bool = True, output: str | None = None
) -> None:
    """A specialist's MCP tool call returned (``ok``) or failed."""
    _emit(
        ProgressUpdate(
            agent=agent,
            kind="tool_end",
            tool_call_id=tool_call_id,
            ok=ok,
            output=output,
        )
    )


def report_note(agent: str, text: str) -> None:
    """Set the current 'what's happening now' phase hint for the heartbeat."""
    _emit(ProgressUpdate(agent=agent, kind="note", text=text))


def report_delegation_start(agent: str, label: str) -> None:
    """A specialist delegation became active; ``label`` is the heartbeat phase
    hint to show while it spins up. Paired with :func:`report_delegation_end`."""
    _emit(ProgressUpdate(agent=agent, kind="delegation_start", text=label))


def report_message(agent: str, text: str) -> None:
    """Emit a persistent chat message from inside a specialist run.

    Unlike a note (transient heartbeat label), this renders as its own assistant
    message bubble — used e.g. to show a clickable sign-in link while the run
    keeps going and resumes automatically once the user authorizes.
    """
    _emit(ProgressUpdate(agent=agent, kind="message", text=text))


def report_delegation_end(agent: str) -> None:
    """A specialist delegation finished (success, error, timeout, or cancel).

    The chat endpoint tracks how many are still in flight so the "composing the
    answer" phase is only shown once *all* of them are done — otherwise a fast
    specialist finishing would mislabel a turn where another is still working.
    """
    _emit(ProgressUpdate(agent=agent, kind="delegation_end"))


def report_progress(agent: str, text: str) -> None:
    """Backwards-compatible alias for :func:`report_note`."""
    report_note(agent, text)
