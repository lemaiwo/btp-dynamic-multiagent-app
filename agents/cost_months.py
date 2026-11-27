"""The months view of the cost in euros: the model usage ledger summed per UTC
month and priced with the admin's price table, next to the actual AI Core
cost the usage service reports (``agents/btp_usage.py``).

A month is a ``created_at`` range ``[first day 00:00 UTC, first day of the
next month)``, read with ONE grouped statement per month (``source``,
``model``; string columns only, so it groups on HANA too) and no SQL date
function, so the same statement runs on SQLite, Postgres and HANA (plan D7).
At most 12 months, so at most 12 statements.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents import model_prices
from agents.db import LEDGER_SOURCES, ModelUsage

MAX_MONTHS = 12
DEFAULT_MONTHS = 6
_COUNT_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "requests")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def month_windows(count: int, now: datetime | None = None) -> list[tuple[str, datetime, datetime]]:
    """``count`` months ``(label "YYYY-MM", start, end)``, newest (the current
    UTC month) first; ``start`` inclusive, ``end`` exclusive, both aware UTC."""
    moment = (now or _utcnow()).astimezone(timezone.utc)
    year, month = moment.year, moment.month
    windows = []
    for _ in range(count):
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
        end = datetime(end_year, end_month, 1, tzinfo=timezone.utc)
        windows.append((f"{year:04d}-{month:02d}", start, end))
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return windows


async def counts_by_month(
    session: AsyncSession, windows: list[tuple[str, datetime, datetime]]
) -> dict[str, dict[str, dict[str, dict[str, int]]]]:
    """``{label: {source: {model: {input_tokens, output_tokens,
    cache_read_tokens, requests}}}}``, one grouped statement per window."""
    result: dict[str, dict[str, dict[str, dict[str, int]]]] = {}
    for label, start, end in windows:
        rows = await session.execute(
            select(
                ModelUsage.source,
                ModelUsage.model,
                func.sum(ModelUsage.input_tokens),
                func.sum(ModelUsage.output_tokens),
                func.sum(ModelUsage.cache_read_tokens),
                func.sum(ModelUsage.requests),
            )
            .where(ModelUsage.created_at >= start, ModelUsage.created_at < end)
            .group_by(ModelUsage.source, ModelUsage.model)
        )
        month: dict[str, dict[str, dict[str, int]]] = {}
        for source, model, *sums in rows.all():
            month.setdefault(source, {})[model] = {
                key: int(value or 0) for key, value in zip(_COUNT_KEYS, sums)}
        result[label] = month
    return result


def _merged(by_source: dict[str, dict[str, dict[str, int]]]) -> dict[str, dict[str, int]]:
    merged: dict[str, dict[str, int]] = {}
    for models in by_source.values():
        for model, counts in models.items():
            into = merged.setdefault(model, dict.fromkeys(_COUNT_KEYS, 0))
            for key in _COUNT_KEYS:
                into[key] += counts.get(key, 0)
    return merged


def _actual(usage: Any, label: str) -> tuple[dict | None, str]:
    if usage.state != "ok":
        return None, usage.state
    month = usage.months.get(label)
    if month is None:
        return None, "no_data"
    return {key: month[key] for key in ("amount", "currency", "capacity_units",
                                        "estimated")}, "ok"


def build_months(
    book: model_prices.PriceBook,
    windows: list[tuple[str, datetime, datetime]],
    counts: dict[str, dict[str, dict[str, dict[str, int]]]],
    usage: Any,
) -> dict[str, Any]:
    """The answer of ``GET /admin/api/cost/months``. ``usage`` is a
    :class:`agents.btp_usage.UsageAnswer` (only ``state`` and ``months`` are
    read)."""
    months = []
    for label, _start, _end in windows:
        by_source_counts = counts.get(label, {})
        merged = _merged(by_source_counts)
        by_source: dict[str, float | None] = {}
        for source in LEDGER_SOURCES:
            priced = model_prices.cost(book, by_source_counts.get(source, {}))
            by_source[source] = priced["amount"] if priced else None
        by_model = []
        for model in sorted(merged):
            priced = model_prices.cost(book, {model: merged[model]})
            by_model.append({"model": model, **merged[model],
                             "amount": priced["amount"] if priced and priced["complete"]
                             else None})
        actual, status = _actual(usage, label)
        months.append({
            "month": label,
            "actual": actual,
            "actual_status": status,
            "estimated": model_prices.cost(book, merged),
            "by_source": by_source,
            "by_model": by_model,
        })
    return {"usage_service": usage.state, "currency": model_prices.CURRENCY,
            "months": months}


def usage_range(windows: list[tuple[str, datetime, datetime]]) -> tuple[date, date]:
    """The first days of the oldest and the newest month, for the usage service."""
    return windows[-1][1].date(), windows[0][1].date()
