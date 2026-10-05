"""Built-in toolsets: tools served in-process instead of over MCP.

An agent asks for one by listing a pseudo-URL such as ``builtin:gmail`` among
its MCP servers, with the same ``oauth`` block a real oauth2 server would carry.
``agents.registry`` spots the scheme and calls :func:`build_builtin_toolset`
instead of opening an MCP connection; ``agents.admin`` lets these URLs past the
https/allow-list rules, which have nothing to check when there is no host.

They exist because a vendor's own hosted MCP server is not always reachable by a
self-registered OAuth client -- Google's Gmail MCP server refuses every
``tools/call`` from one, and Microsoft's Outlook and Teams MCP servers are
gated behind a Copilot licence. The REST APIs underneath have no such problem.

The set is closed. An unrecognised ``builtin:`` URL is a typo, not an extension
point, and is rejected at admin validation rather than silently ignored.

Auth modes per built-in (``agents.admin.McpServerPayload`` refuses the rest at
save time; ``ui5-admin/webapp/model/builtins.ts`` mirrors this table):

================== ========= ========= ============= ======== ========
built-in           oauth2    app_only  destination   none     session
================== ========= ========= ============= ======== ========
builtin:gmail      user      --        user / app*   --       --
builtin:outlook    user      app*      user / app*   --       --
builtin:teams      user      app (ro)  user / app(ro)--       --
builtin:slack      --        --        app           --       --
builtin:jira       --        --        app           --       --
builtin:smtp       --        --        app (MAIL)    --       --
builtin:sapnotes   --        --        app (public)  default  --
builtin:sapnotedetail --     --        app (cookie)  --       default
builtin:odata      --        --        user / app    --       --
================== ========= ========= ============= ======== ========

``destination`` reaches the API through a BTP destination. The config block's
``user_context`` picks between the two columns: on, the destination is resolved
with the signed-in user's JWT (``X-user-token``) and the tools act as that
user; off, the destination's own credential is used and the app-only rules
apply (``*`` = ``mailbox`` required, ``ro`` = read-only). See
:mod:`agents.destination_auth`. ``builtin:smtp`` reads a ``MAIL``
destination's properties (host, user, password) rather than a URL; see
:mod:`agents.smtp_tools`.

``builtin:odata`` is the one built-in whose entry names no destination: its
config block lists catalogue services (``services``, ``allow_write``), and the
identity comes from each catalogue service, not from the entry. It is also the
only factory that is handed more than its entry -- the catalogue snapshot the
registry loaded -- through ``context``; see :mod:`agents.odata.tools`.
"""

from __future__ import annotations

from typing import Any, Callable

from agents.gmail_tools import BUILTIN_GMAIL_URL, gmail_toolset
from agents.jira_tools import BUILTIN_JIRA_URL, jira_toolset
from agents.odata import BUILTIN_ODATA_URL
from agents.odata.tools import odata_toolset
from agents.outlook_tools import BUILTIN_OUTLOOK_URL, outlook_toolset
from agents.sapnotes_tools import BUILTIN_SAPNOTES_URL, sapnotes_toolset
from agents.sapnotedetail_tools import BUILTIN_SAPNOTEDETAIL_URL, sapnotedetail_toolset
from agents.slack_tools import BUILTIN_SLACK_URL, slack_toolset
from agents.smtp_tools import BUILTIN_SMTP_URL, smtp_toolset
from agents.teams_tools import BUILTIN_TEAMS_URL, teams_toolset

# url -> factory(oauth, server_key) -> AbstractToolset
_FACTORIES: dict[str, Callable[..., Any]] = {
    BUILTIN_GMAIL_URL: gmail_toolset,
    BUILTIN_OUTLOOK_URL: outlook_toolset,
    BUILTIN_TEAMS_URL: teams_toolset,
    BUILTIN_SLACK_URL: slack_toolset,
    BUILTIN_JIRA_URL: jira_toolset,
    BUILTIN_SMTP_URL: smtp_toolset,
    BUILTIN_SAPNOTES_URL: sapnotes_toolset,
    BUILTIN_SAPNOTEDETAIL_URL: sapnotedetail_toolset,
    BUILTIN_ODATA_URL: odata_toolset,
}

BUILTIN_URLS = frozenset(_FACTORIES)


def is_builtin_url(url: str | None) -> bool:
    """True when ``url`` names a built-in toolset rather than an MCP server."""
    return str(url or "").strip().lower() in BUILTIN_URLS


def build_builtin_toolset(
    url: str,
    oauth: dict[str, Any] | None,
    auth_mode: str | None = None,
    *,
    context: dict[str, Any] | None = None,
):
    """The toolset for a ``builtin:`` URL.

    ``auth_mode`` decides who the tools act as: ``app_only`` means the
    application itself, ``destination`` means whatever credential the BTP
    destination holds, and anything else means the signed-in user. It is
    optional so existing callers keep the per-user behaviour they already had.

    ``context`` is what the registry knows and an entry does not hold:
    ``odata_services`` (the catalogue snapshot, by service name) and
    ``agent_name``. Only ``builtin:odata`` receives it; every other factory
    is called exactly as before, so none has to accept keywords it ignores.
    """
    key = str(url).strip().lower()
    factory = _FACTORIES.get(key)
    if factory is None:
        raise ValueError(
            f"unknown built-in toolset {url!r}; known: {', '.join(sorted(BUILTIN_URLS))}"
        )
    if key == BUILTIN_ODATA_URL:
        context = context or {}
        return factory(
            oauth or {},
            server_key=key,
            auth_mode=auth_mode,
            services=context.get("odata_services") or {},
            agent_name=context.get("agent_name") or "",
        )
    return factory(oauth or {}, server_key=key, auth_mode=auth_mode)
