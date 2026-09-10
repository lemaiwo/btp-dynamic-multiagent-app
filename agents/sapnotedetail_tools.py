"""SAP note detail from the private me.sap.com backend.

NVD gives a note number and a CVSS score but never says which support package
fixes it, so half of every digest lands in an UNKNOWN bucket. That field lives
in SAP's backbone and has no supported API: two SAP Community requests for one
went unanswered, and the Support Portal OData services do not carry it.

What does carry it is `me.sap.com/backend/raw/sapnotes/Detail`, a private
endpoint behind XSUAA/SAML. It ignores HTTP Basic outright -- probed, it
returns a byte-identical bootstrap page with credentials and without -- so the
only usable credential is a session cookie a human obtained in a browser. See
docs/superpowers/specs/2026-09-10-sap-note-detail-design.md, and note the
upstream project's warning that this is a private API subject to SAP's ToS.

Everything here degrades rather than raises. A private API can change shape
without notice, and a thin digest that says so is worth more than a run that
dies.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic_ai.toolsets import FunctionToolset

logger = logging.getLogger(__name__)

BUILTIN_SAPNOTEDETAIL_URL = "builtin:sapnotedetail"

DETAIL_URL = "https://me.sap.com/backend/raw/sapnotes/Detail"

DEFAULT_MAX_CONCURRENCY = 5

# Confirmed against two live responses on 2026-09-10; see the spec's
# "Confirmed API shape". Everything of interest hangs off Response.SAPNote,
# and each table is an object with an `Items` list, not a bare list.
_KEY_HEADER = "Header"
_KEY_TITLE = "Title"
_KEY_VALIDITY = "Validity"
# The fixing level lives here, NOT in `SupportPackage`, which was empty on
# every one of ten sampled notes.
_KEY_SUPPORT_PACKAGE_PATCH = "SupportPackagePatch"


def _text(value: Any) -> str:
    """A scalar as text, unwrapping the {"value": ...} envelope SAP uses."""
    if isinstance(value, dict):
        value = value.get("value")
    if value is None:
        return ""
    return str(value).strip()


def unavailable(note: str, reason: str) -> dict[str, Any]:
    """The shape every failure returns. Never raises, always answers."""
    return {
        "note": note,
        "status": "unavailable",
        "reason": reason,
        "title": "",
        "note_type": "",
        "version": "",
        "validity": [],
        "support_packages": [],
    }


def _items(node: Any) -> list[dict[str, Any]]:
    """The `Items` list of a SAP table node, or empty."""
    if not isinstance(node, dict):
        return []
    items = node.get("Items")
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _packages(node: Any) -> list[dict[str, str]]:
    """The fixing support-package levels."""
    out: list[dict[str, str]] = []
    for entry in _items(node):
        row = {
            "component_version": _text(entry.get("SoftwareComponentVersion")),
            "support_package": _text(entry.get("SupportPackage")),
            "patch": _text(entry.get("SupportPackagePatch")),
        }
        if any(row.values()):
            out.append(row)
    return out


def _validity(node: Any) -> list[dict[str, str]]:
    """Which components and release ranges the note applies to.

    The only place a software component appears: the response carries no
    top-level component or priority field.
    """
    out: list[dict[str, str]] = []
    for entry in _items(node):
        out.append({
            "software_component": _text(entry.get("SoftwareComponent")),
            "from": _text(entry.get("From")),
            "to": _text(entry.get("To")),
        })
    return out


def parse_detail(raw: Any, note: str) -> dict[str, Any]:
    """One `Detail` response, normalized. Returns `unavailable` on any surprise."""
    if not isinstance(raw, dict):
        return unavailable(note, "response was not a JSON object")

    # Confirmed envelope: {"Request": {...}, "Response": {"SAPNote": {...}}}.
    record = raw.get("Response")
    record = record.get("SAPNote") if isinstance(record, dict) else None
    if not isinstance(record, dict):
        return unavailable(note, "response carried no Response.SAPNote")

    known = {_KEY_HEADER, _KEY_TITLE, _KEY_VALIDITY, _KEY_SUPPORT_PACKAGE_PATCH}
    if not known & set(record):
        return unavailable(note, "response carried none of the expected fields")

    header = record.get(_KEY_HEADER) or {}
    header = header if isinstance(header, dict) else {}
    return {
        "note": note,
        "status": "ok",
        "title": _text(record.get(_KEY_TITLE)),
        "note_type": _text(header.get("Type")),
        "version": _text(header.get("Version")),
        "validity": _validity(record.get(_KEY_VALIDITY)),
        "support_packages": _packages(record.get(_KEY_SUPPORT_PACKAGE_PATCH)),
    }


class SessionExpired(RuntimeError):
    """The me.sap.com session is no longer usable. Only a human can fix it."""


class SapNoteDetailClient:
    """Fetches note detail with a browser-obtained session cookie.

    Takes an ``httpx.AsyncClient`` so tests can inject a mock transport. The
    cookie is sent as a header rather than a cookie jar because it arrives as
    one opaque serialized string from the operator's browser.

    ``cookie`` is either the string itself or an async callable returning it.
    The callable form exists because ``build_builtin_toolset`` passes only
    ``(oauth, server_key, auth_mode)`` -- there is no principal at build time
    to resolve a credential for -- so production resolves it per call, the
    same way the forwarded JWT is read from a contextvar rather than captured
    when the registry is built.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        cookie: str | Callable[[], Awaitable[str]] = "",
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> None:
        self._http = http
        self._cookie = cookie
        self.max_concurrency = max(1, int(max_concurrency))

    async def _cookie_header(self) -> str:
        value = self._cookie
        if callable(value):
            value = await value()
        return (value or "").strip()

    async def get_detail(self, note: str) -> dict[str, Any]:
        """One note. Auth failures raise SessionExpired; nothing else raises."""
        cookie = await self._cookie_header()
        if not cookie:
            # No stored session at all reads the same as a dead one: a human
            # has to open a browser either way.
            raise SessionExpired(f"note {note}: no session cookie is stored")
        try:
            r = await self._http.get(
                DETAIL_URL,
                params={"q": note, "t": "E"},
                headers={"Cookie": cookie, "Accept": "application/json"},
                follow_redirects=False,
            )
        except Exception as e:  # noqa: BLE001 - a flaky note must not end a run
            logger.warning("note %s: transport error %s", note, type(e).__name__)
            return unavailable(note, f"transport error: {type(e).__name__}")

        if r.status_code in (401, 403):
            raise SessionExpired(f"note {note}: HTTP {r.status_code}")
        # The tell for a dead session is an HTML bootstrap page with a 200,
        # not a 401 -- that is exactly how this endpoint refuses anonymous
        # callers, so it must be treated as an auth failure, not a bad note.
        if "json" not in (r.headers.get("content-type") or "").lower():
            raise SessionExpired(f"note {note}: got HTML, the session is not valid")
        if r.status_code >= 400:
            return unavailable(note, f"HTTP {r.status_code}")

        try:
            return parse_detail(r.json(), note)
        except ValueError:
            return unavailable(note, "response was not valid JSON")

    async def get_details(self, notes: list[str]) -> dict[str, Any]:
        """Many notes, fetched concurrently behind a cap.

        One call per note would be one LLM turn per note -- ~129 for a monthly
        run -- so the whole list is fetched here instead.

        A dead session stops further fetching: retrying a hundred times
        against a corpse is noise, and the answer will not change until a
        human opens a browser. Notes already fetched are kept, and the ones
        never attempted are reported as unavailable rather than dropped, so
        the count always matches what was asked for.
        """
        wanted = [str(n).strip() for n in (notes or []) if str(n).strip()]
        if not wanted:
            return {"notes": [], "count": 0, "unavailable": 0, "session_expired": False}

        results: dict[str, dict[str, Any]] = {}
        session_expired = False
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def one(note: str) -> None:
            nonlocal session_expired
            if session_expired:
                return
            async with semaphore:
                if session_expired:
                    return
                try:
                    results[note] = await self.get_detail(note)
                except SessionExpired as e:
                    logger.warning("me.sap.com session is dead: %s", e)
                    session_expired = True

        await asyncio.gather(*(one(n) for n in wanted))

        ordered = [
            results.get(n) or unavailable(
                n, "session expired" if session_expired else "not fetched"
            )
            for n in wanted
        ]
        return {
            "notes": ordered,
            "count": len(ordered),
            "unavailable": sum(1 for n in ordered if n["status"] != "ok"),
            "session_expired": session_expired,
        }


def sapnotedetail_toolset(
    oauth: dict[str, Any],
    *,
    http: httpx.AsyncClient | None = None,
    cookie: str | None = None,
    server_key: str = BUILTIN_SAPNOTEDETAIL_URL,
    auth_mode: str | None = None,
) -> FunctionToolset:
    """The note-detail toolset for one agent.

    The cookie is NOT read from ``oauth``: it is a credential, and credentials
    live in ``mcp_oauth_tokens``, never in a server config block. Passing it
    explicitly is the test path; Task 5 replaces the default with a resolver
    that reads the current principal's stored session at call time, because
    the registry has no principal to resolve one for at build time.
    """
    session = http or httpx.AsyncClient(timeout=httpx.Timeout(60.0))
    client = SapNoteDetailClient(session, cookie or "")
    toolset = FunctionToolset()
    # The registry closes `http_client` on old toolsets when it swaps a build.
    toolset.http_client = session  # type: ignore[attr-defined]

    @toolset.tool
    async def get_note_details(notes: list[str]) -> dict[str, Any]:
        """Look up SAP note detail, including which support package fixes each.

        Pass every note number you need in ONE call — the list is fetched
        concurrently. Do not call this once per note.

        Each entry carries `support_packages` (the fixing level per software
        component) and `validity` (which releases the note applies to). An
        entry with `status` of `unavailable` could not be read; report it as
        unknown, never as "not affected".

        If `session_expired` is true, the SAP session died and the remaining
        notes were never checked. Say so prominently — the report is
        incomplete, not clean.

        Args:
            notes: SAP note numbers, e.g. ["3771065", "3747649"].
        """
        return await client.get_details(notes)

    return toolset
