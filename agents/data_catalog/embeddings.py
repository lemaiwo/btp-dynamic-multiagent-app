"""Embedding vectors of catalogue nodes, through SAP AI Core, optional.

The model is ``AICORE_EMBEDDING_MODEL`` when set, else the deployed model
``text-embedding-3-large`` when AI Core lists it, else the first deployed
model whose name contains ``embedding``, else none: then nothing is embedded
and the search ranks lexically. The call goes through the same
``gen_ai_hub`` proxy client as the chat models (``AsyncOpenAI(proxy_client=
...).embeddings.create``). Every failure (no credentials, no deployment, a
refused call, a timeout) means "no vectors", logged by exception class only.

``DATA_CATALOG_EMBEDDINGS=0`` switches embedding off (the test suite does;
a developer's ``.env`` may hold real AI Core credentials).

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

_model_cache: tuple[float, str | None] | None = None
_question_cache: OrderedDict[tuple[str, str], list[float]] = OrderedDict()


def enabled() -> bool:
    return os.environ.get(SWITCH_ENV, "1").strip() != "0"


def reset_caches() -> None:
    global _model_cache
    _model_cache = None
    _question_cache.clear()


def _pick(deployed: list[str]) -> str | None:
    if PREFERRED_MODEL in deployed:
        return PREFERRED_MODEL
    return next((m for m in deployed if "embedding" in m.lower()), None)


async def resolve_model() -> str | None:
    """The embedding model to use, or ``None``. Cached for 10 minutes,
    a ``None`` included (no discovery call per search)."""
    global _model_cache
    if not enabled():
        return None
    configured = (os.environ.get(MODEL_ENV) or "").strip()
    if configured:
        return configured
    now = time.monotonic()
    if _model_cache is not None and now - _model_cache[0] < MODEL_CACHE_SECONDS:
        return _model_cache[1]
    from agents.shared import _discover_deployed_models

    try:
        deployed = await asyncio.wait_for(
            asyncio.to_thread(_discover_deployed_models), DISCOVERY_TIMEOUT_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 - no vectors, see the module docstring
        logger.warning("data catalogue: model discovery failed (%s)", type(exc).__name__)
        deployed = []
    model = _pick(list(deployed or []))
    _model_cache = (now, model)
    return model


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
