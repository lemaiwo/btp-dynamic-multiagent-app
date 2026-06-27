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
import os
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from uuid import uuid4

from agents.progress import ProgressUpdate, current_progress
from agents.registry import registry
from agents.shared import available_models, get_model

logger = logging.getLogger(__name__)

CHAT_HTML = Path(__file__).resolve().parent.parent / "templates" / "chat.html"

# The orchestrator's only tools are the per-specialist delegation tools, named
# `delegate_<agent>` (see registry._sanitize_tool_name). Their raw tool cards are
# noisy internals; we drop them and render the specialist's own steps instead.
_DELEGATE_PREFIX = "delegate_"

# Live "working…" heartbeat tuning. After QUIET seconds of silence an animated
# reasoning block appears; while the gap lasts it ticks every TICK seconds so the
# screen never looks frozen during model calls / MCP connects / synthesis.
_HB_QUIET_S = float(os.environ.get("CHAT_HEARTBEAT_QUIET_SECONDS", "1.5"))
_HB_TICK_S = float(os.environ.get("CHAT_HEARTBEAT_TICK_SECONDS", "2.5"))
_HB_POLL_S = 0.5


@dataclass
class _ChatState:
    """Per-request streaming state, shared between the orchestrator pump and the
    heartbeat task. Both run on the one event loop and only ever mutate this in
    synchronous bursts (no ``await`` mid-burst), so they never interleave and no
    lock is needed."""

    start: float
    last_activity: float = 0.0
    started: bool = False  # the adapter's opening chunk has been emitted
    finished: bool = False
    text_open: bool = False  # a model text part is currently streaming
    note: str | None = None  # current phase label for the heartbeat
    active_delegations: int = 0  # specialists still running this turn
    hb_open: bool = False  # a heartbeat reasoning part is currently streaming
    hb_id: str = ""
    last_hb: float = 0.0
    suppressed: set = field(default_factory=set)  # tool_call_ids to drop
    open_tools: set = field(default_factory=set)  # specialist cards still "Running"
    card_ids: dict = field(default_factory=dict)  # raw tool_call_id -> unique card id

    def __post_init__(self) -> None:
        self.last_activity = self.start


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
            ReasoningDeltaChunk,
            ReasoningEndChunk,
            ReasoningStartChunk,
            TextDeltaChunk,
            TextEndChunk,
            TextStartChunk,
            ToolInputAvailableChunk,
            ToolInputDeltaChunk,
            ToolInputErrorChunk,
            ToolInputStartChunk,
            ToolOutputAvailableChunk,
            ToolOutputDeniedChunk,
            ToolOutputErrorChunk,
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
        st = _ChatState(start=monotonic())

        def put(chunk) -> None:
            out_queue.put_nowait(chunk)

        def close_heartbeat() -> None:
            """Finalize any open heartbeat so it collapses to 'Thought for Ns'."""
            if st.hb_open:
                put(ReasoningEndChunk(id=st.hb_id))
                st.hb_open = False

        def emit_real(chunk) -> None:
            """Emit genuine content: close the heartbeat first so it renders
            before this chunk, then reset the silence timer."""
            close_heartbeat()
            put(chunk)
            st.started = True
            st.last_activity = monotonic()
            # Track open model text parts so the heartbeat never injects a
            # "Thinking…" block into the middle of a streaming answer.
            if isinstance(chunk, TextStartChunk):
                st.text_open = True
            elif isinstance(chunk, TextEndChunk):
                st.text_open = False

        def sink(update: ProgressUpdate) -> None:
            # A specialist reported progress. Tool calls become native tool cards
            # (a pulsing "Running" badge that flips to "Completed"/"Error");
            # notes just update the heartbeat's phase label.
            if update.kind == "note":
                if update.text:
                    st.note = update.text
                return
            if update.kind == "message":
                # A persistent assistant bubble (e.g. a sign-in link) emitted
                # mid-run, distinct from the transient heartbeat label.
                if update.text:
                    mid = uuid4().hex
                    emit_real(TextStartChunk(id=mid))
                    emit_real(TextDeltaChunk(id=mid, delta=update.text))
                    emit_real(TextEndChunk(id=mid))
                return
            if update.kind == "delegation_start":
                st.active_delegations += 1
                if update.text:
                    st.note = update.text
                return
            if update.kind == "delegation_end":
                st.active_delegations = max(0, st.active_delegations - 1)
                # Only call it "composing" once every specialist this turn is
                # done — otherwise a fast one finishing mislabels a turn where
                # another is still working.
                if st.active_delegations == 0:
                    st.note = "Composing the answer…"
                return
            raw = update.tool_call_id or uuid4().hex
            if update.kind == "tool_start":
                # Use a fresh, globally-unique card id so cards never collide if
                # two (sequential) specialists happen to reuse a raw tool_call_id.
                cid = f"spec:{uuid4().hex}"
                st.card_ids[raw] = cid
                name = update.tool_name or "tool"
                st.open_tools.add(cid)
                emit_real(ToolInputStartChunk(tool_call_id=cid, tool_name=name))
                emit_real(
                    ToolInputAvailableChunk(
                        tool_call_id=cid,
                        tool_name=name,
                        input=update.args if update.args is not None else {},
                    )
                )
            elif update.kind == "tool_end":
                cid = st.card_ids.pop(raw, None) or f"spec:{raw}"
                st.open_tools.discard(cid)
                if update.ok:
                    emit_real(
                        ToolOutputAvailableChunk(
                            tool_call_id=cid, output=update.output or ""
                        )
                    )
                else:
                    emit_real(
                        ToolOutputErrorChunk(
                            tool_call_id=cid, error_text=update.output or "error"
                        )
                    )

        def should_suppress(chunk) -> bool:
            # Drop the orchestrator's own `delegate_*` tool cards (noisy internals);
            # the specialist's own steps are rendered by the sink instead.
            try:
                if isinstance(
                    chunk,
                    (ToolInputStartChunk, ToolInputAvailableChunk, ToolInputErrorChunk),
                ):
                    if (chunk.tool_name or "").startswith(_DELEGATE_PREFIX):
                        st.suppressed.add(chunk.tool_call_id)
                        return True
                    return False
                if isinstance(chunk, ToolInputDeltaChunk):
                    return chunk.tool_call_id in st.suppressed
                if isinstance(
                    chunk,
                    (ToolOutputAvailableChunk, ToolOutputErrorChunk, ToolOutputDeniedChunk),
                ):
                    return chunk.tool_call_id in st.suppressed
            except Exception:  # noqa: BLE001 — never break the stream on a check
                logger.debug("delegate-card suppression check failed", exc_info=True)
            return False

        async def pump() -> None:
            # Drive the orchestrator and feed its Vercel chunks into the queue.
            # Specialist progress (via the sink) interleaves with these in real
            # arrival order, since both share the single out_queue.
            try:
                async for chunk in adapter.run_stream(model=model_ref):
                    if should_suppress(chunk):
                        continue
                    emit_real(chunk)
            except Exception:
                logger.exception("Chat stream failed")
            finally:
                st.finished = True
                close_heartbeat()
                put(sentinel)

        async def heartbeat() -> None:
            # Keep an animated "Thinking…" reasoning block alive during any quiet
            # stretch (orchestrator/specialist model calls, MCP connects,
            # synthesis) so it's always clear the agent is still working.
            try:
                while not st.finished:
                    await asyncio.sleep(_HB_POLL_S)
                    # Wait for the stream's opening chunk before injecting parts.
                    if st.finished or not st.started:
                        continue
                    # A specialist tool card is already pulsing "Running", or the
                    # model is actively streaming text; no need for a second
                    # "working…" indicator on top of either.
                    if st.open_tools or st.text_open:
                        continue
                    now = monotonic()
                    if now - st.last_activity < _HB_QUIET_S:
                        continue
                    if not st.hb_open:
                        st.hb_id = uuid4().hex
                        st.hb_open = True
                        st.last_hb = now
                        put(ReasoningStartChunk(id=st.hb_id))
                        put(ReasoningDeltaChunk(id=st.hb_id, delta=st.note or "Working…"))
                    elif now - st.last_hb >= _HB_TICK_S:
                        st.last_hb = now
                        elapsed = int(now - st.start)
                        put(
                            ReasoningDeltaChunk(
                                id=st.hb_id, delta=f"  \n…still working ({elapsed}s)"
                            )
                        )
            except asyncio.CancelledError:
                pass

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
        hb_task = asyncio.create_task(heartbeat())
        try:
            response = adapter.streaming_response(merged())
            # Stream live through proxies. The SAP approuter gzip-compresses
            # text/event-stream (compressible() matches `text/*`), which BUFFERS
            # the whole response — so progress (cards, heartbeat, the sign-in
            # link) only appears once the run finishes. The `compression`
            # middleware skips bodies marked `no-transform`; `X-Accel-Buffering`
            # disables any nginx-style buffering too.
            response.headers["Cache-Control"] = "no-cache, no-transform"
            response.headers["X-Accel-Buffering"] = "no"
            await response(scope, receive, send)
        finally:
            current_progress.reset(token)
            for task in (hb_task, pump_task):
                if not task.done():
                    task.cancel()
            for task in (hb_task, pump_task):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task


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
