"""Obtain a me.sap.com browser session, and fetch one note with it.

me.sap.com sits behind XSUAA/SAML and ignores HTTP Basic outright: the
`Detail` endpoint returns a byte-identical JS bootstrap page with Basic
credentials and anonymously. The only way in is to complete the
accounts.sap.com login in a real browser and reuse the resulting cookies,
which is what `mcp-sap-notes` does and why it ships Playwright.

This script exists so Chromium stays on an operator machine. The platform
only ever receives the cookie string this produces.

Run:
    python scripts/sap_session.py capture 3771065
    python scripts/sap_session.py capture 3771065 --headful   # MFA expected
    python scripts/sap_session.py refresh --base-url https://... --token ...

`capture` writes under `tmp/` by default (gitignored) -- a captured response
is a live SAP session's output and must never land in a tracked path
unreviewed. Promoting a capture to `tests/fixtures/sapnote_detail.json` is a
deliberate, manual step: scrub it first (drop `Actions`, any `token=`/`sid=`
query parameter, any `*.sap.corp` host, any email or S-user id) before it is
committed. See docs/SAP_NOTE_DETAIL.md for the operator-facing walkthrough.
"""

from __future__ import annotations

import argparse
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

DETAIL_URL = "https://me.sap.com/backend/raw/sapnotes/Detail"
# Any me.sap.com page forces the SAML round trip; the notes page is the one
# whose session we actually want.
LOGIN_TARGET = "https://me.sap.com/notes"
# The host the captured session is FOR. Cookies are scoped to it, because the
# jar also holds same-named cookies belonging to the identity provider.
TARGET_HOST = "me.sap.com"


async def login(
    user: str, pwd: str, *, headful: bool = False, timeout_s: int = 180
) -> str:
    """Complete the accounts.sap.com login and return a Cookie header.

    Cookies are scoped to :data:`TARGET_HOST` rather than to "anything on
    sap.com". Specific names are still not hard-coded -- which cookie carries
    the session is undocumented and has changed before -- but the host filter
    is not optional: see :func:`cookies_for_host` for the duplicate-name
    collision that silently produced an unauthenticated session.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headful)
        page = await (await browser.new_context()).new_page()
        # NOT wait_until="commit": me.sap.com bounces through XSUAA to
        # accounts.sap.com, and returning as soon as the first navigation
        # commits means every later query races a redirect and dies with
        # "Execution context was destroyed".
        await page.goto(LOGIN_TARGET, wait_until="domcontentloaded", timeout=60_000)
        await _settle(page)

        if headful:
            print(
                "A browser window is open. Sign in there if it does not fill "
                "itself in; this waits for you.",
                flush=True,
            )

        # accounts.sap.com is a TWO-STEP form and getting this wrong is why an
        # earlier version hung: page one carries only #j_username and a
        # Continue button, with no password field at all. Filling a password
        # there silently does nothing, and submitting the username alone then
        # parks on step two forever.
        SUBMIT = ["#logOnFormSubmit", "button[type='submit']", "input[type='submit']"]

        await _try_fill(page, ["#j_username", "input[name='j_username']"], user)
        await _try_click(page, SUBMIT)
        await _settle(page)

        # Step two: the password field appears next to the now-prefilled
        # username. Waited for rather than assumed, because a session SAP
        # still remembers can skip straight past this.
        if await _wait_visible(page, "#j_password", timeout_ms=20_000):
            await _try_fill(page, ["#j_password", "input[name='j_password']"], pwd)
            await _try_click(page, SUBMIT)
            await _settle(page)

        # Wait until we are back on me.sap.com, however long the human needs.
        try:
            await page.wait_for_url(lambda u: "me.sap.com" in u and "accounts.sap.com" not in u,
                                    timeout=timeout_s * 1000)
        except Exception:
            # Say WHERE it got stuck. "Did not reach me.sap.com" alone sends
            # the operator back for another blind ten-minute wait; the URL and
            # a screenshot say whether the form was never filled, whether MFA
            # is sitting there, or whether SAP changed the page entirely.
            where, title = page.url, ""
            try:
                title = await page.title()
            except Exception:  # noqa: BLE001 - diagnostics must not mask the real error
                pass
            shot = ROOT / "tmp" / "sap-login-stuck.png"
            try:
                shot.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(shot), full_page=True)
                shot_note = f"\n  screenshot: {shot}"
            except Exception:  # noqa: BLE001
                shot_note = ""
            await browser.close()
            raise SystemExit(
                f"Login did not complete within {timeout_s}s.\n"
                f"  stuck at: {where}\n"
                f"  page title: {title!r}{shot_note}\n"
                "Re-run with --headful and finish the login by hand while "
                "watching the window."
            ) from None

        cookies = await page.context.cookies()
        await browser.close()

    kept = cookies_for_host(cookies, TARGET_HOST)
    if not kept:
        raise SystemExit(f"Logged in but captured no cookies for {TARGET_HOST}.")
    return "; ".join(f"{c['name']}={c['value']}" for c in kept)


def cookies_for_host(cookies: list[dict], host: str) -> list[dict]:
    """The cookies a browser would actually send to ``host``.

    Filtering on "sap.com appears in the domain" is wrong and was the reason
    an authenticated session still got the anonymous bootstrap page: the login
    leaves TWO cookies named ``JSESSIONID`` in the jar, one for me.sap.com and
    one for accounts.sap.com. Serializing every sap.com cookie into one header
    sends the name twice, and me.sap.com reads the identity provider's session
    instead of its own.

    Standard cookie-domain matching: an exact host match, or a dot-prefixed
    domain that the host is a subdomain of.
    """
    out: list[dict] = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lower()
        if not domain:
            continue
        if domain.startswith("."):
            if host == domain[1:] or host.endswith(domain):
                out.append(cookie)
        elif host == domain:
            out.append(cookie)
    return out


async def _settle(page) -> None:
    """Let a redirect chain finish before touching the DOM.

    Never raises: a page that is still busy is a reason to fall back to the
    human, not to abandon the run.
    """
    for state in ("domcontentloaded", "networkidle"):
        try:
            await page.wait_for_load_state(state, timeout=15_000)
        except Exception:  # noqa: BLE001 - best effort by design
            return


async def _wait_visible(page, selector: str, *, timeout_ms: int = 15_000) -> bool:
    """True when the selector becomes visible. Never raises."""
    try:
        await page.locator(selector).first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:  # noqa: BLE001 - absence is an answer, not an error
        return False


async def _try_fill(page, selectors: list[str], value: str) -> bool:
    """Fill the first selector that resolves. Never raises.

    Uses a locator rather than `query_selector`: locators auto-wait and
    re-resolve, so a redirect landing mid-call retries instead of blowing up
    with "Execution context was destroyed".
    """
    for selector in selectors:
        try:
            await page.locator(selector).first.fill(value, timeout=5_000)
            return True
        except Exception:  # noqa: BLE001 - the human completes it instead
            continue
    return False


async def _try_click(page, selectors: list[str]) -> bool:
    """Click the first selector that resolves. Never raises."""
    for selector in selectors:
        try:
            await page.locator(selector).first.click(timeout=5_000)
            return True
        except Exception:  # noqa: BLE001 - the human completes it instead
            continue
    return False


async def fetch_detail_raw(cookie: str, note: str) -> tuple[int, str, bytes]:
    """One raw `Detail` request. Returns (status, content_type, body)."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(
            DETAIL_URL,
            params={"q": note, "t": "E"},
            headers={"Cookie": cookie, "Accept": "application/json"},
            follow_redirects=False,
        )
    return r.status_code, r.headers.get("content-type", ""), r.content


async def refresh(
    base_url: str,
    token: str,
    *,
    headful: bool = False,
    timeout_s: int = 180,
    ttl_hours: int = 12,
) -> None:
    """Log in and hand the resulting cookie to a running app.

    This is the operator's monthly step: the stored session is always stale
    by the time the scheduled run is due, so `refresh` then a run-now is the
    normal sequence, not a fallback. The cookie is written to the app over
    HTTPS via `POST /admin/api/sessions/builtin:sapnotedetail` and never
    printed or written to disk here.

    `ttl_hours` is NOT a measured SAP session lifetime -- it is the upstream
    project's cache TTL, carried over as a default. If the real session is
    shorter-lived than this, the credentials panel keeps reporting `valid`
    and preflight keeps passing for a window after SAP has already dropped
    the session, which is exactly the failure this parameter exists to let
    an operator correct.
    """
    user = os.environ.get("SAP_DIALOG_USER", "").strip()
    pwd = os.environ.get("SAP_DIALOG_PWD", "").strip()
    if not user or not pwd:
        raise SystemExit("SAP_DIALOG_USER / SAP_DIALOG_PWD are not set in .env")

    cookie = await login(user, pwd, headful=headful, timeout_s=timeout_s)
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{base_url.rstrip('/')}/admin/api/sessions/builtin:sapnotedetail",
            json={"cookie": cookie, "expires_in_hours": ttl_hours},
            headers={"Authorization": f"Bearer {token}"},
        )
    if r.status_code >= 400:
        raise SystemExit(f"app refused the session: {r.status_code} {r.text[:200]}")
    print(f"session stored, valid until {r.json().get('expires_at')}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["capture", "refresh"])
    # Only "capture" needs a note; argparse can't make positionals
    # conditional on another argument's value, so it stays optional here and
    # is validated by hand below.
    parser.add_argument("note", nargs="?")
    parser.add_argument("--headful", action="store_true")
    # The default suits an unattended-ish run; a login needing MFA and a
    # human walking to the keyboard needs considerably longer.
    parser.add_argument("--timeout", type=int, default=180,
                        help="seconds to wait for the login to complete")
    parser.add_argument(
        # A capture is a live SAP session's raw response. It defaults under
        # tmp/ (gitignored), never at the tracked fixture path -- promoting
        # one to the fixture is a deliberate, separate step that requires
        # scrubbing first (see the module docstring).
        "--out", default=str(ROOT / "tmp" / "sapnote_detail.json")
    )
    parser.add_argument(
        "--base-url", help="refresh only: base URL of the running app"
    )
    parser.add_argument(
        "--token",
        help="refresh only: admin bearer token; defaults to $ADMIN_TOKEN",
    )
    parser.add_argument(
        "--ttl-hours",
        type=int,
        default=12,
        help=(
            "refresh only: how long the app should treat the stored session "
            "as valid (1-48). This is NOT a measured SAP session lifetime -- "
            "it is the upstream project's cache TTL, kept as the default. "
            "Lower it if sessions are dying sooner than the credentials "
            "panel reports."
        ),
    )
    args = parser.parse_args()

    if args.command == "refresh":
        base_url = args.base_url
        if not base_url:
            sys.exit("refresh needs --base-url")
        token = args.token or os.environ.get("ADMIN_TOKEN", "").strip()
        if not token:
            sys.exit("refresh needs --token or ADMIN_TOKEN in .env")
        if not 1 <= args.ttl_hours <= 48:
            sys.exit(f"--ttl-hours must be between 1 and 48, got {args.ttl_hours}")
        await refresh(
            base_url,
            token,
            headful=args.headful,
            timeout_s=args.timeout,
            ttl_hours=args.ttl_hours,
        )
        return

    if not args.note:
        sys.exit("capture needs a note number")

    user = os.environ.get("SAP_DIALOG_USER", "").strip()
    pwd = os.environ.get("SAP_DIALOG_PWD", "").strip()
    if not user or not pwd:
        sys.exit("SAP_DIALOG_USER / SAP_DIALOG_PWD are not set in .env")

    cookie = await login(user, pwd, headful=args.headful, timeout_s=args.timeout)
    print(f"captured {cookie.count('=')} cookies")  # never print the value

    status, ctype, body = await fetch_detail_raw(cookie, args.note)
    print(f"status {status}  type {ctype}  bytes {len(body)}")
    if status != 200 or "json" not in ctype.lower():
        preview = "".join(chr(b) if 32 <= b < 127 else "." for b in body[:200])
        sys.exit(f"Not JSON -- the session did not take. Body head: {preview}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(body)
    print(f"wrote {out}")


if __name__ == "__main__":
    asyncio.run(main())
