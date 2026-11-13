"""The one reading of a ``builtin:hana`` entry. Standard library only.

The save-time gate, storage and the toolset all call this module, so none
accepts what another refuses. A value is accepted only in exactly the form
that is stored: nothing here trims, lower-cases or converts. A refusal names
the field and the rule, never the value, and never the name of a key it does
not know. Never imports ``agents.db`` (storage imports this module).

The entry::

    {"systems": [{"name": "<slug>", "destination": "<name>"}, ...],
     "allow_select": true}

Each system is one HANA database (a tenant or SYSTEMDB) behind its own BTP
destination, which holds the host and the credential; the agent names a
system only by its slug, never a destination or a host. There is therefore
no top-level ``destination``: one would say nothing about which system it is.
The connection is the destination's technical user, so ``user_context:
true`` is refused. ``allow_select`` is stored only as ``true``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agents.hana import BUILTIN_HANA_URL

ENTRY_KEYS = ("systems", "allow_select")
SYSTEM_KEYS = ("name", "destination")
MAX_SYSTEMS = 10

SYSTEM_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
# The OData gate's DESTINATION_NAME_RE (agents/odata/models.py) without its
# anchors; copied, because importing it would pull pydantic into this module.
# tests/test_hanadb_config.py holds the two equal.
DESTINATION_PATTERN = r"[A-Za-z0-9_.-]{1,200}"
_DESTINATION_RE = re.compile(DESTINATION_PATTERN)
# Echoed by the API's own answers (`_redact_servers` in agents/db.py) or sent
# with their default by a client that serialises every field. Tolerated only
# when they say nothing; never stored.
_ECHOED = ("has_client_secret", "user_context")

HANA_SINGLE_ENTRY_MESSAGE = (
    f"an agent may have at most one {BUILTIN_HANA_URL} entry; list every system it "
    "reads in that entry's oauth.systems")


class HanaConfigError(ValueError):
    """A refused entry. The text names the field and the rule only."""


@dataclass(frozen=True)
class Pins:
    """What the toolset is built with: ``(name, destination)`` per system."""

    systems: tuple[tuple[str, str], ...]
    allow_select: bool


def _is(pattern: re.Pattern[str], value: Any) -> bool:
    # fullmatch, never `$`: `$` also matches before a trailing line feed.
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _check_system(index: int, item: Any, seen: set[str]) -> None:
    field = f"oauth.systems[{index}]"
    if not isinstance(item, dict):
        raise HanaConfigError(
            f"{field}: each system is an object with name and destination")
    if {k for k in item if item[k] is not None} - set(SYSTEM_KEYS):
        # Not named: a key outside the closed set is the client's own text.
        raise HanaConfigError(
            f"{field}: a system holds only {', '.join(SYSTEM_KEYS)}; "
            "remove every other key")
    name = item.get("name")
    if not _is(SYSTEM_NAME_RE, name):
        raise HanaConfigError(
            f"{field}.name: required, 1-32 lower-case letters, digits or '_', "
            "starting with a letter")
    if name in seen:
        raise HanaConfigError(f"{field}.name: duplicate; each system name is listed once")
    seen.add(name)
    if not _is(_DESTINATION_RE, item.get("destination")):
        raise HanaConfigError(
            f"{field}.destination: required, a destination name of 1-200 letters, "
            "digits, '_', '.' or '-' without surrounding spaces")


def check_entry(cfg: Any) -> None:
    """Raises :class:`HanaConfigError` unless ``cfg`` is a valid block."""
    if not isinstance(cfg, dict):
        raise HanaConfigError(
            f"oauth: a {BUILTIN_HANA_URL} entry needs a config object with systems")
    if cfg.get("user_context") is True:
        raise HanaConfigError(
            f"oauth.user_context: {BUILTIN_HANA_URL} has no signed-in user to act as; "
            "it connects as the technical user of each system's destination")
    if cfg.get("destination") is not None:
        raise HanaConfigError(
            f"oauth.destination: a {BUILTIN_HANA_URL} entry has no destination of its "
            "own; the destinations are per system, in oauth.systems")
    present = {k for k, v in cfg.items() if v is not None}
    stray = present - set(ENTRY_KEYS) - {k for k in _ECHOED if cfg.get(k) is False}
    if stray:
        # Not named: a key outside the closed set is the client's own text.
        raise HanaConfigError(
            f"oauth: a {BUILTIN_HANA_URL} entry holds only {', '.join(ENTRY_KEYS)}; "
            "remove every other key")
    systems = cfg.get("systems")
    if not isinstance(systems, list) or not systems:
        raise HanaConfigError(
            f"oauth.systems: required, a list of 1 to {MAX_SYSTEMS} systems")
    if len(systems) > MAX_SYSTEMS:
        raise HanaConfigError(
            f"oauth.systems: at most {MAX_SYSTEMS} systems per entry")
    seen: set[str] = set()
    for index, item in enumerate(systems):
        _check_system(index, item, seen)
    allow = cfg.get("allow_select")
    if allow is not None and not isinstance(allow, bool):
        raise HanaConfigError(
            "oauth.allow_select: must be the JSON boolean true or false; a string "
            "or a number does not set it")


def clean_entry(cfg: Any) -> dict[str, Any]:
    """The block as it is stored: checked, its own keys only, fixed order, a
    new object. ``allow_select`` only as ``true``."""
    check_entry(cfg)
    cleaned: dict[str, Any] = {
        "systems": [{"name": s["name"], "destination": s["destination"]}
                    for s in cfg["systems"]]}
    if cfg.get("allow_select") is True:
        cleaned["allow_select"] = True
    return cleaned


def pins_of(cfg: Any) -> Pins:
    """The checked block as the toolset reads it. ``allow_select`` is true
    only for exactly ``true`` and decides whether ``run_select`` is
    registered; the reach rules hold for every ``builtin:hana`` entry,
    whatever it says (`agents.db.check_hana_reach`)."""
    cleaned = clean_entry(cfg)
    return Pins(
        systems=tuple((s["name"], s["destination"]) for s in cleaned["systems"]),
        allow_select=cleaned.get("allow_select") is True,
    )
