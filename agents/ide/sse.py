"""Server-sent event framing for the IDE stage runs (contract §1.3).

``format_event`` turns one runner event into an SSE frame
``event: <type>\\ndata: <json>\\n\\n``. The event types are the runner's:
``run``, ``text``, ``tool``, ``plan``, ``file``, ``artifact``, ``finding``,
``approval_required`` (the last two in diagnose sessions only), ``usage``,
``error`` and ``done``. The JSON is written with
``ensure_ascii=False``; ``json.dumps`` always escapes CR and LF inside
strings, so a multi-line text delta stays on its single ``data:`` line.

``relay`` reads ``(event, data)`` items from the queue the run emits into
and yields frames until ``done``, with a ``: ping`` comment every
``HEARTBEAT_INTERVAL_S`` seconds while nothing arrives (proxies and the
approuter drop idle connections). It only relays: closing the generator
(client disconnect) leaves the run task alone.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

HEARTBEAT_INTERVAL_S = 15.0
PING = b": ping\n\n"

# Put on the queue when the run task ends; a stream that sees it before
# ``done`` closes instead of waiting forever.
END = object()


def format_event(event: str, data: dict[str, Any]) -> bytes:
    """One SSE frame; ``data`` becomes a single line of JSON."""
    if not event or any(c in event for c in "\r\n:"):
        raise ValueError(f"invalid SSE event name {event!r}")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


async def relay(
    queue: asyncio.Queue, first: tuple[str, dict] | None = None
) -> AsyncIterator[bytes]:
    """Yield the run's events as frames until ``done`` (or the run's end)."""
    if first is not None:
        yield format_event(*first)
        if first[0] == "done":
            return
    while True:
        try:
            # Read per wait so tests (and ops) can change the interval.
            item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL_S)
        except asyncio.TimeoutError:
            yield PING
            continue
        if item is END:
            yield format_event(
                "error",
                {"message": "The run ended without a result.", "code": "run_failed"},
            )
            return
        event, data = item
        yield format_event(event, data)
        if event == "done":
            return
