"""Dynamic ASGI wrapper around the pydantic-ai chat web app.

On each reload of the agent registry, the underlying `Agent.to_web()` app
is rebuilt and swapped in. Incoming requests are dispatched to the current
app, so administrators can add/edit/remove agents and press 'Reload' to
see the changes take effect without restarting the process.

`POST /api/chat` is intercepted and served by a custom handler
(:meth:`DynamicChatApp._handle_chat`) so we can interleave *live progress*
from delegated specialists into the response stream. `to_web()`'s own
`/api/chat` runs the orchestrator as an opaque black box and exposes no hook
for that, so we rebuild just that one route on top of Pydantic AI's own
`VercelAIAdapter` (same Vercel AI protocol, same React chat UI — only the
backend route changes). Every other route (`/`, `/api/configure`,
`/api/health`, CORS preflight, branding HTML) still falls through to the
stock `to_web()` app.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from uuid import uuid4

from agents.progress import ProgressUpdate, current_progress
from agents.registry import registry
from agents.shared import available_models, get_model

logger = logging.getLogger(__name__)

CHAT_HTML = Path(__file__).resolve().parent.parent / "templates" / "chat.html"

# Prefix shown in front of each streamed progress line in the chat. Kept compact
# so a specialist's working steps read as quiet status notes, not as answers.
_PROGRESS_PREFIX = "› "


class DynamicChatApp:
    """ASGI app that forwards to the current orchestrator's web app."""

    def __init__(self) -> None:
        self._app = None
        # Models offered in the UI dropdown besides the orchestrator's active
        # model, kept in sync by refresh() so the custom /api/chat route can
        # honour the user's model choice exactly like to_web() does.
        self._extra_models: list = []

    def refresh(self) -> None:
        """Rebuild the inner web app from the current orchestrator."""
        html_source = CHAT_HTML if CHAT_HTML.is_file() else None
        models = []
        active_id = id(registry.orchestrator.model)
        for name in available_models():
            try:
                m = get_model(name)
            except Exception:
                logger.warning(
                    "Skipping model %r in chat dropdown: failed to construct", name,
                    exc_info=True,
                )
                continue
            if id(m) == active_id:
                # to_web always includes agent.model; don't duplicate it.
                continue
            models.append(m)
        self._extra_models = models
        self._app = registry.orchestrator.to_web(
            html_source=html_source, models=models or None
        )

    async def __call__(self, scope, receive, send):
        if self._app is None:
            self.refresh()
        assert self._app is not None
        if (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and scope.get("path", "").rstrip("/") == "/api/chat"
        ):
            await self._handle_chat(scope, receive, send)
            return
        await self._app(scope, receive, send)

    # -- custom /api/chat with live specialist progress --------------------

    def _model_mapping(self) -> dict:
        """Map model id -> Model for the active model plus dropdown options."""
        active = registry.orchestrator.model
        mapping = {active.model_id: active}
        for m in self._extra_models:
            mapping[m.model_id] = m
        return mapping

    def _select_model(self, adapter):
        """Resolve the model the user picked in the UI.

        Returns ``(model_ref_or_None, error_or_None)``. ``None`` model means
        "use the orchestrator's default". Mirrors to_web()'s validation so an
        unknown id is rejected rather than silently ignored.
        """
        run_input = getattr(adapter, "run_input", None)
        extra = getattr(run_input, "__pydantic_extra__", None) or {}
        model_id = extra.get("model")
        if not model_id:
            return None, None
        mapping = self._model_mapping()
        if model_id not in mapping:
            return None, f'Model "{model_id}" is not available'
        return mapping[model_id], None

    async def _handle_chat(self, scope, receive, send) -> None:
        # Imported lazily so importing this module doesn't hard-require the
        # web extra at import time (matches to_web()'s own lazy nature).
        from starlette.requests import Request

        from pydantic_ai.ui.vercel_ai import VercelAIAdapter
        from pydantic_ai.ui.vercel_ai.response_types import (
            TextDeltaChunk,
            TextEndChunk,
            TextStartChunk,
        )

        request = Request(scope, receive)
        try:
            adapter = await VercelAIAdapter.from_request(
                request, agent=registry.orchestrator
            )
        except Exception:
            logger.exception("Failed to parse chat request")
            await _send_json(send, 400, {"error": "Invalid chat request"})
            return

        model_ref, error = self._select_model(adapter)
        if error is not None:
            await _send_json(send, 400, {"error": error})
            return

        out_queue: asyncio.Queue = asyncio.Queue()
        sentinel = object()

        def sink(update: ProgressUpdate) -> None:
            # Render each progress report as a self-contained text part with its
            # own id. The orchestrator's narration text part has already closed
            # by the time a specialist tool runs, so these never overlap an open
            # part — each shows up as a quiet status line in the message.
            cid = uuid4().hex
            line = f"{_PROGRESS_PREFIX}{update.agent}: {update.text}\n"
            out_queue.put_nowait(TextStartChunk(id=cid))
            out_queue.put_nowait(TextDeltaChunk(id=cid, delta=line))
            out_queue.put_nowait(TextEndChunk(id=cid))

        async def pump() -> None:
            # Drive the orchestrator and feed its Vercel chunks into the queue.
            # Specialist progress (via the sink above) interleaves with these in
            # real arrival order, since both share the single out_queue.
            try:
                async for chunk in adapter.run_stream(model=model_ref):
                    out_queue.put_nowait(chunk)
            except Exception:
                logger.exception("Chat stream failed")
            finally:
                out_queue.put_nowait(sentinel)

        async def merged():
            while True:
                item = await out_queue.get()
                if item is sentinel:
                    break
                yield item

        # Install the progress sink *before* starting the orchestrator task so
        # the contextvar is copied into it (and into the nested specialist runs
        # it spawns), exactly like JWTBindingMiddleware does for current_jwt.
        token = current_progress.set(sink)
        pump_task = asyncio.create_task(pump())
        try:
            response = adapter.streaming_response(merged())
            await response(scope, receive, send)
        finally:
            current_progress.reset(token)
            if not pump_task.done():
                pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump_task


async def _send_json(send, status: int, body: dict) -> None:
    import json

    payload = json.dumps(body).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("latin-1")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


dynamic_chat_app = DynamicChatApp()
