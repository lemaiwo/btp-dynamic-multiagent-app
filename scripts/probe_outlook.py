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

Credentials come from ``.env`` so that a client secret never has to be typed on
a command line (where it lands in shell history, in ``ps`` output, and in the
transcript of any agent asked to run the probe):

    OUTLOOK_TENANT_ID=...
    OUTLOOK_CLIENT_ID=...
    OUTLOOK_CLIENT_SECRET=...
    OUTLOOK_FOLDER=agent        # optional, defaults to "agent"

Usage:
    python scripts/probe_outlook.py                     # reads the repo's .env
    python scripts/probe_outlook.py --env-file .env.outlook
    python scripts/probe_outlook.py --tenant <ID> --client <ID> --secret <VALUE>

Explicit flags override the environment, which overrides ``.env``.

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

from dataclasses import dataclass
from pathlib import Path

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


# Relative to the repo root rather than the cwd: the probe is run from
# wherever the user happens to be standing, and a silently-empty .env
# would surface as "missing 3 of 3 values" with no hint that the file
# it looked at was not the one they edited.
DEFAULT_ENV_FILE = str(Path(__file__).resolve().parent.parent / ".env")
DEFAULT_FOLDER = "agent"

# env var -> the flag that overrides it, for the "what is missing" message.
_REQUIRED = {
    "OUTLOOK_TENANT_ID": "--tenant",
    "OUTLOOK_CLIENT_ID": "--client",
    "OUTLOOK_CLIENT_SECRET": "--secret",
}


@dataclass(frozen=True)
class Config:
    tenant: str
    client: str
    secret: str
    folder: str
    port: int


class ConfigError(Exception):
    """Raised with a message that says exactly which values are missing."""


def read_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Parse a ``.env`` file into a dict, ignoring blanks and comments.

    Hand-rolled rather than python-dotenv: the probe's whole point is that it
    runs before anything else is set up, so it must not need this project's
    dependencies installed. The format is the ``KEY=value`` subset that actually
    appears in a ``.env`` -- optional ``export`` prefix, optional surrounding
    quotes. A malformed line is skipped, not fatal; the missing-value message
    downstream is a better error than a parse error pointing at line 14.
    """
    values: dict[str, str] = {}
    file = Path(path)
    if not file.is_file():
        return values
    for line in file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        if key:
            values[key] = raw
    return values


def resolve_config(args: argparse.Namespace, environ: dict[str, str]) -> Config:
    """Merge flags, real environment and ``.env`` into one config.

    Precedence is flag > process environment > ``.env`` file, so an exported
    variable can override a stale ``.env`` without editing it, and a flag can
    override both for a one-off run against a second tenant.
    """
    from_file = read_env_file(args.env_file)
    merged = {**from_file, **{k: v for k, v in environ.items() if v}}

    picked = {
        "OUTLOOK_TENANT_ID": args.tenant or merged.get("OUTLOOK_TENANT_ID", ""),
        "OUTLOOK_CLIENT_ID": args.client or merged.get("OUTLOOK_CLIENT_ID", ""),
        "OUTLOOK_CLIENT_SECRET": args.secret or merged.get("OUTLOOK_CLIENT_SECRET", ""),
    }
    missing = [name for name, value in picked.items() if not value.strip()]
    if missing:
        lines = [
            f"Missing {len(missing)} of 3 required values.",
            "",
            f"Add these to {Path(args.env_file)} (it is gitignored):",
            "",
        ]
        lines += [f"    {name}=..." for name in missing]
        lines += [
            "",
            "Or pass them as flags: "
            + " ".join(_REQUIRED[name] + " <value>" for name in missing),
            "",
            "See docs/OUTLOOK_SETUP.md steps 1-2 for where each value comes from.",
        ]
        raise ConfigError("\n".join(lines))

    return Config(
        tenant=picked["OUTLOOK_TENANT_ID"].strip(),
        client=picked["OUTLOOK_CLIENT_ID"].strip(),
        secret=picked["OUTLOOK_CLIENT_SECRET"].strip(),
        folder=(args.folder or merged.get("OUTLOOK_FOLDER") or DEFAULT_FOLDER).strip(),
        port=args.port,
    )


def _fingerprint(secret: str) -> str:
    """A stable, non-reversible tag for the secret.

    Enough to tell "the value I pasted" from "a different value" across runs
    without printing any of it -- the probe is meant to be runnable by an agent
    whose output the user reads, so nothing here may echo the credential.
    """
    return hashlib.sha256(secret.encode()).hexdigest()[:8]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Go/no-go probe for the Outlook (Graph) integration. "
                    "Reads credentials from .env by default.",
    )
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE,
                    help=f"file holding OUTLOOK_* values (default: {DEFAULT_ENV_FILE})")
    ap.add_argument("--tenant", default="", help="Directory (tenant) ID; overrides .env")
    ap.add_argument("--client", default="", help="Application (client) ID; overrides .env")
    ap.add_argument("--secret", default="",
                    help="Client secret VALUE, not its id; prefer .env over this flag")
    ap.add_argument("--folder", default="", help=f"Inbox subfolder (default: {DEFAULT_FOLDER})")
    ap.add_argument("--port", type=int, default=7932)
    raw_args = ap.parse_args()

    try:
        args = resolve_config(raw_args, dict(os.environ))
    except ConfigError as exc:
        print(exc)
        return 2

    print(f"          tenant {args.tenant}")
    print(f"          client {args.client}")
    print(f"          secret sha256:{_fingerprint(args.secret)} (never printed)")
    print(f"          folder {args.folder!r}\n")

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
