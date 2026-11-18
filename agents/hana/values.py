"""What of a ``builtin:hana`` result may reach the model: cells, column names
and the answer cap.

Shared by the tools (:mod:`agents.hana.tools`) and the export
(:mod:`agents.hana.export`), which is why it is a module of its own: the export
needs the same cell and column rules without importing the toolset that
imports it. Standard library only.
"""

from __future__ import annotations

import datetime as dt
import math
import unicodedata
from decimal import Decimal
from typing import Any

from agents.hana.connection import LOB_CUT_MARKER

#: The most JSON characters an answer may hold; a larger one is refused, never cut.
MAX_RESULT_CHARS = 60_000
MAX_COLUMN_NAME_CHARS = 128
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})

__all__ = ["MAX_COLUMN_NAME_CHARS", "MAX_RESULT_CHARS", "column_name", "cut_columns",
           "json_value", "safe_part"]


def json_value(value: Any) -> Any:
    """One cell as JSON can carry it, without ever using ``repr``."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, memoryview):
        return f"<{value.nbytes} bytes>"
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return f"<{type(value).__name__}>"


def column_name(value: Any) -> str:
    """One column name as the model sees it: at most
    :data:`MAX_COLUMN_NAME_CHARS` characters, a control, format, line or
    paragraph separator character as ``?``."""
    text = str(value)[:MAX_COLUMN_NAME_CHARS]
    return "".join("?" if unicodedata.category(ch) in _UNSAFE_CATEGORIES else ch
                   for ch in text)


def safe_part(part: dict[str, Any]) -> None:
    part["columns"] = [column_name(c) for c in part["columns"]]
    part["rows"] = [[json_value(v) for v in row] for row in part["rows"]]


def cut_columns(columns: Any, rows: Any) -> list[str]:
    """The columns, sorted, in which a LOB value came back cut.

    Judged on the values as the connection read them, BEFORE masking (the
    masking shortens a statement text and drops the marker): a text the
    connection cut ends in :data:`LOB_CUT_MARKER`. A plain value that happens
    to end in those characters is named too, the safe direction. The names
    are the model's column names (:func:`column_name`)."""
    cut = set()
    for row in rows:
        for column, value in zip(columns, row):
            if isinstance(value, str) and value.endswith(LOB_CUT_MARKER):
                cut.add(column_name(column))
    return sorted(cut)
