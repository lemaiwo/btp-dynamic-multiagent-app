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
# Whether a user destination is configured: only then is a signed-in user's
# JWT captured at a model call (else every queue entry would hold one for
# nothing). Set by the lifecycle when it builds the user resolver.
_user_forwarding = False


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
    """Switch off for the given reason (shown as given); user forwarding ends too."""
    global _enabled, _reason, _user_forwarding
    _enabled = False
    _reason = reason
    _user_forwarding = False


def user_forwarding() -> bool:
    """Whether model calls of a signed-in user are logged through the user destination."""
    return _user_forwarding


def set_user_forwarding(on: bool) -> None:
    """Set by the lifecycle: ``True`` once a user destination resolver is built."""
    global _user_forwarding
    _user_forwarding = bool(on)


def reset_for_tests() -> None:
    """Back to the state of a fresh process: off, not started."""
    global _enabled, _reason, _user_forwarding
    _enabled = False
    _reason = _NOT_STARTED
    _user_forwarding = False
