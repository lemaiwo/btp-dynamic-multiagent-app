"""The grid of a run: SAP's ``Analytics`` answer read as rows.

Pure functions, no I/O, for :mod:`agents.ina.client`, which hands
:func:`read_grid` the JSON object of a read's answer and the run's plan
(``protocol.RunPlan``: the measures, the row dimensions with the key and text
attribute the read asked for, ``top``).

The form is the one SIA (S/4HANA 2022, SAP_BW 757) answered on 2026-11-04
(``tests/fixtures/ina/run_sia_grid.json``, recorded run 8, sanitised): the
``Version2`` result format, columnar.

* ``Grids[0].Axes``: one ``Rows`` and one ``Columns`` axis, each with its
  ``Dimensions`` (in the order of its ``Tuples``), ``TupleCount`` (the
  tuples sent: ``SubSetDescription.RowTo`` cuts them) and
  ``TupleCountTotal`` (the tuples that exist).
* ``Tuples``: one entry per dimension of the axis, whose
  ``MemberIndexes.Values`` holds one index per tuple into that dimension's
  member table.
* A row dimension's member table is its ``Attributes``: per attribute the
  read listed (the key, ``<dim>.KEY``, and its text when the VariableSubmit
  answer named one) a ``Values`` list, one entry per member, the key with
  ``ValuesException`` (0 is a plain value), plus ``MemberTypes`` (0 a member,
  1 a total).
* The measure structure on Columns carries no attributes; a column's member
  index is the measure's place in the request. Seen for ONE measure only:
  the order of several is inferred, not recorded, so ``protocol.check_caps``
  refuses a run of more than one measure until a probe verifies it (the
  reader keeps its checks for several).
* ``Cells``: ``Values.Values`` and ``Units.Values`` row-major, ``rows x
  columns`` as ``CellArraySizes`` says. A list may come wrapped in
  ``{Encoding, Size, Values}``; only ``Encoding`` ``None`` (what the request
  asks for) is read.

Fail-closed and default-deny. A form this reader does not know is
``sap_error`` with one fixed text (``UNREADABLE``, never a value of the
answer): an axis or a tuple list that does not match the request, a member
index out of range, a key that is not a string, a member that is a total
(the read hides them; one that comes anyway would read as a member), cell
sizes that do not add up, a cell that is neither a number, null nor text.
The form is checked on everything SAP sent, the extra row included.

A value SAP marks (a key's ``ValuesException`` or a cell's ``Exceptions``
other than 0) or sends as no number (a null or a text cell) is refused as
well, never read as null (a missing number would read as data), with a text
of its own (:data:`VALUE_REFUSED`: the model tells the user instead of
retrying). Values are checked only in the rows that are returned: the extra
row only says "there is more". Only the requested key, text, value and unit
leave, the texts cleaned and cut; every other attribute, label, formatted
value and the data source are dropped. Never cut either: more rows than
``top + 1`` or more cells than :data:`MAX_CELLS` in what would be returned
is ``result_too_large``.

Every refusal logs ONE WARNING, ``ina: grid refused: <reason> (count=n)``:
a key of the closed list :data:`REFUSAL_REASONS` and a count made here
(entries found, or the offending ones), never a value, name or text of the
answer. The model gets one sentence; the operator can tell refusals apart.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

from .description import UNREADABLE, _key, _label
from .errors import InaError

if TYPE_CHECKING:  # protocol imports this module
    from .protocol import RunPlan

logger = logging.getLogger(__name__)

# The cap of one run's answer: cells of what is returned (design addendum
# (k), part 2 point 2).
MAX_CELLS = 5000
_KEY_CHARS = 128
_TEXT_CHARS = 120
_UNIT_CHARS = 32
_MEMBER = 0

# Why a grid is refused: the closed list of keys the log line names.
# A form this reader does not know (UNREADABLE), checked on all SAP sent:
FORM_REASONS: tuple[str, ...] = (
    "grids",  # not exactly one grid
    "not_an_object",  # a grid, axis, dimension, attribute or tuple, or the cells
    "axes",  # not exactly one Rows and one Columns axis
    "tuple_count",  # TupleCount or TupleCountTotal no count, or the total below it
    "dimensions",  # the row dimensions are not the requested ones
    "row_tuples",  # the row tuples or their member index lists do not match
    "member_index",  # a member index outside its member table
    "attributes",  # no attribute list, or an attribute name not text or twice
    "key_attribute",  # the requested key attribute is missing
    "members",  # the member table is no list, or longer than the rows asked for
    "key_form",  # a key (not marked) that is no text
    "key_exception_list",  # ValuesException missing, too short or not whole numbers
    "text_form",  # the texts are not one text per member
    "member_type_list",  # MemberTypes missing or of another length
    "member_type",  # a member that is a total, or of another type
    "structure",  # the measure structure is not the requested one
    "structure_attributes",  # the measure structure carries attributes
    "columns",  # the columns are not the requested measures
    "cell_sizes",  # CellArraySizes is not [rows, measures]
    "cells",  # the cell values missing or of another length
    "cell_exception_list",  # Exceptions of another length or not whole numbers
    "units",  # the units of another length or not text
    "cell_form",  # a cell that is neither a number, null nor text
)
# A value SAP marked, or sent as no number, in a row that is returned:
VALUE_REASONS: tuple[str, ...] = ("cell_exception", "cell_null", "cell_text", "key_exception")
# More than may be returned (result_too_large):
SIZE_REASONS: tuple[str, ...] = ("too_many_rows", "too_many_cells")
REFUSAL_REASONS: tuple[str, ...] = FORM_REASONS + VALUE_REASONS + SIZE_REASONS

VALUE_REFUSED = (
    "SAP marked a value of the rows asked for as an error or sent no number for it, "
    "so no row is returned; tell the user instead of retrying"
)


def _refused(reason: str, count: int = 1) -> InaError:
    """The refusal of a grid for ``reason``, logged as ONE WARNING that names
    the reason and ``count`` only. A reason outside the closed list is a
    ``ValueError``, so a typo is a test failure, not a new key."""
    if reason not in REFUSAL_REASONS:
        raise ValueError(f"not a grid refusal reason: {reason!r}")
    logger.warning("ina: grid refused: %s (count=%d)", reason, count)
    if reason in VALUE_REASONS:
        return InaError("sap_error", VALUE_REFUSED)
    if reason == "too_many_rows":
        return InaError(
            "result_too_large",
            "SAP answered with more rows than were asked for",
            hint="filter, or group by fewer dimensions",
        )
    if reason == "too_many_cells":
        return InaError(
            "result_too_large",
            f"the answer has more than {MAX_CELLS} cells",
            hint="group by fewer dimensions, filter, or lower top",
        )
    return InaError("sap_error", UNREADABLE)


def _length(value: object) -> int:
    """How many entries ``value`` holds, as a count for the log."""
    return len(value) if isinstance(value, list) else 0


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _refused("not_an_object")
    return value


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _refused("tuple_count")
    return value


def _values(holder: object, length: int, reason: str) -> list[Any]:
    """A list of exactly ``length`` entries, plain or wrapped as
    ``{Encoding, Size, Values}`` with ``Encoding`` ``None``; anything else is
    refused for ``reason``."""
    if isinstance(holder, dict):
        if holder.get("Encoding", "None") != "None" or holder.get("Size", length) != length:
            raise _refused(reason, _length(holder.get("Values")))
        holder = holder.get("Values")
    if not isinstance(holder, list) or len(holder) != length:
        raise _refused(reason, _length(holder))
    return holder


def _whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _whole_numbers(values: list[Any], reason: str) -> list[int]:
    """``values`` when every entry is a whole number (an exception flag)."""
    wrong = sum(1 for v in values if not _whole(v))
    if wrong:
        raise _refused(reason, wrong)
    return values


def _indexes(holder: object, length: int, members: int) -> list[int]:
    out = _values(holder, length, "row_tuples")
    wrong = sum(1 for index in out if not _whole(index) or not 0 <= index < members)
    if wrong:
        raise _refused("member_index", wrong)
    return out


def _axes(grid: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    axes = grid.get("Axes")
    if not isinstance(axes, list) or len(axes) != 2:
        raise _refused("axes", _length(axes))
    by_type = {_object(axis).get("Type"): axis for axis in axes}
    if set(by_type) != {"Rows", "Columns"}:
        raise _refused("axes", len(axes))
    return by_type["Rows"], by_type["Columns"]


def _dimensions(
    axis: dict[str, Any], expected: list[str], reason: str
) -> dict[str, dict[str, Any]]:
    """The axis's dimensions by name; they must be exactly ``expected``."""
    raw = axis.get("Dimensions")
    if not isinstance(raw, list) or len(raw) != len(expected):
        raise _refused(reason, _length(raw))
    out = {_object(d).get("Name"): d for d in raw}
    if len(out) != len(raw) or set(out) != set(expected):
        raise _refused(reason, len(raw))
    return out


def _member_types(dimension: dict[str, Any], members: int) -> None:
    """Every member a plain one: a total (type 1), or any other type, is not
    a row this reader may pass on."""
    kinds = _values(dimension.get("MemberTypes"), members, "member_type_list")
    wrong = sum(1 for kind in kinds if kind != _MEMBER or isinstance(kind, bool))
    if wrong:
        raise _refused("member_type", wrong)


def _member_table(
    dimension: dict[str, Any], key_name: str, text_name: str | None, cap: int
) -> tuple[list[str], list[str] | None, list[bool]]:
    """``(keys, texts, marked)`` of a row dimension's members. ``texts`` is
    None when no text was asked for or SAP sent none; ``marked`` says which
    members SAP flagged in the key's ``ValuesException``. A marked member is
    never read (its key and text may be anything; ``""`` stands in): the
    caller refuses a returned row that points at one."""
    attributes = dimension.get("Attributes")
    if not isinstance(attributes, list):
        raise _refused("attributes", 0)
    by_name: dict[str, dict[str, Any]] = {}
    for attribute in attributes:
        name = _object(attribute).get("Name")
        if not isinstance(name, str) or name in by_name:
            raise _refused("attributes", len(attributes))
        by_name[name] = attribute
    key = by_name.get(key_name)
    if key is None:
        raise _refused("key_attribute", len(attributes))
    raw_keys = key.get("Values")
    if not isinstance(raw_keys, list) or len(raw_keys) > cap:
        raise _refused("members", _length(raw_keys))
    exceptions = _values(key.get("ValuesException"), len(raw_keys), "key_exception_list")
    marked = [flag != 0 for flag in _whole_numbers(exceptions, "key_exception_list")]
    wrong = sum(1 for k, m in zip(raw_keys, marked) if not m and not isinstance(k, str))
    if wrong:
        raise _refused("key_form", wrong)
    _member_types(dimension, len(raw_keys))
    keys = ["" if m else _key(k, _KEY_CHARS) for k, m in zip(raw_keys, marked)]
    if text_name is None or text_name not in by_name:
        return keys, None, marked
    raw_texts = _values(by_name[text_name].get("Values"), len(raw_keys), "text_form")
    wrong = sum(1 for t, m in zip(raw_texts, marked) if not m and not isinstance(t, str))
    if wrong:
        raise _refused("text_form", wrong)
    return keys, ["" if m else _label(t, _TEXT_CHARS) for t, m in zip(raw_texts, marked)], marked


def _measure_order(columns: dict[str, Any], plan: RunPlan) -> list[str]:
    """The measure of each column, in column order."""
    (structure,) = _dimensions(columns, [plan.structure], "structure").values()
    if structure.get("Attributes", []) != []:
        # Names on the structure would make the member indexes point into
        # them; not seen, so not guessed at.
        raise _refused("structure_attributes", _length(structure.get("Attributes")))
    width = len(plan.measures)
    _member_types(structure, width)
    if _count(columns.get("TupleCount")) != width:
        raise _refused("columns", width)
    tuples = columns.get("Tuples")
    if not isinstance(tuples, list) or len(tuples) != 1:
        raise _refused("columns", _length(tuples))
    order = _values(_object(tuples[0]).get("MemberIndexes"), width, "columns")
    if not all(_whole(index) for index in order) or sorted(order) != list(range(width)):
        raise _refused("columns", width)
    return [plan.measures[index] for index in order]


def _is_cell(value: object) -> bool:
    """A cell of a form this reader knows: a finite number, or null or text
    (refused as values in a returned row, never read)."""
    if value is None or isinstance(value, str):
        return True
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return not isinstance(value, float) or math.isfinite(value)


def read_grid(payload: object, plan: RunPlan) -> dict[str, Any]:
    """The rows of the one grid of ``payload`` for ``plan``.

    Answers ``{columns, rows, units, row_count, truncated}``: ``columns`` are
    the requested dimensions (each followed by ``<name>.text`` when SAP sent
    a text that differs from the key in a returned row) and measures (each
    followed by ``<name>.unit`` when its unit is not the same in every
    returned row; otherwise the unit is in ``units``). At most ``top`` rows
    are returned; ``truncated`` is true when SAP says more exist
    (``TupleCountTotal``, or the extra row the request asks for).
    """
    grids = payload.get("Grids") if isinstance(payload, dict) else None
    if not isinstance(grids, list) or len(grids) != 1:
        raise _refused("grids", _length(grids))
    grid = _object(grids[0])
    rows_axis, columns_axis = _axes(grid)

    count = _count(rows_axis.get("TupleCount"))
    total = _count(rows_axis.get("TupleCountTotal", count))
    if total < count:
        raise _refused("tuple_count")
    if count > plan.top + 1:
        raise _refused("too_many_rows", count)
    shown = min(count, plan.top)
    if shown * len(plan.measures) > MAX_CELLS:
        raise _refused("too_many_cells", shown * len(plan.measures))

    dimensions = _dimensions(rows_axis, plan.group_by, "dimensions")
    tuples = rows_axis.get("Tuples")
    if not isinstance(tuples, list) or len(tuples) != len(plan.group_by):
        raise _refused("row_tuples", _length(tuples))
    # The member keys (and texts) of each requested dimension, row by row,
    # and how many members a returned row points at that SAP marked.
    keys_by_row: dict[str, list[str]] = {}
    texts_by_row: dict[str, list[str] | None] = {}
    marked_members = 0
    for dimension, entry in zip(rows_axis["Dimensions"], tuples):
        name = dimension["Name"]
        keys, texts, marked = _member_table(
            dimensions[name], plan.keys[name], plan.texts.get(name), plan.top + 1
        )
        indexes = _indexes(_object(entry).get("MemberIndexes"), count, len(keys))
        marked_members += len({i for i in indexes[:shown] if marked[i]})
        keys_by_row[name] = [keys[i] for i in indexes]
        texts_by_row[name] = None if texts is None else [texts[i] for i in indexes]

    measures = _measure_order(columns_axis, plan)
    width = len(measures)
    sizes = grid.get("CellArraySizes")
    if sizes != [count, width]:
        raise _refused("cell_sizes", _length(sizes))
    cells = _object(grid.get("Cells"))
    values = _values(cells.get("Values"), count * width, "cells")
    exceptions: list[int] = [0] * (count * width)
    if "Exceptions" in cells:
        exceptions = _whole_numbers(
            _values(cells["Exceptions"], count * width, "cell_exception_list"),
            "cell_exception_list",
        )
    units: list[Any] = [""] * (count * width)
    if "Units" in cells:
        units = _values(cells["Units"], count * width, "units")
        wrong = sum(1 for u in units if not isinstance(u, str))
        if wrong:
            raise _refused("units", wrong)
    # The form of every cell, the extra row's included.
    wrong = sum(1 for v in values if not _is_cell(v))
    if wrong:
        raise _refused("cell_form", wrong)
    # The values of the rows that are returned (row-major: the first
    # shown * width cells); the extra row only says there is more.
    returned = shown * width
    for reason, found in (
        ("key_exception", marked_members),
        ("cell_exception", sum(1 for e in exceptions[:returned] if e != 0)),
        ("cell_null", sum(1 for v in values[:returned] if v is None)),
        ("cell_text", sum(1 for v in values[:returned] if isinstance(v, str))),
    ):
        if found:
            raise _refused(reason, found)

    measured: list[dict[str, tuple[int | float, str | None]]] = []
    for row in range(shown):
        measured.append({
            measure: (values[row * width + column],
                      _label(units[row * width + column], _UNIT_CHARS) or None)
            for column, measure in enumerate(measures)
        })
    # A text column only where SAP sent a text that says more than the key.
    text_rows = {
        name: texts
        for name, texts in texts_by_row.items()
        if texts is not None
        and any(text and text != key for key, text in zip(keys_by_row[name][:shown], texts))
    }
    unit_of: dict[str, str] = {}
    mixed: set[str] = set()
    for measure in plan.measures:
        seen = {line[measure][1] for line in measured}
        if len(seen) > 1:
            mixed.add(measure)
        elif seen and (only := next(iter(seen))) is not None:
            unit_of[measure] = only
    rows: list[list[Any]] = []
    for row, line in enumerate(measured):
        out: list[Any] = []
        for name in plan.group_by:
            out.append(keys_by_row[name][row])
            if name in text_rows:
                out.append(text_rows[name][row] or None)
        for measure in plan.measures:
            value, unit = line[measure]
            out.append(value)
            if measure in mixed:
                out.append(unit)
        rows.append(out)
    return {
        "columns": _columns(plan, set(text_rows), mixed),
        "rows": rows,
        "units": unit_of,
        "row_count": len(rows),
        "truncated": total > plan.top,
    }


def _columns(plan: RunPlan, with_text: set[str], mixed: set[str]) -> list[str]:
    columns: list[str] = []
    for name in plan.group_by:
        columns.append(name)
        if name in with_text:
            columns.append(f"{name}.text")
    for name in plan.measures:
        columns.append(name)
        if name in mixed:
            columns.append(f"{name}.unit")
    return columns


__all__ = [
    "FORM_REASONS",
    "MAX_CELLS",
    "REFUSAL_REASONS",
    "SIZE_REASONS",
    "VALUE_REASONS",
    "VALUE_REFUSED",
    "read_grid",
]
