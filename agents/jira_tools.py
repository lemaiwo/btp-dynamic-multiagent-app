"""In-process Jira tools over the Jira Server / Data Center REST v2 API.

Attached when an agent lists the pseudo-URL ``builtin:jira``. Unlike the mail
toolsets, this one holds no credential: the target URL and its Authorization
header both come from a BTP destination, resolved at call time. The only thing
configured here is the destination's name.

Jira is Server/DC, so a comment body is a plain wiki-markup string. Jira Cloud
would need Atlassian Document Format instead; nothing here tries to serve both.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic_ai.toolsets import FunctionToolset

from agents.lookback import parse_lookback

logger = logging.getLogger(__name__)

BUILTIN_JIRA_URL = "builtin:jira"
JIRA_API = "/rest/api/2"

MAX_ISSUES = 50
DEFAULT_MAX_ISSUES = 10
DEFAULT_MAX_CHARS = 4000
_TRUNCATED = "…[truncated]"

# Comments come back with the search, so the already-answered filter costs no
# extra request. Descriptions are truncated rather than dropped: an agent that
# must fetch every issue in full to triage a list burns its context on issues
# it will skip.
_LIST_FIELDS = ["summary", "status", "reporter", "updated", "description", "comment"]


def _jql_quote(value: str) -> str:
    """A JQL string literal.

    Backslash first, then quote -- the other order would re-escape the
    backslashes it just inserted. Without this, a project key containing a
    quote could close the literal and append clauses of its own.
    """
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_jql(project: str, status: str, lookback_minutes: int | None) -> str:
    """The search query for a project, a status and a window.

    Built here rather than accepted from the agent. Raw JQL from a model is
    both a correctness problem -- there is no way to enforce the configured
    project -- and a reach problem, since JQL can address every issue the
    credential can see.
    """
    clauses: list[str] = []
    if project:
        clauses.append(f"project = {_jql_quote(project)}")
    if status:
        clauses.append(f"status = {_jql_quote(status)}")
    if lookback_minutes:
        clauses.append(f"updated >= {_jql_quote(f'-{int(lookback_minutes)}m')}")
    order = "ORDER BY updated ASC"
    return f"{' AND '.join(clauses)} {order}" if clauses else order


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + _TRUNCATED


def _person(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    return str(node.get("displayName") or node.get("name") or "")


def _summarize(issue: dict[str, Any]) -> dict[str, Any]:
    fields = issue.get("fields") or {}
    return {
        "key": str(issue.get("key") or ""),
        "summary": str(fields.get("summary") or ""),
        "status": str((fields.get("status") or {}).get("name") or ""),
        "reporter": _person(fields.get("reporter")),
        "updated": str(fields.get("updated") or ""),
        "description": _truncate(
            str(fields.get("description") or ""), DEFAULT_MAX_CHARS
        ),
    }


def _comments_of(issue: dict[str, Any]) -> list[dict[str, Any]]:
    return ((issue.get("fields") or {}).get("comment") or {}).get("comments") or []


def _answered_by(issue: dict[str, Any], account: str) -> bool:
    """Whether this account already commented on the issue.

    This is the idempotency marker the Outlook integration never had: with no
    way to record that a message was handled, every run re-sent the same
    replies. Jira carries the record in the issue itself.
    """
    if not account:
        return False
    return any(
        str((c.get("author") or {}).get("name") or "") == account
        for c in _comments_of(issue)
    )


class JiraClient:
    """Thin wrapper over the Jira endpoints this app uses.

    Takes a resolver rather than a base URL because both the URL and the
    Authorization header come from the destination, and both change under us
    when its token is refreshed.

    ``project``, ``status`` and ``lookback_minutes`` are the configured values.
    The first two are pinned: a call cannot override them. The last is a
    ceiling: a call can narrow the window but never widen it.
    """

    def __init__(
        self,
        resolver: Any,
        http: httpx.AsyncClient,
        project: str = "",
        status: str = "",
        lookback_minutes: int | None = None,
    ) -> None:
        self._resolver = resolver
        self._http = http
        self.project = (project or "").strip()
        self.status = (status or "").strip()
        self.lookback_minutes = lookback_minutes
        self._account: str | None = None

    def _window(self, requested: int | None) -> int | None:
        """The effective window: the tighter of the request and the ceiling."""
        if requested is None:
            return self.lookback_minutes
        if self.lookback_minutes is None:
            return requested
        return min(requested, self.lookback_minutes)

    async def _req(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        """One Jira call, refreshing the destination once on a 401.

        A 401 here means the destination's cached token aged out, not that the
        credential is wrong -- so invalidate and retry exactly once. A second
        401 is a real failure and must not become a loop.
        """
        destination = await self._resolver.resolve()
        response = await self._http.request(
            method,
            f"{destination.url}{JIRA_API}{path}",
            headers=destination.headers,
            **kw,
        )
        if response.status_code == 401:
            self._resolver.invalidate()
            destination = await self._resolver.resolve()
            response = await self._http.request(
                method,
                f"{destination.url}{JIRA_API}{path}",
                headers=destination.headers,
                **kw,
            )
        response.raise_for_status()
        return response.json() if response.content else {}

    async def whoami(self) -> str:
        """The account name behind the destination, fetched once.

        Used only to recognise this agent's own comments. An empty result
        disables the answered-issue filter rather than failing the listing --
        losing repeat-run safety is bad, but failing every listing is worse.
        """
        if self._account is None:
            try:
                data = await self._req("GET", "/myself")
            except httpx.HTTPError:
                logger.warning("Could not identify the Jira account", exc_info=True)
                self._account = ""
            else:
                self._account = str(data.get("name") or data.get("key") or "")
        return self._account

    async def list_issues(
        self,
        *,
        project: str | None = None,
        status: str | None = None,
        limit: int = DEFAULT_MAX_ISSUES,
        lookback_minutes: int | None = None,
    ) -> list[dict[str, Any]]:
        """Issues matching the configured filter, oldest update first.

        Issues this account has already commented on are dropped, so the
        result can be shorter than ``limit`` even when more issues match.
        """
        capped = max(1, min(int(limit or DEFAULT_MAX_ISSUES), MAX_ISSUES))
        jql = build_jql(
            self.project or (project or "").strip(),
            self.status or (status or "").strip(),
            self._window(lookback_minutes),
        )
        try:
            data = await self._req(
                "POST",
                "/search",
                json={"jql": jql, "maxResults": capped, "fields": _LIST_FIELDS},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400:
                # A 400 is nearly always a project key or status that does not
                # exist. Without the query, the operator cannot tell which.
                raise RuntimeError(
                    f"Jira rejected the search. JQL sent: {jql}. "
                    f"Response: {exc.response.text[:400]}"
                ) from exc
            raise

        account = await self.whoami()
        return [
            _summarize(issue)
            for issue in data.get("issues") or []
            if not _answered_by(issue, account)
        ]
