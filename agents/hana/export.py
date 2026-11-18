"""``export_result`` of the ``builtin:hana`` toolset: the reads and the file.

The tool itself (its registration, arguments and description) is in
:mod:`agents.hana.tools`; this module holds what only the export does: read a
check or a guarded SELECT up to :data:`EXPORT_MAX_ROWS` rows, write the parts
as one Excel workbook through the web chat's ``docgen`` (passed in by the
caller, so this module does not load the web chat at import), and answer the
file's metadata and a preview.

The order is fixed: refuse first (switches, arguments, the SELECT guard),
then read, then store. Once the file is stored the call always answers it:
when even the answer without previews passes :data:`MAX_RESULT_CHARS`, the
answer is counts only (:func:`_minimal_answer`), never ``result_too_large``,
which would tell the model to narrow a query whose export exists.

The driver call and the guard are passed in by the toolset (``run``,
``guard``), so the toolset's module stays the one place they are looked up.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from agents.hana.checks import InvalidArgument, bind_values, build_answer, lookup, resolve_args
from agents.hana.connection import LOB_CUT_MARKER, HanaSystem
from agents.hana.errors import HanaError
from agents.hana.guard import SelectRefused
from agents.hana.values import (
    MAX_RESULT_CHARS,
    column_name,
    cut_columns,
    json_value,
    safe_part,
)
from agents.tool_results import result_chars

logger = logging.getLogger(__name__)

#: The most rows a file holds (the web chat's sheet cap), whatever a check's
#: ``max_rows_cap`` says (that cap exists for the 60,000 character answer,
#: not for a file).
EXPORT_MAX_ROWS = 10_000
#: The rows of the file the model sees in the answer.
EXPORT_PREVIEW_ROWS = 20
#: What an export reads of each LOB value, and of all of them together.
EXPORT_LOB_CHARS = 4096
EXPORT_LOB_BUDGET = 5_000_000
#: What the text and binary values of an export's rows may hold together,
#: counted while they are fetched (``row_chars_budget`` of ``run_query``): a
#: wide table stops before 10,001 rows of it are held, copied and written.
EXPORT_ROW_CHARS_BUDGET = 20_000_000
#: The workbook's column cap (``MAX_COLUMNS`` of :mod:`agents.webchat.docgen`,
#: not imported here so this module does not load the web chat at import),
#: checked by ``run_query`` (``max_columns``) before anything is fetched; the
#: check in :meth:`HanaExport.write` stays as the second line.
EXPORT_MAX_COLUMNS = 100
EXPORT_TOP_RULE = f"must be a whole number from 1 to {EXPORT_MAX_ROWS}"
EXPORT_ONE_OF_RULE = "give exactly one of check and sql"
EXPORT_NO_SQL_RULE = "free SQL is not allowed for this agent"
EXPORT_CHECK_ONLY_RULE = "only with check"
EXPORT_FILENAME_RULE = "must be a file name with at least one usable character"

__all__ = ["EXPORT_CHECK_ONLY_RULE", "EXPORT_FILENAME_RULE", "EXPORT_LOB_BUDGET",
           "EXPORT_LOB_CHARS", "EXPORT_MAX_COLUMNS", "EXPORT_MAX_ROWS", "EXPORT_NO_SQL_RULE",
           "EXPORT_ONE_OF_RULE", "EXPORT_PREVIEW_ROWS", "EXPORT_ROW_CHARS_BUDGET",
           "EXPORT_TOP_RULE", "HanaExport", "export_top"]

Run = Callable[..., Awaitable[tuple[Any, Any, Any]]]
Read = tuple[dict[str, Any], list[dict[str, Any]], list[tuple[str, list[str]]]]


async def _read(run: Run, target: HanaSystem, sql: str, binds: Any,
                top_rows: int) -> tuple[Any, Any, Any]:
    """One read of an export, with its LOB and row-character budgets and its
    column cap (checked before the fetch). A read over any of them is
    ``export_too_large`` here, not ``result_too_large``: the file, not an
    answer, is what would be too large, and nothing is stored."""
    try:
        return await run(target, sql, binds, top_rows, lob_chars=EXPORT_LOB_CHARS,
                         lob_budget=EXPORT_LOB_BUDGET,
                         row_chars_budget=EXPORT_ROW_CHARS_BUDGET,
                         max_columns=EXPORT_MAX_COLUMNS)
    except HanaError as refused:
        if refused.code == "result_too_large":
            raise HanaError("export_too_large") from None
        raise


def _excel_value(value: Any) -> Any:
    """One cell as the workbook stores it: :func:`json_value`, except that a
    finite ``int`` / ``float`` stays a number and a ``Decimal`` becomes a
    number only when the float says exactly the same (``1.25``), else text, so
    a long decimal is never rounded in the file. A non-finite float is the
    text :func:`json_value` gives it (``nan``, ``inf``, ``-inf``): the
    workbook writer refuses it as a number."""
    if isinstance(value, Decimal):
        if value.is_finite():
            as_float = float(value)
            if math.isfinite(as_float) and Decimal(repr(as_float)) == value:
                return as_float
        return str(value)
    return json_value(value)


def _fit_cells(parts: list[dict[str, Any]], max_cells: int) -> None:
    """Cut the rows of the parts, in order, so the workbook stays within
    ``max_cells`` (header cells count, as ``build_xlsx`` counts them); a part
    that lost rows is ``truncated``."""
    budget = max_cells - sum(len(p["columns"]) for p in parts)
    for part in parts:
        width = max(1, len(part["columns"]))
        allowed = max(0, budget // width)
        if len(part["rows"]) > allowed:
            part["rows"] = part["rows"][:allowed]
            part["truncated"] = True
        budget -= len(part["rows"]) * width


def _fit_text(parts: list[dict[str, Any]], sheets_of: list[tuple[str, list[str]]],
              max_chars: int) -> list[tuple[str, list[str]]]:
    """Cut every text cell longer than the workbook's cell limit
    (``max_chars``, ``MAX_CELL_CHARS`` of the web chat's ``docgen``) to fit,
    ending in :data:`LOB_CUT_MARKER`, and add its column to that sheet's cut
    columns. The second line behind the reader's per-value cap
    (``EXPORT_LOB_CHARS``): one long cell must never fail the whole file,
    which the workbook builder would refuse. Expects the parts' column names
    already in their model form (:func:`column_name`)."""
    keep = max_chars - len(LOB_CUT_MARKER)
    fitted = []
    for part, (label, cut) in zip(parts, sheets_of):
        names = set(cut)
        rows = []
        for row in part["rows"]:
            cells = list(row)
            for i, value in enumerate(cells):
                if isinstance(value, str) and len(value) > max_chars:
                    cells[i] = value[:keep] + LOB_CUT_MARKER
                    names.add(part["columns"][i])
            rows.append(cells)
        part["rows"] = rows
        fitted.append((label, sorted(names)))
    return fitted


def export_top(value: Any) -> int:
    """The file's row limit: ``None`` is :data:`EXPORT_MAX_ROWS`."""
    if value is None:
        return EXPORT_MAX_ROWS
    if not isinstance(value, int) or isinstance(value, bool) or not (
            1 <= value <= EXPORT_MAX_ROWS):
        raise InvalidArgument("top", EXPORT_TOP_RULE)
    return value


def _minimal_answer(answer: dict[str, Any], parts: list[dict[str, Any]]) -> dict[str, Any]:
    """The stored file by counts only: no column, preview, cut column or part
    label, so it always fits the answer cap."""
    minimal = {key: answer[key] for key in ("system", "check") if key in answer}
    minimal.update(
        file_id=answer.get("file_id"), filename=answer.get("filename"),
        download_url=answer.get("download_url"),
        row_count=sum(len(part["rows"]) for part in parts),
        truncated=any(bool(part["truncated"]) for part in parts),
        column_count=sum(len(part["columns"]) for part in parts),
        preview_omitted=True)
    return minimal


class HanaExport:
    """The export of one toolset: its switches and its lock (one export at a
    time per toolset: an export holds up to 10,000 rows in memory and a slot
    of the connection layer for its whole read)."""

    def __init__(self, *, allow_select: bool, allow_select_all: bool) -> None:
        self.allow_select = allow_select
        self.allow_select_all = allow_select_all
        self.lock = asyncio.Lock()

    async def read_check(self, target: HanaSystem, name: Any, top_rows: int,
                         given: dict[str, Any], *, run: Run) -> Read:
        found = lookup(name, allow_select=self.allow_select)
        args = resolve_args(found, **given)  # top left out: the check's cap is not the file's
        if "top" in args:
            args["top"] = top_rows
        results = [await _read(run, target, statement.sql, bind_values(statement, args),
                               top_rows)
                   for statement in found.statements]
        cut = [cut_columns(columns, rows) for columns, rows, _ in results]
        answer = build_answer(found, results)  # masks the check's masked columns
        parts = answer.get("parts", [answer])
        labels = [part.get("label") or found.name for part in parts]
        return ({"system": target.name, "check": found.name,
                 "_composite": "parts" in answer}, parts, list(zip(labels, cut)))

    async def read_select(self, target: HanaSystem, statement: Any, top_rows: int,
                          given: dict[str, Any], *, run: Run,
                          guard: Callable[..., Any]) -> Read | dict[str, Any]:
        if not self.allow_select:
            raise InvalidArgument("sql", EXPORT_NO_SQL_RULE)
        for name, value in given.items():
            if value is not None:
                raise InvalidArgument(name, EXPORT_CHECK_ONLY_RULE)
        try:
            # The same guard call as run_select, on the model's SQL as written.
            guard(statement, any_schema=self.allow_select_all)
        except SelectRefused as refused:
            error = HanaError("select_refused").to_dict()
            error["rule"] = refused.rule
            return {"error": error}
        columns, rows, truncated = await _read(run, target, statement, (), top_rows)
        part = {"columns": list(columns), "rows": [list(r) for r in rows],
                "truncated": bool(truncated)}
        return ({"system": target.name, "_composite": False}, [part],
                [("Result", cut_columns(columns, rows))])

    @staticmethod
    async def write(docgen: Any, ctx: Any, filename: str, head: dict[str, Any],
                    parts: list[dict[str, Any]],
                    sheets_of: list[tuple[str, list[str]]]) -> dict[str, Any]:
        if any(len(part["columns"]) > docgen.MAX_COLUMNS for part in parts):
            raise HanaError("export_too_large")
        for part in parts:
            part["columns"] = [column_name(c) for c in part["columns"]]
        _fit_cells(parts, docgen.MAX_CELLS)
        sheets_of = _fit_text(parts, sheets_of, docgen.MAX_CELL_CHARS)
        # Built without validation: every cell is already a Cell (_excel_value).
        sheets = [docgen.SheetSpec.model_construct(
            name=label, columns=part["columns"],
            rows=[[_excel_value(v) for v in row] for row in part["rows"]])
            for part, (label, _) in zip(parts, sheets_of)]
        info = await docgen.store_file(ctx, filename, docgen.XLSX,
                                       lambda: docgen.build_xlsx(sheets))
        if "error" in info:
            error = info["error"] if isinstance(info["error"], dict) else {}
            code = error.get("code")
            if code == "too_large":
                raise HanaError("export_too_large")
            # The store's codes and messages are fixed texts without a value
            # (the workbook builder's SheetError included); nothing else is logged.
            logger.warning("hana: export store failed (%s: %s)", code, error.get("message"))
            raise HanaError("export_failed")
        # From here on the file is stored: every path answers it.
        composite = head.pop("_composite")
        answer: dict[str, Any] = {**head, "file_id": info.get("file_id"),
                                  "filename": info.get("name"),
                                  "download_url": info.get("download_url")}
        shown = []
        for part, (label, cut) in zip(parts, sheets_of):
            preview = {"columns": part["columns"], "rows": part["rows"][:EXPORT_PREVIEW_ROWS]}
            safe_part(preview)
            entry: dict[str, Any] = {"columns": preview["columns"],
                                     "row_count": len(part["rows"]),
                                     "truncated": bool(part["truncated"]),
                                     "preview": preview["rows"]}
            if cut:
                entry["truncated_columns"] = cut
            shown.append({"label": label, **entry} if composite else entry)
        if composite:
            answer["parts"] = shown
        else:
            answer.update(shown[0])
        if result_chars(answer) > MAX_RESULT_CHARS:
            # The file is stored: the call answers it, without the preview.
            for entry in shown:
                entry["preview"] = []
                entry["preview_omitted"] = True
            if not composite:
                answer.update(shown[0])
        if result_chars(answer) > MAX_RESULT_CHARS:
            # Columns, cut columns or labels alone pass the cap: counts only.
            return _minimal_answer(answer, parts)
        return answer
