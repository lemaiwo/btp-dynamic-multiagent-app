"""Hermetic chat server for the live-progress Playwright test.

Serves the *real* pydantic-ai chat UI and our custom ``/api/chat`` route
(``agents.chat_app.DynamicChatApp``), but backs it with deterministic
``FunctionModel`` agents instead of SAP AI Core + MCP. This lets a real
browser exercise the streaming/progress path with zero external dependencies.

Scripted behaviour for the prompt the test sends:

1. Orchestrator delegates to the ``demo`` specialist via ``delegate_demo``.
2. The specialist calls two tools (``step_one`` and ``step_two``); each call
   fires the progress handler, which streams a ``› demo: calling step_*`` line.
3. Orchestrator returns the final answer text.

``TestModel`` is used (not ``FunctionModel``) because the chat path streams
responses and ``TestModel`` supports streaming out of the box while still
calling every tool and emitting a deterministic final answer.

Run directly:  python tests/_progress_test_server.py [port]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Local, dependency-free environment (no CF/XSUAA/AICORE/Postgres).
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from pydantic_ai import Agent, RunContext  # noqa: E402
from pydantic_ai.models.test import TestModel  # noqa: E402

from agents.chat_app import CHAT_HTML, dynamic_chat_app  # noqa: E402
from agents.registry import BuildResult, _make_progress_handler, registry  # noqa: E402

FINAL_ANSWER = "Done. The demo specialist reported 42."


def build_app():
    # Specialist calls both of its tools, then returns deterministic text.
    specialist = Agent(TestModel(custom_output_text="specialist result: 42"))

    @specialist.tool_plain
    def step_one() -> str:
        return "step one complete"

    @specialist.tool_plain
    def step_two() -> str:
        return "step two complete"

    # Orchestrator calls its only tool (delegate_demo), then returns the answer.
    orchestrator = Agent(TestModel(custom_output_text=FINAL_ANSWER))

    # PROGRESS_DISABLED is a test-only switch used by the UI test's negative
    # control to prove the assertions fail when progress is NOT forwarded.
    forward_progress = os.environ.get("PROGRESS_DISABLED") != "1"

    @orchestrator.tool
    async def delegate_demo(ctx: RunContext, query: str) -> str:
        handler = _make_progress_handler("demo") if forward_progress else None
        result = await specialist.run(
            query,
            usage=ctx.usage,
            event_stream_handler=handler,
        )
        return str(result.output)

    # Install our scripted build and wire the chat app without touching AI Core
    # (refresh() would call get_model(); we set _app directly instead).
    registry._build = BuildResult(
        orchestrator=orchestrator,
        specialists={"demo": specialist},
        mcp_clients=[],
        configs=[],
    )
    dynamic_chat_app._extra_models = []
    html_source = CHAT_HTML if CHAT_HTML.is_file() else None
    dynamic_chat_app._app = orchestrator.to_web(html_source=html_source)
    return dynamic_chat_app


if __name__ == "__main__":
    import uvicorn

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7949
    uvicorn.run(build_app(), host="127.0.0.1", port=port, log_level="warning")
