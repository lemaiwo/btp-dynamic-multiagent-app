"""Built-in toolsets: tools served in-process instead of over MCP.

An agent asks for one by listing a pseudo-URL such as ``builtin:gmail`` among
its MCP servers, with the same ``oauth`` block a real oauth2 server would carry.
``agents.registry`` spots the scheme and calls :func:`build_builtin_toolset`
instead of opening an MCP connection; ``agents.admin`` lets these URLs past the
https/allow-list rules, which have nothing to check when there is no host.

They exist because a vendor's own hosted MCP server is not always reachable by a
self-registered OAuth client -- Google's Gmail MCP server refuses every
``tools/call`` from one, and Microsoft's Outlook MCP server is gated behind a
Copilot licence. The REST APIs underneath have no such problem.

The set is closed. An unrecognised ``builtin:`` URL is a typo, not an extension
point, and is rejected at admin validation rather than silently ignored.
"""

from __future__ import annotations

from typing import Any, Callable

from agents.gmail_tools import BUILTIN_GMAIL_URL, gmail_toolset
from agents.jira_tools import BUILTIN_JIRA_URL, jira_toolset
from agents.outlook_tools import BUILTIN_OUTLOOK_URL, outlook_toolset
from agents.sapnotes_tools import BUILTIN_SAPNOTES_URL, sapnotes_toolset

# url -> factory(oauth, server_key) -> AbstractToolset
_FACTORIES: dict[str, Callable[..., Any]] = {
    BUILTIN_GMAIL_URL: gmail_toolset,
    BUILTIN_OUTLOOK_URL: outlook_toolset,
    BUILTIN_JIRA_URL: jira_toolset,
    BUILTIN_SAPNOTES_URL: sapnotes_toolset,
}

BUILTIN_URLS = frozenset(_FACTORIES)


def is_builtin_url(url: str | None) -> bool:
    """True when ``url`` names a built-in toolset rather than an MCP server."""
    return str(url or "").strip().lower() in BUILTIN_URLS


def build_builtin_toolset(
    url: str, oauth: dict[str, Any] | None, auth_mode: str | None = None
):
    """The toolset for a ``builtin:`` URL.

    ``auth_mode`` decides who the tools act as: ``app_only`` means the
    application itself, ``destination`` means whatever credential the BTP
    destination holds, and anything else means the signed-in user. It is
    optional so existing callers keep the per-user behaviour they already had.
    """
    key = str(url).strip().lower()
    factory = _FACTORIES.get(key)
    if factory is None:
        raise ValueError(
            f"unknown built-in toolset {url!r}; known: {', '.join(sorted(BUILTIN_URLS))}"
        )
    return factory(oauth or {}, server_key=key, auth_mode=auth_mode)
