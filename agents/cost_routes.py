"""``/admin/api/model-prices``: the admin's price table of the cost-in-euros
feature (``agents/model_prices.py``).

Every route needs the admin scope. The ``PUT`` body is read raw and
validated in the handler, so a refusal is a fixed ``<field>: <rule>`` text
and never echoes input. A price change reloads nothing: prices are read when
a cost is computed, not by the registry build.

``GET /admin/api/cost/months?months=1..12`` (default 6) answers the months
view (``agents/cost_months.py``): the actual AI Core cost from the usage
service (``agents/btp_usage.py``) next to the ledger's estimate.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from agents import btp_usage, cost_months, model_prices
from agents.auth import current_principal, require_admin
from agents.db import SessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")
BODY_BYTES = 65_536


@router.get("/model-prices", dependencies=[Depends(require_admin)])
async def read_prices() -> dict[str, Any]:
    async with SessionLocal() as session:
        return await model_prices.read_table(session)


@router.put("/model-prices", dependencies=[Depends(require_admin)])
async def save_prices(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > BODY_BYTES:
        raise HTTPException(status_code=413, detail="The body is too large")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(status_code=422, detail="body: a JSON object") from None
    try:
        async with SessionLocal() as session:
            answer = await model_prices.replace_table(session, data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except model_prices.StalePrices:
        raise HTTPException(status_code=409, detail=model_prices.STALE_TEXT) from None
    except model_prices.SettingsRowMissing:
        raise HTTPException(status_code=503, detail=model_prices.MISSING_ROW_TEXT) from None
    # Who and how many, never a rate.
    logger.info("model prices saved by %s: %d models",
                current_principal.get() or "unknown", len(answer["prices"]))
    return answer


@router.get("/cost/months", dependencies=[Depends(require_admin)])
async def cost_months_view(
    months: int = Query(cost_months.DEFAULT_MONTHS, ge=1, le=cost_months.MAX_MONTHS),
) -> dict[str, Any]:
    """Per UTC month, newest first: the actual AI Core cost of this
    subaccount (``null`` with ``actual_status`` saying why) and the estimate
    from the ledger and the price table. The usage service is asked before
    the database session opens, so a slow service holds no connection."""
    windows = cost_months.month_windows(months)
    first, last = cost_months.usage_range(windows)
    usage = await btp_usage.monthly_cost(first, last)
    async with SessionLocal() as session:
        book = await model_prices.load(session)
        counts = await cost_months.counts_by_month(session, windows)
    return cost_months.build_months(book, windows, counts, usage)
