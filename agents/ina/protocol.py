"""The InA wire format of the built-in: what is sent, what is read back.

Pure functions and no I/O, for :mod:`agents.ina.client`, which sends what
these build and hands them what SAP answered:

* :func:`plan_run` checks a run's arguments against the caps and the
  query's description (``models.InaDefinition``) before anything is sent,
  and :func:`analytics_request` / :func:`describe_request` build the two
  ``GetResponse`` bodies (design addendum (k), part 2 point 2);
* :func:`analytics_capabilities` reads the ``Analytics`` capability names of
  a ``GetServerInfo`` answer, :func:`read_description` a query's dimensions,
  measures and variables, :func:`read_grid` the rows of a result, and
  :func:`sap_message_error` the refusal an answer's ``Messages`` carry.

What leaves is default-deny. Of a grid only the requested dimensions (key,
and text when SAP sent one) and measures (value, and unit) come back, text
cleaned of control, format and separator characters and cut; a column of
anything else is dropped, and a grid of a form this module does not know is
refused (``sap_error``), not guessed at. A filter or variable value is a
string or a number and only ever becomes a JSON string of the body. Of SAP's
own words only the text of an error message passes, cleaned, URL- and
credential-masked (``agents.errors.mask_text``) and cut at 500 characters.
Every refusal is an ``InaError`` that names the argument and the rule, never
a value the caller sent.

The describe side is verified against SIA (S/4HANA 2022, SAP_BW 757;
``tests/fixtures/ina/describe_sia.json``, recorded 2026-11-04): the
``Cube`` with its ``Dimensions`` (the measure structure is ``DimensionType``
2 under a generated name, its members keyed ``<structure>.KEY``), and the
``Variables``, which say "mandatory" as ``InputType`` and their values as
``MultipleValues`` / ``FilterCapability.ComparisonGroup``. Not verified
against a landscape (``describe_ok.json`` and ``run_ok.json`` are
synthetic): the request of a run and the shape of the result grid. What SIA
answered to the run as built here, also with values for its mandatory
variables: "the query can only run once all mandatory variables have
values" (``run_sia_variables_missing.json``); SAP's own InA client submits
the variables first (``ProcessingDirectives.ProcessingStep``
``VariableSubmit`` on one ``InstanceId``), and names each value by the key
of the variable's dimension, not by the variable. Each is built or read in
one place here for the next recorded run to correct.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from agents.errors import mask_text
from agents.odata.common import plain_float

# The description reader lives in its own module; its names are re-exported.
from .description import (
    _MAX_SCANNED,
    MEASURES_DIMENSION,
    UNREADABLE,
    InaDescription,
    _key,
    _label,
    fits_name,
    read_description,
    shown_name,
)
from .errors import InaError
from .models import InaDefinition

# The caps of one run (design addendum (k), part 2 point 2).
MAX_CELLS = 5000
MAX_DIMENSIONS = 20
MAX_MEASURES = 20
MAX_FILTERS = 30
MAX_TOP = 2000
DEFAULT_TOP = 100
MAX_ORDER = 10
MAX_IN_VALUES = 50
MAX_VALUE_CHARS = 255
FILTER_OPERATORS = ("=", "<>", ">=", "<=", "between", "in")

MAX_MESSAGE_CHARS = 500
_MAX_CAPABILITIES = 1000
_MAX_MESSAGES = 50
# What a grid may hold before it is parsed at all: `top + 1` rows of every
# measure a run may name.
_MAX_RAW_CELLS = (MAX_TOP + 1) * MAX_MEASURES

CLIENT_INFO = {"Version": "1.0", "Identifier": "btp-dynamic-multiagent", "Component": "builtin-ina"}
DESCRIBE_EXPAND = [
    "Cube",
    "Axis?Name=Rows",
    "Axis?Name=Columns",
    "Axis?Name=Filter",
    "Variables",
    "DynamicFilter",
]
_OPTIONS = ["StatefulServer"]

# The tool's operator and the InA comparison it is sent as; `in` is one
# element per value.
_COMPARISON = {"=": "=", "<>": "<>", ">=": ">=", "<=": "<=", "between": "BETWEEN"}
_FILTER_KEYS = frozenset({"dimension", "operator", "value"})
_ORDER_KEYS = frozenset({"by", "direction"})

_CAPABILITY = re.compile(r"[A-Za-z0-9_.\-]{1,128}")
# SAP's text for a query it does not know, in the two languages seen so far
# ("Query X ist unbekannt" on SIA).
# The name is one token (an InA query name has no white space); anything
# between it and the phrase ("Query X, variable Y not found") is another message.
_UNKNOWN_QUERY = re.compile(
    r"^(?:query|abfrage)\s+[^\s,;:]{1,130}\s+(?:ist unbekannt|is unknown|does not exist"
    r"|existiert nicht|not found)\.?$",
    re.IGNORECASE,
)
logger = logging.getLogger(__name__)

TELL_THE_USER = "tell the user instead of retrying"



def _message_text(value: object) -> str:
    """An InA message text for the model: one line, masked, at most 500."""
    return mask_text(_label(value, MAX_MESSAGE_CHARS * 4))[:MAX_MESSAGE_CHARS]


def _is_error_message(kind: object) -> bool:
    """InA message type 2 is an error (0 information, 1 warning)."""
    if isinstance(kind, bool):
        return False
    if isinstance(kind, int):
        return kind == 2
    return isinstance(kind, str) and kind.strip().lower() in ("2", "e", "error")


def sap_message_error(payload: object) -> InaError | None:
    """The refusal an answer's ``Messages`` carry, or None.

    Only a message of type error counts; information and warnings are not
    passed on at all. An unknown query is ``query_unknown`` with a fixed
    text; any other error is ``sap_error`` with SAP's text, masked and cut.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("Messages"), list):
        return None
    for message in payload["Messages"][:_MAX_MESSAGES]:
        if not isinstance(message, dict) or not _is_error_message(message.get("Type")):
            continue
        raw = _label(message.get("Text"), 400)
        if _UNKNOWN_QUERY.match(raw):
            return InaError(
                "query_unknown",
                "SAP does not know the InA query of this registration",
                hint="the query's InA name may be wrong or the query not released for "
                "InA; " + TELL_THE_USER,
            )
        text = _message_text(message.get("Text"))
        return InaError("sap_error", text or "SAP refused the request")
    return None


def analytics_capabilities(payload: object) -> list[str] | None:
    """The capability names of the ``Analytics`` service of a
    ``GetServerInfo`` answer; None when the service is not there."""
    services = payload.get("Services") if isinstance(payload, dict) else None
    if not isinstance(services, list):
        return None
    for service in services[:_MAX_SCANNED]:
        if isinstance(service, dict) and service.get("Service") == "Analytics":
            raw = service.get("Capabilities")
            names: list[str] = []
            for item in raw if isinstance(raw, list) else []:
                name = item.get("Capability") if isinstance(item, dict) else item
                if isinstance(name, str) and _CAPABILITY.fullmatch(name) and name not in names:
                    names.append(name)
                if len(names) >= _MAX_CAPABILITIES:
                    break
            return names
    return None


# -- a run -----------------------------------------------------------------


@dataclass
class RunPlan:
    """A run's checked arguments, in the form the request and the grid
    reader need. Made only by :func:`plan_run`."""

    measures: list[str]
    group_by: list[str]
    selections: list[dict[str, Any]]
    sort: list[dict[str, Any]]
    variables: list[dict[str, Any]]
    top: int
    # The measure structure the measures are members of, on Columns.
    structure: str = MEASURES_DIMENSION


def _refuse(field_name: str, rule: str, code: str = "invalid_filter") -> InaError:
    return InaError(code, f"{field_name}: {rule}")


def _wire_value(value: object, where: str) -> str:
    """A filter or variable value as the text InA gets: a string as it is,
    a number in plain digits. Anything else is refused, naming ``where``."""
    if isinstance(value, bool) or value is None:
        raise _refuse(where, "a string or a number")
    if isinstance(value, str):
        text = value
    elif isinstance(value, int):
        try:
            text = str(value)
        except ValueError:  # more digits than Python converts (sys.int_info)
            raise _refuse(where, f"at most {MAX_VALUE_CHARS} characters") from None
    elif isinstance(value, float):
        float_text = plain_float(value)
        if float_text is None:
            raise _refuse(where, "a number with at most 15 significant digits, as plain digits")
        text = float_text
    else:
        raise _refuse(where, "a string or a number")
    if len(text) > MAX_VALUE_CHARS:
        raise _refuse(where, f"at most {MAX_VALUE_CHARS} characters")
    return text


def check_caps(
    *, measures: Any, group_by: Any, filters: Any, top: Any, order: Any, variables: Any
) -> None:
    """The checks of a run that need no description: types and caps."""
    for value, where, cap, kind, minimum in (
        (measures, "measures", MAX_MEASURES, "measure", 1),
        (group_by, "group_by", MAX_DIMENSIONS, "dimension", 0),
    ):
        if not isinstance(value, (list, tuple)):
            raise _refuse(where, f"a list of {kind} names")
        if len(value) > cap:
            raise _refuse(where, f"at most {cap} {kind}s", "result_too_large")
        if len(value) < minimum:
            raise _refuse(where, f"at least {minimum} {kind}")
    if isinstance(top, bool) or not isinstance(top, int) or top < 1:
        raise _refuse("top", f"a whole number from 1 to {MAX_TOP}")
    if top > MAX_TOP:
        raise _refuse("top", f"at most {MAX_TOP}", "result_too_large")
    if filters is not None and not isinstance(filters, (list, tuple)):
        raise _refuse("filters", "a list of filters")
    if filters is not None and len(filters) > MAX_FILTERS:
        raise _refuse("filters", f"at most {MAX_FILTERS} filters")
    if order is not None and (not isinstance(order, (list, tuple)) or len(order) > MAX_ORDER):
        raise _refuse("order", f"a list of at most {MAX_ORDER} entries")
    if variables is not None and not isinstance(variables, dict):
        raise _refuse("variables", "an object of variable names and values")


def _names(value: Any, where: str, known: dict[str, Any], kind: str) -> list[str]:
    """``value`` (a list :func:`check_caps` has seen) as distinct known names."""
    seen: list[str] = []
    for index, name in enumerate(value):
        if not isinstance(name, str) or name not in known:
            raise _refuse(f"{where}[{index}]", f"not a {kind} of this query")
        if name in seen:
            raise _refuse(f"{where}[{index}]", f"repeats an earlier {kind}")
        seen.append(name)
    return seen


def _elements(operator: str, value: object, where: str) -> list[dict[str, str]]:
    if operator == "in":
        if not isinstance(value, list) or not 1 <= len(value) <= MAX_IN_VALUES:
            raise _refuse(where, f"a list of 1 to {MAX_IN_VALUES} values for 'in'")
        return [
            {"Comparison": "=", "Low": _wire_value(v, f"{where}[{i}]")} for i, v in enumerate(value)
        ]
    if operator == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise _refuse(where, "a list of two values, low and high, for 'between'")
        return [
            {
                "Comparison": _COMPARISON["between"],
                "Low": _wire_value(value[0], f"{where}[0]"),
                "High": _wire_value(value[1], f"{where}[1]"),
            }
        ]
    return [{"Comparison": _COMPARISON[operator], "Low": _wire_value(value, where)}]


def _selection(field_name: str, elements: list[dict[str, str]]) -> dict[str, Any]:
    return {"SetOperand": {"FieldName": field_name, "Elements": elements}}


def plan_run(
    definition: InaDefinition,
    *,
    measures: Any,
    group_by: Any,
    filters: Any,
    top: Any,
    order: Any,
    variables: Any,
) -> RunPlan:
    """Check a run's arguments against the caps and ``definition``.

    Every refusal names the argument and the rule, never a value the
    caller sent; a name is repeated only when it comes from the
    definition (a mandatory variable).
    """
    check_caps(
        measures=measures,
        group_by=group_by,
        filters=filters,
        top=top,
        order=order,
        variables=variables,
    )
    filters, order, variables = filters or [], order or [], variables or {}
    dimensions = {d.name: d for d in definition.dimensions}
    known_measures = {m.name: m for m in definition.measures}
    structure = definition.measure_structure or MEASURES_DIMENSION
    chosen_measures = _names(measures, "measures", known_measures, "measure")
    chosen_dimensions = _names(group_by, "group_by", dimensions, "dimension")

    selections: list[dict[str, Any]] = []
    for index, entry in enumerate(filters):
        where = f"filters[{index}]"
        if not isinstance(entry, dict) or set(entry) != _FILTER_KEYS:
            raise _refuse(where, "an object of dimension, operator and value")
        name, operator = entry["dimension"], entry["operator"]
        if not isinstance(name, str) or name not in dimensions:
            raise _refuse(f"{where}.dimension", "not a dimension of this query")
        if operator not in FILTER_OPERATORS:
            raise _refuse(f"{where}.operator", f"one of {', '.join(FILTER_OPERATORS)}")
        target = dimensions[name]
        selections.append(
            _selection(
                target.key_field or target.name,
                _elements(operator, entry["value"], f"{where}.value"),
            )
        )

    sort: list[dict[str, Any]] = []
    ordered: set[str] = set()
    for index, entry in enumerate(order):
        where = f"order[{index}]"
        if not isinstance(entry, dict) or "by" not in entry or not set(entry) <= _ORDER_KEYS:
            raise _refuse(where, "an object of by and direction")
        by, direction = entry["by"], entry.get("direction", "asc")
        if direction not in ("asc", "desc"):
            raise _refuse(f"{where}.direction", "asc or desc")
        if not isinstance(by, str) or by not in (*chosen_dimensions, *chosen_measures):
            raise _refuse(f"{where}.by", "not a dimension or measure of this run")
        if by in ordered:
            raise _refuse(f"{where}.by", "repeats an earlier entry")
        ordered.add(by)
        wire_direction = "Desc" if direction == "desc" else "Asc"
        if by in chosen_measures:
            sort.append(
                {
                    "Dimension": structure,
                    "SortType": "Measure",
                    "MeasureName": by,
                    "Direction": wire_direction,
                }
            )
        else:
            sort.append({"Dimension": by, "SortType": "MemberKey", "Direction": wire_direction})

    known_variables = {v.name: v for v in definition.variables}
    for name in variables:
        if name not in known_variables:
            raise _refuse("variables", "a name that is not a variable of this query")
    wire_variables: list[dict[str, Any]] = []
    for variable in definition.variables:
        value = variables.get(variable.name)
        if value is None or value == "" or value == []:
            if variable.mandatory:
                raise InaError(
                    "variable_required",
                    f"variables.{variable.name}: the query needs a value for this "
                    f"mandatory variable",
                    hint="ask the user for the value",
                )
            continue
        where = f"variables.{variable.name}"
        if variable.type == "single":
            if isinstance(value, list):
                raise _refuse(where, "one value")
            elements = _elements("=", value, where)
        elif variable.type == "interval":
            if not isinstance(value, list):
                raise _refuse(where, "a list of two values, low and high")
            elements = _elements("between", value, where)
        else:
            if not isinstance(value, list):
                raise _refuse(where, f"a list of 1 to {MAX_IN_VALUES} values")
            elements = _elements("in", value, where)
        # SIA's form (recorded run 5): the value named by its dimension's
        # key, each element marked inclusive.
        for element in elements:
            element["IsExcluding"] = False  # type: ignore[assignment]
        wire_variables.append(
            {
                "Name": variable.name,
                "Values": {"Selection": _selection(variable.key_field or variable.name, elements)},
            }
        )
    return RunPlan(
        measures=chosen_measures,
        group_by=chosen_dimensions,
        selections=selections,
        sort=sort,
        variables=wire_variables,
        top=top,
        structure=structure,
    )


# -- the requests -----------------------------------------------------------


def data_source(query: str, package: str, instance_id: str = "") -> dict[str, str]:
    """``DataSource`` of a request; ``PackageName`` is left out when blank,
    as in the request SIA accepted. ``InstanceId`` names the query instance
    a run's VariableSubmit, read and Close share in the SAP session."""
    source = {"ObjectName": query, "Type": "Query"}
    if package:
        source["PackageName"] = package
    if instance_id:
        source["InstanceId"] = instance_id
    return source


def describe_request(capabilities: list[str], source: dict[str, str]) -> dict[str, Any]:
    """The ``GetResponse`` body of a describe: the envelope SIA parsed."""
    return {
        "ClientInfo": CLIENT_INFO,
        "Metadata": {
            "Capabilities": capabilities,
            "Context": "Analytics",
            "DataSource": source,
            "Expand": list(DESCRIBE_EXPAND),
            "Language": "EN",
        },
        "Options": list(_OPTIONS),
    }


def analytics_request(
    capabilities: list[str], source: dict[str, str], plan: RunPlan
) -> dict[str, Any]:
    """The ``GetResponse`` body of a run."""
    definition: dict[str, Any] = {
        "Dimensions": [
            *({"Name": name, "Axis": "Rows"} for name in plan.group_by),
            {
                "Name": plan.structure,
                "Axis": "Columns",
                "Members": [{"Name": m, "Visibility": "Visible"} for m in plan.measures],
            },
        ],
        "ResultSetFeatureRequest": {
            "ResultFormat": "Version2",
            "ResultEncoding": "None",
            # One row more than top: the extra row is how "there is more" is
            # told from "that is all"; it is never returned.
            "MaxResultRecords": plan.top + 1,
            "SubSetDescription": {
                "RowFrom": 0,
                "RowTo": plan.top + 1,
                "ColumnFrom": 0,
                "ColumnTo": -1,
            },
            "UseDefaultAttributeKey": False,
        },
    }
    if plan.selections:
        definition["Filter"] = {
            "Selection": {"Operator": {"Code": "And", "SubSelections": plan.selections}}
        }
    if plan.sort:
        definition["Sort"] = plan.sort
    if plan.variables:
        definition["Variables"] = plan.variables
    return {
        "ClientInfo": CLIENT_INFO,
        "Analytics": {
            "Capabilities": capabilities,
            "DataSource": source,
            "Definition": definition,
            "Language": "EN",
        },
        "Options": list(_OPTIONS),
    }


def variable_submit_request(
    capabilities: list[str], source: dict[str, str], plan: RunPlan
) -> dict[str, Any]:
    """The VariableSubmit SIA needs before a read with variables: the read's
    body with ``ProcessingDirectives.ProcessingStep``, sent as a request of
    its own on the read's ``InstanceId`` (SIA has no
    ``DirectVariableTransfer``: values in the read alone are not taken; a
    ``Batch`` of the two was answered with the submit's ``Cube`` alone).
    SAP's own InA client does the same (SAPUI5 ``sap/sac/df/firefly``,
    ``ff4400.olap.providers.js``, ``InAQMgrVarAction`` /
    ``ProcessingStep.VARIABLE_SUBMIT``)."""
    body = analytics_request(capabilities, source, plan)
    body["Analytics"]["ProcessingDirectives"] = {"ProcessingStep": "VariableSubmit"}
    return body


def close_request(instance_id: str) -> dict[str, Any]:
    """Release a run's query instance in the SAP session, as firefly's
    ``InAQMgrShutdownAction`` does (``Actions: [{Type: Close, DataSources:
    [<InstanceId>]}]``): the session outlives the run, the instance need not."""
    return {
        "Analytics": {"Actions": [{"Type": "Close", "DataSources": [instance_id]}]},
        "Options": list(_OPTIONS),
    }


# -- the grid ---------------------------------------------------------------


def _cells_value(value: object) -> Any:
    """A cell's value as the model gets it, or raise when it is no value."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise InaError("sap_error", UNREADABLE)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise InaError("sap_error", UNREADABLE)
        return value
    if isinstance(value, str):
        return _key(value, MAX_VALUE_CHARS)
    raise InaError("sap_error", UNREADABLE)


def _axis(axes: list[Any], name: str, kind: int) -> dict[str, Any] | None:
    found = [
        a for a in axes if isinstance(a, dict) and (a.get("Name") == name or a.get("Type") == kind)
    ]
    if len(found) > 1:
        raise InaError("sap_error", UNREADABLE)
    return found[0] if found else None


def _axis_dimensions(axis: dict[str, Any]) -> list[str]:
    raw = axis.get("Dimensions")
    if not isinstance(raw, list):
        raise InaError("sap_error", UNREADABLE)
    names = [d.get("Name") if isinstance(d, dict) else None for d in raw]
    if not all(isinstance(n, str) for n in names):
        raise InaError("sap_error", UNREADABLE)
    return names  # type: ignore[return-value]


def _tuples(axis: dict[str, Any], width: int) -> list[list[tuple[str, str]]]:
    """``(key, text)`` per member of every tuple of ``axis``."""
    raw = axis.get("Tuples")
    if not isinstance(raw, list) or len(raw) > MAX_CELLS:
        raise InaError("sap_error", UNREADABLE)
    out: list[list[tuple[str, str]]] = []
    for entry in raw:
        members = entry.get("Members") if isinstance(entry, dict) else None
        if not isinstance(members, list) or len(members) != width:
            raise InaError("sap_error", UNREADABLE)
        row: list[tuple[str, str]] = []
        for member in members:
            key = member.get("Name") if isinstance(member, dict) else None
            if not isinstance(key, str):
                raise InaError("sap_error", UNREADABLE)
            row.append((_key(key, 128), _label(member.get("Text"), 120)))
        out.append(row)
    return out


def read_grid(payload: object, plan: RunPlan) -> dict[str, Any]:
    """The rows of ``Grids[0]`` for the dimensions and measures of ``plan``.

    ``columns`` are the requested dimensions (each followed by
    ``<name>.text`` when SAP sent a text that differs from the key) and
    measures (each followed by ``<name>.unit`` when its unit is not the same
    in every row; otherwise the unit is in ``units``). Columns of anything
    that was not asked for are dropped. The request asks for ``top + 1``
    rows: when that extra row came, ``top`` rows are returned and
    ``truncated`` is true. More than :data:`MAX_CELLS` cells in what would
    be returned, or more than ``top + 1`` rows, is refused, never cut.
    """
    grids = payload.get("Grids") if isinstance(payload, dict) else None
    if not isinstance(grids, list) or not grids or not isinstance(grids[0], dict):
        raise InaError("sap_error", UNREADABLE)
    grid = grids[0]
    axes, cells = grid.get("Axes"), grid.get("Cells")
    if not isinstance(axes, list) or not isinstance(cells, list):
        raise InaError("sap_error", UNREADABLE)
    # A bound on the work before anything is parsed; the cap is applied to
    # what would be returned, below.
    if len(cells) > _MAX_RAW_CELLS:
        raise _too_many_cells()
    columns_axis = _axis(axes, "Columns", 2)
    rows_axis = _axis(axes, "Rows", 1)
    if columns_axis is None or _axis_dimensions(columns_axis) != [MEASURES_DIMENSION]:
        raise InaError("sap_error", UNREADABLE)
    column_measures = [t[0][0] for t in _tuples(columns_axis, 1)]
    if rows_axis is None:
        if plan.group_by:
            raise InaError("sap_error", UNREADABLE)
        row_dimensions: list[str] = []
        row_tuples: list[list[tuple[str, str]]] = [[]] if cells else []
    else:
        row_dimensions = _axis_dimensions(rows_axis)
        if sorted(row_dimensions) != sorted(plan.group_by):
            raise InaError("sap_error", UNREADABLE)
        row_tuples = _tuples(rows_axis, len(row_dimensions))
    if not row_tuples and not cells:
        return {
            "columns": _columns(plan, set(), set()),
            "rows": [],
            "units": {},
            "row_count": 0,
            "truncated": False,
        }
    width = len(column_measures)
    if len(row_tuples) * width != len(cells):
        raise InaError("sap_error", UNREADABLE)
    if len(row_tuples) > plan.top + 1:
        raise InaError(
            "result_too_large",
            "SAP answered with more rows than were asked for",
            hint="filter, or group by fewer dimensions",
        )
    truncated = len(row_tuples) > plan.top
    row_tuples = row_tuples[: plan.top]
    if len(row_tuples) * len(plan.measures) > MAX_CELLS:
        raise _too_many_cells()
    position = {}
    for index, name in enumerate(column_measures):
        if name in plan.measures:
            if name in position:
                raise InaError("sap_error", UNREADABLE)
            position[name] = index
    if set(position) != set(plan.measures):
        raise InaError("sap_error", UNREADABLE)
    dimension_at = {name: row_dimensions.index(name) for name in plan.group_by}

    values: list[dict[str, tuple[Any, str | None]]] = []
    for row_index in range(len(row_tuples)):
        row: dict[str, tuple[Any, str | None]] = {}
        for name, column in position.items():
            cell = cells[row_index * width + column]
            # Only a `Value` that is there and null is null; a cell without
            # one is not a cell this reader knows.
            if not isinstance(cell, dict) or "Value" not in cell:
                raise InaError("sap_error", UNREADABLE)
            unit = cell.get("Unit") if isinstance(cell.get("Unit"), str) else cell.get("Currency")
            row[name] = (_cells_value(cell.get("Value")), _label(unit, 32) or None)
        values.append(row)

    # A text column only where SAP sent a text that says more than the key.
    with_text = {
        name
        for name in plan.group_by
        if any(text and text != key for key, text in (t[dimension_at[name]] for t in row_tuples))
    }
    units: dict[str, str] = {}
    mixed: set[str] = set()
    for name in plan.measures:
        seen = {row[name][1] for row in values}
        if len(seen) > 1:
            mixed.add(name)
        elif seen and (only := next(iter(seen))) is not None:
            units[name] = only
    rows: list[list[Any]] = []
    for members, row in zip(row_tuples, values):
        line: list[Any] = []
        for name in plan.group_by:
            key, text = members[dimension_at[name]]
            line.append(key)
            if name in with_text:
                line.append(text or None)
        for name in plan.measures:
            value, unit = row[name]
            line.append(value)
            if name in mixed:
                line.append(unit)
        rows.append(line)
    return {
        "columns": _columns(plan, with_text, mixed),
        "rows": rows,
        "units": units,
        "row_count": len(rows),
        "truncated": truncated,
    }


def _too_many_cells() -> InaError:
    return InaError(
        "result_too_large",
        f"the answer has more than {MAX_CELLS} cells",
        hint="group by fewer dimensions, ask for fewer measures, filter, or lower top",
    )


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
    "CLIENT_INFO",
    "DEFAULT_TOP",
    "DESCRIBE_EXPAND",
    "FILTER_OPERATORS",
    "MAX_CELLS",
    "MAX_DIMENSIONS",
    "MAX_FILTERS",
    "MAX_IN_VALUES",
    "MAX_MEASURES",
    "MAX_ORDER",
    "MAX_TOP",
    "MEASURES_DIMENSION",
    "TELL_THE_USER",
    "UNREADABLE",
    "InaDescription",
    "RunPlan",
    "analytics_capabilities",
    "analytics_request",
    "check_caps",
    "close_request",
    "data_source",
    "describe_request",
    "fits_name",
    "plan_run",
    "read_description",
    "read_grid",
    "sap_message_error",
    "shown_name",
    "variable_submit_request",
]
