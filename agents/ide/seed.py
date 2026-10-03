"""Idempotent seeding of the IDE agents and skills.

``seed_from_file_if_empty`` only seeds an empty database, and existing
deployments are not empty. ``ensure_ide_seed`` inserts the skills and agents
whose *name does not exist yet* and never touches an existing row, so admin
edits win. Disabled with ``IDE_SEED=false``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from agents.admin import AgentPayload, SkillPayload, _deep_json_or_keep, _or_keep
from agents.db import (
    SessionLocal,
    get_agent_by_name,
    get_skill_by_name,
    upsert_agent,
    upsert_skill,
)

logger = logging.getLogger(__name__)

_NOOP = {"skills_added": 0, "agents_added": 0}


def _enabled() -> bool:
    return os.environ.get("IDE_SEED", "true").strip().lower() not in {"false", "0", "no", "off"}


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
        for entry in data.get("skills", []):
            try:
                skill = SkillPayload.model_validate(entry)
            except Exception as e:
                logger.warning("Skipping invalid IDE seed skill %r: %s", entry, e)
                continue
            if await get_skill_by_name(session, skill.name) is not None:
                continue
            await upsert_skill(
                session,
                name=skill.name,
                description=skill.description,
                content=skill.content,
                commit=False,
            )
            skills_added += 1

        for entry in data.get("agents", []):
            try:
                payload = AgentPayload.model_validate(entry)
            except Exception as e:
                logger.warning("Skipping invalid IDE seed agent %r: %s", entry, e)
                continue
            if await get_agent_by_name(session, payload.name) is not None:
                continue
            try:
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
            except ValueError as e:
                logger.warning("Skipping invalid IDE seed agent %r: %s", payload.name, e)
                continue
            agents_added += 1
        if skills_added or agents_added:
            await session.commit()
        else:
            await session.rollback()
    return {"skills_added": skills_added, "agents_added": agents_added}
