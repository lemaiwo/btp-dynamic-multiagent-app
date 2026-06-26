"""Browser test: live specialist progress shows up in the chat UI as tool cards.

Boots the hermetic chat server (``tests/_progress_test_server.py`` — the real
pydantic-ai chat UI + our custom ``/api/chat``, backed by TestModel agents),
drives the actual React chat UI with Playwright/Chromium, sends a message, and
asserts that each tool the delegated specialist calls (``step_one``/``step_two``)
renders as a native tool card that reaches the "Completed" state, that the
orchestrator's noisy ``delegate_*`` card is suppressed, and that the final answer
follows.

Prereqs:  pip install playwright && playwright install chromium
Run:       python tests/test_chat_progress_ui.py [--headed]
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "tests" / "_progress_test_server.py"
SCRATCH = Path(tempfile.gettempdir())
PORT = 7953
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
    """Type into the chat composer and submit, trying a few UI affordances."""
    box = page.get_by_role("textbox").first
    box.wait_for(state="visible", timeout=20_000)
    box.click()
    box.fill(text)
    # Most chat UIs submit on Enter; fall back to a Send button if the text
    # is still sitting in the box afterwards.
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    try:
        if (box.input_value() or "").strip() == text:
            for name in ("Send", "Submit", "send"):
                btn = page.get_by_role("button", name=name)
                if btn.count() > 0:
                    btn.first.click()
                    break
    except Exception:  # noqa: BLE001 — input_value not meaningful for all widgets
        pass


def main() -> int:
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    proc = subprocess.Popen([sys.executable, str(SERVER), str(PORT)])
    failures: list[str] = []
    try:
        _wait_for_server(BASE + "/")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not HEADED)
            page = browser.new_page()
            page.set_default_timeout(20_000)
            page.goto(BASE + "/", wait_until="domcontentloaded")

            _send_message(page, "please run the demo task")

            # Each specialist tool must render as a card naming the tool...
            for needle in ("step_one", "step_two"):
                try:
                    page.get_by_text(needle, exact=False).first.wait_for(
                        state="visible", timeout=20_000
                    )
                    print(f"  PASS  tool card visible: {needle!r}")
                except PWTimeout:
                    failures.append(f"tool card not found: {needle!r}")
                    print(f"  FAIL  tool card not found: {needle!r}")

            # ...and each card must reach the 'Completed' state.
            try:
                page.get_by_text("Completed", exact=False).first.wait_for(
                    state="visible", timeout=20_000
                )
                print("  PASS  'Completed' tool state visible")
            except PWTimeout:
                failures.append("'Completed' tool state not found")
                print("  FAIL  'Completed' tool state not found")

            # ...and the orchestrator's final answer must follow.
            try:
                page.get_by_text("reported 42", exact=False).first.wait_for(
                    state="visible", timeout=20_000
                )
                print("  PASS  final answer visible")
            except PWTimeout:
                failures.append("final answer not found")
                print("  FAIL  final answer not found")

            body = page.inner_text("body")

            # The orchestrator's internal delegate_* card must be suppressed.
            if "delegate_demo" in body:
                failures.append("delegate_demo card leaked into the UI")
                print("  FAIL  delegate_demo card was not suppressed")
            else:
                print("  PASS  delegate_* card suppressed")

            # Ordering: progress should come before the final answer in the DOM.
            i_prog = body.find("step_one")
            i_final = body.find("reported 42")
            if i_prog != -1 and i_final != -1 and i_prog < i_final:
                print("  PASS  progress precedes final answer")
            else:
                failures.append(
                    f"ordering wrong (progress@{i_prog}, final@{i_final})"
                )
                print(f"  FAIL  ordering (progress@{i_prog}, final@{i_final})")

            if failures:
                SCRATCH.mkdir(parents=True, exist_ok=True)
                shot = SCRATCH / "chat_progress_fail.png"
                page.screenshot(path=str(shot), full_page=True)
                dump = SCRATCH / "chat_progress_body.txt"
                dump.write_text(body, encoding="utf-8")
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
    print("\n=== PASSED: live progress renders in the chat UI ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
