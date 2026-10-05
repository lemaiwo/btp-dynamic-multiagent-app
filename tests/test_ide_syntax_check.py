"""Task B12: the ARC-1 syntax dry run of proposals, stored per file revision.

- ``syntaxcheck.parse_syntax(text)`` fails safe: only a recognised empty
  message list (or an explicit ``ok/success/valid: true`` without messages)
  is ``ok``; a recognised list of messages is ``errors`` when one of them is
  an error, else ``ok`` with the warnings as items; anything else -- plain
  text, an unknown JSON shape, an error object -- is ``unavailable``.
- ``syntaxcheck.check_syntax`` runs ``SAPDiagnose action=syntax`` with the
  proposed ``source`` of every object file whose latest revision is not
  checked yet (at most ``SYNTAX_CHECK_MAX``), as the signed-in user through
  ``arc1.get_arc1_client(target, destination, policy="change")``, and stores
  the result on the revision. An ARC-1 error, a missing token, a timeout or
  any other failure is stored as ``unavailable`` -- never ``ok``.
- The runner runs it after the base check of a change run and merges the
  result into the run's ``file`` events (one per path); it never fails the
  run.
- ``POST /ide/api/sessions/{sid}/file/syntax?path=&revision=`` re-checks one
  revision on demand and overwrites the stored result.

The payloads under ``tests/fixtures/arc1_syntax/`` are synthetic (plan
assumption A5); replace them with the real ones from the live check.

Run:  python -m pytest tests/test_ide_syntax_check.py -q
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
(ROOT / "tests" / "_test_ide_syntax_check.db").unlink(missing_ok=True)
os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_ide_syntax_check.db'}",
)
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart  # noqa: E402
from pydantic_ai.models.function import DeltaToolCall, FunctionModel  # noqa: E402
from sqlalchemy import select  # noqa: E402

from agents.auth import require_developer  # noqa: E402
from agents.db import SessionLocal, init_db  # noqa: E402
from agents.deep import DeepConfig, deep_toolset  # noqa: E402
from agents.ide import arc1, readonly, runner, store, syntaxcheck  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeArtifact,
    IdeComment,
    IdeConventions,
    IdeFileRevision,
    IdeMessage,
    IdeSession,
    IdeWorkspaceFile,
)
from agents.ide.paths import object_for  # noqa: E402
from agents.ide.routes import router as ide_router  # noqa: E402
from agents.ide.session_tools import ide_session_toolset  # noqa: E402
from agents.registry import BuildResult, registry  # noqa: E402

pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")

OWNER = "alice"
TARGET = "DEMO"
DEST = "arc1-abap-readonly"
PATH = "src/CLAS/zcl_x.clas.abap"
FIXTURES = ROOT / "tests" / "fixtures" / "arc1_syntax"


def payload(name: str) -> str:
    """The tool text of a synthetic fixture (see its README)."""
    raw = (FIXTURES / name).read_text(encoding="utf-8")
    if name.endswith(".json"):
        data = json.loads(raw)
        assert "synthetic" in data["_note"]
        return json.dumps(data["payload"])
    note, _, text = raw.partition("---\n")
    assert "synthetic" in note
    return text


ERRORS = payload("errors.json")
OK = payload("ok_empty_list.json")


class FakeArc1:
    """SAP behind ``arc1.get_arc1_client``: a syntax call answers
    ``answers[NAME]`` (text, or an exception to raise), else ``default``;
    a source read is a 404 (the object is new)."""

    def __init__(self):
        self.calls: list[tuple[str, dict, str, str, str]] = []
        self.answers: dict[str, object] = {}
        self.default: object = OK
        self.delay = 0.0

    def factory(self, target: str, destination: str = "", policy: str = "change"):
        fake = self

        class _Client:
            async def call(self, tool, args):
                fake.calls.append((tool, dict(args), target, destination, policy))
                if tool == "SAPRead":
                    if args.get("type") == "VERSIONS":
                        return ""
                    raise arc1.Arc1Error(404, f"Object {args['name']} does not exist")
                if tool != "SAPDiagnose" or args.get("action") != "syntax":
                    raise AssertionError(f"unexpected call {tool} {args}")
                if fake.delay:
                    await asyncio.sleep(fake.delay)
                answer = fake.answers.get(args["name"], fake.default)
                if isinstance(answer, BaseException):
                    raise answer
                return answer

        return _Client()

    def syntax_calls(self) -> list[dict]:
        return [a for t, a, *_ in self.calls if t == "SAPDiagnose"]


@pytest.fixture
def fake(monkeypatch):
    f = FakeArc1()
    monkeypatch.setattr(arc1, "get_arc1_client", f.factory)
    return f


@pytest.fixture(autouse=True)
async def _clean():
    await init_db()
    async with SessionLocal() as db:
        for model in (IdeComment, IdeFileRevision, IdeWorkspaceFile, IdeArtifact,
                      IdeMessage, IdeSession, IdeConventions):
            await db.execute(model.__table__.delete())
        await db.commit()
        await store.upsert_conventions(db, TARGET, label="Demo", destination=DEST)
    saved = registry._build
    yield
    registry._build = saved


class Events(list):
    def __call__(self, kind: str, data: dict) -> None:
        self.append((kind, data))

    def of(self, kind: str) -> list[dict]:
        return [d for k, d in self if k == kind]


async def _session(stage="propose", session_type="change", owner=OWNER, **values) -> str:
    async with SessionLocal() as db:
        s = await store.create_session(db, owner=owner, title="t", target=TARGET,
                                       session_type=session_type)
        s.stage = stage
        for key, value in values.items():
            setattr(s, key, value)
        await db.commit()
        return s.id


async def _proposal(sid: str, path: str, sources: list[str], **values) -> None:
    """A workspace row with one revision per source (the last is latest)."""
    obj = object_for(path)
    async with SessionLocal() as db:
        db.add(IdeWorkspaceFile(
            session_id=sid, path=path,
            object_type=obj[0] if obj else None, object_name=obj[1] if obj else None,
            proposed_source=sources[-1] if sources else None,
            state=values.pop("state", "new"), revision=len(sources), **values))
        for n, source in enumerate(sources, start=1):
            db.add(IdeFileRevision(session_id=sid, path=path, revision=n,
                                   proposed_source=source, run_id="run-0"))
        await db.commit()


async def _revisions(sid: str) -> dict[tuple[str, int], IdeFileRevision]:
    async with SessionLocal() as db:
        rows = (await db.execute(select(IdeFileRevision).where(
            IdeFileRevision.session_id == sid))).scalars().all()
        return {(r.path, r.revision): r for r in rows}


# --- parse_syntax ------------------------------------------------------------


@pytest.mark.parametrize("name, status, items", [
    ("ok_empty_list.json", "ok", []),
    ("errors.json", "errors", [
        {"line": 12, "message": "Field \"LV_TOTAL\" is unknown.", "severity": "error"},
        {"line": 20, "message": "The variable \"LV_TMP\" is not used.",
         "severity": "warning"},
    ]),
    ("warnings_only.json", "ok", [
        {"line": 3, "message": "Obsolete statement MOVE.", "severity": "warning"},
    ]),
    ("plain_text_ok.txt", "unavailable", []),
    ("unknown_shape.json", "unavailable", []),
    ("error_object.json", "unavailable", []),
])
def test_fixture_parses_to(name, status, items):
    assert syntaxcheck.parse_syntax(payload(name)) == (status, items)


@pytest.mark.parametrize("text, status", [
    ('{"ok": true}', "ok"),
    ('{"success": true, "messages": []}', "ok"),
    ('{"valid": true}', "ok"),
    ('{"results": []}', "ok"),
    ('{"findings": [{"message": "x", "severity": "error"}]}', "errors"),
    ('{"ok": true, "errors": [{"message": "x", "severity": "E"}]}', "errors"),
    # Not positively recognised -> never ok.
    ('{"valid": false}', "unavailable"),
    ('{"ok": "yes"}', "unavailable"),
    ('{"ok": false, "messages": []}', "unavailable"),
    ('{}', "unavailable"),
    ("", "unavailable"),
    ("   ", "unavailable"),
    ("null", "unavailable"),
    ("true", "unavailable"),
    ("[1, 2]", "unavailable"),
    ('["Syntax error in line 3"]', "unavailable"),
    ('[{"line": 3}]', "unavailable"),
    ('[{"message": ""}]', "unavailable"),
    ('[{"message": "ok"}, "stray"]', "unavailable"),
    ('{"messages": "none"}', "unavailable"),
    ('{"messages": {"line": 1, "message": "x"}}', "unavailable"),
    ("No syntax errors", "unavailable"),
    ("[", "unavailable"),
])
def test_parse_syntax_fails_safe(text, status):
    assert syntaxcheck.parse_syntax(text)[0] == status


def test_parse_syntax_never_raises_on_non_text():
    for value in (None, 12, b"[]", ["x"], {"ok": True}):
        assert syntaxcheck.parse_syntax(value) == ("unavailable", [])  # type: ignore[arg-type]


@pytest.mark.parametrize("severity, expected", [
    ("E", "error"), ("error", "error"), ("Error", "error"), ("A", "error"),
    ("W", "warning"), ("warning", "warning"), ("WARNING", "warning"),
    ("I", "warning"), ("info", "warning"),
    # Unknown or missing: must not read as harmless.
    ("weird", "error"), (None, "error"), (3, "error"),
])
def test_severity_mapping(severity, expected):
    item = {"line": 1, "message": "m"}
    if severity is not None:
        item["severity"] = severity
    status, items = syntaxcheck.parse_syntax(json.dumps([item]))
    assert items[0]["severity"] == expected
    assert status == ("errors" if expected == "error" else "ok")


def test_type_is_a_severity_too():
    _, items = syntaxcheck.parse_syntax(json.dumps([{"message": "m", "type": "W"}]))
    assert items == [{"line": None, "message": "m", "severity": "warning"}]


@pytest.mark.parametrize("line, expected", [
    (7, 7), ("12", 12), (True, None), ("x", None), (-1, None), (1.5, None), (None, None),
])
def test_line(line, expected):
    _, items = syntaxcheck.parse_syntax(json.dumps([{"message": "m", "line": line,
                                                     "severity": "W"}]))
    assert items[0]["line"] == expected


def test_messages_are_cut_and_capped():
    many = [{"line": n, "message": "w" * 400, "severity": "W"} for n in range(60)]
    # The one error is past the 50 kept items: the status still says errors.
    many.append({"line": 99, "message": "boom", "severity": "E"})
    status, items = syntaxcheck.parse_syntax(json.dumps(many))
    assert status == "errors"
    assert len(items) == 50
    assert all(len(i["message"]) == 300 for i in items)


# --- check_syntax ------------------------------------------------------------


async def test_check_syntax_calls_sap_per_latest_revision_and_stores(fake):
    sid = await _session()
    await _proposal(sid, PATH, ["v1", "v2"])
    await _proposal(sid, "src/INTF/zif_y.intf.abap", ["intf"])
    await _proposal(sid, "notes/plan.md", ["a note"])
    fake.answers["ZIF_Y"] = ERRORS
    changed = await syntaxcheck.check_syntax(sid, TARGET, DEST, None)
    # One dry run per object file, on the latest revision, as the user,
    # through the change policy and the target's destination.
    assert sorted(fake.calls, key=lambda c: c[1]["name"]) == [
        ("SAPDiagnose", {"action": "syntax", "type": "CLAS", "name": "ZCL_X",
                         "source": "v2"}, TARGET, DEST, "change"),
        ("SAPDiagnose", {"action": "syntax", "type": "INTF", "name": "ZIF_Y",
                         "source": "intf"}, TARGET, DEST, "change"),
    ]
    revs = await _revisions(sid)
    assert revs[(PATH, 2)].syntax_status == "ok"
    assert revs[(PATH, 2)].syntax_checked_at is not None
    assert revs[(PATH, 1)].syntax_status is None
    assert revs[("src/INTF/zif_y.intf.abap", 1)].syntax_status == "errors"
    assert json.loads(revs[("src/INTF/zif_y.intf.abap", 1)].syntax_json)[0] == {
        "line": 12, "message": "Field \"LV_TOTAL\" is unknown.", "severity": "error"}
    assert revs[("notes/plan.md", 1)].syntax_status is None
    assert sorted(changed, key=lambda e: e["path"]) == [
        {"path": PATH, "state": "new", "revision": 2, "base_status": None,
         "syntax_status": "ok"},
        {"path": "src/INTF/zif_y.intf.abap", "state": "new", "revision": 1,
         "base_status": None, "syntax_status": "errors"},
    ]


async def test_check_syntax_emits_when_given_emit(fake):
    sid = await _session()
    await _proposal(sid, PATH, ["v1"])
    ev = Events()
    await syntaxcheck.check_syntax(sid, TARGET, DEST, ev)
    assert ev.of("file") == [{"path": PATH, "state": "new", "revision": 1,
                              "base_status": None, "syntax_status": "ok"}]


async def test_check_syntax_skips_checked_and_unproposed(fake):
    sid = await _session()
    await _proposal(sid, PATH, ["v1"])
    await _proposal(sid, "src/CLAS/zcl_read.clas.abap", [], state="read")
    await _proposal(sid, "src/CLAS/zcl_same.clas.abap", ["same"], state="read")
    async with SessionLocal() as db:
        # Base check found the proposal equal to SAP: nothing proposed.
        row = (await db.execute(select(IdeWorkspaceFile).where(
            IdeWorkspaceFile.path == "src/CLAS/zcl_same.clas.abap"))).scalar_one()
        row.proposed_source = None
        await db.commit()
    assert len(await syntaxcheck.check_syntax(sid, TARGET, DEST, None)) == 1
    # Already checked: no second call.
    assert await syntaxcheck.check_syntax(sid, TARGET, DEST, None) == []
    assert len(fake.syntax_calls()) == 1


async def test_check_syntax_paths_restrict(fake):
    sid = await _session()
    await _proposal(sid, PATH, ["v1"])
    await _proposal(sid, "src/INTF/zif_y.intf.abap", ["intf"])
    await syntaxcheck.check_syntax(sid, TARGET, DEST, None, paths=[PATH])
    assert [a["name"] for a in fake.syntax_calls()] == ["ZCL_X"]


async def test_check_syntax_at_most_the_cap(fake):
    sid = await _session()
    for n in range(syntaxcheck.SYNTAX_CHECK_MAX + 5):
        await _proposal(sid, f"src/CLAS/zcl_{n:02d}.clas.abap", ["x"])
    changed = await syntaxcheck.check_syntax(sid, TARGET, DEST, None)
    assert syntaxcheck.SYNTAX_CHECK_MAX == 20
    assert len(changed) == 20
    assert len(fake.syntax_calls()) == 20
    unchecked = [r for r in (await _revisions(sid)).values() if r.syntax_status is None]
    assert len(unchecked) == 5


@pytest.mark.parametrize("exc", [
    arc1.Arc1Error(502, "ARC-1 SAPDiagnose failed: ReadTimeout"),
    arc1.Arc1UserRequired(),
    arc1.Arc1NotConfigured("no ARC-1"),
    arc1.Arc1Refused("not allowed"),
    RuntimeError("transport broke"),
    TimeoutError(),
])
async def test_check_syntax_failure_is_stored_unavailable(fake, exc):
    sid = await _session()
    await _proposal(sid, PATH, ["v1"])
    fake.answers["ZCL_X"] = exc
    changed = await syntaxcheck.check_syntax(sid, TARGET, DEST, None)
    assert changed[0]["syntax_status"] == "unavailable"
    rev = (await _revisions(sid))[(PATH, 1)]
    assert (rev.syntax_status, json.loads(rev.syntax_json)) == ("unavailable", [])


@pytest.mark.parametrize("name", ["plain_text_ok.txt", "unknown_shape.json",
                                  "error_object.json"])
async def test_unrecognised_answer_is_stored_unavailable_never_ok(fake, name):
    sid = await _session()
    await _proposal(sid, PATH, ["v1"])
    fake.answers["ZCL_X"] = payload(name)
    await syntaxcheck.check_syntax(sid, TARGET, DEST, None)
    assert (await _revisions(sid))[(PATH, 1)].syntax_status == "unavailable"


async def test_no_user_token_stops_calling_but_marks_unavailable(fake):
    sid = await _session()
    await _proposal(sid, PATH, ["v1"])
    await _proposal(sid, "src/INTF/zif_y.intf.abap", ["intf"])
    fake.default = arc1.Arc1UserRequired()
    changed = await syntaxcheck.check_syntax(sid, TARGET, DEST, None)
    assert {e["syntax_status"] for e in changed} == {"unavailable"}
    assert len(changed) == 2
    # Every further call would fail the same way.
    assert len(fake.syntax_calls()) == 1


async def test_slow_call_is_unavailable(fake, monkeypatch):
    monkeypatch.setattr(syntaxcheck, "SYNTAX_CALL_TIMEOUT_S", 0.05)
    sid = await _session()
    await _proposal(sid, PATH, ["v1"])
    fake.delay = 1.0
    changed = await syntaxcheck.check_syntax(sid, TARGET, DEST, None)
    assert changed[0]["syntax_status"] == "unavailable"


async def test_total_budget_leaves_the_rest_unchecked(fake, monkeypatch):
    monkeypatch.setattr(syntaxcheck, "SYNTAX_CHECK_TIMEOUT_S", 0.15)
    monkeypatch.setattr(syntaxcheck, "SYNTAX_CALL_TIMEOUT_S", 5)
    sid = await _session()
    for n in range(4):
        await _proposal(sid, f"src/CLAS/zcl_{n}.clas.abap", ["x"])
    fake.delay = 0.1
    changed = await syntaxcheck.check_syntax(sid, TARGET, DEST, None)
    statuses = [r.syntax_status for r in (await _revisions(sid)).values()]
    # Cut off by the budget: never stored as unavailable or ok.
    assert len(changed) < 4
    assert statuses.count(None) == 4 - len(changed)
    assert "unavailable" not in statuses


def test_readonly_policy_allows_the_dry_run():
    args = {"action": "syntax", "type": "CLAS", "name": "ZCL_X", "source": "x"}
    assert readonly.check_call("SAPDiagnose", args, "change") is None
    assert not readonly.needs_approval("SAPDiagnose", args, "change")


# --- the runner ---------------------------------------------------------------


class _Specialist:
    """A scripted model with the deep toolset, passed at run time."""

    def __init__(self, turns: list):
        self.turns = turns

    async def run(self, prompt, **kwargs):
        turns = self.turns
        counter = iter(range(1_000_000))

        def turn_of(messages):
            n = sum(1 for m in messages if isinstance(m, ModelResponse))
            return turns[min(n, len(turns) - 1)]

        def fn(messages, info):
            turn = turn_of(messages)
            if isinstance(turn, str):
                return ModelResponse(parts=[TextPart(turn)])
            return ModelResponse(parts=[ToolCallPart(t, a) for t, a in turn])

        async def stream(messages, info):
            turn = turn_of(messages)
            if isinstance(turn, str):
                yield turn
                return
            yield {i: DeltaToolCall(name=t, json_args=json.dumps(a),
                                    tool_call_id=f"call-{next(counter)}-{i}")
                   for i, (t, a) in enumerate(turn)}

        model = FunctionModel(fn, stream_function=stream)
        ts = deep_toolset(DeepConfig(enabled=True, subagent_max_depth=1),
                          parent_toolsets=[], model=model,
                          agent_name="abap-orchestrator")
        return await Agent().run(prompt, model=model,
                                 toolsets=[ide_session_toolset(), ts], **kwargs)


def _install(turns: list) -> None:
    registry._build = BuildResult(
        orchestrator=None, specialists={"abap-orchestrator": _Specialist(turns)},
        mcp_clients=[], configs=[])


def _writes(**files: str) -> list:
    return [[("write_file", {"path": p, "content": c}) for p, c in files.items()],
            "done"]


async def test_run_checks_each_changed_object_once(fake):
    fake.answers["ZIF_Y"] = ERRORS
    _install(_writes(**{PATH: "class v1", "src/INTF/zif_y.intf.abap": "intf v1",
                        "notes/plan.md": "a note"}))
    sid = await _session()
    ev = Events()
    await runner.run_stage(sid, OWNER, "go", emit=ev)
    assert sorted(a["name"] for a in fake.syntax_calls()) == ["ZCL_X", "ZIF_Y"]
    assert {a["source"] for a in fake.syntax_calls()} == {"class v1", "intf v1"}
    files = {e["path"]: e for e in ev.of("file")}
    # One event per path, with the stored status; the note is not checked.
    assert len(ev.of("file")) == 3
    assert files[PATH]["syntax_status"] == "ok"
    assert files["src/INTF/zif_y.intf.abap"]["syntax_status"] == "errors"
    assert files["src/INTF/zif_y.intf.abap"]["base_status"] == "absent"
    assert files["notes/plan.md"]["syntax_status"] is None
    revs = await _revisions(sid)
    assert revs[(PATH, 1)].syntax_status == "ok"
    assert revs[("src/INTF/zif_y.intf.abap", 1)].syntax_status == "errors"
    assert ev[-1][0] == "done"

    # A second run without changes: nothing new to check.
    calls = len(fake.syntax_calls())
    _install(["nothing to change"])
    ev2 = Events()
    await runner.run_stage(sid, OWNER, "again", emit=ev2)
    assert len(fake.syntax_calls()) == calls
    assert ev2.of("done")[0]["status"] == "idle"

    # A changed file: its new revision is checked, the old one keeps its result.
    fake.answers["ZCL_X"] = ERRORS
    _install(_writes(**{PATH: "class v2"}))
    ev3 = Events()
    await runner.run_stage(sid, OWNER, "change it", emit=ev3)
    assert fake.syntax_calls()[-1]["source"] == "class v2"
    assert len(fake.syntax_calls()) == calls + 1
    revs = await _revisions(sid)
    assert revs[(PATH, 1)].syntax_status == "ok"
    assert revs[(PATH, 2)].syntax_status == "errors"
    assert ev3.of("file") == [{"path": PATH, "state": "new", "revision": 2,
                               "base_status": "absent", "syntax_status": "errors"}]


async def test_run_end_syntax_failure_never_fails_the_run(fake, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(syntaxcheck, "check_syntax", boom)
    _install(_writes(**{PATH: "class v1"}))
    sid = await _session()
    ev = Events()
    await runner.run_stage(sid, OWNER, "go", emit=ev)
    assert "error" not in [k for k, _ in ev]
    assert ev.of("file")[0]["syntax_status"] is None
    assert ev.of("done")[0]["status"] == "idle"
    assert ev[-1][0] == "done"


async def test_failed_run_does_not_check(fake):
    class _Failing:
        async def run(self, prompt, **kwargs):
            raise RuntimeError("model down")

    registry._build = BuildResult(orchestrator=None,
                                  specialists={"abap-orchestrator": _Failing()},
                                  mcp_clients=[], configs=[])
    sid = await _session()
    await _proposal(sid, PATH, ["v1"])
    ev = Events()
    await runner.run_stage(sid, OWNER, "go", emit=ev)
    assert ev.of("error")
    assert fake.syntax_calls() == []


# --- POST /sessions/{sid}/file/syntax --------------------------------------------

USERS = {
    "alice": {"user_name": "alice", "scope": ["developer"]},
    "bob": {"user_name": "bob", "scope": ["developer"]},
}


def _fake_developer(request: Request) -> dict:
    user = request.headers.get("x-test-user", "")
    if user not in USERS:
        raise HTTPException(status_code=403, detail="Developer scope required")
    return USERS[user]


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(ide_router)
    app.dependency_overrides[require_developer] = _fake_developer
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


def _syntax(client, sid, path=PATH, revision=None, user="alice"):
    params = {"path": path}
    if revision is not None:
        params["revision"] = revision
    return client.post(f"/ide/api/sessions/{sid}/file/syntax", params=params,
                       headers={"x-test-user": user})


async def test_route_rechecks_and_overwrites(client, fake):
    sid = await _session()
    await _proposal(sid, PATH, ["v1", "v2"])
    await syntaxcheck.check_syntax(sid, TARGET, DEST, None)
    assert (await _revisions(sid))[(PATH, 2)].syntax_status == "ok"

    fake.answers["ZCL_X"] = ERRORS
    r = await _syntax(client, sid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["path"], body["revision"], body["status"]) == (PATH, 2, "errors")
    assert body["items"][0] == {"line": 12, "message": "Field \"LV_TOTAL\" is unknown.",
                                "severity": "error"}
    assert body["checked_at"]
    assert fake.syntax_calls()[-1] == {"action": "syntax", "type": "CLAS",
                                       "name": "ZCL_X", "source": "v2"}
    assert fake.calls[-1][2:] == (TARGET, DEST, "change")
    assert (await _revisions(sid))[(PATH, 2)].syntax_status == "errors"

    # An earlier revision on demand; the latest keeps its result.
    fake.answers["ZCL_X"] = OK
    r = await _syntax(client, sid, revision="1")
    assert (r.json()["revision"], r.json()["status"]) == (1, "ok")
    assert fake.syntax_calls()[-1]["source"] == "v1"
    revs = await _revisions(sid)
    assert (revs[(PATH, 1)].syntax_status, revs[(PATH, 2)].syntax_status) == (
        "ok", "errors")

    # The file detail serves the stored result.
    r = await client.get(f"/ide/api/sessions/{sid}/file", params={"path": PATH},
                         headers={"x-test-user": "alice"})
    assert r.json()["syntax_status"] == "errors"
    assert r.json()["syntax"][0]["severity"] == "error"


@pytest.mark.parametrize("answer", [
    arc1.Arc1Error(502, "ARC-1 SAPDiagnose failed: ReadTimeout"),
    arc1.Arc1UserRequired(),
    "No syntax errors found.",
])
async def test_route_failure_is_unavailable_200(client, fake, answer):
    sid = await _session()
    await _proposal(sid, PATH, ["v1"])
    fake.answers["ZCL_X"] = answer
    r = await _syntax(client, sid)
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["items"]) == ("unavailable", [])
    assert (await _revisions(sid))[(PATH, 1)].syntax_status == "unavailable"


async def test_route_another_owner_is_404(client, fake):
    sid = await _session(owner="bob")
    await _proposal(sid, PATH, ["v1"])
    r = await _syntax(client, sid)
    assert r.status_code == 404
    assert fake.calls == []


async def test_route_scratch_path_is_not_an_object(client, fake):
    sid = await _session()
    await _proposal(sid, "notes/plan.md", ["a note"])
    r = await _syntax(client, sid, path="notes/plan.md")
    assert r.status_code == 422
    assert r.json()["code"] == "not_an_object"
    assert fake.calls == []


async def test_route_running_session_is_409(client, fake):
    sid = await _session(status="running", run_id="run-9")
    await _proposal(sid, PATH, ["v1"])
    r = await _syntax(client, sid)
    assert r.status_code == 409
    assert r.json()["code"] == "run_in_progress"
    assert fake.calls == []


async def test_route_no_proposal_and_unknown_revision(client, fake):
    sid = await _session()
    await _proposal(sid, PATH, [], state="read")
    r = await _syntax(client, sid)
    assert (r.status_code, r.json()["code"]) == (422, "no_proposal")
    await _proposal(sid, "src/INTF/zif_y.intf.abap", ["intf"])
    for bad in ("0", "2", "-1", "abc"):
        r = await _syntax(client, sid, path="src/INTF/zif_y.intf.abap", revision=bad)
        assert (r.status_code, r.json()["code"]) == (404, "unknown_revision"), bad
    r = await _syntax(client, sid, path="src/CLAS/zcl_missing.clas.abap")
    assert r.status_code == 404
    r = await _syntax(client, sid, path="../etc/passwd")
    assert r.status_code == 422
    assert fake.calls == []


async def test_route_lost_flag_diagnose_is_409(client, fake):
    sid = await _session(stage="investigate", session_type="diagnose")
    await _proposal(sid, PATH, ["v1"])
    r = await _syntax(client, sid)
    assert r.status_code == 409
    assert r.json()["code"] == "target_not_non_production"
    assert fake.calls == []


# --- B12 review, fix round 1 -------------------------------------------------


@pytest.mark.parametrize("text, status", [
    # A list next to anything the parser does not know is not an "all clear".
    ('{"error": "boom", "messages": []}', "unavailable"),
    ('{"status": "failed", "messages": []}', "unavailable"),
    ('{"messages": [], "truncated": true}', "unavailable"),
    ('{"messages": [], "isError": true}', "unavailable"),
    ('{"messages": [], "timedOut": true, "message": "timeout"}', "unavailable"),
    # An ok flag counts only when it is exactly true.
    ('{"messages": [], "success": "false"}', "unavailable"),
    ('{"messages": [], "ok": 1}', "unavailable"),
    ('{"ok": true, "error": "x"}', "unavailable"),
    # Several lists: all of them count.
    ('{"messages": [], "errors": [{"message": "x", "severity": "E"}]}', "errors"),
    ('{"messages": [{"message": "w", "severity": "W"}], "findings": []}', "ok"),
    ('{"messages": [], "errors": "none"}', "unavailable"),
    # Known benign keys do not spoil a recognised answer.
    ('{"objectName": "ZCL_X", "messages": []}', "ok"),
])
def test_parse_syntax_positive_recognition_only(text, status):
    assert syntaxcheck.parse_syntax(text)[0] == status


def test_merged_lists_keep_every_item():
    status, items = syntaxcheck.parse_syntax(json.dumps({
        "messages": [{"message": "w", "severity": "W", "line": 1}],
        "errors": [{"message": "e", "severity": "E", "line": 2}],
    }))
    assert status == "errors"
    assert [i["message"] for i in items] == ["w", "e"]


@pytest.mark.parametrize("line", ["²", "٣", "１", " 1 2 "])
def test_line_non_ascii_digits_are_no_line(line):
    status, items = syntaxcheck.parse_syntax(json.dumps(
        [{"message": "m", "line": line, "severity": "E"}]))
    assert (status, items[0]["line"]) == ("errors", None)


async def test_parse_failure_is_unavailable_for_that_file_only(fake, monkeypatch):
    real = syntaxcheck.parse_syntax

    def flaky(text):
        if text == "boom":
            raise ValueError("cannot parse")
        return real(text)

    monkeypatch.setattr(syntaxcheck, "parse_syntax", flaky)
    fake.answers["ZCL_X"] = "boom"
    sid = await _session()
    await _proposal(sid, PATH, ["class v1"])
    await _proposal(sid, "src/INTF/zif_y.intf.abap", ["intf v1"])
    out = await syntaxcheck.check_syntax(sid, TARGET, DEST, None)
    assert {e["path"]: e["syntax_status"] for e in out} == {
        PATH: "unavailable", "src/INTF/zif_y.intf.abap": "ok"}
    revs = await _revisions(sid)
    assert revs[(PATH, 1)].syntax_status == "unavailable"
    assert revs[("src/INTF/zif_y.intf.abap", 1)].syntax_status == "ok"


async def test_superscript_line_in_a_run_answer_is_stored(fake):
    fake.answers["ZCL_X"] = json.dumps([{"message": "m", "line": "²",
                                         "severity": "E"}])
    sid = await _session()
    await _proposal(sid, PATH, ["class v1"])
    out = await syntaxcheck.check_syntax(sid, TARGET, DEST, None)
    assert out[0]["syntax_status"] == "errors"


async def test_route_parse_failure_is_unavailable_200(client, fake, monkeypatch):
    def broken(text):
        raise ValueError("cannot parse")

    monkeypatch.setattr(syntaxcheck, "parse_syntax", broken)
    sid = await _session()
    await _proposal(sid, PATH, ["v1"])
    r = await _syntax(client, sid)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "unavailable"


async def test_route_superscript_line_is_200(client, fake):
    fake.answers["ZCL_X"] = json.dumps([{"message": "m", "line": "²",
                                         "severity": "E"}])
    sid = await _session()
    await _proposal(sid, PATH, ["v1"])
    r = await _syntax(client, sid)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "errors"
    assert r.json()["items"][0]["line"] is None


@pytest.mark.parametrize("source", ["", "   \n\t "])
async def test_empty_source_is_unavailable_without_a_call(fake, source):
    sid = await _session()
    await _proposal(sid, PATH, [source])
    out = await syntaxcheck.check_syntax(sid, TARGET, DEST, None)
    assert fake.syntax_calls() == []
    assert out[0]["syntax_status"] == "unavailable"
    assert (await _revisions(sid))[(PATH, 1)].syntax_status == "unavailable"


async def test_route_empty_source_is_unavailable_without_a_call(client, fake):
    sid = await _session()
    await _proposal(sid, PATH, ["  "])
    r = await _syntax(client, sid)
    assert (r.status_code, r.json()["status"]) == (200, "unavailable")
    assert fake.syntax_calls() == []


async def test_route_concurrent_check_is_409(client, fake):
    sid = await _session()
    await _proposal(sid, PATH, ["v1"])
    fake.delay = 0.3
    first = asyncio.create_task(_syntax(client, sid))
    while not fake.syntax_calls():
        await asyncio.sleep(0.01)
    r = await _syntax(client, sid)
    assert (r.status_code, r.json()["code"]) == (409, "syntax_check_running")
    assert (await first).status_code == 200
    # The guard is released afterwards.
    fake.delay = 0
    r = await _syntax(client, sid)
    assert r.status_code == 200
