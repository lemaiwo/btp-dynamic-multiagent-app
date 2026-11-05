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
default would silently drop rows from a larger batch. A batch asks for exactly
as many rows as it has (unique) keys; on a full key a key matches one row at
most, so ``rows == maxRows`` is a complete answer.

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
from typing import Any

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
KEY_RE = re.compile(r"^[A-Za-z0-9 _\-./']{1,40}$")
TARGET_RE = re.compile(r"^[A-Za-z0-9_.\-]{0,64}$")
MAX_ROWS = 10_000  # ARC-1's SAPQuery ceiling for maxRows
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
}


class Arc1ReadError(Exception):
    """A read that did not produce rows; ``code`` is stable, ``message`` fixed."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        fields: list[str] | None = None,
        request_id: str | None = None,
    ):
        self.code = code
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
    return {str(k).upper(): _cell(v) for k, v in obj.items()}


def parse_rows(text: str) -> list[dict[str, str]]:
    """Rows of a ``SAPQuery`` answer as ``{FIELD: str}``; field names upper-cased.

    Accepts ``{"columns": [names], "rows": [[...]]}``, ``{"columns":
    [{"name": ...}], "rows": [{...}]}`` and a bare list of objects. Anything
    else is ``unexpected_result``: reading a strange answer as "no rows" would
    report every CSV row as missing in the source.
    """
    try:
        data = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        raise Arc1ReadError("unexpected_result") from None
    if isinstance(data, list):
        return [_row(item) for item in data]
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
    out: list[dict[str, str]] = []
    for item in data["rows"]:
        if isinstance(item, list):
            if len(item) != len(names):
                raise Arc1ReadError("unexpected_result")
            out.append({n: _cell(v) for n, v in zip(names, item, strict=True)})
        else:
            out.append(_row(item))
    return out


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
    return Arc1ReadError("arc1_failed"), True


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
        if run is not None:
            await run.__aexit__(*exc)

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
        text = await self._query(
            "SELECT FIELDNAME FROM DD03L "
            f"WHERE TABNAME = {_literal(self.table)} AND AS4LOCAL = 'A'",
            DD03L_MAX_ROWS,
        )
        present = {row.get("FIELDNAME", "").strip().upper() for row in self._parse(text)}
        missing = [f for f in self.fields if f not in present]
        if missing:
            err = Arc1ReadError("field_not_in_table", fields=missing)
            self._log(err)
            raise err

    def _resolve_target(self, schema: Any) -> dict[str, str]:
        props = schema.get("properties") if isinstance(schema, dict) else None
        names = [str(p) for p in props] if isinstance(props, dict) else []
        required = schema.get("required") if isinstance(schema, dict) else None
        required = [str(r) for r in required] if isinstance(required, list) else []
        target_like = sorted(
            (n for n in names if n.lower() in TARGET_PROPERTIES),
            key=lambda n: TARGET_PROPERTIES.index(n.lower()),
        )
        if self.target:
            if not target_like:
                # The schema's property names come from the ARC-1 server, not
                # from customer data, and tell an admin what the tool takes.
                err = Arc1ReadError(
                    "target_not_supported",
                    _MESSAGES["target_not_supported"]
                    + " It takes: " + ", ".join(names[:20]) + ".",
                )
                self._log(err)
                raise err
            return {target_like[0]: self.target}
        if any(n in required for n in target_like):
            err = Arc1ReadError("target_required")
            self._log(err)
            raise err
        return {}

    def _parse(self, text: str) -> list[dict[str, str]]:
        try:
            return parse_rows(text)
        except Arc1ReadError as err:
            self._log(err)
            raise

    async def read(self, keys: list[str]) -> dict[str, dict[str, str]]:
        """``{key: row}`` for the keys found; one ``SAPQuery`` per call.

        The caller sizes the batch (at most :data:`MAX_ROWS` unique keys).
        """
        unique = list(dict.fromkeys(keys))
        if not unique:
            return {}
        if len(unique) > MAX_ROWS:
            raise ValueError("a batch holds at most 10000 keys")
        sql = build_sql(self.table, self.fields, self.key_field, unique)
        self._require_run()
        if self._target_args is None:
            await self.preflight()
        text = await self._query(sql, len(unique))
        out: dict[str, dict[str, str]] = {}
        for row in self._parse(text):
            if self.key_field not in row:
                err = Arc1ReadError("unexpected_result")
                self._log(err)
                raise err
            out[row[self.key_field]] = row
        return out

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
