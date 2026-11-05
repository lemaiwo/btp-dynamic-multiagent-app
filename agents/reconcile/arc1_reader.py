"""Read source-table rows in batches through ARC-1's ``SAPQuery`` tool.

The reconciliation run compares every CSV row with its row in the source
system. This module is the only code that talks to ARC-1 for it, and it keeps
three promises:

* **Only ``SAPQuery``, only our SQL.** The statement is built here by
  :func:`build_sql` from identifiers that must match the spec's
  :data:`~agents.reconcile.spec.IDENT_RE` (checked again here with
  ``fullmatch``, so a trailing newline cannot slip through) and from key
  literals that match :data:`KEY_RE` and are single-quoted with ``'`` doubled.
  Besides that, only the fixed DD03L statement of :meth:`Arc1Reader.preflight`
  is sent. No ``MANDT``: ABAP SQL reads the logon client of the ARC-1 target.
* **The caller's identity.** The reader is handed the agent's ARC-1 MCP server
  and opens one session with ``for_run(None)`` (as ``agents.ide.arc1.
  Arc1Client._send`` does). The server's auth resolves the signed-in tester per
  request, so a background task started inside the tester's tool call keeps
  running as the tester; there is no technical-user fallback. A missing grant
  surfaces as ``sign_in_needed``.
* **Fixed texts.** :class:`Arc1ReadError` carries a stable ``code`` and one of
  a few fixed messages. ARC-1 error texts quote SQL, keys and host names, so
  they are never copied into it; only ARC-1's ``requestId`` (a correlation id)
  is kept. Logs carry the code and the table, never the SQL text or keys.

``maxRows`` is always sent: ARC-1 defaults it to 100, so relying on the
default would silently drop rows from a larger batch. A batch asks for **one
row more** than it has (unique) keys (so it holds at most
:data:`MAX_BATCH_KEYS`). **``key_field`` must be the table's full key** (the
spec and Task 9 enforce this): then a key matches one row at most, an answer
of as many rows as keys is provably not cut at ``maxRows``, and a second row
for one key reaches the reader (``ambiguous_key``) instead of pushing a found
key out of a full answer. An answer cut below the keys asked for (ARC-1's
byte limit) cannot be told from keys that are not there; the run reads such
keys once more on their own before it counts them missing. A short answer is
caught where ARC-1 says so: an envelope with ``truncated``/``hasMore`` true or
a ``total``/``totalRows``/``rowCount`` above the rows returned is ``truncated``.

CHAR keys: ABAP compares CHAR values ignoring trailing blanks, so rows are
matched to the requested keys after ``rstrip()`` and the result is keyed by
the key *as the caller passed it*.

Target selection: a single-target ARC-1 has no target argument; a multi-target
one may. Which property it is is read from the tool's input schema
(:data:`TARGET_PROPERTIES`, case-insensitive) rather than assumed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, NoReturn

from pydantic_ai.exceptions import ModelRetry

from agents.destination import DestinationError
from agents.destination_auth import DestinationUserRequired
from agents.ide.arc1 import Arc1Error, _find, arc1_error_from_text
from agents.ide.diagnose import result_text
from agents.oauth2 import OAuthAuthorizationRequired
from agents.reconcile.spec import IDENT_RE

logger = logging.getLogger(__name__)

QUERY_TOOL = "SAPQuery"
TARGET_PROPERTIES = ("target", "system", "sapsystem", "targetid")
KEY_RE = re.compile(r"^[A-Za-z0-9 _\-./']{1,40}\Z")
TARGET_RE = re.compile(r"^[A-Za-z0-9_.\-]{0,64}\Z")
MAX_ROWS = 10_000  # ARC-1's SAPQuery ceiling for maxRows
# A batch asks for one row more than it has keys, so it holds at most this many.
MAX_BATCH_KEYS = MAX_ROWS - 1
DD03L_MAX_ROWS = 1000
REFUSED_MARKERS = ("policy", "forbidden", "not_allowed", "blocked")
BACKOFF_SECONDS = 0.5

_MESSAGES = {
    "unexpected_result": "ARC-1 returned a query result in a shape the reader does not know.",
    "arc1_failed": "ARC-1 could not run the query. Try again later.",
    "arc1_refused": "ARC-1 refused the query (policy or authorization).",
    "sign_in_needed": "Reading the source system needs the tester to sign in to ARC-1 again.",
    "query_tool_missing": "The ARC-1 server does not offer the SAPQuery tool.",
    "target_required": "The ARC-1 server needs a target system, but none is configured.",
    "target_not_supported": "A target system is configured, but the ARC-1 query tool "
    "takes no target argument.",
    "field_not_in_table": "Some fields the spec reads are not in the source table.",
    "ambiguous_key": "The source table returned more than one row for a key; "
    "the key field is not the full key.",
    "truncated": "ARC-1 returned only part of the rows of a batch.",
    "preflight_refused": "ARC-1 refused the field check on the data dictionary (DD03L). "
    "The field check can be turned off for this source.",
}
_SCHEMA_UNREADABLE = (
    "A target system is configured, but the input schema of the ARC-1 query "
    "tool could not be read."
)


class Arc1ReadError(Exception):
    """A read that did not produce rows; ``code`` is stable, ``message`` fixed."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        fields: list[str] | None = None,
        request_id: str | None = None,
        transport: bool = False,
    ):
        self.code = code
        # The MCP session itself failed (no answer from ARC-1 at all): a new
        # session may work where this one cannot. Never set for an answer.
        self.transport = transport
        self.message = message or _MESSAGES.get(code, "Reading the source system failed.")
        self.fields = list(fields or [])
        self.request_id = request_id
        super().__init__(self.message)


def _check_ident(name: object) -> str:
    if not isinstance(name, str) or not IDENT_RE.fullmatch(name):
        raise ValueError("not an SAP identifier")
    return name


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_sql(table: str, fields: list[str], key_field: str, keys: list[str]) -> str:
    """``SELECT <fields> FROM <table> WHERE <key_field> IN (<keys>)``.

    Identifiers must already be upper-case SAP names; keys must match
    :data:`KEY_RE` (no control characters, no ``%``, ``;`` or brackets). Any
    other input is a ``ValueError`` before a byte is sent.
    """
    _check_ident(table)
    _check_ident(key_field)
    if not fields:
        raise ValueError("no fields to select")
    for f in fields:
        _check_ident(f)
    if not keys:
        raise ValueError("no keys")
    for k in keys:
        if not isinstance(k, str) or not KEY_RE.fullmatch(k):
            raise ValueError("a key holds characters a key literal may not")
    return (
        f"SELECT {', '.join(fields)} FROM {table} "
        f"WHERE {key_field} IN ({', '.join(_literal(k) for k in keys)})"
    )


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    raise Arc1ReadError("unexpected_result")


def _row(obj: object) -> dict[str, str]:
    if not isinstance(obj, dict):
        raise Arc1ReadError("unexpected_result")
    out = {str(k).upper(): _cell(v) for k, v in obj.items()}
    if len(out) != len(obj):  # "a" and "A": which one is the field?
        raise Arc1ReadError("unexpected_result")
    return out


@dataclass(frozen=True)
class QueryAnswer:
    """The rows of a ``SAPQuery`` answer plus what its envelope says about them."""

    rows: list[dict[str, str]] = field(default_factory=list)
    truncated: bool = False
    total: int | None = None

    @property
    def short(self) -> bool:
        """ARC-1 said there are more rows than it returned."""
        return self.truncated or (self.total is not None and self.total > len(self.rows))


def _envelope_total(data: dict[str, Any]) -> int | None:
    for name in ("total", "totalRows", "rowCount"):
        value = data.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def parse_rows(text: str) -> list[dict[str, str]]:
    """Rows of a ``SAPQuery`` answer as ``{FIELD: str}``; see :func:`parse_answer`."""
    return parse_answer(text).rows


def parse_answer(text: str) -> QueryAnswer:
    """A ``SAPQuery`` answer as :class:`QueryAnswer`; field names upper-cased.

    Accepts ``{"columns": [names], "rows": [[...]]}``, ``{"columns":
    [{"name": ...}], "rows": [{...}]}`` and a bare list of objects. Anything
    else -- plain text included -- is ``unexpected_result``: reading a strange
    answer as "no rows" would report every CSV row as missing in the source.
    Values become ``str``: ``None`` is ``""``, numbers and booleans are
    ``str(value)`` (``7``, ``1.5``, ``True``); nested values are refused. Field
    names that collide after upper-casing are refused too. The envelope's
    ``truncated``/``hasMore`` and ``total``/``totalRows``/``rowCount`` are kept.
    """
    try:
        data = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        raise Arc1ReadError("unexpected_result") from None
    if isinstance(data, list):
        return QueryAnswer(rows=[_row(item) for item in data])
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise Arc1ReadError("unexpected_result")
    names: list[str] = []
    for col in data.get("columns") or []:
        if isinstance(col, dict) and isinstance(col.get("name"), str):
            names.append(col["name"].upper())
        elif isinstance(col, str):
            names.append(col.upper())
        else:
            raise Arc1ReadError("unexpected_result")
    if len(set(names)) != len(names):
        raise Arc1ReadError("unexpected_result")
    out: list[dict[str, str]] = []
    for item in data["rows"]:
        if isinstance(item, list):
            if len(item) != len(names):
                raise Arc1ReadError("unexpected_result")
            out.append({n: _cell(v) for n, v in zip(names, item, strict=True)})
        else:
            out.append(_row(item))
    return QueryAnswer(
        rows=out,
        truncated=data.get("truncated") is True or data.get("hasMore") is True,
        total=_envelope_total(data),
    )


def _schema_parts(schema: Any) -> list[dict[str, Any]]:
    """The schema and its ``anyOf``/``allOf``/``oneOf`` branches, one level deep."""
    if not isinstance(schema, dict):
        return []
    parts = [schema]
    for comb in ("anyOf", "allOf", "oneOf"):
        branches = schema.get(comb)
        if isinstance(branches, list):
            parts += [b for b in branches if isinstance(b, dict)]
    return parts


def _tool_attr(tool: Any, *names: str) -> Any:
    for name in names:
        if isinstance(tool, dict):
            if name in tool:
                return tool[name]
        elif hasattr(tool, name):
            return getattr(tool, name)
    return None


def _payload_error(payload: Arc1Error) -> tuple[Arc1ReadError, bool]:
    """Map an ARC-1 error payload to (error, retry?)."""
    rid = payload.extra.get("request_id")
    retryable = payload.extra.get("retryable")
    code = payload.code or ""
    if retryable is False or any(m in code for m in REFUSED_MARKERS):
        return Arc1ReadError("arc1_refused", request_id=rid), False
    return Arc1ReadError("arc1_failed", request_id=rid), retryable is True


def _exception_error(exc: BaseException, target: str) -> tuple[Arc1ReadError, bool]:
    """Map an exception from the MCP client (groups included) to (error, retry?)."""
    if (
        _find(exc, OAuthAuthorizationRequired) is not None
        or _find(exc, DestinationUserRequired) is not None
    ):
        return Arc1ReadError("sign_in_needed"), False
    retry = _find(exc, ModelRetry)
    if retry is not None:
        payload = arc1_error_from_text(QUERY_TOOL, target, str(retry))
        if payload is not None:
            return _payload_error(payload)
        return Arc1ReadError("arc1_failed"), True
    if _find(exc, DestinationError) is not None:
        return Arc1ReadError("arc1_failed"), False  # configuration: a retry cannot fix it
    return Arc1ReadError("arc1_failed", transport=True), True


class Arc1Reader:
    """Batch reads of one source table by its key, through one ARC-1 session.

    Use as ``async with Arc1Reader(server, ...) as r: await r.preflight();
    rows = await r.read(keys)``. ``read`` runs ``preflight`` first if it has
    not run yet, because the target argument comes from the tool schema.
    """

    def __init__(
        self,
        server: Any,
        *,
        table: str,
        key_field: str,
        fields: list[str],
        target: str = "",
        retries: int = 3,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        check_fields: bool = True,
    ):
        self.table = _check_ident(table)
        self.key_field = _check_ident(key_field)
        ordered = [self.key_field, *(_check_ident(f) for f in fields)]
        self.fields = list(dict.fromkeys(ordered))
        target = (target or "").strip()
        if not TARGET_RE.fullmatch(target):
            raise ValueError("not a target name")
        self.target = target
        self.retries = max(1, int(retries))
        # Off for an ARC-1 whose policy refuses DD03L while the table itself is
        # readable; a missing field then shows up as missing values instead.
        self.check_fields = check_fields
        self._server = server
        self._sleep = sleep
        self._run: Any = None
        self._target_args: dict[str, str] | None = None

    async def __aenter__(self) -> Arc1Reader:
        try:
            run = await self._server.for_run(None)
            await run.__aenter__()
        except Exception as exc:  # noqa: BLE001 -- mapped to a fixed code
            err, _ = _exception_error(exc, self.target)
            self._log(err, exc)
            raise err from None
        self._run = run
        return self

    async def __aexit__(self, *exc: Any) -> None:
        run, self._run = self._run, None
        if run is None:
            return
        try:
            await run.__aexit__(*exc)
        except Exception as close_exc:  # noqa: BLE001 -- mapped or logged below
            err, _ = _exception_error(close_exc, self.target)
            self._log(err, close_exc)
            if exc and exc[0] is not None:
                return  # never replace the exception already propagating
            raise err from None

    def _log(self, err: Arc1ReadError, exc: BaseException | None = None) -> None:
        # Code, table and exception type only: SQL text, keys and ARC-1
        # messages (which quote them, and host names) stay out of the log.
        logger.warning(
            "[reconcile] ARC-1 read of %s failed: %s%s",
            self.table, err.code, f" ({type(exc).__name__})" if exc is not None else "",
        )

    def _require_run(self) -> Any:
        if self._run is None:
            raise RuntimeError("Arc1Reader is used outside 'async with'")
        return self._run

    async def preflight(self) -> None:
        """Find ``SAPQuery``, settle the target argument, check the fields exist."""
        run = self._require_run()
        try:
            tools = await run.list_tools()
        except Exception as exc:  # noqa: BLE001 -- mapped to a fixed code
            err, _ = _exception_error(exc, self.target)
            self._log(err, exc)
            raise err from None
        tool = next((t for t in tools or [] if _tool_attr(t, "name") == QUERY_TOOL), None)
        if tool is None:
            err = Arc1ReadError("query_tool_missing")
            self._log(err)
            raise err
        self._target_args = self._resolve_target(
            _tool_attr(tool, "inputSchema", "input_schema") or {}
        )
        if not self.check_fields:
            return
        try:
            text = await self._query(
                "SELECT FIELDNAME FROM DD03L "
                f"WHERE TABNAME = {_literal(self.table)} AND AS4LOCAL = 'A'",
                DD03L_MAX_ROWS,
            )
        except Arc1ReadError as exc:
            if exc.code != "arc1_refused":
                raise
            err = Arc1ReadError("preflight_refused", request_id=exc.request_id)
            self._log(err)
            raise err from None
        answer = self._parse(text)
        if answer.short or len(answer.rows) >= DD03L_MAX_ROWS:
            # A cut-off field list would call existing fields missing.
            logger.warning(
                "[reconcile] DD03L answer for %s may be incomplete; field check skipped",
                self.table,
            )
            return
        present = {row.get("FIELDNAME", "").strip().upper() for row in answer.rows}
        missing = [f for f in self.fields if f not in present]
        if missing:
            err = Arc1ReadError("field_not_in_table", fields=missing)
            self._log(err)
            raise err

    def _resolve_target(self, schema: Any) -> dict[str, str]:
        names: list[str] = []
        required: list[str] = []
        for i, part in enumerate(_schema_parts(schema)):
            props = part.get("properties")
            if isinstance(props, dict):
                names += [str(p) for p in props if str(p) not in names]
            req = part.get("required")
            # Top level and allOf branches bind; an anyOf/oneOf branch may not apply.
            binding = i == 0 or part in (schema.get("allOf") or [])
            if binding and isinstance(req, list):
                required += [str(r) for r in req]
        target_like = sorted(
            (n for n in names if n.lower() in TARGET_PROPERTIES),
            key=lambda n: TARGET_PROPERTIES.index(n.lower()),
        )
        if self.target:
            if not target_like:
                # The schema's property names come from the ARC-1 server, not
                # from customer data, and tell an admin what the tool takes.
                message = (
                    _MESSAGES["target_not_supported"] + " It takes: " + ", ".join(names[:20]) + "."
                    if names
                    else _SCHEMA_UNREADABLE
                )
                err = Arc1ReadError("target_not_supported", message)
                self._log(err)
                raise err
            return {target_like[0]: self.target}
        if any(n in required for n in target_like):
            err = Arc1ReadError("target_required")
            self._log(err)
            raise err
        return {}

    def _parse(self, text: str) -> QueryAnswer:
        try:
            return parse_answer(text)
        except Arc1ReadError as err:
            self._log(err)
            raise

    async def read(self, keys: list[str]) -> dict[str, dict[str, str]]:
        """``{key: row}`` for the keys found; one ``SAPQuery`` per call.

        The caller sizes the batch (at most :data:`MAX_BATCH_KEYS` unique keys).
        """
        for k in keys:
            if not isinstance(k, str) or not KEY_RE.fullmatch(k):
                raise ValueError("a key holds characters a key literal may not")
        # Requested keys by their trimmed form (ABAP CHAR ignores trailing blanks).
        wanted: dict[str, list[str]] = {}
        for k in dict.fromkeys(keys):
            wanted.setdefault(k.rstrip(), []).append(k)
        if not wanted:
            return {}
        if len(wanted) > MAX_BATCH_KEYS:
            raise ValueError("a batch holds at most 9999 keys")
        sql = build_sql(
            self.table, self.fields, self.key_field, [ks[0] for ks in wanted.values()]
        )
        self._require_run()
        if self._target_args is None:
            await self.preflight()
        answer = self._parse(await self._query(sql, len(wanted) + 1))
        if answer.short:
            self._fail("truncated")
        if len(answer.rows) > len(wanted):
            self._fail("ambiguous_key" if self._has_duplicate(answer.rows) else "unexpected_result")
        out: dict[str, dict[str, str]] = {}
        seen: set[str] = set()
        for row in answer.rows:
            if self.key_field not in row:
                self._fail("unexpected_result")
            found = row[self.key_field].rstrip()
            if found in seen:
                self._fail("ambiguous_key")
            if found not in wanted:
                self._fail("unexpected_result")  # a row we did not ask for
            seen.add(found)
            for requested in wanted[found]:
                out[requested] = row
        return out

    def _has_duplicate(self, rows: list[dict[str, str]]) -> bool:
        found = [row.get(self.key_field, "").rstrip() for row in rows]
        return len(set(found)) != len(found)

    def _fail(self, code: str) -> NoReturn:
        err = Arc1ReadError(code)
        self._log(err)
        raise err

    async def _query(self, sql: str, max_rows: int) -> str:
        """One ``SAPQuery`` with retries for transient failures only."""
        run = self._require_run()
        args: dict[str, Any] = {"sql": sql, "maxRows": max_rows, **(self._target_args or {})}
        for attempt in range(1, self.retries + 1):
            cause: BaseException | None = None
            try:
                result = await run.direct_call_tool(QUERY_TOOL, args)
            except Exception as exc:  # noqa: BLE001 -- mapped to a fixed code
                err, retry = _exception_error(exc, self.target)
                cause = exc
            else:
                text = result_text(result)
                payload = arc1_error_from_text(QUERY_TOOL, self.target, text)
                if payload is None:
                    return text
                err, retry = _payload_error(payload)
            if not retry or attempt == self.retries:
                self._log(err, cause)
                raise err from None
            await self._sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
        raise AssertionError("unreachable")  # pragma: no cover
