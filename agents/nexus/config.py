"""Settings of the agent-register logging, read from the environment.

Every setting has a default, so a landscape that sets nothing gets the
documented behaviour. A value that cannot be used falls back to its default
with one WARNING that names the variable and never the value: an operator
may have pasted something that does not belong in a log.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Mapping

from agents.nexus.observability import label_value

logger = logging.getLogger(__name__)

DEFAULT_DESTINATION = "NEXUS_AI_PLATFORM"
DEFAULT_USER_DESTINATION = "NEXUS_AI_PLATFORM_USER"
DEFAULT_REPUSH_MINUTES = 15
DEFAULT_QUEUE_MAX = 2000
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 30.0
# The register stores a use case in at most 60 characters; a longer
# fallback could never match one of its use cases.
MAX_USE_CASE = 60
DEFAULT_RESOURCE_GROUP = "default"


@dataclass(frozen=True)
class NexusSettings:
    """What the register logging needs to know from the environment."""

    destination: str
    # None: no user forwarding; calls are reported with the technical identity.
    user_destination: str | None
    # None: an agent the register assigned no use case to is reported without one.
    fallback_use_case: str | None
    # 0: no timer; agents are pushed only at start-up and on admin changes.
    repush_minutes: int
    # Prompt and answer text are sent only when this is exactly "true".
    send_payload: bool
    queue_max: int
    timeout_seconds: float
    # A label for AI Core's observability record (``NEXUS_LANDSCAPE``); None: no label.
    landscape: str | None = None


def _text(environ: Mapping[str, str], name: str) -> str:
    return (environ.get(name) or "").strip()


def _int(environ: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    raw = _text(environ, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = None
    if value is None or value < minimum:
        logger.warning("%s is not a valid number; using the default %s", name, default)
        return default
    return value


def _timeout(environ: Mapping[str, str], name: str) -> float:
    raw = _text(environ, name)
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not (0 < value <= MAX_TIMEOUT_SECONDS):  # also false for nan
        logger.warning(
            "%s is not a valid number; using the default %s", name, DEFAULT_TIMEOUT_SECONDS
        )
        return DEFAULT_TIMEOUT_SECONDS
    return value


def _landscape(environ: Mapping[str, str]) -> str | None:
    raw = _text(environ, "NEXUS_LANDSCAPE")
    if not raw:
        return None
    if label_value(raw) is None:
        logger.warning("NEXUS_LANDSCAPE is not a valid label value; ignored")
        return None
    return raw


def settings(environ: Mapping[str, str] = os.environ) -> NexusSettings:
    """The settings as the given environment states them, defaults filled in."""
    fallback = _text(environ, "NEXUS_FALLBACK_USE_CASE") or None
    if fallback is not None and len(fallback) > MAX_USE_CASE:
        logger.warning(
            "NEXUS_FALLBACK_USE_CASE is longer than %s characters; ignored", MAX_USE_CASE
        )
        fallback = None
    return NexusSettings(
        destination=_text(environ, "NEXUS_DESTINATION") or DEFAULT_DESTINATION,
        user_destination=(
            _text(environ, "NEXUS_USER_DESTINATION") or None
            if "NEXUS_USER_DESTINATION" in environ
            else DEFAULT_USER_DESTINATION
        ),
        fallback_use_case=fallback,
        repush_minutes=_int(environ, "NEXUS_REPUSH_MINUTES", DEFAULT_REPUSH_MINUTES, 0),
        send_payload=environ.get("NEXUS_SEND_PAYLOAD") == "true",
        queue_max=_int(environ, "NEXUS_QUEUE_MAX", DEFAULT_QUEUE_MAX, 1),
        timeout_seconds=_timeout(environ, "NEXUS_TIMEOUT_SECONDS"),
        landscape=_landscape(environ),
    )


def resource_group(environ: Mapping[str, str] = os.environ) -> str:
    """The AI Core resource group the app's models run in (blank: ``default``)."""
    return _text(environ, "AICORE_RESOURCE_GROUP") or DEFAULT_RESOURCE_GROUP
