"""Browser test: OAuth sign-in auto-continues the chat (no second message).

Boots the hermetic chat server in OAUTH_DEMO mode (the real delegation tool
around a specialist that needs sign-in until a token exists), drives the real
React chat UI with Playwright, and asserts the full auto-continue flow:

1. Sending a message surfaces a sign-in link (the specialist needs OAuth).
2. The live "Waiting for you to sign in…" heartbeat appears.
3. Simulating the popup callback (GET /test/authorize stores a token) makes the
   chat resume *by itself* — the specialist's tool card and the final answer
   appear with NO further user message.

This is the regression guard for "it should just continue by itself".

Prereqs:  pip install playwright && playwright install chromium
Run:       python tests/test_chat_oauth_autocontinue_ui.py [--headed]
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "tests" / "_progress_test_server.py"
SCRATCH = Path(tempfile.gettempdir())
PORT = 7955
BASE = f"http://127.0.0.1:{PORT}"
HEADED = "--headed" in sys.argv


def _wait_for_server(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.3)
    raise RuntimeError(f"server at {url} never came up: {last}")


def _send_message(page, text: str) -> None:
    box = page.get_by_role("textbox").first
    box.wait_for(state="visible", timeout=20_000)
    box.click()
    box.fill(text)
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    try:
        if (box.input_value() or "").strip() == text:
            for name in ("Send", "Submit", "send"):
                btn = page.get_by_role("button", name=name)
                if btn.count() > 0:
                    btn.first.click()
                    break
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    db_path = (SCRATCH / "oauth_autocontinue_test.db")
    if db_path.exists():
        db_path.unlink()
    db_url = "sqlite+aiosqlite:///" + str(db_path).replace("\\", "/")
    env = dict(os.environ, OAUTH_DEMO="1", DATABASE_URL=db_url)
    # Keep the auth wait snappy and polling tight for the test.
    env.update(MCP_AUTH_WAIT_SECONDS="60", MCP_AUTH_POLL_SECONDS="0.5")
    proc = subprocess.Popen([sys.executable, str(SERVER), str(PORT)], env=env)
    failures: list[str] = []
    try:
        _wait_for_server(BASE + "/")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not HEADED)
            page = browser.new_page()
            page.set_default_timeout(20_000)
            page.goto(BASE + "/", wait_until="domcontentloaded")

            _send_message(page, "what does ARC-1 report?")

            # 1. A sign-in link/message must appear.
            try:
                page.get_by_text("Sign in to arc1", exact=False).first.wait_for(
                    state="visible", timeout=20_000
                )
                print("  PASS  sign-in link shown")
            except PWTimeout:
                failures.append("sign-in link not shown")
                print("  FAIL  sign-in link not shown")

            # 2. The "waiting for sign-in" heartbeat must appear.
            try:
                page.get_by_text("Waiting for you to sign in", exact=False).first.wait_for(
                    state="visible", timeout=15_000
                )
                print("  PASS  'waiting for sign-in' heartbeat shown")
            except PWTimeout:
                failures.append("'waiting for sign-in' heartbeat not shown")
                print("  FAIL  'waiting for sign-in' heartbeat not shown")

            # 3. Simulate the popup callback completing (token stored).
            with urllib.request.urlopen(BASE + "/test/authorize", timeout=5) as r:
                assert r.status == 200
            print("  ...  simulated sign-in (token stored)")

            # 4. The chat must resume on its own: specialist card + final answer,
            #    with NO further user message.
            try:
                page.get_by_text("query_arc1", exact=False).first.wait_for(
                    state="visible", timeout=20_000
                )
                print("  PASS  specialist resumed (tool card appeared)")
            except PWTimeout:
                failures.append("specialist did not resume (no tool card)")
                print("  FAIL  specialist did not resume (no tool card)")

            try:
                page.get_by_text("42 open incidents", exact=False).first.wait_for(
                    state="visible", timeout=20_000
                )
                print("  PASS  final answer auto-continued")
            except PWTimeout:
                failures.append("final answer did not auto-continue")
                print("  FAIL  final answer did not auto-continue")

            # The user only ever sent ONE message. The text appears twice in the
            # page: once as the chat bubble and once as the sidebar conversation
            # title. A client-side resend would add a second bubble (>= 3).
            body = page.inner_text("body")
            n = body.count("what does ARC-1 report?")
            if n >= 3:
                failures.append(f"user message resent (appears {n}x)")
                print(f"  FAIL  message resent (appears {n}x)")
            else:
                print(f"  PASS  no manual resend (message appears {n}x: bubble + sidebar)")

            if failures:
                SCRATCH.mkdir(parents=True, exist_ok=True)
                shot = SCRATCH / "chat_oauth_fail.png"
                page.screenshot(path=str(shot), full_page=True)
                (SCRATCH / "chat_oauth_body.txt").write_text(body, encoding="utf-8")
                print(f"  saved screenshot -> {shot}")

            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            if db_path.exists():
                db_path.unlink()
        except Exception:  # noqa: BLE001
            pass

    if failures:
        print(f"\n=== FAILED: {len(failures)} issue(s) ===")
        for f in failures:
            print("  -", f)
        return 1
    print("\n=== PASSED: OAuth sign-in auto-continues the chat ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
