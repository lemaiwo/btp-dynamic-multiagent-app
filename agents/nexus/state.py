"""The on/off switch of the agent-register logging.

A plain module-level boolean on purpose: the model wrapper reads it on every
model call, so the check must cost nothing and do no I/O. Only the lifecycle
(start-up, Reload, shutdown) changes it; ``reason`` is what the admin and the
log are told about why the feature is in its current state.
"""
from __future__ import annotations

_NOT_STARTED = "off (not started)"

_enabled = False
_reason = _NOT_STARTED


def enabled() -> bool:
    """Whether model calls are reported to the register right now."""
    return _enabled


def reason() -> str:
    """A short text saying why the switch is in its current state."""
    return _reason


def set_on(destination: str) -> None:
    """Switch on, reporting through the named destination."""
    global _enabled, _reason
    _enabled = True
    _reason = f"on (destination {destination})"


def set_off(reason: str) -> None:
    """Switch off for the given reason (shown as given)."""
    global _enabled, _reason
    _enabled = False
    _reason = reason


def reset_for_tests() -> None:
    """Back to the state of a fresh process: off, not started."""
    global _enabled, _reason
    _enabled = False
    _reason = _NOT_STARTED
