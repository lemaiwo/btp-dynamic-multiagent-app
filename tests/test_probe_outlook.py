"""Tests for the Outlook probe's credential resolution.

The probe itself is a live sign-in and cannot be tested here. What can be
tested is everything that decides *which* credentials it uses -- the part that
exists so a client secret never has to be typed on a command line.

The important properties: precedence (flag > environment > .env), a missing
value producing a message that names it, and the secret never being echoed.

No network, no mailbox, no browser.

Run:  python tests/test_probe_outlook.py
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from probe_outlook import (  # noqa: E402
    ConfigError,
    DEFAULT_FOLDER,
    _fingerprint,
    _token_roles,
    read_env_file,
    resolve_config,
)

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILED, PASSED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}   {detail}")


def _args(**kw) -> argparse.Namespace:
    base = {"env_file": ".env.does-not-exist", "tenant": "", "client": "",
            "secret": "", "folder": "", "port": 7932}
    base.update(kw)
    return argparse.Namespace(**base)


def _write(text: str) -> str:
    fh = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False,
                                     encoding="utf-8")
    fh.write(text)
    fh.close()
    return fh.name


def main() -> None:
    print("\n-- .env parsing --")

    path = _write(
        "# a comment\n"
        "\n"
        "OUTLOOK_TENANT_ID=tenant-from-file\n"
        "  OUTLOOK_CLIENT_ID = client-from-file  \n"
        'OUTLOOK_CLIENT_SECRET="quoted-secret"\n'
        "export OUTLOOK_FOLDER='exported-folder'\n"
        "MALFORMED LINE WITHOUT EQUALS\n"
    )
    parsed = read_env_file(path)
    check("comments and blank lines are skipped", "# a comment" not in parsed)
    check("plain value is read", parsed.get("OUTLOOK_TENANT_ID") == "tenant-from-file")
    check("whitespace around key and value is stripped",
          parsed.get("OUTLOOK_CLIENT_ID") == "client-from-file",
          repr(parsed.get("OUTLOOK_CLIENT_ID")))
    check("double quotes are stripped",
          parsed.get("OUTLOOK_CLIENT_SECRET") == "quoted-secret",
          repr(parsed.get("OUTLOOK_CLIENT_SECRET")))
    check("export prefix and single quotes are handled",
          parsed.get("OUTLOOK_FOLDER") == "exported-folder",
          repr(parsed.get("OUTLOOK_FOLDER")))
    check("a malformed line is skipped rather than fatal", len(parsed) == 4, str(parsed))
    check("a missing file is empty, not an error", read_env_file("nope.env") == {})

    print("\n-- precedence --")

    cfg = resolve_config(_args(env_file=path), {})
    check("values come from .env when nothing else is set",
          cfg.tenant == "tenant-from-file" and cfg.secret == "quoted-secret")
    check("OUTLOOK_FOLDER in .env is honoured", cfg.folder == "exported-folder")

    cfg = resolve_config(_args(env_file=path), {"OUTLOOK_TENANT_ID": "tenant-from-env"})
    check("process environment beats .env", cfg.tenant == "tenant-from-env")
    check("unset environment does not blank a .env value",
          cfg.client == "client-from-file")

    cfg = resolve_config(_args(env_file=path, tenant="tenant-from-flag"),
                         {"OUTLOOK_TENANT_ID": "tenant-from-env"})
    check("an explicit flag beats both", cfg.tenant == "tenant-from-flag")

    # An empty environment variable is how a shell reports "unset but exported".
    # Treating it as a real value would silently blank a good .env entry.
    cfg = resolve_config(_args(env_file=path), {"OUTLOOK_TENANT_ID": ""})
    check("an empty environment variable does not override .env",
          cfg.tenant == "tenant-from-file")

    print("\n-- defaults and validation --")

    only_creds = _write("OUTLOOK_TENANT_ID=t\nOUTLOOK_CLIENT_ID=c\n"
                        "OUTLOOK_CLIENT_SECRET=s\n")
    cfg = resolve_config(_args(env_file=only_creds), {})
    check("folder defaults to 'agent'", cfg.folder == DEFAULT_FOLDER, cfg.folder)
    check("port comes through", cfg.port == 7932)

    try:
        resolve_config(_args(), {})
        check("missing values raise", False, "no exception")
    except ConfigError as exc:
        text = str(exc)
        check("missing values raise", True)
        check("the message names every missing variable",
              all(n in text for n in ("OUTLOOK_TENANT_ID", "OUTLOOK_CLIENT_ID",
                                      "OUTLOOK_CLIENT_SECRET")))
        check("the message offers the flag alternative", "--secret" in text)
        check("the message points at the setup doc", "OUTLOOK_SETUP.md" in text)

    partial = _write("OUTLOOK_TENANT_ID=t\nOUTLOOK_CLIENT_ID=c\n")
    try:
        resolve_config(_args(env_file=partial), {})
        check("a partially filled .env still raises", False, "no exception")
    except ConfigError as exc:
        text = str(exc)
        check("a partially filled .env still raises", True)
        check("only the missing variable is reported",
              "OUTLOOK_CLIENT_SECRET" in text and "OUTLOOK_TENANT_ID" not in text,
              text)

    # A value of only spaces is a paste accident, not a credential.
    blank = _write("OUTLOOK_TENANT_ID=t\nOUTLOOK_CLIENT_ID=c\n"
                   "OUTLOOK_CLIENT_SECRET=   \n")
    try:
        resolve_config(_args(env_file=blank), {})
        check("a whitespace-only secret is treated as missing", False, "accepted")
    except ConfigError:
        check("a whitespace-only secret is treated as missing", True)

    print("\n-- app-only token roles --")

    def _jwt(claims: dict) -> str:
        import base64 as b64
        import json as js
        body = b64.urlsafe_b64encode(js.dumps(claims).encode()).decode().rstrip("=")
        return f"header.{body}.signature"

    check("roles are read out of the token",
          _token_roles(_jwt({"roles": ["Mail.ReadWrite"]})) == ["Mail.ReadWrite"])
    check("several roles come back in full",
          sorted(_token_roles(_jwt({"roles": ["Mail.Read", "Mail.Send"]})))
          == ["Mail.Read", "Mail.Send"])
    # A delegated-only registration still gets a token; the absence of this
    # claim is the only signal that it holds no application permissions.
    check("a token with no roles claim yields an empty list",
          _token_roles(_jwt({"aud": "https://graph.microsoft.com"})) == [])
    check("a malformed token is empty rather than an exception",
          _token_roles("not-a-jwt") == [])
    check("an empty token is empty rather than an exception",
          _token_roles("") == [])

    print("\n-- the secret is never echoed --")

    secret = "GOCSPX-not-a-real-secret-value"
    tag = _fingerprint(secret)
    check("fingerprint is short", len(tag) == 8, tag)
    check("fingerprint leaks no part of the secret", secret[:6] not in tag)
    check("fingerprint is stable across calls", tag == _fingerprint(secret))
    check("a different secret fingerprints differently",
          tag != _fingerprint(secret + "x"))

    for name in (path, only_creds, partial, blank):
        Path(name).unlink(missing_ok=True)

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
