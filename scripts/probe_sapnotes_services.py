"""What does apps.support.sap.com serve to an authenticated technical user?

`scripts/probe_sapnotes_auth.py` established that HTTP Basic works for the
technical communication user. This asks the follow-up question: is there a
service there returning note metadata as structured data, rather than the
SAPCAR-wrapped signed archive `notesdownloads.sap.com` hands back?

That matters because Phase 2 of the security-note workflow needs one field --
the support-package level that fixes a note -- and getting it from a JSON or
XML service is a different proposition from decompressing a proprietary
archive and parsing SAP's note markup. See docs/SAP_SECURITY_NOTES.md.

SAFETY

Every request here is authenticated, and authentication is already known to
succeed, so there is no lockout risk: a 404 means "no such path", not "bad
password". Redirects are still not followed, so the Authorization header
cannot be re-sent to another host.

Run:  python scripts/probe_sapnotes_services.py
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

HOST = "https://apps.support.sap.com"

# Ordered cheapest-signal first. The `svt` namespace is the one the working
# SAP-SUPPORT_PORTAL check path (/sap/bc/bsp/svt/sapping) already sits in, so
# it is the most likely place for a sibling service.
PATHS = [
    # Maintenance Planner. This is the API SAP Cloud ALM uses to fetch
    # applicable recommended security notes, with a technical S-user -- the
    # credential class we now hold. `/sap/support/mp/` is MP's documented
    # location, not a guessed one.
    #
    # The manifest is the point of this list: a UI5 app declares its OData
    # services under `sap.app/dataSources`, so reading it names the real API
    # instead of guessing service names at SAP's front door.
    "/sap/support/mp/index.html",
    "/sap/support/mp/manifest.json",
    "/sap/support/mp/webapp/manifest.json",
    "/sap/support/mp/",
]


def summarise(body: bytes, limit: int = 220) -> str:
    """Printable ASCII only -- this console is cp1252 and the bodies vary."""
    return "".join(chr(b) if 32 <= b < 127 else "." for b in body[:limit])


async def main() -> None:
    user = os.environ.get("SAP_NOTES_USER", "").strip()
    pwd = os.environ.get("SAP_NOTES_PWD", "").strip()
    if not user or not pwd:
        sys.exit("SAP_NOTES_USER / SAP_NOTES_PWD are not set in .env")

    interesting: list[str] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for path in PATHS:
            url = f"{HOST}{path}"
            try:
                r = await client.get(url, auth=(user, pwd), follow_redirects=False)
            except Exception as e:  # noqa: BLE001
                print(f"{path[:52]:54} ERR  {type(e).__name__}", flush=True)
                continue

            ctype = (r.headers.get("content-type") or "")[:34]
            loc = r.headers.get("location", "")
            line = f"{path[:52]:54} {r.status_code} {ctype:34} {len(r.content):>7}b"
            if loc:
                line += f" -> {loc[:44]}"
            print(line, flush=True)

            # Structured bodies are worth reading whatever the status. A 403
            # from SAP Gateway carries an XML error naming the authorization
            # that is missing, which is the difference between "this service
            # does not exist" and "this user cannot reach it yet" -- and only
            # the second one is actionable.
            if r.content and ("xml" in ctype or "json" in ctype):
                if r.status_code < 400:
                    interesting.append(path)
                print(f"    {summarise(r.content, 700)}", flush=True)

    print("\n=== structured responses ===")
    print("\n".join(f"  {p}" for p in interesting) if interesting
          else "  none -- no OData/JSON service answered on these paths")


if __name__ == "__main__":
    asyncio.run(main())
