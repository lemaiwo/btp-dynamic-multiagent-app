"""Go/no-go probe for the Outlook (Microsoft Graph) integration.

Answers the three tenant questions that decide whether the integration is an
afternoon's work or a dead end, before any of it is written:

  1. Can this app registration sign a user in at all?
  2. Does user consent suffice for Mail.ReadWrite, or is an admin needed?
  3. Does a refresh token come back (i.e. is offline_access wired up)?

It also resolves the Inbox subfolder that will act as the work queue and prints
its id for the agent config.

Standalone on purpose: it imports nothing from this app, touches no database,
and creates, moves or deletes nothing in the mailbox. Reads only.

Usage:
    python scripts/probe_outlook.py --tenant <ID> --client <ID> --secret <VALUE>
                                    [--folder agent] [--port 7932]

See docs/OUTLOOK_SETUP.md for what each outcome means.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser

try:
    import httpx
except ImportError:  # pragma: no cover - the script may run outside the venv
    sys.exit("httpx is required:  pip install httpx   (or use .venv's python)")

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = "https://graph.microsoft.com/Mail.ReadWrite offline_access"

# Entra rejects plain http redirect URIs except on the loopback host, and it
# treats localhost and 127.0.0.1 as different strings. This must match the
# redirect URI registered in step 1 of the doc exactly.
REDIRECT_HOST = "localhost"

_received: dict[str, str] = {}
_done = threading.Event()


class _Callback(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        _received.update(
            {k: v[0] for k, v in urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query).items()}
        )
        ok = "code" in _received
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = (
            "<h2>Signed in.</h2><p>Close this tab and return to the terminal.</p>"
            if ok else
            f"<h2>Sign-in failed.</h2><pre>{_received}</pre>"
        )
        self.wfile.write(body.encode())
        _done.set()

    def log_message(self, *_args) -> None:  # keep the console clean
        return


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(40)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _explain(err: str, desc: str) -> str:
    for code, meaning in [
        ("AADSTS65001", "consent not granted -- an admin must approve Mail.ReadWrite"),
        ("AADSTS90094", "admin consent required for this permission"),
        ("AADSTS50011", "redirect URI mismatch -- register http://localhost:PORT/oauth/callback"),
        ("AADSTS7000218", "registered as a public client; it must be a Web app with a secret"),
        ("AADSTS53003", "blocked by a Conditional Access policy -- escalate to IT"),
        ("AADSTS700016", "application not found in this tenant -- check the client and tenant ids"),
    ]:
        if code in err or code in desc:
            return f"{code}: {meaning}"
    return f"{err}: {desc}" if err else "unknown error"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True, help="Directory (tenant) ID")
    ap.add_argument("--client", required=True, help="Application (client) ID")
    ap.add_argument("--secret", required=True, help="Client secret VALUE, not its id")
    ap.add_argument("--folder", default="agent", help="Inbox subfolder to look for")
    ap.add_argument("--port", type=int, default=7932)
    args = ap.parse_args()

    redirect = f"http://{REDIRECT_HOST}:{args.port}/oauth/callback"
    base = f"https://login.microsoftonline.com/{args.tenant}/oauth2/v2.0"
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)

    server = http.server.HTTPServer((REDIRECT_HOST, args.port), _Callback)
    threading.Thread(target=server.handle_request, daemon=True).start()

    url = f"{base}/authorize?" + urllib.parse.urlencode({
        "client_id": args.client,
        "response_type": "code",
        "redirect_uri": redirect,
        "response_mode": "query",
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "consent",
    })
    print("Gate 1/3  opening the sign-in page...")
    print(f"          if no browser opens, visit:\n          {url}\n")
    webbrowser.open(url)

    if not _done.wait(timeout=300):
        server.server_close()
        print("FAIL  timed out after 5 minutes waiting for the sign-in callback")
        print("      If the browser showed a Microsoft error page, its message is")
        print("      the useful part -- match it against the table in the doc.")
        return 1
    server.server_close()

    if "code" not in _received:
        print("FAIL  sign-in did not return a code")
        print("     ", _explain(_received.get("error", ""),
                                _received.get("error_description", "")))
        return 1
    if _received.get("state") != state:
        print("FAIL  state mismatch -- discarding the response")
        return 1
    print("PASS  gate 1: sign-in and consent succeeded")

    tok = httpx.post(f"{base}/token", data={
        "grant_type": "authorization_code",
        "code": _received["code"],
        "redirect_uri": redirect,
        "client_id": args.client,
        "client_secret": args.secret,
        "code_verifier": verifier,
        "scope": SCOPE,
    }, timeout=30)
    payload = tok.json()
    if tok.status_code != 200:
        print(f"FAIL  token exchange returned {tok.status_code}")
        print("     ", _explain(payload.get("error", ""),
                                payload.get("error_description", "")))
        return 1
    print("PASS  gate 2: token exchange succeeded")
    print("      granted scopes:", payload.get("scope", "(none reported)"))

    if payload.get("refresh_token"):
        print("PASS  gate 3: refresh token present -- scheduled runs will survive")
    else:
        print("FAIL  gate 3: NO refresh token. Add offline_access (doc step 3).")
        print("      Everything works for about an hour, then scheduled runs break.")

    h = {"Authorization": f"Bearer {payload['access_token']}"}
    me = httpx.get(f"{GRAPH}/me", headers=h, timeout=30)
    if me.status_code != 200:
        print(f"FAIL  Graph /me returned {me.status_code}: {me.text[:200]}")
        return 1
    who = me.json()
    print(f"\n      signed in as {who.get('userPrincipalName')} ({who.get('displayName')})")

    kids = httpx.get(f"{GRAPH}/me/mailFolders/inbox/childFolders",
                     params={"$top": 100}, headers=h, timeout=30)
    if kids.status_code != 200:
        print(f"FAIL  listing Inbox subfolders returned {kids.status_code}: {kids.text[:200]}")
        return 1
    folders = {f.get("displayName"): f.get("id") for f in kids.json().get("value", [])}
    print(f"      Inbox subfolders: {', '.join(sorted(folders)) or '(none)'}")

    if args.folder in folders:
        print(f"\nPASS  folder {args.folder!r} found")
        print(f"      id: {folders[args.folder]}")
        print("\nAll gates clear. Safe to build the toolset.")
        return 0

    print(f"\nNOTE  folder {args.folder!r} does not exist yet -- create it in Outlook.")
    print("      Everything else passed; this is not a blocker.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
