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
  is not listed at all. With ``write_versions``, the same holds for a
  service whose OData version the caller cannot write.
* Operations (function imports, actions, functions) need ``allow_call``,
  which is off unless the caller says it can run them, and with
  ``call_versions`` only for services of a version whose operations it can
  run: a search must never offer what the execute tool would refuse. An
  operation is a write unless it is marked ``changes_data: false`` AND is
  a ``GET`` (``operation_changes_data``); a write is listed only with
  ``allow_write``.
* Results are built key by key from the definition, never by copying a
  catalogue dict, so a UI-only flag (``personal_data``) or a future key
  cannot leak into a tool result.

Pure functions over the ``ODataService.to_dict()`` snapshot: no I/O, no
database, so the registry can hand the same snapshot to every run.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import Any

from .models import ENTITY_OPS, WRITE_OPS, operation_is_write

MAX_SUMMARY_MATCHES = 20
MAX_FULL_TARGETS = 5

# A query is model text: bound the work it can cause, whatever its length.
_MAX_QUERY_CHARS = 200
_MAX_QUERY_TOKENS = 12
_TOKEN_RE = re.compile(r"\w+")
_MIN_TOKEN_CHARS = 2  # shorter query tokens are ignored
_SUBSTRING_MIN_CHARS = 3  # shorter tokens match whole words only

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
    """The query's usable words, case-folded, in order, without repeats.

    A one-character token is dropped: it is in nearly every name and would
    turn any query that contains "a" into a listing of everything.
    """
    text = _text(query)[:_MAX_QUERY_CHARS].casefold()
    words = [w for w in _TOKEN_RE.findall(text) if len(w) >= _MIN_TOKEN_CHARS]
    return list(dict.fromkeys(words))[:_MAX_QUERY_TOKENS]


def _hits(tokens: list[str], *texts: Any) -> int:
    """How many (token, text) pairs match, case-insensitively.

    A token of three or more characters matches as a substring: technical
    names are CamelCase without separators (``PurReqnReleaseStatus``), and
    "release" has to find them. A two-character token matches a whole word
    only, or "re" and "pu" would hit most names in an SAP service.
    """
    total = 0
    for raw in texts:
        text = _text(raw).casefold()
        if not text:
            continue
        words: set[str] | None = None
        for token in tokens:
            if len(token) >= _SUBSTRING_MIN_CHARS:
                total += token in text
            else:
                if words is None:
                    words = set(_TOKEN_RE.findall(text))
                total += token in words
    return total


def visible_operations(entity_set: dict[str, Any], allow_write: bool) -> list[str]:
    """The entity set's enabled operations this agent may use, in a fixed order."""
    enabled = entity_set.get("operations")
    enabled = enabled if isinstance(enabled, list) else []
    return [
        op for op in ENTITY_OPS if op in enabled and (allow_write or op not in WRITE_OPS)
    ]


def _fields_writable(entity_set: dict[str, Any], allow_write: bool) -> bool:
    """Whether a field of this entity set can be written by the caller at all.

    Only through a create or an update that is enabled on the set: on an
    entity set that can merely be read (or deleted from), a ``writable``
    field is not writable for anyone, and is not shown as such.
    """
    return bool({"create", "update"} & set(visible_operations(entity_set, allow_write)))


def _visible_fields(entity_set: dict[str, Any], allow_write: bool) -> list[dict[str, Any]]:
    allow_write = _fields_writable(entity_set, allow_write)
    return [
        f
        for f in _dicts(entity_set.get("fields"))
        if f.get("selectable") is True or (allow_write and f.get("writable") is True)
    ]


def operation_changes_data(operation: dict[str, Any]) -> bool:
    """Whether calling this operation is a write.

    ``models.operation_is_write`` on the stored dict: a read is only what is
    marked ``changes_data: false`` (exactly) AND is sent with ``GET``. A
    missing key must not read as "only reads", and a ``POST`` is a write
    whatever the flag says.
    """
    return operation_is_write(operation.get("changes_data"), operation.get("http_method"))


def _operation_visible(operation: dict[str, Any], allow_write: bool) -> bool:
    if operation.get("enabled") is not True:
        return False
    return allow_write or not operation_changes_data(operation)


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


def _examples_out(entity_set: dict[str, Any], allow_write: bool) -> list[dict[str, Any]]:
    """The entity set's example queries, without any that names a hidden field.

    An example is free text an admin wrote, and nothing at save time checks
    it against the field flags: THIS is the enforcement point that keeps a
    field the agent may not see (switched off, or switched off after the
    example was written) out of a tool result. So it fails closed:

    * an example whose ``description``, ``filter`` or ``orderby`` contains a
      hidden field's name as a word (case-insensitive) is dropped whole --
      rewriting an expression could change what it means;
    * ``select`` keeps only names the agent may select; an example whose
      ``select`` had entries and keeps none is dropped.

    Key names are not hidden (see ``_entity_set_detail``).
    """
    visible = {_text(f.get("name")) for f in _visible_fields(entity_set, allow_write)}
    keys = {_text(k.get("name")) for k in _dicts(entity_set.get("keys"))}
    hidden = {
        _text(f.get("name")).casefold() for f in _dicts(entity_set.get("fields"))
    } - {n.casefold() for n in visible | keys}
    selectable = {
        _text(f.get("name"))
        for f in _dicts(entity_set.get("fields"))
        if f.get("selectable") is True
    }
    out: list[dict[str, Any]] = []
    for example in _dicts(entity_set.get("examples")):
        texts = [_text(example.get(k)) for k in ("description", "filter", "orderby")]
        words = {w for text in texts for w in _TOKEN_RE.findall(text.casefold())}
        if words & hidden:
            continue
        item: dict[str, Any] = {"description": texts[0]}
        for key in ("filter", "orderby"):
            if _text(example.get(key)):
                item[key] = example[key]
        select = example.get("select")
        if isinstance(select, list) and select:
            kept = [n for n in select if isinstance(n, str) and n in selectable]
            if not kept:
                continue
            item["select"] = kept
        top = example.get("top")
        if isinstance(top, int) and not isinstance(top, bool):
            item["top"] = top
        out.append(item)
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
    # Following a navigation reads through one entity of this set, which the
    # execute tool allows only with 'get' enabled here (`ODataClient.check_read`).
    operations = entity_set.get("operations")
    through = isinstance(operations, list) and "get" in operations
    for nav in _dicts(entity_set.get("navigations")) if through else []:
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
        # Deliberately every key, whatever its field's `selectable` flag: a
        # key NAME is always visible, because the agent needs it to address
        # one entity and it is part of every URL anyway. A key VALUE comes
        # back only when the key field is selectable, which
        # `execute_operation` enforces. So a non-selectable key is listed
        # here and not under `fields`.
        "keys": [
            {"name": _text(k.get("name")), "type": _text(k.get("type"))}
            for k in _dicts(entity_set.get("keys"))
        ],
        "fields": [
            _field_out(f, _fields_writable(entity_set, allow_write))
            for f in _visible_fields(entity_set, allow_write)
        ],
        "navigations": navigations,
        "parameters": [],
        "examples": _examples_out(entity_set, allow_write),
        "bound_to": None,
        "changes_data": None,
    }


def _operation_detail(
    operation: dict[str, Any], readable: dict[str, list[str]]
) -> dict[str, Any]:
    bound_to = operation.get("bound_to")
    # Named only when this agent can see that entity set; otherwise the
    # operation would reveal an entity set the catalogue keeps from it.
    if not isinstance(bound_to, str) or not readable.get(bound_to):
        bound_to = None
    return {
        "keys": [],
        "fields": [],
        "navigations": [],
        "parameters": [
            {
                "name": _text(p.get("name")),
                "type": _text(p.get("type")),
                "required": p.get("required") is not False,
            }
            for p in _dicts(operation.get("parameters"))
        ],
        "examples": [],
        "bound_to": bound_to,
        # What a call of it IS for this tool, not merely what is stored.
        "changes_data": operation_changes_data(operation),
    }


class _Candidate:
    """One visible target with its score; the result dict is built later.

    Scoring looks at every visible target of every attached service, while
    at most ``MAX_SUMMARY_MATCHES`` are returned: the (larger) result dicts
    are built for those only.
    """

    __slots__ = (
        "kind", "operations", "raw", "readable", "score", "service", "target", "write",
    )

    def __init__(
        self,
        *,
        score: int,
        service: dict[str, Any],
        kind: str,
        target: str,
        raw: dict[str, Any],
        operations: list[str],
        readable: dict[str, list[str]],
        write: bool,
    ) -> None:
        self.score = score
        self.service = service
        self.kind = kind
        self.target = target
        self.raw = raw
        self.operations = operations
        self.readable = readable
        # Whether writes are visible for THIS service (the entry's switch
        # and the service's version), decided once in `_candidates`.
        self.write = write

    def sort_key(self) -> tuple[int, str, str]:
        return (-self.score, _text(self.service.get("name")), self.target)

    def match(self, *, full: bool) -> dict[str, Any]:
        allow_write = self.write
        name = _text(self.service.get("name"))
        out: dict[str, Any] = {
            "service": name,
            "service_title": _text(self.service.get("title")) or name,
            "target": self.target,
            "kind": self.kind,
            "title": _text(self.raw.get("title")) or self.target,
            "description": _text(self.raw.get("description")),
            "operations": self.operations,
        }
        if full:
            if self.kind == "entity_set":
                out.update(_entity_set_detail(self.raw, self.readable, allow_write))
            else:
                out.update(_operation_detail(self.raw, self.readable))
            out.update(_service_out(self.service))
        return out


def _candidates(
    service: dict[str, Any],
    tokens: list[str],
    allow_write: bool,
    allow_call: bool,
    call_write: bool,
) -> list[_Candidate]:
    """Every visible target of one service with its score.

    ``allow_write`` is already the answer for this service (the entry's
    switch and the service's version), and so are ``allow_call`` (its
    operations can be called at all) and ``call_write`` (also those that
    change data).
    """
    definition = service.get("definition")
    if not isinstance(definition, dict):
        return []
    service_score = _W_SERVICE * _hits(tokens, service.get("title"), service.get("purpose"))
    entity_sets = _dicts(definition.get("entity_sets"))
    readable = {
        _text(e.get("name")): visible_operations(e, allow_write) for e in entity_sets
    }
    out: list[_Candidate] = []

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
        out.append(
            _Candidate(
                score=own + service_score,
                service=service,
                kind="entity_set",
                target=target,
                raw=entity_set,
                operations=operations,
                readable=readable,
                write=allow_write,
            )
        )

    if not allow_call:
        return out
    for operation in _dicts(definition.get("operations")):
        target = _text(operation.get("name"))
        if not target or not _operation_visible(operation, call_write):
            continue
        own = _W_TARGET * _hits(tokens, operation.get("title"), target)
        own += _W_DESCRIPTION * _hits(tokens, operation.get("description"))
        out.append(
            _Candidate(
                score=own + service_score,
                service=service,
                kind="operation",
                target=target,
                raw=operation,
                operations=["call"],
                readable=readable,
                write=allow_write,
            )
        )
    return out


def search_catalogue(
    services: list[dict],
    query: str,
    *,
    detail: str = "summary",
    service: str | None = None,
    allow_write: bool = False,
    allow_call: bool = False,
    write_versions: Collection[str] | None = None,
    call_versions: Collection[str] | None = None,
) -> dict:
    """Rank the visible targets of ``services`` against ``query``.

    ``allow_write`` is the agent entry's switch. ``write_versions`` narrows
    it to the OData versions the caller can actually write (``None``: any):
    for a service of another version nothing write-related is listed.
    ``allow_call`` says the caller can run operations (function imports,
    actions, functions); without it none is listed, whatever it changes.
    ``call_versions`` narrows it to the OData versions whose operations the
    caller can run (``None``: any). An operation is listed only when it is
    enabled and either only reads (``operation_changes_data``) or
    ``allow_write`` is on -- the entry's switch as it is, since what a call
    needs is the switch and the version in ``call_versions``, not entity
    writes of that version.

    ``services`` are ``ODataService.to_dict()`` dicts. Never raises: a
    refusal is ``{"error": {"code", "message", "hint"?}}``, because the
    caller is a model and an exception would end its run instead of letting
    it correct the argument. A refused value is not echoed back.

    An empty query lists every visible target (service, then name). A
    non-empty one keeps the targets with at least one hit, best first:
    5 per hit in the target's title or name, 3 in its description, 2 in a
    visible field's label, name, hint or value meaning, 1 in the service's
    title or purpose; ties by service, then target name. One-character
    words are ignored and two-character words match whole words only.
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
            # Defence in depth: `odata_toolset` already drops a disabled
            # service, so through the tool this name is `unknown_service`.
            return _error(
                "service_disabled",
                "This service is disabled.",
                f"Attached services: {available}",
            )

    tokens = _tokens(query)
    # A query without any word ("", "*") lists everything. One that has
    # words but none usable (all one character) matches nothing: listing
    # everything for "a" would read as "all of this is about a".
    listing = not _TOKEN_RE.search(_text(query)[:_MAX_QUERY_CHARS])
    scored: list[_Candidate] = []
    if tokens or listing:
        for entry in enabled:
            write = allow_write is True and (
                write_versions is None or entry.get("odata_version") in write_versions
            )
            call = allow_call is True and (
                call_versions is None or entry.get("odata_version") in call_versions
            )
            scored.extend(
                _candidates(entry, tokens, write, call, call and allow_write is True)
            )
    if not listing:
        scored = [c for c in scored if c.score > 0]
    scored.sort(key=_Candidate.sort_key)

    limit = MAX_FULL_TARGETS if full else MAX_SUMMARY_MATCHES
    result: dict[str, Any] = {
        "matches": [c.match(full=full) for c in scored[:limit]],
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
            if not listing
            else "Nothing is available to this agent in the attached services."
        )
    return result
