"""Search over the catalogue services attached to one agent.

The catalogue is the only description of the SAP back end a model gets, so
this module decides what exists for it. Three rules hold for every result:

* Only what the catalogue enables is listed. An entity set without an
  enabled operation, a disabled operation, a disabled service and a field
  that is neither selectable nor (with ``allow_write``) writable are not
  ranked on and not returned -- a model cannot learn a hidden name by
  searching for it.
* Writes need ``allow_write``: without it, write operations are left out of
  an entity set's ``operations``, and an operation that ``changes_data``
  is not listed at all.
* Results are built key by key from the definition, never by copying a
  catalogue dict, so a UI-only flag (``personal_data``) or a future key
  cannot leak into a tool result.

Pure functions over the ``ODataService.to_dict()`` snapshot: no I/O, no
database, so the registry can hand the same snapshot to every run.
"""

from __future__ import annotations

import re
from typing import Any

from .models import ENTITY_OPS, WRITE_OPS

MAX_SUMMARY_MATCHES = 20
MAX_FULL_TARGETS = 5

# A query is model text: bound the work it can cause, whatever its length.
_MAX_QUERY_CHARS = 200
_MAX_QUERY_TOKENS = 12
_TOKEN_RE = re.compile(r"\w+")

_W_TARGET = 5  # title and technical name of an entity set / operation
_W_DESCRIPTION = 3
_W_FIELD = 2  # a visible field's label, name, hint or value meaning
_W_SERVICE = 1  # the service's title and purpose

_DETAILS = ("summary", "full")


def _error(code: str, message: str, hint: str = "") -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if hint:
        error["hint"] = hint
    return {"error": error}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _tokens(query: Any) -> list[str]:
    text = _text(query)[:_MAX_QUERY_CHARS].casefold()
    return list(dict.fromkeys(_TOKEN_RE.findall(text)))[:_MAX_QUERY_TOKENS]


def _hits(tokens: list[str], *texts: Any) -> int:
    """How many (token, text) pairs match, case-insensitively, as substrings.

    Substring, not word, matching: technical names are CamelCase without
    separators (``PurReqnReleaseStatus``), and "release" has to find them.
    """
    folded = [_text(t).casefold() for t in texts]
    return sum(1 for text in folded if text for token in tokens if token in text)


def visible_operations(entity_set: dict[str, Any], allow_write: bool) -> list[str]:
    """The entity set's enabled operations this agent may use, in a fixed order."""
    enabled = entity_set.get("operations")
    enabled = enabled if isinstance(enabled, list) else []
    return [
        op for op in ENTITY_OPS if op in enabled and (allow_write or op not in WRITE_OPS)
    ]


def _visible_fields(entity_set: dict[str, Any], allow_write: bool) -> list[dict[str, Any]]:
    return [
        f
        for f in _dicts(entity_set.get("fields"))
        if f.get("selectable") is True or (allow_write and f.get("writable") is True)
    ]


def _operation_visible(operation: dict[str, Any], allow_write: bool) -> bool:
    if operation.get("enabled") is not True:
        return False
    # Anything but an explicit `False` is a write: the model default is
    # `changes_data=True`, and a missing key must not read as "only reads".
    return allow_write or operation.get("changes_data") is False


def _field_out(field: dict[str, Any], allow_write: bool) -> dict[str, Any]:
    return {
        "name": _text(field.get("name")),
        "type": _text(field.get("type")),
        "label": _text(field.get("label")),
        "filterable": field.get("filterable") is True,
        # Without allow_write no write reaches SAP, so no field is writable
        # for this agent, whatever the catalogue says.
        "writable": allow_write and field.get("writable") is True,
        "hint": _text(field.get("hint")),
        "values": [
            {"value": _text(v.get("value")), "meaning": _text(v.get("meaning"))}
            for v in _dicts(field.get("values"))
        ],
    }


def _example_out(example: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"description": _text(example.get("description"))}
    for key in ("filter", "orderby"):
        if _text(example.get(key)):
            out[key] = example[key]
    select = example.get("select")
    if isinstance(select, list) and select:
        out["select"] = [s for s in select if isinstance(s, str)]
    top = example.get("top")
    if isinstance(top, int) and not isinstance(top, bool):
        out["top"] = top
    return out


def _service_out(service: dict[str, Any]) -> dict[str, Any]:
    return {
        "purpose": _text(service.get("purpose")),
        "not_for": _text(service.get("not_for")),
        "runs_as": "signed-in user" if service.get("user_context") is True else "technical user",
    }


def _entity_set_detail(
    entity_set: dict[str, Any], readable: dict[str, list[str]], allow_write: bool
) -> dict[str, Any]:
    navigations = []
    for nav in _dicts(entity_set.get("navigations")):
        # A navigation can be followed only into an entity set this agent
        # may read that way; any other one would be a name that always fails.
        needed = "list" if nav.get("collection") is True else "get"
        if needed in readable.get(_text(nav.get("target")), []):
            navigations.append(
                {
                    "name": _text(nav.get("name")),
                    "target": _text(nav.get("target")),
                    "collection": nav.get("collection") is True,
                    "description": _text(nav.get("description")),
                }
            )
    return {
        "keys": [
            {"name": _text(k.get("name")), "type": _text(k.get("type"))}
            for k in _dicts(entity_set.get("keys"))
        ],
        "fields": [_field_out(f, allow_write) for f in _visible_fields(entity_set, allow_write)],
        "navigations": navigations,
        "examples": [_example_out(e) for e in _dicts(entity_set.get("examples"))],
    }


def _operation_detail(operation: dict[str, Any]) -> dict[str, Any]:
    bound_to = operation.get("bound_to")
    return {
        "bound_to": bound_to if isinstance(bound_to, str) else None,
        "changes_data": operation.get("changes_data") is not False,
        "parameters": [
            {
                "name": _text(p.get("name")),
                "type": _text(p.get("type")),
                "required": p.get("required") is not False,
            }
            for p in _dicts(operation.get("parameters"))
        ],
    }


def _candidates(
    service: dict[str, Any], tokens: list[str], allow_write: bool, full: bool
) -> list[tuple[int, dict[str, Any]]]:
    """Every visible target of one service with its score."""
    definition = service.get("definition")
    if not isinstance(definition, dict):
        return []
    name = _text(service.get("name"))
    head = {"service": name, "service_title": _text(service.get("title")) or name}
    service_score = _W_SERVICE * _hits(tokens, service.get("title"), service.get("purpose"))
    entity_sets = _dicts(definition.get("entity_sets"))
    readable = {
        _text(e.get("name")): visible_operations(e, allow_write) for e in entity_sets
    }
    out: list[tuple[int, dict[str, Any]]] = []

    for entity_set in entity_sets:
        target = _text(entity_set.get("name"))
        operations = readable.get(target, [])
        if not target or not operations:
            continue
        own = _W_TARGET * _hits(tokens, entity_set.get("title"), target)
        own += _W_DESCRIPTION * _hits(tokens, entity_set.get("description"))
        for field in _visible_fields(entity_set, allow_write):
            meanings = [v.get("meaning") for v in _dicts(field.get("values"))]
            own += _W_FIELD * _hits(
                tokens, field.get("label"), field.get("name"), field.get("hint"), *meanings
            )
        match = {
            **head,
            "target": target,
            "kind": "entity_set",
            "title": _text(entity_set.get("title")) or target,
            "description": _text(entity_set.get("description")),
            "operations": operations,
        }
        if full:
            match.update(_entity_set_detail(entity_set, readable, allow_write))
            match.update(_service_out(service))
        out.append((own + service_score, match))

    for operation in _dicts(definition.get("operations")):
        target = _text(operation.get("name"))
        if not target or not _operation_visible(operation, allow_write):
            continue
        own = _W_TARGET * _hits(tokens, operation.get("title"), target)
        own += _W_DESCRIPTION * _hits(tokens, operation.get("description"))
        match = {
            **head,
            "target": target,
            "kind": "operation",
            "title": _text(operation.get("title")) or target,
            "description": _text(operation.get("description")),
            "operations": ["call"],
        }
        if full:
            match.update(_operation_detail(operation))
            match.update(_service_out(service))
        out.append((own + service_score, match))
    return out


def search_catalogue(
    services: list[dict],
    query: str,
    *,
    detail: str = "summary",
    service: str | None = None,
    allow_write: bool = False,
) -> dict:
    """Rank the visible targets of ``services`` against ``query``.

    ``services`` are ``ODataService.to_dict()`` dicts. Never raises: a
    refusal is ``{"error": {"code", "message", "hint"?}}``, because the
    caller is a model and an exception would end its run instead of letting
    it correct the argument. A refused value is not echoed back.

    An empty query lists every visible target (service, then name). A
    non-empty one keeps the targets with at least one hit, best first:
    5 per hit in the target's title or name, 3 in its description, 2 in a
    visible field's label, name, hint or value meaning, 1 in the service's
    title or purpose; ties by service, then target name.
    """
    if detail not in _DETAILS:
        return _error("invalid_argument", "detail must be 'summary' or 'full'")
    full = detail == "full"
    usable = [
        s for s in services if isinstance(s, dict) and isinstance(s.get("name"), str)
    ]
    enabled = [s for s in usable if s.get("enabled", True) is not False]

    if service is not None:
        available = ", ".join(sorted(s["name"] for s in enabled)) or "none"
        chosen = [s for s in usable if s["name"] == service]
        if not chosen:
            return _error(
                "unknown_service",
                "No such service is attached to this agent.",
                f"Attached services: {available}",
            )
        enabled = [s for s in chosen if s in enabled]
        if not enabled:
            return _error(
                "service_disabled",
                "This service is disabled.",
                f"Attached services: {available}",
            )

    tokens = _tokens(query)
    scored: list[tuple[int, dict[str, Any]]] = []
    for entry in enabled:
        scored.extend(_candidates(entry, tokens, allow_write, full))
    if tokens:
        scored = [pair for pair in scored if pair[0] > 0]
    scored.sort(key=lambda pair: (-pair[0], pair[1]["service"], pair[1]["target"]))

    limit = MAX_FULL_TARGETS if full else MAX_SUMMARY_MATCHES
    result: dict[str, Any] = {
        "matches": [match for _, match in scored[:limit]],
        "total": len(scored),
    }
    if len(scored) > limit:
        result["hint"] = (
            f"Showing {limit} of {len(scored)}. Narrow the query"
            + ("" if service is not None else " or pass 'service'")
            + " to see the others."
        )
    elif not scored:
        result["hint"] = (
            "Nothing matched. Try other words, or an empty query to list "
            "everything this agent can use."
            if tokens
            else "Nothing is available to this agent in the attached services."
        )
    return result
