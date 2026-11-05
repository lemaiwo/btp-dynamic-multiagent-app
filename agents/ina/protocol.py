"""The InA wire format of the built-in: what is sent, what is read back.

Pure functions and no I/O, for :mod:`agents.ina.client`, which sends what
these build and hands them what SAP answered:

* :func:`plan_run` checks a run's arguments against the caps and the
  query's description (``models.InaDefinition``) before anything is sent,
  and :func:`analytics_request` / :func:`describe_request` build the two
  ``GetResponse`` bodies (design addendum (k), part 2 point 2);
* :func:`analytics_capabilities` reads the ``Analytics`` capability names of
  a ``GetServerInfo`` answer, :func:`read_description` a query's dimensions,
  measures and variables, :func:`text_attributes` the text attribute of each
  row dimension a VariableSubmit answer names, :func:`read_grid`
  (``grid.py``) the rows of a result, and :func:`sap_message_error` the
  refusal an answer's ``Messages`` carry.

What leaves is default-deny. Of a grid only the requested dimensions (key,
and text when SAP sent one) and measures (value, and unit) come back, text
cleaned of control, format and separator characters and cut, and a grid of a
form the reader does not know is refused (``sap_error``), not guessed at. A
filter or variable value is a string or a number and only ever becomes a
JSON string of the body. Of SAP's own words only the text of an error
message passes, cleaned, URL- and credential-masked
(``agents.errors.mask_text``) and cut at 500 characters. Every refusal is an
``InaError`` that names the argument and the rule, never a value the caller
sent.

Verified against SIA (S/4HANA 2022, SAP_BW 757), recorded 2026-11-04:
the describe (``tests/fixtures/ina/describe_sia.json``: the ``Cube`` with its
``Dimensions``, the measure structure ``DimensionType`` 2 under a generated
name, its members keyed ``<structure>.KEY``; the ``Variables``, which say
"mandatory" as ``InputType`` and name the dimension their values belong to)
and a run, which is three requests on one ``DataSource.InstanceId`` in one
stateful SAP session (``agents/ina/client.py`` sends them):

1. :func:`variable_submit_request`, only when the run has variables: SIA has
   no ``DirectVariableTransfer``, so values in the read alone are not taken
   ("the query can only run once all mandatory variables have values",
   ``run_sia_variables_missing.json``). Each value is named by the key of
   the variable's dimension. Its answer's ``Cube`` names the text attribute
   of a dimension (``submit_sia.json``), which the describe answer does not.
2. :func:`analytics_request`, the read: each row dimension lists its key
   and text attribute (without them SIA sent no member keys) and hides its
   totals (without that SIA sent subtotal and total rows); ``RowTo`` is
   ``top + 1`` and cuts the rows, no ``MaxResultRecords``, as in the reads
   SIA answered so (``run_sia_grid.json``).
3. :func:`close_request`, which releases the instance (SIA answered
   "Query ... has been closed").

Not seen on SIA: a read of several measures (the column order would be
inferred, so :func:`check_caps` refuses more than one measure per run until a
probe verifies it), a ``Filter`` or a ``Sort`` (no recorded request held
one; the submit carries them too), and variables of type interval or
multiple (their reading is inferred). The requests are those the probe
sent; this module's own sequence has not run against the system yet.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from agents.errors import mask_text
from agents.odata.common import plain_float

# The description reader lives in its own module; its names are re-exported.
from .description import (
    _MAX_SCANNED,
    MEASURES_DIMENSION,
    UNREADABLE,
    InaDescription,
    _label,
    fits_name,
    read_description,
    shown_name,
)
from .errors import InaError

# The grid reader holds the cap of an answer's cells; both names are kept here.
from .grid import MAX_CELLS, read_grid
from .models import InaDefinition, InaDimension

# The caps of one run (design addendum (k), part 2 point 2; MAX_CELLS is the
# grid reader's).
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
# A row dimension of a read lists its members and hides its totals (SAP's
# `DefaultResultStructure`): a total row would read as one more member.
HIDE_TOTALS = (
    {"Result": "Members", "Visibility": "Visible"},
    {"Result": "Total", "Visibility": "Hidden"},
)

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
    # The key attribute of each row dimension: the described key field, else
    # `<dimension>.KEY` (SIA's `KeyAttribute`).
    keys: dict[str, str] = field(default_factory=dict)
    # The text attribute of a row dimension, when the VariableSubmit answer
    # named one (`text_attributes`); set by the client before the read.
    texts: dict[str, str] = field(default_factory=dict)


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
    """The checks of a run that need no description: types and caps, one
    measure per run among them (so the client refuses two before it reads an
    undescribed query)."""
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
    if len(measures) > 1:
        # Every grid SIA answered held one measure. With several, nothing in
        # the answer names the measure of a column (the structure comes back
        # without attributes), so a wrong reading of the order would be wrong
        # numbers under the right names: refused until a probe verifies it.
        raise InaError(
            "invalid_filter",
            "measures: one measure per run until the column order is verified",
            hint="run the query once per measure",
        )
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


def _selection(field_name: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {"SetOperand": {"FieldName": field_name, "Elements": elements}}


def key_of(dimension: InaDimension) -> str:
    """The key attribute of a dimension, the one name a filter and the read
    use: the described key field, else ``<dimension>.KEY`` (the system's
    ``KeyAttribute`` for each of its dimensions)."""
    return dimension.key_field or f"{dimension.name}.KEY"


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
        selections.append(
            _selection(
                key_of(dimensions[name]),
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
        inclusive: list[dict[str, Any]] = [{**e, "IsExcluding": False} for e in elements]
        wire_variables.append(
            {
                "Name": variable.name,
                "Values": {
                    "Selection": _selection(variable.key_field or variable.name, inclusive)
                },
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
        keys={name: key_of(dimensions[name]) for name in chosen_dimensions},
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


def _row(plan: RunPlan, name: str, *, submit: bool) -> dict[str, Any]:
    """A row dimension of a request. The read lists the key attribute and
    the text attribute (when known) -- without them SIA sent no member keys
    at all (run 6) -- and hides the totals; the submit keeps the plain form
    SIA accepted (runs 5 to 8)."""
    entry: dict[str, Any] = {"Name": name, "Axis": "Rows"}
    if not submit:
        attributes = [plan.keys.get(name) or f"{name}.KEY"]
        if plan.texts.get(name):
            attributes.append(plan.texts[name])
        entry["Attributes"] = [{"Name": a, "Obtainability": "Always"} for a in attributes]
        entry["ResultStructure"] = [dict(r) for r in HIDE_TOTALS]
    return entry


def _run_body(
    capabilities: list[str], source: dict[str, str], plan: RunPlan, *, submit: bool
) -> dict[str, Any]:
    feature: dict[str, Any] = {
        "ResultFormat": "Version2",
        "ResultEncoding": "None",
        # One row more than top: the extra row is how "there is more" is told
        # from "that is all"; it is never returned. `RowTo` alone cuts the
        # rows (run 7), and `TupleCountTotal` says how many exist.
        "SubSetDescription": {
            "RowFrom": 0,
            "RowTo": plan.top + 1,
            "ColumnFrom": 0,
            "ColumnTo": -1,
        },
        "UseDefaultAttributeKey": False,
    }
    if submit:
        # As SIA accepted it as a submit (runs 5 to 8); the reads that came
        # back without totals (runs 7, 8) left it out.
        feature["MaxResultRecords"] = plan.top + 1
    definition: dict[str, Any] = {
        "Dimensions": [
            *(_row(plan, name, submit=submit) for name in plan.group_by),
            {
                "Name": plan.structure,
                "Axis": "Columns",
                "Members": [{"Name": m, "Visibility": "Visible"} for m in plan.measures],
            },
        ],
        "ResultSetFeatureRequest": feature,
    }
    if plan.selections:
        definition["Filter"] = {
            "Selection": {"Operator": {"Code": "And", "SubSelections": plan.selections}}
        }
    if plan.sort:
        definition["Sort"] = plan.sort
    if plan.variables:
        definition["Variables"] = plan.variables
    analytics: dict[str, Any] = {
        "Capabilities": capabilities,
        "DataSource": source,
        "Definition": definition,
        "Language": "EN",
    }
    if submit:
        analytics["ProcessingDirectives"] = {"ProcessingStep": "VariableSubmit"}
    return {"ClientInfo": CLIENT_INFO, "Analytics": analytics, "Options": list(_OPTIONS)}


def analytics_request(
    capabilities: list[str], source: dict[str, str], plan: RunPlan
) -> dict[str, Any]:
    """The ``GetResponse`` body of a run's read, in the form SIA answered
    with keys and texts (run 8): each row dimension lists its key and text
    attribute (``plan.keys``, ``plan.texts``) and hides its totals, and
    ``RowTo`` is ``top + 1``."""
    return _run_body(capabilities, source, plan, submit=False)


def variable_submit_request(
    capabilities: list[str], source: dict[str, str], plan: RunPlan
) -> dict[str, Any]:
    """The VariableSubmit SIA needs before a read with variables, sent as a
    request of its own on the read's ``InstanceId`` (SIA has no
    ``DirectVariableTransfer``: values in the read alone are not taken; a
    ``Batch`` of the two was answered with the submit's ``Cube`` alone).
    The body is the read of the form SIA accepted as a submit in runs 5 to 8
    (row dimensions without attributes, ``MaxResultRecords``) with
    ``ProcessingDirectives.ProcessingStep``. SAP's own InA client does the
    same (SAPUI5 ``sap/sac/df/firefly``, ``ff4400.olap.providers.js``,
    ``InAQMgrVarAction`` / ``ProcessingStep.VARIABLE_SUBMIT``)."""
    return _run_body(capabilities, source, plan, submit=True)


def text_attributes(cube: object, group_by: list[str]) -> dict[str, str]:
    """The text attribute of each of ``group_by`` that a VariableSubmit
    answer's ``Cube`` names (``Dimensions[].TextAttribute``; the describe
    answer names it for few dimensions). Taken only when it passes the
    gate's name rule, belongs to the dimension (``<dimension>.``) and is not
    its key; anything else asks for the key alone."""
    dimensions = cube.get("Dimensions") if isinstance(cube, dict) else None
    if not isinstance(dimensions, list):
        return {}
    wanted = set(group_by)
    out: dict[str, str] = {}
    for raw in dimensions[:_MAX_SCANNED]:
        name = raw.get("Name") if isinstance(raw, dict) else None
        if not isinstance(name, str) or name not in wanted or name in out:
            continue
        text = raw.get("TextAttribute")
        if (
            fits_name(text)
            and text.startswith(f"{name}.")
            and text != raw.get("KeyAttribute", f"{name}.KEY")
            and text != f"{name}.KEY"
        ):
            out[name] = text
    return out


def close_request(instance_id: str) -> dict[str, Any]:
    """Release a run's query instance in the SAP session, as firefly's
    ``InAQMgrShutdownAction`` does (``Actions: [{Type: Close, DataSources:
    [<InstanceId>]}]``): the session outlives the run, the instance need not."""
    return {
        "Analytics": {"Actions": [{"Type": "Close", "DataSources": [instance_id]}]},
        "Options": list(_OPTIONS),
    }


__all__ = [
    "CLIENT_INFO",
    "DEFAULT_TOP",
    "DESCRIBE_EXPAND",
    "FILTER_OPERATORS",
    "HIDE_TOTALS",
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
    "key_of",
    "close_request",
    "data_source",
    "describe_request",
    "fits_name",
    "plan_run",
    "read_description",
    "read_grid",
    "sap_message_error",
    "shown_name",
    "text_attributes",
    "variable_submit_request",
]
