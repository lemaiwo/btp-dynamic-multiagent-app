"""The admin's model price table and the cost maths of the cost-in-euros
feature.

**Why integers.** A rate (capacity units per 1,000 tokens) and the price of
a capacity unit (EUR) are stored as integers in units of 10^-9
(``ModelPrice``, ``OrchestratorConfig.capacity_unit_price``): ``Numeric``
is a float on SQLite, so the tests would not run the exact arithmetic
Postgres and HANA do. The API takes and answers decimal STRINGS with at most
9 decimals (a JSON number is accepted on the way in); the maths runs on
``decimal.Decimal`` and only the final amount becomes a float.

**The formula** (per model, summed)::

    cost = (input' * input_rate + cached * cache_rate + output * output_rate)
           / 1000 * cu_price

with ``input' = max(input - cached, 0)`` and ``cache_rate`` falling back to
``input_rate`` when NULL. A model without a rate is left out and the answer
says ``complete: false``; no CU price means no amount at all (``None``).

**The table is replaced as a whole** (``replace_table``), guarded by
``expected_updated_at`` against ``orchestrator_config.prices_updated_at``,
the one stamp of every rate AND the CU price (the orchestrator row's own
``updated_at`` moves on every instructions save). A refusal is a
``ValueError`` of ``<field>: <rule>``, never a submitted value.
"""

from __future__ import annotations

import decimal
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents import model_usage_store
from agents.db import (
    ModelPrice,
    OrchestratorConfig,
    as_utc,  # naive (SQLite, HANA) means UTC
    begin_exclusive_write,
    has_row_locks,
    next_stamp,  # the strictly increasing stamp of every expected_updated_at
    stamp_moved,
)

SCALE = 10**9
MAX_VALUE = 1_000_000
CURRENCY = "EUR"
MAX_MODELS = 200
MODEL_MAX_CHARS = 128

_AMOUNT_RE = re.compile(r"[0-9]{1,7}(\.[0-9]{1,9})?", re.ASCII)
_STAMP_MAX_CHARS = 40
_BODY_KEYS = ("expected_updated_at", "capacity_unit_price", "prices")
_ROW_KEYS = ("model", "input_rate", "output_rate", "cache_read_rate")
# Room for tokens * rate * price without rounding inside the maths.
_CONTEXT = decimal.Context(prec=60)
_MICRO = Decimal("0.000001")

STALE_TEXT = "The prices were changed meanwhile: reload them"
MISSING_ROW_TEXT = "The settings row is missing: restart the app"


class StalePrices(Exception):
    """The table moved since the client read it (``expected_updated_at``)."""


class SettingsRowMissing(Exception):
    """``orchestrator_config`` has no row 1 (``init_db`` ensures it)."""


def _amount_rule(field: str) -> ValueError:
    return ValueError(f"{field}: a number from 0 to {MAX_VALUE} with at most 9 decimals")


def parse_amount(value: Any, field: str) -> int:
    """A rate or price from the API as an integer of 10^-9 units.

    A JSON number (never a bool, finite) or a plain decimal string
    (``^[0-9]{1,7}(\\.[0-9]{1,9})?$``: no sign, exponent or blank). Between 0
    and ``MAX_VALUE``, at most 9 decimals; anything else is the one fixed
    refusal naming ``field``.
    """
    if isinstance(value, bool):
        raise _amount_rule(field)
    if isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise _amount_rule(field)
        number = Decimal(str(value))  # the shortest text of the float, as JSON wrote it
    elif isinstance(value, str) and _AMOUNT_RE.fullmatch(value):
        number = Decimal(value)
    else:
        raise _amount_rule(field)
    exponent = number.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -9 or number < 0 or number > MAX_VALUE:
        raise _amount_rule(field)
    return int(number.scaleb(9, context=_CONTEXT))


def amount_text(n: int) -> str:
    """An integer of 10^-9 units as the API's decimal string (``"2"``, ``"0.5"``)."""
    return format(Decimal(n).scaleb(-9, context=_CONTEXT).normalize(context=_CONTEXT), "f")


@dataclass(frozen=True)
class PriceBook:
    """What ``cost`` needs: the CU price and ``(input, output, cache_read)``
    rates per model, all integers of 10^-9 (``cache_read`` None = input)."""

    cu_price: int | None
    rates: dict[str, tuple[int, int, int | None]]


async def load(session: AsyncSession) -> PriceBook:
    rows = (await session.execute(select(ModelPrice))).scalars()
    rates = {r.model: (r.input_rate, r.output_rate, r.cache_read_rate) for r in rows}
    settings = await session.get(OrchestratorConfig, 1)
    return PriceBook(cu_price=settings.capacity_unit_price if settings else None, rates=rates)


def cost(book: PriceBook, by_model: dict[str, dict[str, int]]) -> dict | None:
    """The euros of a usage ``{model: {input_tokens, output_tokens,
    cache_read_tokens, requests}}``.

    ``None`` without a CU price or without any answered request; otherwise
    ``{amount, currency, complete}``, ``complete`` false when a model that
    answered has no rate (its tokens are then not in ``amount``).

    Counts are clamped before pricing (input and output to >= 0, cached to
    ``[0, input]``): a cached count is part of the input, so more cached than
    input is a reading error, never a charge. ``amount`` is rounded once, at
    the end, to 6 decimals half up (``ROUND_HALF_UP``), in a precision wide
    enough for any count, so no size of count raises ``InvalidOperation``.
    """
    if book.cu_price is None:
        return None
    used = {m: u for m, u in by_model.items() if int(u.get("requests") or 0) > 0}
    if not used:
        return None
    scale = Decimal(SCALE)
    total_cu = Decimal(0)
    complete = True
    for model, usage in used.items():
        rate = book.rates.get(model)
        if rate is None:
            complete = False
            continue
        input_rate, output_rate, cache_rate = rate
        if cache_rate is None:
            cache_rate = input_rate
        tokens_in = max(int(usage.get("input_tokens") or 0), 0)
        cached = min(max(int(usage.get("cache_read_tokens") or 0), 0), tokens_in)
        tokens_out = max(int(usage.get("output_tokens") or 0), 0)
        units = _CONTEXT.add(
            _CONTEXT.add(
                _CONTEXT.multiply(Decimal(tokens_in - cached), Decimal(input_rate)),
                _CONTEXT.multiply(Decimal(cached), Decimal(cache_rate)),
            ),
            _CONTEXT.multiply(Decimal(tokens_out), Decimal(output_rate)),
        )
        total_cu = _CONTEXT.add(total_cu, _CONTEXT.divide(units, _CONTEXT.multiply(scale, 1000)))
    euros = _CONTEXT.multiply(total_cu, _CONTEXT.divide(Decimal(book.cu_price), scale))
    # Digits before the point plus the 6 kept: quantize refuses a result
    # longer than its context's precision.
    wide = decimal.Context(prec=max(_CONTEXT.prec, euros.adjusted() + 8))
    amount = euros.quantize(_MICRO, rounding=ROUND_HALF_UP, context=wide)
    return {"amount": float(amount), "currency": CURRENCY, "complete": complete}


async def costs_for(
    session: AsyncSession,
    source: str,
    ids: list[str],
    *,
    by: Literal["ref", "parent"] = "ref",
    book: PriceBook | None = None,
) -> dict[str, dict | None]:
    """``{id: cost}`` for the run answers (``cost``: ``None`` or ``{amount,
    currency, complete}``). Without a CU price every id is ``None`` and the
    ledger is not read; pass ``book`` to price several reads of one answer
    with one load."""
    if book is None:
        book = await load(session)
    if book.cu_price is None:
        return dict.fromkeys(ids)
    reader = (model_usage_store.counts_by_parent if by == "parent"
              else model_usage_store.counts_by_ref)
    counts = await reader(session, source, ids)
    return {i: cost(book, counts.get(i, {})) for i in ids}


def _stamp_text(value: datetime | None) -> str | None:
    moment = as_utc(value)
    return moment.isoformat() if moment else None


async def read_table(session: AsyncSession) -> dict[str, Any]:
    """The ``GET`` answer: the CU price, the rates by model, the ledger's
    models without a rate and the table's stamp."""
    rows = list((await session.execute(select(ModelPrice).order_by(ModelPrice.model))).scalars())
    settings = await session.get(OrchestratorConfig, 1)
    priced = {r.model for r in rows}
    seen = await model_usage_store.seen_models(session)
    cu_price = settings.capacity_unit_price if settings else None
    return {
        "currency": CURRENCY,
        "capacity_unit_price": None if cu_price is None else amount_text(cu_price),
        "prices": [
            {
                "model": r.model,
                "input_rate": amount_text(r.input_rate),
                "output_rate": amount_text(r.output_rate),
                "cache_read_rate": (
                    None if r.cache_read_rate is None else amount_text(r.cache_read_rate)),
            }
            for r in rows
        ],
        "unpriced_models": sorted(set(seen) - priced),
        "updated_at": _stamp_text(settings.prices_updated_at) if settings else None,
    }


def _model_name(value: Any, field: str) -> str:
    """A model name exactly as the ledger stores it: the collector strips
    the name and the deployment names are NFC, so a name with edge
    whitespace or in another normal form would never match a ledger row."""
    rule = ValueError(f"{field}: 1 to {MODEL_MAX_CHARS} characters without control characters")
    if not isinstance(value, str) or not 1 <= len(value) <= MODEL_MAX_CHARS:
        raise rule
    if any(unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Zl", "Zp") for ch in value):
        raise rule
    if value != value.strip() or unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{field}: no leading or trailing whitespace, in Unicode NFC")
    return value


def _expected(value: Any) -> datetime | None:
    if value is None:
        return None
    rule = ValueError("expected_updated_at: a timestamp as the API answers it, or null")
    if not isinstance(value, str) or len(value) > _STAMP_MAX_CHARS:
        raise rule
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return as_utc(moment)
    except (ValueError, OverflowError):
        raise rule from None


_Row = tuple[str, int, int, int | None]


def validate(data: Any) -> tuple[datetime | None, int | None, list[_Row]]:
    """The whole ``PUT`` body, checked before anything is read or written:
    ``(expected_updated_at, cu_price, rows)``, or a ``ValueError`` of a fixed
    ``<field>: <rule>`` that never holds a submitted value (the route's only
    422)."""
    if not isinstance(data, dict):
        raise ValueError("body: a JSON object")
    if set(data) - set(_BODY_KEYS):
        raise ValueError("body: unknown key")
    if "expected_updated_at" not in data:
        raise ValueError("expected_updated_at: required (null for a table never saved)")
    if "capacity_unit_price" not in data:
        raise ValueError("capacity_unit_price: required (null for no price)")
    if "prices" not in data:
        raise ValueError("prices: required")
    expected = _expected(data["expected_updated_at"])
    raw_cu = data["capacity_unit_price"]
    cu_price = None if raw_cu is None else parse_amount(raw_cu, "capacity_unit_price")
    prices = data["prices"]
    if not isinstance(prices, list) or len(prices) > MAX_MODELS:
        raise ValueError(f"prices: a list of at most {MAX_MODELS} rows")
    rows: list[_Row] = []
    seen: set[str] = set()
    for i, row in enumerate(prices):
        at = f"prices[{i}]"
        if not isinstance(row, dict):
            raise ValueError(f"{at}: an object")
        if set(row) - set(_ROW_KEYS):
            raise ValueError(f"{at}: unknown key")
        for key in ("model", "input_rate", "output_rate"):
            if key not in row:
                raise ValueError(f"{at}.{key}: required")
        model = _model_name(row["model"], f"{at}.model")
        if model in seen:
            raise ValueError(f"{at}.model: a model of an earlier row")
        seen.add(model)
        cache = row.get("cache_read_rate")
        rows.append((
            model,
            parse_amount(row["input_rate"], f"{at}.input_rate"),
            parse_amount(row["output_rate"], f"{at}.output_rate"),
            None if cache is None else parse_amount(cache, f"{at}.cache_read_rate"),
        ))
    return expected, cu_price, rows


async def replace_table(session: AsyncSession, data: Any) -> dict[str, Any]:
    """Replace every rate and the CU price, or refuse.

    ``ValueError`` (a fixed ``<field>: <rule>``) before anything is read;
    ``StalePrices`` when the stamp moved: ``expected_updated_at`` null is
    the stamp of a table never saved, so null on a saved table is stale too
    (unlike the OData routes, where null asks for no check);
    ``SettingsRowMissing`` without the orchestrator row. Lock, compare and
    write are one transaction: ``BEGIN IMMEDIATE`` on SQLite, the
    orchestrator row ``FOR UPDATE`` on Postgres and HANA.
    """
    expected, cu_price, rows = validate(data)
    await begin_exclusive_write(session)
    query = select(OrchestratorConfig).where(OrchestratorConfig.id == 1)
    if has_row_locks(session):
        query = query.with_for_update()
    settings = (
        await session.execute(query.execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if settings is None:
        await session.rollback()
        raise SettingsRowMissing()
    stored = settings.prices_updated_at
    stale = stored is not None if expected is None else stamp_moved(stored, expected)
    if stale:
        await session.rollback()
        raise StalePrices()
    stamp = next_stamp(stored)
    await session.execute(delete(ModelPrice))
    session.add_all(
        ModelPrice(model=model, input_rate=i, output_rate=o, cache_read_rate=c, updated_at=stamp)
        for model, i, o, c in rows
    )
    settings.capacity_unit_price = cu_price
    settings.prices_updated_at = stamp
    await session.commit()
    return await read_table(session)
