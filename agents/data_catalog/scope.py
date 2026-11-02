"""Which catalogue sources an agent may search: its own entries, nothing else.

A source is ``(source_kind, source_name)``. Today that is ``("odata",
<service>)`` for every service of the agent's ``builtin:odata`` entries;
``ina`` and ``generic`` join when their built-ins exist.

The tier rule is part of the scope and stays this narrow on purpose: the
scope is the CALLING agent's own entries, so a source attached only to an
advanced agent never reaches a basic turn through another agent. Whether the
calling agent itself may run in the turn's tier is ``agents.access``'s rule,
applied by the search (``find_sources(scope_tier=...)``).
"""

from __future__ import annotations

from typing import Any

from agents.db import odata_entries

Source = tuple[str, str]


def _servers_of(agent: Any) -> list[Any]:
    if isinstance(agent, dict):
        return [agent]
    if isinstance(agent, list):
        return agent
    servers = getattr(agent, "mcp_servers", None)
    return servers if isinstance(servers, list) else []


def allowed_sources_of(agent: Any) -> set[Source]:
    """The sources of an agent row (``AgentConfig``), of one server entry
    (``{"url": "builtin:odata", "oauth": {...}}``) or of a list of them."""
    sources: set[Source] = set()
    for block in odata_entries(_servers_of(agent)):
        services = block.get("services")
        if isinstance(services, list):
            sources.update(("odata", n) for n in services if isinstance(n, str) and n)
    return sources
