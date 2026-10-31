"""Persistence of the web chat: conversations, messages, files.

Owner-scoped throughout: every read takes the caller's principal and a
conversation or file of another principal is simply not found (the routes
answer 404, never 403). Child rows (messages, files) are reached through an
owned conversation, or carry the owner themselves (``ChatFile.principal``).

Statements run on SQLite, Postgres and SAP HANA alike: no ``RETURNING``,
``Text`` columns (``content``, ``parts_json``, ``model_json``) are never
compared, ordered or grouped, and ``data`` is selected only by the one
download route. SQLite ignores ``ON DELETE CASCADE`` without
``PRAGMA foreign_keys=ON``, so children are deleted explicitly.

The run lock is ``ChatConversation.status``: ``idle`` -> ``running`` in a
conditional UPDATE whose rowcount says who won (:func:`claim_run`). It is
per database row, so it holds across app instances; the run itself lives
in one process (``agents/webchat/run.py``).
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agents.auth import current_principal
from agents.db import ChatConversation, ChatFile, ChatMessage
from agents.ide.models import iso_utc
from agents.ide.store import _chunks, env_int
from agents.notifications import _marker_key as principal_key

DEFAULT_TITLE = "New chat"
# The principal of a local run without XSUAA (``agents.auth`` binds the same).
LOCAL_PRINCIPAL = "local-dev"
MAX_TITLE_CHARS = 120
MAX_MESSAGES = 500
DEFAULT_LIST = 50
MAX_LIST = 100
_LIKE_ESCAPE = "\\"
# Days a conversation is kept after its last activity; 0 keeps them all.
# Read at import (a non-integer is the default, with a warning).
CHAT_RETENTION_DAYS = env_int("CHAT_RETENTION_DAYS", 180, minimum=0)


def caller() -> str:
    """The caller's principal as the rows key it (a digest when it does not
    fit the column, as the notification markers do)."""
    return principal_key(current_principal.get() or LOCAL_PRINCIPAL)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def one_line(text: str, limit: int = MAX_TITLE_CHARS) -> str:
    """Plain one-line text: no control, format or separator characters,
    whitespace collapsed, cut at ``limit``."""
    kept = "".join(
        " " if unicodedata.category(ch) in ("Cc", "Zl", "Zp") else ch
        for ch in text
        if unicodedata.category(ch) not in ("Cf", "Cs", "Co", "Cn")
    )
    return " ".join(kept.split())[:limit].strip()


def title_from(text: str) -> str:
    """A conversation title from the first user text: its first non-empty
    line, at most ``MAX_TITLE_CHARS`` characters."""
    for line in text.splitlines():
        title = one_line(line)
        if title:
            return title
    return DEFAULT_TITLE


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------
ORCHESTRATOR_NAME = "Orchestrator"
# ``update_conversation``: leave ``agent_ref`` as it is.
KEEP = object()


def conversation_out(conv: ChatConversation, agent_name: str | None = None) -> dict[str, Any]:
    """``agent_name`` is the display name of ``agent_ref`` the caller resolved
    (``agents.webchat.user_agents.display_names``); ``Orchestrator`` without
    a ref."""
    return {
        "id": conv.id,
        "title": conv.title,
        "model": conv.model_name,
        "status": conv.status,
        "created_at": iso_utc(conv.created_at),
        "updated_at": iso_utc(conv.updated_at),
        "agent_ref": conv.agent_ref,
        "agent_name": agent_name or ORCHESTRATOR_NAME,
    }


async def create_conversation(
    db: AsyncSession, principal: str, *, title: str | None, model: str | None,
    agent_ref: str | None = None,
) -> ChatConversation:
    now = utcnow()
    conv = ChatConversation(
        principal=principal,
        title=one_line(title or "") or DEFAULT_TITLE,
        model_name=model,
        agent_ref=agent_ref,
        created_at=now,
        updated_at=now,
        status="idle",
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


def _like(q: str) -> str:
    escaped = (q.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
               .replace("%", _LIKE_ESCAPE + "%").replace("_", _LIKE_ESCAPE + "_"))
    return f"%{escaped.lower()}%"


async def list_conversations(
    db: AsyncSession, principal: str, *, q: str | None = None,
    limit: int = DEFAULT_LIST, before: datetime | None = None,
) -> list[ChatConversation]:
    """The caller's conversations, newest activity first."""
    stmt = select(ChatConversation).where(ChatConversation.principal == principal)
    if q:
        stmt = stmt.where(
            func.lower(ChatConversation.title).like(_like(q), escape=_LIKE_ESCAPE)
        )
    if before is not None:
        stmt = stmt.where(ChatConversation.updated_at < before)
    stmt = stmt.order_by(ChatConversation.updated_at.desc(), ChatConversation.id.desc())
    rows = await db.execute(stmt.limit(max(1, min(limit, MAX_LIST))))
    return list(rows.scalars().all())


async def get_conversation(
    db: AsyncSession, cid: str, principal: str
) -> ChatConversation | None:
    """The conversation, only when the caller owns it."""
    row = await db.execute(
        select(ChatConversation).where(
            ChatConversation.id == cid, ChatConversation.principal == principal
        ).execution_options(populate_existing=True)
    )
    return row.scalar_one_or_none()


async def update_conversation(
    db: AsyncSession, conv: ChatConversation, *,
    title: str | None = None, model: str | None = None, clear_model: bool = False,
    agent_ref: Any = KEEP,
) -> ChatConversation:
    values: dict[str, Any] = {}
    if title is not None:
        values["title"] = one_line(title) or DEFAULT_TITLE
    if model is not None or clear_model:
        values["model_name"] = model
    if agent_ref is not KEEP:
        values["agent_ref"] = agent_ref
    if values:
        await db.execute(
            update(ChatConversation)
            .where(ChatConversation.id == conv.id,
                   ChatConversation.principal == conv.principal)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
    # Read again in this session: ``conv`` may come from another one.
    fresh = await get_conversation(db, conv.id, conv.principal)
    return fresh if fresh is not None else conv


async def _delete_children(db: AsyncSession, ids: list[str]) -> None:
    for model in (ChatFile, ChatMessage):
        await db.execute(
            delete(model).where(model.conversation_id.in_(ids))
            .execution_options(synchronize_session=False)
        )


async def delete_conversation(db: AsyncSession, cid: str, principal: str) -> bool:
    """Delete an idle conversation the caller owns with its children;
    whether it was deleted (``False`` also while a run holds it)."""
    result = await db.execute(
        delete(ChatConversation)
        .where(ChatConversation.id == cid, ChatConversation.principal == principal,
               ChatConversation.status != "running")
        .execution_options(synchronize_session=False)
    )
    if not result.rowcount:
        await db.rollback()
        return False
    await _delete_children(db, [cid])
    await db.commit()
    return True


# ---------------------------------------------------------------------------
# The run lock
# ---------------------------------------------------------------------------
async def claim_run(db: AsyncSession, cid: str, principal: str) -> bool:
    """``idle`` -> ``running``; whether this caller got the lock."""
    result = await db.execute(
        update(ChatConversation)
        .where(ChatConversation.id == cid, ChatConversation.principal == principal,
               ChatConversation.status == "idle")
        .values(status="running", updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return bool(result.rowcount)


async def release_run(db: AsyncSession, cid: str) -> None:
    await db.execute(
        update(ChatConversation)
        .where(ChatConversation.id == cid, ChatConversation.status == "running")
        .values(status="idle")
        .execution_options(synchronize_session=False)
    )
    await db.commit()


async def reset_running(db: AsyncSession) -> int:
    """Startup only: a process that has just started owns no chat run, so
    every ``running`` conversation goes back to ``idle`` and every
    ``running`` message becomes ``canceled``. Assumes ONE app instance, like
    the other startup sweeps."""
    result = await db.execute(
        update(ChatConversation).where(ChatConversation.status == "running")
        .values(status="idle").execution_options(synchronize_session=False)
    )
    await db.execute(
        update(ChatMessage).where(ChatMessage.status == "running")
        .values(status="canceled", error="interrupted: the app restarted during the run")
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return max(result.rowcount or 0, 0)


# ---------------------------------------------------------------------------
# Messages and files
# ---------------------------------------------------------------------------
def file_out(row: Any) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "media_type": row.media_type,
            "size": row.size, "kind": row.kind}


def _parts(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def message_out(msg: ChatMessage, files: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": msg.id,
        "position": msg.position,
        "role": msg.role,
        "content": msg.content,
        "parts": _parts(msg.parts_json),
        "status": msg.status,
        "error": msg.error,
        "created_at": iso_utc(msg.created_at),
        "files": files,
    }


_FILE_META = (ChatFile.id, ChatFile.message_id, ChatFile.name, ChatFile.media_type,
              ChatFile.size, ChatFile.kind, ChatFile.created_at)


async def conversation_detail(db: AsyncSession, conv: ChatConversation,
                              agent_name: str | None = None) -> dict[str, Any]:
    messages = (await db.execute(
        select(ChatMessage).where(ChatMessage.conversation_id == conv.id)
        .order_by(ChatMessage.position)
    )).scalars().all()
    files = (await db.execute(
        select(*_FILE_META).where(ChatFile.conversation_id == conv.id)
        .order_by(ChatFile.created_at, ChatFile.id)
    )).all()
    by_message: dict[str | None, list[dict[str, Any]]] = {}
    for row in files:
        by_message.setdefault(row.message_id, []).append(file_out(row))
    out = conversation_out(conv, agent_name)
    out["messages"] = [message_out(m, by_message.get(m.id, [])) for m in messages]
    out["usage"] = conversation_usage(out["messages"])
    return out


_USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens", "requests")


def conversation_usage(messages: list[dict[str, Any]]) -> dict[str, int]:
    """The conversation's token total: the sum over the ``usage`` parts of its
    assistant messages (one per completed turn; ``turns`` counts them).
    Summed in Python from the parts already read: ``parts_json`` is a
    ``Text`` column, which HANA cannot aggregate. Non-integer values count 0."""
    total = dict.fromkeys(_USAGE_KEYS, 0)
    turns = 0
    for msg in messages:
        if msg["role"] != "assistant":
            continue
        for part in msg["parts"]:
            if not isinstance(part, dict) or part.get("type") != "usage":
                continue
            turns += 1
            for key in _USAGE_KEYS:
                value = part.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    total[key] += value
    return {**total, "turns": turns}


async def message_count(db: AsyncSession, cid: str) -> int:
    return (await db.execute(
        select(func.count()).select_from(ChatMessage)
        .where(ChatMessage.conversation_id == cid)
    )).scalar_one()


@dataclass
class TurnRows:
    user_message_id: str
    assistant_message_id: str
    files: list[dict[str, Any]]


async def start_turn(
    db: AsyncSession, conv: ChatConversation, text: str,
    uploads: list[tuple[str, str, bytes]],
) -> TurnRows:
    """The user message with its uploads and the ``running`` assistant
    message, in one transaction. The caller holds the run lock, so the next
    positions are this turn's."""
    last = (await db.execute(
        select(func.max(ChatMessage.position)).where(ChatMessage.conversation_id == conv.id)
    )).scalar_one()
    position = (last or 0) + 1
    now = utcnow()
    user = ChatMessage(conversation_id=conv.id, position=position, role="user",
                       content=text, status="done", created_at=now)
    assistant = ChatMessage(conversation_id=conv.id, position=position + 1,
                            role="assistant", status="running",
                            created_at=now + timedelta(microseconds=1))
    db.add_all([user, assistant])
    await db.flush()
    files: list[ChatFile] = []
    for name, media_type, data in uploads:
        files.append(ChatFile(conversation_id=conv.id, message_id=user.id,
                              principal=conv.principal, kind="upload", name=name,
                              media_type=media_type, size=len(data), data=data,
                              created_at=utcnow()))
    db.add_all(files)
    await db.commit()
    return TurnRows(
        user_message_id=user.id,
        assistant_message_id=assistant.id,
        files=[{"id": f.id, "name": f.name, "media_type": f.media_type, "size": f.size}
               for f in files],
    )


async def add_generated_file(
    db: AsyncSession, *, conversation_id: str, message_id: str, principal: str,
    name: str, media_type: str, data: bytes,
) -> dict[str, Any]:
    row = ChatFile(conversation_id=conversation_id, message_id=message_id,
                   principal=principal, kind="generated", name=name,
                   media_type=media_type, size=len(data), data=data, created_at=utcnow())
    db.add(row)
    await db.commit()
    return {"file_id": row.id, "name": row.name, "media_type": row.media_type,
            "size": row.size, "download_url": f"/chat/api/files/{row.id}"}


async def history_json(db: AsyncSession, cid: str) -> list[str]:
    """The ``model_json`` of the conversation's finished assistant turns,
    in order (each a JSON list of pydantic-ai messages)."""
    rows = await db.execute(
        select(ChatMessage.model_json).where(
            ChatMessage.conversation_id == cid,
            ChatMessage.role == "assistant",
            ChatMessage.status == "done",
            ChatMessage.model_json.is_not(None),
        ).order_by(ChatMessage.position)
    )
    return [value for value in rows.scalars().all() if value]


async def finish_turn(
    db: AsyncSession, *, conversation_id: str, message_id: str, status: str,
    content: str | None, parts: list[dict[str, Any]], model_json: str | None,
    error: str | None, title: str | None,
) -> None:
    """Store the assistant message's outcome and release the run lock; a
    ``title`` replaces the default title only."""
    await db.execute(
        update(ChatMessage).where(ChatMessage.id == message_id)
        .values(status=status, content=content, parts_json=json.dumps(parts),
                model_json=model_json, error=(error or None) and error[:400])
        .execution_options(synchronize_session=False)
    )
    await db.execute(
        update(ChatConversation).where(ChatConversation.id == conversation_id)
        .values(status="idle", updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    if title:
        await db.execute(
            update(ChatConversation)
            .where(ChatConversation.id == conversation_id,
                   ChatConversation.title == DEFAULT_TITLE)
            .values(title=title)
            .execution_options(synchronize_session=False)
        )
    await db.commit()


async def get_file(db: AsyncSession, fid: str, principal: str) -> ChatFile | None:
    """The file with its bytes, only when the caller owns it."""
    row = await db.execute(
        select(ChatFile).where(ChatFile.id == fid, ChatFile.principal == principal)
    )
    return row.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
async def purge_older_than(db: AsyncSession, days: int) -> int:
    """Delete idle conversations whose last activity is older than ``days``,
    with their messages and files; the number deleted.

    No ``DELETE ... RETURNING`` (SAP HANA): the candidates are selected
    ``FOR UPDATE`` in id order, deleted under the same conditions, and
    whichever of them is still there afterwards was not removed (a turn
    started in between); only the children of removed rows are deleted.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    cutoff = utcnow() - timedelta(days=days)
    purgeable = (ChatConversation.updated_at < cutoff, ChatConversation.status != "running")
    candidates = list((await db.execute(
        select(ChatConversation.id).where(*purgeable)
        .order_by(ChatConversation.id).with_for_update()
    )).scalars().all())
    removed_total = 0
    for chunk in _chunks(candidates):
        await db.execute(
            delete(ChatConversation).where(ChatConversation.id.in_(chunk), *purgeable)
            .execution_options(synchronize_session=False)
        )
        kept = set((await db.execute(
            select(ChatConversation.id).where(ChatConversation.id.in_(chunk))
        )).scalars().all())
        removed = [cid for cid in chunk if cid not in kept]
        if removed:
            await _delete_children(db, removed)
        removed_total += len(removed)
    await db.commit()
    return removed_total
