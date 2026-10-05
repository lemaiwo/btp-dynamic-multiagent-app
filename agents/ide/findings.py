"""Findings: what a diagnose run came across, as metadata.

The read-only guard hands every ``SAPDiagnose`` data result of the session
target's server to :func:`collect`: the text the model was given, which is
ARC-1's own text (a diagnose run exists only for a ``non_production``
target).

**Metadata.** A finding is the seven keys of :data:`FINDING_KEYS`. Every
value is taken from a field that names something (an id, a program, a
runtime error, a timestamp, a line), and is dropped unless it has the form
of one -- so a title is never the short text of a dump, a gateway message
or a trace description, which are prose about the failure and the people in
it. A payload whose identity (``ref_id``) is not an identifier yields no
finding.

**Detail.** With ``with_detail=True`` a dump or gateway-error *detail* read
also carries the text itself under ``detail``. :func:`collect` always asks
for it and ``agents.ide.runner`` stores it with the finding; the routes
serve it only while the target is still flagged ``non_production``.

**Never in the way.** Nothing here raises into the run. A result that does
not parse, has an unknown shape or holds a value of the wrong type gives no
finding; an unexpected error is logged by its type, never with a value.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from agents.ide import diagnose, shapes
from agents.ide.models import iso_utc
from agents.ide.schemas import FindingKind, coerce_member

logger = logging.getLogger(__name__)

FINDING_KEYS = (
    "kind", "ref_id", "title", "program", "include", "line", "occurred_at",
)

MAX_PER_CALL = 200  # findings taken from one tool result
MAX_PER_RUN = 500  # findings one run carries
MAX_DETAIL = 200_000  # characters of stored detail text

_MAX_REF = 255
_MAX_NAME = 40
_MAX_TITLE = 200

# An ABAP-side name or id: no blanks, no quotes, no brackets.
_IDENT = re.compile(r"[A-Za-z0-9_/$=.:<>~\-]+")
# The same, with single blanks between words ("Frontend Error").
_LABEL = re.compile(r"[A-Za-z0-9_/$=.:<>~\-]+(?: [A-Za-z0-9_/$=.:<>~\-]+){0,5}")
_STAMP = re.compile(r"\d[0-9T:Z.+\- ]{3,31}")
_URL_PATH = re.compile(r"/[A-Za-z0-9_/.\-~%$=:]*")


def _ident(value: Any, limit: int = _MAX_NAME) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > limit or not _IDENT.fullmatch(value):
        return None
    return value


def _label(value: Any, limit: int = 80) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > limit or not _LABEL.fullmatch(value):
        return None
    return value


def _stamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _STAMP.fullmatch(value) else None


def _line(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value < 10_000_000 else None


def _meta(
    kind: str,
    ref_id: str,
    title: str,
    *,
    program: Any = None,
    include: Any = None,
    line: Any = None,
    occurred_at: Any = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "ref_id": ref_id,
        "title": title[:_MAX_TITLE],
        "program": _ident(program),
        "include": _ident(include),
        "line": _line(line),
        "occurred_at": _stamp(occurred_at),
    }


def _records(value: Any) -> list[dict[str, Any]]:
    """The objects of a list field; anything else is skipped."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _cut(text: str) -> str:
    return text[:MAX_DETAIL]


# --- per shape ---------------------------------------------------------------


def _dump(item: dict[str, Any], fallback_id: Any = None) -> dict[str, Any] | None:
    ref = _ident(item.get("id"), _MAX_REF) or _ident(fallback_id, _MAX_REF)
    if ref is None:
        return None
    title = (
        _ident(item.get("runtimeError"), 80)
        or _ident(item.get("exception"), 80)
        or "Runtime error"
    )
    return _meta(
        "dump", ref, title,
        program=item.get("program"), include=item.get("include"),
        line=item.get("line"), occurred_at=item.get("timestamp"),
    )


def _dumps_list(data: dict[str, Any], args: dict[str, Any], detail: bool) -> list[dict]:
    return [f for f in (_dump(item) for item in _records(data.get("dumps"))) if f]


def _dump_text(data: dict[str, Any]) -> str:
    """The dump as text: ARC-1's formatted text, else its chapters."""
    formatted = data.get("formattedText")
    if isinstance(formatted, str) and formatted.strip():
        return formatted
    chapters = data.get("chapters")
    if not isinstance(chapters, list):
        chapters = data.get("sections")
    parts = []
    for chapter in _records(chapters):
        text = chapter.get("text")
        if isinstance(text, str) and text:
            title = chapter.get("title")
            head = title if isinstance(title, str) and title else str(chapter.get("id", ""))
            parts.append(f"## {head}\n{text}")
    if parts:
        return "\n\n".join(parts)
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


def _dump_detail(data: dict[str, Any], args: dict[str, Any], detail: bool) -> list[dict]:
    finding = _dump(data, args.get("id"))
    if finding is None:
        return []
    if detail:
        finding["detail"] = _cut(_dump_text(data))
    return [finding]


def _traces_list(data: dict[str, Any], args: dict[str, Any], detail: bool) -> list[dict]:
    out = []
    for item in _records(data.get("traces")):
        ref = _ident(item.get("id"), _MAX_REF)
        if ref is None:
            continue
        # The traced object's name. ``title`` and ``description`` are typed
        # by whoever armed the trace and are not used.
        title = _ident(item.get("objectName"), 80) or "ABAP trace"
        out.append(_meta("trace", ref, title, occurred_at=item.get("timestamp")))
    return out


def _gateway(item: dict[str, Any], fallback_id: Any = None) -> dict[str, Any] | None:
    error_type = _label(item.get("errorType"))
    ref = None
    url = item.get("detailUrl")
    if isinstance(url, str):
        # Path only: the query of a detail link can name the user.
        path = url.split("?", 1)[0].split("#", 1)[0].strip()
        if len(path) <= _MAX_REF and len(path) > 1 and _URL_PATH.fullmatch(path):
            ref = path
    if ref is None:
        ident = _ident(item.get("id"), 100) or _ident(fallback_id, 100)
        if ident is None or error_type is None:
            return None
        ref = f"{error_type}:{ident}"
    title = " ".join(
        part for part in (error_type, _ident(item.get("service"), 80)) if part
    ) or "Gateway error"
    source: dict[str, Any] = {}
    stack = _records(item.get("callStack"))
    if stack:
        source = stack[0]
    return _meta(
        "gateway_error", ref, title,
        program=source.get("program"), include=source.get("include"),
        line=source.get("line"), occurred_at=item.get("timestamp"),
    )


def _gateway_list(data: dict[str, Any], args: dict[str, Any], detail: bool) -> list[dict]:
    return [f for f in (_gateway(item) for item in _records(data.get("errors"))) if f]


def _gateway_detail(data: dict[str, Any], args: dict[str, Any], detail: bool) -> list[dict]:
    finding = _gateway(data, args.get("id"))
    if finding is None:
        return []
    if detail:
        finding["detail"] = _cut(
            json.dumps(data, indent=2, ensure_ascii=False, default=str)
        )
    return [finding]


def _auth(data: dict[str, Any], args: dict[str, Any], detail: bool) -> list[dict]:
    out = []
    for row in _records(data.get("rows")):
        rc = row.get("rc")
        # Only a check that is known to have failed is a finding.
        if isinstance(rc, bool) or not isinstance(rc, int) or rc == 0:
            continue
        auth_object = _ident(row.get("authObject"))
        if auth_object is None:
            continue
        program = _ident(row.get("program"))
        stamp = _stamp(row.get("timestamp"))
        out.append(_meta(
            "auth_check", f"{auth_object}:{program or ''}:{stamp or ''}", auth_object,
            program=program, occurred_at=stamp,
        ))
    return out


def _odata_path(url: Any) -> str | None:
    """The path of an OData URL: no query, where the filter values are."""
    if not isinstance(url, str):
        return None
    path = url.split("?", 1)[0].split("#", 1)[0].strip()
    if (
        not path.startswith("/")
        or len(path) > _MAX_REF
        or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in path)
    ):
        return None
    return path


def _odata(data: dict[str, Any], args: dict[str, Any], detail: bool) -> list[dict]:
    fallback = _odata_path(data.get("url")) or _odata_path(args.get("url"))
    out = []
    for request in _records(data.get("requests")):
        path = _odata_path(request.get("url")) or fallback
        if path is None:
            continue
        out.append(_meta(
            "odata_call", path, "OData timing", occurred_at=request.get("timestamp"),
        ))
    return out


_Extractor = Callable[[dict[str, Any], dict[str, Any], bool], list[dict]]

# By the name of the shape ``agents.ide.shapes`` recognises. A shape that is not here
# (trace analyses, trace requests, SQL trace state) yields no finding.
_EXTRACTORS: dict[str, _Extractor] = {
    "dumps_list": _dumps_list,
    "dump_detail": _dump_detail,
    "traces_list": _traces_list,
    "gateway_errors_list": _gateway_list,
    "gateway_error_detail": _gateway_detail,
    "authorization_trace": _auth,
    "odata_perf": _odata,
}


# --- public ------------------------------------------------------------------


def extract(
    tool: Any, args: Any, text: Any, *, with_detail: bool = False
) -> list[dict[str, Any]]:
    """The findings in one ``SAPDiagnose`` data result. Never raises.

    ``text`` is the result as the model got it. Each finding has exactly the
    keys of :data:`FINDING_KEYS`; with ``with_detail`` a detail read adds
    ``detail`` (the text, cut to :data:`MAX_DETAIL`).
    """
    try:
        if (
            not isinstance(text, str)
            or not isinstance(args, dict)
            or not text
            or len(text) > shapes.MAX_INPUT
        ):
            return []
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            return []
        shape = shapes.match_shape(tool, args, data)
        extractor = _EXTRACTORS.get(shape) if shape else None
        if extractor is None:
            return []
        found = extractor(data, args, bool(with_detail))
        merged: list[dict[str, Any]] = []
        merge(merged, found, limit=MAX_PER_CALL)
        return merged
    except Exception as exc:  # noqa: BLE001 -- a finding is never worth a run
        # The type only: the message of an error can quote the payload.
        logger.warning("finding extraction failed: %s", type(exc).__name__)
        return []


def merge(
    into: list[dict[str, Any]],
    items: list[dict[str, Any]],
    *,
    limit: int = MAX_PER_RUN,
) -> None:
    """Add ``items`` to ``into``, one entry per ``(kind, ref_id)``.

    A value that is ``None`` in a later item does not replace an earlier
    one: a list read after a detail read knows less, not something else.
    New findings beyond ``limit`` are dropped; known ones are still updated.
    """
    index = {(f.get("kind"), f.get("ref_id")): f for f in into}
    for item in items:
        key = (item.get("kind"), item.get("ref_id"))
        known = index.get(key)
        if known is None:
            if len(into) >= limit:
                continue
            known = dict(item)
            into.append(known)
            index[key] = known
            continue
        for name, value in item.items():
            if value is not None:
                known[name] = value


def collect(run: Any, tool: Any, args: Any, result: Any) -> None:
    """Record the findings of one tool result on the diagnose run.

    Called by the guard with the result it is about to return. Every agent
    of a run -- the top-level one, a delegate, a deep sub-agent -- runs in a
    context that holds the same ``DiagnoseRun``, so all of them add to the
    same list, detail text included. Never raises.
    """
    try:
        found = extract(
            tool, args, diagnose.result_text(result), with_detail=True,
        )
        if found:
            merge(run.findings, found)
    except Exception as exc:  # noqa: BLE001
        logger.warning("finding collection failed: %s", type(exc).__name__)


def finding_out(row: Any) -> dict[str, Any]:
    """An ``IdeFinding`` row as the contract's ``Finding`` (no detail text).

    A stored kind outside the contract (an older version, a hand edit) is
    served as ``trace`` with a WARNING rather than failing the whole list."""
    created = getattr(row, "created_at", None)
    return {
        "id": row.id,
        "kind": coerce_member(row.kind, FindingKind, "trace", "finding kind"),
        "ref_id": row.ref_id,
        "title": row.title,
        "program": row.program,
        "include": row.include,
        "line": row.line,
        "occurred_at": row.occurred_at,
        "created_at": iso_utc(created),
    }
