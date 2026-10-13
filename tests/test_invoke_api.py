"""The invoke API: ``POST /api/invoke/{slug}`` and ``GET /api/invoke/runs/{id}``.

Most tests mount the router on a bare app with a small ASGI wrapper that binds
``current_principal`` / ``current_jwt`` from a test header (the real app's
middleware binds them from the validated JWT; without an XSUAA binding every
request is the same local principal). One test drives the real app, so the
include line in ``app.py`` and the scope dependency are covered too.

The specialist is a fake set on ``invoke.registry``: it receives the prompt
and the per-run ``submit_result`` toolset exactly as ``Agent.run`` would, and
calls the tool's function. One test runs a real pydantic-ai ``Agent`` on a
``FunctionModel`` to prove ``toolsets=`` and ``BinaryContent`` on 1.72.

Run:  .venv/bin/python -m pytest tests/test_invoke_api.py -q
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from agents import invoke, job_runner  # noqa: E402
from agents.auth import current_jwt, current_principal  # noqa: E402
from agents.db import (  # noqa: E402
    BITBUCKET_APPROVER_INVOKE_MESSAGE,
    AgentConfig,
    JobRun,
    ODataAuditLog,
    SessionLocal,
    engine,
    init_db,
    upsert_agent,
)

ALICE = "alice@example.com"
BOB = "bob@example.com"
SLUG = "expense"
MCP = {"url": "https://mcp.example.test/mcp", "auth_mode": "jwt"}
SCHEMA = {
    "type": "object",
    "properties": {"expense_id": {"type": "string"},
                   "status": {"type": "string", "enum": ["draft"]}},
    "required": ["expense_id", "status"],
    "additionalProperties": False,
}
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"PNG-CONTENT-MARKER" + b"\0" * 32
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"JPEG-CONTENT" + b"\0" * 32
GIF_BYTES = b"GIF89a" + b"\0" * 32
SECRET_NAME = "SECRET-receipt-name"


def pdf(pages: int, *, compressed: bool = False) -> bytes:
    """A minimal PDF with ``pages`` page objects, plain or in a Flate stream."""
    objects = b"".join(
        b"%d 0 obj << /Type /Page /Parent 2 0 R >> endobj\n" % (i + 3) for i in range(pages)
    )
    head = b"%PDF-1.7\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
    if not compressed:
        return head + objects + b"%%EOF\n"
    body = zlib.compress(objects)
    return (head + b"9 0 obj << /Type /ObjStm /Filter /FlateDecode /Length %d >>\nstream\n"
            % len(body) + body + b"\nendstream\nendobj\n%%EOF\n")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
class Specialist:
    """Stands in for ``Agent``: records the call, runs ``behaviour``."""

    def __init__(self, behaviour) -> None:
        self.behaviour = behaviour
        self.calls: list[tuple[Any, dict]] = []
        self.release = asyncio.Event()

    async def run(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        submit = kwargs["toolsets"][0].tools["submit_result"].function
        self.seen_principal = current_principal.get()
        self.seen_jwt = current_jwt.get()
        return SimpleNamespace(output=await self.behaviour(self, submit))


async def submits_valid(sp: Specialist, submit) -> str:
    answer = await submit(result={"expense_id": "4711", "status": "draft"})
    assert answer == {"ok": True}
    return "Created draft expense 4711 from the receipt."


async def never_submits(sp: Specialist, submit) -> str:
    return "I think the expense is 4711."


async def blocks(sp: Specialist, submit) -> str:
    await sp.release.wait()
    await submit(result={"expense_id": "1", "status": "draft"})
    return "done"


def use_specialist(monkeypatch, sp: Specialist, name: str = "expense") -> None:
    monkeypatch.setattr(
        invoke, "registry", SimpleNamespace(build=SimpleNamespace(specialists={name: sp}))
    )


def bound_app() -> FastAPI:
    """The router behind a wrapper that binds the caller from ``x-test-user``."""
    inner = FastAPI()
    inner.include_router(invoke.router)

    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        headers = dict(scope["headers"])
        user = headers.get(b"x-test-user", ALICE.encode()).decode()
        p = current_principal.set(user)
        j = current_jwt.set(f"jwt-of-{user}")
        try:
            await inner(scope, receive, send)
        finally:
            current_principal.reset(p)
            current_jwt.reset(j)

    return asgi  # type: ignore[return-value]


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=bound_app()),
                           base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def _clean(monkeypatch):
    monkeypatch.setattr(job_runner, "FINALIZE_RETRY_DELAYS", (0, 0))
    monkeypatch.delenv("INVOKE_MAX_CONCURRENT", raising=False)
    await init_db()
    async with SessionLocal() as s:
        await s.execute(delete(AgentConfig))
        await s.execute(delete(JobRun))
        await s.execute(delete(ODataAuditLog))
        await s.commit()
    yield
    await invoke.cancel_all_invoke_runs()
    invoke._active.clear()


async def make_agent(name: str = "expense", slug: str = SLUG, **kw: Any) -> AgentConfig:
    kw.setdefault("expose_invoke", True)
    kw.setdefault("result_schema", SCHEMA)
    async with SessionLocal() as s:
        return await upsert_agent(
            s, name=name, description="d", instructions="i", mcp_servers=[MCP],
            expose_chat=False, api_slug=slug, **kw,
        )


def form(text: str | None = "Book this receipt", files: list[tuple] | None = None,
         user: str = ALICE, **extra: str) -> dict[str, Any]:
    """A multipart body built by hand: httpx sends a form without files as
    urlencoded, and the API takes multipart only."""
    boundary = "----invoke-test-boundary"
    chunks: list[bytes] = []
    fields = ({} if text is None else {"input": text}) | extra
    for name, value in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                      f"\r\n\r\n".encode() + value.encode() + b"\r\n")
    for filename, data, ctype in files or []:
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
            f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'.encode("utf-8")
            + data + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return {"content": b"".join(chunks),
            "headers": {"content-type": f"multipart/form-data; boundary={boundary}",
                        "x-test-user": user}}


async def wait_done(client: AsyncClient, run_id: str, user: str = ALICE) -> dict:
    for _ in range(200):
        r = await client.get(f"/api/invoke/runs/{run_id}", headers={"x-test-user": user})
        assert r.status_code == 200
        if r.json()["status"] != "running":
            return r.json()
        await asyncio.sleep(0.01)
    raise AssertionError("run did not finish")


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
async def test_202_then_poll_completes_with_the_result(client, monkeypatch):
    sp = Specialist(submits_valid)
    use_specialist(monkeypatch, sp)
    await make_agent()
    r = await client.post(f"/api/invoke/{SLUG}",
                          **form(files=[("r.png", PNG_BYTES, "image/png")]))
    assert r.status_code == 202 and set(r.json()) == {"run_id"}
    answer = await wait_done(client, r.json()["run_id"])
    assert answer["status"] == "completed" and answer["error"] is None
    assert answer["result"] == {"expense_id": "4711", "status": "draft"}
    assert answer["text"] == "Created draft expense 4711 from the receipt."
    assert set(answer) == {"status", "result", "writes", "text", "activity", "error"}
    # The run is the caller's: their principal and JWT, no run-as.
    assert (sp.seen_principal, sp.seen_jwt) == (ALICE, f"jwt-of-{ALICE}")
    prompt, kwargs = sp.calls[0]
    assert prompt[-1] == "Book this receipt"
    assert prompt[0].media_type == "image/png" and prompt[0].data == PNG_BYTES
    assert kwargs["output_type"] is str
    async with SessionLocal() as s:
        row = await s.get(JobRun, r.json()["run_id"])
    # The row outlives the deadline by a margin, so a stale sweep never takes
    # a live run.
    assert (row.trigger, row.created_by, row.timeout_seconds) == ("invoke", ALICE, 360)
    assert json.loads(row.report_json) == {"text": answer["text"], "result": answer["result"]}
    assert row.summary == answer["text"][:200]


async def test_wait_answers_200_with_the_poll_answer(client, monkeypatch):
    use_specialist(monkeypatch, Specialist(submits_valid))
    await make_agent()
    r = await client.post(f"/api/invoke/{SLUG}?wait=5", **form())
    assert r.status_code == 200
    assert r.json()["status"] == "completed" and r.json()["result"]["expense_id"] == "4711"


async def test_wait_that_runs_out_answers_202(client, monkeypatch):
    sp = Specialist(blocks)
    use_specialist(monkeypatch, sp)
    await make_agent()
    r = await client.post(f"/api/invoke/{SLUG}?wait=0.05", **form())
    assert r.status_code == 202 and set(r.json()) == {"run_id"}
    sp.release.set()
    assert (await wait_done(client, r.json()["run_id"]))["status"] == "completed"


@pytest.mark.parametrize("wait", ["31", "-1", "soon"])
async def test_wait_out_of_range_is_422(client, monkeypatch, wait):
    use_specialist(monkeypatch, Specialist(submits_valid))
    await make_agent()
    r = await client.post(f"/api/invoke/{SLUG}?wait={wait}", **form())
    assert r.status_code == 422


async def test_a_run_without_submit_result_fails_with_no_result(client, monkeypatch):
    use_specialist(monkeypatch, Specialist(never_submits))
    await make_agent()
    r = await client.post(f"/api/invoke/{SLUG}?wait=5", **form())
    answer = r.json()
    assert (answer["status"], answer["error"], answer["result"]) == ("failed", "no_result", None)
    assert answer["text"] == "I think the expense is 4711."


async def test_a_schema_violation_goes_back_to_the_model(client, monkeypatch):
    answers: list[dict] = []

    async def corrects(sp, submit):
        answers.append(await submit(result={"expense_id": 4711, "status": "posted",
                                            "extra_secret": "x"}))
        answers.append(await submit(result={"status": "draft"}))
        answers.append(await submit(result={"expense_id": "4711", "status": "draft"}))
        answers.append(await submit(result={"expense_id": "4712", "status": "draft"}))
        return "ok"

    use_specialist(monkeypatch, Specialist(corrects))
    await make_agent()
    answer = (await client.post(f"/api/invoke/{SLUG}?wait=5", **form())).json()
    first, second, third, fourth = answers
    assert first["error"]["code"] == "schema_violation"
    assert "$.expense_id: must be of type string" in first["error"]["problems"]
    assert "$.status: must be one of the allowed values" in first["error"]["problems"]
    assert not any("extra_secret" in p for p in first["error"]["problems"])
    assert second["error"]["problems"] == ["$.expense_id: is required"]
    assert third == {"ok": True}
    assert fourth["error"]["code"] == "already_submitted"
    # First valid call wins.
    assert answer["status"] == "completed" and answer["result"]["expense_id"] == "4711"


async def test_an_agent_without_a_schema_accepts_any_object(client, monkeypatch):
    async def any_obj(sp, submit):
        assert await submit(result={"anything": [1, 2]}) == {"ok": True}
        return "ok"

    use_specialist(monkeypatch, Specialist(any_obj))
    await make_agent(result_schema=None)
    answer = (await client.post(f"/api/invoke/{SLUG}?wait=5", **form())).json()
    assert answer["result"] == {"anything": [1, 2]}


async def test_a_failing_run_answers_a_code_not_the_exception_text(client, monkeypatch):
    async def explodes(sp, submit):
        raise RuntimeError("boom https://sap.example.test/x?token=SECRET")

    use_specialist(monkeypatch, Specialist(explodes))
    await make_agent()
    r = await client.post(f"/api/invoke/{SLUG}?wait=5", **form())
    assert r.json()["status"] == "failed" and r.json()["error"] == "run_failed"
    assert "SECRET" not in r.text and "boom" not in r.text


async def test_a_missing_specialist_fails_the_run(client, monkeypatch):
    use_specialist(monkeypatch, Specialist(submits_valid), name="someone-else")
    await make_agent()
    r = await client.post(f"/api/invoke/{SLUG}?wait=5", **form())
    assert (r.json()["status"], r.json()["error"]) == ("failed", "agent_not_built")


async def test_the_timeout_is_the_invoke_one(client, monkeypatch):
    sp = Specialist(blocks)
    use_specialist(monkeypatch, sp)
    monkeypatch.setattr(invoke, "INVOKE_TIMEOUT_SECONDS", 0.05)
    await make_agent()
    r = await client.post(f"/api/invoke/{SLUG}?wait=5", **form())
    assert (r.json()["status"], r.json()["error"]) == ("failed", "timeout")


async def test_a_tools_own_timeout_is_not_the_runs(client, monkeypatch):
    async def tool_timeout(sp, submit):
        raise TimeoutError()

    use_specialist(monkeypatch, Specialist(tool_timeout))
    await make_agent()
    r = await client.post(f"/api/invoke/{SLUG}?wait=5", **form())
    assert (r.json()["status"], r.json()["error"]) == ("failed", "run_failed")


async def test_a_cancelled_run_reads_canceled(client, monkeypatch):
    sp = Specialist(blocks)
    use_specialist(monkeypatch, sp)
    await make_agent()
    run_id = (await client.post(f"/api/invoke/{SLUG}", **form())).json()["run_id"]
    while not sp.calls:
        await asyncio.sleep(0.01)
    await invoke.cancel_all_invoke_runs()
    answer = await wait_done(client, run_id)
    assert (answer["status"], answer["error"]) == ("canceled", "canceled")


# ---------------------------------------------------------------------------
# Who may see and start what
# ---------------------------------------------------------------------------
async def test_another_principals_run_is_404(client, monkeypatch):
    use_specialist(monkeypatch, Specialist(submits_valid))
    await make_agent()
    run_id = (await client.post(f"/api/invoke/{SLUG}?wait=5", **form())).json()
    run_id = (await client.post(f"/api/invoke/{SLUG}", **form())).json()["run_id"]
    r = await client.get(f"/api/invoke/runs/{run_id}", headers={"x-test-user": BOB})
    assert r.status_code == 404 and r.json() == {"detail": "No such run"}
    assert (await client.get(f"/api/invoke/runs/{uuid.uuid4()}")).status_code == 404


async def test_a_job_run_of_the_same_principal_is_404(client):
    agent = await make_agent()
    async with SessionLocal() as s:
        s.add(JobRun(id="job-1", agent_id=agent.id, agent_name=agent.name,
                     trigger="manual", status="success", created_by=ALICE))
        await s.commit()
    assert (await client.get("/api/invoke/runs/job-1")).status_code == 404


@pytest.mark.parametrize("case", ["unknown", "not_exposed", "disabled"])
async def test_an_agent_that_is_not_invokable_is_the_same_404(client, monkeypatch, case):
    use_specialist(monkeypatch, Specialist(submits_valid))
    if case == "not_exposed":
        await make_agent(expose_invoke=False)
    elif case == "disabled":
        await make_agent(enabled=False)
    r = await client.post(f"/api/invoke/{SLUG}", **form())
    assert r.status_code == 404 and r.json() == {"detail": "No such agent"}


async def test_a_second_call_of_the_same_user_is_409_with_the_running_id(client, monkeypatch):
    sp = Specialist(blocks)
    use_specialist(monkeypatch, sp)
    await make_agent()
    first = (await client.post(f"/api/invoke/{SLUG}", **form())).json()["run_id"]
    r = await client.post(f"/api/invoke/{SLUG}", **form())
    assert r.status_code == 409 and r.json()["run_id"] == first
    assert ALICE not in r.text
    # Another user is not held off by Alice's run.
    other = await client.post(f"/api/invoke/{SLUG}", **form(user=BOB))
    assert other.status_code == 202
    sp.release.set()
    await wait_done(client, first)
    await wait_done(client, other.json()["run_id"], BOB)
    # The lock is gone once the run ended.
    assert (await client.post(f"/api/invoke/{SLUG}?wait=5", **form())).status_code == 200


async def test_the_instance_cap_is_429(client, monkeypatch):
    sp = Specialist(blocks)
    use_specialist(monkeypatch, sp)
    monkeypatch.setenv("INVOKE_MAX_CONCURRENT", "1")
    await make_agent()
    first = (await client.post(f"/api/invoke/{SLUG}", **form())).json()["run_id"]
    r = await client.post(f"/api/invoke/{SLUG}", **form(user=BOB))
    assert r.status_code == 429
    assert r.json() == {"detail": "Too many invoke runs are in progress; try again later"}
    sp.release.set()
    await wait_done(client, first)


async def test_an_invoke_run_does_not_hold_off_the_scheduler(client, monkeypatch):
    sp = Specialist(blocks)
    use_specialist(monkeypatch, sp)
    agent = await make_agent()
    run_id = (await client.post(f"/api/invoke/{SLUG}", **form())).json()["run_id"]
    from agents.db import active_job_run

    async with SessionLocal() as s:
        assert await active_job_run(s, agent.id) is None
    sp.release.set()
    await wait_done(client, run_id)


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------
async def _refused(client, expected: int, **kw) -> dict:
    r = await client.post(f"/api/invoke/{SLUG}", **kw)
    assert r.status_code == expected, r.text
    assert SECRET_NAME not in r.text and "CONTENT" not in r.text
    return r.json()


async def test_attachment_refusals(client, monkeypatch):
    sp = Specialist(submits_valid)
    use_specialist(monkeypatch, sp)
    await make_agent(invoke_limits={"max_file_bytes": 400, "max_files": 2,
                                    "max_pdf_pages": 2, "media_types": ["image/png",
                                                                        "application/pdf"]})
    name = f"{SECRET_NAME}.png"
    big = PNG_BYTES + b"\0" * 400
    assert (await _refused(client, 413, **form(files=[(name, big, "image/png")])))[
        "detail"] == "A file is larger than this agent accepts"
    three = [(name, PNG_BYTES, "image/png")] * 3
    assert (await _refused(client, 422, **form(files=three)))["detail"] == (
        "More files than this agent accepts")
    assert (await _refused(client, 415, **form(files=[(name, GIF_BYTES, "image/gif")])))[
        "detail"] == "A file is of a media type this agent does not accept"
    # JPEG is sniffable but not allowed for this agent.
    assert (await _refused(client, 415, **form(files=[(name, JPEG_BYTES, "image/jpeg")])))[
        "detail"] == "A file is of a media type this agent does not accept"
    assert (await _refused(client, 422, **form(files=[(name, pdf(3), "application/pdf")])))[
        "detail"] == "A PDF has more pages than this agent accepts"
    assert (await _refused(client, 422, **form(files=[(name, pdf(0), "application/pdf")])))[
        "detail"] == "The page count of a PDF could not be determined"
    assert (await _refused(client, 422, **form(files=[(name, b"", "image/png")])))[
        "detail"] == "A file is empty"
    assert sp.calls == []


async def test_a_declared_type_that_the_bytes_contradict_is_415(client, monkeypatch):
    use_specialist(monkeypatch, Specialist(submits_valid))
    await make_agent()
    out = await _refused(client, 415,
                         **form(files=[(f"{SECRET_NAME}.pdf", PNG_BYTES, "application/pdf")]))
    assert out["detail"] == "A file's content does not match its declared media type"
    # A PDF renamed .png is caught the same way.
    await _refused(client, 415, **form(files=[(f"{SECRET_NAME}.png", pdf(1), "image/png")]))


async def test_an_undeclared_type_is_taken_from_the_bytes(client, monkeypatch):
    sp = Specialist(submits_valid)
    use_specialist(monkeypatch, sp)
    await make_agent()
    r = await client.post(f"/api/invoke/{SLUG}?wait=5",
                          **form(files=[("a.bin", PNG_BYTES, "application/octet-stream"),
                                        ("b.jpg", JPEG_BYTES, "image/jpg")]))
    assert r.status_code == 200
    assert [p.media_type for p in sp.calls[0][0][:-1]] == ["image/png", "image/jpeg"]


@pytest.mark.parametrize("text", [None, "", "   \n"])
async def test_input_is_required(client, monkeypatch, text):
    use_specialist(monkeypatch, Specialist(submits_valid))
    await make_agent()
    out = await _refused(client, 422, **form(text=text))
    assert out["detail"] == "The field 'input' is required and must not be empty"


async def test_other_form_shapes_are_refused(client, monkeypatch):
    use_specialist(monkeypatch, Specialist(submits_valid))
    await make_agent()
    assert (await _refused(client, 422, **form(other="x")))["detail"] == (
        "The form holds a field other than 'input' and 'files'")
    assert (await _refused(client, 415, json={"input": "x"}))["detail"] == (
        "The request must be multipart/form-data")
    assert (await _refused(client, 413, **form(text="x" * (64 * 1024 + 1))))["detail"] == (
        "The field 'input' is too large")


async def test_the_body_cap_refuses_before_reading_on(client, monkeypatch):
    use_specialist(monkeypatch, Specialist(submits_valid))
    await make_agent(invoke_limits={"max_file_bytes": 100, "max_files": 1})
    body = b"x" * (300 * 1024)
    r = await client.post(f"/api/invoke/{SLUG}", content=body,
                          headers={"content-type": "multipart/form-data; boundary=zz"})
    assert r.status_code == 413 and r.json()["detail"] == "The request body is too large"


def test_pdf_pages_are_counted_in_compressed_object_streams():
    assert invoke.count_pdf_pages(pdf(4)) == 4
    assert invoke.count_pdf_pages(pdf(4, compressed=True)) == 4
    # "/Type /Pages" (the tree node) is no page.
    assert invoke.count_pdf_pages(b"%PDF-1.4 << /Type /Pages /Count 9 >>") == 0


async def test_activity_carries_name_type_and_size_only(client, monkeypatch):
    use_specialist(monkeypatch, Specialist(submits_valid))
    await make_agent()
    long_name = "re\x00ce​ipt\n" + "x" * 300 + ".png"
    r = await client.post(f"/api/invoke/{SLUG}?wait=5",
                          **form(files=[(long_name, PNG_BYTES, "image/png"),
                                        ("doc.pdf", pdf(1), "application/pdf")]))
    events = [e for e in r.json()["activity"]["events"] if e["kind"] == "attachment"]
    assert [set(e) for e in events] == [{"ts", "agent", "kind", "name", "media_type", "size"}] * 2
    first, second = events
    assert first["name"] == ("receipt" + "x" * 300)[:120]
    assert (first["media_type"], first["size"]) == ("image/png", len(PNG_BYTES))
    assert (second["name"], second["media_type"]) == ("doc.pdf", "application/pdf")
    async with SessionLocal() as s:
        row = await s.get(JobRun, (await s.execute(select(JobRun.id))).scalar_one())
    assert "PNG-CONTENT-MARKER" not in row.activity_json
    assert "Book this receipt" not in (row.activity_json or "")


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------
async def test_writes_come_from_the_audit_rows_of_the_run(client, monkeypatch):
    use_specialist(monkeypatch, Specialist(submits_valid))
    await make_agent()
    run_id = (await client.post(f"/api/invoke/{SLUG}?wait=5", **form())).json()
    run_id = (await client.post(f"/api/invoke/{SLUG}", **form())).json()["run_id"]
    await wait_done(client, run_id)

    def audit(rid, op, created, outcome="ok"):
        return ODataAuditLog(
            call_id=uuid.uuid4().hex, agent="expense", run_id=rid, sent_as="SECRET-SENT-AS",
            run_principal=ALICE, service="expenses", target="ExpenseReports",
            operation=op, key_json=json.dumps({"ExpenseId": "SECRET-KEY"}),
            created_key_json=created, body_fields_json='["Amount"]', outcome=outcome,
        )

    async with SessionLocal() as s:
        s.add(audit(run_id, "create", json.dumps({"ExpenseId": "4711"})))
        s.add(audit(run_id, "update", None, outcome="sap_error"))
        s.add(audit("another-run", "create", json.dumps({"ExpenseId": "9999"})))
        await s.commit()
    r = await client.get(f"/api/invoke/runs/{run_id}")
    assert r.json()["writes"] == [
        {"service": "expenses", "entity_set": "ExpenseReports", "operation": "create",
         "key": {"ExpenseId": "4711"}, "outcome": "ok"},
        {"service": "expenses", "entity_set": "ExpenseReports", "operation": "update",
         "key": None, "outcome": "sap_error"},
    ]
    assert "SECRET" not in r.text and "Amount" not in r.text


async def test_the_run_binds_its_id_for_the_audit(client, monkeypatch):
    from agents.run_activity import current_run_id

    seen: list[str | None] = []

    async def looks(sp, submit):
        seen.append(current_run_id.get())
        await submit(result={"expense_id": "1", "status": "draft"})
        return "ok"

    use_specialist(monkeypatch, Specialist(looks))
    await make_agent()
    r = await client.post(f"/api/invoke/{SLUG}", **form())
    await wait_done(client, r.json()["run_id"])
    assert seen == [r.json()["run_id"]]
    assert current_run_id.get() is None


# ---------------------------------------------------------------------------
# The approving-bitbucket guard, storage and route
# ---------------------------------------------------------------------------
APPROVING = {"url": "builtin:bitbucket", "auth_mode": "destination",
             "oauth": {"destination": "BITBUCKET", "workspace": "acme-ws",
                       "allow_comment": True, "allow_approve": True}}


async def test_storage_refuses_invoke_for_an_approving_agent():
    async with SessionLocal() as s:
        with pytest.raises(ValueError) as refused:
            await upsert_agent(s, name="approver", description="d", instructions="i",
                               mcp_servers=[APPROVING], expose_chat=False,
                               api_slug="approver", expose_invoke=True)
    assert str(refused.value) == BITBUCKET_APPROVER_INVOKE_MESSAGE
    async with SessionLocal() as s:
        stored = await upsert_agent(s, name="approver", description="d", instructions="i",
                                    mcp_servers=[APPROVING], expose_chat=False,
                                    api_slug="approver")
    assert stored.expose_invoke == 0


async def test_admin_update_cannot_turn_approving_on_for_an_invoke_agent():
    import app as app_module

    agent = await make_agent()
    async with AsyncClient(transport=ASGITransport(app=app_module.app),
                           base_url="http://test") as c:
        r = await c.put(f"/admin/api/agents/{agent.id}", json={
            "name": "expense", "description": "d", "instructions": "i",
            "mcp_servers": [APPROVING], "expose_chat": False, "api_slug": SLUG,
        })
    assert r.status_code == 422
    assert r.json()["detail"] == BITBUCKET_APPROVER_INVOKE_MESSAGE


async def test_a_hand_written_approving_row_is_404_at_the_route(client, monkeypatch):
    use_specialist(monkeypatch, Specialist(submits_valid))
    async with SessionLocal() as s:
        s.add(AgentConfig(name="expense", description="d", instructions="i",
                          mcp_url=APPROVING["url"], auth_mode="destination",
                          oauth_json=json.dumps(APPROVING["oauth"]), enabled=1,
                          expose_chat=0, expose_invoke=1, api_slug=SLUG))
        await s.commit()
    r = await client.post(f"/api/invoke/{SLUG}", **form())
    assert r.status_code == 404 and r.json() == {"detail": "No such agent"}


async def test_expose_invoke_needs_a_slug():
    async with SessionLocal() as s:
        with pytest.raises(ValueError, match="expose_invoke requires an api_slug"):
            await upsert_agent(s, name="x", description="d", instructions="i",
                               mcp_servers=[MCP], expose_invoke=True)


# ---------------------------------------------------------------------------
# The real app
# ---------------------------------------------------------------------------
async def test_the_real_app_serves_the_routes(monkeypatch):
    import app as app_module

    use_specialist(monkeypatch, Specialist(submits_valid))
    await make_agent()
    async with AsyncClient(transport=ASGITransport(app=app_module.app),
                           base_url="http://test") as c:
        r = await c.post(f"/api/invoke/{SLUG}?wait=5", **form())
        assert r.status_code == 200 and r.json()["status"] == "completed"
        assert (await c.get("/api/invoke/runs/nope")).status_code == 404


@pytest.mark.usefixtures("real_agents_and_mcp")
async def test_a_real_agent_gets_the_tool_and_the_attachment():
    """pydantic-ai 1.72: ``Agent.run(toolsets=[...])`` adds the tool for the
    run only, and ``BinaryContent`` reaches the model as a user part."""
    from pydantic_ai import Agent
    from pydantic_ai.messages import BinaryContent, ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    seen: dict[str, Any] = {}

    def model(messages, info: AgentInfo):
        seen.setdefault("tools", [t.name for t in info.function_tools])
        seen.setdefault("parts", messages[0].parts)
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart(
                "submit_result", {"result": {"expense_id": "1", "status": "draft"}})])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(model))
    box = invoke.ResultBox(schema=SCHEMA)
    result = await agent.run(
        [BinaryContent(data=PNG_BYTES, media_type="image/png"), "go"],
        toolsets=[invoke.result_toolset(box)], output_type=str,
    )
    assert result.output == "done"
    assert box.result == {"expense_id": "1", "status": "draft"}
    assert "submit_result" in seen["tools"]
    content = seen["parts"][-1].content
    assert isinstance(content[0], BinaryContent) and content[1] == "go"
    # Not left on the agent for the next run.
    assert "submit_result" not in [t.name for ts in agent.toolsets
                                   for t in getattr(ts, "tools", {}).values()]


# ---------------------------------------------------------------------------
# Schema: init_db adds the columns to an existing SQLite database
# ---------------------------------------------------------------------------
async def test_init_db_adds_the_invoke_columns_to_an_existing_table():
    async with engine.begin() as conn:
        for column in ("expose_invoke", "result_schema_json", "invoke_limits_json"):
            await conn.exec_driver_sql(f"ALTER TABLE agent_configs DROP COLUMN {column}")
        await conn.exec_driver_sql(
            "INSERT INTO agent_configs (name, description, instructions, mcp_url, "
            "auth_mode, expose_chat, expose_api, run_timeout_seconds, enabled) "
            "VALUES ('old', 'd', 'i', 'https://mcp.example.test/mcp', 'jwt', 1, 0, 1800, 1)"
        )
    await init_db()
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.exec_driver_sql(
            "PRAGMA table_info(agent_configs)")).fetchall()}
    assert {"expose_invoke", "result_schema_json", "invoke_limits_json"} <= cols
    async with SessionLocal() as s:
        row = (await s.execute(select(AgentConfig).where(AgentConfig.name == "old"))).scalar_one()
    assert (row.expose_invoke, row.result_schema_json, row.invoke_limits_json) == (0, None, None)
    assert row.to_dict()["expose_invoke"] is False and row.to_dict()["result_schema"] is None


# ---------------------------------------------------------------------------
# Review fixes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("body", [
    b"%PDF-1.4\n" + b"stream\n" * 500_000,
    b"%PDF-1.4\n" + b"stream\nx\nendstream\n" * 200_000,
])
def test_the_page_count_is_linear_and_capped(body):
    import time

    started = time.monotonic()
    assert invoke.count_pdf_pages(body) == 0
    assert time.monotonic() - started < 1.0


async def test_a_pdf_of_many_streams_is_refused_quickly(client, monkeypatch):
    import time

    use_specialist(monkeypatch, Specialist(submits_valid))
    await make_agent()
    body = b"%PDF-1.4\n" + b"stream\nx\nendstream\n" * 200_000
    started = time.monotonic()
    out = await _refused(client, 422, **form(files=[("a.pdf", body, "application/pdf")]))
    assert out["detail"] == "The page count of a PDF could not be determined"
    assert time.monotonic() - started < 2.0


def _streamed(chunks: list[bytes], read: list[int]):
    async def gen():
        for chunk in chunks:
            read.append(len(chunk))
            yield chunk
    return gen()


async def test_the_cap_is_checked_before_the_body_is_read(client, monkeypatch):
    sp = Specialist(blocks)
    use_specialist(monkeypatch, sp)
    monkeypatch.setenv("INVOKE_MAX_CONCURRENT", "1")
    await make_agent()
    first = (await client.post(f"/api/invoke/{SLUG}", **form())).json()["run_id"]
    body = form(user=BOB)
    read: list[int] = []
    r = await client.post(f"/api/invoke/{SLUG}", headers=body["headers"],
                          content=_streamed([body["content"]], read))
    assert r.status_code == 429 and read == []
    # A repeat by the same user is refused before its body is read too.
    body = form()
    r = await client.post(f"/api/invoke/{SLUG}", headers=body["headers"],
                          content=_streamed([body["content"]], read))
    assert r.status_code == 409 and r.json()["run_id"] == first and read == []
    sp.release.set()
    await wait_done(client, first)


async def test_an_upload_in_progress_holds_the_slot(client, monkeypatch):
    use_specialist(monkeypatch, Specialist(submits_valid))
    await make_agent()
    body = form()
    gate = asyncio.Event()

    async def slow():
        yield body["content"][:20]
        await gate.wait()
        yield body["content"][20:]

    upload = asyncio.create_task(client.post(f"/api/invoke/{SLUG}", headers=body["headers"],
                                             content=slow()))
    while not invoke._active:
        await asyncio.sleep(0.01)
    r = await client.post(f"/api/invoke/{SLUG}", **form())
    assert r.status_code == 409 and r.json()["run_id"] is None
    gate.set()
    first = await upload
    assert first.status_code == 202
    await wait_done(client, first.json()["run_id"])


async def test_a_refused_upload_gives_its_slot_back(client, monkeypatch):
    use_specialist(monkeypatch, Specialist(submits_valid))
    monkeypatch.setenv("INVOKE_MAX_CONCURRENT", "1")
    await make_agent()
    await _refused(client, 415, **form(files=[("a.gif", GIF_BYTES, "image/gif")]))
    assert invoke._active == {}
    assert (await client.post(f"/api/invoke/{SLUG}?wait=5", **form())).status_code == 200


async def test_a_body_without_its_closing_boundary_is_refused(client, monkeypatch):
    sp = Specialist(submits_valid)
    use_specialist(monkeypatch, sp)
    await make_agent()
    body = form(files=[("r.png", PNG_BYTES, "image/png")])
    cut = body["content"].rsplit(b"\r\n--", 1)[0]  # the file part never ends
    out = await _refused(client, 422, content=cut, headers=body["headers"])
    assert out["detail"] == "The multipart body could not be read"
    assert sp.calls == []


async def test_a_client_that_goes_away_gets_a_fixed_answer():
    from starlette.requests import ClientDisconnect

    class Gone:
        headers = {"content-type": "multipart/form-data; boundary=zz"}

        async def stream(self):
            yield b"--zz\r\n"
            raise ClientDisconnect()

    with pytest.raises(invoke._FormRefused) as refused:
        await invoke._read_form(Gone(), invoke.InvokeLimits())
    assert (refused.value.status, refused.value.detail) == (
        400, "The request body was not received completely")


async def test_expose_invoke_is_on_only_for_the_boolean_true():
    async with SessionLocal() as s:
        row = await upsert_agent(s, name="x", description="d", instructions="i",
                                 mcp_servers=[MCP], api_slug="x", expose_invoke="false")
    assert row.expose_invoke == 0


async def test_a_nan_result_is_refused_and_the_poll_still_answers(client, monkeypatch):
    answers: list[dict] = []

    async def nan(sp, submit):
        answers.append(await submit(result={"expense_id": float("nan"), "status": "draft"}))
        answers.append(await submit(result={"expense_id": "1", "status": "draft"}))
        return "ok"

    use_specialist(monkeypatch, Specialist(nan))
    await make_agent(result_schema=None)
    r = await client.post(f"/api/invoke/{SLUG}?wait=5", **form())
    assert answers[0]["error"]["code"] == "schema_violation" and answers[1] == {"ok": True}
    assert r.status_code == 200 and r.json()["result"] == {"expense_id": "1", "status": "draft"}


async def test_a_swept_run_reads_canceled(client):
    agent = await make_agent()
    async with SessionLocal() as s:
        s.add(JobRun(id="swept", agent_id=agent.id, agent_name=agent.name, trigger="invoke",
                     status="interrupted", created_by=ALICE,
                     error="Run was interrupted by an app restart."))
        await s.commit()
    answer = (await client.get("/api/invoke/runs/swept")).json()
    assert (answer["status"], answer["error"]) == ("canceled", "canceled")


async def test_the_deadline_covers_the_preflight(client, monkeypatch):
    use_specialist(monkeypatch, Specialist(submits_valid))
    monkeypatch.setattr(invoke, "INVOKE_TIMEOUT_SECONDS", 0.05)

    async def slow_preflight(session, roots):
        await asyncio.sleep(5)

    monkeypatch.setattr(invoke, "find_credential_blocker", slow_preflight)
    await make_agent()
    r = await client.post(f"/api/invoke/{SLUG}?wait=5", **form())
    assert (r.json()["status"], r.json()["error"]) == ("failed", "timeout")


async def test_a_run_cancelled_before_it_starts_is_recorded(client, monkeypatch):
    sp = Specialist(submits_valid)
    use_specialist(monkeypatch, sp)
    await make_agent()
    real_create = asyncio.create_task
    started: list[asyncio.Task] = []

    def create_and_cancel(coro, **kw):
        task = real_create(coro, **kw)
        task.cancel()  # before its first step
        started.append(task)
        return task

    monkeypatch.setattr(invoke, "_spawn", create_and_cancel)
    r = await client.post(f"/api/invoke/{SLUG}", **form())
    answer = await wait_done(client, r.json()["run_id"])
    assert (answer["status"], answer["error"]) == ("canceled", "canceled")
    assert invoke._active == {} and sp.calls == []


async def test_the_index_on_the_audit_run_id_exists():
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DROP INDEX IF EXISTS ix_odata_audit_log_run_id")
    await init_db()
    async with engine.begin() as conn:
        names = {r[1] for r in (await conn.exec_driver_sql(
            "PRAGMA index_list(odata_audit_log)")).fetchall()}
    assert "ix_odata_audit_log_run_id" in names


class _StubValidator:
    def __init__(self, tokens):
        self.tokens = tokens

    def validate(self, token):
        from fastapi import HTTPException as H

        if token not in self.tokens:
            raise H(status_code=401, detail="Invalid token")
        return self.tokens[token]

    def has_scope(self, payload, scope):
        return scope in payload.get("scope", [])


async def test_the_scope_and_the_jwt_through_the_real_middleware(monkeypatch):
    import app as app_module
    from agents import auth

    validator = _StubValidator({
        "tok-invoker": {"user_uuid": "u-1", "user_name": "invoker", "scope": ["invoke"]},
        "tok-user": {"user_uuid": "u-2", "user_name": "chat", "scope": ["user"]},
    })
    monkeypatch.setattr(auth, "get_validator", lambda: validator)
    sp = Specialist(submits_valid)
    use_specialist(monkeypatch, sp)
    await make_agent()
    async with AsyncClient(transport=ASGITransport(app=app_module.app),
                           base_url="http://test") as c:
        body = form()
        denied = await c.post(f"/api/invoke/{SLUG}", content=body["content"], headers={
            **body["headers"], "Authorization": "Bearer tok-user"})
        assert denied.status_code == 403
        assert (await c.get("/api/invoke/runs/x", headers={
            "Authorization": "Bearer tok-user"})).status_code == 403
        ok = await c.post(f"/api/invoke/{SLUG}?wait=5", content=body["content"], headers={
            **body["headers"], "Authorization": "Bearer tok-invoker"})
        assert ok.status_code == 200 and ok.json()["status"] == "completed"
    assert sp.seen_jwt == "tok-invoker" and sp.seen_principal == "u-1"


# ---------------------------------------------------------------------------
# Re-review fixes
# ---------------------------------------------------------------------------
def scanned_pdf(pages: int, image_bytes: int, *, trailing_stream: bool = True) -> bytes:
    """A scanned receipt: plain page objects, one large Flate image per page
    (each far beyond the inflate budget together), and a content stream after
    them."""
    out = [b"%PDF-1.7\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
           b"2 0 obj << /Type /Pages /Count %d >> endobj\n" % pages]
    image = zlib.compress(b"\0" * image_bytes)
    for i in range(pages):
        out.append(b"%d 0 obj << /Type /Page /Parent 2 0 R >> endobj\n" % (10 + i))
        out.append(b"%d 0 obj << /Type /XObject /Subtype /Image /Filter /FlateDecode "
                   b"/Length %d >>\nstream\n" % (20 + i, len(image)) + image
                   + b"\nendstream\nendobj\n")
    if trailing_stream:
        out.append(b"40 0 obj << /Length 2 >>\nstream\nq\nendstream\nendobj\n")
    out.append(b"%%EOF\n")
    return b"".join(out)


def test_large_image_streams_are_not_inflated():
    import time

    body = scanned_pdf(4, 25 * 1024 * 1024)
    assert len(body) < 200 * 1024
    started = time.monotonic()
    assert invoke.count_pdf_pages(body) == 4
    assert time.monotonic() - started < 1.0


async def test_a_scanned_receipt_is_accepted(client, monkeypatch):
    sp = Specialist(submits_valid)
    use_specialist(monkeypatch, sp)
    await make_agent()
    r = await client.post(f"/api/invoke/{SLUG}?wait=5", **form(
        files=[("scan.pdf", scanned_pdf(4, 25 * 1024 * 1024), "application/pdf")]))
    assert r.status_code == 200 and r.json()["status"] == "completed"


def test_an_object_stream_over_the_budget_cannot_be_counted(monkeypatch):
    monkeypatch.setattr(invoke, "_PDF_INFLATE_BUDGET", 64)
    assert invoke.count_pdf_pages(pdf(20, compressed=True)) == 0
    monkeypatch.setattr(invoke, "_PDF_INFLATE_BUDGET", 64 * 1024 * 1024)
    assert invoke.count_pdf_pages(pdf(20, compressed=True)) == 20


async def test_a_stalled_upload_is_408_and_frees_its_slot(client, monkeypatch):
    use_specialist(monkeypatch, Specialist(submits_valid))
    monkeypatch.setenv("INVOKE_UPLOAD_TIMEOUT_SECONDS", "0.1")
    await make_agent()
    body = form()

    async def stalls():
        yield body["content"][:20]
        await asyncio.sleep(30)
        yield body["content"][20:]

    r = await client.post(f"/api/invoke/{SLUG}", headers=body["headers"], content=stalls())
    assert r.status_code == 408
    assert r.json() == {"detail": "The request body was not received in time"}
    assert invoke._active == {}


async def test_the_cancel_record_never_overwrites_an_outcome():
    agent = await make_agent()
    async with SessionLocal() as s:
        s.add(JobRun(id="done-run", agent_id=agent.id, agent_name=agent.name,
                     trigger="invoke", status="success", created_by=ALICE))
        s.add(JobRun(id="live-run", agent_id=agent.id, agent_name=agent.name,
                     trigger="invoke", status="running", created_by=ALICE))
        await s.commit()
    for run_id in ("done-run", "live-run"):
        state = invoke._RunState()
        await invoke._interrupted(run_id, state)
        assert state.finalized
    async with SessionLocal() as s:
        done = await s.get(JobRun, "done-run")
        live = await s.get(JobRun, "live-run")
    assert (done.status, done.error) == ("success", None)
    assert (live.status, live.error) == ("interrupted", "canceled: the run was cancelled")
    assert live.finished_at is not None
