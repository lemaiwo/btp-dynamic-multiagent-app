"""Outlook tools served in-process against Microsoft Graph.

The Outlook counterpart of ``agents/gmail_tools.py``, and it exists for the same
reason: Microsoft's own hosted Outlook MCP server (Work IQ Mail, under Agent
365) is preview-only, needs a Copilot licence and an admin-registered enterprise
app, and does not cover personal accounts. Graph itself has none of those
conditions -- see docs/OUTLOOK_SETUP.md.

The work queue is an **Inbox subfolder** rather than a Gmail label, which makes
the loop simpler at both ends: listing the folder is the search, so there is no
query syntax to get wrong, and moving the message out is a single atomic call
that physically removes it from the queue.

An agent opts in with the pseudo-URL ``builtin:outlook`` and its usual ``oauth``
block; ``agents/builtins.py`` dispatches to it. Authentication is the same
``PerUserOAuth2Auth`` used everywhere else, so sign-in, refresh and the
"connect this agent" link behave identically.

There is deliberately no send tool, and the setup doc deliberately does not ask
for ``Mail.Send``, so "drafts only" holds at the permission level too.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic_ai.toolsets import FunctionToolset

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.microsoft.com"
GRAPH_V1 = "/v1.0"
BUILTIN_OUTLOOK_URL = "builtin:outlook"

# Graph accepts these names wherever a folder id is expected, so they must not
# be looked up among the Inbox's children -- "inbox" is not its own child.
WELL_KNOWN_FOLDERS = {
    "inbox", "archive", "drafts", "sentitems", "deleteditems",
    "junkemail", "outbox", "clutter", "conflicts", "conversationhistory",
    "localfailures", "msgfolderroot", "recoverableitemsdeletions",
    "scheduled", "searchfolders", "serverfailures", "syncissues",
}

MAX_MESSAGES = 50
DEFAULT_MAX_MESSAGES = 10
DEFAULT_MAX_CHARS = 4000
_TRUNCATED = "…[truncated]"

# Only these fields are pulled for the queue listing. Bodies are fetched per
# message, on purpose: a folder listing that carried them would blow the run's
# context long before the agent got to the first reply.
_LIST_FIELDS = "id,conversationId,subject,from,receivedDateTime,bodyPreview,isRead"


def _address(node: Any) -> str:
    """The bare address out of Graph's nested emailAddress shape."""
    if not isinstance(node, dict):
        return ""
    return str((node.get("emailAddress") or {}).get("address") or "")


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + _TRUNCATED


class OutlookClient:
    """Thin wrapper over the Graph endpoints this app uses.

    Takes an ``httpx.AsyncClient`` so the caller owns authentication (in
    production ``PerUserOAuth2Auth``) and tests can inject a mock transport.
    """

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._folders: dict[str, str] | None = None

    async def _req(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        r = await self._http.request(method, f"{GRAPH_V1}{path}", **kw)
        r.raise_for_status()
        return r.json() if r.content else {}

    async def _inbox_children(self) -> dict[str, str]:
        """``{display name: id}`` for the Inbox's subfolders, fetched once."""
        if self._folders is None:
            data = await self._req(
                "GET", "/me/mailFolders/inbox/childFolders", params={"$top": 100}
            )
            self._folders = {
                str(f.get("displayName")): str(f.get("id"))
                for f in data.get("value") or []
                if f.get("displayName") and f.get("id")
            }
        return self._folders

    async def _folder_id(self, name: str) -> str:
        """Resolve a folder by display name, well-known name, or raw id."""
        if name.lower() in WELL_KNOWN_FOLDERS:
            return name.lower()
        folders = await self._inbox_children()
        if name in folders:
            return folders[name]
        if name in folders.values():  # already an id
            return name
        raise ValueError(
            f"no Inbox subfolder named {name!r}; found: "
            f"{', '.join(sorted(folders)) or '(none)'}"
        )

    async def list_pending(
        self, folder: str, limit: int = DEFAULT_MAX_MESSAGES
    ) -> list[dict[str, Any]]:
        """Messages waiting in a folder, oldest first, metadata only."""
        capped = max(1, min(int(limit or DEFAULT_MAX_MESSAGES), MAX_MESSAGES))
        fid = await self._folder_id(folder)
        data = await self._req(
            "GET",
            f"/me/mailFolders/{fid}/messages",
            params={
                "$top": capped,
                "$select": _LIST_FIELDS,
                # Oldest first: the queue should drain in arrival order.
                "$orderby": "receivedDateTime asc",
            },
        )
        return [
            {
                "message_id": m.get("id", ""),
                "conversation_id": m.get("conversationId", ""),
                "subject": m.get("subject", ""),
                "from": _address(m.get("from")),
                "received": m.get("receivedDateTime", ""),
                "preview": m.get("bodyPreview", ""),
                "unread": not m.get("isRead", False),
            }
            for m in data.get("value") or []
        ]

    async def get_message(
        self, message_id: str, max_chars: int = DEFAULT_MAX_CHARS
    ) -> dict[str, Any]:
        """One message as plain text, capped."""
        # Graph returns HTML unless asked otherwise. Letting it convert is much
        # cheaper and more faithful than stripping tags on this side.
        msg = await self._req(
            "GET",
            f"/me/messages/{message_id}",
            headers={"Prefer": 'outlook.body-content-type="text"'},
        )
        return {
            "message_id": msg.get("id", ""),
            "conversation_id": msg.get("conversationId", ""),
            "subject": msg.get("subject", ""),
            "from": _address(msg.get("from")),
            "to": [_address(r) for r in msg.get("toRecipients") or []],
            "received": msg.get("receivedDateTime", ""),
            "body": _truncate(str((msg.get("body") or {}).get("content") or ""), max_chars),
        }

    async def create_reply_draft(self, message_id: str, body: str) -> dict[str, Any]:
        """Draft a reply to a message. Never sends.

        Two steps by design: ``createReply`` has Graph build a correctly
        threaded draft (recipients, subject, conversation), then a PATCH puts
        our text in it. That is why none of the MIME assembly the Gmail toolset
        needs appears here.
        """
        draft = await self._req("POST", f"/me/messages/{message_id}/createReply")
        draft_id = draft.get("id", "")
        if not draft_id:
            raise RuntimeError(f"createReply returned no draft id for {message_id!r}")
        await self._req(
            "PATCH",
            f"/me/messages/{draft_id}",
            json={"body": {"contentType": "text", "content": body}},
        )
        return {
            "draft_id": draft_id,
            "message_id": message_id,
            "conversation_id": draft.get("conversationId", ""),
        }

    async def move_message(self, message_id: str, destination: str) -> dict[str, Any]:
        """Move a message to another folder. This is the idempotency step."""
        dest = await self._folder_id(destination)
        await self._req(
            "POST", f"/me/messages/{message_id}/move", json={"destinationId": dest}
        )
        return {"message_id": message_id, "moved_to": destination}


def build_http_client(oauth: dict[str, Any], server_key: str) -> httpx.AsyncClient:
    """An httpx client carrying the signed-in user's Microsoft token."""
    from agents.oauth2 import PerUserOAuth2Auth

    return httpx.AsyncClient(
        base_url=GRAPH_API,
        auth=PerUserOAuth2Auth(server_key=server_key, spec_oauth=oauth),
        timeout=httpx.Timeout(30.0),
    )


def outlook_toolset(
    oauth: dict[str, Any],
    *,
    http: httpx.AsyncClient | None = None,
    server_key: str = BUILTIN_OUTLOOK_URL,
) -> FunctionToolset:
    """The Outlook toolset for one agent, ready for ``Agent(toolsets=...)``."""
    session = http or build_http_client(oauth, server_key)
    client = OutlookClient(session)
    toolset = FunctionToolset()
    # The registry closes `http_client` on old toolsets when it swaps a build.
    toolset.http_client = session  # type: ignore[attr-defined]

    @toolset.tool
    async def list_pending(
        folder: str, limit: int = DEFAULT_MAX_MESSAGES
    ) -> list[dict[str, Any]]:
        """List the mail waiting in an Inbox subfolder, oldest first.

        Args:
            folder: Display name of the Inbox subfolder acting as the queue,
                e.g. `agent`.
            limit: How many messages to return (capped at 50).
        """
        return await client.list_pending(folder, limit)

    @toolset.tool
    async def get_message(
        message_id: str, max_chars: int = DEFAULT_MAX_CHARS
    ) -> dict[str, Any]:
        """Read one message as plain text.

        Args:
            message_id: Id from `list_pending`.
            max_chars: Cap before the body is truncated.
        """
        return await client.get_message(message_id, max_chars)

    @toolset.tool
    async def create_reply_draft(message_id: str, body: str) -> dict[str, Any]:
        """Save a draft reply to a message.

        The draft is only saved, never sent. There is no tool that can send it.

        Args:
            message_id: Id from `list_pending`.
            body: Plain-text body of the reply.
        """
        return await client.create_reply_draft(message_id, body)

    @toolset.tool
    async def move_message(message_id: str, destination: str) -> dict[str, Any]:
        """Move a message out of the queue folder, marking it handled.

        Args:
            message_id: Id from `list_pending`.
            destination: Target folder — an Inbox subfolder's display name such
                as `agent-done`, or a well-known folder such as `inbox` or
                `archive`.
        """
        return await client.move_message(message_id, destination)

    return toolset
