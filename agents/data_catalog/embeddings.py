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
   deployed model whose name contains ``embedding`` (source ``discovery``);
   else none.

With no model nothing is embedded and the search ranks lexically. The
setting and the list of deployments are each cached for 10 minutes (a
search must not read the row or ask AI Core every time);
``agents.db.set_embedding_setting`` resets the caches after its commit. The
caches are per process: another app instance sees a save after its own 10
minutes. A setting that cannot be read means none, and is read again on the
next call (fail closed: an admin who switched embeddings off gets no call).

The call goes through the same ``gen_ai_hub`` proxy client as the chat
models (``AsyncOpenAI(proxy_client=...).embeddings.create``). Every failure
(no credentials, no deployment, a refused call, a timeout) means "no
vectors", logged by exception class only.

Only catalogue text is embedded (names, labels, purpose, synonyms,
examples), never a row of data. A vector is stored with the SHA-256 of the
text it was made from, so an unchanged node is never sent again.
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
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.data_catalog.models import DataCatalogNode, DataCatalogVector
from agents.db import SessionLocal, get_embedding_setting

logger = logging.getLogger(__name__)

MODEL_ENV = "AICORE_EMBEDDING_MODEL"
SWITCH_ENV = "DATA_CATALOG_EMBEDDINGS"
PREFERRED_MODEL = "text-embedding-3-large"
EMBEDDED_KINDS = ("source", "entity", "function", "query")
CALL_TIMEOUT_SECONDS = 30.0
DISCOVERY_TIMEOUT_SECONDS = 15.0
MODEL_CACHE_SECONDS = 600.0
BATCH_SIZE = 64
MAX_TEXT_CHARS = 2000
_QUESTION_CACHE_SIZE = 256

# Where the effective model came from (resolve_model_detail).
SOURCES = ("setting", "env", "discovery", "none")

# The clock of the two caches (a seam for the tests).
_clock = time.monotonic
_setting_cache: tuple[float, dict[str, Any]] | None = None
_deployed_cache: tuple[float, list[str]] | None = None
# Raised by every reset: a read that began before a reset is not cached,
# so a save that lands while a resolve awaits the row is seen next time.
_cache_epoch = 0
_question_cache: OrderedDict[tuple[str, str], list[float]] = OrderedDict()


def enabled() -> bool:
    return os.environ.get(SWITCH_ENV, "1").strip() != "0"


def reset_caches() -> None:
    global _setting_cache, _deployed_cache, _cache_epoch
    _setting_cache = None
    _deployed_cache = None
    _cache_epoch += 1
    _question_cache.clear()


def _pick(deployed: list[str]) -> str | None:
    if PREFERRED_MODEL in deployed:
        return PREFERRED_MODEL
    return next((m for m in deployed if "embedding" in m.lower()), None)


async def _setting() -> dict[str, Any] | None:
    """The admin's setting, cached for 10 minutes; ``None`` when it cannot
    be read (not cached, so the next call tries again)."""
    global _setting_cache
    now, epoch = _clock(), _cache_epoch
    if _setting_cache is not None and now - _setting_cache[0] < MODEL_CACHE_SECONDS:
        return _setting_cache[1]
    try:
        async with SessionLocal() as db:
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
    10 minutes, an empty list included (no discovery call per search)."""
    global _deployed_cache
    now, epoch = _clock(), _cache_epoch
    if _deployed_cache is not None and now - _deployed_cache[0] < MODEL_CACHE_SECONDS:
        return _deployed_cache[1]
    from agents.shared import _discover_deployed_models

    try:
        found = await asyncio.wait_for(
            asyncio.to_thread(_discover_deployed_models), DISCOVERY_TIMEOUT_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 - no vectors, see the module docstring
        logger.warning("data catalogue: model discovery failed (%s)", type(exc).__name__)
        found = []
    deployed = [m for m in (found or []) if isinstance(m, str)]
    if epoch == _cache_epoch:
        _deployed_cache = (now, deployed)
    return deployed


async def resolve_model_detail() -> tuple[str | None, str]:
    """``(model, source)``: the embedding model to use, or ``None``, and
    where it came from, one of :data:`SOURCES`, in the order of the module
    docstring. ``setting`` with ``None`` is an admin's model that AI Core
    does not list as deployed; switched off (by the environment or the
    admin) and nothing found are ``none``."""
    if not enabled():
        return None, "none"
    setting = await _setting()
    if setting is None or not setting["enabled"]:
        return None, "none"
    chosen = setting["model"]
    if chosen:
        return (chosen if chosen in await _deployed() else None), "setting"
    configured = (os.environ.get(MODEL_ENV) or "").strip()
    if configured:
        return configured, "env"
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


async def embed_texts(texts: list[str], model: str | None = None) -> list[list[float]] | None:
    """One vector per text, or ``None`` when no vectors can be had."""
    if not texts:
        return []
    model = model or await resolve_model()
    if not model:
        return None
    out: list[list[float]] = []
    try:
        for start in range(0, len(texts), BATCH_SIZE):
            chunk = [t[:MAX_TEXT_CHARS] for t in texts[start : start + BATCH_SIZE]]
            vectors = await asyncio.wait_for(_create(model, chunk), CALL_TIMEOUT_SECONDS)
            if len(vectors) != len(chunk):
                raise ValueError("vector count")
            out.extend(vectors)
    except Exception as exc:  # noqa: BLE001 - no vectors, see the module docstring
        logger.warning("data catalogue: embedding call failed (%s)", type(exc).__name__)
        return None
    return out


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


async def refresh_vectors(
    db: AsyncSession, *, scope: tuple[str, str] | None = None
) -> int:
    """Embed every embeddable node (of ``scope``, or all) whose text changed
    since its vector was made; drop vectors of another model. Returns how
    many vectors were written; 0 without a model. Does not commit."""
    model = await resolve_model()
    if not model:
        return 0
    query = select(DataCatalogNode).where(DataCatalogNode.kind.in_(EMBEDDED_KINDS))
    if scope is not None:
        query = query.where(
            DataCatalogNode.source_kind == scope[0], DataCatalogNode.source_name == scope[1]
        )
    nodes = list((await db.execute(query.order_by(DataCatalogNode.id))).scalars())
    if not nodes:
        return 0
    from agents.data_catalog.importer import batched

    stored: dict[int, str] = {}
    for chunk in batched([n.id for n in nodes]):
        rows = await db.execute(
            select(DataCatalogVector.node_id, DataCatalogVector.text_hash).where(
                DataCatalogVector.node_id.in_(chunk), DataCatalogVector.model == model
            )
        )
        stored.update({nid: h for nid, h in rows.all()})
    todo = []
    for node in nodes:
        text = node_text(node)
        digest = text_hash(text)
        if stored.get(node.id) != digest:
            todo.append((node.id, text, digest))
    if not todo:
        return 0
    vectors = await embed_texts([t for _, t, _ in todo], model)
    if vectors is None:
        return 0
    ids = [nid for nid, _, _ in todo]
    for chunk in batched(ids):
        await db.execute(delete(DataCatalogVector).where(DataCatalogVector.node_id.in_(chunk)))
    for (nid, _, digest), vector in zip(todo, vectors):
        db.add(DataCatalogVector(node_id=nid, model=model, text_hash=digest, vector=pack(vector)))
    await db.flush()
    return len(todo)
