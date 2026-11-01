"""The spec of a web chat dashboard (design addendum (i), decision 2).

A dashboard is what ``create_dashboard`` (``agents/webchat/docgen.py``)
stores: charts, KPI tiles and tables on one page, drawn in the viewer by the
front end's own SVG renderer (``static/chat/render/charts.js``). Nothing but
this spec is stored, and the model never receives the SVG.

The spec is a pydantic model with ``extra="forbid"`` and strict types: an
unknown key is refused, not dropped, ``true`` is no number and ``"3"`` no
size. The same model checks the spec when the tool is called
(:func:`validate_spec`) and again when the stored bytes are read
(:func:`parse_dashboard`, the viewer's preview): a row that somebody wrote
past the tool is never rendered unchecked.

A refusal is a :class:`DashboardError` whose message is one fixed text of
the form ``widgets[3].y: not a column of data``: the place (the widget's
0-based index and the field) and the rule, never a value of the spec. The
model reads it and can correct the call.

Caps (all refusals, nothing is cut): 200,000 characters of JSON for the
whole spec (``MAX_SPEC_CHARS``), 1 MiB stored (``MAX_DASHBOARD_BYTES``),
1..12 widgets, 20 columns and 2,000 rows per data block, 1..8 series per
chart, 60 points in a KPI trend.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from typing import Annotated, Any, Literal, Union

from annotated_types import Len
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    Strict,
    ValidationError,
    model_validator,
)
from pydantic_core import PydanticCustomError

DASHBOARD_MEDIA_TYPE = "application/vnd.sap-btp-agent.dashboard+json"
DASHBOARD_EXTENSION = ".dashboard.json"
MAX_DASHBOARD_BYTES = 1024 * 1024
MAX_SPEC_CHARS = 200_000

MAX_TITLE_CHARS = 200
MAX_DESCRIPTION_CHARS = 2_000
MAX_WIDGETS = 12
MAX_WIDGET_TITLE_CHARS = 120
MAX_COLUMNS = 20
MAX_ROWS = 2_000
MAX_CELL_CHARS = 200
MAX_SERIES = 8
MAX_UNIT_CHARS = 12
MAX_NOTE_CHARS = 300
MAX_KPI_VALUE_CHARS = 40
MAX_DELTA_LABEL_CHARS = 60
MAX_TREND_POINTS = 60

CHART_TYPES = ("bar", "line", "area", "pie", "scatter")
WIDGET_TYPES = (*CHART_TYPES, "kpi", "table")

# The rules a validator states itself (the ``field`` of the error names
# where); every text is fixed.
RULE_NOT_A_COLUMN = "not a column of data"
RULE_NOT_NUMERIC = "column must hold numbers or null in every row"
RULE_PIE_ONE = "a pie chart has exactly one series"
RULE_SCATTER_ONE = "a scatter chart has exactly one series"
RULE_STACKED = "only bar and area charts can be stacked"
RULE_TWICE = "names a column twice"
RULE_ROW_WIDTH = "a row must have one cell per column"
RULE_CELL = "a cell must be a finite number, a text or null"
RULE_CONTROL = "contains a control character"
RULE_EMPTY = "must not be empty"


class DashboardError(ValueError):
    """A refused spec; the message is a fixed text, ``code`` the error code
    the tool answers (``invalid_argument`` or ``too_large``)."""

    def __init__(self, message: str, code: str = "invalid_argument") -> None:
        super().__init__(message)
        self.code = code


def _rule(rule: str, field: str | None = None) -> PydanticCustomError:
    return PydanticCustomError("dashboard_rule", rule, {"field": field} if field else {})


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
def _plain(value: str, *, lines: bool) -> str:
    """Refuse control and format characters (a line feed and a tab are text
    where ``lines`` allows them): the viewer and the HTML download show the
    text as it is stored."""
    for ch in value:
        if lines and ch in "\n\t":
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Zl", "Zp"):
            raise _rule(RULE_CONTROL)
    return value


def _line(value: str) -> str:
    return _plain(value, lines=False)


def _multiline(value: str) -> str:
    return _plain(value, lines=True)


def _non_empty(value: str) -> str:
    if not value.strip():
        raise _rule(RULE_EMPTY)
    return value


def _text(max_chars: int, *, lines: bool = False, required: bool = False) -> Any:
    plain = AfterValidator(_multiline if lines else _line)
    if required:
        return Annotated[str, Strict(), Field(max_length=max_chars), plain,
                         AfterValidator(_non_empty)]
    return Annotated[str, Strict(), Field(max_length=max_chars), plain]


Size = Annotated[int, Strict(), Field(ge=1, le=2)]
Title = _text(MAX_WIDGET_TITLE_CHARS, required=True)
Unit = _text(MAX_UNIT_CHARS)


def _is_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value)))


def _cell(value: Any) -> Any:
    if value is None or _is_number(value):
        return value
    if isinstance(value, str):
        if len(value) > MAX_CELL_CHARS:
            raise PydanticCustomError(
                "dashboard_rule", f"a text cell has at most {MAX_CELL_CHARS} characters")
        return _line(value)
    raise _rule(RULE_CELL)


def _number(value: Any) -> Any:
    if not _is_number(value):
        raise _rule("must be a finite number")
    return value


def _kpi_value(value: Any) -> Any:
    if _is_number(value):
        return value
    if isinstance(value, str):
        if len(value) > MAX_KPI_VALUE_CHARS:
            raise _rule(f"a text value has at most {MAX_KPI_VALUE_CHARS} characters")
        return _non_empty(_line(value))
    raise _rule("must be a finite number or a text")


# Numbers are checked by hand rather than as ``float``: an integer stays an
# integer in the stored JSON, and ``true`` is refused.
Number = Annotated[Any, AfterValidator(_number)]
Cell = Annotated[Any, AfterValidator(_cell)]
ColumnName = _text(MAX_CELL_CHARS, required=True)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TableData(_Strict):
    """``columns`` (the names) and ``rows`` (one cell per column)."""

    columns: Annotated[list[ColumnName], Len(1, MAX_COLUMNS)]
    rows: Annotated[list[list[Cell]], Len(0, MAX_ROWS)]

    @model_validator(mode="after")
    def _shape(self) -> TableData:
        if len(set(self.columns)) != len(self.columns):
            raise _rule(RULE_TWICE, "columns")
        width = len(self.columns)
        for index, row in enumerate(self.rows):
            if len(row) != width:
                raise _rule(RULE_ROW_WIDTH, f"rows[{index}]")
        return self

    def numeric(self, column: str) -> bool:
        at = self.columns.index(column)
        return all(row[at] is None or _is_number(row[at]) for row in self.rows)


class ChartWidget(_Strict):
    type: Literal["bar", "line", "area", "pie", "scatter"]
    title: Title
    data: TableData
    x: Annotated[str, Strict()]
    y: Annotated[list[Annotated[str, Strict()]], Len(1, MAX_SERIES)]
    stacked: Annotated[bool, Strict()] = False
    unit: Unit | None = None
    format: Literal["number", "percent", "currency"] = "number"
    size: Size = 1
    note: _text(MAX_NOTE_CHARS, lines=True) | None = None  # type: ignore[valid-type]

    @model_validator(mode="after")
    def _columns(self) -> ChartWidget:
        columns = self.data.columns
        if self.x not in columns:
            raise _rule(RULE_NOT_A_COLUMN, "x")
        for index, name in enumerate(self.y):
            if name not in columns:
                raise _rule(RULE_NOT_A_COLUMN, f"y[{index}]" if len(self.y) > 1 else "y")
        if len(set(self.y)) != len(self.y):
            raise _rule(RULE_TWICE, "y")
        if self.type == "pie" and len(self.y) != 1:
            raise _rule(RULE_PIE_ONE, "y")
        if self.type == "scatter":
            if len(self.y) != 1:
                raise _rule(RULE_SCATTER_ONE, "y")
            if not self.data.numeric(self.x):
                raise _rule(RULE_NOT_NUMERIC, "x")
        if self.stacked and self.type not in ("bar", "area"):
            raise _rule(RULE_STACKED, "stacked")
        for index, name in enumerate(self.y):
            if not self.data.numeric(name):
                raise _rule(RULE_NOT_NUMERIC, f"y[{index}]" if len(self.y) > 1 else "y")
        return self


class KpiWidget(_Strict):
    type: Literal["kpi"]
    title: Title
    value: Annotated[Any, AfterValidator(_kpi_value)]
    unit: Unit | None = None
    delta: Number | None = None
    delta_label: _text(MAX_DELTA_LABEL_CHARS) | None = None  # type: ignore[valid-type]
    trend: Annotated[list[Number], Len(0, MAX_TREND_POINTS)] | None = None
    size: Size = 1


class TableWidget(_Strict):
    type: Literal["table"]
    title: Title
    data: TableData
    size: Size = 1


Widget = Annotated[Union[ChartWidget, KpiWidget, TableWidget], Field(discriminator="type")]


class DashboardSpec(_Strict):
    title: _text(MAX_TITLE_CHARS, required=True)  # type: ignore[valid-type]
    description: _text(MAX_DESCRIPTION_CHARS, lines=True) | None = None  # type: ignore[valid-type]
    widgets: Annotated[list[Widget], Len(1, MAX_WIDGETS)]


# ---------------------------------------------------------------------------
# Refusal texts
# ---------------------------------------------------------------------------
_KEY_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,31}")


def _where(loc: tuple[Any, ...], field: str | None) -> str:
    """``('widgets', 3, 'bar', 'data', 'rows', 5, 2)`` -> ``widgets[3].data.rows[5][2]``;
    the discriminator's tag pydantic puts after a widget's index is left out."""
    out = ""
    for index, part in enumerate(loc):
        if isinstance(part, int):
            out += f"[{part}]"
        elif index == 2 and loc[0] == "widgets" and part in WIDGET_TYPES:
            continue
        else:
            out += f".{part}" if out else str(part)
    if field:
        out += f".{field}" if out and not field.startswith("[") else field
    return out or "dashboard"


def _expected(ctx: dict[str, Any]) -> str:
    return str(ctx.get("expected", "")).replace("'", "")


def _rule_text(error: dict[str, Any]) -> str:
    """One fixed text per pydantic error type; never the input."""
    kind = error["type"]
    ctx = error.get("ctx") or {}
    if kind == "dashboard_rule":
        return error["msg"]
    if kind == "extra_forbidden":
        return "unknown key"
    if kind == "missing":
        return "is required"
    if kind == "union_tag_invalid":
        return "type must be one of " + ", ".join(WIDGET_TYPES)
    if kind == "union_tag_not_found":
        return "type is required"
    if kind == "string_too_long":
        return f"at most {ctx.get('max_length')} characters"
    if kind == "too_long":
        return f"at most {ctx.get('max_length')} items"
    if kind == "too_short":
        return f"at least {ctx.get('min_length')} item" + (
            "s" if ctx.get("min_length") != 1 else "")
    if kind == "literal_error":
        return f"must be one of {_expected(ctx)}"
    if kind == "finite_number":
        return "must be a finite number"
    if kind in ("greater_than_equal", "less_than_equal"):
        return "must be 1 or 2"
    if kind in ("string_type", "str_type"):
        return "must be a text"
    if kind in ("float_type", "int_type", "int_from_float", "float_parsing", "int_parsing"):
        return "must be a number"
    if kind == "bool_type":
        return "must be true or false"
    if kind == "list_type":
        return "must be a list"
    if kind in ("model_type", "dict_type", "model_attributes_type"):
        return "must be an object"
    return "invalid value"


def refusal(exc: ValidationError) -> DashboardError:
    """The first error of ``exc`` as a fixed text with its place.

    A value is a widget of several types only through the discriminator, so
    the first error is the one that matters; an unknown key and a missing
    one read the same in any order.
    """
    errors = exc.errors(include_input=False, include_url=False)
    if not errors:
        return DashboardError("dashboard: invalid value")
    first = errors[0]
    field = (first.get("ctx") or {}).get("field")
    loc = tuple(first["loc"])
    if first["type"] == "extra_forbidden" and not (
            isinstance(loc[-1], str) and _KEY_NAME.fullmatch(loc[-1])):
        loc = loc[:-1]  # a key that is no name is not repeated
    return DashboardError(f"{_where(loc, field)}: {_rule_text(first)}")


# ---------------------------------------------------------------------------
# Build and parse
# ---------------------------------------------------------------------------
def spec_chars(raw: Any) -> int:
    """The length of ``raw`` as compact JSON (what the 200,000 cap counts);
    NaN is counted as JSON writes it and refused by the spec model."""
    try:
        return len(json.dumps(raw, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return 0  # not JSON at all: the spec model refuses it


ERR_SPEC_TOO_LARGE = f"dashboard: at most {MAX_SPEC_CHARS} characters of JSON"
ERR_BYTES_TOO_LARGE = f"dashboard: at most {MAX_DASHBOARD_BYTES // 1024} KiB stored"


def validate_spec(raw: Any) -> DashboardSpec:
    """``raw`` (a dict as the tool or the stored file has it) as a checked
    spec; :class:`DashboardError` with a fixed text otherwise."""
    if spec_chars(raw) > MAX_SPEC_CHARS:
        raise DashboardError(ERR_SPEC_TOO_LARGE, "too_large")
    if not isinstance(raw, dict):
        raise DashboardError("dashboard: must be an object")
    try:
        return DashboardSpec.model_validate(raw)
    except ValidationError as exc:
        raise refusal(exc) from None


def as_json(spec: DashboardSpec) -> dict[str, Any]:
    """The spec as stored and served: defaults filled in, absent options
    left out."""
    return spec.model_dump(mode="json", exclude_none=True)


def build_dashboard(spec: DashboardSpec) -> bytes:
    """The validated spec as compact UTF-8 JSON (what ``chat_files`` holds)."""
    data = json.dumps(as_json(spec), ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")
    if len(data) > MAX_DASHBOARD_BYTES:
        raise DashboardError(ERR_BYTES_TOO_LARGE, "too_large")
    return data


def _no_constant(name: str) -> Any:
    raise ValueError("not JSON")


def parse_dashboard(data: bytes) -> dict[str, Any]:
    """Stored bytes back to the spec, checked again; ``ValueError`` (a
    :class:`DashboardError` or a JSON/UTF-8 error) for anything else."""
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_DASHBOARD_BYTES:
        raise DashboardError(ERR_BYTES_TOO_LARGE, "too_large")
    raw = json.loads(bytes(data).decode("utf-8"), parse_constant=_no_constant)
    return as_json(validate_spec(raw))
