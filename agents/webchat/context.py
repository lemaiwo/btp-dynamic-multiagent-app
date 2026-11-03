"""The context of a web chat conversation: what the agent gets on the next
turn, and a way to trim it (design addendum 2026-10-31 (e)).

``GET /chat/api/conversations/{id}/context`` answers :func:`describe`;
``POST .../context/attachments/{file_id}`` is
:func:`set_attachment_excluded`.

**Where the attachments are.** Every upload of a turn goes into the prompt
with its ``chat_files`` id (``run.build_prompt``): a PNG/JPEG/PDF as a
``BinaryContent`` whose ``identifier`` is the id (pydantic-ai keeps it in
the stored ``model_json``), an extracted text file as an ``<attachment ...
id="<file id>">`` block. Excluding a file rewrites the stored ``model_json``
of the turn that carries it: the part becomes the text
``[attachment "<name>" removed from the context]``; including it again puts
the bytes (or the re-extracted text) back from ``chat_files``. Whether a
file is excluded is read from the stored history alone (no column: that
would be a new HANA schema generation): its part is absent and the marker
is present. Two files of the same name in one turn share a marker text;
including one of them back takes the first marker of that name. Uploads of
turns stored before identifiers existed are not listed.

**Rewrites hold the run lock.** :func:`set_attachment_excluded` takes the
conversation's ``idle`` -> ``running`` lock (``store.claim_run``) for the
rewrite and releases it afterwards, so no turn can read or store the
history meanwhile; a running turn is a 409.

**Visibility.** A personal agent's own instructions and own skills are its
owner's, always shown. The orchestrator's and an admin agent's instructions
are admin-authored: their text only for the ``admin`` scope
(``agents.access.is_admin``), else the length. Admin skills: name and
length only.

**Figures.** The token estimate is a rough figure (characters / 4 plus a
fixed amount per binary); the measured figure is the ``input_tokens`` of the
last model request of the last stored turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from dataclasses import dataclass
from typing import Any

from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from sqlalchemy import select, update

from agents.access import BASIC, agent_allowed, is_admin, tier_of
from agents.data_catalog.scope import allowed_sources_of
from agents.data_catalog.tool import TOOL_NAMES as DATA_CATALOG_TOOL_NAMES
from agents.db import (
    AgentConfig,
    ChatConversation,
    ChatFile,
    ChatMessage,
    ChatUserSkill,
    ODataService,
    SessionLocal,
    SkillConfig,
    delegation_tool_name,
    get_orchestrator_instructions,
)
from agents.deep import DeepConfig, deep_toolset
from agents.multipart_form import count_pdf_pages
from agents.registry import _approves
from agents.webchat import run, store, uploads, user_agents
from agents.webchat.docgen import TOOL_NAMES

logger = logging.getLogger(__name__)

# Known model families, matched by substring of the model id (first match
# wins, so the longer family names come first).
CONTEXT_WINDOWS: tuple[tuple[str, int], ...] = (
    ("gpt-4o", 128_000),
    ("gpt-5", 400_000),
    ("claude", 200_000),
    ("gemini", 1_000_000),
)
CONTEXT_WINDOWS_ENV = "CHAT_CONTEXT_WINDOWS"

CHARS_PER_TOKEN = 4
IMAGE_TOKENS = 1_000
PDF_PAGE_TOKENS = 1_500
PDF_CHUNK_BYTES = 100 * 1024

MARKER = '[attachment "{name}" removed from the context]'


class ContextError(Exception):
    """A refusal of the context routes: ``status`` and a fixed ``detail``."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


FILE_NOT_FOUND = "No such file"
RUNNING = "a run is in progress"


# ---------------------------------------------------------------------------
# Context windows
# ---------------------------------------------------------------------------
def _overrides() -> dict[str, int]:
    """``CHAT_CONTEXT_WINDOWS`` as ``{id or prefix: tokens}``; anything that
    is not a JSON object of positive integers is ignored (one WARNING)."""
    raw = os.environ.get(CONTEXT_WINDOWS_ENV, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        logger.warning("webchat: %s is not JSON; ignored", CONTEXT_WINDOWS_ENV)
        return {}
    if not isinstance(value, dict):
        logger.warning("webchat: %s is not a JSON object; ignored", CONTEXT_WINDOWS_ENV)
        return {}
    return {k: v for k, v in value.items()
            if isinstance(k, str) and k and isinstance(v, int)
            and not isinstance(v, bool) and v > 0}


def context_windows(model_id: str | None) -> int | None:
    """The context window of ``model_id`` in tokens: an override by exact id,
    else by the longest matching prefix, else the code table by substring;
    ``None`` when unknown."""
    if not model_id:
        return None
    overrides = _overrides()
    if model_id in overrides:
        return overrides[model_id]
    prefixes = [k for k in overrides if model_id.startswith(k)]
    if prefixes:
        return overrides[max(prefixes, key=len)]
    lowered = model_id.lower()
    for family, tokens in CONTEXT_WINDOWS:
        if family in lowered:
            return tokens
    return None


# ---------------------------------------------------------------------------
# Estimates
# ---------------------------------------------------------------------------
def binary_tokens(item: BinaryContent) -> int:
    """The fixed estimate of one binary: an image 1,000; a PDF 1,500 per page
    when the page count can be told from the bytes, else per 100 KB."""
    if item.is_image:
        return IMAGE_TOKENS
    if item.media_type == uploads.PDF:
        pages = count_pdf_pages(item.data)
        if pages > 0:
            return pages * PDF_PAGE_TOKENS
    return max(1, math.ceil(len(item.data) / PDF_CHUNK_BYTES)) * PDF_PAGE_TOKENS


def _content_chars(content: Any) -> tuple[int, int]:
    """(text characters, binary tokens) of a user prompt's content."""
    if isinstance(content, str):
        return len(content), 0
    chars = tokens = 0
    for item in content or ():
        if isinstance(item, str):
            chars += len(item)
        elif isinstance(item, BinaryContent):
            tokens += binary_tokens(item)
    return chars, tokens


def history_figures(messages: list[ModelMessage]) -> tuple[int, int]:
    """(text characters, binary tokens) of a history: user prompts, system
    prompts, text parts, tool calls and returns, retry prompts."""
    chars = tokens = 0
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart):
                    c, t = _content_chars(part.content)
                    chars, tokens = chars + c, tokens + t
                elif isinstance(part, SystemPromptPart):
                    chars += len(part.content)
                elif isinstance(part, ToolReturnPart):
                    chars += len(part.model_response_str())
                elif isinstance(part, RetryPromptPart):
                    chars += len(part.model_response())
        elif isinstance(message, ModelResponse):
            for part in message.parts:
                if isinstance(part, TextPart):
                    chars += len(part.content)
                elif isinstance(part, ToolCallPart):
                    chars += len(part.tool_name) + len(part.args_as_json_str())
    return chars, tokens


def estimate_tokens(messages: list[ModelMessage], instructions: str,
                    skills: list[str]) -> int:
    """A rough token figure: characters / 4 over the history's text, the
    instructions and the skills, plus the fixed estimate per binary. CPU
    work for a PDF (page count): call it in a thread."""
    chars, tokens = history_figures(messages)
    chars += len(instructions or "") + sum(len(s or "") for s in skills)
    return math.ceil(chars / CHARS_PER_TOKEN) + tokens


def measured_tokens(last_model_json: str | None) -> int | None:
    """The ``input_tokens`` of the last model response of a stored turn
    (the context size the model last saw); ``None`` when absent."""
    if not last_model_json:
        return None
    try:
        messages = ModelMessagesTypeAdapter.validate_json(last_model_json)
    except Exception:  # noqa: BLE001 - a turn that does not parse has no figure
        return None
    for message in reversed(messages):
        if isinstance(message, ModelResponse):
            value = getattr(message.usage, "input_tokens", 0)
            return value if isinstance(value, int) and value > 0 else None
    return None


# ---------------------------------------------------------------------------
# Attachments in the stored history
# ---------------------------------------------------------------------------
def _block_id(item: str, file_id: str) -> bool:
    """Whether ``item`` is the ``<attachment>`` block of ``file_id``."""
    if not item.startswith("<attachment "):
        return False
    head = item.split("\n", 1)[0]
    return f' id="{file_id}">' in head or f" id='{file_id}'>" in head


def _carries(item: Any, file_id: str) -> bool:
    if isinstance(item, BinaryContent):
        return item.identifier == file_id
    return isinstance(item, str) and _block_id(item, file_id)


def _prompts(messages: list[ModelMessage]) -> list[UserPromptPart]:
    return [part for m in messages if isinstance(m, ModelRequest)
            for part in m.parts if isinstance(part, UserPromptPart)]


def attachment_state(messages: list[ModelMessage], file_id: str, name: str) -> bool | None:
    """``False`` when the turn carries the file, ``True`` when only its
    marker is there, ``None`` when neither (the file is not in the
    history)."""
    marker = MARKER.format(name=name)
    seen_marker = False
    for part in _prompts(messages):
        content = part.content
        if isinstance(content, str):
            seen_marker = seen_marker or content == marker
            continue
        for item in content:
            if _carries(item, file_id):
                return False
            if item == marker:
                seen_marker = True
    return True if seen_marker else None


def _replace(messages: list[ModelMessage], match: Any, make: Any) -> bool:
    """Replace the first prompt item for which ``match(item)`` holds by
    ``make(item)``; whether one was replaced."""
    for part in _prompts(messages):
        if isinstance(part.content, str):
            continue
        items = list(part.content)
        for index, item in enumerate(items):
            if match(item):
                items[index] = make(item)
                part.content = items
                return True
    return False


# ---------------------------------------------------------------------------
# Reading a conversation
# ---------------------------------------------------------------------------
@dataclass
class _Turn:
    message_id: str
    user_message_id: str | None
    messages: list[ModelMessage]
    model_json: str


async def _stored_turns(db: Any, cid: str) -> list[_Turn]:
    """The finished turns that make up the history, in order, each with
    the id of the user message before it (the uploads hang off that one)."""
    rows = (await db.execute(
        select(ChatMessage.id, ChatMessage.position, ChatMessage.role,
               ChatMessage.status, ChatMessage.model_json)
        .where(ChatMessage.conversation_id == cid)
        .order_by(ChatMessage.position)
    )).all()
    user_at = {r.position: r.id for r in rows if r.role == "user"}
    turns: list[_Turn] = []
    for r in rows:
        if r.role != "assistant" or r.status != "done" or not r.model_json:
            continue
        try:
            messages = ModelMessagesTypeAdapter.validate_json(r.model_json)
        except Exception as exc:  # noqa: BLE001 - load_history leaves it out too
            logger.warning("webchat: stored turn not usable as history (%s)",
                           type(exc).__name__)
            continue
        turns.append(_Turn(r.id, user_at.get(r.position - 1), messages, r.model_json))
    return turns


async def _usage(db: Any, cid: str) -> dict[str, int]:
    rows = (await db.execute(
        select(ChatMessage.role, ChatMessage.parts_json)
        .where(ChatMessage.conversation_id == cid, ChatMessage.role == "assistant")
    )).all()
    return store.conversation_usage(
        [{"role": r.role, "parts": store._parts(r.parts_json)} for r in rows])


def _deep_tool_names(name: str) -> list[str]:
    """The tools of a personal agent's deep toolset (the default config),
    read from the toolset itself so the list cannot drift."""
    toolset = deep_toolset(DeepConfig(enabled=True), parent_toolsets=[], model=None,
                           agent_name=name)
    return list(toolset.tools)


def _skill(scope: str, name: str, content: str, visible: bool) -> dict[str, Any]:
    return {"scope": scope, "name": name, "chars": len(content), "visible": visible,
            "text": content if visible else None}


async def describe(conv: ChatConversation, principal: str,
                   claims: dict[str, Any] | None) -> dict[str, Any]:
    """The answer of ``GET /conversations/{id}/context``."""
    access = conv.access or BASIC
    admin = is_admin(claims)
    target = await user_agents.resolve_agent_ref(principal, conv.agent_ref, access)
    skills: list[dict[str, Any]] = []
    skill_texts: list[str] = []
    tools: list[dict[str, Any]] = [{"kind": "document", "name": n} for n in TOOL_NAMES]
    async with SessionLocal() as db:
        rows = (await db.execute(select(AgentConfig))).scalars().all()
        usable = {r.name: r for r in rows if user_agents.chat_usable(r)}
        if isinstance(target, str):
            names = await user_agents.display_names(db, principal, [conv.agent_ref])
            kind, _ = _kind_of(conv.agent_ref)
            agent = {"ref": conv.agent_ref, "name": names.get(conv.agent_ref), "kind": kind,
                     "tier": None}
            text, visible, model_id = "", False, None
        elif target.kind == "orchestrator":
            agent = {"ref": None, "name": target.name, "kind": "orchestrator", "tier": BASIC}
            text, visible = await get_orchestrator_instructions(db), admin
            model_id = conv.model_name or run.catalog.default_name()
            tools += [{"kind": "delegate", "name": delegation_tool_name(r.name), "agent": r.name}
                      for r in sorted(usable.values(), key=lambda r: r.name)
                      if agent_allowed(r, access)]
        elif target.kind == "admin":
            row = target.admin_row
            agent = {"ref": conv.agent_ref, "name": row.name, "kind": "admin",
                     "tier": tier_of(row)}
            text, visible = row.instructions or "", admin
            model_id = (row.model_name or "").strip() or run.catalog.default_name()
            contents = dict((await db.execute(
                select(SkillConfig.name, SkillConfig.content)
                .where(SkillConfig.name.in_(row.skills))
            )).all()) if row.skills else {}
            for name in row.skills:
                if name in contents:
                    skills.append(_skill("admin", name, contents[name] or "", False))
                    skill_texts.append(contents[name] or "")
            enabled = {r.name: r for r in rows if r.enabled and not _approves(r.mcp_servers)}
            tools += [{"kind": "delegate", "name": delegation_tool_name(p), "agent": p}
                      for p in row.peers if p != row.name and p in enabled]
            # Its own servers, labelled as a personal agent's tool sources are.
            tools += [{"kind": "connector", "name": label, "agent": row.name}
                      for label in user_agents.connector_labels(row)]
            # The registry attaches the data catalogue's tools when one of the
            # agent's OData services exists and is enabled (attached_services).
            services = sorted(n for k, n in allowed_sources_of(row) if k == "odata")
            if services and (await db.execute(
                select(ODataService.name)
                .where(ODataService.name.in_(services), ODataService.enabled == 1)
                .limit(1)
            )).first() is not None:
                tools += [{"kind": "data_catalog", "name": n, "agent": row.name}
                          for n in DATA_CATALOG_TOOL_NAMES]
        else:
            mine = target.user_row
            sources = {r.name: r for r in rows if user_agents.shares_tools(r)}
            agent = {"ref": conv.agent_ref, "name": mine.name, "kind": "mine",
                     "tier": user_agents.personal_tier(mine, usable, sources)}
            text, visible = mine.instructions or "", True
            checked = await run.personal_model_name(mine.model_name, conv.model_name)
            model_id = checked or run.catalog.default_name()
            refs = user_agents.skill_refs(mine)
            admin_wanted = [r["name"] for r in refs if r["scope"] == "admin"]
            admin_contents = dict((await db.execute(
                select(SkillConfig.name, SkillConfig.content)
                .where(SkillConfig.name.in_(admin_wanted))
            )).all()) if admin_wanted else {}
            own = {s.name_key: s for s in (await db.execute(
                select(ChatUserSkill).where(ChatUserSkill.principal == principal)
            )).scalars().all()}
            for ref in refs:
                if ref["scope"] == "admin" and ref["name"] in admin_contents:
                    content = admin_contents[ref["name"]] or ""
                    skills.append(_skill("admin", ref["name"], content, False))
                    skill_texts.append(content)
                elif ref["scope"] == "mine" and ref["name"].casefold() in own:
                    skill = own[ref["name"].casefold()]
                    skills.append(_skill("mine", skill.name, skill.content or "", True))
                    skill_texts.append(skill.content or "")
            if mine.deep:
                tools += [{"kind": "deep", "name": n} for n in _deep_tool_names(mine.name)]
            tools += [{"kind": "delegate", "name": delegation_tool_name(n), "agent": n}
                      for n in user_agents.consult_names(mine)
                      if n in usable and agent_allowed(usable[n], access)]
            # The shared tool sets of the next turn (addendum (g)): one entry
            # per connector label of each source that still shares.
            tools += [{"kind": "connector", "name": label, "agent": n}
                      for n in user_agents.tool_names(mine)
                      if n in sources and agent_allowed(sources[n], access)
                      for label in user_agents.connector_labels(sources[n])]
        turns = await _stored_turns(db, conv.id)
        files = (await db.execute(
            select(ChatFile.id, ChatFile.message_id, ChatFile.name, ChatFile.media_type,
                   ChatFile.size)
            .where(ChatFile.conversation_id == conv.id, ChatFile.kind == "upload")
            .order_by(ChatFile.created_at, ChatFile.id)
        )).all()
        usage = await _usage(db, conv.id)

    history = [m for t in turns for m in t.messages]
    by_user = {t.user_message_id: t for t in turns if t.user_message_id}
    attachments = []
    for f in files:
        turn = by_user.get(f.message_id)
        state = attachment_state(turn.messages, f.id, f.name) if turn else None
        if state is not None:
            attachments.append({"file_id": f.id, "name": f.name, "media_type": f.media_type,
                                "size": f.size, "excluded": state})
    chars, _ = history_figures(history)
    estimated = await asyncio.to_thread(estimate_tokens, history, text, skill_texts)
    return {
        "agent": agent,
        "access": access,
        "model": {"id": model_id, "context_window": context_windows(model_id)},
        "instructions": {"visible": visible, "text": text if visible else None,
                         "chars": len(text), "skills": skills},
        "history": {
            "turns": len(turns),
            "messages": len(history),
            "chars": chars,
            "estimated_tokens": estimated,
            "measured_tokens": measured_tokens(turns[-1].model_json) if turns else None,
            "attachments": attachments,
        },
        "tools": tools,
        "usage": usage,
    }


def _kind_of(ref: str | None) -> tuple[str, str | None]:
    try:
        return user_agents.parse_ref(ref)
    except ValueError:
        return "mine", None


# ---------------------------------------------------------------------------
# Excluding and including an attachment
# ---------------------------------------------------------------------------
async def _restored(row: Any) -> Any:
    """The prompt item of an upload, rebuilt from its stored bytes."""
    if row.media_type in uploads.BINARY_TYPES:
        return BinaryContent(data=row.data, media_type=row.media_type, identifier=row.id)
    text = await asyncio.to_thread(uploads.extract, row.media_type, row.data)
    return run.attachment_block(row.name, row.media_type, text or "", row.id)


async def set_attachment_excluded(conv: ChatConversation, file_id: str,
                                  excluded: bool) -> dict[str, Any]:
    """Take an upload out of the stored history, or put it back.

    ``ContextError(404)`` when ``file_id`` is not an upload of ``conv`` in
    the history, ``ContextError(409)`` while a turn runs. Idempotent: an
    upload already in the asked state is answered without a write."""
    async with SessionLocal() as db:
        row = (await db.execute(
            select(ChatFile).where(ChatFile.id == file_id,
                                   ChatFile.conversation_id == conv.id,
                                   ChatFile.principal == conv.principal,
                                   ChatFile.kind == "upload")
        )).scalar_one_or_none()
        if row is None:
            raise ContextError(404, FILE_NOT_FOUND)
        if not await store.claim_run(db, conv.id, conv.principal):
            raise ContextError(409, RUNNING)
    try:
        async with SessionLocal() as db:
            turn = next((t for t in await _stored_turns(db, conv.id)
                         if t.user_message_id == row.message_id), None)
            state = attachment_state(turn.messages, row.id, row.name) if turn else None
            if state is None:
                raise ContextError(404, FILE_NOT_FOUND)
            if state is not excluded:
                marker = MARKER.format(name=row.name)
                if excluded:
                    _replace(turn.messages, lambda item: _carries(item, row.id),
                             lambda _item: marker)
                else:
                    restored = await _restored(row)
                    _replace(turn.messages, lambda item: item == marker,
                             lambda _item: restored)
                await db.execute(
                    update(ChatMessage).where(ChatMessage.id == turn.message_id)
                    .values(model_json=ModelMessagesTypeAdapter.dump_json(
                        turn.messages).decode("utf-8"))
                    .execution_options(synchronize_session=False)
                )
                await db.commit()
    finally:
        async with SessionLocal() as db:
            await store.release_run(db, conv.id)
    return {"file_id": row.id, "excluded": excluded}
