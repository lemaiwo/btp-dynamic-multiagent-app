"""Embeddings of the data catalogue, switched on and run from the admin
(design addendum 2026-11-03 (l)).

Point 1, the setting: the two columns on ``orchestrator_config``
(``embeddings_enabled``, ``embedding_model``), their storage gate, and the
order in which ``embeddings.resolve_model`` picks the model:
``DATA_CATALOG_EMBEDDINGS=0`` -> none; the admin's ``embeddings_enabled = 0``
-> none; the admin's model -> that model, only while AI Core lists it
deployed (else nothing, source still ``setting``); ``AICORE_EMBEDDING_MODEL``;
discovery. The setting is read through the 10-minute cache and a save
resets it.

Point 2, the routes (``agents/data_catalog/embedding_routes.py``): the
status and its counts, the PUT and its refusals, the background run
(``agents/data_catalog/embedding_run.py``: one at a time, progress,
``last_run``, vectors of another model dropped, cancelled at shutdown), the
deploy (idempotent, 422 for a model AI Core does not offer, the one WARNING,
502 with the code in ``X-Embeddings-Error``) and the ``mode`` of the
admin's test search.

No network: discovery is patched (``agents.shared._discover_deployed_models``),
so are the three calls of ``agents.aicore_deployments`` (``FakeAICore``) and
the embedding call (``embeddings._create``); the suite's
``DATA_CATALOG_EMBEDDINGS=0`` (``tests/conftest.py``) is set to ``1`` here
only where a rung needs it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()

import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import delete, select, update  # noqa: E402

import agents.shared as shared  # noqa: E402
from agents import aicore_deployments, auth  # noqa: E402
from agents.data_catalog import embedding_routes, embedding_run, embeddings, search  # noqa: E402
from agents.data_catalog import routes as dc_routes  # noqa: E402
from agents.data_catalog.models import (  # noqa: E402
    DataCatalogEdge,
    DataCatalogNode,
    DataCatalogVector,
)
from agents.db import (  # noqa: E402
    OrchestratorConfig,
    SessionLocal,
    get_embedding_setting,
    init_db,
    set_embedding_setting,
)

SECRET = "s3cr3t-embedding-token"
SMALL_MODEL = "text-embedding-3-small"
DEPLOYED = ["gpt-4o", "text-embedding-3-small", "text-embedding-3-large"]


class Discovery:
    """Stands in for AI Core's list of RUNNING deployments; counts the calls."""

    def __init__(self, models: list[str]) -> None:
        self.models = list(models)
        self.calls = 0

    def __call__(self) -> list[str]:
        self.calls += 1
        return list(self.models)


async def store(enabled: int, model: str | None) -> None:
    """Write the row directly: no storage gate and no cache reset."""
    async with SessionLocal() as s:
        await s.execute(
            update(OrchestratorConfig)
            .where(OrchestratorConfig.id == 1)
            .values(embeddings_enabled=enabled, embedding_model=model)
            .execution_options(synchronize_session=False)
        )
        await s.commit()


async def stored() -> tuple[int, str | None]:
    async with SessionLocal() as s:
        row = (await s.execute(
            select(OrchestratorConfig.embeddings_enabled, OrchestratorConfig.embedding_model)
            .where(OrchestratorConfig.id == 1)
        )).one()
    return row[0], row[1]


@pytest.fixture(autouse=True)
async def _defaults(monkeypatch):
    """Every test starts (and leaves the shared session database) with the
    column defaults: embeddings on, the model automatic."""
    await init_db()
    await store(1, None)
    embeddings.reset_caches()
    monkeypatch.setenv(embeddings.SWITCH_ENV, "1")
    monkeypatch.delenv(embeddings.MODEL_ENV, raising=False)
    yield
    await store(1, None)
    embeddings.reset_caches()


@pytest.fixture
def discovery(monkeypatch) -> Discovery:
    found = Discovery(DEPLOYED)
    monkeypatch.setattr(shared, "_discover_deployed_models", found)
    return found


@pytest.fixture
def no_embedding_call(monkeypatch):
    async def refuse(model, texts):  # pragma: no cover - the test fails if reached
        raise AssertionError("an embedding call was made")

    monkeypatch.setattr(embeddings, "_create", refuse)


# --- the columns and their storage --------------------------------------------------------


def test_the_two_columns_and_their_defaults():
    table = OrchestratorConfig.__table__
    enabled, model = table.c.embeddings_enabled, table.c.embedding_model
    assert not enabled.nullable and enabled.server_default.arg == "1"
    assert model.nullable and model.type.length == 128 and model.server_default is None


async def test_without_the_row_embeddings_are_on_and_the_model_automatic():
    class NoRow:
        async def get(self, *args, **kwargs):
            return None

    assert await get_embedding_setting(NoRow()) == {"enabled": True, "model": None}


async def test_the_row_init_db_ensures_has_the_defaults():
    async with SessionLocal() as s:
        assert await get_embedding_setting(s) == {"enabled": True, "model": None}


async def test_the_setting_is_stored_and_read_back():
    async with SessionLocal() as s:
        await set_embedding_setting(s, False, "text-embedding-3-small")
    assert await stored() == (0, "text-embedding-3-small")
    async with SessionLocal() as s:
        assert await get_embedding_setting(s) == {
            "enabled": False, "model": "text-embedding-3-small"}
        await set_embedding_setting(s, True, None)
    assert await stored() == (1, None)
    async with SessionLocal() as s:
        assert await get_embedding_setting(s) == {"enabled": True, "model": None}


async def test_any_stored_value_but_zero_is_on_and_an_empty_model_is_automatic():
    await store(2, "")
    async with SessionLocal() as s:
        assert await get_embedding_setting(s) == {"enabled": True, "model": None}


@pytest.mark.parametrize("enabled", [1, 0, "true", "false", None, 1.0])
async def test_storage_refuses_an_enabled_that_is_not_a_boolean(enabled):
    async with SessionLocal() as s:
        with pytest.raises(ValueError) as refused:
            await set_embedding_setting(s, enabled, None)
    assert str(refused.value).startswith("enabled: ")
    assert await stored() == (1, None)


@pytest.mark.parametrize("model", [
    "", " text-embedding-3-small", "text-embedding-3-small ", "text embedding",
    "text-embedding\n", "a\x00b", "x" * 129, f"{SECRET}​", 5, ["x"], True,
])
async def test_storage_refuses_a_model_that_is_not_a_model_name(model):
    async with SessionLocal() as s:
        with pytest.raises(ValueError) as refused:
            await set_embedding_setting(s, True, model)
    message = str(refused.value)
    assert message.startswith("model: ")
    assert SECRET not in message and "embedding\n" not in message
    assert await stored() == (1, None)


async def test_a_model_name_of_128_characters_is_stored():
    name = "a" * 128
    async with SessionLocal() as s:
        await set_embedding_setting(s, True, name)
    assert await stored() == (1, name)


def test_an_existing_database_gets_both_columns_with_their_defaults(tmp_path):
    """A database of an earlier version has neither column; init_db adds them
    (twice is a no-op) and the existing row reads on and automatic."""
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE orchestrator_config (id INTEGER PRIMARY KEY, instructions TEXT NOT NULL,
          model_name VARCHAR(128), updated_at DATETIME);
        INSERT INTO orchestrator_config VALUES (1, 'old orchestrator', 'gpt-4o', NULL);
        """
    )
    con.commit()
    con.close()
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db}"}
    code = (
        "import asyncio; from agents.db import init_db; "
        "asyncio.run(init_db()); asyncio.run(init_db())"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db)
    assert con.execute(
        "SELECT instructions, model_name, embeddings_enabled, embedding_model "
        "FROM orchestrator_config"
    ).fetchall() == [("old orchestrator", "gpt-4o", 1, None)]
    con.close()


# --- the resolution order ----------------------------------------------------------------


async def test_the_switch_wins_over_everything(monkeypatch, discovery, no_embedding_call):
    await store(1, "text-embedding-3-small")
    monkeypatch.setenv(embeddings.MODEL_ENV, "my-embedder")
    monkeypatch.setenv(embeddings.SWITCH_ENV, "0")
    assert await embeddings.resolve_model_detail() == (None, "none")
    assert await embeddings.resolve_model() is None
    assert await embeddings.embed_texts(["x"]) is None
    assert discovery.calls == 0


async def test_the_admin_switched_off_means_none_whatever_else_is_set(
    monkeypatch, discovery, no_embedding_call
):
    await store(0, "text-embedding-3-small")
    monkeypatch.setenv(embeddings.MODEL_ENV, "my-embedder")
    assert await embeddings.resolve_model_detail() == (None, "none")
    assert await embeddings.embed_texts(["x"]) is None
    assert discovery.calls == 0


async def test_the_admin_model_wins_over_the_environment_while_it_is_deployed(
    monkeypatch, discovery
):
    await store(1, "text-embedding-3-small")
    monkeypatch.setenv(embeddings.MODEL_ENV, "my-embedder")
    assert await embeddings.resolve_model_detail() == ("text-embedding-3-small", "setting")
    assert await embeddings.resolve_model() == "text-embedding-3-small"


async def test_an_admin_model_that_is_not_deployed_embeds_nothing(
    monkeypatch, discovery, no_embedding_call
):
    """No fallback to the environment or to discovery: the admin chose a
    model, and the status says that one is not there."""
    await store(1, "text-embedding-ada-002")
    monkeypatch.setenv(embeddings.MODEL_ENV, "my-embedder")
    assert await embeddings.resolve_model_detail() == (None, "setting")
    assert await embeddings.embed_texts(["x"]) is None
    assert discovery.calls == 1


async def test_a_failed_discovery_leaves_an_admin_model_unusable(monkeypatch, caplog):
    def boom():
        raise RuntimeError(f"https://user:{SECRET}@aicore.example/v2/lm/deployments")

    monkeypatch.setattr(shared, "_discover_deployed_models", boom)
    await store(1, "text-embedding-3-small")
    assert await embeddings.resolve_model_detail() == (None, "setting")
    assert "RuntimeError" in caplog.text and SECRET not in caplog.text


async def test_automatic_takes_the_environment_model_without_a_deployment_check(
    monkeypatch, discovery
):
    monkeypatch.setenv(embeddings.MODEL_ENV, " my-embedder ")
    assert await embeddings.resolve_model_detail() == ("my-embedder", "env")
    assert discovery.calls == 0


@pytest.mark.parametrize(("deployed", "expected"), [
    (DEPLOYED, ("text-embedding-3-large", "discovery")),
    (["gpt-4o", "Gemini-Embedding-001", "text-embedding-3-small"],
     ("Gemini-Embedding-001", "discovery")),
    (["gpt-4o", "claude-sonnet"], (None, "none")),
    ([], (None, "none")),
])
async def test_automatic_without_the_environment_discovers(monkeypatch, deployed, expected):
    monkeypatch.setattr(shared, "_discover_deployed_models", Discovery(deployed))
    assert await embeddings.resolve_model_detail() == expected


async def test_an_unreadable_setting_embeds_nothing_and_is_read_again(
    monkeypatch, discovery, caplog
):
    """Fail closed: an admin who switched embeddings off must not get calls
    because the row could not be read. The failure is not cached."""
    real = embeddings.get_embedding_setting

    async def unreadable(session):
        raise RuntimeError(f"postgresql://app:{SECRET}@db.internal/registry")

    monkeypatch.setattr(embeddings, "get_embedding_setting", unreadable)
    assert await embeddings.resolve_model_detail() == (None, "none")
    assert "RuntimeError" in caplog.text and SECRET not in caplog.text
    monkeypatch.setattr(embeddings, "get_embedding_setting", real)
    assert await embeddings.resolve_model_detail() == ("text-embedding-3-large", "discovery")


# --- the cache ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_the_setting_and_the_deployments_are_read_through_the_ten_minute_cache(
    monkeypatch, discovery
):
    clock = Clock()
    monkeypatch.setattr(embeddings, "_clock", clock)
    reads = 0
    real = embeddings.get_embedding_setting

    async def counted(session):
        nonlocal reads
        reads += 1
        return await real(session)

    monkeypatch.setattr(embeddings, "get_embedding_setting", counted)
    assert await embeddings.resolve_model_detail() == ("text-embedding-3-large", "discovery")
    await store(0, None)  # behind the cache's back: no save, no reset
    clock.now += embeddings.MODEL_CACHE_SECONDS - 1
    assert await embeddings.resolve_model_detail() == ("text-embedding-3-large", "discovery")
    assert (reads, discovery.calls) == (1, 1)
    clock.now += 2
    assert await embeddings.resolve_model_detail() == (None, "none")
    assert reads == 2


async def test_a_save_resets_the_cache(discovery):
    assert await embeddings.resolve_model_detail() == ("text-embedding-3-large", "discovery")
    async with SessionLocal() as s:
        await set_embedding_setting(s, True, "text-embedding-3-small")
    assert await embeddings.resolve_model_detail() == ("text-embedding-3-small", "setting")
    async with SessionLocal() as s:
        await set_embedding_setting(s, False, "text-embedding-3-small")
    assert await embeddings.resolve_model_detail() == (None, "none")


async def test_a_read_that_a_save_overtook_is_not_cached(monkeypatch, discovery):
    """A save that lands while a resolve is reading the row: the value read
    is used once but not cached, so the next resolve sees the save."""
    real = embeddings.get_embedding_setting

    async def overtaken(session):
        value = await real(session)
        await store(0, None)
        embeddings.reset_caches()  # what set_embedding_setting does after its commit
        return value

    monkeypatch.setattr(embeddings, "get_embedding_setting", overtaken)
    assert await embeddings.resolve_model_detail() == ("text-embedding-3-large", "discovery")
    monkeypatch.setattr(embeddings, "get_embedding_setting", real)
    assert await embeddings.resolve_model_detail() == (None, "none")


async def test_discovery_takes_any_model_whose_name_holds_embed(monkeypatch):
    """As ``agents.aicore_deployments`` lists embedding models (``embed``,
    any case): ``amazon--titan-embed-text`` is one; the preferred model
    still wins when both are deployed."""
    monkeypatch.setattr(shared, "_discover_deployed_models",
                        Discovery(["gpt-4o", "Amazon--Titan-EMBED-Text"]))
    assert await embeddings.resolve_model_detail() == ("Amazon--Titan-EMBED-Text", "discovery")
    embeddings.reset_caches()
    monkeypatch.setattr(shared, "_discover_deployed_models",
                        Discovery(["amazon--titan-embed-text", "text-embedding-3-large"]))
    assert await embeddings.resolve_model_detail() == ("text-embedding-3-large", "discovery")


async def test_a_save_resets_the_caches_after_its_commit(monkeypatch):
    """Reset before the commit, a resolve running meanwhile could cache the
    old row for ten minutes: when the reset runs, the saving session holds
    no transaction and another connection reads the new row."""
    real = embeddings.reset_caches
    url = os.environ["DATABASE_URL"]
    assert url.startswith("sqlite+aiosqlite:///")
    seen: list[tuple[bool, tuple[int, str | None]]] = []
    async with SessionLocal() as s:
        def reset_and_read() -> None:
            con = sqlite3.connect(url.removeprefix("sqlite+aiosqlite:///"))
            try:
                row = con.execute("SELECT embeddings_enabled, embedding_model "
                                  "FROM orchestrator_config WHERE id = 1").fetchone()
            finally:
                con.close()
            seen.append((s.in_transaction(), tuple(row)))
            real()

        monkeypatch.setattr(embeddings, "reset_caches", reset_and_read)
        await set_embedding_setting(s, False, SMALL_MODEL)
    assert seen == [(False, (0, SMALL_MODEL))]


async def test_a_discovery_that_a_save_overtook_is_not_cached(monkeypatch):
    """The mirror of the setting's rule for the list of deployments: a save
    that lands while discovery runs leaves that answer uncached."""
    found = Discovery(["text-embedding-3-small"])

    def overtaken() -> list[str]:
        models = found()
        embeddings.reset_caches()  # what a save does after its commit
        return models

    monkeypatch.setattr(shared, "_discover_deployed_models", overtaken)
    assert await embeddings.resolve_model_detail() == ("text-embedding-3-small", "discovery")
    found.models = ["text-embedding-3-large"]
    monkeypatch.setattr(shared, "_discover_deployed_models", found)
    assert await embeddings.resolve_model_detail() == ("text-embedding-3-large", "discovery")


async def test_a_failed_or_empty_discovery_is_cached_for_a_minute_only(monkeypatch):
    """A failed discovery must not make an admin model look undeployed for
    ten minutes (a Compute run would embed nothing): it is asked again after
    a minute; a list with models keeps the ten minutes."""
    clock = Clock()
    monkeypatch.setattr(embeddings, "_clock", clock)
    calls = 0

    def flaky() -> list[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("AI Core unreachable")
        return ["text-embedding-3-small"]

    monkeypatch.setattr(shared, "_discover_deployed_models", flaky)
    await store(1, "text-embedding-3-small")
    assert await embeddings.resolve_model_detail() == (None, "setting")
    clock.now += embeddings.EMPTY_CACHE_SECONDS - 1
    assert await embeddings.resolve_model_detail() == (None, "setting")
    assert calls == 1
    clock.now += 2
    assert await embeddings.resolve_model_detail() == ("text-embedding-3-small", "setting")
    clock.now += embeddings.EMPTY_CACHE_SECONDS + 1
    assert await embeddings.resolve_model_detail() == ("text-embedding-3-small", "setting")
    assert calls == 2


async def test_resolving_never_raises_whatever_discovery_answers(monkeypatch):
    monkeypatch.setattr(shared, "_discover_deployed_models", lambda: 5)
    assert await embeddings.resolve_model_detail() == (None, "none")
    embeddings.reset_caches()
    monkeypatch.delattr(shared, "_discover_deployed_models")
    assert await embeddings.resolve_model_detail() == (None, "none")


def test_the_session_factory_is_looked_up_at_call_time():
    """``tests/test_hana_integration.py`` rebinds ``agents.db.SessionLocal``;
    a name bound at import would keep the first engine."""
    source = Path(embeddings.__file__).read_text()
    assert "from agents.db import SessionLocal" not in source
    assert "agents_db.SessionLocal()" in source


# --- the routes (point 2) ----------------------------------------------------------------

BASE = "/admin/api/data-catalog/embeddings"
PRINCIPAL = "admin.one@example.test"
LARGE, SMALL, ADA = "text-embedding-3-large", "text-embedding-3-small", "text-embedding-ada-002"


class FakeAICore:
    """``agents.aicore_deployments`` without a network: the scenario offers
    three embedding models, one of which has a RUNNING deployment. ``fail``
    makes the next calls raise that code; ``deploy`` creates a PENDING
    deployment unless a live one exists, as the module does."""

    def __init__(self) -> None:
        self.models = [{"model": m, "versions": ["1"]} for m in (ADA, LARGE, SMALL)]
        self.deployments = [{"model": LARGE, "status": "RUNNING", "deployment_id": "d-1"},
                            {"model": ADA, "status": "DEAD", "deployment_id": "d-0"}]
        self.fail: dict[str, str] = {}
        self.calls: list[str] = []

    def _check(self, call: str) -> None:
        self.calls.append(call)
        if call in self.fail:
            raise aicore_deployments.AICoreError(self.fail[call])

    async def list_models(self) -> list[dict[str, Any]]:
        self._check("list_models")
        return [dict(m, versions=list(m["versions"])) for m in self.models]

    async def list_deployments(self) -> list[dict[str, str]]:
        self._check("list_deployments")
        return [dict(d) for d in self.deployments]

    async def deploy(self, model: str) -> dict[str, Any]:
        self._check("deploy")
        live = [d for d in self.deployments
                if d["model"] == model and d["status"] in ("RUNNING", "PENDING")]
        if live:
            return {"deployment_id": live[0]["deployment_id"], "status": live[0]["status"],
                    "created": False}
        made = {"model": model, "status": "PENDING",
                "deployment_id": f"d-{len(self.deployments) + 1}"}
        self.deployments.append(made)
        return {"deployment_id": made["deployment_id"], "status": "PENDING", "created": True}


@pytest.fixture
def aicore(monkeypatch) -> FakeAICore:
    fake = FakeAICore()
    for name in ("list_models", "list_deployments", "deploy"):
        monkeypatch.setattr(aicore_deployments, name, getattr(fake, name))
    monkeypatch.setenv("AICORE_RESOURCE_GROUP", "rg-agents")
    return fake


class Embedder:
    """Stands in for the embedding call (``embeddings._create``): a vector
    per text; ``hold`` stops the call of that batch (0-based) until
    ``release`` is set; ``boom`` raises instead."""

    def __init__(self) -> None:
        self.batches: list[list[str]] = []
        self.hold: int | None = None
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.boom: Exception | None = None

    async def __call__(self, model: str, texts: list[str]) -> list[list[float]]:
        index = len(self.batches)
        self.batches.append(list(texts))
        if self.hold == index:
            self.reached.set()
            await self.release.wait()
        if self.boom is not None:
            raise self.boom
        return [[1.0, float(len(t)), 0.5] for t in texts]


@pytest.fixture
def embedder(monkeypatch) -> Embedder:
    fake = Embedder()
    monkeypatch.setattr(embeddings, "_create", fake)
    return fake


async def _clear_catalogue() -> None:
    async with SessionLocal() as s:
        for model in (DataCatalogEdge, DataCatalogVector, DataCatalogNode):
            await s.execute(delete(model))
        await s.commit()


@pytest.fixture
async def catalogue() -> dict[str, int]:
    """One source with two entities and a function (4 embeddable nodes) and
    a field (never embedded). Answers the node ids by name."""
    await _clear_catalogue()
    async with SessionLocal() as s:
        source = DataCatalogNode(kind="source", source_kind="odata", source_name="sales",
                                 name="sales", label="Sales", purpose="Sales orders")
        s.add(source)
        await s.flush()
        rows = [DataCatalogNode(kind=kind, source_kind="odata", source_name="sales", name=name,
                                label=name, parent_id=source.id, readable=1)
                for kind, name in (("entity", "Order"), ("entity", "Item"),
                                   ("function", "Release"), ("field", "OrderId"))]
        s.add_all(rows)
        await s.commit()
        ids = {"sales": source.id} | {r.name: r.id for r in rows}
    yield ids
    await _clear_catalogue()


async def add_vector(node_id: int, model: str, *, current: bool = True) -> None:
    async with SessionLocal() as s:
        node = await s.get(DataCatalogNode, node_id)
        digest = embeddings.text_hash(embeddings.node_text(node)) if current else "0" * 64
        s.add(DataCatalogVector(node_id=node_id, model=model, text_hash=digest,
                                vector=embeddings.pack([1.0, 0.0, 0.0])))
        await s.commit()


async def vectors_stored() -> set[tuple[int, str]]:
    async with SessionLocal() as s:
        rows = await s.execute(select(DataCatalogVector.node_id, DataCatalogVector.model))
    return set(rows.all())


@pytest.fixture
async def client():
    """The two catalogue routers under ``/admin`` with the caller's
    principal bound, as ``app.py``'s middleware binds it."""
    bare = FastAPI()

    @bare.middleware("http")
    async def bind(request, call_next):
        token = auth.current_principal.set(PRINCIPAL)
        try:
            return await call_next(request)
        finally:
            auth.current_principal.reset(token)

    bare.include_router(embedding_routes.router, prefix="/admin")
    bare.include_router(dc_routes.router, prefix="/admin")
    embedding_routes.reset_cache()
    embedding_run.reset()
    async with AsyncClient(transport=ASGITransport(app=bare), base_url="http://test") as c:
        yield c
    await embedding_run.cancel_all()
    embedding_run.reset()
    embedding_routes.reset_cache()


async def finished() -> None:
    task = embedding_run._task
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), 5)


# --- the status -------------------------------------------------------------------------


async def test_the_status_names_setting_model_source_deployments_and_counts(
    client, aicore, discovery, catalogue
):
    r = await client.get(BASE)
    assert r.status_code == 200
    assert r.json() == {
        "setting": {"enabled": True, "model": None},
        "effective_model": LARGE,
        "source": "discovery",
        "env_override": False,
        "deployed": [{"model": LARGE, "status": "RUNNING", "deployment_id": "d-1"},
                     {"model": ADA, "status": "DEAD", "deployment_id": "d-0"}],
        # Not deployed: no live deployment (a DEAD one is no deployment).
        "available": [{"model": ADA, "versions": ["1"]}, {"model": SMALL, "versions": ["1"]}],
        "nodes": 4, "vectors": 0, "stale": 4,
        "running": False, "progress": None, "last_run": None,
        "resource_group": "rg-agents", "aicore_error": None,
    }


async def test_the_counts_are_of_the_effective_model_and_its_text_hashes(
    client, aicore, discovery, catalogue
):
    await add_vector(catalogue["sales"], LARGE)
    await add_vector(catalogue["Order"], LARGE, current=False)  # an older text
    await add_vector(catalogue["Item"], SMALL)  # another model
    await add_vector(catalogue["OrderId"], LARGE)  # a field: not counted
    body = (await client.get(BASE)).json()
    assert (body["nodes"], body["vectors"], body["stale"]) == (4, 2, 3)


async def test_without_a_model_nothing_counts_as_embedded(client, aicore, catalogue):
    await store(0, None)
    await add_vector(catalogue["sales"], LARGE)
    body = (await client.get(BASE)).json()
    assert (body["effective_model"], body["source"]) == (None, "none")
    assert (body["nodes"], body["vectors"], body["stale"]) == (4, 0, 4)


async def test_the_environment_model_is_an_override(monkeypatch, client, aicore, catalogue):
    monkeypatch.setenv(embeddings.MODEL_ENV, SMALL)
    body = (await client.get(BASE)).json()
    assert (body["effective_model"], body["source"], body["env_override"]) == (
        SMALL, "env", True)


async def test_an_unreachable_ai_core_still_answers_the_status(
    client, aicore, discovery, catalogue, caplog
):
    aicore.fail = {"list_deployments": "token_failed", "list_models": "token_failed"}
    r = await client.get(BASE)
    assert r.status_code == 200
    body = r.json()
    assert (body["deployed"], body["available"], body["aicore_error"]) == ([], [], "token_failed")
    assert body["nodes"] == 4 and body["effective_model"] == LARGE


async def test_the_ai_core_lists_are_cached_briefly_between_polls(
    client, aicore, discovery, catalogue
):
    await client.get(BASE)
    await client.get(BASE)
    assert aicore.calls.count("list_deployments") == 1
    assert aicore.calls.count("list_models") == 1


# --- the setting --------------------------------------------------------------------------


async def test_put_stores_a_deployed_model_and_answers_the_status(
    client, aicore, discovery, catalogue
):
    aicore.deployments.append({"model": SMALL, "status": "RUNNING", "deployment_id": "d-2"})
    r = await client.put(BASE, json={"enabled": True, "model": SMALL})
    assert r.status_code == 200
    assert r.json()["setting"] == {"enabled": True, "model": SMALL}
    assert (r.json()["effective_model"], r.json()["source"]) == (SMALL, "setting")
    assert await stored() == (1, SMALL)
    r = await client.put(BASE, json={"enabled": False, "model": None})
    assert r.status_code == 200 and r.json()["setting"] == {"enabled": False, "model": None}
    assert (r.json()["effective_model"], r.json()["source"]) == (None, "none")
    assert await stored() == (0, None)


async def test_put_takes_a_deployed_model_in_any_status(client, aicore, discovery, catalogue):
    """The Select lists every deployment with its status; one that is not
    RUNNING is stored, and the status says nothing is embedded with it."""
    r = await client.put(BASE, json={"enabled": True, "model": ADA})
    assert r.status_code == 200
    assert (r.json()["effective_model"], r.json()["source"]) == (None, "setting")


@pytest.mark.parametrize("model", [SMALL, "gpt-4o", f"x-{SECRET}"])
async def test_put_refuses_a_model_that_is_not_a_deployed_embedding_model(
    client, aicore, discovery, model
):
    r = await client.put(BASE, json={"enabled": True, "model": model})
    assert r.status_code == 422
    assert r.json() == {"detail": "model: not a deployed embedding model"}
    assert await stored() == (1, None)


@pytest.mark.parametrize("body", [
    {"enabled": True},
    {"model": None},
    {"enabled": "true", "model": None},
    {"enabled": 1, "model": None},
    {"enabled": True, "model": 5},
    {"enabled": True, "model": ""},
    {"enabled": True, "model": None, "token": SECRET},
    {"enabled": True, "model": None, SECRET: 1},
    [SECRET],
    SECRET,
])
async def test_put_refuses_a_body_that_is_not_the_setting(client, aicore, discovery, body):
    r = await client.put(BASE, json=body)
    assert r.status_code == 422
    assert SECRET not in r.text
    assert await stored() == (1, None)
    assert "list_deployments" not in aicore.calls


async def test_a_body_over_four_kilobytes_is_refused_before_it_is_read(client, aicore):
    padding = "x" * (4 * 1024)
    r = await client.put(BASE, json={"enabled": True, "model": None, "p": padding})
    assert r.status_code == 413
    r = await client.post(f"{BASE}/deploy", json={"model": padding})
    assert r.status_code == 413
    nested = "[" * 3000 + "]" * 3000
    r = await client.put(BASE, content=nested[:4000], headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json() == {"detail": "body: expected a JSON object"}
    assert aicore.calls == []


async def test_put_of_a_model_while_ai_core_is_unreachable_is_a_502(
    client, aicore, discovery, caplog
):
    aicore.fail = {"list_deployments": "aicore_unavailable"}
    r = await client.put(BASE, json={"enabled": True, "model": SMALL})
    assert r.status_code == 502
    assert r.headers["X-Embeddings-Error"] == "aicore_unavailable"
    assert r.json()["detail"] == aicore_deployments.ERROR_MESSAGES["aicore_unavailable"]
    assert await stored() == (1, None)


async def test_put_of_automatic_needs_no_ai_core(client, aicore, discovery):
    aicore.fail = {"list_deployments": "aicore_unavailable", "list_models": "aicore_unavailable"}
    r = await client.put(BASE, json={"enabled": False, "model": None})
    assert r.status_code == 200 and r.json()["aicore_error"] == "aicore_unavailable"
    assert await stored() == (0, None)


# --- the run --------------------------------------------------------------------------------


async def test_a_run_embeds_in_batches_reports_progress_and_keeps_its_last_run(
    monkeypatch, client, aicore, discovery, catalogue, embedder
):
    monkeypatch.setattr(embeddings, "BATCH_SIZE", 2)
    await add_vector(catalogue["Order"], SMALL)  # another model: dropped by the run
    embedder.hold = 1
    r = await client.post(f"{BASE}/run")
    assert r.status_code == 202 and r.json() == {"running": True}
    await asyncio.wait_for(embedder.reached.wait(), 5)
    body = (await client.get(BASE)).json()
    assert body["running"] is True and body["progress"] == {"done": 2, "total": 4}
    r = await client.post(f"{BASE}/run")
    assert r.status_code == 409
    assert r.json() == {"detail": "An embedding run is already in progress"}
    embedder.release.set()
    await finished()
    body = (await client.get(BASE)).json()
    assert body["running"] is False and body["progress"] is None
    last = body["last_run"]
    assert (last["model"], last["embedded"], last["error_class"]) == (LARGE, 4, None)
    assert last["started_at"] <= last["finished_at"] and last["finished_at"].endswith("+00:00")
    assert (body["vectors"], body["stale"]) == (4, 0)
    assert [len(b) for b in embedder.batches] == [2, 2]
    assert {m for _, m in await vectors_stored()} == {LARGE}


async def test_a_second_run_sends_only_what_changed(client, aicore, discovery, catalogue, embedder):
    await add_vector(catalogue["sales"], LARGE)
    await add_vector(catalogue["Order"], LARGE)
    assert (await client.post(f"{BASE}/run")).status_code == 202
    await finished()
    assert sum(len(b) for b in embedder.batches) == 2
    assert (await client.get(BASE)).json()["last_run"]["embedded"] == 2


async def test_a_full_run_drops_vectors_of_another_model_even_with_nothing_to_send(
    client, aicore, discovery, catalogue, embedder
):
    for name in ("sales", "Order", "Item", "Release"):
        await add_vector(catalogue[name], LARGE)
    await add_vector(catalogue["Order"], SMALL)
    assert (await client.post(f"{BASE}/run")).status_code == 202
    await finished()
    assert embedder.batches == []
    assert {m for _, m in await vectors_stored()} == {LARGE}


async def test_a_run_without_a_model_is_refused(client, aicore, catalogue, embedder):
    await store(0, None)
    r = await client.post(f"{BASE}/run")
    assert r.status_code == 409 and r.json() == {"detail": "No embedding model is in effect"}
    assert embedding_run._task is None and embedder.batches == []


async def test_a_run_takes_no_body_but_an_empty_object(client, aicore, discovery, catalogue,
                                                      embedder):
    r = await client.post(f"{BASE}/run", json={"model": SECRET})
    assert r.status_code == 422 and SECRET not in r.text
    r = await client.post(f"{BASE}/run", json={})
    assert r.status_code == 202
    await finished()


async def test_a_failed_run_keeps_its_error_class_and_the_stored_vectors(
    client, aicore, discovery, catalogue, embedder, caplog
):
    await add_vector(catalogue["Order"], SMALL)
    embedder.boom = RuntimeError(f"https://user:{SECRET}@aicore.example/v2?token={SECRET}")
    assert (await client.post(f"{BASE}/run")).status_code == 202
    await finished()
    last = (await client.get(BASE)).json()["last_run"]
    assert (last["model"], last["embedded"], last["error_class"]) == (LARGE, 0, "RuntimeError")
    assert last["finished_at"] is not None
    assert await vectors_stored() == {(catalogue["Order"], SMALL)}
    assert SECRET not in caplog.text and "RuntimeError" in caplog.text


async def test_the_run_is_cancelled_at_shutdown(client, aicore, discovery, catalogue, embedder):
    embedder.hold = 0
    assert (await client.post(f"{BASE}/run")).status_code == 202
    await asyncio.wait_for(embedder.reached.wait(), 5)
    await embedding_run.cancel_all()
    assert embedding_run.running() is False
    last = embedding_run.last_run()
    assert last["finished_at"] is None and last["error_class"] == "CancelledError"
    assert await vectors_stored() == set()


def test_the_lifespan_cancels_the_run_at_shutdown():
    import inspect

    import app as app_module

    assert "embedding_run.cancel_all()" in inspect.getsource(app_module.lifespan)


# --- the deploy -----------------------------------------------------------------------------


def _deploy_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "AI Core deployment of" in r.getMessage()]


async def test_deploy_makes_a_deployment_logs_one_warning_and_the_status_shows_it(
    client, aicore, discovery, caplog
):
    await client.get(BASE)  # the lists are cached now
    r = await client.post(f"{BASE}/deploy", json={"model": SMALL})
    assert r.status_code == 202
    assert r.json() == {"deployment_id": "d-3", "status": "PENDING", "created": True}
    assert _deploy_warnings(caplog) == [
        f"AI Core deployment of {SMALL} requested by {PRINCIPAL} in resource group rg-agents"]
    deployed = (await client.get(BASE)).json()["deployed"]
    assert {"model": SMALL, "status": "PENDING", "deployment_id": "d-3"} in deployed


async def test_deploy_answers_a_live_deployment_and_creates_nothing(
    client, aicore, discovery, caplog
):
    r = await client.post(f"{BASE}/deploy", json={"model": LARGE})
    assert r.status_code == 202
    assert r.json() == {"deployment_id": "d-1", "status": "RUNNING", "created": False}
    assert len(aicore.deployments) == 2 and len(_deploy_warnings(caplog)) == 1


@pytest.mark.parametrize("model", ["gpt-4o", "text-embedding-4-imaginary", f"x {SECRET}", "a\nb"])
async def test_deploy_refuses_a_model_the_scenario_does_not_offer(
    client, aicore, discovery, caplog, model
):
    r = await client.post(f"{BASE}/deploy", json={"model": model})
    assert r.status_code == 422
    assert r.json() == {"detail": "model: not an embedding model AI Core offers"}
    assert "deploy" not in aicore.calls and _deploy_warnings(caplog) == []
    assert SECRET not in caplog.text


@pytest.mark.parametrize("body", [{}, {"model": 5}, {"model": None}, {"model": SMALL, "x": 1},
                                  [SMALL]])
async def test_deploy_refuses_a_body_that_is_not_one_model(client, aicore, body):
    r = await client.post(f"{BASE}/deploy", json=body)
    assert r.status_code == 422
    assert aicore.calls == []


@pytest.mark.parametrize("code", ["aicore_unavailable", "token_failed", "deploy_failed"])
async def test_a_failed_deploy_is_a_502_with_its_code_and_creates_nothing(
    client, aicore, discovery, caplog, code
):
    aicore.fail = {"deploy": code}
    r = await client.post(f"{BASE}/deploy", json={"model": SMALL})
    assert r.status_code == 502
    assert r.headers["X-Embeddings-Error"] == code
    assert r.json() == {"detail": aicore_deployments.ERROR_MESSAGES[code]}
    assert len(aicore.deployments) == 2
    assert len(_deploy_warnings(caplog)) == 1  # the request was made: it may bill


async def test_a_model_list_that_fails_is_a_502_and_requests_nothing(client, aicore, caplog):
    aicore.fail = {"list_models": "token_failed"}
    r = await client.post(f"{BASE}/deploy", json={"model": SMALL})
    assert r.status_code == 502 and r.headers["X-Embeddings-Error"] == "token_failed"
    assert "deploy" not in aicore.calls and _deploy_warnings(caplog) == []


async def test_a_model_the_deploy_itself_does_not_know_is_a_422(client, aicore, caplog):
    aicore.fail = {"deploy": "model_unknown"}
    r = await client.post(f"{BASE}/deploy", json={"model": SMALL})
    assert r.status_code == 422
    assert r.json() == {"detail": "model: not an embedding model AI Core offers"}


async def test_an_unexpected_failure_of_ai_core_calls_names_no_text(
    monkeypatch, client, aicore, caplog
):
    async def broken(model):
        raise RuntimeError(f"https://user:{SECRET}@aicore.example/v2")

    monkeypatch.setattr(aicore_deployments, "deploy", broken)
    r = await client.post(f"{BASE}/deploy", json={"model": SMALL})
    assert r.status_code == 502 and r.headers["X-Embeddings-Error"] == "aicore_unavailable"
    assert SECRET not in r.text and SECRET not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_a_deploy_goes_on_when_its_request_is_cancelled(
    monkeypatch, client, aicore, discovery
):
    """A client that goes away must not cancel the deploy between a create
    AI Core may have received and the release of the module's deploy lock."""
    entered, gate = asyncio.Event(), asyncio.Event()
    real = aicore.deploy

    async def slow(model: str) -> dict[str, Any]:
        entered.set()
        await gate.wait()
        return await real(model)

    monkeypatch.setattr(aicore_deployments, "deploy", slow)
    request = asyncio.create_task(client.post(f"{BASE}/deploy", json={"model": SMALL}))
    await asyncio.wait_for(entered.wait(), 5)
    in_flight = list(embedding_routes._deploys)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert len(in_flight) == 1 and not in_flight[0].done()
    gate.set()
    await asyncio.wait_for(asyncio.gather(*in_flight), 5)
    assert {"model": SMALL, "status": "PENDING", "deployment_id": "d-3"} in aicore.deployments
    assert embedding_routes._deploys == set()


async def test_a_deploy_that_outlasts_the_wait_is_a_502_and_goes_on(
    monkeypatch, client, aicore, discovery
):
    gate = asyncio.Event()
    real = aicore.deploy

    async def slow(model: str) -> dict[str, Any]:
        await gate.wait()
        return await real(model)

    monkeypatch.setattr(aicore_deployments, "deploy", slow)
    monkeypatch.setattr(embedding_routes, "DEPLOY_WAIT_SECONDS", 0.05)
    r = await client.post(f"{BASE}/deploy", json={"model": SMALL})
    assert r.status_code == 502 and r.headers["X-Embeddings-Error"] == "deploy_failed"
    in_flight = list(embedding_routes._deploys)
    gate.set()
    await asyncio.wait_for(asyncio.gather(*in_flight), 5)
    assert any(d["model"] == SMALL for d in aicore.deployments)


async def test_a_timeout_inside_the_deploy_is_not_the_wait_running_out(
    monkeypatch, client, aicore, discovery, caplog
):
    async def timed_out(model: str) -> dict[str, Any]:
        raise TimeoutError

    monkeypatch.setattr(aicore_deployments, "deploy", timed_out)
    r = await client.post(f"{BASE}/deploy", json={"model": SMALL})
    assert r.status_code == 502 and r.headers["X-Embeddings-Error"] == "aicore_unavailable"
    assert "TimeoutError" in caplog.text


# --- the search mode ------------------------------------------------------------------------


async def test_the_admin_search_says_whether_vectors_ranked_it(
    monkeypatch, client, discovery, catalogue, embedder
):
    r = await client.post("/admin/api/data-catalog/search", json={"question": "order"})
    assert r.status_code == 200 and r.json()["mode"] == "lexical"  # a model, no vector yet
    assert r.json()["candidates"] and r.json()["scope"] == "all"
    for name in ("Order", "Item", "Release"):
        await add_vector(catalogue[name], LARGE)
    r = await client.post("/admin/api/data-catalog/search", json={"question": "order"})
    assert r.json()["mode"] == "vector"
    await store(0, None)
    embeddings.reset_caches()
    r = await client.post("/admin/api/data-catalog/search", json={"question": "order"})
    assert r.json()["mode"] == "lexical"


async def test_find_sources_answers_the_same_list_as_before(discovery, catalogue, embedder):
    for name in ("Order", "Item", "Release"):
        await add_vector(catalogue[name], LARGE)
    allowed = {("odata", "sales")}
    async with SessionLocal() as db:
        found = await search.find_sources(db, "order", allowed_sources=allowed, tier="basic")
        detail, mode = await search.find_sources_detail(
            db, "order", allowed_sources=allowed, tier="basic")
        _, lexical = await search.find_sources_detail(
            db, "order", allowed_sources=allowed, tier="basic", use_vectors=False)
        _, empty = await search.find_sources_detail(
            db, "order", allowed_sources=set(), tier="basic")
    assert isinstance(found, list) and found == detail and mode == "vector"
    assert (lexical, empty) == ("lexical", "lexical")


# --- the routes as such ----------------------------------------------------------------------


class _StubValidator:
    def validate(self, token: str) -> dict[str, Any]:
        claims = {"usr": {"user_name": "u", "scope": ["user"]},
                  "dev": {"user_name": "d", "scope": ["developer"]}}
        if token not in claims:
            raise HTTPException(status_code=401, detail="Invalid token")
        return claims[token]

    def has_scope(self, payload: dict[str, Any], scope: str) -> bool:
        return scope in (payload.get("scope") or [])


async def test_every_embedding_route_requires_the_admin_scope(monkeypatch, aicore):
    from agents.auth import require_admin

    routes = embedding_routes.router.routes
    for route in routes:
        assert require_admin in [d.call for d in route.dependant.dependencies], route.path
    calls = sorted((m, route.path) for route in routes for m in route.methods - {"HEAD"})
    assert calls == [("GET", BASE.removeprefix("/admin")), ("POST", f"{BASE}/deploy".removeprefix(
        "/admin")), ("POST", f"{BASE}/run".removeprefix("/admin")),
        ("PUT", BASE.removeprefix("/admin"))]
    monkeypatch.setattr(auth, "get_validator", lambda: _StubValidator())
    token = auth.current_claims.set(None)
    try:
        bare = FastAPI()
        bare.include_router(embedding_routes.router, prefix="/admin")
        async with AsyncClient(transport=ASGITransport(app=bare), base_url="http://t") as c:
            for method, path in calls:
                kwargs = {} if method == "GET" else {"json": {"model": SMALL}}
                r = await c.request(method, f"/admin{path}", **kwargs)
                assert r.status_code == 401, (method, path)
                for bearer in ("usr", "dev"):
                    r = await c.request(method, f"/admin{path}",
                                        headers={"Authorization": f"Bearer {bearer}"}, **kwargs)
                    assert r.status_code == 403, (method, path, bearer)
    finally:
        auth.current_claims.reset(token)
    assert aicore.calls == []
