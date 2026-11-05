"""Synthetic workbooks for the ``builtin:reconcile`` tests.

Neutral labels only: a mapping sheet ``Fields`` with two title rows and the
header on row 4, for the tables ``ZTAB`` and ``ZOTHER``. The rule labels hold a
newline, a no-break space and a double space on purpose, as hand-kept mapping
sheets do, so the label matching is tested against them.
"""

from __future__ import annotations

import io
from typing import Any

from openpyxl import Workbook

MAPPING_HEADER = [
    "Target table",
    "Target field",
    "Source table",
    "Source field",
    "Relevant (Y/N)",
    "Rule type W1 go live",
    "W1 go live\ntransformation\xa0rule from source",
    "Rule type W2 go live",
    "W2  go live transformation rule",
]

MAPPING_ROWS = [
    ["ZTAB", "OBJNR", "SRC", "OBJNR", "Y", "Direct", "1:1", "Direct", "1:1"],
    ["ZOTHER", "KEY1", "SRC2", "KEY1", "Y", "Direct", "1:1", "Direct", "1:1"],
    ["ztab ", "KIND", "SRC", "KIND", "Y", "Value map", "Use the code table", "Constant", "'A'"],
    [None, None, None, None, None, None, None, None, None],
    ["ZTAB", "NOTE", None, None, "N", "Not needed", None, "Not needed", None],
    ["ZOTHER", "KEY2", "SRC2", "KEY2", "Y", "Constant", "'X'", "Constant", "'X'"],
]


def build_book(sheets: dict[str, list[list[Any]]]) -> bytes:
    """Workbook bytes; each sheet's rows from row 1, ``None`` is an empty cell."""
    book = Workbook()
    book.remove(book.active)
    for name, rows in sheets.items():
        ws = book.create_sheet(name)
        for r, row in enumerate(rows, 1):
            for c, value in enumerate(row, 1):
                if value is not None:
                    ws.cell(row=r, column=c, value=value)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def mapping_rows(extra: list[list[Any]] | None = None) -> list[list[Any]]:
    """The ``Fields`` sheet as rows: two title rows, a blank row, the header, the data."""
    return [
        ["Field mapping overview"],
        ["Version 1"],
        [],
        list(MAPPING_HEADER),
        *[list(row) for row in MAPPING_ROWS],
        *(extra or []),
    ]


def mapping_book(extra: list[list[Any]] | None = None) -> bytes:
    """The synthetic mapping workbook: ``Fields`` plus an unrelated sheet ``Codes``."""
    return build_book({
        "Fields": mapping_rows(extra),
        "Codes": [["Source value", "Target value"], ["01", "A"]],
    })


# Cross-reference sheets: four layouts a hand-kept code-table workbook mixes.
# ``Codes`` holds a duplicate key (first wins), a blank-key row and a padded
# cell; ``Groups`` puts two tables side by side under duplicate labels.
XREF_SHEETS: dict[str, list[list[Any]]] = {
    "Codes": [
        ["Note", "Source value", "Target value"],
        [None, "01", "A"],
        [None, " X1 ", " B "],
        [None, None, "orphan"],
        [None, "01", "Z"],
        [None, 7, "C"],
    ],
    "Groups": [
        ["Group tables"],
        [],
        ["Hierarchy", "Group", None, "Group", "Group"],
        ["H1", "G1", None, "OLD1", "NEW1"],
        ["H2", "G2", None, "OLD2", "NEW2"],
        [None, None, None, "OLD3", "NEW3"],
    ],
    "Units": [
        ["Units of measure"],
        ["Id", "Source unit", "Text", None, None, "Target unit"],
        [1, "ST", "piece", None, None, "PC"],
        [2, "KG", "kilogram", None, None, "KGM"],
    ],
    "Multi": [
        ["Kind", "Code", "Target"],
        ["K1", "01", "T1"],
        ["K2", "01", "T2"],
        ["K1", "02", "T3"],
    ],
}


def xref_book(extra: dict[str, list[list[Any]]] | None = None) -> bytes:
    """The synthetic cross-reference workbook (:data:`XREF_SHEETS`), plus ``extra`` sheets."""
    return build_book({**{k: [list(r) for r in v] for k, v in XREF_SHEETS.items()},
                       **(extra or {})})
