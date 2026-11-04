"""The record of an InA analytical query, and the save-time gate.

An InA query is what an admin registered on the "Analytical queries" page:
which destination and identity reach SAP, the query's InA object name, and
the description a ``Describe`` read from SAP (dimensions, measures,
variables). The models here are the single definition of that shape -- the
admin API validates against them, the database stores their dump, and the
agent tools read them back.

Everything is ``extra="forbid"``: an unknown key is a typo or a newer
client, and silently dropping it would store something else than the admin
saw.

Refusals name the field (``definition.dimensions.3.name``) and the rule,
never a value: unlike the OData catalogue's EDM names, an InA name may hold
almost any character, so even a duplicate is named by its position.
``validate_ina_query`` builds its message from ``loc`` and ``msg`` only.

The one-line rule and the refusal text are those of every gate
(``agents.loc_fields``) and the destination name rule is the OData
catalogue's (``agents.odata.models``), so a text is refused alike on both
registration pages. What SAP described -- a member's name and label, a
unit, the query's name -- is held to the stricter form of the one-line rule
(``one_line_strict``: no invisible format character either); the record's
own texts (title, purpose, "not for", package) to the plain one, as on the
OData page. Never imports ``agents.db``.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    StringConstraints,
    ValidationError,
    field_validator,
)

# Imported rather than restated: one rule for every gate.
from agents.loc_fields import one_line, one_line_strict, refusal_text
from agents.odata.models import DESTINATION_NAME_RE

# The slug agents and server entries use; no hyphen at either end, as
# `SERVICE_NAME_RE` of the OData catalogue.
QUERY_NAME_RE = r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$"

MAX_DIMENSIONS = 100
MAX_MEASURES = 100
MAX_VARIABLES = 50
# How many queries one agent's ``builtin:ina`` entry may name.
MAX_ENTRY_QUERIES = 50


# A technical name SAP uses (the query's object name, a dimension, measure,
# variable or key field). Deliberately lax about which characters: InA names
# are not EDM identifiers (`0CALMONTH`, `[ZREGION].[KEY]`, `!V0001`); only
# what would break a line, or hide or reorder a part of it, is refused.
InaName = Annotated[
    StrictStr, StringConstraints(min_length=1, max_length=128), AfterValidator(one_line_strict)
]
Label = Annotated[StrictStr, StringConstraints(max_length=120), AfterValidator(one_line_strict)]
Unit = Annotated[StrictStr, StringConstraints(max_length=32), AfterValidator(one_line_strict)]


def _first_repeat(names: list[str]) -> int | None:
    """The position of the first name that an earlier entry already has."""
    seen: set[str] = set()
    for index, name in enumerate(names):
        if name in seen:
            return index
        seen.add(name)
    return None


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InaDimension(_Model):
    name: InaName
    label: Label = ""
    key_field: InaName | None = None


class InaMeasure(_Model):
    name: InaName
    label: Label = ""
    unit: Unit | None = None


class InaVariable(_Model):
    name: InaName
    label: Label = ""
    mandatory: StrictBool = False
    type: Literal["single", "interval", "multiple"] = "single"


class InaDefinition(_Model):
    """What ``Describe`` read from SAP. Names are compared exactly: InA names
    are case sensitive, and the three lists are separate in a request."""

    dimensions: list[InaDimension] = Field(default_factory=list, max_length=MAX_DIMENSIONS)
    measures: list[InaMeasure] = Field(default_factory=list, max_length=MAX_MEASURES)
    variables: list[InaVariable] = Field(default_factory=list, max_length=MAX_VARIABLES)

    @field_validator("dimensions", "measures", "variables")
    @classmethod
    def _distinct_names(cls, v: list[Any]) -> list[Any]:
        repeat = _first_repeat([member.name for member in v])
        if repeat is not None:
            raise ValueError(
                f"entry {repeat} repeats the name of an earlier entry; names must be distinct"
            )
        return v


class InaQueryPayload(_Model):
    """One registered query, as the admin API takes it and export writes it."""

    name: Annotated[StrictStr, StringConstraints(pattern=QUERY_NAME_RE)]
    # Stripped before the length check, so a blank title or purpose is
    # refused: the model picks a query by these two lines.
    title: Annotated[
        StrictStr, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
    ]
    purpose: Annotated[
        StrictStr, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
    ]
    not_for: Annotated[StrictStr, StringConstraints(max_length=200)] = ""
    destination: Annotated[StrictStr, StringConstraints(pattern=DESTINATION_NAME_RE)]
    user_context: StrictBool = False
    query: InaName  # the InA `DataSource.ObjectName`
    package: Annotated[StrictStr, StringConstraints(max_length=64)] = ""  # InA `PackageName`
    # None = not described yet: a query is registered first, then `Describe`
    # reads its members from SAP. Storage decides what an absent key means on
    # an update: `agents.db.update_ina_query` keeps the stored description.
    definition: InaDefinition | None = None
    metadata_fetched_at: datetime | None = None
    enabled: StrictBool = True

    # Runs after the strip above, so surrounding whitespace is still dropped.
    _one_line = field_validator("title", "purpose", "not_for", "package")(one_line)


def validate_ina_query(data: Any) -> InaQueryPayload:
    """The validated query, or one ``ValueError``.

    The message lists ``loc: msg`` per problem and never the input: a
    refusal is echoed in a 422 and logged, and a field is where a URL with
    a token in it gets pasted by mistake.
    """
    if not isinstance(data, dict):
        raise ValueError("ina_query: expected an object")
    try:
        return InaQueryPayload.model_validate(data)
    except ValidationError as exc:
        # `from None`: the ValidationError carries the input in its repr.
        raise ValueError(refusal_text(exc, "ina_query")) from None


def check_query_names(
    names: Any, *, field: str = "oauth.queries", distinct: bool = False
) -> list[str]:
    """The query names of a ``builtin:ina`` entry, distinct and in order.

    The one slug-list rule of the entry, for the admin gate
    (``agents.admin._validate_ina_entry``), storage
    (``agents.db._clean_ina_entry``) and the existence check
    (``agents.db.check_ina_queries``): a non-empty list of names in the form
    of ``QUERY_NAME_RE``, at most ``MAX_ENTRY_QUERIES`` of them. Anything
    else is refused rather than skipped: an entry whose list was silently
    shortened would be an agent with less than its admin configured.

    A repeated name is dropped (first occurrence wins), or refused with
    ``distinct`` (the gate, which tells the admin). Each ``ValueError``
    names ``field`` and the rule; a name is repeated only when it has the
    slug form, never a value that does not.
    """
    if names is None or names == []:
        raise ValueError(
            f"{field}: the names of the registered queries this agent may run "
            "(at least one)"
        )
    if not isinstance(names, list):
        raise ValueError(f"{field}: must be a list of registered query names")
    kept: list[str] = []
    for name in names:
        if not isinstance(name, str) or not re.fullmatch(QUERY_NAME_RE, name):
            raise ValueError(
                f"{field}: invalid query name (lower-case letters, digits and '-', "
                "at most 64 characters)"
            )
        if name in kept:
            if distinct:
                raise ValueError(f"{field}: duplicate query '{name}'")
            continue
        kept.append(name)
    if len(kept) > MAX_ENTRY_QUERIES:
        raise ValueError(f"{field}: more than {MAX_ENTRY_QUERIES} queries")
    return kept
