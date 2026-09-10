"""Can me.sap.com's private note API be reached without a browser?

`mcp-sap-notes` gets SAP note detail -- including the fixing support-package
level, which is the one field Phase 2 needs -- from
`https://me.sap.com/backend/raw/sapnotes/Detail`. It reaches it by driving a
real browser through the accounts.sap.com IAS form with Playwright and reusing
the resulting session cookies.

This asks whether that is strictly necessary, or whether the technical
communication user's HTTP Basic credential (which the backbone accepts) is
also accepted here. If it is, Phase 2 needs no browser, no Playwright in the
Cloud Foundry buildpack, and no dialog user with MFA.

Expected answer is no: me.sap.com sits behind XSUAA/SAML, a different auth
system from the NetWeaver backbone at apps.support.sap.com. Worth one request
to find out rather than assuming, given assuming has been wrong twice today.

Run:  python scripts/probe_me_sap_notes_api.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

# A real, well-known SAP note number. Detail is the endpoint that carries
# `supportPackages` / `validity`; Search is the OData proxy the MCP server
# uses for keyword lookup (fuzzy only -- no CVSS or date filter, which is why
# NVD still does discovery).
NOTE = "3771065"
URLS = [
    f"https://me.sap.com/backend/raw/sapnotes/Detail?q={NOTE}&t=E",
    f"https://me.sap.com/backend/raw/sapnotes/Search?q={NOTE}&t=E&maxResults=1",
]


def summarise(body: bytes, limit: int = 300) -> str:
    """Printable ASCII only; this console is cp1252 and bodies vary wildly."""
    return "".join(chr(b) if 32 <= b < 127 else "." for b in body[:limit])


async def main() -> None:
    user = os.environ.get("SAP_NOTES_USER", "").strip()
    pwd = os.environ.get("SAP_NOTES_PWD", "").strip()
    if not user or not pwd:
        sys.exit("SAP_NOTES_USER / SAP_NOTES_PWD are not set in .env")

    async with httpx.AsyncClient(timeout=30.0) as client:
        for url in URLS:
            print(f"\n== {url.split('?')[0].rsplit('/', 1)[-1]} ==")
            for label, auth in (("with basic", (user, pwd)), ("anonymous", None)):
                try:
                    r = await client.get(url, auth=auth, follow_redirects=False)
                except Exception as e:  # noqa: BLE001
                    print(f"  {label:11} ERROR {type(e).__name__}")
                    continue
                ctype = (r.headers.get("content-type") or "")[:40]
                print(f"  {label:11} {r.status_code} {ctype} {len(r.content)}b")
                # A JSON body means the API answered. An HTML body means it
                # served the SPA bootstrap or a login page, i.e. it wants a
                # browser session and this credential did not substitute.
                if "json" in ctype:
                    print(f"    JSON -> {summarise(r.content)}")
                elif r.content:
                    print(f"    {summarise(r.content, 140)}")


if __name__ == "__main__":
    asyncio.run(main())
