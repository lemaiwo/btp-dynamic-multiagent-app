"""Refreshing the IDE seed in a database that already holds it (decision D7).

``ensure_ide_seed`` only inserts names that do not exist yet, so a landscape
seeded with version 1 keeps the version-1 prompts forever. ``refresh_ide_seed``
brings unedited rows up to the shipped text: a row whose text hashes to a
known earlier seed version (``agents/ide/seed_history.json``) is updated, a
row an admin edited is reported and left alone, a missing name is added.
``POST /ide/api/admin/seed/refresh`` is the admin's way to run it.

Run:  python -m pytest tests/test_ide_seed_refresh.py -q
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
(ROOT / "tests" / "_test_ide_seed_refresh.db").unlink(missing_ok=True)
os.environ.setdefault(
    "DATABASE_URL", f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_ide_seed_refresh.db'}"
)
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import delete  # noqa: E402

from agents import chat_app as chat_app_module  # noqa: E402
from agents import registry as registry_module  # noqa: E402
from agents.auth import require_admin, require_developer  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    SessionLocal,
    SkillConfig,
    get_agent_by_name,
    get_skill_by_name,
    init_db,
)
from agents.ide import seed as seed_module  # noqa: E402
from agents.ide.routes import router as ide_router  # noqa: E402
from agents.ide.seed import ensure_ide_seed, refresh_ide_seed  # noqa: E402

SHIPPED = ROOT / "agents" / "ide" / "seed.ide.json"
HISTORY = ROOT / "agents" / "ide" / "seed_history.json"
V1 = ROOT / "tests" / "fixtures" / "ide_seed" / "seed.ide.v1.json"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _names(data: dict) -> tuple[list[str], list[str]]:
    return [a["name"] for a in data["agents"]], [s["name"] for s in data["skills"]]


SHIPPED_DATA = json.loads(SHIPPED.read_text())
V1_DATA = json.loads(V1.read_text())
AGENTS, SKILLS = _names(SHIPPED_DATA)
# Every name a test here touches, removed before each test (the DB may be
# shared with other modules in one pytest process).
TMP_AGENTS = ["seedref-ag-a", "seedref-ag-b"]
TMP_SKILLS = ["seedref-sk-a"]


@pytest.fixture(autouse=True)
async def _db():
    await init_db()
    async with SessionLocal() as s:
        await s.execute(delete(AgentConfig).where(AgentConfig.name.in_(AGENTS + TMP_AGENTS)))
        await s.execute(delete(SkillConfig).where(SkillConfig.name.in_(SKILLS + TMP_SKILLS)))
        await s.commit()


def _agent(name: str, instructions: str) -> dict:
    return {
        "name": name, "description": "d", "instructions": instructions,
        "mcp_servers": [{"url": "http://example.invalid/mcp", "auth_mode": "none"}],
    }


def _write_seed(directory: Path, version: int, agents: dict[str, str],
                skills: dict[str, str], history: dict | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "seed.ide.json"
    path.write_text(json.dumps({
        "version": version,
        "skills": [{"name": n, "description": "d", "content": c} for n, c in skills.items()],
        "agents": [_agent(n, i) for n, i in agents.items()],
    }))
    if history is not None:
        (directory / "seed_history.json").write_text(json.dumps(history))
    return path


async def _instructions(name: str) -> str | None:
    async with SessionLocal() as s:
        row = await get_agent_by_name(s, name)
        return None if row is None else row.instructions


async def _content(name: str) -> str | None:
    async with SessionLocal() as s:
        row = await get_skill_by_name(s, name)
        return None if row is None else row.content


# --- the shipped history ------------------------------------------------------


def test_history_covers_every_seed_name_with_hashes():
    history = json.loads(HISTORY.read_text())
    assert set(history) == {"agents", "skills"}
    assert sorted(history["agents"]) == sorted(AGENTS)
    assert sorted(history["skills"]) == sorted(SKILLS)
    for kind in ("agents", "skills"):
        for name, hashes in history[kind].items():
            assert hashes and all(re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes), name


def test_history_holds_the_version_1_texts():
    """The fixture is ``git show 30be3f4:agents/ide/seed.ide.json``: the
    texts every landscape seeded before version 2 holds."""
    assert V1_DATA["version"] == 1
    history = json.loads(HISTORY.read_text())
    for a in V1_DATA["agents"]:
        assert sha(a["instructions"]) in history["agents"][a["name"]], a["name"]
    for s in V1_DATA["skills"]:
        assert sha(s["content"]) in history["skills"][s["name"]], s["name"]


def test_history_holds_the_shipped_texts():
    """Change a seed text, and this fails until
    ``scripts/ide_seed_history.py`` adds the new hash: the next version can
    then recognise this one as an unedited seed."""
    history = json.loads(HISTORY.read_text())
    for a in SHIPPED_DATA["agents"]:
        assert sha(a["instructions"]) in history["agents"][a["name"]], (
            f"{a['name']}: run .venv/bin/python scripts/ide_seed_history.py")
    for s in SHIPPED_DATA["skills"]:
        assert sha(s["content"]) in history["skills"][s["name"]], (
            f"{s['name']}: run .venv/bin/python scripts/ide_seed_history.py")


def test_shipped_seed_is_version_2():
    assert SHIPPED_DATA["version"] == 2


# --- refresh_ide_seed ---------------------------------------------------------


async def test_version_1_rows_are_updated_to_the_shipped_text():
    assert await ensure_ide_seed(V1) == {"skills_added": 8, "agents_added": 5}
    result = await refresh_ide_seed(SHIPPED)
    changed_agents = sorted(
        a["name"] for a in SHIPPED_DATA["agents"]
        if a["instructions"] != next(
            v["instructions"] for v in V1_DATA["agents"] if v["name"] == a["name"])
    )
    assert changed_agents, "version 2 must change agent prompts"
    for name in ("abap-orchestrator", "abap-developer", "abap-reviewer",
                 "abap-diagnostics"):
        assert f"agent:{name}" in result["updated"], name
    assert result["skipped_edited"] == [] and result["added"] == []
    for a in SHIPPED_DATA["agents"]:
        assert await _instructions(a["name"]) == a["instructions"], a["name"]
    for s in SHIPPED_DATA["skills"]:
        assert await _content(s["name"]) == s["content"], s["name"]
    # Unchanged texts are not reported as updated.
    assert len(result["updated"]) == len(set(result["updated"]))
    for a in SHIPPED_DATA["agents"]:
        if a["name"] not in changed_agents:
            assert f"agent:{a['name']}" not in result["updated"], a["name"]
    # A second call has nothing left to do.
    assert await refresh_ide_seed(SHIPPED) == {
        "updated": [], "skipped_edited": [], "added": []}


async def test_admin_edited_row_is_skipped_and_kept(tmp_path):
    history = {"agents": {"seedref-ag-a": [sha("v1 a")], "seedref-ag-b": [sha("v1 b")]},
               "skills": {"seedref-sk-a": [sha("v1 skill")]}}
    v1 = _write_seed(tmp_path / "v1", 1, {"seedref-ag-a": "v1 a", "seedref-ag-b": "v1 b"},
                     {"seedref-sk-a": "v1 skill"})
    await ensure_ide_seed(v1)
    async with SessionLocal() as s:
        row = await get_agent_by_name(s, "seedref-ag-b")
        row.instructions = "edited by an admin"
        sk = await get_skill_by_name(s, "seedref-sk-a")
        sk.content = "edited skill"
        await s.commit()
    v2 = _write_seed(tmp_path / "v2", 2, {"seedref-ag-a": "v2 a", "seedref-ag-b": "v2 b"},
                     {"seedref-sk-a": "v2 skill"}, history)
    result = await refresh_ide_seed(v2)
    # Skills first, then agents, as the seed file is applied.
    assert result == {"updated": ["agent:seedref-ag-a"],
                      "skipped_edited": ["skill:seedref-sk-a", "agent:seedref-ag-b"],
                      "added": []}
    assert await _instructions("seedref-ag-a") == "v2 a"
    assert await _instructions("seedref-ag-b") == "edited by an admin"
    assert await _content("seedref-sk-a") == "edited skill"


async def test_missing_rows_are_added(tmp_path):
    v2 = _write_seed(tmp_path, 2, {"seedref-ag-a": "v2 a"}, {"seedref-sk-a": "v2 skill"},
                     {"agents": {}, "skills": {}})
    result = await refresh_ide_seed(v2)
    assert result == {"updated": [], "skipped_edited": [],
                      "added": ["skill:seedref-sk-a", "agent:seedref-ag-a"]}
    assert await _instructions("seedref-ag-a") == "v2 a"
    assert await _content("seedref-sk-a") == "v2 skill"


async def test_refresh_changes_only_the_text(tmp_path):
    history = {"agents": {"seedref-ag-a": [sha("v1 a")]}, "skills": {}}
    await ensure_ide_seed(_write_seed(tmp_path / "v1", 1, {"seedref-ag-a": "v1 a"}, {}))
    async with SessionLocal() as s:
        row = await get_agent_by_name(s, "seedref-ag-a")
        row.description = "admin description"
        row.enabled = False
        await s.commit()
    await refresh_ide_seed(_write_seed(tmp_path / "v2", 2, {"seedref-ag-a": "v2 a"}, {},
                                       history))
    async with SessionLocal() as s:
        row = await get_agent_by_name(s, "seedref-ag-a")
        assert (row.instructions, row.description, row.enabled) == (
            "v2 a", "admin description", False)


async def test_refresh_ignores_the_startup_switch(tmp_path, monkeypatch):
    """``IDE_SEED=false`` keeps the startup insert off; an admin who calls
    the refresh asks for it explicitly."""
    monkeypatch.setenv("IDE_SEED", "false")
    v2 = _write_seed(tmp_path, 2, {"seedref-ag-a": "v2 a"}, {}, {"agents": {}, "skills": {}})
    assert (await refresh_ide_seed(v2))["added"] == ["agent:seedref-ag-a"]


async def test_unreadable_seed_or_history_changes_nothing(tmp_path):
    bad = tmp_path / "bad" / "seed.ide.json"
    bad.parent.mkdir()
    bad.write_text("{")
    with pytest.raises(ValueError):
        await refresh_ide_seed(bad)
    v2 = _write_seed(tmp_path / "nohist", 2, {"seedref-ag-a": "v2 a"}, {})
    (v2.parent / "seed_history.json").write_text("[1]")
    with pytest.raises(ValueError):
        await refresh_ide_seed(v2)
    assert await _instructions("seedref-ag-a") is None


async def test_text_changed_meanwhile_is_skipped(tmp_path, monkeypatch):
    """The update is conditional on the text it read: an admin save between
    the read and the write wins."""
    history = {"agents": {"seedref-ag-a": [sha("v1 a")]}, "skills": {}}
    await ensure_ide_seed(_write_seed(tmp_path / "v1", 1, {"seedref-ag-a": "v1 a"}, {}))
    real = seed_module._stored_texts

    async def racing(*args, **kwargs):
        texts = await real(*args, **kwargs)
        async with SessionLocal() as s:
            row = await get_agent_by_name(s, "seedref-ag-a")
            row.instructions = "saved by an admin meanwhile"
            await s.commit()
        return texts

    monkeypatch.setattr(seed_module, "_stored_texts", racing)
    result = await refresh_ide_seed(
        _write_seed(tmp_path / "v2", 2, {"seedref-ag-a": "v2 a"}, {}, history))
    assert result["skipped_edited"] == ["agent:seedref-ag-a"]
    assert await _instructions("seedref-ag-a") == "saved by an admin meanwhile"


# --- POST /ide/api/admin/seed/refresh -----------------------------------------

USERS = {
    "dev": {"user_name": "DEVUSER01", "scope": ["developer"]},
    "admin": {"user_name": "jane.doe@example.com", "scope": ["developer", "admin"]},
}


def _fake_developer(request: Request) -> dict:
    user = request.headers.get("x-test-user", "")
    if user not in USERS:
        raise HTTPException(status_code=403, detail="Developer scope required")
    return USERS[user]


def _fake_admin(request: Request) -> dict:
    claims = _fake_developer(request)
    if "admin" not in claims["scope"]:
        raise HTTPException(status_code=403, detail="Admin scope required")
    return claims


@pytest.fixture
def reloads(monkeypatch):
    calls: list[str] = []

    async def reload():
        calls.append("registry")

    monkeypatch.setattr(registry_module.registry, "reload", reload)
    monkeypatch.setattr(chat_app_module.dynamic_chat_app, "refresh",
                        lambda: calls.append("chat"))
    return calls


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(ide_router)
    app.dependency_overrides[require_developer] = _fake_developer
    app.dependency_overrides[require_admin] = _fake_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _refresh(client, user: str):
    return client.post("/ide/api/admin/seed/refresh", headers={"x-test-user": user})


async def test_route_refreshes_and_reloads(client, reloads, tmp_path, monkeypatch):
    history = {"agents": {"seedref-ag-a": [sha("v1 a")]}, "skills": {}}
    await ensure_ide_seed(_write_seed(tmp_path / "v1", 1, {"seedref-ag-a": "v1 a"}, {}))
    v2 = _write_seed(tmp_path / "v2", 2, {"seedref-ag-a": "v2 a", "seedref-ag-b": "v2 b"},
                     {}, history)
    monkeypatch.setattr(seed_module, "IDE_SEED_FILE", v2)
    r = await _refresh(client, "admin")
    assert r.status_code == 200, r.text
    assert r.json() == {"updated": ["agent:seedref-ag-a"], "skipped_edited": [],
                        "added": ["agent:seedref-ag-b"], "reload_failed": False}
    assert reloads == ["registry", "chat"]
    # Nothing left to change: no reload.
    r = await _refresh(client, "admin")
    assert r.json() == {"updated": [], "skipped_edited": [], "added": [],
                        "reload_failed": False}
    assert reloads == ["registry", "chat"]


@pytest.mark.parametrize("broken", ["registry", "chat"])
async def test_route_reload_failure_after_commit_is_reported(
        client, tmp_path, monkeypatch, caplog, broken):
    """Review minor: the seed rows are committed before the rebuild; a
    failing rebuild must not turn that into a 500 the admin would retry.
    It is logged and flagged, and the next reload picks the rows up."""
    calls: list[str] = []

    async def reload():
        calls.append("registry")
        if broken == "registry":
            raise RuntimeError("secret detail https://user:pw@host/x?token=1")

    def refresh():
        calls.append("chat")
        if broken == "chat":
            raise RuntimeError("secret detail")

    monkeypatch.setattr(registry_module.registry, "reload", reload)
    monkeypatch.setattr(chat_app_module.dynamic_chat_app, "refresh", refresh)
    await ensure_ide_seed(_write_seed(tmp_path / "v1", 1, {"seedref-ag-a": "v1 a"}, {}))
    v2 = _write_seed(tmp_path / "v2", 2, {"seedref-ag-a": "v2 a"}, {},
                     {"agents": {"seedref-ag-a": [sha("v1 a")]}, "skills": {}})
    monkeypatch.setattr(seed_module, "IDE_SEED_FILE", v2)
    with caplog.at_level("ERROR"):
        r = await _refresh(client, "admin")
    assert r.status_code == 200, r.text
    assert r.json() == {"updated": ["agent:seedref-ag-a"], "skipped_edited": [],
                        "added": [], "reload_failed": True}
    assert "secret detail" not in r.text
    assert await _instructions("seedref-ag-a") == "v2 a"
    assert any("reload" in rec.getMessage().lower() for rec in caplog.records)


async def test_route_only_skipped_does_not_reload(client, reloads, tmp_path, monkeypatch):
    await ensure_ide_seed(_write_seed(tmp_path / "v1", 1, {"seedref-ag-a": "edited"}, {}))
    v2 = _write_seed(tmp_path / "v2", 2, {"seedref-ag-a": "v2 a"}, {},
                     {"agents": {"seedref-ag-a": [sha("v1 a")]}, "skills": {}})
    monkeypatch.setattr(seed_module, "IDE_SEED_FILE", v2)
    r = await _refresh(client, "admin")
    assert r.json()["skipped_edited"] == ["agent:seedref-ag-a"]
    assert reloads == []


async def test_route_developer_is_403(client, reloads, tmp_path, monkeypatch):
    v2 = _write_seed(tmp_path, 2, {"seedref-ag-a": "v2 a"}, {}, {"agents": {}, "skills": {}})
    monkeypatch.setattr(seed_module, "IDE_SEED_FILE", v2)
    r = await _refresh(client, "dev")
    assert r.status_code == 403
    assert await _instructions("seedref-ag-a") is None
    assert reloads == []
    r = await _refresh(client, "nobody")
    assert r.status_code == 403


async def test_route_unreadable_seed_is_500_with_a_code(client, reloads, tmp_path,
                                                        monkeypatch):
    bad = tmp_path / "seed.ide.json"
    bad.write_text("{")
    monkeypatch.setattr(seed_module, "IDE_SEED_FILE", bad)
    r = await _refresh(client, "admin")
    assert r.status_code == 500
    assert r.json()["code"] == "seed_unreadable"
    assert r.json()["detail"] == "The assistant seed file could not be read."
    assert reloads == []


def test_route_is_a_post_only():
    keys = {(m, r.path) for r in ide_router.routes for m in getattr(r, "methods", ())}
    assert ("POST", "/ide/api/admin/seed/refresh") in keys
    assert ("GET", "/ide/api/admin/seed/refresh") not in keys


def test_orchestrator_description_says_abap_assistant():
    """The description is what the admin UI lists; the product name there is
    "ABAP Assistant". Descriptions are not hashed, so no history entry. Only
    the current seed: the v1 fixture is the released file as it was."""
    path = ROOT / "agents" / "ide" / "seed.ide.json"
    agents = {a["name"]: a for a in json.loads(path.read_text("utf-8"))["agents"]}
    text = agents["abap-orchestrator"]["description"]
    assert "ABAP Assistant" in text and "ABAP IDE" not in text, text


# sha256 of ``git show 30be3f4:agents/ide/seed.ide.json`` (the 2.18.0 seed).
V1_SHA256 = "f3ab0c8ad33378e2ac32c4d477485b2b171d2b277940c47e4f8f2e035fb17a35"


def test_v1_fixture_is_the_released_seed_byte_for_byte():
    """The upgrade tests start from what 2.18.0 shipped; an edit to the
    fixture would test an upgrade from a seed nobody ever had."""
    assert hashlib.sha256(V1.read_bytes()).hexdigest() == V1_SHA256
