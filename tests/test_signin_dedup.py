"""One sign-in prompt per server, however many specialists need it at once.

The orchestrator routinely fires several delegations in a single turn (pydantic-ai
runs tool calls concurrently). Each one runs the sign-in pre-check, so without
de-duplication every delegation posts its own "please sign in" bubble for the
*same* MCP server — the user sees N identical prompts, opens one popup, and the
other N-1 bubbles are dead links pointing at an already-satisfied flow.

Run:  python tests/test_signin_dedup.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./tests/_test_signin_dedup.db")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
# A developer .env may set this. Set it EMPTY rather than popping it: app.py
# calls load_dotenv(), which fills in vars that are absent but never overrides
# ones already present. Empty means "no allowlist", i.e. the default rule.
os.environ["MCP_URL_ALLOWLIST"] = ""

import agents.registry as registry  # noqa: E402
from agents.auth import current_base_url  # noqa: E402

FAILED = 0
PASSED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILED, PASSED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}   {detail}")


USER = "user-abc"
SERVER = "https://arc1.example.com/SIA/100/mcp"
OTHER_SERVER = "https://other.example.com/mcp"

messages: list[tuple[str, str]] = []
notes: list[tuple[str, str]] = []


def _fake_report_message(agent: str, text: str) -> None:
    messages.append((agent, text))


def _fake_report_note(agent: str, text: str) -> None:
    notes.append((agent, text))


class _FakeProgress:
    """Stand-in for agents.progress, patched into sys.modules."""

    report_message = staticmethod(_fake_report_message)
    report_note = staticmethod(_fake_report_note)


async def main() -> None:
    sys.modules["agents.progress"] = _FakeProgress  # type: ignore[assignment]
    current_base_url.set("https://approuter.example.com")

    # A sign-in that "completes" only after both delegations are already waiting,
    # so the concurrent window is real rather than an artefact of fast polling.
    token_ready = asyncio.Event()

    async def _fake_wait(user_id: str, server_key: str) -> bool:
        await token_ready.wait()
        return True

    registry._wait_for_token = _fake_wait  # type: ignore[assignment]

    # --- two concurrent delegations, same user + same server --------------
    print("\n== concurrent delegations for one server ==")
    messages.clear()
    notes.clear()
    task_a = asyncio.create_task(registry._await_signin("ABAP Agent", SERVER, USER))
    task_b = asyncio.create_task(registry._await_signin("ABAP Agent", SERVER, USER))
    await asyncio.sleep(0)  # let both reach the wait
    await asyncio.sleep(0)
    check("only one sign-in bubble posted", len(messages) == 1, f"got {len(messages)}")
    check(
        "the bubble carries the server-pinned link",
        bool(messages) and "server=" in messages[0][1],
    )
    check("both delegations report waiting", len(notes) >= 2, f"got {len(notes)}")

    token_ready.set()
    results = await asyncio.gather(task_a, task_b)
    check("both delegations resume", results == [True, True], str(results))
    check("still only one bubble after resume", len(messages) == 1, f"got {len(messages)}")

    # --- a later sign-in for the same server prompts again ----------------
    # The guard must not latch: once the flow finishes, a genuinely new
    # authorization need has to reach the user.
    print("\n== guard releases after the flow ends ==")
    messages.clear()
    token_ready.set()
    ok = await registry._await_signin("ABAP Agent", SERVER, USER)
    check("later sign-in prompts again", ok is True and len(messages) == 1, f"got {len(messages)}")

    # --- different servers each get their own prompt ----------------------
    print("\n== distinct servers prompt separately ==")
    messages.clear()
    token_ready.clear()
    t1 = asyncio.create_task(registry._await_signin("ABAP Agent", SERVER, USER))
    t2 = asyncio.create_task(registry._await_signin("ABAP Agent", OTHER_SERVER, USER))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    check("one bubble per distinct server", len(messages) == 2, f"got {len(messages)}")
    token_ready.set()
    await asyncio.gather(t1, t2)

    # --- a cancelled waiter must not wedge the guard ----------------------
    # If the chat client disconnects mid-wait, the next request has to be able
    # to prompt again rather than inherit a stuck marker.
    print("\n== cancellation releases the guard ==")
    messages.clear()
    token_ready.clear()
    stuck = asyncio.create_task(registry._await_signin("ABAP Agent", SERVER, USER))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    stuck.cancel()
    try:
        await stuck
    except asyncio.CancelledError:
        pass
    messages.clear()
    token_ready.set()
    await registry._await_signin("ABAP Agent", SERVER, USER)
    check("prompt works after a cancelled wait", len(messages) == 1, f"got {len(messages)}")

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
