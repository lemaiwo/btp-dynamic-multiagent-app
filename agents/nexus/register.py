"""The registration push of the agent register and its assignment cache.

The register keeps a list of this app's agents and assigns each one a use
case (an admin there decides which). This module pushes that list and
remembers the answers, so a model call can name its use case without any
I/O (:func:`use_case_for` reads a dict).

What goes into the list (:func:`build_agent_list`): one entry per admin
agent row, then two fixed entries: ``orchestrator`` (the router in front of
the specialists; its instructions are prompt text and are never pushed, only
a fixed description) and ``personal-agent`` (every chat user's own agents,
one entry for all). The register refuses a whole call for one bad entry, so
the list is made safe before it leaves:

* names are stripped (the register trims and would see two equal names);
  the first of two equal names wins, an admin row wins over a fixed entry of
  the same name, a blank name or one over 64 characters is left out;
* a description is cut to 2,000 characters, a model name over 120 is sent
  blank (the register refuses longer ones);
* each skip or blanking is one WARNING naming the agent (admin
  configuration, never a model, description or credential).

A name pushed before and no longer listed is sent once more as inactive, so
the register stops counting it as live. It is sent as the entry last pushed
for it with ``active: false``: the register clears a field a call leaves
out, and would lose the agent's description and model. Pushes go in batches of
:data:`BATCH_SIZE`; a push counts only when every batch is accepted, and a
failed push changes neither the cache nor the memory of what was pushed.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from agents.nexus.context import MAX_ID, ORCHESTRATOR, PERSONAL_AGENT

logger = logging.getLogger(__name__)

AGENT_TYPE = "PYDANTIC"
BATCH_SIZE = 500
MAX_DESCRIPTION = 2000
MAX_MODEL = 120
ASSIGNED = "ASSIGNED"
ORCHESTRATOR_DESCRIPTION = "Routes each request to the right specialist agent."
PERSONAL_DESCRIPTION = "Personal agents of chat users (one entry for all of them)."


class _Client(Protocol):
    async def register(self, agents: list[dict]) -> list[dict] | None: ...


@dataclass(frozen=True)
class Assignment:
    """What the register answered for one agent."""

    use_case: str | None
    resource_group: str | None
    status: str | None


class _State:
    def __init__(self) -> None:
        self.client: _Client | None = None
        self.cache: dict[str, Assignment] = {}
        self.pushed_last: dict[str, dict] = {}
        self.fallback_read = False
        self.fallback: str | None = None
        self.lock: asyncio.Lock | None = None
        self.wanted: asyncio.Event | None = None
        self.pusher: asyncio.Task | None = None
        self.pushing = False


_state = _State()


def _entry(name: str, description: str, model: str, active: bool) -> dict:
    return {"name": name, "type": AGENT_TYPE, "description": description,
            "model": model, "active": active}


def build_agent_list(rows: Iterable[Any]) -> list[dict]:
    """The list to push: the admin rows, then the two fixed entries."""
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        raw = row.name if isinstance(row.name, str) else ""
        name = raw.strip()
        if not name:
            logger.warning("agent register: an agent with a blank name is left out")
            continue
        if len(name) > MAX_ID:
            logger.warning("agent register: agent %s left out: name over %d characters",
                           name[:MAX_ID], MAX_ID)
            continue
        if name in seen:
            logger.warning("agent register: agent %r left out: same name as an earlier one",
                           raw)
            continue
        seen.add(name)
        model = row.model_name.strip() if isinstance(row.model_name, str) else ""
        if len(model) > MAX_MODEL:
            logger.warning("agent register: model of agent %s sent blank: over %d characters",
                           name, MAX_MODEL)
            model = ""
        description = row.description.strip() if isinstance(row.description, str) else ""
        out.append(_entry(name, description[:MAX_DESCRIPTION], model, bool(row.enabled)))
    for name, description in ((ORCHESTRATOR, ORCHESTRATOR_DESCRIPTION),
                              (PERSONAL_AGENT, PERSONAL_DESCRIPTION)):
        if name in seen:
            logger.warning("agent register: the fixed entry %s is left out: an admin agent "
                           "has that name", name)
            continue
        out.append(_entry(name, description, "", True))
    return out


async def _load_rows() -> list[Any]:
    """The admin agent rows (deferred import: no cycle with ``agents.db``)."""
    from agents.db import SessionLocal, list_agents

    async with SessionLocal() as session:
        return await list_agents(session)


def configure(client: _Client) -> None:
    """Use this client for pushes; the fallback use case is read again."""
    _state.client = client
    _state.fallback_read = False


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


async def push_once() -> bool:
    """Push the agent list once; ``True`` when every batch was accepted."""
    client = _state.client
    if client is None:
        return False
    if _state.lock is None:
        _state.lock = asyncio.Lock()
    async with _state.lock:
        try:
            agents = build_agent_list(await _load_rows())
        except Exception as e:  # noqa: BLE001 - its text may hold a database URL
            logger.warning("agent register: registration push failed (%s)", type(e).__name__)
            return False
        listed = {a["name"]: a for a in agents}
        removed = sorted(set(_state.pushed_last) - set(listed))
        agents += [{**_state.pushed_last[n], "active": False} for n in removed]
        answers: list[dict] = []
        batches = [agents[i:i + BATCH_SIZE] for i in range(0, len(agents), BATCH_SIZE)]
        for number, batch in enumerate(batches, 1):
            try:
                answer = await client.register(batch)
            except Exception as e:  # noqa: BLE001 - never raises by contract; be sure
                logger.warning("agent register: registration push failed (%s)",
                               type(e).__name__)
                return False
            if answer is None:
                logger.warning("agent register: registration push failed (batch %d of %d "
                               "not accepted)", number, len(batches))
                return False
            answers.extend(answer)
        cache: dict[str, Assignment] = {}
        for item in answers:
            name = _text(item.get("name"))
            if name:
                cache[name] = Assignment(_text(item.get("useCase")),
                                         _text(item.get("resourceGroup")),
                                         _text(item.get("status")))
        _state.cache = cache
        _state.pushed_last = listed
        return True


def assignment_for(agent: str) -> Assignment | None:
    """The register's last answer for this agent, if any."""
    return _state.cache.get(agent)


def _fallback() -> str | None:
    if not _state.fallback_read:
        from agents.nexus import config
        _state.fallback = config.settings().fallback_use_case
        _state.fallback_read = True
    return _state.fallback


def use_case_for(agent: str) -> str | None:
    """The assigned use case, else ``NEXUS_FALLBACK_USE_CASE``, else ``None``."""
    found = _state.cache.get(agent)
    if found is not None and found.status == ASSIGNED and found.use_case:
        return found.use_case
    return _fallback()


def request_push() -> None:
    """Ask the pusher for a push; requests before it wakes are one push."""
    wanted = _state.wanted
    if wanted is not None and _state.pusher is not None and not _state.pusher.done():
        wanted.set()


def start_pusher(client: _Client) -> None:
    """Start the background pusher on the running loop (one at a time)."""
    configure(client)
    if _state.pusher is not None and not _state.pusher.done():
        return
    _state.wanted = asyncio.Event()
    _state.pusher = asyncio.get_running_loop().create_task(
        _push_loop(), name="agent-register-pusher")


async def _push_loop() -> None:
    wanted = _state.wanted
    assert wanted is not None
    while True:
        await wanted.wait()
        wanted.clear()
        _state.pushing = True
        try:
            await push_once()
        except Exception as e:  # noqa: BLE001 - the pusher must outlive one bad push
            logger.warning("agent register: registration push failed (%s)", type(e).__name__)
        finally:
            _state.pushing = False


async def stop_pusher() -> None:
    """Stop the pusher; a push in progress is cancelled."""
    task = _state.pusher
    _state.pusher = None
    _state.wanted = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def settle(timeout: float = 2.0) -> None:
    """Test helper: wait until no push is wanted or running."""
    async with asyncio.timeout(timeout):
        while _state.pushing or (_state.wanted is not None and _state.wanted.is_set()):
            await asyncio.sleep(0.005)


def reset_for_tests() -> None:
    """Forget everything; a running pusher is cancelled without waiting."""
    global _state
    if _state.pusher is not None and not _state.pusher.done():
        try:
            _state.pusher.cancel()
        except RuntimeError:
            pass  # its loop is already closed
    _state = _State()
