"""Trace approvals for diagnose sessions: server-side checks, execution, audit.

Arming an ABAP trace changes a setting in the SAP system as the signed-in
user. The agent can therefore only *propose* it (:func:`request` stores a
pending ``IdeApproval`` and tells the UI); the developer decides, and only
:func:`decide` -- called by the approval route -- ever sends ``trace_start``
or ``trace_cancel`` to ARC-1 (decision D1, spike variant B).

What :func:`decide` checks, on the server, at the moment of the decision:

1. the session belongs to the caller (else 404, as for any foreign resource),
   and the approval belongs to that session (a forged id is 404 too);
2. the session is a ``diagnose`` session (409 ``not_diagnose``);
3. the approval is still pending (409 ``approval_not_pending``) and younger
   than ``store.approval_ttl_min()`` counted from ``created_at`` (410
   ``approval_expired``; the row is marked ``expired``);
4. for an approval: the target's conventions say ``non_production`` *now*
   -- re-read, not remembered from the request (403
   ``target_not_non_production``);
5. for an approval of a cancel: the request id is one a ``trace_start``
   approval of this very session armed (409 ``unknown_trace_request``);
6. for an approval: a user JWT is bound, and on CF the target has a
   destination, so the call can run as that user (424, nothing is decided).

A denial needs only 1-3: saying no changes nothing in SAP and must always
be possible, whatever happened to the target's flag or the stored row.

**Arguments.** They are never model text. :func:`normalize_request` keeps
an allowlist of keys, checks enums and types, clamps ``maxExecutions`` and
``expiresHours`` and drops everything else -- ``traceUser`` and ``user``
included, so ARC-1 traces the connected user, which under principal
propagation is the developer who approved (decision D4). It runs when the
proposal is stored, again on the stored row when it is approved, and a third
time inside ``Arc1Client.arm_trace``. The bounds have hard caps the
environment can lower but not raise.

**Exactly once.** ``store.decide_approval`` moves ``pending`` to
``approved`` in one conditional UPDATE; only the caller that wins it calls
ARC-1. The outcome is recorded afterwards, separately: success keeps
``approved`` and adds ``result`` (``trace_request_id``, ``expires_at``), a
failure moves ``approved`` to ``failed`` with an ``error_code``. A failed or
approved approval is spent; arming again needs a new proposal.

**Disconnects.** The ARC-1 call and the recording run in their own task
with their own DB session and are shielded, so a client that goes away
mid-call cannot leave an armed trace without a result and an audit row.

**Audit.** Two rows per approved action. The *intent* row (``trace_arm`` or
``trace_cancel``, outcome ``approved``) is written before ARC-1 is called
and is a precondition for calling it: if it cannot be written the approval
fails with ``audit_unavailable`` and nothing is sent. The *outcome* row
follows the call (``ok``/``cancelled``, or ``trace_failed``). A process that
dies in between leaves the intent row and an ``approved`` approval without
a result; :func:`mark_interrupted` closes such a row (the sweep calls it).
A denial and an expiry write one row each. Every row names the deciding
principal (the approval row has no ``decided_by``) and comes with one INFO
line on the ``agents.ide.audit`` logger.

Audit rows hold the parameters without the free-text description, plus the
approval id. The description lives only on the approval row -- control
characters removed, e-mail addresses replaced by ``[EMAIL]`` and cut to 60
characters whatever the target -- and goes with the session.

**Timeouts.** When ARC-1 does not answer in time nobody knows whether the
trace was armed. The approval fails with ``arc1_timeout_unknown`` and a note
to check ``trace_requests``; it is never retried here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.db import SessionLocal
from agents.ide import arc1, readonly, store
from agents.ide.diagnose import DiagnoseRun
from agents.ide.models import IdeApproval, IdeSession, iso_utc, utcnow
from agents.ide.schemas import ApprovalAction, ApprovalStatus, coerce_member
from agents.ide.store import approval_ttl_min

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("agents.ide.audit")

# The environment can lower the bounds, never raise them above these.
HARD_MAX_EXECUTIONS = 3
HARD_MAX_HOURS = 8


_env_int = store.env_int

MAX_EXECUTIONS = _env_int("IDE_TRACE_MAX_EXECUTIONS", HARD_MAX_EXECUTIONS)
MAX_HOURS = _env_int("IDE_TRACE_MAX_HOURS", HARD_MAX_HOURS)
# Same variable as ``store.APPROVAL_TTL_MIN``; decisions use
# ``store.approval_ttl_min()``, which never goes below one minute.
APPROVAL_TTL_MIN = store.APPROVAL_TTL_MIN
# Open proposals per session; a model that keeps proposing cannot flood the UI.
MAX_PENDING = 5
# How long the approval path waits for ARC-1 before recording a failure.
# Never below 5 s: a lower value would record every arming as
# ``arc1_timeout_unknown`` although ARC-1 went on to arm the trace.
MIN_ARM_TIMEOUT_S = 5
ARM_TIMEOUT_S = float(
    _env_int("IDE_TRACE_ARM_TIMEOUT_S", 60, minimum=MIN_ARM_TIMEOUT_S)
)

PROCESS_TYPES = ("http", "dialog", "batch", "rfc")
OBJECT_TYPES = ("any", "url", "transaction", "report", "functionModule")
TRACE_KEYS = (
    "processType", "objectType", "maxExecutions", "expiresHours",
    "sqlTrace", "aggregate", "description",
)
DESCRIPTION_MAX = 60
DECISIONS = ("approve", "deny")
AUDIT_OUTCOMES = ("approved", "ok", "denied", "failed", "expired", "cancelled")
TIMEOUT_NOTE = "may have been armed; check trace_requests"

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\s]+")
# E-mail addresses in a description: found from the ``@`` (also URL-encoded,
# ``%40``/``%2540``) and widened to the address, which is linear; a pattern
# that starts at the local part is quadratic on a long token.
_AT_RE = re.compile(r"@|%(?:25){0,8}40")
_DOMAIN_RE = re.compile(r"[\w\-]{1,63}(?:\.[\w\-]{1,63}){1,8}")
_LOCAL_EXTRA = frozenset("._%+-")
_CODE_RE = re.compile(r"[a-z0-9_]{1,64}")


class ApprovalError(Exception):
    """A refused approval request or decision, with a stable ``code``."""

    def __init__(self, code: str, status: int, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code

    def body(self) -> dict[str, Any]:
        return {"detail": self.message, "code": self.code}


def _invalid(message: str) -> ApprovalError:
    return ApprovalError("invalid_request", 422, message)


def _not_found() -> ApprovalError:
    return ApprovalError("not_found", 404, "Not found")


def _limit(configured: Any, hard: int) -> int:
    try:
        value = int(configured)
    except (TypeError, ValueError):
        value = hard
    return max(1, min(value, hard))


def max_executions() -> int:
    return _limit(MAX_EXECUTIONS, HARD_MAX_EXECUTIONS)


def max_hours() -> int:
    return _limit(MAX_HOURS, HARD_MAX_HOURS)


def _enum(args: dict, key: str, allowed: tuple[str, ...], default: str) -> str:
    value = args.get(key, default)
    # Exact string values only: no case folding, no lists, no numbers.
    if not isinstance(value, str) or value not in allowed:
        raise _invalid(f"{key} must be one of {', '.join(allowed)}")
    return value


def _clamped(args: dict, key: str, upper: int) -> int:
    value = args.get(key, 1)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(f"{key} must be a whole number")
    return max(1, min(value, upper))


def _flag(args: dict, key: str) -> bool:
    value = args.get(key, False)
    if not isinstance(value, bool):
        raise _invalid(f"{key} must be true or false")
    return value


def _redact_emails(text: str) -> str:
    """``text`` with every e-mail address replaced by ``[EMAIL]``."""
    out: list[str] = []
    done = 0
    for m in _AT_RE.finditer(text):
        if m.start() < done:
            continue
        domain = _DOMAIN_RE.match(text, m.end())
        if domain is None:
            continue
        start = m.start()
        while start > done and (
            text[start - 1].isalnum() or text[start - 1] in _LOCAL_EXTRA
        ):
            start -= 1
        if start == m.start():
            continue
        out.append(text[done:start])
        out.append("[EMAIL]")
        done = domain.end()
    if not out:
        return text
    out.append(text[done:])
    return "".join(out)


def _description(args: dict) -> str:
    """The proposal's free text, as it may be shown and stored.

    Control characters go, and e-mail addresses are replaced whatever the
    target (decision D6 of the IDE redesign): the description is kept with
    the approval and the developer sees it in the decision card, and an
    address there is never needed to arm a trace. Redacted before the cut,
    so a cut cannot leave half an address behind.
    """
    value = args.get("description", "")
    if not isinstance(value, str):
        raise _invalid("description must be text")
    text = _CONTROL_RE.sub(" ", _redact_emails(value)).strip()
    return text[:DESCRIPTION_MAX].strip()


def normalize_request(action: str, args: Any) -> dict[str, Any]:
    """The validated parameters of a trace proposal; the only shape that is
    ever stored, shown, audited or sent.

    ``trace_start`` keeps the seven :data:`TRACE_KEYS` and nothing else;
    ``trace_cancel`` keeps ``id``. Raises ``ApprovalError`` (422
    ``invalid_request``) for an unknown action, a non-dict, a wrong enum or
    type, or a bad id.
    """
    if not isinstance(action, str) or action not in store.APPROVAL_ACTIONS:
        raise _invalid("Unknown approval action")
    if not isinstance(args, dict):
        raise _invalid("Arguments must be an object")
    if action == "trace_cancel":
        rid = args.get("id")
        if not isinstance(rid, str) or not arc1.TRACE_ID_RE.fullmatch(rid):
            raise _invalid("id must be 1-100 characters of A-Z a-z 0-9 _ . : -")
        return {"id": rid}
    return {
        "processType": _enum(args, "processType", PROCESS_TYPES, "http"),
        "objectType": _enum(args, "objectType", OBJECT_TYPES, "any"),
        "maxExecutions": _clamped(args, "maxExecutions", max_executions()),
        "expiresHours": _clamped(args, "expiresHours", max_hours()),
        "sqlTrace": _flag(args, "sqlTrace"),
        "aggregate": _flag(args, "aggregate"),
        "description": _description(args),
    }


# --- serialisation ---------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return iso_utc(value)


def _loads(text: str | None) -> Any:
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def approval_json(row: IdeApproval) -> dict[str, Any]:
    """The ``Approval`` of plan 1c §1.2.

    ``ttl_min`` is how long a pending approval can be decided, counted from
    ``created_at``: the value ``decide`` itself checks, so the card's
    countdown and the 410 ``approval_expired`` cannot disagree.
    """
    params = _loads(row.params_json)
    result = _loads(row.result_json)
    return {
        "id": row.id,
        # Values outside the contract are coerced (WARNING), never a 500:
        # ``failed`` offers no decision, ``trace_cancel`` arms nothing.
        "action": coerce_member(
            row.action, ApprovalAction, "trace_cancel", "approval action"
        ),
        "params": params if isinstance(params, dict) else {},
        "status": coerce_member(
            row.status, ApprovalStatus, "failed", "approval status"
        ),
        "created_at": _iso(row.created_at),
        "ttl_min": approval_ttl_min(),
        "decided_at": _iso(row.decided_at),
        "result": result if isinstance(result, dict) else None,
        "error_code": row.error_code,
    }


# --- small helpers -----------------------------------------------------------


def _aware(value: datetime) -> datetime:
    # SQLite hands back naive datetimes; they were written as UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _is_expired(row: IdeApproval) -> bool:
    cutoff = utcnow() - timedelta(minutes=approval_ttl_min())
    return row.created_at is None or _aware(row.created_at) < cutoff


async def _check_cancel_target(db: AsyncSession, sid: str, params: dict) -> None:
    """A cancel can only name a trace this session armed -- never an id the
    model made up or read somewhere, which could be someone else's trace."""
    if params.get("id") not in await store.armed_trace_ids(db, sid):
        raise ApprovalError(
            "unknown_trace_request", 409,
            "This trace request was not armed in this session",
        )


# --- audit -----------------------------------------------------------------


def _audit_params(params: dict[str, Any], aid: str) -> dict[str, Any]:
    """What an audit row keeps of an approval: no free text, plus its id."""
    return {
        **{k: v for k, v in params.items() if k != "description"},
        "approval_id": aid,
    }


def _log_audit(
    level: int, principal: str, sid: str, target: str, action: str,
    outcome: str, request_id: str | None, params: dict[str, Any],
) -> None:
    shown = {k: v for k, v in params.items() if k != "description"}
    audit_logger.log(
        level,
        "principal=%s session=%s target=%s action=%s outcome=%s request_id=%s params=%s",
        principal, sid, target, action, outcome, request_id or "-",
        json.dumps(shown, sort_keys=True),
    )


async def _audit(
    db: AsyncSession, *, principal: str, sid: str, target: str, action: str,
    params: dict[str, Any], outcome: str, request_id: str | None = None,
    strict: bool = False,
) -> None:
    """One audit row plus one log line.

    ``params`` are :func:`_audit_params`. Unless ``strict``, it never
    raises: by then the decision (and possibly the ARC-1 call) has happened
    and must still be answered; the log line is written first so a failing
    insert leaves a trace. ``strict`` is for the intent row, whose absence
    must stop the call.
    """
    if outcome not in AUDIT_OUTCOMES:
        raise ValueError(f"Unknown audit outcome: {outcome!r}")
    _log_audit(logging.INFO, principal, sid, target, action, outcome, request_id, params)
    try:
        await store.add_audit(
            db, principal=principal, session_id=sid, target=target, action=action,
            params=params, outcome=outcome, request_id=request_id,
        )
    except Exception:  # noqa: BLE001
        if strict:
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise
        logger.error(
            "[ide] audit row for %s (%s) in session %s could not be written",
            action, outcome, sid, exc_info=True,
        )
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass


# --- request (the agent proposes) ------------------------------------------


async def request(
    db: AsyncSession,
    run: DiagnoseRun,
    action: str,
    args: Any,
    tool_call_id: str | None,
) -> IdeApproval:
    """Store a pending approval for the run's session and tell the UI.

    Nothing reaches ARC-1 here. The stored parameters are the normalised
    ones; whatever else the model put in ``args`` is gone.

    The same proposal twice in one run (same action, same normalised
    parameters) is one approval: while the first is still pending it is
    returned again, without a second row or event. A proposal that was
    decided or has expired meanwhile does not count -- asking again is new.
    """
    params = normalize_request(action, args)
    async with run.proposal_lock:
        existing = await _same_pending(db, run, action, params)
        if existing is not None:
            return existing
        return await _store_request(db, run, action, params, tool_call_id)


async def _same_pending(
    db: AsyncSession, run: DiagnoseRun, action: str, params: dict[str, Any]
) -> IdeApproval | None:
    for known in run.approvals:
        if known.get("action") != action or known.get("params") != params:
            continue
        row = await store.get_approval(db, run.session_id, str(known.get("id") or ""))
        if row is not None and row.status == "pending" and not _is_expired(row):
            return row
    return None


async def _store_request(
    db: AsyncSession,
    run: DiagnoseRun,
    action: str,
    params: dict[str, Any],
    tool_call_id: str | None,
) -> IdeApproval:
    # The run says it is a diagnose run; the session row is what counts.
    session_type = (
        await db.execute(
            select(IdeSession.session_type).where(IdeSession.id == run.session_id)
        )
    ).scalar_one_or_none()
    if session_type is None:
        raise _not_found()
    if session_type != "diagnose":
        raise ApprovalError(
            "not_diagnose", 409, "Approvals exist only in diagnose sessions"
        )
    if action == "trace_cancel":
        await _check_cancel_target(db, run.session_id, params)
    if not await store.target_is_non_production(db, run.target):
        raise ApprovalError(
            "target_not_non_production", 403,
            "Traces can only be armed on a target flagged as non-production",
        )
    if await store.count_pending_approvals(db, run.session_id) >= MAX_PENDING:
        raise ApprovalError(
            "too_many_pending", 409,
            "Too many approvals are waiting for the developer's decision",
        )
    row = await store.add_approval(
        db, run.session_id, run_id=run.run_id, tool_call_id=tool_call_id,
        action=action, params=params,
    )
    data = approval_json(row)
    run.approvals.append(data)
    if run.emit is not None:
        try:
            run.emit("approval_required", data)
        except Exception:  # noqa: BLE001 -- a closed stream must not undo the proposal
            logger.warning("[ide] approval_required could not be emitted", exc_info=True)
    return row


# --- decide (the developer approves or denies) -----------------------------

# Execution tasks in flight; kept so they are not garbage-collected and so
# shutdown (and tests) can wait for them.
_TASKS: set[asyncio.Task] = set()


async def drain() -> None:
    """Wait for ARC-1 calls of approvals that are still being executed."""
    while _TASKS:
        await asyncio.gather(*list(_TASKS), return_exceptions=True)


def _error_code(exc: BaseException) -> tuple[str, str | None]:
    """A stable code (and ARC-1's request id) for a failed call. Exception
    text can carry URLs and tokens, so it only ever goes to the log."""
    if isinstance(exc, arc1.Arc1Error):
        code = exc.code if isinstance(exc.code, str) else ""
        rid = exc.extra.get("request_id") if isinstance(exc.extra, dict) else None
        rid = rid if isinstance(rid, str) and arc1.TRACE_ID_RE.fullmatch(rid) else None
        return (code if _CODE_RE.fullmatch(code) else "arc1_error"), rid
    return "arc1_error", None


def _result(action: str, params: dict[str, Any], raw: Any) -> dict[str, Any]:
    """What is kept of ARC-1's answer: a well-formed request id and an expiry.
    Nothing else of the payload is stored."""
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    if action == "trace_cancel":
        return {"trace_request_id": params["id"]}
    rid = raw.get("trace_request_id")
    if isinstance(rid, str) and arc1.TRACE_ID_RE.fullmatch(rid):
        out["trace_request_id"] = rid
    expires = raw.get("expires_at")
    if isinstance(expires, str) and arc1.TRACE_EXPIRES_RE.fullmatch(expires):
        out["expires_at"] = expires
    else:
        # ARC-1 named none: the latest moment the approved request can live.
        out["expires_at"] = (
            utcnow() + timedelta(hours=params["expiresHours"])
        ).isoformat(timespec="seconds")
    return out


async def _run_approved(
    *, sid: str, aid: str, principal: str, target: str, destination: str,
    action: str, params: dict[str, Any],
) -> None:
    """Call ARC-1 for an approval that was just moved to ``approved`` and
    record the outcome. Own DB session: the request's may be gone by then."""
    code: str | None = None
    request_id: str | None = None
    result: dict[str, Any] | None = None
    scope = asyncio.timeout(ARM_TIMEOUT_S)
    try:
        client = arc1.get_arc1_client(target, destination, policy=readonly.DIAGNOSE)
        async with scope:
            if action == "trace_start":
                raw = await client.arm_trace(dict(params))
            else:
                raw = await client.cancel_trace(params["id"])
        result = _result(action, params, raw)
        request_id = result.get("trace_request_id")
    except asyncio.CancelledError:
        # Shutdown mid-call: whether the trace was armed is unknown.
        async with SessionLocal() as db:
            await asyncio.shield(_finish(
                db, sid=sid, aid=aid, principal=principal, target=target,
                action=action, params=params, code="interrupted",
                request_id=None, result=None,
            ))
        raise
    except TimeoutError as exc:
        # Only our own deadline is a timeout; a tool's is just a failure.
        if scope.expired():
            # ARC-1 may have armed it before we stopped waiting.
            code, result = "arc1_timeout_unknown", {"note": TIMEOUT_NOTE}
        else:
            code, request_id = _error_code(exc)
    except Exception as exc:  # noqa: BLE001 -- recorded and audited, never raised
        code, request_id = _error_code(exc)
        logger.warning(
            "[ide] %s for session %s on %s failed (%s)",
            action, sid, target, code, exc_info=True,
        )
    async with SessionLocal() as db:
        await _finish(
            db, sid=sid, aid=aid, principal=principal, target=target,
            action=action, params=params, code=code, request_id=request_id,
            result=result,
        )


async def _finish(
    db: AsyncSession, *, sid: str, aid: str, principal: str, target: str,
    action: str, params: dict[str, Any], code: str | None,
    request_id: str | None, result: dict[str, Any] | None,
) -> None:
    try:
        await store.record_approval_outcome(db, sid, aid, result=result, error_code=code)
    except Exception:  # noqa: BLE001 -- the audit row below must still be written
        logger.error(
            "[ide] outcome of approval %s could not be recorded", aid, exc_info=True
        )
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
    shown = _audit_params(params, aid)
    if code:
        await _audit(db, principal=principal, sid=sid, target=target,
                     action="trace_failed", params=shown, outcome="failed",
                     request_id=request_id)
    elif action == "trace_start":
        await _audit(db, principal=principal, sid=sid, target=target,
                     action="trace_arm", params=shown, outcome="ok",
                     request_id=request_id)
    else:
        await _audit(db, principal=principal, sid=sid, target=target,
                     action="trace_cancel", params=shown, outcome="cancelled",
                     request_id=request_id)


async def mark_interrupted(db: AsyncSession, approval: IdeApproval) -> bool:
    """Close an approval that was approved but never got an outcome.

    That is what a process dying during the ARC-1 call leaves behind: the
    intent audit row and ``approved`` without a result. Whether the trace
    was armed is unknown, so the row becomes ``failed``/``interrupted`` with
    the same note as a timeout, and a ``trace_failed`` audit row names the
    session's owner (the only one who can have approved). True if this call
    closed it; an approval that is pending, finished or being recorded by
    someone else is left alone. The caller decides how old "never" is.
    """
    sid, aid, action = approval.session_id, approval.id, approval.action
    if approval.status != "approved" or approval.result_json is not None:
        return False
    stored = _loads(approval.params_json)
    if not await store.record_approval_outcome(
        db, sid, aid, result={"note": TIMEOUT_NOTE}, error_code="interrupted"
    ):
        return False
    session = await db.get(IdeSession, sid)
    await _audit(
        db,
        principal=(session.owner if session is not None else "") or "unknown",
        sid=sid,
        target=session.target if session is not None else "",
        action="trace_failed",
        params=_audit_params(_safe_params(action, stored), aid),
        outcome="failed",
    )
    return True


# Past the arm timeout plus this margin an approved approval without an
# outcome has no call in flight any more (the call itself is cut off at
# ``ARM_TIMEOUT_S`` and recorded right after).
INTERRUPT_MARGIN_S = 60.0


async def sweep_interrupted(db: AsyncSession, sid: str | None = None) -> int:
    """Close every approval that was approved but never got an outcome.

    Part of the expiry pass (``app._purge_ide_sessions``), which runs at
    startup and once a day; with ``sid`` it covers one session only, which
    is what the owner's approval list does so a crash is visible there
    without waiting for the next pass. Only rows decided
    longer ago than ``ARM_TIMEOUT_S + INTERRUPT_MARGIN_S`` are touched, so a
    call that is still running in this or another instance is left to
    record its own outcome; :func:`mark_interrupted` does not judge age.
    One row that cannot be closed does not stop the others. Returns the
    number of rows closed.
    """
    rows = await store.list_unfinished_approvals(
        db, older_than_s=ARM_TIMEOUT_S + INTERRUPT_MARGIN_S, session_id=sid
    )
    # Ids first: a rollback below expires every loaded row.
    keys = [(row.session_id, row.id) for row in rows]
    closed = 0
    for sid, aid in keys:
        try:
            row = await store.get_approval(db, sid, aid)
            if row is not None and await mark_interrupted(db, row):
                closed += 1
        except Exception:  # noqa: BLE001 -- the next row still gets its turn
            logger.warning(
                "[ide] approval %s could not be closed as interrupted", aid,
                exc_info=True,
            )
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
    return closed


async def _lost(db: AsyncSession, sid: str, aid: str) -> ApprovalError:
    """Why the conditional UPDATE did not win: decided meanwhile, or expired."""
    row = await store.get_approval(db, sid, aid)
    if row is None:
        return _not_found()
    if row.status == "expired" or (row.status == "pending" and _is_expired(row)):
        return ApprovalError("approval_expired", 410, "This approval has expired")
    return ApprovalError(
        "approval_not_pending", 409, "This approval was already decided"
    )


async def decide(
    db: AsyncSession, *, sid: str, aid: str, principal: str, decision: str
) -> IdeApproval:
    """Approve or deny one pending approval as ``principal``.

    Returns the approval as it is afterwards: ``denied``; ``approved`` with
    a ``result`` when ARC-1 armed or cancelled the trace; ``failed`` with an
    ``error_code`` when the user approved and ARC-1 did not do it. Raises
    ``ApprovalError`` for every refusal in the module docstring and
    ``Arc1UserRequired``/``Arc1NotConfigured`` (424) when the call could not
    run as the user -- in which case nothing was decided.
    """
    # 1. Owner. Another user's session and a forged approval id look the same.
    session = await store.get_owned_session(db, sid, principal)
    if session is None:
        raise _not_found()
    target, session_type = session.target, session.session_type
    row = await store.get_approval(db, sid, aid)
    if row is None:
        raise _not_found()
    if not isinstance(decision, str) or decision not in DECISIONS:
        raise _invalid("decision must be approve or deny")
    # 2. Session type.
    if session_type != "diagnose":
        raise ApprovalError(
            "not_diagnose", 409, "Approvals exist only in diagnose sessions"
        )
    # 3. Pending and not expired (the UPDATE below checks both again).
    action = row.action
    if row.status == "pending" and _is_expired(row):
        stored = _loads(row.params_json)
        if await store.expire_approval(db, sid, aid):
            await _audit(
                db, principal=principal, sid=sid, target=target,
                action="trace_failed", outcome="expired",
                params=_audit_params(_safe_params(action, stored), aid),
            )
        raise await _lost(db, sid, aid)
    if row.status != "pending":
        raise await _lost(db, sid, aid)
    ttl = approval_ttl_min()
    # Deny: always possible for the owner of a pending approval. It sends
    # nothing, so neither the target's flag nor the stored params can block it.
    if decision == "deny":
        shown = _audit_params(_safe_params(action, _loads(row.params_json)), aid)
        decided = await store.decide_approval(
            db, sid, aid, status="denied", max_age_min=ttl
        )
        if decided is None:
            raise await _lost(db, sid, aid)
        await _audit(db, principal=principal, sid=sid, target=target,
                     action="trace_deny", params=shown, outcome="denied")
        return decided
    # Arguments: rebuilt from the stored row through the same gate as the
    # proposal, so nothing but validated TraceParams can leave this function.
    params = normalize_request(action, _loads(row.params_json))
    # 4. The target's flag, re-read now.
    conventions = await store.get_conventions(db, target)
    destination = (conventions.destination if conventions is not None else "") or ""
    if not await store.target_is_non_production(db, target):
        logger.warning(
            "[ide] approval %s refused: target %s is not flagged non-production",
            aid, target,
        )
        raise ApprovalError(
            "target_not_non_production", 403,
            "Traces can only be armed on a target flagged as non-production",
        )
    # 5. A cancel only for a trace this session armed.
    if action == "trace_cancel":
        await _check_cancel_target(db, sid, params)
    # 6. Refuse a call that could not run as the user *before* the approval
    # is spent, so signing in again is enough to retry.
    arc1.require_user_context(destination)
    decided = await store.decide_approval(
        db, sid, aid, status="approved", max_age_min=ttl
    )
    if decided is None:
        raise await _lost(db, sid, aid)
    # The intent row, durable before anything is sent: who approved what.
    # Without it there is no call (fail closed).
    shown = _audit_params(params, aid)
    try:
        await _audit(
            db, principal=principal, sid=sid, target=target,
            action="trace_arm" if action == "trace_start" else "trace_cancel",
            params=shown, outcome="approved", strict=True,
        )
    except Exception:  # noqa: BLE001
        logger.error(
            "[ide] approval %s: the intent audit row could not be written; "
            "nothing was sent to ARC-1", aid, exc_info=True,
        )
        await store.record_approval_outcome(db, sid, aid, error_code="audit_unavailable")
        await _audit(db, principal=principal, sid=sid, target=target,
                     action="trace_failed", params=shown, outcome="failed")
        return await store.get_approval(db, sid, aid) or decided
    # Own task, created here so it copies this request's context (the JWT).
    task = asyncio.create_task(_run_approved(
        sid=sid, aid=aid, principal=principal, target=target,
        destination=destination, action=action, params=params,
    ))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    await asyncio.shield(task)
    return await store.get_approval(db, sid, aid) or decided


def _safe_params(action: str, stored: Any) -> dict[str, Any]:
    """Normalised params for an audit row, or nothing if they do not pass."""
    try:
        return normalize_request(action, stored)
    except ApprovalError:
        return {}
