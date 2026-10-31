"""Basic and advanced access: which admin agents a chat run may reach.

Two tiers. ``basic`` is every chat user (the ``user`` scope); ``advanced`` is
the existing ``$XSAPPNAME.developer`` scope (role collection "ABAP IDE
Developer") OR the ``$XSAPPNAME.admin`` scope: the admin role has full
access. An admin agent has a ``tier`` (``AgentConfig.tier``); an
``advanced`` one is reachable from chat only in an advanced context.

The context of a run (:func:`effective_tier`):

- the web chat binds :data:`current_access` to the conversation's ``access``
  for the length of a turn, in the turn's own task (and resets it there), so
  a developer's basic conversation stays basic;
- everywhere else (the classic chat, A2A, the IDE) nothing binds it and the
  tier follows the bound claims: the developer or the admin scope means
  advanced.

Jobs, workflows and the invoke API run named agents and never ask.

The test is :func:`is_developer` (the name the web chat's ``/me.developer``
key follows), the rule of ``require_developer`` OR ``require_admin``: the
validator's ``has_scope(<claims>, "developer")`` or ``has_scope(<claims>,
"admin")``; without an XSUAA validator (local development) everyone is
admitted, as both dependencies admit everyone there.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

from agents import auth

logger = logging.getLogger(__name__)

TIERS = ("basic", "advanced")
BASIC = "basic"
ADVANCED = "advanced"
DEVELOPER_SCOPE = "developer"
ADMIN_SCOPE = "admin"
# Either grants advanced access.
ADVANCED_SCOPES = (DEVELOPER_SCOPE, ADMIN_SCOPE)

# The access of the web chat turn in progress; ``None`` everywhere else.
current_access: ContextVar[str | None] = ContextVar("current_access", default=None)


def is_developer(claims: dict[str, Any] | None) -> bool:
    """Whether ``claims`` grant advanced access: the developer OR the admin
    scope (the rules of ``require_developer`` / ``require_admin``). Without
    an XSUAA validator (local development) it is assumed; a validator that
    cannot be set up admits nobody."""
    try:
        validator = auth.get_validator()
    except Exception:  # noqa: BLE001 - AuthConfigurationError: fail closed
        return False
    if validator is None:
        return True
    if not isinstance(claims, dict):
        return False
    return any(bool(validator.has_scope(claims, scope)) for scope in ADVANCED_SCOPES)


def caller_is_developer() -> bool:
    """:func:`is_developer` of the claims bound to this request or run."""
    return is_developer(auth.current_claims.get())


def effective_tier() -> str:
    """The tier of the run in progress: the bound :data:`current_access`
    when it is a known tier, else ``advanced`` for claims with the developer
    or the admin scope, else ``basic``."""
    bound = current_access.get()
    if bound is not None:
        return bound if bound in TIERS else BASIC
    return ADVANCED if caller_is_developer() else BASIC


def tier_of(row_or_tier: Any) -> str:
    """The tier of an agent row (its ``tier`` attribute) or of a tier
    string; anything unknown counts as ``advanced`` (fails closed)."""
    value = row_or_tier if isinstance(row_or_tier, str) else getattr(row_or_tier, "tier", None)
    if value is None:
        return BASIC  # a row written before the column existed
    return value if value in TIERS else ADVANCED


def agent_allowed(row_or_tier: Any, tier: str) -> bool:
    """Whether an agent of ``row_or_tier`` may be reached in context ``tier``."""
    return tier_of(row_or_tier) == BASIC or tier == ADVANCED
