"""What a ``Describe`` reads: SAP's description of a query as an ``InaDefinition``.

Split out of :mod:`agents.ina.protocol` (which re-exports every name) so
neither module passes the size cap. Pure functions, no I/O: the client hands
:func:`read_description` the ``Cube`` of a metadata answer, and the text
helpers here (:func:`shown_name`, :func:`fits_name` and the cleaners the
grid reader shares) are the one place SAP's names and labels are cleaned.

Verified against SIA (S/4HANA 2022, SAP_BW 757;
``tests/fixtures/ina/describe_sia.json``, recorded 2026-11-04): the ``Cube``
with its ``Dimensions`` (the measure structure is ``DimensionType`` 2 under
a generated name, its members keyed ``<structure>.KEY``), and the
``Variables``, which say "mandatory" as ``InputType``, take their values as
``MultipleValues`` / ``FilterCapability.ComparisonGroup`` and name the
dimension their values belong to (``DimensionReference``). Every variable
of both recorded queries is ``SingleValue``: the ``Interval`` / ``Range``
and ``MultipleValues`` readings are inferred, not seen on SIA.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from pydantic import TypeAdapter, ValidationError

from .errors import InaError
from .models import MAX_DIMENSIONS as MAX_DESCRIBED_DIMENSIONS
from .models import MAX_MEASURES as MAX_DESCRIBED_MEASURES
from .models import MAX_VARIABLES as MAX_DESCRIBED_VARIABLES
from .models import InaDefinition, InaDimension, InaMeasure, InaName, InaVariable

# How many entries of a description are looked at; the definition keeps at
# most the gate's 100 / 100 / 50 of them.
_MAX_SCANNED = 2000

# The structure of the measures (key figures) on the Columns axis, by the
# name of the synthetic examples; SIA's has a generated name and is told by
# its type.
MEASURES_DIMENSION = "Measures"
_MEASURE_STRUCTURE_TYPE = 2

# Control, format, surrogate, private-use and line/paragraph separators.
_DROPPED = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})

_NAME = TypeAdapter(InaName)

logger = logging.getLogger(__name__)

UNREADABLE = "the answer of SAP has a form this client does not read"


# -- text ------------------------------------------------------------------


def _label(value: object, limit: int) -> str:
    """One line of text: dropped characters become spaces, runs of white
    space one space, cut at ``limit``."""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(
        " " if unicodedata.category(ch) in _DROPPED else ch for ch in value[: limit * 4]
    )
    return " ".join(cleaned.split())[:limit]


def _key(value: str, limit: int) -> str:
    """A member key as SAP sent it, without the dropped characters. Not
    otherwise changed: the model filters by it again."""
    return "".join(ch for ch in value[: limit * 4] if unicodedata.category(ch) not in _DROPPED)[
        :limit
    ]


def shown_name(value: object) -> str:
    """A name SAP sent that the gate refused, as it may be listed."""
    text = _label(value, 64)
    return text or "<unnamed>"


def fits_name(value: object) -> bool:
    """Whether ``value`` passes the gate's name rule (``models.InaName``)."""
    try:
        _NAME.validate_python(value)
    except ValidationError:
        return False
    return True



# -- the description -------------------------------------------------------


@dataclass
class InaDescription:
    """What ``describe`` read: a definition the save-time gate takes, the
    names it had to leave out (cleaned and cut) and whether a list was
    longer than the gate allows."""

    definition: InaDefinition
    skipped: list[str] = field(default_factory=list)
    truncated: bool = False


def _variable_type(raw: dict[str, Any]) -> str:
    """The values a variable takes: ``SelectionType`` when the answer has
    one; else SIA's form, ``FilterCapability.ComparisonGroup`` (``Interval``
    or ``Range`` is an interval, ``SingleValue`` one value) and
    ``MultipleValues`` (exactly ``true``: several). ``VariableType`` says what
    kind of variable it is (``DimensionMemberVariable``,
    ``SimpleTypeVariable``), not what it takes, and decides nothing."""
    kind = raw.get("SelectionType")
    if isinstance(kind, str):
        low = kind.lower()
        if "interval" in low:
            return "interval"
        if "multi" in low:
            return "multiple"
        return "single"
    capability = raw.get("FilterCapability")
    group = capability.get("ComparisonGroup") if isinstance(capability, dict) else None
    if group in ("Interval", "Range"):
        return "interval"
    if raw.get("MultipleValues") is True:
        return "multiple"
    return "single"


def _is_mandatory(value: object) -> bool | None:
    """What a ``Mandatory`` value says: True for a JSON ``true``, ``1``,
    ABAP's ``"X"`` or ``"true"``; False for ``false``, ``0``, ``"false"`` or
    ABAP's blank ``""``; None for anything else (``2``, ``"yes"``, ``null``,
    a float or a list), which the caller reads as "could not be read", never
    as "optional"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return {1: True, 0: False}.get(value)
    if isinstance(value, str):
        return {"x": True, "true": True, "": False, "false": False}.get(value.strip().lower())
    return None


# SIA's spelling (and SAP's InA client's): ``InputType``.
_INPUT_TYPES = {"mandatory": True, "mandatorynotinitial": True, "optional": False}


def _variable_is_mandatory(raw: dict[str, Any], position: int) -> bool:
    """Whether SAP demands a value for the variable ``raw``.

    Two markers are known: ``Mandatory`` (the spellings of
    :func:`_is_mandatory`) and ``InputType`` (``Mandatory``,
    ``MandatoryNotInitial`` or ``Optional``, as SIA sends it). A variable is
    mandatory when either says so, and optional only when every marker it
    carries was read and says optional. A variable with no marker, or a
    marker of another value or type (``Mandatory: null``, ``"yes"``, ``2``;
    an ``InputType`` of another word), is mandatory, with one WARNING that
    names its position in SAP's list, not its name: read as optional, a run
    without it would be sent and refused by SAP, and the Test could not tell
    the admin beforehand.
    """
    verdicts: list[bool] = []
    unreadable = False
    if "Mandatory" in raw:
        said = _is_mandatory(raw["Mandatory"])
        if said is None:
            unreadable = True
        else:
            verdicts.append(said)
    if "InputType" in raw:
        value = raw["InputType"]
        verdict = _INPUT_TYPES.get(value.strip().lower()) if isinstance(value, str) else None
        if verdict is None:
            unreadable = True
        else:
            verdicts.append(verdict)
    if any(verdicts):
        return True
    if verdicts and not unreadable:
        return False
    logger.warning(
        "ina: variable %d of the description carries no mandatory marker this client "
        "reads; read as mandatory",
        position,
    )
    return True


def _variable_key_field(raw: dict[str, Any]) -> str | None:
    """``<DimensionReference.Name>.KEY``: the field SIA's describe names a
    variable's value by, and the only one its VariableSubmit took (recorded
    run 5). None when the reference is missing or the name would not pass
    the gate."""
    reference = raw.get("DimensionReference")
    name = reference.get("Name") if isinstance(reference, dict) else None
    field_name = f"{name}.KEY" if isinstance(name, str) and name else None
    return field_name if fits_name(field_name) else None


def _is_measure_structure(raw: dict[str, Any]) -> bool:
    kind = raw.get("DimensionType")
    return (
        (isinstance(kind, int) and not isinstance(kind, bool) and kind == _MEASURE_STRUCTURE_TYPE)
        or raw.get("Name") == MEASURES_DIMENSION
    ) and isinstance(raw.get("Members"), list)


def read_description(cube: object) -> InaDescription:
    """SAP's description of a query (the ``Cube`` of a metadata answer) as
    an ``InaDefinition``.

    Never refuses the whole description for one odd entry: labels are cut
    to 120 characters and units to 32 with dropped characters removed; a
    variable is mandatory unless its markers say optional
    (:func:`_variable_is_mandatory`); an
    entry whose name does not pass the gate's rule, or repeats an earlier
    name, is left out and listed in ``skipped``; beyond 100 dimensions, 100
    measures or 50 variables ``truncated`` is set. A description without a
    measure structure is refused (``sap_error``): it is not one this reader
    knows, and a query read as "no measures" could never be run.
    """
    if not isinstance(cube, dict) or not isinstance(cube.get("Dimensions"), list):
        raise InaError("sap_error", UNREADABLE)
    out = InaDescription(definition=InaDefinition())
    dimensions: list[InaDimension] = []
    measures: list[InaMeasure] = []
    variables: list[InaVariable] = []

    def keep(kind: type, bucket: list, limit: int, name: object, **values: Any) -> None:
        if not fits_name(name) or any(entry.name == name for entry in bucket):
            out.skipped.append(shown_name(name))
            return
        if len(bucket) >= limit:
            out.truncated = True
            return
        bucket.append(kind(name=name, **values))

    structures = 0
    measure_structure: str | None = None
    for raw in cube["Dimensions"][:_MAX_SCANNED]:
        if not isinstance(raw, dict):
            continue
        if _is_measure_structure(raw):
            structures += 1
            structure = raw.get("Name") if isinstance(raw.get("Name"), str) else ""
            if structures == 1 and fits_name(structure):
                # The run puts this one on Columns; a second structure's
                # members are listed but would be sent under the first's name.
                measure_structure = structure
            for member in raw["Members"][:_MAX_SCANNED]:
                if not isinstance(member, dict):
                    continue
                name = member.get(f"{structure}.KEY", member.get("Name"))
                label = next(
                    (
                        member[k]
                        for k in (
                            f"{structure}.LONG_TEXT",
                            f"{structure}.MEDIUM_TEXT",
                            f"{structure}.SHORT_TEXT",
                            "Description",
                            "Text",
                        )
                        if isinstance(member.get(k), str)
                    ),
                    "",
                )
                unit = _label(member.get("Unit") or member.get("Currency"), 32) or None
                keep(
                    InaMeasure,
                    measures,
                    MAX_DESCRIBED_MEASURES,
                    name,
                    label=_label(label, 120),
                    unit=unit,
                )
            continue
        key_field = raw.get("KeyAttribute")
        keep(
            InaDimension,
            dimensions,
            MAX_DESCRIBED_DIMENSIONS,
            raw.get("Name"),
            label=_label(raw.get("Description"), 120),
            key_field=key_field if fits_name(key_field) else None,
        )
    if not structures:
        raise InaError("sap_error", UNREADABLE)
    raw_variables = cube.get("Variables")
    listed = raw_variables[:_MAX_SCANNED] if isinstance(raw_variables, list) else []
    for position, raw in enumerate(listed, start=1):
        if not isinstance(raw, dict):
            continue
        keep(
            InaVariable,
            variables,
            MAX_DESCRIBED_VARIABLES,
            raw.get("Name"),
            label=_label(raw.get("Description") or raw.get("Text"), 120),
            mandatory=_variable_is_mandatory(raw, position),
            type=_variable_type(raw),
            key_field=_variable_key_field(raw),
        )
    out.definition = InaDefinition(
        measure_structure=measure_structure,
        dimensions=dimensions,
        measures=measures,
        variables=variables,
    )
    return out


__all__ = [
    "MEASURES_DIMENSION",
    "UNREADABLE",
    "InaDescription",
    "fits_name",
    "read_description",
    "shown_name",
]
