"""The agent tools of the data catalogue: ``find_data_source`` and
``list_data_sources``.

``registry.build_orchestrator`` attaches one toolset to every specialist
whose entries hold at least one usable catalogue source. The scope is fixed
at build: that specialist's own sources, never an argument and never another
agent's. The turn's tier is read per call (``agents.access.effective_tier``):
an advanced agent's scope is empty in a basic turn.

The answers are catalogue text (names, labels, the admin's hints), returned
as data. Nothing is read from SAP here.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai.toolsets import FunctionToolset

from agents.access import effective_tier
from agents.data_catalog.search import QUESTION_MAX, find_sources, list_sources

NO_MATCH_HINT = "no source matches; say what data you cannot reach"
EMPTY_QUESTION_HINT = "ask with the user's question in a few words"
MAX_CANDIDATES = 5
TOOL_NAMES = ("find_data_source", "list_data_sources")


def data_catalog_toolset(
    *, sources: frozenset[tuple[str, str]], scope_tier: str
) -> FunctionToolset[Any]:
    """The two tools over ``sources``, the calling agent's own."""
    scope = frozenset(sources)

    async def find_data_source(question: str) -> dict[str, Any]:
        """Find the SAP data source (service, entity set or operation) that can
        answer a question. Call it before searching operations when you do not
        know which source holds the data. Answers at most five candidates with
        their purpose, keys and related entities; an empty list means none of
        your sources holds that data."""
        from agents.db import SessionLocal

        text = " ".join(str(question or "").split())[:QUESTION_MAX]
        if not text:
            return {"candidates": [], "hint": EMPTY_QUESTION_HINT}
        async with SessionLocal() as db:
            found = await find_sources(
                db,
                text,
                allowed_sources=set(scope),
                tier=effective_tier(),
                scope_tier=scope_tier,
                k=MAX_CANDIDATES,
            )
        if not found:
            return {"candidates": [], "hint": NO_MATCH_HINT}
        return {"candidates": found}

    async def list_data_sources() -> dict[str, Any]:
        """List the SAP data sources you can reach, with what each is for. Use
        it to answer "what can you access?"."""
        from agents.db import SessionLocal

        async with SessionLocal() as db:
            listed = await list_sources(
                db, allowed_sources=set(scope), tier=effective_tier(), scope_tier=scope_tier
            )
        return {"sources": listed}

    tools: FunctionToolset[Any] = FunctionToolset(id="data-catalog")
    tools.add_function(find_data_source, takes_ctx=False)
    tools.add_function(list_data_sources, takes_ctx=False)
    return tools
