"""One web chat turn: the orchestrator run behind ``POST .../messages``.

The route (``agents/webchat/routes.py``) takes the conversation's run lock,
reads the form and stores the user message and a ``running`` assistant
message; then :func:`start_turn` starts the run in its own task and the
route streams the turn's events as Server-Sent Events.

The run task is created inside the request, so it inherits the bound JWT
and principal (contextvars are copied at task creation): every MCP call and
destination of the turn acts as the signed-in user. The task binds the two
contextvars of this module itself, and resets them in the same task: the
progress sink (``agents.progress.current_progress``) that turns specialist
progress into events, and the :class:`ChatRunContext` the document tools
write through. Nothing is bound across the SSE generator's ``yield``.

A turn is a :class:`Turn`: an event log plus subscribers. The POST stream
and a re-attach (``GET .../stream``) both subscribe; a subscriber gets the
events so far, then the live ones. A client that goes away only stops
receiving: the run goes on, is stored, and the UI reloads the conversation.
``POST .../cancel`` cancels the task; the partial answer is stored as
``canceled`` without ``model_json`` (the model never saw a complete turn).

Event mapping (the design's contract): the orchestrator's text deltas are
``text``; its own tool calls other than ``delegate_*`` (the document tools)
are ``tool`` cards, and a document tool's result also a ``file``; the
specialists' tool calls reach us through the progress side channel
(``agents.progress``) and are ``tool`` cards too; notes and delegation
phases are ``note``; a sign-in notice is ``message``. A heartbeat ``note``
("Working…") is sent after ``HEARTBEAT_QUIET_SECONDS`` of silence.

Each delegation is an ``agent`` event (a ``running`` frame with the prompt,
a closing frame with status, output, duration and the specialist's own
tokens) and an ``agent`` part; a ``tool`` card names the delegation it ran
under (``agent_id``, ``None`` for the orchestrator's own tools) and its
closing frame its ``duration_ms``. Once per turn, before ``done``, a
``usage`` event and part carry the run tree's tokens (the orchestrator
run's ``result.usage()``) and the per-delegation shares; ``done`` carries
the turn's ``duration_ms``. Durations are measured here (monotonic), and
only numbers and the model id go into ``usage``.

Who answers is the conversation's ``agent_ref`` (``user_agents``): the
orchestrator (``None``), an admin specialist run straight (``admin:<name>``)
or a personal agent built for this turn (``mine:<id>``). The running agent's
own tool cards carry its display name (``orchestrator`` for the
orchestrator); a ref that names nothing usable fails the turn with
``agent_unavailable``, stored as a ``failed`` assistant message. A
specialist run straight holds its build's ``in_flight`` counter, as a
delegation does, so a reload does not close its MCP clients mid-turn.

A file a tool hands to the model (a ``BinaryContent`` whose ``identifier``
starts with ``tool:``, such as the supplier PDF of ``builtin:ariba``'s
``read_document``) is the model's for that turn only: the stored
``model_json`` holds :data:`TOOL_FILE_PLACEHOLDER` in its place
(:func:`without_tool_files`), so the bytes are not kept with the
conversation and not sent again with a later turn's history. The user's own
uploads (identifier = the ``chat_files`` id) are kept as before.

Exception text reaches the stream and the row only through
``agents.errors.describe_exception``. The turns are per process (one app
instance, like the other runners); the run lock is the database row.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import uuid4
from xml.sax.saxutils import quoteattr

from pydantic_ai.messages import (
    BinaryContent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    UserPromptPart,
)

from agents.access import BASIC, current_access
from agents.db import SessionLocal
from agents.errors import describe_exception, mask_text
from agents.oauth2 import OAuthAuthorizationRequired
from agents.progress import ProgressUpdate, current_progress
from agents.registry import _short_tool_output, registry
from agents.run_activity import _clip, _detail_for
from agents.shared import available_models, default_model_name, get_model, run_usage_limits
from agents.webchat import store, user_agents
from agents.webchat.docgen import (
    TOOL_NAMES,
    ChatRunContext,
    current_chat_run,
    docgen_toolset,
)
from agents.webchat.uploads import Upload

logger = logging.getLogger(__name__)

DELEGATE_PREFIX = "delegate_"
ORCHESTRATOR_AGENT = "orchestrator"
HEARTBEAT_QUIET_SECONDS = float(os.environ.get("CHAT_HEARTBEAT_QUIET_SECONDS", "1.5"))
HEARTBEAT_POLL_SECONDS = 0.5
PING_SECONDS = 15.0
WORKING = "Working…"
COMPOSING = "Composing the answer…"
OUTPUT_CHARS = 400
MODEL_CACHE_SECONDS = 60.0

SSE_HEADERS = {
    # The approuter gzips text/* and so buffers a whole stream; it skips a
    # body marked no-transform. X-Accel-Buffering switches off nginx-style
    # buffering on the way (the same trick as agents/chat_app.py).
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}

# Fixed texts of the `error` event; only run_failed carries the exception,
# through describe_exception.
MSG_CANCELED = "The run was cancelled."
MSG_MODEL_UNAVAILABLE = "The selected model is not available."
MSG_CREDENTIAL = "A system the agents use needs you to sign in first."
MSG_AGENT_UNAVAILABLE = "The selected agent is no longer available."
CANCELED_ERROR = "canceled: the run was cancelled"
AGENT_UNAVAILABLE_ERROR = "agent_unavailable: the selected agent is no longer available"


class UnknownModel(ValueError):
    """A model id that is not offered."""


class ModelUnavailable(RuntimeError):
    """An offered model that could not be built."""


class AgentUnavailable(RuntimeError):
    """The conversation's ``agent_ref`` names nothing the user may use now."""


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class ModelCatalog:
    """The models the chat offers: ``available_models()`` (AI Core, or the
    ``AICORE_AVAILABLE_MODELS`` override) plus the orchestrator's own.

    ``available_models`` may query AI Core synchronously, so it runs in a
    thread and its answer is kept for ``MODEL_CACHE_SECONDS``.
    """

    def __init__(self, ttl: float = MODEL_CACHE_SECONDS) -> None:
        self.ttl = ttl
        self._names: list[str] | None = None
        self._at = 0.0

    def default_name(self) -> str:
        if registry.loaded:
            name = getattr(registry.orchestrator.model, "model_name", None)
            if isinstance(name, str) and name:
                return name
        return default_model_name()

    async def names(self) -> list[str]:
        now = time.monotonic()
        if self._names is None or now - self._at > self.ttl:
            try:
                self._names = list(await asyncio.to_thread(available_models))
            except Exception as exc:  # noqa: BLE001 - the default is still offered
                logger.warning("webchat: listing models failed (%s)", type(exc).__name__)
                self._names = []
            self._at = now
        default = self.default_name()
        return [default] + [n for n in self._names if n != default]

    async def listing(self) -> list[dict[str, Any]]:
        names = await self.names()
        return [{"id": n, "label": n, "default": i == 0} for i, n in enumerate(names)]

    async def check(self, name: str | None) -> str | None:
        """``name`` when it is offered (``None`` for the default);
        ``UnknownModel`` otherwise."""
        if not name:
            return None
        if name not in await self.names():
            raise UnknownModel(name)
        return None if name == self.default_name() else name

    def build(self, name: str | None) -> Any:
        """The model object for a checked name (``None``: the orchestrator's)."""
        if name is None:
            return None
        try:
            return get_model(name)
        except Exception as exc:  # noqa: BLE001
            raise ModelUnavailable(type(exc).__name__) from None


catalog = ModelCatalog()


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------
_END = object()


@dataclass
class Turn:
    """The live state of one turn: its event log and who is listening."""

    conversation_id: str
    assistant_message_id: str
    principal: str
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    task: asyncio.Task | None = None
    finalized: bool = False
    closed: bool = False
    started: float = field(default_factory=time.monotonic)

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def publish(self, event: str, data: dict[str, Any]) -> None:
        if self.closed:
            return
        self.events.append((event, data))
        for queue in self.subscribers:
            queue.put_nowait((event, data))

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        for item in self.events:
            queue.put_nowait(item)
        if self.closed:
            queue.put_nowait(_END)
        else:
            self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for queue in self.subscribers:
            queue.put_nowait(_END)
        self.subscribers.clear()

    @property
    def running(self) -> bool:
        return not self.closed


# conversation id -> its turn in flight. Per process.
_turns: dict[str, Turn] = {}
_tasks: set[asyncio.Task] = set()


def live_turn(conversation_id: str) -> Turn | None:
    turn = _turns.get(conversation_id)
    return turn if turn is not None and turn.running else None


def frame(event: str, data: dict[str, Any]) -> bytes:
    """One SSE frame. JSON never holds a raw line break, so one data line."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


async def sse(turn: Turn, queue: asyncio.Queue) -> AsyncIterator[bytes]:
    """The SSE body of one subscriber; ``: ping`` every ``PING_SECONDS``.

    Starlette stops this generator when the client goes away; the run task
    is not touched by that.
    """
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), PING_SECONDS)
            except TimeoutError:
                yield b": ping\n\n"
                continue
            if item is _END:
                return
            event, data = item
            yield frame(event, data)
    finally:
        turn.unsubscribe(queue)


# ---------------------------------------------------------------------------
# What the run did: parts and events
# ---------------------------------------------------------------------------
def _preview_args(name: Any, args: Any) -> str:
    try:
        return mask_text(_detail_for(name, args))
    except Exception:  # noqa: BLE001 - a preview never breaks a run
        return ""


def _preview_output(text: str | None) -> str:
    return _clip(mask_text(text or ""), OUTPUT_CHARS) if text else ""


def _ms(since: float) -> int:
    return int((time.monotonic() - since) * 1000)


def _count(value: Any) -> int:
    """A token count as an int; anything else is 0 (usage holds numbers only)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _usage_numbers(usage: Any) -> dict[str, int] | None:
    """The numbers of a delegation's ``usage`` dict, ``None`` without one."""
    if not isinstance(usage, dict):
        return None
    out = {k: _count(usage.get(k)) for k in
           ("input_tokens", "output_tokens", "cache_read_tokens", "requests")}
    model = usage.get("model")
    out["model"] = model if isinstance(model, str) else None
    return out


def _docgen_output(content: Any) -> str:
    if isinstance(content, dict):
        if isinstance(content.get("error"), dict):
            return f"error: {content['error'].get('code', 'error')}"
        if content.get("file_id"):
            return f"{content.get('name')} ({content.get('size')} bytes)"
    return ""


class TurnRecorder:
    """Builds the assistant message's parts and publishes the events.

    The progress sink and the event stream handler both run on the event
    loop and mutate this in synchronous bursts only, so no lock is needed.
    """

    def __init__(self, turn: Turn, agent_label: str = ORCHESTRATOR_AGENT) -> None:
        self.turn = turn
        # The name on the running agent's own tool cards.
        self.agent_label = agent_label
        self.parts: list[dict[str, Any]] = []
        self._tools: dict[str, dict[str, Any]] = {}
        self._tool_started: dict[str, float] = {}
        self._agents: dict[str, dict[str, Any]] = {}
        self._agent_started: dict[str, float] = {}
        self._card_ids: dict[str, str] = {}
        self._suppressed: set[str] = set()
        self.note = WORKING
        self.delegations = 0
        self.last_activity = time.monotonic()
        self._noted = False
        self.finished = False

    # -- events --------------------------------------------------------------
    def _touch(self) -> None:
        self.last_activity = time.monotonic()
        self._noted = False

    def text(self, delta: str) -> None:
        if not delta:
            return
        last = self.parts[-1] if self.parts else None
        if last is not None and last["type"] == "text":
            last["text"] += delta
        else:
            self.parts.append({"type": "text", "text": delta})
        self.turn.publish("text", {"delta": delta})
        self._touch()

    def tool_start(self, card_id: str, agent: str, name: str, args: Any,
                   agent_id: str | None = None) -> None:
        part = {"type": "tool", "id": card_id, "agent": agent, "agent_id": agent_id,
                "name": name, "status": "running", "args": _preview_args(name, args),
                "output": "", "duration_ms": None}
        self.parts.append(part)
        self._tools[card_id] = part
        self._tool_started[card_id] = time.monotonic()
        self.turn.publish("tool", {k: part[k] for k in ("id", "agent", "agent_id", "name",
                                                         "status", "args")})
        self._touch()

    def tool_end(self, card_id: str, ok: bool, output: str) -> None:
        part = self._tools.pop(card_id, None)
        if part is None:
            return
        part["status"] = "done" if ok else "error"
        part["output"] = output
        part["duration_ms"] = _ms(self._tool_started.pop(card_id, time.monotonic()))
        self.turn.publish("tool", {k: part[k] for k in ("id", "agent", "agent_id", "name",
                                                         "status", "output", "duration_ms")})
        self._touch()

    def agent_start(self, delegation_id: str, agent: str, parent: str | None,
                    prompt: str | None) -> None:
        if delegation_id in self._agents:
            return
        part = {"type": "agent", "id": delegation_id, "agent": agent, "parent": parent,
                "status": "running", "prompt": _preview_output(prompt), "output": "",
                "duration_ms": None, "usage": None}
        self.parts.append(part)
        self._agents[delegation_id] = part
        self._agent_started[delegation_id] = time.monotonic()
        self.turn.publish("agent", {k: part[k] for k in ("id", "agent", "parent", "status",
                                                          "prompt")})
        self._touch()

    def agent_end(self, delegation_id: str, ok: bool, output: str | None,
                  usage: Any) -> None:
        part = self._agents.pop(delegation_id, None)
        if part is None:
            return
        part["status"] = "done" if ok else "error"
        part["output"] = _preview_output(output)
        part["duration_ms"] = _ms(self._agent_started.pop(delegation_id, time.monotonic()))
        part["usage"] = _usage_numbers(usage) if ok else None
        self.turn.publish("agent", {k: part[k] for k in ("id", "agent", "parent", "status",
                                                          "output", "duration_ms", "usage")})
        self._touch()

    def usage(self, run_usage: Any, model: str | None) -> None:
        """The turn's ``usage`` part and event (once, before ``done``)."""
        input_tokens = _count(getattr(run_usage, "input_tokens", 0))
        output_tokens = _count(getattr(run_usage, "output_tokens", 0))
        agents = [
            {"id": p["id"], "agent": p["agent"],
             "input_tokens": p["usage"]["input_tokens"],
             "output_tokens": p["usage"]["output_tokens"],
             "requests": p["usage"]["requests"], "model": p["usage"]["model"]}
            for p in self.parts if p["type"] == "agent" and p.get("usage")
        ]
        data = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": _count(getattr(run_usage, "cache_read_tokens", 0)),
            "total_tokens": input_tokens + output_tokens,
            "requests": _count(getattr(run_usage, "requests", 0)),
            "tool_calls": _count(getattr(run_usage, "tool_calls", 0)),
            "model": model,
            "duration_ms": self.turn.elapsed_ms(),
            "agents": agents,
        }
        self.parts.append({"type": "usage", **data})
        self.turn.publish("usage", data)

    def file(self, info: dict[str, Any]) -> None:
        data = {k: info.get(k) for k in ("file_id", "name", "media_type", "size")}
        # A dashboard's card reads "Dashboard · <title> · n widgets".
        data.update({k: info[k] for k in ("title", "widgets") if k in info})
        self.parts.append({"type": "file", **data})
        self.turn.publish("file", data)
        self._touch()

    def set_note(self, text: str) -> None:
        self.note = text
        self.turn.publish("note", {"text": text})
        self._noted = True

    def message(self, text: str) -> None:
        self.parts.append({"type": "note", "text": text})
        self.turn.publish("message", {"text": text})
        self._touch()

    def close_open_tools(self) -> None:
        """Close what is still running: tool cards, then delegations (both
        ``error``, output "not finished"). Idempotent."""
        for card_id in list(self._tools):
            self.tool_end(card_id, False, "not finished")
        for delegation_id in reversed(list(self._agents)):
            self.agent_end(delegation_id, False, "not finished", None)

    @property
    def text_so_far(self) -> str:
        return "".join(p["text"] for p in self.parts if p["type"] == "text")

    # -- the progress sink (specialists) ------------------------------------
    def sink(self, update: ProgressUpdate) -> None:
        if update.kind == "note":
            if update.text:
                self.set_note(update.text)
        elif update.kind == "message":
            if update.text:
                self.message(update.text)
        elif update.kind == "delegation_start":
            self.delegations += 1
            if update.delegation_id:
                self.agent_start(update.delegation_id, update.agent, update.parent,
                                 update.prompt)
            if update.text:
                self.set_note(update.text)
        elif update.kind == "delegation_end":
            self.delegations = max(0, self.delegations - 1)
            if update.delegation_id:
                self.agent_end(update.delegation_id, update.ok, update.output, update.usage)
            if self.delegations == 0:
                self.set_note(COMPOSING)
        elif update.kind == "tool_start":
            # A specialist consulting a peer: its own `delegate_*` call is an
            # `agent` card already (the peer's delegation_start), as the
            # orchestrator's delegate calls are suppressed in `handler`.
            if (update.tool_name or "").startswith(DELEGATE_PREFIX):
                return
            raw = update.tool_call_id or uuid4().hex
            card_id = f"spec:{uuid4().hex}"
            self._card_ids[raw] = card_id
            self.tool_start(card_id, update.agent, update.tool_name or "tool", update.args,
                            agent_id=update.delegation_id)
        elif update.kind == "tool_end":
            raw = update.tool_call_id or ""
            card_id = self._card_ids.pop(raw, None)
            if card_id is not None:
                self.tool_end(card_id, update.ok, _preview_output(update.output))

    # -- the orchestrator's event stream ------------------------------------
    async def handler(self, _ctx: Any, events: AsyncIterator[Any]) -> None:
        async for event in events:
            if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                self.text(event.part.content)
            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                self.text(event.delta.content_delta)
            elif isinstance(event, FunctionToolCallEvent):
                part = event.part
                if part.tool_name.startswith(DELEGATE_PREFIX):
                    self._suppressed.add(part.tool_call_id)
                    continue
                try:
                    args = part.args_as_dict()
                except Exception:  # noqa: BLE001
                    args = None
                self.tool_start(f"own:{part.tool_call_id}", self.agent_label,
                                part.tool_name, args)
            elif isinstance(event, FunctionToolResultEvent):
                result = event.result
                if result.tool_call_id in self._suppressed:
                    continue
                failed = isinstance(result, RetryPromptPart)
                content = getattr(result, "content", None)
                if result.tool_name in TOOL_NAMES:
                    output = _docgen_output(content)
                    if isinstance(content, dict) and "error" in content:
                        failed = True
                else:
                    # An admin specialist run straight has its own tools here:
                    # the registry's preview keeps the fixed-form lines of the
                    # tools whose results are never stored (SharePoint,
                    # Bitbucket).
                    output = _preview_output(_short_tool_output(result))
                self.tool_end(f"own:{result.tool_call_id}", not failed, output)
                if (not failed and result.tool_name in TOOL_NAMES
                        and isinstance(content, dict) and content.get("file_id")):
                    self.file(content)

    # -- the heartbeat -------------------------------------------------------
    async def heartbeat(self) -> None:
        while not self.finished:
            await asyncio.sleep(HEARTBEAT_POLL_SECONDS)
            if self.finished or self._noted or self._tools:
                continue
            if time.monotonic() - self.last_activity >= HEARTBEAT_QUIET_SECONDS:
                self.set_note(self.note or WORKING)


# ---------------------------------------------------------------------------
# Prompt and history
# ---------------------------------------------------------------------------
def attachment_block(name: str, media_type: str, text: str, file_id: str | None) -> str:
    """The delimited text of an extracted upload. The text is data: a
    closing tag inside it is defused so it cannot end its own block. The
    ``id`` attribute is the ``chat_files`` id, by which the context view
    finds the block again (``agents/webchat/context.py``)."""
    body = text.replace("</attachment", "<\\/attachment")
    ident = f" id={quoteattr(file_id)}" if file_id else ""
    return (f"<attachment name={quoteattr(name)} type={quoteattr(media_type)}{ident}>\n"
            f"{body}\n</attachment>")


def build_prompt(text: str, uploads: list[Upload],
                 file_ids: list[str] | None = None) -> list[Any]:
    """``[BinaryContent…, "<attachment …>…</attachment>"…, text]``.

    ``file_ids`` are the ``chat_files`` ids of ``uploads``, in the same
    order: a binary upload carries its id as the ``BinaryContent``
    identifier (pydantic-ai keeps it in the stored ``model_json``), so the
    context view can take the bytes out of a stored turn and put them back.
    """
    ids: list[str | None] = list(file_ids or [])
    ids += [None] * (len(uploads) - len(ids))
    prompt: list[Any] = [BinaryContent(data=u.data, media_type=u.media_type, identifier=fid)
                         for u, fid in zip(uploads, ids) if u.binary]
    for u, fid in zip(uploads, ids):
        if u.binary or u.text is None:
            continue
        prompt.append(attachment_block(u.name, u.media_type, u.text, fid))
    prompt.append(text)
    return prompt


# The identifier prefix of a file a tool handed to the model (never an upload:
# those carry their `chat_files` id), and what the stored turn says instead.
TOOL_FILE_PREFIX = "tool:"
TOOL_FILE_PLACEHOLDER = "[document read by a tool; not kept in the conversation]"


def _tool_file(item: Any) -> bool:
    return isinstance(item, BinaryContent) and str(item.identifier or "").startswith(
        TOOL_FILE_PREFIX)


def without_tool_files(messages: list[ModelMessage]) -> list[ModelMessage]:
    """``messages`` with every tool-handed file replaced by the placeholder
    text. A new list of new requests where something was replaced; every other
    message and part is the original object."""
    out: list[ModelMessage] = []
    for message in messages:
        if not isinstance(message, ModelRequest) or not any(
            isinstance(p, UserPromptPart) and not isinstance(p.content, str)
            and any(_tool_file(i) for i in p.content) for p in message.parts
        ):
            out.append(message)
            continue
        parts = [
            replace(p, content=[TOOL_FILE_PLACEHOLDER if _tool_file(i) else i
                                for i in p.content])
            if isinstance(p, UserPromptPart) and not isinstance(p.content, str) else p
            for p in message.parts
        ]
        out.append(replace(message, parts=parts))
    return out


def load_history(stored: list[str]) -> list[ModelMessage]:
    """The concatenated ``model_json`` of the earlier turns; a row that does
    not parse is left out (logged by position only)."""
    history: list[ModelMessage] = []
    for index, raw in enumerate(stored):
        try:
            history.extend(ModelMessagesTypeAdapter.validate_json(raw))
        except Exception as exc:  # noqa: BLE001
            logger.warning("webchat: stored turn %d not usable as history (%s)",
                           index, type(exc).__name__)
    return history


def _credential_needed(exc: BaseException) -> bool:
    if isinstance(exc, OAuthAuthorizationRequired):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_credential_needed(e) for e in exc.exceptions)
    return False


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
@dataclass
class TurnInput:
    conversation_id: str
    assistant_message_id: str
    principal: str
    prompt: list[Any]
    history: list[ModelMessage]
    model_name: str | None
    title: str | None
    agent_ref: str | None = None
    # The conversation's access tier (agents/access.py), bound for the run.
    access: str = BASIC


@dataclass
class Runner:
    """What runs one turn: the agent, the label of its own tool cards, the
    ``model=`` override of the run (``None``: the agent's own), the model id
    for ``usage``, the one note of what a personal agent skipped, and the
    build counter a specialist run straight holds."""

    agent: Any
    label: str
    model: Any
    model_name: str | None
    note: str | None = None
    counter: Any = None


def _model_id(model: Any) -> str | None:
    name = getattr(model, "model_name", None)
    return name if isinstance(name, str) and name else None


async def personal_model_name(stored: str | None, picked: str | None) -> str | None:
    """The checked model of a personal agent: its own when still offered,
    else the conversation's pick; ``None`` for the default (the
    orchestrator's model)."""
    for name in (stored, picked):
        if not name:
            continue
        try:
            checked = await catalog.check(name)
        except UnknownModel:
            continue
        return checked
    return None


async def _personal_model(stored: str | None, picked: str | None) -> tuple[Any, str]:
    checked = await personal_model_name(stored, picked)
    if checked is not None:
        return catalog.build(checked), checked
    return registry.orchestrator.model, catalog.default_name()


async def resolve_runner(data: TurnInput) -> Runner:
    """The runner of ``data.agent_ref`` (re-read every turn);
    ``AgentUnavailable`` when it names nothing usable now."""
    target = await user_agents.resolve_agent_ref(data.principal, data.agent_ref,
                                                 data.access)
    if isinstance(target, str):
        raise AgentUnavailable(target)
    if target.kind == "orchestrator":
        return Runner(registry.orchestrator, ORCHESTRATOR_AGENT, catalog.build(data.model_name),
                      data.model_name or catalog.default_name())
    build = registry.build
    if target.kind == "admin":
        specialist = build.specialists.get(target.name)
        if specialist is None:
            raise AgentUnavailable(user_agents.AGENT_UNAVAILABLE)
        return Runner(specialist, target.name, None, _model_id(specialist.model),
                      counter=getattr(build, "in_flight", None))
    model, model_name = await _personal_model(target.user_row.model_name, data.model_name)
    agent, note = await user_agents.build_personal_agent(target.user_row, model=model,
                                                         build=build)
    # Held like a specialist run straight: the shared tool sets of the build
    # (addendum (g)) use its MCP clients, which a reload must not close
    # mid-turn.
    return Runner(agent, target.name, None, _model_id(model) or model_name, note=note,
                  counter=getattr(build, "in_flight", None))


async def _save_generated(turn: Turn, name: str, media_type: str, data: bytes) -> dict[str, Any]:
    async with SessionLocal() as db:
        return await store.add_generated_file(
            db, conversation_id=turn.conversation_id, message_id=turn.assistant_message_id,
            principal=turn.principal, name=name, media_type=media_type, data=data,
        )


async def _finalize(turn: Turn, rec: TurnRecorder | None, *, status: str,
                    content: str | None, model_json: str | None, error: str | None,
                    title: str | None) -> None:
    """Store the outcome and release the lock; never raises."""
    if rec is not None:
        rec.close_open_tools()
    parts = rec.parts if rec is not None else []
    try:
        async with SessionLocal() as db:
            await store.finish_turn(
                db, conversation_id=turn.conversation_id,
                message_id=turn.assistant_message_id, status=status, content=content,
                parts=parts, model_json=model_json, error=error, title=title,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("webchat: storing turn %s failed (%s)", turn.assistant_message_id,
                       type(exc).__name__)
        try:
            async with SessionLocal() as db:
                await store.release_run(db, turn.conversation_id)
        except Exception as exc2:  # noqa: BLE001
            logger.warning("webchat: releasing conversation lock failed (%s)",
                           type(exc2).__name__)
    turn.finalized = True


async def _finish(turn: Turn, rec: TurnRecorder | None, *, status: str,
                  content: str | None, model_json: str | None, error: str | None,
                  title: str | None) -> None:
    """Store the outcome, then send ``title`` / ``done`` and close the turn.

    The store runs in its own task and is awaited through ``shield``: a
    cancel that lands meanwhile (``POST .../cancel`` racing the end of the
    run, or shutdown) waits for the store to finish, closes the turn with
    the outcome that was stored, and is raised afterwards.
    """
    store_task = asyncio.ensure_future(_finalize(
        turn, rec, status=status, content=content, model_json=model_json,
        error=error, title=title))
    try:
        await asyncio.shield(store_task)
    except asyncio.CancelledError:
        await store_task
        _end(turn, status, title)
        raise
    _end(turn, status, title)


def _end(turn: Turn, status: str, title: str | None) -> None:
    if title:
        turn.publish("title", {"title": title})
    turn.publish("done", {"message_id": turn.assistant_message_id, "status": status,
                          "duration_ms": turn.elapsed_ms()})
    turn.close()
    if _turns.get(turn.conversation_id) is turn:
        _turns.pop(turn.conversation_id, None)


async def _run(turn: Turn, data: TurnInput) -> None:
    rec = TurnRecorder(turn)
    ctx = ChatRunContext(
        conversation_id=data.conversation_id, message_id=data.assistant_message_id,
        principal=data.principal,
        save=lambda name, media_type, blob: _save_generated(turn, name, media_type, blob),
    )
    # Bound here, in the run's own task, and reset in it.
    progress_token = current_progress.set(rec.sink)
    ctx_token = current_chat_run.set(ctx)
    # The orchestrator's (and a personal agent's consults') delegation tools
    # are gated on this (agents/access.py): the conversation decides, not
    # the caller's role alone, so a developer's basic conversation stays basic.
    access_token = current_access.set(data.access)
    beat = asyncio.create_task(rec.heartbeat())
    status, content, model_json, error = "failed", None, None, None
    counter = None
    try:
        try:
            runner = await resolve_runner(data)
            rec.agent_label = runner.label
            if runner.note:
                rec.message(runner.note)
            counter = runner.counter
            if counter is not None:
                counter.value += 1
            result = await runner.agent.run(
                data.prompt,
                message_history=data.history or None,
                model=runner.model,
                toolsets=[docgen_toolset()],
                usage_limits=run_usage_limits(),
                event_stream_handler=rec.handler,
            )
            output = result.output
            content = output if isinstance(output, str) else str(output)
            model_json = ModelMessagesTypeAdapter.dump_json(
                without_tool_files(result.new_messages())).decode("utf-8")
            status = "done"
            # Whatever is still open is closed first, so the usage part is
            # the last part and its event comes after every agent frame.
            rec.close_open_tools()
            rec.usage(result.usage(), runner.model_name)
        except asyncio.CancelledError:
            rec.finished = True
            turn.publish("error", {"code": "canceled", "message": MSG_CANCELED})
            await _finish(turn, rec, status="canceled", content=rec.text_so_far or None,
                          model_json=None, error=CANCELED_ERROR, title=data.title)
            raise
        except AgentUnavailable:
            error = AGENT_UNAVAILABLE_ERROR
            turn.publish("error", {"code": "agent_unavailable", "message": MSG_AGENT_UNAVAILABLE})
        except ModelUnavailable as exc:
            error = describe_exception(exc)
            turn.publish("error", {"code": "model_unavailable", "message": MSG_MODEL_UNAVAILABLE})
        except Exception as exc:  # noqa: BLE001
            error = describe_exception(exc)
            logger.warning("webchat: turn %s failed (%s)", turn.assistant_message_id,
                           type(exc).__name__)
            if _credential_needed(exc):
                turn.publish("error", {"code": "credential_required", "message": MSG_CREDENTIAL})
            else:
                turn.publish("error", {"code": "run_failed", "message": error})
            content = rec.text_so_far or None
    finally:
        if counter is not None:
            counter.value -= 1
        rec.finished = True
        beat.cancel()
        current_access.reset(access_token)
        current_chat_run.reset(ctx_token)
        current_progress.reset(progress_token)
    await _finish(turn, rec, status=status, content=content, model_json=model_json,
                  error=error, title=data.title)


def _turn_done(turn: Turn, data: TurnInput, task: asyncio.Task) -> None:
    """A task cancelled before its first step never ran its own finalize."""
    _tasks.discard(task)
    if turn.finalized or turn.closed:
        return

    async def close() -> None:
        turn.publish("error", {"code": "canceled", "message": MSG_CANCELED})
        await _finalize(turn, None, status="canceled", content=None, model_json=None,
                        error=CANCELED_ERROR, title=data.title)
        _end(turn, "canceled", data.title)

    closer = asyncio.get_running_loop().create_task(close())
    _tasks.add(closer)
    closer.add_done_callback(_tasks.discard)


# Indirection so a test can control task creation.
_spawn = asyncio.create_task


def start_turn(data: TurnInput, start_event: dict[str, Any]) -> tuple[Turn, asyncio.Queue]:
    """Start the run in its own task (call inside the request: the task
    copies the bound JWT and principal) and subscribe the caller first, so
    no event is missed."""
    turn = Turn(conversation_id=data.conversation_id,
                assistant_message_id=data.assistant_message_id, principal=data.principal)
    _turns[data.conversation_id] = turn
    queue = turn.subscribe()
    turn.publish("start", start_event)
    task = _spawn(_run(turn, data))
    turn.task = task
    _tasks.add(task)
    task.add_done_callback(lambda t: _turn_done(turn, data, t))
    return turn, queue


def cancel_turn(conversation_id: str) -> bool:
    turn = live_turn(conversation_id)
    if turn is None or turn.task is None or turn.task.done():
        return False
    turn.task.cancel()
    return True


async def cancel_all_chat_runs() -> None:
    """Cancel every turn in flight and let each store itself (shutdown)."""
    while True:
        tasks = [t for t in _tasks if not t.done()]
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
