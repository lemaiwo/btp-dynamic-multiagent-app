"""Gmail tools served in-process against the Gmail REST API.

Google's hosted Gmail MCP server (``gmailmcp.googleapis.com``) authenticates a
token from a self-registered OAuth client and then refuses every ``tools/call``
with "The caller does not have permission". Scope, publishing status, Workspace
policy and token freshness were each eliminated -- see docs/GMAIL_SETUP.md. The
same token is accepted by ``gmail.googleapis.com``, so the handful of tools this
app actually needs are implemented here instead of over MCP.

An agent opts in by listing the pseudo-URL ``builtin:gmail`` among its MCP
servers with its usual ``oauth`` block. ``agents.registry`` spots the scheme and
attaches this toolset instead of opening an MCP connection; everything else --
the admin form, the token store, the sign-in link -- is unchanged, because the
HTTP client here authenticates with the very same ``PerUserOAuth2Auth`` used for
oauth2 MCP servers.

There is deliberately no send tool. "Drafts only" is then a property of the
toolset rather than a line in a prompt that a model may talk itself out of.
"""

from __future__ import annotations

import base64
import logging
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any

import httpx
from pydantic_ai.toolsets import FunctionToolset

logger = logging.getLogger(__name__)

GMAIL_API = "https://gmail.googleapis.com"
BUILTIN_GMAIL_URL = "builtin:gmail"

# Only URLs in here bypass the https/allow-list rules in admin validation, so
# the set stays explicit rather than "anything starting with builtin:".
BUILTIN_URLS = {BUILTIN_GMAIL_URL}

# Gmail caps threads.list at 500; the real limit here is the model's context.
# Each thread costs a metadata round trip and a few hundred tokens.
MAX_THREADS = 50
DEFAULT_MAX_THREADS = 10

# A single mail can carry a whole quoted history. Truncating per message is what
# keeps one fat thread from ending the run with context_length_exceeded.
DEFAULT_MAX_CHARS = 4000
_TRUNCATED = "…[truncated]"


def is_builtin_url(url: str | None) -> bool:
    """True when ``url`` names a built-in toolset rather than an MCP server."""
    return str(url or "").strip().lower() in BUILTIN_URLS


def _decode(data: str | None) -> str:
    if not data:
        return ""
    # Gmail uses base64url without padding; b64decode wants it.
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode()).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - a malformed part must not kill the run
        logger.warning("Undecodable Gmail body part; skipping it")
        return ""


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    """Header lookup, lower-cased: Gmail's casing is not guaranteed."""
    return {
        str(h.get("name", "")).lower(): str(h.get("value", ""))
        for h in payload.get("headers") or []
    }


def _plain_text(payload: dict[str, Any]) -> str:
    """The text/plain body, walking nested multiparts.

    text/html is skipped rather than converted: every mail that has HTML here
    also has a plain alternative, and stripping tags would only add noise.
    """
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        return _decode((payload.get("body") or {}).get("data"))
    for part in payload.get("parts") or []:
        found = _plain_text(part)
        if found:
            return found
    return ""


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + _TRUNCATED


class GmailClient:
    """Thin wrapper over the Gmail REST endpoints this app uses.

    Takes an ``httpx.AsyncClient`` so the caller owns authentication (in
    production, ``PerUserOAuth2Auth``) and tests can inject a mock transport.
    """

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        r = await self._http.get(f"/gmail/v1{path}", params=params or None)
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        r = await self._http.post(f"/gmail/v1{path}", json=payload)
        r.raise_for_status()
        return r.json()

    async def search_threads(
        self, query: str, max_results: int = DEFAULT_MAX_THREADS
    ) -> list[dict[str, Any]]:
        """Threads matching a Gmail query, newest first, metadata only."""
        capped = max(1, min(int(max_results or DEFAULT_MAX_THREADS), MAX_THREADS))
        listing = await self._get("/users/me/threads", q=query, maxResults=capped)

        rows: list[dict[str, Any]] = []
        for stub in listing.get("threads") or []:
            tid = stub.get("id")
            if not tid:
                continue
            # threads.list returns only id+snippet, so the headers need a second
            # call. format=metadata keeps it cheap -- no bodies come back.
            detail = await self._get(
                f"/users/me/threads/{tid}",
                format="metadata",
                metadataHeaders=["Subject", "From", "Date"],
            )
            messages = detail.get("messages") or []
            head = _headers((messages[0] or {}).get("payload") or {}) if messages else {}
            rows.append(
                {
                    "thread_id": tid,
                    "subject": head.get("subject", ""),
                    "from": head.get("from", ""),
                    "date": head.get("date", ""),
                    "snippet": stub.get("snippet", ""),
                    "message_count": len(messages),
                }
            )
        return rows

    async def get_thread(
        self, thread_id: str, max_chars: int = DEFAULT_MAX_CHARS
    ) -> dict[str, Any]:
        """Every message in a thread as plain text, each body capped."""
        thread = await self._get(f"/users/me/threads/{thread_id}", format="full")
        messages = []
        for msg in thread.get("messages") or []:
            payload = msg.get("payload") or {}
            head = _headers(payload)
            messages.append(
                {
                    "message_id": msg.get("id", ""),
                    "from": head.get("from", ""),
                    "to": head.get("to", ""),
                    "date": head.get("date", ""),
                    "subject": head.get("subject", ""),
                    "body": _truncate(_plain_text(payload), max_chars),
                }
            )
        return {"thread_id": thread_id, "messages": messages}

    async def create_draft(self, thread_id: str, body: str) -> dict[str, Any]:
        """Draft a reply to the last message in a thread. Never sends."""
        thread = await self._get(f"/users/me/threads/{thread_id}", format="metadata")
        messages = thread.get("messages") or []
        if not messages:
            raise ValueError(f"thread {thread_id!r} has no messages to reply to")
        head = _headers((messages[-1] or {}).get("payload") or {})

        subject = head.get("subject", "")
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}" if subject else "Re:"

        reply = EmailMessage()
        reply["To"] = head.get("reply-to") or head.get("from", "")
        reply["Subject"] = subject
        # threadId alone does not thread the reply in other mail clients -- the
        # RFC headers are what carry the conversation.
        parent = head.get("message-id", "")
        if parent:
            reply["In-Reply-To"] = parent
            existing = head.get("references", "")
            reply["References"] = f"{existing} {parent}".strip()
        reply.set_content(body)

        raw = base64.urlsafe_b64encode(reply.as_bytes()).decode()
        created = await self._post(
            "/users/me/drafts", {"message": {"raw": raw, "threadId": thread_id}}
        )
        return {
            "draft_id": created.get("id", ""),
            "thread_id": thread_id,
            "to": parseaddr(reply["To"])[1],
            "subject": subject,
        }

    async def list_labels(self) -> dict[str, str]:
        """Every label as ``{display name: id}``."""
        data = await self._get("/users/me/labels")
        return {
            str(lbl.get("name", "")): str(lbl.get("id", ""))
            for lbl in data.get("labels") or []
            if lbl.get("name") and lbl.get("id")
        }

    async def modify_labels(
        self,
        thread_id: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add and remove labels on a whole thread, by name or by id."""
        by_name = await self.list_labels()
        known_ids = set(by_name.values())

        def resolve(values: list[str] | None) -> list[str]:
            out = []
            for v in values or []:
                if v in by_name:
                    out.append(by_name[v])
                elif v in known_ids:
                    out.append(v)
                else:
                    # Loud on purpose: silently dropping the removal would leave
                    # the mail labelled and every later run would redo it.
                    raise ValueError(
                        f"unknown label {v!r}; call list_labels for valid names"
                    )
            return out

        payload = {"addLabelIds": resolve(add), "removeLabelIds": resolve(remove)}
        await self._post(f"/users/me/threads/{thread_id}/modify", payload)
        return {"thread_id": thread_id, **payload}


def build_http_client(oauth: dict[str, Any], server_key: str) -> httpx.AsyncClient:
    """An httpx client that carries the signed-in user's Google token.

    ``PerUserOAuth2Auth`` attaches and refreshes the token and raises
    ``OAuthAuthorizationRequired`` when the user has not connected yet, which
    the registry already turns into a sign-in link.
    """
    from agents.oauth2 import PerUserOAuth2Auth

    return httpx.AsyncClient(
        base_url=GMAIL_API,
        auth=PerUserOAuth2Auth(server_key=server_key, spec_oauth=oauth),
        timeout=httpx.Timeout(30.0),
    )


def gmail_toolset(
    oauth: dict[str, Any],
    *,
    http: httpx.AsyncClient | None = None,
    server_key: str = BUILTIN_GMAIL_URL,
    auth_mode: str | None = None,
) -> FunctionToolset:
    """The Gmail toolset for one agent, ready to pass to ``Agent(toolsets=...)``.

    ``auth_mode`` is accepted so every built-in factory has the same signature,
    and rejected if it asks for app-only: Google's service-account equivalent
    needs domain-wide delegation, which is a different setup entirely and is not
    implemented here. Failing loudly beats silently falling back to per-user
    auth on an agent configured to expect no user.
    """
    if auth_mode == "app_only":
        raise ValueError(
            "builtin:gmail does not support client_credentials; Google app-only "
            "access needs domain-wide delegation. Use auth_mode 'oauth2'."
        )
    session = http or build_http_client(oauth, server_key)
    client = GmailClient(session)
    toolset = FunctionToolset()
    # The registry closes `http_client` on every old toolset when it swaps a
    # build. Without this the client leaks one socket pool per reload.
    toolset.http_client = session  # type: ignore[attr-defined]

    @toolset.tool
    async def search_threads(
        query: str, max_results: int = DEFAULT_MAX_THREADS
    ) -> list[dict[str, Any]]:
        """Find email threads with a Gmail search query.

        Args:
            query: Gmail search syntax, e.g. `label:agent`, `is:unread
                newer_than:7d`. Labels match on their display name, and a name
                containing spaces needs quoting: `label:"TE BETALEN"`.
            max_results: How many threads to return (capped at 50).
        """
        return await client.search_threads(query, max_results)

    @toolset.tool
    async def get_thread(
        thread_id: str, max_chars: int = DEFAULT_MAX_CHARS
    ) -> dict[str, Any]:
        """Read every message in a thread as plain text.

        Args:
            thread_id: Thread id from `search_threads`.
            max_chars: Per-message cap before the body is truncated.
        """
        return await client.get_thread(thread_id, max_chars)

    @toolset.tool
    async def create_draft(thread_id: str, body: str) -> dict[str, Any]:
        """Save a draft reply to the newest message in a thread.

        The draft is only saved, never sent. There is no tool that can send it.

        Args:
            thread_id: Thread id from `search_threads`.
            body: Plain-text body of the reply.
        """
        return await client.create_draft(thread_id, body)

    @toolset.tool
    async def list_labels() -> dict[str, str]:
        """Every Gmail label as a mapping of display name to label id."""
        return await client.list_labels()

    @toolset.tool
    async def modify_labels(
        thread_id: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add and/or remove labels on a whole thread.

        Args:
            thread_id: Thread id from `search_threads`.
            add: Label names (or ids) to add, e.g. `["agent-drafted"]`.
            remove: Label names (or ids) to remove, e.g. `["agent"]`.
        """
        return await client.modify_labels(thread_id, add, remove)

    return toolset
