"""Does the technical communication user work over HTTP Basic?

Answers one question and stops: can a non-ABAP HTTP client authenticate to the
SAP Support Backbone with `SAP_NOTES_USER` / `SAP_NOTES_PWD`? That is the gate
Phase 2 of the security-note workflow sits behind -- if it opens, note detail
(the fixing support-package level) becomes reachable and most of the UNKNOWN
bucket turns into a real answer. See docs/SAP_SECURITY_NOTES.md.

WHY THIS IS A SEPARATE SCRIPT, RUN BY HAND

It sends a password to an external host. That is the kind of step a person
should choose deliberately rather than have an agent take on their behalf, so
it lives here instead of in a test.

WHY IT ONLY TRIES ONCE

A technical communication user's password does not expire, but the account can
still be locked by repeated failures. One 401 tells you the same thing ten
would, so this stops at the first one. Do not turn it into a sweep.

THE PATHS ARE SAP'S OWN

`/sap/bc/bsp/svt/sapping` and `/note/0040000000874972019` are the prefixes SAP
tells you to enter in SM59 to verify SAP-SUPPORT_PORTAL and
SAP-SUPPORT_NOTE_DOWNLOAD ("Checklist for Support Backbone Update", 2.8.1 and
2.8.3). Reaching them with these credentials is exactly what an ABAP system
running SNOTE does, which is why they are the right test and not a probe of
something SAP never meant to serve.

Run:  python scripts/probe_sapnotes_auth.py
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
except ImportError:  # pragma: no cover - dotenv is in requirements.txt
    pass

# The SAP-SUPPORT_PORTAL check. Tried first because it is the one endpoint
# confirmed to answer `WWW-Authenticate: Basic realm="SAP NetWeaver
# Application Server"`, so a failure here is a credential answer rather than
# an ambiguous "that host does not do Basic at all".
PORTAL = "https://apps.support.sap.com/sap/bc/bsp/svt/sapping"

# The SAP-SUPPORT_NOTE_DOWNLOAD check. Unauthenticated this returns a SAML
# auto-submit form aimed at accounts.sap.com -- the browser flow. Whether it
# serves an HTTP client that presents Basic credentials instead is the actual
# open question, and it is only worth asking once PORTAL has said yes.
NOTE = "https://origin.notesdownloads.sap.com/note/0040000000874972019"


def _redact(user: str) -> str:
    return f"{user[0]}***{user[-1]}" if len(user) > 1 else "***"


async def attempt(
    client: httpx.AsyncClient,
    url: str,
    auth: tuple[str, str],
    save_to: Path | None = None,
) -> int:
    # follow_redirects=False on purpose: a cross-host 302 would otherwise
    # re-send the Authorization header to a host that was never the target.
    r = await client.get(url, auth=auth, follow_redirects=False)
    print(f"  status      {r.status_code}")
    print(f"  www-auth    {r.headers.get('www-authenticate', '')[:70]!r}")
    print(f"  location    {r.headers.get('location', '')[:70]}")
    print(f"  content     {r.headers.get('content-type')}  ({len(r.content)} bytes)")
    # Save BEFORE printing anything derived from the body. The payload is
    # binary and this console is cp1252, so a display bug raises
    # UnicodeEncodeError -- and if that ran first it would throw away the
    # thing that was just fetched, which is exactly what happened once.
    if save_to is not None and r.status_code < 400 and r.content:
        save_to.parent.mkdir(parents=True, exist_ok=True)
        save_to.write_bytes(r.content)
        print(f"  saved       {save_to}  ({len(r.content)} bytes)")

    head = r.content[:120]
    print(f"  first bytes {head[:48].hex(' ')}")
    # Printable ASCII only, everything else as a dot. `errors='replace'`
    # is not enough: it yields U+FFFD, which cp1252 cannot encode either.
    safe = "".join(chr(b) if 32 <= b < 127 else "." for b in head)
    print(f"  as ascii    {safe}")
    return r.status_code


async def main() -> None:
    user = os.environ.get("SAP_NOTES_USER", "").strip()
    pwd = os.environ.get("SAP_NOTES_PWD", "").strip()
    if not user or not pwd:
        sys.exit("SAP_NOTES_USER / SAP_NOTES_PWD are not set in .env")

    print(f"technical user {_redact(user)}  (the value is never printed)")
    print("\n== SAP-SUPPORT_PORTAL ==")

    async with httpx.AsyncClient(timeout=30.0) as client:
        status = await attempt(client, PORTAL, (user, pwd))

        if status == 401:
            print(
                "\n>>> 401 with valid-looking credentials. STOPPING.\n"
                ">>> Not retrying and not trying the other host: repeated\n"
                ">>> failures are what locks the user. Check the user is\n"
                ">>> activated at launchpad.support.sap.com/#/techuser and\n"
                ">>> that it carries the SECCONTACT role for security notes."
            )
            return
        if status >= 400:
            print(f"\n>>> {status}: reached, not authorized. The credential was accepted")
            print(">>> or ignored, but this path is not open to it. Stopping.")
            return

        print("\n>>> AUTHENTICATED. Basic auth works for this credential class.")
        print("\n== SAP-SUPPORT_NOTE_DOWNLOAD ==")
        # Saved so the payload's format can be worked out offline, without
        # anyone needing the credential a second time.
        out = ROOT / "tmp" / "sapnote_0040000000874972019.bin"
        note_status = await attempt(client, NOTE, (user, pwd), save_to=out)
        if note_status < 400:
            print("\n>>> Note download served to an HTTP client. Phase 2 is unblocked.")
        else:
            print(f"\n>>> {note_status}. Portal auth works but note download does not")
            print(">>> answer this client; it may need the SNOTE download protocol.")


if __name__ == "__main__":
    asyncio.run(main())
