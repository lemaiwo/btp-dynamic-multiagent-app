"""Invoke API: run one agent as the signed-in user, with attachments.

Another application (an expense app in another subaccount, say) calls an
agent by its slug on behalf of its user, hands it a text and a few files (a
receipt as an image or PDF), and wants back a structured result plus what
the agent changed. Neither existing entry point fits: the scheduler endpoint
(``agents/api_runs.py``) runs as the agent's fixed technical user with the
stored prompt, and A2A (``agents/a2a.py``) always talks to the orchestrator,
keeps tasks in memory and takes no files.

Routes, both behind the ``invoke`` scope (``require_invoke``; the approuter
demands it as well, the backend has a public CF route of its own):

* ``POST /api/invoke/{slug}``, multipart: ``input`` (text, required) and
  ``files`` (0..n). The agent must exist, be enabled and have
  ``expose_invoke``; an agent whose ``builtin:bitbucket`` entry approves is
  never run here. Every one of those cases is the same 404, which names
  nothing. Answers ``202 {"run_id"}``; ``?wait=<0..30>`` waits that many
  seconds and answers 200 with the poll answer when the run finished.
* ``GET /api/invoke/runs/{run_id}``: the poll answer, only to the principal
  that started the run (404 for anybody else and for a run of another kind):
  ``{status, result, writes, text, activity, error}``.

Identity. The run is the caller's: ``current_principal`` and ``current_jwt``
come from the validated JWT the middleware bound, and the run's task is
created inside the request, so it inherits both (contextvars are copied at
task creation). No ``run_as`` and no ``run_as_principal``: a destination
that propagates the user sees this user, and an OAuth server needs this
user's token.

Attachments are held in memory for the run only: the multipart body is read
here (``_read_form``) instead of by Starlette, whose parser spools a file
over 1 MB to a temporary file on disk. They are never stored and never
logged; the run's activity gets one entry per file with its cleaned name,
media type and size. The media type is decided by the file's magic bytes
(PNG, JPEG, PDF); a declared type that disagrees is refused. A PDF's pages
are counted conservatively (``count_pdf_pages``). The files reach the model
as pydantic-ai ``BinaryContent`` (proved for the OpenAI-compatible and the
Bedrock route by ``scripts/probe_attachments.py``).

Result. The specialist gets ``submit_result(result)`` as the OUTPUT tool of
this run (``Agent.run(output_type=[ToolOutput(...), str])``, never registered
in the registry build): a valid call ends the run, so the model is not asked
for anything after the hand-in (a model that answers nothing then, as
``gpt-5.6-terra`` did, ends the run all the same). The tool checks the object
against the agent's ``result_schema`` (``agents/invoke_config.py``) and
answers a violation as a ``ModelRetry`` the model can correct, at most
``RESULT_RETRIES`` times. A run that ends with text instead is ``failed``
with ``no_result``: the caller never reads a result the model only talked
about. ``text`` is the text the model wrote in the response that carried the
hand-in, if any.

Writes. ``recording(run_id)`` binds the run's id (``current_run_id``), which
``builtin:odata`` writes into every audit row of the run, peers and deep
sub-agents included; the poll answer lists those rows (service, entity set,
operation, the key SAP returned for a create, outcome) and nothing else.

Rows and locks. A run is a ``job_runs`` row with ``trigger="invoke"`` and
``created_by`` the caller, ``timeout_seconds`` the run limit
(``INVOKE_TIMEOUT_SECONDS``, default 300) plus a margin so the stale sweep
agrees with the timeout here. One run per (principal, agent) at a time (409
with the running id) and at most ``INVOKE_MAX_CONCURRENT`` per app instance
(429); both are in memory, per process, as is the job runner's start lock.
Invoke rows do not count as the scheduler's overlap lock (``active_job_run``).

Errors to the caller are stable codes with fixed texts; an exception's text
goes to the row (masked by ``describe_exception``, read by admins) and the
log, never to the poll answer.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic_ai import ModelRetry
from pydantic_ai.messages import BinaryContent, ModelResponse, TextPart
from pydantic_ai.output import ToolOutput
from sqlalchemy import select, update

from agents.auth import current_principal, require_invoke
from agents.db import (
    ACTIVE_RUN_STATUS,
    AgentConfig,
    JobRun,
    ODataAuditLog,
    SessionLocal,
    bitbucket_approves,
    create_job_run,
    get_agent_by_slug,
    get_job_run,
)
from agents.errors import describe_exception
from agents.invoke_config import (
    JPEG,
    PDF,
    InvokeLimits,
    loads_or_none,
    parse_limits,
)
from agents.invoke_config import validate as validate_result
from agents.job_runner import _finalize, _sweep_stale, find_credential_blocker
from agents.multipart_form import (
    MAX_PDF_STREAMS,
    PDF_INFLATE_BUDGET,
    FormRefused,
    FormSpec,
    read_multipart,
    sniff,
)
from agents.multipart_form import clean_name as _clean_name
from agents.multipart_form import count_pdf_pages as _count_pdf_pages
from agents.registry import _make_progress_handler, registry
from agents.run_activity import final_activity, live_activity, recording
from agents.shared import run_usage_limits

logger = logging.getLogger(__name__)

router = APIRouter(tags=["invoke"])

TRIGGER = "invoke"
INVOKE_TIMEOUT_SECONDS = 300
MAX_INVOKE_TIMEOUT_SECONDS = 3600
# The row's timeout_seconds (read by the stale sweep) is the deadline plus
# this: the sweep counts from started_at, the deadline from the task's start.
ROW_TIMEOUT_MARGIN_SECONDS = 60
CANCELED_ERROR = "canceled: the run was cancelled"
MAX_WAIT_SECONDS = 30
DEFAULT_MAX_CONCURRENT = 8
DEFAULT_UPLOAD_TIMEOUT_SECONDS = 60.0
INPUT_MAX_BYTES = 64 * 1024
MAX_RESULT_CHARS = 64_000
SUMMARY_CHARS = 200
NAME_CHARS = 120
# Decompressed bytes a page count may inflate across all streams of one PDF
# (agents/multipart_form.py); a test lowers it here.
_PDF_INFLATE_BUDGET = PDF_INFLATE_BUDGET

# Fixed texts: none names a file, a value, the agent or the caller.
NOT_FOUND = "No such agent"
RUN_NOT_FOUND = "No such run"
MSG_NOT_MULTIPART = "The request must be multipart/form-data"
MSG_MALFORMED = "The multipart body could not be read"
MSG_DISCONNECTED = "The request body was not received completely"
MSG_UNEXPECTED_FIELD = "The form holds a field other than 'input' and 'files'"
MSG_INPUT_MISSING = "The field 'input' is required and must not be empty"
MSG_INPUT_TOO_LARGE = "The field 'input' is too large"
MSG_INPUT_NOT_TEXT = "The field 'input' must be UTF-8 text"
MSG_BODY_TOO_LARGE = "The request body is too large"
MSG_FILE_TOO_LARGE = "A file is larger than this agent accepts"
MSG_TOO_MANY_FILES = "More files than this agent accepts"
MSG_EMPTY_FILE = "A file is empty"
MSG_TYPE_NOT_ALLOWED = "A file is of a media type this agent does not accept"
MSG_TYPE_MISMATCH = "A file's content does not match its declared media type"
MSG_TOO_MANY_PAGES = "A PDF has more pages than this agent accepts"
MSG_PAGES_UNKNOWN = "The page count of a PDF could not be determined"
MSG_BUSY = "A run of this agent for this user is already in progress"
MSG_CAPACITY = "Too many invoke runs are in progress; try again later"
MSG_UPLOAD_TIMEOUT = "The request body was not received in time"

# Codes a run row's `error` may start with (`<code>: <text>`); the poll
# answer gives the code only, anything else is `run_failed`.
ERROR_CODES = frozenset({
    "no_result", "timeout", "canceled", "agent_missing", "agent_not_built",
    "credential_required", "run_failed",
})

_DECLARED_ALIASES = {"image/jpg": JPEG, "image/pjpeg": JPEG}
_UNDECLARED = ("", "application/octet-stream")

# Background tasks are kept referenced (asyncio holds weak references only).
_tasks: set[asyncio.Task] = set()
# (principal, agent id) -> run id of the run in flight, or None while its
# upload is being read. Per process.
_active: dict[tuple[str, int], str | None] = {}
_start_lock = asyncio.Lock()


def _env_positive(name: str, default: float) -> float:
    """A positive number from the environment, read per call; anything else
    is the default (the variable is named in the log, never its value)."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = 0
    if not value > 0 or value == float("inf"):
        logger.warning("%s is not a positive number; using %s", name, default)
        return default
    return value


def invoke_timeout() -> float:
    """``INVOKE_TIMEOUT_SECONDS`` (default 300, at most 3600): the deadline
    around one invoke run, read per call. The module constant is the default,
    so a test can still patch it."""
    value = _env_positive("INVOKE_TIMEOUT_SECONDS", INVOKE_TIMEOUT_SECONDS)
    if value > MAX_INVOKE_TIMEOUT_SECONDS:
        logger.warning("INVOKE_TIMEOUT_SECONDS is above %d; using %d",
                       MAX_INVOKE_TIMEOUT_SECONDS, MAX_INVOKE_TIMEOUT_SECONDS)
        return float(MAX_INVOKE_TIMEOUT_SECONDS)
    return value


def max_concurrent() -> int:
    """``INVOKE_MAX_CONCURRENT`` (default 8), a positive integer."""
    value = _env_positive("INVOKE_MAX_CONCURRENT", DEFAULT_MAX_CONCURRENT)
    if value != int(value):
        logger.warning("INVOKE_MAX_CONCURRENT is not an integer; using %d",
                       DEFAULT_MAX_CONCURRENT)
        return DEFAULT_MAX_CONCURRENT
    return int(value)


def upload_timeout() -> float:
    """``INVOKE_UPLOAD_TIMEOUT_SECONDS`` (default 60): how long reading one
    request body may take."""
    return _env_positive("INVOKE_UPLOAD_TIMEOUT_SECONDS", DEFAULT_UPLOAD_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------
@dataclass
class Attachment:
    name: str
    media_type: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


def clean_name(raw: str) -> str:
    """A file name for the activity: no control, format or separator
    characters, at most ``NAME_CHARS`` characters."""
    return _clean_name(raw, NAME_CHARS)


def count_pdf_pages(data: bytes) -> int:
    """``agents.multipart_form.count_pdf_pages`` under this module's budget
    (read per call, so a test can lower it)."""
    return _count_pdf_pages(data, budget=_PDF_INFLATE_BUDGET, max_streams=MAX_PDF_STREAMS)


# The refusal of a body; kept under its old name for the callers and tests.
_FormRefused = FormRefused


def _form_spec(limits: InvokeLimits) -> FormSpec:
    return FormSpec(
        text_fields={"input": INPUT_MAX_BYTES},
        max_files=limits.max_files,
        max_file_bytes=limits.max_file_bytes,
        msg_not_multipart=MSG_NOT_MULTIPART,
        msg_malformed=MSG_MALFORMED,
        msg_disconnected=MSG_DISCONNECTED,
        msg_unexpected_field=MSG_UNEXPECTED_FIELD,
        msg_body_too_large=MSG_BODY_TOO_LARGE,
        msg_file_too_large=MSG_FILE_TOO_LARGE,
        msg_text_too_large=MSG_INPUT_TOO_LARGE,
        msg_too_many_files=MSG_TOO_MANY_FILES,
    )


async def _read_form(request: Request, limits: InvokeLimits) -> tuple[str, list[Attachment]]:
    """The ``input`` text and the checked attachments of the request."""
    parts = await read_multipart(request, _form_spec(limits), label="invoke")
    texts = [p for p in parts if p.filename is None]
    if len(texts) != 1:
        raise _FormRefused(422, MSG_INPUT_MISSING)
    try:
        text = bytes(texts[0].data).decode("utf-8")
    except UnicodeDecodeError:
        raise _FormRefused(422, MSG_INPUT_NOT_TEXT) from None
    if not text.strip():
        raise _FormRefused(422, MSG_INPUT_MISSING)

    attachments: list[Attachment] = []
    for part in parts:
        if part.filename is None:
            continue
        # One copy into immutable bytes for BinaryContent, and the buffer it
        # came from is let go at once: the body is never held twice.
        data = bytes(part.data)
        part.data = bytearray()
        if not data:
            raise _FormRefused(422, MSG_EMPTY_FILE)
        sniffed = sniff(data)
        if sniffed is None:
            raise _FormRefused(415, MSG_TYPE_NOT_ALLOWED)
        stated = _DECLARED_ALIASES.get(part.content_type, part.content_type)
        if stated not in _UNDECLARED and stated != sniffed:
            raise _FormRefused(415, MSG_TYPE_MISMATCH)
        if sniffed not in limits.media_types:
            raise _FormRefused(415, MSG_TYPE_NOT_ALLOWED)
        if sniffed == PDF:
            pages = await asyncio.to_thread(count_pdf_pages, data)
            if pages == 0:
                raise _FormRefused(422, MSG_PAGES_UNKNOWN)
            if pages > limits.max_pdf_pages:
                raise _FormRefused(422, MSG_TOO_MANY_PAGES)
        attachments.append(Attachment(clean_name(part.filename), sniffed, data))
    return text, attachments


# ---------------------------------------------------------------------------
# submit_result
# ---------------------------------------------------------------------------
@dataclass
class ResultBox:
    """What ``submit_result`` accepted during one run (first valid call)."""

    schema: dict[str, Any] | None
    result: dict[str, Any] | None = None


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **extra}}


RESULT_RETRIES = 5


def result_output(box: ResultBox) -> ToolOutput:
    """The ``submit_result`` output tool of one run, bound to ``box``: a valid
    call ends the run with the result; a refused one is a ``ModelRetry``."""
    schema_text = (
        "\n\nThe result must match this JSON schema:\n" + json.dumps(box.schema)
        if box.schema is not None else ""
    )
    description = (
        "Hand in the structured result of this request. Call it once, when "
        "the work is done, with the result object; a request is only answered "
        "through this tool and the call ends it. If it is refused, correct the "
        "object and call it again." + schema_text
    )

    def submit_result(result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise ModelRetry("The result must be a JSON object.")
        try:
            # allow_nan=False: NaN / Infinity are no JSON, and the poll answer
            # (JSONResponse) would refuse to serialise a stored one.
            size = len(json.dumps(result, allow_nan=False))
        except (TypeError, ValueError):
            raise ModelRetry("The result must be plain JSON (no NaN or Infinity).") from None
        if size > MAX_RESULT_CHARS:
            raise ModelRetry(f"The result is longer than {MAX_RESULT_CHARS} characters.")
        if box.schema is not None:
            problems = validate_result(box.schema, result)
            if problems:
                raise ModelRetry("The result does not match the schema: "
                                 + "; ".join(problems))
        box.result = json.loads(json.dumps(result))
        return box.result

    return ToolOutput(submit_result, name="submit_result", description=description,
                      max_retries=RESULT_RETRIES)


def _final_text(result: Any) -> str | None:
    """The model's text: a text answer, or what it wrote next to the hand-in."""
    if isinstance(result.output, str):
        return result.output
    # The last message is the tool return of the hand-in ("Final result
    # processed."); the response that carried the call is before it.
    for message in reversed(result.all_messages()):
        if isinstance(message, ModelResponse):
            texts = [p.content for p in message.parts
                     if isinstance(p, TextPart) and p.content]
            return "\n\n".join(texts) if texts else None
    return None


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
# Indirection so a test can cancel a run before its first step.
_spawn = asyncio.create_task


@dataclass
class _RunState:
    """Whether the run's outcome was written: if not when the task ends
    (cancelled before its first step, or something escaped), the done
    callback writes ``interrupted`` so the row never stays ``running``."""

    finalized: bool = False


async def cancel_all_invoke_runs() -> None:
    """Cancel every invoke run in flight and wait for each to record itself
    (lifespan shutdown, like ``job_runner.cancel_all_runs``). Repeated until
    nothing is left: a run cancelled before its first step is recorded by a
    task its done callback starts."""
    while True:
        tasks = [t for t in _tasks if not t.done()]
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)


async def _interrupted(run_id: str, state: _RunState) -> None:
    """Record a cancelled run as ``interrupted``, only while its row is still
    ``running``: a cancel can land inside ``_finalize`` after its commit, and
    an unconditional write would then turn a stored success into a cancel.
    A conditional UPDATE without RETURNING (HANA has none); never raises."""
    try:
        activity = final_activity(run_id)
    except Exception:  # noqa: BLE001
        activity = None
    values: dict[str, Any] = {
        "status": "interrupted", "error": CANCELED_ERROR,
        "finished_at": datetime.now(timezone.utc),
    }
    if activity is not None:
        values["activity_json"] = json.dumps(activity)
    try:
        async with SessionLocal() as session:
            await session.execute(
                update(JobRun)
                .where(JobRun.id == run_id, JobRun.status == ACTIVE_RUN_STATUS)
                .values(**values)
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Invoke run %s: recording the cancel failed (%s); the stale "
                       "sweep closes the row", run_id, type(exc).__name__)
    state.finalized = True


def _run_done(run_id: str, key: tuple[str, int], state: _RunState,
              task: asyncio.Task) -> None:
    _tasks.discard(task)
    if _active.get(key) == run_id:
        _active.pop(key, None)
    if not state.finalized:
        closer = asyncio.get_running_loop().create_task(_interrupted(run_id, state))
        _tasks.add(closer)
        closer.add_done_callback(_tasks.discard)


async def _run(run_id: str, key: tuple[str, int], agent_id: int, text: str,
               attachments: list[Attachment], state: _RunState) -> None:
    try:
        with recording(run_id) as activity:
            await _execute(run_id, activity, agent_id, key[0], text, attachments, state)
    finally:
        if _active.get(key) == run_id:
            _active.pop(key, None)
        if not state.finalized:
            await asyncio.shield(_interrupted(run_id, state))


async def _execute(run_id: str, activity: Any, agent_id: int, principal: str, text: str,
                   attachments: list[Attachment], state: _RunState) -> None:
    """Run the specialist and record the outcome; never raises except a
    cancel, which is recorded first (the row must never stay `running`).

    The deadline covers everything from here on, preflight included, and
    the outcome is written after it: a write cut by the deadline would leave
    the row ``running``. The row's own timeout is the deadline plus
    ``ROW_TIMEOUT_MARGIN_SECONDS``, so a stale sweep never takes a live run.
    """
    box = ResultBox(schema=None)
    limit = invoke_timeout()
    try:
        for item in attachments:
            activity.record_attachment(item.name, item.media_type, item.size)
        async with asyncio.timeout(limit) as deadline:
            try:
                outcome = await _attempt(agent_id, principal, text, attachments, box)
            except TimeoutError:
                if deadline.expired():
                    raise
                # A tool's own timeout, not this run's deadline.
                raise RuntimeError("a call inside the run timed out") from None
    except TimeoutError:
        outcome = {"status": "failed",
                   "error": f"timeout: the run exceeded {limit:g}s"}
    except asyncio.CancelledError:
        await asyncio.shield(_interrupted(run_id, state))
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Invoke run %s of agent id %s failed (%s)",
                       run_id, agent_id, type(exc).__name__)
        outcome = {"status": "failed", "error": f"run_failed: {describe_exception(exc)}"}
    if outcome["status"] == "failed" and "report" not in outcome and box.result is not None:
        # The result was handed in before the run failed (the model endpoint
        # refused a later request, the deadline passed): the caller still gets
        # it, with the failure; `writes` are read from the audit log anyway.
        outcome["report"] = {"text": None, "result": box.result}
    await _finalize(run_id, **outcome)
    state.finalized = True


async def _attempt(agent_id: int, principal: str, text: str,
                   attachments: list[Attachment], box: ResultBox) -> dict[str, Any]:
    """The run itself; answers the keyword arguments of the outcome.

    ``box`` is the caller's: a result accepted before a failure stays
    readable there.
    """
    async with SessionLocal() as session:
        agent = await session.get(AgentConfig, agent_id)
    if agent is None:
        return {"status": "failed", "error": "agent_missing: the agent no longer exists"}
    async with SessionLocal() as session:
        blocker = await find_credential_blocker(session, {(principal, agent.name): agent})
    if blocker is not None:
        return {"status": "failed",
                "error": ("credential_required: an MCP server of this agent or of a "
                          "peer has no usable sign-in for the caller")}
    specialist = registry.build.specialists.get(agent.name)
    if specialist is None:
        return {"status": "failed", "error": "agent_not_built: the agent is not built"}
    box.schema = loads_or_none(agent.result_schema_json)
    prompt: list[Any] = [
        *(BinaryContent(data=a.data, media_type=a.media_type) for a in attachments),
        text,
    ]
    result = await specialist.run(
        prompt,
        output_type=[result_output(box), str],
        usage_limits=run_usage_limits(),
        event_stream_handler=_make_progress_handler(agent.name),
    )
    text = _final_text(result)
    report = {"text": text, "result": box.result}
    summary = (text or "")[:SUMMARY_CHARS]
    if box.result is None:
        return {"status": "failed", "summary": summary, "report": report,
                "error": "no_result: the run ended without a valid submit_result"}
    return {"status": "success", "summary": summary, "report": report}


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------
_STATUS = {"running": "running", "success": "completed", "failed": "failed",
           "interrupted": "canceled"}


def _error_code(status: str, error: str | None) -> str | None:
    # Whoever wrote it (this runner, a stale sweep, the startup sweep), an
    # interrupted run is `canceled`.
    if status == "interrupted":
        return "canceled"
    if not error:
        return None
    code = error.split(":", 1)[0].strip()
    return code if code in ERROR_CODES else "run_failed"


async def _writes(run_id: str) -> list[dict[str, Any]]:
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(ODataAuditLog.service, ODataAuditLog.target, ODataAuditLog.operation,
                   ODataAuditLog.created_key_json, ODataAuditLog.outcome)
            .where(ODataAuditLog.run_id == run_id)
            .order_by(ODataAuditLog.id)
        )).all()
    return [
        {"service": r.service, "entity_set": r.target, "operation": r.operation,
         "key": loads_or_none(r.created_key_json), "outcome": r.outcome}
        for r in rows
    ]


async def poll_answer(row: JobRun) -> dict[str, Any]:
    report = row.report if isinstance(row.report, dict) else {}
    status = _STATUS.get(row.status, "failed")
    return {
        "status": status,
        "result": report.get("result") if isinstance(report.get("result"), dict) else None,
        "writes": await _writes(row.id),
        "text": report.get("text") if isinstance(report.get("text"), str) else None,
        "activity": live_activity(row.id) or row.activity,
        "error": _error_code(row.status, row.error) if status != "completed" else None,
    }


def _caller() -> str:
    principal = current_principal.get()
    if not principal:
        # The middleware binds one for every authenticated request; without
        # one nothing could key the run to its owner.
        raise HTTPException(status_code=401, detail="No caller identity")
    return principal


@router.post("/api/invoke/{slug}", dependencies=[Depends(require_invoke)])
async def invoke_agent(
    slug: str,
    request: Request,
    wait: float = Query(0, ge=0, le=MAX_WAIT_SECONDS),
) -> JSONResponse:
    principal = _caller()
    async with SessionLocal() as session:
        agent = await get_agent_by_slug(session, slug)
    if (agent is None or not agent.enabled or not agent.expose_invoke
            or bitbucket_approves(agent.mcp_servers)):
        raise HTTPException(status_code=404, detail=NOT_FOUND)

    # The slot is taken BEFORE the body is read: a refused caller (a repeat
    # of the same user, or over the cap) has not had megabytes of upload read
    # into memory first. An upload in progress holds its slot (None until
    # its run has an id) and counts toward INVOKE_MAX_CONCURRENT.
    key = (principal, agent.id)
    async with _start_lock:
        if key in _active:
            return JSONResponse(status_code=409,
                                content={"detail": MSG_BUSY, "run_id": _active[key]})
        if len(_active) >= max_concurrent():
            raise HTTPException(status_code=429, detail=MSG_CAPACITY)
        _active[key] = None
    started = False
    try:
        try:
            # A deadline on the upload: a slow-drip caller would otherwise
            # hold its reserved slot (and so the instance cap) indefinitely.
            async with asyncio.timeout(upload_timeout()):
                text, attachments = await _read_form(
                    request, parse_limits(agent.invoke_limits_json)
                )
        except _FormRefused as refused:
            raise HTTPException(status_code=refused.status, detail=refused.detail) from None
        except TimeoutError:
            raise HTTPException(status_code=408, detail=MSG_UPLOAD_TIMEOUT) from None
        await _sweep_stale()
        async with SessionLocal() as session:
            row = await create_job_run(
                session, agent=agent, trigger=TRIGGER, created_by=principal,
                timeout_seconds=math.ceil(invoke_timeout()) + ROW_TIMEOUT_MARGIN_SECONDS,
            )
        run_id = row.id
        _active[key] = run_id
        state = _RunState()
        # Created here, inside the request: the task copies the bound JWT and
        # principal, which are the run's identity.
        task = _spawn(_run(run_id, key, agent.id, text, attachments, state))
        started = True
    finally:
        if not started:
            _active.pop(key, None)
    _tasks.add(task)
    task.add_done_callback(functools.partial(_run_done, run_id, key, state))
    logger.info("Started invoke run %s of agent %s (%d attachment(s))",
                run_id, agent.name, len(attachments))

    if wait > 0:
        # asyncio.wait never cancels the run: a caller that gives up waiting
        # (or disconnects) leaves it running to be polled.
        await asyncio.wait({task}, timeout=wait)
        if task.done():
            async with SessionLocal() as session:
                done = await get_job_run(session, run_id)
            if done is not None and done.status != "running":
                return JSONResponse(status_code=200, content=await poll_answer(done))
    return JSONResponse(status_code=202, content={"run_id": run_id})


@router.get("/api/invoke/runs/{run_id}", dependencies=[Depends(require_invoke)])
async def get_invoke_run(run_id: str) -> dict[str, Any]:
    principal = _caller()
    async with SessionLocal() as session:
        row = await get_job_run(session, run_id)
    if row is None or row.trigger != TRIGGER or row.created_by != principal:
        raise HTTPException(status_code=404, detail=RUN_NOT_FOUND)
    return await poll_answer(row)
