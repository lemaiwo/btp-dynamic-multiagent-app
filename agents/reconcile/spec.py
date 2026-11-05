"""The reconciliation spec: what a model compiles from mapping rules, once.

A model reads the mapping workbook and writes one :class:`ReconcileSpec`; from
then on code alone builds the ABAP SQL for the source table and compares every
CSV row with it. Because the spec is model output it is treated as untrusted
data: every identifier that can reach a query is upper-cased and checked
against :data:`IDENT_RE` (no quotes, spaces, ``;`` or namespaces in v1), every
list and text has a cap so a spec cannot blow up a run, and every model forbids
unknown keys so a misspelt option fails loudly instead of being ignored.

:class:`SpecError` carries fixed texts only: a spec holds column names and
values from the customer's files, and an error that echoes them would carry
them into logs.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

IDENT_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,29}\Z")  # SAP table / field, no namespaces in v1
LETTER_RE = re.compile(r"^[A-Z]{1,3}\Z")
MAX_TEXT, MAX_VALUE, MAX_LIST = 2000, 255, 200
MAX_COLUMNS, MAX_LOOKUPS, MAX_CASES, MAX_SPEC_BYTES = 300, 5, 20, 1024 * 1024
MAX_COLUMN_NAME = 64


class SpecError(ValueError):
    """A spec that cannot be used. The message is one of a few fixed texts."""


def _ident_required(value: str) -> str:
    """Upper-case an SAP identifier and refuse anything :data:`IDENT_RE` does not match.

    Upper-casing first lets a model write ``mara``; the regex then guarantees the
    value can be pasted into ABAP SQL as a bare name. ``fullmatch``, not ``match``:
    ``$`` also matches before a trailing newline, which would let ``"MARA\\n"`` through.
    """
    upper = value.upper()
    if not IDENT_RE.fullmatch(upper):
        raise ValueError("not an SAP identifier (A-Z, 0-9, _; at most 30 characters)")
    return upper


def _ident(value: str | None) -> str | None:
    """:func:`_ident_required` for an optional field."""
    return None if value is None else _ident_required(value)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ColumnRef(_Strict):
    """A column of a workbook sheet, by letter (``B``) or by header label; exactly one."""

    letter: str | None = None
    label: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("letter")
    @classmethod
    def _letter(cls, value: str | None) -> str | None:
        if value is None:
            return None
        upper = value.upper()
        if not LETTER_RE.fullmatch(upper):
            raise ValueError("a column letter is one to three letters A-Z")
        return upper

    @model_validator(mode="after")
    def _exactly_one(self) -> ColumnRef:
        if (self.letter is None) == (self.label is None):
            raise ValueError("give exactly one of letter and label")
        return self


class XrefLookup(_Strict):
    """A code table in the mapping workbook: source value(s) in ``keys``, target in ``value``."""

    sheet: str = Field(min_length=1, max_length=31)  # Excel's sheet-name limit
    header_row: int = Field(ge=1, le=1000)
    keys: list[ColumnRef] = Field(min_length=1, max_length=4)
    value: ColumnRef


class Value(_Strict):
    """A constant or a source field's value; exactly one."""

    const: str | None = Field(default=None, max_length=MAX_VALUE)
    field: str | None = None

    _field = field_validator("field")(_ident)

    @model_validator(mode="after")
    def _exactly_one(self) -> Value:
        if (self.const is None) == (self.field is None):
            raise ValueError("give exactly one of const and field")
        return self


class Condition(_Strict):
    """One test on a source field; ``values`` count fixed by ``op``."""

    field: str
    op: Literal["equals", "in", "blank", "not_blank"]
    values: list[Annotated[str, Field(max_length=MAX_VALUE)]] = Field(
        default_factory=list, max_length=MAX_LIST
    )

    _field = field_validator("field")(_ident)

    @model_validator(mode="after")
    def _value_count(self) -> Condition:
        count = len(self.values)
        if self.op == "equals" and count != 1:
            raise ValueError("equals takes exactly one value")
        if self.op == "in" and count == 0:
            raise ValueError("in takes at least one value")
        if self.op in ("blank", "not_blank") and count:
            raise ValueError(f"{self.op} takes no values")
        return self


class Case(_Strict):
    """``then`` applies when every condition in ``when`` holds."""

    when: list[Condition] = Field(min_length=1, max_length=5)
    then: Value


class _Rule(_Strict):
    alpha: bool = False  # leading zeros of all-digit values do not count
    numeric: bool = False  # compare as decimal numbers
    note: str = Field(default="", max_length=MAX_TEXT)  # the mapping text, for the report


class DirectRule(_Rule):
    """The CSV value is the source field's value."""

    kind: Literal["direct"]
    source: str

    _source = field_validator("source")(_ident)


class ConstantRule(_Rule):
    """The CSV value is always ``value``."""

    kind: Literal["constant"]
    value: str = Field(max_length=MAX_VALUE)


class ValueMapRule(_Rule):
    """The source value(s) translated through code tables; the first lookup that matches wins."""

    kind: Literal["value_map"]
    sources: list[str] = Field(min_length=1, max_length=4)
    lookups: list[XrefLookup] = Field(min_length=1, max_length=MAX_LOOKUPS)
    on_miss: Literal["report", "default", "source"] = "report"
    default: str | None = Field(default=None, max_length=MAX_VALUE)

    @field_validator("sources")
    @classmethod
    def _sources(cls, value: list[str]) -> list[str]:
        return [_ident_required(item) for item in value]

    @model_validator(mode="after")
    def _consistent(self) -> ValueMapRule:
        if self.on_miss == "default" and self.default is None:
            raise ValueError("on_miss 'default' needs a default")
        for lookup in self.lookups:
            if len(lookup.keys) != len(self.sources):
                raise ValueError("every lookup needs one key column per source field")
        return self


class ExpressionRule(_Rule):
    """The first case whose conditions all hold gives the value; else ``otherwise``."""

    kind: Literal["expression"]
    cases: list[Case] = Field(min_length=1, max_length=MAX_CASES)
    otherwise: Value


class NotNeededRule(_Rule):
    """The column is not migrated: the CSV value must be blank."""

    kind: Literal["not_needed"]


class UnresolvedRule(_Rule):
    """A rule the model could not compile; reported, never compared."""

    kind: Literal["unresolved"]
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    needs_table: str | None = None

    _needs_table = field_validator("needs_table")(_ident)


Rule = Annotated[
    DirectRule | ConstantRule | ValueMapRule | ExpressionRule | NotNeededRule | UnresolvedRule,
    Field(discriminator="kind"),
]


class KeySpec(_Strict):
    """How a CSV row finds its source row."""

    csv_column: str = Field(min_length=1, max_length=MAX_COLUMN_NAME)
    ecc_field: str
    length: int = Field(ge=1, le=40)
    pad_numeric: bool = True  # all-digit CSV keys are zero-padded to `length`

    _ecc_field = field_validator("ecc_field")(_ident)


class ReconcileSpec(_Strict):
    """One source table, one key, one rule per CSV column."""

    version: Literal[1] = 1
    table: str
    wave: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9 _-]+$")
    key: KeySpec
    trim: bool = True  # also makes ' ' equal to blank
    columns: dict[str, Rule] = Field(min_length=1, max_length=MAX_COLUMNS)

    _table = field_validator("table")(_ident)

    @field_validator("columns")
    @classmethod
    def _column_names(cls, value: dict[str, Rule]) -> dict[str, Rule]:
        for name in value:
            if not 1 <= len(name) <= MAX_COLUMN_NAME:
                raise ValueError(f"a column name is 1 to {MAX_COLUMN_NAME} characters")
            if any(unicodedata.category(ch) == "Cc" for ch in name):
                raise ValueError("a column name has no control characters")
        return value

    def ecc_fields(self) -> list[str]:
        """The source fields to select: the key first, then every field a rule reads, sorted."""
        fields: set[str] = set()
        for rule in self.columns.values():
            if isinstance(rule, DirectRule):
                fields.add(rule.source)
            elif isinstance(rule, ValueMapRule):
                fields.update(rule.sources)
            elif isinstance(rule, ExpressionRule):
                for case in rule.cases:
                    fields.update(cond.field for cond in case.when)
                    if case.then.field is not None:
                        fields.add(case.then.field)
                if rule.otherwise.field is not None:
                    fields.add(rule.otherwise.field)
        fields.discard(self.key.ecc_field)
        return [self.key.ecc_field, *sorted(fields)]

    def to_json(self) -> bytes:
        return self.model_dump_json(indent=2).encode()

    @classmethod
    def from_json(cls, data: bytes) -> ReconcileSpec:
        """Parse a stored or submitted spec; :class:`SpecError` with a fixed text on any failure.

        The size is checked before parsing so a huge document costs nothing, and
        the pydantic error is dropped (``from None``) because its text quotes input.
        """
        if len(data) > MAX_SPEC_BYTES:
            raise SpecError("The spec is larger than 1 MiB.")
        try:
            raw = json.loads(data)
        except (ValueError, RecursionError):
            raise SpecError("The spec is not valid JSON.") from None
        try:
            return cls.model_validate(raw)
        except ValidationError:
            raise SpecError("The spec does not match the reconciliation spec schema.") from None


# Error types whose pydantic message quotes part of the input (the tag a model
# wrote); they get a fixed text instead.
_ECHOING_TYPES = {"union_tag_invalid": "Unknown rule kind."}
_MAX_HINT_ERRORS = 10
_MAX_HINT_PART = 160


def _hint_text(text: str) -> str:
    """One line, no control characters, bounded: the location may hold a model-written key."""
    flat = "".join("?" if unicodedata.category(ch)[0] == "C" else ch for ch in text)
    return flat if len(flat) <= _MAX_HINT_PART else flat[: _MAX_HINT_PART - 3] + "..."


def validation_hint(exc: ValidationError) -> str:
    """What is wrong with a spec, by location and message, without any input value.

    For a caller that runs ``ReconcileSpec.model_validate`` itself and wants to
    tell the model what to fix. ``str(exc)`` is no good for that: it prints every
    offending input, and a spec holds values from the customer's files.
    """
    lines: list[str] = []
    errors = exc.errors(include_url=False, include_context=False)
    for err in errors[:_MAX_HINT_ERRORS]:
        loc = ".".join(str(part) for part in err["loc"]) or "spec"
        msg = _ECHOING_TYPES.get(err["type"], err["msg"])
        value = err.get("input")
        if isinstance(value, str) and value and value in msg:
            msg = err["type"]
        lines.append(f"{_hint_text(loc)}: {_hint_text(msg)}")
    if len(errors) > _MAX_HINT_ERRORS:
        lines.append(f"(and {len(errors) - _MAX_HINT_ERRORS} more)")
    return "; ".join(lines)
