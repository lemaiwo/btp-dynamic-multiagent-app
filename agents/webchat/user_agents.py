"""Who answers a web chat turn: the conversation's ``agent_ref`` at run time.

``agent_ref`` is ``None`` (the orchestrator, as before), ``admin:<name>`` (an
admin specialist, run straight) or ``mine:<id>`` (a personal agent of the
conversation's owner, ``ChatUserAgent``).

A personal agent is prompt-level only. It is built here, per turn, as a
fresh pydantic-ai ``Agent`` and never reaches the registry build, A2A, the
invoke API or a job: it exists for the length of one web chat turn. What it
may consult are admin specialists that the user's chat can already reach
through the orchestrator (enabled, ``expose_chat``, not approving), wired
with the registry's own delegation tool so progress, usage, the re-entry
guard and the depth limit are identical. Every turn re-reads the rows: an
admin change (disabled, no longer chat-exposed, deleted) or a deleted skill
is skipped with ONE note, never an error; an ``admin:`` ref whose specialist
is gone fails the turn with ``agent_unavailable``.

Exactly the same "chat-usable" rule is used by the catalogue, the save-time
checks of a personal agent and the run: :func:`chat_agents`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from pydantic_ai import Agent
from sqlalchemy import select

from agents.db import AgentConfig, ChatUserAgent, ChatUserSkill, SessionLocal, SkillConfig
from agents.registry import _TOOL_RETRIES, _approves, _attach_delegation_tool
from agents.webchat.store import ORCHESTRATOR_NAME

logger = logging.getLogger(__name__)

ADMIN_PREFIX = "admin:"
MINE_PREFIX = "mine:"
DELETED_SUFFIX = " (deleted)"
# A mine: ref whose row is gone has no stored name to show.
DELETED_AGENT = "Agent" + DELETED_SUFFIX
AGENT_UNAVAILABLE = "agent_unavailable"

DOCUMENTS_SECTION = (
    "## Documents\n"
    "When the user asks for a document, a spreadsheet or a PDF, create it with "
    "create_word_document, create_pdf_document or create_excel_workbook and link "
    "it in your answer with the download_url the tool returns.\n"
    "If a specialist's response contains a sign-in or authorization link, relay "
    "that response to the user verbatim, including the full link."
)


def skipped_note(consults: int, skills: int) -> str:
    return f"Skipped: {consults} consult(s) / {skills} skill(s) no longer available"


# ---------------------------------------------------------------------------
# Reading rows
# ---------------------------------------------------------------------------
def chat_usable(row: AgentConfig) -> bool:
    """An admin agent a chat user may reach: enabled, chat-exposed and not an
    approving Bitbucket agent (the registry refuses that one chat too)."""
    return bool(row.enabled) and bool(row.expose_chat) and not _approves(row.mcp_servers)


async def chat_agents(db: Any) -> dict[str, AgentConfig]:
    """The chat-usable admin agents by name (one query)."""
    rows = (await db.execute(select(AgentConfig))).scalars().all()
    return {r.name: r for r in rows if chat_usable(r)}


async def admin_skill_names(db: Any) -> set[str]:
    return set((await db.execute(select(SkillConfig.name))).scalars().all())


def load_list(raw: str | None) -> list[Any]:
    """A stored JSON list; anything else is empty."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def skill_refs(row: ChatUserAgent) -> list[dict[str, str]]:
    return [
        {"scope": s["scope"], "name": s["name"]}
        for s in load_list(row.skills_json)
        if isinstance(s, dict) and s.get("scope") in ("admin", "mine")
        and isinstance(s.get("name"), str)
    ]


def consult_names(row: ChatUserAgent) -> list[str]:
    return [n for n in load_list(row.consults_json) if isinstance(n, str)]


def parse_ref(ref: str | None) -> tuple[str, str | None]:
    """``("orchestrator", None)``, ``("admin", name)`` or ``("mine", id)``;
    ``ValueError`` for anything else."""
    if ref is None:
        return "orchestrator", None
    if ref.startswith(ADMIN_PREFIX) and len(ref) > len(ADMIN_PREFIX):
        return "admin", ref[len(ADMIN_PREFIX):]
    if ref.startswith(MINE_PREFIX) and len(ref) > len(MINE_PREFIX):
        return "mine", ref[len(MINE_PREFIX):]
    raise ValueError("not an agent reference")


@dataclass
class RefTarget:
    """What a usable ref names: the orchestrator, an admin row or a personal
    agent row of the caller."""

    kind: str  # orchestrator | admin | mine
    name: str
    admin_row: AgentConfig | None = None
    user_row: ChatUserAgent | None = None


async def resolve_agent_ref(principal: str, ref: str | None) -> RefTarget | str:
    """The target of ``ref`` for this principal, or :data:`AGENT_UNAVAILABLE`
    when it names nothing the user may use (now)."""
    try:
        kind, value = parse_ref(ref)
    except ValueError:
        return AGENT_UNAVAILABLE
    if kind == "orchestrator":
        return RefTarget("orchestrator", ORCHESTRATOR_NAME)
    async with SessionLocal() as db:
        if kind == "admin":
            row = (await chat_agents(db)).get(value or "")
            if row is None:
                return AGENT_UNAVAILABLE
            return RefTarget("admin", row.name, admin_row=row)
        mine = (await db.execute(
            select(ChatUserAgent).where(ChatUserAgent.id == value,
                                        ChatUserAgent.principal == principal)
        )).scalar_one_or_none()
    if mine is None:
        return AGENT_UNAVAILABLE
    return RefTarget("mine", mine.name, user_row=mine)


async def display_names(db: Any, principal: str, refs: list[str | None]) -> dict[str | None, str]:
    """The display name of every ref: ``Orchestrator`` for ``None``, the
    agent's name, or the name with `` (deleted)`` when the ref resolves to
    nothing (two queries at most, whatever the number of refs)."""
    out: dict[str | None, str] = {None: ORCHESTRATOR_NAME}
    admin: dict[str, str] = {}
    mine: dict[str, str] = {}
    for ref in set(refs):
        if ref is None:
            continue
        try:
            kind, value = parse_ref(ref)
        except ValueError:
            out[ref] = DELETED_AGENT
            continue
        (admin if kind == "admin" else mine)[ref] = value or ""
    if admin:
        usable = await chat_agents(db)
        for ref, name in admin.items():
            out[ref] = name if name in usable else f"{name[:64]}{DELETED_SUFFIX}"
    if mine:
        ids = list(set(mine.values()))
        rows = dict((await db.execute(
            select(ChatUserAgent.id, ChatUserAgent.name).where(
                ChatUserAgent.principal == principal, ChatUserAgent.id.in_(ids))
        )).all())
        for ref, rid in mine.items():
            out[ref] = rows.get(rid, DELETED_AGENT)
    return out


# ---------------------------------------------------------------------------
# The personal agent of one turn
# ---------------------------------------------------------------------------
def personal_instructions(instructions: str, skills: list[tuple[str, str]]) -> str:
    """The agent's own instructions, one ``## Skill: <name>`` section per
    resolvable skill, then the fixed documents section."""
    parts = [instructions.strip()]
    parts += [f"## Skill: {name}\n{content.strip()}" for name, content in skills]
    parts.append(DOCUMENTS_SECTION)
    return "\n\n".join(parts)


async def build_personal_agent(row: ChatUserAgent, *, model: Any,
                               build: Any) -> tuple[Agent, str | None]:
    """A fresh ``Agent`` for one turn of the personal agent ``row`` and the
    one note of what was skipped (``None`` when nothing was).

    ``build`` is the registry's current build: a consulted name is attached
    only when its row is chat-usable now AND the build holds its specialist.
    """
    refs = skill_refs(row)
    consults = consult_names(row)
    async with SessionLocal() as db:
        admin_wanted = [r["name"] for r in refs if r["scope"] == "admin"]
        admin_skills = dict((await db.execute(
            select(SkillConfig.name, SkillConfig.content)
            .where(SkillConfig.name.in_(admin_wanted))
        )).all()) if admin_wanted else {}
        own = {s.name_key: s for s in (await db.execute(
            select(ChatUserSkill).where(ChatUserSkill.principal == row.principal)
        )).scalars().all()}
        usable = await chat_agents(db) if consults else {}
    skills: list[tuple[str, str]] = []
    skipped_skills = 0
    for ref in refs:
        if ref["scope"] == "admin" and ref["name"] in admin_skills:
            skills.append((ref["name"], admin_skills[ref["name"]]))
        elif ref["scope"] == "mine" and ref["name"].casefold() in own:
            skill = own[ref["name"].casefold()]
            skills.append((skill.name, skill.content))
        else:
            skipped_skills += 1
    agent = Agent(model, instructions=personal_instructions(row.instructions, skills),
                  retries=_TOOL_RETRIES)
    specialists = getattr(build, "specialists", {}) if build is not None else {}
    counter = getattr(build, "in_flight", None)
    skipped_consults = 0
    for name in consults:
        admin_row = usable.get(name)
        specialist = specialists.get(name)
        if admin_row is None or specialist is None:
            skipped_consults += 1
            continue
        _attach_delegation_tool(agent, specialist, admin_row, counter=counter)
    note = (skipped_note(skipped_consults, skipped_skills)
            if skipped_consults or skipped_skills else None)
    return agent, note
