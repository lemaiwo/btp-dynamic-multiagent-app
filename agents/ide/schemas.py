"""Typed request and response models of ``/ide/api`` (plan §1.2).

Why a module of its own: these models are the contract between the backend
and the UI. ``routes.py`` declares one as ``response_model`` on every JSON
route, so a serialiser that adds a field by accident is filtered by FastAPI
and one that drops a field fails validation instead of reaching the UI.
``export_schema()`` writes the same models as one JSON Schema file
(``ui5-ide/webapp/test/contract/ide-api.schema.json``, by
``scripts/export_ide_schema.py``) that the UI's fake backend is tested
against; ``tests/test_ide_schemas.py`` fails while that file is stale.

Conventions:

- Output fields that are always present have no default, so the schema
  lists them as ``required``: the UI may rely on them. A field that can be
  empty is ``X | None`` and still required.
- Timestamps are ISO strings exactly as ``routes._iso`` writes them (``str |
  None``), not ``datetime``: re-serialising would change their format.
- Input models forbid extra fields and take strict types where a loose one
  would change meaning (``"yes"`` is not a boolean, ``"3"`` not a revision).
- Error answers stay ``{detail, code}`` (``ErrorOut``); FastAPI passes a
  returned ``JSONResponse`` through unchanged, so a route's ``response_model``
  never reshapes a refusal.
"""

from __future__ import annotations

import json
import logging
import types
import typing
from typing import Annotated, Any, Literal, get_args

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    RootModel,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.json_schema import models_json_schema

from agents.ide.paths import clean_workspace_path

# The one target-name rule (conventions create, every target path/query and
# session create): starts with a letter or digit, so "." / ".." / "-x" can
# never become a target that a /conventions/{target} path cannot address.
TARGET_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$"
MAX_COMMENT_CHARS = 4000  # = store.MAX_COMMENT_CHARS



def drop_lone_surrogates(value: Any) -> Any:
    """``value`` without lone surrogates when it is a string; anything else
    unchanged, for the field's own type check to refuse.

    A browser that cuts a selection inside an emoji sends half a UTF-16 pair
    (``"\\ud83d"``). Pydantic refuses such a string, and the 422 that
    echoes the input then fails to encode as UTF-8 (a 500). Dropping the
    half pair before validation keeps the request usable; a whole emoji is a
    single code point and is kept.
    """
    if isinstance(value, str):
        return "".join(ch for ch in value if not 0xD800 <= ord(ch) <= 0xDFFF)
    return value


# Free text a developer types (comment body/quote, notes, chat messages).
TextIn = Annotated[str, BeforeValidator(drop_lone_surrogates)]
StrictTextIn = Annotated[StrictStr, BeforeValidator(drop_lone_surrogates)]

SessionType = Literal["change", "diagnose"]
SessionStatus = Literal["idle", "running"]
Waiting = Literal["approval", "comments", "changes", "document"]
BaseStatus = Literal["sap", "absent", "unknown"]
SyntaxStatus = Literal["ok", "errors", "unavailable"]
CommentState = Literal["open", "sent", "addressed", "dismissed"]
CommentAnchor = Literal["file", "document"]
DocumentKind = Literal["design", "plan", "review", "note", "report"]
# = agents.ide.stages.Stage (tests/test_ide_schema_enums.py keeps them equal).
StageName = Literal["chat", "design", "plan", "propose", "review", "done", "investigate"]
MessageRole = Literal["user", "assistant", "system"]
FileState = Literal["read", "modified", "new"]
FindingKind = Literal["dump", "trace", "gateway_error", "auth_check", "odata_call"]
ApprovalStatus = Literal["pending", "approved", "denied", "failed", "expired"]
ApprovalAction = Literal["trace_start", "trace_cancel"]


log = logging.getLogger(__name__)


def coerce_member(value: Any, allowed: Any, fallback: Any, field: str) -> Any:
    """``value`` when it is a member of the ``Literal`` ``allowed``, else
    ``fallback`` (logged as a WARNING, without the stored value).

    The serialisers read enum columns (stage, role, kind, state) through
    this: the response models are strict, so a row written by an older
    version or by hand would otherwise turn a whole list into a 500. The
    fallback is chosen per field as the member that grants nothing (``done``
    for a stage, ``idle`` for a status, ``dismissed`` for a comment), and
    the server's own gates still read the real column.
    """
    members = get_args(allowed)
    if isinstance(value, str) and value in members:
        return value
    log.warning(
        "Coerced %d stored %s value(s) outside the contract to %r", 1, field, fallback
    )
    return fallback


# --- errors -----------------------------------------------------------------


class ErrorOut(BaseModel):
    """A refusal with a stable ``code``; ``detail`` is for people, the UI
    branches on ``code``."""

    detail: str
    code: str


# --- sessions ---------------------------------------------------------------


class PinsOut(BaseModel):
    """What the developer approved: a version per document kind, a revision
    per file. A kind nothing was pinned for is absent, not ``null``
    (``design?: number`` in the UI's types)."""

    design: int | None = None
    plan: int | None = None
    review: int | None = None
    report: int | None = None
    files: dict[str, int] | None = None

    @model_serializer(mode="wrap")
    def _drop_unset(self, handler):
        return {k: v for k, v in handler(self).items() if v is not None}


class SessionOut(BaseModel):
    id: str
    owner: str
    title: str
    target: str
    type: SessionType
    stage: StageName
    status: SessionStatus
    created_at: str | None
    updated_at: str | None
    requests_used: int
    request_cap: int
    # Replaces ``masked`` (masked === !target_non_production): the target's
    # conventions as they are now, the same switch a diagnose run uses.
    target_non_production: bool
    pins: PinsOut
    # Why the session waits for its developer; server-computed (worklist).
    waiting: Waiting | None
    open_comments: int
    unresolved_comments: int
    # The worklist's object line, server-computed (no per-row detail call):
    # distinct object names in path order, at most ``store.OBJECTS_SHOWN``.
    objects: list[str]
    objects_total: int
    # Distinct objects with a ``new`` or ``modified`` file.
    changed_objects: int
    # Findings of a diagnose session; ``None`` for a change session.
    findings_count: int | None


class ArtifactSummaryOut(BaseModel):
    id: str
    stage: StageName
    kind: DocumentKind
    version: int
    created_at: str | None
    # The pins at submission time, e.g. ``{"design": 2}`` on a plan.
    based_on: dict[str, int] | None


class ArtifactOut(ArtifactSummaryOut):
    content: str


class SyntaxItemOut(BaseModel):
    line: int | None
    message: str
    severity: Literal["error", "warning"]


class LintFindingOut(BaseModel):
    line: int | None
    column: int | None
    severity: str
    message: str
    rule: str


class FileSummaryOut(BaseModel):
    path: str
    state: FileState
    object_type: str | None
    object_name: str | None
    revision: int
    base_status: BaseStatus | None
    # Of the file's latest revision; ``None`` = not checked.
    syntax_status: SyntaxStatus | None


class FileDetailOut(FileSummaryOut):
    origin_source: str | None
    proposed_source: str | None
    origin_version: str | None
    lint: list[LintFindingOut]
    # Of the revision served.
    syntax: list[SyntaxItemOut]


class FileRevisionOut(BaseModel):
    revision: int
    run_id: str | None
    created_at: str | None
    chars: int
    syntax_status: SyntaxStatus | None


class SyntaxResultOut(BaseModel):
    path: str
    revision: int
    status: SyntaxStatus
    items: list[SyntaxItemOut]
    checked_at: str | None


class SessionDetailOut(SessionOut):
    artifacts: list[ArtifactSummaryOut]
    files: list[FileSummaryOut]


# --- request bodies of routes.py -------------------------------------------


def _clean_title(value: str) -> str:
    # Plain text on one line, as comment bodies are stored
    # (``store.plain_text``): no control characters (NUL included), no
    # format characters; whitespace runs collapse to one space.
    from agents.ide.store import plain_text

    value = " ".join(plain_text(value or "").split())
    if not value:
        raise ValueError("title must not be empty")
    if len(value) > 200:
        raise ValueError("title is at most 200 characters")
    return value


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: TextIn
    target: str = Field(pattern=TARGET_PATTERN)
    type: Literal["change", "diagnose"] = "change"

    _title = field_validator("title")(_clean_title)


class SessionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: TextIn

    _title = field_validator("title")(_clean_title)


MAX_MESSAGE_CHARS = 20000


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: TextIn = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)

    _text = field_validator("text")(_not_blank)


class OpenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(pattern=r"^[A-Za-z]{4}$")
    name: str = Field(pattern=r"^\s*[A-Za-z0-9_/$]{1,40}\s*$")


class ApprovalDecision(BaseModel):
    """The whole body: a decision. Parameters are never taken from the
    client; what is armed is what the stored proposal says."""

    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["approve", "deny"]


MAX_APPROVE_REVISIONS = 200


class ApproveBody(BaseModel):
    """``POST /sessions/{sid}/approve``: what the developer saw.

    ``version``: the document version (decision D3). Omitted means "the
    latest", as before pins; a version that is no longer the latest is
    refused (409 ``version_changed``).

    ``revisions``: in the propose stage, ``path -> revision`` of every
    proposal the developer saw. It must equal the current revision of every
    proposed file (same paths, same numbers), checked in the same locked,
    conditional step that pins them, or the approve is refused (409
    ``version_changed``), so a stale tab never pins revisions it did not
    show. Sent in another stage it is refused as 409 ``stage_changed``: the
    caller still shows the propose stage. Omitted means today's behaviour.
    """

    model_config = ConfigDict(extra="forbid")
    version: StrictInt | None = Field(default=None, ge=1)
    revisions: dict[StrictStr, Annotated[StrictInt, Field(ge=1)]] | None = Field(
        default=None, max_length=MAX_APPROVE_REVISIONS
    )

    @model_validator(mode="after")
    def _clean_revision_paths(self) -> ApproveBody:
        for path in self.revisions or {}:
            # The same rule as every other file path of the API; a path the
            # rule would rewrite (whitespace) is refused, not normalised.
            if clean_workspace_path(path) != path:
                raise ValueError("revision paths must be clean workspace paths")
        return self


class RequestChangesBody(BaseModel):
    """``POST /sessions/{sid}/request-changes``: an optional note sent with
    the open review comments. A blank note counts as none; with no open
    comment and no note the run is refused (409 ``nothing_to_send``)."""

    model_config = ConfigDict(extra="forbid")
    note: StrictTextIn | None = Field(default=None, max_length=MAX_COMMENT_CHARS)


class AdminSessionRowOut(BaseModel):
    """The admin overview row: metadata only, never content, pins or counts
    of what is inside a session."""

    id: str
    owner: str
    title: str
    target: str
    type: SessionType
    stage: StageName
    status: SessionStatus
    created_at: str | None
    updated_at: str | None


# --- messages and run activity ----------------------------------------------


class MessageOut(BaseModel):
    """A message in the list. The run activity is not in the list (it can be
    large); ``has_activity`` says whether ``.../activity`` has one."""

    id: str
    stage: StageName
    role: MessageRole
    content: str
    created_at: str | None
    has_activity: bool


class ToolEventOut(BaseModel):
    """One event of ``agents.run_activity.RunActivity``: a tool call (with
    ``id``/``tool``/``status``/``output``) or a note/message/delegation."""

    ts: str | None = None
    agent: str | None = None
    kind: str
    id: str | None = None
    tool: str | None = None
    detail: str = ""
    status: str | None = None
    output: str | None = None
    ended: str | None = None
    # Why a call ended as it did, when the runner knows (``readonly_refused``,
    # a trace-proposal refusal code); the UI shows it on the call.
    code: str | None = None


class TodoOut(BaseModel):
    content: str
    status: str


class ActivityOut(BaseModel):
    events: list[ToolEventOut]
    plan: list[TodoOut]
    dropped: int


# --- review comments --------------------------------------------------------


class _CommentBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: TextIn = Field(min_length=1, max_length=MAX_COMMENT_CHARS)
    # The selected text. Accepted up to the body's bound so a long selection
    # is not refused; the store cleans it to one line and cuts it to 200.
    quote: TextIn | None = Field(default=None, max_length=MAX_COMMENT_CHARS)


class FileCommentCreate(_CommentBase):
    anchor: Literal["file"]
    path: str = Field(min_length=1, max_length=200)
    revision: StrictInt = Field(ge=1)
    line_start: StrictInt = Field(ge=1)  # 1-based, inclusive
    line_end: StrictInt = Field(ge=1)


class DocumentCommentCreate(_CommentBase):
    anchor: Literal["document"]
    kind: DocumentKind
    version: StrictInt = Field(ge=1)
    paragraph: StrictInt = Field(ge=0)  # 0-based block index of docView.ts


class CommentCreate(
    RootModel[
        Annotated[
            FileCommentCreate | DocumentCommentCreate, Field(discriminator="anchor")
        ]
    ]
):
    """A new comment on a file range or a document paragraph."""


class CommentPatch(BaseModel):
    """Either an edit (only while open: a new ``body`` and/or ``quote``) or a
    user transition (``state``). A ``quote`` left out keeps the stored one;
    ``quote: null`` clears it."""

    model_config = ConfigDict(extra="forbid")
    body: TextIn | None = Field(default=None, min_length=1, max_length=MAX_COMMENT_CHARS)
    quote: TextIn | None = Field(default=None, max_length=MAX_COMMENT_CHARS)
    state: Literal["open", "dismissed"] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> CommentPatch:
        edit = self.body is not None or "quote" in self.model_fields_set
        if edit == (self.state is not None):
            raise ValueError("give either body/quote or state")
        return self


class CommentOut(BaseModel):
    id: str
    anchor: CommentAnchor
    path: str | None
    revision: int | None
    line_start: int | None
    line_end: int | None
    kind: DocumentKind | None
    version: int | None
    paragraph: int | None
    body: str
    quote: str | None
    state: CommentState
    answer: str | None
    created_at: str | None
    updated_at: str | None


# --- conventions and me -----------------------------------------------------


class ConventionsBody(BaseModel):
    """Update body; omitted fields keep their stored value."""

    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, max_length=120)
    destination: str | None = Field(default=None, max_length=200)
    namespace: str | None = Field(default=None, max_length=30)
    package: str | None = Field(default=None, max_length=30)
    atc_variant: str | None = Field(default=None, max_length=30)
    clean_core_level: str | None = Field(default=None, pattern=r"^[A-D]$")
    free_text: str | None = Field(default=None, max_length=20000)
    # Strict: "yes" or 1 must not switch a target to raw diagnose data.
    non_production: StrictBool | None = None


ClearableField = Literal[
    "label", "destination", "namespace", "package", "atc_variant", "free_text"
]


class ConventionsUpdate(ConventionsBody):
    """``PUT /conventions/{target}``: omitted fields keep their value, the
    fields in ``clear`` are emptied. A subclass rather than
    ``ConventionsBody.clear`` so ``ConventionsCreate`` (which extends
    ``ConventionsBody``) does not accept ``clear``. ``non_production`` and
    ``clean_core_level`` are not clearable: they always hold a value."""

    clear: list[ClearableField] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def _set_xor_clear(self) -> ConventionsUpdate:
        both = set(self.clear) & (self.model_fields_set - {"clear"})
        if both:
            raise ValueError(f"fields both set and cleared: {sorted(both)}")
        return self


class ConventionsCreate(ConventionsBody):
    target: str = Field(pattern=TARGET_PATTERN)


class ConventionsOut(BaseModel):
    target: str
    label: str | None
    destination: str | None
    namespace: str | None
    package: str | None
    atc_variant: str | None
    clean_core_level: str | None
    free_text: str | None
    non_production: bool
    updated_at: str | None


class MeOut(BaseModel):
    principal: str
    # A UI hint only; admin routes enforce the scope themselves.
    is_admin: bool
    targets: list[str]
    diagnose_targets: list[str]
    # The effective IDE_DIAGNOSE_RETENTION_DAYS: days a diagnose session is
    # kept from creation; 0 = kept until deleted. Served so the UI never
    # hard-codes the default.
    diagnose_retention_days: int


class SeedRefreshOut(BaseModel):
    updated: list[str]
    skipped_edited: list[str]
    added: list[str]
    # The rows were saved but rebuilding the registry/chat app failed; the
    # next reload or restart uses them.
    reload_failed: bool = False


# --- findings, approvals, search --------------------------------------------


class FindingOut(BaseModel):
    """Metadata only: the detail text is served by the detail route alone."""

    id: str
    kind: FindingKind
    ref_id: str
    title: str
    program: str | None
    include: str | None
    line: int | None
    occurred_at: str | None
    created_at: str | None


class FindingDetailOut(BaseModel):
    finding: FindingOut
    detail: str


class FindingOpenOut(BaseModel):
    file: FileSummaryOut
    line: int | None
    hint: str | None


class ApprovalOut(BaseModel):
    id: str
    action: ApprovalAction
    params: dict[str, Any]
    status: ApprovalStatus
    created_at: str | None
    ttl_min: int
    decided_at: str | None
    result: dict[str, Any] | None
    error_code: str | None


class ObjectHitOut(BaseModel):
    type: str
    name: str
    package: str
    description: str


# --- schema export ----------------------------------------------------------

CONTRACT_MODELS: tuple[type[BaseModel], ...] = (
    ActivityOut, AdminSessionRowOut, ApprovalDecision, ApprovalOut, ApproveBody,
    ArtifactOut, ArtifactSummaryOut, CommentCreate, CommentOut, CommentPatch,
    ConventionsBody, ConventionsCreate, ConventionsOut, ConventionsUpdate, ErrorOut,
    FileDetailOut, FileRevisionOut, FileSummaryOut, FindingDetailOut, FindingOpenOut,
    FindingOut, LintFindingOut, MeOut, MessageBody, MessageOut, ObjectHitOut, OpenBody,
    PinsOut, RequestChangesBody, SeedRefreshOut, SessionCreate, SessionDetailOut,
    SessionOut, SessionPatch, SyntaxItemOut, SyntaxResultOut, TodoOut, ToolEventOut,
)


API_PREFIX = "/ide/api"
SSE_MEDIA_TYPE = "text/event-stream"

# ``responses=`` for a route that answers a server-sent event stream. It is
# what marks the route as a stream in the route map (and in OpenAPI): the
# route itself returns a ``StreamingResponse``, which FastAPI passes through,
# so this changes no behaviour.
SSE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"description": "Server-sent event stream",
          "content": {SSE_MEDIA_TYPE: {}}},
}


def is_stream_route(route: Any) -> bool:
    """Whether the route declares a ``text/event-stream`` 200 answer."""
    answer = (route.responses or {}).get(200) or (route.responses or {}).get("200")
    return SSE_MEDIA_TYPE in ((answer or {}).get("content") or {})


def _unwrap(annotation: Any) -> tuple[str | None, bool]:
    """``(model name, is a list)`` of a response or body annotation.

    ``list[X]`` is a list of ``X``; ``X | None`` (an optional body) is ``X``.
    Anything that is not a pydantic model has no name in the contract.
    """
    if annotation is None:
        return None, False
    origin = typing.get_origin(annotation)
    if origin is list:
        name, _ = _unwrap(typing.get_args(annotation)[0])
        return name, True
    if origin in (typing.Union, types.UnionType):
        members = [a for a in typing.get_args(annotation) if a is not type(None)]
        return _unwrap(members[0]) if len(members) == 1 else (None, False)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation.__name__, False
    return None, False


def route_map() -> dict[str, dict[str, Any]]:
    """``"<METHOD> <path below /ide/api>"`` -> the models of that route.

    Built from the real routers, so the UI's fake backend maps a route to its
    models from the same source FastAPI serves, not from a hand-kept table.
    Keys are sorted, so the exported file is stable.
    """
    # Imported here: the routers import this module.
    from agents.ide.review_routes import router as review_router
    from agents.ide.routes import router as ide_router

    entries: dict[str, dict[str, Any]] = {}
    for route in (*ide_router.routes, *review_router.routes):
        response, is_list = _unwrap(getattr(route, "response_model", None))
        bodies = route.dependant.body_params
        request = _unwrap(bodies[0].field_info.annotation)[0] if bodies else None
        for method in route.methods:
            key = f"{method} {route.path.removeprefix(API_PREFIX)}"
            entries[key] = {
                "response": response,
                "request": request,
                "list": is_list,
                "stream": is_stream_route(route),
            }
    return {key: entries[key] for key in sorted(entries)}


def export_schema() -> dict[str, Any]:
    """All contract models as ``{"$defs": {name: schema}}``, names sorted,
    plus ``"x-routes"``: the route map (``route_map()``).

    One ``$defs`` for the whole API: nested models (``PinsOut`` inside
    ``SessionOut``) are defined once and referenced as ``#/$defs/<name>``,
    so the file is self-contained for the UI's validator.
    """
    _, top = models_json_schema(
        [(m, "validation") for m in CONTRACT_MODELS],
        ref_template="#/$defs/{model}",
    )
    defs = top.get("$defs", {})
    return {
        "$defs": {name: defs[name] for name in sorted(defs)},
        "x-routes": route_map(),
    }


def render_schema() -> str:
    """The exported schema as the committed file holds it: 2-space indent,
    trailing newline."""
    return json.dumps(export_schema(), indent=2, ensure_ascii=False) + "\n"
