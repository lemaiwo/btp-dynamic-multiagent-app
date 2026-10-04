"""Recognition of ``SAPDiagnose`` result shapes.

Moved out of ``agents.ide.masking`` so that finding extraction no longer
depends on the masking module: a payload is matched to a named shape by the
call (tool, ``action``, ``id``) and the keys it carries.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

MAX_INPUT = 2_000_000  # chars; larger results are not parsed

# Field kinds
_IDENT, _NUM, _TIME, _ID, _TEXT, _LONG, _BOX, _CHAPTERS, _FORMATTED = (
    "ident", "num", "time", "id", "text", "long", "box", "chapters", "formatted",
)


def _norm(key: str) -> str:
    """Key match is case-insensitive and ignores ``-``/``_`` (``sy-uname``
    and ``syUname`` are the same key)."""
    return key.lower().replace("-", "").replace("_", "")


def _kinds(kind: str, *names: str) -> dict[str, str]:
    return {_norm(n): kind for n in names}


# Scalar fields every shape may carry. Keys are normalised.
_BASE: dict[str, str] = {
    **_kinds(
        _IDENT,
        "program", "include", "class", "className", "method", "exception", "runtimeError",
        "tcode", "transaction", "objectType", "authObject", "table", "service",
        "processType", "type", "client", "callingProgram", "calledProgram",
        "package", "component", "functionModule", "report", "analysis", "kind", "state",
        *(f"field{i}" for i in range(1, 11)),
        *(f"value{i}" for i in range(1, 11)),
    ),
    **_kinds(
        _NUM,
        "line", "duration", "grossTime", "netTime", "hits", "executions", "count", "total",
        "size", "rc", "status", "level", "maxExecutions", "active", "aggregate", "sqlTrace",
        "records",
    ),
    **_kinds(
        _TIME,
        "timestamp", "date", "time", "datetime", "created", "createdAt", "changedAt",
        "occurredAt", "expires", "expiresAt", "startTime", "endTime",
    ),
    **_kinds(_ID, "id", "traceId", "requestId"),
    **_kinds(
        _TEXT,
        "shortText", "description", "title", "message", "statement", "url", "detailUrl",
        "errorType", "objectName",  # "Frontend Error"; an object name or a URL
    ),
    **_kinds(_LONG, "text", "content", "lines"),
}


@dataclass(frozen=True)
class Shape:
    """A known payload: which call produces it, which keys prove it and
    which fields may pass.

    ``variant`` is ``list`` for a call without ``id``, ``detail`` for one
    with ``id``, and for ``traces`` with ``id`` the ``analysis`` value
    (default ``hitlist``). ``required`` holds alternatives: the payload
    matches when *any* of the key sets is fully present. ``fields`` maps a
    normalised key to its kind, at any depth of the payload.
    """

    action: str
    variant: str
    required: tuple[frozenset[str], ...]
    fields: Mapping[str, str]


def _shape(
    action: str, variant: str, required: tuple[set[str], ...], **extra: str
) -> Shape:
    fields = dict(_BASE)
    fields.update({_norm(k): v for k, v in extra.items()})
    return Shape(
        action, variant, tuple(frozenset(r) for r in required), MappingProxyType(fields)
    )


SHAPES: dict[str, Shape] = {
    "dumps_list": _shape("dumps", "list", ({"dumps"},), dumps=_BOX),
    "dump_detail": _shape(
        "dumps",
        "detail",
        ({"chapters"}, {"sections"}, {"formattedText"}),
        chapters=_CHAPTERS,
        sections=_CHAPTERS,
        formattedText=_FORMATTED,
    ),
    "traces_list": _shape("traces", "list", ({"traces"},), traces=_BOX),
    "trace_hitlist": _shape("traces", "hitlist", ({"hitlist"},), hitlist=_BOX),
    "trace_statements": _shape(
        "traces", "statements", ({"statements"},), statements=_BOX, children=_BOX
    ),
    "trace_dbaccesses": _shape("traces", "dbAccesses", ({"dbAccesses"},), dbAccesses=_BOX),
    "authorization_trace": _shape("authorization_trace", "list", ({"rows"},), rows=_BOX),
    "gateway_errors_list": _shape("gateway_errors", "list", ({"errors"},), errors=_BOX),
    "gateway_error_detail": _shape(
        "gateway_errors", "detail", ({"errorType"},), callStack=_BOX
    ),
    "odata_perf": _shape("odata_perf", "list", ({"requests"},), requests=_BOX),
    "trace_requests": _shape(
        "trace_requests", "list", ({"traceRequests"},), traceRequests=_BOX
    ),
    "sql_trace_state": _shape("sql_trace_state", "list", ({"active"}, {"state"})),
    "sql_trace_directory": _shape(
        "sql_trace_directory",
        "list",
        ({"files"}, {"traces"}, {"directory"}),
        files=_BOX,
        traces=_BOX,
        directory=_BOX,
    ),
}

_TOOL = "SAPDiagnose"


def _variant(action: str, args: dict[str, Any]) -> str | None:
    has_id = args.get("id") not in (None, "")
    if not has_id:
        return "list"
    if action == "traces":
        analysis = args.get("analysis", "hitlist")
        return analysis if isinstance(analysis, str) and analysis else None
    return "detail"


def _is_diagnose(tool: Any) -> bool:
    """``SAPDiagnose``, also behind an MCP server prefix (``arc1_SAPDiagnose``)."""
    return isinstance(tool, str) and (tool == _TOOL or tool.endswith("_" + _TOOL))


def match_shape(tool: Any, args: Any, data: Any) -> str | None:
    if not _is_diagnose(tool) or not isinstance(args, dict) or not isinstance(data, dict):
        return None
    action = args.get("action")
    if not isinstance(action, str):
        return None
    variant = _variant(action, args)
    for name, shape in SHAPES.items():
        if shape.action != action or shape.variant != variant:
            continue
        if any(req <= data.keys() for req in shape.required):
            return name
    return None
