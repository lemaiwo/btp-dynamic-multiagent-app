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
from dataclasses import dataclass, replace
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
    - ``delegation_start`` / ``delegation_end`` — a delegation tool began or
      finished consulting ``agent``. ``delegation_id`` names it (the
      delegation tool call's id); the start carries ``parent`` (the delegation
      it runs inside, ``None`` at the top) and ``prompt`` (the query, raw: a
      sink previews and masks it), the end ``ok``, ``output`` (a masked
      preview), ``usage`` (the specialist run's own tokens, ``None`` when it
      did not complete) and ``duration_ms``.

    Every update that sets no ``delegation_id`` gets it from
    :data:`current_delegation`, so a specialist's tool calls say which
    delegation they ran under without any change to its handler.

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
    delegation_id: str | None = None
    parent: str | None = None
    prompt: str | None = None
    usage: dict[str, Any] | None = None
    duration_ms: int | None = None


# Set by the chat endpoint per request; read indirectly via the report_* helpers.
# Default None means "no one is listening" (e.g. A2A runs, tests), in which
# case progress reports are silently dropped.
current_progress: ContextVar[Callable[[ProgressUpdate], None] | None] = ContextVar(
    "current_progress", default=None
)


# The delegation the current code runs inside: set by the delegation tool
# (``agents.registry``) around the specialist's whole run and reset in the same
# task, so the specialist's progress updates carry it (contextvars are copied
# into the tasks the run creates). ``None`` outside any delegation.
current_delegation: ContextVar[str | None] = ContextVar("current_delegation", default=None)


def is_interactive() -> bool:
    """True when someone is watching live and can act on a sign-in prompt.

    A sink is interactive unless it says otherwise: the chat's sink is a plain
    callable, while a background recorder (``agents.run_activity``) sets
    ``interactive = False`` -- it wants the progress, but nobody behind it can
    click a sign-in link, so the registry must not wait for one.
    """
    sink = current_progress.get()
    return sink is not None and getattr(sink, "interactive", True)


def _emit(update: ProgressUpdate) -> None:
    """Forward an update to the active request's sink, if any.

    Safe to call from anywhere inside an agent run: when no sink is installed
    (or it raises) the call is a no-op, so specialists never fail because of
    progress reporting.
    """
    sink = current_progress.get()
    if sink is None:
        return
    if update.delegation_id is None:
        delegation = current_delegation.get()
        if delegation is not None:
            update = replace(update, delegation_id=delegation)
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


def report_delegation_start(
    agent: str, label: str, *, delegation_id: str | None = None, query: str | None = None
) -> None:
    """A specialist delegation became active; ``label`` is the heartbeat phase
    hint to show while it spins up. Paired with :func:`report_delegation_end`.

    Call it BEFORE binding :data:`current_delegation` to ``delegation_id``:
    the value bound at this moment is the delegation this one runs inside
    (``parent``)."""
    _emit(ProgressUpdate(
        agent=agent, kind="delegation_start", text=label, delegation_id=delegation_id,
        parent=current_delegation.get(), prompt=query,
    ))


def report_message(agent: str, text: str) -> None:
    """Emit a persistent chat message from inside a specialist run.

    Unlike a note (transient heartbeat label), this renders as its own assistant
    message bubble — used e.g. to show a clickable sign-in link while the run
    keeps going and resumes automatically once the user authorizes.
    """
    _emit(ProgressUpdate(agent=agent, kind="message", text=text))


def report_delegation_end(
    agent: str,
    *,
    delegation_id: str | None = None,
    ok: bool = True,
    output: str | None = None,
    usage: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> None:
    """A specialist delegation finished (success, error, timeout, or cancel).

    The chat endpoint tracks how many are still in flight so the "composing the
    answer" phase is only shown once *all* of them are done — otherwise a fast
    specialist finishing would mislabel a turn where another is still working.
    ``usage`` is the specialist run's own share (numbers and the model name
    only), ``None`` for a delegation that did not complete.
    """
    _emit(ProgressUpdate(
        agent=agent, kind="delegation_end", delegation_id=delegation_id, ok=ok,
        output=output, usage=usage, duration_ms=duration_ms,
    ))


def report_progress(agent: str, text: str) -> None:
    """Backwards-compatible alias for :func:`report_note`."""
    report_note(agent, text)
