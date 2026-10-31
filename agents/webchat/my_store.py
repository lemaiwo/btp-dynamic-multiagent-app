"""Persistence of the web chat's personal agents and skills, and the bundle.

Owner-scoped throughout: every read and write takes the caller's principal
(``store.caller()``), and a row of another principal is simply not found.
Statements run on SQLite, Postgres and SAP HANA alike: the ``Text`` columns
(``instructions``, ``content``, ``skills_json``, ``consults_json``) are never
compared, ordered or grouped; a cascade that edits ``skills_json`` reads the
caller's rows and rewrites them in Python.

Cascades (the addendum's rules):

* deleting an agent clears ``agent_ref`` of the caller's conversations that
  point at it (they fall back to the orchestrator);
* deleting a skill removes it from the ``skills`` of the caller's agents;
  renaming one (``PUT``) renames the references with it.

The import (:func:`import_bundle`) is per item, not all-or-nothing: an item
with an error is not written, the rest is. References to admin agents or
skills that do not exist here are dropped and reported; a ``mine`` skill
reference follows a skill of the same bundle through a rename.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agents.access import BASIC, agent_allowed
from agents.db import ChatConversation, ChatUserAgent, ChatUserSkill
from agents.ide.models import iso_utc
from agents.ide.store import _chunks
from agents.webchat import my_models as m
from agents.webchat import user_agents as ua
from agents.webchat.store import utcnow

CONFLICT_RULES = ("rename", "replace", "skip")


class NameTaken(Exception):
    """Another row of the caller has this name (case-insensitive)."""


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------
def agent_out(row: ChatUserAgent,
              usable: dict[str, Any] | None = None) -> dict[str, Any]:
    """``usable`` are the chat-usable admin agents (``ua.chat_agents``), for
    the agent's ``tier``; without them only ``deep`` decides it."""
    return {
        "id": row.id,
        "ref": f"{ua.MINE_PREFIX}{row.id}",
        "name": row.name,
        "description": row.description or "",
        "instructions": row.instructions,
        "skills": ua.skill_refs(row),
        "consults": ua.consult_names(row),
        "model": row.model_name,
        "deep": bool(row.deep),
        "tier": ua.personal_tier(row, usable or {}),
        "created_at": iso_utc(row.created_at),
        "updated_at": iso_utc(row.updated_at),
        "source": "mine",
        "readonly": False,
    }


def skill_out(row: ChatUserSkill) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description or "",
        "content": row.content,
        "created_at": iso_utc(row.created_at),
        "updated_at": iso_utc(row.updated_at),
        "source": "mine",
        "readonly": False,
    }


def _dump(values: list[Any]) -> str | None:
    return json.dumps(values) if values else None


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
async def list_agents(db: AsyncSession, principal: str) -> list[ChatUserAgent]:
    return list((await db.execute(
        select(ChatUserAgent).where(ChatUserAgent.principal == principal)
        .order_by(ChatUserAgent.name_key, ChatUserAgent.id)
    )).scalars().all())


async def list_skills(db: AsyncSession, principal: str) -> list[ChatUserSkill]:
    return list((await db.execute(
        select(ChatUserSkill).where(ChatUserSkill.principal == principal)
        .order_by(ChatUserSkill.name_key, ChatUserSkill.id)
    )).scalars().all())


async def get_agent(db: AsyncSession, principal: str, aid: str) -> ChatUserAgent | None:
    return (await db.execute(
        select(ChatUserAgent).where(ChatUserAgent.id == aid,
                                    ChatUserAgent.principal == principal)
    )).scalar_one_or_none()


async def get_skill(db: AsyncSession, principal: str, sid: str) -> ChatUserSkill | None:
    return (await db.execute(
        select(ChatUserSkill).where(ChatUserSkill.id == sid,
                                    ChatUserSkill.principal == principal)
    )).scalar_one_or_none()


async def _count(db: AsyncSession, model: Any, principal: str) -> int:
    return (await db.execute(
        select(func.count()).select_from(model).where(model.principal == principal)
    )).scalar_one()


async def _name_taken(db: AsyncSession, model: Any, principal: str, key: str,
                      except_id: str | None = None) -> bool:
    stmt = select(model.id).where(model.principal == principal, model.name_key == key)
    if except_id is not None:
        stmt = stmt.where(model.id != except_id)
    return (await db.execute(stmt.limit(1))).first() is not None


# ---------------------------------------------------------------------------
# References of an agent
# ---------------------------------------------------------------------------
@dataclass
class Refs:
    """An agent's references as they may be stored, and what did not resolve:
    ``problems`` holds ``(loc, field)`` per refused entry."""

    skills: list[dict[str, str]] = field(default_factory=list)
    consults: list[str] = field(default_factory=list)
    deep: bool = False
    problems: list[tuple[tuple[Any, ...], str]] = field(default_factory=list)


async def resolve_refs(db: AsyncSession, principal: str, data: m.AgentIn,
                       own_names: dict[str, str] | None = None, *,
                       developer: bool = False) -> Refs:
    """Check ``data``'s skills and consults against what the caller may use
    now: admin skills by exact name, own skills by name (case-insensitive,
    stored in the skill's own spelling; ``own_names`` maps key -> name when
    the caller already knows them), consults among the chat-usable admin
    agents. ``deep`` and an advanced consult only for a ``developer``."""
    out = Refs()
    if data.deep:
        if developer:
            out.deep = True
        else:
            out.problems.append((("deep",), "deep"))
    admin_skills = await ua.admin_skill_names(db) if any(
        r.scope == "admin" for r in data.skills) else set()
    if own_names is None and any(r.scope == "mine" for r in data.skills):
        own_names = {s.name_key: s.name for s in await list_skills(db, principal)}
    own_names = own_names or {}
    for index, ref in enumerate(data.skills):
        if ref.scope == "admin" and ref.name in admin_skills:
            out.skills.append({"scope": "admin", "name": ref.name})
        elif ref.scope == "mine" and m.name_key(ref.name) in own_names:
            out.skills.append({"scope": "mine", "name": own_names[m.name_key(ref.name)]})
        else:
            out.problems.append((("skills", index, "name"), "skills"))
    usable = await ua.chat_agents(db) if data.consults else {}
    for index, name in enumerate(data.consults):
        if name in usable and (developer or agent_allowed(usable[name], BASIC)):
            out.consults.append(name)
        else:
            out.problems.append((("consults", index), "consults"))
    return out


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------
def _apply_agent(row: ChatUserAgent, data: m.AgentIn, refs: Refs) -> None:
    row.name = data.name
    row.name_key = m.name_key(data.name)
    row.description = data.description
    row.instructions = data.instructions
    row.skills_json = _dump(refs.skills)
    row.consults_json = _dump(refs.consults)
    row.model_name = data.model
    row.deep = 1 if refs.deep else 0


async def agent_count(db: AsyncSession, principal: str) -> int:
    return await _count(db, ChatUserAgent, principal)


async def create_agent(db: AsyncSession, principal: str, data: m.AgentIn,
                       refs: Refs) -> ChatUserAgent:
    if await _name_taken(db, ChatUserAgent, principal, m.name_key(data.name)):
        raise NameTaken
    now = utcnow()
    row = ChatUserAgent(principal=principal, created_at=now, updated_at=now)
    _apply_agent(row, data, refs)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def replace_agent(db: AsyncSession, row: ChatUserAgent, data: m.AgentIn,
                        refs: Refs) -> ChatUserAgent:
    if await _name_taken(db, ChatUserAgent, row.principal, m.name_key(data.name),
                         except_id=row.id):
        raise NameTaken
    _apply_agent(row, data, refs)
    row.updated_at = utcnow()
    await db.commit()
    await db.refresh(row)
    return row


async def delete_agent(db: AsyncSession, row: ChatUserAgent) -> None:
    """Delete the agent; the caller's conversations that pointed at it go
    back to the orchestrator."""
    await db.execute(
        update(ChatConversation)
        .where(ChatConversation.principal == row.principal,
               ChatConversation.agent_ref == f"{ua.MINE_PREFIX}{row.id}")
        .values(agent_ref=None)
        .execution_options(synchronize_session=False)
    )
    await db.delete(row)
    await db.commit()


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------
async def skill_count(db: AsyncSession, principal: str) -> int:
    return await _count(db, ChatUserSkill, principal)


def _apply_skill(row: ChatUserSkill, data: m.SkillIn) -> None:
    row.name = data.name
    row.name_key = m.name_key(data.name)
    row.description = data.description
    row.content = data.content


async def create_skill(db: AsyncSession, principal: str, data: m.SkillIn) -> ChatUserSkill:
    if await _name_taken(db, ChatUserSkill, principal, m.name_key(data.name)):
        raise NameTaken
    now = utcnow()
    row = ChatUserSkill(principal=principal, created_at=now, updated_at=now)
    _apply_skill(row, data)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _rewrite_own_skill_refs(db: AsyncSession, principal: str, key: str,
                                  new_name: str | None) -> None:
    """Every agent of the caller that attaches the own skill ``key``: the
    reference renamed to ``new_name``, or removed when it is ``None``.
    Read and rewritten in Python (``skills_json`` is ``Text``)."""
    for agent in await list_agents(db, principal):
        refs = ua.skill_refs(agent)
        changed = False
        kept: list[dict[str, str]] = []
        for ref in refs:
            if ref["scope"] == "mine" and m.name_key(ref["name"]) == key:
                changed = True
                if new_name is not None:
                    kept.append({"scope": "mine", "name": new_name})
            else:
                kept.append(ref)
        if changed:
            agent.skills_json = _dump(kept)
            agent.updated_at = utcnow()


async def replace_skill(db: AsyncSession, row: ChatUserSkill, data: m.SkillIn) -> ChatUserSkill:
    if await _name_taken(db, ChatUserSkill, row.principal, m.name_key(data.name),
                         except_id=row.id):
        raise NameTaken
    old_key, old_name = row.name_key, row.name
    _apply_skill(row, data)
    row.updated_at = utcnow()
    if old_name != row.name:
        # The references follow a rename (also one of case only).
        await _rewrite_own_skill_refs(db, row.principal, old_key, row.name)
    await db.commit()
    await db.refresh(row)
    return row


async def delete_skill(db: AsyncSession, row: ChatUserSkill) -> None:
    await _rewrite_own_skill_refs(db, row.principal, row.name_key, None)
    await db.delete(row)
    await db.commit()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
async def _by_ids(db: AsyncSession, model: Any, principal: str, ids: list[str]) -> list[Any]:
    rows: list[Any] = []
    for chunk in _chunks(ids):
        rows += (await db.execute(
            select(model).where(model.principal == principal, model.id.in_(chunk))
        )).scalars().all()
    return sorted(rows, key=lambda r: (r.name_key, r.id))


async def export_bundle(db: AsyncSession, principal: str, *, agent_ids: list[str] | None,
                        skill_ids: list[str] | None) -> dict[str, Any]:
    """The caller's own agents and skills as a ``webchat-bundle/1``: all of
    them without a selection; with one, the selected rows plus the own skills
    the selected agents attach (an exported agent keeps its skills)."""
    if agent_ids is None and skill_ids is None:
        agents = await list_agents(db, principal)
        skills = await list_skills(db, principal)
    else:
        agents = await _by_ids(db, ChatUserAgent, principal, agent_ids or [])
        skills = await _by_ids(db, ChatUserSkill, principal, skill_ids or [])
        wanted = {m.name_key(r["name"]) for a in agents for r in ua.skill_refs(a)
                  if r["scope"] == "mine"} - {s.name_key for s in skills}
        if wanted:
            skills += [s for s in await list_skills(db, principal) if s.name_key in wanted]
            skills.sort(key=lambda r: (r.name_key, r.id))
    return {
        "format": m.BUNDLE_FORMAT,
        "exported_at": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agents": [
            {"name": a.name, "description": a.description or "",
             "instructions": a.instructions, "skills": ua.skill_refs(a),
             "consults": ua.consult_names(a), "model": a.model_name,
             "deep": bool(a.deep)}
            for a in agents
        ],
        "skills": [
            {"name": s.name, "description": s.description or "", "content": s.content}
            for s in skills
        ],
    }


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
def _section() -> dict[str, Any]:
    return {"created": 0, "replaced": 0, "skipped": 0, "renamed": []}


@dataclass
class _Import:
    principal: str
    on_conflict: str
    report: dict[str, Any] = field(default_factory=lambda: {
        "agents": _section(), "skills": _section(), "dropped": [], "errors": []})

    def error(self, item: str, field_name: str, rule: str) -> None:
        self.report["errors"].append({"item": item, "field": field_name, "rule": rule})

    def drop(self, item: str, field_name: str, name: str | None) -> None:
        self.report["dropped"].append({"item": item, "field": field_name, "name": name})


def _validated(state: _Import, model: Any, kind: str, raw: Any, index: int) -> tuple[str, Any]:
    label = m.item_label(kind, raw, index)
    if not isinstance(raw, dict):
        state.error(label, "item", m.RULE_NOT_OBJECT)
        return label, None
    try:
        return label, model.model_validate(raw)
    except ValidationError as exc:
        for field_name, rule in m.item_errors(exc):
            state.error(label, field_name, rule)
        return label, None


def _target_name(state: _Import, kind: str, label: str, name: str, existing: Any,
                 taken: dict[str, Any], count: int, limit: int) -> tuple[str, str | None]:
    """What to do with an item named ``name``: ``("create", name)``,
    ``("replace", name)``, ``("skip", existing name)`` or ``("error", None)``."""
    section = state.report[kind]
    if existing is not None and state.on_conflict == "skip":
        section["skipped"] += 1
        return "skip", existing.name
    if existing is not None and state.on_conflict == "replace":
        section["replaced"] += 1
        return "replace", name
    target = name
    if existing is not None:
        target = m.renamed(name, set(taken))
        if target is None:
            state.error(label, "name", m.RULE_NO_FREE_NAME)
            return "error", None
    if count >= limit:
        state.error(label, "name", m.RULE_LIMIT)
        return "error", None
    if target != name:
        section["renamed"].append({"from": name, "to": target})
    section["created"] += 1
    return "create", target


async def import_bundle(db: AsyncSession, principal: str, bundle: dict[str, Any], *,
                        on_conflict: str, models: set[str],
                        developer: bool = False) -> dict[str, Any]:
    """Write a bundle's skills, then its agents, per item; the report.

    ``bundle`` has been checked for its format and caps by the route;
    ``models`` are the model ids offered here (another one is dropped to the
    default and reported). For a caller who is not a ``developer``, ``deep``
    and consults of advanced admin agents are dropped and reported. One
    commit at the end.
    """
    state = _Import(principal, on_conflict)
    now = utcnow()
    skills = {s.name_key: s for s in await list_skills(db, principal)}
    agents = {a.name_key: a for a in await list_agents(db, principal)}
    # bundle skill key -> the name it has here after the import
    bundle_skills: dict[str, str] = {}
    seen: set[str] = set()
    count = len(skills)
    for index, raw in enumerate(bundle.get("skills") or []):
        label, data = _validated(state, m.SkillIn, "skill", raw, index)
        if data is None:
            continue
        key = m.name_key(data.name)
        if key in seen:
            state.error(label, "name", m.RULE_DUPLICATE)
            continue
        seen.add(key)
        action, name = _target_name(state, "skills", label, data.name, skills.get(key),
                                    skills, count, m.MAX_SKILLS)
        if action == "error" or name is None:
            continue
        if action == "skip":
            bundle_skills[key] = name
            continue
        if action == "replace":
            row = skills[key]
        else:
            row = ChatUserSkill(principal=principal, created_at=now)
            db.add(row)
            count += 1
        data = data.model_copy(update={"name": name})
        _apply_skill(row, data)
        row.updated_at = now
        skills[row.name_key] = row
        bundle_skills[key] = row.name

    admin_skills = await ua.admin_skill_names(db)
    usable = await ua.chat_agents(db)
    seen = set()
    count = len(agents)
    for index, raw in enumerate(bundle.get("agents") or []):
        label, data = _validated(state, m.AgentIn, "agent", raw, index)
        if data is None:
            continue
        key = m.name_key(data.name)
        if key in seen:
            state.error(label, "name", m.RULE_DUPLICATE)
            continue
        seen.add(key)
        refs = Refs()
        for ref in data.skills:
            ref_key = m.name_key(ref.name)
            if ref.scope == "admin" and ref.name in admin_skills:
                refs.skills.append({"scope": "admin", "name": ref.name})
            elif ref.scope == "mine" and ref_key in bundle_skills:
                refs.skills.append({"scope": "mine", "name": bundle_skills[ref_key]})
            elif ref.scope == "mine" and ref_key in skills:
                refs.skills.append({"scope": "mine", "name": skills[ref_key].name})
            else:
                state.drop(label, "skills", ref.name)
        for name in data.consults:
            if name in usable and (developer or agent_allowed(usable[name], BASIC)):
                refs.consults.append(name)
            else:
                state.drop(label, "consults", name)
        if data.deep:
            if developer:
                refs.deep = True
            else:
                state.drop(label, "deep", None)
        if data.model is not None and data.model not in models:
            state.drop(label, "model", None)
            data = data.model_copy(update={"model": None})
        action, name = _target_name(state, "agents", label, data.name, agents.get(key),
                                    agents, count, m.MAX_AGENTS)
        if action in ("error", "skip") or name is None:
            continue
        if action == "replace":
            row = agents[key]
        else:
            row = ChatUserAgent(principal=principal, created_at=now)
            db.add(row)
            count += 1
        _apply_agent(row, data.model_copy(update={"name": name}), refs)
        row.updated_at = now
        agents[row.name_key] = row
    await db.commit()
    return state.report
