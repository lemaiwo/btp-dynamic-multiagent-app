"""Embedding vectors of catalogue nodes, through SAP AI Core, optional.

Which model (design addendum (l), point 1; :func:`resolve_model_detail`
answers it with where it came from, for the admin's status):

1. ``DATA_CATALOG_EMBEDDINGS=0`` -> none (the test suite sets it; a
   developer's ``.env`` may hold real AI Core credentials; an emergency
   switch on a landscape);
2. the admin's setting off (``orchestrator_config.embeddings_enabled = 0``)
   -> none;
3. the admin's model (``embedding_model``) -> that model while AI Core lists
   a RUNNING deployment of it, else none: no fallback, the admin chose it
   (source ``setting`` either way);
4. ``AICORE_EMBEDDING_MODEL`` -> that model (source ``env``);
5. discovery: the deployed ``text-embedding-3-large``, else the first
   deployed model whose name contains ``embed`` in any case (source
   ``discovery``; ``agents.aicore_deployments`` lists embedding models by
   the same rule); else none.

With no model nothing is embedded and the search ranks lexically. The
setting and the list of deployments are each cached for 10 minutes (a
search must not read the row or ask AI Core every time), an empty or failed
list of deployments for one minute only (cached for ten, a failed discovery
would make the admin's model look undeployed and a run embed nothing);
``agents.db.set_embedding_setting`` resets the caches after its commit. The
caches are per process: another app instance sees a save after its own 10
minutes. A setting that cannot be read means none, and is read again on the
next call (fail closed: an admin who switched embeddings off gets no call).

Discovery (``agents.shared._discover_deployed_models``) reads the list of
gen_ai_hub's proxy client, ONE instance per process that reads AI Core only
while its list is empty: a deployment that reaches RUNNING later is not in
it, and resetting our caches does not change that. AI Core's own list
(``agents.aicore_deployments``, read by the admin's status, PUT and run)
does show it: :func:`note_running` compares, and when the two disagree the
next discovery makes the proxy client read its deployments again
(``refresh=True``); a refresh that fails is no answer, so the one after it
asks again.

The call goes through the same ``gen_ai_hub`` proxy client as the chat
models (``AsyncOpenAI(proxy_client=...).embeddings.create``). Every failure
(no credentials, no deployment, a refused call, a timeout) means "no
vectors", logged by exception class only.

Only catalogue text is embedded (names, labels, purpose, synonyms,
examples), never a row of data. A vector is stored with the SHA-256 of the
text it was made from, so an unchanged node is never sent again.

**One embed-and-write at a time per process** (:func:`refresh_vectors`): a
catalogue or hint save, the startup rebuild and the admin's run take one
lock around reading what is stale, embedding it and committing the vectors,
so two of them never send the same nodes (two bills) nor write the same
vectors from two transactions. The admin's run takes it per batch and reads
the batch's nodes again under it (what a save embedded meanwhile is not sent
again), so a save waits for one batch, not for the run. Another app instance
is not covered (ONE instance assumed, as by the startup sweep).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import time
from array import array
from collections import OrderedDict
from collections.abc import Callable, Iterable
from functools import partial
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

import agents.db as agents_db
from agents.data_catalog.models import DataCatalogNode, DataCatalogVector
from agents.db import get_embedding_setting

logger = logging.getLogger(__name__)

MODEL_ENV = "AICORE_EMBEDDING_MODEL"
SWITCH_ENV = "DATA_CATALOG_EMBEDDINGS"
PREFERRED_MODEL = "text-embedding-3-large"
EMBEDDED_KINDS = ("source", "entity", "function", "query")
CALL_TIMEOUT_SECONDS = 30.0
DISCOVERY_TIMEOUT_SECONDS = 15.0
MODEL_CACHE_SECONDS = 600.0
EMPTY_CACHE_SECONDS = 60.0
BATCH_SIZE = 64
MAX_TEXT_CHARS = 2000
_QUESTION_CACHE_SIZE = 256

# Where the effective model came from (resolve_model_detail).
SOURCES = ("setting", "env", "discovery", "none")

# The clock of the two caches (a seam for the tests).
_clock = time.monotonic
_setting_cache: tuple[float, dict[str, Any]] | None = None
_deployed_cache: tuple[float, float, list[str]] | None = None
# Raised by every reset: a read that began before a reset is not cached,
# so a save that lands while a resolve awaits the row is seen next time.
_cache_epoch = 0
# Set by note_running: the proxy client's list is stale or unknown, so the
# next discovery reads AI Core again. Cleared once a refresh's answer is
# kept; not by reset_caches (a save makes the proxy client no fresher).
_refresh_next = False
_question_cache: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
# The lock of refresh_vectors (see the module docstring), one per event loop:
# the app has one, the test suite one per test.
_write_lock: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None


def enabled() -> bool:
    return os.environ.get(SWITCH_ENV, "1").strip() != "0"


def reset_caches() -> None:
    global _setting_cache, _deployed_cache, _cache_epoch
    _setting_cache = None
    _deployed_cache = None
    _cache_epoch += 1
    _question_cache.clear()


def _writing() -> asyncio.Lock:
    global _write_lock
    loop = asyncio.get_running_loop()
    if _write_lock is None or _write_lock[0] is not loop:
        _write_lock = (loop, asyncio.Lock())
    return _write_lock[1]


def _pick(deployed: list[str]) -> str | None:
    if PREFERRED_MODEL in deployed:
        return PREFERRED_MODEL
    return next((m for m in deployed if "embed" in m.casefold()), None)


async def _setting() -> dict[str, Any] | None:
    """The admin's setting, cached for 10 minutes; ``None`` when it cannot
    be read (not cached, so the next call tries again)."""
    global _setting_cache
    now, epoch = _clock(), _cache_epoch
    if _setting_cache is not None and now - _setting_cache[0] < MODEL_CACHE_SECONDS:
        return _setting_cache[1]
    try:
        # Looked up at call time: a test (and the HANA suite) rebinds it.
        async with agents_db.SessionLocal() as db:
            setting = await get_embedding_setting(db)
    except Exception as exc:  # noqa: BLE001 - no vectors, see the module docstring
        logger.warning(
            "data catalogue: embedding setting unreadable (%s)", type(exc).__name__)
        return None
    if epoch == _cache_epoch:
        _setting_cache = (now, setting)
    return setting


async def _deployed() -> list[str]:
    """The model names AI Core lists with a RUNNING deployment, cached for
    10 minutes; an empty list, a failed discovery included, for one minute
    (no discovery call per search either way). After :func:`note_running`
    asked for it, discovery reads AI Core again first, until such a refresh
    has answered. Never raises."""
    global _deployed_cache, _refresh_next
    now, epoch = _clock(), _cache_epoch
    if _deployed_cache is not None and now - _deployed_cache[0] < _deployed_cache[1]:
        return _deployed_cache[2]
    refresh, answered = _refresh_next, False
    try:
        from agents.shared import _discover_deployed_models

        # Only a refresh passes the argument: the chat's call is unchanged.
        call = partial(_discover_deployed_models, refresh=True) if refresh else (
            _discover_deployed_models)
        found = await asyncio.wait_for(asyncio.to_thread(call), DISCOVERY_TIMEOUT_SECONDS)
        deployed = [m for m in (found or []) if isinstance(m, str)]
        # ``None`` is a refresh that failed (``agents.shared`` logged it).
        answered = found is not None
    except Exception as exc:  # noqa: BLE001 - no vectors, see the module docstring
        logger.warning("data catalogue: model discovery failed (%s)", type(exc).__name__)
        deployed = []
    # A refresh that failed is asked again, and so is one that a note
    # overtook (it asked for a refresh after this one).
    if refresh and answered and epoch == _cache_epoch:
        _refresh_next = False
    if epoch == _cache_epoch:
        ttl = MODEL_CACHE_SECONDS if deployed else EMPTY_CACHE_SECONDS
        _deployed_cache = (now, ttl, deployed)
    return deployed


def note_running(running: Iterable[str]) -> None:
    """AI Core's own list of deployments (``agents.aicore_deployments``, read
    by the admin's status, PUT and run) names these embedding models
    RUNNING. When discovery's last answer disagrees about the embedding
    models (one reached RUNNING after a deploy, or one stopped), or there is
    none since the last reset, that answer is dropped and the next discovery
    makes gen_ai_hub's proxy client read AI Core again (see the module
    docstring): the admin who sees RUNNING must not get "no model in effect"
    for as long as the process lives. While the two agree nothing is read
    again."""
    global _deployed_cache, _cache_epoch, _refresh_next
    named = {m for m in running if isinstance(m, str)}
    if _deployed_cache is not None and named == {
        m for m in _deployed_cache[2] if "embed" in m.casefold()
    }:
        return
    _deployed_cache = None
    _cache_epoch += 1
    _refresh_next = True


async def _rung() -> tuple[str, str | None] | None:
    """The rung of the module docstring that decides, before AI Core is
    asked: ``("off", None)``, ``("setting", model)``, ``("env", model)`` or
    ``("discovery", None)``; ``None`` when the setting cannot be read."""
    if not enabled():
        return "off", None
    setting = await _setting()
    if setting is None:
        return None
    if not setting["enabled"]:
        return "off", None
    if setting["model"]:
        return "setting", setting["model"]
    configured = (os.environ.get(MODEL_ENV) or "").strip()
    return ("env", configured) if configured else ("discovery", None)


async def resolve_model_detail() -> tuple[str | None, str]:
    """``(model, source)``: the embedding model to use, or ``None``, and
    where it came from, one of :data:`SOURCES`, in the order of the module
    docstring. ``setting`` with ``None`` is an admin's model that AI Core
    does not list as deployed; switched off (by the environment or the
    admin), an unreadable setting and nothing found are ``none``."""
    rung = await _rung()
    if rung is None or rung[0] == "off":
        return None, "none"
    kind, named = rung
    if kind == "setting":
        return (named if named in await _deployed() else None), "setting"
    if kind == "env":
        return named, "env"
    model = _pick(await _deployed())
    return model, ("discovery" if model else "none")


async def resolve_model() -> str | None:
    """The embedding model to use, or ``None`` (see the module docstring)."""
    return (await resolve_model_detail())[0]


async def _create(model: str, texts: list[str]) -> list[list[float]]:
    from gen_ai_hub.proxy import get_proxy_client
    from gen_ai_hub.proxy.native.openai import AsyncOpenAI

    client = AsyncOpenAI(proxy_client=get_proxy_client("gen-ai-hub"))
    response = await client.embeddings.create(model=model, input=texts)
    data = sorted(response.data, key=lambda d: d.index)
    return [list(d.embedding) for d in data]


async def _embed_batches(texts: list[str], model: str) -> list[list[float]]:
    """One vector per text, in batches of :data:`BATCH_SIZE`; raises on any
    failure."""
    out: list[list[float]] = []
    for start in range(0, len(texts), BATCH_SIZE):
        chunk = [t[:MAX_TEXT_CHARS] for t in texts[start : start + BATCH_SIZE]]
        vectors = await asyncio.wait_for(_create(model, chunk), CALL_TIMEOUT_SECONDS)
        if len(vectors) != len(chunk):
            raise ValueError("vector count")
        out.extend(vectors)
    return out


async def embed_texts(texts: list[str], model: str | None = None) -> list[list[float]] | None:
    """One vector per text, or ``None`` when no vectors can be had."""
    if not texts:
        return []
    model = model or await resolve_model()
    if not model:
        return None
    try:
        return await _embed_batches(texts, model)
    except Exception as exc:  # noqa: BLE001 - no vectors, see the module docstring
        logger.warning("data catalogue: embedding call failed (%s)", type(exc).__name__)
        return None


async def embed_question(question: str, model: str) -> list[float] | None:
    key = (model, question)
    cached = _question_cache.get(key)
    if cached is not None:
        _question_cache.move_to_end(key)
        return cached
    vectors = await embed_texts([question], model)
    if not vectors:
        return None
    _question_cache[key] = vectors[0]
    while len(_question_cache) > _QUESTION_CACHE_SIZE:
        _question_cache.popitem(last=False)
    return vectors[0]


def pack(vector: list[float]) -> bytes:
    return array("f", vector).tobytes()


def unpack(raw: bytes) -> list[float]:
    values = array("f")
    values.frombytes(raw)
    return values.tolist()


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def node_text(node: Any) -> str:
    """What is embedded of a node: catalogue text only."""
    parts = [node.name, node.label or "", node.purpose or ""]
    parts.extend(node.synonyms)
    parts.extend(node.examples)
    return " | ".join(p for p in parts if p)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _stored_hashes(db: AsyncSession, ids: list[int], model: str) -> dict[int, str]:
    from agents.data_catalog.importer import batched

    stored: dict[int, str] = {}
    for chunk in batched(ids):
        rows = await db.execute(
            select(DataCatalogVector.node_id, DataCatalogVector.text_hash).where(
                DataCatalogVector.node_id.in_(chunk), DataCatalogVector.model == model
            )
        )
        stored.update({nid: h for nid, h in rows.all()})
    return stored


def _changed(nodes: list[DataCatalogNode], stored: dict[int, str]) -> list[tuple[int, str, str]]:
    """``(id, text, hash)`` of every node whose vector is missing or was
    made from another text: the one rule of what a refresh sends and of
    what the admin's status counts as stale."""
    todo = []
    for node in nodes:
        text = node_text(node)
        digest = text_hash(text)
        if stored.get(node.id) != digest:
            todo.append((node.id, text, digest))
    return todo


async def _embeddable(
    db: AsyncSession, scope: tuple[str, str] | None = None
) -> list[DataCatalogNode]:
    query = select(DataCatalogNode).where(DataCatalogNode.kind.in_(EMBEDDED_KINDS))
    if scope is not None:
        query = query.where(
            DataCatalogNode.source_kind == scope[0], DataCatalogNode.source_name == scope[1]
        )
    return list((await db.execute(query.order_by(DataCatalogNode.id))).scalars())


async def vector_counts(db: AsyncSession, model: str | None) -> dict[str, int]:
    """``{nodes, vectors, stale}`` for the admin's status: the embeddable
    nodes, those with a vector of ``model``, and those without one or with
    one of an older text (all of them without a model)."""
    nodes = await _embeddable(db)
    if not model:
        return {"nodes": len(nodes), "vectors": 0, "stale": len(nodes)}
    stored = await _stored_hashes(db, [n.id for n in nodes], model)
    return {"nodes": len(nodes), "vectors": len(stored), "stale": len(_changed(nodes, stored))}


class ModelChanged(Exception):
    """The admin's run: the model in effect is no longer the one the run
    embeds with (a save changed it or switched embeddings off meanwhile)."""


async def _write(
    db: AsyncSession,
    todo: list[tuple[int, str, str]],
    vectors: list[list[float]],
    model: str,
    *,
    every_model: bool,
) -> None:
    """The new vectors of ``todo``, in place of the nodes' older vector of
    ``model``; with ``every_model`` (the import path, one transaction) their
    vectors of other models go too. The admin's run commits batch by batch
    and leaves the other models' vectors to the drop at its end: a run that
    fails or stops keeps every node the vector of the model it had (it may be
    the one in effect again, after a save switched back)."""
    from agents.data_catalog.importer import batched

    for chunk in batched([nid for nid, _, _ in todo]):
        query = delete(DataCatalogVector).where(DataCatalogVector.node_id.in_(chunk))
        if not every_model:
            query = query.where(DataCatalogVector.model == model)
        await db.execute(query)
    for (nid, _, digest), vector in zip(todo, vectors):
        db.add(DataCatalogVector(node_id=nid, model=model, text_hash=digest, vector=pack(vector)))


async def _in_effect(model: str) -> bool | None:
    """Whether ``model`` is still the one in effect: ``False`` only for a
    definite change (switched off, or another model named or picked),
    ``None`` when it cannot be told (the setting unreadable, discovery
    empty). The admin's or the environment's model is compared by name:
    whether AI Core still lists it is not the run's question (its next call
    fails if it is gone)."""
    rung = await _rung()
    if rung is None:
        return None
    kind, named = rung
    if kind == "off":
        return False
    if kind != "discovery":
        return named == model
    picked = _pick(await _deployed())
    return None if picked is None else picked == model


async def _still_in_effect(model: str) -> bool:
    """``True`` while ``model`` is the one in effect; raises
    :class:`ModelChanged` when a save changed the model or switched
    embeddings off. A check that cannot tell is made once more; still not
    told, ``False``: the run goes on with its model (what it writes is of
    that model either way, and a run of a model no longer in effect is
    dropped by the next one), but it drops no other model's vectors (one of
    them may be the model in effect)."""
    for _ in range(2):
        verdict = await _in_effect(model)
        if verdict is False:
            raise ModelChanged()
        if verdict:
            return True
    logger.warning(
        "data catalogue: the model in effect could not be read; the run goes on with %s", model)
    return False


async def _nodes(db: AsyncSession, ids: list[int]) -> list[DataCatalogNode]:
    """The nodes of ``ids`` (at most a batch) as stored now: the session's
    copies of an earlier read are refreshed, so a text a save changed
    counts, and a node a save removed is gone."""
    query = (
        select(DataCatalogNode)
        .where(DataCatalogNode.id.in_(ids))
        .order_by(DataCatalogNode.id)
        .execution_options(populate_existing=True)
    )
    return list((await db.execute(query)).scalars())


async def _drop_other_models(db: AsyncSession, model: str) -> None:
    # The search reads the vectors of the effective model only: those of a
    # model switched away from are dead weight.
    await db.execute(delete(DataCatalogVector).where(DataCatalogVector.model != model))


async def _refresh_in_batches(
    db: AsyncSession,
    scope: tuple[str, str] | None,
    model: str,
    on_progress: Callable[[int, int], None] | None,
) -> int:
    """The admin's run of :func:`refresh_vectors` (``commit_batches``)."""
    nodes = await _embeddable(db, scope)
    todo = _changed(nodes, await _stored_hashes(db, [n.id for n in nodes], model))
    if on_progress is not None:
        on_progress(0, len(todo))
    written = 0
    for start in range(0, len(todo), BATCH_SIZE):
        ids = [nid for nid, _, _ in todo[start : start + BATCH_SIZE]]
        async with _writing():
            # Read again under the lock: a save may have embedded a node of
            # this batch, changed its text or removed it since the run began.
            chunk = _changed(await _nodes(db, ids), await _stored_hashes(db, ids, model))
            if chunk:
                vectors = await _embed_batches([t for _, t, _ in chunk], model)
                await _still_in_effect(model)
                await _write(db, chunk, vectors, model, every_model=False)
                await db.commit()
                written += len(chunk)
        if on_progress is not None:
            on_progress(start + len(ids), len(todo))
    if scope is None:
        async with _writing():
            if await _still_in_effect(model):
                await _drop_other_models(db, model)
                await db.commit()
            else:
                logger.info("data catalogue: the vectors of other models are kept")
    return written


async def refresh_vectors(
    db: AsyncSession,
    *,
    scope: tuple[str, str] | None = None,
    model: str | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    commit_batches: bool = False,
) -> int:
    """Embed every embeddable node (of ``scope``, or all) whose text changed
    since its vector was made; drop vectors of another model (a node's, when
    the import path sends it again; every one, after a refresh of all nodes
    that did not fail). Returns how many vectors were written; 0 without a
    model. Under the lock of the module docstring.

    The import and startup path (the default) writes in one transaction,
    which it commits before it lets the lock go (a refresh that waited for
    the lock reads what this one wrote); an embedding failure writes nothing
    and answers 0.

    The admin's run (``agents.data_catalog.embedding_run``) passes ``model``
    (resolved once, named in its ``last_run``), ``on_progress(done, total)``
    (called once the nodes to send are known and after each batch: ``done``
    counts the nodes dealt with, sent by the run or found current by then)
    and ``commit_batches``: each batch is written and COMMITTED as soon as it
    is embedded, so a failure at batch 70 of 80 keeps the 69 that were paid
    for and the next run sends only what is still stale; a batch replaces
    vectors of the run's model only. An embedding failure is raised (the run
    reports its class). Before each write and before the drop of other models
    the model is checked again (:func:`_still_in_effect`): when a save changed
    it, :class:`ModelChanged` is raised and nothing more is written or
    dropped; a check that cannot tell is no change for the writes, and the
    drop is then skipped (the next run that can tell makes it).
    """
    model = model or await resolve_model()
    if not model:
        return 0
    if commit_batches:
        return await _refresh_in_batches(db, scope, model, on_progress)
    async with _writing():
        nodes = await _embeddable(db, scope)
        todo = _changed(nodes, await _stored_hashes(db, [n.id for n in nodes], model))
        if on_progress is not None:
            on_progress(0, len(todo))
        if todo:
            found = await embed_texts([t for _, t, _ in todo], model)
            if found is None:
                return 0
            await _write(db, todo, found, model, every_model=True)
        if scope is None:
            await _drop_other_models(db, model)
        await db.commit()
    return len(todo)
