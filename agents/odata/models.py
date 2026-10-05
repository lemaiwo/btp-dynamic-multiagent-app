"""The catalogue definition of an OData service, and the save-time gate.

A catalogue service is what an admin curated from a service's ``$metadata``:
which entity sets, fields and operations exist *for agents*. The models here
are the single definition of that shape -- the admin API validates against
them, the database stores their dump, and the agent tools read them back --
so a definition that could not be executed is refused when it is saved, not
discovered by a model at run time.

Everything is ``extra="forbid"``: an unknown key is a typo or a newer
client, and silently dropping it would store something else than the admin
saw.

Refusals name the offending *name* (an entity set, a field, an operation;
all of them already matched ``EDM_NAME_RE``), never a free-text value:
``validate_odata_service`` builds its message from ``loc`` and ``msg`` only.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .urls import confine_service_path

SERVICE_NAME_RE = r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$"  # the slug agents and server entries use
EDM_NAME_RE = r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$"
DESTINATION_NAME_RE = (
    r"^[A-Za-z0-9_.-]{1,200}$"  # same rule as agents/admin.py _DESTINATION_NAME_RE
)

ENTITY_OPS = ("list", "get", "create", "update", "delete")
WRITE_OPS = ("create", "update", "delete")

MAX_ENTITY_SETS = 200
MAX_FIELDS = 500
MAX_OPERATIONS = 200
MAX_DEFINITION_BYTES = 2_000_000

# One URL segment below the service root. Deliberately narrower than what a
# URL allows: no '/', '?', '#', '%', whitespace or parentheses.
# Used with `fullmatch`: Python's `$` would also accept a trailing "\n".
_ENTITY_PATH_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}")
# How many problems one refusal lists, and how long one `loc` part may be
# (a `loc` part can be an unknown key, i.e. text the caller typed).
_MAX_REPORTED_ERRORS = 20
_MAX_LOC_PART = 64

EdmName = Annotated[str, StringConstraints(pattern=EDM_NAME_RE)]
EdmType = Annotated[str, StringConstraints(min_length=1, max_length=200)]
EntityOp = Literal["list", "get", "create", "update", "delete"]


def _first_duplicate(names: list[str]) -> str | None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            return name
        seen.add(name)
    return None


def _one_line(value: str, info: ValidationInfo) -> str:
    """Refuse a control character or a line separator in a one-line text.

    These texts are printed as one line each -- the service's ``purpose`` in
    an agent's instructions, titles, labels and value meanings in the search
    tool's answer -- so a line break in one of them would start a line of its
    own there, which reads as something the catalogue did not say. The
    message names the field, never the text.
    """
    if any(unicodedata.category(ch) in ("Cc", "Zl", "Zp") for ch in value):
        raise ValueError(f"{info.field_name} must be one line of text without control characters")
    return value


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KeyDef(_Model):
    name: EdmName
    type: EdmType = "Edm.String"


class ValueMeaning(_Model):
    """One coded value and what it means (``B`` = awaiting release)."""

    value: str = Field(min_length=1, max_length=64)
    meaning: str = Field(min_length=1, max_length=200)

    _one_line = field_validator("value", "meaning")(_one_line)


class FieldDef(_Model):
    """A property of an entity set.

    The three flags default to ``False``: a field does nothing for an agent
    until the admin switches it on (deny by default).
    """

    name: EdmName
    type: EdmType = "Edm.String"
    label: str = Field(default="", max_length=120)
    selectable: StrictBool = False
    filterable: StrictBool = False
    writable: StrictBool = False
    hint: str = Field(default="", max_length=300)
    values: list[ValueMeaning] = Field(default_factory=list)
    personal_data: StrictBool = False

    _one_line = field_validator("label")(_one_line)

    @model_validator(mode="after")
    def _filterable_is_selectable(self) -> FieldDef:
        # Agents see only selectable fields. A filter on a field they may not
        # read would still answer "is the value X?" row by row, so a hidden
        # field must not be filterable either. (No such rule for `writable`:
        # a write-only field is legitimate.)
        if self.filterable and not self.selectable:
            raise ValueError(
                f"field {self.name!r} is filterable but not selectable; "
                "a filterable field must also be selectable"
            )
        return self


class NavigationDef(_Model):
    name: EdmName
    target: EdmName  # entity set name
    collection: StrictBool
    description: str = Field(default="", max_length=300)


class ExampleQuery(_Model):
    description: str = Field(min_length=1, max_length=200)
    filter: str = Field(default="", max_length=1000)
    select: list[Annotated[str, StringConstraints(max_length=128)]] = Field(default_factory=list)
    orderby: str = Field(default="", max_length=300)
    top: int | None = Field(default=None, ge=1)


class EntitySetDef(_Model):
    name: EdmName
    title: str = Field(default="", max_length=120)
    path: str = ""  # "" = name; one confined segment
    entity_type: str = ""
    description: str = Field(default="", max_length=600)
    keys: list[KeyDef]
    operations: list[EntityOp] = Field(default_factory=list)
    fields: list[FieldDef] = Field(max_length=MAX_FIELDS)
    navigations: list[NavigationDef] = Field(default_factory=list)
    examples: list[ExampleQuery] = Field(default_factory=list)

    _one_line = field_validator("title")(_one_line)

    @field_validator("path")
    @classmethod
    def _confined_segment(cls, v: str) -> str:
        # The segment is appended to the service path of every request for
        # this entity set, so it must not be able to leave it.
        if v and (not _ENTITY_PATH_RE.fullmatch(v) or ".." in v or v == "."):
            raise ValueError(
                "path must be one URL segment of letters, digits, '_', '.' and '-' "
                "(empty = the entity set name)"
            )
        return v

    @field_validator("operations")
    @classmethod
    def _distinct_operations(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def _consistent(self) -> EntitySetDef:
        who = f"entity set {self.name!r}"
        duplicate = _first_duplicate([f.name for f in self.fields])
        if duplicate:
            raise ValueError(f"duplicate field {duplicate!r} in {who}")
        duplicate = _first_duplicate([n.name for n in self.navigations])
        if duplicate:
            raise ValueError(f"duplicate navigation {duplicate!r} in {who}")
        duplicate = _first_duplicate([k.name for k in self.keys])
        if duplicate:
            raise ValueError(f"duplicate key {duplicate!r} in {who}")
        field_names = {f.name for f in self.fields}
        for key in self.keys:
            if key.name not in field_names:
                raise ValueError(f"key {key.name!r} of {who} is not one of its fields")
        for op in self.operations:
            if op in ("get", "update", "delete") and not self.keys:
                raise ValueError(f"{who} has {op!r} but no key")
            # A list call always sends $select, and rows are filtered to the
            # selectable fields on the way back: without one, nothing returns.
            if op in ("list", "get") and not any(f.selectable for f in self.fields):
                raise ValueError(f"{who} has {op!r} but no selectable field")
            if op in ("create", "update") and not any(f.writable for f in self.fields):
                raise ValueError(f"{who} has {op!r} but no writable field")
        return self

    def field(self, name: str) -> FieldDef | None:
        return next((f for f in self.fields if f.name == name), None)

    def selectable_names(self) -> list[str]:
        return [f.name for f in self.fields if f.selectable]

    def has_write(self) -> bool:
        return any(op in WRITE_OPS for op in self.operations)


class ParamDef(_Model):
    name: EdmName
    type: EdmType = "Edm.String"
    required: StrictBool = True


class OperationDef(_Model):
    """A function import (V2), or an action or function (V4).

    ``enabled`` defaults to off and ``changes_data`` to on: an imported
    operation is invisible until an admin enables it, and is treated as a
    write until an admin says it only reads.
    """

    name: EdmName
    qualified_name: str = ""  # V4: namespace-qualified
    title: str = Field(default="", max_length=120)
    kind: Literal["function_import", "action", "function"]
    http_method: Literal["GET", "POST"]
    bound_to: EdmName | None = None  # entity set name
    parameters: list[ParamDef] = Field(default_factory=list)
    description: str = Field(default="", max_length=600)
    enabled: StrictBool = False
    changes_data: StrictBool = True

    _one_line = field_validator("title")(_one_line)

    @field_validator("qualified_name")
    @classmethod
    def _edm_or_empty(cls, v: str) -> str:
        # fullmatch, not match: `$` alone would let "ns.Op\n" through.
        if v and not re.fullmatch(EDM_NAME_RE, v):
            raise ValueError(
                "qualified_name must be a namespace-qualified name (letters, digits, '_' and '.')"
            )
        return v

    @model_validator(mode="after")
    def _distinct_parameters(self) -> OperationDef:
        duplicate = _first_duplicate([p.name for p in self.parameters])
        if duplicate:
            raise ValueError(f"duplicate parameter {duplicate!r} in operation {self.name!r}")
        return self


class ServiceDefinition(_Model):
    entity_sets: list[EntitySetDef] = Field(default_factory=list, max_length=MAX_ENTITY_SETS)
    operations: list[OperationDef] = Field(default_factory=list, max_length=MAX_OPERATIONS)

    @model_validator(mode="after")
    def _consistent(self) -> ServiceDefinition:
        duplicate = _first_duplicate([e.name for e in self.entity_sets])
        if duplicate:
            raise ValueError(f"duplicate entity set {duplicate!r}")
        duplicate = _first_duplicate([o.name for o in self.operations])
        if duplicate:
            raise ValueError(f"duplicate operation {duplicate!r}")
        names = {e.name for e in self.entity_sets}
        for op in self.operations:
            if op.bound_to is not None and op.bound_to not in names:
                raise ValueError(
                    f"bound_to {op.bound_to!r} of operation {op.name!r} "
                    "is not an entity set of this service"
                )
        # Read through the module so the cap is one constant (and patchable).
        limit = MAX_DEFINITION_BYTES
        if len(self.model_dump_json().encode("utf-8")) > limit:
            raise ValueError(f"the definition is larger than {limit} bytes")
        return self

    def entity_set(self, name: str) -> EntitySetDef | None:
        return next((e for e in self.entity_sets if e.name == name), None)

    def operation(self, name: str) -> OperationDef | None:
        return next((o for o in self.operations if o.name == name), None)

    def has_write(self) -> bool:
        """Whether anything in here can change data in SAP."""
        return any(e.has_write() for e in self.entity_sets) or any(
            o.enabled and o.changes_data for o in self.operations
        )


class ODataServicePayload(_Model):
    """One catalogue service, as the admin API takes it and export writes it."""

    name: Annotated[str, StringConstraints(pattern=SERVICE_NAME_RE)]
    # Stripped before the length check, so a blank title or purpose is
    # refused: the model picks a service by these two lines.
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    purpose: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    not_for: str = Field(default="", max_length=200)
    destination: Annotated[str, StringConstraints(pattern=DESTINATION_NAME_RE)]
    user_context: StrictBool = False
    odata_version: Literal["v2", "v4"]
    service_path: str
    enabled: StrictBool = True
    # Required on purpose: with a default, an update that leaves the key out
    # would replace the stored definition by an empty one. A create sends {}.
    definition: ServiceDefinition
    metadata_fetched_at: datetime | None = None

    # Runs after the strip above, so surrounding whitespace is still dropped.
    _one_line = field_validator("title", "purpose", "not_for")(_one_line)

    @field_validator("service_path")
    @classmethod
    def _confined_service_path(cls, v: str) -> str:
        return confine_service_path(v)

    @model_validator(mode="after")
    def _kinds_match_version(self) -> ODataServicePayload:
        for op in self.definition.operations:
            who = f"operation {op.name!r}"
            if self.odata_version == "v2":
                if op.kind != "function_import":
                    raise ValueError(
                        f"{who} has kind {op.kind!r}; a v2 service allows only 'function_import'"
                    )
                continue
            if op.kind == "function_import":
                raise ValueError(
                    f"{who} has kind {op.kind!r}; a v4 service allows only 'action' and 'function'"
                )
            if op.kind == "action" and op.http_method != "POST":
                raise ValueError(f"{who} is a v4 action and must use POST")
            if op.kind == "function" and op.http_method != "GET":
                raise ValueError(f"{who} is a v4 function and must use GET")
            if not op.qualified_name:
                raise ValueError(
                    f"{who} needs a qualified_name (v4 operations are namespace-qualified)"
                )
        return self

    def has_write(self) -> bool:
        return self.definition.has_write()


# What a location part may look like to be repeated: every field of the
# models above does, and so does an honestly mistyped one.
_LOC_FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,%d}" % (_MAX_LOC_PART - 1))
_UNKNOWN_FIELD = "<unknown field>"


def _loc(parts: tuple[Any, ...]) -> str:
    """Where a problem is, as ``a.0.b``.

    A list index is repeated as it is. A key is the client's own text: the
    key of an unknown field arrives here like any other, and a value pasted
    where a key belongs (a URL, a token) would come back with its first
    characters. So a key is named only when it has the form of a field name
    (`fullmatch`: a trailing newline does not pass); anything else is
    ``<unknown field>``.
    """
    shown = [
        str(part)
        if isinstance(part, int) and not isinstance(part, bool)
        else part
        if isinstance(part, str) and _LOC_FIELD_RE.fullmatch(part)
        else _UNKNOWN_FIELD
        for part in parts
    ]
    return ".".join(shown) or "service"


def validate_odata_service(data: dict[str, Any]) -> dict[str, Any]:
    """The clean, JSON-ready service dict, or one ``ValueError``.

    The message lists ``loc: msg`` per problem and never the input: the
    catalogue is admin text, but a refusal is echoed in a 422 and logged,
    and a field is where a URL with a token in it gets pasted by mistake.
    """
    if not isinstance(data, dict):
        raise ValueError("service: expected an object")
    try:
        payload = ODataServicePayload.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        lines = [f"{_loc(err['loc'])}: {err['msg']}" for err in errors[:_MAX_REPORTED_ERRORS]]
        if len(errors) > _MAX_REPORTED_ERRORS:
            lines.append(f"and {len(errors) - _MAX_REPORTED_ERRORS} more")
        # `from None`: the ValidationError carries the input in its repr.
        raise ValueError("; ".join(lines)) from None
    return payload.model_dump(mode="json")
