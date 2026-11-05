"""Shared parts of the ``builtin:reconcile`` tools (``agents/reconcile/tools.py``).

The fixed messages per code (:data:`MESSAGES`), the error shape
``{"error": {code, message, ...}}`` (docgen's), the one way a tool reads a
file of the conversation (:func:`load_file`: owner-checked by
``store.get_file`` AND of this conversation), spec parsing whose errors never
echo input, and :func:`activity_summary`, the fixed-form preview the registry
keeps of a result instead of its cell text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

import agents.db as agents_db
from agents.oauth2 import OAuthAuthorizationRequired
from agents.reconcile import runs_store
from agents.reconcile.engine import iter_csv
from agents.reconcile.spec import ReconcileSpec, ValueMapRule, _hint_text, validation_errors
from agents.reconcile.workbook import Refused
from agents.webchat import store
from agents.webchat.docgen import ChatRunContext, current_chat_run
from agents.webchat.uploads import CSV, JSON, MAX_FILE_BYTES, TEXT, XLSX

logger = logging.getLogger(__name__)

MAX_SPEC_ERRORS = 20
MAX_LISTED = 50  # column names and uploads named in one refusal
MAX_LISTED_NAME = 64
MAX_ID = 64
_FILE_PART = re.compile(r"[^A-Za-z0-9_-]+")
CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")
_STATUSES = frozenset({runs_store.RUNNING, *runs_store.OUTCOMES})
_ZIP = b"PK\x03\x04"

MESSAGES = {
    "no_chat": "The reconciliation tools work only in the web chat.",
    "file_not_found": "No such file in this conversation. Use one of the uploads listed.",
    "wrong_file_type": "This file is not of the type this argument needs "
    "(a .xlsx workbook, a .csv file or a .json spec, at most the chat's file size).",
    "invalid_spec": "The spec does not match the reconciliation spec schema.",
    "missing_rules": "Some CSV columns have no rule in the spec. Give each one a rule "
    "(not_needed or unresolved when there is nothing to compare).",
    "unknown_columns": "The spec names columns (or a key column) the CSV header does not have.",
    "lookup_failed": "A code table of the spec cannot be read from the workbook.",
    "csv_invalid": "The CSV file cannot be read: a UTF-8, comma-separated file with "
    "one header row of distinct names is needed.",
    "csv_empty": "The CSV file has no data rows.",
    "xref_required": "The spec translates values through code tables, "
    "but no code-table workbook was given.",
    "arc1_not_configured": "This agent has no ARC-1 server for the source system. "
    "An admin must name the agent's ARC-1 entry in the reconcile entry.",
    "sign_in_needed": "Reading the source system needs the tester to sign in to ARC-1 again.",
    "not_signed_in": "A reconciliation run needs the signed-in tester.",
    "target_required": "The ARC-1 server needs a target system, but none is configured.",
    "target_not_supported": "A target system is configured, but the ARC-1 query tool "
    "takes no target argument.",
    "query_tool_missing": "The ARC-1 server does not offer the SAPQuery tool.",
    "field_not_in_table": "Some fields the spec reads are not in the source table.",
    "preflight_refused": "ARC-1 refused the field check on the data dictionary.",
    "arc1_refused": "ARC-1 refused the query (policy or authorization).",
    "arc1_failed": "ARC-1 could not run the query. Try again later.",
    "arc1_timeout": "A read of the source system took too long.",
    "unexpected_result": "ARC-1 returned a result in a shape the reader does not know.",
    "ambiguous_key": "The source table returned more than one row for a key; "
    "the key field is not the full key.",
    "truncated": "ARC-1 returned only part of the rows of a batch.",
    "report_too_large": "The reconciliation report is too large to store.",
    "interrupted": "The run was interrupted (the app restarted or stopped). Start it again.",
    "already_running": "A reconciliation run of this conversation is still running.",
    "run_not_found": "No such reconciliation run in this conversation.",
    "internal": "The reconciliation tool failed unexpectedly.",
}
FAILED = "The run failed."


def error(code: str, message: str | None = None, **extra: Any) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message or MESSAGES.get(code, FAILED)}
    error.update({k: v for k, v in extra.items() if v is not None})
    return {"error": error}


def listed(names: list[str]) -> list[str]:
    """Column names for a refusal: printable, bounded, at most :data:`MAX_LISTED`."""
    out = []
    for name in names[:MAX_LISTED]:
        flat = "".join("?" if not ch.isprintable() else ch for ch in str(name))
        out.append(flat[:MAX_LISTED_NAME])
    return out


class Refusal(Exception):
    """A tool answer that ends the call early (a file or spec that cannot be used)."""

    def __init__(self, answer: dict[str, Any]) -> None:
        super().__init__(answer["error"]["code"])
        self.answer = answer


# --- files of the conversation -------------------------------------------------

WORKBOOK, CSV_FILE, SPEC = "workbook", "csv", "spec"


def _right_type(want: str, media_type: str, data: bytes) -> bool:
    if want == WORKBOOK:
        return media_type == XLSX and data.startswith(_ZIP)
    if want == CSV_FILE:
        return media_type in (CSV, TEXT) and not data.startswith(_ZIP)
    return media_type == JSON


async def load_file(ctx: ChatRunContext, file_id: Any, want: str,
                kinds: tuple[str, ...] = ("upload",)) -> bytes:
    """The bytes of a file of THIS conversation and its owner, of the type wanted.

    Owner, conversation and kind are checked in the query
    (``store.get_conversation_file``): a file id of another conversation (even
    the same tester's) is unknown and its bytes are never loaded, so a model
    cannot be talked into reading another conversation's files.
    """
    async with agents_db.SessionLocal() as db:
        row = None
        if isinstance(file_id, str) and 0 < len(file_id) <= MAX_ID:
            row = await store.get_conversation_file(
                db, file_id, ctx.principal, conversation_id=ctx.conversation_id, kinds=kinds)
        if row is None:
            uploads = await store.conversation_files(
                db, ctx.conversation_id, ctx.principal, kind="upload")
            raise Refusal(error("file_not_found", uploads=[
                {"file_id": f["id"], "name": f["name"], "size": f["size"]}
                for f in uploads[:MAX_LISTED]
            ]))
        if row.size > MAX_FILE_BYTES or len(row.data) > MAX_FILE_BYTES:
            raise Refusal(error("wrong_file_type"))
        if not _right_type(want, row.media_type, row.data):
            raise Refusal(error("wrong_file_type"))
        return bytes(row.data)


def chat() -> ChatRunContext:
    ctx = current_chat_run.get()
    if ctx is None:
        raise Refusal(error("no_chat"))
    return ctx


def parse_spec(raw: Any) -> ReconcileSpec:
    if not isinstance(raw, dict):
        raise Refusal(error("invalid_spec", errors=[
            {"loc": "spec", "msg": "the spec is one JSON object"}]))
    try:
        return ReconcileSpec.model_validate(raw)
    except ValidationError as exc:
        raise Refusal(error("invalid_spec",
                              errors=validation_errors(exc, MAX_SPEC_ERRORS))) from None
    except RecursionError:
        raise Refusal(error("invalid_spec", errors=[
            {"loc": "spec", "msg": "nested too deeply"}])) from None


def spec_of_file(data: bytes) -> ReconcileSpec:
    try:
        raw = json.loads(data)
    except (ValueError, RecursionError):
        raise Refusal(error("invalid_spec", errors=[
            {"loc": "spec", "msg": "the file is not valid JSON"}])) from None
    return parse_spec(raw)


def csv_shape(data: bytes) -> tuple[list[str], int]:
    """The header and the number of data rows (malformed rows not counted)."""
    header, rows = iter_csv(data)
    return header, sum(1 for _ in rows)


def needs_xref(spec: ReconcileSpec) -> bool:
    return any(isinstance(rule, ValueMapRule) for rule in spec.columns.values())


async def in_thread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Workbook and CSV work off the event loop; a reader refusal is the answer."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Refused as exc:
        raise Refusal(exc.as_error()) from None


def lookup_refusal(exc: Refused) -> dict[str, Any]:
    # The hint names a sheet and a CSV column (already cut): kept to one bounded line.
    return error("lookup_failed", reason=exc.code,
                 hint=_hint_text(exc.hint) if exc.hint else None)


def spec_file_name(spec: ReconcileSpec) -> str:
    parts = [_FILE_PART.sub("_", p).strip("_") or "x" for p in (spec.table, spec.wave)]
    return "-".join(parts) + ".reconcile-spec.json"


def oauth_cause(exc: BaseException) -> OAuthAuthorizationRequired | None:
    """The ``OAuthAuthorizationRequired`` an ARC-1 error was mapped from.

    The reader raises its own code ``from None``, which keeps the original
    as ``__context__``; MCP and anyio wrap it in exception groups."""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, OAuthAuthorizationRequired):
            return cur
        if isinstance(cur, BaseExceptionGroup):
            stack.extend(cur.exceptions)
        stack.extend(e for e in (cur.__cause__, cur.__context__) if e is not None)
    return None


# --- activity ------------------------------------------------------------------

_TOOL_NAME_RE = re.compile(
    r"(?:reconcile(?:_[0-9]+)?_)?"
    r"(read_mapping|read_xref_sheet|save_spec|start_reconciliation|reconciliation_status)"
)


def _count(value: Any) -> str:
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else "?"


def _length(value: Any) -> str:
    return str(len(value)) if isinstance(value, (list, dict)) else "?"


def activity_summary(tool_name: Any, result: Any) -> str | None:
    """What a run's activity keeps of a result of the five tools, else ``None``.

    Counts, a status word and a refusal's code only, in a fixed form: the
    preview is shown in the chat's tool card and stored with an API-triggered
    run, and these results hold mapping and code-table cell text. Decided by
    the tool's name alone, plain or prefixed (``reconcile_``,
    ``reconcile_0_``), as ``agents.sharepoint_tools.activity_summary`` is.
    """
    match = _TOOL_NAME_RE.fullmatch(tool_name) if isinstance(tool_name, str) else None
    if match is None:
        return None
    tool = match.group(1)
    if not isinstance(result, dict):
        return f"{tool}: no summary"
    if "error" in result:
        error = result.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        return f"error: {code}" if isinstance(code, str) and CODE_RE.fullmatch(code) else "error"
    if tool == "read_mapping":
        return f"{tool}: {_length(result.get('rows'))} rows"
    if tool == "read_xref_sheet":
        if "sheets" in result:
            return f"{tool}: {_length(result.get('sheets'))} sheets"
        return f"{tool}: {_length(result.get('rows'))} rows"
    if tool == "save_spec":
        kinds = result.get("columns_by_kind")
        columns = (sum(len(v) for v in kinds.values() if isinstance(v, list))
                   if isinstance(kinds, dict) else "?")
        return f"{tool}: {columns} columns, {_length(result.get('lookups'))} lookups"
    if tool == "start_reconciliation":
        return (f"{tool}: {_count(result.get('rows_total'))} rows, "
                f"{_count(result.get('batches'))} batches")
    status = result.get("status")
    word = status if status in _STATUSES else "?"
    return f"{tool}: {word} {_count(result.get('rows_done'))}/{_count(result.get('rows_total'))}"


