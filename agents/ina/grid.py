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
  the order of several is inferred, not recorded.
* ``Cells``: ``Values.Values`` and ``Units.Values`` row-major, ``rows x
  columns`` as ``CellArraySizes`` says. A list may come wrapped in
  ``{Encoding, Size, Values}``; only ``Encoding`` ``None`` (what the request
  asks for) is read.

Fail-closed and default-deny. Anything this reader does not know is
``sap_error`` (one fixed text, never a value of the answer): an axis or a
tuple list that does not match the request, a member index out of range, a
key that is not a string or carries an exception, a member that is a total
(the read hides them; one that comes anyway would read as a member), cell
sizes that do not add up, a cell that is not a plain number or carries an
exception (never read as null: a missing number would read as data). Only
the requested key, text, value and unit leave, the texts cleaned and cut;
every other attribute, label, formatted value and the data source are
dropped. Never cut either: more rows than ``top + 1`` or more cells than
:data:`MAX_CELLS` in what would be returned is ``result_too_large``.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from .description import UNREADABLE, _key, _label
from .errors import InaError

if TYPE_CHECKING:  # protocol imports this module
    from .protocol import RunPlan

# The cap of one run's answer: cells of what is returned (design addendum
# (k), part 2 point 2).
MAX_CELLS = 5000
_KEY_CHARS = 128
_TEXT_CHARS = 120
_UNIT_CHARS = 32
_MEMBER = 0


def _unreadable() -> InaError:
    return InaError("sap_error", UNREADABLE)


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _unreadable()
    return value


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _unreadable()
    return value


def _values(holder: object, length: int) -> list[Any]:
    """A list of exactly ``length`` entries, plain or wrapped as
    ``{Encoding, Size, Values}`` with ``Encoding`` ``None``."""
    if isinstance(holder, dict):
        if holder.get("Encoding", "None") != "None":
            raise _unreadable()
        if "Size" in holder and holder["Size"] != length:
            raise _unreadable()
        holder = holder.get("Values")
    if not isinstance(holder, list) or len(holder) != length:
        raise _unreadable()
    return holder


def _indexes(holder: object, length: int, members: int) -> list[int]:
    out = _values(holder, length)
    for index in out:
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < members:
            raise _unreadable()
    return out


def _axes(grid: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    axes = grid.get("Axes")
    if not isinstance(axes, list) or len(axes) != 2:
        raise _unreadable()
    by_type = {_object(axis).get("Type"): axis for axis in axes}
    if set(by_type) != {"Rows", "Columns"}:
        raise _unreadable()
    return by_type["Rows"], by_type["Columns"]


def _dimensions(axis: dict[str, Any], expected: list[str]) -> dict[str, dict[str, Any]]:
    """The axis's dimensions by name; they must be exactly ``expected``."""
    raw = axis.get("Dimensions")
    if not isinstance(raw, list) or len(raw) != len(expected):
        raise _unreadable()
    out = {_object(d).get("Name"): d for d in raw}
    if len(out) != len(raw) or set(out) != set(expected):
        raise _unreadable()
    return out


def _member_types(dimension: dict[str, Any], members: int) -> None:
    """Every member a plain one: a total (type 1), or any other type, is not
    a row this reader may pass on."""
    if any(kind != _MEMBER or isinstance(kind, bool)
           for kind in _values(dimension.get("MemberTypes"), members)):
        raise _unreadable()


def _member_table(
    dimension: dict[str, Any], key_name: str, text_name: str | None, cap: int
) -> tuple[list[str], list[str] | None]:
    """``(keys, texts)`` of a row dimension's members. ``texts`` is None
    when no text was asked for or SAP sent none."""
    attributes = dimension.get("Attributes")
    if not isinstance(attributes, list):
        raise _unreadable()
    by_name: dict[str, dict[str, Any]] = {}
    for attribute in attributes:
        name = _object(attribute).get("Name")
        if not isinstance(name, str) or name in by_name:
            raise _unreadable()
        by_name[name] = attribute
    key = by_name.get(key_name)
    if key is None:
        raise _unreadable()
    raw_keys = key.get("Values")
    if not isinstance(raw_keys, list) or len(raw_keys) > cap:
        raise _unreadable()
    if not all(isinstance(k, str) for k in raw_keys):
        raise _unreadable()
    exceptions = _values(key.get("ValuesException"), len(raw_keys))
    if any(e != 0 or isinstance(e, bool) for e in exceptions):
        raise _unreadable()
    _member_types(dimension, len(raw_keys))
    keys = [_key(k, _KEY_CHARS) for k in raw_keys]
    if text_name is None or text_name not in by_name:
        return keys, None
    raw_texts = _values(by_name[text_name].get("Values"), len(raw_keys))
    if not all(isinstance(t, str) for t in raw_texts):
        raise _unreadable()
    return keys, [_label(t, _TEXT_CHARS) for t in raw_texts]


def _measure_order(columns: dict[str, Any], plan: RunPlan) -> list[str]:
    """The measure of each column, in column order."""
    (structure,) = _dimensions(columns, [plan.structure]).values()
    if structure.get("Attributes", []) != []:
        # Names on the structure would make the member indexes point into
        # them; not seen, so not guessed at.
        raise _unreadable()
    width = len(plan.measures)
    _member_types(structure, width)
    if _count(columns.get("TupleCount")) != width:
        raise _unreadable()
    tuples = columns.get("Tuples")
    if not isinstance(tuples, list) or len(tuples) != 1:
        raise _unreadable()
    order = _indexes(_object(tuples[0]).get("MemberIndexes"), width, width)
    if sorted(order) != list(range(width)):
        raise _unreadable()
    return [plan.measures[index] for index in order]


def _cell_value(value: object) -> int | float:
    """A cell's number; anything else is unreadable, never null."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _unreadable()
    if isinstance(value, float) and not math.isfinite(value):
        raise _unreadable()
    return value


def _too_many_cells() -> InaError:
    return InaError(
        "result_too_large",
        f"the answer has more than {MAX_CELLS} cells",
        hint="group by fewer dimensions, ask for fewer measures, filter, or lower top",
    )


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
        raise _unreadable()
    grid = _object(grids[0])
    rows_axis, columns_axis = _axes(grid)

    count = _count(rows_axis.get("TupleCount"))
    total = _count(rows_axis.get("TupleCountTotal", count))
    if total < count:
        raise _unreadable()
    if count > plan.top + 1:
        raise InaError(
            "result_too_large",
            "SAP answered with more rows than were asked for",
            hint="filter, or group by fewer dimensions",
        )
    shown = min(count, plan.top)
    if shown * len(plan.measures) > MAX_CELLS:
        raise _too_many_cells()

    dimensions = _dimensions(rows_axis, plan.group_by)
    tuples = rows_axis.get("Tuples")
    if not isinstance(tuples, list) or len(tuples) != len(plan.group_by):
        raise _unreadable()
    # The member keys (and texts) of each requested dimension, row by row.
    keys_by_row: dict[str, list[str]] = {}
    texts_by_row: dict[str, list[str] | None] = {}
    for dimension, entry in zip(rows_axis["Dimensions"], tuples):
        name = dimension["Name"]
        keys, texts = _member_table(
            dimensions[name], plan.keys[name], plan.texts.get(name), plan.top + 1
        )
        indexes = _indexes(_object(entry).get("MemberIndexes"), count, len(keys))
        keys_by_row[name] = [keys[i] for i in indexes]
        texts_by_row[name] = None if texts is None else [texts[i] for i in indexes]

    measures = _measure_order(columns_axis, plan)
    width = len(measures)
    if grid.get("CellArraySizes") != [count, width]:
        raise _unreadable()
    cells = _object(grid.get("Cells"))
    values = _values(cells.get("Values"), count * width)
    if "Exceptions" in cells:
        if any(e != 0 or isinstance(e, bool) for e in _values(cells["Exceptions"], count * width)):
            raise _unreadable()
    units: list[Any] = [""] * (count * width)
    if "Units" in cells:
        units = _values(cells["Units"], count * width)
        if not all(isinstance(u, str) for u in units):
            raise _unreadable()
    # Every cell is checked, also those of the extra row that is not returned.
    numbers = [_cell_value(v) for v in values]

    measured: list[dict[str, tuple[int | float, str | None]]] = []
    for row in range(shown):
        measured.append({
            measure: (numbers[row * width + column],
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


__all__ = ["MAX_CELLS", "read_grid"]
