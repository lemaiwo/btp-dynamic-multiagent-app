"""Trace approvals (plan 1c Task 11): server-side checks, execution, audit.

Arming a trace changes a setting in the SAP system as the signed-in user, so
everything that decides whether it happens is checked on the server at the
moment of the decision: the session belongs to the caller, it is a diagnose
session, the approval is pending and not expired, and the target is flagged
``non_production`` *now*. The arguments sent to ARC-1 are rebuilt from the
validated parameters, never taken from model text, and never name a user.

Run:  python -m pytest tests/test_ide_approvals.py -q
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from sqlalchemy import select, update  # noqa: E402

from agents.auth import current_jwt  # noqa: E402
from agents.db import SessionLocal, init_db  # noqa: E402
from agents.ide import approvals, arc1, readonly  # noqa: E402
from agents.ide.approvals import ApprovalError, normalize_request  # noqa: E402
from agents.ide.diagnose import DiagnoseRun  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeApproval,
    IdeAuditLog,
    IdeConventions,
    IdeSession,
    utcnow,
)
from agents.ide.store import (  # noqa: E402
    add_approval,
    create_session,
    get_approval,
    upsert_conventions,
)

DEST = "arc1-abap-readonly"
TARGET = "DEMO"
OWNER = "jane.doe@example.com"
TRACE = {
    "processType": "http",
    "objectType": "url",
    "maxExecutions": 2,
    "expiresHours": 4,
    "sqlTrace": True,
    "aggregate": False,
    "description": "slow list report",
}


# --- normalize_request -----------------------------------------------------------


def test_normalize_drops_trace_user_and_unknown_keys():
    out = normalize_request("trace_start", {
        **TRACE,
        "traceUser": "DEVUSER01",
        "user": "DEVUSER01",
        "action": "set_sql_trace_state",
        "server": "other",
        "objectName": "/sap/opu/odata",
    })
    assert out == TRACE
    assert set(out) == set(approvals.TRACE_KEYS)


def test_normalize_defaults():
    assert normalize_request("trace_start", {}) == {
        "processType": "http", "objectType": "any", "maxExecutions": 1,
        "expiresHours": 1, "sqlTrace": False, "aggregate": False,
        "description": "",
    }


def test_normalize_clamps_bounds():
    out = normalize_request("trace_start", {"maxExecutions": 99, "expiresHours": 0})
    assert out["maxExecutions"] == approvals.MAX_EXECUTIONS == 3
    assert out["expiresHours"] == 1
    out = normalize_request("trace_start", {"maxExecutions": -4, "expiresHours": 9000})
    assert out["maxExecutions"] == 1
    assert out["expiresHours"] == approvals.MAX_HOURS == 8


def test_bounds_cannot_be_configured_above_the_hard_caps(monkeypatch):
    monkeypatch.setattr(approvals, "MAX_EXECUTIONS", 500)
    monkeypatch.setattr(approvals, "MAX_HOURS", 500)
    out = normalize_request("trace_start", {"maxExecutions": 99, "expiresHours": 99})
    assert out["maxExecutions"] == 3
    assert out["expiresHours"] == 8


def test_normalize_redacts_emails_and_cuts_description():
    out = normalize_request("trace_start", {
        "description": "for jane.doe@example.com " + "x" * 200,
    })
    assert "jane.doe@example.com" not in out["description"]
    assert len(out["description"]) <= 60
    out = normalize_request("trace_start", {"description": "a\nb\x00c\td"})
    assert out["description"] == "a b c d"


def test_normalize_is_idempotent():
    once = normalize_request("trace_start", {
        **TRACE, "description": "User: DEVUSER01 mail jane.doe@example.com",
    })
    # Only e-mail addresses are redacted; user names stay (masking is gone).
    assert once["description"] == "User: DEVUSER01 mail [EMAIL]"
    assert normalize_request("trace_start", once) == once


@pytest.mark.parametrize("args", [
    {"processType": "shell"},
    {"objectType": "table"},
    {"processType": ["http"]},
    {"processType": "HTTP"},
    {"maxExecutions": "many"},
    {"maxExecutions": True},
    {"expiresHours": 1.5},
    {"sqlTrace": "yes"},
    {"aggregate": 1},
    {"description": {"x": 1}},
    "not a dict",
    None,
])
def test_normalize_rejects_bad_trace_values(args):
    with pytest.raises(ApprovalError) as e:
        normalize_request("trace_start", args)
    assert (e.value.code, e.value.status) == ("invalid_request", 422)


@pytest.mark.parametrize("args", [
    {}, {"id": ""}, {"id": 12}, {"id": "a b"}, {"id": "x" * 101},
    {"id": "abc;drop"}, {"id": "../x"}, {"id": "ok\n"}, {"id": ["A1"]},
])
def test_normalize_rejects_bad_enum_and_cancel_id(args):
    with pytest.raises(ApprovalError) as e:
        normalize_request("trace_cancel", args)
    assert (e.value.code, e.value.status) == ("invalid_request", 422)
    with pytest.raises(ApprovalError):
        normalize_request("trace_start", {"processType": "nope"})
    with pytest.raises(ApprovalError):
        normalize_request("set_sql_trace_state", {})


def test_normalize_cancel_keeps_only_the_id():
    assert normalize_request(
        "trace_cancel", {"id": "REQ_1.a:b-2", "traceUser": "DEVUSER01"}
    ) == {"id": "REQ_1.a:b-2"}


# --- harness -------------------------------------------------------------------------


class FakeArc1:
    """Stands in for ``Arc1Client``; records what the approval path sends."""

    def __init__(self):
        self.built: list[tuple] = []
        self.armed: list[dict] = []
        self.cancelled: list[str] = []
        self.exc: BaseException | None = None
        self.result = {"trace_request_id": "REQ-1", "expires_at": "2026-10-03T18:00:00Z"}

    def factory(self, target, destination="", policy=readonly.CHANGE):
        self.built.append((target, destination, policy))
        return self

    async def arm_trace(self, params):
        self.armed.append(params)
        if self.exc is not None:
            raise self.exc
        return dict(self.result)

    async def cancel_trace(self, request_id):
        self.cancelled.append(request_id)
        if self.exc is not None:
            raise self.exc
        return {"trace_request_id": request_id}


@pytest.fixture(autouse=True)
async def _clean_db():
    await init_db()
    async with SessionLocal() as db:
        for model in (IdeApproval, IdeAuditLog, IdeSession, IdeConventions):
            await db.execute(model.__table__.delete())
        await db.commit()
        await upsert_conventions(db, TARGET, label="Demo", destination=DEST,
                                 actor="test-admin", non_production=True)
        # The setup's own flag audit row is not what these tests count.
        await db.execute(IdeAuditLog.__table__.delete())
        await db.commit()
    arc1._SERVERS.clear()
    yield
    arc1._SERVERS.clear()


@pytest.fixture
def fake(monkeypatch):
    f = FakeArc1()
    monkeypatch.setattr(arc1, "get_arc1_client", f.factory)
    return f


@pytest.fixture
def jwt():
    token = current_jwt.set("user.jwt.token")
    yield
    current_jwt.reset(token)


async def _session(owner=OWNER, session_type="diagnose", target=TARGET) -> str:
    async with SessionLocal() as db:
        row = await create_session(db, owner=owner, title="S", target=target,
                                   session_type=session_type)
        return row.id


async def _pending(sid: str, action="trace_start", params=None) -> str:
    async with SessionLocal() as db:
        row = await add_approval(
            db, sid, run_id="r1", tool_call_id="c1", action=action,
            params=normalize_request(action, TRACE if params is None else params),
        )
        return row.id


async def _armed(sid: str, rid: str = "REQ-1") -> str:
    """An approval of this session that armed trace request ``rid``."""
    async with SessionLocal() as db:
        row = IdeApproval(
            session_id=sid, action="trace_start", status="approved",
            params_json=json.dumps(TRACE, sort_keys=True),
            result_json=json.dumps({"trace_request_id": rid}),
        )
        db.add(row)
        await db.commit()
        return row.id


def _audit_params(aid: str, params=None) -> dict:
    """What an audit row keeps: the params without the free text, plus the
    approval they belong to."""
    params = TRACE if params is None else params
    return {**{k: v for k, v in params.items() if k != "description"},
            "approval_id": aid}


async def _decide(sid, aid, principal=OWNER, decision="approve"):
    async with SessionLocal() as db:
        row = await approvals.decide(
            db, sid=sid, aid=aid, principal=principal, decision=decision
        )
        return approvals.approval_json(row)


async def _audit() -> list[IdeAuditLog]:
    async with SessionLocal() as db:
        rows = await db.execute(select(IdeAuditLog).order_by(IdeAuditLog.ts))
        return list(rows.scalars().all())


async def _status(sid, aid) -> str:
    async with SessionLocal() as db:
        return (await get_approval(db, sid, aid)).status


async def _refused(sid, aid, code, status, **kw):
    with pytest.raises(ApprovalError) as e:
        await _decide(sid, aid, **kw)
    assert (e.value.code, e.value.status) == (code, status)


# --- decide ---------------------------------------------------------------------------


async def test_approve_arms_once_and_audits(fake, jwt, caplog):
    sid = await _session()
    aid = await _pending(sid, params={**TRACE, "traceUser": "DEVUSER01"})
    with caplog.at_level(logging.INFO, logger="agents.ide.audit"):
        out = await _decide(sid, aid)

    assert fake.built == [(TARGET, DEST, readonly.DIAGNOSE)]
    assert fake.armed == [TRACE]
    assert "traceUser" not in fake.armed[0] and "user" not in fake.armed[0]
    assert out["status"] == "approved"
    assert out["action"] == "trace_start"
    assert out["params"] == TRACE
    assert out["result"] == {
        "trace_request_id": "REQ-1", "expires_at": "2026-10-03T18:00:00Z",
    }
    assert out["error_code"] is None and out["decided_at"]

    rows = await _audit()
    assert [(r.action, r.outcome, r.principal, r.session_id, r.target, r.request_id)
            for r in rows] == [
        ("trace_arm", "approved", OWNER, sid, TARGET, None),
        ("trace_arm", "ok", OWNER, sid, TARGET, "REQ-1"),
    ]
    for r in rows:
        assert json.loads(r.params_json) == _audit_params(aid)
        assert "slow list report" not in r.params_json
    lines = [r.getMessage() for r in caplog.records if r.name == "agents.ide.audit"]
    assert any("trace_arm" in m and "ok" in m and OWNER in m and "REQ-1" in m
               for m in lines)
    assert all("slow list report" not in m for m in lines)

    # The approval is spent: a second click cannot arm again.
    await _refused(sid, aid, "approval_not_pending", 409)
    await _refused(sid, aid, "approval_not_pending", 409, decision="deny")
    assert len(fake.armed) == 1
    assert len(await _audit()) == 2


async def test_concurrent_approvals_arm_once(fake, jwt):
    sid = await _session()
    aid = await _pending(sid)
    results = await asyncio.gather(
        *[_decide(sid, aid) for _ in range(4)], return_exceptions=True
    )
    assert len(fake.armed) == 1
    assert sum(1 for r in results if isinstance(r, dict)) == 1
    assert all(
        isinstance(r, dict) or (isinstance(r, ApprovalError) and r.status == 409)
        for r in results
    )
    assert [(r.action, r.outcome) for r in await _audit()] == [
        ("trace_arm", "approved"), ("trace_arm", "ok"),
    ]


async def test_args_come_from_the_stored_row_and_are_renormalised(fake, jwt):
    """Whatever ended up in ``params_json``, the approval path validates it
    again: a tampered row cannot name a user or exceed the bounds."""
    sid = await _session()
    aid = await _pending(sid)
    async with SessionLocal() as db:
        await db.execute(update(IdeApproval).where(IdeApproval.id == aid).values(
            params_json=json.dumps({**TRACE, "traceUser": "DEVUSER01",
                                    "maxExecutions": 50, "expiresHours": 99})
        ))
        await db.commit()
    await _decide(sid, aid)
    assert fake.armed == [{**TRACE, "maxExecutions": 3, "expiresHours": 8}]
    for r in await _audit():
        assert json.loads(r.params_json) == _audit_params(aid, fake.armed[0])


async def test_invalid_stored_params_fail_closed(fake, jwt):
    sid = await _session()
    aid = await _pending(sid)
    async with SessionLocal() as db:
        await db.execute(update(IdeApproval).where(IdeApproval.id == aid).values(
            params_json=json.dumps({"processType": "shell"})
        ))
        await db.commit()
    await _refused(sid, aid, "invalid_request", 422)
    assert fake.armed == []
    assert await _status(sid, aid) == "pending"


async def test_approve_cancel_runs_cancel_and_audits(fake, jwt):
    sid = await _session()
    await _armed(sid, "REQ-1")
    aid = await _pending(sid, action="trace_cancel", params={"id": "REQ-1"})
    out = await _decide(sid, aid)
    assert fake.cancelled == ["REQ-1"] and fake.armed == []
    assert out["status"] == "approved"
    assert out["result"] == {"trace_request_id": "REQ-1"}
    rows = await _audit()
    assert [(r.action, r.outcome, r.request_id) for r in rows] == [
        ("trace_cancel", "approved", None),
        ("trace_cancel", "cancelled", "REQ-1"),
    ]
    assert json.loads(rows[0].params_json) == {"id": "REQ-1", "approval_id": aid}


async def test_cancel_only_for_a_trace_this_session_armed(fake, jwt):
    sid = await _session()
    other = await _session()
    await _armed(other, "REQ-OTHER")
    # A proposal for an id this session never armed is refused ...
    async with SessionLocal() as db:
        for rid in ("REQ-OTHER", "REQ-UNKNOWN"):
            with pytest.raises(ApprovalError) as e:
                await approvals.request(db, _run(sid), "trace_cancel", {"id": rid}, None)
            assert (e.value.code, e.value.status) == ("unknown_trace_request", 409)
    # ... and so is the approval of a row that got in some other way.
    aid = await _pending(sid, action="trace_cancel", params={"id": "REQ-OTHER"})
    await _refused(sid, aid, "unknown_trace_request", 409)
    assert fake.cancelled == [] and fake.built == [] and await _audit() == []
    # A failed or denied arm request armed nothing.
    async with SessionLocal() as db:
        db.add(IdeApproval(session_id=sid, action="trace_start", status="failed",
                           params_json="{}",
                           result_json=json.dumps({"trace_request_id": "REQ-F"})))
        await db.commit()
    aid = await _pending(sid, action="trace_cancel", params={"id": "REQ-F"})
    await _refused(sid, aid, "unknown_trace_request", 409)
    assert fake.cancelled == []


async def test_forged_approval_id_404(fake, jwt):
    mine = await _session()
    other = await _session()
    aid = await _pending(other)
    await _refused(mine, aid, "not_found", 404)
    await _refused(mine, "no-such-approval", "not_found", 404)
    assert fake.armed == [] and await _audit() == []
    assert await _status(other, aid) == "pending"


async def test_foreign_owner_404(fake, jwt):
    sid = await _session()
    aid = await _pending(sid)
    await _refused(sid, aid, "not_found", 404, principal="mallory@example.com")
    await _refused(sid, aid, "not_found", 404, principal="")
    await _refused(sid, aid, "not_found", 404, principal="mallory@example.com",
                   decision="deny")
    assert fake.armed == [] and await _audit() == []
    assert await _status(sid, aid) == "pending"


async def test_change_session_refused(fake, jwt):
    sid = await _session(session_type="change")
    aid = await _pending(sid)
    await _refused(sid, aid, "not_diagnose", 409)
    await _refused(sid, aid, "not_diagnose", 409, decision="deny")
    assert fake.armed == [] and await _audit() == []
    assert await _status(sid, aid) == "pending"


async def test_target_flag_rechecked_at_decision(fake, jwt):
    sid = await _session()
    aid = await _pending(sid)
    async with SessionLocal() as db:
        await db.execute(update(IdeConventions).values(non_production=False))
        await db.commit()
    await _refused(sid, aid, "target_not_non_production", 403)
    assert fake.armed == [] and fake.built == []
    assert await _status(sid, aid) == "pending"


async def test_deny_is_possible_whatever_the_target_flag_or_params(fake):
    """Saying no must always work: it changes nothing in SAP."""
    sid = await _session()
    aid = await _pending(sid)
    bad = await _pending(sid)
    async with SessionLocal() as db:
        await db.execute(update(IdeConventions).values(non_production=False))
        await db.execute(update(IdeApproval).where(IdeApproval.id == bad).values(
            params_json=json.dumps({"processType": "shell", "traceUser": "DEVUSER01"})
        ))
        await db.commit()
    assert (await _decide(sid, aid, decision="deny"))["status"] == "denied"
    assert (await _decide(sid, bad, decision="deny"))["status"] == "denied"
    rows = await _audit()
    assert [(r.action, r.outcome) for r in rows] == [("trace_deny", "denied")] * 2
    assert all("DEVUSER01" not in r.params_json and "shell" not in r.params_json
               for r in rows)
    assert fake.built == []


async def test_target_without_conventions_refused(fake, jwt):
    sid = await _session(target="OTHER")
    aid = await _pending(sid)
    await _refused(sid, aid, "target_not_non_production", 403)
    assert fake.armed == []


async def test_expired_410(fake, jwt):
    sid = await _session()
    aid = await _pending(sid)
    old = utcnow() - timedelta(minutes=approvals.approval_ttl_min() + 1)
    async with SessionLocal() as db:
        await db.execute(
            update(IdeApproval).where(IdeApproval.id == aid).values(created_at=old)
        )
        await db.commit()
    await _refused(sid, aid, "approval_expired", 410)
    assert await _status(sid, aid) == "expired"
    # Still 410, not 409, on a second try -- and audited once.
    await _refused(sid, aid, "approval_expired", 410)
    await _refused(sid, aid, "approval_expired", 410, decision="deny")
    assert fake.armed == []
    assert [(r.action, r.outcome) for r in await _audit()] == [("trace_failed", "expired")]


async def test_deny_audits_and_never_calls_arc1(fake):
    # No JWT bound either: a denial needs none.
    sid = await _session()
    aid = await _pending(sid)
    out = await _decide(sid, aid, decision="deny")
    assert out["status"] == "denied" and out["result"] is None
    assert fake.built == [] and fake.armed == [] and fake.cancelled == []
    assert [(r.action, r.outcome, r.principal) for r in await _audit()] == [
        ("trace_deny", "denied", OWNER)
    ]
    await _refused(sid, aid, "approval_not_pending", 409)
    assert fake.armed == []


async def test_unknown_decision_is_422(fake, jwt):
    sid = await _session()
    aid = await _pending(sid)
    for decision in ("approved", "yes", "", None, True):
        await _refused(sid, aid, "invalid_request", 422, decision=decision)
    assert fake.armed == [] and await _status(sid, aid) == "pending"


async def test_arc1_failure_marks_failed_and_audits(fake, jwt, caplog):
    fake.exc = arc1.Arc1Error(
        502, "ARC-1 SAPDiagnose failed on DEMO: boom", "sap_authentication_failed",
        {"request_id": "rid-7"},
    )
    sid = await _session()
    aid = await _pending(sid)
    with caplog.at_level(logging.INFO, logger="agents.ide.audit"):
        out = await _decide(sid, aid)
    assert out["status"] == "failed"
    assert out["error_code"] == "sap_authentication_failed"
    assert out["result"] is None and out["decided_at"]
    rows = await _audit()
    assert [(r.action, r.outcome, r.principal, r.request_id) for r in rows] == [
        ("trace_arm", "approved", OWNER, None),
        ("trace_failed", "failed", OWNER, "rid-7"),
    ]
    lines = [r.getMessage() for r in caplog.records if r.name == "agents.ide.audit"]
    # The log shows the user did approve before the call failed.
    assert any("approved" in m for m in lines)
    assert any("trace_failed" in m and "failed" in m for m in lines)
    # A failed approval is spent too: no retry without a new request.
    await _refused(sid, aid, "approval_not_pending", 409)
    assert len(fake.armed) == 1


async def test_unexpected_exception_is_failed_with_generic_code(fake, jwt):
    fake.exc = RuntimeError("https://user:secret@host.example/mcp?token=abc")
    sid = await _session()
    aid = await _pending(sid)
    out = await _decide(sid, aid)
    assert (out["status"], out["error_code"]) == ("failed", "arc1_error")
    row = (await _audit())[-1]
    assert (row.action, row.outcome) == ("trace_failed", "failed")
    assert "secret" not in json.dumps(out) and "secret" not in row.params_json


async def test_arm_timeout_is_failed(fake, jwt, monkeypatch):
    async def _hang(params):
        await asyncio.sleep(30)

    fake.arm_trace = _hang
    monkeypatch.setattr(approvals, "ARM_TIMEOUT_S", 0.05)
    sid = await _session()
    aid = await _pending(sid)
    out = await _decide(sid, aid)
    # ARC-1 may have armed the trace before we gave up: say so, never retry.
    assert (out["status"], out["error_code"]) == ("failed", "arc1_timeout_unknown")
    assert out["result"] == {"note": "may have been armed; check trace_requests"}
    assert [(r.action, r.outcome) for r in await _audit()] == [
        ("trace_arm", "approved"), ("trace_failed", "failed"),
    ]
    await _refused(sid, aid, "approval_not_pending", 409)


async def test_no_arc1_call_without_a_durable_intent_row(fake, jwt, monkeypatch):
    """Fail closed: if the audit trail cannot record who approved, nothing
    is armed."""
    async def _down(*a, **kw):
        raise RuntimeError("audit table unavailable")

    monkeypatch.setattr(approvals.store, "add_audit", _down)
    sid = await _session()
    aid = await _pending(sid)
    out = await _decide(sid, aid)
    assert (out["status"], out["error_code"]) == ("failed", "audit_unavailable")
    assert fake.built == [] and fake.armed == []
    monkeypatch.undo()
    await _refused(sid, aid, "approval_not_pending", 409)


async def test_crash_after_approval_leaves_the_intent_row(fake, jwt, monkeypatch):
    async def _crash(*a, **kw):
        raise RuntimeError("process died")

    with monkeypatch.context() as m:
        m.setattr(approvals, "_finish", _crash)
        sid = await _session()
        aid = await _pending(sid)
        with pytest.raises(RuntimeError):
            await _decide(sid, aid)
    rows = await _audit()
    assert [(r.action, r.outcome, r.principal, r.target) for r in rows] == [
        ("trace_arm", "approved", OWNER, TARGET)
    ]
    assert json.loads(rows[0].params_json) == _audit_params(aid)
    assert await _status(sid, aid) == "approved"

    # The sweep (Task 12/13) closes such a row.
    async with SessionLocal() as db:
        row = await get_approval(db, sid, aid)
        assert await approvals.mark_interrupted(db, row) is True
        row = await get_approval(db, sid, aid)
        assert (row.status, row.error_code) == ("failed", "interrupted")
        assert await approvals.mark_interrupted(db, row) is False
    rows = await _audit()
    assert [(r.action, r.outcome, r.principal) for r in rows] == [
        ("trace_arm", "approved", OWNER), ("trace_failed", "failed", OWNER),
    ]


async def test_mark_interrupted_leaves_finished_approvals_alone(fake, jwt):
    sid = await _session()
    aid = await _pending(sid)
    pending = await _pending(sid)
    await _decide(sid, aid)
    async with SessionLocal() as db:
        for a in (aid, pending):
            row = await get_approval(db, sid, a)
            assert await approvals.mark_interrupted(db, row) is False
    assert await _status(sid, aid) == "approved"
    assert await _status(sid, pending) == "pending"
    assert len(await _audit()) == 2


async def test_cancelled_request_still_records_the_outcome(fake, jwt):
    """A client that disconnects mid-call must not leave an armed trace
    without a result and an audit row."""
    gate, started = asyncio.Event(), asyncio.Event()

    async def _slow(params):
        fake.armed.append(params)
        started.set()
        await gate.wait()
        return dict(fake.result)

    fake.arm_trace = _slow
    sid = await _session()
    aid = await _pending(sid)
    task = asyncio.create_task(_decide(sid, aid))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    await approvals.drain()
    async with SessionLocal() as db:
        row = await get_approval(db, sid, aid)
    assert row.status == "approved"
    assert json.loads(row.result_json)["trace_request_id"] == "REQ-1"
    assert [(r.action, r.outcome) for r in await _audit()] == [
        ("trace_arm", "approved"), ("trace_arm", "ok"),
    ]


async def test_no_jwt_refused_before_arc1(fake):
    sid = await _session()
    aid = await _pending(sid)
    async with SessionLocal() as db:
        with pytest.raises(arc1.Arc1UserRequired):
            await approvals.decide(db, sid=sid, aid=aid, principal=OWNER,
                                   decision="approve")
    assert fake.built == [] and fake.armed == []
    # Nothing was decided: the user can sign in again and approve.
    assert await _status(sid, aid) == "pending"


async def test_unusual_arc1_result_is_not_stored_verbatim(fake, jwt):
    fake.result = {"trace_request_id": "bad id; DROP", "expires_at": {"x": 1},
                   "user": "DEVUSER01", "raw": "x" * 5000}
    sid = await _session()
    aid = await _pending(sid)
    out = await _decide(sid, aid)
    assert out["status"] == "approved"
    assert set(out["result"]) == {"expires_at"}
    # No usable expiry from ARC-1: computed from the approved expiresHours.
    assert out["result"]["expires_at"].startswith("20")
    assert (await _audit())[-1].request_id is None


# --- request (the agent proposes) -----------------------------------------------------


def _run(sid, events=None, target=TARGET) -> DiagnoseRun:
    return DiagnoseRun(
        session_id=sid, owner=OWNER, target=target, run_id="run-1",
        destination=DEST,
        emit=None if events is None else (lambda e, d: events.append((e, d))),
    )


async def test_request_stores_normalised_params_and_emits(fake):
    sid = await _session()
    events: list = []
    run = _run(sid, events)
    async with SessionLocal() as db:
        row = await approvals.request(
            db, run, "trace_start",
            {**TRACE, "traceUser": "DEVUSER01", "maxExecutions": 40}, "call-9",
        )
    assert row.status == "pending" and row.run_id == "run-1"
    assert row.tool_call_id == "call-9"
    assert json.loads(row.params_json) == {**TRACE, "maxExecutions": 3}
    assert [e for e, _ in events] == ["approval_required"]
    data = events[0][1]
    assert data["id"] == row.id and data["status"] == "pending"
    assert data["params"] == {**TRACE, "maxExecutions": 3}
    assert data["result"] is None and data["decided_at"] is None
    assert run.approvals == [data]
    assert fake.built == [] and await _audit() == []


async def test_request_refused_on_bad_args_or_production_target(fake):
    sid = await _session()
    async with SessionLocal() as db:
        with pytest.raises(ApprovalError) as e:
            await approvals.request(db, _run(sid), "trace_start",
                                    {"processType": "shell"}, None)
        assert e.value.status == 422
        with pytest.raises(ApprovalError) as e:
            await approvals.request(db, _run(sid), "set_sql_trace_state", {}, None)
        assert e.value.status == 422
        await db.execute(update(IdeConventions).values(non_production=False))
        await db.commit()
        with pytest.raises(ApprovalError) as e:
            await approvals.request(db, _run(sid), "trace_start", TRACE, None)
        assert (e.value.code, e.value.status) == ("target_not_non_production", 403)
        rows = (await db.execute(select(IdeApproval))).scalars().all()
    assert list(rows) == []


async def test_request_refused_outside_a_diagnose_session(fake):
    sid = await _session(session_type="change")
    async with SessionLocal() as db:
        with pytest.raises(ApprovalError) as e:
            await approvals.request(db, _run(sid), "trace_start", TRACE, None)
        assert (e.value.code, e.value.status) == ("not_diagnose", 409)
        with pytest.raises(ApprovalError) as e:
            await approvals.request(db, _run("no-such-session"), "trace_start", TRACE, None)
        assert e.value.status == 404
        rows = (await db.execute(select(IdeApproval))).scalars().all()
    assert list(rows) == []


async def test_request_caps_pending_approvals(fake):
    sid = await _session()
    async with SessionLocal() as db:
        for _ in range(approvals.MAX_PENDING):
            await approvals.request(db, _run(sid), "trace_start", TRACE, None)
        with pytest.raises(ApprovalError) as e:
            await approvals.request(db, _run(sid), "trace_start", TRACE, None)
    assert (e.value.code, e.value.status) == ("too_many_pending", 409)


async def test_request_survives_a_failing_emit(fake):
    sid = await _session()

    def _boom(event, data):
        raise RuntimeError("stream closed")

    run = _run(sid)
    run.emit = _boom
    await _armed(sid, "REQ-1")
    async with SessionLocal() as db:
        row = await approvals.request(db, run, "trace_cancel", {"id": "REQ-1"}, None)
    assert row.status == "pending"


# --- Arc1Client ----------------------------------------------------------------------


class _FakeRun:
    def __init__(self, exc=None, result="ok"):
        self.exc, self.result, self.calls = exc, result, []

    async def direct_call_tool(self, name, args):
        self.calls.append((name, args))
        if self.exc is not None:
            raise self.exc
        return self.result


class _FakeServer:
    def __init__(self, run):
        self.run = run

    async def for_run(self, ctx):
        return self.run


@pytest.fixture
def built(monkeypatch):
    state = {"calls": [], "run": _FakeRun()}

    def _create(name, base_url, auth_mode="jwt", tool_prefix=None, oauth=None, **kw):
        state["calls"].append((name, base_url, auth_mode, oauth))
        return _FakeServer(state["run"])

    monkeypatch.setattr(arc1.shared, "create_mcp_server", _create)
    return state


@pytest.mark.parametrize("policy", [readonly.CHANGE, readonly.DIAGNOSE])
@pytest.mark.parametrize("action", ["trace_start", "trace_cancel"])
async def test_call_never_executes_trace_start(built, jwt, policy, action):
    client = arc1.Arc1Client(TARGET, DEST, policy=policy)
    with pytest.raises(arc1.Arc1Refused):
        await client.call("SAPDiagnose", {"action": action, "id": "REQ-1"})
    assert built["calls"] == [] and built["run"].calls == []


async def test_arm_trace_builds_args_itself_without_trace_user(built, jwt):
    built["run"].result = json.dumps(
        {"id": "REQ-9", "expiresAt": "2026-10-03T20:00:00Z", "traceUser": "DEVUSER01"}
    )
    client = arc1.Arc1Client(TARGET, DEST, policy=readonly.DIAGNOSE)
    out = await client.arm_trace({
        **TRACE, "traceUser": "DEVUSER01", "user": "DEVUSER01",
        "action": "set_sql_trace_state", "maxExecutions": 77, "expiresHours": 77,
    })
    assert built["run"].calls == [("SAPDiagnose", {
        "action": "trace_start", **TRACE, "maxExecutions": 3, "expiresHours": 8,
    })]
    # As the signed-in user: destination mode with user_context, never app-level.
    assert built["calls"][0][2] == "destination"
    assert built["calls"][0][3] == {"destination": DEST, "user_context": True}
    assert out == {"trace_request_id": "REQ-9", "expires_at": "2026-10-03T20:00:00Z"}


async def test_cancel_trace_sends_only_the_id(built, jwt):
    client = arc1.Arc1Client(TARGET, DEST, policy=readonly.DIAGNOSE)
    out = await client.cancel_trace("REQ-9")
    assert built["run"].calls == [
        ("SAPDiagnose", {"action": "trace_cancel", "id": "REQ-9"})
    ]
    assert out == {"trace_request_id": "REQ-9"}
    with pytest.raises(arc1.Arc1Refused):
        await client.cancel_trace("bad id")
    assert len(built["run"].calls) == 1


async def test_arm_trace_rejects_invalid_params_before_any_call(built, jwt):
    client = arc1.Arc1Client(TARGET, DEST, policy=readonly.DIAGNOSE)
    with pytest.raises(arc1.Arc1Refused):
        await client.arm_trace({"processType": "shell"})
    with pytest.raises(arc1.Arc1Refused):
        await client.arm_trace("trace_start")
    assert built["calls"] == [] and built["run"].calls == []


async def test_arm_trace_needs_a_diagnose_client(built, jwt):
    client = arc1.Arc1Client(TARGET, DEST)  # change policy
    with pytest.raises(arc1.Arc1Refused):
        await client.arm_trace(TRACE)
    with pytest.raises(arc1.Arc1Refused):
        await client.cancel_trace("REQ-9")
    assert built["calls"] == [] and built["run"].calls == []


async def test_arm_trace_without_jwt_is_424_and_not_built(built):
    client = arc1.Arc1Client(TARGET, DEST, policy=readonly.DIAGNOSE)
    with pytest.raises(arc1.Arc1UserRequired):
        await client.arm_trace(TRACE)
    with pytest.raises(arc1.Arc1UserRequired):
        await client.cancel_trace("REQ-9")
    assert built["calls"] == [] and built["run"].calls == []


async def test_arm_trace_on_cf_needs_a_user_context_destination(built, jwt, monkeypatch):
    """No jwt-mode fallback on CF: without a destination the call would not
    be exchanged for the user's SAP identity."""
    monkeypatch.setenv("IDE_ARC1_URL_DEMO", "https://arc1.example.com/mcp")
    monkeypatch.setattr(arc1.shared, "ON_CF", True)
    client = arc1.Arc1Client(TARGET, "", policy=readonly.DIAGNOSE)
    with pytest.raises(arc1.Arc1NotConfigured):
        await client.arm_trace(TRACE)
    assert built["calls"] == [] and built["run"].calls == []


async def test_arm_trace_maps_arc1_error_payload(built, jwt):
    built["run"].result = json.dumps(
        {"error": "SAP_AUTHENTICATION_FAILED", "retryable": False, "requestId": "r-1"}
    )
    client = arc1.Arc1Client(TARGET, DEST, policy=readonly.DIAGNOSE)
    with pytest.raises(arc1.Arc1Error) as e:
        await client.arm_trace(TRACE)
    assert e.value.code == "sap_authentication_failed"
    assert e.value.extra["request_id"] == "r-1"


async def test_unchecked_call_only_takes_the_two_trace_actions(built, jwt):
    client = arc1.Arc1Client(TARGET, DEST, policy=readonly.DIAGNOSE)
    for tool, args in [
        ("SAPDiagnose", {"action": "set_sql_trace_state"}),
        ("SAPDiagnose", {"action": "dumps"}),
        ("SAPWrite", {"action": "trace_start"}),
        ("SAPDiagnose", {"action": "trace_start", "traceUser": "DEVUSER01"}),
        ("SAPDiagnose", {"action": "trace_start", "user": "DEVUSER01"}),
    ]:
        with pytest.raises(arc1.Arc1Refused):
            await client._call_unchecked(tool, args)
    assert built["calls"] == [] and built["run"].calls == []


# --- the card's countdown ---------------------------------------------------------


async def test_approval_json_carries_the_ttl_the_decision_uses(monkeypatch):
    """The card counts down from ``created_at`` + ``ttl_min``: the value is
    the one ``decide`` checks (``store.approval_ttl_min()``), so the two
    cannot disagree -- including the floor of one minute."""
    sid = await _session()
    aid = await _pending(sid)
    async with SessionLocal() as db:
        row = await db.get(IdeApproval, aid)
    monkeypatch.setattr(approvals.store, "APPROVAL_TTL_MIN", 30)
    assert approvals.approval_json(row)["ttl_min"] == 30
    monkeypatch.setattr(approvals.store, "APPROVAL_TTL_MIN", 0)
    assert approvals.approval_json(row)["ttl_min"] == 1
