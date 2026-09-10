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


async def login(
    user: str, pwd: str, *, headful: bool = False, timeout_s: int = 180
) -> str:
    """Complete the accounts.sap.com login and return a Cookie header.

    Every cookie on a *.sap.com domain is kept and serialized, rather than
    naming specific ones: which cookies carry the session is undocumented and
    has changed before, and sending all of them is what the upstream package
    does.
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

        # Best-effort only. The selectors are a guess at a form that SAP owns
        # and changes, so every failure here is silent by design: the wait
        # below hands over to the human, which is what --headful is for.
        await _try_fill(page, ["input[name='j_username']", "input[type='email']", "#j_username"], user)
        await _try_fill(page, ["input[name='j_password']", "input[type='password']", "#j_password"], pwd)
        await _try_click(page, ["button[type='submit']", "#logOnFormSubmit", "input[type='submit']"])

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

    kept = [c for c in cookies if "sap.com" in c.get("domain", "")]
    if not kept:
        raise SystemExit("Logged in but captured no sap.com cookies.")
    return "; ".join(f"{c['name']}={c['value']}" for c in kept)


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


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["capture"])
    parser.add_argument("note")
    parser.add_argument("--headful", action="store_true")
    # The default suits an unattended-ish run; a login needing MFA and a
    # human walking to the keyboard needs considerably longer.
    parser.add_argument("--timeout", type=int, default=180,
                        help="seconds to wait for the login to complete")
    parser.add_argument(
        "--out", default=str(ROOT / "tests" / "fixtures" / "sapnote_detail.json")
    )
    args = parser.parse_args()

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
