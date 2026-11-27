"""Retention of the model usage ledger (``model_usage``, cost in euros).

The ledger outlives runs and conversations on purpose (monthly sums), so it
has its own clock: ``MODEL_USAGE_RETENTION_DAYS`` (default 400, ``0`` keeps
every row; a non-integer value falls back to the default with a warning that
names the variable only).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from agents.db import ModelUsage
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
