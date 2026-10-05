"""Task E1: the scripted-model backend of the real-stream e2e works.

``tests/e2e/ide_stream_server.py`` starts the real app with a scripted
pydantic-ai ``FunctionModel`` in place of the IDE agents and an in-memory
ARC-1. These tests drive its script through the real ``runner.run_stage``
(no browser, no HTTP): every stage the Playwright spec (task E2) walks must
produce the events and rows the spec expects, deterministically.

They also pin that the server is test-only: nothing the app ships imports
it, and the MTA build leaves ``tests/`` out of the archive.

Run:  python -m pytest tests/test_ide_stream_server.py -q
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "e2e"))
(ROOT / "tests" / "_test_ide_stream_server.db").unlink(missing_ok=True)
os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_ide_stream_server.db'}",
)
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import ide_stream_server as server  # noqa: E402
import jwt  # noqa: E402
import pytest  # noqa: E402
import yaml  # noqa: E402
from sqlalchemy import select  # noqa: E402

from agents import auth  # noqa: E402
from agents.auth import current_jwt  # noqa: E402
from agents.db import SessionLocal, init_db  # noqa: E402
from agents.ide import approvals, arc1, runner, store, syntaxcheck  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeApproval,
    IdeArtifact,
    IdeAuditLog,
    IdeComment,
    IdeConventions,
    IdeFileRevision,
    IdeFinding,
    IdeMessage,
    IdeSession,
    IdeWorkspaceFile,
)
from agents.registry import BuildResult, registry  # noqa: E402

pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")

OWNER = server.PRINCIPAL
ALL_MODELS = (IdeFinding, IdeApproval, IdeAuditLog, IdeComment, IdeFileRevision,
              IdeWorkspaceFile, IdeArtifact, IdeMessage, IdeSession, IdeConventions)


class Events(list):
    def __call__(self, kind: str, data: dict) -> None:
        self.append((kind, data))

    def kinds(self) -> list[str]:
        return [k for k, _ in self]

    def of(self, kind: str) -> list[dict]:
        return [d for k, d in self if k == kind]

    def tools(self, name: str) -> list[dict]:
        return [e for e in self.of("tool") if e.get("tool") == name]


@pytest.fixture
async def sap(monkeypatch):
    """The server's wiring, applied through monkeypatch so it is undone."""
    for name in ("IDE_ORCHESTRATOR_AGENT", "IDE_DIAGNOSE_AGENT",
                 "IDE_SESSION_REQUEST_CAP"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(arc1.env_url_name(server.TARGET), server.ARC1_URL)
    await init_db()
    async with SessionLocal() as db:
        for model in ALL_MODELS:
            await db.execute(model.__table__.delete())
        await db.commit()
    await server.seed_conventions()
    fake = server.FakeSap()
    saved = registry._build
    server.install(fake, patch=monkeypatch.setattr)
    build = BuildResult(orchestrator=None, specialists={}, mcp_clients=[], configs=[])
    server.install_agents(build, fake)
    registry._build = build
    yield fake
    registry._build = saved
    await approvals.drain()


async def _session(stage: str = "chat", session_type: str = "change") -> str:
    async with SessionLocal() as db:
        s = await store.create_session(db, owner=OWNER, title="e2e",
                                       target=server.TARGET,
                                       session_type=session_type)
        s.stage = stage
        await db.commit()
        return s.id


async def _artifacts(sid: str, kind: str) -> list[IdeArtifact]:
    async with SessionLocal() as db:
        return await store.list_artifacts(db, sid, kind)


async def _files(sid: str) -> dict[str, IdeWorkspaceFile]:
    async with SessionLocal() as db:
        rows = (await db.execute(select(IdeWorkspaceFile).where(
            IdeWorkspaceFile.session_id == sid))).scalars().all()
        return {r.path: r for r in rows}


async def _run(sid: str, text: str | None = "go", **kw) -> Events:
    ev = Events()
    await runner.run_stage(sid, OWNER, text, emit=ev, **kw)
    assert ev.kinds()[0] == "run" and ev.kinds()[-1] == "done", ev.kinds()
    assert "error" not in ev.kinds(), ev.of("error")
    assert ev.of("done")[0]["status"] == "idle"
    return ev


# --- the script --------------------------------------------------------------


async def test_chat_run_streams_text_only(sap):
    sid = await _session("chat")
    ev = await _run(sid, "What does ZCL_DEMO_EXISTING do?")
    assert len(ev.of("text")) >= 5
    assert "".join(e["delta"] for e in ev.of("text")) == server.CHAT_ANSWER
    assert ev.of("tool") == [] and ev.of("artifact") == []


async def test_design_run_streams_and_submits_design_v1(sap):
    sid = await _session("design")
    ev = await _run(sid, "Design the change.")
    texts = [e["delta"] for e in ev.of("text")]
    assert len(texts) >= 5
    # The text streams before the document is stored, and the artifact event
    # arrives mid-run (from submit_document), before ``done``.
    kinds = ev.kinds()
    assert kinds.index("text") < kinds.index("artifact") < kinds.index("done")
    assert ev.of("artifact") == [{"id": ev.of("artifact")[0]["id"],
                                  "kind": "design", "version": 1}]
    tool = ev.tools("submit_document")
    assert tool and tool[-1]["status"] == "ok"
    (art,) = await _artifacts(sid, "design")
    assert art.version == 1 and art.content == server.DESIGN_V1
    assert server.DESIGN_V1_MARK in art.content
    # Deterministic: a second session gets exactly the same stream.
    sid2 = await _session("design")
    ev2 = await _run(sid2, "Design the change.")
    assert [e["delta"] for e in ev2.of("text")] == texts


async def test_request_changes_resolves_comments_and_submits_v2(sap):
    sid = await _session("design")
    await _run(sid, "Design the change.")
    async with SessionLocal() as db:
        comment = await store.add_comment(
            db, sid, anchor="document", kind="design", version=1, paragraph=1,
            body="Name the package of the new class.")
        await db.commit()
        cid = comment.id
    ev = await _run(sid, None, request_changes=runner.RequestChanges())
    comments = ev.of("comments")
    assert comments[0]["ids"] == [cid] and comments[0]["state"] == "sent"
    assert comments[-1]["ids"] == [cid] and comments[-1]["state"] == "addressed"
    assert ev.tools("resolve_comments")[-1]["status"] == "ok"
    assert [a["version"] for a in ev.of("artifact")] == [2]
    v2 = (await _artifacts(sid, "design"))[0]
    assert v2.version == 2 and server.DESIGN_V2_MARK in v2.content
    async with SessionLocal() as db:
        row = await db.get(IdeComment, cid)
    assert row.state == "addressed" and row.answer == server.COMMENT_ANSWER


async def test_plan_run_writes_a_plan_and_submits_it(sap):
    sid = await _session("plan")
    ev = await _run(sid, "Plan it.")
    assert ev.of("plan") and ev.of("plan")[-1]["todos"]
    assert [(a["kind"], a["version"]) for a in ev.of("artifact")] == [("plan", 1)]


async def test_propose_run_opens_writes_and_checks(sap):
    sid = await _session("propose")
    ev = await _run(sid, "Propose the change.")
    opened = ev.tools("open_object")
    assert opened and opened[-1]["status"] == "ok"
    assert [a["kind"] for a in ev.of("artifact")] == ["note"]
    # One closing file event per path, after the run-end checks.
    files = {e["path"]: e for e in ev.of("file") if e.get("syntax_status")}
    assert files[server.EXISTING_PATH] == {
        "path": server.EXISTING_PATH, "state": "modified", "revision": 1,
        "base_status": "sap", "syntax_status": "ok"}
    assert files[server.NEW_PATH] == {
        "path": server.NEW_PATH, "state": "new", "revision": 1,
        "base_status": "absent", "syntax_status": "errors"}
    for step in ("check_sap_base", "check_syntax"):
        assert ev.tools(step)[-1]["status"] == "ok", step
    rows = await _files(sid)
    existing = rows[server.EXISTING_PATH]
    assert existing.origin_source == server.EXISTING_SOURCE
    assert existing.proposed_source == server.EXISTING_CHANGED
    assert existing.origin_version == server.VERSION_MARKER
    async with SessionLocal() as db:
        revs = {r.path: r for r in (await db.execute(select(IdeFileRevision).where(
            IdeFileRevision.session_id == sid))).scalars().all()}
    items = json.loads(revs[server.NEW_PATH].syntax_json)
    assert len(items) == 1 and items[0]["severity"] == "error"
    assert items[0]["line"] == server.NEW_SOURCE.splitlines().index(
        server.SYNTAX_ERROR_LINE) + 1
    # The syntax check went out as the dry run, with each revision's source.
    dry = [a for t, a in sap.client_calls if t == "SAPDiagnose"]
    assert {a["name"] for a in dry} == {server.EXISTING, server.NEW}


async def test_propose_request_changes_fixes_the_syntax_error(sap):
    sid = await _session("propose")
    await _run(sid, "Propose the change.")
    async with SessionLocal() as db:
        comment = await store.add_comment(
            db, sid, anchor="file", path=server.NEW_PATH, revision=1,
            line_start=1, line_end=1, body="This does not compile.")
        await db.commit()
        cid = comment.id
    ev = await _run(sid, None, request_changes=runner.RequestChanges())
    last = ev.of("comments")[-1]
    assert (last["ids"], last["state"]) == ([cid], "addressed")
    new = {e["path"]: e for e in ev.of("file")}[server.NEW_PATH]
    assert (new["revision"], new["syntax_status"]) == (2, "ok")


async def test_review_run_submits_a_review(sap):
    sid = await _session("review")
    ev = await _run(sid, "Review it.")
    assert [a["kind"] for a in ev.of("artifact")] == ["review"]


async def test_diagnose_run_finds_a_dump_and_proposes_a_trace(sap):
    sid = await _session("investigate", "diagnose")
    ev = await _run(sid, "Why does the report dump?")
    assert len(ev.of("text")) >= 5
    final = {e["id"]: e for e in ev.tools("SAPDiagnose")}  # last event per call
    assert [e["status"] for e in final.values()] == ["ok", "ok", "ok"]
    assert list(final.values())[-1]["output"].startswith("Proposal stored")
    findings = ev.of("finding")
    assert findings and findings[0]["kind"] == "dump"
    assert findings[0]["ref_id"] == server.DUMP_ID
    (required,) = ev.of("approval_required")
    assert required["action"] == "trace_start"
    async with SessionLocal() as db:
        (row,) = await store.list_findings(db, sid)
        assert row.detail and server.DUMP_ERROR in row.detail
        (pending,) = await store.list_approvals(db, sid)
    assert pending.status == "pending"
    # The developer approves: the fake arms the trace as the signed-in user.
    token = current_jwt.set(server.DEV_TOKEN)
    try:
        async with SessionLocal() as db:
            decided = await approvals.decide(db, sid=sid, aid=pending.id,
                                             principal=OWNER, decision="approve")
    finally:
        current_jwt.reset(token)
    assert decided.status == "approved"
    assert json.loads(decided.result_json)["trace_request_id"] == server.TRACE_REQUEST_ID
    assert len(sap.armed) == 1 and "traceUser" not in sap.armed[0]


async def test_diagnose_report_run_submits_the_report(sap):
    sid = await _session("investigate", "diagnose")
    ev = await _run(sid, None, report=True)
    assert [a["kind"] for a in ev.of("artifact")] == ["report"]


# --- the fake ARC-1 -----------------------------------------------------------


def test_fake_syntax_answers_are_shapes_parse_syntax_recognises():
    sap = server.FakeSap()
    ok = sap.answer("SAPDiagnose", {"action": "syntax", "type": "CLAS",
                                    "name": server.EXISTING,
                                    "source": server.EXISTING_CHANGED})
    bad = sap.answer("SAPDiagnose", {"action": "syntax", "type": "CLAS",
                                     "name": server.NEW, "source": server.NEW_SOURCE})
    assert syntaxcheck.parse_syntax(ok) == ("ok", [])
    status, items = syntaxcheck.parse_syntax(bad)
    assert status == "errors" and len(items) == 1


def test_fake_missing_object_is_recognised_as_not_found():
    sap = server.FakeSap()
    with pytest.raises(arc1.Arc1Error) as caught:
        sap.answer("SAPRead", {"type": "CLAS", "name": server.NEW})
    assert arc1.is_not_found(caught.value)
    assert arc1.parse_version(sap.answer(
        "SAPRead", {"type": "VERSIONS", "objectType": "CLAS",
                    "name": server.EXISTING})) == server.VERSION_MARKER


async def test_fake_client_keeps_the_read_only_policy():
    sap = server.FakeSap()
    client = sap.client(server.TARGET, "", policy="change")
    with pytest.raises(arc1.Arc1Refused):
        await client.call("SAPWrite", {"type": "CLAS", "name": server.EXISTING})
    with pytest.raises(arc1.Arc1Error) as caught:
        await client.call("SAPDiagnose", {"action": "unittest", "type": "CLAS",
                                          "name": server.EXISTING})
    assert caught.value.status_code == 404


# --- the server around the app ------------------------------------------------


def test_configure_env_points_at_a_throwaway_sqlite_and_placeholders():
    env = {"DATABASE_URL": "postgresql+asyncpg://somewhere/db",
           "VCAP_SERVICES": "{}", "VCAP_APPLICATION": "{}"}
    server.configure_env(env)
    assert env["DATABASE_URL"].startswith("sqlite+aiosqlite:///")
    assert env["DATABASE_URL"].endswith("_e2e_stream.db")
    assert "VCAP_SERVICES" not in env and "VCAP_APPLICATION" not in env
    assert env[arc1.env_url_name(server.TARGET)] == server.ARC1_URL
    assert env["AICORE_AUTH_URL"].startswith("http://127.0.0.1:")


def test_dev_token_is_unsigned_and_names_the_local_principal():
    claims = jwt.decode(server.DEV_TOKEN, options={"verify_signature": False})
    assert auth._principal_claim(claims) == server.PRINCIPAL == "local-dev"
    assert jwt.get_unverified_header(server.DEV_TOKEN)["alg"] == "none"


async def test_dev_token_middleware_adds_a_token_only_when_missing():
    seen: list[dict] = []

    async def app(scope, receive, send):
        if scope["type"] == "http":
            seen.append(dict(scope["headers"]))

    wrapped = server.DevTokenMiddleware(app)
    await wrapped({"type": "http", "headers": [(b"host", b"x")]}, None, None)
    await wrapped({"type": "http", "headers": [(b"authorization", b"Bearer mine")]},
                  None, None)
    await wrapped({"type": "lifespan"}, None, None)
    assert seen[0][b"authorization"] == f"Bearer {server.DEV_TOKEN}".encode()
    assert seen[1][b"authorization"] == b"Bearer mine"
    assert len(seen) == 2  # the lifespan scope passes through untouched


def test_server_is_not_shipped():
    """Nothing the app ships imports the e2e server, and the MTA build
    leaves tests/ (and every *.db) out of the archive."""
    shipped = [ROOT / "app.py", *sorted((ROOT / "agents").rglob("*.py"))]
    pattern = re.compile(r"ide_stream_server|tests\.e2e|tests/e2e")
    assert [p for p in shipped if pattern.search(p.read_text(encoding="utf-8"))] == []
    mta = yaml.safe_load((ROOT / "mta.yaml").read_text())
    (module,) = [m for m in mta["modules"] if m.get("path") == "." and m["type"] == "python"]
    ignore = module["build-parameters"]["ignore"]
    assert "tests/" in ignore and "*.db" in ignore


def test_db_file_is_gitignored():
    assert server.DB_FILE.name == "_e2e_stream.db"
    lines = (ROOT / ".gitignore").read_text().splitlines()
    assert "_e2e_stream.db*" in lines
