"""The log queue of the agent register: bounded, one worker, never in the way.

Every model call hands two log calls to this module (``logRequest`` before,
``logResponse`` after). The register is a side channel: whatever it does, a
model call must not wait for it, fail because of it or grow memory without
bound. Hence:

* :func:`enqueue` is synchronous, never awaits and never raises. The queue is
  bounded at ``queue_max``; a full queue drops the OLDEST entry and counts it
  (the newest calls are the ones somebody may still be looking for).
* ONE worker task sends the entries in order through
  :meth:`RegisterClient.post`. Nothing is retried: a failed entry is counted
  and gone (a second try would double a record the register may have kept).
* After :data:`FAILURES_BEFORE_PAUSE` failures in a row the worker pauses
  for :data:`PAUSE_SECONDS`; entries keep queueing (and the oldest drop) in
  the meantime. After a pause ONE entry is sent as a probe: when it fails too
  the worker pauses again at once, so a register that stays down costs one
  request (one timeout) a minute, not one per model call.
* A worker whose event loop was closed (a restarted app, a test's loop) is
  not running: the next :func:`start` builds a new one on the running loop.
* A call whose technical destination does not exist (the client's
  ``destination_missing``) pauses the worker at once and calls the
  ``on_destination_missing`` callback given to :func:`start` ONCE per pause:
  the lifecycle then decides the switch again (and switches off, clearing the
  queue, when the destination service says so). No further entry costs a
  destination-service call meanwhile.
* Failures, drops and calls that went technical instead of as the user
  (``user_fallback``) are logged at most once per :data:`WARN_INTERVAL_SECONDS`,
  as counts since the previous line and the last failure's class or status.
  Never a URL, a body, a header or an exception's text: an exception from
  the HTTP layer can carry the URL and the body can carry a prompt.
* :func:`stop` flushes for a bounded time only (Cloud Foundry kills the
  process 10 s after SIGTERM), then drops the rest and says how many.

* **One identity per pair.** A model call's two entries carry the same
  :class:`Identity` (a signed-in user) or none. The REQUEST decides: when it
  went out as the technical user (the user exchange failed), the RESPONSE of
  the same ``correlationId`` is sent without the identity, technical too;
  when it went as the user, the response goes as the user (the client keeps
  that destination for it). A response whose request was never sent (dropped)
  decides on its own. The decisions are kept for at most
  :data:`PAIR_MEMORY_MAX` correlation ids. An entry, and with it the user's
  JWT, is gone once it was sent; the JWT is never logged or counted.

The clock (:data:`_now`) is a module attribute so tests move it instead of
sleeping through the pause and the warning interval.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from agents.nexus.context import Identity

logger = logging.getLogger(__name__)

FAILURES_BEFORE_PAUSE = 5
PAUSE_SECONDS = 60.0
WARN_INTERVAL_SECONDS = 300.0
DEFAULT_FLUSH_SECONDS = 3.0
PAIR_MEMORY_MAX = 10_000
_USER, _TECHNICAL = "user", "technical"

_now = time.monotonic


class _Client(Protocol):
    async def post(self, action: str, body: dict, *,
                   identity: Identity | None = None) -> Any: ...


@dataclass
class _Entry:
    """One log call waiting to be sent."""

    action: str
    body: dict
    identity: Identity | None = field(default=None, repr=False)
    correlation_id: str | None = None


_COUNTER_NAMES = ("sent", "failed", "dropped", "user_fallback")


class _State:
    """Everything the module holds; replaced whole by :func:`reset_for_tests`."""

    def __init__(self) -> None:
        self.queue: deque[_Entry] = deque()
        self.queue_max: int | None = None
        self.client: _Client | None = None
        self.worker: asyncio.Task | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.wake: asyncio.Event | None = None
        self.in_flight: _Entry | None = None
        self.waiting = False
        self.totals = dict.fromkeys(_COUNTER_NAMES, 0)
        self.since = dict.fromkeys(_COUNTER_NAMES, 0)
        self.last_failure: str | None = None
        self.last_warned: float | None = None
        self.failures_in_row = 0
        self.paused_until: float | None = None
        # correlationId -> "user" | "technical": how the request of a pair went.
        self.pair_identity: OrderedDict[str, str] = OrderedDict()
        # Called once per pause when the technical destination is missing.
        self.on_destination_missing: Callable[[], None] | None = None


_state = _State()


def _limit() -> int:
    if _state.queue_max is None:
        from agents.nexus import config
        _state.queue_max = config.settings().queue_max
    return _state.queue_max


def configure(*, queue_max: int) -> None:
    """Set the queue bound (at least 1); entries over it are dropped oldest first."""
    _state.queue_max = max(1, int(queue_max))
    _trim()


def _trim() -> None:
    limit = _limit()
    while len(_state.queue) > limit:
        _state.queue.popleft()
        _count("dropped")


def _count(name: str, n: int = 1) -> None:
    _state.totals[name] += n
    _state.since[name] += n


def enqueue(action: str, body: dict, identity: Identity | None = None) -> None:
    """Queue one log call; synchronous, never waits, never raises."""
    try:
        correlation_id = body.get("correlationId") if isinstance(body, dict) else None
        _state.queue.append(_Entry(action, body, identity,
                                   correlation_id if isinstance(correlation_id, str) else None))
        before = _state.totals["dropped"]
        _trim()
        if _state.totals["dropped"] != before:
            _maybe_warn()
        _signal()
    except Exception:  # noqa: BLE001 - a log call must never break a model call
        logger.debug("agent register: enqueue failed", exc_info=False)


def _signal() -> None:
    """Wake the worker, from its own loop or from another thread."""
    wake, loop = _state.wake, _state.loop
    if wake is None or loop is None or loop.is_closed():
        return
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if current is loop:
        wake.set()
    else:
        loop.call_soon_threadsafe(wake.set)


def wake() -> None:
    """Make the worker look at its queue and the clock again (tests, lifecycle)."""
    _signal()


def start(client: _Client, *, queue_max: int | None = None,
          on_destination_missing: Callable[[], None] | None = None) -> None:
    """Start the worker on the running loop; a running one gets the new client.

    ``on_destination_missing`` (the lifecycle's re-check) replaces the one of
    an earlier start.
    """
    if queue_max is not None:
        configure(queue_max=queue_max)
    _state.client = client
    _state.on_destination_missing = on_destination_missing
    if running():
        _signal()
        return
    _forget_worker()
    _state.loop = asyncio.get_running_loop()
    _state.wake = asyncio.Event()
    _state.worker = _state.loop.create_task(_work(), name="agent-register-sender")
    _signal()


def _loop_closed() -> bool:
    return _state.loop is not None and _state.loop.is_closed()


def running() -> bool:
    """Whether the worker task is alive (a task of a closed loop never runs again)."""
    return _state.worker is not None and not _state.worker.done() and not _loop_closed()


def _forget_worker() -> None:
    """Drop a worker that cannot run any more; what it held in flight is lost."""
    if _state.in_flight is not None:
        _count("dropped")
    _state.worker = None
    _state.loop = None
    _state.wake = None
    _state.in_flight = None
    _state.waiting = False


def pending() -> list[_Entry]:
    """The entries not yet sent (a copy, oldest first)."""
    return list(_state.queue)


def counters() -> dict[str, int]:
    """Totals since start: sent, failed, dropped, user_fallback."""
    return dict(_state.totals)


def _paused() -> bool:
    return _state.paused_until is not None and _now() < _state.paused_until


async def _work() -> None:
    state = _state
    assert state.wake is not None
    while True:
        if state.paused_until is not None:
            remaining = state.paused_until - _now()
            if remaining > 0:
                await _wait(remaining)
                continue
            state.paused_until = None
            # The next entry is a probe: one more failure pauses again.
            state.failures_in_row = FAILURES_BEFORE_PAUSE - 1
        if not state.queue:
            await _wait(None)
            continue
        entry = state.queue.popleft()
        state.in_flight = entry
        try:
            await _send(entry)
        finally:
            state.in_flight = None


async def _wait(timeout: float | None) -> None:
    wake = _state.wake
    assert wake is not None
    _state.waiting = True
    try:
        if timeout is None:
            await wake.wait()
        else:
            try:
                async with asyncio.timeout(timeout):
                    await wake.wait()
            except TimeoutError:
                pass
    finally:
        _state.waiting = False
        wake.clear()


def _identity_for(entry: _Entry) -> Identity | None:
    """The entry's identity, unless its request already went technical."""
    if entry.identity is None or entry.action != "logResponse" or entry.correlation_id is None:
        return entry.identity
    decided = _state.pair_identity.pop(entry.correlation_id, None)
    return None if decided == _TECHNICAL else entry.identity


def _remember_pair(entry: _Entry, outcome: Any) -> None:
    if entry.identity is None or entry.action != "logRequest" or entry.correlation_id is None:
        return
    sent_as = getattr(outcome, "sent_as", None)
    if sent_as not in (_USER, _TECHNICAL):
        return  # nothing went out: the response decides on its own
    memory = _state.pair_identity
    memory[entry.correlation_id] = sent_as
    memory.move_to_end(entry.correlation_id)
    while len(memory) > PAIR_MEMORY_MAX:
        memory.popitem(last=False)


async def _send(entry: _Entry) -> None:
    client = _state.client
    failure: str | None
    missing = False
    try:
        if client is None:
            raise RuntimeError
        outcome = await client.post(entry.action, entry.body, identity=_identity_for(entry))
        _remember_pair(entry, outcome)
        ok = bool(getattr(outcome, "ok", False))
        failure = None if ok else (getattr(outcome, "failure", None) or "unknown")
        if getattr(outcome, "user_fallback", False):
            _count("user_fallback")
            if ok:
                _maybe_warn()
        missing = getattr(outcome, "destination_missing", False) is True
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - only the class is kept, never the text
        ok, failure = False, type(e).__name__
    if ok:
        _count("sent")
        _state.failures_in_row = 0
        return
    _count("failed")
    _state.last_failure = failure
    _state.failures_in_row += 1
    _maybe_warn()
    if missing or _state.failures_in_row >= FAILURES_BEFORE_PAUSE:
        _state.paused_until = _now() + PAUSE_SECONDS
    if missing:
        _report_missing()


def _report_missing() -> None:
    """Ask the lifecycle once to decide the switch again (it never raises into us)."""
    callback = _state.on_destination_missing
    if callback is None:
        return
    try:
        callback()
    except Exception as e:  # noqa: BLE001 - the worker must outlive its callback
        logger.debug("agent register: re-check request failed (%s)", type(e).__name__)


def _maybe_warn() -> None:
    now = _now()
    if _state.last_warned is not None and now - _state.last_warned < WARN_INTERVAL_SECONDS:
        return
    since = _state.since
    if not since["failed"] and not since["dropped"] and not since["user_fallback"]:
        return
    logger.warning(
        "agent register: sent=%d failed=%d dropped=%d user_fallback=%d last=%s",
        since["sent"], since["failed"], since["dropped"], since["user_fallback"],
        _state.last_failure or "-",
    )
    _state.last_warned = now
    _state.since = dict.fromkeys(_COUNTER_NAMES, 0)


def _idle() -> bool:
    return not _state.queue and _state.in_flight is None


async def stop(flush_seconds: float = DEFAULT_FLUSH_SECONDS) -> int:
    """Flush for at most ``flush_seconds``, stop the worker, drop the rest.

    Returns how many log calls were dropped (in flight or still queued).
    No flush is attempted while the worker pauses after failures.
    """
    if not running():
        _forget_worker()
    worker = _state.worker
    if worker is not None and not worker.done() and flush_seconds > 0 and not _paused():
        try:
            async with asyncio.timeout(flush_seconds):
                while not _idle():
                    await asyncio.sleep(0.01)
        except TimeoutError:
            pass
    left = 0
    if worker is not None and not worker.done():
        if _state.in_flight is not None:
            left += 1
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            if _caller_cancelled():
                raise  # the caller itself was cancelled: not ours to hide
        except Exception as e:  # noqa: BLE001 - the worker's end must not stop the app's
            logger.debug("agent register: sender ended with %s", type(e).__name__)
    left += len(_state.queue)
    _state.queue.clear()
    _state.pair_identity.clear()
    _state.in_flight = None
    _state.worker = None
    _state.wake = None
    _state.loop = None
    if left:
        _state.totals["dropped"] += left
        logger.warning("agent register: %d log call(s) dropped at shutdown", left)
    return left


def _caller_cancelled() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


async def drain(timeout: float = 2.0) -> None:
    """Test helper: wait until the queue is empty and the worker idle."""
    async with asyncio.timeout(timeout):
        while not (_idle() and (_state.waiting or not running())):
            await asyncio.sleep(0.005)


async def wait_paused(timeout: float = 2.0) -> None:
    """Test helper: wait until the worker pauses after failures."""
    async with asyncio.timeout(timeout):
        while not (_paused() and _state.waiting):
            await asyncio.sleep(0.005)


def reset_for_tests() -> None:
    """Forget everything; a running worker is cancelled without waiting."""
    global _state
    if _state.worker is not None and not _state.worker.done() and not _loop_closed():
        _state.worker.cancel()
    _state = _State()
