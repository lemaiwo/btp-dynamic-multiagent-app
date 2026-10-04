"""Seeding of the IDE agents and skills: insert at startup, refresh on demand.

``seed_from_file_if_empty`` only seeds an empty database, and existing
deployments are not empty. ``ensure_ide_seed`` (startup) inserts the skills
and agents whose *name does not exist yet* and never touches an existing row,
so admin edits win. Disabled with ``IDE_SEED=false``.

Insert-only also means a landscape keeps the prompts it was first seeded
with. ``refresh_ide_seed`` (``POST /ide/api/admin/seed/refresh``, admin only;
plan decision D7) brings it up to the shipped texts without overwriting an
admin's edit:

- a missing name is inserted (``added``);
- a stored ``instructions`` (agent) or ``content`` (skill) whose SHA-256 is
  listed for that name in ``seed_history.json`` -- the hashes of every text
  this file ever shipped -- is an unedited seed and is replaced by the
  current text (``updated``); a stored text equal to the current one is left
  as it is and not reported;
- any other stored text was edited by an admin and is kept
  (``skipped_edited``).

Only that one text column changes; description, servers, skills, peers,
``enabled`` and every other field stay as the admin left them. The update
is conditional on the text it read, so an admin save in between wins.

``seed_history.json`` lists, per name, the hashes of every shipped version,
the current one included: after a change to ``seed.ide.json`` run
``scripts/ide_seed_history.py`` (``tests/test_ide_seed_refresh.py`` fails
until then), so the next version recognises this one as unedited.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agents.admin import AgentPayload, SkillPayload, _deep_json_or_keep, _or_keep
from agents.db import (
    AgentConfig,
    SessionLocal,
    SkillConfig,
    get_agent_by_name,
    get_skill_by_name,
    upsert_agent,
    upsert_skill,
)

logger = logging.getLogger(__name__)

IDE_SEED_FILE = Path(__file__).with_name("seed.ide.json")
HISTORY_FILE_NAME = "seed_history.json"

_NOOP = {"skills_added": 0, "agents_added": 0}


def _enabled() -> bool:
    return os.environ.get("IDE_SEED", "true").strip().lower() not in {"false", "0", "no", "off"}


def text_hash(text: str) -> str:
    """The hash ``seed_history.json`` stores for a seeded text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _insert_skill(session: AsyncSession, skill: SkillPayload) -> None:
    await upsert_skill(
        session,
        name=skill.name,
        description=skill.description,
        content=skill.content,
        commit=False,
    )


async def _insert_agent(session: AsyncSession, payload: AgentPayload) -> None:
    """Insert one seed agent; ``ValueError`` when the config is refused."""
    await upsert_agent(
        session,
        name=payload.name,
        description=payload.description,
        instructions=payload.instructions,
        mcp_servers=payload.to_servers_list(),
        skills=payload.skills,
        peers=_or_keep(payload.peers),
        enabled=payload.enabled,
        expose_chat=payload.expose_chat,
        expose_api=payload.expose_api,
        api_slug=payload.api_slug,
        run_as_principal=payload.run_as_principal,
        run_prompt=payload.run_prompt,
        run_timeout_seconds=payload.run_timeout_seconds,
        model_name=_or_keep(payload.model_name),
        commit=False,
        deep_json=_deep_json_or_keep(payload.deep),
    )


def _skills(data: dict) -> list[SkillPayload]:
    out = []
    for entry in data.get("skills", []):
        try:
            out.append(SkillPayload.model_validate(entry))
        except Exception as e:
            logger.warning("Skipping invalid IDE seed skill %r: %s", entry, e)
    return out


def _agents(data: dict) -> list[AgentPayload]:
    out = []
    for entry in data.get("agents", []):
        try:
            out.append(AgentPayload.model_validate(entry))
        except Exception as e:
            logger.warning("Skipping invalid IDE seed agent %r: %s", entry, e)
    return out


async def ensure_ide_seed(path: Path) -> dict:
    if not _enabled():
        logger.info("IDE_SEED disabled; skipping IDE seed")
        return dict(_NOOP)
    if not path.exists():
        logger.info("No IDE seed file at %s", path)
        return dict(_NOOP)
    try:
        data = json.loads(path.read_text())
    except Exception:
        logger.exception("Failed to read IDE seed file %s", path)
        return dict(_NOOP)

    skills_added = agents_added = 0
    async with SessionLocal() as session:
        # Skills first, so seeded agents can reference them.
        for skill in _skills(data):
            if await get_skill_by_name(session, skill.name) is not None:
                continue
            await _insert_skill(session, skill)
            skills_added += 1

        for payload in _agents(data):
            if await get_agent_by_name(session, payload.name) is not None:
                continue
            try:
                await _insert_agent(session, payload)
            except ValueError as e:
                logger.warning("Skipping invalid IDE seed agent %r: %s", payload.name, e)
                continue
            agents_added += 1
        if skills_added or agents_added:
            await session.commit()
        else:
            await session.rollback()
    return {"skills_added": skills_added, "agents_added": agents_added}


# --- refresh ------------------------------------------------------------------


def _read_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read the IDE {what} {path.name}: "
                         f"{exc.__class__.__name__}") from exc


def _load_history(path: Path) -> dict[str, dict[str, frozenset[str]]]:
    """``{"agents": {name: hashes}, "skills": {name: hashes}}``. A missing
    file is an empty history: every existing row then counts as edited,
    which is the safe side. A malformed one is a ``ValueError``."""
    if not path.exists():
        logger.warning("No IDE seed history at %s: existing rows count as edited", path)
        return {"agents": {}, "skills": {}}
    raw = _read_json(path, "seed history")
    if not isinstance(raw, dict):
        raise ValueError("the IDE seed history is not an object")
    out: dict[str, dict[str, frozenset[str]]] = {}
    for kind in ("agents", "skills"):
        table = raw.get(kind, {})
        if not isinstance(table, dict) or not all(
            isinstance(hashes, list) and all(isinstance(h, str) for h in hashes)
            for hashes in table.values()
        ):
            raise ValueError(f"the IDE seed history has a malformed {kind!r} table")
        out[kind] = {name: frozenset(hashes) for name, hashes in table.items()}
    return out


async def _stored_texts(
    session: AsyncSession, model: type, column: str, names: list[str]
) -> dict[str, str]:
    """``{name: text}`` of the rows that exist (one query per kind)."""
    if not names:
        return {}
    col = getattr(model, column)
    rows = await session.execute(select(model.name, col).where(model.name.in_(names)))
    return {name: text for name, text in rows.all()}


async def _refresh_text(
    session: AsyncSession, model: type, column: str, name: str, stored: str, new: str
) -> bool:
    """Replace ``stored`` by ``new`` only if the row still holds ``stored``."""
    col = getattr(model, column)
    result = await session.execute(
        update(model)
        .where(model.name == name, col == stored)
        .values({column: new})
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def refresh_ide_seed(path: Path, history_path: Path | None = None) -> dict:
    """Bring the IDE seed rows up to ``path`` (see the module docstring).

    Returns ``{"updated": [...], "skipped_edited": [...], "added": [...]}``
    with entries ``"skill:<name>"`` / ``"agent:<name>"`` (skills first, then
    agents, in file order). Runs regardless of ``IDE_SEED``: an admin who
    calls it asks for it. ``ValueError`` when the seed file or the history
    cannot be read; nothing is changed then. Reloading the registry is the
    caller's job (the route does it when something changed).
    """
    data = _read_json(path, "seed file")
    if not isinstance(data, dict):
        raise ValueError("the IDE seed file is not an object")
    history = _load_history(history_path or path.with_name(HISTORY_FILE_NAME))
    skills, agents = _skills(data), _agents(data)
    result: dict[str, list[str]] = {"updated": [], "skipped_edited": [], "added": []}

    async with SessionLocal() as session:
        stored_skills = await _stored_texts(
            session, SkillConfig, "content", [s.name for s in skills])
        stored_agents = await _stored_texts(
            session, AgentConfig, "instructions", [a.name for a in agents])
        plan = [
            ("skill", SkillConfig, "content", s, s.content, stored_skills,
             history["skills"]) for s in skills
        ] + [
            ("agent", AgentConfig, "instructions", a, a.instructions, stored_agents,
             history["agents"]) for a in agents
        ]
        for kind, model, column, payload, new, stored_texts, known in plan:
            label = f"{kind}:{payload.name}"
            stored = stored_texts.get(payload.name)
            if stored is None:
                if kind == "skill":
                    await _insert_skill(session, payload)
                else:
                    try:
                        await _insert_agent(session, payload)
                    except ValueError as e:
                        logger.warning("Skipping invalid IDE seed agent %r: %s",
                                       payload.name, e)
                        continue
                result["added"].append(label)
            elif stored == new:
                continue
            elif text_hash(stored) in known.get(payload.name, frozenset()):
                if await _refresh_text(session, model, column, payload.name, stored, new):
                    result["updated"].append(label)
                else:  # changed since it was read: that save wins
                    result["skipped_edited"].append(label)
            else:
                result["skipped_edited"].append(label)
        if result["updated"] or result["added"]:
            await session.commit()
        else:
            await session.rollback()
    return result
