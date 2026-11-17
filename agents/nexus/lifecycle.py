"""Start-up, Reload re-evaluation, the re-push timer and shutdown of the agent register logging.

The switch (``agents.nexus.state``) is decided here and nowhere else:

* no destination service bound: off, nothing is started, no timer, no HTTP;
* the destination service says the destination does not exist (its 404,
  ``DestinationNotFound``): off; the timer's only call each period is that
  same lookup at the destination service, never the register;
* the destination exists, or cannot be resolved right now (any other error,
  a timeout included): on. The sender treats an unreachable register like any
  failed send.

While on, the sender worker and the registration pusher run on one
``RegisterClient``: the technical destination, plus the user destination when
``NEXUS_USER_DESTINATION`` names one that exists (only then is a signed-in
user's token captured at a model call: ``state.set_user_forwarding``).

An evaluation never raises into its caller (the app's lifespan, an admin
Reload, the timer) and is bounded by ``NEXUS_TIMEOUT_SECONDS`` as a whole, so a
hanging destination service cannot hold up the app's start. One INFO line is
logged for every change of the result, plus one saying whether user forwarding
is on. Nothing logged here holds a token or an exception's text.

The module keeps one process-wide state, like ``sender`` and ``register``.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from agents.destination import (
    DestinationNotFound,
    DestinationResolver,
    DestinationServiceConfig,
    config_from_environment,
)
from agents.nexus import config, register, sender, state
from agents.nexus.client import RegisterClient

logger = logging.getLogger(__name__)

PREFIX = "agent register integration: "
OFF_NO_SERVICE = "off (no destination service)"
OFF_CHECK_FAILED = "off (check failed)"
OFF_STOPPED = "off (stopped)"
DEFAULT_FLUSH_SECONDS = 3.0

# Test seam: the transport of every RegisterClient built here (None: the network).
_transport: httpx.AsyncBaseTransport | None = None


def _off_not_found(name: str) -> str:
    return f"off (destination {name} not found)"


@dataclass
class _State:
    lock: asyncio.Lock | None = None
    client: RegisterClient | None = None
    # (technical resolver, user resolver or None) the client was built from.
    client_key: tuple[Any, Any] | None = None
    # One resolver per (destination name, service binding): keeps its caches.
    resolvers: dict[tuple[str, DestinationServiceConfig], Any] = field(default_factory=dict)
    service_bound: bool = False
    timer: asyncio.Task | None = None
    # Clients replaced while on, closed once a send in flight has had its time.
    retiring: set[asyncio.Task] = field(default_factory=set)
    retired: list[RegisterClient] = field(default_factory=list)
    logged_reason: str | None = None
    logged_forwarding: str | None = None
    # Set by stop(): a Reload or the timer racing shutdown must not restart anything.
    stopping: bool = False
    # An evaluation that raised while on was warned about (once per failure streak).
    failure_warned: bool = False


_state = _State()


def _period_seconds() -> float:
    """The re-push period (a seam: tests make it short)."""
    return config.settings().repush_minutes * 60.0


def timer_running() -> bool:
    """Whether the periodic re-check and re-push task is alive."""
    return _state.timer is not None and not _state.timer.done()


def client() -> RegisterClient | None:
    """The client in use while on (``None`` while off)."""
    return _state.client


def _resolver(name: str, cfg: DestinationServiceConfig) -> Any:
    key = (name, cfg)
    found = _state.resolvers.get(key)
    if found is None:
        found = DestinationResolver(name, cfg)
        _state.resolvers[key] = found
    return found


FOUND = "found"
MISSING = "missing"
UNVERIFIED = "unverified"  # the shared deadline cut the check short


async def _check(resolver: Any, deadline: float) -> str:
    """``MISSING`` only when the destination service says the destination does not exist.

    Any other failure counts as present; ``UNVERIFIED`` when it was the shared
    deadline that ended the check (told apart from a callee's own timeout by
    ``expired()``).
    """
    cm = asyncio.timeout_at(deadline)
    try:
        async with cm:
            await resolver.resolve_properties(force=True)
    except DestinationNotFound:
        return MISSING
    except Exception as e:  # noqa: BLE001 - exists, not resolvable now (timeout included)
        logger.debug("agent register: destination check inconclusive (%s)", type(e).__name__)
        if cm.expired():
            return UNVERIFIED
    return FOUND


def _note_reason(reason: str) -> None:
    if reason != _state.logged_reason:
        _state.logged_reason = reason
        logger.info("%s%s", PREFIX, reason)


def _note_forwarding(text: str) -> None:
    if text != _state.logged_forwarding:
        _state.logged_forwarding = text
        logger.info("%s%s", PREFIX, text)


@dataclass(frozen=True)
class _Result:
    on: bool
    reason: str
    destination: str = ""
    technical: Any = None
    user: Any = None
    forwarding: str | None = None  # the INFO text on user forwarding (on only)


async def _evaluate() -> _Result:
    """Ask the destination service; one deadline for both destinations."""
    cfg = config_from_environment(os.environ)
    _state.service_bound = cfg is not None
    if cfg is None:
        return _Result(False, OFF_NO_SERVICE)
    settings = config.settings()
    deadline = asyncio.get_running_loop().time() + settings.timeout_seconds
    technical = _resolver(settings.destination, cfg)
    if await _check(technical, deadline) == MISSING:
        return _Result(False, _off_not_found(settings.destination))
    user = None
    name = settings.user_destination
    if name is None:
        forwarding = ("user forwarding off (no user destination configured); "
                      "calls are logged with the technical token")
    else:
        candidate = _resolver(name, cfg)
        found = await _check(candidate, deadline)
        if found == FOUND:
            user = candidate
            forwarding = f"user forwarding on (destination {name})"
        elif found == UNVERIFIED:
            user = candidate
            forwarding = f"user forwarding on (destination {name}, not verified)"
        else:
            forwarding = (f"user destination {name} not found; "
                          "calls are logged with the technical token")
    return _Result(True, f"on (destination {settings.destination})", settings.destination,
                   technical, user, forwarding)


async def _switch_on(destination: str, technical: Any, user: Any) -> None:
    was_on = state.enabled() and _state.client is not None
    key = (technical, user)
    if _state.client is None or _state.client_key != key:
        old = _state.client
        _state.client = RegisterClient(technical, user,
                                       timeout=config.settings().timeout_seconds,
                                       transport=_transport)
        _state.client_key = key
        if old is not None:
            _retire(old)
    new = _state.client
    state.set_on(destination)
    state.set_user_forwarding(user is not None)
    sender.start(new, queue_max=config.settings().queue_max)
    register.configure(new)  # also re-reads the fallback use case (Reload)
    register.start_pusher(new)
    if not was_on:
        register.request_push()


def _retire(old: RegisterClient) -> None:
    """Close a replaced client once a send it may hold in flight has had its time."""
    _state.retired.append(old)

    async def close_later() -> None:
        await asyncio.sleep(config.settings().timeout_seconds + 1.0)
        await _close(old)

    task = asyncio.get_running_loop().create_task(close_later(), name="agent-register-retire")
    _state.retiring.add(task)
    task.add_done_callback(_state.retiring.discard)


async def _close(old: RegisterClient) -> None:
    if old in _state.retired:
        _state.retired.remove(old)
    try:
        await old.aclose()
    except Exception as e:  # noqa: BLE001 - closing must never fail the caller
        logger.debug("agent register: closing the client failed (%s)", type(e).__name__)


async def _switch_off(reason: str) -> None:
    state.set_off(reason)
    _state.logged_forwarding = None
    for step in (register.stop_pusher, lambda: sender.stop(0)):
        try:
            await step()
        except Exception as e:  # noqa: BLE001
            logger.debug("agent register: stop step failed (%s)", type(e).__name__)
    old, _state.client, _state.client_key = _state.client, None, None
    if old is not None:
        await _close(old)


async def refresh_switch() -> str:
    """Evaluate the switch once and apply it; the reason. Never raises."""
    if _state.lock is None:
        _state.lock = asyncio.Lock()
    async with _state.lock:
        if _state.stopping:
            return state.reason()
        try:
            result = await _evaluate()
        except Exception as e:  # noqa: BLE001 - never into the lifespan or a Reload
            if not _state.failure_warned:
                _state.failure_warned = True
                logger.warning("agent register: switch check failed (%s)", type(e).__name__)
            if state.enabled() and _state.client is not None:
                # Already on: an evaluation that broke says nothing about the
                # register. Keep the worker, its queue and the pusher as they are.
                return state.reason()
            result = _Result(False, OFF_CHECK_FAILED)
        else:
            _state.failure_warned = False
        if _state.stopping:  # shutdown began while the destination service was asked
            return state.reason()
        try:
            if result.on:
                await _switch_on(result.destination, result.technical, result.user)
            elif (state.enabled() or _state.client is not None
                  or result.reason != state.reason()):
                await _switch_off(result.reason)
        except Exception as e:  # noqa: BLE001
            logger.warning("agent register: switch change failed (%s)", type(e).__name__)
            await _switch_off(OFF_CHECK_FAILED)  # catches what its steps raise
        _note_reason(state.reason())
        if state.enabled() and result.forwarding:
            _note_forwarding(result.forwarding)
        return state.reason()


def _ensure_timer() -> None:
    """A timer exactly while a destination service is bound and a period is set."""
    wanted = _state.service_bound and config.settings().repush_minutes > 0
    if wanted and not timer_running():
        _state.timer = asyncio.get_running_loop().create_task(
            _timer_loop(), name="agent-register-timer")
    elif not wanted and timer_running():
        assert _state.timer is not None
        _state.timer.cancel()
        _state.timer = None


async def _timer_loop() -> None:
    while True:
        await asyncio.sleep(_period_seconds())
        try:
            await refresh_switch()
            if not _state.service_bound:
                return
            if state.enabled():
                register.request_push()
        except Exception as e:  # noqa: BLE001 - the timer must outlive one bad period
            logger.warning("agent register: periodic check failed (%s)", type(e).__name__)


async def start() -> str:
    """At app start-up: decide the switch, start what it needs; the reason. Never raises.

    A start after ``stop()`` (a new lifespan) clears the stopping flag.
    """
    _state.stopping = False
    return await _apply()


async def _apply() -> str:
    reason = await refresh_switch()
    if _state.stopping:
        return reason
    try:
        _ensure_timer()
    except Exception as e:  # noqa: BLE001
        logger.warning("agent register: timer not started (%s)", type(e).__name__)
    return reason


async def reevaluate() -> str:
    """At an admin Reload / Restart: decide again, re-configure, push when on. Never raises.

    After ``stop()`` it changes nothing (a Reload racing shutdown).
    """
    reason = await _apply()
    if state.enabled() and not _state.stopping:
        register.request_push()
    return reason


async def stop(flush_seconds: float = DEFAULT_FLUSH_SECONDS) -> None:
    """At shutdown: timer and pusher stopped, the queue flushed (bounded), the client closed.

    Sets the stopping flag first, so a Reload or a timer evaluation still in
    flight applies nothing afterwards (the switching steps do not await, so a
    check of the flag right before them is enough).
    """
    _state.stopping = True
    timer, _state.timer = _state.timer, None
    tasks = [t for t in (timer, *_state.retiring) if t is not None and not t.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    state.set_off(OFF_STOPPED)
    for step in (register.stop_pusher, lambda: sender.stop(flush_seconds)):
        try:
            await step()
        except Exception as e:  # noqa: BLE001
            logger.warning("agent register: shutdown step failed (%s)", type(e).__name__)
    current, _state.client, _state.client_key = _state.client, None, None
    for old in [c for c in (current, *_state.retired) if c is not None]:
        await _close(old)
    _state.logged_reason = OFF_STOPPED
    _state.logged_forwarding = None


def reset_for_tests() -> None:
    """Forget everything; a running timer is cancelled without waiting."""
    global _state
    for task in (_state.timer, *_state.retiring):
        if task is not None and not task.done():
            try:
                task.cancel()
            except RuntimeError:
                pass  # its loop is already closed
    _state = _State()
