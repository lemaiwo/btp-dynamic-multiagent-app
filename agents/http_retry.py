"""One retry after a rate-limit or transient refusal, honouring ``Retry-After``.

Shared by the in-process connector toolsets (Gmail, Slack, NVD). Each of them
makes several small requests per tool call, so a single 429 mid-listing used
to fail the whole listing; one bounded retry turns that into a short pause.
Deliberately not a general retry policy: one attempt, a capped wait, and the
second response is returned as-is for the caller's normal error handling.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

# Statuses that mean "not now" rather than "no". 403 is included because NVD
# answers a blown rate limit with 403, not 429.
DEFAULT_RETRY_STATUSES = (429,)

# Never sleep longer than this on a server's say-so, however large the header.
MAX_WAIT_SECONDS = 30.0


def retry_after_seconds(
    response: httpx.Response, default: float, *, max_wait: float = MAX_WAIT_SECONDS
) -> float:
    """How long ``Retry-After`` asks us to wait, bounded; ``default`` if absent.

    Only the delta-seconds form is parsed. The HTTP-date form is rare on these
    APIs and getting it wrong is worse than waiting the default.
    """
    raw = (response.headers.get("Retry-After") or "").strip()
    try:
        wait = float(raw) if raw else default
    except ValueError:
        wait = default
    return max(0.0, min(wait, max_wait))


async def retry_once(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    statuses: tuple[int, ...] = DEFAULT_RETRY_STATUSES,
    backoff: float = 1.0,
    max_wait: float = MAX_WAIT_SECONDS,
    what: str = "request",
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> httpx.Response:
    """Call ``send``; on a status in ``statuses`` wait and call it once more.

    The second response is returned whatever its status, so the caller's
    ``raise_for_status`` (or its own status handling) still applies.
    """
    response = await send()
    if response.status_code not in statuses:
        return response
    wait = retry_after_seconds(response, backoff, max_wait=max_wait)
    logger.info(
        "%s answered %s; retrying once after %.1fs", what, response.status_code, wait
    )
    await sleep(wait)
    return await send()
