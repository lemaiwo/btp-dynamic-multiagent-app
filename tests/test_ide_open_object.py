"""Task B10: ``open_object``, the SAP version marker and the base check.

- ``open_object(type, name, include)`` (``agents.ide.session_tools``) reads an
  object from SAP through ``arc1.get_arc1_client(target, destination,
  policy="change")`` as the signed-in user, stores ``origin_source``, the
  version marker (``arc1.read_version``), ``base_status="sap"`` and
  ``base_checked_at``, keeps an existing proposal, and puts the source into
  the scratchpad unless the path already holds a proposal. Visible only in
  a change session; an ARC-1 error is ``Error: <code>: ...`` and stores
  nothing.
- ``arc1.read_version`` parses the newest revision's ``id``/``uri``/``date``
  and is ``None`` for anything else; ``arc1.is_not_found`` recognises a
  missing object (plan A2).
- ``basecheck.check_bases`` at run end: every object row without an origin
  and never checked (at most ``BASE_CHECK_MAX``) is read from SAP: found ->
  base + ``modified`` (``read`` when equal), missing -> ``absent`` (stays
  ``new``), any other error -> ``unknown``; a call that never reached SAP
  (no user token, no configuration) leaves the row unchecked.
- The runner runs it after the workspace save of a change run and merges the
  result into the ``file`` events (one per path).

Run:  python -m pytest tests/test_ide_open_object.py -q
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
(ROOT / "tests" / "_test_ide_open_object.db").unlink(missing_ok=True)
os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_ide_open_object.db'}",
)
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402
from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart  # noqa: E402
from pydantic_ai.models.function import (  # noqa: E402
    AgentInfo,
    DeltaToolCall,
    FunctionModel,
)
from sqlalchemy import select  # noqa: E402

from agents import shared  # noqa: E402
from agents.auth import current_jwt  # noqa: E402
from agents.db import SessionLocal, init_db  # noqa: E402
from agents.deep import (  # noqa: E402
    DeepConfig,
    DeepState,
    WorkspaceScope,
    current_workspace,
    deep_toolset,
)
from agents.ide import arc1, basecheck, runner, store  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeArtifact,
    IdeComment,
    IdeConventions,
    IdeFileRevision,
    IdeMessage,
    IdeSession,
    IdeWorkspaceFile,
)
from agents.ide.session_tools import (  # noqa: E402
    IdeRunContext,
    current_ide_run,
    ide_session_toolset,
    open_object,
)
from agents.registry import BuildResult, registry  # noqa: E402

pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")

OWNER = "alice"
TARGET = "DEMO"
DEST = "arc1-abap-readonly"
PATH = "src/CLAS/zcl_x.clas.abap"
SOURCE = "CLASS zcl_x DEFINITION.\nENDCLASS."
VERSIONS = json.dumps({"revisions": [
    {"id": "rev-1", "uri": "/sap/bc/adt/oo/classes/zcl_x/versions/1",
     "date": "2026-10-02T10:00:00Z"},
    {"id": "rev-0", "uri": "/sap/bc/adt/oo/classes/zcl_x/versions/0",
     "date": "2026-09-01T10:00:00Z"},
]})


class FakeArc1:
    """A fake SAP behind ``arc1.get_arc1_client``: ``objects`` maps
    ``(type, NAME, include)`` to source, ``versions`` maps NAME to the
    VERSIONS answer, ``errors`` maps NAME to the exception a source read
    raises; anything else is a 404."""

    def __init__(self):
        self.calls: list[tuple[str, dict, str, str, str]] = []
        self.objects: dict[tuple[str, str, str | None], str] = {}
        self.versions: dict[str, str] = {}
        self.errors: dict[str, Exception] = {}
        self.delay = 0.0

    def factory(self, target: str, destination: str = "", policy: str = "change"):
        fake = self

        class _Client:
            async def call(self, tool, args):
                fake.calls.append((tool, dict(args), target, destination, policy))
                if tool == "SAPDiagnose" and args.get("action") == "syntax":
                    return "[]"  # B12: the run-end syntax dry run, no messages
                if tool != "SAPRead":
                    raise AssertionError(f"unexpected tool {tool}")
                if fake.delay:
                    await asyncio.sleep(fake.delay)
                if args.get("type") == "VERSIONS":
                    return fake.versions.get(args["name"], "")
                if args["name"] in fake.errors:
                    raise fake.errors[args["name"]]
                key = (args["type"], args["name"], args.get("include"))
                if key not in fake.objects:
                    raise arc1.Arc1Error(404, f"Object {args['name']} does not exist")
                return fake.objects[key]

        return _Client()

    def source_reads(self) -> list[dict]:
        return [a for t, a, *_ in self.calls
                if t == "SAPRead" and a.get("type") != "VERSIONS"]


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


async def _session(stage="chat", session_type="change", **values) -> str:
    async with SessionLocal() as db:
        s = await store.create_session(db, owner=OWNER, title="t", target=TARGET,
                                       session_type=session_type)
        s.stage = stage
        values.setdefault("run_id", "run-1")
        for key, value in values.items():
            setattr(s, key, value)
        await db.commit()
        return s.id


@contextmanager
def bound(sid: str, *, session_type="change", stage="chat", emit=None,
          state: DeepState | None = None):
    scope = WorkspaceScope(session_id=sid,
                           state=state or DeepState(run_id=f"ide:{sid}"),
                           session_type=session_type)
    ws = current_workspace.set(scope)
    run = current_ide_run.set(IdeRunContext(
        sid=sid, owner=OWNER, target=TARGET, destination=DEST,
        session_type=session_type, stage=stage, report=False, run_id="run-1",
        emit=emit if emit is not None else Events()))
    try:
        yield scope
    finally:
        current_ide_run.reset(run)
        current_workspace.reset(ws)


async def _files(sid: str) -> dict[str, IdeWorkspaceFile]:
    async with SessionLocal() as db:
        rows = (await db.execute(select(IdeWorkspaceFile).where(
            IdeWorkspaceFile.session_id == sid))).scalars().all()
        return {r.path: r for r in rows}


async def _visible_tools() -> list[str]:
    seen: list[list[str]] = []

    def model(messages, info: AgentInfo):
        seen.append(sorted(t.name for t in info.function_tools))
        return ModelResponse(parts=[TextPart("ok")])

    await Agent().run("hi", model=FunctionModel(model),
                      toolsets=[ide_session_toolset()])
    return seen[0]


# --- read_version / is_not_found -------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    (VERSIONS, "rev-1"),
    # newest by date even when not listed first
    (json.dumps([{"id": "old", "date": "2026-01-01T00:00:00Z"},
                 {"id": "new", "date": "2026-05-01T00:00:00Z"}]), "new"),
    # no dates: the first entry (ADT lists newest first)
    (json.dumps({"versions": [{"id": "a"}, {"id": "b"}]}), "a"),
    (json.dumps([{"uri": "/sap/bc/adt/x/versions/3"}]), "/sap/bc/adt/x/versions/3"),
    (json.dumps([{"date": "2026-10-02T10:00:00Z"}]), "2026-10-02T10:00:00Z"),
    (json.dumps([{"id": 7}]), "7"),
])
async def test_read_version_parses_the_newest_marker(fake, text, expected):
    fake.versions["ZCL_X"] = text
    client = arc1.get_arc1_client(TARGET, DEST)
    assert await arc1.read_version(client, "CLAS", "ZCL_X") == expected
    assert fake.calls[-1][:2] == (
        "SAPRead", {"type": "VERSIONS", "objectType": "CLAS", "name": "ZCL_X"})


@pytest.mark.parametrize("text", [
    "", "garbage", "* source", "[]", "{}", json.dumps({"revisions": []}),
    json.dumps([{"title": "x"}]), json.dumps([{"id": True}]),
    json.dumps([{"id": "x" * 300}]), json.dumps([{"id": "a\nb"}]),
    json.dumps([1, 2]), json.dumps({"error": "x"}),
])
async def test_read_version_garbage_is_none(fake, text):
    fake.versions["ZCL_X"] = text
    assert await arc1.read_version(arc1.get_arc1_client(TARGET), "CLAS", "ZCL_X") is None


async def test_read_version_error_is_none():
    class _Failing:
        async def call(self, tool, args):
            raise arc1.Arc1Error(502, "ARC-1 SAPRead failed: boom")

    assert await arc1.read_version(_Failing(), "CLAS", "ZCL_X") is None


@pytest.mark.parametrize("exc,expected", [
    # B10 fix round 2: a status or code alone is not enough when the message
    # is about something else; only an object-shaped message (or none at all
    # with status 404) says the object is missing.
    (arc1.Arc1Error(404, ""), True),
    (arc1.Arc1Error(404, "Class ZCL_X does not exist"), True),
    (arc1.Arc1Error(502, "Class ZCL_X does not exist", "object_not_found"), True),
    (arc1.Arc1Error(404, "x"), False),
    (arc1.Arc1Error(502, "x", "object_not_found"), False),
    (arc1.Arc1Error(502, "x", "sap_not_found"), False),
    (arc1.Arc1Error(502, "x", "not_found"), False),
    (arc1.Arc1Error(502, "ARC-1 SAPRead failed: Object ZCL_X does not exist"), True),
    (arc1.Arc1Error(502, "ARC-1 SAPRead failed: Class ZCL_X does not exist"), True),
    (arc1.Arc1Error(502, "ARC-1 SAPRead failed: Interface ZIF_Y not found"), True),
    (arc1.Arc1Error(502, "ARC-1 SAPRead failed: CLAS /NS/ZCL_X not found"), True),
    # Fix round 1: free text counts only when it is about an object.
    (arc1.Arc1Error(502, "ARC-1 SAPRead failed: HTTP 404"), False),
    (arc1.Arc1Error(502, "ARC-1 SAPRead failed: resource Not Found"), False),
    (arc1.Arc1Error(502, "ARC-1 SAPRead failed: SAP user not found"), False),
    (arc1.Arc1Error(502, "ARC-1 SAPRead failed: User DEVUSER01 does not exist"), False),
    (arc1.Arc1Error(502, "ARC-1 SAPRead failed: Object ZCL_X not found for user "
                         "DEVUSER01"), False),
    (arc1.Arc1Error(502, "ARC-1 SAPRead failed: No authorization; object "
                         "S_DEVELOP not found"), False),
    (arc1.Arc1Error(502, "ARC-1 SAPRead failed: destination arc1-abap-readonly "
                         "not found"), False),
    (arc1.Arc1Error(502, "ARC-1 SAPRead failed: tool SAPRead not found"), False),
    (arc1.Arc1Error(502, "ARC-1 SAPRead failed: token not found"), False),
    (arc1.Arc1Error(502, "ARC-1 SAPRead failed: ReadTimeout"), False),
    (arc1.Arc1Error(502, "x", "sap_authentication_failed"), False),
    (arc1.Arc1Error(502, "ARC-1 SAPRead failed at line 4040"), False),
    (arc1.Arc1UserRequired(), False),
    (arc1.Arc1NotConfigured("destination not found"), False),
    (arc1.Arc1Refused("not found in the allowlist"), False),
])
def test_is_not_found(exc, expected):
    assert arc1.is_not_found(exc) is expected


# --- open_object: visibility -----------------------------------------------------


@pytest.mark.parametrize("stage", ["chat", "design", "plan", "propose", "review"])
async def test_change_session_sees_open_object(stage):
    sid = await _session(stage)
    with bound(sid, stage=stage):
        assert "open_object" in await _visible_tools()


async def test_diagnose_session_does_not_see_open_object(fake):
    sid = await _session("investigate", session_type="diagnose")
    with bound(sid, session_type="diagnose", stage="investigate"):
        assert "open_object" not in await _visible_tools()
        out = await open_object("CLAS", "ZCL_X")
    assert out.startswith("Error:")
    assert fake.calls == [] and await _files(sid) == {}


async def test_unbound_is_refused(fake):
    assert (await open_object("CLAS", "ZCL_X")).startswith("Error:")
    assert await _visible_tools() == []
    assert fake.calls == []


# --- open_object: behaviour ------------------------------------------------------


async def test_open_reads_sap_stores_base_and_fills_the_scratchpad(fake):
    fake.objects[("CLAS", "ZCL_X", None)] = SOURCE
    fake.versions["ZCL_X"] = VERSIONS
    sid = await _session()
    ev = Events()
    with bound(sid, emit=ev) as scope:
        out = await open_object("clas", " zcl_x ")
        assert scope.state.files[PATH] == SOURCE
    assert out == f"Opened {PATH} (2 lines, version rev-1)."
    # As the signed-in user, through the target's destination, change policy.
    assert fake.calls[0] == ("SAPRead", {"type": "CLAS", "name": "ZCL_X"},
                             TARGET, DEST, "change")
    row = (await _files(sid))[PATH]
    assert (row.state, row.origin_source, row.proposed_source) == ("read", SOURCE, None)
    assert (row.origin_version, row.base_status) == ("rev-1", "sap")
    assert row.base_checked_at is not None
    assert (row.object_type, row.object_name, row.revision) == ("CLAS", "ZCL_X", 0)
    assert ev.of("file") == [{"path": PATH, "state": "read", "revision": 0,
                              "base_status": "sap", "syntax_status": None}]


async def test_open_without_marker_says_version_unknown(fake):
    fake.objects[("CLAS", "ZCL_X", None)] = SOURCE
    sid = await _session()
    with bound(sid):
        out = await open_object("CLAS", "ZCL_X")
    assert out == f"Opened {PATH} (2 lines, version unknown)."
    assert (await _files(sid))[PATH].origin_version is None


async def test_open_class_section(fake):
    fake.objects[("CLAS", "ZCL_X", "testclasses")] = "CLASS ltc DEFINITION."
    sid = await _session()
    with bound(sid) as scope:
        out = await open_object("CLAS", "ZCL_X", "testclasses")
        path = "src/CLAS/zcl_x.clas.testclasses.abap"
        assert scope.state.files[path] == "CLASS ltc DEFINITION."
    assert out.startswith(f"Opened {path} (1 lines")
    assert fake.source_reads() == [
        {"type": "CLAS", "name": "ZCL_X", "include": "testclasses"}]


async def test_open_keeps_a_stored_proposal_and_updates_only_the_base(fake):
    fake.objects[("CLAS", "ZCL_X", None)] = "fresh origin"
    fake.versions["ZCL_X"] = VERSIONS
    sid = await _session()
    async with SessionLocal() as db:
        db.add(IdeWorkspaceFile(session_id=sid, path=PATH, object_type="CLAS",
                                object_name="ZCL_X", origin_source="old origin",
                                proposed_source="my proposal", state="modified",
                                revision=2))
        await db.commit()
    state = DeepState(run_id="x")
    state.put(PATH, "my proposal")
    with bound(sid, state=state) as scope:
        out = await open_object("CLAS", "ZCL_X")
        assert scope.state.files[PATH] == "my proposal"
    assert out.startswith(f"Opened {PATH}") and "proposal" in out
    row = (await _files(sid))[PATH]
    assert (row.origin_source, row.proposed_source, row.state, row.revision) == (
        "fresh origin", "my proposal", "modified", 2)
    assert (row.origin_version, row.base_status) == ("rev-1", "sap")


async def test_open_turns_a_new_proposal_into_modified(fake):
    fake.objects[("CLAS", "ZCL_X", None)] = SOURCE
    sid = await _session()
    async with SessionLocal() as db:
        db.add(IdeWorkspaceFile(session_id=sid, path=PATH, object_type="CLAS",
                                object_name="ZCL_X", proposed_source="mine",
                                state="new", revision=1))
        await db.commit()
    state = DeepState(run_id="x")
    state.put(PATH, "mine")
    with bound(sid, state=state):
        await open_object("CLAS", "ZCL_X")
    row = (await _files(sid))[PATH]
    assert (row.state, row.origin_source, row.proposed_source) == (
        "modified", SOURCE, "mine")


async def test_open_keeps_an_unsaved_scratchpad_proposal(fake):
    """The run wrote the file before opening it: the scratchpad content is
    the proposal and is not overwritten."""
    fake.objects[("CLAS", "ZCL_X", None)] = SOURCE
    sid = await _session()
    state = DeepState(run_id="x")
    state.put(PATH, "written this run")
    with bound(sid, state=state) as scope:
        await open_object("CLAS", "ZCL_X")
        assert scope.state.files[PATH] == "written this run"
    row = (await _files(sid))[PATH]
    assert (row.origin_source, row.base_status) == (SOURCE, "sap")


async def test_open_refreshes_an_unchanged_read_copy(fake):
    fake.objects[("CLAS", "ZCL_X", None)] = "v2"
    sid = await _session()
    async with SessionLocal() as db:
        db.add(IdeWorkspaceFile(session_id=sid, path=PATH, object_type="CLAS",
                                object_name="ZCL_X", origin_source="v1",
                                state="read"))
        await db.commit()
    state = DeepState(run_id="x")
    state.put(PATH, "v1")
    with bound(sid, state=state) as scope:
        await open_object("CLAS", "ZCL_X")
        assert scope.state.files[PATH] == "v2"


async def test_arc1_error_is_reported_and_nothing_stored(fake):
    fake.errors["ZCL_X"] = arc1.Arc1Error(
        502, "ARC-1 SAPRead on DEMO failed: denied", "sap_authentication_failed")
    sid = await _session()
    with bound(sid) as scope:
        out = await open_object("CLAS", "ZCL_X")
        assert scope.state.files == {}
    assert out.startswith("Error: sap_authentication_failed:")
    assert await _files(sid) == {}


async def test_missing_object_is_reported(fake):
    sid = await _session()
    with bound(sid):
        out = await open_object("CLAS", "ZCL_NOPE")
    assert out.startswith("Error: arc1_error:") and "does not exist" in out
    assert await _files(sid) == {}


@pytest.mark.parametrize("args", [
    ("TABL", "MARA", None), ("CLAS", "", None), ("CLAS", "ZCL X", None),
    ("CLAS", "ZCL_X;DROP", None), ("CLAS", "Z" * 41, None),
    ("CLAS", "/", None), ("CLAS", "$", None), ("CLAS", "//$", None),
    ("CLAS", "ZCL_X", "../../etc"), ("PROG", "ZREPORT", "testclasses"),
    ("CLAS", "ZCL_X", "bogus"), (5, "ZCL_X", None), ("CLAS", ["ZCL_X"], None),
])
async def test_invalid_arguments_call_nothing(fake, args):
    sid = await _session()
    with bound(sid):
        out = await open_object(*args)
    assert out.startswith("Error: invalid_object:")
    assert fake.calls == [] and await _files(sid) == {}


async def test_no_user_token_on_cf_is_refused_without_a_call(monkeypatch):
    """The real client: on CF without a bound JWT the call is refused before
    any connection, and nothing is written."""
    monkeypatch.setattr(arc1, "get_arc1_client", arc1.Arc1Client)
    monkeypatch.setattr(shared, "ON_CF", True)
    built: list = []
    monkeypatch.setattr(arc1.Arc1Client, "_server",
                        lambda self: built.append(1) or None)
    token = current_jwt.set(None)
    try:
        sid = await _session()
        with bound(sid) as scope:
            out = await open_object("CLAS", "ZCL_X")
            assert scope.state.files == {}
    finally:
        current_jwt.reset(token)
    assert out.startswith("Error: user_token_required:")
    assert built == [] and await _files(sid) == {}


async def test_reaped_run_writes_nothing(fake):
    fake.objects[("CLAS", "ZCL_X", None)] = SOURCE
    sid = await _session(run_id="run-NEWER")
    with bound(sid) as scope:
        out = await open_object("CLAS", "ZCL_X")
        assert scope.state.files == {}
    assert out.startswith("Error:")
    assert await _files(sid) == {}


async def test_source_too_large_for_the_scratchpad(fake, monkeypatch):
    from agents import deep

    fake.objects[("CLAS", "ZCL_X", None)] = "x" * (deep.MAX_FILE_BYTES + 1)
    sid = await _session()
    with bound(sid):
        out = await open_object("CLAS", "ZCL_X")
    assert out.startswith("Error: source_too_large:")
    assert await _files(sid) == {}


# --- check_bases -----------------------------------------------------------------


async def _new_row(sid: str, path: str, proposed: str = "proposal", **values):
    from agents.ide.paths import object_for

    obj = object_for(path)
    async with SessionLocal() as db:
        db.add(IdeWorkspaceFile(
            session_id=sid, path=path,
            object_type=obj[0] if obj else None, object_name=obj[1] if obj else None,
            proposed_source=proposed, state=values.pop("state", "new"),
            revision=values.pop("revision", 1), **values))
        await db.commit()


async def test_check_bases_absent_present_unknown(fake):
    fake.objects[("CLAS", "ZCL_OLD", None)] = "old source"
    fake.versions["ZCL_OLD"] = VERSIONS
    fake.errors["ZCL_ERR"] = arc1.Arc1Error(502, "ARC-1 SAPRead failed: ReadTimeout")
    sid = await _session()
    await _new_row(sid, "src/CLAS/zcl_new.clas.abap")
    await _new_row(sid, "src/CLAS/zcl_old.clas.abap")
    await _new_row(sid, "src/CLAS/zcl_err.clas.abap")
    await _new_row(sid, "notes/impact.md")  # scratch: never checked
    await _new_row(sid, "src/CLAS/zcl_known.clas.abap",  # has a base already
                   origin_source="o", base_status="sap", state="modified")
    changed = await basecheck.check_bases(sid, TARGET, DEST)
    by_path = {c["path"]: c for c in changed}
    assert set(by_path) == {"src/CLAS/zcl_new.clas.abap",
                            "src/CLAS/zcl_old.clas.abap",
                            "src/CLAS/zcl_err.clas.abap"}
    assert by_path["src/CLAS/zcl_new.clas.abap"] == {
        "path": "src/CLAS/zcl_new.clas.abap", "state": "new", "revision": 1,
        "base_status": "absent"}
    assert by_path["src/CLAS/zcl_old.clas.abap"] == {
        "path": "src/CLAS/zcl_old.clas.abap", "state": "modified", "revision": 1,
        "base_status": "sap"}
    assert by_path["src/CLAS/zcl_err.clas.abap"]["base_status"] == "unknown"
    rows = await _files(sid)
    new, old, err = (rows[f"src/CLAS/zcl_{n}.clas.abap"] for n in ("new", "old", "err"))
    assert (new.state, new.base_status, new.origin_source) == ("new", "absent", None)
    assert (old.state, old.base_status, old.origin_source, old.origin_version,
            old.proposed_source) == ("modified", "sap", "old source", "rev-1",
                                     "proposal")
    assert (err.state, err.base_status, err.origin_source) == ("new", "unknown", None)
    assert all(r.base_checked_at is not None for r in (new, old, err))
    assert rows["notes/impact.md"].base_status is None
    # Each object read as the user through the destination, change policy.
    assert {c[3:] for c in fake.calls} == {(DEST, "change")}
    assert {a["name"] for a in fake.source_reads()} == {"ZCL_NEW", "ZCL_OLD", "ZCL_ERR"}


async def test_check_bases_equal_source_is_read(fake):
    fake.objects[("CLAS", "ZCL_SAME", None)] = "same"
    sid = await _session()
    await _new_row(sid, "src/CLAS/zcl_same.clas.abap", proposed="same")
    changed = await basecheck.check_bases(sid, TARGET, DEST)
    assert changed[0]["state"] == "read"
    row = (await _files(sid))["src/CLAS/zcl_same.clas.abap"]
    assert (row.state, row.proposed_source, row.origin_source) == ("read", None, "same")


async def test_check_bases_checks_at_most_the_cap(fake):
    sid = await _session()
    n = basecheck.BASE_CHECK_MAX + 5
    for i in range(n):
        await _new_row(sid, f"src/CLAS/zcl_n{i:02d}.clas.abap")
    changed = await basecheck.check_bases(sid, TARGET, DEST)
    assert len(changed) == basecheck.BASE_CHECK_MAX
    assert len(fake.source_reads()) == basecheck.BASE_CHECK_MAX
    statuses = [r.base_status for r in (await _files(sid)).values()]
    assert statuses.count("absent") == basecheck.BASE_CHECK_MAX
    assert statuses.count(None) == 5
    # The next run checks the rest.
    assert len(await basecheck.check_bases(sid, TARGET, DEST)) == 5


@pytest.mark.parametrize("exc", [
    arc1.Arc1UserRequired(), arc1.Arc1NotConfigured("no URL"),
])
async def test_check_bases_call_that_never_reached_sap_leaves_rows_unchecked(fake, exc):
    fake.errors["ZCL_A"] = exc
    fake.errors["ZCL_B"] = exc
    sid = await _session()
    await _new_row(sid, "src/CLAS/zcl_a.clas.abap")
    await _new_row(sid, "src/CLAS/zcl_b.clas.abap")
    assert await basecheck.check_bases(sid, TARGET, DEST) == []
    assert {r.base_status for r in (await _files(sid)).values()} == {None}
    # Stops after the first: every call would fail the same way.
    assert len(fake.source_reads()) == 1


async def test_check_bases_skips_a_row_that_got_a_base_meanwhile(fake):
    """The UPDATE is conditional: a row that got its base between the read
    and the write is left as it is."""
    sid = await _session()
    path = "src/CLAS/zcl_race.clas.abap"
    await _new_row(sid, path)
    fake.objects[("CLAS", "ZCL_RACE", None)] = "from sap"
    real = fake.factory

    def factory(*a, **kw):
        client = real(*a, **kw)
        call = client.call

        async def racing(tool, args):
            out = await call(tool, args)
            async with SessionLocal() as db:
                await db.execute(IdeWorkspaceFile.__table__.update().values(
                    origin_source="opened meanwhile", base_status="sap"))
                await db.commit()
            return out

        client.call = racing
        return client

    arc1.get_arc1_client = factory
    try:
        assert await basecheck.check_bases(sid, TARGET, DEST) == []
    finally:
        arc1.get_arc1_client = fake.factory
    assert (await _files(sid))[path].origin_source == "opened meanwhile"


async def test_check_bases_nothing_to_check_calls_nothing(fake):
    sid = await _session()
    assert await basecheck.check_bases(sid, TARGET, DEST) == []
    assert fake.calls == []


# --- the runner ------------------------------------------------------------------


class _Specialist:
    """The registry's agent stand-in: a scripted model with the deep toolset
    and the IDE session toolset, passed at run time."""

    def __init__(self, turns: list):
        self.turns = turns

    async def run(self, prompt, **kwargs):
        turns = self.turns

        def turn_of(messages):
            n = sum(1 for m in messages if isinstance(m, ModelResponse))
            return turns[min(n, len(turns) - 1)]

        def fn(messages, info):
            turn = turn_of(messages)
            if isinstance(turn, str):
                return ModelResponse(parts=[TextPart(turn)])
            return ModelResponse(parts=[ToolCallPart(t, a) for t, a in turn])

        async def stream(messages, info):
            # The runner streams; tool calls go out as whole deltas.
            turn = turn_of(messages)
            if isinstance(turn, str):
                yield turn
                return
            yield {i: DeltaToolCall(name=t, json_args=json.dumps(a),
                                    tool_call_id=f"call-{n_calls()}-{i}")
                   for i, (t, a) in enumerate(turn)}

        counter = iter(range(1_000_000))

        def n_calls() -> int:
            return next(counter)

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


async def test_run_end_checks_the_bases_of_written_objects(fake):
    fake.objects[("CLAS", "ZCL_OLD", None)] = "CLASS zcl_old. \" from SAP"
    fake.versions["ZCL_OLD"] = VERSIONS
    _install([
        [("write_file", {"path": "src/CLAS/zcl_new.clas.abap", "content": "new"}),
         ("write_file", {"path": "src/CLAS/zcl_old.clas.abap", "content": "changed"})],
        "done",
    ])
    sid = await _session("propose", run_id=None)
    ev = Events()
    await runner.run_stage(sid, OWNER, "go", emit=ev)
    files = {e["path"]: e for e in ev.of("file")}
    # One event per path, with the checked base.
    assert len(ev.of("file")) == 2
    assert files["src/CLAS/zcl_new.clas.abap"] == {
        "path": "src/CLAS/zcl_new.clas.abap", "state": "new", "revision": 1,
        "base_status": "absent", "syntax_status": "ok"}
    assert files["src/CLAS/zcl_old.clas.abap"] == {
        "path": "src/CLAS/zcl_old.clas.abap", "state": "modified", "revision": 1,
        "base_status": "sap", "syntax_status": "ok"}
    rows = await _files(sid)
    old = rows["src/CLAS/zcl_old.clas.abap"]
    assert (old.origin_source, old.proposed_source, old.origin_version) == (
        "CLASS zcl_old. \" from SAP", "changed", "rev-1")
    # The change session's destination reached the client.
    assert {c[3] for c in fake.calls} == {DEST}
    assert ev[-1][0] == "done"


async def test_run_end_base_check_failure_never_fails_the_run(fake, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(basecheck, "check_bases", boom)
    _install([[("write_file", {"path": "src/CLAS/zcl_new.clas.abap",
                               "content": "new"})], "done"])
    sid = await _session("propose", run_id=None)
    ev = Events()
    await runner.run_stage(sid, OWNER, "go", emit=ev)
    assert "error" not in [k for k, _ in ev]
    assert ev.of("file")[0]["base_status"] is None
    assert ev.of("done")[0]["status"] == "idle"


async def test_open_object_in_a_run(fake):
    fake.objects[("CLAS", "ZCL_X", None)] = SOURCE
    fake.versions["ZCL_X"] = VERSIONS
    _install([[("open_object", {"type": "CLAS", "name": "ZCL_X"})], "done"])
    sid = await _session("chat", run_id=None)
    ev = Events()
    await runner.run_stage(sid, OWNER, "look at ZCL_X", emit=ev)
    row = (await _files(sid))[PATH]
    assert (row.state, row.origin_source, row.origin_version, row.base_status) == (
        "read", SOURCE, "rev-1", "sap")
    tool = [e for e in ev.of("tool") if e.get("tool") == "open_object"]
    assert tool and tool[-1]["status"] == "ok"
    # The read file is not reported twice and nothing was re-checked.
    assert len(fake.source_reads()) == 1


@pytest.mark.parametrize("policy", ["change", "diagnose"])
def test_readonly_policy_allows_the_reads(policy):
    """The real client runs the read-only check first: both reads pass it."""
    from agents.ide import readonly

    assert readonly.check_call("SAPRead", {"type": "VERSIONS", "objectType": "CLAS",
                                           "name": "ZCL_X"}, policy) is None
    assert readonly.check_call("SAPRead", {"type": "CLAS", "name": "ZCL_X",
                                           "include": "testclasses"}, policy) is None


# --- fix round 1 (B10 review) ----------------------------------------------------


async def _stale(sid: str, path: str, status: str, *, checked_after: bool = False):
    """An object row with base ``status`` and one revision; the check ran
    before that revision unless ``checked_after``."""
    from datetime import timedelta

    from agents.ide.models import utcnow

    now = utcnow()
    checked = now + timedelta(seconds=5) if checked_after else now - timedelta(hours=1)
    await _new_row(sid, path, base_status=status, base_checked_at=checked)
    async with SessionLocal() as db:
        db.add(IdeFileRevision(session_id=sid, path=path, revision=1,
                               proposed_source="proposal", created_at=now))
        await db.commit()


@pytest.mark.parametrize("status", ["absent", "unknown"])
async def test_check_bases_rechecks_after_a_newer_revision(fake, status):
    fake.objects[("CLAS", "ZCL_BACK", None)] = "from sap"
    sid = await _session()
    await _stale(sid, "src/CLAS/zcl_back.clas.abap", status)
    await _stale(sid, "src/CLAS/zcl_fresh.clas.abap", status, checked_after=True)
    changed = await basecheck.check_bases(sid, TARGET, DEST)
    assert [c["path"] for c in changed] == ["src/CLAS/zcl_back.clas.abap"]
    rows = await _files(sid)
    back = rows["src/CLAS/zcl_back.clas.abap"]
    assert (back.base_status, back.origin_source, back.state) == (
        "sap", "from sap", "modified")
    # Checked after its latest revision: not asked again.
    assert rows["src/CLAS/zcl_fresh.clas.abap"].base_status == status
    assert {a["name"] for a in fake.source_reads()} == {"ZCL_BACK"}


async def test_check_bases_unchecked_rows_come_first(fake, monkeypatch):
    monkeypatch.setattr(basecheck, "BASE_CHECK_MAX", 1)
    sid = await _session()
    await _stale(sid, "src/CLAS/zcl_aa.clas.abap", "absent")
    await _new_row(sid, "src/CLAS/zcl_zz.clas.abap")
    await basecheck.check_bases(sid, TARGET, DEST)
    assert [a["name"] for a in fake.source_reads()] == ["ZCL_ZZ"]


async def test_check_bases_user_not_found_is_unknown(fake):
    fake.errors["ZCL_U"] = arc1.Arc1Error(502, "ARC-1 SAPRead failed: SAP user not found")
    sid = await _session()
    await _new_row(sid, "src/CLAS/zcl_u.clas.abap")
    changed = await basecheck.check_bases(sid, TARGET, DEST)
    assert changed[0]["base_status"] == "unknown"


@pytest.mark.parametrize("answer", [
    "", "   \n ", "Class ZCL_GHOST does not exist", "Object ZCL_GHOST not found.\n",
])
async def test_check_bases_unusable_source_is_unknown(fake, answer):
    fake.objects[("CLAS", "ZCL_GHOST", None)] = answer
    sid = await _session()
    await _new_row(sid, "src/CLAS/zcl_ghost.clas.abap")
    changed = await basecheck.check_bases(sid, TARGET, DEST)
    assert changed[0]["base_status"] == "unknown"
    row = (await _files(sid))["src/CLAS/zcl_ghost.clas.abap"]
    assert (row.origin_source, row.state) == (None, "new")
    # The marker is read only for an object that was found.
    assert all(a.get("type") != "VERSIONS" for _, a, *_ in fake.calls)


def test_a_real_source_mentioning_not_found_is_usable():
    assert basecheck.usable_source("METHOD x.\n  \" not found\nENDMETHOD.")
    assert not basecheck.usable_source("Class ZCL_X does not exist")
    assert not basecheck.usable_source("  ")
    assert not basecheck.usable_source(None)


@pytest.mark.parametrize("answer", ["", "Class ZCL_GHOST does not exist"])
async def test_open_object_unusable_source_is_an_error(fake, answer):
    fake.objects[("CLAS", "ZCL_GHOST", None)] = answer
    sid = await _session()
    with bound(sid) as scope:
        out = await open_object("CLAS", "ZCL_GHOST")
        assert scope.state.files == {}
    assert out.startswith("Error: no_source:")
    assert await _files(sid) == {}
    assert all(a.get("type") != "VERSIONS" for _, a, *_ in fake.calls)


async def test_open_object_missing_object_reads_no_marker(fake):
    sid = await _session()
    with bound(sid):
        await open_object("CLAS", "ZCL_NOPE")
    assert [a.get("type") for _, a, *_ in fake.calls] == ["CLAS"]


async def test_open_object_refuses_past_the_object_limit(fake):
    from agents.ide.session_tools import MAX_SESSION_OBJECTS

    assert MAX_SESSION_OBJECTS == 50
    fake.objects[("CLAS", "ZCL_N00", None)] = SOURCE
    sid = await _session()
    for i in range(MAX_SESSION_OBJECTS):
        await _new_row(sid, f"src/CLAS/zcl_n{i:02d}.clas.abap")
    await _new_row(sid, "notes/a.md")  # scratch notes do not count
    with bound(sid):
        out = await open_object("CLAS", "ZCL_NEW")
    assert out.startswith("Error: too_many_objects:")
    assert fake.calls == []
    # An object already in the session can still be re-opened.
    with bound(sid):
        out = await open_object("CLAS", "ZCL_N00")
    assert out.startswith("Opened ")


async def test_open_object_namespaced_name(fake):
    from agents.ide.paths import object_for, path_for

    fake.objects[("CLAS", "/NS/ZCL_X", None)] = SOURCE
    sid = await _session()
    with bound(sid):
        out = await open_object("CLAS", "/ns/zcl_x")
    path = path_for("CLAS", "/NS/ZCL_X")
    assert out.startswith(f"Opened {path} ")
    assert fake.calls[0][:2] == ("SAPRead", {"type": "CLAS", "name": "/NS/ZCL_X"})
    row = (await _files(sid))[path]
    assert (row.object_name, row.base_status) == ("/NS/ZCL_X", "sap")
    assert object_for(path)[1] == "/NS/ZCL_X"


async def test_open_object_unexpected_exception_is_a_clean_error(fake):
    fake.errors["ZCL_BOOM"] = RuntimeError("connection reset by https://host.invalid")
    sid = await _session()
    with bound(sid):
        out = await open_object("CLAS", "ZCL_BOOM")
    assert out.startswith("Error: arc1_unavailable:")
    assert "host.invalid" not in out
    assert await _files(sid) == {}


async def test_open_object_from_a_delegate(fake):
    """A delegate run inside the session run (a peer reached through a
    delegation tool) sees and can call ``open_object``."""
    fake.objects[("CLAS", "ZCL_X", None)] = SOURCE

    def scripted(turns):
        def turn_of(messages):
            n = sum(1 for m in messages if isinstance(m, ModelResponse))
            return turns[min(n, len(turns) - 1)]

        def fn(messages, info):
            turn = turn_of(messages)
            if isinstance(turn, str):
                return ModelResponse(parts=[TextPart(turn)])
            return ModelResponse(parts=[ToolCallPart(t, a) for t, a in turn])

        return FunctionModel(fn)

    peer = Agent(scripted([[("open_object", {"type": "CLAS", "name": "ZCL_X"})],
                           "opened"]), toolsets=[ide_session_toolset()])
    lead = Agent(scripted([[("ask_peer", {"question": "open ZCL_X"})], "done"]))

    @lead.tool_plain
    async def ask_peer(question: str) -> str:
        return (await peer.run(question)).output

    sid = await _session("chat")
    with bound(sid):
        result = await lead.run("go")
    assert result.output == "done"
    assert (await _files(sid))[PATH].base_status == "sap"


async def test_check_bases_budget_leaves_rows_unchecked(fake, monkeypatch):
    monkeypatch.setattr(basecheck, "BASE_CHECK_TIMEOUT_S", 0.15)
    sid = await _session()
    for i in range(4):
        await _new_row(sid, f"src/CLAS/zcl_t{i}.clas.abap")
    fake.delay = 0.1
    changed = await basecheck.check_bases(sid, TARGET, DEST)
    statuses = [r.base_status for r in (await _files(sid)).values()]
    assert len(changed) < 4
    assert statuses.count(None) == 4 - len(changed)


async def test_check_bases_reports_its_start(fake):
    sid = await _session()
    await _new_row(sid, "src/CLAS/zcl_a.clas.abap")
    await _new_row(sid, "src/CLAS/zcl_b.clas.abap")
    seen: list[int] = []
    await basecheck.check_bases(sid, TARGET, DEST, on_start=seen.append)
    assert seen == [2]
    seen.clear()
    await basecheck.check_bases(sid, TARGET, DEST, on_start=seen.append)
    assert seen == []  # nothing to check: nothing to show


async def test_run_end_checks_are_visible_as_tool_events(fake):
    _install([[("write_file", {"path": "src/CLAS/zcl_new.clas.abap",
                               "content": "new"})], "done"])
    sid = await _session("propose", run_id=None)
    ev = Events()
    await runner.run_stage(sid, OWNER, "go", emit=ev)
    tools = [e for e in ev.of("tool")
             if e.get("tool") in ("check_sap_base", "check_syntax")]
    assert [(e["tool"], e["status"]) for e in tools] == [
        ("check_sap_base", "running"), ("check_sap_base", "ok"),
        ("check_syntax", "running"), ("check_syntax", "ok")]
    assert tools[0]["detail"] == "Checking SAP base of 1 object"
    assert tools[2]["detail"] == "Checking syntax of 1 object"
    assert tools[0]["id"] == tools[1]["id"] != tools[2]["id"]
    # Before done, and stored with the message's activity.
    kinds = [k for k, _ in ev]
    assert kinds.index("tool") < kinds.index("done")
    async with SessionLocal() as db:
        msg = (await db.execute(select(IdeMessage).where(
            IdeMessage.session_id == sid, IdeMessage.role == "assistant"))).scalar_one()
    stored = [e for e in json.loads(msg.activity_json)["events"]
              if e.get("tool") in ("check_sap_base", "check_syntax")]
    assert [(e["tool"], e["status"]) for e in stored] == [
        ("check_sap_base", "ok"), ("check_syntax", "ok")]


async def test_open_object_store_failure_is_a_clean_error(fake, monkeypatch):
    """The tool's ``type`` parameter shadows the builtin: the failure log
    must not turn a DB error into a TypeError."""
    from agents.ide import session_tools

    fake.objects[("CLAS", "ZCL_X", None)] = SOURCE

    async def broken(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(session_tools, "touch_session", broken)
    sid = await _session()
    with bound(sid):
        out = await open_object("CLAS", "ZCL_X")
    assert out == "Error: the object could not be stored; try again."


# --- B10 fix round 2 ----------------------------------------------------------


@pytest.mark.parametrize("message", [
    "Target 'X' not found",
    "tool SAPRead not found",
    "user DEV not found",
    "destination arc1-abap-readonly not found",
    "User DEVUSER01 does not exist",
])
@pytest.mark.parametrize("status,code", [(404, None), (502, "not_found"),
                                         (502, "object_not_found")])
def test_not_found_code_or_status_needs_an_object_message(message, status, code):
    assert arc1.is_not_found(arc1.Arc1Error(status, message, code)) is False


@pytest.mark.parametrize("message,expected", [
    ("Method foo of class ZCL_X not found", False),
    ("Version 3 of class ZCL_X not found", False),
    ("Include ZX_TOP not found in class ZCL_X", False),
    ("ARC-1 SAPRead failed: Method FOO of class ZCL_X not found", False),
    ("Class 'ZCL_X' does not exist", True),
    ('Class "ZCL_X" not found.', True),
    ("Class ZCL_X doesn't exist", True),
    ("Interface ZIF_Y could not be found", True),
    ("ARC-1 SAPRead failed: Class ZCL_X could not be found.", True),
])
def test_not_found_message_must_name_the_object_itself(message, expected):
    assert arc1.is_not_found(arc1.Arc1Error(502, message)) is expected


@pytest.mark.parametrize("message,expected", [
    # (a) the name slot must look like an object name, not an English word
    ("Class method not found", False),
    ("Object type not found", False),
    ("Function module not found", False),
    ("Include file not found", False),
    ("ARC-1 SAPRead failed: Class method not found", False),
    ("Class zcl_x not found", True),
    ("Class 'method' not found", True),
    ("Program ZREPORT does not exist", True),
    # (b) up to three prefix segments; a leading "ADT HTTP <n>:" is stripped
    ("ARC-1 SAPRead failed: ADT HTTP 404: Class ZCL_X does not exist", True),
    ("ADT HTTP 404: Class ZCL_X does not exist", True),
    ("a: b: c: Class ZCL_X does not exist", True),
    ("a: b: c: d: Class ZCL_X does not exist", False),
    ("ARC-1 SAPRead failed: ADT HTTP 404: SAP user not found", False),
    # (c) DDIC / CDS object words
    ("Table ZT does not exist", True),
    ("Structure ZS_X not found", True),
    ("Data element ZDE_X does not exist", True),
    ("Domain ZDOM_X not found", True),
    ("Data definition ZI_X does not exist", True),
    ("View ZV_X not found", True),
    ("Behavior definition ZI_X does not exist", True),
    ("Service definition ZUI_X not found", True),
    ("Service binding ZUI_X_O4 does not exist", True),
    ("Function group ZFG not found", True),
    ("Interface ZIF_X does not exist", True),
    ("Report ZREP not found", True),
    ("Include ZX_TOP does not exist", True),
    ("TABL ZT not found", True),
    ("Table entry not found", False),
])
def test_not_found_review_minors(message, expected):
    assert arc1.is_not_found(arc1.Arc1Error(502, message)) is expected


@pytest.mark.parametrize("answer", [
    '{"error": "boom"}',
    '  [{"message": "x"}]  ',
    "Error: object could not be read",
    "error: timeout",
    '<?xml version="1.0"?><exc:exception><message>boom</message></exc:exception>',
])
def test_a_one_line_error_envelope_is_no_source(answer):
    assert basecheck.usable_source(answer) is False


@pytest.mark.parametrize("answer", [
    '<?xml version="1.0" encoding="utf-8"?>\n<root>\n  <a/>\n</root>',
    '{\n  "a": 1\n}',
    "CLASS zcl_x DEFINITION PUBLIC. ENDCLASS.",
    "{not json",
])
def test_multi_line_or_non_envelope_sources_stay_usable(answer):
    assert basecheck.usable_source(answer) is True


@pytest.mark.parametrize("answer", [
    "<html><body>Bad gateway</body></html>",
    "  <!DOCTYPE html>\n<html>\n<body>Error</body>\n</html>",
    "<H1>Service unavailable</H1>\n<p>try later</p>",
])
def test_an_error_page_is_no_source(answer):
    assert basecheck.usable_source(answer) is False


async def test_check_bases_error_page_is_unknown(fake):
    fake.objects[("CLAS", "ZCL_PAGE", None)] = "<html>\n<body>oops</body>\n</html>"
    sid = await _session()
    await _new_row(sid, "src/CLAS/zcl_page.clas.abap")
    changed = await basecheck.check_bases(sid, TARGET, DEST)
    assert changed[0]["base_status"] == "unknown"


@pytest.mark.parametrize("status", ["absent", "unknown"])
async def test_check_bases_rechecks_rows_without_a_revision(fake, status):
    """No revision row to compare with: a write to the row after the check
    makes it re-checkable; a row untouched since its check is not read
    again on every run."""
    from datetime import timedelta

    from agents.ide.models import utcnow

    fake.objects[("CLAS", "ZCL_NOREV", None)] = "from sap"
    fake.objects[("CLAS", "ZCL_QUIET", None)] = "from sap"
    sid = await _session()
    checked = utcnow() - timedelta(hours=1)
    await _new_row(sid, "src/CLAS/zcl_norev.clas.abap", revision=0,
                   base_status=status, base_checked_at=checked,
                   updated_at=checked + timedelta(minutes=5))
    await _new_row(sid, "src/CLAS/zcl_quiet.clas.abap", revision=0,
                   base_status=status, base_checked_at=checked,
                   updated_at=checked)
    changed = await basecheck.check_bases(sid, TARGET, DEST)
    assert [c["path"] for c in changed] == ["src/CLAS/zcl_norev.clas.abap"]
    assert changed[0]["base_status"] == "sap"
    assert {a["name"] for a in fake.source_reads()} == {"ZCL_NOREV"}


async def test_open_locks_the_session_before_the_file_row(fake, lock_trail):
    """The tool writes the file row and touches the session: session row
    first, as refresh/open/lint do."""
    fake.objects[("CLAS", "ZCL_X", None)] = SOURCE
    fake.versions["ZCL_X"] = VERSIONS
    sid = await _session()
    lock_trail.clear()
    with bound(sid):
        out = await open_object("CLAS", "ZCL_X")
    assert out.startswith("Opened"), out
    lock_trail.assert_lock_before_writes_to("ide_workspace_files")
