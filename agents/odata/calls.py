"""Which operations of a stored catalogue service can be called at all.

``client.call_refusal`` is the one rule; this module applies it to what is
stored (plain dicts, read without validating the whole definition) for the
two places that are not a call: the search tool, which offers only what
``execute_operation`` can run, and the admin API, which lists the enabled
operations no agent will ever be offered so that they do not sit in the
catalogue unnoticed. No I/O.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .client import call_refusal
from .models import OperationDef
from .v2 import V2Dialect
from .v4 import V4Dialect

# One dialect object per protocol version this app can send. A version that
# is not here is refused, never sent with another version's rules.
DIALECTS: dict[str, Any] = {"v2": V2Dialect(), "v4": V4Dialect()}

# A stored operation that the models do not accept: nothing can run it.
INVALID_DEFINITION = "invalid_definition"


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def key_names(definition: Any) -> dict[str, list[str]]:
    """Entity set name -> the names of its key fields, from a stored definition."""
    out: dict[str, list[str]] = {}
    if not isinstance(definition, dict):
        return out
    for entity_set in _dicts(definition.get("entity_sets")):
        name = entity_set.get("name")
        if isinstance(name, str):
            keys = _dicts(entity_set.get("keys"))
            out.setdefault(name, [k["name"] for k in keys if isinstance(k.get("name"), str)])
    return out


def stored_call_refusal(
    operation: dict[str, Any],
    keys: dict[str, list[str]],
    version: Any,
    *,
    allow_write: bool = True,
) -> str | None:
    """``client.call_refusal`` for one stored operation: ``None`` or a reason.

    ``keys`` is ``key_names`` of the same definition; ``version`` the
    service's ``odata_version``.
    """
    try:
        model = OperationDef.model_validate(operation)
    except ValidationError:
        return INVALID_DEFINITION
    bound_keys = keys.get(model.bound_to) if model.bound_to is not None else None
    dialect = DIALECTS.get(version) if isinstance(version, str) else None
    return call_refusal(model, bound_keys, dialect, allow_write=allow_write)


def uncallable_operations(definition: Any, version: Any) -> list[dict[str, str]]:
    """The ENABLED operations no agent can ever call: ``[{name, reason}]``.

    Whatever an agent's switches are (``allow_write`` is taken as on), so
    what is listed is a property of the catalogue: a bound operation whose
    entity set is missing or has no key or (V2, where the key travels as
    parameters) whose key fields are not all parameters, a parameter that
    is always sent and has a type the dialect does not write, an operation
    of a kind its service's version does not have, a version no dialect
    calls. V2 function imports and V4 actions and functions are callable.
    A warning for the admin, not a refusal: a
    definition that holds one is saved all the same (imports would break).
    """
    if not isinstance(definition, dict):
        return []
    keys = key_names(definition)
    out: list[dict[str, str]] = []
    for operation in _dicts(definition.get("operations")):
        name = operation.get("name")
        if operation.get("enabled") is not True or not isinstance(name, str):
            continue
        reason = stored_call_refusal(operation, keys, version)
        if reason is not None:
            out.append({"name": name, "reason": reason})
    return out
