"""A failing MCP tool must degrade one section, not kill the whole run.

Two defects motivated this:

1. `MCPServerStreamableHTTP` carries its own `max_retries`, defaulting to 1.
   `Agent(retries=...)` does not reach MCP toolset tools, so AGENT_TOOL_RETRIES
   never applied to the very tools it was introduced for (SAPQuery surfaces
   SQL errors as ModelRetry and needs a few attempts to self-correct).

2. When retries are exhausted pydantic-ai raises UnexpectedModelBehavior out of
   `Agent.run()`, so the model never writes a report at all. For a scheduled
   run that turns "one source was unreachable" into "no report", discarding the
   sources that did work.

Run:  python tests/test_tool_resilience.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./tests/_test_tool_resilience.db")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
# A developer .env may set this. Set it EMPTY rather than popping it: app.py
# calls load_dotenv(), which fills in vars that are absent but never overrides
# ones already present. Empty means "no allowlist", i.e. the default rule.
os.environ["MCP_URL_ALLOWLIST"] = ""

from pydantic_ai.exceptions import ModelRetry  # noqa: E402

from agents.shared import _resilient_tool_call  # noqa: E402

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


class _Ctx:
    """Stand-in for pydantic-ai's RunContext (only the retry counters matter)."""

    def __init__(self, retry: int, max_retries: int = 3):
        self.retry = retry
        self.max_retries = max_retries


async def main() -> None:
    print("\n== successful call passes through ==")

    async def ok(name, args, meta=None):
        return {"rows": 3}

    out = await _resilient_tool_call(_Ctx(0), ok, "sap_query", {"sql": "x"})
    check("result forwarded unchanged", out == {"rows": 3}, repr(out))

    print("\n== ModelRetry with attempts remaining re-raises ==")

    async def retryable(name, args, meta=None):
        raise ModelRetry("invalid column name")

    for attempt in (0, 1, 2):
        try:
            await _resilient_tool_call(_Ctx(attempt), retryable, "sap_query", {})
            check(f"retry {attempt} re-raises", False, "returned instead of raising")
        except ModelRetry:
            check(f"retry {attempt} re-raises", True)

    print("\n== ModelRetry with attempts exhausted degrades ==")
    out = await _resilient_tool_call(_Ctx(3), retryable, "sap_query", {})
    check("exhausted retry returns text", isinstance(out, str), repr(out))
    check("text names the tool", "sap_query" in out, out)
    check("text carries the cause", "invalid column name" in out, out)
    check(
        "text tells the model to carry on",
        "other" in out.lower() or "continue" in out.lower(),
        out,
    )

    print("\n== transport errors never escape ==")

    async def broken(name, args, meta=None):
        raise RuntimeError("connection reset")

    out = await _resilient_tool_call(_Ctx(0), broken, "sap_query", {})
    check("non-ModelRetry returns text", isinstance(out, str), repr(out))
    check("text carries the cause", "connection reset" in out, out)
    # A transport failure must not consume the model's retry budget by raising:
    # it is not something the model can fix by rewriting its arguments.
    check("transport error degrades immediately", "RuntimeError" in out, out)

    print("\n== cancellation still propagates ==")

    async def cancelled(name, args, meta=None):
        raise asyncio.CancelledError()

    try:
        await _resilient_tool_call(_Ctx(0), cancelled, "sap_query", {})
        check("CancelledError propagates", False, "was swallowed")
    except asyncio.CancelledError:
        check("CancelledError propagates", True)

    print("\n== create_mcp_server wires the retry budget ==")
    import agents.shared as shared

    captured: dict = {}

    class _FakeMCP:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    real = shared.MCPServerStreamableHTTP
    shared.MCPServerStreamableHTTP = _FakeMCP  # type: ignore[assignment]
    try:
        shared.create_mcp_server("docs", "https://docs.example.com/mcp", "none")
    finally:
        shared.MCPServerStreamableHTTP = real  # type: ignore[assignment]

    check("max_retries passed through", captured.get("max_retries", 1) > 1, str(captured.get("max_retries")))
    check("process_tool_call wired", captured.get("process_tool_call") is not None)

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
