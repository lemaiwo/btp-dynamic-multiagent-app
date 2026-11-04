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

Tool sources (addendum (g)): a personal agent may name admin agents that
share their tools (enabled, ``share_tools``, not approving:
:func:`tool_sources`, one rule for the catalogue, the save-time checks, the
context view and the run). At run time it gets the guarded server list the
current build keeps for that agent (``BuildResult.shared_toolsets``): the
MCP servers and built-ins with their credentials where they are, never the
specialist's own function tools. Only the connector label of an entry
(:func:`connector_labels`) ever leaves the server.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from pydantic_ai import Agent
from sqlalchemy import select

from agents.access import ADVANCED, BASIC, agent_allowed, effective_tier, tier_of
from agents.builtins import is_builtin_url
from agents.db import AgentConfig, ChatUserAgent, ChatUserSkill, SessionLocal, SkillConfig
from agents.deep import DeepConfig, deep_toolset, scoped_deep_instructions
from agents.registry import (
    _TOOL_RETRIES,
    _approves,
    _attach_delegation_tool,
    _make_progress_handler,
)
from agents.webchat.docgen import docgen_toolset
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
    "it in your answer with the download_url the tool returns. When the user asks "
    "for a dashboard, charts or KPIs, use create_dashboard with data from the "
    "conversation only.\n"
    "If a specialist's response contains a sign-in or authorization link, relay "
    "that response to the user verbatim, including the full link."
)


def skipped_note(consults: int, skills: int, tools: int = 0) -> str:
    """The one note of what a turn could not attach. The tool sources are
    named only when one was skipped, so the note of an agent without tools
    reads as before."""
    if tools:
        return (f"Skipped: {consults} consult(s) / {skills} skill(s) / "
                f"{tools} tool source(s) no longer available")
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


def shares_tools(row: AgentConfig) -> bool:
    """An admin agent whose tools a personal agent may use: enabled, with
    ``share_tools``, and not an approving Bitbucket agent (the build shares
    nothing of that one either). ``expose_chat`` is not required."""
    return (bool(row.enabled) and bool(getattr(row, "share_tools", 0))
            and not _approves(row.mcp_servers))


async def tool_sources(db: Any) -> dict[str, AgentConfig]:
    """The admin agents that share their tools, by name (one query)."""
    rows = (await db.execute(select(AgentConfig))).scalars().all()
    return {r.name: r for r in rows if shares_tools(r)}


def connector_labels(row: AgentConfig) -> list[str]:
    """What a user is shown of an agent's server entries: ``builtin:<name>``
    for a built-in, the host of an MCP server's URL; never a path, query,
    credential or destination name. Distinct, in entry order."""
    labels: list[str] = []
    for spec in row.mcp_servers:
        url = str(spec.get("url") or "").strip() if isinstance(spec, dict) else ""
        if is_builtin_url(url):
            label = url.lower()
        else:
            try:
                label = urlparse(url).hostname or ""
            except ValueError:
                label = ""
        if label and label not in labels:
            labels.append(label)
    return labels


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


def tool_names(row: ChatUserAgent) -> list[str]:
    """The admin agents whose tools this personal agent names (addendum (g))."""
    return [n for n in load_list(getattr(row, "tools_json", None)) if isinstance(n, str)]


def personal_tier(row: ChatUserAgent, usable: dict[str, AgentConfig],
                  sources: dict[str, AgentConfig] | None = None) -> str:
    """``advanced`` when the personal agent has the deep tools on, consults
    an advanced admin agent that is chat-usable now, or names an advanced
    tool source that shares now (``sources``, :func:`tool_sources`), else
    ``basic`` (addenda (d), (g)): such an agent is refused in a basic
    conversation."""
    if row.deep:
        return ADVANCED
    for name in consult_names(row):
        admin_row = usable.get(name)
        if admin_row is not None and tier_of(admin_row) != BASIC:
            return ADVANCED
    for name in tool_names(row):
        source = (sources or {}).get(name)
        if source is not None and tier_of(source) != BASIC:
            return ADVANCED
    return BASIC


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


async def resolve_agent_ref(principal: str, ref: str | None,
                            access: str = BASIC) -> RefTarget | str:
    """The target of ``ref`` for this principal in a conversation of
    ``access``, or :data:`AGENT_UNAVAILABLE` when it names nothing the user
    may use (now): an advanced admin agent or an advanced personal agent
    does not exist for a basic conversation."""
    try:
        kind, value = parse_ref(ref)
    except ValueError:
        return AGENT_UNAVAILABLE
    if kind == "orchestrator":
        return RefTarget("orchestrator", ORCHESTRATOR_NAME)
    async with SessionLocal() as db:
        usable = await chat_agents(db)
        if kind == "admin":
            row = usable.get(value or "")
            if row is None or not agent_allowed(row, access):
                return AGENT_UNAVAILABLE
            return RefTarget("admin", row.name, admin_row=row)
        mine = (await db.execute(
            select(ChatUserAgent).where(ChatUserAgent.id == value,
                                        ChatUserAgent.principal == principal)
        )).scalar_one_or_none()
        sources = await tool_sources(db) if mine is not None and tool_names(mine) else {}
    if mine is None or not agent_allowed(personal_tier(mine, usable, sources), access):
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
    only when its row is chat-usable now AND the build holds its specialist;
    a tool source only when its row shares now (re-read: switching it off
    applies to the next turn, without a reload), the build holds its server
    list and the turn's tier allows it (the second lock after the
    conversation's ``agent_ref`` check). Two or more sources are prefixed
    with their agent's name, so the same connector in two sources does not
    make two tools of one name.
    """
    refs = skill_refs(row)
    consults = consult_names(row)
    tools = tool_names(row)
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
        sources = await tool_sources(db) if tools else {}
    shared_by_name = getattr(build, "shared_toolsets", None) or {}
    tier = effective_tier()
    attached: list[tuple[str, list]] = []
    skipped_tools = 0
    for name in tools:
        source = sources.get(name)
        shared = shared_by_name.get(name)
        if source is None or not shared or not agent_allowed(source, tier):
            skipped_tools += 1
            continue
        attached.append((name, shared))
    shared_toolsets: list = []
    for name, shared in attached:
        if len(attached) > 1:
            prefix = _tool_prefix(name)
            shared_toolsets += [t.prefixed(prefix) for t in shared]
        else:
            shared_toolsets += list(shared)
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
    text = personal_instructions(row.instructions, skills)
    if row.deep:
        # The app's default limits; sub-agents get this agent's own toolsets
        # (the document tools and the shared tool sets), as an admin deep
        # agent's get its servers.
        config = DeepConfig(enabled=True)
        agent = Agent(
            model,
            instructions=[text, lambda: scoped_deep_instructions(config)],
            toolsets=[*shared_toolsets, deep_toolset(
                config, parent_toolsets=[docgen_toolset(), *shared_toolsets], model=model,
                agent_name=row.name, retries=_TOOL_RETRIES,
                progress_handler_factory=_make_progress_handler,
            )],
            retries=_TOOL_RETRIES,
        )
    else:
        agent = Agent(model, instructions=text, toolsets=shared_toolsets,
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
        # Gated like the orchestrator's: the turn binds current_access, so an
        # advanced consult is attached but listed only in an advanced turn.
        _attach_delegation_tool(agent, specialist, admin_row, counter=counter,
                                tier_gated=True)
    note = (skipped_note(skipped_consults, skipped_skills, skipped_tools)
            if skipped_consults or skipped_skills or skipped_tools else None)
    return agent, note


def _tool_prefix(name: str) -> str:
    """A tool-name prefix from an admin agent's name (letters, digits and
    ``_`` only, as tool names allow)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", name)[:32] or "tools"
