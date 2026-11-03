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

The routes of point 2 are in ``tests/test_data_catalog_embedding_routes.py``,
which reuses the fixtures here.

No network: discovery is patched (``agents.shared._discover_deployed_models``,
or, where its refresh matters, gen_ai_hub's proxy client: ``ProxyClient``)
and the suite's ``DATA_CATALOG_EMBEDDINGS=0`` (``tests/conftest.py``) is set
to ``1`` here only where a rung needs it.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()

import pytest  # noqa: E402
from sqlalchemy import select, update  # noqa: E402

import agents.shared as shared  # noqa: E402
from agents.data_catalog import embeddings  # noqa: E402
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
    """Stands in for AI Core's list of RUNNING deployments; counts the calls
    and those that asked for a refresh."""

    def __init__(self, models: list[str]) -> None:
        self.models = list(models)
        self.calls = 0
        self.refreshes = 0

    def __call__(self, refresh: bool = False) -> list[str]:
        self.calls += 1
        self.refreshes += refresh
        return list(self.models)


class ProxyClient:
    """Behaves like the client ``gen_ai_hub.proxy.get_proxy_client("gen-ai-hub")``
    hands out (sap-ai-sdk-gen 6.6.0; pinned by
    ``test_the_proxy_client_is_one_instance_that_keeps_its_list``): ONE
    instance per process, whose ``deployments`` are read from AI Core
    (``live``: the models with a RUNNING deployment there) only while its
    list is empty; ``update_deployments()`` reads them again. ``install``
    makes ``get_proxy_client`` answer it, so the real
    ``agents.shared._discover_deployed_models`` runs."""

    def __init__(self, live: Callable[[], list[str]]) -> None:
        self._live = live
        self._deployments: list[SimpleNamespace] = []
        self.updates = 0

    def update_deployments(self) -> list[SimpleNamespace]:
        self.updates += 1
        self._deployments = [SimpleNamespace(model_name=m) for m in self._live()]
        return self._deployments

    @property
    def deployments(self) -> list[SimpleNamespace]:
        if not self._deployments:
            self.update_deployments()
        return self._deployments

    def install(self, monkeypatch) -> ProxyClient:
        import gen_ai_hub.proxy

        monkeypatch.setattr(gen_ai_hub.proxy, "get_proxy_client", lambda *a, **k: self)
        return self


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
    # Not part of the caches (a save does not make the proxy client's list
    # fresh): set by note_running, cleared by a refresh; no test leaves it.
    embeddings._refresh_next = False
    monkeypatch.setenv(embeddings.SWITCH_ENV, "1")
    monkeypatch.delenv(embeddings.MODEL_ENV, raising=False)
    yield
    await store(1, None)
    embeddings.reset_caches()
    embeddings._refresh_next = False


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


# --- the proxy client's list of deployments --------------------------------------------


def test_the_proxy_client_is_one_instance_that_keeps_its_list(tmp_path):
    """What ``ProxyClient`` copies from sap-ai-sdk-gen: ``get_proxy_client``
    answers the same instance every time (``InstanceCacheMeta``), and its
    ``deployments`` do not read AI Core again once they hold one;
    ``update_deployments()`` does. In a child process with made-up
    credentials and AI Core's list replaced: no network, and the instance
    made here is not the suite's."""
    code = textwrap.dedent("""
        from types import SimpleNamespace
        from gen_ai_hub.proxy import get_proxy_client
        from gen_ai_hub.proxy.gen_ai_hub_proxy.client import GenAIHubProxyClient

        live, reads = ["gpt-4o"], []

        def scenario(self, info):
            reads.append(1)
            return {m: SimpleNamespace(model_name=m, created_at=i) for i, m in enumerate(live)}

        GenAIHubProxyClient._get_scenario_deployments = scenario
        first = get_proxy_client("gen-ai-hub")
        print(first is get_proxy_client("gen-ai-hub"))
        print(",".join(d.model_name for d in first.deployments), len(reads))
        live.append("text-embedding-3-small")
        print(",".join(d.model_name for d in get_proxy_client("gen-ai-hub").deployments),
              len(reads))
        print(",".join(sorted(d.model_name for d in first.update_deployments())), len(reads))
    """)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "AICORE_HOME": str(tmp_path),
        "AICORE_CLIENT_ID": "test-client",
        "AICORE_CLIENT_SECRET": "made-up",
        "AICORE_AUTH_URL": "https://auth.invalid",
        "AICORE_BASE_URL": "https://api.invalid/v2",
        "AICORE_RESOURCE_GROUP": "default",
    }
    r = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        "True", "gpt-4o 1", "gpt-4o 1", "gpt-4o,text-embedding-3-small 2"]


def test_discovery_reads_the_proxy_clients_list_and_a_refresh_reads_ai_core(monkeypatch):
    """The chat's model list (the default) is what it was; ``refresh`` makes
    the one instance read its deployments again."""
    live = ["gpt-4o"]
    proxy = ProxyClient(lambda: list(live)).install(monkeypatch)
    assert shared._discover_deployed_models() == ["gpt-4o"]
    live.append(SMALL_MODEL)  # deployed later, RUNNING now
    assert shared._discover_deployed_models() == ["gpt-4o"]
    assert proxy.updates == 1
    assert shared._discover_deployed_models(refresh=True) == ["gpt-4o", SMALL_MODEL]
    assert shared._discover_deployed_models() == ["gpt-4o", SMALL_MODEL]
    assert proxy.updates == 2


async def test_a_running_model_discovery_does_not_list_makes_it_read_ai_core_again(
    monkeypatch,
):
    """A save resets our caches, not the proxy client's list: only AI Core's
    own list (``note_running``) tells that a model reached RUNNING, and then
    the next discovery reads the deployments again, once."""
    live = ["gpt-4o"]
    proxy = ProxyClient(lambda: list(live)).install(monkeypatch)
    proxy.update_deployments()  # the chat read it long ago
    assert await embeddings.resolve_model_detail() == (None, "none")
    live.append(SMALL_MODEL)
    embeddings.reset_caches()
    assert await embeddings.resolve_model_detail() == (None, "none")
    embeddings.note_running([SMALL_MODEL])
    assert await embeddings.resolve_model_detail() == (SMALL_MODEL, "discovery")
    updates = proxy.updates
    embeddings.note_running([SMALL_MODEL])  # agrees: nothing is read again
    assert await embeddings.resolve_model_detail() == (SMALL_MODEL, "discovery")
    assert proxy.updates == updates == 2


async def test_a_note_before_any_discovery_reads_ai_core_again(monkeypatch):
    """No discovery since the last save: what the proxy client holds is
    unknown, so a note that names RUNNING models makes the next one read."""
    live = ["gpt-4o"]
    proxy = ProxyClient(lambda: list(live)).install(monkeypatch)
    proxy.update_deployments()
    live.append(SMALL_MODEL)
    await store(1, SMALL_MODEL)
    embeddings.reset_caches()
    embeddings.note_running([SMALL_MODEL])
    assert await embeddings.resolve_model_detail() == (SMALL_MODEL, "setting")
    assert proxy.updates == 2


async def test_a_refresh_that_a_note_overtook_is_asked_again(monkeypatch):
    """The refresh is done once its answer is kept: a note that lands while
    it runs leaves the next discovery a refresh too."""
    found = Discovery(["gpt-4o"])

    def overtaken(refresh: bool = False) -> list[str]:
        models = found(refresh)
        embeddings.note_running([SMALL_MODEL])
        return models

    embeddings.note_running([SMALL_MODEL])
    monkeypatch.setattr(shared, "_discover_deployed_models", overtaken)
    assert await embeddings.resolve_model_detail() == (None, "none")
    found.models = ["gpt-4o", SMALL_MODEL]
    monkeypatch.setattr(shared, "_discover_deployed_models", found)
    assert await embeddings.resolve_model_detail() == (SMALL_MODEL, "discovery")
    assert found.refreshes == 2


# --- a run's check of the model in effect ---------------------------------------------------


LARGE_MODEL = "text-embedding-3-large"


@pytest.mark.parametrize("change", ["another admin model", "admin switched off",
                                    "environment switch off", "discovery picks another"])
async def test_a_definite_change_of_the_model_stops_the_run(monkeypatch, discovery, change):
    if change == "another admin model":
        await store(1, SMALL_MODEL)
    elif change == "admin switched off":
        await store(0, None)
    elif change == "environment switch off":
        monkeypatch.setenv(embeddings.SWITCH_ENV, "0")
    else:
        discovery.models = ["gpt-4o", SMALL_MODEL]
    embeddings.reset_caches()
    with pytest.raises(embeddings.ModelChanged):
        await embeddings._still_in_effect(LARGE_MODEL)


async def test_the_run_goes_on_while_its_model_is_in_effect(discovery):
    await embeddings._still_in_effect(LARGE_MODEL)
    await store(1, LARGE_MODEL)
    embeddings.reset_caches()
    await embeddings._still_in_effect(LARGE_MODEL)


@pytest.mark.parametrize("cause", ["setting unreadable", "discovery empty",
                                   "admin model no longer listed"])
async def test_a_check_that_cannot_tell_is_tried_again_once_then_the_run_goes_on(
    monkeypatch, discovery, caplog, cause
):
    """A failed read is no change: the run keeps its model (what it writes is
    of that model either way), after one more try."""
    reads = 0

    async def unreadable(session):
        nonlocal reads
        reads += 1
        raise RuntimeError(f"postgresql://app:{SECRET}@db.internal/registry")

    if cause == "setting unreadable":
        monkeypatch.setattr(embeddings, "get_embedding_setting", unreadable)
    elif cause == "discovery empty":
        discovery.models = []
    else:
        await store(1, LARGE_MODEL)
        discovery.models = ["gpt-4o"]
    embeddings.reset_caches()
    await embeddings._still_in_effect(LARGE_MODEL)
    if cause == "setting unreadable":
        assert reads == 2
    assert SECRET not in caplog.text


async def test_a_change_seen_by_the_second_try_stops_the_run(monkeypatch, discovery):
    real, reads = embeddings.get_embedding_setting, 0

    async def flaky(session):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise RuntimeError("connection reset")
        return await real(session)

    monkeypatch.setattr(embeddings, "get_embedding_setting", flaky)
    await store(1, SMALL_MODEL)
    embeddings.reset_caches()
    with pytest.raises(embeddings.ModelChanged):
        await embeddings._still_in_effect(LARGE_MODEL)
    assert reads == 2


def test_the_session_factory_is_looked_up_at_call_time():
    """``tests/test_hana_integration.py`` rebinds ``agents.db.SessionLocal``;
    a name bound at import would keep the first engine."""
    source = Path(embeddings.__file__).read_text()
    assert "from agents.db import SessionLocal" not in source
    assert "agents_db.SessionLocal()" in source
