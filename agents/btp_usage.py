"""The actual AI Core cost of this subaccount per month, from SAP's Usage Data
Management service (cost in euros, task 10).

``GET <destination URL>/reports/v1/monthlySubaccountsCost?fromDate=YYYYMM&toDate=YYYYMM``
through the BTP destination ``BTP_USAGE`` (``USAGE_DESTINATION`` overrides;
``OAuth2ClientCredentials`` on a key of the ``uas`` instance, plan
``reporting-ga-admin``). **That credential sees the whole global account**: the
answer holds every subaccount and service, and a ``subaccountId`` query
parameter is ignored by the service. So this module keeps only rows whose
``subaccountId`` is exactly this subaccount's AND whose ``serviceId`` is AI
Core; nothing else of the answer leaves it (no row, name, id or amount of
another subaccount, nor of a global-account-level row, whose ``subaccountId``
is ``""``).

This subaccount: ``USAGE_SUBACCOUNT_ID``, else ``credentials.subaccountid`` of
the first ``xsuaa`` binding in ``VCAP_SERVICES`` (``VCAP_APPLICATION`` carries
org and space, no subaccount). None of the two: ``no_subaccount``, nothing sent.

**Off unless the destination exists**: no destination service bound, or the
destination service answers 404 for the name, is ``state == "off"``.

Answers (``UsageAnswer.state``): ``ok``, ``off``, ``no_subaccount``,
``destination_error``, ``unauthorized`` (401/403), ``usage_unavailable`` (any
other status, a redirect, a timeout, a connection error), ``too_large`` (over
``MAX_BYTES``), ``not_json``, ``unreadable`` (JSON of a shape this module does
not know). The body of an error status is never read, no redirect is followed,
one deadline (``TIMEOUT_SECONDS``) covers resolve, send and read. A log line
names the step and the code, status or exception class, never the service's
text, a URL (the query names the global account's months, the URL the host)
or a token. ``httpx``'s own request line is held back by the contextvar filter
of :mod:`agents.bitbucket_tools` (``install_request_log_filter`` /
``request_log_quiet``), the one other ``agents`` import besides
:mod:`agents.destination`.

Cached per process: an ``ok`` answer ``CACHE_SECONDS`` (6 h: the service
updates its figures about daily), a failure ``FAIL_CACHE_SECONDS`` (5 min).
With several app instances each reads on its own.

Field names are those of the recorded answer
(``tests/fixtures/btp_usage/monthly_subaccounts_cost.json``, its ``README.md``).
"""

from __future__ import annotations

import asyncio
import decimal
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx

from agents.bitbucket_tools import install_request_log_filter, request_log_quiet
from agents.destination import (
    DestinationNotFound,
    DestinationResolver,
    config_from_environment,
)

logger = logging.getLogger(__name__)

DEFAULT_DESTINATION = "BTP_USAGE"
PATH = "/reports/v1/monthlySubaccountsCost"
# Query parameters as the service takes them (wrong names: a 500 with an empty
# body); the month as ``YYYYMM``.
PARAM_FROM, PARAM_TO = "fromDate", "toDate"
# The answer: {"content": [row, ...]}, no paging (fixture).
FIELD_CONTENT = "content"
FIELD_SUBACCOUNT = "subaccountId"   # the subaccount GUID; "" on global-account rows
FIELD_SERVICE = "serviceId"
FIELD_MONTH = "reportYearMonth"     # a number, 202611
FIELD_COST = "cost"
FIELD_CURRENCY = "currency"
FIELD_METRIC = "measureId"          # "capacity_units" for AI Core
FIELD_USAGE = "usage"               # for AI Core: capacity units
FIELD_ESTIMATED = "estimated"       # true for the open month
AI_CORE_SERVICE_IDS = frozenset({"ai-core"})
CAPACITY_UNITS_METRIC = "capacity_units"

CACHE_SECONDS = 6 * 3600
FAIL_CACHE_SECONDS = 300
TIMEOUT_SECONDS = 20
MAX_BYTES = 4 * 1024 * 1024
MAX_CACHE_ENTRIES = 32
# The month sums: wide enough that adding the rows of a month never rounds.
_SUM = decimal.Context(prec=60)

STATES = frozenset({
    "ok", "off", "no_subaccount", "destination_error", "unauthorized",
    "usage_unavailable", "too_large", "not_json", "unreadable"})

_SUBACCOUNT_RE = re.compile(r"[A-Za-z0-9-]{1,64}", re.ASCII)
_METRIC_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}", re.ASCII)
_CURRENCY_RE = re.compile(r"[A-Z]{3}", re.ASCII)
# Headers of the destination that are not ours to send on a request of our own.
_DROPPED_HEADERS = frozenset({"host", "content-length", "content-type",
                              "transfer-encoding", "cookie"})

# The clock of the cache (tests replace it).
_now = time.monotonic
# A transport for tests; None = httpx's own.
_transport: httpx.AsyncBaseTransport | None = None


@dataclass
class UsageAnswer:
    """``state`` one of :data:`STATES`; ``months`` only for ``ok``:
    ``{"YYYY-MM": {amount, currency, capacity_units, estimated}}``, only months that
    have an AI Core row of this subaccount."""

    state: str
    months: dict[str, dict] = field(default_factory=dict)
    # ``ok`` only: months whose rows could not be one amount (two currencies).
    unreadable_months: frozenset[str] = frozenset()


class _Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_cache: dict[tuple, tuple[float, UsageAnswer]] = {}
_resolvers: dict[tuple, Any] = {}


def reset() -> None:
    """Forget the cache and the resolvers (tests, and nothing else)."""
    _cache.clear()
    _resolvers.clear()


def settings(environ: Any = None) -> str:
    """The destination name: ``USAGE_DESTINATION``, blank = ``BTP_USAGE``."""
    env = os.environ if environ is None else environ
    return str(env.get("USAGE_DESTINATION") or "").strip() or DEFAULT_DESTINATION


def subaccount_id(environ: Any = None) -> str | None:
    """This subaccount's id, or None when it cannot be told (D3).

    A value that does not have the form of an id is no id: it is compared
    with every row's ``subaccountId``, so it must not be blank or odd.
    """
    env = os.environ if environ is None else environ
    override = str(env.get("USAGE_SUBACCOUNT_ID") or "").strip()
    if override:
        return override if _SUBACCOUNT_RE.fullmatch(override) else None
    try:
        services = json.loads(env.get("VCAP_SERVICES") or "{}")
    except (ValueError, TypeError, RecursionError):
        return None
    entries = services.get("xsuaa") if isinstance(services, dict) else None
    if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
        return None
    credentials = entries[0].get("credentials")
    value = credentials.get("subaccountid") if isinstance(credentials, dict) else None
    if isinstance(value, str) and _SUBACCOUNT_RE.fullmatch(value):
        return value
    return None


def _month_param(day: date) -> str:
    return f"{day.year:04d}{day.month:02d}"


def _number(value: Any) -> Decimal | None:
    """A JSON number as the ``Decimal`` of its shortest text, so a month's
    sum is exact (``0.1 + 0.2`` is ``0.3``); a float only at the end."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return Decimal(str(value))


def _month_label(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    year, month = divmod(value, 100)
    if not (1900 <= year <= 9999 and 1 <= month <= 12):
        return None
    return f"{year:04d}-{month:02d}"


def parse(payload: Any, subaccount: str, first: date, last: date) -> dict[str, dict]:
    """The AI Core months of ``subaccount`` between ``first`` and ``last``
    (a month of two currencies left out; see :func:`parse_months`)."""
    return parse_months(payload, subaccount, first, last)[0]


def parse_months(payload: Any, subaccount: str, first: date,
                 last: date) -> tuple[dict[str, dict], frozenset[str]]:
    """``(months, unreadable)``: the AI Core months of ``subaccount`` between
    ``first`` and ``last``, and the labels of the months that cannot be one
    amount (rows in two currencies), which are not in ``months``: only THAT
    month is unreadable, the others stay.

    Raises ``_Refused("unreadable")`` for a shape this module does not know.
    Rows of another subaccount or service are skipped before any of their
    other fields is looked at; only the kept rows are validated field by field.
    Amounts are summed as ``Decimal`` and become floats once, at the end.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get(FIELD_CONTENT), list):
        raise _Refused("unreadable")
    low, high = f"{first.year:04d}-{first.month:02d}", f"{last.year:04d}-{last.month:02d}"
    months: dict[str, dict] = {}
    unreadable: set[str] = set()
    for row in payload[FIELD_CONTENT]:
        if not isinstance(row, dict):
            raise _Refused("unreadable")
        if row.get(FIELD_SUBACCOUNT) != subaccount:
            continue
        if row.get(FIELD_SERVICE) not in AI_CORE_SERVICE_IDS:
            continue
        label = _month_label(row.get(FIELD_MONTH))
        cost = _number(row.get(FIELD_COST))
        usage = _number(row.get(FIELD_USAGE))
        currency = row.get(FIELD_CURRENCY)
        metric = row.get(FIELD_METRIC)
        estimated = row.get(FIELD_ESTIMATED)
        if (label is None or cost is None or usage is None
                or not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency)
                or not isinstance(metric, str) or not _METRIC_RE.fullmatch(metric)
                or not isinstance(estimated, bool)):
            raise _Refused("unreadable")
        if not (low <= label <= high) or label in unreadable:
            continue
        month = months.setdefault(label, {
            "amount": Decimal(0), "currency": currency, "capacity_units": Decimal(0),
            "estimated": False})
        if month["currency"] != currency:
            # Two currencies in one month cannot be one amount.
            unreadable.add(label)
            del months[label]
            continue
        month["amount"] = _SUM.add(month["amount"], cost)
        if metric == CAPACITY_UNITS_METRIC:
            month["capacity_units"] = _SUM.add(month["capacity_units"], usage)
        month["estimated"] = month["estimated"] or estimated
    for month in months.values():
        month["amount"] = float(month["amount"])
        month["capacity_units"] = float(month["capacity_units"])
    return months, frozenset(unreadable)


def _resolver(name: str, cfg: Any) -> Any:
    key = (name, cfg)
    found = _resolvers.get(key)
    if found is None:
        found = DestinationResolver(name, cfg)
        _resolvers[key] = found
    return found


def _target(url: str) -> str:
    """The report URL on the destination's own ``https`` host, or refused."""
    parts = urlsplit(url or "")
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment):
        raise _Refused("destination_error")
    return url.rstrip("/") + PATH


async def _read(response: httpx.Response) -> bytes:
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BYTES:
        raise _Refused("too_large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_BYTES:
            raise _Refused("too_large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _fetch(resolver: Any, first: date, last: date) -> Any:
    """One request; the decoded JSON, or ``_Refused`` with the code."""
    try:
        destination = await resolver.resolve()
    except DestinationNotFound:
        raise _Refused("off") from None
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - any resolve failure is one code
        logger.warning("btp usage: resolve failed (%s)", type(e).__name__)
        raise _Refused("destination_error") from None
    url = _target(destination.url)
    headers = {k: v for k, v in destination.headers.items()
               if k.lower() not in _DROPPED_HEADERS}
    headers["Accept"] = "application/json"
    params = {PARAM_FROM: _month_param(first), PARAM_TO: _month_param(last)}
    install_request_log_filter()
    quiet = request_log_quiet.set(True)
    try:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False,
                                     timeout=TIMEOUT_SECONDS, transport=_transport) as http:
            async with http.stream("GET", url, params=params, headers=headers) as response:
                status = response.status_code
                if status in (401, 403):
                    logger.warning("btp usage: request failed (status %d)", status)
                    # The next call resolves afresh: the token may be the cause.
                    invalidate = getattr(resolver, "invalidate", None)
                    if callable(invalidate):
                        invalidate()
                    raise _Refused("unauthorized")
                if status != 200:
                    logger.warning("btp usage: request failed (status %d)", status)
                    raise _Refused("usage_unavailable")
                body = await _read(response)
    except httpx.HTTPError as e:
        logger.warning("btp usage: request failed (%s)", type(e).__name__)
        raise _Refused("usage_unavailable") from None
    finally:
        request_log_quiet.reset(quiet)
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _Refused("not_json") from None


def _remember(key: tuple, answer: UsageAnswer) -> UsageAnswer:
    seconds = CACHE_SECONDS if answer.state == "ok" else FAIL_CACHE_SECONDS
    if len(_cache) >= MAX_CACHE_ENTRIES and key not in _cache:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (_now() + seconds, answer)
    return answer


async def monthly_cost(first: date, last: date) -> UsageAnswer:
    """AI Core's cost of this subaccount for the months ``first`` .. ``last``
    (the day of either is ignored). Never raises (but for a cancel)."""
    cfg = config_from_environment(os.environ)
    if cfg is None:
        return UsageAnswer("off")
    subaccount = subaccount_id()
    if subaccount is None:
        return UsageAnswer("no_subaccount")
    name = settings()
    key = (name, subaccount, _month_param(first), _month_param(last))
    cached = _cache.get(key)
    if cached is not None and cached[0] > _now():
        return cached[1]
    resolver = _resolver(name, cfg)
    deadline = asyncio.timeout(TIMEOUT_SECONDS)
    try:
        async with deadline:
            payload = await _fetch(resolver, first, last)
        months, unreadable = parse_months(payload, subaccount, first, last)
        if unreadable:
            logger.warning("btp usage: %d month(s) refused (two currencies)", len(unreadable))
        answer = UsageAnswer("ok", months, unreadable)
    except _Refused as refused:
        if refused.code in ("not_json", "unreadable", "too_large"):
            logger.warning("btp usage: answer refused (%s)", refused.code)
        answer = UsageAnswer(refused.code)
    except TimeoutError:
        logger.warning("btp usage: request failed (%s)",
                       "deadline" if deadline.expired() else "TimeoutError")
        answer = UsageAnswer("usage_unavailable")
    except Exception as exc:  # noqa: BLE001 - never raises; a cancel is no Exception
        logger.warning("btp usage: request failed (%s)", type(exc).__name__)
        answer = UsageAnswer("usage_unavailable")
    return _remember(key, answer)
