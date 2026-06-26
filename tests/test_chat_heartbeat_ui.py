"""Browser test: the live "working…" heartbeat fills blank gaps in the chat UI.

Boots the hermetic chat server in HEARTBEAT_DEMO mode (the delegate tool sleeps
with no specialist tool calls, so nothing else fills the gap), drives the real
React chat UI with Playwright/Chromium, and asserts that during the silent
stretch an animated reasoning block appears ("Thinking…" trigger + a "working…"
status that ticks), and that the final answer still follows once the gap ends.

This is the regression guard for the anti-"frozen screen" behavior.

Prereqs:  pip install playwright && playwright install chromium
Run:       python tests/test_chat_heartbeat_ui.py [--headed]
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
PORT = 7954
BASE = f"http://127.0.0.1:{PORT}"
HEADED = "--headed" in sys.argv
HB_SLEEP = "6.0"


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

    env = dict(os.environ, HEARTBEAT_DEMO="1", HB_SLEEP=HB_SLEEP)
    proc = subprocess.Popen([sys.executable, str(SERVER), str(PORT)], env=env)
    failures: list[str] = []
    try:
        _wait_for_server(BASE + "/")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not HEADED)
            page = browser.new_page()
            page.set_default_timeout(20_000)
            page.goto(BASE + "/", wait_until="domcontentloaded")

            _send_message(page, "please run the slow task")

            # During the blank gap, an animated "Thinking…" reasoning block with
            # a "working…" status must appear (heartbeat opens ~1.5s in).
            for needle in ("Thinking", "working"):
                try:
                    page.get_by_text(needle, exact=False).first.wait_for(
                        state="visible", timeout=15_000
                    )
                    print(f"  PASS  heartbeat visible: {needle!r}")
                except PWTimeout:
                    failures.append(f"heartbeat text not found: {needle!r}")
                    print(f"  FAIL  heartbeat text not found: {needle!r}")

            # A periodic tick proves it keeps updating (not a one-shot message).
            try:
                page.get_by_text("still working", exact=False).first.wait_for(
                    state="visible", timeout=15_000
                )
                print("  PASS  heartbeat tick visible")
            except PWTimeout:
                failures.append("heartbeat tick ('still working') not found")
                print("  FAIL  heartbeat tick not found")

            # The final answer must still arrive once the gap ends.
            try:
                page.get_by_text("answer is 42", exact=False).first.wait_for(
                    state="visible", timeout=20_000
                )
                print("  PASS  final answer visible")
            except PWTimeout:
                failures.append("final answer not found")
                print("  FAIL  final answer not found")

            if failures:
                SCRATCH.mkdir(parents=True, exist_ok=True)
                shot = SCRATCH / "chat_heartbeat_fail.png"
                page.screenshot(path=str(shot), full_page=True)
                dump = SCRATCH / "chat_heartbeat_body.txt"
                dump.write_text(page.inner_text("body"), encoding="utf-8")
                print(f"  saved screenshot -> {shot}")
                print(f"  saved body text  -> {dump}")

            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    if failures:
        print(f"\n=== FAILED: {len(failures)} issue(s) ===")
        for f in failures:
            print("  -", f)
        return 1
    print("\n=== PASSED: live heartbeat fills blank gaps in the chat UI ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
