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
builtin:ina        --        --        user / app    --       --
builtin:sharepoint --        app (ro)  app (ro)      --       --
builtin:bitbucket  --        --        app           --       --
builtin:confluence --        --        app           --       --
builtin:ariba      --        --        app           --       --
builtin:git        --        --        app           --       --
builtin:hana        --        --        app (tcp)     --       --
builtin:reconcile  --        --        --            default  --
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
registry loaded -- through ``context``; see :mod:`agents.odata.tools`. Its
writes are recorded: the factory always gets the storing audit recorder
(:mod:`agents.odata.audit`), without which the toolset refuses every write.

``builtin:ina`` is its sibling for analytical queries (BW / embedded
analytics over SAP's InA protocol), reads only: its config block lists
registered queries (``queries``), each of which names its own destination and
identity, and the factory is handed the snapshot of those registrations the
registry loaded (``context["ina_queries"]``); see :mod:`agents.ina.tools`.

``builtin:sharepoint`` reads one pinned workbook as the application and has
no per-user mode: ``user_context`` is refused in both of its modes. Its
config block carries the pins and a ``views`` object; see
:mod:`agents.sharepoint_tools`.

``builtin:bitbucket`` reviews pull requests as the technical user of a
destination and has no per-user mode: ``user_context`` is refused. Its config
block pins one workspace, optionally a repository list and the target branch,
and holds the two capability switches ``allow_comment`` and ``allow_approve``;
see :mod:`agents.bitbucket_tools`.

``builtin:confluence`` reads Confluence pages as the technical user of a
destination and has no per-user mode: ``user_context`` is refused. Its config
block pins the space keys (``spaces``) and the REST prefix (``api_base``); see
:mod:`agents.confluence_tools`.

``builtin:ariba`` reads SAP Ariba sourcing events, the suppliers invited to
them and the PDFs a supplier uploaded with its bid, through the Event
Management API, as the technical user of a destination (the destination holds
the API key and the realm); it has no per-user mode: ``user_context`` is
refused. Its config block is the destination name alone; see
:mod:`agents.ariba_tools`.

``builtin:git`` reads and writes Git repositories on GitHub, Bitbucket Cloud
or GitLab as the technical user of a destination and has no per-user mode:
``user_context`` is refused. Its config block names the provider, pins the
repositories, the base branch and the branch prefix, and holds the two
switches ``allow_push`` and ``allow_pull_request``. Its factory is the one
besides ``builtin:odata`` that gets something from ``context``: the agent's
name, which goes into the trailer of every commit; see :mod:`agents.git_tools`.

``builtin:hana`` reads monitoring views of on-premise SAP HANA databases
over SQL (``hdbcli`` through the Cloud Connector, ``tcp``) as the technical
user of one destination per system, and has no per-user mode:
``user_context`` is refused. Its config block lists the systems (``systems``:
a slug and a destination each) and holds the one switch ``allow_select``,
which adds ``run_select``. The driver is imported only when a call connects,
so building the toolset needs neither ``hdbcli`` nor a binding; see
:mod:`agents.hana.tools`.

``builtin:reconcile`` compares a migrated CSV with its source table, row by
row, and holds no credential (``none`` only). It reads the uploads of the
current web chat conversation only, and it reaches ECC through a sibling
entry of the SAME agent: the ARC-1 MCP server named in ``arc1_server``,
whose own auth (``oauth2``) carries the signed-in user. Its config block is
``arc1_server`` (that sibling's URL), ``target`` (the ARC-1 target, blank =
the server's own), ``batch_size`` (rows per SAPQuery, default 200, at most
10,000) and ``mismatch_cap`` (default 50,000, at most 200,000); see
:mod:`agents.reconcile.config`. Its factory gets two things from
``context``: the agent's name and ``mcp_server_for``, the registry's lookup
of that agent's own remote MCP servers by endpoint; see
:mod:`agents.reconcile.tools`.
"""

from __future__ import annotations

from typing import Any, Callable

from agents.ariba_tools import BUILTIN_ARIBA_URL, ariba_toolset
from agents.bitbucket_config import BUILTIN_BITBUCKET_URL
from agents.bitbucket_tools import bitbucket_toolset
from agents.confluence_tools import BUILTIN_CONFLUENCE_URL, confluence_toolset
from agents.git_config import BUILTIN_GIT_URL
from agents.git_tools import git_toolset
from agents.gmail_tools import BUILTIN_GMAIL_URL, gmail_toolset
from agents.hana import BUILTIN_HANA_URL
from agents.hana.tools import hana_toolset
from agents.ina import BUILTIN_INA_URL
from agents.ina.tools import ina_toolset
from agents.jira_tools import BUILTIN_JIRA_URL, jira_toolset
from agents.odata import BUILTIN_ODATA_URL
from agents.odata.audit import stored_recorder
from agents.odata.tools import odata_toolset
from agents.outlook_tools import BUILTIN_OUTLOOK_URL, outlook_toolset
from agents.reconcile import BUILTIN_RECONCILE_URL
from agents.reconcile.tools import reconcile_toolset
from agents.sapnotedetail_tools import BUILTIN_SAPNOTEDETAIL_URL, sapnotedetail_toolset
from agents.sapnotes_tools import BUILTIN_SAPNOTES_URL, sapnotes_toolset
from agents.sharepoint_tools import BUILTIN_SHAREPOINT_URL, sharepoint_toolset
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
    BUILTIN_INA_URL: ina_toolset,
    BUILTIN_SHAREPOINT_URL: sharepoint_toolset,
    BUILTIN_BITBUCKET_URL: bitbucket_toolset,
    BUILTIN_CONFLUENCE_URL: confluence_toolset,
    BUILTIN_ARIBA_URL: ariba_toolset,
    BUILTIN_GIT_URL: git_toolset,
    BUILTIN_HANA_URL: hana_toolset,
    BUILTIN_RECONCILE_URL: reconcile_toolset,
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
    ``odata_services`` (the catalogue snapshot, by service name),
    ``ina_queries`` (the registered InA queries, by name) and
    ``agent_name`` and ``mcp_server_for`` (a callable: an endpoint URL of
    one of this agent's remote MCP servers to the server object the
    registry built for it, ``None`` for any other URL). Only ``builtin:odata``
    and ``builtin:ina`` receive their snapshot and the agent name,
    ``builtin:git`` the agent name alone, and ``builtin:reconcile`` the agent
    name and ``mcp_server_for``; every other factory is called exactly as
    before, so none has to accept keywords it ignores.

    ``builtin:odata`` is always built with the storing audit recorder of
    this process. It is not read from ``context``: no caller can build the
    toolset through here with writes that are not recorded.
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
            recorder=stored_recorder(),
        )
    if key == BUILTIN_INA_URL:
        context = context or {}
        return factory(
            oauth or {},
            server_key=key,
            auth_mode=auth_mode,
            queries=context.get("ina_queries") or {},
            agent_name=context.get("agent_name") or "",
        )
    if key == BUILTIN_GIT_URL:
        return factory(oauth or {}, server_key=key, auth_mode=auth_mode,
                       agent_name=(context or {}).get("agent_name") or "")
    if key == BUILTIN_HANA_URL:
        # The block as stored, ``None`` included: the entry's own reading
        # (`agents.hana.config`) refuses a missing block with its own text.
        return factory(oauth, server_key=key, auth_mode=auth_mode)
    if key == BUILTIN_RECONCILE_URL:
        context = context or {}
        return factory(oauth or {}, server_key=key, auth_mode=auth_mode,
                       server_for=context.get("mcp_server_for"),
                       agent_name=context.get("agent_name") or "")
    return factory(oauth or {}, server_key=key, auth_mode=auth_mode)
