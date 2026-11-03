"""Which catalogue sources an agent may search: its own entries, nothing else.

A source is ``(source_kind, source_name)``: ``("odata", <service>)`` for every
service of the agent's ``builtin:odata`` entries and ``("ina", <query>)`` for
every query of its ``builtin:ina`` entries. A ``generic`` service is attached
through ``builtin:odata`` like any catalogue service and is named ``("odata",
<service>)`` here, as the registry names it; `node_sources` widens such a
name to the kind its nodes are stored under.

The tier rule is part of the scope and stays this narrow on purpose: the
scope is the CALLING agent's own entries, so a source attached only to an
advanced agent never reaches a basic turn through another agent. Whether the
calling agent itself may run in the turn's tier is ``agents.access``'s rule,
applied by the search (``find_sources(scope_tier=...)``).
"""

from __future__ import annotations

from typing import Any

from agents.data_catalog.importer import CATALOGUE_KINDS, INA, ODATA
from agents.db import ina_entries, odata_entries

Source = tuple[str, str]


def _servers_of(agent: Any) -> list[Any]:
    if isinstance(agent, dict):
        return [agent]
    if isinstance(agent, list):
        return agent
    servers = getattr(agent, "mcp_servers", None)
    return servers if isinstance(servers, list) else []


def _names(blocks: list[dict[str, Any]], key: str) -> set[str]:
    names: set[str] = set()
    for block in blocks:
        listed = block.get(key)
        if isinstance(listed, list):
            names.update(n for n in listed if isinstance(n, str) and n)
    return names


def allowed_sources_of(agent: Any) -> set[Source]:
    """The sources of an agent row (``AgentConfig``), of one server entry
    (``{"url": "builtin:odata", "oauth": {...}}``) or of a list of them."""
    servers = _servers_of(agent)
    return {(ODATA, n) for n in _names(odata_entries(servers), "services")} | {
        (INA, n) for n in _names(ina_entries(servers), "queries")
    }


def node_sources(allowed: set[Source]) -> set[Source]:
    """``allowed`` as the nodes are stored: a catalogue service name reaches
    its nodes under either catalogue kind. The name is unique in
    ``odata_services`` whatever the kind, so this names the same one row
    and no other source; an InA query is ``("ina", name)`` only."""
    widened = set(allowed)
    for kind, name in allowed:
        if kind in CATALOGUE_KINDS:
            widened.update((other, name) for other in CATALOGUE_KINDS)
    return widened
