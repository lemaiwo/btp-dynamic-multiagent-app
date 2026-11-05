"""The ``builtin:reconcile`` toolset factory.

The entry is ``{"url": "builtin:reconcile", "auth_mode": "none", "oauth":
{arc1_server?, target?, batch_size?, mismatch_cap?}}`` (see
:mod:`agents.reconcile.config`). It holds no credential: ECC is read through
the agent's own ARC-1 MCP server entry named in ``arc1_server``, whose auth
carries the signed-in user. The registry hands the factory a lookup
(``server_for``) that answers only servers of the SAME agent, built in the
same registry pass; see ``agents.registry.build_orchestrator``.

The tools (Task 11 of the reconciliation plan) read their state from
:class:`ReconcileContext`, attached to the toolset as ``reconcile_context``,
and the conversation from ``agents.webchat.docgen.current_chat_run`` at call
time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic_ai.toolsets import FunctionToolset

from agents.reconcile import BUILTIN_RECONCILE_URL
from agents.reconcile.config import ReconcileConfig


def _no_server(url: str) -> Any | None:
    return None


@dataclass(frozen=True)
class ReconcileContext:
    """What a reconcile tool knows besides its arguments.

    ``server_for`` must be asked at call time, not at build: the registry
    builds an agent's entries in their configured order, so the ARC-1
    sibling may be built after this toolset. It answers the raw
    ``PerRunMCPServer`` (not the ``ReadOnlyGuard`` wrapper); the ARC-1
    reader enforces its own read-only rule (only ``SAPQuery``, only the SQL
    it builds itself).
    """

    config: ReconcileConfig
    agent_name: str
    server_key: str
    server_for: Callable[[str], Any | None]

    def arc1_server(self) -> Any | None:
        """The agent's ARC-1 server named in the config, or ``None`` when no
        sibling is named or the agent has none under that URL (the tools
        answer ``arc1_not_configured``)."""
        if not self.config.arc1_server:
            return None
        return self.server_for(self.config.arc1_server)


class ReconcileToolset(FunctionToolset):
    """A ``FunctionToolset`` that carries its :class:`ReconcileContext`."""

    def __init__(self, context: ReconcileContext) -> None:
        super().__init__()
        self.reconcile_context = context


def reconcile_toolset(
    oauth: dict[str, Any] | None,
    *,
    server_key: str = BUILTIN_RECONCILE_URL,
    auth_mode: str | None = None,
    server_for: Callable[[str], Any | None] | None = None,
    agent_name: str = "",
) -> ReconcileToolset:
    """The toolset of one ``builtin:reconcile`` entry.

    ``ValueError`` for another auth mode or a block the gate would refuse:
    misconfiguration fails the registry build of this agent, as for every
    built-in.
    """
    if (auth_mode or "none") != "none":
        raise ValueError(f"{BUILTIN_RECONCILE_URL} requires auth_mode=none")
    context = ReconcileContext(
        config=ReconcileConfig.from_oauth(oauth),
        agent_name=agent_name,
        server_key=server_key,
        server_for=server_for or _no_server,
    )
    # Task 11 registers the five tools on this toolset.
    return ReconcileToolset(context)
