"""Retention and reads of the model usage ledger (``model_usage``, cost in
euros).

The ledger outlives runs and conversations on purpose (monthly sums), so it
has its own clock: ``MODEL_USAGE_RETENTION_DAYS`` (default 400, ``0`` keeps
every row; a non-integer value falls back to the default with a warning that
names the variable only).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.db import ModelUsage

if TYPE_CHECKING:  # model_prices imports this module (seen_models)
    from agents.model_prices import PriceBook
from agents.ide.store import env_int

MODEL_USAGE_RETENTION_DAYS = env_int("MODEL_USAGE_RETENTION_DAYS", 400, minimum=0)
MODEL_USAGE_MAX_RETENTION_DAYS = 36_500


async def purge_model_usage(session: AsyncSession, days: int) -> int:
    """Delete ledger rows older than ``days``; returns how many.

    ``days < 1`` (or a non-integer) deletes nothing: 0 switches retention off
    and a misread setting must not empty the ledger. One plain DELETE, no
    RETURNING, so it runs on SQLite, Postgres and HANA. COMMITS the session
    it is given: pass one of its own.
    """
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        return 0
    days = min(days, MODEL_USAGE_MAX_RETENTION_DAYS)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await session.execute(
        delete(ModelUsage)
        .where(ModelUsage.created_at < cutoff)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return int(result.rowcount or 0)


async def seen_models(session: AsyncSession, limit: int = 500) -> list[str]:
    """The distinct model names the ledger holds, at most ``limit`` of them
    (the price table's "models without a rate": bounded, not paged).
    ``model`` is a ``String`` column, so ``DISTINCT`` runs on HANA too."""
    result = await session.execute(select(ModelUsage.model).distinct().limit(limit))
    return [name for (name,) in result.all()]


# Ids per grouped ledger read, as ``workflow_runs_usage`` (bounded ``IN``).
LEDGER_ID_BATCH = 500

Counts = dict[str, dict[str, int]]


async def _counts_by(
    session: AsyncSession, source: str, ids: list[str], key_col: Any
) -> dict[str, Counts]:
    """``{id: {model: {input_tokens, output_tokens, cache_read_tokens,
    requests}}}`` of the ledger rows of ``source`` whose ``key_col`` is one
    of ``ids``: one grouped statement per ``LEDGER_ID_BATCH`` ids, never one
    per id. Grouped on String columns, summed Integer ones (HANA). An id
    without rows is absent."""
    out: dict[str, Counts] = {}
    unique = list(dict.fromkeys(i for i in ids if i))
    for start in range(0, len(unique), LEDGER_ID_BATCH):
        chunk = unique[start:start + LEDGER_ID_BATCH]
        rows = await session.execute(
            select(
                key_col,
                ModelUsage.model,
                func.sum(ModelUsage.input_tokens),
                func.sum(ModelUsage.output_tokens),
                func.sum(ModelUsage.cache_read_tokens),
                func.sum(ModelUsage.requests),
            )
            .where(ModelUsage.source == source, key_col.in_(chunk))
            .group_by(key_col, ModelUsage.model)
        )
        for key, model, inp, outp, cache, requests in rows.all():
            out.setdefault(key, {})[model] = {
                "input_tokens": int(inp or 0),
                "output_tokens": int(outp or 0),
                "cache_read_tokens": int(cache or 0),
                "requests": int(requests or 0),
            }
    return out


async def counts_by_ref(session: AsyncSession, source: str, ids: list[str]) -> dict[str, Counts]:
    """The ledger's per-model counts of each run (``ref_id``)."""
    return await _counts_by(session, source, ids, ModelUsage.ref_id)


async def counts_by_parent(
    session: AsyncSession, source: str, ids: list[str]
) -> dict[str, Counts]:
    """The ledger's per-model counts summed per parent (``parent_id``: the
    workflow run of its steps, the conversation of its turns)."""
    return await _counts_by(session, source, ids, ModelUsage.parent_id)


async def costs_for(
    session: AsyncSession,
    source: str,
    ids: list[str],
    *,
    by: Literal["ref", "parent"] = "ref",
    book: PriceBook | None = None,
) -> dict[str, dict | None]:
    """``{id: cost}`` for the run answers (``model_prices.cost``: ``None`` or
    ``{amount, currency, complete}``). Without a CU price every id is
    ``None`` and the ledger is not read. Lives here rather than in
    ``model_prices`` because it is a ledger read; pass ``book`` to price
    several reads of one answer with one load."""
    from agents import model_prices  # deferred: model_prices imports this module

    if book is None:
        book = await model_prices.load(session)
    if book.cu_price is None:
        return dict.fromkeys(ids)
    reader = counts_by_parent if by == "parent" else counts_by_ref
    counts = await reader(session, source, ids)
    return {i: model_prices.cost(book, counts.get(i, {})) for i in ids}


async def chat_ledger_refs(session: AsyncSession, conversation_id: str) -> set[str]:
    """The assistant message ids of a conversation that have ledger rows
    (``ref_id`` is a String column: ``DISTINCT`` runs on HANA)."""
    rows = await session.execute(
        select(ModelUsage.ref_id)
        .where(ModelUsage.source == "chat", ModelUsage.parent_id == conversation_id)
        .distinct()
    )
    return {ref for (ref,) in rows.all()}
